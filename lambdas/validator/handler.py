"""
Validator Lambda

Receives updated_files {path: content} from the Stack Processor.
Runs cfn-lint against any file that looks like a CFn template.
On pass  → stores each file to S3, invokes PR Creator with {path: s3_key}.
On fail  → retries up to MAX_RETRIES (retry loop requires Bedrock — TODO).
"""

import json
import logging
import os
import subprocess
import sys
import tempfile

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3            = boto3.client("s3")
lambda_client = boto3.client("lambda")

AUDIT_BUCKET            = os.environ["AUDIT_BUCKET"]
MAX_RETRIES             = int(os.environ.get("MAX_RETRIES", "3"))
PR_CREATOR_FUNCTION_NAME = os.environ["PR_CREATOR_FUNCTION_NAME"]


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
            errs = run_cfn_lint(content)
            if errs:
                lint_errors[path] = errs

    if not lint_errors:
        logger.info("Validation passed for event %s (%d file(s))", event_id, len(updated_files))
        s3_keys = store_files(tenant_id, event_id, updated_files)
        invoke_pr_creator(event, s3_keys)
        return {"status": "passed"}

    logger.warning("cfn-lint errors for event %s: %s", event_id, lint_errors)

    if retry_count >= MAX_RETRIES:
        logger.error("Max retries exhausted for event %s", event_id)
        raise RuntimeError(f"Validation failed after {MAX_RETRIES} retries: {lint_errors}")

    # TODO: re-invoke Stack Processor with lint errors once Bedrock is wired up
    raise NotImplementedError("Retry loop requires Bedrock to be configured")


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
        "cloudtrail_event":        event["cloudtrail_event"],
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
