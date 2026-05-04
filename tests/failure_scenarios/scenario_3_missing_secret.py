#!/usr/bin/env python3
"""
Failure Scenario 3 — Missing Secrets Manager Secret (Dependency Failure)
=========================================================================
Failure type : External dependency failure (Secrets Manager)
Component    : PR Creator Lambda
Rubric item  : "Intentionally trigger failure scenario; show system behavior,
               recovery mechanism, and monitoring evidence."

What happens
------------
The PR Creator Lambda is invoked with a `github_token_secret_arn` that points
to a non-existent Secrets Manager secret.  When it calls GetSecretValue, AWS
returns ResourceNotFoundException.  This unhandled ClientError causes the
Lambda to crash (FunctionError=Unhandled).

  PR Creator → secretsmanager.get_secret_value(nonexistent ARN)
             → ResourceNotFoundException
             → Lambda crash (FunctionError)
             ↓  (in the real async pipeline)
          No DLQ destination on PR Creator → event is dropped after 2 Lambda retries
          ↓
          DynamoDB reconciliation row stuck at status="queued" (no pr_url)

Recovery mechanism
------------------
1. Identify which tenants have reconciliation records stuck in "queued" status
   (no pr_url after >1 hour):
     aws dynamodb scan --table-name drift-detector-reconciliations \
       --filter-expression "#s = :q" \
       --expression-attribute-names '{"#s": "status"}' \
       --expression-attribute-values '{":q": {"S": "queued"}}'
2. Admin onboards the tenant correctly via the Admin UI (POST /register),
   which creates the Secrets Manager secret with a valid GitHub PAT.
3. Re-invoke PR Creator with the correct secret ARN and the stuck event's S3 keys.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Errors widget: spike on drift-detector-pr-creator
- DLQ Depth: not affected (PR Creator has no DLQ destination — gap in config)
- Alarm: drift-detector-stack-processor-errors if the stack-processor triggered it
  (this scenario invokes PR Creator directly, so only pr-creator Errors spike)

Usage
-----
  export AWS_PROFILE=<your-profile>
  python tests/failure_scenarios/scenario_3_missing_secret.py

  # Show recovery path (requires a real github_token_secret_arn):
  python tests/failure_scenarios/scenario_3_missing_secret.py --recover \
    --secret-arn arn:aws:secretsmanager:us-east-1:<account>:secret:drift-detector/github-token-<tenant>
"""

import argparse
import json
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION        = "us-east-1"
PR_CREATOR_FN = "drift-detector-pr-creator"

CLOUDTRAIL_EVENT = {
    "eventName":  "PutBucketAcl",
    "eventTime":  "2026-05-01T07:00:00Z",
    "eventSource": "s3.amazonaws.com",
    "awsRegion":  REGION,
    "userIdentity": {"arn": "arn:aws:iam::999999999999:user/failure-test"},
    "requestParameters": {"bucketName": "test-bucket"},
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)

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

def _build_payload(event_id, secret_arn, updated_s3_keys=None):
    ct_event = dict(CLOUDTRAIL_EVENT, eventID=event_id)
    return {
        "tenant_id":               "failure-test-tenant",
        "event_id":                event_id,
        "stack_name":              "failure-test-stack",
        "primary_path":            "template.yaml",
        "github_repo":             "owner/repo",
        "github_token_secret_arn": secret_arn,
        "cloudtrail_event":        ct_event,
        # Empty dict → skips S3 fetches entirely, goes straight to Secrets Manager.
        # In a real failure scenario the keys would point to real S3 objects.
        "updated_s3_keys":         updated_s3_keys or {},
    }

