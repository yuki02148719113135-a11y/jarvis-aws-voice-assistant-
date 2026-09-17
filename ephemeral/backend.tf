terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # 部分設定にしている。バケット名は persistent の output から渡す:
  #
  #   terraform init \
  #     -backend-config="bucket=$(cd ../persistent && terraform output -raw tfstate_bucket)" \
  #     -backend-config="key=ephemeral/terraform.tfstate" \
  #     -backend-config="region=ap-northeast-1" \
  #     -backend-config="encrypt=true" \
  #     -backend-config="use_lockfile=true"
  backend "s3" {}
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = var.project
      Layer     = "ephemeral"
      ManagedBy = "terraform"
    }
  }
}
