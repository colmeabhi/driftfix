# ── SNS alert topic ───────────────────────────────────────────────────────────

resource "aws_sns_topic" "alerts" {
  name = "${var.project}-alerts"
}

resource "aws_sns_topic_subscription" "alert_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ── CloudWatch Log Groups (explicit 30-day retention) ─────────────────────────
# If Lambdas were already invoked before this Terraform run, AWS will have
# auto-created these log groups.  Import them first:
#   terraform import aws_cloudwatch_log_group.processor   /aws/lambda/drift-detector-processor
#   terraform import aws_cloudwatch_log_group.stack_processor /aws/lambda/drift-detector-stack-processor
#   terraform import aws_cloudwatch_log_group.validator   /aws/lambda/drift-detector-validator
#   terraform import aws_cloudwatch_log_group.pr_creator  /aws/lambda/drift-detector-pr-creator

resource "aws_cloudwatch_log_group" "processor" {
  name              = "/aws/lambda/${aws_lambda_function.processor.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "stack_processor" {
  name              = "/aws/lambda/${aws_lambda_function.stack_processor.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "validator" {
  name              = "/aws/lambda/${aws_lambda_function.validator.function_name}"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "pr_creator" {
  name              = "/aws/lambda/${aws_lambda_function.pr_creator.function_name}"
  retention_in_days = 30
}

# ── CloudWatch Dashboard ──────────────────────────────────────────────────────

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = "${var.project}-overview"

  dashboard_body = jsonencode({
    widgets = [
      # Row 1: Invocations (left) | Errors (right)
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Invocations"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Sum"
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.processor.function_name],
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.stack_processor.function_name],
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.validator.function_name],
            ["AWS/Lambda", "Invocations", "FunctionName", aws_lambda_function.pr_creator.function_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Errors"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Sum"
          metrics = [
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.processor.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.stack_processor.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.validator.function_name],
            ["AWS/Lambda", "Errors", "FunctionName", aws_lambda_function.pr_creator.function_name],
          ]
        }
      },
      # Row 2: Duration p99 (left) | Throttles (right)
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Duration p99 (ms)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "p99"
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.processor.function_name],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.stack_processor.function_name],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.validator.function_name],
            ["AWS/Lambda", "Duration", "FunctionName", aws_lambda_function.pr_creator.function_name],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Throttles"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Sum"
          metrics = [
            ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.processor.function_name],
            ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.stack_processor.function_name],
            ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.validator.function_name],
            ["AWS/Lambda", "Throttles", "FunctionName", aws_lambda_function.pr_creator.function_name],
          ]
        }
      },
      # Row 3: Concurrent executions (full width, stacked — shows scaling)
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 18
        height = 6
        properties = {
          title   = "Concurrent Lambda Executions (scaling)"
          view    = "timeSeries"
          stacked = true
          region  = var.aws_region
          period  = 60
          stat    = "Maximum"
          metrics = [
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", aws_lambda_function.processor.function_name],
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", aws_lambda_function.stack_processor.function_name],
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", aws_lambda_function.validator.function_name],
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", aws_lambda_function.pr_creator.function_name],
          ]
        }
      },
      # Row 3 right: DLQ depth
      {
        type   = "metric"
        x      = 18
        y      = 12
        width  = 6
        height = 6
        properties = {
          title   = "DLQ Depth"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Maximum"
          metrics = [
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.processor_dlq.name],
          ]
        }
      },
    ]
  })
}

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────

# Any unhandled exception in the Processor breaks the entire daily batch run.
resource "aws_cloudwatch_metric_alarm" "processor_errors" {
  alarm_name          = "${var.project}-processor-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Processor Lambda threw an unhandled exception — daily batch may be incomplete"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.processor.function_name
  }
}

# Tolerate up to 2 stack-level failures per hour (transient cross-account issues)
# before treating it as a systemic problem.
resource "aws_cloudwatch_metric_alarm" "stack_processor_errors" {
  alarm_name          = "${var.project}-stack-processor-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 2
  alarm_description   = "Stack Processor error rate elevated (>2 failures in 1 h)"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.stack_processor.function_name
  }
}

# Processor p99 duration > 80% of its 900s timeout signals it is close to timing
# out and will start dropping tenants silently.
resource "aws_cloudwatch_metric_alarm" "processor_near_timeout" {
  alarm_name          = "${var.project}-processor-near-timeout"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 3600
  extended_statistic  = "p99"
  threshold           = 720000 # 720 s in ms = 80% of 900 s timeout
  alarm_description   = "Processor Lambda p99 duration exceeded 80% of its 15-min timeout"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.processor.function_name
  }
}

# Any Validator error means a drift event failed cfn-lint and no PR will be opened
# for it.  Even one failure per hour warrants investigation.
resource "aws_cloudwatch_metric_alarm" "validator_errors" {
  alarm_name          = "${var.project}-validator-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Validator Lambda error — a drift event failed cfn-lint or hit an unhandled exception"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.validator.function_name
  }
}

# Validator throttles mean Stack Processor invocations fail and go to the DLQ.
resource "aws_cloudwatch_metric_alarm" "validator_throttles" {
  alarm_name          = "${var.project}-validator-throttled"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Validator Lambda was throttled — drift events are being dropped; check reserved concurrency"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.validator.function_name
  }
}

# Any PR Creator error means a tenant did not receive a pull request for a
# detected drift event.  No tolerance — every failure should be investigated.
resource "aws_cloudwatch_metric_alarm" "pr_creator_errors" {
  alarm_name          = "${var.project}-pr-creator-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "PR Creator Lambda error — a drift PR was not opened; check GitHub token and Secrets Manager"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.pr_creator.function_name
  }
}

# Any throttle on the Processor means the daily cron invocation was dropped entirely.
resource "aws_cloudwatch_metric_alarm" "processor_throttles" {
  alarm_name          = "${var.project}-processor-throttled"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Throttles"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "Processor Lambda was throttled — account concurrency limit may need increasing"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = aws_lambda_function.processor.function_name
  }
}
