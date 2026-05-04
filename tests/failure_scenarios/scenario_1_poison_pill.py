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
                    Stack Processor (caller) fails → SQS DLQ ← alarm fires

Recovery mechanism
------------------
1. Note the event_id printed by the trigger step.
2. Inspect the DLQ (in the real pipeline, the stranded message will be there).
3. Fix the template and re-invoke the Validator with the same event_id and
   retry_count=0 (--recover --event-id <id>).
4. Confirm the Validator returns status=passed for the same event.
5. Confirm Lambda/Errors metric returns to 0.
6. Delete the DLQ message once the event is successfully re-processed.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Errors widget: spike on drift-detector-validator
- DLQ Depth widget: rises in the real async pipeline when Stack Processor
  fails after receiving the Validator's FunctionError
- Alarm: drift-detector-validator-errors fires (threshold = 0)
- Post-recovery: Lambda Errors returns to 0

Usage
-----
  export AWS_PROFILE=<your-profile>

  # Step 1 — trigger the failure:
  python tests/failure_scenarios/scenario_1_poison_pill.py
  # Note the event_id printed at the end.

  # Step 2 — recover the same event:
  python tests/failure_scenarios/scenario_1_poison_pill.py \\
    --recover --event-id <id-from-step-1>
"""

import argparse
import datetime
import json
import sys
import time
import uuid

import boto3
from botocore.exceptions import ClientError

REGION       = "us-east-1"
VALIDATOR_FN = "drift-detector-validator"
DLQ_NAME     = "drift-detector-processor-dlq"

# ── Payloads ──────────────────────────────────────────────────────────────────

# Missing the required `Type` property on TestBucket.
# cfn-lint flags: E3001 Invalid or unsupported type 'null' for resource TestBucket
BROKEN_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: Broken template — multiple cfn-lint violations
Resources:

  # Violation 1 (E3001): Resource missing required 'Type' property
  NoTypeBucket:
    Properties:
      BucketName: this-bucket-has-no-type

  # Violation 2 (E3001): Completely invalid resource type
  InvalidResource:
    Type: AWS::DOESNOTEXIST::Resource
    Properties:
      SomeProperty: value

  # Violation 3 (E3012 / W2001): Ref to a resource that does not exist
  OtherBucket:
    Type: AWS::S3::Bucket
    Properties:
      BucketName: !Ref NonExistentParameter

  # Violation 4 (E3002): Invalid property for the resource type
  BucketWithBadProp:
    Type: AWS::S3::Bucket
    Properties:
      ThisPropertyDoesNotExist: true
      AlsoFake: 99999
"""

VALID_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: Fixed template — resource Type restored
# Metadata:
#  cfn-lint:
#    config:
#      ignore_checks:
#        - W
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

TENANT_ID  = "failure-test-tenant"
STACK_NAME = "failure-test-stack"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print("─" * 60)


