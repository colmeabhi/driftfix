"""
Stack Processor Lambda

Invoked async by the Orchestrator — one invocation per affected stack per batch run.
Receives all CloudTrail write events for a single stack, fetches the current CFn
template, calls Bedrock to generate an updated template, then invokes the Validator.
"""

import json
import logging
import os
import boto3
import time
import random
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sts_client = boto3.client("sts")
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")
bedrock_runtime = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-2"))

RECONCILIATIONS_TABLE = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")
VALIDATOR_FUNCTION_NAME = os.environ["VALIDATOR_FUNCTION_NAME"]


def lambda_handler(event, context):
    tenant = event["tenant"]
    stack_name = event["stack_name"]
    cloudtrail_events = event["cloudtrail_events"]
    tenant_id = tenant["tenant_id"]

    logger.info(
        "Processing stack=%s tenant=%s events=%d",
        stack_name, tenant_id, len(cloudtrail_events),
    )

    creds = assume_cross_account_role(tenant["role_arn"], tenant["external_id"], stack_name)
    region = cloudtrail_events[0].get("awsRegion", "us-east-1")
    cfn_template = fetch_cfn_template(creds, stack_name, region)

    updated_template = invoke_bedrock(cfn_template, cloudtrail_events)

    # One PR per stack — use the first event as the representative record
    invoke_validator(tenant, stack_name, cloudtrail_events[0], updated_template, cfn_template)


def assume_cross_account_role(role_arn, external_id, session_name):
    resp = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"drift-{session_name[:32]}",
        ExternalId=external_id,
        DurationSeconds=3600,
    )
    return resp["Credentials"]


def fetch_cfn_template(creds, stack_name, region):
    cfn = boto3.client(
        "cloudformation",
        region_name=region,
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    resp = cfn.get_template(StackName=stack_name, TemplateStage="Original")
    return resp["TemplateBody"]


def invoke_bedrock(cfn_template, cloudtrail_events):
    if not BEDROCK_MODEL_ID:
        raise RuntimeError("BEDROCK_MODEL_ID is not configured")

    evidence = {
        "region": cloudtrail_events[0].get("awsRegion", "unknown") if cloudtrail_events else "unknown",
        "event_count": len(cloudtrail_events),
        "events": cloudtrail_events[:50],  # cap to avoid huge prompts
        "note": "Only use the evidence provided; do not invent changes.",
    }

    instructions = (
        "You are an Infrastructure as Code assistant.\n"
        "Given:\n"
        "1) ORIGINAL CloudFormation template\n"
        "2) EVIDENCE: CloudTrail write events for this stack\n\n"
        "Task: Produce an UPDATED CloudFormation template that matches the changes in EVIDENCE.\n"
        "Rules:\n"
        "- Only modify resources/properties directly supported by EVIDENCE.\n"
        "- Do not invent resources, properties, or values.\n"
        "- If evidence is insufficient, return the ORIGINAL template unchanged and set manual_review_required=true.\n"
        "- Output ONLY valid JSON with exactly these keys:\n"
        "  updated_template (string), remediation (object)\n"
        "- remediation must include: summary, root_cause, confidence (0..1), proposed_changes (list), "
        "evidence_ids (list), manual_review_required (bool), warnings (list).\n"
    )

    user_input = {
        "original_template": cfn_template,
        "evidence": evidence,
    }

    request_body = {
        "messages": [
            {"role": "user", "content": instructions + "\nINPUT:\n" + json.dumps(user_input)}
        ],
        "max_tokens": 2048,
    }

    max_attempts = 4
    base_sleep = 0.6
    start = time.perf_counter()
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            resp = bedrock_runtime.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                body=json.dumps(request_body),
                contentType="application/json",
                accept="application/json",
            )
            raw = resp["body"].read()
            data = json.loads(raw)



            # Try to extract the model's text response
            model_text = None

            if (
                isinstance(data, dict)
                and isinstance(data.get("choices"), list)
                and data["choices"]
                and isinstance(data["choices"][0], dict)
            ):
                message = data["choices"][0].get("message", {})
                if isinstance(message, dict) and isinstance(message.get("content"), str):
                    model_text = message["content"]

            elif isinstance(data, dict) and isinstance(data.get("output"), str):
                model_text = data["output"]

            elif isinstance(data, dict) and isinstance(data.get("content"), str):
                model_text = data["content"]

            elif isinstance(data, dict) and isinstance(data.get("candidates"), list) and data["candidates"]:
                cand = data["candidates"][0]
                parts = cand.get("content", {}).get("parts", []) if isinstance(cand, dict) else []
                if parts and isinstance(parts[0], dict) and isinstance(parts[0].get("text"), str):
                    model_text = parts[0]["text"]

            if not model_text:
                raise RuntimeError(f"Could not extract model text from Bedrock response: {json.dumps(data)[:1000]}")

            model_text = model_text.strip()
            if model_text.startswith("```json"):
                model_text = model_text[len("```json"):].strip()
            if model_text.startswith("```"):
                model_text = model_text[len("```"):].strip()
            if model_text.endswith("```"):
                model_text = model_text[:-3].strip()

            result = json.loads(model_text)

            updated_template = result.get("updated_template")
            if not isinstance(updated_template, str) or not updated_template.strip():
                raise RuntimeError("Bedrock did not return a non-empty updated_template string")

            elapsed_ms = int((time.perf_counter() - start) * 1000)
            remediation = result.get("remediation", {})
            logger.info(
                "Bedrock success tenant_stack invoke attempt=%d latency_ms=%d manual_review=%s confidence=%s",
                attempt,
                elapsed_ms,
                remediation.get("manual_review_required") if isinstance(remediation, dict) else None,
                remediation.get("confidence") if isinstance(remediation, dict) else None,
            )

            return updated_template

        except ClientError as e:
            last_err = e
            code = e.response.get("Error", {}).get("Code", "Unknown")
            status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")

            retryable = code in {
                "ThrottlingException",
                "ModelNotReadyException",
                "ModelTimeoutException",
                "ServiceUnavailableException",
                "InternalServerException",
            } or (status is not None and status >= 500)

            if retryable and attempt < max_attempts:
                sleep_s = base_sleep * (2 ** (attempt - 1)) + random.random() * 0.2
                logger.warning("Bedrock retryable error code=%s status=%s attempt=%d sleep=%.2fs",
                               code, status, attempt, sleep_s)
                time.sleep(sleep_s)
                continue

            logger.error("Bedrock non-retryable error code=%s status=%s err=%s", code, status, str(e))
            raise

        except Exception as e:
            last_err = e
            if attempt < max_attempts:
                sleep_s = base_sleep * (2 ** (attempt - 1)) + random.random() * 0.2
                logger.warning("Bedrock unexpected error attempt=%d sleep=%.2fs err=%s", attempt, sleep_s, str(e))
                time.sleep(sleep_s)
                continue
            raise

    raise RuntimeError(f"Bedrock failed after {max_attempts} attempts: {last_err}")

def invoke_validator(tenant, stack_name, cloudtrail_event, updated_template, original_template):
    payload = {
        "tenant_id": tenant["tenant_id"],
        "event_id": cloudtrail_event["eventID"],
        "stack_name": stack_name,
        "updated_template": updated_template,
        "original_template": original_template,
        "cloudtrail_event": cloudtrail_event,
        "github_repo": tenant.get("github_repo", ""),
        "retry_count": 0,
    }
    lambda_client.invoke(
        FunctionName=VALIDATOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload),
    )
