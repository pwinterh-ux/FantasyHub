from __future__ import annotations

import concurrent.futures
import re
from datetime import datetime
from typing import Dict, List, Tuple, Optional

import requests
from flask import (
    Blueprint,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    current_app,
    session,
    jsonify,
)
from flask_login import login_required, current_user

from app import db
from models import League, Team, Player, Roster

# Service helpers
from services.lineups_service import (
    get_my_team_player_ids,
    fetch_projected_scores,
    submit_lineup,
    build_players_for_review,
    group_and_sort_players_for_review,
    # rapid helpers
    parse_lineup_requirements,
    pick_optimal_lineup,
)

lineups_bp = Blueprint("lineups", __name__, template_folder="../templates")

# -------------------------- Config / knobs ----------------------------------

MFL_MAX_WEEKS_FALLBACK = 18
PARALLEL_WORKERS = 3  # mirror other mass API calls (tweak to 2-3 as desired)

# -------------------------- Host & cookies ----------------------------------

def _norm_host(h: Optional[str]) -> Optional[str]:
    if not h:
        return None
    h = h.strip()
    h = re.sub(r"^https?://", "", h).rstrip("/")
    return h or None

def _league_host(league: League) -> Optional[str]:
    # Prefer explicit host fields you persist
    host = getattr(league, "league_host", None) or getattr(league, "host", None)
    if host:
        return _norm_host(host)

    # Else infer from baseURL if present
    base_url = getattr(league, "base_url", None) or getattr(league, "baseURL", None)
    if base_url:
        m = re.match(r"^https?://([^/]+)/?", str(base_url).strip())
        if m:
            return m.group(1)

    # Fallback shared host
    return "api.myfantasyleague.com"

def _cookie_header_for_host(host: str) -> Optional[str]:
    """
    Build a Cookie header string for the given host, reusing the same logic
    you use in the trade flow.
    """
    host = _norm_host(host) or ""
    # If your User model exposes a helper, prefer that:
    try:
        if hasattr(current_user, "get_mfl_cookie_header"):
            s = current_user.get_mfl_cookie_header(host)  # type: ignore[attr-defined]
            if s:
                return str(s)
    except Exception:
        pass

    # Legacy fallbacks
    for attr in ("mfl_cookie_api", "mfl_cookie"):
        v = getattr(current_user, attr, None)
        if isinstance(v, dict) and v:
            return "; ".join(f"{k}={val}" for k, val in v.items())
        if isinstance(v, str) and v:
            return v

    for attr in ("session_key", "mfl_session"):
        v = getattr(current_user, attr, None)
        if isinstance(v, str) and v:
            return f"MFLSESSION={v}"

    return None

# ----------------------- Sync gate (reuse if present) -----------------------

def _require_recent_sync_or_gate():
    """
    Use the same 4hr sync gate as the Offers flow. That function returns either:
      - None (allowed), or
      - a rendered response (gate page / redirect) to return immediately.
    """
    try:
        from offers.routes import _require_recent_sync_or_gate as offers_gate
        return offers_gate()
    except Exception:
        # If offers module isn't available, allow through.
        return None

# ----------------------- Current week discovery -----------------------------

def _pick_year_for_week_lookup() -> int:
    row = (
        db.session.query(League.year)
        .filter(League.user_id == current_user.id)
        .order_by(League.year.desc())
        .first()
    )
    return int(row[0]) if row and row[0] else datetime.now().year

def _get_current_mfl_week(year: int) -> int:
    cfg_week = current_app.config.get("MFL_CURRENT_WEEK")
    if isinstance(cfg_week, int) and 1 <= cfg_week <= 22:
        return cfg_week
    try:
        url = f"https://api.myfantasyleague.com/{year}/export"
        params = {"TYPE": "nflSchedule", "JSON": "1"}
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        wk = (
            data.get("nflSchedule", {}).get("currentWeek")
            or data.get("currentWeek")
            or data.get("week")
        )
        wk_i = int(str(wk))
        if 1 <= wk_i <= 22:
            return wk_i
    except Exception:
        pass
    return int(current_app.config.get("MFL_WEEK_FALLBACK", 1))

