"""
Reconciliation record lifecycle — shared by every pipeline Lambda.

One record per (tenant_id, event_id) in the reconciliations table:

    queued ──▶ pr_opened    done: a pull request carries the fix
       │  ──▶ no_change     done: the model found nothing to change
       └──▶ failed          retried by the Orchestrator on a later batch run,
                            up to MAX_ATTEMPTS

A record still "queued" after STALE_QUEUED_MINUTES means the pipeline died
without reporting back; the Orchestrator treats it like "failed".

This module is copied into every Lambda zip by scripts/package_lambdas.sh.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

STATUS_QUEUED    = "queued"
STATUS_PR_OPENED = "pr_opened"
STATUS_NO_CHANGE = "no_change"
STATUS_FAILED    = "failed"

MAX_ATTEMPTS         = 3
STALE_QUEUED_MINUTES = 120

# Records must outlive the Orchestrator's lookback window, otherwise a finished
# event would be seen as new again. 30 days also keeps a usable audit trail.
RECORD_TTL_DAYS = 30

# DynamoDB string attributes are capped by the 400 KB item limit; error text and
# model summaries are trimmed well below that.
_MAX_TEXT = 2000


def expires_at(now=None):
    now = now or datetime.now(timezone.utc)
    return int((now + timedelta(days=RECORD_TTL_DAYS)).timestamp())


def should_process(record, now=None):
    """
    Decide whether the Orchestrator should (re)process an event.

    record is the existing reconciliation item, or None if the event has never
    been seen. Returns (process, attempts_so_far).
    """
    if record is None:
        return True, 0

    attempts = int(record.get("attempts", 1))
    status   = record.get("status")

    if status == STATUS_QUEUED and not _is_stale(record, now):
        return False, attempts          # in flight right now
    if status in (STATUS_QUEUED, STATUS_FAILED):
        return attempts < MAX_ATTEMPTS, attempts
    return False, attempts              # finished, or an unknown status


def gave_up(record, now=None):
    """True when an event failed or got stuck and has no attempts left."""
    if record is None or int(record.get("attempts", 1)) < MAX_ATTEMPTS:
        return False
    status = record.get("status")
    return status == STATUS_FAILED or (status == STATUS_QUEUED and _is_stale(record, now))


def _is_stale(record, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        queued_at = datetime.fromisoformat(record["queued_at"])
    except (KeyError, TypeError, ValueError):
        return True
    return now - queued_at > timedelta(minutes=STALE_QUEUED_MINUTES)


def mark_events(table, tenant_id, event_ids, status, **attrs):
    """
    Set status (plus any extra attributes) on every event in event_ids.
    None-valued attrs are skipped; long strings are trimmed.
    """
    now    = datetime.now(timezone.utc)
    names  = {"#s": "status"}
    values = {":s": status, ":u": now.isoformat(), ":e": expires_at(now)}
    sets   = ["#s = :s", "updated_at = :u", "expires_at = :e"]

    for i, (key, value) in enumerate(attrs.items()):
        if value is None:
            continue
        if isinstance(value, str):
            value = value[:_MAX_TEXT]
        elif isinstance(value, float):
            value = Decimal(str(value))     # boto3 rejects Python floats
        names[f"#a{i}"]  = key
        values[f":a{i}"] = value
        sets.append(f"#a{i} = :a{i}")

    for event_id in event_ids:
        table.update_item(
            Key={"tenant_id": tenant_id, "event_id": event_id},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
