resource "aws_sqs_queue" "drift_events_dlq" {
  name                      = "${var.project}-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "drift_events" {
  name                       = "${var.project}-events"
  visibility_timeout_seconds = 300 # must be >= Lambda timeout
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.drift_events_dlq.arn
    maxReceiveCount     = 3
  })
}

# Allow EventBridge to send messages to the queue
resource "aws_sqs_queue_policy" "drift_events" {
  queue_url = aws_sqs_queue.drift_events.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEventBridge"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action   = "sqs:SendMessage"
        Resource = aws_sqs_queue.drift_events.arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.write_ops.arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "${var.project}-dlq-not-empty"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Messages landed in DLQ — a drift event failed processing after all retries"

  dimensions = {
    QueueName = aws_sqs_queue.drift_events_dlq.name
  }
}
