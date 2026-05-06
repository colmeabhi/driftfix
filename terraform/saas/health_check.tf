# ── Health Check Lambda ────────────────────────────────────────────────────────
# Runs every 5 minutes (EventBridge schedule) and performs a lightweight
# availability probe on each pipeline component:
#
#   Lambda functions   GetFunctionConfiguration → State == "Active"
#   DynamoDB tables    DescribeTable            → TableStatus == "ACTIVE"
#   S3 audit bucket    HeadBucket               → no ClientError
#   SQS processor DLQ  GetQueueAttributes       → no ClientError
#
# Each result is published as a 1 (healthy) or 0 (unhealthy) custom CloudWatch
# metric in the ${var.project}/HealthCheck namespace.  The component health alarms
# below consume these metrics; the drift-detector-status dashboard surfaces both
# the current alarm states and the historical 0/1 time series.

resource "aws_cloudwatch_log_group" "health_check" {
  name              = "/aws/lambda/${var.project}-health-check"
  retention_in_days = 30
}

# ── IAM ───────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "health_check" {
  name               = "${var.project}-health-check"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "health_check" {
  role = aws_iam_role.health_check.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Sid    = "CheckLambdas"
        Effect = "Allow"
        Action = ["lambda:GetFunctionConfiguration"]
        Resource = [
          aws_lambda_function.processor.arn,
          aws_lambda_function.stack_processor.arn,
          aws_lambda_function.validator.arn,
          aws_lambda_function.pr_creator.arn,
        ]
      },
      {
        Sid    = "CheckDynamoDB"
        Effect = "Allow"
        Action = ["dynamodb:DescribeTable"]
        Resource = [
          aws_dynamodb_table.reconciliations.arn,
          aws_dynamodb_table.tenants.arn,
        ]
      },
      {
        Sid      = "CheckS3"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.audit.arn
      },
      {
        Sid      = "CheckSQS"
        Effect   = "Allow"
        Action   = ["sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.processor_dlq.arn
      },
      {
        # PutMetricData does not support resource-level IAM restrictions
        Sid      = "PublishMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
      },
    ]
  })
}

# ── Lambda Function ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "health_check" {
  function_name = "${var.project}-health-check"
  role          = aws_iam_role.health_check.arn
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 128

  s3_bucket = var.lambda_zip_bucket
  s3_key    = "health_check.zip"

  environment {
    variables = {
      PROCESSOR_FUNCTION_NAME        = aws_lambda_function.processor.function_name
      STACK_PROCESSOR_FUNCTION_NAME  = aws_lambda_function.stack_processor.function_name
      VALIDATOR_FUNCTION_NAME        = aws_lambda_function.validator.function_name
      PR_CREATOR_FUNCTION_NAME       = aws_lambda_function.pr_creator.function_name
      DYNAMODB_RECONCILIATIONS_TABLE = aws_dynamodb_table.reconciliations.name
      DYNAMODB_TENANTS_TABLE         = aws_dynamodb_table.tenants.name
      AUDIT_BUCKET                   = aws_s3_bucket.audit.bucket
      DLQ_URL                        = aws_sqs_queue.processor_dlq.url
      METRIC_NAMESPACE               = "${var.project}/HealthCheck"
    }
  }

  depends_on = [aws_cloudwatch_log_group.health_check]
}

# ── EventBridge schedule: every 5 minutes ─────────────────────────────────────

