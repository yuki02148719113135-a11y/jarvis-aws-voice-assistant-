variable "project" {
  type    = string
  default = "jarvis"
}

variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "tfstate_bucket" {
  description = "persistent の state を読むためのバケット名"
  type        = string
}

# ---------------------------------------------------------------
# Issue 4〜6 の比較検証スイッチ
#
#   構成A（常用）  : 両方 false → タスクはパブリックサブネット + パブリックIP
#   構成B（検証用）: use_vpc_endpoints = true → プライベート + インターフェース4本
#   構成C（検証用）: use_nat = true          → プライベート + NAT ゲートウェイ
#
# 両方 true にしても壊れないが、料金が二重にかかるだけなので意味はない。
# ---------------------------------------------------------------

variable "use_vpc_endpoints" {
  type    = bool
  default = false
}

variable "use_nat" {
  type    = bool
  default = false
}

# ---------------------------------------------------------------
# ネットワーク
# ---------------------------------------------------------------

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "public_az_count" {
  description = "ALB が最低2AZを要求するため 2 未満にはできない"
  type        = number
  default     = 2

  validation {
    condition     = var.public_az_count >= 2
    error_message = "ALB には異なる AZ のサブネットが2つ以上必要です。"
  }
}

variable "allowed_cidr" {
  description = "ALB への接続を許可する送信元。自宅IPに絞るのを推奨"
  type        = string
  default     = "0.0.0.0/0"
}

# ---------------------------------------------------------------
# アプリケーション
# ---------------------------------------------------------------

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "container_port" {
  type    = number
  default = 8080
}

variable "desired_count" {
  description = "0 にするとタスク課金だけ止まる（ALB は課金され続ける点に注意）"
  type        = number
  default     = 1
}

variable "task_cpu" {
  type    = number
  default = 512
}

variable "task_memory" {
  type    = number
  default = 1024
}

variable "use_fargate_spot" {
  type    = bool
  default = true
}

variable "model_id" {
  description = "Issue 3 の疎通確認で確定した実モデルIDに差し替える"
  type        = string
  default     = "amazon.nova-2-sonic-v1:0"
}

variable "log_retention_days" {
  type    = number
  default = 7
}
