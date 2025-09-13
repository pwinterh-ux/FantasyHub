# fantasyhub/live/routes.py
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock, Semaphore

from flask import Blueprint, render_template, current_app, session, redirect, url_for
from flask_login import login_required, current_user

from app import db
from models import League, Team, Player
from services.mfl_client import MFLClient
from services.mfl_live import parse_live_scoring, LiveMatchup  # type: ignore

live_bp = Blueprint("live", __name__, url_prefix="/live")

CACHE_KEY = "live_cache"
STALE_SECONDS = 300  # 5 minutes

# --- lightweight server-side cache for live scoring (per-process) ---
_LIVE_CACHE_STORE: dict[int, dict] = {}
_LIVE_CACHE_LOCK = Lock()
_LIVE_CACHE_MAX_USERS = 200   # soft cap to avoid unbounded growth


def _get_live_cache(user_id: int) -> dict | None:
    with _LIVE_CACHE_LOCK:
        return _LIVE_CACHE_STORE.get(user_id)


def _set_live_cache(user_id: int, payload: dict) -> None:
    with _LIVE_CACHE_LOCK:
        if len(_LIVE_CACHE_STORE) >= _LIVE_CACHE_MAX_USERS and user_id not in _LIVE_CACHE_STORE:
            # simple eviction of an arbitrary (first) user to keep bounded
            _LIVE_CACHE_STORE.pop(next(iter(_LIVE_CACHE_STORE)))
        _LIVE_CACHE_STORE[user_id] = payload


def _now_ts() -> float:
    return time.time()


def _league_host(lg: League) -> Optional[str]:
    """
    Best-effort host for per-league requests (e.g., 'www47.myfantasyleague.com').
    Tries league_host/host/base_url and normalizes to hostname.
    """
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
    """
    Prefer per-host cookie; fall back to API cookie. Checks session and current_user storage.
    """
    if not host:
        host = "api.myfantasyleague.com"

    # session keys (legacy)
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

    # per-user cookie bundle (used by trades flow)
    try:
        host_cookies = current_user.get_mfl_host_cookies()
        if host in host_cookies and host_cookies[host]:
            return host_cookies[host]
    except Exception:
        pass

    # fallback
    return getattr(current_user, "mfl_cookie_api", None)


def _team_names_map(league_id: int) -> Dict[str, str]:
    """{franchise_id(str4): team_name} for a league."""
    out: Dict[str, str] = {}
    for t in Team.query.filter(Team.league_id == league_id).all():
        if t.mfl_id:
            out[str(t.mfl_id).zfill(4)] = t.name or str(t.mfl_id).zfill(4)
    return out


def _player_lookup(player_ids: List[int]) -> Dict[str, Dict[str, Any]]:
    """Return {player_id(str): {name,pos,team}} for display."""
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
    """
    Build the top 'Roster Showdown' totals + progress from all starters.
    Also annotates each starter with league name/id for display & sorting.
    """
    total_my = 0.0
    total_opp = 0.0
    my_secs_total = 0
    my_secs_played = 0
    opp_secs_total = 0
    opp_secs_played = 0
    starters_my: List[Dict[str, Any]] = []
    starters_opp: List[Dict[str, Any]] = []

    for t in tiles:
        total_my += float(t.get("my_score") or 0)
        total_opp += float(t.get("opp_score") or 0)

        lg_name = t.get("league_name")
        lg_id = t.get("league_id")

        for s in t.get("my_starters", []):
            total = int(s.get("game_seconds", 3600) or 3600)
            rem = int(s.get("seconds_remaining", 0) or 0)
            my_secs_total += total
            my_secs_played += max(0, total - rem)
            starters_my.append({**s, "league": lg_name, "league_id": lg_id})

        for s in t.get("opp_starters", []):
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
    """
    Normalize a starter (object or dict) to a dict the templates expect.
    Supports seconds remaining from several field names.
    """
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
        gs = int(getattr(item, "game_seconds", 3600) or 3600)

    rem = max(0, rem)
    gs = 3600 if gs is None else int(gs) or 3600  # NFL game assumed 60 min
    minutes_left = (rem + 59) // 60

    return {
        "player_id": pid,
        "score": score,
        "seconds_remaining": rem,
        "game_seconds": gs,
        "minutes_remaining": minutes_left,
    }


def _normalize_side(side: Any) -> Dict[str, Any]:
    """
    Convert any side shape (dict or object) into the payload the template expects.
    Computes starters' total/left seconds from normalized starters.
    """
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
    """
    Pull two sides from a LiveMatchup-like object, regardless of attribute names.
    """
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


@login_required
@live_bp.route("/", methods=["GET"])
def live_index():
    # Hard guard to avoid sporadic AnonymousUser access before decorator runs
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
    )


