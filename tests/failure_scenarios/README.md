# Failure Scenario Test Suite

Each script directly invokes the deployed Validator Lambda (`drift-detector-validator`)
and emulates the async retry/DLQ behavior of the production pipeline.  All scripts
require `AWS_PROFILE` (or ambient credentials) to be set before running.

```
export AWS_PROFILE=<saas-account-profile>
```

CloudWatch evidence (Lambda Errors, Throttles, Duration p99, DLQ Depth) is visible on
the **drift-detector-overview** and **drift-detector-cost** dashboards at:
`https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1#dashboards`

---

## Scenario 1 — Poison Pill: Invalid CloudFormation Template

**File:** `scenario_1_poison_pill.py`

### Description

A CloudFormation template containing four deliberate cfn-lint violations is delivered
to the Validator Lambda.  Because the Bedrock-powered fix loop is not yet implemented,
the Validator raises `NotImplementedError` on every attempt.  The script emulates
Lambda's async invocation behavior: one original delivery followed by two automatic
retries, all carrying the same `retry_count=0` payload (Lambda re-delivers the
original event unchanged).  After all three attempts fail, the script writes a
`RetriesExhausted` message to the SQS DLQ (`drift-detector-processor-dlq`), matching
the structure Lambda uses for its on-failure async destination.

### Expected System Behavior

| Phase | Behavior |
|---|---|
| Delivery 1–3 | Validator receives broken template, cfn-lint reports violations, `NotImplementedError` raised → `FunctionError` returned each time |
| After attempt 3 | Script routes event to SQS DLQ (approximateInvokeCount = 3) |
| CloudWatch | Lambda/Errors spike of 3 on `drift-detector-validator`; DLQ depth = 1 |
| Alarm | `drift-detector-validator-errors` transitions to ALARM (threshold = 0) |
| Recovery | Re-invoking with the fixed template returns `status=passed`; errors return to 0 |

### How to Run

**Step 1 — trigger the failure:**

```bash
python tests/failure_scenarios/scenario_1_poison_pill.py
```

No parameters are required.  The script prints an `event_id` at the end of the trigger
step — note it for the recovery step.

**Step 2 — recover (re-invoke with the fixed template):**

```bash
python tests/failure_scenarios/scenario_1_poison_pill.py \
  --recover --event-id <event_id-from-step-1>
```

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--retry-delay <secs>` | 5 | Base seconds between retry attempts (production Lambda: ~60–120 s) |
| `--no-evidence` | off | Skip the 90-second CloudWatch polling wait |

---

## Scenario 2 — Concurrency Throttle: Account-Level Exhaustion

**File:** `scenario_2_throttle.py`

### Description

This account has an AWS-minimum Lambda concurrency limit of 10 concurrent executions.
The script fires 40 concurrent Validator invocations simultaneously (4× the limit),
exhausting all 10 available slots.  The remaining 30 invocations are rejected
immediately with `TooManyRequestsException`.  Each throttled invocation emulates
Lambda's async retry behavior with exponential backoff.  Because 4× the account limit
retries across multiple waves, the load stays above the concurrency ceiling through all
retry rounds, guaranteeing that some events exhaust all retries and are routed to the
DLQ.

**Retry wave pattern with flood=40, limit=10:**

```
Wave 0 (t= 0s): ~10 succeed,  ~30 throttled
Wave 1 (t=10s): ~10 recover,  ~20 throttled
Wave 2 (t=30s): ~10 recover,  ~10 → DLQ (retries exhausted)
```

### Expected System Behavior

| Phase | Behavior |
|---|---|
| Wave 0 | ~10 invocations claim all concurrency slots and succeed; ~30 receive `TooManyRequestsException` immediately |
| Waves 1–2 | Throttled events retry with exponential backoff; some recover as slots free up; persistent throttles eventually exhaust retries |
| After retries | ~10 events routed to SQS DLQ as `RetriesExhausted` messages |
| CloudWatch | Lambda/Throttles spike ~30; Lambda/Invocations < 40 (throttled requests are never executed); DLQ depth ~10 |
| Alarm | `drift-detector-validator-throttled` transitions to ALARM (threshold = 0) |
| Recovery | Throttling is transient and self-resolves as executing invocations complete; DLQ events require manual replay |

### How to Run

```bash
python tests/failure_scenarios/scenario_2_throttle.py \
  --flood 40 \
  --retry-delay 10
