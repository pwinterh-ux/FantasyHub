# billing/routes.py
from __future__ import annotations

import os
import datetime as _dt
from urllib.parse import urljoin
from typing import Any, Dict, Optional

import stripe
from flask import (
    Blueprint,
    current_app,
    jsonify,
    redirect,
    request,
    url_for,
    abort,
)
from flask_login import login_required, current_user
from flask import has_app_context

from app import db
from models import User

billing_bp = Blueprint("billing", __name__, url_prefix="/billing")

# ───────────────────────────── helpers: config/env ─────────────────────────────

def _get_cfg(key: str) -> Optional[str]:
    """Read from env first (safe at import), then Flask config if an app ctx exists."""
    v = os.environ.get(key)
    if v:
        return v.strip()
    if has_app_context():
        w = current_app.config.get(key)
        if w:
            return str(w).strip()
    return None

def _require(key: str) -> str:
    v = _get_cfg(key)
    if not v:
        raise RuntimeError(f"{key} not configured")
    return v

def _stripe_key() -> str:
    return _require("STRIPE_SECRET_KEY")

def _ensure_stripe() -> None:
    stripe.api_key = _stripe_key()

def _base_url() -> str:
    """Absolute site base, e.g. https://www.rosterdash.com/ (trailing slash)."""
    explicit = _get_cfg("APP_BASE_URL")
    if explicit:
        return explicit if explicit.endswith("/") else explicit + "/"
    root = (request.url_root or "").strip()
    return root if root.endswith("/") else (root + "/")

# ───────────────────────────── price ids & mapping ─────────────────────────────

def _price_ids() -> Dict[str, str]:
    """Resolve your price IDs lazily (works inside/outside app ctx)."""
    return {
        "FOUNDER_ONETIME": _get_cfg("PRICE_FOUNDER_ONETIME") or "price_1S6XEJ3UIVtwjIKCMD3gBcff",

        "MGR5_SEASON":     _get_cfg("PRICE_MGR5_SEASON")     or "price_1S6X1t3UIVtwjIKCdMJtmzX9",
        "MGR5_WEEKLY":     _get_cfg("PRICE_MGR5_WEEKLY")     or "price_1S6X1t3UIVtwjIKC043fYyUe",

        "MGR12_SEASON":    _get_cfg("PRICE_MGR12_SEASON")    or "price_1S6X5f3UIVtwjIKCTVAT1FxX",
        "MGR12_WEEKLY":    _get_cfg("PRICE_MGR12_WEEKLY")    or "price_1S6X5f3UIVtwjIKCd2skWXxT",

        "PWR50_SEASON":    _get_cfg("PRICE_UNLIMITED_SEASON") or "price_1S6XAi3UIVtwjIKCnNeltCrG",
        "PWR50_WEEKLY":    _get_cfg("PRICE_UNLIMITED_WEEKLY") or "price_1S6XBH3UIVtwjIKCmfk3PJhA",
    }

def _plan_by_price() -> Dict[str, Dict[str, Any]]:
    """
    Map Stripe price_id -> normalized tier (what templates expect) + cadence + caps.
    We always store user.plan as the TIER ONLY, not including cadence.
    """
    P = _price_ids()
    return {
        # Manage 5
        P["MGR5_SEASON"]:  {"tier": "MGR5",  "cadence": "season", "league_cap": 5,  "mass_offer_daily_cap": 3},
        P["MGR5_WEEKLY"]:  {"tier": "MGR5",  "cadence": "weekly", "league_cap": 5,  "mass_offer_daily_cap": 3},
        # Dominate 12
        P["MGR12_SEASON"]: {"tier": "MGR12", "cadence": "season", "league_cap": 12, "mass_offer_daily_cap": 99999},
        P["MGR12_WEEKLY"]: {"tier": "MGR12", "cadence": "weekly", "league_cap": 12, "mass_offer_daily_cap": 99999},
        # Power 50
        P["PWR50_SEASON"]: {"tier": "UNLIMITED", "cadence": "season", "league_cap": 50, "mass_offer_daily_cap": 99999},
        P["PWR50_WEEKLY"]: {"tier": "UNLIMITED", "cadence": "weekly", "league_cap": 50, "mass_offer_daily_cap": 99999},
    }

# free/founder constants
FREE_LEAGUE_CAP = 3
FOUNDER_TERM_DAYS = 730
FOUNDER_LEAGUE_CAP = 50
FOUNDER_MASS_OFFERS = 99999

# ───────────────────────────── URL helpers ─────────────────────────────

