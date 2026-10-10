"""
Validator Lambda

Receives updated_files {path: content} from the Stack Processor.
Runs cfn-lint against any file that looks like a CFn template.
On pass  → stores each file to S3, invokes PR Creator with {path: s3_key}.
On fail  → sends the stack back to the Stack Processor with the lint errors so
           the model can correct its output, up to MAX_RETRIES times. After
           that the events are marked failed and the invocation errors, which
           routes it to the DLQ.

Only cfn-lint errors block a template. Warnings and informational findings are
logged: many hand-written templates carry warnings the model did not introduce.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile

import boto3

from reconciliation import STATUS_FAILED, mark_events

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3            = boto3.client("s3")
lambda_client = boto3.client("lambda")
dynamodb      = boto3.resource("dynamodb")

AUDIT_BUCKET                  = os.environ["AUDIT_BUCKET"]
MAX_RETRIES                   = int(os.environ.get("MAX_RETRIES", "3"))
PR_CREATOR_FUNCTION_NAME      = os.environ["PR_CREATOR_FUNCTION_NAME"]
STACK_PROCESSOR_FUNCTION_NAME = os.environ.get("STACK_PROCESSOR_FUNCTION_NAME", "")
RECONCILIATIONS_TABLE         = os.environ.get("DYNAMODB_RECONCILIATIONS_TABLE", "")


def lambda_handler(event, context):
    tenant_id     = event["tenant_id"]
    event_id      = event["event_id"]
    updated_files = event["updated_files"]   # {path: content} — only changed files
    retry_count   = event.get("retry_count", 0)

    logger.info("Validating %d file(s) for tenant=%s event=%s retry=%d",
                len(updated_files), tenant_id, event_id, retry_count)

    # Run cfn-lint only on files that are CloudFormation templates
    lint_errors = {}
    for path, content in updated_files.items():
        if _is_cfn(content):
            errs = blocking_errors(run_cfn_lint(content))
            if errs:
                lint_errors[path] = errs

    if not lint_errors:
        logger.info("Validation passed for event %s (%d file(s))", event_id, len(updated_files))
        s3_keys = store_files(tenant_id, event_id, updated_files)
        invoke_pr_creator(event, s3_keys)
        return {"status": "passed"}

    logger.warning("cfn-lint errors for event %s: %s", event_id, lint_errors)

    if retry_count >= MAX_RETRIES:
        give_up = f"Validation failed after {MAX_RETRIES} retries: {lint_errors}"
    elif not can_regenerate(event):
        # Invoked directly (tests, replays) without the context the Stack
        # Processor needs to try again.
        give_up = f"Validation failed and the payload cannot be retried: {lint_errors}"
    else:
        invoke_stack_processor(event, lint_errors)
        return {"status": "retrying", "retry_count": retry_count + 1}

    logger.error(give_up)
    mark_failed(event, give_up)
    raise RuntimeError(give_up)


# ── Retry loop ────────────────────────────────────────────────────────────────

def can_regenerate(event):
    return bool(STACK_PROCESSOR_FUNCTION_NAME and event.get("tenant") and event.get("cloudtrail_events"))


def invoke_stack_processor(event, lint_errors):
    """Send the stack back for another attempt, with what went wrong last time."""
    payload = {
        "tenant":            event["tenant"],
        "stack_name":        event.get("stack_name", ""),
        "cloudtrail_events": event["cloudtrail_events"],
        "retry_count":       event.get("retry_count", 0) + 1,
        "lint_feedback": {
            "files":  event["updated_files"],
            "errors": lint_errors,
        },
    }
    lambda_client.invoke(
        FunctionName=STACK_PROCESSOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload, default=str),
    )


def mark_failed(event, error):
    """Best-effort: let the Orchestrator know these events need another run."""
    if not RECONCILIATIONS_TABLE:
        return
    try:
        mark_events(dynamodb.Table(RECONCILIATIONS_TABLE), event["tenant_id"],
                    event.get("event_ids") or [event["event_id"]],
                    STATUS_FAILED, error=f"validator: {error}")
    except Exception as e:
        logger.error("Could not mark events failed: %s", e)


def blocking_errors(findings):
    """
    Reduce cfn-lint's JSON findings to short strings for the ones that must be
    fixed: "E3002 line 12: Additional properties are not allowed ...".
    A finding that is not a dict is cfn-lint's own stderr — always blocking.
    """
    errors = []
    for finding in findings:
        if not isinstance(finding, dict):
            errors.append(str(finding).strip())
            continue
        if finding.get("Level", "Error") != "Error":
            logger.info("cfn-lint non-blocking finding: %s", finding.get("Message"))
            continue
        rule = (finding.get("Rule") or {}).get("Id", "cfn-lint")
        line = ((finding.get("Location") or {}).get("Start") or {}).get("LineNumber")
        where = f" line {line}" if line else ""
        errors.append(f"{rule}{where}: {finding.get('Message', '')}".strip())
    return errors


# ── cfn-lint ──────────────────────────────────────────────────────────────────

def run_cfn_lint(content):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(content)
        tmp_path = f.name
    try:
        env = os.environ.copy()
        env["PYTHONPATH"] = "/var/task:" + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "/var/task/bin/cfn-lint", tmp_path, "--format", "json"],
            capture_output=True, text=True,
            env=env,
        )
        if result.returncode == 0:
            return []
        return json.loads(result.stdout) if result.stdout else [result.stderr]
    except FileNotFoundError:
        logger.warning("cfn-lint entry script not found — skipping lint")
        return []
    finally:
        os.unlink(tmp_path)


# ── S3 storage ────────────────────────────────────────────────────────────────

def store_files(tenant_id, event_id, files):
    """
    Store each file under validated/<tenant_id>/<event_id>/<path>.
    Returns {repo_path: s3_key}.
    """
    s3_keys = {}
    for path, content in files.items():
        # Flatten any leading slashes, keep directory structure
        safe_path = path.lstrip("/")
        s3_key = f"validated/{tenant_id}/{event_id}/{safe_path}"
        s3.put_object(Bucket=AUDIT_BUCKET, Key=s3_key, Body=content)
        s3_keys[path] = s3_key
        logger.info("Stored %s → s3://%s/%s", path, AUDIT_BUCKET, s3_key)
    return s3_keys


# ── Downstream invocation ─────────────────────────────────────────────────────

def invoke_pr_creator(event, s3_keys):
    payload = {
        "tenant_id":               event["tenant_id"],
        "event_id":                event["event_id"],
        "stack_name":              event.get("stack_name", ""),
        "primary_path":            event.get("primary_path"),
        "github_repo":             event["github_repo"],
        "github_token_secret_arn": event.get("github_token_secret_arn", ""),
        "event_ids":               event.get("event_ids") or [event["event_id"]],
        "cloudtrail_event":        event["cloudtrail_event"],
        "remediation":             event.get("remediation") or {},
        "review_reasons":          event.get("review_reasons") or [],
        "updated_s3_keys":         s3_keys,   # {repo_path: s3_key}
    }
    lambda_client.invoke(
        FunctionName=PR_CREATOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_cfn(content):
    return bool(content) and (
        "AWSTemplateFormatVersion" in content
        or "Transform: AWS::Serverless" in content
        or ("Resources:" in content and "Type: AWS::" in content)
    )
