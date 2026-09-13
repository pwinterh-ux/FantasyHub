from __future__ import annotations

import concurrent.futures
import re
from datetime import date, datetime, timedelta, timezone
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
from models import League, Team

# Service helpers
from services.lineups_service import (
    get_my_team_roster_statuses,
    is_lineup_eligible_status,
    ensure_roster_status_fresh,
    ensure_lineup_mode_resolved,
    validate_lineup_starters,
    fetch_projected_scores,
    submit_lineup,
    build_players_for_review,
    group_and_sort_players_for_review,
    # rapid helpers
    parse_lineup_requirements,
    pick_optimal_lineup,
    Projection,
)
from services.mfl_parsers import LINEUP_MODE_BEST_BALL
from services.lineup_check_service import build_constrained_optimal_lineup
from services.lineup_constraints import lineup_satisfies_constraints
from services.lineup_lock_service import lock_violation, resolve_lineup_locks

lineups_bp = Blueprint("lineups", __name__, template_folder="../templates")

# -------------------------- Config / knobs ----------------------------------

MFL_MAX_WEEKS_FALLBACK = 18
PARALLEL_WORKERS = 3  # mirror other mass API calls (tweak to 2-3 as desired)
LINEUP_WEEK_ROLLOVER_WEEKDAY = 1  # Tuesday
LINEUP_WEEK_ONE_OPENERS = {
    2026: date(2026, 9, 9),
}

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


def _refresh_lineup_roster(league: League) -> Tuple[bool, Optional[str], str, Optional[str]]:
    host = _league_host(league) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)
    ok, error = ensure_roster_status_fresh(league, host=host, cookie=cookie)
    return ok, error, host, cookie


def _resolve_lineup_mode(league: League) -> str:
    host = _league_host(league) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)
    return ensure_lineup_mode_resolved(league, host=host, cookie=cookie)


def _eligible_players(league_id: int, players: List[Tuple[int, str, str, str]]):
    statuses = get_my_team_roster_statuses(league_id)
    return [row for row in players if is_lineup_eligible_status(statuses.get(row[0]))]

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

