import re
from datetime import datetime, timedelta, timezone

from intent_parser import parse_text_command

# Recognizes a recurrence phrase tacked onto an otherwise ordinary payment
# command -- "Pay Sam 20 every Friday", "Schedule 50 to my electricity
# account on the 1st of the month" -- by stripping the recurrence phrase out
# and handing what's left to the already-tested intent_parser.parse_text_command,
# rather than re-implementing amount/payee parsing here.

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}

RECURRENCE_RE = re.compile(
    r"""
    \b(?:every|each)\s+(?P<weekday>monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b
    |
    \b(?:every|each)\s+(?P<interval>week|fortnight|two\s+weeks|month)\b
    |
    \bweekly\b(?P<weekly_bare>)
    |
    \bfortnightly\b(?P<fortnightly_bare>)
    |
    \bmonthly\b(?P<monthly_bare>)
    |
    \bon\s+the\s+(?P<day_of_month>\d{1,2})(?:st|nd|rd|th)?\s*(?:of\s+(?:every|each)\s+month|of\s+the\s+month)?\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


class RecurrenceParseResult:
    def __init__(self, ok, intent=None, cadence=None, next_run_at=None, error=""):
        self.ok = ok
        self.intent = intent
        self.cadence = cadence  # "weekly" | "fortnightly" | "monthly"
        self.next_run_at = next_run_at  # datetime, UTC
        self.error = error


def is_recurring_command(text: str) -> bool:
    return bool(RECURRENCE_RE.search(text or ""))


def _next_weekday(from_dt: datetime, target_weekday: int) -> datetime:
    days_ahead = (target_weekday - from_dt.weekday()) % 7
    days_ahead = days_ahead or 7  # today's weekday -> next week's occurrence, not today
    return from_dt + timedelta(days=days_ahead)


def _next_month_same_day(from_dt: datetime) -> datetime:
    """Calendar-correct 'one month from now', clamped to the target month's
    last day (e.g. Jan 31 -> Feb 28/29) instead of a flat +30 days, which
    drifts earlier every single cycle."""
    year, month, day = from_dt.year, from_dt.month, from_dt.day
    month += 1
    if month > 12:
        month = 1
        year += 1
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    return from_dt.replace(year=year, month=month, day=min(day, last_day))


def _next_day_of_month(from_dt: datetime, day: int) -> datetime:
    import calendar
    year, month = from_dt.year, from_dt.month
    last_day = calendar.monthrange(year, month)[1]
    candidate = from_dt.replace(day=min(day, last_day), hour=0, minute=0, second=0, microsecond=0)
    if candidate <= from_dt:
        month += 1
        if month > 12:
            month, year = 1, year + 1
        last_day = calendar.monthrange(year, month)[1]
        candidate = from_dt.replace(year=year, month=month, day=min(day, last_day), hour=0, minute=0, second=0, microsecond=0)
    return candidate


def parse_recurring_command(text: str, now: datetime | None = None) -> RecurrenceParseResult:
    """
    Strips the recurrence phrase out, parses the remainder as an ordinary
    payment command (amount + payee/biller), and computes a precise
    next_run_at for it. Doesn't resolve the payee/biller against the DB
    itself -- that's main.py's job, same division of labor as bpay_parser.
    """
    m = RECURRENCE_RE.search(text or "")
    if not m:
        return RecurrenceParseResult(False, error="Not a recurring payment command")

    now = now or datetime.now(timezone.utc)
    stripped = (text[: m.start()] + " " + text[m.end():]).strip()
    stripped = re.sub(r"\s{2,}", " ", stripped)

    # parse_text_command only recognizes "pay"/"send"/"transfer" as the
    # leading verb -- "Schedule 50 to Sam" (a very natural way to phrase a
    # recurring payment) otherwise fails to parse at all. Normalize just
    # that leading word here, local to recurring commands, rather than
    # widen the shared one-off-payment parser's verb list.
    stripped = re.sub(r"^\s*schedule\b", "Pay", stripped, count=1, flags=re.IGNORECASE)

    result = parse_text_command(stripped)
    if not result.ok:
        return RecurrenceParseResult(False, error=result.error)

    if m.group("weekday"):
        cadence = "weekly"
        next_run_at = _next_weekday(now, _WEEKDAYS[m.group("weekday").lower()])
    elif m.group("interval"):
        interval = m.group("interval").lower()
        if interval == "week":
            cadence, next_run_at = "weekly", now + timedelta(days=7)
        elif interval in ("fortnight", "two weeks"):
            cadence, next_run_at = "fortnightly", now + timedelta(days=14)
        else:  # "month"
            cadence, next_run_at = "monthly", _next_month_same_day(now)
    elif m.group("weekly_bare") is not None:
        cadence, next_run_at = "weekly", now + timedelta(days=7)
    elif m.group("fortnightly_bare") is not None:
        cadence, next_run_at = "fortnightly", now + timedelta(days=14)
    elif m.group("monthly_bare") is not None:
        cadence, next_run_at = "monthly", _next_month_same_day(now)
    elif m.group("day_of_month"):
        day = int(m.group("day_of_month"))
        if not (1 <= day <= 31):
            return RecurrenceParseResult(False, error="Day of month must be between 1 and 31")
        cadence = "monthly"
        next_run_at = _next_day_of_month(now, day)
    else:
        return RecurrenceParseResult(False, error="Could not understand the recurrence")

    return RecurrenceParseResult(True, intent=result.intent, cadence=cadence, next_run_at=next_run_at)
