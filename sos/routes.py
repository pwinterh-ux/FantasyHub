# sos/routes.py
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple, Any
from collections import defaultdict

from flask import Blueprint, abort, current_app, render_template, request
from flask_login import login_required, current_user

from app import db
from models import League, Team, Roster, Player, SleeperLeague, SleeperTeam
from services.lineups_service import (
    fetch_projected_scores,
    parse_lineup_requirements,
    pick_optimal_lineup,
)
from services.mfl_client import MFLClient
from services.sleeper_client import SleeperClient

sos_bp = Blueprint(
    "sos",
    __name__,
    url_prefix="/tools",
    template_folder="../templates",
)

# --------------------------- Host & cookie helpers (MFL) --------------------

def _norm_host(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    host = str(raw).strip()
    if host.startswith("http://"):
        host = host[7:]
    elif host.startswith("https://"):
        host = host[8:]
    return host.rstrip("/") or None

def _league_host(league: League) -> Optional[str]:
    for attr in ("league_host", "host", "base_url"):
        val = getattr(league, attr, None)
        if not val:
            continue
        host = _norm_host(val)
        if host:
            return host
    return "api.myfantasyleague.com"

def _cookie_header_for_host(host: str) -> Optional[str]:
    host = _norm_host(host) or "api.myfantasyleague.com"
    # best-effort cookie from user
    try:
        if hasattr(current_user, "get_mfl_cookie_header"):
            header = current_user.get_mfl_cookie_header(host)  # type: ignore[attr-defined]
            if header:
                return str(header)
    except Exception:
        pass
    for attr in ("mfl_cookie_api", "mfl_cookie"):
        val = getattr(current_user, attr, None)
        if isinstance(val, dict) and val:
            return "; ".join(f"{k}={v}" for k, v in val.items())
        if isinstance(val, str) and val:
            return val
    for attr in ("session_key", "mfl_session"):
        val = getattr(current_user, attr, None)
        if isinstance(val, str) and val:
            return f"MFLSESSION={val}"
    return None

# ------------------------------ Record helpers ------------------------------

_RECORD_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?\s*$")

def _record_tuple(record: Optional[str]) -> Tuple[int, int, int]:
    if not record:
        return (0, 0, 0)
    m = _RECORD_RE.match(str(record))
    if not m:
        return (0, 0, 0)
    wins = int(m.group(1)); losses = int(m.group(2)); ties = int(m.group(3) or 0)
    return wins, losses, ties

def _win_pct(record: Optional[str]) -> Optional[float]:
    w, l, t = _record_tuple(record)
    total = w + l + t
    if total <= 0:
        return None
    return (w + 0.5 * t) / total

def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)

def _ensure_int(val: Optional[int]) -> Optional[int]:
    try:
        if val is None: return None
        return int(val)
    except (TypeError, ValueError):
        return None

def _short_name(full: Optional[str]) -> str:
    if not full:
        return "—"
    s = str(full).strip()
    # first token; fallback to first 10 chars if token is gigantic
    token = s.split()[0]
    return token if len(token) <= 12 else token[:12]

# ----------------------------- MFL schedule parse ---------------------------

def _parse_mfl_schedule(xml_bytes: bytes) -> Dict[int, List[List[str]]]:
    """
    {week -> [ [fidA, fidB], ... ]}; includes empty week keys ([]) so we can infer RS end.
    """
    mapping: Dict[int, List[List[str]]] = {}
    if not xml_bytes:
        return mapping
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return mapping

    weekly_nodes = root.findall(".//weeklySchedule") if root is not None else []
    for weekly in weekly_nodes:
        week_attr = weekly.get("week")
        try:
            week = int(str(week_attr))
        except (TypeError, ValueError):
            continue

        entries: List[List[str]] = []
        for matchup in weekly.findall("matchup"):
            ids: List[str] = []
            home = matchup.get("home"); away = matchup.get("away")
            if home: ids.append(str(home))
            if away: ids.append(str(away))
            for franchise in matchup.findall("franchise"):
                fid = franchise.get("id") or franchise.get("franchise")
                if fid: ids.append(str(fid))
            cleaned: List[str] = []
            for fid in ids:
                f = str(fid).strip()
                if not f: continue
                f = f.zfill(4) if f.isdigit() else f
                if f not in cleaned: cleaned.append(f)
            if cleaned:
                entries.append(cleaned)

        mapping.setdefault(week, []).extend(entries)
        if week not in mapping:
            mapping[week] = []
    return mapping

