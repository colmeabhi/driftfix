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
FLOOD_SIZE (default 20) concurrent synchronous invocations are fired.  The
first ~10 claim all available execution slots; the rest are rejected
immediately with TooManyRequestsException — Lambda never executes them.

  Account concurrency limit = 10
  20 concurrent invocations
  → ~10 succeed, ~10 throttled (TooManyRequestsException)

In the real async pipeline (not exercised by this script):
  Stack Processor invokes Validator (InvocationType="Event")
  → Validator throttled → Stack Processor raises TooManyRequestsException
  → Stack Processor's on_failure destination routes event to SQS DLQ

Recovery mechanism
------------------
1. Throttling self-resolves as executing invocations complete (transient).
2. For sustained load, request a Lambda concurrency quota increase via
   AWS Service Quotas console or AWS Support.
3. In the async pipeline, re-invoke stranded DLQ events once load subsides.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Throttles widget: spike on drift-detector-validator
- Lambda Invocations widget: count < flood size (throttled requests are never
  executed, so they don't appear as invocations)
- DLQ Depth: empty for this synchronous test; in the async pipeline the Stack
  Processor's on_failure destination would populate the DLQ
- Alarm: drift-detector-validator-throttled fires (threshold = 0)

Usage
-----
  export AWS_PROFILE=<your-profile>
  python tests/failure_scenarios/scenario_2_throttle.py

  # Adjust flood size:
  python tests/failure_scenarios/scenario_2_throttle.py --flood 25
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
FLOOD_SIZE   = 20   # concurrent invocations; must exceed account concurrency limit

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


def _invoke_worker(args):
    """Called from a thread. Returns (worker_id, outcome, detail)."""
    lam, payload, worker_id = args
    try:
        resp = lam.invoke(
            FunctionName=VALIDATOR_FN,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        body = json.loads(resp["Payload"].read())
        if resp.get("FunctionError"):
            return worker_id, "lambda_error", body.get("errorMessage", "")
        return worker_id, "success", body.get("status", "")
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "TooManyRequestsException":
            return worker_id, "throttled", code
        return worker_id, f"client_error:{code}", str(e)
    except Exception as e:
        return worker_id, "exception", str(e)


def _check_cloudwatch(cw, metric, window_minutes=5):
    end   = datetime.datetime.utcnow()
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
            AttributeNames=["ApproximateNumberOfMessagesVisible"],
        )
        depth = int(attrs["Attributes"].get("ApproximateNumberOfMessagesVisible", 0))
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
        print(f"         Throttles may not occur. Consider --flood {limit * 2}.")
    else:
        print(f"\n  Flood size ({flood_size}) > account limit ({limit}).")
        print(f"  Expected outcome: ~{limit} succeed, ~{flood_size - limit} throttled.")
    return limit


def step_flood(lam, flood_size, account_limit):
    _section(f"TRIGGER — firing {flood_size} concurrent invocations (account limit={account_limit})")

    payloads = []
    for i in range(flood_size):
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

    results = {"success": 0, "throttled": 0, "lambda_error": 0, "other": 0}
    workers = [(boto3.client("lambda", region_name=REGION), p, i)
               for i, p in enumerate(payloads)]

    print(f"  Launching {flood_size} threads simultaneously…")
    with ThreadPoolExecutor(max_workers=flood_size) as pool:
        futures = {pool.submit(_invoke_worker, w): w[2] for w in workers}
        for future in as_completed(futures):
            worker_id, outcome, detail = future.result()
            if outcome == "success":
                results["success"] += 1
                print(f"    worker {worker_id:02d}: ✓ success")
            elif outcome == "throttled":
                results["throttled"] += 1
                print(f"    worker {worker_id:02d}: ✗ THROTTLED (TooManyRequestsException)")
            elif outcome == "lambda_error":
                results["lambda_error"] += 1
                print(f"    worker {worker_id:02d}: ✗ lambda error — {detail}")
            else:
                results["other"] += 1
                print(f"    worker {worker_id:02d}: ? {outcome} — {detail}")

    _section("RESULTS")
    print(f"  Succeeded : {results['success']}")
    print(f"  Throttled : {results['throttled']}  ← expected ~{max(0, flood_size - account_limit)}")
    print(f"  Errors    : {results['lambda_error']}")
    print(f"  Other     : {results['other']}")

    if results["throttled"] > 0:
        print(f"\n  ✓ Throttling confirmed.")
        print(f"    In the async pipeline, the Stack Processor's on_failure destination")
        print(f"    would route each throttled event to the SQS DLQ.")
    else:
        print(f"\n  ℹ  No throttles recorded.")
        print(f"     Lambda may have scaled fast enough to handle all requests, or the")
        print(f"     account limit was not reached. Try --flood {flood_size * 2}.")


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
    step_flood(lam, args.flood, account_limit)
    if not args.no_evidence:
        step_evidence(cw, sqs, args.flood)

    print()


if __name__ == "__main__":
    main()
