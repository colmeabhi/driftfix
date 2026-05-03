# System Architecture & Workflow

This document explains the complete lifecycle of the Drift Detector — from how a customer is onboarded, to how a manual infrastructure change triggers a GitHub pull request, to how every event is tracked. It is intended as the single source of truth for understanding how all the pieces fit together.

---

## The Problem Being Solved

When an engineer makes a manual change in the AWS console — modifying an S3 bucket's encryption, updating a security group rule, resizing an RDS instance — that change is not reflected in the CloudFormation template that originally created the resource. The deployed infrastructure and the declared infrastructure in the repository are now out of sync. This is called **drift**.

Drift causes:
- The next `terraform apply` or CloudFormation update to silently revert the manual fix
- Engineers not knowing the true state of production
- Audit failures when declared state and actual state diverge

The Drift Detector solves this by automatically detecting when a manual change was made, identifying which CloudFormation stack owns that resource, generating an updated template that reflects the change, and opening a pull request in the customer's GitHub repository for an engineer to review and merge.

---

## Two AWS Accounts

```
Customer Account                          SaaS Account (274024437845)
────────────────                          ──────────────────────────
Actual infrastructure lives here          Application runs here
CloudTrail logs API calls                 Reads logs, generates PRs
IAM cross-account role                    Assumes that role to read
```

The SaaS account never has standing access to the customer account. Every time it needs to read something from the customer account, it temporarily assumes the cross-account IAM role using STS, which gives it time-limited credentials valid for 1 hour.

---

## Phase 1 — Onboarding

Before any drift can be detected, the customer must be registered.

### What the customer deploys (`terraform/customer/`)

Running `terraform apply` in the customer account creates:

1. **CloudTrail trail** — logs every AWS API call across all regions to an S3 bucket
2. **S3 bucket** — stores the CloudTrail log files (named `drift-detector-cloudtrail-<account-id>`)
3. **Cross-account IAM role** (`drift-detector-cross-account`) — allows the SaaS account to assume it, guarded by an `external_id`

The role grants read-only permissions:
- `cloudformation:GetTemplate / DescribeStacks / ListStacks` — to fetch stack templates
- `s3:GetObject / ListBucket / ListObjectsV2` — to read CloudTrail log files
- `ec2:DescribeTags`, `s3:GetBucketTagging`, `rds:ListTagsForResource`, etc. — to resolve which stack owns a changed resource via its `aws:cloudformation:stack-name` tag

### What the SaaS operator does

After the customer runs `terraform apply` and shares their outputs, the SaaS operator inserts a record into the DynamoDB tenants table:

```
Table: drift-detector-tenants
─────────────────────────────
tenant_id        "554598304668"         (customer AWS account ID)
role_arn         "arn:aws:iam::554598304668:role/drift-detector-cross-account"
external_id      "KORoCqh4tPRKw6T6..."  (shared secret, prevents confused deputy attacks)
cloudtrail_bucket "drift-detector-cloudtrail-554598304668"
github_repo      "customer-org/infra-repo"
```

This record is the complete tenant configuration. All four Lambda functions read from it at runtime. Once this record exists the tenant is fully onboarded — no code changes needed.

---

## Phase 2 — The Daily Batch Cycle

The system runs once per day at **7 AM UTC**, triggered by an EventBridge scheduled rule (`cron(0 7 * * ? *)`).

```
7:00 AM UTC
    │
    ▼
EventBridge cron rule
    │
    ▼
Orchestrator Lambda (drift-detector-processor)
    │
    ├── for each tenant in DynamoDB...
    │       │
    │       ├── assume cross-account role via STS
    │       ├── list CloudTrail log files from last 24h
    │       ├── parse + filter write events
    │       ├── deduplicate against reconciliations table
    │       ├── group events by stack (tag resolver)
    │       │
    │       └── for each affected stack...
    │               │
    │               ▼ (async invoke, parallel)
    │           Stack Processor Lambda
    │               │
    │               ├── assume cross-account role via STS
    │               ├── fetch CloudFormation template
    │               ├── call Bedrock → updated template
    │               │
    │               ▼
    │           Validator Lambda
    │               │
    │               ├── run cfn-lint
    │               ├── pass → store in S3
    │               ├── fail → retry with Bedrock (up to 3x)
    │               │
    │               ▼
    │           PR Creator Lambda
    │               │
    │               ├── read template from S3
    │               ├── create branch in GitHub
    │               ├── commit updated template
    │               ├── open pull request
    │               └── update DynamoDB reconciliation record
    │
    └── (next tenant...)
```

---

## Phase 3 — Orchestrator Lambda in Detail

**File:** `lambdas/processor/handler.py`
**Timeout:** 15 minutes
**Trigger:** EventBridge cron