def _lineup_week_one_opener(year: int) -> date | None:
    opener = LINEUP_WEEK_ONE_OPENERS.get(year)
    if opener is not None:
        return opener

    configured = current_app.config.get("LINEUP_WEEK_ONE_OPENER")
    if isinstance(configured, dict):
        value = configured.get(year) or configured.get(str(year))
    else:
        value = configured

    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _date_based_lineup_week(year: int, today: date) -> int | None:
    opener = _lineup_week_one_opener(year)
    if opener is None:
        return None
    if today < opener:
        return 1

    rollover = opener
    while rollover.weekday() != LINEUP_WEEK_ROLLOVER_WEEKDAY:
        rollover += timedelta(days=1)

    if today < rollover:
        return 1

    return 2 + ((today - rollover).days // 7)


def _effective_current_week(year: int) -> int:
    cfg_week = current_app.config.get("MFL_CURRENT_WEEK")
    try:
        forced_week = int(cfg_week)
    except (TypeError, ValueError):
        forced_week = None
    if forced_week and 1 <= forced_week <= 22:
        return forced_week

    try:
        wk = int(current_app.config.get("MFL_WEEK_FALLBACK", 1))
    except (TypeError, ValueError):
        wk = 1
    if wk < 1:
        wk = 1

    try:
        today = datetime.now().date()
    except Exception:
        today = None

    if today:
        date_week = _date_based_lineup_week(year, today)
        if date_week is not None:
            wk = max(wk, date_week)

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


@lineups_bp.route("/lineups/check", methods=["GET", "POST"])
@login_required
def lineups_check():
    """Run the read-only portfolio checker; no MFL import endpoint is reachable here."""
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate
    from services.lineup_check_service import check_user_lineups, fetch_injuries
    from services.nfl_schedule_service import (
        build_team_game_states, fetch_mfl_nfl_schedule,
        parse_mfl_nfl_schedule_with_metadata, sync_nfl_schedule,
    )

    season = _pick_year_for_week_lookup()
    week = _effective_current_week(season)
    schedule_ok = False
    week_complete = False
    game_states = {}
    try:
        payload = fetch_mfl_nfl_schedule(season)  # exactly once for the entire scan
        parsed, schedule_metadata = parse_mfl_nfl_schedule_with_metadata(payload, season)
        week_complete = bool(schedule_metadata.get(week, {}).get("structurally_complete"))
        if not week_complete:
            raise ValueError(f"MFL schedule week {week} was absent or structurally incomplete")
        sync_nfl_schedule(season, parsed)
        # The durable table is a cache.  Current decisions use only this fresh,
        # structurally verified payload so stale DB rows cannot contaminate them.
        rows = [row for row in parsed if row["week"] == week]
        schedule_ok = bool(rows)
        game_states = build_team_game_states(rows, datetime.now(timezone.utc), schedule_verified=schedule_ok)
    except Exception:
        db.session.rollback()
        current_app.logger.exception("Lineup Checker schedule refresh failed")

    try:
        injuries = fetch_injuries(season, week)  # exactly once for the entire scan
        injuries_ok = True
    except Exception:
        current_app.logger.exception("Lineup Checker injury refresh failed")
        injuries, injuries_ok = {}, False

    jobs = []
    for league in _user_synced_leagues():
        if int(league.year) != int(season):
            continue
        host = _league_host(league) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)
        best_ball = _resolve_lineup_mode(league) == LINEUP_MODE_BEST_BALL
        refresh_error = None
        if not best_ball:
            ok, refresh_error = ensure_roster_status_fresh(league, host=host, cookie=cookie)
            if ok:
                refresh_error = None
        tuples = [] if best_ball else build_players_for_review(league.id)
        locations = {} if best_ball else get_my_team_roster_statuses(league.id)
        jobs.append({
            "league_id": int(league.id), "mfl_id": str(league.mfl_id),
            "league_name": str(league.name), "year": int(league.year), "host": str(host),
            "cookie": str(cookie or ""), "franchise_id": str(league.franchise_id or ""),
            "roster_slots": str(league.roster_slots or ""), "best_ball": bool(best_ball),
            "refresh_error": str(refresh_error) if refresh_error else None,
            "player_ids": [int(p[0]) for p in tuples],
            "players": [{"player_id": int(pid), "name": str(name), "position": str(pos),
                         "team": str(team), "roster_status": str(locations.get(pid, "UNKNOWN"))}
                        for pid, name, pos, team in tuples],
        })
    result = check_user_lineups(jobs, season=season, week=week, injuries=injuries,
        injuries_ok=injuries_ok, game_states=game_states, schedule_ok=schedule_ok,
        week_complete=week_complete, max_workers=PARALLEL_WORKERS)
    return render_template("lineups/check.html", result=result)


