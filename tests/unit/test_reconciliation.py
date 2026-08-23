from datetime import datetime, timedelta, timezone
from decimal import Decimal

import reconciliation as rec

NOW = datetime(2026, 1, 10, 7, 0, tzinfo=timezone.utc)


def _queued(minutes_ago, attempts=1):
    return {
        "status":    rec.STATUS_QUEUED,
        "queued_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "attempts":  Decimal(attempts),
    }


def test_unseen_event_is_processed():
    assert rec.should_process(None, NOW) == (True, 0)


def test_finished_events_are_never_reprocessed():
    for status in (rec.STATUS_PR_OPENED, rec.STATUS_NO_CHANGE):
        assert rec.should_process({"status": status, "attempts": 1}, NOW)[0] is False


def test_recently_queued_event_is_in_flight():
    assert rec.should_process(_queued(minutes_ago=5), NOW) == (False, 1)


def test_stuck_queued_event_is_retried():
    stale = rec.STALE_QUEUED_MINUTES + 1
    assert rec.should_process(_queued(minutes_ago=stale), NOW) == (True, 1)


def test_failed_event_is_retried_until_attempts_run_out():
    assert rec.should_process({"status": rec.STATUS_FAILED, "attempts": 2}, NOW) == (True, 2)
    assert rec.should_process(
        {"status": rec.STATUS_FAILED, "attempts": rec.MAX_ATTEMPTS}, NOW
    ) == (False, rec.MAX_ATTEMPTS)


def test_stuck_event_out_of_attempts_is_dropped():
    record = _queued(minutes_ago=10_000, attempts=rec.MAX_ATTEMPTS)
    assert rec.should_process(record, NOW)[0] is False


def test_legacy_record_without_queued_at_counts_as_stale():
    assert rec.should_process({"status": rec.STATUS_QUEUED}, NOW) == (True, 1)


def test_records_outlive_the_lookback_window():
    ttl = rec.expires_at(NOW) - int(NOW.timestamp())
    assert ttl == rec.RECORD_TTL_DAYS * 86400


def test_mark_events_updates_every_event(table):
    rec.mark_events(table, "t1", ["e1", "e2"], rec.STATUS_PR_OPENED,
                    pr_url="https://example.test/pr/1", reason=None, confidence=0.8)

    assert table.statuses() == {"e1": rec.STATUS_PR_OPENED, "e2": rec.STATUS_PR_OPENED}
    attrs = table.attrs("e2")
    assert attrs["pr_url"] == "https://example.test/pr/1"
    assert attrs["confidence"] == Decimal("0.8")
    assert "reason" not in attrs            # None values are skipped
    assert "expires_at" in table.updates[0]["UpdateExpression"]


def test_mark_events_trims_long_text(table):
    rec.mark_events(table, "t1", ["e1"], rec.STATUS_FAILED, error="x" * 10_000)
    assert len(table.attrs("e1")["error"]) == 2000


def test_gave_up_only_when_attempts_are_exhausted():
    exhausted = {"status": rec.STATUS_FAILED, "attempts": rec.MAX_ATTEMPTS}
    assert rec.gave_up(exhausted, NOW) is True
    assert rec.gave_up(_queued(minutes_ago=10_000, attempts=rec.MAX_ATTEMPTS), NOW) is True
    assert rec.gave_up({"status": rec.STATUS_FAILED, "attempts": 1}, NOW) is False
    assert rec.gave_up({"status": rec.STATUS_PR_OPENED, "attempts": rec.MAX_ATTEMPTS}, NOW) is False
    assert rec.gave_up(_queued(minutes_ago=5, attempts=rec.MAX_ATTEMPTS), NOW) is False
    assert rec.gave_up(None, NOW) is False
