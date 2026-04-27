"""
Resolves which CloudFormation stack owns a resource that drifted.

Strategy: every resource deployed by CloudFormation carries the tag
  aws:cloudformation:stack-name → <stack name>
so we just fetch that resource's tags instead of walking all stacks.
"""

import logging

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

CFN_STACK_TAG = "aws:cloudformation:stack-name"


def resolve_stack_name(creds, cloudtrail_event):
    """Return the CFn stack name that owns the resource touched by this event."""
    event_source = cloudtrail_event.get("eventSource", "")
    params = cloudtrail_event.get("requestParameters") or {}
    region = cloudtrail_event.get("awsRegion", "us-east-1")

    # CloudFormation API calls carry stackName directly — fast path.
    stack_name = params.get("stackName")
    if stack_name:
        return stack_name

    handler = _HANDLERS.get(event_source)
    if not handler:
        raise ValueError(f"Unsupported event source for tag resolution: {event_source}")

    tags = handler(creds, params, region)
    stack_name = tags.get(CFN_STACK_TAG)
    if not stack_name:
        raise ValueError(
            f"Resource touched by {cloudtrail_event.get('eventName')} has no "
            f"'{CFN_STACK_TAG}' tag — is it managed by CloudFormation?"
        )
    return stack_name


# ── Per-service tag fetchers ──────────────────────────────────────────────────

def _s3_tags(creds, params, region):
    bucket = params.get("bucketName")
    s3 = boto3.client("s3", **_kwargs(creds))
    try:
        resp = s3.get_bucket_tagging(Bucket=bucket)
        return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchTagSet":
            return {}
        raise


def _ec2_tags(creds, params, region):
    # EC2 events carry different ID fields depending on the API call.
    resource_id = (
        params.get("instanceId")
        or _dig(params, "instancesSet", "items", 0, "instanceId")
        or params.get("groupId")
        or params.get("subnetId")
        or params.get("vpcId")
        or params.get("allocationId")
        or params.get("networkInterfaceId")
    )
    if not resource_id:
        raise ValueError(f"Cannot extract EC2 resource ID from params: {list(params)}")

    ec2 = boto3.client("ec2", region_name=region, **_kwargs(creds))
    resp = ec2.describe_tags(Filters=[{"Name": "resource-id", "Values": [resource_id]}])
    return {t["Key"]: t["Value"] for t in resp.get("Tags", [])}


def _rds_tags(creds, params, region):
    db_id = params.get("dBInstanceIdentifier") or params.get("dBClusterIdentifier")
    rds = boto3.client("rds", region_name=region, **_kwargs(creds))

    if params.get("dBClusterIdentifier"):
        resp = rds.describe_db_clusters(DBClusterIdentifier=db_id)
        arn = resp["DBClusters"][0]["DBClusterArn"]
    else:
        resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
        arn = resp["DBInstances"][0]["DBInstanceArn"]

    tags_resp = rds.list_tags_for_resource(ResourceName=arn)
    return {t["Key"]: t["Value"] for t in tags_resp.get("TagList", [])}


def _lambda_tags(creds, params, region):
    func_name = params.get("functionName")
    lmb = boto3.client("lambda", region_name=region, **_kwargs(creds))
    resp = lmb.get_function(FunctionName=func_name)
    arn = resp["Configuration"]["FunctionArn"]
    return lmb.list_tags(Resource=arn).get("Tags", {})


def _iam_tags(creds, params, region):
    # IAM is global — region param unused.
    role_name = params.get("roleName")
    iam = boto3.client("iam", **_kwargs(creds))
    resp = iam.list_role_tags(RoleName=role_name)
    return {t["Key"]: t["Value"] for t in resp.get("Tags", [])}


def _sns_tags(creds, params, region):
    topic_arn = params.get("topicArn")
    sns = boto3.client("sns", region_name=region, **_kwargs(creds))
    resp = sns.list_tags_for_resource(ResourceArn=topic_arn)
    return {t["Key"]: t["Value"] for t in resp.get("Tags", [])}


def _sqs_tags(creds, params, region):
    queue_url = params.get("queueUrl")
    sqs = boto3.client("sqs", region_name=region, **_kwargs(creds))
    return sqs.list_queue_tags(QueueUrl=queue_url).get("Tags", {})


def _dynamodb_tags(creds, params, region):
    table_name = params.get("tableName")
    ddb = boto3.client("dynamodb", region_name=region, **_kwargs(creds))
    # Need the ARN to call list_tags_of_resource.
    desc = ddb.describe_table(TableName=table_name)
    arn = desc["Table"]["TableArn"]
    resp = ddb.list_tags_of_resource(ResourceArn=arn)
    return {t["Key"]: t["Value"] for t in resp.get("Tags", [])}


# ── Dispatch table ────────────────────────────────────────────────────────────

_HANDLERS = {
    "s3.amazonaws.com":         _s3_tags,
    "ec2.amazonaws.com":        _ec2_tags,
    "rds.amazonaws.com":        _rds_tags,
    "lambda.amazonaws.com":     _lambda_tags,
    "iam.amazonaws.com":        _iam_tags,
    "sns.amazonaws.com":        _sns_tags,
    "sqs.amazonaws.com":        _sqs_tags,
    "dynamodb.amazonaws.com":   _dynamodb_tags,
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kwargs(creds):
    return {
        "aws_access_key_id":     creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token":     creds["SessionToken"],
    }


def _dig(d, *keys):
    """Safe nested access into dicts/lists."""
    for k in keys:
        try:
            d = d[k]
        except (KeyError, IndexError, TypeError):
            return None
    return d