@lineups_bp.route("/lineups/check/review/<int:league_id>", methods=["POST"])
@login_required
def lineups_check_review(league_id: int):
    """Validate a checker recommendation and open it in compact Rapid review."""
    lg = db.session.get(League, league_id)
    try:
        week = int(str(request.form.get("week")))
    except (TypeError, ValueError):
        week = 0
    season = _pick_year_for_week_lookup()
    max_week = int(current_app.config.get("MFL_MAX_WEEKS", MFL_MAX_WEEKS_FALLBACK))
    if not lg or lg.user_id != current_user.id:
        flash("League not found or not owned by you.", "warning")
        return redirect(url_for("lineups.lineups_check"))
    if int(lg.year) != int(season) or week not in _allowed_weeks_from(_effective_current_week(season), max_week):
        flash("That Lineup Checker recommendation is no longer current.", "warning")
        return redirect(url_for("lineups.lineups_check"))
    roster_ids = {int(pid) for pid in get_my_team_roster_statuses(lg.id)}
    raw = request.form.getlist("recommended_starters[]") or request.form.getlist("recommended_starters")
    try:
        recommended = [int(value) for value in raw]
    except (TypeError, ValueError):
        recommended = []
    if not recommended or len(set(recommended)) != len(recommended) or not set(recommended).issubset(roster_ids):
        flash("The recommendation contained players who are not on this roster. Run Lineup Checker again.", "warning")
        return redirect(url_for("lineups.lineups_check"))
    session.update(rapid_week=week, rapid_queue=[lg.id], rapid_idx=0,
                   lineups_rapid_total=1, lineups_rapid_success=0,
                   rapid_source="lineup_checker",
                   rapid_prefill={str(lg.id): recommended},
                   rapid_checker_context={str(lg.id): {
                       "leaving": request.form.getlist("leaving[]"),
                       "entering": request.form.getlist("entering[]")}})
    session.pop("lineups_rapid_events", None)
    session.modified = True
    return redirect(url_for("lineups.lineups_rapid_league"))

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

        if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
            jobs.append(dict(league_id=lg.id, league_mfl_id=str(lg.mfl_id),
                             league_year=int(lg.year), host=None, cookie=None, players=[], pid_list=[],
                             my_team_name=None, starters_label="", best_ball=True,
                             refresh_warning=None))
            continue
        refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)

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
            league_id=lg.id,
            league_mfl_id=str(lg.mfl_id),
            league_year=int(lg.year),
            host=host,
            cookie=cookie,
            players=players,
            pid_list=pid_list,
            my_team_name=my_team_name,
            starters_label=(getattr(lg, "roster_slots", None) or ""),
            best_ball=False,
            refresh_warning=refresh_error if not refresh_ok else None,
        ))

    # THREADS: network projections only
    def _net_fetch(job: dict):
        if job.get("best_ball"):
            return (job["league_id"], {}, None)
        try:
            proj_map = fetch_projected_scores(
                job["host"], job["league_mfl_id"], job["league_year"], week_i,
                job["pid_list"], cookie=job["cookie"]
            )
            return (job["league_id"], proj_map, None)
        except Exception as exc:
            return (job["league_id"], {}, str(exc))

    projection_results = {
        league_id: (projections, error)
        for league_id, projections, error in _parallel_map(_net_fetch, jobs, max_workers=PARALLEL_WORKERS)
    }
    leagues_by_id = {lg.id: lg for lg in leagues}

    # MAIN THREAD: assemble view model
    items: List[Dict[str, object]] = []
    for job in jobs:
        lg = leagues_by_id[job["league_id"]]
        projections, projection_error = projection_results.get(
            lg.id, ({}, "Projection lookup failed")
        )
        statuses = get_my_team_roster_statuses(lg.id)
        grouped = group_and_sort_players_for_review(job["players"], projections, statuses)
        items.append(dict(
            league=lg,
            host=job["host"],
            my_team_name=job["my_team_name"],
            starters_label=job["starters_label"],  # raw from DB (may include total prefix)
            grouped_players=grouped,
            flat_players=job["players"],
            best_ball=job.get("best_ball", False),
            refresh_warning=job.get("refresh_warning") or (
                f"Projection error: {projection_error}" if projection_error else None
            ),
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
                league_id=lg.id, league_mfl_id=str(lg.mfl_id), league_year=int(lg.year),
                host=None, cookie=None, starters=[],
                force_result=dict(ok=False, message="Skipped: league not owned by current user")
            ))
            continue

        if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
            jobs.append(dict(
                league_id=lg.id, league_mfl_id=str(lg.mfl_id), league_year=int(lg.year),
                host=None, cookie=None, starters=[],
                force_result=dict(ok=True, skipped=True, message="Skipped — Best Ball (MFL sets optimal lineup)")
            ))
            continue

        refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)
        submitted = selections.get(lg.id, [])
        if not refresh_ok:
            jobs.append(dict(league_id=lg.id, league_mfl_id=str(lg.mfl_id), league_year=int(lg.year), host=None, cookie=None, starters=[],
                             force_result=dict(ok=False, message=f"Lineup not submitted: {refresh_error}")))
            continue
        guard_error = validate_lineup_starters(lg.id, submitted)
        if guard_error:
            jobs.append(dict(league_id=lg.id, league_mfl_id=str(lg.mfl_id), league_year=int(lg.year), host=None, cookie=None, starters=[],
                             force_result=dict(ok=False, message=guard_error)))
            continue
        starters = submitted

        if not starters:
            # Don't send an empty lineup (avoids clearing)
            jobs.append(dict(
                league_id=lg.id, league_mfl_id=str(lg.mfl_id), league_year=int(lg.year),
                host=None, cookie=None, starters=[],
                force_result=dict(ok=False, message="Skipped: no starters selected")
            ))
            continue

        jobs.append(dict(league_id=lg.id, league_mfl_id=str(lg.mfl_id), league_year=int(lg.year),
                         host=host, cookie=cookie, starters=starters, force_result=None))

    if not jobs:
        flash("No leagues selected to submit. Check the 'Include' box for any league you want to submit.", "warning")
        return redirect(url_for("lineups.lineups_index"))

    # THREADS: only network submission (or return forced result)
    def _submit_one(job: dict) -> Dict[str, object]:
        # Forced result (not owned / no starters)
        if job.get("force_result"):
            fr = job["force_result"]
            return dict(league_id=job["league_id"], ok=fr["ok"], skipped=fr.get("skipped", False), message=fr["message"])
        ok, raw = submit_lineup(job["host"], job["league_mfl_id"], job["league_year"],
                                week_i, job["starters"], cookie=job["cookie"])
        # raw may include XML; keep as-is for batch page (legacy)
        return dict(league_id=job["league_id"], ok=ok, message=raw or ("Lineup submitted successfully" if ok else "Unknown response"))

    leagues_by_id = {lg.id: lg for lg in leagues}
    results = _parallel_map(_submit_one, jobs, max_workers=PARALLEL_WORKERS)
    for result in results:
        result["league"] = leagues_by_id[result.pop("league_id")]

    return render_template("lineups/summary.html", week=week_i, results=results)


