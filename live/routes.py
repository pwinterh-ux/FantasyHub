# fantasyhub/live/routes.py
from __future__ import annotations

import time
from datetime import datetime, timezone, date, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Semaphore

from flask import Blueprint, render_template, current_app, session, redirect, url_for
from flask_login import login_required, current_user

from app import db
from models import League, Team, Player, SleeperPlayer  # SleeperPlayer for S:<sid> fallback
from services.mfl_client import MFLClient
from services.mfl_live import parse_live_scoring, LiveMatchup  # type: ignore
from services.guards import can_view_aggregate_detail

# Sleeper live integration
from services.sleeper_client import SleeperClient
from services.sleeper_live import build_sleeper_tiles_for_user

live_bp = Blueprint("live", __name__, url_prefix="/live")

CACHE_KEY = "live_cache"
STALE_SECONDS = 300  # 5 minutes

# --- lightweight server-side cache for live scoring (per-process) ---
_LIVE_CACHE_STORE: dict[int, dict] = {}
_LIVE_CACHE_LOCK = Lock()
_LIVE_CACHE_MAX_USERS = 200   # soft cap


def _get_live_cache(user_id: int) -> dict | None:
    with _LIVE_CACHE_LOCK:
        return _LIVE_CACHE_STORE.get(user_id)


def _set_live_cache(user_id: int, payload: dict) -> None:
    with _LIVE_CACHE_LOCK:
        if len(_LIVE_CACHE_STORE) >= _LIVE_CACHE_MAX_USERS and user_id not in _LIVE_CACHE_STORE:
            _LIVE_CACHE_STORE.pop(next(iter(_LIVE_CACHE_STORE)))
        _LIVE_CACHE_STORE[user_id] = payload


def _now_ts() -> float:
    return time.time()


def _league_host(lg: League) -> Optional[str]:
    for attr in ("league_host", "host", "base_url"):
        val = getattr(lg, attr, None)
        if not val:
            continue
        s = str(val)
        if s.startswith("http"):
            try:
                from urllib.parse import urlparse
                netloc = urlparse(s).netloc
                if netloc:
                    return netloc
            except Exception:
                pass
        else:
            return s
    return None


def _cookie_for_host(host: Optional[str]) -> Optional[str]:
    if not host:
        host = "api.myfantasyleague.com"
    keys = [
        f"mfl_cookie::{host}",
        f"MFL_COOKIE::{host}",
        "mfl_cookie",
        "MFL_COOKIE",
    ]
    for k in keys:
        v = session.get(k)
        if v:
            return v
    for dict_key in ("mfl_cookies", "MFL_COOKIES"):
        d = session.get(dict_key)
        if isinstance(d, dict):
            if host in d and d[host]:
                return d[host]
            base = host.split(".", 1)[-1]
            if base in d and d[base]:
                return d[base]
    try:
        host_cookies = current_user.get_mfl_host_cookies()
        if host in host_cookies and host_cookies[host]:
            return host_cookies[host]
    except Exception:
        pass
    return getattr(current_user, "mfl_cookie_api", None)


def _team_names_map(league_id: int) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in Team.query.filter(Team.league_id == league_id).all():
        if t.mfl_id:
            out[str(t.mfl_id).zfill(4)] = t.name or str(t.mfl_id).zfill(4)
    return out


def _player_lookup(player_ids: List[int]) -> Dict[str, Dict[str, Any]]:
    if not player_ids:
        return {}
    rows = Player.query.filter(Player.id.in_(player_ids)).all()
    look: Dict[str, Dict[str, Any]] = {}
    for p in rows:
        look[str(p.id)] = {
            "name": p.name,
            "pos": getattr(p, "position", None) or getattr(p, "pos", None),
            "team": getattr(p, "team", None),
        }
    return look


