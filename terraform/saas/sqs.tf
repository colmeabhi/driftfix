# DLQ for Processor Lambda async invocation failures
resource "aws_sqs_queue" "processor_dlq" {
  name                      = "${var.project}-processor-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_cloudwatch_metric_alarm" "processor_dlq_not_empty" {
  alarm_name          = "${var.project}-processor-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Processor Lambda batch run failed — check CloudWatch logs"

  dimensions = {
    QueueName = aws_sqs_queue.processor_dlq.name
  }
}