@lineups_bp.route("/lineups/auto-submit", methods=["POST"])
@login_required
def lineups_auto_submit():
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

    forced_results: List[Dict[str, object]] = []
    jobs: List[dict] = []

    for lg in leagues:
        if getattr(lg, "user_id", None) != current_user.id:
            forced_results.append(
                dict(
                    league=lg,
                    ok=False,
                    message="Skipped: league not owned by current user",
                    lineup=[],
                    projected_total=None,
                )
            )
            continue

        if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
            forced_results.append(dict(
                league=lg, ok=True, skipped=True,
                message="Skipped — Best Ball (MFL sets optimal lineup)",
                lineup=[], projected_total=None,
            ))
            continue

        refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)
        if not refresh_ok:
            forced_results.append(dict(
                league=lg, ok=False, message=f"Lineup not submitted: {refresh_error}",
                lineup=[], projected_total=None,
            ))
            continue

        players = build_players_for_review(lg.id)
        if not players:
            forced_results.append(
                dict(
                    league=lg,
                    ok=False,
                    message="Skipped: no rostered players found",
                    lineup=[],
                    projected_total=None,
                )
            )
            continue

        starters_label = getattr(lg, "roster_slots", None) or ""
        total_required, ranges = parse_lineup_requirements(starters_label)
        pid_list = [pid for (pid, _name, _pos, _team) in players]
        if not pid_list:
            forced_results.append(
                dict(
                    league=lg,
                    ok=False,
                    message="Skipped: empty roster",
                    lineup=[],
                    projected_total=None,
                )
            )
            continue

        jobs.append(
            dict(
                league_id=lg.id,
                league_mfl_id=str(lg.mfl_id),
                league_year=int(lg.year),
                host=host,
                cookie=cookie,
                players=players,
                pid_list=pid_list,
                total_required=total_required,
                ranges=ranges,
            )
        )

    def _fetch(job: dict) -> Tuple[int, Dict[int, Projection], Optional[str]]:
        try:
            proj = fetch_projected_scores(
                job["host"],
                job["league_mfl_id"],
                job["league_year"],
                week_i,
                job["pid_list"],
                cookie=job["cookie"],
            )
            return (job["league_id"], proj, None)
        except Exception as exc:
            return (job["league_id"], {}, str(exc))

    proj_results: Dict[int, Dict[str, object]] = {}
    if jobs:
        for league_id, proj_map, error in _parallel_map(_fetch, jobs, max_workers=PARALLEL_WORKERS):
            proj_results[league_id] = {"projections": proj_map, "error": error}

    auto_results: List[Dict[str, object]] = []
    leagues_by_id = {lg.id: lg for lg in leagues}

    for job in jobs:
        lg = leagues_by_id[job["league_id"]]
        entry = proj_results.get(lg.id) or {"projections": {}, "error": "Projection lookup failed"}
        error_msg = entry.get("error")
        if error_msg:
            auto_results.append(
                dict(
                    league=lg,
                    ok=False,
                    message=f"Projection error: {error_msg}",
                    lineup=[],
                    projected_total=None,
                )
            )
            continue

        projections: Dict[int, Projection] = entry.get("projections", {})  # type: ignore[assignment]
        players = job["players"]
        total_required = job["total_required"]
        ranges = job["ranges"]
        auto_ids = pick_optimal_lineup(_eligible_players(lg.id, players), projections, total_required, ranges)

        starters = auto_ids
        if not starters:
            auto_results.append(
                dict(
                    league=lg,
                    ok=False,
                    message="Unable to identify starters for submission",
                    lineup=[],
                    projected_total=None,
                )
            )
            continue

        guard_error = validate_lineup_starters(lg.id, starters)
        if guard_error:
            auto_results.append(dict(league=lg, ok=False, message=guard_error,
                                     lineup=[], projected_total=None))
            continue

        players_lookup = {
            pid: dict(name=name, position=pos, team=nfl)
            for (pid, name, pos, nfl) in players
        }

        try:
            ok, raw = submit_lineup(
                job["host"],
                lg.mfl_id,
                lg.year,
                week_i,
                starters,
                cookie=job["cookie"],
            )
        except Exception as exc:
            auto_results.append(
                dict(
                    league=lg,
                    ok=False,
                    message=f"Submission error: {exc}",
                    lineup=[],
                    projected_total=None,
                )
            )
            continue

        raw_text = raw or ("OK" if ok else "")
        clean = _clean_mfl_message(raw_text or ("OK" if ok else "Failed"))
        final_ok = ok or _is_ok_payload(raw_text)

        lineup_details: List[Dict[str, object]] = []
        projected_total: Optional[float] = None
        total_sum = 0.0
        counted = 0

        for pid in starters:
            info = players_lookup.get(pid, {})
            proj_entry = projections.get(pid)
            proj_val = proj_entry.projected if proj_entry else None
            if proj_val is not None:
                try:
                    total_sum += float(proj_val)
                    counted += 1
                except (TypeError, ValueError):
                    pass
            lineup_details.append(
                dict(
                    player_id=pid,
                    name=info.get("name") or f"Player {pid}",
                    position=info.get("position") or "",
                    team=info.get("team") or "",
                    projected=proj_val,
                )
            )

        if counted:
            projected_total = round(total_sum, 2)

        lineup_details.sort(
            key=lambda row: (
                row.get("projected") is None,
                -float(row.get("projected") or 0.0),
                str(row.get("name") or ""),
            )
        )

        auto_results.append(
            dict(
                league=lg,
                ok=final_ok,
                message=clean or ("Lineup submitted successfully" if final_ok else ""),
                lineup=lineup_details,
                projected_total=projected_total,
            )
        )

    all_results = forced_results + auto_results
    all_results.sort(key=lambda r: str(r["league"].name or "").lower())

    return render_template(
        "lineups/summary.html",
        week=week_i,
        results=all_results,
        auto_mode=True,
    )