def _infer_last_regular_week(schedule_map: Dict[int, List[List[str]]]) -> int:
    weeks_with_games = [wk for wk, games in schedule_map.items() if games]
    if not weeks_with_games:
        return _max_week_limit()
    return max(weeks_with_games)

# --------------------------- Platform-agnostic maps -------------------------

def _team_lookup_mfl(league_pk: int) -> Dict[str, Any]:
    """
    Key: MFL franchise id (zero-padded str). Value: object w/ name, record, points_for, etc.
    """
    rows = Team.query.filter(Team.league_id == league_pk).all()
    out: Dict[str, Any] = {}
    for row in rows:
        fid = (row.mfl_id or "").strip()
        if not fid: continue
        key = fid.zfill(4) if fid.isdigit() else fid
        out[key] = row
    return out

def _team_lookup_sleeper(sleeper_league_pk: int) -> Dict[str, Any]:
    """
    Key: Sleeper roster_id (string). Value: SleeperTeam row.
    """
    rows = SleeperTeam.query.filter(SleeperTeam.league_id == sleeper_league_pk).all()
    out: Dict[str, Any] = {}
    for row in rows:
        rid = str(row.sleeper_roster_id)
        out[rid] = row
    return out

def _tiers_for_league(team_map: Dict[str, Any], *, is_sleeper: bool) -> Dict[str, str]:
    """
    top/mid/bot thirds by current win%
    """
    pairs = []
    for fid, obj in team_map.items():
        rec = getattr(obj, "record", None)
        wp = _win_pct(rec)
        if wp is not None:
            pairs.append((fid, wp))
    if not pairs:
        return {}
    wps = sorted([wp for _, wp in pairs])
    n = len(wps)
    lo_ix = max(0, int(n/3) - 1)
    hi_ix = max(0, int(2*n/3) - 1)
    lo_cut, hi_cut = wps[lo_ix], wps[hi_ix]
    out: Dict[str, str] = {}
    for fid, wp in pairs:
        if wp >= hi_cut: out[fid] = "top"
        elif wp <= lo_cut: out[fid] = "bot"
        else: out[fid] = "mid"
    return out

def _cell_payload(opponent_id: Optional[str], team_map: Dict[str, Any], tier_map: Dict[str, str]) -> dict:
    if not opponent_id or opponent_id not in team_map:
        return {"text_short": "BYE", "title": "BYE", "tier": None, "is_bye": True}
    team = team_map[opponent_id]
    name_full = getattr(team, "name", None) or f"Franchise {opponent_id}"
    rec = getattr(team, "record", None) or "—"
    tier = tier_map.get(opponent_id)
    return {
        "text_short": _short_name(name_full),
        "title": f"{name_full} — {rec}",
        "tier": tier,
        "is_bye": False,
    }

# ----------------------------- Sleeper schedule -----------------------------

def _sleeper_schedule_map(client: SleeperClient, league_sid: str, *, start_week: int, max_week: int) -> Dict[int, List[List[str]]]:
    """
    Build {week -> [[rosterA, rosterB], ...]} by grouping /matchups/{week} on matchup_id.
    We probe from start_week .. max_week and stop once we see 2+ consecutive empty weeks.
    """
    mapping: Dict[int, List[List[str]]] = {}
    empty_streak = 0
    for wk in range(start_week, max_week + 1):
        rows = client.get_matchups(league_sid, wk) or []
        groups: Dict[Any, List[str]] = defaultdict(list)
        for r in rows:
            mid = r.get("matchup_id")
            rid = r.get("roster_id")
            if mid is None or rid is None:
                continue
            groups[mid].append(str(rid))
        pairs: List[List[str]] = []
        for _, members in groups.items():
            if len(members) >= 2:
                pairs.append(members[:2])
        mapping[wk] = pairs
        empty_streak = empty_streak + 1 if not pairs else 0
        # heuristic: once regular season is over, Sleeper will return empties going forward
        if empty_streak >= 3:  # 3 consecutive empty weeks -> assume no more RS
            # keep the empty weeks in map so _infer_last_regular_week works
            break
    return mapping

