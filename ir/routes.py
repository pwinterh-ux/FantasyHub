"""Hidden IR optimizer endpoint (MFL only)."""
from __future__ import annotations

from typing import Any, Dict, List, Set

from flask import Blueprint, current_app, flash, render_template
from flask_login import current_user, login_required

from app import db
from models import League
from lineups.routes import (
    _cookie_header_for_host,
    _effective_current_week,
    _league_host,
    _pick_year_for_week_lookup,
)
from injuries.routes import _fetch_injuries
from services import ir_optimizer
from services.mfl_ir_client import import_ir, sync_in_ir_flags

IR_STATUSES = {"IR", "IR-PUP", "IR-NFI", "IR-R"}

ir_bp = Blueprint("ir", __name__, url_prefix="/ir", template_folder="../templates")


def _eligible_ids(injuries: Dict[str, Dict[str, Any]]) -> Set[str]:
    eligible: Set[str] = set()
    for pid, info in injuries.items():
        status = str(info.get("status") or "").strip().upper()
        if status in IR_STATUSES:
            eligible.add(str(pid))
    return eligible


def _max_slots(league: League) -> int:
    try:
        return max(0, int(league.ir_slots_max or 0))
    except (TypeError, ValueError):
        return 0


@ir_bp.route("/", methods=["GET"])
@login_required
def ir_index():
    year = _pick_year_for_week_lookup()
    week = _effective_current_week(year)

    injuries_map: Dict[str, Dict[str, Any]] = {}
    try:
        injuries_map, _ = _fetch_injuries(year, week)
    except Exception:
        current_app.logger.exception("IR optimizer: failed to fetch current injuries")
        flash("Could not refresh the current-week injuries feed. Using an empty set.", "warning")
        injuries_map = {}

    ir_eligible_ids = _eligible_ids(injuries_map)

    leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.name.asc())
        .all()
    )

    summary_rows: List[dict[str, Any]] = []
    skipped: List[dict[str, str]] = []

    for league in leagues:
        franchise_id = str(getattr(league, "franchise_id", "") or "").strip()
        if not franchise_id:
            skipped.append({"league": league.name, "reason": "No franchise configured"})
            continue

        rostered_ids = ir_optimizer.get_rostered_player_ids(league.id, franchise_id)
        if not rostered_ids:
            skipped.append({"league": league.name, "reason": "No roster synced"})
            continue

        if not (rostered_ids & ir_eligible_ids):
            skipped.append({"league": league.name, "reason": "No IR-eligible injuries"})
            continue

        host = _league_host(league) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)

        try:
            placements = sync_in_ir_flags(
                league=league,
                franchise_id=franchise_id,
                host=host,
                cookie=cookie,
            )
        except Exception as exc:
            current_app.logger.exception(
                "IR optimizer: roster sync failed", extra={"league_id": league.id, "franchise": franchise_id}
            )
            summary_rows.append(
                {
                    "league": league,
                    "ir_used_after": None,
                    "ir_slots_max": _max_slots(league),
                    "activated": [],
                    "deactivated": [],
                    "notes": [f"Roster sync failed: {exc}"],
                    "placements": {},
                }
            )
            continue

        plan = ir_optimizer.plan_for_league(
            league=league,
            franchise_id=franchise_id,
            ir_eligible_ids=ir_eligible_ids,
        )

        if not plan.has_changes:
            skipped.append({"league": league.name, "reason": "No IR changes needed"})
            continue

        notes: List[str] = []
        import_result: Dict[str, Any]
        try:
            import_result = import_ir(
                league=league,
                franchise_id=franchise_id,
                host=host,
                cookie=cookie,
                activate_ids=plan.activate,
                deactivate_ids=plan.deactivate,
            )
        except Exception as exc:
            current_app.logger.exception(
                "IR optimizer: IR import failed", extra={"league_id": league.id, "franchise": franchise_id}
            )
            notes.append(f"IR import failed: {exc}")
            import_result = {"ok": False, "payload": {}}
        else:
            if import_result.get("ok"):
                ir_optimizer.flip_ir_flags(plan)
            else:
                status_code = import_result.get("status_code")
                payload = import_result.get("payload")
                notes.append(
                    "IR import was rejected by MFL"
                    + (f" (status {status_code})" if status_code not in (None, "") else "")
                )
                if isinstance(payload, dict):
                    message = payload.get("error") or payload.get("message") or payload.get("status")
                    if message:
                        notes.append(str(message))

        summary_rows.append(
            {
                "league": league,
                "ir_used_after": plan.ir_used_after,
                "ir_slots_max": _max_slots(league),
                "activated": ir_optimizer.resolve_player_rows(plan, plan.activate),
                "deactivated": ir_optimizer.resolve_player_rows(plan, plan.deactivate),
                "notes": notes,
                "placements": placements,
            }
        )

    return render_template(
        "ir/index.html",
        summary=summary_rows,
        skipped=skipped,
        statuses=sorted(IR_STATUSES),
    )