# ephemeral 層から terraform_remote_state 経由で参照される値。
# ここに出したものが層をまたぐ唯一の接点になる。

output "conversations_table_name" {
  description = "会話履歴テーブル名（タスクへ環境変数で渡す）"
  value       = aws_dynamodb_table.conversations.name
}

output "ecr_repository_url" {
  description = "コンテナイメージの push / pull 先"
  value       = aws_ecr_repository.app.repository_url
}

output "task_execution_role_arn" {
  description = "ECS がイメージ pull とログ送信に使う実行ロール"
  value       = aws_iam_role.task_execution.arn
}

output "task_role_arn" {
  description = "アプリが Bedrock / DynamoDB を叩くタスクロール"
  value       = aws_iam_role.task.arn
}

output "tfstate_bucket" {
  description = "backend 設定に書くバケット名"
  value       = aws_s3_bucket.tfstate.id
}

output "github_actions_role_arn" {
  description = "GitHub Actions が assume するロール（ワークフローの vars に入れる）"
  value       = aws_iam_role.github_actions.arn
}
