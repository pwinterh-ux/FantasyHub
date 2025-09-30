# injuries/routes.py
from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

import requests
from flask import (
    Blueprint,
    current_app,
    flash,
    render_template,
    session,
    url_for,
)
from flask_login import current_user, login_required
from sqlalchemy import MetaData, Table, select, asc

from app import db
from lineups.routes import (
    _cookie_header_for_host,
    _effective_current_week,
    _league_host,
    _pick_year_for_week_lookup,
    _require_recent_sync_or_gate,
)
from models import League, Player, Roster, Team

CACHE_KEY = "injuries_cache_v1"
CACHE_TTL_SECONDS = 15 * 60
_SERVER_CACHE: dict[tuple[str, int, int], tuple[float, dict[str, Any]]] = {}
_SERVER_CACHE_LOCK = threading.Lock()
HEADERS_XML = {
    "User-Agent": "FantasyHub/1.0 (+injury-assist)",
    "Accept": "application/xml,text/xml,*/*;q=0.8",
}

ROSTER_STATUS_LABELS = {
    "S": "Starter",
    "NS": "Non starter",
    "R": "No lineup submitted",
    "IR": "Injured Reserve",
    "TS": "Taxi Squad",
}
HIGHLIGHT_RED_KEYWORDS = {"out", "suspended", "doubtful", "o", "d"}

injuries_bp = Blueprint("injuries", __name__, url_prefix="/injuries", template_folder="../templates")

# ---------- Shared helpers ----------

def _normalize_player_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return str(int(text))  # strip leading zeros for numeric IDs
    except Exception:
        return text

def _highlight_for(injury_status: str | None, roster_status: str | None) -> str:
    if not injury_status or (roster_status or "").upper() != "S":
        return ""
    lowered = injury_status.strip().lower()
    if not lowered:
        return ""
    if (
        lowered in HIGHLIGHT_RED_KEYWORDS
        or lowered.startswith("out")
        or lowered.startswith("suspended")
        or lowered.startswith("doubtful")
        or lowered.startswith("ir")
        or "pup" in lowered
    ):
        return "red"
    if lowered in {"questionable", "q"} or lowered.startswith("questionable"):
        return "yellow"
    return ""

def _status_severity(injury_status: str | None) -> int:
    if not injury_status:
        return 2
    lowered = (injury_status or "").strip().lower()
    if not lowered:
        return 2
    if (
        lowered in HIGHLIGHT_RED_KEYWORDS
        or lowered.startswith("out")
        or lowered.startswith("suspended")
        or lowered.startswith("doubtful")
        or lowered.startswith("ir")
        or "pup" in lowered
    ):
        return 0
    if lowered in {"questionable", "q"} or lowered.startswith("questionable"):
        return 1
    return 2

def _priority_player(entry: dict[str, Any]) -> tuple[int, str]:
    """red first, then yellow, then starters, then others — name tiebreaker."""
    color = entry.get("highlight")
    is_starter_entry = entry.get("is_starter", False)
    if color == "red":
        return (0, entry.get("name", ""))
    if color == "yellow":
        return (1, entry.get("name", ""))
    if is_starter_entry:
        return (2, entry.get("name", ""))
    return (3, entry.get("name", ""))

def _league_priority(entry: dict[str, Any]) -> tuple[int, int, int, str]:
    severity = int(entry.get("league_severity", 99))
    red_count = int(entry.get("red_count", 0))
    yellow_count = int(entry.get("yellow_count", 0))
    return (severity, -red_count, -yellow_count, entry.get("league_name", ""))

def _cache_key_for_current_user(year: int | str, week: int | str) -> Optional[tuple[str, int, int]]:
    try:
        user_id = current_user.get_id()
    except Exception:
        user_id = None
    if not user_id:
        return None
    try:
        return (str(user_id), int(year), int(week))
    except Exception:
        return None

