"""
Stripe webhook handler for RosterDash

Endpoint: POST /webhooks/stripe
Env required:
  STRIPE_SECRET_KEY=sk_...
  STRIPE_WEBHOOK_SECRET=whsec_...

Registers in app.py:
  from webhooks.routes import bp as stripe_webhooks_bp
  app.register_blueprint(stripe_webhooks_bp)
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from flask import Blueprint, current_app, request, jsonify

from app import db
from models import User

bp = Blueprint("stripe_webhooks", __name__, url_prefix="/webhooks/stripe")


# ------------------ config helpers ------------------

def _stripe():
    import stripe
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY not set")
    stripe.api_key = key
    return stripe

def _endpoint_secret() -> str:
    sec = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    if not sec:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set")
    return sec


# ------------------ plan mapping --------------------

# Your price IDs (from your message)
FOUNDER_PRICE_ID = "price_1S6XEJ3UIVtwjIKCMD3gBcff"
# ~2 seasons
FOUNDER_TERM_DAYS = 730
# Give Founder “full access” — tweak if you want something else
FOUNDER_LEAGUE_CAP = 50
FOUNDER_MASS_OFFERS = 99999

PLAN_BY_PRICE: Dict[str, Dict[str, Any]] = {
    # Manage 5
    "price_1S6X1t3UIVtwjIKCdMJtmzX9": {"code": "MGR5_SEASON", "label": "Manage 5 (Season)",  "league_cap": 5,  "mass_offer_daily_cap": 3},
    "price_1S6X1t3UIVtwjIKC043fYyUe": {"code": "MGR5_WEEKLY", "label": "Manage 5 (Weekly)",  "league_cap": 5,  "mass_offer_daily_cap": 3},
    # Dominate 12
    "price_1S6X5f3UIVtwjIKCTVAT1FxX": {"code": "MGR12_SEASON","label": "Dominate 12 (Season)","league_cap":12, "mass_offer_daily_cap": 99999},
    "price_1S6X5f3UIVtwjIKCd2skWXxT": {"code": "MGR12_WEEKLY","label": "Dominate 12 (Weekly)","league_cap":12, "mass_offer_daily_cap": 99999},
    # Power 50
    "price_1S6XAi3UIVtwjIKCnNeltCrG": {"code": "PWR50_SEASON","label": "Power 50 (Season)",   "league_cap":50, "mass_offer_daily_cap": 99999},
    "price_1S6XBH3UIVtwjIKCmfk3PJhA": {"code": "PWR50_WEEKLY","label": "Power 50 (Weekly)",   "league_cap":50, "mass_offer_daily_cap": 99999},
}

# Defaults for FREE
FREE_LEAGUE_CAP = 3
FREE_MASS_OFFERS = 0


# ------------------ small helpers ------------------

def _get_user_by_client_ref_or_customer(client_reference_id: Optional[str], customer_id: Optional[str]) -> Optional[User]:
    if client_reference_id:
        try:
            u = User.query.get(int(client_reference_id))
            if u:
                return u
        except Exception:
            pass
    if customer_id:
        u = User.query.filter_by(stripe_customer_id=customer_id).first()
        if u:
            return u
    return None

def _set_if_hasattr(obj, field, value):
    if hasattr(obj, field):
        setattr(obj, field, value)

def _apply_founder(user: User, now: Optional[datetime] = None):
    """Grant Founder (one-time)."""
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(days=FOUNDER_TERM_DAYS)
    _set_if_hasattr(user, "founder_expires_at", expires)
    # If you also want plan to read FOUNDER when no active sub:
    _set_if_hasattr(user, "plan", "FOUNDER")
    _set_if_hasattr(user, "league_cap", FOUNDER_LEAGUE_CAP)
    _set_if_hasattr(user, "mass_offer_daily_cap", FOUNDER_MASS_OFFERS)

def _apply_subscription_plan(user: User, price_id: str):
    """Map a recurring price → plan + caps."""
    plan = PLAN_BY_PRICE.get(price_id)
    if not plan:
        current_app.logger.info("Unknown subscription price_id=%s; leaving plan unchanged", price_id)
        return

    _set_if_hasattr(user, "plan", plan["code"])
    _set_if_hasattr(user, "league_cap", int(plan["league_cap"]))
    _set_if_hasattr(user, "mass_offer_daily_cap", int(plan["mass_offer_daily_cap"]))
    _set_if_hasattr(user, "stripe_price_id", price_id)

def _downgrade_to_free_or_founder(user: User):
    """On sub cancel: if Founder still active, keep Founder; else FREE."""
    now = datetime.now(timezone.utc)
    founder_ok = False
    if hasattr(user, "founder_expires_at") and user.founder_expires_at:
        try:
            founder_ok = user.founder_expires_at > now
        except Exception:
            founder_ok = False

    if founder_ok:
        # Keep Founder entitlements
        _apply_founder(user, now)
    else:
        _set_if_hasattr(user, "plan", "FREE")
        _set_if_hasattr(user, "league_cap", FREE_LEAGUE_CAP)
        _set_if_hasattr(user, "mass_offer_daily_cap", FREE_MASS_OFFERS)
        _set_if_hasattr(user, "stripe_price_id", None)


# ------------------ webhook route ------------------

@bp.route("", methods=["POST"])
def handle():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        stripe = _stripe()
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=_endpoint_secret(),
        )
    except Exception as e:
        current_app.logger.warning("Stripe webhook signature verification failed: %s", e)
        return jsonify({"error": "invalid signature"}), 400

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {})

    try:
        # 1) Checkout completed (covers one-time + subscription)
        if etype == "checkout.session.completed":
            sess = obj  # stripe.checkout.Session
            client_ref = (sess.get("client_reference_id") or (sess.get("metadata") or {}).get("user_id"))
            customer_id = sess.get("customer")
            user = _get_user_by_client_ref_or_customer(client_ref, customer_id)
            if not user:
                return jsonify({"ok": True, "note": "user not found"}), 200

            # Store customer id if new
            if customer_id and getattr(user, "stripe_customer_id", None) != customer_id:
                _set_if_hasattr(user, "stripe_customer_id", customer_id)

            mode = sess.get("mode")
            if mode == "payment":
                # One-time (Founder)
                # Pull line items to see the price actually purchased
                try:
                    items = stripe.checkout.Session.list_line_items(sess["id"], limit=10)
                    price_ids = [li["price"]["id"] for li in items.auto_paging_iter()]
                except Exception:
                    price_ids = []
                if FOUNDER_PRICE_ID in price_ids:
                    _apply_founder(user)
                    _set_if_hasattr(user, "stripe_price_id", FOUNDER_PRICE_ID)
                db.session.commit()
                return jsonify({"ok": True}), 200

            if mode == "subscription":
                # Fetch subscription → get price id
                sub_id = sess.get("subscription")
                if sub_id:
                    sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
                    items = list(sub["items"]["data"])
                    price_id = items[0]["price"]["id"] if items else None
                    if price_id:
                        _apply_subscription_plan(user, price_id)
                db.session.commit()
                return jsonify({"ok": True}), 200

        # 2) Subscription lifecycle
        if etype in {"customer.subscription.created", "customer.subscription.updated"}:
            sub = obj  # stripe.Subscription
            customer_id = sub.get("customer")
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if not user:
                # fallback by metadata.user_id if you decide to set it
                user_id_meta = (sub.get("metadata") or {}).get("user_id")
                if user_id_meta:
                    try:
                        user = User.query.get(int(user_id_meta))
                    except Exception:
                        user = None
            if user:
                items = list(sub.get("items", {}).get("data", []))
                price_id = items[0]["price"]["id"] if items else None
                if price_id:
                    _apply_subscription_plan(user, price_id)
                db.session.commit()
            return jsonify({"ok": True}), 200

        if etype in {"customer.subscription.deleted"}:
            sub = obj
            customer_id = sub.get("customer")
            user = User.query.filter_by(stripe_customer_id=customer_id).first()
            if user:
                _downgrade_to_free_or_founder(user)
                db.session.commit()
            return jsonify({"ok": True}), 200

        # Other events (invoice.* etc.) can be logged/ignored
        return jsonify({"ok": True, "ignored": etype}), 200

    except Exception as e:
        current_app.logger.exception("Stripe webhook error: %s", e)
        db.session.rollback()
        # ACK with 200 so Stripe doesn't retry forever; error is logged
        return jsonify({"ok": True, "warning": str(e)}), 200
