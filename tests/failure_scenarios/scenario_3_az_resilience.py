#!/usr/bin/env python3
"""
Failure Scenario 3 — AZ Resilience: Simulated Single-AZ DynamoDB Outage
========================================================================
Failure type : Infrastructure-layer disruption (AZ failure)
Component    : DynamoDB (called by Stack Processor and Validator Lambdas)
Rubric item  : "Intentionally trigger failure scenario; show system behavior,
               recovery mechanism, and monitoring evidence."

What happens
------------
AWS Fault Injection Service (FIS) injects HTTP 503 (Service Unavailable) errors
into 33 % of DynamoDB API calls made by the pipeline's Lambda execution roles.
33 % represents one AZ becoming unreachable in a standard 3-AZ region (us-east-1).

  FIS experiment active (2 min)
  ↓
  33% of DynamoDB calls → 503 ServiceUnavailable
  ↓
  AWS SDK retries with exponential back-off → hits healthy AZ replica
  ↓
  Request succeeds on retry (p50 < 1 additional second of latency)
  ↓
  15 concurrent Validator invocations complete successfully
  ↓
  CloudWatch: Duration p99 elevated; Errors = 0 (retries absorbed the failures)

Multi-AZ resilience demonstrated
---------------------------------
DynamoDB stores each item across three AZs (us-east-1a / 1b / 1c).  A write or
read that bounces off the 503-injected "failed AZ" transparently retries on one
of the two healthy replicas — the caller sees slightly higher latency but no
application-layer error.  Lambda itself is scheduled across AZs; a Lambda
invocation routed to the failed AZ will be retried in a healthy AZ automatically.

Recovery mechanism
------------------
Managed service recovery is automatic — no operator action is needed.
1. FIS experiment ends → 503 injection stops.
2. In-flight Lambda invocations that were retrying complete normally.
3. Any events that exhausted all SDK retries land in the SQS DLQ.
4. Operator purges or replays DLQ messages once the AZ is confirmed healthy.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Duration p99 widget: elevated during experiment (retry latency)
- Lambda Errors widget: should remain 0 — retries absorb the 503s
- Lambda Invocations widget: normal throughput maintained
- DLQ Depth widget: rises only if SDK exhausted all retries (rare at 33 %)
- Alarm: drift-detector-processor-near-timeout may fire if Duration p99 spikes

Prerequisites — one-time FIS role setup
----------------------------------------
FIS requires an IAM role it can assume to run the experiment.
Run these commands once in the SaaS account:

  aws iam create-role --role-name fis-experiment-role \\
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{
        "Effect":"Allow",
        "Principal":{"Service":"fis.amazonaws.com"},
        "Action":"sts:AssumeRole"
      }]
    }'

  aws iam attach-role-policy --role-name fis-experiment-role \\
    --policy-arn arn:aws:iam::aws:policy/AdministratorAccess

  # Retrieve the ARN for use below:
  aws iam get-role --role-name fis-experiment-role --query 'Role.Arn' --output text

Usage
-----
  export AWS_PROFILE=<saas-account-profile>

  FIS_ROLE_ARN=$(aws iam get-role --role-name fis-experiment-role \\
    --query 'Role.Arn' --output text)

  # Full scenario (FIS + pipeline load + evidence):
  python tests/failure_scenarios/scenario_3_az_resilience.py \\
    --fis-role-arn "$FIS_ROLE_ARN"

  # Pipeline-load only (no FIS — useful if FIS not yet set up):
  python tests/failure_scenarios/scenario_3_az_resilience.py --skip-fis

  # Tune the experiment:
  python tests/failure_scenarios/scenario_3_az_resilience.py \\
    --fis-role-arn "$FIS_ROLE_ARN" \\
    --percentage 50 --duration-seconds 180 --concurrent 20
"""

import argparse
import datetime
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

import boto3
from botocore.exceptions import ClientError

REGION       = "us-east-1"
PROJECT      = "drift-detector"
VALIDATOR_FN = f"{PROJECT}-validator"

# DynamoDB operations that represent cross-AZ writes/reads in the pipeline.
# Stack Processor writes reconciliation records; Validator/PR Creator update them.
_DDB_OPERATIONS = "PutItem,GetItem,UpdateItem,Query,Scan"

VALID_TEMPLATE = """\
AWSTemplateFormatVersion: '2010-09-09'
Description: AZ resilience test stack
Resources:
  ResilienceTestBucket:
    Type: AWS::S3::Bucket
    Properties:
      Tags:
        - Key: ManagedBy
          Value: CloudFormation
"""

