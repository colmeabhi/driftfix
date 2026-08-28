"""
Orchestrator Lambda — Batch Mode

Triggered by EventBridge schedule (daily at 7 AM UTC).
For each tenant:
  1. Assumes cross-account role via STS
  2. Lists CloudTrail log files from the lookback window (72h) in the customer's S3 bucket
  3. Parses + filters write events (skipping failed calls and CloudFormation's own deploys)
  4. Deduplicates against reconciliations table: finished and in-flight events
     are skipped, failed or stuck ones are retried up to MAX_ATTEMPTS
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

from reconciliation import STATUS_QUEUED, expires_at, gave_up, should_process
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

CLOUDFORMATION = "cloudformation.amazonaws.com"

# Three times the daily schedule: an event whose processing failed is still in
# the window for the next two runs. Dedup makes the overlap between runs safe.
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "72"))


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
    logger.info("Tenant %s — %d log file(s) in last %dh", tenant_id, len(log_keys), LOOKBACK_HOURS)

    all_write_events = []
    for key in log_keys:
        try:
            records = parse_log_file(creds, tenant["cloudtrail_bucket"], key)
            all_write_events.extend(r for r in records if is_write_event(r))
        except Exception as e:
            logger.error("Tenant %s — failed parsing %s: %s", tenant_id, key, e)

    new_events, attempts = filter_already_processed(tenant_id, all_write_events)
    logger.info("Tenant %s — %d write event(s) to process after dedup (%d retried)",
                tenant_id, len(new_events), sum(1 for n in attempts.values() if n))
    if not new_events:
        return

    stack_events = group_events_by_stack(creds, new_events)
    logger.info("Tenant %s — %d unique stack(s) affected", tenant_id, len(stack_events))

    for stack_name, events in stack_events.items():
        mark_events_queued(tenant_id, events, attempts)
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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
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
    """True for a successful out-of-band write — the only kind that causes drift."""
    if event.get("readOnly", False):
        return False
    if event.get("errorCode"):
        return False                    # the call failed, so nothing changed
    if is_cloudformation_originated(event):
        return False
    return event.get("eventName", "").startswith(WRITE_PREFIXES)


def is_cloudformation_originated(event):
    """
    True when the change came from a CloudFormation deployment rather than a
    manual edit. That covers calls to CloudFormation itself (UpdateStack,
    ExecuteChangeSet, ...) and the calls CloudFormation makes to other services
    on the user's behalf while deploying, which CloudTrail stamps with
    invokedBy / sourceIPAddress = cloudformation.amazonaws.com.
    """
    if event.get("eventSource") == CLOUDFORMATION:
        return True
    identity = event.get("userIdentity") or {}
    return (
        identity.get("invokedBy") == CLOUDFORMATION
        or event.get("sourceIPAddress") == CLOUDFORMATION
    )


def filter_already_processed(tenant_id, events):
    """
    Return (events_to_process, attempts) where attempts maps eventID to the
    number of times that event has already been tried. An event is processed
    if it is new, or if an earlier attempt failed or got stuck and it still
    has attempts left — see reconciliation.should_process.
    """
    # Drop any records missing a string eventID — malformed CloudTrail entries.
    # The lookback window overlaps between runs, so also collapse duplicates.
    valid = list({e["eventID"]: e for e in events if isinstance(e.get("eventID"), str)}.values())
    if not valid:
        return [], {}

    records = {}
    for i in range(0, len(valid), 100):
        chunk = valid[i:i + 100]
        try:
            records.update(_get_records(tenant_id, [e["eventID"] for e in chunk]))
        except Exception as ex:
            logger.warning("BatchGetItem chunk %d failed, treating as unprocessed: %s", i // 100, ex)

    to_process, attempts = [], {}
    for event in valid:
        event_id = event["eventID"]
        process, tried = should_process(records.get(event_id))
        if process:
            to_process.append(event)
            attempts[event_id] = tried
        elif gave_up(records.get(event_id)):
            logger.error("Tenant %s — event %s gave up after %d attempt(s)",
                         tenant_id, event_id, tried)
    return to_process, attempts


def _get_records(tenant_id, event_ids):
    """BatchGetItem for up to 100 events, following UnprocessedKeys."""
    request = {
        RECONCILIATIONS_TABLE: {
            "Keys": [{"tenant_id": tenant_id, "event_id": eid} for eid in event_ids],
            "ProjectionExpression": "event_id, #s, #q, #a",
            "ExpressionAttributeNames": {"#s": "status", "#q": "queued_at", "#a": "attempts"},
        }
    }
    records = {}
    while request:
        resp = dynamodb.batch_get_item(RequestItems=request)
        for item in resp["Responses"].get(RECONCILIATIONS_TABLE, []):
            records[item["event_id"]] = item
        request = resp.get("UnprocessedKeys")
    return records


def group_events_by_stack(creds, events):
    stacks = {}
    for event in events:
        try:
            stack_name = resolve_stack_name(creds, event)
            stacks.setdefault(stack_name, []).append(event)
        except (ValueError, ClientError) as e:
            logger.warning("Skipping event %s: %s", event.get("eventID"), e)
    return stacks


def mark_events_queued(tenant_id, events, attempts):
    """
    Record that these events are in flight. The record stays "queued" until a
    downstream Lambda reports pr_opened, no_change or failed; if none does, it
    goes stale and the next batch run retries it.
    """
    table = dynamodb.Table(RECONCILIATIONS_TABLE)
    now = datetime.now(timezone.utc)
    with table.batch_writer() as batch:
        for event in events:
            batch.put_item(Item={
                "tenant_id": tenant_id,
                "event_id": event["eventID"],
                "status": STATUS_QUEUED,
                "queued_at": now.isoformat(),
                "attempts": attempts.get(event["eventID"], 0) + 1,
                "expires_at": expires_at(now),
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