# ----------------------------- Week helpers ---------------------------------

def _effective_week_for_year(year: int) -> int:
    try:
        from lineups.routes import _effective_current_week
        return int(_effective_current_week(year))
    except Exception:
        return 1

def _max_week_limit() -> int:
    try:
        val = int(current_app.config.get("MFL_MAX_WEEKS", 18))
        return max(1, min(val, 22))
    except Exception:
        return 18

# ------------------------------- Main route ---------------------------------

@sos_bp.route("/strength-of-schedule", methods=["GET"])
@login_required
def strength_of_schedule():
    # Gather MFL leagues
    mfl_leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )
    # Gather Sleeper leagues
    sleeper_leagues: List[SleeperLeague] = (
        db.session.query(SleeperLeague)
        .filter(SleeperLeague.user_id == current_user.id)
        .order_by(SleeperLeague.year.desc(), SleeperLeague.name.asc())
        .all()
    )

    if not mfl_leagues and not sleeper_leagues:
        return render_template("sos/index.html", grid=[], has_data=False, start_week=None)

    # Determine default start week based on most recent season across both platforms
    years = [lg.year for lg in mfl_leagues] + [sl.year for sl in sleeper_leagues]
    latest_year = max(years) if years else _effective_week_for_year(1)
    default_start_week = _effective_week_for_year(latest_year)

    try:
        start_week_req = int(request.args.get("start_week", default_start_week))
    except Exception:
        start_week_req = default_start_week

    grid_rows = []
    has_data = False

    # --- MFL processing ---
    for league in mfl_leagues:
        league_year = _ensure_int(league.year)
        if not league_year or not league.mfl_id or not getattr(league, "franchise_id", None):
            grid_rows.append(_err_row(league.name, "mfl", league, "Missing year/MFL id/your franchise id."))
            continue

        host = _league_host(league) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)
        base_host = _norm_host(host) or "api.myfantasyleague.com"
        base_url = f"https://{base_host}/{league_year}/"
        mfl_client = MFLClient(league_year, base_url=base_url)

        try:
            schedule_bytes = mfl_client.get_schedule(str(league.mfl_id), cookie=cookie)
        except Exception as exc:
            grid_rows.append(_err_row(league.name, "mfl", league, f"Schedule fetch failed: {exc}"))
            continue

        schedule_map = _parse_mfl_schedule(schedule_bytes)
        if not schedule_map:
            grid_rows.append(_err_row(league.name, "mfl", league, "No schedule found."))
            continue

        last_rs_week = _infer_last_regular_week(schedule_map)
        start_week = max(1, min(start_week_req, last_rs_week))
        header_weeks = list(range(start_week, last_rs_week + 1))

        team_map = _team_lookup_mfl(league.id)
        my_id = str(getattr(league, "franchise_id")).zfill(4)
        my_team = team_map.get(my_id)
        tier_map = _tiers_for_league(team_map, is_sleeper=False)

        row = _build_grid_row(
            platform="mfl",
            league_name=league.name or "League",
            header_weeks=header_weeks,
            team_map=team_map,
            tier_map=tier_map,
            schedule_map=schedule_map,
            my_team_id=my_id,
            my_team_obj=my_team,
            league_ref=league,
            start_week=start_week,
        )
        grid_rows.append(row)
        has_data = True

    # --- Sleeper processing ---
    if sleeper_leagues:
        sleeper_client = SleeperClient()
        max_week = _max_week_limit()

    for sl in sleeper_leagues:
        if not sl.sleeper_id or not sl.year:
            grid_rows.append(_err_row(sl.name, "sleeper", sl, "Missing Sleeper league id/year."))
            continue

        # Determine "my team" in Sleeper: prefer user_id match, fall back to owner_user_id
        team_map = _team_lookup_sleeper(sl.id)
        my_team_id = None
        for rid, team in team_map.items():
            # First preference: this site user owns the SleeperTeam row
            if getattr(team, "user_id", None) == getattr(current_user, "id", None):
                my_team_id = rid; break
        if my_team_id is None and getattr(current_user, "sleeper_user_id", None):
            for rid, team in team_map.items():
                if str(team.owner_user_id or "") == str(current_user.sleeper_user_id):
                    my_team_id = rid; break

        # Fetch Sleeper schedule across remaining range starting at request start week
        try:
            schedule_map = _sleeper_schedule_map(
                sleeper_client,
                sl.sleeper_id,
                start_week=max(1, start_week_req),
                max_week=max_week,
            )
        except Exception as exc:
            grid_rows.append(_err_row(sl.name, "sleeper", sl, f"Sleeper schedule fetch failed: {exc}"))
            continue

        if not schedule_map:
            grid_rows.append(_err_row(sl.name, "sleeper", sl, "No schedule found."))
            continue

        last_rs_week = _infer_last_regular_week(schedule_map)
        start_week = max(1, min(start_week_req, last_rs_week))
        header_weeks = list(range(start_week, last_rs_week + 1))

        my_team = team_map.get(str(my_team_id)) if my_team_id else None
        tier_map = _tiers_for_league(team_map, is_sleeper=True)

        row = _build_grid_row(
            platform="sleeper",
            league_name=sl.name or "Sleeper League",
            header_weeks=header_weeks,
            team_map=team_map,
            tier_map=tier_map,
            schedule_map=schedule_map,
            my_team_id=str(my_team_id) if my_team_id is not None else None,
            my_team_obj=my_team,
            league_ref=sl,
            start_week=start_week,
        )
        grid_rows.append(row)
        has_data = True

    # Sort leagues by "toughness" for *my* team (more top-tier opponents -> higher)
    rankval = {"top": 3, "mid": 2, "bot": 1, None: 0}
    def _row_score(r: dict) -> float:
        tiers = [rankval.get(c.get("tier"), 0) for c in r.get("my_cells", [])]
        return -sum(tiers)
    grid_rows.sort(key=_row_score)

    return render_template(
        "sos/index.html",
        grid=grid_rows,
        has_data=has_data,
        start_week=start_week_req,
    )