The Orchestrator is the entry point for each batch run. It does not process stacks itself — its job is to figure out *what* needs processing and hand it off.

### Step 1 — Scan all tenants

Reads every record from the `drift-detector-tenants` DynamoDB table. Uses a paginated scan (1MB per page) so no tenants are silently dropped regardless of table size.

### Step 2 — Assume cross-account role

For each tenant, calls `sts:AssumeRole` with the tenant's `role_arn` and `external_id`. Gets back temporary credentials valid for 1 hour. These credentials are scoped to the customer account — they cannot be used to access any other account.

### Step 3 — Read CloudTrail logs

Using the temporary credentials, lists objects in the customer's CloudTrail S3 bucket under `AWSLogs/` modified in the last 24 hours. Each file is a gzip-compressed JSON containing up to hundreds of CloudTrail records.

### Step 4 — Filter write events

Each CloudTrail record has a `readOnly` flag and an `eventName`. The Orchestrator keeps only events where:
- `readOnly` is `false`
- `eventName` starts with `Create`, `Update`, `Delete`, `Put`, `Modify`, `Attach`, `Detach`, `Associate`, or `Disassociate`

This filters out ~90% of CloudTrail volume (DescribeInstances, ListBuckets, etc.) which represent reads that cannot cause drift.

### Step 5 — Deduplicate

Before processing, the Orchestrator calls `BatchGetItem` on the `drift-detector-reconciliations` table to check which `event_id` values have already been seen. Events that already have a record are skipped. This prevents reprocessing if the Lambda retries or the cron fires twice.

Events that pass deduplication are immediately written to the reconciliations table with `status: queued` — this acts as a lock so that even if the Lambda crashes and retries mid-run, those events won't be picked up again.

### Step 6 — Group events by stack

For each new write event, the Orchestrator needs to know which CloudFormation stack owns the resource that was changed. It does this via the **tag resolver** (`stack_resolver.py`).

Every resource deployed by CloudFormation carries the tag:
```
aws:cloudformation:stack-name → <stack-name>
```

The resolver reads this tag from the changed resource using the appropriate service API:

| Event source | API called | Field read |
|---|---|---|
| `s3.amazonaws.com` | `GetBucketTagging` | `bucketName` from requestParameters |
| `ec2.amazonaws.com` | `DescribeTags` | `instanceId`, `groupId`, `subnetId`, etc. |
| `rds.amazonaws.com` | `DescribeDBInstances` + `ListTagsForResource` | `dBInstanceIdentifier` |
| `lambda.amazonaws.com` | `GetFunction` + `ListTags` | `functionName` |
| `iam.amazonaws.com` | `ListRoleTags` | `roleName` |
| `sns.amazonaws.com` | `ListTagsForResource` | `topicArn` |
| `sqs.amazonaws.com` | `ListQueueTags` | `queueUrl` |
| `dynamodb.amazonaws.com` | `DescribeTable` + `ListTagsOfResource` | `tableName` |

For CloudFormation API calls (`UpdateStack`, `CreateStack`), the `stackName` is present directly in `requestParameters` — no tag lookup needed.

Events are then grouped: `{ stack_name → [event1, event2, ...] }`. Multiple changes to the same stack within the same day are batched together so only one PR is opened per stack per day.

### Step 7 — Invoke Stack Processors

For each unique stack, the Orchestrator invokes the Stack Processor Lambda **asynchronously**. All stacks across all tenants are dispatched in parallel — Lambda concurrency handles this automatically. The Orchestrator does not wait for results.

---

## Phase 4 — Stack Processor Lambda in Detail

**File:** `lambdas/stack_processor/handler.py`
**Timeout:** 5 minutes
**Trigger:** Async invoke from Orchestrator (one invocation per affected stack)
**Failure routing:** On failure, sends to `drift-detector-processor-dlq` SQS queue

Each Stack Processor invocation owns exactly one stack for one tenant. Multiple stacks run in parallel as separate Lambda instances.

### Step 1 — Assume cross-account role

Re-assumes the customer's cross-account role independently (does not reuse the Orchestrator's session). This is intentional — if the Orchestrator's 1-hour session expired before the async invoke executed, this ensures fresh credentials.

### Step 2 — Fetch CloudFormation template

Calls `cloudformation:GetTemplate` with `TemplateStage: Original` to get the template as it was when the stack was last deployed. This is the template that needs to be updated to reflect the drift.

### Step 3 — Call Bedrock *(not yet implemented)*

Sends a prompt to AWS Bedrock (Claude) containing:
- The current CloudFormation template
- All CloudTrail events for this stack from the last 24 hours
- A system prompt instructing the model to return an updated YAML template that reflects all the changes

