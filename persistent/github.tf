# ---------------------------------------------------------------
# GitHub Actions から AWS を触るための OIDC 連携
#
# アクセスキーを発行して GitHub に保存する方式は、漏れたときに
# 取り消すまで使われ続ける。OIDC なら GitHub が発行する短命なトークンと
# 引き換えに一時クレデンシャルを受け取るので、保存する秘密がない。
# ---------------------------------------------------------------

locals {
  github_owner = "yuki02148719113135-a11y"
  github_name  = "jarvis-aws-voice-assistant-"
  github_repo  = "${local.github_owner}/${local.github_name}"
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

data "aws_iam_policy_document" "github_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    # このリポジトリからの実行だけに限定する。
    # ここを絞らないと、他人のリポジトリからでもこのロールを取れてしまう。
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      # GitHub は sub にオーナーとリポジトリの不変ID（@数字）を付ける。
      # 名前を変えてもなりすませないようにするための仕組みで、
      # repo:owner/name:* という条件では一致しない（* が / をまたげない）。
      values = [
        "repo:${local.github_repo}:*",
        "repo:${local.github_owner}@*/${local.github_name}@*:*",
      ]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = "${local.project}-github-actions"
  assume_role_policy = data.aws_iam_policy_document.github_assume.json
}

data "aws_iam_policy_document" "github_actions" {
  # ログイン用。リソース指定ができない API なので * になる。
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  # push できるのはこのリポジトリだけ。
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = [aws_ecr_repository.app.arn]
  }
}

resource "aws_iam_role_policy" "github_actions" {
  name   = "${local.project}-github-actions"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.github_actions.json
}
