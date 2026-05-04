#!/usr/bin/env python3
"""
Failure Scenario 2 — Concurrency Throttle
==========================================
Failure type : Resource limit / infrastructure throttling
Component    : Validator Lambda (reserved concurrency artificially lowered)
Rubric item  : "Intentionally trigger failure scenario; show system behavior,
               recovery mechanism, and monitoring evidence."

What happens
------------
Reserved concurrency on the Validator is set to CONCURRENCY_CAP (default 2).
Then FLOOD_SIZE (default 20) concurrent synchronous invocations are fired.
The invocations beyond the cap are rejected immediately with
TooManyRequestsException — Lambda never executes them.

  Reserved concurrency = 2
  20 concurrent invocations
  → 2 succeed, 18 throttled (TooManyRequestsException)

In the real async pipeline:
  Stack Processor invokes Validator (InvocationType="Event")
  → Validator throttled → Stack Processor raises TooManyRequestsException
  → Stack Processor fails → goes to DLQ → alarm fires

Recovery mechanism
------------------
1. Remove the reserved concurrency limit (restores Lambda to account-level
   concurrency pool).
2. For sustained high load, request a Lambda concurrency limit increase via
   AWS Support or the Service Quotas console.
3. Re-invoke any throttled events from the DLQ.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Throttles widget: spike on drift-detector-validator
- Lambda Errors widget: spike on drift-detector-stack-processor
  (if throttle propagates up the call chain)
- DLQ Depth widget: rises when Stack Processor fails due to throttle
- Alarm: drift-detector-processor-dlq-not-empty fires

⚠  SAFETY NOTE
   This script modifies the Validator Lambda's reserved concurrency.
   It ALWAYS restores the original setting at the end, even on error.
   If interrupted, run with --restore to clean up manually.

Usage
-----
  export AWS_PROFILE=<your-profile>
  python tests/failure_scenarios/scenario_2_throttle.py

  # If interrupted before restore:
  python tests/failure_scenarios/scenario_2_throttle.py --restore

  # Adjust parameters:
  python tests/failure_scenarios/scenario_2_throttle.py \
    --cap 3 --flood 30 --workers 30
"""

import argparse
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

REGION          = "us-east-1"
VALIDATOR_FN    = "drift-detector-validator"
CONCURRENCY_CAP = 2    # reserved concurrency cap during the test
FLOOD_SIZE      = 20   # number of concurrent invocations to fire

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
    print('─' * 60)

def _get_current_concurrency(lam):
    try:
        resp = lam.get_function_concurrency(FunctionName=VALIDATOR_FN)
        return resp.get("ReservedConcurrentExecutions")  # None = unreserved
    except ClientError:
        return None

def _set_concurrency(lam, value):
    if value is None:
        lam.delete_function_concurrency(FunctionName=VALIDATOR_FN)
        print(f"  Reserved concurrency removed (unreserved).")
    else:
        lam.put_function_concurrency(
            FunctionName=VALIDATOR_FN,
            ReservedConcurrentExecutions=value,
        )
        print(f"  Reserved concurrency set to {value}.")

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

def _check_cloudwatch(cw, function_name, metric, window_minutes=5):
    import datetime
    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(minutes=window_minutes)
    resp  = cw.get_metric_statistics(
        Namespace  = "AWS/Lambda",
        MetricName = metric,
        Dimensions = [{"Name": "FunctionName", "Value": function_name}],
        StartTime  = start,
        EndTime    = end,
        Period     = window_minutes * 60,
        Statistics = ["Sum"],
    )
    return int(sum(p["Sum"] for p in resp.get("Datapoints", [])))

# ── Steps ─────────────────────────────────────────────────────────────────────

def step_set_cap(lam, cap):
    _section(f"SETUP — lowering Validator reserved concurrency to {cap}")
    original = _get_current_concurrency(lam)
    print(f"  Current reserved concurrency: {original!r} (will restore after test)")
    _set_concurrency(lam, cap)
    # Small pause so the setting propagates before flooding
    print("  Waiting 5 s for concurrency change to propagate…")
    time.sleep(5)
    return original

def step_flood(lam, flood_size):
    _section(f"TRIGGER — firing {flood_size} concurrent invocations (cap={CONCURRENCY_CAP})")

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

    # Create one Lambda client per thread to avoid connection-pool contention.
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
    print(f"  Throttled : {results['throttled']}  ← should be ~{flood_size - CONCURRENCY_CAP}")
    print(f"  Errors    : {results['lambda_error']}")
    print(f"  Other     : {results['other']}")

    if results["throttled"] > 0:
        print(f"\n  ✓ Throttling confirmed.")
    else:
        print(f"\n  ℹ  No throttles recorded — the cap may not have propagated in time.")
        print(f"     Re-run the script; or check the Throttles widget on the dashboard.")

def step_evidence(cw):
    _section("EVIDENCE — CloudWatch metrics")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    throttles = _check_cloudwatch(cw, VALIDATOR_FN, "Throttles")
    invocations = _check_cloudwatch(cw, VALIDATOR_FN, "Invocations")
    print(f"\n  Lambda/Throttles  (drift-detector-validator, last 5 min): {throttles}")
    print(f"  Lambda/Invocations (drift-detector-validator, last 5 min): {invocations}")

    if throttles > 0:
        print(f"\n  ✓ Throttle spike visible — check 'Lambda Throttles' widget on dashboard.")
    else:
        print(f"  ℹ  Metric not yet available — check dashboard in ~2 min.")

    print(f"\n  Dashboard URL (us-east-1):")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")

def step_restore(lam, original):
    _section("RECOVERY — restoring Validator concurrency")
    _set_concurrency(lam, original)
    print(f"  ✓ Validator Lambda is back to normal operation.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cap",     type=int, default=CONCURRENCY_CAP,
                        help=f"Reserved concurrency cap during test (default: {CONCURRENCY_CAP})")
    parser.add_argument("--flood",   type=int, default=FLOOD_SIZE,
                        help=f"Number of concurrent invocations (default: {FLOOD_SIZE})")
    parser.add_argument("--restore", action="store_true",
                        help="Remove reserved concurrency setting and exit (cleanup after interrupt).")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Skip the 90-second CloudWatch polling wait.")
    args = parser.parse_args()

    lam = boto3.client("lambda",     region_name=REGION)
    cw  = boto3.client("cloudwatch", region_name=REGION)

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Failure Scenario 2 — Lambda Concurrency Throttle        ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if args.restore:
        _section("RESTORE ONLY")
        _set_concurrency(lam, None)
        print("  ✓ Done.")
        return

    original = step_set_cap(lam, args.cap)
    try:
        step_flood(lam, args.flood)
        if not args.no_evidence:
            step_evidence(cw)
    finally:
        # Always restore, even if the flood step raises an exception.
        step_restore(lam, original)

    print()


if __name__ == "__main__":
    main()