def _aggregate_from_tiles(tiles: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_my = 0.0
    total_opp = 0.0
    my_secs_total = 0
    my_secs_played = 0
    opp_secs_total = 0
    opp_secs_played = 0
    starters_my: List[Dict[str, Any]] = []
    starters_opp: List[Dict[str, Any]] = []

    for t in tiles:
        mode = (t.get("mode") or "H2H").upper()
        total_my += float(t.get("my_score") or 0)
        if mode != "ALL_PLAY":
            total_opp += float(t.get("opp_score") or 0)

        lg_name = t.get("league_name")
        lg_id = t.get("league_id")

        for s in t.get("my_starters", []):
            pid = s.get("player_id")
            if isinstance(pid, str) and pid.startswith("TEAM:"):
                continue
            total = int(s.get("game_seconds", 3600) or 3600)
            rem = int(s.get("seconds_remaining", 0) or 0)
            my_secs_total += total
            my_secs_played += max(0, total - rem)
            starters_my.append({**s, "league": lg_name, "league_id": lg_id})

        if mode != "ALL_PLAY":
            for s in t.get("opp_starters", []):
                pid = s.get("player_id")
                if isinstance(pid, str) and pid.startswith("TEAM:"):
                    continue
                total = int(s.get("game_seconds", 3600) or 3600)
                rem = int(s.get("seconds_remaining", 0) or 0)
                opp_secs_total += total
                opp_secs_played += max(0, total - rem)
                starters_opp.append({**s, "league": lg_name, "league_id": lg_id})

    my_pct = int(round((my_secs_played / my_secs_total) * 100)) if my_secs_total > 0 else 0
    opp_pct = int(round((opp_secs_played / opp_secs_total) * 100)) if opp_secs_total > 0 else 0

    return {
        "my_total_score": round(total_my, 1),
        "opp_total_score": round(total_opp, 1),
        "my_progress_pct": my_pct,
        "opp_progress_pct": opp_pct,
        "my_starters": starters_my,
        "opp_starters": starters_opp,
    }


def _norm_starter(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        pid = item.get("player_id", item.get("pid"))
        score = float(item.get("score", item.get("fp", 0.0)) or 0.0)
        if "seconds_remaining" in item and item["seconds_remaining"] is not None:
            rem = int(item["seconds_remaining"])
        elif "sec_remaining" in item and item["sec_remaining"] is not None:
            rem = int(item["sec_remaining"])
        elif "game_seconds_remaining" in item and item["game_seconds_remaining"] is not None:
            rem = int(item["game_seconds_remaining"])
        else:
            rem = 0
        gs = int(item.get("game_seconds", 3600) or 3600)
    else:
        pid = getattr(item, "player_id", None)
        score = float(getattr(item, "score", 0.0) or 0.0)
        if hasattr(item, "seconds_remaining") and getattr(item, "seconds_remaining") is not None:
            rem = int(getattr(item, "seconds_remaining") or 0)
        elif hasattr(item, "game_seconds_remaining") and getattr(item, "game_seconds_remaining") is not None:
            rem = int(getattr(item, "game_seconds_remaining") or 0)
        else:
            rem = 0
        gs = int(getattr(item, "game_seconds", 3600) or 0) or 3600

    rem = max(0, rem)
    gs = 3600 if gs is None else int(gs) or 3600
    minutes_left = (rem + 59) // 60

    return {
        "player_id": pid,
        "score": score,
        "seconds_remaining": rem,
        "game_seconds": gs,
        "minutes_remaining": minutes_left,
    }


def _normalize_side(side: Any) -> Dict[str, Any]:
    if isinstance(side, dict):
        starters_raw = side.get("starters") or []
        starters = [_norm_starter(s) for s in starters_raw]
        total_secs = sum(int(s.get("game_seconds", 3600) or 0) for s in starters)
        total_left = sum(int(s.get("seconds_remaining", 0) or 0) for s in starters)
        return {
            "franchise_id": side.get("franchise_id", side.get("fid")),
            "name": side.get("name"),
            "score": float(side.get("score") or 0.0),
            "starters_seconds_total": int(total_secs),
            "starters_seconds_left": int(total_left),
            "starters": starters,
        }

    starters_raw = getattr(side, "starters", None) or []
    starters = [_norm_starter(s) for s in starters_raw]
    total_secs = sum(int(s.get("game_seconds", 3600) or 0) for s in starters)
    total_left = sum(int(s.get("seconds_remaining", 0) or 0) for s in starters)
    return {
        "franchise_id": getattr(side, "franchise_id", getattr(side, "fid", None)),
        "name": getattr(side, "name", None),
        "score": float(getattr(side, "score", 0.0) or 0.0),
        "starters_seconds_total": int(total_secs),
        "starters_seconds_left": int(total_left),
        "starters": starters,
    }


def _iter_sides_from_matchup(m: Any) -> List[Any]:
    for a_name, b_name in [
        ("a", "b"),
        ("home", "away"),
        ("one", "two"),
        ("left", "right"),
        ("team1", "team2"),
        ("side1", "side2"),
        ("my", "opp"),
    ]:
        if hasattr(m, a_name) and hasattr(m, b_name):
            return [getattr(m, a_name), getattr(m, b_name)]

    for list_name in ["sides", "participants", "franchises", "teams", "entries"]:
        if hasattr(m, list_name):
            val = getattr(m, list_name)
            if isinstance(val, (list, tuple)) and len(val) >= 2:
                return list(val[:2])

    candidates = []
    for name in dir(m):
        if name.startswith("_"):
            continue
        try:
            v = getattr(m, name)
        except Exception:
            continue
        if isinstance(v, (list, tuple)):
            items = [x for x in v if hasattr(x, "franchise_id") or (isinstance(x, dict) and "franchise_id" in x)]
            if len(items) >= 2:
                return items[:2]
        else:
            if hasattr(v, "franchise_id") or (isinstance(v, dict) and "franchise_id" in v):
                candidates.append(v)
    if len(candidates) >= 2:
        return candidates[:2]
    raise AttributeError("Could not extract matchup sides from parser result")


# ---------- All-Play XML helper ----------
def _parse_allplay_xml(xml: str, my_fid: str, names_map: Dict[str, str]) -> Optional[Dict[str, Any]]:
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        if root.tag != "liveScoring":
            return None
        if root.find("matchup") is not None:
            return None

        franchises = list(root.findall("franchise"))
        if not franchises:
            return None

        week = root.attrib.get("week")
        teams: List[Dict[str, Any]] = []
        for fr in franchises:
            fid = (fr.attrib.get("id") or "").zfill(4)
            score = float(fr.attrib.get("score", "0") or 0)
            starters_total = 0
            starters_left = 0
            players_node = fr.find("players")
            if players_node is not None:
                for p in players_node.findall("player"):
                    if (p.attrib.get("status") or "").lower() == "starter":
                        starters_total += 3600
                        rem = int(p.attrib.get("gameSecondsRemaining", "0") or 0)
                        starters_left += max(0, rem)
            teams.append({
                "franchise_id": fid,
                "name": names_map.get(fid, fid),
                "score": score,
                "starters_seconds_total": starters_total,
                "starters_seconds_left": starters_left,
            })

        my_row = next((t for t in teams if t["franchise_id"] == my_fid), None)
        if my_row is None:
            return None

        my_node = None
        for fr in franchises:
            if (fr.attrib.get("id") or "").zfill(4) == my_fid:
                my_node = fr
                break

        my_starters: List[Dict[str, Any]] = []
        if my_node is not None:
            players_node = my_node.find("players")
            if players_node is not None:
                for p in players_node.findall("player"):
                    if (p.attrib.get("status") or "").lower() == "starter":
                        my_starters.append({
                            "player_id": p.attrib.get("id"),
                            "score": float(p.attrib.get("score", "0") or 0),
                            "seconds_remaining": max(0, int(p.attrib.get("gameSecondsRemaining", "0") or 0)),
                            "game_seconds": 3600,
                        })

        return {
            "mode": "ALL_PLAY",
            "week": week,
            "teams": teams,
            "me": {
                "franchise_id": my_fid,
                "name": my_row["name"],
                "score": float(my_row["score"] or 0),
                "starters": my_starters,
            },
            "opp": {"franchise_id": None, "name": None, "score": 0.0, "starters": []},
        }
    except Exception:
        return None


# ---------- Doubleheader/H2H XML helper ----------
def _parse_h2h_tiles_from_xml(xml: str, my_fid: str, names_map: Dict[str, str]) -> List[Tuple[Dict[str, Any], set]]:
    import xml.etree.ElementTree as ET

    def starters_from_fr(fr_node) -> Tuple[List[Dict[str, Any]], int, int, set]:
        starters: List[Dict[str, Any]] = []
        total = 0
        left = 0
        pids: set[str] = set()
        players_node = fr_node.find("players")
        if players_node is not None:
            for p in players_node.findall("player"):
                if (p.attrib.get("status") or "").lower() != "starter":
                    continue
                pid = p.attrib.get("id")
                pids.add(str(pid))
                rem = int(p.attrib.get("gameSecondsRemaining", "0") or 0)
                starters.append({
                    "player_id": pid,
                    "score": float(p.attrib.get("score", "0") or 0),
                    "seconds_remaining": max(0, rem),
                    "game_seconds": 3600,
                })
                total += 3600
                left += max(0, rem)
        return starters, total, left, pids

    tiles: List[Tuple[Dict[str, Any], set]] = []
    try:
        root = ET.fromstring(xml)
    except Exception:
        return tiles

    if root.tag != "liveScoring":
        return tiles

    week = root.attrib.get("week")
    for mu in root.findall("matchup"):
        fras = mu.findall("franchise")
        if len(fras) < 2:
            continue

        a, b = fras[0], fras[1]
        a_id = (a.attrib.get("id") or "").zfill(4)
        b_id = (b.attrib.get("id") or "").zfill(4)
        if my_fid not in (a_id, b_id):
            continue

        my_node = a if a_id == my_fid else b
        opp_node = b if a_id == my_fid else a

        my_starters, my_total, my_left, my_pids = starters_from_fr(my_node)
        opp_starters, opp_total, opp_left, opp_pids = starters_from_fr(opp_node)

        my_played = max(0, my_total - my_left)
        opp_played = max(0, opp_total - opp_left)
        my_pct = int(round((my_played / my_total) * 100)) if my_total > 0 else 0
        opp_pct = int(round((opp_played / opp_total) * 100)) if opp_total > 0 else 0

        my_score = float(my_node.attrib.get("score", "0") or 0.0)
        opp_score = float(opp_node.attrib.get("score", "0") or 0.0)

        my_name = names_map.get(my_fid, my_fid)
        opp_fid = (opp_node.attrib.get("id") or "").zfill(4)
        opp_name = names_map.get(opp_fid, opp_fid)

        tile = {
            "mode": "H2H",
            "league_id": None,
            "league_name": None,
            "host": None,
            "week": week,
            "my_fid": my_fid,
            "opp_fid": opp_fid,
            "my_team_name": my_name,
            "opp_team_name": opp_name,
            "my_score": round(my_score, 1),
            "opp_score": round(opp_score, 1),
            "my_progress_pct": my_pct,
            "opp_progress_pct": opp_pct,
            "my_starters": my_starters,
            "opp_starters": opp_starters,
        }
        tiles.append((tile, my_pids | opp_pids))

    return tiles


# ---- NFL team code normalizer (for "minutes hint" merge) ----
_TEAM_NORM = {
    "ARI": "ARI",
    "SF": "SFO", "SFO": "SFO",
    "SEA": "SEA",
    "LAR": "LAR",
    "GB": "GBP", "GBP": "GBP",
    "MIN": "MIN",
    "CHI": "CHI",
    "DET": "DET",
    "DAL": "DAL",
    "PHI": "PHI",
    "NYG": "NYG",
    "WAS": "WAS",
    "ATL": "ATL",
    "CAR": "CAR",
    "NO": "NOS", "NOS": "NOS",
    "TB": "TBB", "TBB": "TBB",
    "KC": "KCC", "KCC": "KCC",
    "LAC": "LAC",
    "LV": "LVR", "LVR": "LVR", "OAK": "LVR",
    "DEN": "DEN",
    "BAL": "BAL",
    "PIT": "PIT",
    "CLE": "CLE",
    "CIN": "CIN",
    "BUF": "BUF",
    "MIA": "MIA",
    "NYJ": "NYJ",
    "NE": "NEP", "NEP": "NEP",
    "JAX": "JAC", "JAC": "JAC",
    "IND": "IND",
    "TEN": "TEN",
    "HOU": "HOU",
    "FA": "FA",
}
def _norm_team(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    return _TEAM_NORM.get(str(code).strip().upper(), str(code).strip().upper())


# =============================================================================
# Week selection logic (calendar-based Wednesday cutover)
# =============================================================================

# NFL Week 1 Thursday openers (source: official schedules)
WEEK1_THURSDAY = {
    2024: date(2024, 9, 5),
    2025: date(2025, 9, 4),
}
TOTAL_WEEKS = 18

def _calendar_week_guess(today_utc: Optional[date] = None, year_hint: Optional[int] = None) -> Optional[int]:
    """
    Returns the NFL/Fantasy week with a Wednesday 00:00 UTC cutover:
      Week 1: Wed before NFL opener (Thu) through following Tue
      Week k: previous + 7 days (k = 1..18)
    Example target: 2025-10-01 (Wed) => Week 5; 2025-10-08 (Wed) => Week 6
    """
    d = today_utc or datetime.now(timezone.utc).date()
    y = year_hint or d.year

    opener = WEEK1_THURSDAY.get(y)
    if not opener:
        return None

    cutover_wed = opener - timedelta(days=1)  # Wednesday before the opener
    if d < cutover_wed:
        return None

    delta_days = (d - cutover_wed).days
    week = 1 + (delta_days // 7)
    if week < 1:
        week = 1
    if week > TOTAL_WEEKS:
        week = TOTAL_WEEKS
    return week


def _fallback_sleeper_state_week() -> Optional[int]:
    """Very defensive: ask Sleeper for state.leg/week/display_week if calendar & MFL failed."""
    try:
        client = SleeperClient()
        for meth in ("get_state", "get_state_nfl", "state", "nfl_state", "get_nfl_state"):
            fn = getattr(client, meth, None)
            if not callable(fn):
                continue
            state = fn()
            if not isinstance(state, dict):
                state = {
                    "leg": getattr(state, "leg", None),
                    "week": getattr(state, "week", None),
                    "display_week": getattr(state, "display_week", None),
                }
            for key in ("leg", "week", "display_week"):
                v = state.get(key)
                if v not in (None, "", 0):
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        pass
        return None
    except Exception:
        return None


@login_required
@live_bp.route("/", methods=["GET"])
def live_index():
    if not current_user.is_authenticated:
        return redirect(url_for("auth.login"))

    cache = _get_live_cache(current_user.id)
    if not cache or (_now_ts() - float(cache.get("ts", 0))) > STALE_SECONDS:
        cache = _refresh_all_live()
        _set_live_cache(current_user.id, cache)

    tiles = cache.get("tiles", []) if cache else []
    agg = cache.get("aggregate", {}) if cache else {}
    player_lookup = cache.get("player_lookup", {})
    team_lookup = cache.get("team_lookup", {})
    next_in = max(0, STALE_SECONDS - int(_now_ts() - float(cache.get("ts", 0)))) if cache else 0

    return render_template(
        "live/index.html",
        tiles=tiles,
        aggregate=agg,
        player_lookup=player_lookup,
        team_lookup=team_lookup,
        fetched_at=datetime.fromtimestamp(cache.get("ts", _now_ts()), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if cache else None,
        next_refresh_in=next_in,
        can_expand_aggregate=can_view_aggregate_detail(current_user),
    )


@login_required
@live_bp.route("/refresh", methods=["POST"])
def refresh_live():
    if not current_user.is_authenticated:
        return {"ok": False, "error": "auth"}, 401

    cache = _get_live_cache(current_user.id)
    age = (_now_ts() - float(cache.get("ts", 0))) if cache else 1e9
    if age < STALE_SECONDS:
        return {
            "ok": True,
            "cached": True,
            "next_in": int(STALE_SECONDS - age),
            "count_leagues": len(cache.get("tiles", [])) if cache else 0,
        }

    cache = _refresh_all_live()
    _set_live_cache(current_user.id, cache)
    return {
        "ok": True,
        "cached": False,
        "count_leagues": len(cache.get("tiles", [])),
        "ts": cache.get("ts"),
    }


def _refresh_all_live() -> Dict[str, Any]:
    today = datetime.now(timezone.utc).date()
    year = today.year

    leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id, League.year == year)
        .all()
    )

    league_infos: List[Dict[str, Any]] = []
    team_lookup: Dict[str, Dict[str, str]] = {}
    for lg in leagues:
        host = _league_host(lg) or "api.myfantasyleague.com"
        base_url = f"https://{host}/{lg.year}/"
        cookie = _cookie_for_host(host)
        my_fid = str(lg.franchise_id).zfill(4) if lg.franchise_id else None
        names_map = _team_names_map(lg.id)
        team_lookup[str(lg.mfl_id)] = names_map

        league_infos.append({
            "league_id": lg.mfl_id,
            "league_name": lg.name,
            "year": lg.year,
            "host": host,
            "base_url": base_url,
            "cookie": cookie,
            "my_fid": my_fid,
            "names_map": names_map,
        })

    logger = current_app.logger

    # Host throttling
    host_locks: dict[str, Semaphore] = defaultdict(lambda: Semaphore(1))
    unique_hosts = {info["host"] for info in league_infos}
    max_workers = min(8, max(2, len(unique_hosts)))

    def build_empty_tile(info: Dict[str, Any], note: str = "None Available") -> Dict[str, Any]:
        return {
            "league_id": info.get("league_id"),
            "league_name": info.get("league_name"),
            "host": info.get("host"),
            "week": None,
            "note": note,
            "my_team_name": None,
            "opp_team_name": None,
            "my_score": 0.0,
            "opp_score": 0.0,
            "my_progress_pct": 0,
            "opp_progress_pct": 0,
            "my_starters": [],
            "opp_starters": [],
        }

    # --------- PASS 1: Build MFL tiles (if any MFL leagues) ---------
    mfl_tiles: List[Dict[str, Any]] = []
    mfl_player_ids: set[str] = set()
    week_hint: Optional[int] = None

    def worker(info: Dict[str, Any]) -> Dict[str, Any]:
        host = info["host"]
        my_fid = info["my_fid"]
        names_map = info["names_map"]

        if not my_fid:
            return {"tiles": [build_empty_tile(info)], "player_ids": set()}

        with host_locks[host]:
            try:
                client = MFLClient(year=info["year"], base_url=info["base_url"])
                xml = client._export("liveScoring", params={"L": info["league_id"]}, cookie=info["cookie"])

                # All-Play?
                ap = _parse_allplay_xml(xml, my_fid=my_fid, names_map=names_map)
                if ap and ap.get("mode") == "ALL_PLAY":
                    week = ap.get("week")
                    me = _normalize_side(ap["me"])

                    opp_rows: List[Dict[str, Any]] = []
                    for row in (ap.get("teams") or []):
                        if row["franchise_id"] == my_fid:
                            continue
                        opp_rows.append({
                            "player_id": f"TEAM:{row['franchise_id']}",
                            "score": float(row["score"] or 0),
                            "seconds_remaining": int(row.get("starters_seconds_left", 0) or 0),
                            "game_seconds": int(row.get("starters_seconds_total", 0) or 0),
                            "display_name": row["name"],
                        })
                    opp_rows.sort(key=lambda r: r["score"], reverse=True)

                    my_starters = me.get("starters") or []
                    pids = {str(s.get("player_id")) for s in my_starters if s.get("player_id")}

                    my_total = int(me.get("starters_seconds_total") or 0)
                    my_left = int(me.get("starters_seconds_left") or 0)
                    my_played = max(0, my_total - my_left)
                    my_pct = int(round((my_played / my_total) * 100)) if my_total > 0 else 0

                    tile = {
                        "mode": "ALL_PLAY",
                        "league_id": info["league_id"],
                        "league_name": info["league_name"],
                        "host": info["host"],
                        "week": week,
                        "note": "All Play",
                        "my_fid": my_fid,
                        "opp_fid": None,
                        "my_team_name": me.get("name") or names_map.get(my_fid, my_fid),
                        "opp_team_name": None,
                        "my_score": round(float(me.get("score") or 0.0), 1),
                        "opp_score": 0.0,
                        "my_progress_pct": my_pct,
                        "opp_progress_pct": 0,
                        "my_starters": my_starters,
                        "opp_starters": opp_rows,
                    }
                    return {"tiles": [tile], "player_ids": pids}

                tiles_and_ids = _parse_h2h_tiles_from_xml(xml, my_fid=my_fid, names_map=names_map)

                if not tiles_and_ids:
                    parsed = parse_live_scoring(xml, my_franchise_id=my_fid)
                    tiles_and_ids = []

                    if isinstance(parsed, dict):
                        week = parsed.get("week")
                        me = _normalize_side(parsed.get("me") or {})
                        opp = _normalize_side(parsed.get("opp") or {})
                        tile = {
                            "mode": "H2H",
                            "league_id": None, "league_name": None, "host": None,
                            "week": week,
                            "my_fid": my_fid,
                            "opp_fid": opp.get("franchise_id"),
                            "my_team_name": me.get("name") or names_map.get(my_fid, my_fid),
                            "opp_team_name": opp.get("name") or names_map.get(str(opp.get("franchise_id", "")).zfill(4), str(opp.get("franchise_id", "")).zfill(4)),
                            "my_score": round(float(me.get("score") or 0.0), 1),
                            "opp_score": round(float(opp.get("score") or 0.0), 1),
                            "my_progress_pct": int(round(((int(me.get("starters_seconds_total", 0)) - int(me.get("starters_seconds_left", 0))) / max(1, int(me.get("starters_seconds_total", 0)))) * 100)) if int(me.get("starters_seconds_total", 0)) > 0 else 0,
                            "opp_progress_pct": int(round(((int(opp.get("starters_seconds_total", 0)) - int(opp.get("starters_seconds_left", 0))) / max(1, int(opp.get("starters_seconds_total", 0)))) * 100)) if int(opp.get("starters_seconds_total", 0)) > 0 else 0,
                            "my_starters": me.get("starters") or [],
                            "opp_starters": opp.get("starters") or [],
                        }
                        pids = {str(s.get("player_id")) for s in tile["my_starters"] if s.get("player_id")}
                        pids |= {str(s.get("player_id")) for s in tile["opp_starters"] if s.get("player_id")}
                        tiles_and_ids.append((tile, pids))
                    elif isinstance(parsed, LiveMatchup):
                        try:
                            side_a, side_b = _iter_sides_from_matchup(parsed)
                        except Exception:
                            return {"tiles": [build_empty_tile(info)], "player_ids": set()}
                        def _fid(x: Any) -> Optional[str]:
                            if isinstance(x, dict):
                                fid = x.get("franchise_id")
                            else:
                                fid = getattr(x, "franchise_id", None)
                            return str(fid).zfill(4) if fid is not None else None
                        if _fid(side_a) == my_fid:
                            my_side, opp_side = side_a, side_b
                        elif _fid(side_b) == my_fid:
                            my_side, opp_side = side_b, side_a
                        else:
                            my_side, opp_side = side_a, side_b
                        me = _normalize_side(my_side)
                        opp = _normalize_side(opp_side)
                        week = getattr(parsed, "week", None)
                        tile = {
                            "mode": "H2H",
                            "league_id": None, "league_name": None, "host": None,
                            "week": week,
                            "my_fid": my_fid,
                            "opp_fid": opp.get("franchise_id"),
                            "my_team_name": me.get("name") or names_map.get(my_fid, my_fid),
                            "opp_team_name": opp.get("name") or names_map.get(str(opp.get("franchise_id", "")).zfill(4), str(opp.get("franchise_id", "")).zfill(4)),
                            "my_score": round(float(me.get("score") or 0.0), 1),
                            "opp_score": round(float(opp.get("score") or 0.0), 1),
                            "my_progress_pct": int(round(((int(me.get("starters_seconds_total", 0)) - int(me.get("starters_seconds_left", 0))) / max(1, int(me.get("starters_seconds_total", 0)))) * 100)) if int(me.get("starters_seconds_total", 0)) > 0 else 0,
                            "opp_progress_pct": int(round(((int(opp.get("starters_seconds_total", 0)) - int(opp.get("starters_seconds_left", 0))) / max(1, int(opp.get("starters_seconds_total", 0)))) * 100)) if int(opp.get("starters_seconds_total", 0)) > 0 else 0,
                            "my_starters": me.get("starters") or [],
                            "opp_starters": opp.get("starters") or [],
                        }
                        pids = {str(s.get("player_id")) for s in tile["my_starters"] if s.get("player_id")}
                        pids |= {str(s.get("player_id")) for s in tile["opp_starters"] if s.get("player_id")}
                        tiles_and_ids.append((tile, pids))

            except Exception as e:
                logger.warning("Live scoring fetch failed for league %s: %s", info["league_id"], e)
                return {"tiles": [build_empty_tile(info)], "player_ids": set()}

        tiles: List[Dict[str, Any]] = []
        all_ids: set[str] = set()
        for tile, pids in tiles_and_ids:
            tile["league_id"] = info["league_id"]
            tile["league_name"] = info["league_name"]
            tile["host"] = info["host"]
            tiles.append(tile)
            all_ids |= set(pids)

        if not tiles:
            return {"tiles": [build_empty_tile(info)], "player_ids": set()}

        return {"tiles": tiles, "player_ids": all_ids}

    if league_infos:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(worker, info) for info in league_infos]
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:
                    logger.warning("Live worker crashed: %s", e)
                    continue
                tiles_part = res.get("tiles") or []
                if week_hint is None:
                    for t in tiles_part:
                        if t.get("week"):
                            try:
                                week_hint = int(t.get("week"))
                                break
                            except Exception:
                                pass
                mfl_tiles.extend(tiles_part)
                mfl_player_ids.update(res.get("player_ids") or set())

    # ---- Build base lookup from MFL ids (optional) ----
    base_lookup = _player_lookup([int(x) for x in mfl_player_ids]) if mfl_player_ids else {}

    # ---- Minutes hints from MFL tiles (optional; improves Sleeper seconds remaining) ----
    minutes_hint_by_team: Dict[str, int] = {}
    def consider(team_code: Optional[str], sec_left: int):
        cc = _norm_team(team_code)
        if not cc:
            return
        prev = minutes_hint_by_team.get(cc, 0)
        if sec_left > prev:
            minutes_hint_by_team[cc] = sec_left

    for t in mfl_tiles:
        for side_key in ("my_starters", "opp_starters"):
            for s in t.get(side_key, []) or []:
                pid = s.get("player_id")
                if pid is None:
                    continue
                pl = base_lookup.get(str(pid)) or {}
                team_code = pl.get("team")
                sec = int(s.get("seconds_remaining", 0) or 0)
                consider(team_code, sec)

    # ---- Final week resolution:
    # 1) week_hint from MFL (if any)
    # 2) calendar-based Wednesday cutover
    # 3) Sleeper state (very defensive fallback)
    if week_hint is None:
        week_hint = _calendar_week_guess(today, year)
    if week_hint is None:
        week_hint = _fallback_sleeper_state_week() or 1

    # --------- PASS 2: Sleeper tiles (carry S:<sid> when no mapping) ---------
    sleeper_tiles: List[Dict[str, Any]] = []
    try:
        sl_client = SleeperClient()
        stiles, _ = build_sleeper_tiles_for_user(
            week=week_hint,
            minutes_hint_by_team=minutes_hint_by_team,
            client=sl_client,
        )
        sleeper_tiles = stiles or []
    except Exception as e:
        logger.info("Sleeper live merge skipped: %s", e)

    # Union tiles
    all_tiles = mfl_tiles + sleeper_tiles

    # ---- Unified player display lookup (MFL ids + Sleeper S:<sid>) ----
    def _unified_player_lookup_from_ids(ids: set[str]) -> Dict[str, Dict[str, Any]]:
        if not ids:
            return {}

        mfl_keys: List[str] = []
        mfl_int_ids: List[int] = []
        sleeper_keys_with_prefix: List[str] = []
        sleeper_sids: List[str] = []
        bare_sids: List[str] = []

        for raw in ids:
            s = str(raw)
            if s.startswith("TEAM:"):
                continue
            if s.startswith("S:"):
                sleeper_keys_with_prefix.append(s)
                sleeper_sids.append(s[2:])
                continue
            try:
                mfl_int_ids.append(int(s))
                mfl_keys.append(s)
            except (TypeError, ValueError):
                bare_sids.append(s)

        out: Dict[str, Dict[str, Any]] = {}

        # MFL rows
        if mfl_int_ids:
            mrows = Player.query.filter(Player.id.in_(mfl_int_ids)).all()
            by_id = {int(r.id): r for r in mrows}
            for k in mfl_keys:
                rid = int(k)
                prow = by_id.get(rid)
                if prow:
                    out[k] = {
                        "name": prow.name,
                        "pos": getattr(prow, "position", None) or getattr(prow, "pos", None),
                        "team": getattr(prow, "team", None),
                    }

        # Sleeper rows + linkage
        all_sids = list(set(sleeper_sids + bare_sids))
        sid_rows_by_sid: Dict[str, SleeperPlayer] = {}
        if all_sids:
            srows = SleeperPlayer.query.filter(SleeperPlayer.sleeper_id.in_(all_sids)).all()
            sid_rows_by_sid = {str(sp.sleeper_id): sp for sp in srows}

        def _extract_mid(sp: SleeperPlayer) -> Optional[int]:
            for attr in ("mfl_id", "player_id", "mfl_player_id", "mflid"):
                if hasattr(sp, attr):
                    val = getattr(sp, attr)
                    if val is None:
                        continue
                    try:
                        return int(str(val))
                    except (TypeError, ValueError):
                        continue
            return None

        candidate_mid_ints: Set[int] = set()
        for sp in sid_rows_by_sid.values():
            mid = _extract_mid(sp)
            if mid is not None:
                candidate_mid_ints.add(mid)

        linked_players_by_id: Dict[int, Player] = {}
        if candidate_mid_ints:
            lrows = Player.query.filter(Player.id.in_(list(candidate_mid_ints))).all()
            linked_players_by_id = {int(r.id): r for r in lrows}

        def _from_player(prow: Player) -> Dict[str, Any]:
            return {
                "name": prow.name,
                "pos": getattr(prow, "position", None) or getattr(prow, "pos", None),
                "team": getattr(prow, "team", None),
            }

        def _from_sleeper(sp: SleeperPlayer) -> Dict[str, Any]:
            return {
                "name": getattr(sp, "name", None),
                "pos": getattr(sp, "position", None),
                "team": getattr(sp, "team", None),
            }

        for key in sleeper_keys_with_prefix:
            sid = key[2:]
            sp = sid_rows_by_sid.get(sid)
            if not sp:
                continue
            mid = _extract_mid(sp)
            prow = linked_players_by_id.get(mid) if mid is not None else None
            out[key] = _from_player(prow) if prow else _from_sleeper(sp)

        for sid in bare_sids:
            sp = sid_rows_by_sid.get(sid)
            if not sp:
                continue
            mid = _extract_mid(sp)
            prow = linked_players_by_id.get(mid) if mid is not None else None
            out[sid] = _from_player(prow) if prow else _from_sleeper(sp)

        return out

    # Collect ALL starter ids from all tiles (includes "S:<sid>" and TEAM: rows)
    all_ids: set[str] = set()
    for t in all_tiles:
        for side_key in ("my_starters", "opp_starters"):
            for s in (t.get(side_key) or []):
                pid = s.get("player_id")
                if pid is not None:
                    all_ids.add(str(pid))

    lookup = _unified_player_lookup_from_ids(all_ids)

    aggregate = _aggregate_from_tiles(all_tiles)

    cache = {
        "ts": _now_ts(),
        "tiles": all_tiles,
        "player_lookup": lookup,
        "team_lookup": team_lookup,
        "aggregate": aggregate,
    }
    return cache
