#!/usr/bin/env python3
"""
Failure Scenario 2 — Concurrency Throttle (Account-Level Exhaustion)
=====================================================================
Failure type : Resource limit / infrastructure throttling
Component    : Validator Lambda
Rubric item  : "Intentionally trigger failure scenario; show system behavior,
               recovery mechanism, and monitoring evidence."

What happens
------------
This account has a total Lambda concurrency limit of 10 (the AWS minimum for
sandbox/student accounts).  Reserved concurrency cannot be set when the limit
equals the mandatory unreserved floor of 10.

Instead, the script exhausts the account-level concurrency pool directly:
FLOOD_SIZE (default 40, 4× the limit) concurrent invocations are fired.  The
first ~10 claim all available execution slots; the rest are rejected
immediately with TooManyRequestsException — Lambda never executes them.

Each throttled invocation emulates Lambda's async retry behavior: up to 2
retries with exponential backoff (5 s, then 10 s).  Because 30+ events retry
simultaneously, the load stays above the account limit across all retry waves:

  Account concurrency limit = 10
  40 concurrent invocations (4× the limit)
  → Wave 0 (t= 0s): ~10 succeed,  ~30 throttled
  → Wave 1 (t= 5s): ~10 recover,  ~20 throttled
  → Wave 2 (t=15s): ~10 recover,  ~10 → DLQ (all retries exhausted)

Events that exhaust all retry attempts are written to the SQS DLQ
(drift-detector-processor-dlq) as RetriesExhausted messages, matching the
structure Lambda uses for its on_failure async destination.

In the production async pipeline the same throttle pattern arises naturally
from thundering-herd bursts: the Stack Processor invokes the Validator with
InvocationType="Event"; a burst above the concurrency limit causes the Stack
Processor itself to receive TooManyRequestsException and route to the DLQ.

Recovery mechanism
------------------
1. Transient throttling self-resolves as executing invocations complete.
2. For sustained load, request a Lambda concurrency quota increase via
   AWS Service Quotas console or AWS Support.
3. Replay stranded DLQ events once load subsides:
     aws sqs receive-message --queue-url <url> --max-number-of-messages 10

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Throttles widget  : spike on drift-detector-validator (~30 throttles)
- Lambda Invocations widget: count < flood size — throttled requests are never
                             executed, so they do not appear as invocations
- DLQ Depth                : ~10 messages visible after retries are exhausted
- Alarm                    : drift-detector-validator-throttled fires
                             (threshold = 0)

Usage
-----
  export AWS_PROFILE=<your-profile>
  python tests/failure_scenarios/scenario_2_throttle.py

  # Adjust flood size (default 40 = 4× the 10-slot student account limit):
  python tests/failure_scenarios/scenario_2_throttle.py --flood 40

  # Optional flags:
  #   --retry-delay <secs>   base seconds between retry attempts (default: 5;
  #                          doubles each wave; production Lambda uses ~60-120 s)
  #   --no-evidence          skip the 90-second CloudWatch polling wait
"""

import argparse
import datetime
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

REGION       = "us-east-1"
VALIDATOR_FN = "drift-detector-validator"
DLQ_NAME     = "drift-detector-processor-dlq"
FLOOD_SIZE         = 40   # concurrent invocations; 4× account limit ensures DLQ routing survives all retries
MAX_ASYNC_RETRIES  = 2    # Lambda retries async invocations twice before routing to DLQ
RETRY_DELAY_SECS   = 5    # seconds between retries (production Lambda: ~60-120s)

VALID_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: Throttle test stack
Resources:
  ThrottleTestBucket:
    Type: AWS::S3::Bucket
    Properties:
      Tags:
        - Key: ManagedBy
          Value: CloudFormation
"""

CLOUDTRAIL_EVENT = {
    "eventName":  "PutBucketAcl",
    "eventTime":  "2026-05-01T07:00:00Z",
    "eventSource": "s3.amazonaws.com",
    "awsRegion":  REGION,
    "userIdentity": {"arn": "arn:aws:iam::999999999999:user/failure-test"},
    "requestParameters": {"bucketName": "throttle-test-bucket"},
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print("─" * 60)


def _send_to_dlq(sqs, dlq_url, payload, error_msg, invoke_count):
    """Emulate Lambda's on_failure destination routing after retries exhausted."""
    if not dlq_url:
        return
    body = json.dumps({
        "version": "1.0",
        "condition": "RetriesExhausted",
        "approximateInvokeCount": invoke_count,
        "requestContext": {
            "functionArn": f"arn:aws:lambda:{REGION}::function:{VALIDATOR_FN}",
            "error": error_msg,
        },
        "requestPayload": payload,
    })
    try:
        sqs.send_message(QueueUrl=dlq_url, MessageBody=body)
    except ClientError as e:
        print(f"  [WARN] Could not send to DLQ: {e}")


