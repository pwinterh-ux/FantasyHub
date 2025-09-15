"""
Entitlements (plan -> capabilities) for FantasyHub
SAFE to add now: this module does not import your app or models and does nothing
until you import/use it elsewhere.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

# Default plan capabilities. Tweak these numbers here when pricing changes.
PLAN_RULES: Dict[str, Dict[str, Any]] = {
    "free": {
        "league_cap": 3,
        "mass_offer_daily_cap": 0,
        "aggregate_showdown": False,
        "saved_presets": False,
        "free_mass_offer_weekly": 1,
        "free_recipients_cap": 6,
    },
    "mgr5": {
        "league_cap": 5,
        "mass_offer_daily_cap": 3,  # you can allow "banking" in your guards if desired
        "aggregate_showdown": True,
        "saved_presets": True,
    },
    "mgr12": {
        "league_cap": 12,
        "mass_offer_daily_cap": 9999,
        "aggregate_showdown": True,
        "saved_presets": True,
    },
    "unlimited": {
        "league_cap": 9999,
        "mass_offer_daily_cap": 9999,
        "aggregate_showdown": True,
        "saved_presets": True,
    },
    "founder": {  # same as unlimited, but time-boxed by founder_expires_at
        "league_cap": 9999,
        "mass_offer_daily_cap": 9999,
        "aggregate_showdown": True,
        "saved_presets": True,
    },
}


def _is_founder_active(user: Any, today: Optional[date] = None) -> bool:
    """
    Return True if the user has an active founder window.
    Expects 'founder_expires_at' on the user (datetime/date) if present.
    """
    if not getattr(user, "founder_expires_at", None):
        return False
    today = today or date.today()
    # Accept either date or datetime on the user attribute
    exp_attr = getattr(user, "founder_expires_at")
    try:
        exp_date = exp_attr.date()  # datetime
    except AttributeError:
        exp_date = exp_attr  # date
    return bool(exp_date and exp_date >= today)


def get_entitlements(user: Any, today: Optional[date] = None) -> Dict[str, Any]:
    """
    Compute effective entitlements for a user.

    The `user` is expected to expose these attributes (if present):
      - plan: one of {'free','mgr5','mgr12','unlimited','founder'}
      - unlimited: bool (explicit override)
      - founder_expires_at: date/datetime (for 'founder' plan duration)

    Returns a *copy* of the capability dict so callers can enrich it safely.
    """
    # Hard override for explicit unlimited
    if bool(getattr(user, "unlimited", False)):
        base = PLAN_RULES["unlimited"].copy()
        base.update({"plan_key": "unlimited"})
        return base

    plan = getattr(user, "plan", "free") or "free"
    if plan == "founder":
        if _is_founder_active(user, today=today):
            base = PLAN_RULES["founder"].copy()
            base.update({"plan_key": "founder"})
            return base
        # Founder expired — fall back to free unless you store/restore a prior plan
        plan = "free"

    base = PLAN_RULES.get(plan, PLAN_RULES["free"]).copy()
    base.update({"plan_key": plan})
    return base


def describe_plan(user: Any) -> str:
    """Human-friendly summary for account page chips/badges."""
    ent = get_entitlements(user)
    cap = ent.get("league_cap", 0)
    plan = ent.get("plan_key", "free")
    cap_str = "Unlimited leagues" if plan in ("unlimited", "founder") else f"Up to {cap} leagues"
    return f"{plan.upper()} · {cap_str} · Mass offers/day: {ent.get('mass_offer_daily_cap', 0)}"