```

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--flood <n>` | 40 | Concurrent invocations to fire (must be > account limit to guarantee throttles) |
| `--retry-delay <secs>` | 5 | Base seconds between retry attempts; doubles each wave (production Lambda: ~60–120 s) |
| `--no-evidence` | off | Skip the 90-second CloudWatch polling wait |

---

## Scenario 3 — AZ Resilience: Simulated Compute Degradation

**File:** `scenario_3_az_resilience.py`

### Description

In a real single-AZ failure, Lambda execution environments on the degraded AZ
experience CPU pressure — slower instruction execution and elevated I/O wait.  The
observable effect is elevated Duration p99 with zero application-level errors: AWS
routes subsequent invocations to healthy AZs transparently.

AWS Fault Injection Service (`aws:lambda:invocation-add-delay`) is the native
simulation tool but is blocked in student accounts by IAM/SCP restrictions.  Instead,
the script temporarily reduces the Validator Lambda's memory from 256 MB to a lower
allocation.  Lambda vCPU is directly proportional to memory, so the reduction slows
CPU throughput.  cfn-lint's validation subprocess runs measurably slower, producing
elevated Duration p50/p99 while keeping errors at zero — reproducing the exact
observable signal of real AZ compute degradation.

The Lambda function `Timeout` is simultaneously extended to 600 s (from 120 s) during
the degradation window so slow cold-starts at reduced memory are not killed prematurely.
Both settings are restored by the script's `finally` block regardless of outcome.

### Expected System Behavior

| Phase | Behavior |
|---|---|
| Baseline | 2 concurrent invocations at 256 MB complete with normal Duration |
| Degradation injected | MemorySize → 224 MB; Timeout → 600 s; all warm execution environments recycled |
| Degraded invocations | 4 concurrent invocations at 224 MB complete successfully (status=passed); Duration p99 elevated vs baseline (CPU reduction signal) |
| CloudWatch | Lambda/Duration p99 elevated during degradation window; Lambda/Errors = 0 throughout |
| Recovery | MemorySize and Timeout restored to original values; post-recovery invocations confirm Duration normalizes |

### How to Run

```bash
python tests/failure_scenarios/scenario_3_az_resilience.py \
  --degraded-memory 224 \
  --concurrent 4
```

> Note: at 224 MB, cfn-lint cold-starts may take 60–120 s per invocation.  The script
> waits for all workers to complete before printing results.  Allow up to 10 minutes
> for the degraded phase.

**Optional flags:**

| Flag | Default | Description |
|---|---|---|
| `--degraded-memory <MB>` | 128 | Lambda memory during degradation window (must be < current 256 MB) |
| `--concurrent <n>` | 4 | Concurrent invocations during degraded phase (keep ≤ 4 to avoid account throttles) |
| `--no-evidence` | off | Skip the 90-second CloudWatch polling wait |

**Manual restore** (if the script is interrupted before the `finally` block runs):

```bash
aws lambda update-function-configuration \
  --function-name drift-detector-validator \
  --region us-east-1 \
  --memory-size 256 \
  --timeout 120
```

---

## Quick Reference

| Scenario | Failure Type | Primary Signal | DLQ? |
|---|---|---|---|
| 1 — Poison Pill | Application error (cfn-lint violations) | Lambda/Errors spike (×3) | Yes — 1 message after retries |
| 2 — Throttle | Resource limit (concurrency exhaustion) | Lambda/Throttles spike (~30); Invocations < flood | Yes — ~10 messages |
| 3 — AZ Degradation | Infrastructure (compute slowdown) | Duration p99 elevated; Errors = 0 | No |