def _invoke(lam, payload, label):
    print(f"\n  Invoking {PR_CREATOR_FN} ({label})…")
    resp = lam.invoke(
        FunctionName=PR_CREATOR_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    body           = json.loads(resp["Payload"].read())
    function_error = resp.get("FunctionError")
    return body, function_error

# ── Trigger ───────────────────────────────────────────────────────────────────

def run_trigger(lam):
    _section("TRIGGER — invoking PR Creator with a non-existent Secrets Manager secret")

    # Get real account ID to form a valid-format ARN (avoids ARN validation errors).
    account_id  = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    fake_secret = (f"arn:aws:secretsmanager:{REGION}:{account_id}:"
                   f"secret:drift-detector/DOES-NOT-EXIST-failure-test")

    event_id = str(uuid.uuid4())
    payload  = _build_payload(event_id, fake_secret)

    print(f"  event_id       : {event_id}")
    print(f"  secret_arn     : {fake_secret}")
    print(f"  updated_s3_keys: {{}}  (empty — skips S3, goes straight to Secrets Manager)")

    body, function_error = _invoke(lam, payload, "nonexistent secret ARN")

    if function_error:
        err_type = body.get("errorType", "Unknown")
        err_msg  = body.get("errorMessage", str(body))
        print(f"\n  [EXPECTED] FunctionError = {function_error!r}")
        print(f"  Error type   : {err_type}")
        print(f"  Error message: {err_msg}")

        if "ResourceNotFoundException" in err_type or "ResourceNotFoundException" in err_msg:
            print(f"\n  ✓ ResourceNotFoundException confirmed — Secrets Manager dependency failure.")
        elif "AccessDeniedException" in err_type:
            print(f"\n  ℹ  Got AccessDeniedException instead of ResourceNotFoundException.")
            print(f"     This means the Lambda cannot access Secrets Manager at all.")
            print(f"     Check the Lambda's IAM policy in terraform/saas/iam.tf.")
        else:
            print(f"\n  ✓ Lambda crashed as expected (different error type than anticipated).")

        print(f"\n  In the async pipeline (Validator → PR Creator):")
        print(f"  • PR Creator retries 2x (Lambda async retry behaviour), then drops the event.")
        print(f"  • DynamoDB reconciliation record for this event stays stuck at status='queued'.")
        print(f"  • No DLQ destination is configured on PR Creator — this is a gap that should")
        print(f"    be fixed by adding an on_failure destination in lambdas.tf.")
    else:
        print(f"\n  [UNEXPECTED] Lambda returned success: {body}")
        print(f"  The fake secret ARN may have accidentally matched a real secret.")
        sys.exit(1)

    return event_id, fake_secret

# ── Evidence ──────────────────────────────────────────────────────────────────

def show_evidence(cw):
    _section("EVIDENCE — CloudWatch metrics")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    errors = _check_cloudwatch(cw, PR_CREATOR_FN, "Errors")
    print(f"\n  Lambda/Errors (drift-detector-pr-creator, last 5 min): {errors}")
    if errors > 0:
        print(f"  ✓ Error spike visible — check 'Lambda Errors' widget on dashboard.")
    else:
        print(f"  ℹ  No data yet — check dashboard in ~2 min.")

    print(f"\n  Dashboard URL (us-east-1):")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")

# ── Recovery ──────────────────────────────────────────────────────────────────

def run_recovery(lam, real_secret_arn):
    _section("RECOVERY — re-invoking PR Creator with a valid Secrets Manager secret")

    print(f"  secret_arn : {real_secret_arn}")
    print()
    print(f"  NOTE: This re-invocation will attempt to create a real GitHub PR.")
    print(f"  The payload uses an empty updated_s3_keys dict, so no files will be")
    print(f"  committed — the PR will be empty.  Use the e2e test for a full flow.")

    event_id = str(uuid.uuid4())
    payload  = _build_payload(event_id, real_secret_arn)

    body, function_error = _invoke(lam, payload, "real secret ARN")

    if function_error:
        err_msg = body.get("errorMessage", str(body))
        print(f"\n  [FAIL] Lambda still errored: {err_msg}")
        print(f"  Check that the secret ARN is correct and the Lambda IAM role can read it.")
        sys.exit(1)
    else:
        print(f"\n  Lambda returned: {body}")
        pr_url = body.get("pr_url")
        if pr_url:
            print(f"  ✓ Recovery successful — PR opened: {pr_url}")
        else:
            print(f"  ℹ  Lambda returned success but no pr_url (empty files dict — expected).")
            print(f"     The Secrets Manager dependency is now working correctly.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recover", action="store_true",
                        help="Run the recovery step (requires --secret-arn).")
    parser.add_argument("--secret-arn", default="",
                        help="Real Secrets Manager ARN to use in recovery step.")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Skip the 90-second CloudWatch polling wait.")
    args = parser.parse_args()

    lam = boto3.client("lambda",     region_name=REGION)
    cw  = boto3.client("cloudwatch", region_name=REGION)

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Failure Scenario 3 — Missing Secrets Manager Secret     ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if args.recover:
        if not args.secret_arn:
            print("  --secret-arn is required for the recovery step.")
            sys.exit(1)
        run_recovery(lam, args.secret_arn)
    else:
        run_trigger(lam)
        if not args.no_evidence:
            show_evidence(cw)
        print("\n  Run with --recover --secret-arn <ARN> to demonstrate the recovery path.")

    print()


if __name__ == "__main__":
    main()