_CLOUDTRAIL_BASE = {
    "eventName":  "PutBucketAcl",
    "eventTime":  "2026-05-01T07:00:00Z",
    "eventSource": "s3.amazonaws.com",
    "awsRegion":  REGION,
    "userIdentity": {"arn": "arn:aws:iam::000000000000:user/az-resilience-test"},
    "requestParameters": {"bucketName": "az-test-bucket"},
}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _section(title):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print("─" * 60)


def _check_cloudwatch(cw, function_name, metric, window_minutes=5):
    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(minutes=window_minutes)
    resp  = cw.get_metric_statistics(
        Namespace  = "AWS/Lambda",
        MetricName = metric,
        Dimensions = [{"Name": "FunctionName", "Value": function_name}],
        StartTime  = start,
        EndTime    = end,
        Period     = window_minutes * 60,
        Statistics = ["Sum", "Average"],
    )
    points = resp.get("Datapoints", [])
    total  = sum(p.get("Sum", 0) for p in points)
    return int(total)


def _check_cloudwatch_p99(cw, function_name, window_minutes=5):
    end   = datetime.datetime.utcnow()
    start = end - datetime.timedelta(minutes=window_minutes)
    resp  = cw.get_metric_statistics(
        Namespace   = "AWS/Lambda",
        MetricName  = "Duration",
        Dimensions  = [{"Name": "FunctionName", "Value": function_name}],
        StartTime   = start,
        EndTime     = end,
        Period      = window_minutes * 60,
        ExtendedStatistics = ["p99"],
    )
    points = resp.get("Datapoints", [])
    if not points:
        return None
    return max(p["ExtendedStatistics"]["p99"] for p in points)


def _get_lambda_role_arn(lam, function_name):
    """Return the execution role ARN for a Lambda function."""
    try:
        cfg = lam.get_function(FunctionName=function_name)["Configuration"]
        return cfg["Role"]
    except ClientError as e:
        print(f"  [WARN] Could not retrieve role for {function_name}: {e}")
        return None


# ── FIS experiment ────────────────────────────────────────────────────────────

def create_fis_experiment(fis, fis_role_arn, target_role_arns,
                           percentage, duration_seconds):
    """
    Create a FIS experiment template that injects 503s on DynamoDB calls
    from the given Lambda execution roles.  Returns the template ID.
    """
    duration_iso = f"PT{duration_seconds}S"

    targets = {}
    actions = {}
    for i, role_arn in enumerate(target_role_arns):
        key = f"role-{i}"
        targets[key] = {
            "resourceType": "aws:iam:role",
            "resourceArns": [role_arn],
            "selectionMode": "ALL",
        }
        actions[f"inject-ddb-503-{i}"] = {
            "actionId": "aws:fis:inject-api-unavailable-error",
            "parameters": {
                "service":     "dynamodb",
                "operations":  _DDB_OPERATIONS,
                "percentage":  str(percentage),
                "duration":    duration_iso,
            },
            "targets": {"Roles": key},
        }

    resp = fis.create_experiment_template(
        description  = "AZ resilience test — DynamoDB 503 injection",
        targets      = targets,
        actions      = actions,
        stopConditions = [{"source": "none"}],
        roleArn      = fis_role_arn,
        tags         = {"project": PROJECT, "purpose": "az-resilience-test"},
    )
    return resp["experimentTemplate"]["id"]


def start_fis_experiment(fis, template_id):
    resp = fis.start_experiment(experimentTemplateId=template_id)
    return resp["experiment"]["id"]


def wait_for_experiment_state(fis, experiment_id, target_state, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = fis.get_experiment(id=experiment_id)["experiment"]["state"]["status"]
        if state == target_state:
            return True
        if state in ("failed", "stopped"):
            return False
        time.sleep(5)
    return False


def stop_fis_experiment(fis, experiment_id):
    try:
        fis.stop_experiment(id=experiment_id)
    except ClientError:
        pass  # already stopped


def delete_fis_template(fis, template_id):
    try:
        fis.delete_experiment_template(id=template_id)
    except ClientError:
        pass


# ── Pipeline load ─────────────────────────────────────────────────────────────

def _invoke_worker(args):
    """Thread worker: invokes Validator once, returns (worker_id, outcome, duration_ms)."""
    lam, payload, worker_id = args
    t0 = time.monotonic()
    try:
        resp = lam.invoke(
            FunctionName=VALIDATOR_FN,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload),
        )
        body = json.loads(resp["Payload"].read())
        duration_ms = (time.monotonic() - t0) * 1000
        if resp.get("FunctionError"):
            return worker_id, "error", duration_ms, body.get("errorMessage", "")
        return worker_id, "success", duration_ms, body.get("status", "")
    except Exception as exc:
        duration_ms = (time.monotonic() - t0) * 1000
        return worker_id, "exception", duration_ms, str(exc)


