# services/sleeper_live.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from flask_login import current_user

from app import db
from models import SleeperLeague, SleeperTeam, SleeperRoster, SleeperPlayer
from services.sleeper_client import SleeperClient

# ----------------------------------------------------------------------
# Team code normalization (canonical 3-letter codes that match Player.team)
# ----------------------------------------------------------------------
TEAM_NORM: Dict[str, str] = {
    # NFC West / West-ish
    "ARI": "ARI",
    "SF": "SFO", "SFO": "SFO",
    "SEA": "SEA",
    "LAR": "LAR",
    # NFC North
    "GB": "GBP", "GBP": "GBP",
    "MIN": "MIN",
    "CHI": "CHI",
    "DET": "DET",
    # NFC East
    "DAL": "DAL",
    "PHI": "PHI",
    "NYG": "NYG",
    "WAS": "WAS",
    # NFC South
    "ATL": "ATL",
    "CAR": "CAR",
    "NO": "NOS", "NOS": "NOS",
    "TB": "TBB", "TBB": "TBB",
    # AFC West
    "KC": "KCC", "KCC": "KCC",
    "LAC": "LAC",
    "LV": "LVR", "LVR": "LVR", "OAK": "LVR",  # legacy
    "DEN": "DEN",
    # AFC North
    "BAL": "BAL",
    "PIT": "PIT",
    "CLE": "CLE",
    "CIN": "CIN",
    # AFC East
    "BUF": "BUF",
    "MIA": "MIA",
    "NYJ": "NYJ",
    "NE": "NEP", "NEP": "NEP",
    # AFC South
    "JAX": "JAC", "JAC": "JAC",
    "IND": "IND",
    "TEN": "TEN",
    "HOU": "HOU",
    # Free agent / unknown
    "FA": "FA",
}

def normalize_team(code: Optional[str]) -> Optional[str]:
    if not code:
        return None
    c = str(code).strip().upper()
    return TEAM_NORM.get(c, c)


@dataclass
class SleeperStarter:
    # Kept for reference; we build dicts for template compatibility
    player_id: Optional[int]  # MFL player id if available; None if unknown
    score: float
    seconds_remaining: int
    game_seconds: int = 3600


def _seconds_from_hint(player_team: Optional[str], minutes_hint_by_team: Dict[str, int]) -> int:
    """
    Infer seconds remaining for a Sleeper player using the MFL-derived hint:
    minutes_hint_by_team is expected to be {CANONICAL_TEAM: seconds_left_estimate}.

    If we don't have a hint for the player's NFL team, returns 0.
    """
    t = normalize_team(player_team)
    if not t:
        return 0
    return int(minutes_hint_by_team.get(t, 0) or 0)


def _collect_sid_to_player(sids: Iterable[str]) -> Dict[str, SleeperPlayer]:
    if not sids:
        return {}
    rows = (
        db.session.query(SleeperPlayer)
        .filter(SleeperPlayer.sleeper_id.in_(list(set(map(str, sids)))))
        .all()
    )
    return {r.sleeper_id: r for r in rows}


