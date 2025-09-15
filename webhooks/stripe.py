from flask import Blueprint, jsonify

bp = Blueprint("stripe_webhooks", __name__, url_prefix="/webhooks/stripe")

@bp.route("", methods=["POST", "GET"])
def webhook_root():
    # Temporary stub to avoid 500s until we wire Stripe
    return jsonify({"ok": True, "stub": "stripe webhook"}), 200
