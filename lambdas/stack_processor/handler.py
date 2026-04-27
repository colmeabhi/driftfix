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

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sts_client = boto3.client("sts")
lambda_client = boto3.client("lambda")
dynamodb = boto3.resource("dynamodb")

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
        raise NotImplementedError("BEDROCK_MODEL_ID is not configured yet")
    raise NotImplementedError("Bedrock integration not yet implemented")


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