def _mk_starters(
    sid_list: List[str],
    points_map: Dict[str, Any],
    sid_to_player: Dict[str, SleeperPlayer],
    minutes_hint_by_team: Dict[str, int],
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """
    Build starters array compatible with templates:
    Each row has:
      {
        "player_id": <MFL id int> OR "S:<sleeper_id>" (fallback),
        "sid": <sleeper_id str>,
        "mfl_id": <MFL id int or None>,
        "score": float,
        "seconds_remaining": int,
        "game_seconds": 3600
      }

    Returns (starters, mfl_player_ids_as_strings) so callers can DB-lookup names.
    """
    starters: List[Dict[str, Any]] = []
    pids: Set[str] = set()

    for sid in sid_list or []:
        sid_str = str(sid)
        sp = sid_to_player.get(sid_str)

        # Score: points_map uses sleeper_id keys as strings
        raw = points_map.get(sid_str, 0.0)
        try:
            score = float(raw or 0.0)
        except (TypeError, ValueError):
            score = 0.0

        # Seconds remaining inferred by team from MFL minutes index
        team_code = getattr(sp, "team", None)
        sec_left = _seconds_from_hint(team_code, minutes_hint_by_team)

        # Map to MFL player id if known
        mfl_id_val: Optional[int] = None
        mid = getattr(sp, "mfl_id", None)
        if mid is not None:
            try:
                mfl_id_val = int(str(mid))
            except (TypeError, ValueError):
                mfl_id_val = None

        # Always include a stable id for the UI. Prefer MFL id; otherwise synthesize "S:<sid>"
        player_id_value: Any = mfl_id_val if mfl_id_val is not None else f"S:{sid_str}"

        entry = {
            "player_id": player_id_value,
            "sid": sid_str,          # carry raw Sleeper id for debugging / future use
            "mfl_id": mfl_id_val,    # optional explicit link for debugging
            "score": round(score, 2),
            "seconds_remaining": max(0, int(sec_left or 0)),
            "game_seconds": 3600,
        }
        starters.append(entry)

        # Only collect numeric ids for DB lookup (Player table is keyed by int id)
        if mfl_id_val is not None:
            pids.add(str(mfl_id_val))

    return starters, pids


def _progress_pct(starters: List[Dict[str, Any]]) -> int:
    total = sum(int(s.get("game_seconds", 3600) or 0) for s in starters)
    left  = sum(int(s.get("seconds_remaining", 0) or 0) for s in starters)
    if total <= 0:
        return 0
    played = max(0, total - left)
    return int(round((played / total) * 100))


def _pick_my_and_opp(
    league: SleeperLeague,
    matchups: List[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """
    From Sleeper /matchups response, find my roster row and the opposing roster row.
    Assumes no doubleheaders in Sleeper.
    """
    # Determine my roster_id from DB (preferred)
    my_team: Optional[SleeperTeam] = None
    for t in league.teams:
        if t.user_id and current_user.is_authenticated and t.user_id == current_user.id:
            my_team = t
            break
    if not my_team:
        # Fallback: no explicit binding -> try the first roster that has a current_opponent_id
        if league.teams:
            my_team = next(iter(league.teams), None)
    if not my_team:
        return None

    my_rid = my_team.sleeper_roster_id
    if my_rid is None:
        return None

    # Build matchup_id -> rows, and locate my row
    by_mid: Dict[int, List[Dict[str, Any]]] = {}
    my_row: Optional[Dict[str, Any]] = None
    for row in matchups or []:
        mid = row.get("matchup_id")
        rid = row.get("roster_id")
        if mid is None or rid is None:
            continue
        by_mid.setdefault(int(mid), []).append(row)
        if int(rid) == int(my_rid):
            my_row = row

    if not my_row:
        return None

    mid = int(my_row.get("matchup_id"))
    rows = by_mid.get(mid, [])
    opp_row = None
    if len(rows) == 2:
        opp_row = rows[0] if int(rows[1].get("roster_id")) == int(my_rid) else rows[1]
    else:
        # multi-team oddity; pick the first different roster
        for r in rows:
            if int(r.get("roster_id")) != int(my_rid):
                opp_row = r
                break
    if not opp_row:
        return None

    return my_row, opp_row


def build_sleeper_tiles_for_user(
    *,
    week: int,
    minutes_hint_by_team: Dict[str, int],
    client: SleeperClient,
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    """
    Build Sleeper live tiles for the current user for a given MFL week.
    - Uses Sleeper matchups to get starters + points.
    - Infers minutes remaining per starter via minutes_hint_by_team (from MFL).
    Returns (tiles, player_ids) where player_ids are **MFL ids** (strings) for lookup;
    we still include "S:<sid>" player_id strings on rows that have no mapping so the UI
    can render '#S:...' instead of blanks.
    """
    # Pull user's enabled Sleeper leagues (current season already reflected in DB)
    leagues: List[SleeperLeague] = (
        db.session.query(SleeperLeague)
        .filter(SleeperLeague.user_id == current_user.id)
        .all()
    )

    tiles: List[Dict[str, Any]] = []
    all_ids: Set[str] = set()

    for lg in leagues:
        # Fetch matchups for the requested week
        try:
            rows = client.get_matchups(lg.sleeper_id, week) or []
        except Exception:
            # If Sleeper API fails for this league, skip gracefully
            tiles.append({
                "mode": "H2H",
                "league_id": lg.sleeper_id,
                "league_name": lg.name,
                "host": "sleeper",
                "week": week,
                "note": "Unavailable",
                "my_team_name": None,
                "opp_team_name": None,
                "my_score": 0.0,
                "opp_score": 0.0,
                "my_progress_pct": 0,
                "opp_progress_pct": 0,
                "my_starters": [],
                "opp_starters": [],
            })
            continue

        if not rows:
            tiles.append({
                "mode": "H2H",
                "league_id": lg.sleeper_id,
                "league_name": lg.name,
                "host": "sleeper",
                "week": week,
                "note": "None Available",
                "my_team_name": None,
                "opp_team_name": None,
                "my_score": 0.0,
                "opp_score": 0.0,
                "my_progress_pct": 0,
                "opp_progress_pct": 0,
                "my_starters": [],
                "opp_starters": [],
            })
            continue

        picked = _pick_my_and_opp(lg, rows)
        if not picked:
            tiles.append({
                "mode": "H2H",
                "league_id": lg.sleeper_id,
                "league_name": lg.name,
                "host": "sleeper",
                "week": week,
                "note": "Not in matchup",
                "my_team_name": None,
                "opp_team_name": None,
                "my_score": 0.0,
                "opp_score": 0.0,
                "my_progress_pct": 0,
                "opp_progress_pct": 0,
                "my_starters": [],
                "opp_starters": [],
            })
            continue

        my_row, opp_row = picked

        # Resolve names from DB
        my_team: Optional[SleeperTeam] = None
        opp_team: Optional[SleeperTeam] = None
        for t in lg.teams:
            if int(t.sleeper_roster_id or -1) == int(my_row.get("roster_id")):
                my_team = t
            if int(t.sleeper_roster_id or -1) == int(opp_row.get("roster_id")):
                opp_team = t

        my_name = (my_team.name if my_team else None) or "Me"
        opp_name = (opp_team.name if opp_team else None) or "Opponent"

        # Build sid -> SleeperPlayer index (just for the starters we need)
        my_sids: List[str] = [str(x) for x in (my_row.get("starters") or []) if x is not None]
        opp_sids: List[str] = [str(x) for x in (opp_row.get("starters") or []) if x is not None]
        sid_to_player = _collect_sid_to_player(my_sids + opp_sids)

        # Score maps
        my_pts_map: Dict[str, Any] = my_row.get("players_points") or {}
        opp_pts_map: Dict[str, Any] = opp_row.get("players_points") or {}

        # Build starters with inferred minutes + collect MFL ids
        my_starters, my_ids = _mk_starters(my_sids, my_pts_map, sid_to_player, minutes_hint_by_team)
        opp_starters, opp_ids = _mk_starters(opp_sids, opp_pts_map, sid_to_player, minutes_hint_by_team)
        all_ids |= my_ids | opp_ids

        # Team scores from Sleeper
        def _to_f(v: Any) -> float:
            try:
                return float(v or 0.0)
            except (TypeError, ValueError):
                return 0.0

        my_score = _to_f(my_row.get("points"))
        opp_score = _to_f(opp_row.get("points"))

        tile = {
            "mode": "H2H",
            "league_id": lg.sleeper_id,
            "league_name": lg.name,
            "host": "sleeper",  # used by template to show ⚡
            "week": week,
            "note": None,
            "my_fid": None,
            "opp_fid": None,
            "my_team_name": my_name,
            "opp_team_name": opp_name,
            "my_score": round(my_score, 1),
            "opp_score": round(opp_score, 1),
            "my_progress_pct": _progress_pct(my_starters),
            "opp_progress_pct": _progress_pct(opp_starters),
            "my_starters": my_starters,
            "opp_starters": opp_starters,
        }
        tiles.append(tile)

    return tiles, all_ids
