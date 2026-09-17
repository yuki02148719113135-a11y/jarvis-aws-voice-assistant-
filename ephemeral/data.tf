data "aws_availability_zones" "this" {
  state = "available"
}

# persistent 層の outputs を読む。ARN やテーブル名をここ以外で
# ハードコードしないこと。これがあるから ephemeral を何度 destroy
# しても記憶とイメージが残る。
data "terraform_remote_state" "persistent" {
  backend = "s3"

  config = {
    bucket = var.tfstate_bucket
    key    = "persistent/terraform.tfstate"
    region = var.region
  }
}

locals {
  name = var.project

  persistent = data.terraform_remote_state.persistent.outputs

  # エンドポイントか NAT のどちらかを使うときだけ、タスクを
  # プライベートサブネットへ置く。どちらも使わない常用構成では
  # パブリックサブネット + パブリックIP で外に出る。
  private_mode = var.use_vpc_endpoints || var.use_nat

  azs = slice(data.aws_availability_zones.this.names, 0, var.public_az_count)
}
