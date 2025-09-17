"""
Entitlements (plan -> capabilities) for FantasyHub / RosterDash.

This module is pure logic (no Flask/DB imports). It computes the effective
capabilities for a given user object based on lightweight fields like:
  - user.plan (tier key, case-insensitive)
  - user.unlimited (bool override)
  - user.founder_expires_at (date/datetime for Founder window)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

# ----------------------------- Plan capability table -----------------------------

# IMPORTANT:
# - "unlimited" here corresponds to your "Power 50" product
#   (league cap intentionally set to 50 per your preference).
# - "founder" remains truly unlimited (cap 9999) while active.
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
        "mass_offer_daily_cap": 3,  # allow "banking" elsewhere if desired
        "aggregate_showdown": True,
        "saved_presets": True,
    },
    "mgr12": {
        "league_cap": 12,
        "mass_offer_daily_cap": 9999,
        "aggregate_showdown": True,
        "saved_presets": True,
    },
    # Power 50 (formerly "unlimited" name in your UI/logic)
    "unlimited": {
        "league_cap": 50,            # <- Power 50 cap
        "mass_offer_daily_cap": 9999,
        "aggregate_showdown": True,
        "saved_presets": True,
    },
    # Founder: time-boxed window; keep effectively unlimited
    "founder": {
        "league_cap": 9999,          # <- truly unlimited while active
        "mass_offer_daily_cap": 9999,
        "aggregate_showdown": True,
        "saved_presets": True,
    },
}

# Accept common aliases/capitalizations and normalize to the keys above.
_ALIASES = {
    "pwr50": "unlimited",
    "power50": "unlimited",
    "power_50": "unlimited",
    "power-50": "unlimited",
    # If something upstream stores uppercase, map it down:
    "free": "free",
    "mgr5": "mgr5",
    "mgr12": "mgr12",
    "unlimited": "unlimited",
    "founder": "founder",
}

def _norm_plan(value) -> str:
    """Normalize plan strings to lowercase keys in PLAN_RULES."""
    if value is None:
        return "free"
    p = str(value).strip().lower()
    p = _ALIASES.get(p, p)
    return p if p in PLAN_RULES else "free"


# ----------------------------- Founder window helper -----------------------------

def _is_founder_active(user: Any, today: Optional[date] = None) -> bool:
    """
    Return True if the user has an active founder window.
    Expects 'founder_expires_at' on the user (datetime or date).
    """
    exp_attr = getattr(user, "founder_expires_at", None)
    if not exp_attr:
        return False

    today = today or date.today()
    try:
        exp_date = exp_attr.date()  # datetime -> date
    except AttributeError:
        exp_date = exp_attr         # already a date

    return bool(exp_date and exp_date >= today)


# ----------------------------- Public API ---------------------------------------

def get_entitlements(user: Any, today: Optional[date] = None) -> Dict[str, Any]:
    """
    Compute effective entitlements for a user.

    The `user` is expected to expose these attributes (if present):
      - plan: string key (any case). Accepted: free, mgr5, mgr12, unlimited (Power 50), founder
      - unlimited: bool (hard override)
      - founder_expires_at: date/datetime (applies for 'founder' plan duration)

    Returns a *copy* of the capability dict so callers can enrich it safely.
    """
    # Hard override for explicit unlimited flag
    if bool(getattr(user, "unlimited", False)):
        base = PLAN_RULES["unlimited"].copy()
        base.update({"plan_key": "unlimited"})
        return base

    plan = _norm_plan(getattr(user, "plan", "free"))

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
    """
    Human-friendly summary for account page chips/badges.

    Shows "Unlimited leagues" only if plan_key == 'founder' (or the cap is truly huge),
    otherwise uses the actual cap number (so Power 50 shows "Up to 50 leagues").
    """
    ent = get_entitlements(user)
    cap = int(ent.get("league_cap", 0) or 0)
    plan = ent.get("plan_key", "free")

    unlimited_text = (plan == "founder") or (cap >= 9999)
    cap_str = "Unlimited leagues" if unlimited_text else f"Up to {cap} leagues"

    return f"{plan.upper()} · {cap_str} · Mass offers/day: {ent.get('mass_offer_daily_cap', 0)}"
