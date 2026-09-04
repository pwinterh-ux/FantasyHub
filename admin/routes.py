"""
Admin routes (merged JSON tools + HTML UI)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import os
import subprocess
import sys
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
from sqlalchemy import or_, text

from app import db
from models import User
from . import bp


# --------------------------------------------------------------------
# Admin guard
# --------------------------------------------------------------------

def _require_admin():
    if not getattr(current_user, "is_authenticated", False):
        abort(401)
    if not bool(getattr(current_user, "is_admin", False)):
        abort(403)
    return None


# --------------------------------------------------------------------
# Health
# --------------------------------------------------------------------

@bp.route("/health", methods=["GET"])
def health():
    if (resp := _require_admin()) is not None:
        return resp
    return jsonify({"ok": True, "version": current_app.config.get("APP_VERSION", "dev")})


# --------------------------------------------------------------------
# Grant bonus mass-offers
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
            text(
                """
                UPDATE users
                   SET bonus_mass_offers = COALESCE(bonus_mass_offers, 0) + :count
                 WHERE id = :user_id
                """
            ),
            {"count": count, "user_id": user_id},
        )
        db.session.commit()
        return jsonify({"ok": True, "user_id": user_id, "added": count})
    except Exception as e:
        current_app.logger.exception("grant_bonus failed")
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------
# Log viewers
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
            text(
                """
                SELECT id, created_at, user_id, league_id, host, method, endpoint,
                       status_code, response_ms, ok, throttled, message
                  FROM api_call_logs
              ORDER BY id DESC
                 LIMIT :limit
                """
            ),
            {"limit": limit},
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
            text(
                """
                SELECT id, event_id, event_type, received_at, processed_at, success, error
                  FROM stripe_webhook_logs
              ORDER BY id DESC
                 LIMIT :limit
                """
            ),
            {"limit": limit},
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
            text(
                """
                SELECT id, created_at, user_id, league_id, action_type, target_week, result_ok, message
                  FROM action_logs
              ORDER BY id DESC
                 LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()

        return jsonify({"items": [dict(r) for r in rows], "limit": limit})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --------------------------------------------------------------------
# Admin UI
# --------------------------------------------------------------------

ALLOWED_PLANS = ["free", "mgr5", "mgr12", "unlimited", "founder"]


@bp.route("/", methods=["GET"])
@login_required
def admin_home():
    if (resp := _require_admin()) is not None:
        return resp
    return render_template("admin/index.html")


# --------------------------------------------------------------------
# Refresh dynasty rankings
# --------------------------------------------------------------------

def _project_root() -> Path:
    """
    current_app.root_path should normally be the repo/app root on PythonAnywhere.
    This keeps subprocess execution anchored to the same codebase as the web app.
    """
    return Path(current_app.root_path).resolve()


def _rankings_refresh_cmd() -> list[str]:
    """
    Launch the rankings refresh with the actual Python interpreter.

    On PythonAnywhere, sys.executable inside the web process points to
    /usr/local/bin/uwsgi, so it must not be used to launch a Python CLI.
    sys.prefix points at the configured Python/virtualenv environment.
    """
    root = _project_root()
    script = root / "rankings" / "refresh_dynasty_ranks.py"

    if not script.exists():
        raise RuntimeError(
            f"Rankings refresh script not found: {script}"
        )

    python_exe = Path(sys.prefix) / "bin" / "python"

    if not python_exe.exists():
        python_exe = Path(sys.prefix) / "bin" / "python3"

    if not python_exe.exists():
        raise RuntimeError(
            f"Python interpreter not found under sys.prefix={sys.prefix}"
        )

    return [
        str(python_exe),
        str(script),
        "--source",
        "all",
    ]


@bp.route("/refresh-rankings", methods=["POST"])
@login_required
def refresh_rankings():
    if (resp := _require_admin()) is not None:
        return resp

    root = _project_root()
    cmd = _rankings_refresh_cmd()

    env = os.environ.copy()
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")

    current_app.logger.info("Starting dynasty rankings refresh: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        current_app.logger.exception("Dynasty rankings refresh timed out")
        flash("Rankings refresh failed: timed out after 3 minutes.", "danger")
        return redirect(url_for("admin.admin_home"))
    except Exception as exc:
        current_app.logger.exception("Failed to start dynasty rankings refresh")
        flash(f"Failed to start rankings refresh: {exc}", "danger")
        return redirect(url_for("admin.admin_home"))

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    combined = "\n".join(x for x in [stdout, stderr] if x).strip()

    if proc.returncode == 0:
        detail = combined.splitlines()[-1] if combined else "Completed."
        current_app.logger.info("Dynasty rankings refresh completed: %s", detail)
        flash("Rankings refresh completed successfully.", "success")
    else:
        detail = combined.splitlines()[-1] if combined else f"Exit code {proc.returncode}"
        current_app.logger.error(
            "Dynasty rankings refresh failed. returncode=%s stdout=%s stderr=%s",
            proc.returncode,
            stdout,
            stderr,
        )
        flash(f"Rankings refresh failed: {detail}", "danger")

    return redirect(url_for("admin.admin_home"))


# --------------------------------------------------------------------
# Users list/search
# --------------------------------------------------------------------

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

    try:
        page = max(int(request.args.get("page", "1")), 1)
    except Exception:
        page = 1

    per_page = 50
    offset = (page - 1) * per_page

    results = (
        qry.order_by(User.id.asc())
        .offset(offset)
        .limit(per_page + 1)
        .all()
    )

    has_next = len(results) > per_page
    if has_next:
        results = results[:per_page]

    return render_template(
        "admin/users.html",
        view="list",
        q=q,
        users=results,
        plans=ALLOWED_PLANS,
        page=page,
        has_next=has_next,
        has_prev=page > 1,
    )


# --------------------------------------------------------------------
# User edit
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
        plan = (request.form.get("plan") or "").strip().lower()
        if plan not in ALLOWED_PLANS:
            flash("Invalid plan key.", "danger")
            return redirect(url_for("admin.users_edit", user_id=user_id))

        try:
            bonus = int(request.form.get("bonus_mass_offers") or "0")
            if bonus < 0:
                bonus = 0
        except Exception:
            bonus = 0

        founder_str = (request.form.get("founder_expires_at") or "").strip()
        founder_dt = None
        if founder_str:
            try:
                founder_dt = datetime.strptime(founder_str, "%Y-%m-%d")
            except Exception:
                flash("Invalid founder expiration date.", "warning")

        clear_mfl = request.form.get("clear_mfl") == "on"

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