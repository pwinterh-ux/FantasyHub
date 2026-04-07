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
TAXI_PREFIX = "TAXI"

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


# -------- TAXI helpers --------

def _normalize_slot(raw: Any) -> str:
    s = str(raw or "").strip().upper()
    if s in {"T", "TAXI"} or s.startswith(TAXI_PREFIX):
        return "TAXI"
    return s

def _extract_taxi_ids(placements: Dict[str, Any]) -> Set[str]:
    """
    placements shape (from mfl_ir_client.sync_in_ir_flags):
      { "1234": "ROSTER" | "IR" | "TAXI_SQUAD" | ... }  (or dict rows in the future)
    """
    taxi: Set[str] = set()
    if not isinstance(placements, dict):
        return taxi
    for pid, v in placements.items():
        if isinstance(v, str):
            if "TAXI" in v.upper():
                taxi.add(str(pid))
        elif isinstance(v, dict):
            slot = _normalize_slot(v.get("slot") or v.get("status") or v.get("roster_slot"))
            if slot.startswith(TAXI_PREFIX):
                taxi.add(str(pid))
    return taxi

def _count_ir_now(placements: Dict[str, Any]) -> int:
    """Count players currently occupying IR slot (Taxi never counted)."""
    if not isinstance(placements, dict):
        return 0
    total = 0
    for v in placements.values():
        if isinstance(v, str):
            if v.upper() == "IR":
                total += 1
        elif isinstance(v, dict):
            slot = _normalize_slot(v.get("slot") or v.get("status") or v.get("roster_slot"))
            if slot == "IR":
                total += 1
    return total

# --------------------------------


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

    ir_eligible_ids_all = _eligible_ids(injuries_map)  # strings

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

        host = _league_host(league) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)

        # 1) Live placements FIRST so Taxi is known
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
            skipped.append({"league": league.name, "reason": f"Roster sync failed: {exc}"})
            continue

        taxi_ids = _extract_taxi_ids(placements)                 # set of strings
        rostered_non_taxi = {str(pid) for pid in rostered_ids if str(pid) not in taxi_ids}

        # 2) Build IR-eligible set with Taxi removed (so planning never sees Taxi)
        ir_eligible_ids = {pid for pid in ir_eligible_ids_all if pid in rostered_non_taxi}

        # 3) Plan with Taxi-free eligible ids. This still lets us activate
        # players off IR when they are no longer eligible, even if nobody on
        # the current roster qualifies to move onto IR.
        plan = ir_optimizer.plan_for_league(
            league=league,
            franchise_id=franchise_id,
            ir_eligible_ids=ir_eligible_ids,   # already stripped of Taxi
        )

        # 4) Replace plan.activate with Taxi-filtered version (belt-and-suspenders)
        activate_filtered = [pid for pid in plan.activate if str(pid) not in taxi_ids]

        # If nothing to change after Taxi filtering, skip
        if not activate_filtered and not plan.deactivate:
            skipped.append({"league": league.name, "reason": "No IR changes needed"})
            continue

        # 5) Accurate IR usage for display (Taxi not counted)
        ir_used_now = _count_ir_now(placements)
        ir_used_after_display = ir_used_now - len(activate_filtered) + len(plan.deactivate)

        notes: List[str] = []
        filtered_out_count = len(plan.activate) - len(activate_filtered)
        if filtered_out_count > 0:
            notes.append(f"Excluded {filtered_out_count} Taxi player(s) from IR activation.")

        # 6) Submit with filtered activation list only
        try:
            import_result: Dict[str, Any] = import_ir(
                league=league,
                franchise_id=franchise_id,
                host=host,
                cookie=cookie,
                activate_ids=activate_filtered,
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
                # Update local flags only for what we actually sent
                original_activate = list(getattr(plan, "activate", []))
                try:
                    plan.activate = activate_filtered
                    ir_optimizer.flip_ir_flags(plan)
                finally:
                    plan.activate = original_activate
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

        # 7) Resolve rows for display (already filtered for activate)
        activated_rows = ir_optimizer.resolve_player_rows(plan, activate_filtered)
        deactivated_rows = ir_optimizer.resolve_player_rows(plan, plan.deactivate)

        summary_rows.append(
            {
                "league": league,
                "ir_used_after": ir_used_after_display,   # Taxi-free
                "ir_slots_max": _max_slots(league),
                "activated": activated_rows,
                "deactivated": deactivated_rows,
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
