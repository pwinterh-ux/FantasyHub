"""Sleeper configuration and sync routes."""
from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, url_for, flash
from flask_login import current_user, login_required

from app import db
from models import SleeperLeague
from services.sleeper_client import SleeperClient, season_default
from services.sleeper_sync import ensure_sleeper_league, sync_league_via_client
from services.sleeper_refresh import refresh_all_sleeper_for_user  # optional bulk helper if you keep it
try:
    # reuse entitlements to detect plan (FREE vs paid)
    from services.entitlements import get_entitlements
except Exception:  # pragma: no cover - defensive import
    def get_entitlements(_user):  # type: ignore
        return {}

sleeper_bp = Blueprint("sleeper", __name__, url_prefix="/sleeper")


# ---------------------- Account linking / unlinking ---------------------- #

@sleeper_bp.route("/link", methods=["POST"])
@login_required
def link_account():
    payload = request.get_json(silent=True) or request.form or {}
    username = (payload.get("username") or "").strip()
    if not username:
        return jsonify({"ok": False, "error": "Username is required."}), 400

    client = SleeperClient()
    try:
        sleeper_user = client.get_user(username)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    current_user.sleeper_user_id = sleeper_user.user_id
    current_user.sleeper_display_name = (
        sleeper_user.display_name or sleeper_user.username or username
    )
    db.session.commit()

    return jsonify(
        {
            "ok": True,
            "user_id": sleeper_user.user_id,
            "display_name": current_user.sleeper_display_name,
        }
    )


@sleeper_bp.route("/unlink", methods=["POST"])
@login_required
def unlink_account():
    leagues = SleeperLeague.query.filter_by(user_id=current_user.id).all()
    for league in leagues:
        db.session.delete(league)
    current_user.sleeper_user_id = None
    current_user.sleeper_display_name = None
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------- Cap helpers --------------------------- #

def _sleeper_cap_for_user(user) -> tuple[bool, int | None]:
    """
    Return (is_unlimited, cap_int_or_None).
    Business rule: FREE plan -> cap 3; ANY paid plan -> unlimited.
    """
    ent = get_entitlements(user) or {}
    plan_key = (ent.get("plan_key") or ent.get("plan") or "").lower()
    if not plan_key or plan_key in {"free"}:
        return (False, 3)
    # any non-free is unlimited for Sleeper right now
    return (True, None)


# --------------------------- Config (UI) --------------------------- #

@sleeper_bp.route("/config", methods=["GET"])
@login_required
def sleeper_config():
    if not current_user.sleeper_user_id:
        flash("Link a Sleeper account first.", "warning")
        return redirect(url_for("mfl.mfl_config"))

    try:
        year = int(request.args.get("year", season_default()))
    except ValueError:
        year = season_default()

    client = SleeperClient()
    try:
        leagues_payload = client.get_user_leagues(current_user.sleeper_user_id, year)
    except Exception as exc:
        flash(f"Could not fetch Sleeper leagues: {exc}", "danger")
        return redirect(url_for("mfl.mfl_config"))

    existing_rows = SleeperLeague.query.filter_by(user_id=current_user.id, year=year).all()
    existing_ids = {row.sleeper_id: row for row in existing_rows}

    leagues = []
    seasons_found = set()
    for item in leagues_payload:
        sleeper_id = str(item.get("league_id") or item.get("leagueId") or "").strip()
        if not sleeper_id:
            continue
        season_val = item.get("season")
        try:
            season = int(season_val)
        except (TypeError, ValueError):
            season = year
        seasons_found.add(season)
        if season != year:
            continue
        name = (item.get("name") or f"League {sleeper_id}").strip()
        leagues.append(
            {
                "sleeper_id": sleeper_id,
                "name": name,
                "checked": sleeper_id in existing_ids,
            }
        )

    leagues.sort(key=lambda lg: lg["name"].lower())

    # Cap info for the template banner/help (UI hint only; enforcement in POST)
    is_unlimited, cap_val = _sleeper_cap_for_user(current_user)
    current_count = len(existing_rows)

    return render_template(
        "sleeper_config.html",
        leagues=leagues,
        year=year,
        username=current_user.sleeper_display_name or current_user.sleeper_user_id,
        has_link=True,
        seasons=sorted(seasons_found),
        # cap ui vars
        sl_is_unlimited=is_unlimited,
        sl_cap=cap_val,
        sl_current_count=current_count,
    )


