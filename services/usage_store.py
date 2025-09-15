# services/usage_store.py
from __future__ import annotations

from datetime import date
from typing import Optional

from sqlalchemy import text

from app import db
from models import User


# ------------------------- period key helpers -------------------------

def _day_key(d: date) -> str:
    return d.isoformat()  # 'YYYY-MM-DD'

def _week_key(monday: date) -> str:
    # store the monday date for the week, e.g. '2025-09-08'
    return monday.isoformat()


# ------------------------- counter primitives -------------------------

def _get_counter(user_id: int, metric: str, period_key: str) -> int:
    row = db.session.execute(
        text("""
            SELECT count
              FROM usage_counters
             WHERE user_id = :uid AND metric = :metric AND period_key = :p
        """),
        {"uid": user_id, "metric": metric, "p": period_key},
    ).fetchone()
    return int(row[0]) if row else 0


def _inc_counter(user_id: int, metric: str, period_key: str, by: int = 1) -> None:
    # Try UPDATE, then INSERT if no row updated (simple upsert)
    res = db.session.execute(
        text("""
            UPDATE usage_counters
               SET count = count + :by
             WHERE user_id = :uid AND metric = :metric AND period_key = :p
        """),
        {"by": by, "uid": user_id, "metric": metric, "p": period_key},
    )
    if res.rowcount == 0:
        db.session.execute(
            text("""
                INSERT INTO usage_counters (user_id, metric, period_key, count)
                VALUES (:uid, :metric, :p, :c)
            """),
            {"uid": user_id, "metric": metric, "p": period_key, "c": by},
        )
    db.session.commit()


def _set_counter(user_id: int, metric: str, period_key: str, to_value: int) -> None:
    # Force a value (used for one-off flags like weekly free)
    res = db.session.execute(
        text("""
            UPDATE usage_counters
               SET count = :val
             WHERE user_id = :uid AND metric = :metric AND period_key = :p
        """),
        {"val": to_value, "uid": user_id, "metric": metric, "p": period_key},
    )
    if res.rowcount == 0:
        db.session.execute(
            text("""
                INSERT INTO usage_counters (user_id, metric, period_key, count)
                VALUES (:uid, :metric, :p, :c)
            """),
            {"uid": user_id, "metric": metric, "p": period_key, "c": to_value},
        )
    db.session.commit()


# ------------------------- public: daily mass offers -------------------------

def get_today_mass_offer_count(user_id: int, today: date) -> int:
    return _get_counter(user_id, metric="mass_offer_day", period_key=_day_key(today))


def increment_today_mass_offer_count(user_id: int, today: date) -> None:
    _inc_counter(user_id, metric="mass_offer_day", period_key=_day_key(today), by=1)


# ------------------------- public: weekly free (free tier) -------------------

def get_weekly_free_used(user_id: int, monday_date: date) -> bool:
    # count >= 1 means the weekly free has been used
    used = _get_counter(user_id, metric="mass_offer_weekfree", period_key=_week_key(monday_date))
    return used >= 1


def mark_weekly_free_used(user_id: int, monday_date: date) -> None:
    _set_counter(user_id, metric="mass_offer_weekfree", period_key=_week_key(monday_date), to_value=1)


# ------------------------- public: bonus mass offers -------------------------

def get_bonus_balance(user_id: int) -> int:
    row = db.session.execute(
        text("SELECT bonus_mass_offers FROM users WHERE id = :uid"),
        {"uid": user_id},
    ).fetchone()
    return int(row[0] or 0) if row else 0


def use_one_bonus(user_id: int) -> int:
    # Decrement but never below zero; return remaining
    # Use two statements to keep it simple/portable on shared MySQL
    current = get_bonus_balance(user_id)
    remaining = max(0, current - 1)
    db.session.execute(
        text("UPDATE users SET bonus_mass_offers = :rem WHERE id = :uid"),
        {"rem": remaining, "uid": user_id},
    )
    db.session.commit()
    return remaining


# ------------------------- OPTIONAL: lineup/week counters --------------------

def get_lineups_this_week(user_id: int, monday_date: date) -> int:
    return _get_counter(user_id, metric="lineup_week", period_key=_week_key(monday_date))

def increment_lineups_this_week(user_id: int, monday_date: date) -> None:
    _inc_counter(user_id, metric="lineup_week", period_key=_week_key(monday_date), by=1)