Bedrock returns an updated template. This is the core AI step.

### Step 4 — Invoke Validator

Passes the updated template to the Validator Lambda asynchronously, along with the tenant config, stack name, and the representative CloudTrail event.

---

## Phase 5 — Validator Lambda in Detail

**File:** `lambdas/validator/handler.py`
**Timeout:** 2 minutes

The Validator ensures Bedrock did not produce a broken CloudFormation template before it reaches GitHub.

### The validation loop

```
Receive template from Stack Processor
        │
        ▼
    cfn-lint
        │
   ┌────┴────┐
 Pass       Fail
   │          │
   │      retry_count < MAX_RETRIES (3)?
   │          │
   │     ┌────┴────┐
   │    Yes        No
   │     │          │
   │   re-invoke   raise RuntimeError
   │   Bedrock     → message goes to DLQ
   │   with errors → CloudWatch alarm fires
   │   appended    → human investigates
   │
   ▼
Store template in S3
   └── Key: validated/<tenant_id>/<event_id>.yaml
        │
        ▼
Invoke PR Creator Lambda
```

The retry prompt appends cfn-lint's error output to the original prompt: *"Your previous attempt had these errors, fix them: [errors]"*. This iterative refinement loop is capped at 3 attempts.

If all retries are exhausted, the Lambda raises an exception. The SQS DLQ receives the failed invocation record, and a CloudWatch alarm fires when the DLQ is non-empty.

---

## Phase 6 — PR Creator Lambda in Detail

**File:** `lambdas/pr_creator/handler.py`
**Timeout:** 1 minute

### Step 1 — Read validated template from S3

Fetches the template stored by the Validator from the audit bucket at `validated/<tenant_id>/<event_id>.yaml`.

### Step 2 — Get GitHub token

Retrieves the GitHub personal access token from AWS Secrets Manager (`drift-detector/github-token`). The token is cached in Lambda memory for the lifetime of the execution environment — subsequent invocations in the same warm instance skip the Secrets Manager call.

### Step 3 — Create branch (idempotent)

Creates a branch named `drift/<tenant_id>/<event_id[:8]>`. The branch name is deterministic — if the Lambda retries, it hits a 422 (branch already exists) and continues rather than creating a duplicate.

### Step 4 — Commit updated template

Commits the updated YAML template to the branch. If the file already exists on the branch (retry scenario), it updates it using the file's current SHA.

### Step 5 — Open pull request (idempotent)

Opens a PR with title `[Drift] <eventName> detected — template updated` and a description table showing the event name, time, and the actor who made the change.

If a PR already exists for this branch (retry), it finds and returns the existing PR URL rather than trying to open a duplicate.

### Step 6 — Update reconciliation record

Updates the tenant's event record in `drift-detector-reconciliations`:
```
status:  "pr_opened"
pr_url:  "https://github.com/owner/repo/pull/123"
```

---

## Data Stores

### DynamoDB — `drift-detector-tenants`

| Field | Type | Description |
|---|---|---|
| `tenant_id` | String (PK) | Customer AWS account ID |
| `role_arn` | String | ARN of the cross-account IAM role |
| `external_id` | String | Shared secret for confused deputy protection |
| `cloudtrail_bucket` | String | S3 bucket name in the customer account |
| `github_repo` | String | `owner/repo` of the customer's IaC repository |

Written once at onboarding. Read by the Orchestrator at the start of every batch run.

### DynamoDB — `drift-detector-reconciliations`

| Field | Type | Description |
|---|---|---|
| `tenant_id` | String (PK) | Customer AWS account ID |
| `event_id` | String (SK) | CloudTrail event UUID |
| `status` | String | `queued` → `pr_opened` (or failed path) |
| `queued_at` | String | ISO timestamp when the Orchestrator queued this event |
| `pr_url` | String | GitHub PR URL (set by PR Creator) |

This table serves two purposes:
1. **Deduplication** — the Orchestrator checks this table before processing any event to prevent reprocessing
2. **Audit trail** — every event that was ever processed is recorded here with its outcome

A GSI on `(status, processed_at)` allows querying all events by status (e.g. "show me all events that opened a PR today").

### S3 — `drift-detector-audit-<account-id>`

| Path | Written by | Read by |
|---|---|---|
| `validated/<tenant_id>/<event_id>.yaml` | Validator | PR Creator |

Versioning is enabled. Stores every validated CloudFormation template before it is committed to GitHub. Acts as an audit log of what the system generated.

### Secrets Manager — `drift-detector/github-token`

Holds the GitHub personal access token used by the PR Creator to authenticate with the GitHub API. Stored as JSON: `{"token": "ghp_..."}`.