# ============================ Rapid flow (one-by-one) ========================

def _live_lock_context(lg: League, players: list, week: int, host: str, cookie: str | None) -> dict:
    return resolve_lineup_locks({"host": host, "year": int(lg.year), "mfl_id": str(lg.mfl_id),
        "franchise_id": str(lg.franchise_id or ""), "cookie": cookie or ""}, players, week)


def _lineup_is_legal(ids: set[int], players: list, total: int | None, ranges: dict) -> bool:
    lookup = {int(row[0]): str(row[2]).upper() for row in players}
    if not ids.issubset(lookup):
        return False
    if total is not None and len(ids) != total:
        return False
    counts = {}
    for pid in ids:
        counts[lookup[pid]] = counts.get(lookup[pid], 0) + 1
    return lineup_satisfies_constraints(counts, ranges)


def _lock_safe_view(lg: League, players: list, projections: dict, total: int | None,
                    ranges: dict, statuses: dict, lock_context: dict,
                    requested_prefill: list[int] | None = None) -> tuple[dict, set[int], str | None]:
    current = set(lock_context["current"])
    frozen = set(lock_context["locked_starters"]) | set(lock_context.get("unknown_starters", set()))
    forbidden = (set(lock_context["locked_bench"]) |
                 set(lock_context.get("unknown_bench", set())) |
                 set(lock_context.get("bye_players", set())) |
                 set(lock_context.get("no_game_players", set())))
    warning = lock_context.get("warning")
    if not lock_context["safe"]:
        selected = current
    elif requested_prefill is not None:
        selected = set(requested_prefill)
        selected.update(frozen)
        selected.difference_update(forbidden)
        selected = {pid for pid in selected if is_lineup_eligible_status(statuses.get(pid))}
        if not _lineup_is_legal(selected, players, total, ranges):
            selected = current - set(lock_context.get("bye_players", set())) - set(lock_context.get("no_game_players", set()))
            warning = "The saved checker recommendation is no longer legal after current game locks. Current starters are preserved; refresh Lineup Checker."
    else:
        player_dicts = [{"player_id": pid, "name": name, "position": pos, "team": team,
                         "projection": projections[pid].projected if pid in projections else None}
                        for pid, name, pos, team in players
                        if is_lineup_eligible_status(statuses.get(pid))]
        result = build_constrained_optimal_lineup(player_dicts, total, ranges,
                    frozen, forbidden)
        selected = (set(result["starter_ids"]) if result["ok"] else
                    current - set(lock_context.get("bye_players", set())) - set(lock_context.get("no_game_players", set())))
        if not result["ok"]:
            warning = result["reason"]
    grouped = group_and_sort_players_for_review(players, projections, statuses)
    for rows in grouped.values():
        for row in rows:
            pid = int(row["player_id"])
            row.update(is_current_starter=pid in current,
                       game_state=lock_context["states"].get(pid, "UNKNOWN"),
                       is_locked=pid in lock_context["locked_starters"] | lock_context["locked_bench"],
                       locked_as_starter=pid in lock_context["locked_starters"],
                       unknown_as_starter=pid in lock_context.get("unknown_starters", set()),
                       is_editable=pid not in frozen | forbidden)
    return grouped, selected, warning


