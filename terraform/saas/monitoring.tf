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
  name              = "/aws/lambda/${var.project}-processor"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "stack_processor" {
  name              = "/aws/lambda/${var.project}-stack-processor"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "validator" {
  name              = "/aws/lambda/${var.project}-validator"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "pr_creator" {
  name              = "/aws/lambda/${var.project}-pr-creator"
  retention_in_days = 30
}

# ── Cold Start / Scaling Delay Metric Filters ────────────────────────────────
# Extracts Init Duration from Lambda REPORT log lines (cold starts only).
# Init Duration appears only when Lambda initialises a new execution environment;
# warm invocations produce no data point, so gaps in the metric = warm periods.
# Custom namespace: ${var.project}/Lambda  metric: InitDuration  unit: Milliseconds
#
# Use Average stat for mean cold start latency; SampleCount for cold start frequency.

locals {
  # Space-delimited extraction of Init Duration from Lambda REPORT log lines.
  # Colon characters are not allowed in term values, so keyword fields like
  # "RequestId:" and "Duration:" are captured as unconstrained positional fields.
  # Anchored by r=REPORT (first token) and ik=Init (19th token); warm-start lines
  # have only 19 tokens and will not match this 23-token pattern.
  cold_start_pattern = "[r=REPORT, rk, id, dk, dur, du=ms, bk, bdk, bd, bu=ms, mk, sk, mem, mu=MB, xk, xmk, uk, xm, xu=MB, ik=Init, idk, init_dur, iu=ms]"
}

resource "aws_cloudwatch_log_metric_filter" "cold_starts_processor" {
  name           = "${var.project}-processor-init-duration"
  log_group_name = aws_cloudwatch_log_group.processor.name
  pattern        = local.cold_start_pattern

  metric_transformation {
    name      = "ProcessorInitDuration"
    namespace = "${var.project}/Lambda"
    value     = "$init_dur"
    unit      = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "cold_starts_stack_processor" {
  name           = "${var.project}-stack-processor-init-duration"
  log_group_name = aws_cloudwatch_log_group.stack_processor.name
  pattern        = local.cold_start_pattern

  metric_transformation {
    name      = "StackProcessorInitDuration"
    namespace = "${var.project}/Lambda"
    value     = "$init_dur"
    unit      = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "cold_starts_validator" {
  name           = "${var.project}-validator-init-duration"
  log_group_name = aws_cloudwatch_log_group.validator.name
  pattern        = local.cold_start_pattern

  metric_transformation {
    name      = "ValidatorInitDuration"
    namespace = "${var.project}/Lambda"
    value     = "$init_dur"
    unit      = "Milliseconds"
  }
}

resource "aws_cloudwatch_log_metric_filter" "cold_starts_pr_creator" {
  name           = "${var.project}-pr-creator-init-duration"
  log_group_name = aws_cloudwatch_log_group.pr_creator.name
  pattern        = local.cold_start_pattern

  metric_transformation {
    name      = "PrCreatorInitDuration"
    namespace = "${var.project}/Lambda"
    value     = "$init_dur"
    unit      = "Milliseconds"
  }
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
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-pr-creator"],
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
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Errors", "FunctionName", "${var.project}-pr-creator"],
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
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-pr-creator"],
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
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Throttles", "FunctionName", "${var.project}-pr-creator"],
          ]
        }
      },
      # Row 3: Duration p95 (left) | Throughput events/min (right)
      {
        type   = "metric"
        x      = 0
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Lambda Duration p95 (ms)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "p95"
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "Duration", "FunctionName", "${var.project}-pr-creator"],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 12
        width  = 12
        height = 6
        properties = {
          title   = "Pipeline Throughput (events/min)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          metrics = [
            [{ expression = "iv/5", label = "validator (events/min)",        id = "tv" }],
            [{ expression = "is/5", label = "stack-processor (events/min)",  id = "ts" }],
            [{ expression = "ip/5", label = "processor (events/min)",        id = "tp" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-validator",        { id = "iv", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-stack-processor",  { id = "is", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-processor",        { id = "ip", visible = false, stat = "Sum" }],
          ]
        }
      },
      # Row 4: Concurrent executions (full width, stacked — shows scaling)
      {
        type   = "metric"
        x      = 0
        y      = 18
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
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", "${var.project}-processor"],
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", "${var.project}-stack-processor"],
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", "${var.project}-validator"],
            ["AWS/Lambda", "ConcurrentExecutions", "FunctionName", "${var.project}-pr-creator"],
          ]
        }
      },
      # Row 4 right: DLQ depth
      {
        type   = "metric"
        x      = 18
        y      = 18
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
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", "${var.project}-processor-dlq"],
          ]
        }
      },
      # Row 5: Cold Start / Scaling Delay (Init Duration)
      # Data points appear only on cold starts; gaps = warm invocations.
      # Average = mean scaling delay; SampleCount = cold start frequency.
      {
        type   = "metric"
        x      = 0
        y      = 24
        width  = 24
        height = 6
        properties = {
          title   = "Cold Start / Scaling Delay — Init Duration (ms)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 300
          stat    = "Average"
          metrics = [
            ["${var.project}/Lambda", "ProcessorInitDuration",      { label = "processor (avg ms)" }],
            ["${var.project}/Lambda", "StackProcessorInitDuration", { label = "stack-processor (avg ms)" }],
            ["${var.project}/Lambda", "ValidatorInitDuration",       { label = "validator (avg ms)" }],
            ["${var.project}/Lambda", "PrCreatorInitDuration",       { label = "pr-creator (avg ms)" }],
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
    FunctionName = "${var.project}-processor"
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
    FunctionName = "${var.project}-stack-processor"
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
    FunctionName = "${var.project}-processor"
  }
}

# Processor p95 duration > 67% of its 900s timeout — early warning before p99 fires.
resource "aws_cloudwatch_metric_alarm" "processor_near_timeout_p95" {
  alarm_name          = "${var.project}-processor-near-timeout-p95"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Duration"
  namespace           = "AWS/Lambda"
  period              = 3600
  extended_statistic  = "p95"
  threshold           = 600000 # 600 s in ms = 67% of 900 s timeout
  alarm_description   = "Processor Lambda p95 duration exceeded 67% of its 15-min timeout — early warning"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    FunctionName = "${var.project}-processor"
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
    FunctionName = "${var.project}-validator"
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
    FunctionName = "${var.project}-validator"
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
    FunctionName = "${var.project}-pr-creator"
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
    FunctionName = "${var.project}-processor"
  }
}

# ── Cost Estimation Dashboard ─────────────────────────────────────────────────
# Estimates Lambda compute cost in USD using published us-east-1 pricing:
#   Requests : $0.20 per 1M  →  $2e-7 per invocation
#   Duration : $0.0000166667 per GB-second
#     processor / stack-processor : 512 MB = 0.5 GB  → $8.333e-9 per ms
#     validator / pr-creator      : 256 MB = 0.25 GB → $4.167e-9 per ms
#
# Formula per Lambda:
#   cost = invocations * 2e-7 + duration_sum_ms * price_per_ms
#
# The time-series widget (Row 1) uses period=3600 so each point shows cost for
# one hourly bucket; it naturally follows the dashboard time-range window.
#
# The singleValue widgets (Row 2) use setPeriodToTimeRange=true so they sum
# all invocations and duration across the entire selected time range and report
# total cost for that window.  Changing the dashboard time range (1 h, 3 h,
# 1 d, 3 d, 1 w, custom) automatically updates both total-cost numbers.
#
# Figures are estimates only — they exclude free-tier, data transfer, Secrets
# Manager, DynamoDB, S3, and SNS costs.

resource "aws_cloudwatch_dashboard" "cost" {
  dashboard_name = "${var.project}-cost"

  dashboard_body = jsonencode({
    widgets = [
      # Row 1: Per-Lambda estimated hourly cost
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 24
        height = 6
        properties = {
          title   = "Estimated Lambda Cost per Hour (USD)"
          view    = "timeSeries"
          stacked = false
          region  = var.aws_region
          period  = 3600
          metrics = [
            [{ expression = "ip*2e-7 + dp*8.333e-9", label = "processor (USD/hr)",        id = "cp" }],
            [{ expression = "is*2e-7 + ds*8.333e-9", label = "stack-processor (USD/hr)",  id = "cs" }],
            [{ expression = "iv*2e-7 + dv*4.167e-9", label = "validator (USD/hr)",         id = "cv" }],
            [{ expression = "ic*2e-7 + dc*4.167e-9", label = "pr-creator (USD/hr)",        id = "cc" }],
            [{ expression = "cp+cs+cv+cc",            label = "TOTAL (USD/hr)",             id = "total" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-processor",       { id = "ip", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-stack-processor", { id = "is", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-validator",        { id = "iv", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-pr-creator",       { id = "ic", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-processor",       { id = "dp", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-stack-processor", { id = "ds", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-validator",        { id = "dv", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-pr-creator",       { id = "dc", visible = false, stat = "Sum" }],
          ]
        }
      },
      # Row 2: Cumulative cost (singleValue) | Invocation count breakdown
      # setPeriodToTimeRange=true aggregates over the entire dashboard time window
      # so these totals automatically reflect 1 h / 3 h / 1 d / 3 d / 1 w / custom
      # selections — no hardcoded period.
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title                = "Estimated Total Lambda Cost — Selected Period (USD)"
          view                 = "singleValue"
          region               = var.aws_region
          setPeriodToTimeRange = true
          metrics = [
            [{ expression = "ip24*2e-7 + dp24*8.333e-9", label = "processor",        id = "cp24" }],
            [{ expression = "is24*2e-7 + ds24*8.333e-9", label = "stack-processor",  id = "cs24" }],
            [{ expression = "iv24*2e-7 + dv24*4.167e-9", label = "validator",         id = "cv24" }],
            [{ expression = "ic24*2e-7 + dc24*4.167e-9", label = "pr-creator",        id = "cc24" }],
            [{ expression = "cp24+cs24+cv24+cc24",        label = "TOTAL",             id = "total24" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-processor",       { id = "ip24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-stack-processor", { id = "is24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-validator",        { id = "iv24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-pr-creator",       { id = "ic24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-processor",       { id = "dp24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-stack-processor", { id = "ds24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-validator",        { id = "dv24", visible = false, stat = "Sum" }],
            ["AWS/Lambda", "Duration",    "FunctionName", "${var.project}-pr-creator",       { id = "dc24", visible = false, stat = "Sum" }],
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
          title                = "Invocation Count — Selected Period"
          view                 = "singleValue"
          region               = var.aws_region
          setPeriodToTimeRange = true
          stat                 = "Sum"
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-processor",       { label = "processor" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-stack-processor", { label = "stack-processor" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-validator",        { label = "validator" }],
            ["AWS/Lambda", "Invocations", "FunctionName", "${var.project}-pr-creator",       { label = "pr-creator" }],
          ]
        }
      },
    ]
  })
}
