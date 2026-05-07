# Load Test Report — IaC Drift Reconciliation SaaS

**Generated:** 2026-05-04  
**Graphs:** G1–G7 (see `results/G*.png`)  
**Rubric Coverage:** Throughput · Latency P95/P99 · Scaling Response · Workload Methodology · Resource Utilization · Bottleneck Analysis

---

## 1. Workload Methodology

The SaaS processes IaC drift in **batch mode**: the orchestrator Lambda scans CloudTrail logs once, groups events by stack, and invokes one `stack-processor` Lambda **per affected stack in parallel**. Load is controlled by the number of drifted stacks on the customer account.

| Step | What happens |
|------|-------------|
| Drift induction | `drift-inducer.sh` applies out-of-band AWS API changes to customer CloudFormation stacks. CloudTrail captures these within ~60 s. |
| Trigger | `drift-detector-processor` Lambda is invoked manually (mimicking the daily EventBridge schedule). It reads CloudTrail, deduplicates against DynamoDB, and fans out one async `stack-processor` per affected stack. |
| Processing | Each `stack-processor` fetches the original template from S3/GitHub, calls Amazon Bedrock (Nova Lite) to generate a remediation template, invokes `validator`, then `pr-creator`. |
| Reset | `reset-stacks.sh` destroys and redeploys all test stacks between scenarios. |

Five structured scenarios were run on **2026-05-04** against a customer AWS account with 10 pre-deployed CloudFormation stacks.

---

## 2. Test Cases

### T1 — Functional
- **Objective:** Verify end-to-end pipeline for a single stack (Bedrock call → validator → PR creation).
- **Load:** 1 stack, single drift event.
- **Result:** Pipeline completed successfully. Bedrock responded in 1,710 ms, validator accepted the template, PR created. Zero errors.

### T2 — Scaling
- **Objective:** Confirm Lambda scales horizontally when multiple stacks are processed in parallel.
- **Load:** 3 stacks drifted concurrently.
- **Result:** 3 parallel `stack-processor` invocations. Concurrency peaked at 3. P99 Bedrock latency grew to 7,414 ms — first sign of tail latency from parallel Bedrock requests.

### T3 — Performance
- **Objective:** Measure peak P95/P99 and throughput at maximum drift event density.
- **Load:** 10 stacks, 3 rounds of increasing drift intensity.
- **Result:** 10 parallel invocations, concurrency peaked at 10. P99 Bedrock latency = 13,260 ms. Lambda Duration P99 = 15.2 s. Throughput = ~39.5 stacks/min. Zero errors.

### T4 — Failure
- **Objective:** Verify error isolation — a bad tenant config must not block valid stacks.
- **Load:** 10 stacks + 1 injected bad DynamoDB tenant (`load-test-bad-tenant` with invalid IAM role).
- **Result:** Bad tenant was rejected at the processor stage (ERROR logged, not forwarded to stack-processor). The 10 valid stacks processed normally. P99 = 14,855 ms (slightly higher than T3, consistent with 10 concurrent Bedrock calls). Zero Lambda errors on valid stacks.

### T5 — Security
- **Objective:** Confirm no secrets or credentials are leaked to CloudWatch logs.
- **Load:** 6 stacks + CloudWatch log audit.
- **Result:** Patterns `AKIA`, `ghp_`, `password`, `secret_key`, `aws_secret` — **none found** in logs. Security scan passed. Pipeline completed 6 stacks without errors.

---

## 3. Throughput

> Formula: `concurrent_stacks ÷ (Lambda_Duration_P99_ms ÷ 60,000)`  
> Since stacks are processed in parallel, all stacks in a batch complete within ~P99 time.

| Scenario | Stacks | Lambda P99 (s) | Throughput (stacks/min) |
|----------|--------|----------------|-------------------------|
| T1: Functional   | 1  | 3.8 s  | **15.9** |
| T2: Scaling      | 3  | 9.4 s  | **19.2** |
| T3: Performance  | 10 | 15.2 s | **39.5** |
| T4: Failure      | 10 | 16.4 s | **36.6** |
| T5: Security     | 6  | 11.6 s | **31.0** |

Throughput scales well with concurrency — T3 and T4 achieve ~37–40 stacks/min at 10 parallel stacks, showing effective horizontal scaling.

---

## 4. Latency — P50 / P95 / P99

### 4a. Bedrock Converse API Latency (ms)

| Scenario | Samples | P50 | P95 | P99 | Max |
|----------|---------|-----|-----|-----|-----|
| T1: Functional   | 1  | 1,710 | 1,710  | **1,710**  | 1,710  |
| T2: Scaling      | 3  | 4,607 | 7,185  | **7,414**  | 7,471  |
| T3: Performance  | 10 | 5,114 | 12,542 | **13,260** | 13,439 |
| T4: Failure      | 10 | 5,284 | 14,692 | **14,855** | 14,896 |
| T5: Security     | 7  | 4,343 | 9,168  | **9,664**  | 9,788  |

P50 stays stable at 4–5 s across T2–T5, but **P99 grows significantly with concurrency** — from 1.7 s (1 stack) to 14.9 s (10 stacks). This is tail latency from parallel Bedrock requests competing for inference capacity.

