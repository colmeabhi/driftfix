output "processor_dlq_url" {
  value = aws_sqs_queue.processor_dlq.url
}

output "dynamodb_tenants_table" {
  value = aws_dynamodb_table.tenants.name
}

output "dynamodb_reconciliations_table" {
  value = aws_dynamodb_table.reconciliations.name
}

output "audit_bucket" {
  value = aws_s3_bucket.audit.bucket
}