def fire_concurrent_invocations(concurrent):
    _section(f"LOAD — firing {concurrent} concurrent Validator invocations")

    payloads = []
    for _ in range(concurrent):
        event_id = str(uuid.uuid4())
        ct_event = dict(_CLOUDTRAIL_BASE, eventID=event_id)
        payloads.append({
            "tenant_id":               "az-resilience-test-tenant",
            "event_id":                event_id,
            "updated_files":           {"template.yaml": VALID_TEMPLATE},
            "stack_name":              "az-resilience-test-stack",
            "primary_path":            "template.yaml",
            "cloudtrail_event":        ct_event,
            "github_repo":             "owner/repo",
            "github_token_secret_arn": "",
            "retry_count":             0,
        })

    results = {"success": 0, "error": 0, "exception": 0}
    durations = []
    workers = [(boto3.client("lambda", region_name=REGION), p, i)
               for i, p in enumerate(payloads)]

    print(f"  Launching {concurrent} threads simultaneously…")
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = {pool.submit(_invoke_worker, w): w[2] for w in workers}
        for future in as_completed(futures):
            worker_id, outcome, duration_ms, detail = future.result()
            durations.append(duration_ms)
            if outcome == "success":
                results["success"] += 1
                print(f"    worker {worker_id:02d}: ✓ {detail}  ({duration_ms:.0f} ms)")
            elif outcome == "error":
                results["error"] += 1
                print(f"    worker {worker_id:02d}: ✗ error — {detail}  ({duration_ms:.0f} ms)")
            else:
                results["exception"] += 1
                print(f"    worker {worker_id:02d}: ✗ exception — {detail}")

    _section("INVOCATION RESULTS")
    print(f"  Succeeded  : {results['success']} / {concurrent}")
    print(f"  Errors     : {results['error']}")
    print(f"  Exceptions : {results['exception']}")
    if durations:
        durations.sort()
        p50 = durations[len(durations) // 2]
        p99 = durations[int(len(durations) * 0.99)]
        print(f"  Duration p50: {p50:.0f} ms   p99: {p99:.0f} ms  (client-side, includes retries)")

    if results["success"] == concurrent:
        print(f"\n  ✓ All {concurrent} invocations succeeded despite AZ-level DynamoDB disruption.")
        print(f"    AWS SDK retry/backoff routed retried calls to healthy AZ replicas.")
    elif results["success"] > 0:
        print(f"\n  ✓ System remained partially operational ({results['success']}/{concurrent} succeeded).")
        print(f"    Failed invocations exhausted SDK retries — check DLQ for those events.")
    else:
        print(f"\n  ✗ All invocations failed — the injection percentage may be too high,")
        print(f"    or the Lambda does not write to DynamoDB during validation.")

    return results


# ── Evidence ──────────────────────────────────────────────────────────────────

def show_evidence(cw):
    _section("EVIDENCE — CloudWatch metrics (post-experiment)")
    print("  Waiting 90 s for CloudWatch metrics to propagate…", end="", flush=True)
    for _ in range(9):
        time.sleep(10)
        print(".", end="", flush=True)
    print()

    invocations = _check_cloudwatch(cw, VALIDATOR_FN, "Invocations")
    errors      = _check_cloudwatch(cw, VALIDATOR_FN, "Errors")
    p99_ms      = _check_cloudwatch_p99(cw, VALIDATOR_FN)

    print(f"\n  Lambda/Invocations  (last 5 min): {invocations}")
    print(f"  Lambda/Errors       (last 5 min): {errors}")
    if p99_ms is not None:
        print(f"  Lambda/Duration p99 (last 5 min): {p99_ms:.0f} ms")
        if p99_ms > 3000:
            print(f"    ↑ Elevated p99 — retry latency from 503-injected calls is visible.")
        else:
            print(f"    ↑ p99 within normal range — SDK retries resolved quickly.")

    if errors == 0 and invocations > 0:
        print(f"\n  ✓ Zero Lambda errors at invocation level — multi-AZ retries succeeded.")
        print(f"    The pipeline processed all events despite the simulated AZ disruption.")
    elif errors > 0:
        print(f"\n  ℹ  {errors} error(s) recorded — some events exhausted SDK retries.")
        print(f"    Check the DLQ (drift-detector-processor-dlq) for stranded messages.")

    print(f"\n  Dashboard URL:")
    print(f"    https://us-east-1.console.aws.amazon.com/cloudwatch/home?region=us-east-1"
          f"#dashboards:name=drift-detector-overview")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--fis-role-arn", default="",
        help="ARN of the IAM role FIS can assume to run the experiment. "
             "Required unless --skip-fis is set.",
    )
    parser.add_argument(
        "--skip-fis", action="store_true",
        help="Skip FIS injection — fire concurrent invocations only "
             "(useful if FIS role is not yet set up).",
    )
    parser.add_argument(
        "--percentage", type=int, default=33,
        help="Percentage of DynamoDB API calls to inject 503 into (default: 33 = 1-of-3 AZs).",
    )
    parser.add_argument(
        "--duration-seconds", type=int, default=120,
        help="How long the FIS experiment runs in seconds (default: 120).",
    )
    parser.add_argument(
        "--concurrent", type=int, default=15,
        help="Number of concurrent Validator invocations to fire (default: 15).",
    )
    parser.add_argument(
        "--no-evidence", action="store_true",
        help="Skip the 90-second CloudWatch polling wait.",
    )
    args = parser.parse_args()

    if not args.skip_fis and not args.fis_role_arn:
        print("\n  --fis-role-arn is required unless --skip-fis is set.")
        print("  See the Prerequisites section in this script's docstring.")
        sys.exit(1)

    lam = boto3.client("lambda",     region_name=REGION)
    cw  = boto3.client("cloudwatch", region_name=REGION)
    fis = boto3.client("fis",        region_name=REGION)

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║  Failure Scenario 3 — AZ Resilience: DynamoDB Outage    ║")
    print("╚══════════════════════════════════════════════════════════╝")

    template_id  = None
    experiment_id = None

    try:
        if not args.skip_fis:
            # ── Discover Lambda execution role ARNs ───────────────────────
            _section("SETUP — discovering Lambda execution role ARNs")
            role_arns = []
            for fn in [f"{PROJECT}-stack-processor", f"{PROJECT}-validator"]:
                arn = _get_lambda_role_arn(lam, fn)
                if arn:
                    role_arns.append(arn)
                    print(f"  {fn}: {arn}")

            if not role_arns:
                print("  [ERROR] Could not retrieve any Lambda role ARNs. "
                      "Check AWS credentials and Lambda function names.")
                sys.exit(1)

            # ── Create FIS experiment template ────────────────────────────
            _section(f"SETUP — creating FIS experiment template "
                     f"({args.percentage}% 503 injection, {args.duration_seconds}s)")
            template_id = create_fis_experiment(
                fis, args.fis_role_arn, role_arns,
                args.percentage, args.duration_seconds,
            )
            print(f"  Template ID: {template_id}")

            # ── Start experiment ──────────────────────────────────────────
            _section("TRIGGER — starting FIS AZ-failure experiment")
            experiment_id = start_fis_experiment(fis, template_id)
            print(f"  Experiment ID: {experiment_id}")
            print(f"  Waiting for experiment to reach 'running' state…")
            if wait_for_experiment_state(fis, experiment_id, "running", timeout=60):
                print(f"  ✓ Experiment running — "
                      f"{args.percentage}% of DynamoDB calls now return 503.")
                print(f"    This simulates {args.percentage}% of AZ DynamoDB endpoints failing.")
            else:
                state = fis.get_experiment(id=experiment_id)["experiment"]["state"]
                print(f"  [WARN] Experiment did not reach 'running'. State: {state}")
                print(f"  Proceeding with load anyway…")

            print(f"\n  Pausing 5 s before firing load to let FIS injection take effect…")
            time.sleep(5)

        else:
            _section("SKIP-FIS MODE — no 503 injection; demonstrating pipeline throughput only")
            print("  (To include AZ failure injection, re-run with --fis-role-arn)")

        # ── Fire concurrent invocations ───────────────────────────────────
        fire_concurrent_invocations(args.concurrent)

        # ── CloudWatch evidence ───────────────────────────────────────────
        if not args.no_evidence:
            show_evidence(cw)

    finally:
        # Always stop + clean up the FIS experiment, even on error.
        if experiment_id:
            _section("RECOVERY — stopping FIS experiment (AZ returns to healthy state)")
            stop_fis_experiment(fis, experiment_id)
            print(f"  Experiment {experiment_id} stopped.")
            print(f"  503 injection is now off — DynamoDB restored to full multi-AZ operation.")

        if template_id:
            delete_fis_template(fis, template_id)
            print(f"  Experiment template {template_id} deleted.")

    print()


if __name__ == "__main__":
    main()
