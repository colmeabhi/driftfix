"""
Processor Lambda

Triggered by SQS. For each CloudTrail write event:
1. Looks up the tenant config in DynamoDB
2. Assumes the customer's cross-account role via STS
3. Fetches the relevant CloudFormation template from the customer account
4. Calls Bedrock to generate an updated template reflecting the drift
5. Invokes the Validator Lambda with the generated template
"""

import json
import os
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
sts = boto3.client("sts")
lambda_client = boto3.client("lambda")

TENANTS_TABLE = os.environ["DYNAMODB_TENANTS_TABLE"]
RECONCILIATIONS_TABLE = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
AUDIT_BUCKET = os.environ["AUDIT_BUCKET"]
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "")
VALIDATOR_FUNCTION_NAME = os.environ["VALIDATOR_FUNCTION_NAME"]


def lambda_handler(event, context):
    batch_item_failures = []

    for record in event["Records"]:
        try:
            process_record(record)
        except Exception as e:
            logger.error("Failed to process record %s: %s", record["messageId"], e)
            batch_item_failures.append({"itemIdentifier": record["messageId"]})

    return {"batchItemFailures": batch_item_failures}


def process_record(record):
    body = json.loads(record["body"])
    cloudtrail_event = extract_cloudtrail_event(body)

    tenant_id = resolve_tenant_id(cloudtrail_event)
    event_id = cloudtrail_event.get("eventID", record["messageId"])

    logger.info("Processing event %s for tenant %s", event_id, tenant_id)

    tenant = get_tenant(tenant_id)
    temp_creds = assume_cross_account_role(tenant["role_arn"], tenant["external_id"], event_id)
    cfn_template = fetch_cfn_template(temp_creds, cloudtrail_event)
    updated_template = invoke_bedrock(cfn_template, cloudtrail_event)

    invoke_validator(tenant_id, event_id, updated_template, cfn_template, cloudtrail_event, tenant)


def extract_cloudtrail_event(sqs_body):
    # EventBridge wraps the original SNS/S3 notification; unwrap to the CloudTrail record
    detail = sqs_body.get("detail", sqs_body)
    return detail


def resolve_tenant_id(cloudtrail_event):
    # TODO: implement tenant resolution logic (e.g. by account ID in the event)
    account_id = cloudtrail_event.get("recipientAccountId") or cloudtrail_event.get("userIdentity", {}).get("accountId")
    return account_id


def get_tenant(tenant_id):
    table = dynamodb.Table(TENANTS_TABLE)
    resp = table.get_item(Key={"tenant_id": tenant_id})
    item = resp.get("Item")
    if not item:
        raise ValueError(f"Unknown tenant: {tenant_id}")
    return item


def assume_cross_account_role(role_arn, external_id, session_name):
    resp = sts.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"drift-{session_name[:32]}",
        ExternalId=external_id,
        DurationSeconds=3600,
    )
    return resp["Credentials"]


def fetch_cfn_template(creds, cloudtrail_event):
    cfn = boto3.client(
        "cloudformation",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    # TODO: determine which stack the event belongs to
    stack_name = cloudtrail_event.get("requestParameters", {}).get("stackName", "")
    if not stack_name:
        raise ValueError("Could not determine affected CloudFormation stack from event")

    resp = cfn.get_template(StackName=stack_name, TemplateStage="Original")
    return resp["TemplateBody"]


def invoke_bedrock(cfn_template, cloudtrail_event):
    if not BEDROCK_MODEL_ID:
        raise NotImplementedError("BEDROCK_MODEL_ID is not configured yet")

    # TODO: implement Bedrock call once model is decided
    raise NotImplementedError("Bedrock integration not yet implemented")


def invoke_validator(tenant_id, event_id, updated_template, original_template, cloudtrail_event, tenant):
    payload = {
        "tenant_id": tenant_id,
        "event_id": event_id,
        "updated_template": updated_template,
        "original_template": original_template,
        "cloudtrail_event": cloudtrail_event,
        "github_repo": tenant.get("github_repo", ""),
        "retry_count": 0,
    }
    lambda_client.invoke(
        FunctionName=VALIDATOR_FUNCTION_NAME,
        InvocationType="Event",  # async
        Payload=json.dumps(payload),
    )
