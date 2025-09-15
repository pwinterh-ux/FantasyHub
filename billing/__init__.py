"""
Billing blueprint bootstrap (Stripe Checkout/Portal routes live in routes.py)

SAFE to add now: This file defines a blueprint but does nothing until you
explicitly register it in app.py (e.g., app.register_blueprint(billing.bp)).
"""

from __future__ import annotations
from flask import Blueprint

bp = Blueprint("billing", __name__, url_prefix="/billing")

# Import route definitions when the blueprint is created.
# This has no effect until the blueprint is registered by the app.
from . import routes  # noqa: E402,F401
