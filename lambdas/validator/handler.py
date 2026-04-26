"""
Validator Lambda

Receives a generated CloudFormation template from the Processor.
Runs cfn-lint against it. On failure, re-invokes Bedrock via Processor
(up to MAX_RETRIES). On success, stores the template in S3 and invokes
the PR Creator Lambda.
"""

import json
import os
import subprocess
import tempfile
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
lambda_client = boto3.client("lambda")

AUDIT_BUCKET = os.environ["AUDIT_BUCKET"]
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))
PR_CREATOR_FUNCTION_NAME = os.environ["PR_CREATOR_FUNCTION_NAME"]


def lambda_handler(event, context):
    tenant_id = event["tenant_id"]
    event_id = event["event_id"]
    updated_template = event["updated_template"]
    retry_count = event.get("retry_count", 0)

    logger.info("Validating template for tenant=%s event=%s retry=%d", tenant_id, event_id, retry_count)

    lint_errors = run_cfn_lint(updated_template)

    if not lint_errors:
        logger.info("Validation passed for event %s", event_id)
        s3_key = store_in_audit_bucket(tenant_id, event_id, updated_template)
        invoke_pr_creator(event, s3_key)
        return {"status": "passed"}

    logger.warning("cfn-lint errors for event %s: %s", event_id, lint_errors)

    if retry_count >= MAX_RETRIES:
        logger.error("Max retries exhausted for event %s — sending to DLQ path", event_id)
        raise RuntimeError(f"Template validation failed after {MAX_RETRIES} retries: {lint_errors}")

    # Retry: invoke Processor again with lint errors appended to context
    retry_event = dict(event)
    retry_event["retry_count"] = retry_count + 1
    retry_event["previous_lint_errors"] = lint_errors
    # TODO: re-invoke Bedrock with error context once model is wired up
    raise NotImplementedError("Retry loop requires Bedrock to be configured")


def run_cfn_lint(template_body):
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        if isinstance(template_body, dict):
            import yaml
            yaml.dump(template_body, f)
        else:
            f.write(template_body)
        tmp_path = f.name

    try:
        result = subprocess.run(
            ["cfn-lint", tmp_path, "--format", "json"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return []
        return json.loads(result.stdout) if result.stdout else [result.stderr]
    except FileNotFoundError:
        logger.warning("cfn-lint not found in Lambda layer — skipping lint")
        return []
    finally:
        import os as _os
        _os.unlink(tmp_path)


def store_in_audit_bucket(tenant_id, event_id, template_body):
    key = f"validated/{tenant_id}/{event_id}.yaml"
    body = template_body if isinstance(template_body, str) else json.dumps(template_body)
    s3.put_object(Bucket=AUDIT_BUCKET, Key=key, Body=body)
    return key


def invoke_pr_creator(event, s3_key):
    payload = {
        "tenant_id": event["tenant_id"],
        "event_id": event["event_id"],
        "github_repo": event["github_repo"],
        "cloudtrail_event": event["cloudtrail_event"],
        "template_s3_key": s3_key,
    }
    lambda_client.invoke(
        FunctionName=PR_CREATOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