def _success_url() -> str:
    return urljoin(_base_url(), "account?checkout=success")

def _cancel_url() -> str:
    return urljoin(_base_url(), "pricing")

# ───────────────────────────── Checkout ─────────────────────────────

@billing_bp.route("/checkout/<price_id>", methods=["POST"])
@login_required
def start_checkout(price_id: str):
    """
    Create a Checkout Session for:
      - mode=subscription (recurring plans)
      - mode=payment      (one-time Founder pass)
    Returns JSON: {url: session.url}
    """
    _ensure_stripe()

    mode = (request.args.get("mode") or "subscription").strip().lower()
    if mode not in {"subscription", "payment"}:
        return jsonify({"error": "Invalid mode. Use 'subscription' or 'payment'."}), 400

    existing_customer = getattr(current_user, "stripe_customer_id", None)

    params: Dict[str, Any] = {
        "mode": mode,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": _success_url(),
        "cancel_url": _cancel_url(),
        "allow_promotion_codes": True,
        "client_reference_id": str(current_user.id),
        "metadata": {
            "user_id": str(current_user.id),
            "username": getattr(current_user, "username", "") or "",
            "email": getattr(current_user, "email", "") or "",
            "price_id": price_id,  # for webhook without extra fetch
        },
    }

    if mode == "payment":
        # Payment mode allows customer_creation
        if existing_customer:
            params["customer"] = existing_customer
        else:
            params["customer_creation"] = "always"
    else:
        # Subscription mode: do NOT set customer_creation
        if existing_customer:
            params["customer"] = existing_customer

    try:
        session = stripe.checkout.Session.create(**params)

        # If Stripe already attached/created a customer, store it
        try:
            if not existing_customer and getattr(session, "customer", None):
                current_user.stripe_customer_id = session.customer
                db.session.commit()
        except Exception:
            db.session.rollback()

        return jsonify({"url": session.url})
    except stripe.error.StripeError as e:
        msg = getattr(e, "user_message", None) or str(e)
        return jsonify({"error": msg}), 400
    except Exception as e:
        return jsonify({"error": f"Unable to start checkout: {e}"}), 400

# ───────────────────────────── Billing Portal ─────────────────────────────

@billing_bp.route("/portal", methods=["GET"])
@login_required
def billing_portal():
    _ensure_stripe()
    customer_id = getattr(current_user, "stripe_customer_id", None)
    if not customer_id:
        return redirect(url_for("pricing"))
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=urljoin(_base_url(), "account"),
        )
        return redirect(session.url)
    except stripe.error.StripeError as e:
        msg = getattr(e, "user_message", None) or str(e)
        return redirect(url_for("account", portal_error=msg))

# ───────────────────────────── Webhook (single endpoint) ─────────────────────────────

def _webhook_secret() -> str:
    sec = _get_cfg("STRIPE_WEBHOOK_SECRET")
    if not sec:
        # 500 so misconfiguration is obvious
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set")
    return sec

def _set_if_has(user: User, field: str, value: Any) -> None:
    if hasattr(user, field):
        setattr(user, field, value)

def _apply_founder(user: User, now_utc: Optional[_dt.datetime] = None) -> None:
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    _set_if_has(user, "founder_expires_at", now_utc + _dt.timedelta(days=FOUNDER_TERM_DAYS))
    _set_if_has(user, "plan", "FOUNDER")
    _set_if_has(user, "league_cap", FOUNDER_LEAGUE_CAP)
    _set_if_has(user, "mass_offer_daily_cap", FOUNDER_MASS_OFFERS)

def _apply_subscription_plan(user: User, price_id: str) -> None:
    plan = _plan_by_price().get(price_id)
    if not plan:
        current_app.logger.info("[Stripe] Unknown price_id=%s; skipping plan update", price_id)
        return
    # Normalize to tier only — templates expect MGR5/MGR12/PWR50
    _set_if_has(user, "plan", plan["tier"])
    _set_if_has(user, "league_cap", int(plan["league_cap"]))
    _set_if_has(user, "mass_offer_daily_cap", int(plan["mass_offer_daily_cap"]))
    _set_if_has(user, "stripe_price_id", price_id)  # infer cadence later if you need it

def _downgrade_to_free_or_founder(user: User) -> None:
    now = _dt.datetime.now(_dt.timezone.utc)
    founder_ok = False
    if hasattr(user, "founder_expires_at") and user.founder_expires_at:
        try:
            founder_ok = user.founder_expires_at > now
        except Exception:
            founder_ok = False
    if founder_ok:
        _apply_founder(user, now)
    else:
        _set_if_has(user, "plan", "FREE")
        _set_if_has(user, "league_cap", FREE_LEAGUE_CAP)
        _set_if_has(user, "mass_offer_daily_cap", 0)
        _set_if_has(user, "stripe_price_id", None)