def _effective_current_week(year: int) -> int:
    cfg_week = current_app.config.get("MFL_CURRENT_WEEK")
    try:
        forced_week = int(cfg_week)
    except (TypeError, ValueError):
        forced_week = None
    if forced_week and 1 <= forced_week <= 22:
        return forced_week

    try:
        wk = int(current_app.config.get("MFL_WEEK_FALLBACK", 2))
    except (TypeError, ValueError):
        wk = 2
    if wk < 1:
        wk = 1

    try:
        now = datetime.now()
    except Exception:
        now = None

    if now:
        if wk < 2 and now.month == 9 and now.day >= 8:
            wk = 2

        week3_start = None
        try:
            week3_start = datetime(year, 9, 16)
        except Exception:
            pass
        if week3_start and now >= week3_start:
            delta_weeks = (now - week3_start).days // 7
            wk = max(wk, 3 + delta_weeks)

    minwk = current_app.config.get("MFL_MIN_CURRENT_WEEK")
    if isinstance(minwk, int) and 1 <= minwk <= 22:
        wk = max(wk, minwk)

    try:
        max_week = int(current_app.config.get("MFL_MAX_WEEKS", MFL_MAX_WEEKS_FALLBACK))
    except (TypeError, ValueError):
        max_week = MFL_MAX_WEEKS_FALLBACK
    if max_week < 1:
        max_week = MFL_MAX_WEEKS_FALLBACK

    return max(1, min(wk, max_week))


def _allowed_weeks_from(current_week: int, max_week: int) -> List[int]:
    if current_week < 1:
        current_week = 1
    if max_week < current_week:
        max_week = current_week
    return list(range(current_week, max_week + 1))

# ----------------------------- Utilities ------------------------------------

def _user_synced_leagues() -> list[League]:
    # Scope strictly to the current user's leagues
    return (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )

def _parallel_map(func, items, max_workers=PARALLEL_WORKERS):
    if max_workers and max_workers > 1 and len(items) > 1:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            return list(ex.map(func, items))
    return [func(x) for x in items]

# ---------------------- MFL status parsing (strip XML) ----------------------

_STATUS_OK_RE = re.compile(r"<\s*status\s*>\s*OK\s*<\s*/\s*status\s*>", re.I)

def _clean_mfl_message(text: str) -> str:
    """Strip XML/HTML and keep meaningful text."""
    if not text:
        return ""
    t = text
    # remove xml decl
    t = re.sub(r"<\?xml[^>]*\?>", "", t, flags=re.I).strip()
    # remove status tag itself (we infer success via code paths)
    t = re.sub(r"<\s*/?\s*status\s*>", "", t, flags=re.I)
    # now strip any remaining tags
    t = re.sub(r"<[^>]+>", " ", t)
    # collapse whitespace
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _is_ok_payload(text: str) -> bool:
    if not text:
        return False
    return bool(_STATUS_OK_RE.search(text)) or text.strip().upper() == "OK"

# ============================= Index (two tiles) =============================

@lineups_bp.route("/lineups", methods=["GET", "POST"])
@login_required
def lineups_index():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    year = _pick_year_for_week_lookup()
    current_week = _effective_current_week(year)
    max_week = int(current_app.config.get("MFL_MAX_WEEKS", MFL_MAX_WEEKS_FALLBACK))
    weeks = _allowed_weeks_from(current_week, max_week)

    # index.html still shows both tiles: batch review and rapid flow
    return render_template("lineups/index.html", weeks=weeks, selected_week=current_week)

# ============================ Batch flow (classic) ===========================

