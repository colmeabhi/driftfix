resource "aws_sns_topic" "cloudtrail_events" {
  name = "${var.project}-cloudtrail-events"
}

# Allow S3 to publish notifications to SNS
resource "aws_sns_topic_policy" "allow_s3" {
  arn = aws_sns_topic.cloudtrail_events.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowS3Publish"
        Effect = "Allow"
        Principal = {
          Service = "s3.amazonaws.com"
        }
        Action   = "SNS:Publish"
        Resource = aws_sns_topic.cloudtrail_events.arn
        Condition = {
          ArnLike = {
            "aws:SourceArn" = aws_s3_bucket.cloudtrail.arn
          }
        }
      }
    ]
  })
}

# S3 notifies SNS when a new CloudTrail log file lands
resource "aws_s3_bucket_notification" "cloudtrail_to_sns" {
  bucket = aws_s3_bucket.cloudtrail.id

  topic {
    topic_arn = aws_sns_topic.cloudtrail_events.arn
    events    = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_sns_topic_policy.allow_s3]
}

# SNS subscription: forward events cross-account to the SaaS EventBridge bus
resource "aws_sns_topic_subscription" "to_saas_eventbridge" {
  topic_arn = aws_sns_topic.cloudtrail_events.arn
  protocol  = "https"
  # EventBridge does not have a native SNS subscription ARN format;
  # in practice you subscribe via an SQS queue in the SaaS account or
  # use EventBridge's cross-account event bus ingestion directly from S3.
  # Placeholder — wire this after deciding on the exact fan-out mechanism.
  endpoint = "https://events.${var.aws_region}.amazonaws.com/event-bus/${var.saas_eventbridge_bus_arn}"
}
