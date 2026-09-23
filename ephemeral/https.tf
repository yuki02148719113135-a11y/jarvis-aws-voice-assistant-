# ---------------------------------------------------------------
# HTTPS リスナー（検証用）
#
# ブラウザのマイクはセキュアコンテキストでしか使えない。localhost は例外だが、
# ALB 経由で試すには TLS が要る。独自ドメインがなく ACM の発行が使えないため、
# 自己署名証明書をインポートして載せている。ブラウザには警告が出る。
#
# certificate_arn が空のときは 443 を作らない（HTTP だけで立てられる）。
# ---------------------------------------------------------------

variable "certificate_arn" {
  description = "ACM にインポートした証明書の ARN。空なら HTTPS を作らない"
  type        = string
  default     = ""
}

locals {
  enable_https = var.certificate_arn != ""
}

resource "aws_lb_listener" "https" {
  count             = local.enable_https ? 1 : 0
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"

  # 自己署名なので、ポリシーは新しいものを選んでおく。
  ssl_policy      = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}

output "https_url" {
  description = "検証用の HTTPS エンドポイント（証明書の警告は想定内）"
  value       = local.enable_https ? "https://${aws_lb.this.dns_name}" : "（証明書未設定のため HTTP のみ）"
}