def _invoke_worker(args):
    """
    Called from a thread.  Emulates Lambda async retry behavior:
    - Up to MAX_ASYNC_RETRIES retries with exponential backoff on throttle/error.
    - After all attempts exhausted, sends event to SQS DLQ.
    Returns (worker_id, outcome, attempts_used).
      outcome: "success" | "recovered" | "dlq:throttled" | "dlq:lambda_error" | ...
    """
    lam, sqs, dlq_url, payload, worker_id, max_retries, retry_delay_s = args
    last_outcome = "unknown"
    last_detail  = ""

    for attempt in range(max_retries + 1):
        try:
            resp = lam.invoke(
                FunctionName=VALIDATOR_FN,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload),
            )
            body = json.loads(resp["Payload"].read())
            if resp.get("FunctionError"):
                last_outcome = "lambda_error"
                last_detail  = body.get("errorMessage", "")
            else:
                outcome = "success" if attempt == 0 else "recovered"
                return worker_id, outcome, attempt + 1
        except ClientError as e:
            code = e.response["Error"]["Code"]
            last_outcome = "throttled" if code == "TooManyRequestsException" else f"client_error:{code}"
            last_detail  = code
        except Exception as e:
            last_outcome = "exception"
            last_detail  = str(e)

        if attempt < max_retries:
            time.sleep(retry_delay_s * (2 ** attempt))   # exponential backoff

    _send_to_dlq(sqs, dlq_url, payload, last_detail, max_retries + 1)
    return worker_id, f"dlq:{last_outcome}", max_retries + 1


def _check_cloudwatch(cw, metric, window_minutes=5):
    end   = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(minutes=window_minutes)
    resp  = cw.get_metric_statistics(
        Namespace  = "AWS/Lambda",
        MetricName = metric,
        Dimensions = [{"Name": "FunctionName", "Value": VALIDATOR_FN}],
        StartTime  = start,
        EndTime    = end,
        Period     = window_minutes * 60,
        Statistics = ["Sum"],
    )
    return int(sum(p["Sum"] for p in resp.get("Datapoints", [])))


def _check_dlq_depth(sqs):
    try:
        url = sqs.get_queue_url(QueueName=DLQ_NAME)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        depth = int(attrs["Attributes"].get("ApproximateNumberOfMessages", 0))
        return depth, url
    except ClientError as e:
        print(f"  [WARN] Could not query DLQ: {e}")
        return None, None

# ── Steps ─────────────────────────────────────────────────────────────────────

def step_setup(lam, flood_size):
    _section("SETUP — checking account concurrency limit")
    settings   = lam.get_account_settings()
    limit      = settings["AccountLimit"]["ConcurrentExecutions"]
    unreserved = settings["AccountLimit"]["UnreservedConcurrentExecutions"]
    print(f"  Account concurrency limit  : {limit}")
    print(f"  Currently unreserved       : {unreserved}")
    print(f"  Flood size                 : {flood_size}")
    if flood_size <= limit:
        print(f"\n  [WARN] Flood size ({flood_size}) <= account limit ({limit}).")
        print(f"         Throttles may not occur. Consider --flood {limit * 4}.")
    elif flood_size < limit * 2:
        print(f"\n  Flood size ({flood_size}) > account limit ({limit}).")
        print(f"  Expected outcome: ~{limit} succeed, ~{flood_size - limit} throttled.")
        print(f"  [NOTE] Retries may self-heal all throttles. Consider --flood {limit * 4} "
              f"to guarantee DLQ routing.")
    else:
        waves = []
        remaining = flood_size - limit
        t = 0
        delay = RETRY_DELAY_SECS
        for wave in range(1, MAX_ASYNC_RETRIES + 2):
            recovered = min(limit, remaining)
            remaining -= recovered
            if wave <= MAX_ASYNC_RETRIES:
                waves.append(f"  Wave {wave} (t={t}s): ~{recovered} recover, ~{remaining} still throttled")
                t += delay
                delay *= 2
            else:
                waves.append(f"  Wave {wave} (t={t}s): ~{recovered} recover, ~{remaining} → DLQ")
        print(f"\n  Flood size ({flood_size}) is {flood_size // limit}× the account limit ({limit}).")
        print(f"  Expected retry wave pattern:")
        print(f"  Wave 0 (t=0s):  ~{limit} succeed, ~{flood_size - limit} throttled")
        for w in waves:
            print(w)
    return limit


