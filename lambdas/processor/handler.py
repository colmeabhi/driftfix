"""
Orchestrator Lambda — Batch Mode

Triggered by EventBridge schedule (daily at 7 AM UTC).
For each tenant:
  1. Assumes cross-account role via STS
  2. Lists CloudTrail log files from last 24h from the customer's S3 bucket
  3. Parses + filters write events
  4. Deduplicates against reconciliations table (prevents reprocessing on retry)
  5. Groups new events by stack via tag-based resolver
  6. Invokes one Stack Processor Lambda per stack (async — runs in parallel)
"""

import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

from stack_resolver import resolve_stack_name

logger = logging.getLogger()
logger.setLevel(logging.INFO)

dynamodb = boto3.resource("dynamodb")
sts_client = boto3.client("sts")
lambda_client = boto3.client("lambda")

TENANTS_TABLE = os.environ["DYNAMODB_TENANTS_TABLE"]
RECONCILIATIONS_TABLE = os.environ["DYNAMODB_RECONCILIATIONS_TABLE"]
STACK_PROCESSOR_FUNCTION_NAME = os.environ["STACK_PROCESSOR_FUNCTION_NAME"]

WRITE_PREFIXES = ("Create", "Update", "Delete", "Put", "Modify", "Attach", "Detach", "Associate", "Disassociate")


def lambda_handler(event, context):
    tenants = list_tenants()
    logger.info("Batch run starting — %d tenant(s)", len(tenants))
    for tenant in tenants:
        try:
            process_tenant(tenant)
        except Exception as e:
            logger.error("Failed to process tenant %s: %s", tenant.get("tenant_id"), e)


def list_tenants():
    table = dynamodb.Table(TENANTS_TABLE)
    items = []
    kwargs = {}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        start_key = resp.get("LastEvaluatedKey")
        if not start_key:
            break
        kwargs["ExclusiveStartKey"] = start_key
    return items


def process_tenant(tenant):
    tenant_id = tenant["tenant_id"]
    creds = assume_cross_account_role(tenant["role_arn"], tenant["external_id"], tenant_id)

    log_keys = list_cloudtrail_logs(creds, tenant["cloudtrail_bucket"])
    logger.info("Tenant %s — %d log file(s) in last 24h", tenant_id, len(log_keys))

    all_write_events = []
    for key in log_keys:
        try:
            records = parse_log_file(creds, tenant["cloudtrail_bucket"], key)
            all_write_events.extend(r for r in records if is_write_event(r))
        except Exception as e:
            logger.error("Tenant %s — failed parsing %s: %s", tenant_id, key, e)

    new_events = filter_already_processed(tenant_id, all_write_events)
    logger.info("Tenant %s — %d new write event(s) after dedup", tenant_id, len(new_events))
    if not new_events:
        return

    stack_events = group_events_by_stack(creds, new_events)
    logger.info("Tenant %s — %d unique stack(s) affected", tenant_id, len(stack_events))

    for stack_name, events in stack_events.items():
        mark_events_queued(tenant_id, events)
        invoke_stack_processor(tenant, stack_name, events)


def assume_cross_account_role(role_arn, external_id, session_name):
    resp = sts_client.assume_role(
        RoleArn=role_arn,
        RoleSessionName=f"drift-{session_name[:32]}",
        ExternalId=external_id,
        DurationSeconds=3600,
    )
    return resp["Credentials"]


def list_cloudtrail_logs(creds, bucket):
    s3 = _s3_client(creds)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    keys = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="AWSLogs/"):
        for obj in page.get("Contents", []):
            if obj["LastModified"] >= cutoff and obj["Key"].endswith(".json.gz"):
                keys.append(obj["Key"])
    return keys


def parse_log_file(creds, bucket, key):
    s3 = _s3_client(creds)
    resp = s3.get_object(Bucket=bucket, Key=key)
    data = json.loads(gzip.decompress(resp["Body"].read()))
    return data.get("Records", [])


def is_write_event(event):
    if event.get("readOnly", False):
        return False
    return event.get("eventName", "").startswith(WRITE_PREFIXES)


def filter_already_processed(tenant_id, events):
    if not events:
        return []

    # Drop any records missing a string eventID — malformed CloudTrail entries
    valid = [e for e in events if isinstance(e.get("eventID"), str)]
    if not valid:
        return []

    existing_ids = set()

    for i in range(0, len(valid), 100):
        chunk = valid[i:i + 100]
        try:
            resp = dynamodb.batch_get_item(
                RequestItems={
                    RECONCILIATIONS_TABLE: {
                        "Keys": [
                            {"tenant_id": tenant_id, "event_id": e["eventID"]}
                            for e in chunk
                        ],
                        "ProjectionExpression": "event_id",
                    }
                }
            )
            for item in resp["Responses"].get(RECONCILIATIONS_TABLE, []):
                existing_ids.add(item["event_id"])
        except Exception as ex:
            logger.warning("BatchGetItem chunk %d failed, treating as unprocessed: %s", i // 100, ex)

    return [e for e in valid if e["eventID"] not in existing_ids]


def group_events_by_stack(creds, events):
    stacks = {}
    for event in events:
        try:
            stack_name = resolve_stack_name(creds, event)
            stacks.setdefault(stack_name, []).append(event)
        except (ValueError, ClientError) as e:
            logger.warning("Skipping event %s: %s", event.get("eventID"), e)
    return stacks


def mark_events_queued(tenant_id, events):
    table = dynamodb.Table(RECONCILIATIONS_TABLE)
    now = datetime.now(timezone.utc).isoformat()
    with table.batch_writer() as batch:
        for event in events:
            batch.put_item(Item={
                "tenant_id": tenant_id,
                "event_id": event["eventID"],
                "status": "queued",
                "queued_at": now,
            })


def invoke_stack_processor(tenant, stack_name, events):
    payload = {
        "tenant": tenant,
        "stack_name": stack_name,
        "cloudtrail_events": events,
    }
    lambda_client.invoke(
        FunctionName=STACK_PROCESSOR_FUNCTION_NAME,
        InvocationType="Event",
        Payload=json.dumps(payload, default=str),
    )


def _s3_client(creds):
    return boto3.client(
        "s3",
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