def _invoke(lam, payload, label):
    print(f"\n  Invoking {VALIDATOR_FN} ({label})…")
    resp = lam.invoke(
        FunctionName=VALIDATOR_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    body = json.loads(resp["Payload"].read())
    return body, resp.get("FunctionError")


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


def _check_dlq(sqs):
    """Return the approximate visible message count on the processor DLQ."""
    try:
        url = sqs.get_queue_url(QueueName=DLQ_NAME)["QueueUrl"]
        attrs = sqs.get_queue_attributes(
            QueueUrl=url,
            AttributeNames=["ApproximateNumberOfMessagesVisible"],
        )
        return int(attrs["Attributes"].get("ApproximateNumberOfMessagesVisible", 0)), url
    except ClientError as e:
        print(f"  [WARN] Could not query DLQ: {e}")
        return None, None


def _build_payload(event_id, template, retry_count):
    ct_event = dict(CLOUDTRAIL_EVENT, eventID=event_id)
    return {
        "tenant_id":               TENANT_ID,
        "event_id":                event_id,
        "updated_files":           {"template.yaml": template},
        "stack_name":              STACK_NAME,
        "primary_path":            "template.yaml",
        "cloudtrail_event":        ct_event,
        "github_repo":             "owner/repo",
        "github_token_secret_arn": "",
        "retry_count":             retry_count,
    }

# ── Trigger ───────────────────────────────────────────────────────────────────

def run_trigger(lam):
    _section("TRIGGER — sending poison pill to Validator Lambda")

    event_id = str(uuid.uuid4())
    payload  = _build_payload(event_id, BROKEN_TEMPLATE, retry_count=3)

    print(f"  event_id   : {event_id}")
    print(f"  retry_count: 3 (== MAX_RETRIES → RuntimeError path)")
    print(f"  template   : BROKEN (missing resource Type field)")

    body, function_error = _invoke(lam, payload, "broken template")

    if function_error:
        print(f"\n  [EXPECTED] FunctionError = {function_error!r}")
        print(f"  Error message: {body.get('errorMessage', str(body))}")
        print(f"\n  ✓ Failure triggered successfully.")
        print(f"    In the async pipeline, the Stack Processor (caller) receives this")
        print(f"    FunctionError, fails itself, and its on_failure destination routes")
        print(f"    the event to the SQS DLQ (drift-detector-processor-dlq).")
    else:
        print(f"\n  [UNEXPECTED] Lambda returned success: {body}")
        print(f"  Check cfn-lint is installed in the Lambda layer.")
        sys.exit(1)

    print(f"\n  ── Next step ──────────────────────────────────────────")
    print(f"  Run the recovery step with the same event_id to close the loop:")
    print(f"  python tests/failure_scenarios/scenario_1_poison_pill.py \\")
    print(f"    --recover --event-id {event_id}")
    print(f"  ────────────────────────────────────────────────────────")

    return event_id

# ── Evidence ──────────────────────────────────────────────────────────────────

def show_trigger_evidence(cw, sqs):
    _section("EVIDENCE — CloudWatch metrics (post-trigger)")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    errors = _check_cloudwatch(cw, "Errors")
    print(f"\n  Lambda/Errors (drift-detector-validator, last 5 min): {errors}")
    if errors > 0:
        print(f"  ✓ Error spike confirmed — alarm drift-detector-validator-errors should be ALARM.")
    else:
        print(f"  ℹ  No data yet — CloudWatch can lag up to 3 min. Check dashboard manually.")

    depth, url = _check_dlq(sqs)
    if depth is not None:
        print(f"\n  DLQ ({DLQ_NAME}): {depth} message(s) visible")
        if depth > 0:
            print(f"  ✓ DLQ has messages — inspect with:")
            print(f"    aws sqs receive-message --queue-url {url} --max-number-of-messages 1")
        else:
            print(f"  ℹ  DLQ is empty — this trigger used RequestResponse (synchronous).")
            print(f"     In the async pipeline the Stack Processor's on_failure destination")
            print(f"     would route the stranded event here.")

    print(f"\n  Dashboard URL (us-east-1):")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")


def show_recovery_evidence(cw, sqs):
    _section("EVIDENCE — CloudWatch metrics (post-recovery)")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    errors = _check_cloudwatch(cw, "Errors")
    invocations = _check_cloudwatch(cw, "Invocations")
    print(f"\n  Lambda/Invocations (last 5 min): {invocations}")
    print(f"  Lambda/Errors      (last 5 min): {errors}")
    if errors == 0 and invocations > 0:
        print(f"  ✓ Errors returned to 0 after recovery — alarm should transition to OK.")
    elif errors > 0:
        print(f"  ℹ  {errors} error(s) still present — CloudWatch window includes pre-recovery errors.")
        print(f"     Wait 5 min and re-check; the alarm will resolve once the window clears.")

    depth, url = _check_dlq(sqs)
    if depth is not None:
        print(f"\n  DLQ ({DLQ_NAME}): {depth} message(s) visible")
        if depth == 0:
            print(f"  ✓ DLQ is empty — no stranded events remaining.")
        else:
            print(f"  ℹ  DLQ has {depth} message(s) (may be from other tests).")
            print(f"     To drain: aws sqs purge-queue --queue-url {url}")

    print(f"\n  Dashboard URL (us-east-1):")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")

# ── Recovery ──────────────────────────────────────────────────────────────────

def run_recovery(lam, event_id):
    _section("RECOVERY — re-invoking Validator with the fixed template")

    if event_id:
        print(f"  event_id : {event_id}  ← same event as the trigger run")
    else:
        event_id = str(uuid.uuid4())
        print(f"  event_id : {event_id}  (new — pass --event-id to reuse the trigger's event)")

    print(f"  template : VALID (resource Type restored)")
    print(f"  retry_count: 0")

    payload = _build_payload(event_id, VALID_TEMPLATE, retry_count=0)
    body, function_error = _invoke(lam, payload, "valid template")

    if function_error:
        print(f"\n  [FAIL] Recovery invocation still errored: {body}")
        sys.exit(1)

    status = body.get("status")
    print(f"\n  Lambda returned status = {status!r}")
    if status == "passed":
        print(f"  ✓ Validator accepted the fixed template for event {event_id}.")
        print(f"    In the async pipeline: re-queue this event_id into the pipeline")
        print(f"    after fixing the source template, then delete the DLQ message.")
    else:
        print(f"  ℹ  Unexpected status: {body}")

    return event_id

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--recover", action="store_true",
                        help="Run the recovery step (use with --event-id to close the loop).")
    parser.add_argument("--event-id", default="",
                        help="event_id printed by the trigger run; ties recovery to the same event.")
    parser.add_argument("--no-evidence", action="store_true",
                        help="Skip the 90-second CloudWatch polling wait.")
    args = parser.parse_args()

    lam = boto3.client("lambda",     region_name=REGION)
    cw  = boto3.client("cloudwatch", region_name=REGION)
    sqs = boto3.client("sqs",        region_name=REGION)

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Failure Scenario 1 — Poison Pill: Invalid CFN Template  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if args.recover:
        run_recovery(lam, args.event_id)
        if not args.no_evidence:
            show_recovery_evidence(cw, sqs)
    else:
        run_trigger(lam)
        if not args.no_evidence:
            show_trigger_evidence(cw, sqs)

    print()


if __name__ == "__main__":
    main()
