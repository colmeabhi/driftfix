#!/usr/bin/env python3
"""
Failure Scenario 1 — Poison Pill: Invalid CloudFormation Template
=================================================================
Failure type : Application-layer error (Lambda crash)
Component    : Validator Lambda
Rubric item  : "Intentionally trigger failure scenario; show system behavior,
               recovery mechanism, and monitoring evidence."

What happens
------------
A CloudFormation template with a structural error (resource missing `Type`)
is sent to the Validator Lambda.  cfn-lint detects the problem and returns
lint errors.  Because retry_count is already at MAX_RETRIES (3), the Lambda
raises RuntimeError instead of re-invoking the Stack Processor.

  cfn-lint errors → RuntimeError → Lambda FunctionError
                                 ↓  (in the real async pipeline)
                           SQS DLQ (processor_dlq) ← alarm fires

Recovery mechanism
------------------
1. Operator inspects the DLQ message to identify which event/tenant is broken.
2. Root cause: the LLM (Gemini/Bedrock) generated an invalid template.
3. Fix: correct the template manually, then re-invoke the Validator with a
   valid payload and retry_count=0.
4. DLQ message is deleted after successful re-processing.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Errors widget: spike on drift-detector-validator
- DLQ Depth widget: rises by 1 if this were called asynchronously
- Alarm: drift-detector-processor-dlq-not-empty fires (async pipeline path)

Usage
-----
  export AWS_PROFILE=<your-profile>
  python tests/failure_scenarios/scenario_1_poison_pill.py

  # Then recover by re-invoking with a valid template:
  python tests/failure_scenarios/scenario_1_poison_pill.py --recover
"""

import argparse
import json
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION       = "us-east-1"
VALIDATOR_FN = "drift-detector-validator"

# ── Payloads ──────────────────────────────────────────────────────────────────

# This template is missing the required `Type` property on TestBucket.
# cfn-lint will flag: E3001 Invalid or unsupported type 'null' for resource TestBucket
BROKEN_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: Broken template — missing Type on resource
Resources:
  TestBucket:
    Properties:
      BucketName: this-bucket-has-no-type
"""

VALID_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: Fixed template — resource Type restored
Resources:
  TestBucket:
    Type: AWS::S3::Bucket
    Properties:
      Tags:
        - Key: ManagedBy
          Value: CloudFormation
"""

CLOUDTRAIL_EVENT = {
    "eventID":    "",
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

def _invoke(lam, payload, label):
    print(f"\n  Invoking {VALIDATOR_FN} ({label})…")
    resp = lam.invoke(
        FunctionName=VALIDATOR_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    body = json.loads(resp["Payload"].read())
    function_error = resp.get("FunctionError")
    return body, function_error

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
    points = resp.get("Datapoints", [])
    total  = sum(p["Sum"] for p in points)
    return int(total)

# ── Trigger ───────────────────────────────────────────────────────────────────

def run_trigger(lam):
    _section("TRIGGER — sending poison pill to Validator Lambda")

    event_id = str(uuid.uuid4())
    ct_event = dict(CLOUDTRAIL_EVENT, eventID=event_id)
    payload  = {
        "tenant_id":               "failure-test-tenant",
        "event_id":                event_id,
        "updated_files":           {"template.yaml": BROKEN_TEMPLATE},
        "stack_name":              "failure-test-stack",
        "primary_path":            "template.yaml",
        "cloudtrail_event":        ct_event,
        "github_repo":             "owner/repo",
        "github_token_secret_arn": "",
        "retry_count":             3,   # at MAX_RETRIES → RuntimeError, not NotImplementedError
    }

    print(f"  event_id   : {event_id}")
    print(f"  retry_count: {payload['retry_count']} (== MAX_RETRIES → exhausted)")
    print(f"  template   : BROKEN (missing resource Type field)")

    body, function_error = _invoke(lam, payload, "broken template")

    if function_error:
        print(f"\n  [EXPECTED] FunctionError = {function_error!r}")
        err_msg = body.get("errorMessage", str(body))
        print(f"  Error message: {err_msg}")
        print(f"\n  ✓ Scenario triggered successfully.")
        print(f"    In the real async pipeline (Stack Processor → Validator),")
        print(f"    this failure would land in the SQS DLQ.")
    else:
        print(f"\n  [UNEXPECTED] Lambda returned success: {body}")
        print(f"  Check cfn-lint is installed in the Lambda layer.")
        sys.exit(1)

    return event_id

# ── Evidence ──────────────────────────────────────────────────────────────────

def show_evidence(cw):
    _section("EVIDENCE — CloudWatch metrics")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    errors = _check_cloudwatch(cw, VALIDATOR_FN, "Errors")
    print(f"\n  Lambda/Errors (drift-detector-validator, last 5 min): {errors}")
    if errors > 0:
        print(f"  ✓ Error spike visible — check 'Lambda Errors' widget on dashboard.")
    else:
        print(f"  ℹ  No data yet — CloudWatch can lag up to 3 min. Check dashboard manually.")

    print(f"\n  Dashboard URL (us-east-1):")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")

# ── Recovery ──────────────────────────────────────────────────────────────────

def run_recovery(lam):
    _section("RECOVERY — re-invoking Validator with a valid template")

    event_id = str(uuid.uuid4())
    ct_event = dict(CLOUDTRAIL_EVENT, eventID=event_id)
    payload  = {
        "tenant_id":               "failure-test-tenant",
        "event_id":                event_id,
        "updated_files":           {"template.yaml": VALID_TEMPLATE},
        "stack_name":              "failure-test-stack",
        "primary_path":            "template.yaml",
        "cloudtrail_event":        ct_event,
        "github_repo":             "owner/repo",
        "github_token_secret_arn": "",
        "retry_count":             0,
    }

    print(f"  event_id : {event_id}")
    print(f"  template : VALID")

    body, function_error = _invoke(lam, payload, "valid template")

    if function_error:
        print(f"\n  [FAIL] Recovery invocation still errored: {body}")
        sys.exit(1)
    else:
        status = body.get("status")
        print(f"\n  Lambda returned status = {status!r}")
        if status == "passed":
            print(f"  ✓ Recovery successful — Validator accepted the fixed template.")
        else:
            print(f"  ℹ  Unexpected status: {body}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recover", action="store_true",
                        help="Skip the trigger; run the recovery step only.")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Skip the 90-second CloudWatch polling wait.")
    args = parser.parse_args()

    lam = boto3.client("lambda",     region_name=REGION)
    cw  = boto3.client("cloudwatch", region_name=REGION)

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Failure Scenario 1 — Poison Pill: Invalid CFN Template  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if args.recover:
        run_recovery(lam)
    else:
        run_trigger(lam)
        if not args.no_evidence:
            show_evidence(cw)
        print("\n  Run with --recover to demonstrate the recovery path.")

    print()


if __name__ == "__main__":
    main()