def _get_server_cached_payload(cache_key: Optional[tuple[str, int, int]], now_ts: float) -> Optional[dict[str, Any]]:
    if not cache_key:
        return None
    with _SERVER_CACHE_LOCK:
        entry = _SERVER_CACHE.get(cache_key)
        if not entry:
            return None
        cached_ts, payload = entry
        if now_ts - cached_ts >= CACHE_TTL_SECONDS:
            _SERVER_CACHE.pop(cache_key, None)
            return None
        return payload

def _store_server_cached_payload(cache_key: Optional[tuple[str, int, int]], payload: dict[str, Any], ts: float) -> None:
    if not cache_key:
        return
    with _SERVER_CACHE_LOCK:
        _SERVER_CACHE[cache_key] = (ts, payload)

def _clear_server_cached_payload(cache_key: Optional[tuple[str, int, int]]) -> None:
    if not cache_key:
        return
    with _SERVER_CACHE_LOCK:
        _SERVER_CACHE.pop(cache_key, None)

# ---------- MFL-specific ----------

def _fetch_injuries(year: int, week: int) -> tuple[dict[str, dict[str, str]], dict[str, Any]]:
    url = f"https://api.myfantasyleague.com/{year}/export"
    params = {"TYPE": "injuries", "W": str(week), "JSON": "0"}
    headers = dict(HEADERS_XML)
    cookie = _cookie_header_for_host("api.myfantasyleague.com")
    if cookie:
        headers["Cookie"] = cookie
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        root = ET.Element("injuries")

    injuries_map: dict[str, dict[str, str]] = {}
    for injury in root.findall("injury"):
        pid = _normalize_player_id(injury.get("id"))
        if not pid:
            continue
        injuries_map[pid] = {
            "status": injury.get("status", "") or "",
            "details": injury.get("details", "") or "",
            "exp_return": injury.get("exp_return", "") or "",
        }
    meta: dict[str, Any] = {
        "week": _normalize_player_id(root.get("week")) or str(week),
        "timestamp": root.get("timestamp"),
    }
    return injuries_map, meta

def _fetch_roster_statuses(*, league: League, player_ids: Iterable[str], year: int, week: int) -> dict[str, str]:
    ids = [pid for pid in {_normalize_player_id(p) for p in player_ids} if pid]
    if not ids:
        return {}
    host = _league_host(league) or "api.myfantasyleague.com"
    url = f"https://{host}/{year}/export"
    params = {"TYPE": "playerRosterStatus", "L": str(league.mfl_id), "W": str(week), "P": ",".join(ids), "JSON": "0"}
    headers = dict(HEADERS_XML)
    cookie = _cookie_header_for_host(host)
    if cookie:
        headers["Cookie"] = cookie
    resp = requests.get(url, params=params, headers=headers, timeout=20)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        root = ET.Element("playerRosterStatuses")

    status_map: dict[str, str] = {}
    target_franchise = str(league.franchise_id or "").strip()
    for node in root.findall("playerStatus"):
        pid = _normalize_player_id(node.get("id"))
        if not pid:
            continue
        roster_status = None
        for roster_node in node.findall("roster_franchise"):
            fid = str(roster_node.get("franchise_id") or "").strip()
            if target_franchise and fid != target_franchise:
                continue
            roster_status = roster_node.get("status")
            if roster_status:
                break
        if roster_status is None and node.find("roster_franchise") is not None:
            roster_status = node.find("roster_franchise").get("status")
        if roster_status:
            status_map[pid] = roster_status
    return status_map

def _gather_league_players(league: League) -> list[dict[str, Any]]:
    team: Optional[Team] = (
        db.session.query(Team)
        .filter(Team.league_id == league.id, Team.mfl_id == league.franchise_id)
        .first()
    )
    if not team:
        return []
    rows: List[tuple[Roster, Player]] = (
        db.session.query(Roster, Player)
        .join(Player, Player.id == Roster.player_id)
        .filter(Roster.team_id == team.id)
        .all()
    )
    players: List[dict[str, Any]] = []
    for roster_row, player in rows:
        pid = _normalize_player_id(player.mfl_id)
        if not pid:
            continue
        players.append(
            {
                "player_id": pid,
                "name": player.name or "Unknown",
                "position": player.position or "",
                "team": player.team or "",
                # Note: this table flag is *not* weekly. We compute weekly below from MFL API.
                "is_starter": bool(roster_row.is_starter),
            }
        )
    return players