def _find_user(client_reference_id: Optional[str], customer_id: Optional[str]) -> Optional[User]:
    if client_reference_id:
        try:
            u = db.session.get(User, int(client_reference_id))
            if u:
                return u
        except Exception:
            pass
    if customer_id:
        return db.session.query(User).filter_by(stripe_customer_id=customer_id).first()
    return None

@billing_bp.route("/webhook", methods=["POST"])
def stripe_webhook():
    # Load secret and verify signature
    try:
        secret = _webhook_secret()
    except Exception as e:
        current_app.logger.error("[Stripe] webhook misconfigured: %s", e)
        return jsonify({"error": "misconfigured"}), 500

    payload = request.get_data(as_text=True)
    sig = request.headers.get("Stripe-Signature", "")

    try:
        _ensure_stripe()
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except ValueError:
        return abort(400)  # invalid JSON
    except stripe.error.SignatureVerificationError:
        return abort(400)  # bad signature

    etype = event.get("type", "")
    obj = event.get("data", {}).get("object", {}) or {}
    current_app.logger.info("[Stripe] event=%s id=%s", etype, event.get("id"))

    try:
        # 1) Checkout completed (one-time Founder OR subscription)
        if etype == "checkout.session.completed":
            sess = obj  # stripe.checkout.Session
            client_ref = sess.get("client_reference_id") or (sess.get("metadata") or {}).get("user_id")
            customer_id = sess.get("customer")
            user = _find_user(client_ref, customer_id)
            if not user:
                return jsonify({"ok": True, "note": "user not found"}), 200

            # Save/attach customer id
            if customer_id and getattr(user, "stripe_customer_id", None) != customer_id:
                _set_if_has(user, "stripe_customer_id", customer_id)

            mode = (sess.get("mode") or "").lower()
            meta_price = (sess.get("metadata") or {}).get("price_id")

            if mode == "payment":
                # Founder one-time (no subscription object)
                if meta_price and meta_price == _price_ids()["FOUNDER_ONETIME"]:
                    _apply_founder(user)
                    _set_if_has(user, "stripe_price_id", meta_price)
                db.session.commit()
                return jsonify({"ok": True}), 200

            if mode == "subscription":
                # Fetch subscription to read the definitive price id
                sub_id = sess.get("subscription")
                price_id = None
                if sub_id:
                    sub = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
                    items = list(sub.get("items", {}).get("data", []))
                    price_id = items[0]["price"]["id"] if items else None
                if price_id:
                    _apply_subscription_plan(user, price_id)
                db.session.commit()
                return jsonify({"ok": True}), 200

        # 2) Subscription created/updated → (re)apply plan from price
        if etype in {"customer.subscription.created", "customer.subscription.updated"}:
            sub = obj
            customer_id = sub.get("customer")
            user = db.session.query(User).filter_by(stripe_customer_id=customer_id).first()
            if user:
                items = list(sub.get("items", {}).get("data", []))
                price_id = items[0]["price"]["id"] if items else None
                if price_id:
                    _apply_subscription_plan(user, price_id)
                db.session.commit()
            return jsonify({"ok": True}), 200

        # 3) Subscription canceled → downgrade (respect Founder if still valid)
        if etype == "customer.subscription.deleted":
            sub = obj
            customer_id = sub.get("customer")
            user = db.session.query(User).filter_by(stripe_customer_id=customer_id).first()
            if user:
                _downgrade_to_free_or_founder(user)
                db.session.commit()
            return jsonify({"ok": True}), 200

        # 4) (Optional) payment failure → mark status if you track it
        if etype == "invoice.payment_failed":
            inv = obj
            customer_id = inv.get("customer")
            user = db.session.query(User).filter_by(stripe_customer_id=customer_id).first()
            if user and hasattr(user, "stripe_status"):
                _set_if_has(user, "stripe_status", "past_due")
                db.session.commit()
            return jsonify({"ok": True}), 200

        # Unhandled → ack
        return jsonify({"ok": True, "ignored": etype}), 200

    except Exception as e:
        current_app.logger.exception("[Stripe] webhook error: %s", e)
        db.session.rollback()
        # ACK so Stripe doesn’t retry forever; error is logged for you to fix/replay
        return jsonify({"ok": True, "warning": str(e)}), 200
