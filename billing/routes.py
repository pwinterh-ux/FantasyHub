"""
Stripe Checkout & Billing Portal routes (NON-IMPACTING until blueprint registered)

Usage later:
  - Register blueprint in app.py:
        from billing import bp as billing_bp
        app.register_blueprint(billing_bp)

  - Set env vars on PythonAnywhere (test mode first):
        STRIPE_SECRET_KEY=sk_test_...
        PRICE_MGR5_WEEKLY=price_...
        PRICE_MGR5_SEASON=price_...
        PRICE_MGR12_WEEKLY=price_...
        PRICE_MGR12_SEASON=price_...
        PRICE_UNLIMITED_WEEKLY=price_...
        PRICE_UNLIMITED_SEASON=price_...
        PRICE_FOUNDER_ONETIME=price_...

Endpoints:
  POST /billing/checkout/<price_id>?mode=subscription|payment
      -> Creates a Stripe Checkout Session and returns {"url": "..."} for redirect.

  GET  /billing/portal
      -> Creates a Stripe Billing Portal Session for the current user.

  GET  /billing/success, /billing/cancel
      -> Simple landing routes you can customize.
"""

from __future__ import annotations

import os
from typing import Optional

from flask import current_app, request, jsonify, url_for, redirect
from flask_login import current_user

from . import bp  # blueprint from billing/__init__.py


# —————————————————————————————————————————————————————————
# Helpers
# —————————————————————————————————————————————————————————

def _stripe():
    """Lazy import & configure Stripe with the secret key from env."""
    import stripe  # local import to avoid import-time errors if not installed yet
    api_key = os.getenv("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not set in environment.")
    stripe.api_key = api_key
    return stripe


def _infer_mode_for_price(price_id: str) -> str:
    """
    Infer Checkout mode for a given price_id. If the caller provides ?mode=...,
    that wins. Otherwise, default to 'subscription' except when matching
    PRICE_FOUNDER_ONETIME (one-time payment).
    """
    qmode = (request.args.get("mode") or "").strip().lower()
    if qmode in {"subscription", "payment"}:
        return qmode
    founder = os.getenv("PRICE_FOUNDER_ONETIME")
    if founder and price_id == founder:
        return "payment"
    return "subscription"


def _absolute(url_path: str) -> str:
    """Build an absolute URL from an endpoint path (e.g., '/account')."""
    if url_path.startswith("http://") or url_path.startswith("https://"):
        return url_path
    # default to url_for when possible, else join with external root
    try:
        return url_for(url_path, _external=True)  # if an endpoint name was passed
    except Exception:
        # Fallback: build from host + path
        try:
            # Prefer SERVER_NAME if configured
            base = current_app.config.get("EXTERNAL_BASE_URL")
            if base:
                return base.rstrip("/") + "/" + url_path.lstrip("/")
        except Exception:
            pass
        # Last resort: relative path (Stripe requires absolute, so set EXTERNAL_BASE_URL in prod)
        return url_path


def _success_url() -> str:
    # You can customize these endpoints later; they’re safe placeholders.
    try:
        return url_for("billing.success", _external=True)
    except Exception:
        return "/billing/success"


def _cancel_url() -> str:
    try:
        return url_for("billing.cancel", _external=True)
    except Exception:
        return "/billing/cancel"


def _return_to_account_url() -> str:
    # Where to send users after they finish in the Billing Portal.
    try:
        return url_for("account", _external=True)
    except Exception:
        return "/account"


# —————————————————————————————————————————————————————————
# Routes (inert until blueprint is registered)
# —————————————————————————————————————————————————————————

@bp.route("/checkout/<price_id>", methods=["POST"])
def checkout(price_id: str):
    """
    Create a Stripe Checkout Session for the given price_id.
    Mode defaults to 'subscription' unless founder one-time price or ?mode=payment.
    Returns JSON: {"url": "https://checkout.stripe.com/..."} for the client to redirect.
    """
    if not getattr(current_user, "is_authenticated", False):
        return jsonify({"error": "Authentication required"}), 401

    try:
        stripe = _stripe()
        mode = _infer_mode_for_price(price_id)
        success_url = _success_url()
        cancel_url = _cancel_url()

        # Basic line item — quantity fixed at 1. If you later support multiple seats, adjust here.
        line_items = [{"price": price_id, "quantity": 1}]

        # Create a Checkout Session
        params = dict(
            success_url=success_url,
            cancel_url=cancel_url,
            client_reference_id=str(getattr(current_user, "id", "")),
            allow_promotion_codes=True,
            automatic_tax={"enabled": False},  # flip to True if you enable Stripe Tax
        )

        if mode == "subscription":
            params.update(
                mode="subscription",
                line_items=line_items,
            )
        else:
            params.update(
                mode="payment",
                line_items=line_items,
            )

        # If the user already has a customer ID, attach it so Portal works seamlessly later
        customer_id = getattr(current_user, "stripe_customer_id", None)
        if customer_id:
            params["customer"] = customer_id

        session = stripe.checkout.Session.create(**params)

        # Optional: store session.id on your side if you want to reconcile later
        # Not doing it here to keep this route side-effect free beyond Stripe

        return jsonify({"url": session.url})
    except Exception as e:
        current_app.logger.exception("Stripe checkout creation failed")
        return jsonify({"error": str(e)}), 400


@bp.route("/portal", methods=["GET"])
def portal():
    """
    Create a Billing Portal Session for the current user and redirect to it.
    Requires current_user.stripe_customer_id to be set (after first successful Checkout).
    """
    if not getattr(current_user, "is_authenticated", False):
        return jsonify({"error": "Authentication required"}), 401

    customer_id = getattr(current_user, "stripe_customer_id", None)
    if not customer_id:
        return jsonify({"error": "No Stripe customer on file. Start with a checkout purchase."}), 400

    try:
        stripe = _stripe()
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=_return_to_account_url(),
        )
        return redirect(session.url, code=302)
    except Exception as e:
        current_app.logger.exception("Stripe portal creation failed")
        return jsonify({"error": str(e)}), 400


@bp.route("/success", methods=["GET"])
def success():
    """
    Lightweight success landing. You can customize to show 'Plan active' and a button to /account.
    """
    return (
        "<h3>Payment successful</h3>"
        '<p>You can close this tab and return to your account page.</p>'
        '<p><a href="/account">Go to Account</a></p>'
    ), 200


@bp.route("/cancel", methods=["GET"])
def cancel():
    """
    Lightweight cancel landing.
    """
    return (
        "<h3>Checkout canceled</h3>"
        '<p>No changes were made. <a href="/pricing">Go back to Pricing</a></p>'
    ), 200


# —————————————————————————————————————————————————————————
# Optional debug endpoint (hide in production)
# —————————————————————————————————————————————————————————

@bp.route("/prices", methods=["GET"])
def debug_prices():
    """
    Lists known price IDs from environment for quick smoke checks (TEST MODE).
    DO NOT expose in production without auth.
    """
    if not current_app.debug:
        return jsonify({"error": "Not available"}), 404

    keys = [
        "PRICE_MGR5_WEEKLY",
        "PRICE_MGR5_SEASON",
        "PRICE_MGR12_WEEKLY",
        "PRICE_MGR12_SEASON",
        "PRICE_UNLIMITED_WEEKLY",
        "PRICE_UNLIMITED_SEASON",
        "PRICE_FOUNDER_ONETIME",
    ]
    return jsonify({k: os.getenv(k, "") for k in keys})