# ---------- Sleeper (reflection) ----------

def _reflect_table(name: str) -> Optional[Table]:
    try:
        metadata = MetaData()
        engine = db.session.get_bind()
        return Table(name, metadata, autoload_with=engine)
    except Exception:
        current_app.logger.debug("Unable to reflect table %s", name, exc_info=True)
        db.session.rollback()
        return None

def _sleeper_leagues_for_user(user_id: int) -> list[Mapping[str, Any]]:
    tbl = _reflect_table("sleeper_leagues")
    if tbl is None:
        return []
    stmt = select(tbl).where(tbl.c.user_id == user_id).order_by(asc(tbl.c.name))
    return db.session.execute(stmt).mappings().all()

def _sleeper_teams_for_league(league_db_id: int) -> list[Mapping[str, Any]]:
    teams = _reflect_table("sleeper_teams")
    if teams is None:
        return []
    stmt = select(teams).where(teams.c.league_id == league_db_id)
    return db.session.execute(stmt).mappings().all()

def _identify_my_sleeper_team(team_rows: list[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    for t in team_rows:
        if "user_id" in t and t["user_id"] is not None and str(t["user_id"]) == str(current_user.id):
            return t
    my_sid = getattr(current_user, "sleeper_user_id", None)
    if my_sid not in (None, ""):
        for t in team_rows:
            if str(t.get("owner_user_id")) == str(my_sid):
                return t
    return team_rows[0] if team_rows else None

def _gather_league_players_sleeper(league_row: Mapping[str, Any]) -> list[dict[str, Any]]:
    teams = _sleeper_teams_for_league(int(league_row["id"]))
    my_team = _identify_my_sleeper_team(teams)
    if not my_team:
        return []
    rosters_tbl = _reflect_table("sleeper_rosters")
    players_tbl = _reflect_table("sleeper_players")
    if rosters_tbl is None or players_tbl is None:
        return []
    j = (
        select(
            rosters_tbl.c.is_starter,
            rosters_tbl.c.lineup_slot,
            rosters_tbl.c.player_sid,
            players_tbl.c.mfl_id,
            players_tbl.c.name,
            players_tbl.c.position,
            players_tbl.c.team,
        )
        .select_from(rosters_tbl.join(players_tbl, rosters_tbl.c.player_sid == players_tbl.c.sleeper_id))
        .where(rosters_tbl.c.team_id == my_team["id"])
    )
    rows = db.session.execute(j).mappings().all()
    out: list[dict[str, Any]] = []
    for r in rows:
        mfl_pid = _normalize_player_id(r.get("mfl_id"))
        if not mfl_pid:
            continue
        is_starter = bool(r.get("is_starter"))
        slot = (r.get("lineup_slot") or "").strip().lower()
        roster_code = "S" if (is_starter or slot == "starter") else "NS"
        out.append(
            {
                "player_id": mfl_pid,
                "name": r.get("name") or str(r.get("player_sid") or mfl_pid),
                "position": r.get("position") or "",
                "team": r.get("team") or "",
                "is_starter": roster_code == "S",
                "roster_status": roster_code,
                "roster_status_label": ROSTER_STATUS_LABELS.get(roster_code, roster_code),
            }
        )
    return out

# ---------- Page assembly ----------

def _build_payload(year: int, week: int) -> dict[str, Any]:
    injuries_map, meta = _fetch_injuries(year, week)

    # MFL leagues
    mfl_leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.name.asc())
        .all()
    )

    league_blocks: List[dict[str, Any]] = []

    for league in mfl_leagues:
        try:
            roster_players = _gather_league_players(league)
            if not roster_players:
                current_app.logger.debug("Injuries: MFL %s has no roster rows; skipping", league.name)
                continue

            roster_ids = [p["player_id"] for p in roster_players]
            injured_ids = [pid for pid in roster_ids if pid in injuries_map]
            if not injured_ids:
                continue  # no one on report

            status_map = _fetch_roster_statuses(league=league, player_ids=injured_ids, year=year, week=week)

            players_output: List[dict[str, Any]] = []
            severity_scores: List[int] = []
            red_count = yellow_count = starter_count = 0

            for player in roster_players:
                pid = player["player_id"]
                if pid not in injuries_map:
                    continue
                injury_info = injuries_map[pid]
                roster_status = status_map.get(pid, "")
                injury_status = injury_info.get("status")
                highlight = _highlight_for(injury_status, roster_status)
                severity = _status_severity(injury_status)

                # Weekly starter flag (this is the one the template uses)
                weekly_is_starter = (roster_status or "").upper() == "S"

                if weekly_is_starter:
                    starter_count += 1
                    severity_scores.append(severity)
                else:
                    severity_scores.append(2)

                if highlight == "red":
                    red_count += 1
                elif highlight == "yellow":
                    yellow_count += 1

                players_output.append(
                    {
                        **player,
                        # ensure template sees the weekly value:
                        "is_starter": weekly_is_starter,
                        "injury_status": injury_status or "",
                        "injury_details": injury_info.get("details", ""),
                        "expected_return": injury_info.get("exp_return", ""),
                        "roster_status": roster_status,
                        "roster_status_label": ROSTER_STATUS_LABELS.get(
                            (roster_status or "").upper(), (roster_status or "").upper()
                        ),
                        "highlight": highlight,
                        "severity": severity,
                    }
                )

            players_output.sort(key=_priority_player)

            if players_output:
                league_severity = min(severity_scores) if severity_scores else 2
                league_blocks.append(
                    {
                        "platform": "mfl",
                        "league_pk": league.id,
                        "league_name": league.name,
                        "league_year": league.year,
                        "league_mfl_id": league.mfl_id,
                        "players": players_output,
                        "league_severity": league_severity,
                        "red_count": red_count,
                        "yellow_count": yellow_count,
                        "starter_count": starter_count,
                        "player_count": len(players_output),
                    }
                )
        except Exception:
            current_app.logger.exception("Injuries: failed to build MFL block for %s", getattr(league, "name", league.id))

    # Sleeper leagues
    s_leagues = _sleeper_leagues_for_user(current_user.id)
    for s in s_leagues:
        try:
            roster_players = _gather_league_players_sleeper(s)
            if not roster_players:
                continue

            players_output: List[dict[str, Any]] = []
            severity_scores: List[int] = []
            red_count = yellow_count = starter_count = 0

            for player in roster_players:
                pid = player["player_id"]  # MFL id
                if pid not in injuries_map:
                    continue
                injury_info = injuries_map[pid]
                roster_status = "S" if player.get("is_starter") else "NS"
                injury_status = injury_info.get("status")
                highlight = _highlight_for(injury_status, roster_status)
                severity = _status_severity(injury_status)

                if roster_status == "S":
                    starter_count += 1
                    severity_scores.append(severity)
                else:
                    severity_scores.append(2)

                if highlight == "red":
                    red_count += 1
                elif highlight == "yellow":
                    yellow_count += 1

                players_output.append(
                    {
                        **player,
                        "injury_status": injury_status or "",
                        "injury_details": injury_info.get("details", ""),
                        "expected_return": injury_info.get("exp_return", ""),
                        "roster_status": roster_status,
                        "roster_status_label": ROSTER_STATUS_LABELS.get(roster_status, roster_status),
                        "highlight": highlight,
                        "severity": severity,
                    }
                )

            players_output.sort(key=_priority_player)

            if players_output:
                league_severity = min(severity_scores) if severity_scores else 2
                league_blocks.append(
                    {
                        "platform": "sleeper",
                        "league_pk": s.get("id"),
                        "league_name": s.get("name") or s.get("display_name") or "Sleeper League",
                        "league_year": s.get("year"),
                        "league_mfl_id": s.get("sleeper_id"),
                        "players": players_output,
                        "league_severity": league_severity,
                        "red_count": red_count,
                        "yellow_count": yellow_count,
                        "starter_count": starter_count,
                        "player_count": len(players_output),
                    }
                )
        except Exception:
            current_app.logger.exception("Injuries: failed to build Sleeper block for %s", s.get("name") or s.get("id"))

    fetched_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%SZ")
    injury_timestamp = None
    try:
        if meta.get("timestamp"):
            injury_timestamp = datetime.utcfromtimestamp(int(meta["timestamp"])).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        injury_timestamp = None

    league_blocks.sort(key=_league_priority)

    return {
        "generated_at": fetched_at,
        "injury_report_week": meta.get("week") or str(week),
        "injury_report_timestamp": injury_timestamp,
        "leagues": league_blocks,
    }

