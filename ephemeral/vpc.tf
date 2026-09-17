resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id

  tags = { Name = local.name }
}

# ---------------------------------------------------------------
# パブリックサブネット（ALB 用に必ず2AZ以上）
# ---------------------------------------------------------------

resource "aws_subnet" "public" {
  count = var.public_az_count

  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, count.index)
  availability_zone       = local.azs[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-public-${count.index}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "${local.name}-public" }
}

resource "aws_route_table_association" "public" {
  count = var.public_az_count

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# ---------------------------------------------------------------
# プライベートサブネット（構成B / C のときだけ作る。1AZで足りる）
# ---------------------------------------------------------------

resource "aws_subnet" "private" {
  count = local.private_mode ? 1 : 0

  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, 10)
  availability_zone = local.azs[0]

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table" "private" {
  count = local.private_mode ? 1 : 0

  vpc_id = aws_vpc.this.id

  tags = { Name = "${local.name}-private" }
}

resource "aws_route_table_association" "private" {
  count = local.private_mode ? 1 : 0

  subnet_id      = aws_subnet.private[0].id
  route_table_id = aws_route_table.private[0].id
}

# ---------------------------------------------------------------
# 構成C：NAT ゲートウェイ
# ---------------------------------------------------------------

resource "aws_eip" "nat" {
  count  = var.use_nat ? 1 : 0
  domain = "vpc"

  tags = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "this" {
  count = var.use_nat ? 1 : 0

  allocation_id = aws_eip.nat[0].id
  subnet_id     = aws_subnet.public[0].id

  tags = { Name = local.name }

  depends_on = [aws_internet_gateway.this]
}

resource "aws_route" "private_nat" {
  count = var.use_nat ? 1 : 0

  route_table_id         = aws_route_table.private[0].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.this[0].id
}

# ---------------------------------------------------------------
# ゲートウェイ型エンドポイント（無料。常に作る）
# ---------------------------------------------------------------

resource "aws_vpc_endpoint" "gateway" {
  for_each = toset(["s3", "dynamodb"])

  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type = "Gateway"

  route_table_ids = compact([
    aws_route_table.public.id,
    try(aws_route_table.private[0].id, ""),
  ])

  tags = { Name = "${local.name}-${each.key}" }
}

# ---------------------------------------------------------------
# 構成B：インターフェース型エンドポイント（1本あたり約8.6円/時）
# ---------------------------------------------------------------

resource "aws_security_group" "endpoint" {
  count = var.use_vpc_endpoints ? 1 : 0

  name        = "${local.name}-endpoint"
  description = "VPC endpoints"
  vpc_id      = aws_vpc.this.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  tags = { Name = "${local.name}-endpoint" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = var.use_vpc_endpoints ? toset([
    "bedrock-runtime",
    "ecr.api",
    "ecr.dkr",
    "logs",
  ]) : toset([])

  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private[0].id]
  security_group_ids  = [aws_security_group.endpoint[0].id]
  private_dns_enabled = true

  tags = { Name = "${local.name}-${each.key}" }
}
