"""
Admin blueprint bootstrap.

SAFE to add now: This file defines a blueprint but does nothing until you
explicitly register it in app.py (e.g., app.register_blueprint(admin.bp)).
"""

from __future__ import annotations
from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")

# Route definitions live in admin/routes.py; importing here keeps them colocated.
from . import routes  # noqa: E402,F401
