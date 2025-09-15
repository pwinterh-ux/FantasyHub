"""
Admin routes (merged JSON tools + HTML UI)

Access control:
- ONLY users with users.is_admin truthy can access anything in this blueprint.

What’s included:
- JSON utilities you already had:
    GET  /_admin/health
    POST /_admin/grant-bonus
    GET  /_admin/logs/api
    GET  /_admin/logs/webhooks
    GET  /_admin/logs/actions
- NEW hidden HTML admin pages:
    GET  /_admin/users           (list/search)
    GET  /_admin/users/<id>      (edit form)
    POST /_admin/users/<id>      (save plan/bonus/founder/clear MFL)

Register in app.py if you haven’t already:
    from admin import bp as admin_bp
    app.register_blueprint(admin_bp)
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
    current_app,
)
from flask_login import current_user, login_required
from sqlalchemy import or_

from app import db
from models import User
from . import bp  # blueprint defined in admin/__init__.py (url_prefix="/_admin")

# --------------------------------------------------------------------
# Admin guard (ONLY users.is_admin)
# --------------------------------------------------------------------

def _require_admin():
    if not getattr(current_user, "is_authenticated", False):
        abort(401)
    if not bool(getattr(current_user, "is_admin", False)):
        abort(403)
    return None


# --------------------------------------------------------------------
# Health (JSON)
# --------------------------------------------------------------------

@bp.route("/health", methods=["GET"])
def health():
    if (resp := _require_admin()) is not None:
        return resp
    return jsonify({"ok": True, "version": current_app.config.get("APP_VERSION", "dev")})


# --------------------------------------------------------------------
# Grant bonus mass-offers (JSON)
#  Body: { "user_id": 123, "count": 5 }
# --------------------------------------------------------------------

@bp.route("/grant-bonus", methods=["POST"])
def grant_bonus():
    if (resp := _require_admin()) is not None:
        return resp

    data = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    try:
        count = int(data.get("count") or 1)
    except Exception:
        count = 1

    if not user_id or count <= 0:
        return jsonify({"error": "Provide user_id and positive count."}), 400

    try:
        db.session.execute(
            """
            UPDATE users
               SET bonus_mass_offers = COALESCE(bonus_mass_offers, 0) + :count
             WHERE id = :user_id
            """,
            {"count": count, "user_id": user_id},
        )
        db.session.commit()
        return jsonify({"ok": True, "user_id": user_id, "added": count})
    except Exception as e:
        current_app.logger.exception("grant_bonus failed")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------
# Log viewers (JSON)
# --------------------------------------------------------------------

def _limit_param(default: int = 100, max_cap: int = 500) -> int:
    try:
        n = int(request.args.get("limit", default))
    except Exception:
        n = default
    return min(max(1, n), max_cap)


@bp.route("/logs/api", methods=["GET"])
def logs_api():
    if (resp := _require_admin()) is not None:
        return resp

    limit = _limit_param()
    try:
        rows = db.session.execute(
            f"""
            SELECT id, created_at, user_id, league_id, host, method, endpoint,
                   status_code, response_ms, ok, throttled, message
              FROM api_call_logs
          ORDER BY id DESC
             LIMIT {limit}
        """
        ).mappings().all()
        return jsonify({"items": [dict(r) for r in rows], "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/logs/webhooks", methods=["GET"])
def logs_webhooks():
    if (resp := _require_admin()) is not None:
        return resp

    limit = _limit_param()
    try:
        rows = db.session.execute(
            f"""
            SELECT id, event_id, event_type, received_at, processed_at, success, error
              FROM stripe_webhook_logs
          ORDER BY id DESC
             LIMIT {limit}
        """
        ).mappings().all()
        return jsonify({"items": [dict(r) for r in rows], "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@bp.route("/logs/actions", methods=["GET"])
def logs_actions():
    if (resp := _require_admin()) is not None:
        return resp

    limit = _limit_param()
    try:
        rows = db.session.execute(
            f"""
            SELECT id, created_at, user_id, league_id, action_type, target_week, result_ok, message
              FROM action_logs
          ORDER BY id DESC
             LIMIT {limit}
        """
        ).mappings().all()
        return jsonify({"items": [dict(r) for r in rows], "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------
# NEW: HTML Admin UI — list/search users
#  GET /_admin/users?q=...
# --------------------------------------------------------------------

ALLOWED_PLANS = ["free", "mgr5", "mgr12", "unlimited", "founder"]

@bp.route("/users", methods=["GET"])
@login_required
def users_list():
    if (resp := _require_admin()) is not None:
        return resp

    q = (request.args.get("q") or "").strip()
    qry = User.query
    if q:
        like = f"%{q}%"
        qry = qry.filter(or_(User.email.ilike(like), User.username.ilike(like)))
    users = qry.order_by(User.id.asc()).limit(50).all()

    return render_template("admin/users.html", view="list", q=q, users=users, plans=ALLOWED_PLANS)


# --------------------------------------------------------------------
# NEW: HTML Admin UI — edit user
#  GET/POST /_admin/users/<id>
#  Edits: plan, bonus_mass_offers, founder_expires_at, clear MFL auth
# --------------------------------------------------------------------

@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@login_required
def users_edit(user_id: int):
    if (resp := _require_admin()) is not None:
        return resp

    user: Optional[User] = User.query.get(user_id)
    if not user:
        flash("User not found.", "warning")
        return redirect(url_for("admin.users_list"))

    if request.method == "POST":
        # Plan
        plan = (request.form.get("plan") or "").strip().lower()
        if plan not in ALLOWED_PLANS:
            flash("Invalid plan key.", "danger")
            return redirect(url_for("admin.users_edit", user_id=user_id))

        # Bonus
        try:
            bonus = int(request.form.get("bonus_mass_offers") or "0")
            if bonus < 0:
                bonus = 0
        except Exception:
            bonus = 0

        # Founder expiry (optional YYYY-MM-DD)
        founder_str = (request.form.get("founder_expires_at") or "").strip()
        founder_dt = None
        if founder_str:
            try:
                founder_dt = datetime.strptime(founder_str, "%Y-%m-%d")
            except Exception:
                flash("Invalid founder expiration date.", "warning")

        # Clear MFL auth?
        clear_mfl = (request.form.get("clear_mfl") == "on")

        # Apply
        user.plan = plan
        user.bonus_mass_offers = bonus
        user.founder_expires_at = founder_dt

        if clear_mfl:
            user.mfl_user = None
            user.session_key = None
            user.mfl_cookie_api = None
            user.mfl_cookie_hosts_json = "{}"
            user.mfl_cookie_updated_at = None

        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("admin.users_edit", user_id=user_id))

    return render_template(
        "admin/users.html",
        view="edit",
        u=user,
        plans=ALLOWED_PLANS,
    )