@lineups_bp.route("/lineups/review", methods=["POST"])
@login_required
def lineups_review():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    week = request.form.get("week") or request.args.get("week")
    try:
        week_i = int(str(week))
    except Exception:
        flash("Please select a valid week.", "warning")
        return redirect(url_for("lineups.lineups_index"))

    leagues = _user_synced_leagues()
    if not leagues:
        flash("No synced leagues found.", "warning")
        return redirect(url_for("lineups.lineups_index"))

    # MAIN THREAD: gather DB + cookie data up front
    jobs: List[dict] = []
    for lg in leagues:
        # Hard owner check
        if getattr(lg, "user_id", None) != current_user.id:
            continue

        host = _league_host(lg) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)

        # Roster from DB
        players = build_players_for_review(lg.id)  # [(pid, name, pos, team)]
        pid_list = [pid for (pid, _, _, _) in players]

        # My team name (by franchise match only)
        my_team_name = None
        try:
            team = (
                db.session.query(Team)
                .filter(Team.league_id == lg.id, Team.mfl_id == lg.franchise_id)
                .first()
            )
            my_team_name = team.name if team else None
        except Exception:
            pass

        jobs.append(dict(
            league=lg,
            host=host,
            cookie=cookie,
            players=players,
            pid_list=pid_list,
            my_team_name=my_team_name,
            starters_label=(getattr(lg, "roster_slots", None) or ""),
        ))

    # THREADS: network projections only
    def _net_fetch(job: dict):
        lg: League = job["league"]
        proj_map = fetch_projected_scores(
            job["host"], lg.mfl_id, lg.year, week_i, job["pid_list"], cookie=job["cookie"]
        )
        return (lg.id, proj_map)

    proj_by_league_id = dict(_parallel_map(_net_fetch, jobs, max_workers=PARALLEL_WORKERS))

    # MAIN THREAD: assemble view model
    items: List[Dict[str, object]] = []
    for job in jobs:
        lg: League = job["league"]
        grouped = group_and_sort_players_for_review(job["players"], proj_by_league_id.get(lg.id, {}))
        items.append(dict(
            league=lg,
            host=job["host"],
            my_team_name=job["my_team_name"],
            starters_label=job["starters_label"],  # raw from DB (may include total prefix)
            grouped_players=grouped,
            flat_players=job["players"],
        ))

    return render_template("lineups/review.html", week=week_i, items=items)


@lineups_bp.route("/lineups/submit", methods=["POST"])
@login_required
def lineups_submit():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    try:
        week_i = int(str(request.form.get("week")))
    except Exception:
        flash("Missing or invalid week.", "warning")
        return redirect(url_for("lineups.lineups_index"))

    leagues = _user_synced_leagues()
    if not leagues:
        flash("No synced leagues to submit.", "warning")
        return redirect(url_for("lineups.lineups_index"))

    # Collect selected starters per league from the form + an include checkbox
    selections: Dict[int, List[int]] = {}
    includes: Dict[int, bool] = {}
    for lg in leagues:
        key = f"starters_{lg.id}"
        vals = request.form.getlist(f"{key}[]") or request.form.getlist(key)
        picked: List[int] = []
        for v in vals:
            try:
                picked.append(int(str(v)))
            except Exception:
                continue
        selections[lg.id] = picked
        includes[lg.id] = (request.form.get(f"include_{lg.id}") == "1")

    # MAIN THREAD: capture host + cookie + guards up front
    jobs: List[dict] = []
    for lg in leagues:
        # Skip if league wasn't explicitly included
        if not includes.get(lg.id, False):
            continue

        # Hard owner check (skip anything not owned by this user)
        if getattr(lg, "user_id", None) != current_user.id:
            jobs.append(dict(
                league=lg, host=None, cookie=None, starters=[],
                force_result=dict(ok=False, message="Skipped: league not owned by current user")
            ))
            continue

        host = _league_host(lg) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)

        # Intersect submitted starters with my roster to prevent injected IDs
        allowed_ids = set(get_my_team_player_ids(lg.id))
        submitted = selections.get(lg.id, [])
        starters = [pid for pid in submitted if pid in allowed_ids]

        if not starters:
            # Don't send an empty lineup (avoids clearing)
            jobs.append(dict(
                league=lg, host=None, cookie=None, starters=[],
                force_result=dict(ok=False, message="Skipped: no starters selected")
            ))
            continue

        jobs.append(dict(league=lg, host=host, cookie=cookie, starters=starters, force_result=None))

    if not jobs:
        flash("No leagues selected to submit. Check the 'Include' box for any league you want to submit.", "warning")
        return redirect(url_for("lineups.lineups_index"))

    # THREADS: only network submission (or return forced result)
    def _submit_one(job: dict) -> Dict[str, object]:
        lg: League = job["league"]
        # Forced result (not owned / no starters)
        if job.get("force_result"):
            fr = job["force_result"]
            return dict(league=lg, ok=fr["ok"], message=fr["message"])
        ok, raw = submit_lineup(job["host"], lg.mfl_id, lg.year, week_i, job["starters"], cookie=job["cookie"])
        # raw may include XML; keep as-is for batch page (legacy)
        return dict(league=lg, ok=ok, message=raw or ("Lineup submitted successfully" if ok else "Unknown response"))

    results = _parallel_map(_submit_one, jobs, max_workers=PARALLEL_WORKERS)

    return render_template("lineups/summary.html", week=week_i, results=results)

