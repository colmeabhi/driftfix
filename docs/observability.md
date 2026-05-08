# Observability — Drift Detector SaaS

This document describes every observability feature deployed in the Drift Detector SaaS
platform: CloudWatch dashboards, alarms, SNS notifications, log groups, metric filters,
custom metrics, and the automated health check system.

All infrastructure is defined in `terraform/saas/monitoring.tf` and
`terraform/saas/health_check.tf`.

---

## Public Dashboards

The three dashboards below are publicly accessible without AWS credentials.

| Dashboard | Purpose | Public URL |
|---|---|---|
| **Service Status** | Live component health — recommended entry point for clients and reviewers | [drift-detector-status](https://cloudwatch.amazonaws.com/dashboard.html?dashboard=drift-detector-status&context=eyJSIjoidXMtZWFzdC0xIiwiRCI6ImN3LWRiLTI3NDAyNDQzNzg0NSIsIlUiOiJ1cy1lYXN0LTFfSkRGSWhSek4wIiwiQyI6IjRzYzdsaGhpNXU0dXUxZGVxbnVsY2ExOW9tIiwiSSI6InVzLWVhc3QtMTphOTE2YmI3Mi04ZjQzLTRmODctOTEyZC01Y2NkNDM0MTQyNjAiLCJNIjoiUHVibGljIn0=) |
| **Operations Overview** | Lambda invocations, errors, duration, throttles, cold starts | [drift-detector-overview](https://cloudwatch.amazonaws.com/dashboard.html?dashboard=drift-detector-overview&context=eyJSIjoidXMtZWFzdC0xIiwiRCI6ImN3LWRiLTI3NDAyNDQzNzg0NSIsIlUiOiJ1cy1lYXN0LTFfSkRGSWhSek4wIiwiQyI6IjRzYzdsaGhpNXU0dXUxZGVxbnVsY2ExOW9tIiwiSSI6InVzLWVhc3QtMTplZjg2Njk3ZS00MTZlLTRiNjMtOTIyMC0zN2RlZDdkMTNkY2QiLCJNIjoiUHVibGljIn0=) |
| **Cost Estimation** | Lambda compute cost by selected time period | [drift-detector-cost](https://cloudwatch.amazonaws.com/dashboard.html?dashboard=drift-detector-cost&context=eyJSIjoidXMtZWFzdC0xIiwiRCI6ImN3LWRiLTI3NDAyNDQzNzg0NSIsIlUiOiJ1cy1lYXN0LTFfSkRGSWhSek4wIiwiQyI6IjRzYzdsaGhpNXU0dXUxZGVxbnVsY2ExOW9tIiwiSSI6InVzLWVhc3QtMTo2MWE4NjNjMi0yYmE2LTQwM2MtOTY1Ny1hY2NmNTUzNTk5OTIiLCJNIjoiUHVibGljIn0=) |

> The Service Status dashboard meets the public-facing observability entry point
> requirement.  It shows live alarm state for every infrastructure component and
> requires no AWS login to view.

---

## CloudWatch Dashboards

### drift-detector-status  *(service status)*

The primary external-facing dashboard.  Shows a two-row alarm status board followed
by time-series panels for deeper investigation.

| Row | Widget | Metrics / Data |
|---|---|---|
| 1 | **Component Health** — alarm badge per infrastructure component | 8 health check alarms (see below) |
| 2 | **Pipeline Alarms** — badge per error/throttle/timeout condition | 9 pipeline alarms |
| 3 | **Component Health Over Time** — time series, 1 = healthy / 0 = unhealthy | `drift-detector/HealthCheck` custom metrics |
| 4 | **DLQ Depth** \| **Lambda Errors (all functions)** | `AWS/SQS`, `AWS/Lambda` |
| 5 | **Health Check Invocations & Errors** \| **Lambda Throttles** | `AWS/Lambda` |

---

### drift-detector-overview  *(operations)*

Day-to-day operational view.  All widgets use the dashboard time-range selector.

| Row | Widget | Stat | Period |
|---|---|---|---|
| 1 | Lambda Invocations | Sum | 5 min |
| 1 | Lambda Errors | Sum | 5 min |
| 2 | Lambda Duration p99 (ms) | p99 | 5 min |
| 2 | Lambda Throttles | Sum | 5 min |
| 3 | Lambda Duration p95 (ms) | p95 | 5 min |
| 3 | Pipeline Throughput (events/min) | derived expression | 5 min |
| 4 | Concurrent Lambda Executions (stacked) | Maximum | 1 min |
| 4 | DLQ Depth | Maximum | 5 min |
| 5 | Cold Start / Scaling Delay — Init Duration (ms) | Average | 5 min |

Functions tracked on all widgets: `processor`, `stack-processor`, `validator`, `pr-creator`.

---

### drift-detector-cost  *(cost estimation)*

Estimates Lambda compute spend using published us-east-1 pricing.  All singleValue
widgets use `setPeriodToTimeRange = true` so totals automatically reflect the
dashboard's selected time window (1 h, 3 h, 1 d, 3 d, 1 w, or custom).

**Pricing model:**

| Component | Memory | GB-second rate | Per-invocation rate | Per-ms rate |
|---|---|---|---|---|
| processor, stack-processor | 512 MB = 0.5 GB | $0.0000166667 | $2e-7 | $8.333e-9 |
| validator, pr-creator | 256 MB = 0.25 GB | $0.0000166667 | $2e-7 | $4.167e-9 |

**Formula per function:**

```
cost = invocations × $2e-7  +  duration_sum_ms × price_per_ms
```

`Invocations` and `Duration` (Sum) are fetched as hidden metrics; math expressions
visible in the graph compute the dollar cost.  Figures exclude free-tier, data
transfer, Secrets Manager, DynamoDB, S3, and SNS charges.

| Row | Widget | Description |
|---|---|---|
| 1 | Estimated Cost per Hour (time series) | One cost data point per 1-hour bucket, per function + TOTAL |
| 2 | Estimated Total Cost — Selected Period (singleValue) | Aggregated cost for the chosen time window |
| 2 | Invocation Count — Selected Period (singleValue) | Total invocations for the chosen time window |

---

## CloudWatch Alarms

All alarms send notifications to the `drift-detector-alerts` SNS topic.

### Pipeline Alarms

| Alarm | Condition | Rationale |
|---|---|---|
| `drift-detector-processor-errors` | Errors > 0 in 1 h | Any unhandled exception breaks the daily batch run |
| `drift-detector-stack-processor-errors` | Errors > 2 in 1 h | Up to 2 transient cross-account failures tolerated |
| `drift-detector-validator-errors` | Errors > 0 in 1 h | Any cfn-lint or unhandled exception means a tenant receives no PR |
| `drift-detector-pr-creator-errors` | Errors > 0 in 1 h | A failed PR Creator means a drift PR was not opened |
| `drift-detector-validator-throttled` | Throttles > 0 in 1 h | Throttles cause Stack Processor to route to DLQ |
| `drift-detector-processor-throttled` | Throttles > 0 in 1 h | A throttled Processor drops the entire daily cron run |
| `drift-detector-processor-near-timeout` | p99 Duration > 720 000 ms in 1 h | 80% of the 900 s Processor timeout — silent tenant drops imminent |
| `drift-detector-processor-near-timeout-p95` | p95 Duration > 600 000 ms in 1 h | Early warning at 67% of timeout before p99 fires |
| `drift-detector-processor-dlq-not-empty` | `ApproximateNumberOfMessagesVisible` > 0 | Any stranded event in the DLQ requires operator replay |

### Component Health Alarms

Generated by the health check Lambda (see [Health Check System](#health-check-system)).
Each alarm fires when the corresponding health metric drops to `0` or when the health
check Lambda stops publishing altogether (`treat_missing_data = breaching`).

| Alarm | Metric checked |
|---|---|
| `drift-detector-processor-health` | `ProcessorHealth` |
| `drift-detector-stack-processor-health` | `StackProcessorHealth` |
| `drift-detector-validator-health` | `ValidatorHealth` |
| `drift-detector-pr-creator-health` | `PrCreatorHealth` |
| `drift-detector-reconciliations-table-health` | `ReconciliationsTableHealth` |
| `drift-detector-tenants-table-health` | `TenantsTableHealth` |
| `drift-detector-audit-bucket-health` | `AuditBucketHealth` |
| `drift-detector-processor-dlq-health` | `ProcessorDLQHealth` |

---

## SNS Notifications

**Topic:** `drift-detector-alerts`

Every alarm listed above sends both `alarm_actions` (on enter ALARM) and `ok_actions`
(on return to OK) to this topic.  An optional email subscription is created when the
`alert_email` Terraform variable is set:

```hcl
# terraform/saas/terraform.tfvars
alert_email = "ops@example.com"
```

The subscriber receives one email per state transition (ALARM → OK and OK → ALARM)
for each of the 17 alarms above.

---

## CloudWatch Log Groups

All log groups are created explicitly with a 30-day retention policy.  If the Lambda
function was invoked before Terraform was applied, the auto-created log group must be
imported:

```bash
terraform import aws_cloudwatch_log_group.processor   /aws/lambda/drift-detector-processor
terraform import aws_cloudwatch_log_group.stack_processor /aws/lambda/drift-detector-stack-processor
terraform import aws_cloudwatch_log_group.validator   /aws/lambda/drift-detector-validator
terraform import aws_cloudwatch_log_group.pr_creator  /aws/lambda/drift-detector-pr-creator
```

| Log Group | Retention | Contents |
|---|---|---|
| `/aws/lambda/drift-detector-processor` | 30 days | Batch run progress, tenant fan-out counts, CloudTrail read errors |
| `/aws/lambda/drift-detector-stack-processor` | 30 days | Per-stack Bedrock/Gemini interactions, reconciliation writes |
| `/aws/lambda/drift-detector-validator` | 30 days | cfn-lint output, S3 write confirmations, PR Creator invocations |
| `/aws/lambda/drift-detector-pr-creator` | 30 days | GitHub API calls, Secrets Manager reads, DynamoDB status updates |
| `/aws/lambda/drift-detector-health-check` | 30 days | JSON health report per run, error details for any unhealthy component |

### CloudWatch Log Insights — useful queries

**Find all health check failures in the last 24 hours:**
```
fields @timestamp, unhealthy
| filter event = "health_check" and overall_healthy = 0
| sort @timestamp desc
```

**Find Validator cfn-lint errors:**
```
fields @timestamp, @message
| filter @logStream like /drift-detector-validator/
| filter @message like /cfn-lint/
| sort @timestamp desc
```

**Find all Lambda cold starts across all functions:**
```
fields @timestamp, @logStream, @message
| filter @message like /Init Duration/
| parse @message "Init Duration: * ms" as init_ms
| stats avg(init_ms), max(init_ms) by @logStream
```

---

## Custom CloudWatch Metrics

### Namespace: `drift-detector/Lambda`  *(cold start latency)*

Populated by log metric filters applied to each Lambda function's REPORT log lines.
Data points appear **only on cold starts** — warm invocations produce no data point,
so gaps in the metric indicate periods of warm execution.

| Metric | Unit | Useful stats |
|---|---|---|
| `ProcessorInitDuration` | Milliseconds | Average = mean cold start latency; SampleCount = cold start frequency |
| `StackProcessorInitDuration` | Milliseconds | same |
| `ValidatorInitDuration` | Milliseconds | same |
| `PrCreatorInitDuration` | Milliseconds | same |

The filter pattern extracts `Init Duration` from the Lambda REPORT log line format:
```
REPORT RequestId: ...  Duration: X ms  ...  Init Duration: Y ms
```

---

### Namespace: `drift-detector/HealthCheck`  *(component availability)*

Published by `drift-detector-health-check` every 5 minutes.  Value is `1` (healthy)
or `0` (unhealthy).  CloudWatch alarms watch the `Minimum` statistic over a 5-minute
period so a single unhealthy report triggers an alarm.

| Metric | Component probed | API call |
|---|---|---|
| `ProcessorHealth` | `drift-detector-processor` Lambda | `GetFunctionConfiguration` → `State == Active` |
| `StackProcessorHealth` | `drift-detector-stack-processor` Lambda | `GetFunctionConfiguration` → `State == Active` |
| `ValidatorHealth` | `drift-detector-validator` Lambda | `GetFunctionConfiguration` → `State == Active` |
| `PrCreatorHealth` | `drift-detector-pr-creator` Lambda | `GetFunctionConfiguration` → `State == Active` |
| `ReconciliationsTableHealth` | `drift-detector-reconciliations` DynamoDB | `DescribeTable` → `TableStatus == ACTIVE` |
| `TenantsTableHealth` | `drift-detector-tenants` DynamoDB | `DescribeTable` → `TableStatus == ACTIVE` |
| `AuditBucketHealth` | S3 audit bucket | `HeadBucket` → no `ClientError` |
| `ProcessorDLQHealth` | `drift-detector-processor-dlq` SQS | `GetQueueAttributes` → no `ClientError` |

---

## Health Check System

**Function:** `drift-detector-health-check`  
**Schedule:** Every 5 minutes via EventBridge rule `drift-detector-health-check`  
**Source:** `lambdas/health_check/handler.py`

The health check Lambda is a lightweight probe-and-publish function.  It does not
invoke any pipeline Lambda or write any application data.  Each component check makes
a single read-only API call.  All 8 results are published to CloudWatch in a single
`PutMetricData` call, and a structured JSON report is written to CloudWatch Logs.

**IAM permissions granted (least-privilege):**

| Permission | Scope |
|---|---|
| `lambda:GetFunctionConfiguration` | The 4 pipeline function ARNs only |
| `dynamodb:DescribeTable` | The 2 table ARNs only |
| `s3:ListBucket` | The audit bucket ARN only |
| `sqs:GetQueueAttributes` | The DLQ ARN only |
| `cloudwatch:PutMetricData` | `*` (AWS does not support resource-level restriction) |
| `logs:*` | Standard CloudWatch Logs write |

**Log output per invocation (JSON):**
```json
{
  "event": "health_check",
  "overall_healthy": true,
  "unhealthy": [],
  "checks": {
    "ProcessorHealth":            { "healthy": true, "state": "Active", "lastUpdateStatus": "Successful" },
    "ValidatorHealth":            { "healthy": true, "state": "Active", "lastUpdateStatus": "Successful" },
    "ReconciliationsTableHealth": { "healthy": true, "tableStatus": "ACTIVE" },
    "AuditBucketHealth":          { "healthy": true },
    "ProcessorDLQHealth":         { "healthy": true }
  }
}
```

When any component is unhealthy the log line is emitted at `ERROR` level, making it
filterable in Log Insights and visible in the Lambda Errors metric for the health check
function itself.

---

## EventBridge Schedules

| Rule | Schedule | Target |
|---|---|---|
| `drift-detector-batch-schedule` | `cron(0 7 * * ? *)` — daily 7:00 AM UTC | `drift-detector-processor` |
| `drift-detector-health-check` | `rate(5 minutes)` | `drift-detector-health-check` |

---

## Terraform Source Files

| File | Contents |
|---|---|
| `terraform/saas/monitoring.tf` | Log groups, cold start metric filters, overview dashboard, cost dashboard, all pipeline alarms |
| `terraform/saas/health_check.tf` | Health check Lambda, IAM, EventBridge schedule, component health alarms, status dashboard |
| `terraform/saas/sqs.tf` | SQS DLQ definition and `processor-dlq-not-empty` alarm |
| `lambdas/health_check/handler.py` | Health check Lambda source code |
