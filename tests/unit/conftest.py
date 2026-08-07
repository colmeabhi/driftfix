"""
Test setup for the Lambda handlers.

Every Lambda keeps its code in a file called handler.py and reads its
configuration from environment variables at import time, so the handlers are
loaded by path under unique module names with a fake environment. No test
talks to AWS: boto3 clients are created but never called.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

LAMBDAS = Path(__file__).resolve().parents[2] / "lambdas"

os.environ.update({
    "AWS_DEFAULT_REGION":             "us-east-1",
    "AWS_ACCESS_KEY_ID":              "testing",
    "AWS_SECRET_ACCESS_KEY":          "testing",
    "DYNAMODB_TENANTS_TABLE":         "tenants",
    "DYNAMODB_RECONCILIATIONS_TABLE": "reconciliations",
    "STACK_PROCESSOR_FUNCTION_NAME":  "stack-processor",
    "VALIDATOR_FUNCTION_NAME":        "validator",
    "PR_CREATOR_FUNCTION_NAME":       "pr-creator",
    "AUDIT_BUCKET":                   "audit",
    "GITHUB_TOKEN_SECRET_ARN":        "arn:aws:secretsmanager:us-east-1:0:secret:t",
    "BEDROCK_MODEL_ID":               "test-model",
})

# The shared module and the processor's stack_resolver are imported by name.
sys.path.insert(0, str(LAMBDAS / "shared"))
sys.path.insert(0, str(LAMBDAS / "processor"))


def load_handler(lambda_name):
    name = f"{lambda_name}_handler"
    if name not in sys.modules:
        spec   = importlib.util.spec_from_file_location(name, LAMBDAS / lambda_name / "handler.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class FakeTable:
    """Records update_item calls so tests can assert on status changes."""

    def __init__(self):
        self.updates = []

    def update_item(self, **kwargs):
        self.updates.append(kwargs)

    def statuses(self):
        return {u["Key"]["event_id"]: u["ExpressionAttributeValues"][":s"] for u in self.updates}

    def attrs(self, event_id):
        """Extra attributes written for one event, as {name: value}."""
        for u in self.updates:
            if u["Key"]["event_id"] == event_id:
                names, values = u["ExpressionAttributeNames"], u["ExpressionAttributeValues"]
                return {names[k]: values[k.replace("#", ":")] for k in names if k != "#s"}
        return {}


class FakeLambdaClient:
    """Records async invocations and decodes their payloads."""

    def __init__(self):
        self.calls = []

    def invoke(self, FunctionName, InvocationType, Payload):
        import json
        self.calls.append((FunctionName, json.loads(Payload)))


@pytest.fixture
def table():
    return FakeTable()


@pytest.fixture
def lambda_client():
    return FakeLambdaClient()
