"""
Health Check Lambda

Runs every 5 minutes via EventBridge. Probes each pipeline component for
availability and publishes custom CloudWatch metrics so the
drift-detector-status dashboard and component health alarms reflect live
service state without requiring real pipeline traffic.

Components checked
------------------
  Lambda functions   GetFunctionConfiguration → State == "Active"
  DynamoDB tables    DescribeTable            → TableStatus == "ACTIVE"
  S3 audit bucket    HeadBucket               → no ClientError
  SQS processor DLQ  GetQueueAttributes       → no ClientError

Metrics published  (namespace: ${METRIC_NAMESPACE})
---------------------------------------------------
  ProcessorHealth            : 1 (healthy) | 0 (unhealthy)
  StackProcessorHealth       : 1           | 0
  ValidatorHealth            : 1           | 0
  PrCreatorHealth            : 1           | 0
  ReconciliationsTableHealth : 1           | 0
  TenantsTableHealth         : 1           | 0
  AuditBucketHealth          : 1           | 0
  ProcessorDLQHealth         : 1           | 0

A JSON health report is also written to CloudWatch Logs on every invocation
so individual check failures are searchable in Log Insights.
"""

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION    = os.environ.get("AWS_REGION", "us-east-1")
NAMESPACE = os.environ.get("METRIC_NAMESPACE", "drift-detector/HealthCheck")

LAMBDA_CHECKS = {
    "ProcessorHealth":      os.environ["PROCESSOR_FUNCTION_NAME"],
    "StackProcessorHealth": os.environ["STACK_PROCESSOR_FUNCTION_NAME"],
    "ValidatorHealth":      os.environ["VALIDATOR_FUNCTION_NAME"],
    "PrCreatorHealth":      os.environ["PR_CREATOR_FUNCTION_NAME"],
}
DYNAMODB_CHECKS = {
    "ReconciliationsTableHealth": os.environ["DYNAMODB_RECONCILIATIONS_TABLE"],
    "TenantsTableHealth":         os.environ["DYNAMODB_TENANTS_TABLE"],
}
AUDIT_BUCKET = os.environ["AUDIT_BUCKET"]
DLQ_URL      = os.environ["DLQ_URL"]

_lam = boto3.client("lambda",     region_name=REGION)
_ddb = boto3.client("dynamodb",   region_name=REGION)
_s3  = boto3.client("s3",         region_name=REGION)
_sqs = boto3.client("sqs",        region_name=REGION)
_cw  = boto3.client("cloudwatch", region_name=REGION)


def _check_lambda(fn_name):
    try:
        cfg    = _lam.get_function_configuration(FunctionName=fn_name)
        state  = cfg.get("State", "Unknown")
        update = cfg.get("LastUpdateStatus", "Unknown")
        # InProgress covers the brief window between deploys; treat as healthy
        healthy = state == "Active" and update in ("Successful", "InProgress")
        return {"healthy": healthy, "state": state, "lastUpdateStatus": update}
    except ClientError as exc:
        return {"healthy": False, "error": str(exc)}


def _check_dynamodb(table_name):
    try:
        resp   = _ddb.describe_table(TableName=table_name)
        status = resp["Table"]["TableStatus"]
        return {"healthy": status == "ACTIVE", "tableStatus": status}
    except ClientError as exc:
        return {"healthy": False, "error": str(exc)}


def _check_s3():
    try:
        _s3.head_bucket(Bucket=AUDIT_BUCKET)
        return {"healthy": True}
    except ClientError as exc:
        return {"healthy": False, "error": str(exc)}


def _check_sqs():
    try:
        _sqs.get_queue_attributes(
            QueueUrl=DLQ_URL,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        return {"healthy": True}
    except ClientError as exc:
        return {"healthy": False, "error": str(exc)}


def _publish_metrics(results):
    _cw.put_metric_data(
        Namespace=NAMESPACE,
        MetricData=[
            {"MetricName": name, "Value": 1 if r["healthy"] else 0, "Unit": "Count"}
            for name, r in results.items()
        ],
    )


def lambda_handler(event, context):
    results = {}

    for metric, fn_name in LAMBDA_CHECKS.items():
        results[metric] = _check_lambda(fn_name)
    for metric, table in DYNAMODB_CHECKS.items():
        results[metric] = _check_dynamodb(table)

    results["AuditBucketHealth"]  = _check_s3()
    results["ProcessorDLQHealth"] = _check_sqs()

    _publish_metrics(results)

    overall   = all(r["healthy"] for r in results.values())
    unhealthy = [k for k, v in results.items() if not v["healthy"]]

    report = {
        "event":           "health_check",
        "overall_healthy": overall,
        "unhealthy":       unhealthy,
        "checks":          results,
    }
    if overall:
        logger.info(json.dumps(report))
    else:
        logger.error(json.dumps(report))

    return report
