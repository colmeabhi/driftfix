output "eventbridge_bus_arn" {
  value = aws_cloudwatch_event_bus.main.arn
}

output "sqs_queue_url" {
  value = aws_sqs_queue.drift_events.url
}

output "sqs_dlq_url" {
  value = aws_sqs_queue.drift_events_dlq.url
}

output "dynamodb_table_name" {
  value = aws_dynamodb_table.reconciliations.name
}
