"""
Webhooks package bootstrap.

SAFE to add now: defines no routes by itself. Routes live in webhooks/stripe.py,
which will remain inactive until you register its blueprint in app.py.
"""
from __future__ import annotations
