"""When may a notification be delivered: notification mode, quiet hours and the user's time zone."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Europe/Berlin")
    except Exception:
        return ZoneInfo("Europe/Berlin")


def _hm(s: str | None) -> time | None:
    if not s:
        return None
    h, _, m = s.partition(":")
    return time(int(h) % 24, int(m or 0))


def in_quiet_hours(now: datetime, tz: str | None, start: str | None, end: str | None) -> datetime | None:
    """If ``now`` is inside the quiet window, return the instant when it ends (aware, UTC-comparable)."""
    qs, qe = _hm(start), _hm(end)
    if qs is None or qe is None or qs == qe:
        return None
    local = now.astimezone(_tz(tz))
    t = local.time()
    inside = (qs <= t or t < qe) if qs > qe else (qs <= t < qe)
    if not inside:
        return None
    end_day = local.date() if t < qe else local.date() + timedelta(days=1)
    return datetime.combine(end_day, qe, tzinfo=local.tzinfo)


def next_local_hour(now: datetime, tz: str | None, hour: int) -> datetime:
    local = now.astimezone(_tz(tz))
    target = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target


def due_at(now: datetime, mode: str, *, tz: str | None, quiet_start: str | None, quiet_end: str | None,
           morning_hour: int = 8, evening_hour: int = 19) -> datetime | None:
    """None → do not push (MANUAL)."""
    if mode == "MANUAL":
        return None
    if mode == "MORNING":
        return next_local_hour(now, tz, morning_hour)
    if mode == "EVENING":
        return next_local_hour(now, tz, evening_hour)
    return in_quiet_hours(now, tz, quiet_start, quiet_end) or now


def local_day_start(now: datetime, tz: str | None) -> datetime:
    local = now.astimezone(_tz(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0)