@sos_bp.route("/strength-of-schedule/mfl/<int:league_id>/simulate", methods=["GET"])
@login_required
def simulate_mfl_season(league_id: int):
    league: League | None = (
        db.session.query(League)
        .filter(League.id == league_id, League.user_id == current_user.id)
        .first()
    )
    if not league:
        abort(404)

    league_year = _ensure_int(league.year)
    if not league_year or not league.mfl_id:
        abort(404)

    default_start_week = _effective_week_for_year(league_year)
    try:
        start_week_req = int(request.args.get("start_week", default_start_week))
    except Exception:
        start_week_req = default_start_week

    host = _league_host(league) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)
    base_host = _norm_host(host) or "api.myfantasyleague.com"
    base_url = f"https://{base_host}/{league_year}/"
    mfl_client = MFLClient(league_year, base_url=base_url)

    error = None
    simulation = None
    start_week = start_week_req
    try:
        schedule_bytes = mfl_client.get_schedule(str(league.mfl_id), cookie=cookie)
        schedule_map = _parse_mfl_schedule(schedule_bytes)
        if not schedule_map:
            raise RuntimeError("No schedule found.")

        last_rs_week = _infer_last_regular_week(schedule_map)
        start_week = max(1, min(start_week_req, last_rs_week))
        simulation = _simulate_mfl_league(
            league=league,
            host=host,
            cookie=cookie,
            schedule_map=schedule_map,
            start_week=start_week,
            last_week=last_rs_week,
        )
    except Exception as exc:
        current_app.logger.exception("MFL season simulation failed for league %s", league_id)
        error = str(exc)

    return render_template(
        "sos/simulate.html",
        league=league,
        error=error,
        simulation=simulation,
        start_week=start_week,
    )