def _clear_checker_rapid_state() -> None:
    for key in ("rapid_week", "rapid_queue", "rapid_idx", "lineups_rapid_total",
                "lineups_rapid_success", "lineups_rapid_events", "rapid_source",
                "rapid_prefill", "rapid_checker_context"):
        session.pop(key, None)
    session.modified = True

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
        session.pop("rapid_source", None)
        session.pop("rapid_prefill", None)
        session.pop("rapid_checker_context", None)
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

    if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
        return render_template(
            "lineups/rapid_league.html", week=week_i, league=lg,
            my_team_name=None, starters_label="", total_required=None, ranges={},
            grouped_players={}, auto_selected=set(), index=idx + 1,
            total_leagues=len(queue), best_ball=True, refresh_warning=None,
        )

    refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)

    players = build_players_for_review(lg.id)
    pid_list = [pid for (pid, _, _, _) in players]
    proj_map = fetch_projected_scores(host, lg.mfl_id, lg.year, week_i, pid_list, cookie=cookie)

    starters_label = getattr(lg, "roster_slots", None) or ""
    total_required, ranges = parse_lineup_requirements(starters_label)
    statuses = get_my_team_roster_statuses(lg.id)
    locks = _live_lock_context(lg, players, week_i, host, cookie)
    prefill = None
    if session.get("rapid_source") == "lineup_checker":
        prefill = (session.get("rapid_prefill") or {}).get(str(lg.id))
    grouped, auto_ids, lock_warning = _lock_safe_view(
        lg, players, proj_map, total_required, ranges, statuses, locks, prefill)

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
        best_ball=False,
        refresh_warning=(refresh_error if not refresh_ok else None) or lock_warning,
        checker_context=((session.get("rapid_checker_context") or {}).get(str(lg.id))
                         if session.get("rapid_source") == "lineup_checker" else None),
        locked_starter_count=len(locks["locked_starters"]),
        checker_source=session.get("rapid_source") == "lineup_checker",
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

    if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
        message = "Best Ball — no lineup required (MFL sets the optimal lineup)."
        _record_rapid_event(lg, "skipped", message)
        queue: List[int] = session.get("rapid_queue") or []
        idx = int(session.get("rapid_idx") or 0)
        session["rapid_idx"] = min(idx + 1, len(queue))
        session.modified = True
        return jsonify({
            "ok": True,
            "skipped": True,
            "message": message,
            "next": session["rapid_idx"] < len(queue),
        })

    vals = request.form.getlist("starters[]") or request.form.getlist("starters")
    submitted: List[int] = []
    for v in vals:
        try:
            submitted.append(int(str(v)))
        except Exception:
            continue

    refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)
    if not refresh_ok:
        message = f"Lineup not submitted: {refresh_error}"
        _record_rapid_event(lg, "error", message)
        return jsonify({"ok": False, "message": message, "next": False}), 503
    guard_error = validate_lineup_starters(lg.id, submitted)
    if guard_error:
        _record_rapid_event(lg, "error", guard_error)
        return jsonify({"ok": False, "message": guard_error, "next": False}), 400
    players = build_players_for_review(lg.id)
    lock_error = lock_violation(_live_lock_context(lg, players, week_i, host, cookie), submitted)
    if lock_error:
        _record_rapid_event(lg, "error", lock_error)
        return jsonify({"ok": False, "message": lock_error, "next": False}), 409
    starters = submitted
    if not starters:
        _record_rapid_event(lg, "error", "No starters selected.")
        return jsonify({"ok": False, "message": "No starters selected.", "next": False}), 400

    ok, raw = submit_lineup(host, lg.mfl_id, lg.year, week_i, starters, cookie=cookie)
    msg = raw or ("OK" if ok else "Failed")

    queue: List[int] = session.get("rapid_queue") or []
    idx: int = int(session.get("rapid_idx") or 0)

    if ok or _is_ok_payload(raw or ""):
        _record_rapid_event(lg, "submitted", msg)
        # Success: advance pointer, keep order
        session["rapid_idx"] = min(idx + 1, len(queue))
        session.modified = True
        checker_source = session.get("rapid_source") == "lineup_checker"
        if checker_source:
            _clear_checker_rapid_state()
            return jsonify({"ok": True, "message": _clean_mfl_message(msg), "next": False,
                            "redirect": url_for("lineups.lineups_check")})
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
    checker_source = session.get("rapid_source") == "lineup_checker"
    queue: List[int] = session.get("rapid_queue") or []
    idx: int = int(session.get("rapid_idx") or 0)
    # record skip for current league (if any)
    if queue and idx < len(queue):
        league_id_pk = queue[idx]
        lg: League | None = db.session.get(League, league_id_pk)
        if lg:
            message = ("Best Ball — no lineup required (MFL sets the optimal lineup)."
                       if lg.lineup_mode == LINEUP_MODE_BEST_BALL else "Skipped by user.")
            _record_rapid_event(lg, "skipped", message)
        session["rapid_idx"] = idx + 1
        session.modified = True
    next_exists = (session.get("rapid_idx", 0) < len(queue))
    if checker_source:
        _clear_checker_rapid_state()
        return jsonify({"ok": True, "message": "Skipped.", "next": False,
                        "redirect": url_for("lineups.lineups_check")})
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
    session.pop("rapid_source", None)
    session.pop("rapid_prefill", None)
    session.pop("rapid_checker_context", None)

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

    if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
        flash("Best Ball league: MFL sets the optimal lineup; no weekly submission is required.", "info")
        return redirect(request.args.get("next") or "/leagues")

    refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)

    players = build_players_for_review(lg.id)
    pid_list = [pid for (pid, _, _, _) in players]
    proj_map = fetch_projected_scores(host, lg.mfl_id, lg.year, selected_week, pid_list, cookie=cookie)

    starters_label = getattr(lg, "roster_slots", None) or ""
    total_required, ranges = parse_lineup_requirements(starters_label)
    statuses = get_my_team_roster_statuses(lg.id)
    locks = _live_lock_context(lg, players, selected_week, host, cookie)
    grouped, auto_ids, lock_warning = _lock_safe_view(
        lg, players, proj_map, total_required, ranges, statuses, locks)

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
        refresh_warning=(refresh_error if not refresh_ok else None) or lock_warning,
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
    if _resolve_lineup_mode(lg) == LINEUP_MODE_BEST_BALL:
        return jsonify({
            "ok": True,
            "skipped": True,
            "message": "Best Ball — no lineup required (MFL sets the optimal lineup).",
            "redirect": request.args.get("next") or request.form.get("next") or "/leagues",
        })

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

    refresh_ok, refresh_error, host, cookie = _refresh_lineup_roster(lg)
    if not refresh_ok:
        return jsonify({"ok": False, "message": f"Lineup not submitted: {refresh_error}"}), 503
    guard_error = validate_lineup_starters(lg.id, submitted)
    if guard_error:
        return jsonify({"ok": False, "message": guard_error}), 400
    players = build_players_for_review(lg.id)
    lock_error = lock_violation(_live_lock_context(lg, players, week_i, host, cookie), submitted)
    if lock_error:
        return jsonify({"ok": False, "message": lock_error}), 409
    starters = submitted
    if not starters:
        return jsonify({"ok": False, "message": "No starters selected."}), 400

    ok, raw = submit_lineup(host, lg.mfl_id, lg.year, week_i, starters, cookie=cookie)
    clean = _clean_mfl_message(raw or ("OK" if ok else "Failed"))
    if ok or _is_ok_payload(raw or ""):
        # Success: tell client to go back to My Leagues
        return jsonify({"ok": True, "message": clean, "redirect": request.args.get("next") or request.form.get("next") or "/leagues"})
    else:
        # Error: keep user on the page; toast will persist
        return jsonify({"ok": False, "message": clean})
