from decimal import Decimal

import pytest
import reconciliation as rec
from conftest import load_handler

pr = load_handler("pr_creator")

EVENT = {
    "eventName":    "ModifyInstanceAttribute",
    "eventTime":    "2026-01-10T07:00:00Z",
    "userIdentity": {"arn": "arn:aws:iam::111111111111:user/alice"},
}
FILES = {"stacks/web/prod.json": "{}", "stacks/web/template.yaml": "x"}
REMEDIATION = {
    "summary":          "Instance resized to t3.xlarge.",
    "root_cause":       "Manual resize in the console.",
    "confidence":       0.82,
    "proposed_changes": ["Set Size to t3.xlarge", {"file": "prod.json"}],
    "warnings":         ["Check the other environments."],
}


def test_confident_pr_has_plain_title_and_the_models_explanation():
    title = pr.build_pr_title(EVENT, "web", [])
    body  = pr.build_pr_description(EVENT, "web", FILES, REMEDIATION, [], event_count=3)

    assert title == "[Drift] ModifyInstanceAttribute detected on web"
    assert "[!WARNING]" not in body
    assert "| Model confidence | 82% |" in body
    assert "(+2 more)" in body
    assert "### What changed\n\nInstance resized to t3.xlarge." in body
    assert "### Root cause\n\nManual resize in the console." in body
    assert "- Set Size to t3.xlarge" in body
    assert '- {"file": "prod.json"}' in body
    assert "### Warnings\n\n- Check the other environments." in body
    assert "- `stacks/web/prod.json`\n- `stacks/web/template.yaml`" in body


def test_flagged_pr_is_marked_in_title_and_opens_with_a_banner():
    reasons = ["The model flagged this change for manual review."]
    title = pr.build_pr_title(EVENT, "web", reasons)
    body  = pr.build_pr_description(EVENT, "web", FILES, REMEDIATION, reasons)

    assert title.startswith("[Drift – needs review] ")
    assert body.startswith("> [!WARNING]\n> **This change needs careful review")
    assert "> - The model flagged this change for manual review." in body


def test_description_survives_missing_or_odd_remediation():
    body = pr.build_pr_description({}, "web", FILES)
    assert "## Drift Detected" in body and "###" not in body

    odd = {"summary": 5, "proposed_changes": "not a list", "confidence": "high"}
    body = pr.build_pr_description(EVENT, "web", FILES, odd)
    assert "###" not in body and "Model confidence" not in body


@pytest.fixture
def creator(monkeypatch, table):
    class Dynamo:
        def Table(self, name):
            return table

    monkeypatch.setattr(pr, "dynamodb", Dynamo())
    monkeypatch.setattr(pr, "fetch_from_s3", lambda key: "content")
    monkeypatch.setattr(pr, "get_github_token", lambda arn: "token")
    return {
        "tenant_id": "t1", "event_id": "e1-abcdef", "event_ids": ["e1-abcdef", "e2"],
        "stack_name": "web", "github_repo": "acme/infra", "cloudtrail_event": EVENT,
        "updated_s3_keys": {"stacks/web/prod.json": "validated/t1/e1/prod.json"},
        "remediation": REMEDIATION, "review_reasons": [],
    }


def test_every_event_in_the_group_is_marked_pr_opened(creator, table, monkeypatch):
    seen = {}

    def fake_open(token, repo, branch, files, title, description, commit_message):
        seen.update(branch=branch, title=title, message=commit_message)
        return "https://github.test/acme/infra/pull/7"
    monkeypatch.setattr(pr, "open_pull_request", fake_open)

    assert pr.lambda_handler(creator, None) == {"pr_url": "https://github.test/acme/infra/pull/7"}
    assert seen["branch"] == "drift/t1/e1-abcde"
    assert seen["message"] == "fix(drift): update prod.json after ModifyInstanceAttribute"
    assert table.statuses() == {"e1-abcdef": rec.STATUS_PR_OPENED, "e2": rec.STATUS_PR_OPENED}
    attrs = table.attrs("e2")
    assert attrs["pr_url"].endswith("/pull/7")
    assert attrs["needs_review"] is False
    assert attrs["confidence"] == Decimal("0.82")


def test_payload_without_event_ids_falls_back_to_the_single_event(creator, table, monkeypatch):
    monkeypatch.setattr(pr, "open_pull_request", lambda *a, **k: "url")
    del creator["event_ids"]
    pr.lambda_handler(creator, None)
    assert table.statuses() == {"e1-abcdef": rec.STATUS_PR_OPENED}


def test_github_failure_marks_events_failed_and_reraises(creator, table, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("GitHub said no")
    monkeypatch.setattr(pr, "open_pull_request", boom)

    with pytest.raises(RuntimeError):
        pr.lambda_handler(creator, None)
    assert table.statuses() == {"e1-abcdef": rec.STATUS_FAILED, "e2": rec.STATUS_FAILED}
    assert table.attrs("e2")["error"] == "pr_creator: GitHub said no"