# ============================ Rapid flow (one-by-one) ========================

@lineups_bp.route("/lineups/rapid", methods=["GET", "POST"])
@login_required
def lineups_rapid_start():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    if request.method == "POST":
        try:
            week_i = int(str(request.form.get("week")))
        except Exception:
            flash("Please select a valid week.", "warning")
            return redirect(url_for("lineups.lineups_rapid_start"))

        leagues = _user_synced_leagues()
        queue = [lg.id for lg in leagues if getattr(lg, "user_id", None) == current_user.id]
        if not queue:
            flash("No synced leagues found.", "warning")
            return redirect(url_for("lineups.lineups_index"))

        session["rapid_week"] = week_i
        session["rapid_queue"] = queue
        session["rapid_idx"] = 0
        session.pop("lineups_rapid_events", None)
        session["lineups_rapid_total"] = len(queue)
        session["lineups_rapid_success"] = 0
        session.modified = True
        return redirect(url_for("lineups.lineups_rapid_league"))

    year = _pick_year_for_week_lookup()
    current_week = _effective_current_week(year)
    max_week = int(current_app.config.get("MFL_MAX_WEEKS", MFL_MAX_WEEKS_FALLBACK))
    weeks = _allowed_weeks_from(current_week, max_week)
    return render_template("lineups/rapid_start.html", weeks=weeks, selected_week=current_week)


@lineups_bp.route("/lineups/rapid/league", methods=["GET"])
@login_required
def lineups_rapid_league():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    queue: List[int] = session.get("rapid_queue") or []
    idx: int = int(session.get("rapid_idx") or 0)
    week_i: Optional[int] = session.get("rapid_week")

    if not queue or week_i is None or idx >= len(queue):
        return redirect(url_for("lineups.lineups_rapid_finish"))

    league_id_pk = queue[idx]
    lg: League | None = db.session.get(League, league_id_pk)
    if not lg or getattr(lg, "user_id", None) != current_user.id:
        session["rapid_idx"] = idx + 1
        session.modified = True
        return redirect(url_for("lineups.lineups_rapid_league"))

    host = _league_host(lg) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)

    players = build_players_for_review(lg.id)
    pid_list = [pid for (pid, _, _, _) in players]
    proj_map = fetch_projected_scores(host, lg.mfl_id, lg.year, week_i, pid_list, cookie=cookie)

    starters_label = getattr(lg, "roster_slots", None) or ""
    total_required, ranges = parse_lineup_requirements(starters_label)
    auto_ids = pick_optimal_lineup(players, proj_map, total_required, ranges)
    grouped = group_and_sort_players_for_review(players, proj_map)

    my_team_name = None
    try:
        team = (
            db.session.query(Team)
            .filter(Team.league_id == lg.id, Team.mfl_id == lg.franchise_id)
            .first()
        )
        my_team_name = team.name if team else None
    except Exception:
        pass

    return render_template(
        "lineups/rapid_league.html",
        week=week_i,
        league=lg,
        my_team_name=my_team_name,
        starters_label=starters_label,
        total_required=total_required,
        ranges=ranges,
        grouped_players=grouped,
        auto_selected=set(auto_ids),
        index=idx + 1,
        total_leagues=len(queue),
    )


def _record_rapid_event(league: League, status: str, message: str):
    """
    Append an event to session for the finish page, grouped per league.
    status: 'submitted' | 'error' | 'skipped'
    """
    events = session.get("lineups_rapid_events") or []
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events.append({
        "league_id": league.id,
        "league_name": league.name,
        "status": status,
        "message": _clean_mfl_message(message or ""),
        "ts": ts,
    })
    session["lineups_rapid_events"] = events

    # update counters
    if status == "submitted":
        session["lineups_rapid_success"] = int(session.get("lineups_rapid_success") or 0) + 1
    session.modified = True


