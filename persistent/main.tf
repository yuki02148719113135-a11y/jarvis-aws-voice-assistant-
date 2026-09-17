terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # 初回 apply 時はこのブロックをコメントアウトしたまま実行する。
  # バケット作成後に有効化して `terraform init -migrate-state` で移行する。
  backend "s3" {
    bucket       = "jarvis-tfstate-985090322936"
    key          = "persistent/terraform.tfstate"
    region       = "ap-northeast-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = local.region

  default_tags {
    tags = {
      Project   = local.project
      Layer     = "persistent"
      ManagedBy = "terraform"
    }
  }
}

locals {
  project = "jarvis"
  region  = "ap-northeast-1"
}

data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------
# tfstate 置き場
# ---------------------------------------------------------------

resource "aws_s3_bucket" "tfstate" {
  bucket = "${local.project}-tfstate-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ---------------------------------------------------------------
# 会話の記憶
# ---------------------------------------------------------------

resource "aws_dynamodb_table" "conversations" {
  name         = "${local.project}-conversations"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "session_id"
  range_key    = "created_at"

  attribute {
    name = "session_id"
    type = "S"
  }

  attribute {
    name = "created_at"
    type = "N"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }
}

# ---------------------------------------------------------------
# コンテナイメージ
# ---------------------------------------------------------------

resource "aws_ecr_repository" "app" {
  name                 = local.project
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = false
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "直近5世代のみ保持"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}

# ---------------------------------------------------------------
# IAM
# ---------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# 実行ロール：ECS がイメージを pull し、ログを送るために使う
resource "aws_iam_role" "task_execution" {
  name               = "${local.project}-task-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "task_execution" {
  role       = aws_iam_role.task_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# タスクロール：アプリ本体が AWS API を叩くために使う
resource "aws_iam_role" "task" {
  name               = "${local.project}-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "task" {
  statement {
    sid = "InvokeNovaSonic"

    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithBidirectionalStream",
    ]

    resources = [
      "arn:aws:bedrock:${local.region}::foundation-model/amazon.nova-2-sonic*",
    ]
  }

  statement {
    sid = "ConversationMemory"

    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
    ]

    resources = [aws_dynamodb_table.conversations.arn]
  }
}

resource "aws_iam_role_policy" "task" {
  name   = "${local.project}-task"
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.task.json
}