@sleeper_bp.route("/config", methods=["POST"])
@login_required
def sleeper_config_submit():
    if not current_user.sleeper_user_id:
        return jsonify({"ok": False, "error": "Sleeper account not linked."}), 400

    try:
        year = int(request.form.get("year", season_default()))
    except ValueError:
        year = season_default()

    selected_ids = set(request.form.getlist("league_id"))
    name_map: dict[str, str] = {}
    for key, val in request.form.items():
        if key.startswith("league_name_"):
            league_id = key.replace("league_name_", "", 1)
            name_map[league_id] = val

    existing_rows = SleeperLeague.query.filter_by(user_id=current_user.id, year=year).all()
    existing_ids = {row.sleeper_id for row in existing_rows}

    # ---------- CAP ENFORCEMENT (server-side) ----------
    is_unlimited, cap_val = _sleeper_cap_for_user(current_user)
    if not is_unlimited:
        # final count = (existing not being removed but also selected) + new additions
        # We enforce strictly on the selection for THIS year (sleeper leagues are year-scoped).
        final_total = len(selected_ids)
        if cap_val is not None and final_total > int(cap_val):
            over_by = final_total - int(cap_val)
            msg = (
                f"Free plan allows up to {cap_val} Sleeper leagues per season. "
                f"You selected {len(selected_ids)}, which is {over_by} too many. "
                f"Please uncheck at least {over_by} league(s) or upgrade."
            )
            return jsonify({"ok": False, "error": msg}), 400

    # Delete previously saved but now unselected
    to_delete = [row for row in existing_rows if row.sleeper_id not in selected_ids]
    for row in to_delete:
        db.session.delete(row)
    db.session.commit()

    # Ensure selected leagues exist (create or update name)
    synced_targets: list[SleeperLeague] = []
    for sleeper_id in selected_ids:
        name = name_map.get(sleeper_id, f"League {sleeper_id}")
        league = ensure_sleeper_league(current_user, sleeper_id, name, year)
        synced_targets.append(league)

    db.session.commit()

    if not synced_targets:
        return jsonify({"ok": False, "error": "No leagues selected."}), 400

    leagues_payload = [
        {
            "id": league.id,               # DB id used by /sync-one
            "sleeper_id": league.sleeper_id,
            "name": league.name,
            "year": league.year,
        }
        for league in synced_targets
    ]

    return jsonify(
        {
            "ok": True,
            "leagues": leagues_payload,
            "phases": [{"key": "SYNC", "label": "Sync"}],
            "redirect_url": url_for("leagues.my_leagues"),
        }
    )


# --------------------------- Sync one league --------------------------- #

@sleeper_bp.route("/sync-one", methods=["POST"])
@login_required
def sleeper_sync_one():
    try:
        league_db_id = int(request.form.get("league_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Missing league_id", "retryable": False}), 400

    league = SleeperLeague.query.filter_by(id=league_db_id, user_id=current_user.id).first()
    if not league:
        return jsonify({"ok": False, "error": "League not found", "retryable": False}), 404

    client = SleeperClient()
    try:
        metrics = sync_league_via_client(league, client, site_user=current_user)
    except Exception as exc:
        db.session.rollback()
        return jsonify({"ok": False, "error": str(exc), "retryable": True}), 502

    return jsonify({"ok": True, "metrics": metrics})


# ----------------- List saved leagues (for refresh progress) ----------------- #

@sleeper_bp.route("/list", methods=["GET"])
@login_required
def list_leagues():
    """
    Return saved Sleeper leagues (db id + name + sleeper_id + year) for the current user.
    Optional filter: ?year=YYYY
    Used by the template's 'Refresh assets' progress to drive per-league updates.
    """
    year_param = request.args.get("year")
    query = SleeperLeague.query.filter_by(user_id=current_user.id)
    if year_param not in (None, ""):
        try:
            yr = int(year_param)
            query = query.filter_by(year=yr)
        except ValueError:
            pass

    rows = query.order_by(SleeperLeague.name.asc()).all()
    leagues = [
        {
            "id": row.id,  # DB id expected by /sync-one
            "sleeper_id": row.sleeper_id,
            "name": row.name or f"League {row.sleeper_id}",
            "year": row.year,
        }
        for row in rows
    ]
    return jsonify({"ok": True, "leagues": leagues})


# ------------------ Bulk refresh (server-side summary) ------------------ #
# (optional; UI uses /list + /sync-one for progress)

@sleeper_bp.route("/refresh-assets", methods=["POST"])
@login_required
def refresh_assets():
    """
    Refresh rosters + future picks for all Sleeper leagues linked to the current user.
    Server returns a summary only; the UI progress wheel should call /list and then
    /sync-one per league to show step-by-step progress.
    """
    try:
        result = refresh_all_sleeper_for_user(current_user)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 502

    return jsonify(
        {
            "ok": True,
            "refreshed": result.get("refreshed", 0),
            "errors": result.get("errors", []),
        }
    )
