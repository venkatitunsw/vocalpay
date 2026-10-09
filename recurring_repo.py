import calendar
from datetime import datetime, timezone, timedelta
from uuid import uuid4

from db import get_conn

_CADENCE_DAYS = {"weekly": 7, "fortnightly": 14}  # "monthly" is calendar math -- see _advance_monthly


def create_schedule(
    user_id: str,
    payment_rail: str,
    target_identifier: str,
    amount_cents: int,
    cadence: str,
    start_at: datetime | None = None,
) -> str:
    valid_cadences = sorted(_CADENCE_DAYS) + ["monthly"]
    if cadence not in valid_cadences:
        raise ValueError(f"Unknown cadence '{cadence}' -- expected one of {valid_cadences}")
    if payment_rail not in ("payid", "bpay"):
        raise ValueError(f"Unknown payment_rail '{payment_rail}' -- expected 'payid' or 'bpay'")

    schedule_id = str(uuid4())
    now = datetime.now(timezone.utc)
    next_run_at = (start_at or now).isoformat()

    conn = get_conn()
    try:
        conn.execute(
            """
            INSERT INTO recurring_schedules
            (schedule_id, user_id, payment_rail, target_identifier, amount_cents, cadence, next_run_at, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """,
            (schedule_id, user_id, payment_rail, target_identifier, amount_cents, cadence, next_run_at, now.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()
    return schedule_id


def list_schedules(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM recurring_schedules WHERE user_id=? ORDER BY created_at", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_schedule_status(schedule_id: str, status: str) -> None:
    if status not in ("active", "paused", "cancelled"):
        raise ValueError(f"Unknown status '{status}'")
    conn = get_conn()
    try:
        conn.execute("UPDATE recurring_schedules SET status=? WHERE schedule_id=?", (status, schedule_id))
        conn.commit()
    finally:
        conn.close()


def due_schedules(as_of: datetime | None = None) -> list[dict]:
    as_of_iso = (as_of or datetime.now(timezone.utc)).isoformat()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM recurring_schedules WHERE status='active' AND next_run_at<=?", (as_of_iso,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _advance_monthly(from_time: datetime) -> datetime:
    """A calendar month forward, clamped to the target month's last day (e.g.
    Jan 31 -> Feb 28/29) -- a flat +30 days drifts earlier every cycle and
    eventually crosses the day it started on."""
    year, month, day = from_time.year, from_time.month, from_time.day
    month += 1
    if month > 12:
        month, year = 1, year + 1
    last_day = calendar.monthrange(year, month)[1]
    return from_time.replace(year=year, month=month, day=min(day, last_day))


def advance_schedule(schedule_id: str, cadence: str, from_time: datetime | None = None) -> str:
    """Pushes next_run_at forward by one cadence period from `from_time`
    (defaulting to now) -- called once a due schedule's payment has actually
    been created, so a schedule is never silently skipped or double-fired."""
    base = from_time or datetime.now(timezone.utc)
    if cadence == "monthly":
        next_run_at = _advance_monthly(base).isoformat()
    elif cadence in _CADENCE_DAYS:
        next_run_at = (base + timedelta(days=_CADENCE_DAYS[cadence])).isoformat()
    else:
        raise ValueError(f"Unknown cadence '{cadence}' -- expected one of {sorted(_CADENCE_DAYS) + ['monthly']}")
    conn = get_conn()
    try:
        conn.execute("UPDATE recurring_schedules SET next_run_at=? WHERE schedule_id=?", (next_run_at, schedule_id))
        conn.commit()
    finally:
        conn.close()
    return next_run_at
