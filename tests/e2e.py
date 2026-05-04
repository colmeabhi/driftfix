#!/usr/bin/env python3
"""
End-to-end integration test for the drift-detector pipeline.

Starts at the Validator Lambda (bypasses Processor/StackProcessor which need
real cross-account roles) and exercises:
  Validator → S3 audit bucket → PR Creator → GitHub PR → DynamoDB status

Usage:
  python tests/e2e.py --github-repo owner/repo
  python tests/e2e.py --github-repo owner/repo --skip-pr  # stop before PR Creator
"""

import argparse
import json
import time
import uuid
import boto3
import sys

REGION = "us-east-1"
VALIDATOR_FN    = "drift-detector-validator"
PR_CREATOR_FN   = "drift-detector-pr-creator"
AUDIT_BUCKET    = "drift-detector-audit-274024437845"
RECON_TABLE     = "drift-detector-reconciliations"

VALID_CFN_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: Drift detector e2e test stack
Resources:
  TestBucket:
    Type: AWS::S3::Bucket
    Properties:
      Tags:
        - Key: ManagedBy
          Value: CloudFormation
"""

FAKE_CLOUDTRAIL_EVENT = {
    "eventID": "",          # filled in per run
    "eventName": "PutBucketAcl",
    "eventTime": "2026-04-28T07:00:00Z",
    "eventSource": "s3.amazonaws.com",
    "awsRegion": "us-east-1",
    "userIdentity": {
        "arn": "arn:aws:iam::999999999999:user/test-actor"
    },
    "requestParameters": {
        "bucketName": "my-test-bucket"
    },
}


def ok(msg):  print(f"  [PASS] {msg}")
def fail(msg): print(f"  [FAIL] {msg}"); sys.exit(1)
def info(msg): print(f"  [INFO] {msg}")


def invoke_validator(lam, tenant_id, event_id, github_repo):
    ct_event = dict(FAKE_CLOUDTRAIL_EVENT)
    ct_event["eventID"] = event_id

    payload = {
        "tenant_id":               tenant_id,
        "event_id":                event_id,
        "updated_files":           {"template.yaml": VALID_CFN_TEMPLATE},
        "original_files":          {"template.yaml": VALID_CFN_TEMPLATE},
        "primary_path":            "template.yaml",
        "stack_name":              "e2e-test-stack",
        "cloudtrail_event":        ct_event,
        "github_repo":             github_repo,
        "github_token_secret_arn": "",
        "retry_count":             0,
    }

    print("\n[1] Invoking Validator Lambda…")
    resp = lam.invoke(
        FunctionName=VALIDATOR_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    status_code = resp["StatusCode"]
    body = json.loads(resp["Payload"].read())

    if resp.get("FunctionError"):
        fail(f"Validator raised an error: {body}")

    if status_code == 200 and body.get("status") == "passed":
        ok(f"Validator returned status=passed")
    else:
        fail(f"Unexpected Validator response (HTTP {status_code}): {body}")

    return body


def check_s3(s3, tenant_id, event_id):
    print("\n[2] Checking S3 audit bucket for stored template…")
    key = f"validated/{tenant_id}/{event_id}/template.yaml"
    try:
        obj = s3.get_object(Bucket=AUDIT_BUCKET, Key=key)
        content = obj["Body"].read().decode()
        if "AWS::S3::Bucket" in content:
            ok(f"Template stored at s3://{AUDIT_BUCKET}/{key}")
        else:
            fail(f"Template stored but content looks wrong: {content[:200]}")
    except s3.exceptions.NoSuchKey:
        fail(f"Template not found at s3://{AUDIT_BUCKET}/{key}")


def wait_for_pr(ddb, tenant_id, event_id, timeout=60):
    print(f"\n[3] Polling DynamoDB for PR Creator result (up to {timeout}s)…")
    table = ddb.Table(RECON_TABLE)
    deadline = time.time() + timeout
    while time.time() < deadline:
        item = table.get_item(
            Key={"tenant_id": tenant_id, "event_id": event_id}
        ).get("Item", {})
        status = item.get("status")
        info(f"  recon status = {status!r}")
        if status == "pr_opened":
            ok(f"PR opened: {item.get('pr_url')}")
            return item.get("pr_url")
        if status in (None, "queued"):
            time.sleep(5)
            continue
        fail(f"Unexpected reconciliation status: {status!r}, item={item}")
    fail(f"Timed out waiting for pr_opened after {timeout}s")


def check_pr_creator_directly(lam, tenant_id, event_id, github_repo, s3_key):
    """Invoke PR Creator synchronously for faster feedback."""
    print("\n[3] Invoking PR Creator Lambda directly…")
    ct_event = dict(FAKE_CLOUDTRAIL_EVENT)
    ct_event["eventID"] = event_id

    payload = {
        "tenant_id":               tenant_id,
        "event_id":                event_id,
        "stack_name":              "e2e-test-stack",
        "primary_path":            "template.yaml",
        "github_repo":             github_repo,
        "github_token_secret_arn": "",
        "cloudtrail_event":        ct_event,
        "updated_s3_keys":         {"template.yaml": s3_key},
    }
    resp = lam.invoke(
        FunctionName=PR_CREATOR_FN,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    body = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        fail(f"PR Creator raised an error: {body}")
    pr_url = body.get("pr_url")
    if pr_url:
        ok(f"PR created: {pr_url}")
    else:
        fail(f"PR Creator did not return a pr_url: {body}")
    return pr_url


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--github-repo", required=True, help="owner/repo for the test PR")
    parser.add_argument("--skip-pr", action="store_true", help="stop after Validator (skip PR Creator)")
    parser.add_argument("--tenant-id", default="e2e-test-tenant")
    args = parser.parse_args()

    event_id = str(uuid.uuid4())

    print(f"\nDrift Detector — End-to-End Test")
    print(f"  tenant_id : {args.tenant_id}")
    print(f"  event_id  : {event_id}")
    print(f"  repo      : {args.github_repo}")

    lam = boto3.client("lambda", region_name=REGION)
    s3  = boto3.client("s3",     region_name=REGION)
    ddb = boto3.resource("dynamodb", region_name=REGION)

    invoke_validator(lam, args.tenant_id, event_id, args.github_repo)
    check_s3(s3, args.tenant_id, event_id)

    if args.skip_pr:
        print("\n--skip-pr set, stopping before PR Creator.")
    else:
        wait_for_pr(ddb, args.tenant_id, event_id)

    print("\nAll checks passed.\n")


if __name__ == "__main__":
    main()
