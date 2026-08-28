from datetime import datetime, timedelta, timezone

import reconciliation as rec
from conftest import load_handler

processor = load_handler("processor")
TABLE = "reconciliations"


class FakeDynamo:
    """Serves batch_get_item from a dict and captures batch_writer puts."""

    def __init__(self, records=None, split=False):
        self.records, self.split, self.puts = records or {}, split, []

    def batch_get_item(self, RequestItems):
        keys = RequestItems[TABLE]["Keys"]
        # With split=True only the first key is answered per call, to exercise
        # the UnprocessedKeys loop.
        now, later = (keys[:1], keys[1:]) if self.split else (keys, [])
        found = [dict(self.records[k["event_id"]], event_id=k["event_id"])
                 for k in now if k["event_id"] in self.records]
        resp = {"Responses": {TABLE: found}}
        if later:
            resp["UnprocessedKeys"] = {TABLE: dict(RequestItems[TABLE], Keys=later)}
        return resp

    def Table(self, name):
        return self

    def batch_writer(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def put_item(self, Item):
        self.puts.append(Item)


def _events(*ids):
    return [{"eventID": i} for i in ids]


def _old():
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def test_new_events_pass_and_finished_events_are_skipped(monkeypatch):
    monkeypatch.setattr(processor, "dynamodb", FakeDynamo({
        "done": {"status": rec.STATUS_PR_OPENED, "attempts": 1},
    }))
    events, attempts = processor.filter_already_processed("t1", _events("new", "done"))
    assert [e["eventID"] for e in events] == ["new"]
    assert attempts == {"new": 0}


def test_failed_and_stuck_events_are_retried(monkeypatch):
    monkeypatch.setattr(processor, "dynamodb", FakeDynamo({
        "failed":    {"status": rec.STATUS_FAILED, "attempts": 1},
        "stuck":     {"status": rec.STATUS_QUEUED, "attempts": 2, "queued_at": _old()},
        "exhausted": {"status": rec.STATUS_FAILED, "attempts": rec.MAX_ATTEMPTS},
        "in_flight": {"status": rec.STATUS_QUEUED, "attempts": 1,
                      "queued_at": datetime.now(timezone.utc).isoformat()},
    }))
    events, attempts = processor.filter_already_processed(
        "t1", _events("failed", "stuck", "exhausted", "in_flight"))
    assert attempts == {"failed": 1, "stuck": 2}
    assert [e["eventID"] for e in events] == ["failed", "stuck"]


def test_duplicate_and_malformed_events_are_dropped(monkeypatch):
    monkeypatch.setattr(processor, "dynamodb", FakeDynamo())
    events, _ = processor.filter_already_processed(
        "t1", _events("a", "a") + [{"eventID": None}, {}])
    assert [e["eventID"] for e in events] == ["a"]


def test_unprocessed_keys_are_followed(monkeypatch):
    monkeypatch.setattr(processor, "dynamodb", FakeDynamo(
        {"b": {"status": rec.STATUS_NO_CHANGE, "attempts": 1}}, split=True))
    events, _ = processor.filter_already_processed("t1", _events("a", "b"))
    assert [e["eventID"] for e in events] == ["a"]


def test_queued_marker_counts_the_attempt(monkeypatch):
    fake = FakeDynamo()
    monkeypatch.setattr(processor, "dynamodb", fake)
    processor.mark_events_queued("t1", _events("new", "retry"), {"new": 0, "retry": 2})

    by_id = {p["event_id"]: p for p in fake.puts}
    assert by_id["new"]["attempts"] == 1
    assert by_id["retry"]["attempts"] == 3
    assert all(p["status"] == rec.STATUS_QUEUED for p in fake.puts)
    # The record must outlive the lookback window or the event looks new again.
    lifetime_h = (by_id["new"]["expires_at"] - datetime.now(timezone.utc).timestamp()) / 3600
    assert lifetime_h > processor.LOOKBACK_HOURS
