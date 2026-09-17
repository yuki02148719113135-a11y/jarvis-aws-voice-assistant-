output "endpoint" {
  description = "ブラウザからアクセスする先"
  value       = "http://${aws_lb.this.dns_name}"
}

output "cluster_name" {
  description = "desired_count を手動で 0 にするときに使う"
  value       = aws_ecs_cluster.this.name
}

output "service_name" {
  value = aws_ecs_service.this.name
}

output "log_group" {
  description = "aws logs tail で追う先"
  value       = aws_cloudwatch_log_group.app.name
}

output "active_config" {
  description = "今どの構成で立っているか（比較検証の記録用）"
  value = (
    var.use_nat ? "C: private + NAT gateway" :
    var.use_vpc_endpoints ? "B: private + 4 interface endpoints" :
    "A: public subnet + public IP"
  )
}
