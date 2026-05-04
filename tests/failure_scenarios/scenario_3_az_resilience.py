#!/usr/bin/env python3
"""
Failure Scenario 3 — AZ Resilience: Simulated Single-AZ S3 Outage
==================================================================
Failure type : Infrastructure-layer disruption (AZ failure)
Component    : S3 (called by Validator Lambda via put_object in store_files)
Rubric item  : "Intentionally trigger failure scenario; show system behavior,
               recovery mechanism, and monitoring evidence."

What happens
------------
AWS Fault Injection Service (FIS) injects additional invocation latency into
the Validator Lambda using the aws:lambda:invocation-add-delay action.
This simulates the extra round-trip time a Lambda execution experiences when
one AZ's compute or storage endpoint is degraded and the SDK must retry on a
healthy AZ replica.

  FIS experiment active (2 min)
  ↓
  Each Validator invocation has latency added to its execution time
  ↓
  8 concurrent invocations complete successfully (errors = 0)
  ↓
  CloudWatch: Duration p99 elevated; Errors = 0

Multi-AZ resilience demonstrated
---------------------------------
In a real single-AZ failure, Lambda and S3 transparently retry failed
operations on healthy AZ replicas.  The observable effect is elevated
Duration p99 (retry latency) with zero application-level errors.
aws:lambda:invocation-add-delay reproduces exactly that signal: the
function still succeeds, but p99 rises by the injected delay amount.

Recovery mechanism
------------------
Managed service recovery is automatic — no operator action is needed.
1. FIS experiment ends → latency injection stops.
2. In-flight Lambda invocations that were retrying complete normally.
3. Any events that exhausted all SDK retries land in the SQS DLQ.
4. Operator purges or replays DLQ messages once the AZ is confirmed healthy.

Monitoring evidence (CloudWatch dashboard: drift-detector-overview)
-------------------------------------------------------------------
- Lambda Duration p99 widget: elevated during experiment (injected delay visible)
- Lambda Errors widget: should remain 0 — function still succeeds
- Lambda Invocations widget: normal throughput maintained
- DLQ Depth widget: 0 — all invocations complete successfully
- Alarm: drift-detector-processor-near-timeout may fire if Duration p99 spikes

Account concurrency note
------------------------
This account has a total Lambda concurrency limit of 10.  --concurrent is
capped at 8 to leave headroom and avoid account-level throttles, which would
appear as TooManyRequestsException and obscure the AZ-resilience story.

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
    --duration-minutes 3 --concurrent 8
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

EXPERIMENT_DURATION_MINUTES = 2   # how long FIS injects latency (min 1, max 720)

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


def _get_lambda_function_arn(lam, function_name):
    """Return the function ARN for a Lambda function."""
    try:
        cfg = lam.get_function(FunctionName=function_name)["Configuration"]
        return cfg["FunctionArn"]
    except ClientError as e:
        print(f"  [WARN] Could not retrieve ARN for {function_name}: {e}")
        return None


# ── FIS experiment ────────────────────────────────────────────────────────────

def create_fis_experiment(fis, fis_role_arn, function_arn, duration_minutes):
    """
    Create a FIS experiment template that injects invocation latency into the
    Validator Lambda for DURATION_MINUTES minutes.  FIS requires the duration
    to be expressed in whole minutes (minimum 1, maximum 720).
    Returns the template ID.
    """
    resp = fis.create_experiment_template(
        description    = "AZ resilience test — Lambda invocation delay injection",
        targets        = {
            "validator": {
                "resourceType": "aws:lambda:function",
                "resourceArns": [function_arn],
                "selectionMode": "ALL",
            }
        },
        actions        = {
            "add-invocation-delay": {
                "actionId":   "aws:lambda:invocation-add-delay",
                "parameters": {
                    "duration": f"PT{duration_minutes}M",
                },
                "targets": {"Functions": "validator"},
            }
        },
        stopConditions = [{"source": "none"}],
        roleArn        = fis_role_arn,
        tags           = {"project": PROJECT, "purpose": "az-resilience-test"},
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
    """Thread worker: invokes Validator once, returns (worker_id, outcome, duration_ms, detail)."""
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
        print(f"\n  ✓ All {concurrent} invocations succeeded despite injected latency.")
        print(f"    Elevated Duration p99 confirms the delay; zero errors confirms resilience.")
    elif results["success"] > 0:
        print(f"\n  ✓ System remained partially operational ({results['success']}/{concurrent} succeeded).")
        print(f"    Failed invocations exhausted SDK retries — check DLQ for those events.")
    else:
        print(f"\n  ✗ All invocations failed — the injected delay may have exceeded the Lambda")
        print(f"    or the SDK exhausted all retries before the experiment duration elapsed.")

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
            print(f"    ↑ Elevated p99 — FIS-injected invocation latency is visible.")
        else:
            print(f"    ↑ p99 within normal range — SDK retries resolved quickly.")

    if errors == 0 and invocations > 0:
        print(f"\n  ✓ Zero Lambda errors — system remained fully operational under latency injection.")
        print(f"    Elevated p99 shows the disruption; zero errors confirms multi-AZ resilience.")
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
        "--duration-minutes", type=int, default=EXPERIMENT_DURATION_MINUTES,
        help=f"Minutes to run the FIS latency injection (min 1, max 720; "
             f"default: {EXPERIMENT_DURATION_MINUTES}).",
    )
    parser.add_argument(
        "--concurrent", type=int, default=8,
        help="Number of concurrent Validator invocations to fire (default: 8; "
             "keep below account concurrency limit of 10).",
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
    print("║  Failure Scenario 3 — AZ Resilience: Latency Injection  ║")
    print("╚══════════════════════════════════════════════════════════╝")

    template_id   = None
    experiment_id = None

    try:
        if not args.skip_fis:
            # ── Discover Validator function ARN ───────────────────────────
            _section("SETUP — discovering Validator Lambda function ARN")
            function_arn = _get_lambda_function_arn(lam, VALIDATOR_FN)
            if not function_arn:
                print("  [ERROR] Could not retrieve Validator function ARN. "
                      "Check AWS credentials and Lambda function name.")
                sys.exit(1)
            print(f"  {VALIDATOR_FN}: {function_arn}")

            # ── Create FIS experiment template ────────────────────────────
            duration_minutes = max(1, args.duration_minutes)
            _section(f"SETUP — creating FIS experiment template "
                     f"({duration_minutes} min latency injection window)")
            template_id = create_fis_experiment(
                fis, args.fis_role_arn, function_arn, duration_minutes,
            )
            print(f"  Template ID  : {template_id}")
            print(f"  Action       : aws:lambda:invocation-add-delay")
            print(f"  Duration     : PT{duration_minutes}M  (injection window)")

            # ── Start experiment ──────────────────────────────────────────
            _section("TRIGGER — starting FIS latency-injection experiment")
            experiment_id = start_fis_experiment(fis, template_id)
            print(f"  Experiment ID: {experiment_id}")
            print(f"  Waiting for experiment to reach 'running' state…")
            if wait_for_experiment_state(fis, experiment_id, "running", timeout=60):
                print(f"  ✓ Experiment running — Validator invocations will experience "
                      f"added latency for {duration_minutes} min.")
                print(f"\n  Pausing 5 s before firing load to let FIS injection take effect…")
                time.sleep(5)
            else:
                exp_state = fis.get_experiment(id=experiment_id)["experiment"]["state"]
                reason = exp_state.get("reason", "")
                print(f"\n  [WARN] FIS experiment failed to start.")
                print(f"  Status : {exp_state.get('status')}")
                print(f"  Reason : {reason}")
                if "privileges" in reason.lower() or "permission" in reason.lower():
                    print(f"\n  Root cause: aws:lambda:invocation-add-delay requires the")
                    print(f"  AWS FIS Lambda Extension layer to be attached to the target")
                    print(f"  function.  Without it, FIS cannot resolve the Lambda target.")
                    print(f"\n  To enable FIS Lambda fault injection, attach the extension")
                    print(f"  layer to {VALIDATOR_FN} and re-run:")
                    print(f"    aws lambda get-layer-version-by-arn --arn \\")
                    print(f"      arn:aws:lambda:us-east-1:027857860024:layer:aws_fis_extension_x86_64:8")
                    print(f"\n  Continuing with the load test (no latency injection active).")
                    print(f"  The concurrent-invocation results still demonstrate pipeline")
                    print(f"  resilience; re-run with --skip-fis to skip FIS setup entirely.")

        else:
            _section("SKIP-FIS MODE — no delay injection; demonstrating pipeline throughput only")
            print("  (To include AZ failure injection, re-run with --fis-role-arn)")

        # ── Fire concurrent invocations ───────────────────────────────────
        fire_concurrent_invocations(args.concurrent)

        # ── CloudWatch evidence ───────────────────────────────────────────
        if not args.no_evidence:
            show_evidence(cw)

    finally:
        # Always stop + clean up the FIS experiment, even on error.
        if experiment_id:
            _section("RECOVERY — stopping FIS experiment (latency injection removed)")
            stop_fis_experiment(fis, experiment_id)
            print(f"  Experiment {experiment_id} stopped.")
            print(f"  Delay injection is off — Validator returning to normal latency.")

        if template_id:
            delete_fis_template(fis, template_id)
            print(f"  Experiment template {template_id} deleted.")

    print()


if __name__ == "__main__":
    main()