@lineups_bp.route("/lineups/rapid/submit", methods=["POST"])
@login_required
def lineups_rapid_submit():
    gate = _require_recent_sync_or_gate()
    if gate:
        return jsonify({"ok": False, "message": "Sync required. Please refresh leagues.", "next": False}), 400

    try:
        league_id_pk = int(str(request.form.get("league_id")))
        week_i = int(str(request.form.get("week")))
    except Exception:
        return jsonify({"ok": False, "message": "Invalid request.", "next": False}), 400

    lg: League | None = db.session.get(League, league_id_pk)
    if not lg or getattr(lg, "user_id", None) != current_user.id:
        return jsonify({"ok": False, "message": "League not found or not owned by you.", "next": False}), 404

    vals = request.form.getlist("starters[]") or request.form.getlist("starters")
    submitted: List[int] = []
    for v in vals:
        try:
            submitted.append(int(str(v)))
        except Exception:
            continue

    allowed_ids = set(get_my_team_player_ids(lg.id))
    starters = [pid for pid in submitted if pid in allowed_ids]
    if not starters:
        _record_rapid_event(lg, "error", "No starters selected.")
        return jsonify({"ok": False, "message": "No starters selected.", "next": False}), 400

    host = _league_host(lg) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)

    ok, raw = submit_lineup(host, lg.mfl_id, lg.year, week_i, starters, cookie=cookie)
    msg = raw or ("OK" if ok else "Failed")

    queue: List[int] = session.get("rapid_queue") or []
    idx: int = int(session.get("rapid_idx") or 0)

    if ok or _is_ok_payload(raw or ""):
        _record_rapid_event(lg, "submitted", msg)
        # Success: advance pointer, keep order
        session["rapid_idx"] = min(idx + 1, len(queue))
        session.modified = True
    else:
        # Failure: move this league to the back; keep idx so next league is shown
        _record_rapid_event(lg, "error", msg)
        if idx < len(queue):
            curr = queue[idx]
            del queue[idx]
            queue.append(curr)
            session["rapid_queue"] = queue
            session.modified = True

    next_exists = (session.get("rapid_idx", 0) < len(session.get("rapid_queue") or []))
    return jsonify({
        "ok": bool(ok or _is_ok_payload(raw or "")),
        "message": _clean_mfl_message(msg),
        "next": next_exists,
        "requeued": (not ok)
    })


@lineups_bp.route("/lineups/rapid/skip", methods=["POST"])
@login_required
def lineups_rapid_skip():
    queue: List[int] = session.get("rapid_queue") or []
    idx: int = int(session.get("rapid_idx") or 0)
    # record skip for current league (if any)
    if queue and idx < len(queue):
        league_id_pk = queue[idx]
        lg: League | None = db.session.get(League, league_id_pk)
        if lg:
            _record_rapid_event(lg, "skipped", "Skipped by user.")
        session["rapid_idx"] = idx + 1
        session.modified = True
    next_exists = (session.get("rapid_idx", 0) < len(queue))
    return jsonify({"ok": True, "message": "Skipped.", "next": next_exists})


@lineups_bp.route("/lineups/rapid/finish")
@login_required
def lineups_rapid_finish():
    week = session.get("rapid_week")
    total = session.get("lineups_rapid_total", 0)
    success = session.get("lineups_rapid_success", 0)
    events = session.get("lineups_rapid_events") or []

    # Group by league
    grouped: Dict[int, Dict[str, object]] = {}
    for e in events:
        lid = int(e.get("league_id"))
        if lid not in grouped:
            grouped[lid] = {
                "league_id": lid,
                "league_name": e.get("league_name") or str(lid),
                "events": [],
            }
        grouped[lid]["events"].append({
            "status": e.get("status"),
            "message": e.get("message"),
            "ts": e.get("ts"),
        })

    # Counts
    submitted_count = sum(1 for e in events if e.get("status") == "submitted")
    error_count     = sum(1 for e in events if e.get("status") == "error")
    skipped_count   = sum(1 for e in events if e.get("status") == "skipped")

    # Clear session keys used for rapid flow (keep toast persistence to browser storage)
    session.pop("rapid_week", None)
    session.pop("rapid_queue", None)
    session.pop("rapid_idx", None)
    session.pop("lineups_rapid_total", None)
    session.pop("lineups_rapid_success", None)
    session.pop("lineups_rapid_events", None)

    return render_template(
        "lineups/rapid_finish.html",
        week=week,
        total_leagues=total,
        submitted_count=submitted_count,
        error_count=error_count,
        skipped_count=skipped_count,
        grouped_events=list(grouped.values()),
    )

