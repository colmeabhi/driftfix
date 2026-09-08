import json

import pytest
from conftest import load_handler

sp = load_handler("stack_processor")

FILES = {
    "stacks/web/template.yaml": "AWSTemplateFormatVersion: '2010-09-09'\nResources: {}\n",
    "stacks/web/prod.json":     '{"InstanceTypeParam": "t3.large"}',
}
EVENTS = [{"eventID": "e1", "eventName": "ModifyInstanceAttribute", "awsRegion": "eu-west-1"}]


def _reply(updated_files, **remediation):
    return json.dumps({"updated_files": updated_files, "remediation": remediation})


class FakeBedrock:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def converse(self, modelId, messages, inferenceConfig):
        self.prompts.append(messages[0]["content"][0]["text"])
        return {"output": {"message": {"content": [{"text": self.replies.pop(0)}]}}}


def test_prompt_contains_every_file_not_just_the_template():
    prompt  = sp.build_prompt(FILES, EVENTS)
    payload = json.loads(prompt.split("INPUT:\n", 1)[1])
    assert payload["files"] == FILES
    assert payload["evidence"]["region"] == "eu-west-1"
    assert payload["evidence"]["events"] == EVENTS


def test_parameter_file_change_is_returned_on_its_own():
    new_params = '{"InstanceTypeParam": "t3.xlarge"}'
    updated, remediation = sp.parse_model_response(
        _reply({"stacks/web/prod.json": new_params}, confidence=0.9), FILES)
    assert updated == {"stacks/web/prod.json": new_params}
    assert remediation == {"confidence": 0.9}


def test_reply_wrapped_in_markdown_fence_is_parsed():
    text = "```json\n" + _reply({"stacks/web/prod.json": "{}"}) + "\n```"
    updated, _ = sp.parse_model_response(text, FILES)
    assert list(updated) == ["stacks/web/prod.json"]


def test_invented_paths_and_empty_contents_are_dropped():
    updated, _ = sp.parse_model_response(
        _reply({"made/up.yaml": "x", "stacks/web/prod.json": "  "}), FILES)
    assert updated == {}


def test_insufficient_evidence_returns_no_files():
    updated, remediation = sp.parse_model_response(
        _reply({}, manual_review_required=True), FILES)
    assert updated == {}
    assert remediation["manual_review_required"] is True


@pytest.mark.parametrize("text", [
    "not json at all",
    '{"remediation": {}}',
    '{"updated_files": "a string"}',
    "[1, 2]",
])
def test_malformed_reply_raises(text):
    with pytest.raises(ValueError):
        sp.parse_model_response(text, FILES)


def test_missing_remediation_becomes_empty_dict():
    _, remediation = sp.parse_model_response('{"updated_files": {}}', FILES)
    assert remediation == {}


def test_malformed_reply_is_retried(monkeypatch):
    good = _reply({"stacks/web/prod.json": "{}"}, confidence=0.7)
    fake = FakeBedrock("sorry, I cannot", good)
    monkeypatch.setattr(sp, "bedrock_runtime", fake)

    updated, remediation = sp.invoke_bedrock(FILES, EVENTS)
    assert len(fake.prompts) == 2
    assert updated == {"stacks/web/prod.json": "{}"}
    assert remediation["confidence"] == 0.7


def test_gives_up_after_repeated_malformed_replies(monkeypatch):
    monkeypatch.setattr(sp, "bedrock_runtime", FakeBedrock(*["nope"] * 4))
    with pytest.raises(RuntimeError, match="after 4 attempts"):
        sp.invoke_bedrock(FILES, EVENTS)