resource "aws_cloudwatch_event_rule" "health_check_schedule" {
  name                = "${var.project}-health-check"
  description         = "Trigger health check Lambda every 5 minutes"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "health_check_lambda" {
  rule = aws_cloudwatch_event_rule.health_check_schedule.name
  arn  = aws_lambda_function.health_check.arn
}

resource "aws_lambda_permission" "eventbridge_invoke_health_check" {
  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.health_check.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.health_check_schedule.arn
}

# ── Component health alarms ───────────────────────────────────────────────────
# One alarm per health metric.  Fires when the metric drops to 0 (unhealthy).
# treat_missing_data = "breaching" ensures the alarm also fires if the health
# check Lambda itself stops publishing — e.g. if the function is deleted or
# its IAM role is revoked.  Alarms will start in ALARM state after first apply
# and transition to OK within 5 minutes once the EventBridge schedule fires.

locals {
  health_metrics = {
    processor             = "ProcessorHealth"
    stack-processor       = "StackProcessorHealth"
    validator             = "ValidatorHealth"
    pr-creator            = "PrCreatorHealth"
    reconciliations-table = "ReconciliationsTableHealth"
    tenants-table         = "TenantsTableHealth"
    audit-bucket          = "AuditBucketHealth"
    processor-dlq         = "ProcessorDLQHealth"
  }
}

resource "aws_cloudwatch_metric_alarm" "component_health" {
  for_each = local.health_metrics

  alarm_name          = "${var.project}-${each.key}-health"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = each.value
  namespace           = "${var.project}/HealthCheck"
  period              = 300
  statistic           = "Minimum"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_description   = "${each.key} reported unhealthy (metric=0) or health check stopped publishing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# ── Service Status Dashboard ──────────────────────────────────────────────────
# Row 1 (y=0):  Component health alarm board  — colored OK/ALARM badges per component
# Row 2 (y=6):  Pipeline alarm board          — errors, throttles, timeout alarms
# Row 3 (y=12): Health metric time series     — historical 0/1 per component
# Row 4 (y=18): DLQ depth  |  Lambda errors (all functions)
# Row 5 (y=24): Health check invocations & errors  |  Lambda throttles

resource "aws_cloudwatch_dashboard" "status" {
  dashboard_name = "${var.project}-status"

  dashboard_body = jsonencode({
    widgets = [

      # ── Row 1: Component health alarm badges ─────────────────────────────────
      {
        type   = "alarm"
        x      = 0
        y      = 0
        width  = 24
        height = 6
        properties = {
          title  = "Component Health  (green = OK, red = ALARM)"
          alarms = [
            aws_cloudwatch_metric_alarm.component_health["processor"].arn,
            aws_cloudwatch_metric_alarm.component_health["stack-processor"].arn,
            aws_cloudwatch_metric_alarm.component_health["validator"].arn,
            aws_cloudwatch_metric_alarm.component_health["pr-creator"].arn,
            aws_cloudwatch_metric_alarm.component_health["reconciliations-table"].arn,
            aws_cloudwatch_metric_alarm.component_health["tenants-table"].arn,
            aws_cloudwatch_metric_alarm.component_health["audit-bucket"].arn,
            aws_cloudwatch_metric_alarm.component_health["processor-dlq"].arn,
          ]
        }
      },

      # ── Row 2: Pipeline alarm badges ─────────────────────────────────────────
      {
        type   = "alarm"
        x      = 0
        y      = 6
        width  = 24
        height = 6
        properties = {
          title  = "Pipeline Alarms  (errors, throttles, timeouts, DLQ)"
          alarms = [
            aws_cloudwatch_metric_alarm.processor_errors.arn,
            aws_cloudwatch_metric_alarm.stack_processor_errors.arn,
            aws_cloudwatch_metric_alarm.validator_errors.arn,
            aws_cloudwatch_metric_alarm.pr_creator_errors.arn,
            aws_cloudwatch_metric_alarm.validator_throttles.arn,
            aws_cloudwatch_metric_alarm.processor_throttles.arn,
            aws_cloudwatch_metric_alarm.processor_near_timeout_p95.arn,
            aws_cloudwatch_metric_alarm.processor_near_timeout.arn,
            aws_cloudwatch_metric_alarm.processor_dlq_not_empty.arn,
          ]
        }
      },

      # ── Row 3: Health metric time series (0 = down, 1 = up) ─────────────────
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 24
        height = 6
        properties = {
          title   = "Component Health Over Time  (1 = healthy, 0 = unhealthy)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Minimum"
          yAxis   = { left = { min = 0, max = 1.2 } }
          metrics = [
            ["${var.project}/HealthCheck", "ProcessorHealth",            { label = "processor" }],
            ["${var.project}/HealthCheck", "StackProcessorHealth",       { label = "stack-processor" }],
            ["${var.project}/HealthCheck", "ValidatorHealth",            { label = "validator" }],
            ["${var.project}/HealthCheck", "PrCreatorHealth",            { label = "pr-creator" }],
            ["${var.project}/HealthCheck", "ReconciliationsTableHealth", { label = "reconciliations-table" }],
            ["${var.project}/HealthCheck", "TenantsTableHealth",         { label = "tenants-table" }],
            ["${var.project}/HealthCheck", "AuditBucketHealth",          { label = "audit-bucket" }],
            ["${var.project}/HealthCheck", "ProcessorDLQHealth",         { label = "processor-dlq" }],
          ]
        }
      },

      # ── Row 4: DLQ depth | Lambda errors ─────────────────────────────────────
      {
        type   = "metric"
        x      = 0
        y      = 18
        width  = 12
        height = 6
        properties = {
          title   = "DLQ Depth"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Maximum"
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible",
             "QueueName", "${var.project}-processor-dlq"],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 18
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Errors (all functions)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Sum"
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-pr-creator"],
          ]
        }
      },

      # ── Row 5: Health check self-monitoring | Lambda throttles ────────────────
      {
        type   = "metric"
        x      = 0
        y      = 24
        width  = 12
        height = 6
        properties = {
          title   = "Health Check Lambda — Invocations & Errors"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Sum"
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-health-check",
             { label = "invocations" }],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-health-check",
             { label = "errors" }],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 24
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Throttles (all functions)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Sum"
          metrics = [
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-pr-creator"],
          ]
        }
      },

    ]
  })
}