# ----------------------------- Keep-alive -----------------------------------

@lineups_bp.route("/lineups/ping", methods=["GET"])
@login_required
def lineups_ping():
    return jsonify({"ok": True})

# ============================ Single-League flow =============================

@lineups_bp.route("/lineups/league/<int:league_id>", methods=["GET"])
@login_required
def lineups_single_league(league_id: int):
    """
    Single-league lineup page:
    - Default to current MFL week and allow selecting current and future weeks.
    - Auto-pick optimal starters by projection for that league/week.
    - Submit button + Back. Week dropdown triggers page reload with fresh projections.
    """
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    lg: League | None = db.session.get(League, league_id)
    if not lg or getattr(lg, "user_id", None) != current_user.id:
        flash("League not found or not owned by you.", "warning")
        return redirect("/leagues")

    # Determine current + allowed weeks anchored to this league year
    current_week = _effective_current_week(int(lg.year or _pick_year_for_week_lookup()))
    max_week = int(current_app.config.get("MFL_MAX_WEEKS", MFL_MAX_WEEKS_FALLBACK))
    weeks = _allowed_weeks_from(current_week, max_week)

    # Selected week (clamped to allowed)
    try:
        selected_week = int(request.args.get("week", current_week))
    except Exception:
        selected_week = current_week
    if selected_week < current_week:
        selected_week = current_week

    host = _league_host(lg) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)

    players = build_players_for_review(lg.id)
    pid_list = [pid for (pid, _, _, _) in players]
    proj_map = fetch_projected_scores(host, lg.mfl_id, lg.year, selected_week, pid_list, cookie=cookie)

    starters_label = getattr(lg, "roster_slots", None) or ""
    total_required, ranges = parse_lineup_requirements(starters_label)
    auto_ids = pick_optimal_lineup(players, proj_map, total_required, ranges)
    grouped = group_and_sort_players_for_review(players, proj_map)

    my_team_name = None
    try:
        team = (
            db.session.query(Team)
            .filter(Team.league_id == lg.id, Team.mfl_id == lg.franchise_id)
            .first()
        )
        my_team_name = team.name if team else None
    except Exception:
        pass

    return render_template(
        "lineups/single_league.html",
        league=lg,
        my_team_name=my_team_name,
        week=selected_week,
        weeks=weeks,
        current_week=current_week,
        starters_label=starters_label,
        total_required=total_required,
        ranges=ranges,
        grouped_players=grouped,
        auto_selected=set(auto_ids),
        next_url=request.args.get("next") or "/leagues",
    )


@lineups_bp.route("/lineups/league/<int:league_id>/submit", methods=["POST"])
@login_required
def lineups_single_submit(league_id: int):
    gate = _require_recent_sync_or_gate()
    if gate:
        return jsonify({"ok": False, "message": "Sync required. Please refresh leagues."}), 400

    lg: League | None = db.session.get(League, league_id)
    if not lg or getattr(lg, "user_id", None) != current_user.id:
        return jsonify({"ok": False, "message": "League not found or not owned by you."}), 404

    try:
        week_i = int(str(request.form.get("week")))
    except Exception:
        return jsonify({"ok": False, "message": "Missing or invalid week."}), 400

    vals = request.form.getlist("starters[]") or request.form.getlist("starters")
    submitted: List[int] = []
    for v in vals:
        try:
            submitted.append(int(str(v)))
        except Exception:
            continue

    allowed_ids = set(get_my_team_player_ids(lg.id))
    starters = [pid for pid in submitted if pid in allowed_ids]
    if not starters:
        return jsonify({"ok": False, "message": "No starters selected."}), 400

    host = _league_host(lg) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)

    ok, raw = submit_lineup(host, lg.mfl_id, lg.year, week_i, starters, cookie=cookie)
    clean = _clean_mfl_message(raw or ("OK" if ok else "Failed"))
    if ok or _is_ok_payload(raw or ""):
        # Success: tell client to go back to My Leagues
        return jsonify({"ok": True, "message": clean, "redirect": request.args.get("next") or request.form.get("next") or "/leagues"})
    else:
        # Error: keep user on the page; toast will persist
        return jsonify({"ok": False, "message": clean})
