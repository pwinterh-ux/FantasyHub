# services/store.py
from __future__ import annotations

from datetime import date
from typing import Optional
from sqlalchemy import text
from app import db


def _dstr(d: date) -> str:
    # MySQL DATE literal
    return d.strftime("%Y-%m-%d")


# -----------------------------------------------------------------------------
# Daily mass-offer counters (paid tiers)
# Table: mass_offer_daily_counters(user_id INT, on_date DATE, count INT, PRIMARY KEY(user_id,on_date))
# -----------------------------------------------------------------------------

def get_today_count(user_id: int, d: Optional[date] = None) -> int:
    d = d or date.today()
    row = db.session.execute(
        text("""
            SELECT count
              FROM mass_offer_daily_counters
             WHERE user_id = :uid AND on_date = :d
             LIMIT 1
        """),
        {"uid": user_id, "d": _dstr(d)},
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def increment_today_count(user_id: int, d: Optional[date] = None) -> None:
    d = d or date.today()
    db.session.execute(
        text("""
            INSERT INTO mass_offer_daily_counters (user_id, on_date, count)
            VALUES (:uid, :d, 1)
            ON DUPLICATE KEY UPDATE count = count + 1
        """),
        {"uid": user_id, "d": _dstr(d)},
    )
    db.session.commit()


# -----------------------------------------------------------------------------
# Bonus sends (users.bonus_mass_offers)
# -----------------------------------------------------------------------------

def get_bonus_balance(user_id: int) -> int:
    row = db.session.execute(
        text("SELECT COALESCE(bonus_mass_offers, 0) FROM users WHERE id = :uid LIMIT 1"),
        {"uid": user_id},
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def use_one_bonus(user_id: int) -> int:
    db.session.execute(
        text("""
            UPDATE users
               SET bonus_mass_offers = GREATEST(COALESCE(bonus_mass_offers, 0) - 1, 0)
             WHERE id = :uid
        """),
        {"uid": user_id},
    )
    db.session.commit()
    # return remaining
    return get_bonus_balance(user_id)


# -----------------------------------------------------------------------------
# Weekly free (Free plan only)
# Table: weekly_free_mass_offers(user_id INT, week_monday DATE, used TINYINT(1),
#                                PRIMARY KEY(user_id, week_monday))
# -----------------------------------------------------------------------------

def get_weekly_free_used(user_id: int, week_monday: date) -> bool:
    row = db.session.execute(
        text("""
            SELECT used
              FROM weekly_free_mass_offers
             WHERE user_id = :uid AND week_monday = :wk
             LIMIT 1
        """),
        {"uid": user_id, "wk": _dstr(week_monday)},
    ).fetchone()
    return bool(row and int(row[0]) == 1)


def mark_weekly_free_used(user_id: int, week_monday: date) -> None:
    db.session.execute(
        text("""
            INSERT INTO weekly_free_mass_offers (user_id, week_monday, used)
            VALUES (:uid, :wk, 1)
            ON DUPLICATE KEY UPDATE used = 1
        """),
        {"uid": user_id, "wk": _dstr(week_monday)},
    )
    db.session.commit()
