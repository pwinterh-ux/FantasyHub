"""
Stripe webhook handler (NON-IMPACTING until blueprint is registered)

Register later in app.py:
    from webhooks.stripe import bp as stripe_webhooks_bp
    app.register_blueprint(stripe_webhooks_bp)

Env required when you do register:
    STRIPE_SECRET_KEY=sk_...
    STRIPE_WEBHOOK_SECRET=whsec_...

Notes:
- Verifies Stripe signature.
- Logs all events; attempts to persist to stripe_webhook_logs if your DB/model exists.
- Provides TODO hooks to apply entitlements on relevant events.
"""

from __future__ import annotations

import os
import json
from typing import Any, Dict, Optional

from flask import Blueprint, current_app, request, jsonify

bp = Blueprint("stripe_webhooks", __name__, url_prefix="/webhooks/stripe")


# —————————————————————————————————————————————————————————
# Stripe lazy loader
# —————————————————————————————————————————————————————————

def _stripe():
    import stripe  # local import keeps this file safe until you actually need it
    api_key = os.getenv("STRIPE_SECRET_KEY")
    if not api_key:
        raise RuntimeError("STRIPE_SECRET_KEY not set")
    stripe.api_key = api_key
    return stripe


def _endpoint_secret() -> str:
    secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not secret:
        raise RuntimeError("STRIPE_WEBHOOK_SECRET not set")
    return secret


# —————————————————————————————————————————————————————————
# Optional persistence to DB (safe no-op if models not present)
# —————————————————————————————————————————————————————————

def _persist_webhook_event(event: Dict[str, Any], success: bool, error: Optional[str] = None) -> None:
    """
    Try to persist to stripe_webhook_logs. If table/model isn't ready yet,
    just log to the app logger and return.
    """
    try:
        # Attempt SQLAlchemy import
        from models import db
        # If you create a mapped model StripeWebhookLog later, you can use it here.
        # For now, do a raw insert to avoid depending on the model class.
        try:
            payload_json = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            payload_json = None

        sql = """
            INSERT INTO stripe_webhook_logs (event_id, event_type, success, error, payload)
            VALUES (:event_id, :event_type, :success, :error, CAST(:payload AS JSON))
            ON DUPLICATE KEY UPDATE
                processed_at = NOW(),
                success = VALUES(success),
                error = VALUES(error)
        """
        params = {
            "event_id": event.get("id"),
            "event_type": event.get("type"),
            "success": 1 if success else 0,
            "error": error,
            "payload": payload_json,
        }
        db.session.execute(sql, params)
        db.session.commit()
    except Exception as e:
        # Swallow all errors here — webhook must not crash if DB/table isn't ready.
        current_app.logger.info("[StripeWebhook][log-only] type=%s id=%s success=%s err=%s",
                                event.get("type"), event.get("id"), success, error or str(e))


# —————————————————————————————————————————————————————————
# Entitlements application (stub; wire up later)
# —————————————————————————————————————————————————————————

def _apply_entitlements_from_event(event: Dict[str, Any]) -> None:
    """
    TODO: Map Stripe objects to your user and apply entitlements.
    - Use `client_reference_id` (set in Checkout) to find the user.
    - Use `price.id` (from subscription/items or payment link) to determine plan.
    - Update users.plan, league_cap, mass_offer_daily_cap, stripe_customer_id, etc.
    Safe no-op for now.
    """
    try:
        etype = event.get("type", "")
        data = event.get("data", {}).get("object", {})
        current_app.logger.info("[StripeWebhook] apply-entitlements etype=%s keys=%s",
                                etype, list(data.keys()))
        # Example sketch (implement later):
        # - on checkout.session.completed:
        #       user_id = int(data.get("client_reference_id"))
        #       customer = data.get("customer")
        #       mode = data.get("mode")  # "subscription" or "payment"
        #       if mode == "subscription":
        #           sub_id = data.get("subscription")
        #           # fetch subscription to read items[0].price.id, then map to plan
        # - on customer.subscription.updated / created:
        #       price_id = event["data"]["object"]["items"]["data"][0]["price"]["id"]
        #       status   = event["data"]["object"]["status"]
        #       # map price_id -> plan caps and set on user
        pass
    except Exception as e:
        current_app.logger.exception("apply_entitlements_from_event failed: %s", e)


# —————————————————————————————————————————————————————————
# Route
# —————————————————————————————————————————————————————————

@bp.route("", methods=["POST"])
def handle():
    """
    Main Stripe webhook endpoint: POST /webhooks/stripe
    Verifies signature and ACKs events. Safe to deploy; if env vars are missing,
    returns 400 so you know to set them before going live.
    """
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        stripe = _stripe()
        secret = _endpoint_secret()
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=secret)
    except Exception as e:
        current_app.logger.warning("Stripe webhook signature verification failed: %s", e)
        try:
            _persist_webhook_event({"id": None, "type": "signature_error"}, success=False, error=str(e))
        finally:
            return jsonify({"error": "invalid signature"}), 400

    # At this point, the signature is valid.
    etype = event.get("type", "")
    try:
        # Optionally persist the raw event (idempotency is enforced by UNIQUE(event_id) in DB schema)
        _persist_webhook_event(event, success=True, error=None)

        # Handle selected events (safe no-ops now; log for visibility)
        if etype in {
            "checkout.session.completed",
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
            "invoice.payment_succeeded",
            "invoice.payment_failed",
        }:
            _apply_entitlements_from_event(event)

        # Always ACK — Stripe expects a 2xx to stop retries
        return jsonify({"received": True}), 200

    except Exception as e:
        current_app.logger.exception("Stripe webhook processing error: %s", e)
        try:
            _persist_webhook_event(event, success=False, error=str(e))
        finally:
            # Still ACK with 200 to avoid repeated retries if your business logic failed;
            # you can reconcile later via scripts/reconcile_stripe.py
            return jsonify({"received": True, "warning": "processing error logged"}), 200