@injuries_bp.route("/")
@login_required
def injuries_index():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    year = _pick_year_for_week_lookup()
    week = _effective_current_week(year)

    cache: Optional[dict[str, Any]] = session.get(CACHE_KEY)
    payload: Optional[dict[str, Any]] = None
    used_cache = False
    error_message: Optional[str] = None

    cache_key = _cache_key_for_current_user(year, week)
    now_ts = time.time()
    if cache:
        try:
            cached_week = int(cache.get("week", 0))
            cached_year = int(cache.get("year", 0))
            cached_ts = float(cache.get("ts", 0))
        except Exception:
            cached_week = cached_year = 0
            cached_ts = 0.0
        if cached_week == int(week) and cached_year == int(year) and now_ts - cached_ts < CACHE_TTL_SECONDS:
            payload = _get_server_cached_payload(cache_key, now_ts)
            if payload is not None:
                used_cache = True
        else:
            stale_key = _cache_key_for_current_user(cached_year, cached_week)
            _clear_server_cached_payload(stale_key)
            session.pop(CACHE_KEY, None)

    if payload is None:
        try:
            payload = _build_payload(year, week)
            fresh_ts = time.time()
            session[CACHE_KEY] = {"ts": fresh_ts, "week": int(week), "year": int(year)}
            session.modified = True
            _store_server_cached_payload(cache_key, payload, fresh_ts)
        except requests.RequestException:
            current_app.logger.exception("Failed to refresh injuries feed")
            error_message = "Could not refresh injury data from MFL right now. Showing the last cached results if available."
            cached_payload = _get_server_cached_payload(cache_key, now_ts)
            if cached_payload is not None:
                payload = cached_payload
                used_cache = True
            else:
                payload = {
                    "generated_at": None,
                    "injury_report_week": str(week),
                    "injury_report_timestamp": None,
                    "leagues": [],
                }
        except Exception:
            current_app.logger.exception("Unexpected error while building injury report")
            error_message = "Unexpected error while building injury report. Please try again later."
            payload = {
                "generated_at": None,
                "injury_report_week": str(week),
                "injury_report_timestamp": None,
                "leagues": [],
            }

    # CTA: MFL gets submit, Sleeper gets none (hidden in template)
    display_leagues: List[dict[str, Any]] = []
    for block in payload.get("leagues", []):
        block_copy = dict(block)
        if block.get("platform") == "mfl" and block.get("league_pk"):
            block_copy["submit_url"] = url_for("lineups.lineups_single_league", league_id=block["league_pk"], week=week)
        else:
            block_copy["submit_url"] = ""
        display_leagues.append(block_copy)

    if error_message:
        flash(error_message, "warning")

    return render_template(
        "injuries/index.html",
        year=year,
        week=week,
        payload={**payload, "leagues": display_leagues},
        cache_ttl_minutes=CACHE_TTL_SECONDS // 60,
        used_cache=used_cache,
    )
