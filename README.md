# IaC Drift Reconciliation SaaS

Automatically detects when manual AWS console changes cause infrastructure drift, generates corrected CloudFormation templates using an LLM, and opens GitHub pull requests for engineers to review and merge.

When an engineer makes an out-of-band change in the AWS Console — modifying a security group, resizing an instance, updating a bucket policy — that change is not reflected in the CloudFormation template that originally created the resource. This system detects the gap daily, generates an updated template that reflects the change, and proposes it as a pull request, keeping declared state and actual state in sync.

---

## High-Level Deployment

The system spans two AWS accounts and a hosted customer portal.

### SaaS Pipeline

Deployed via `terraform/saas/`. The pipeline runs on a daily EventBridge schedule:

```
EventBridge (7 AM UTC)
    └── Orchestrator Lambda
            ├── Reads CloudTrail logs from each customer's S3 bucket (cross-account)
            ├── Deduplicates events against DynamoDB
            ├── Groups changes by CloudFormation stack
            └── Fans out one Stack Processor Lambda per affected stack (parallel)
                    └── Stack Processor
                            ├── Calls Amazon Bedrock to generate updated CFn template
                            └── Validator Lambda
                                    ├── Runs cfn-lint (up to 3 retries with Bedrock)
                                    └── PR Creator Lambda
                                            ├── Commits updated template to GitHub branch
                                            └── Opens pull request
```

A **Health Check Lambda** runs every 5 minutes independently, probing all pipeline components and publishing custom `drift-detector/HealthCheck` metrics to CloudWatch.

### Customer Portal

A self-service onboarding portal for new customers. Built as a containerized FastAPI + React app:

- Source: `admin_ui/app.py` (backend) + `ui/customer-portal/` (frontend)
- Built with Docker (`Dockerfile`) and pushed to Amazon ECR
- Deployed on **AWS App Runner** — no servers to manage, scales automatically
- Live URL: `https://YOUR_APP_RUNNER_URL/portal/`

Customers use the portal to generate a CloudFormation template, deploy it in their own AWS account, and connect their GitHub repository. The portal handles tenant registration automatically.

### Customer-Side Infrastructure

Each customer deploys `terraform/customer/` in their own AWS account, which creates:

- A **CloudTrail trail** logging all API calls to an S3 bucket
- An **S3 bucket** storing CloudTrail log files
- A **cross-account IAM role** allowing the SaaS account to read logs and CloudFormation templates via STS `AssumeRole`

---

## Cloud Services Used

| Service | Role |
|---|---|
| **AWS Lambda** | Five pipeline functions: orchestrator, stack_processor, validator, pr_creator, health_check |
| **Amazon DynamoDB** | `drift-detector-tenants` (customer config) and `drift-detector-reconciliations` (event audit trail) |
| **Amazon S3** | CloudTrail log storage (customer account) and validated template storage (SaaS account) |
| **AWS CloudTrail** | Source of truth — logs every AWS API call in the customer account |
| **Amazon Bedrock (Nova Lite)** | LLM that generates updated CloudFormation templates from CloudTrail events |
| **AWS EventBridge** | Daily cron trigger (`cron(0 7 * * ? *)`) for the batch pipeline |
| **AWS SQS** | Dead-letter queue for failed Lambda invocations — triggers a CloudWatch alarm when non-empty |
| **Amazon CloudWatch** | Dashboards, alarms, metric filters, log groups, and custom health metrics |
| **AWS SNS** | Alarm notifications for pipeline failures and DLQ events |
| **AWS Secrets Manager** | Stores GitHub personal access tokens for PR creation |
| **AWS STS** | Cross-account role assumption — gives time-limited credentials scoped to each customer account |
| **AWS IAM** | Cross-account trust policies, Lambda execution roles, least-privilege access |
| **AWS App Runner** | Hosts the customer portal container — managed, auto-scaling, no cluster to configure |
| **Amazon ECR** | Docker image registry for the customer portal container |
| **AWS CloudFormation** | The customer IaC stacks being monitored for drift |

---

## Observability

All observability infrastructure is defined in `terraform/saas/monitoring.tf` and `terraform/saas/health_check.tf`. Full details in [`docs/observability.md`](docs/observability.md).

### Health Check Lambda