# ------------------------- Grid row assembly helpers ------------------------

def _build_grid_row(
    *,
    platform: str,
    league_name: str,
    header_weeks: List[int],
    team_map: Dict[str, Any],
    tier_map: Dict[str, str],
    schedule_map: Dict[int, List[List[str]]],
    my_team_id: Optional[str],
    my_team_obj: Any,
    league_ref: Any,
    start_week: Optional[int] = None,
) -> dict:
    # Compact “my team” strip (right column total)
    my_cells: List[dict] = []
    total_w = total_l = total_t = 0

    for wk in header_weeks:
        opp_id = _opponent_for(schedule_map, my_team_id, wk)
        payload = _cell_payload(opp_id, team_map, tier_map)
        if not payload["is_bye"]:
            w, l, t = _record_tuple(getattr(team_map[opp_id], "record", None))
            total_w += w; total_l += l; total_t += t
        my_cells.append({"week": wk, **payload})

    # Full-league grid: for each team × week, store opponent cell payload
    team_ids_sorted = sorted(team_map.keys(), key=lambda k: (getattr(team_map[k], "standing", 999) or 999, getattr(team_map[k], "name", "")))
    league_grid: List[dict] = []
    for fid in team_ids_sorted:
        row_cells: List[dict] = []
        for wk in header_weeks:
            opp = _opponent_for(schedule_map, fid, wk)
            row_cells.append(_cell_payload(opp, team_map, tier_map))
        league_grid.append({
            "fid": fid,
            "name_full": getattr(team_map[fid], "name", f"Team {fid}"),
            "name_short": _short_name(getattr(team_map[fid], "name", None)),
            "cells": row_cells,
        })

    return {
        "platform": platform,  # 'mfl' or 'sleeper'
        "league": league_ref,
        "league_name": league_name,
        "header_weeks": header_weeks,
        "start_week": start_week,
        "my_team": my_team_obj,
        "my_cells": my_cells,
        "total_record": {"wins": total_w, "losses": total_l, "ties": total_t},
        "grid": league_grid,
        "error": None,
    }

def _opponent_for(schedule_map: Dict[int, List[List[str]]], team_id: Optional[str], week: int) -> Optional[str]:
    if not team_id:
        return None
    for pair in schedule_map.get(week, []):
        if team_id in pair:
            # return the "other" member in the pair
            for pid in pair:
                if pid != team_id:
                    return pid
    return None

def _err_row(name: Optional[str], platform: str, ref: Any, msg: str) -> dict:
    return {
        "platform": platform,
        "league": ref,
        "league_name": name or "(unnamed)",
        "header_weeks": [],
        "my_team": None,
        "my_cells": [],
        "total_record": None,
        "grid": [],
        "error": msg,
    }

# ---------------------------- MFL simulation --------------------------------

def _mfl_rosters_for_league(league_id: int) -> Dict[str, List[Tuple[int, str, str, str]]]:
    rows = (
        db.session.query(Team.mfl_id, Roster.player_id, Player.name, Player.position, Player.team)
        .join(Roster, Roster.team_id == Team.id)
        .join(Player, Player.id == Roster.player_id)
        .filter(Team.league_id == league_id)
        .order_by(Team.mfl_id, Player.position, Player.name)
        .all()
    )
    out: Dict[str, List[Tuple[int, str, str, str]]] = defaultdict(list)
    for fid, pid, name, pos, nfl in rows:
        key = str(fid or "").strip()
        if not key:
            continue
        key = key.zfill(4) if key.isdigit() else key
        try:
            pid_i = int(pid)
        except Exception:
            continue
        out[key].append((pid_i, name or "", (pos or "").upper(), (nfl or "").upper()))
    return dict(out)

