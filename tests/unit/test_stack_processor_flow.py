import pytest
import reconciliation as rec
from conftest import load_handler

sp = load_handler("stack_processor")

TEMPLATE = "stacks/web/template.yaml"
PARAMS   = "stacks/web/prod.json"
FILES    = {TEMPLATE: "AWSTemplateFormatVersion: '2010-09-09'\n", PARAMS: '{"Size": "t3.large"}'}
TENANT   = {"tenant_id": "111111111111", "role_arn": "arn:role", "external_id": "x",
            "github_repo": "acme/infra", "github_token_secret_arn": "arn:secret"}
EVENTS   = [{"eventID": "e1", "awsRegion": "us-east-1"}, {"eventID": "e2"}]


@pytest.fixture
def pipeline(monkeypatch, table, lambda_client):
    """Stack Processor with AWS, GitHub and the model replaced by fakes."""
    class Dynamo:
        def Table(self, name):
            return table

    monkeypatch.setattr(sp, "dynamodb", Dynamo())
    monkeypatch.setattr(sp, "lambda_client", lambda_client)
    monkeypatch.setattr(sp, "assume_cross_account_role", lambda *a: {})
    monkeypatch.setattr(sp, "_get_secret", lambda arn: "token")
    monkeypatch.setattr(sp, "fetch_files", lambda *a: (dict(FILES), TEMPLATE))

    def set_model(updated_files, **remediation):
        monkeypatch.setattr(sp, "invoke_bedrock", lambda *a, **k: (updated_files, remediation))

    return set_model


def _run():
    return sp.lambda_handler(
        {"tenant": TENANT, "stack_name": "web", "cloudtrail_events": EVENTS}, None)


def test_confident_change_goes_to_validator(pipeline, table, lambda_client):
    pipeline({PARAMS: '{"Size": "t3.xlarge"}'}, confidence=0.9, summary="Resized")
    _run()

    (function, payload), = lambda_client.calls
    assert function == "validator"
    assert payload["updated_files"] == {PARAMS: '{"Size": "t3.xlarge"}'}
    assert payload["event_ids"] == ["e1", "e2"]
    assert payload["review_reasons"] == []
    assert payload["remediation"]["summary"] == "Resized"
    assert table.updates == []      # still "queued" until the PR is opened


def test_unchanged_files_open_no_pull_request(pipeline, table, lambda_client):
    # The model echoes a file back with only whitespace differences.
    pipeline({TEMPLATE: FILES[TEMPLATE] + "\n"}, summary="Evidence was insufficient",
             manual_review_required=True)
    assert _run() == {"status": rec.STATUS_NO_CHANGE}

    assert lambda_client.calls == []
    assert table.statuses() == {"e1": rec.STATUS_NO_CHANGE, "e2": rec.STATUS_NO_CHANGE}
    attrs = table.attrs("e1")
    assert attrs["reason"] == "Evidence was insufficient"
    assert attrs["manual_review_required"] is True


def test_empty_model_answer_is_no_change(pipeline, table, lambda_client):
    pipeline({})
    _run()
    assert lambda_client.calls == []
    assert table.attrs("e1")["reason"] == "The model proposed no change."


def test_failure_marks_every_event_failed_and_reraises(pipeline, table, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Bedrock failed after 4 attempts")
    monkeypatch.setattr(sp, "invoke_bedrock", boom)

    with pytest.raises(RuntimeError):
        _run()
    assert table.statuses() == {"e1": rec.STATUS_FAILED, "e2": rec.STATUS_FAILED}
    assert "Bedrock failed" in table.attrs("e1")["error"]


@pytest.mark.parametrize("remediation, expected", [
    ({"confidence": 0.9},                                   0),
    ({"confidence": 0.6},                                   0),   # at the threshold is fine
    ({"confidence": 0.59},                                  1),
    ({"confidence": 0.9, "manual_review_required": True},   1),
    ({"confidence": 0.2, "manual_review_required": True},   2),
    ({},                                                    1),   # no score reported
    ({"confidence": "high"},                                1),
    ({"confidence": True},                                  1),
])
def test_review_reasons(remediation, expected):
    assert len(sp.review_reasons(remediation)) == expected


def test_low_confidence_change_is_flagged_for_review(pipeline, lambda_client):
    pipeline({PARAMS: '{"Size": "t3.xlarge"}'}, confidence=0.4)
    _run()
    reasons = lambda_client.calls[0][1]["review_reasons"]
    assert len(reasons) == 1 and "0.40" in reasons[0] and "0.60" in reasons[0]


def test_retry_that_fixes_nothing_fails_instead_of_reporting_no_change(pipeline, table):
    pipeline({})
    with pytest.raises(RuntimeError, match="no corrected files"):
        sp.lambda_handler({
            "tenant": TENANT, "stack_name": "web", "cloudtrail_events": EVENTS,
            "retry_count": 1,
            "lint_feedback": {"files": {TEMPLATE: "bad"}, "errors": {TEMPLATE: ["E1"]}},
        }, None)
    assert table.statuses() == {"e1": rec.STATUS_FAILED, "e2": rec.STATUS_FAILED}