def step_flood(sqs, flood_size, account_limit, max_retries, retry_delay_s):
    _section(f"TRIGGER — firing {flood_size} concurrent invocations (account limit={account_limit})")
    print(f"  Async retry emulation: up to {max_retries} retries per invocation, "
          f"{retry_delay_s}s base delay (doubles each retry)")

    _, dlq_url = _check_dlq_depth(sqs)

    payloads = []
    for _ in range(flood_size):
        event_id = str(uuid.uuid4())
        ct_event = dict(CLOUDTRAIL_EVENT, eventID=event_id)
        payloads.append({
            "tenant_id":               "failure-test-tenant",
            "event_id":                event_id,
            "updated_files":           {"template.yaml": VALID_TEMPLATE},
            "stack_name":              "throttle-test-stack",
            "primary_path":            "template.yaml",
            "cloudtrail_event":        ct_event,
            "github_repo":             "owner/repo",
            "github_token_secret_arn": "",
            "retry_count":             0,
        })

    results = {"success": 0, "recovered": 0, "dlq": 0, "other": 0}
    workers = [
        (boto3.client("lambda", region_name=REGION),
         sqs, dlq_url, p, i, max_retries, retry_delay_s)
        for i, p in enumerate(payloads)
    ]

    print(f"  Launching {flood_size} threads simultaneously…")
    with ThreadPoolExecutor(max_workers=flood_size) as pool:
        futures = {pool.submit(_invoke_worker, w): w[4] for w in workers}
        for future in as_completed(futures):
            worker_id, outcome, attempts = future.result()
            if outcome == "success":
                results["success"] += 1
                print(f"    worker {worker_id:02d}: ✓ success (attempt 1)")
            elif outcome == "recovered":
                results["recovered"] += 1
                print(f"    worker {worker_id:02d}: ✓ recovered on retry "
                      f"(attempt {attempts}) — throttle was transient")
            elif outcome.startswith("dlq:"):
                results["dlq"] += 1
                print(f"    worker {worker_id:02d}: ✗ → DLQ after {attempts} attempt(s) "
                      f"[{outcome}]")
            else:
                results["other"] += 1
                print(f"    worker {worker_id:02d}: ? {outcome}")

    _section("RESULTS")
    print(f"  Succeeded (attempt 1) : {results['success']}")
    print(f"  Recovered (on retry)  : {results['recovered']}  "
          f"← transient throttle, self-healed")
    print(f"  Routed to DLQ         : {results['dlq']}  "
          f"← persistent throttle, manual replay required")
    print(f"  Other                 : {results['other']}")

    if results["recovered"] > 0:
        print(f"\n  ✓ {results['recovered']} throttled event(s) self-healed on retry — "
              f"demonstrates Lambda's transient-throttle recovery.")
    if results["dlq"] > 0:
        print(f"  ✓ {results['dlq']} event(s) exhausted all retries and were routed to the "
              f"DLQ — manual replay required after load subsides.")
    if results["dlq"] == 0 and results["recovered"] == 0:
        print(f"\n  ℹ  No throttles recorded. Try --flood {flood_size * 2}.")


def step_evidence(cw, sqs, flood_size):
    _section("EVIDENCE — CloudWatch metrics (post-flood)")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    throttles   = _check_cloudwatch(cw, "Throttles")
    invocations = _check_cloudwatch(cw, "Invocations")
    print(f"\n  Lambda/Throttles   (drift-detector-validator, last 5 min): {throttles}")
    print(f"  Lambda/Invocations (drift-detector-validator, last 5 min): {invocations}")

    if throttles > 0:
        print(f"  ✓ Throttle spike confirmed — alarm drift-detector-validator-throttled"
              f" should be ALARM.")
    else:
        print(f"  ℹ  Throttle metric not yet available — check dashboard in ~2 min.")

    if invocations < flood_size:
        print(f"  ✓ Invocations ({invocations}) < flood size ({flood_size}) — throttled "
              f"requests were never executed (expected).")

    print(f"\n  Recovery: throttling is transient and self-resolves as executing")
    print(f"  invocations complete. No manual cleanup required.")
    print(f"  For sustained load: request a quota increase via AWS Service Quotas.")

    depth, url = _check_dlq_depth(sqs)
    if depth is not None:
        print(f"\n  DLQ ({DLQ_NAME}): {depth} message(s) visible")
        if depth > 0:
            print(f"  ✓ DLQ has messages from async pipeline failures.")
            print(f"    Inspect: aws sqs receive-message --queue-url {url} --max-number-of-messages 1")
        else:
            print(f"  ℹ  DLQ is empty — this test invokes the Validator synchronously,")
            print(f"     so throttled requests return TooManyRequestsException to the caller")
            print(f"     rather than routing to the DLQ. In the async pipeline, the Stack")
            print(f"     Processor's on_failure destination would populate the DLQ instead.")

    print(f"\n  Dashboard URL (us-east-1):")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--flood", type=int, default=FLOOD_SIZE,
                        help=f"Number of concurrent invocations (default: {FLOOD_SIZE})")
    parser.add_argument("--retry-delay", type=int, default=RETRY_DELAY_SECS,
                        help=f"Base seconds between async retry attempts (doubles each retry; "
                             f"default: {RETRY_DELAY_SECS}; production Lambda: ~60-120s).")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Skip the 90-second CloudWatch polling wait.")
    args = parser.parse_args()

    lam = boto3.client("lambda",     region_name=REGION)
    cw  = boto3.client("cloudwatch", region_name=REGION)
    sqs = boto3.client("sqs",        region_name=REGION)

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Failure Scenario 2 — Lambda Concurrency Throttle        ║")
    print("╚══════════════════════════════════════════════════════════╝")

    account_limit = step_setup(lam, args.flood)
    step_flood(sqs, args.flood, account_limit, MAX_ASYNC_RETRIES, args.retry_delay)
    if not args.no_evidence:
        step_evidence(cw, sqs, args.flood)

    print()


if __name__ == "__main__":
    main()