def _fetch_projected_scores_chunked(
    *,
    host: str,
    league: League,
    week: int,
    player_ids: List[int],
    cookie: Optional[str],
    chunk_size: int = 80,
) -> Dict[int, Any]:
    merged: Dict[int, Any] = {}
    clean_ids = sorted({int(pid) for pid in player_ids if pid is not None})
    for ix in range(0, len(clean_ids), chunk_size):
        chunk = clean_ids[ix:ix + chunk_size]
        if not chunk:
            continue
        merged.update(
            fetch_projected_scores(
                host,
                league.mfl_id,
                league.year,
                week,
                chunk,
                cookie=cookie,
            )
        )
    return merged

def _projection_value(projections: Dict[int, Any], pid: int) -> float:
    proj = projections.get(pid)
    raw = getattr(proj, "projected", None)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except Exception:
        return 0.0

def _record_label(wins: int, losses: int, ties: int) -> str:
    if ties:
        return f"{wins}-{losses}-{ties}"
    return f"{wins}-{losses}"

def _record_sort_key(row: dict) -> Tuple[float, float, str]:
    wins = int(row.get("final_wins") or 0)
    losses = int(row.get("final_losses") or 0)
    ties = int(row.get("final_ties") or 0)
    games = wins + losses + ties
    win_pct = ((wins + 0.5 * ties) / games) if games else 0.0
    return (-win_pct, -float(row.get("final_pf") or 0), str(row.get("team_name") or ""))

