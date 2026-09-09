"""Shared current-NFL-week resolution for MFL-facing features."""
from datetime import date, datetime, timedelta

from flask import current_app


WEEK_ONE_OPENERS = {2026: date(2026, 9, 9)}


def current_nfl_week(year: int) -> int:
    """Resolve a configured or date-derived week, bounded to the MFL season."""
    configured = current_app.config.get("MFL_CURRENT_WEEK")
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = None
    if configured and 1 <= configured <= 22:
        return configured

    try:
        week = max(1, int(current_app.config.get("MFL_WEEK_FALLBACK", 1)))
    except (TypeError, ValueError):
        week = 1

    opener = WEEK_ONE_OPENERS.get(year)
    today = datetime.now().date()
    if opener and today >= opener:
        rollover = opener
        while rollover.weekday() != 1:  # MFL lineup weeks roll Tuesday.
            rollover += timedelta(days=1)
        date_week = 1 if today < rollover else 2 + (today - rollover).days // 7
        week = max(week, date_week)

    minimum = current_app.config.get("MFL_MIN_CURRENT_WEEK")
    if isinstance(minimum, int) and 1 <= minimum <= 22:
        week = max(week, minimum)

    try:
        maximum = int(current_app.config.get("MFL_MAX_WEEKS", 18))
    except (TypeError, ValueError):
        maximum = 18
    return max(1, min(week, max(1, maximum)))
