import json

import pytest
import reconciliation as rec
from conftest import load_handler

validator = load_handler("validator")
sp        = load_handler("stack_processor")

TEMPLATE = "AWSTemplateFormatVersion: '2010-09-09'\nResources:\n  B:\n    Type: AWS::S3::Bucket\n"
TENANT   = {"tenant_id": "t1", "role_arn": "arn:role", "external_id": "x"}
EVENTS   = [{"eventID": "e1"}, {"eventID": "e2"}]

# Shape taken from real `cfn-lint --format json` output.
ERROR = {
    "Level": "Error",
    "Location": {"Start": {"LineNumber": 6, "ColumnNumber": 7}},
    "Message": "Additional properties are not allowed ('Bogus' was unexpected)",
    "Rule": {"Id": "E3002"},
}
WARNING = {"Level": "Warning", "Message": "Parameter not used", "Rule": {"Id": "W2001"}}


@pytest.fixture
def harness(monkeypatch, table, lambda_client):
    class Dynamo:
        def Table(self, name):
            return table

    stored = {}
    monkeypatch.setattr(validator, "dynamodb", Dynamo())
    monkeypatch.setattr(validator, "lambda_client", lambda_client)
    monkeypatch.setattr(validator, "RECONCILIATIONS_TABLE", "reconciliations")
    monkeypatch.setattr(validator, "STACK_PROCESSOR_FUNCTION_NAME", "stack-processor")
    monkeypatch.setattr(validator, "store_files",
                        lambda t, e, files: stored.update(files) or {p: f"s3/{p}" for p in files})

    def set_lint(*findings):
        monkeypatch.setattr(validator, "run_cfn_lint", lambda content: list(findings))

    return set_lint


def _payload(**overrides):
    payload = {
        "tenant": TENANT, "cloudtrail_events": EVENTS,
        "tenant_id": "t1", "event_id": "e1", "event_ids": ["e1", "e2"],
        "stack_name": "web", "updated_files": {"stacks/web/template.yaml": TEMPLATE},
        "primary_path": "stacks/web/template.yaml", "cloudtrail_event": EVENTS[0],
        "remediation": {"summary": "s"}, "review_reasons": ["r"],
        "github_repo": "acme/infra", "retry_count": 0,
    }
    payload.update(overrides)
    return payload


def test_blocking_errors_keeps_errors_and_drops_warnings():
    assert validator.blocking_errors([ERROR, WARNING]) == [
        "E3002 line 6: Additional properties are not allowed ('Bogus' was unexpected)"
    ]
    assert validator.blocking_errors([WARNING]) == []


def test_blocking_errors_treats_linter_crash_output_as_blocking():
    assert validator.blocking_errors(["Traceback: boom\n"]) == ["Traceback: boom"]


def test_clean_template_goes_to_pr_creator_with_the_models_verdict(harness, lambda_client, table):
    harness()
    assert validator.lambda_handler(_payload(), None) == {"status": "passed"}

    (function, payload), = lambda_client.calls
    assert function == "pr-creator"
    assert payload["event_ids"] == ["e1", "e2"]
    assert payload["remediation"] == {"summary": "s"}
    assert payload["review_reasons"] == ["r"]
    assert table.updates == []


def test_warnings_alone_do_not_block(harness, lambda_client):
    harness(WARNING)
    assert validator.lambda_handler(_payload(), None) == {"status": "passed"}
    assert lambda_client.calls[0][0] == "pr-creator"


def test_lint_error_sends_the_stack_back_with_feedback(harness, lambda_client, table):
    harness(ERROR)
    result = validator.lambda_handler(_payload(retry_count=1), None)

    assert result == {"status": "retrying", "retry_count": 2}
    (function, payload), = lambda_client.calls
    assert function == "stack-processor"
    assert payload["tenant"] == TENANT
    assert payload["cloudtrail_events"] == EVENTS
    assert payload["retry_count"] == 2
    assert payload["lint_feedback"]["files"] == {"stacks/web/template.yaml": TEMPLATE}
    assert payload["lint_feedback"]["errors"]["stacks/web/template.yaml"][0].startswith("E3002 line 6")
    assert table.updates == []      # not failed yet — another attempt is running


def test_retries_exhausted_marks_events_failed_and_raises(harness, lambda_client, table):
    harness(ERROR)
    with pytest.raises(RuntimeError, match="after 3 retries"):
        validator.lambda_handler(_payload(retry_count=validator.MAX_RETRIES), None)

    assert lambda_client.calls == []
    assert table.statuses() == {"e1": rec.STATUS_FAILED, "e2": rec.STATUS_FAILED}
    assert table.attrs("e1")["error"].startswith("validator: Validation failed after 3 retries")


def test_direct_invocation_without_tenant_fails_instead_of_looping(harness, lambda_client, table):
    harness(ERROR)
    payload = _payload()
    del payload["tenant"], payload["cloudtrail_events"], payload["event_ids"]

    with pytest.raises(RuntimeError, match="cannot be retried"):
        validator.lambda_handler(payload, None)
    assert lambda_client.calls == []
    assert table.statuses() == {"e1": rec.STATUS_FAILED}


def test_non_template_files_are_not_linted(harness, lambda_client):
    harness(ERROR)      # would fail if the parameter file were linted
    payload = _payload(updated_files={"stacks/web/prod.json": '{"Size": "t3.xlarge"}'})
    assert validator.lambda_handler(payload, None) == {"status": "passed"}


# ── The other half of the loop: the Stack Processor's side ───────────────────

def test_retry_prompt_carries_the_rejected_output_and_its_errors():
    feedback = {"files": {"t.yaml": "bad"}, "errors": {"t.yaml": ["E3002 line 6: nope"]}}
    prompt   = sp.build_prompt({"t.yaml": "orig"}, EVENTS, feedback)

    assert "failed cfn-lint validation" in prompt
    payload = json.loads(prompt.split("INPUT:\n", 1)[1])
    assert payload["files"] == {"t.yaml": "orig"}
    assert payload["previous_attempt"] == {
        "files": {"t.yaml": "bad"}, "lint_errors": {"t.yaml": ["E3002 line 6: nope"]},
    }


def test_first_attempt_prompt_has_no_retry_section():
    prompt = sp.build_prompt({"t.yaml": "orig"}, EVENTS)
    assert "cfn-lint" not in prompt and "previous_attempt" not in prompt


def test_validator_and_stack_processor_payloads_fit_together(monkeypatch, harness, lambda_client):
    """What the Validator sends back is exactly what the Stack Processor reads."""
    harness(ERROR)
    validator.lambda_handler(_payload(), None)
    _, retry_event = lambda_client.calls.pop()

    seen = {}

    def fake_process(tenant, stack_name, events, retry_count=0, lint_feedback=None):
        seen.update(retry_count=retry_count, lint_feedback=lint_feedback, stack=stack_name)
    monkeypatch.setattr(sp, "process_stack", fake_process)
    sp.lambda_handler(retry_event, None)

    assert seen["retry_count"] == 1 and seen["stack"] == "web"
    assert set(seen["lint_feedback"]) == {"files", "errors"}


def test_retry_count_travels_back_to_the_validator(monkeypatch, lambda_client):
    monkeypatch.setattr(sp, "lambda_client", lambda_client)
    sp.invoke_validator(TENANT, "web", EVENTS, {"t.yaml": "x"}, "t.yaml", {}, [], retry_count=2)

    function, payload = lambda_client.calls[0]
    assert function == "validator"
    assert payload["retry_count"] == 2
    assert payload["tenant"] == TENANT and payload["cloudtrail_events"] == EVENTS