def _simulate_mfl_league(
    *,
    league: League,
    host: str,
    cookie: Optional[str],
    schedule_map: Dict[int, List[List[str]]],
    start_week: int,
    last_week: int,
) -> dict:
    team_map = _team_lookup_mfl(league.id)
    rosters = _mfl_rosters_for_league(league.id)
    all_player_ids = sorted({pid for players in rosters.values() for pid, _n, _p, _t in players})
    total_required, ranges = parse_lineup_requirements(getattr(league, "roster_slots", None))
    my_id = str(getattr(league, "franchise_id", "") or "").zfill(4)

    standings: Dict[str, dict] = {}
    for fid, team in team_map.items():
        w, l, t = _record_tuple(getattr(team, "record", None))
        standings[fid] = {
            "fid": fid,
            "team_name": getattr(team, "name", None) or f"Franchise {fid}",
            "current_wins": w,
            "current_losses": l,
            "current_ties": t,
            "sim_wins": 0,
            "sim_losses": 0,
            "sim_ties": 0,
            "final_wins": w,
            "final_losses": l,
            "final_ties": t,
            "current_pf": float(getattr(team, "points_for", None) or 0),
            "sim_pf": 0.0,
            "final_pf": float(getattr(team, "points_for", None) or 0),
            "is_me": fid == my_id,
        }

    weeks: List[dict] = []
    close_matchups: List[dict] = []
    big_wins: List[dict] = []

    for week in range(start_week, last_week + 1):
        projections = _fetch_projected_scores_chunked(
            host=host,
            league=league,
            week=week,
            player_ids=all_player_ids,
            cookie=cookie,
        )
        weekly_lineups: Dict[str, dict] = {}
        for fid, players in rosters.items():
            starter_ids = pick_optimal_lineup(players, projections, total_required, ranges)
            player_lookup = {pid: (name, pos, nfl) for pid, name, pos, nfl in players}
            starters = []
            total = 0.0
            for pid in starter_ids:
                score = _projection_value(projections, pid)
                total += score
                name, pos, nfl = player_lookup.get(pid, ("", "", ""))
                starters.append({
                    "id": pid,
                    "name": name or str(pid),
                    "position": pos or "",
                    "team": nfl or "",
                    "projected": score,
                })
            starters.sort(key=lambda p: (-float(p["projected"]), p["position"], p["name"]))
            weekly_lineups[fid] = {
                "projected": total,
                "starters": starters,
                "starter_count": len(starters),
            }

        matchups: List[dict] = []
        for pair in schedule_map.get(week, []):
            if len(pair) < 2:
                continue
            a_id, b_id = pair[0], pair[1]
            if a_id not in team_map or b_id not in team_map:
                continue

            a_lineup = weekly_lineups.get(a_id, {"projected": 0.0, "starters": [], "starter_count": 0})
            b_lineup = weekly_lineups.get(b_id, {"projected": 0.0, "starters": [], "starter_count": 0})
            a_score = float(a_lineup["projected"])
            b_score = float(b_lineup["projected"])
            margin = abs(a_score - b_score)

            winner_id = None
            if abs(a_score - b_score) < 0.005:
                standings[a_id]["sim_ties"] += 1
                standings[b_id]["sim_ties"] += 1
            elif a_score > b_score:
                winner_id = a_id
                standings[a_id]["sim_wins"] += 1
                standings[b_id]["sim_losses"] += 1
            else:
                winner_id = b_id
                standings[b_id]["sim_wins"] += 1
                standings[a_id]["sim_losses"] += 1

            standings[a_id]["sim_pf"] += a_score
            standings[b_id]["sim_pf"] += b_score

            matchup = {
                "week": week,
                "team_a": {
                    "fid": a_id,
                    "name": getattr(team_map[a_id], "name", None) or f"Franchise {a_id}",
                    "projected": a_score,
                    "starters": a_lineup["starters"],
                    "starter_count": a_lineup["starter_count"],
                    "is_winner": winner_id == a_id,
                    "is_me": a_id == my_id,
                },
                "team_b": {
                    "fid": b_id,
                    "name": getattr(team_map[b_id], "name", None) or f"Franchise {b_id}",
                    "projected": b_score,
                    "starters": b_lineup["starters"],
                    "starter_count": b_lineup["starter_count"],
                    "is_winner": winner_id == b_id,
                    "is_me": b_id == my_id,
                },
                "winner_name": (getattr(team_map[winner_id], "name", None) if winner_id else "Tie") if winner_id else "Tie",
                "margin": margin,
                "is_tie": winner_id is None,
                "is_close": margin <= 5.0,
                "is_big_win": margin >= 25.0,
                "is_my_matchup": a_id == my_id or b_id == my_id,
            }
            matchups.append(matchup)
            if matchup["is_close"]:
                close_matchups.append(matchup)
            if matchup["is_big_win"]:
                big_wins.append(matchup)

        weeks.append({"week": week, "matchups": matchups})

    for row in standings.values():
        row["final_wins"] = row["current_wins"] + row["sim_wins"]
        row["final_losses"] = row["current_losses"] + row["sim_losses"]
        row["final_ties"] = row["current_ties"] + row["sim_ties"]
        row["final_pf"] = row["current_pf"] + row["sim_pf"]
        row["current_record"] = _record_label(row["current_wins"], row["current_losses"], row["current_ties"])
        row["sim_record"] = _record_label(row["sim_wins"], row["sim_losses"], row["sim_ties"])
        row["final_record"] = _record_label(row["final_wins"], row["final_losses"], row["final_ties"])

    standings_rows = sorted(standings.values(), key=_record_sort_key)
    for index, row in enumerate(standings_rows, start=1):
        row["rank"] = index

    my_row = next((r for r in standings_rows if r["is_me"]), None)
    my_matchups = [m for w in weeks for m in w["matchups"] if m["is_my_matchup"]]
    close_matchups.sort(key=lambda m: (m["margin"], m["week"]))
    big_wins.sort(key=lambda m: (-m["margin"], m["week"]))

    projection_chunks = (len(all_player_ids) + 79) // 80 if all_player_ids else 0

    return {
        "league_name": league.name or "League",
        "start_week": start_week,
        "last_week": last_week,
        "weeks": weeks,
        "standings": standings_rows,
        "my_team": my_row,
        "my_matchups": my_matchups,
        "close_matchups": close_matchups[:12],
        "big_wins": big_wins[:12],
        "lineup_rules": getattr(league, "roster_slots", None) or "",
        "projection_calls": len(list(range(start_week, last_week + 1))) * projection_chunks,
    }