`lambdas/health_check/` runs on a 5-minute EventBridge schedule. It probes all pipeline components (DynamoDB, SQS, S3, Secrets Manager, Lambda) and publishes a custom `drift-detector/HealthCheck` metric per component. A value of `1` means healthy; `0` triggers an alarm.

### Public CloudWatch Dashboards

Three dashboards are publicly accessible without AWS credentials:

| Dashboard | Purpose |
|---|---|
| `drift-detector-status` | Live component health alarm board — recommended entry point for reviewers |
| `drift-detector-overview` | Lambda invocations, errors, duration p99/p95, throttles, cold starts, concurrency |
| `drift-detector-cost` | Lambda compute cost estimate; automatically reflects the selected time window |

### Alarms

- **8 health check alarms** — one per infrastructure component; fire when the `HealthCheck` metric drops to 0
- **9 pipeline alarms** — cover Lambda errors, throttles, timeouts, and DLQ depth
- All alarms notify via SNS; DLQ depth alarm fires when any message lands in the queue

---

## How Scaling and Failure Demonstrations Were Performed

Five structured load test scenarios were run against a live customer AWS account with 10 pre-deployed CloudFormation stacks on 2026-05-04. Three dedicated failure scenarios were run separately using the scripts in `tests/failure_scenarios/`. Full load test results are in [`results/report.md`](results/report.md).

### Drift Induction

Drift was induced using [`scripts/load_test/run_load_test.sh`](scripts/load_test/run_load_test.sh), which applies out-of-band AWS API changes directly to customer CloudFormation stacks (e.g. `PutBucketEncryption`, `ModifyDBInstance`). CloudTrail captures these changes within ~60 seconds. Between scenarios, `reset-stacks.sh` destroys and redeploys all test stacks to a clean state.

The Orchestrator Lambda was invoked manually to trigger the pipeline immediately, mimicking the daily EventBridge schedule.

### Load Test Scenarios

| Scenario | Stacks | Purpose |
|---|---|---|
| T1 — Functional | 1 | Verify the full end-to-end pipeline for a single stack |
| T2 — Scaling | 3 | Confirm Lambda scales horizontally with parallel stacks |
| T3 — Performance | 10 | Measure P95/P99 latency and throughput at peak load |
| T4 — Failure | 10 + 1 bad tenant | Verify failure isolation — bad tenant must not block valid stacks |
| T5 — Security | 6 | Audit CloudWatch logs for credential or secret leakage |

### Scaling Behavior

Lambda concurrency scaled exactly 1:1 with the number of drifted stacks in every scenario — from 1 concurrent execution (T1) to 10 (T3) — with zero throttles and no manual provisioning. Throughput grew from ~16 stacks/min at 1 stack to ~39.5 stacks/min at 10 stacks (2.5× gain for 10× load).

The sole bottleneck under load is **Amazon Bedrock tail latency**: P50 median stayed stable at 4–5 s regardless of load, but P99 grew from 1.7 s (1 stack) to 14.9 s (10 stacks) as parallel Bedrock requests competed for inference capacity. Lambda errors and throttles were zero across all runs.

### Failure Isolation (T4)

A tenant record with an invalid IAM role ARN was injected into DynamoDB. The Orchestrator rejected it at the processor stage — the bad tenant was logged as an error and not forwarded to any stack processor. All 10 valid stacks completed normally with zero errors.

### Security Audit (T5)

CloudWatch Logs were audited for patterns: `AKIA`, `ghp_`, `password`, `secret_key`, `aws_secret`. None were found across any Lambda log group. All secrets are stored in AWS Secrets Manager and fetched at runtime — never logged.

### Dedicated Failure Scenarios

Three targeted failure scenarios were run using scripts in [`tests/failure_scenarios/`](tests/failure_scenarios/). Each directly invokes the Validator Lambda and emulates async retry and DLQ behavior. Full documentation in [`tests/failure_scenarios/README.md`](tests/failure_scenarios/README.md).

**Scenario 1 — Poison Pill** (`scenario_1_poison_pill.py`)

A CloudFormation template with 4 deliberate cfn-lint violations is delivered to the Validator. The validator fails on each of 3 attempts, then a `RetriesExhausted` message is routed to the SQS DLQ — matching Lambda's on-failure async destination behavior.

- Primary signal: Lambda/Errors spike of 3 on `drift-detector-validator`
- DLQ depth: 1 message after retries exhausted
- Alarm: `drift-detector-validator-errors` transitions to ALARM

