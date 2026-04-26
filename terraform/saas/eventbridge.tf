locals {
  # During onboarding, add customer account IDs to var.allowed_customer_account_ids.
  # Falls back to the SaaS account itself so the bus policy is valid on first deploy.
  event_bus_principals = length(var.allowed_customer_account_ids) > 0 ? [
    for id in var.allowed_customer_account_ids : "arn:aws:iam::${id}:root"
  ] : ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
}

resource "aws_cloudwatch_event_bus" "main" {
  name = "${var.project}-bus"
}

resource "aws_cloudwatch_event_bus_policy" "allow_customers" {
  event_bus_name = aws_cloudwatch_event_bus.main.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowCustomerAccounts"
        Effect   = "Allow"
        Principal = { AWS = local.event_bus_principals }
        Action   = "events:PutEvents"
        Resource = aws_cloudwatch_event_bus.main.arn
      }
    ]
  })
}

# Filter: only forward write-type CloudTrail events to SQS
resource "aws_cloudwatch_event_rule" "write_ops" {
  name           = "${var.project}-write-ops"
  event_bus_name = aws_cloudwatch_event_bus.main.name

  event_pattern = jsonencode({
    source      = ["aws.cloudtrail"]
    detail-type = ["AWS API Call via CloudTrail"]
    detail = {
      eventName = [
        { prefix = "Create" },
        { prefix = "Update" },
        { prefix = "Delete" },
        { prefix = "Put" },
        { prefix = "Modify" }
      ]
      readOnly = [false]
    }
  })
}

resource "aws_cloudwatch_event_target" "to_sqs" {
  rule           = aws_cloudwatch_event_rule.write_ops.name
  event_bus_name = aws_cloudwatch_event_bus.main.name
  arn            = aws_sqs_queue.drift_events.arn
}
