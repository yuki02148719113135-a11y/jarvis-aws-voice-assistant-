# ---------------------------------------------------------------
# Cognito ユーザープール
#
# persistent 側に置く。ephemeral に置くと消すたびにユーザーが消え、
# 登録し直しになる。月5万アクティブユーザーまで無料。
#
# ALB の認証機能（authenticate-cognito アクション）を使う手もあるが、
# リダイレクト先に正式な証明書が要る。自己署名で検証している構成では
# 使えないため、アプリ側でトークンを検証する形にした。
# ---------------------------------------------------------------

variable "cognito_callback_urls" {
  description = "ログイン後の戻り先。ALB を立てたら URL を足す"
  type        = list(string)
  default     = ["http://localhost:8080/"]
}

resource "aws_cognito_user_pool" "this" {
  name = local.project

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length  = 8
    require_numbers = true
    require_symbols = false
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_client" "this" {
  name         = local.project
  user_pool_id = aws_cognito_user_pool.this.id

  # ブラウザだけで動くため秘密鍵を持たせない。
  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  callback_urls = var.cognito_callback_urls
  logout_urls   = var.cognito_callback_urls

  allowed_oauth_flows                  = ["implicit"]
  allowed_oauth_scopes                 = ["openid", "email"]
  allowed_oauth_flows_user_pool_client = true
  supported_identity_providers         = ["COGNITO"]
}

# Hosted UI（ログイン画面）。自前で画面を作らずに済む。
resource "aws_cognito_user_pool_domain" "this" {
  domain       = "${local.project}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.this.id
}