### SQS — `drift-detector-processor-dlq`

Dead-letter queue for failed async Lambda invocations from the Orchestrator and Stack Processor. A CloudWatch alarm fires when this queue is non-empty. A human must inspect the failed invocation in CloudWatch Logs and decide whether to replay or discard.

---

## The Full Data Flow — One Event End to End

```
2:00 AM — Engineer runs PutBucketEncryption on "my-prod-bucket" in the console
    │
    ▼
CloudTrail records the API call → writes to S3 log file within 15 min
    │
    ▼ (next day)
7:00 AM UTC — EventBridge fires cron
    │
    ▼
Orchestrator assumes cross-account role (1hr session)
    │
Orchestrator lists S3 log files from last 24h → finds the file containing the event
    │
Orchestrator parses gzip JSON → extracts PutBucketEncryption record
    │
Orchestrator checks reconciliations table → event_id not seen before → passes dedup
    │
Orchestrator calls s3:GetBucketTagging on "my-prod-bucket"
    → reads tag aws:cloudformation:stack-name = "prod-storage-stack"
    │
Orchestrator writes {tenant_id, event_id, status: "queued"} to reconciliations
    │
Orchestrator invokes Stack Processor async with:
    {tenant, stack_name: "prod-storage-stack", cloudtrail_events: [...]}
    │
    ▼ (parallel Lambda invocation)
Stack Processor assumes cross-account role (fresh 1hr session)
    │
Stack Processor calls cloudformation:GetTemplate on "prod-storage-stack"
    → gets the current YAML template
    │
Stack Processor calls Bedrock with template + CloudTrail event
    → Bedrock returns updated YAML with encryption change reflected
    │
Stack Processor invokes Validator async
    │
    ▼
Validator runs cfn-lint on the updated template → passes
    │
Validator stores template at:
    s3://drift-detector-audit-274024437845/validated/554598304668/<event_id>.yaml
    │
Validator invokes PR Creator async
    │
    ▼
PR Creator reads template from S3
    │
PR Creator gets GitHub token from Secrets Manager
    │
PR Creator creates branch: drift/554598304668/a3f8c2d1
    │
PR Creator commits updated template.yaml to branch
    │
PR Creator opens PR:
    Title: "[Drift] PutBucketEncryption detected — template updated"
    Body:  event name, time, actor IAM ARN
    │
PR Creator updates reconciliations:
    status: "pr_opened"
    pr_url: "https://github.com/customer-org/infra-repo/pull/47"
    │
    ▼
Engineer receives PR notification from GitHub
    │
Engineer reviews: "yes, this was intentional" → merges
    │
Drift reconciled. Declared state matches actual state.
```

---

## Why Each Piece Exists

| Component | Problem it solves |
|---|---|
| **CloudTrail** | Source of truth for what changed — every API call in the customer account is logged |
| **Cross-account IAM role + ExternalId** | Lets the SaaS read from the customer account without storing long-lived credentials |
| **EventBridge cron** | Batches all changes from one day into a single run rather than reacting to every event in real time |
| **Orchestrator Lambda** | Reads logs for all tenants, deduplicates, groups by stack, fans out — keeps each downstream Lambda focused on a single unit of work |
| **Tag resolver** | Identifies which CFn stack owns a changed resource without scanning every stack — O(1) tag lookup instead of O(stacks × resources) |
| **Deduplication via reconciliations table** | Prevents the same event from being processed twice if the Lambda retries or the cron fires unexpectedly |
| **Stack Processor per stack** | One Lambda invocation per affected stack — Lambda concurrency makes them run in parallel automatically |
| **Bedrock (Claude)** | Understands both the CloudTrail event (what changed) and the CloudFormation template (how it was declared) and generates a corrected template |
| **cfn-lint validation loop** | Catches Bedrock hallucinations before they reach GitHub — an invalid template in a PR is worse than no PR |
| **S3 audit bucket** | Stores every generated template for 90 days — audit trail, retry capability, and debugging |
| **Deterministic branch names** | Idempotency — Lambda retries never create duplicate branches or PRs |
| **DLQ + CloudWatch alarm** | Events that fail all retries don't disappear silently — a human is notified to investigate |
| **GitHub PR** | Puts the human engineer back in the loop — the system proposes a fix, the engineer approves it |

---

## What Is Not Yet Implemented

| Item | Status | Impact |
|---|---|---|
| Bedrock integration | Stubbed — raises `NotImplementedError` | Stack Processor cannot generate updated templates yet — the pipeline stops here |
| Template file path resolution | Hardcoded to `template.yaml` | PR Creator always commits to `template.yaml` regardless of the actual file path in the repo |
| Budget alerts | Not configured | No email notification if spend exceeds threshold |