### 4b. Lambda Duration P99 (ms) — Stack Processor

Includes S3 fetch + Bedrock call + validator invoke + PR creation.

| Scenario | Lambda Duration P99 |
|----------|---------------------|
| T1: Functional   | **3,764 ms** |
| T2: Scaling      | **9,374 ms** |
| T3: Performance  | **15,164 ms** |
| T4: Failure      | **16,417 ms** |
| T5: Security     | **11,614 ms** |

---

## 5. Scaling Response

Lambda scaled from 1 to 10 concurrent executions with **no manual provisioning**, confirming automatic horizontal scale-out.

| Scenario | Stacks | Peak Concurrency | Bedrock P95 (ms) | Bedrock P99 (ms) |
|----------|--------|-----------------|------------------|------------------|
| T1: Functional   | 1  | 1  | 1,710  | 1,710  |
| T2: Scaling      | 3  | 3  | 7,185  | 7,414  |
| T3: Performance  | 10 | 10 | 12,542 | 13,260 |
| T4: Failure      | 10 | 10 | 14,692 | 14,855 |
| T5: Security     | 6  | 6  | 9,168  | 9,664  |

Lambda concurrency scaled 1:1 with the number of drifted stacks in every scenario. There were **zero throttles** recorded across all runs.

---

## 6. Resource Utilization

### Lambda Invocations per Pipeline Stage

| Scenario | Processor | Stack Processor | Validator | PR Creator |
|----------|-----------|-----------------|-----------|------------|
| T1: Functional   | — | 1  | 1  | 1  |
| T2: Scaling      | 1 | 3  | 3  | 3  |
| T3: Performance  | 1 | 10 | 10 | 10 |
| T4: Failure      | 1 | 10 | 10 | 10 |
| T5: Security     | 1 | 7  | 7  | 7  |

The **1:1:1 ratio** across stack-processor, validator, and pr-creator in every scenario confirms no invocations were dropped between pipeline stages.

### Lambda Duration P99 Summary

The `stack-processor` function dominates end-to-end latency. Duration grows linearly with the number of concurrent stacks due to Bedrock tail latency (see Section 5). Memory utilization was stable at ~93–94 MB across all invocations, well within the 256 MB allocation.

---

## 7. Bottleneck Analysis

### Primary Bottleneck — Amazon Bedrock (Tail Latency)

Bedrock is the dominant latency source. The **P50 median stays around 4–5 s** regardless of load (1–10 stacks), but **P99 grows from 1.7 s to 14.9 s** as concurrency increases from 1 to 10. This classic "tail at scale" pattern indicates that when 10 Lambdas simultaneously call Bedrock, the slowest 1% of requests experience significant queueing.

| Factor | Observation |
|--------|-------------|
| Bedrock P50 (stable) | ~4,300–5,300 ms across T2–T5 — Bedrock median is healthy |
| Bedrock P99 degradation | 1,710 ms → 14,855 ms from T1 → T4 (8.7× increase for 10× concurrency) |
| Lambda errors | **0** across all scenarios |
| Lambda throttles | **0** — Lambda scaled freely |
| Error isolation (T4) | Bad tenant was caught at the processor stage; valid stacks unaffected |
| Credential leakage (T5) | None detected in CloudWatch logs |

### Recommendations

1. **Bedrock provisioned throughput** — Reserve Bedrock inference capacity to eliminate tail latency variance under burst load.
2. **Lambda reserved concurrency** — Set reserved concurrency on `stack-processor` equal to peak expected stacks per tenant to prevent throttling as tenant count grows.
3. **Async validator invocation** — The synchronous validator call inside `stack_processor` extends held Lambda time. Converting to async with DynamoDB polling would reduce cost and P99.
4. **DLQ monitoring** — Add a CloudWatch alarm on `drift-detector-processor-dlq` depth to surface any future fault injection or IAM misconfiguration quickly.

---

## 8. Conclusion

The IaC drift detection pipeline performed reliably across all five test scenarios with **zero Lambda errors, zero throttles, and zero credential leaks**.

Throughput scales effectively with load — from 16 stacks/min (1 stack) to ~40 stacks/min (10 stacks) — demonstrating good horizontal scaling from Lambda's automatic concurrency. The pipeline correctly isolated a bad-tenant failure (T4) and passed a full credential-leakage audit (T5).

The one bottleneck is **Bedrock tail latency**: P99 grows from 1.7 s at 1 stack to 14.9 s at 10 stacks. The P50 median (4–5 s) is healthy and stable, so this is a tail issue rather than a general throughput problem. For production at higher tenant counts, provisioned Bedrock throughput is the single most impactful optimization.

Overall the system is **production-ready for the current scale** (tens of stacks per tenant), with a clear path to supporting hundreds of stacks through the above optimizations.

---

_Graphs: `G1_bedrock_latency.png` · `G2_latency_timeline.png` · `G3_throughput.png` · `G4_concurrency.png` · `G5_error_rate.png` · `G6_scaling_response.png` · `G7_resource_utilization.png`_  
_Data source: AWS CloudWatch Metrics + Logs Insights exports, 2026-05-04._