**Scenario 2 — Concurrency Throttle** (`scenario_2_throttle.py`)

40 concurrent Validator invocations are fired against a 10-concurrency account limit. ~30 are immediately rejected with `TooManyRequestsException`. Retry waves with exponential backoff exhaust remaining attempts, routing ~10 events to the DLQ.

- Primary signal: Lambda/Throttles spike ~30; Invocations < 40 (throttled requests never execute)
- DLQ depth: ~10 messages
- Alarm: `drift-detector-validator-throttled` transitions to ALARM

**Scenario 3 — AZ Resilience** (`scenario_3_az_resilience.py`)

Simulates compute degradation from a single-AZ failure by temporarily reducing Validator Lambda memory from 256 MB to 224 MB (Lambda vCPU is proportional to memory), slowing cfn-lint execution. Settings are restored automatically by the script's `finally` block.

- Primary signal: Duration p99 elevated during degradation window
- Lambda/Errors: 0 throughout (no application failures, only slowdown)
- DLQ: none

**Summary:**

| Scenario | Failure Type | Primary Signal | DLQ? |
|---|---|---|---|
| 1 — Poison Pill | Application error (cfn-lint violations) | Lambda/Errors ×3 | Yes — 1 message |
| 2 — Throttle | Resource exhaustion (concurrency limit) | Lambda/Throttles ~30 | Yes — ~10 messages |
| 3 — AZ Degradation | Infrastructure slowdown | Duration p99 elevated, Errors = 0 | No |

### Performance Graphs

Generated by [`scripts/load_test/graphs_from_csv.py`](scripts/load_test/graphs_from_csv.py) from CloudWatch metric exports. Charts are in [`results/`](results/).

| Graph | Shows |
|---|---|
| G1 — Bedrock Latency | P50/P95/P99 Bedrock response time per scenario |
| G2 — Latency Timeline | P50 vs P99 divergence across scenarios |
| G3 — Throughput | Stacks processed per minute per scenario |
| G4 — Concurrency | Peak Lambda concurrent executions per scenario |
| G5 — Error Rate | Errors and DLQ messages per scenario |
| G6 — Scaling Response | Bedrock latency vs concurrent stack count |
| G7 — Resource Utilization | Lambda invocations and duration per pipeline stage |

---

## Division of Work

| Member | Responsibilities |
|---|---|
| | |
| | |
| | |

---

## Repository Structure

```
.
├── admin_ui/               FastAPI backend for the customer portal
│   ├── app.py
│   └── requirements.txt
├── docs/                   Architecture and onboarding documentation
│   ├── system-architecture.md
│   ├── customer-onboarding.md
│   └── observability.md
├── lambdas/                Lambda function source code
│   ├── processor/          Orchestrator — reads CloudTrail, fans out to stack processors
│   ├── stack_processor/    Fetches CFn template, calls Bedrock, invokes validator
│   ├── validator/          Runs cfn-lint, retries with Bedrock up to 3×
│   ├── pr_creator/         Creates GitHub branch, commits template, opens PR
│   └── health_check/       Probes all pipeline components every 5 minutes
├── results/                Load test artifacts
│   ├── report.md           Full load test report
│   ├── G1–G7_*.png         Performance graphs
│   ├── *.csv               Raw CloudWatch metric exports
│   └── scenario_log.json   Per-scenario run log
├── scripts/
│   ├── package_lambdas.sh  Packages Lambda functions for deployment
│   └── load_test/          Load test scripts and graph generation
├── terraform/
│   ├── saas/               SaaS-side infrastructure (Lambda, DynamoDB, SQS, EventBridge,
│   │                       CloudWatch dashboards/alarms, health check)
│   └── customer/           Customer-side infrastructure (CloudTrail, S3, IAM cross-account role)
├── tests/
│   ├── e2e.py              End-to-end integration tests
│   └── failure_scenarios/  Dedicated failure scenario scripts
│       ├── README.md
│       ├── scenario_1_poison_pill.py
│       ├── scenario_2_throttle.py
│       └── scenario_3_az_resilience.py
├── ui/
│   └── customer-portal/    React + Vite + Tailwind CSS customer onboarding portal
├── Dockerfile              Multi-stage build: React frontend + FastAPI backend
├── .env.example            Environment variable template
└── .gitignore
```
