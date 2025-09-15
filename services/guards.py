"""
Guards & gates for FantasyHub (league caps, feature flags, terms gate, mass-offer caps)

SAFE to add now: no app imports at module load, no DB access by default.
When you’re ready to wire it, import specific helpers in your routes.

Design:
- All plan math comes from services.entitlements.get_entitlements(user)
- Decorators (require_feature, require_terms) import Flask bits lazily to avoid side-effects
- Mass-offer consumption is callback-driven so you can plug in your DB logic later
"""
from __future__ import annotations

from functools import wraps
from datetime import date, timedelta
from typing import Any, Callable, Optional, Tuple

# Lazy import pattern inside functions prevents import-time coupling.
# from flask import jsonify
# from flask_login import current_user

# —————————————————————————————————————————————————————————
# Plan & caps helpers
# —————————————————————————————————————————————————————————

def _get_entitlements_for(user: Any) -> dict:
    """Fetch effective entitlements for a user via services.entitlements."""
    from services.entitlements import get_entitlements
    return get_entitlements(user)


def enforce_league_cap(user: Any, current_league_count: int) -> bool:
    """
    Return True if the user is within their league cap (OK to add/sync another),
    False if over/at the cap.
    """
    ent = _get_entitlements_for(user)
    cap = int(ent.get("league_cap", 0))
    return current_league_count < cap


# —————————————————————————————————————————————————————————
# Decorators: feature gate & terms gate
# —————————————————————————————————————————————————————————

def require_feature(feature_name: str):
    """
    Decorator to protect endpoints that require a paid feature (e.g., 'aggregate_showdown').

    Usage:
        @require_feature('aggregate_showdown')
        def aggregate_view(): ...
    """
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            # Lazy imports so this file remains inert until used
            from flask import jsonify
            from flask_login import current_user

            ent = _get_entitlements_for(current_user)
            if not ent.get(feature_name, False):
                return jsonify({
                    "error": "Upgrade required for this feature.",
                    "upgradeRequired": True,
                    "feature": feature_name,
                    "plan": ent.get("plan_key", "free"),
                }), 402
            return fn(*args, **kwargs)
        return wrapper
    return deco


def require_terms(fn):
    """
    Decorator to ensure the user has accepted Terms/Privacy/AUP before write actions.

    Assumes your User model will eventually have:
      - tos_version, privacy_version, aup_version (non-null when accepted)
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import jsonify
        from flask_login import current_user

        u = current_user
        has_accepted = bool(getattr(u, "tos_version", None)
                            and getattr(u, "privacy_version", None)
                            and getattr(u, "aup_version", None))
        if not has_accepted:
            return jsonify({
                "error": "Please accept the Terms, Privacy Policy, and AUP to continue.",
                "needsTerms": True
            }), 402
        return fn(*args, **kwargs)
    return wrapper


# —————————————————————————————————————————————————————————
# Mass-offer caps with pluggable storage
# —————————————————————————————————————————————————————————

def week_monday_key(d: Optional[date] = None) -> date:
    """
    Return the Monday date for the week containing d (used to track 1 free send/week).
    """
    d = d or date.today()
    return d - timedelta(days=d.weekday())


def consume_mass_offer(
    user: Any,
    recipients_count: int,
    *,
    # Callbacks let you plug in DB logic without importing your models here.
    get_today_count: Callable[[int, date], int],
    increment_today_count: Callable[[int, date], None],
    get_bonus_balance: Callable[[int], int],
    use_one_bonus: Callable[[int], int],
    # Optional weekly-free tracking for the Free plan:
    get_weekly_free_used: Optional[Callable[[int, date], bool]] = None,
    mark_weekly_free_used: Optional[Callable[[int, date], None]] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Enforce mass-offer limits for the user and update counters.

    Returns: (ok: bool, message: Optional[str])
      - ok=True: proceed with send.
      - ok=False: block and show 'message'.

    Required callbacks (supply your DB-backed functions when wiring):
      get_today_count(user_id, today) -> int
      increment_today_count(user_id, today) -> None
      get_bonus_balance(user_id) -> int
      use_one_bonus(user_id) -> int  (returns remaining bonus after decrement)

    Optional callbacks (for Free weekly allowance):
      get_weekly_free_used(user_id, monday_date) -> bool
      mark_weekly_free_used(user_id, monday_date) -> None
    """
    ent = _get_entitlements_for(user)
    plan = ent.get("plan_key", "free")
    daily_cap = int(ent.get("mass_offer_daily_cap", 0))

    # Free-tier recipient limit (protect platform health & paid value)
    if plan == "free":
        recipients_cap = int(ent.get("free_recipients_cap", 6))
        if recipients_count > recipients_cap:
            return False, f"Free plan limit is {recipients_cap} recipients per mass send. Upgrade to send to all."

        # Enforce 1 free mass send per week, if tracking callbacks provided
        if get_weekly_free_used and mark_weekly_free_used:
            week_key = week_monday_key()
            if get_weekly_free_used(user.id, week_key):
                return False, "You’ve used your weekly free mass offer. Upgrade to send more."
            # Reserve the weekly free now
            mark_weekly_free_used(user.id, week_key)
        else:
            # If weekly tracking not wired yet, allow the send (soft mode)
            pass

        # Free has no daily cap beyond the 1/week; we’re done
        return True, None

    # Paid tiers: check daily cap
    today = date.today()
    used_today = int(get_today_count(user.id, today))

    if daily_cap <= 0:
        # Shouldn't happen for paid plans, but respect bonus sends
        bonus = int(get_bonus_balance(user.id))
        if bonus > 0:
            remaining = use_one_bonus(user.id)
            return True, None
        return False, "Your plan does not allow mass offers. Upgrade to enable this feature."

    if used_today < daily_cap:
        # Still under today’s cap — increment and allow
        increment_today_count(user.id, today)
        return True, None

    # Cap reached — try bonus sends
    bonus = int(get_bonus_balance(user.id))
    if bonus > 0:
        remaining = use_one_bonus(user.id)
        return True, None

    # Block with friendly message
    return False, f"Daily mass-offer cap reached ({daily_cap}). Try again tomorrow or upgrade your plan."
