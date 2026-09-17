resource "aws_ecs_cluster" "this" {
  name = local.name
}

resource "aws_ecs_cluster_capacity_providers" "this" {
  cluster_name       = aws_ecs_cluster.this.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${local.name}"
  retention_in_days = var.log_retention_days
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Fargate task"
  vpc_id      = aws_vpc.this.id

  # インターネットからは一切受けない。ALB からのみ。
  ingress {
    description     = "from ALB"
    from_port       = var.container_port
    to_port         = var.container_port
    protocol        = "tcp"
    security_groups = [aws_security_group.alb.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${local.name}-task" }
}

resource "aws_ecs_task_definition" "this" {
  family                   = local.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = local.persistent.task_execution_role_arn
  task_role_arn            = local.persistent.task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64" # Graviton。x86 より約2割安い
  }

  container_definitions = jsonencode([{
    name      = local.name
    image     = "${local.persistent.ecr_repository_url}:${var.image_tag}"
    essential = true

    portMappings = [{
      containerPort = var.container_port
      protocol      = "tcp"
    }]

    environment = [
      { name = "AWS_REGION", value = var.region },
      { name = "BEDROCK_MODEL_ID", value = var.model_id },
      { name = "CONVERSATIONS_TABLE", value = local.persistent.conversations_table_name },
      { name = "PORT", value = tostring(var.container_port) },
    ]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.app.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "app"
      }
    }
  }])
}

resource "aws_ecs_service" "this" {
  name            = local.name
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count

  capacity_provider_strategy {
    capacity_provider = var.use_fargate_spot ? "FARGATE_SPOT" : "FARGATE"
    weight            = 1
  }

  network_configuration {
    # 構成B / C ではプライベートサブネット、常用構成A では
    # パブリックサブネットに置いてパブリックIPで外に出る。
    subnets          = local.private_mode ? aws_subnet.private[*].id : [aws_subnet.public[0].id]
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = !local.private_mode
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = local.name
    container_port   = var.container_port
  }

  # イメージを push し直しただけで作り直せるようにしておく
  force_new_deployment = true

  depends_on = [aws_lb_listener.http]
}