@login_required
@live_bp.route("/refresh", methods=["POST"])
def refresh_live():
    # Hard guard to avoid sporadic AnonymousUser access before decorator runs
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
    year = datetime.now(timezone.utc).year
    leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id, League.year == year)
        .all()
    )

    # ---- Precompute everything needed in worker threads (no DB inside threads) ----
    league_infos: List[Dict[str, Any]] = []
    team_lookup: Dict[str, Dict[str, str]] = {}
    for lg in leagues:
        host = _league_host(lg) or "api.myfantasyleague.com"
        base_url = f"https://{host}/{lg.year}/"
        cookie = _cookie_for_host(host)  # read from session/current_user on main thread
        my_fid = str(lg.franchise_id).zfill(4) if lg.franchise_id else None
        names_map = _team_names_map(lg.id)  # DB read on main thread
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

    # Early out when no leagues
    if not league_infos:
        cache = {
            "ts": _now_ts(),
            "tiles": [],
            "player_lookup": {},
            "team_lookup": {},
            "aggregate": {"my_total_score": 0, "opp_total_score": 0, "my_progress_pct": 0, "opp_progress_pct": 0,
                          "my_starters": [], "opp_starters": []},
        }
        return cache

    logger = current_app.logger

    # Concurrency: parallel across hosts, 1-at-a-time per host
    host_locks: dict[str, Semaphore] = defaultdict(lambda: Semaphore(1))
    unique_hosts = {info["host"] for info in league_infos}
    max_workers = min(8, max(2, len(unique_hosts)))  # small pool; IO-bound

    def build_empty_tile(info: Dict[str, Any], note: str = "None Available") -> Dict[str, Any]:
        return {
            "league_id": info["league_id"],
            "league_name": info["league_name"],
            "host": info["host"],
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

    def worker(info: Dict[str, Any]) -> Dict[str, Any]:
        # Returns {"tile": tile_dict, "player_ids": set_of_ids}
        host = info["host"]
        my_fid = info["my_fid"]
        names_map = info["names_map"]

        # If franchise id missing, we can't render a proper matchup
        if not my_fid:
            return {"tile": build_empty_tile(info), "player_ids": set()}

        # Serialize per-host calls
        with host_locks[host]:
            try:
                client = MFLClient(year=info["year"], base_url=info["base_url"])
                xml = client._export("liveScoring", params={"L": info["league_id"]}, cookie=info["cookie"])
                parsed = parse_live_scoring(xml, my_franchise_id=my_fid)
            except Exception as e:
                logger.warning("Live scoring fetch failed for league %s: %s", info["league_id"], e)
                return {"tile": build_empty_tile(info), "player_ids": set()}

        # ---- Normalize parser output to me/opp/week dicts ----
        if isinstance(parsed, dict):
            week = parsed.get("week")
            me = _normalize_side(parsed.get("me") or {})
            opp = _normalize_side(parsed.get("opp") or {})
        elif isinstance(parsed, LiveMatchup):
            week = getattr(parsed, "week", None)
            try:
                side_a, side_b = _iter_sides_from_matchup(parsed)
            except Exception as e:
                logger.warning("Could not extract sides for league %s: %s", info["league_id"], e)
                return {"tile": build_empty_tile(info), "player_ids": set()}
            # pick my side by franchise id
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
        else:
            logger.warning("Unexpected live parser result type for league %s: %r", info["league_id"], type(parsed))
            return {"tile": build_empty_tile(info), "player_ids": set()}

        # Names
        my_name = me.get("name") or names_map.get(my_fid, my_fid)
        if not my_name:
            my_name = my_fid
        opp_name = opp.get("name")
        if not opp_name:
            opp_id = opp.get("franchise_id")
            opp_name = names_map.get(str(opp_id).zfill(4), str(opp_id).zfill(4)) if opp_id else None

        # Scores
        my_score = float(me.get("score") or 0.0)
        opp_score = float(opp.get("score") or 0.0)

        # Progress pct
        my_total = int(me.get("starters_seconds_total") or 0)
        my_left = int(me.get("starters_seconds_left") or 0)
        opp_total = int(opp.get("starters_seconds_total") or 0)
        opp_left = int(opp.get("starters_seconds_left") or 0)

        my_played = max(0, my_total - my_left)
        opp_played = max(0, opp_total - opp_left)

        my_pct = int(round((my_played / my_total) * 100)) if my_total > 0 else 0
        opp_pct = int(round((opp_played / opp_total) * 100)) if opp_total > 0 else 0

        # Starters and collect player ids
        my_starters = me.get("starters") or []
        opp_starters = opp.get("starters") or []
        pids: set[str] = set()
        for s in my_starters:
            pid = s.get("player_id")
            if pid is not None:
                pids.add(str(pid))
        for s in opp_starters:
            pid = s.get("player_id")
            if pid is not None:
                pids.add(str(pid))

        tile = {
            "league_id": info["league_id"],
            "league_name": info["league_name"],
            "host": info["host"],
            "week": week,
            "my_fid": my_fid,
            "opp_fid": opp.get("franchise_id"),
            "my_team_name": my_name,
            "opp_team_name": opp_name,
            "my_score": round(my_score, 1),
            "opp_score": round(opp_score, 1),
            "my_progress_pct": my_pct,
            "opp_progress_pct": opp_pct,
            "my_starters": my_starters,
            "opp_starters": opp_starters,
        }
        return {"tile": tile, "player_ids": pids}

    # Run workers
    tiles: List[Dict[str, Any]] = []
    all_player_ids: set[str] = set()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = [ex.submit(worker, info) for info in league_infos]
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as e:
                logger.warning("Live worker crashed: %s", e)
                continue
            tiles.append(res.get("tile") or {})
            all_player_ids.update(res.get("player_ids") or set())

    # Player lookup after all results (DB on main thread)
    lookup = _player_lookup([int(x) for x in all_player_ids]) if all_player_ids else {}
    aggregate = _aggregate_from_tiles(tiles)

    cache = {
        "ts": _now_ts(),
        "tiles": tiles,
        "player_lookup": lookup,
        "team_lookup": team_lookup,
        "aggregate": aggregate,
    }
    return cache
