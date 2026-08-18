from conftest import load_handler

processor = load_handler("processor")

CFN = "cloudformation.amazonaws.com"


def _event(**overrides):
    event = {
        "eventID":         "e1",
        "eventName":       "ModifyInstanceAttribute",
        "eventSource":     "ec2.amazonaws.com",
        "sourceIPAddress": "203.0.113.7",
        "userIdentity":    {"type": "IAMUser", "arn": "arn:aws:iam::111111111111:user/alice"},
    }
    event.update(overrides)
    return event


def test_manual_console_change_is_drift():
    assert processor.is_write_event(_event()) is True


def test_read_only_calls_are_ignored():
    assert processor.is_write_event(_event(readOnly=True)) is False
    assert processor.is_write_event(_event(eventName="DescribeInstances")) is False


def test_failed_calls_are_ignored():
    assert processor.is_write_event(_event(errorCode="Client.UnauthorizedOperation")) is False


def test_calls_to_cloudformation_itself_are_deploys_not_drift():
    event = _event(eventName="UpdateStack", eventSource=CFN,
                   requestParameters={"stackName": "web"})
    assert processor.is_write_event(event) is False


def test_calls_made_by_cloudformation_during_a_deploy_are_ignored():
    by_identity = _event(userIdentity={"type": "AssumedRole", "invokedBy": CFN})
    by_source   = _event(sourceIPAddress=CFN)
    assert processor.is_write_event(by_identity) is False
    assert processor.is_write_event(by_source) is False


def test_missing_user_identity_does_not_crash():
    assert processor.is_write_event(_event(userIdentity=None)) is True
