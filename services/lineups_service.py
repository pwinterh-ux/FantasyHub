from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import requests

from app import db
from models import League, Team, Roster, Player
from services.mfl_client import MFLClient
from services.mfl_parsers import (
    parse_league_info,
    parse_rosters_fallback,
    LINEUP_MODE_UNKNOWN,
    ROSTER_STATUS_ACTIVE,
    ROSTER_STATUS_IR,
    ROSTER_STATUS_TAXI,
    ROSTER_STATUS_UNKNOWN,
)
from services.mfl_sync import sync_league_info

log = logging.getLogger(__name__)
ROSTER_STATUS_TTL_SECONDS = 300

# ---------------------------- Data types ------------------------------------

@dataclass
class Projection:
    player_id: int
    projected: Optional[float]  # None if blank


# ---------------------------- Small helpers ---------------------------------

def _zpad4(x: int | str) -> str:
    """
    Zero-pad purely-numeric IDs to AT LEAST 4 chars.
    Examples: '151' -> '0151', '9431' -> '9431', '10424' -> '10424'
    """
    s = str(x).strip()
    if not s.isdigit():
        return s
    return s if len(s) >= 4 else s.zfill(4)

def _norm_host(host: str | None) -> Optional[str]:
    if not host:
        return None
    h = host.strip()
    if h.startswith("http://"):
        h = h[7:]
    elif h.startswith("https://"):
        h = h[8:]
    return h.rstrip("/")

def _base_url(host: str, year: int | str) -> str:
    return f"https://{_norm_host(host)}/{year}"

def _players_csv(ids: List[int | str]) -> str:
    # Build the comma-separated list using zero-padded-to-4 IDs
    return ",".join(_zpad4(i) for i in ids if i is not None)

def _encode_params_with_commas(params: dict) -> str:
    # Encode values while keeping commas as %2C explicitly
    return "&".join(f"{k}={quote_plus(str(v), safe=',')}" for k, v in params.items())


# ----------------------- DB-backed roster gather ----------------------------

def get_my_team_player_ids(league_id_pk: int) -> List[int]:
    """
    Return ALL rostered player IDs (ints) for the user's franchise
    in the given League (by DB primary key), using Team.mfl_id == League.franchise_id.
    Includes Taxi and IR players regardless of their stored roster status.
    """
    league: League | None = db.session.get(League, league_id_pk)
    if not league:
        return []

    team: Team | None = (
        db.session.query(Team)
        .filter(Team.league_id == league.id, Team.mfl_id == league.franchise_id)
        .first()
    )
    if not team:
        return []

    rows: List[Roster] = db.session.query(Roster).filter(Roster.team_id == team.id).all()
    player_ids: List[int] = []
    for r in rows:
        try:
            if r.player_id is not None:
                player_ids.append(int(r.player_id))
        except Exception:
            continue
    return player_ids


def is_lineup_eligible_status(status: Optional[str]) -> bool:
    return str(status or ROSTER_STATUS_UNKNOWN).upper() == ROSTER_STATUS_ACTIVE


def get_my_team_roster(league_id_pk: int) -> List[Roster]:
    league = db.session.get(League, league_id_pk)
    if not league:
        return []
    team = db.session.query(Team).filter_by(
        league_id=league.id, mfl_id=league.franchise_id
    ).first()
    return db.session.query(Roster).filter_by(team_id=team.id).all() if team else []


def get_my_team_roster_statuses(league_id_pk: int) -> Dict[int, str]:
    return {
        int(row.player_id): str(row.roster_status or ROSTER_STATUS_UNKNOWN).upper()
        for row in get_my_team_roster(league_id_pk)
    }


def get_my_team_active_player_ids(league_id_pk: int) -> List[int]:
    return [pid for pid, status in get_my_team_roster_statuses(league_id_pk).items()
            if is_lineup_eligible_status(status)]


def roster_status_is_fresh(league: League, *, now: Optional[datetime] = None) -> bool:
    synced_at = league.roster_status_synced_at
    return bool(synced_at and (now or datetime.utcnow()) - synced_at <= timedelta(seconds=ROSTER_STATUS_TTL_SECONDS))


def ensure_lineup_mode_resolved(
    league: League, *, host: str, cookie: Optional[str]
) -> str:
    """Resolve a migrated UNKNOWN lineup mode once, retaining manual fallback on failure."""
    current_mode = str(league.lineup_mode or LINEUP_MODE_UNKNOWN).upper()
    if current_mode != LINEUP_MODE_UNKNOWN:
        return current_mode

    try:
        client = MFLClient(year=league.year, base_url=f"https://{_norm_host(host)}/{league.year}/")
        info_xml = client.get_league_info(
            league.mfl_id,
            cookie or "",
            context={"operation": "lineup_mode_self_heal", "league_id": str(league.mfl_id)},
        )
        franchise_meta, roster_slots, _base_url, ir_slots_max, lineup_mode, taxi_slots_max = (
            parse_league_info(info_xml)
        )
        sync_league_info(
            league,
            franchise_meta,
            roster_slots=roster_slots,
            ir_slots_max=ir_slots_max,
            lineup_mode=lineup_mode,
            taxi_slots_max=taxi_slots_max,
            commit=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        log.exception(
            "Unable to refresh MFL lineup mode for league_id=%s mfl_id=%s; "
            "proceeding with manual-lineup fallback",
            league.id,
            league.mfl_id,
        )
        return LINEUP_MODE_UNKNOWN

    resolved_mode = str(league.lineup_mode or LINEUP_MODE_UNKNOWN).upper()
    if resolved_mode == LINEUP_MODE_UNKNOWN:
        log.warning(
            "Unable to resolve MFL lineup mode for league_id=%s mfl_id=%s; "
            "proceeding with manual-lineup fallback",
            league.id,
            league.mfl_id,
        )
    return resolved_mode


def ensure_roster_status_fresh(
    league: League, *, host: str, cookie: Optional[str], now: Optional[datetime] = None
) -> Tuple[bool, Optional[str]]:
    """Refresh roster locations with one MFL rosters request when the five-minute TTL expires."""
    if roster_status_is_fresh(league, now=now):
        return True, None
    try:
        client = MFLClient(year=league.year, base_url=f"https://{_norm_host(host)}/{league.year}/")
        assets_list = parse_rosters_fallback(client.get_rosters(league.mfl_id, cookie or ""))
        wanted_fid = _zpad4(league.franchise_id or "")
        assets = next((item for item in assets_list if item.franchise_id == wanted_fid), None)
        if assets is None:
            raise RuntimeError("MFL roster response did not include your franchise")
        team = db.session.query(Team).filter_by(league_id=league.id, mfl_id=wanted_fid).first()
        if not team:
            raise RuntimeError("Your MFL franchise is not present in the local league")

        incoming = {entry.player_id: entry.roster_status for entry in (assets.roster_players or [])}
        existing = {row.player_id: row for row in db.session.query(Roster).filter_by(team_id=team.id).all()}
        for player_id, status in incoming.items():
            if not db.session.get(Player, player_id):
                db.session.add(Player(id=player_id, mfl_id=str(player_id), name=f"Player #{player_id}"))
            row = existing.get(player_id)
            if row is None:
                row = Roster(team_id=team.id, player_id=player_id, is_starter=False)
                db.session.add(row)
            row.roster_status = status
            row.in_ir = status == ROSTER_STATUS_IR
        for player_id, row in existing.items():
            if player_id not in incoming:
                db.session.delete(row)
        league.roster_status_synced_at = now or datetime.utcnow()
        db.session.commit()
        return True, None
    except Exception as exc:
        db.session.rollback()
        log.exception("MFL roster-status refresh failed for league %s", league.mfl_id)
        return False, f"Could not refresh current Taxi/IR status: {exc}"


def validate_lineup_starters(league_id_pk: int, submitted: List[int]) -> Optional[str]:
    """Reject any non-owned or non-ACTIVE starter instead of silently changing a lineup."""
    statuses = get_my_team_roster_statuses(league_id_pk)
    for player_id in submitted:
        if player_id not in statuses:
            return f"Lineup not submitted: player {player_id} is not on your roster."
        status = statuses[player_id]
        if not is_lineup_eligible_status(status):
            player = db.session.get(Player, player_id)
            name = player.name if player and player.name else f"Player {player_id}"
            label = {ROSTER_STATUS_TAXI: "Taxi Squad", ROSTER_STATUS_IR: "Injured Reserve"}.get(
                status, "an unknown roster status"
            )
            return f"Lineup not submitted: {name} is currently on {label}."
    return None


# ----------------------- Projected scores (XML) -----------------------------

def fetch_projected_scores(
    host: str,
    league_mfl_id: str | int,
    year: int | str,
    week: int | str,
    player_ids: List[int | str],
    *,
    cookie: Optional[str] = None,
    timeout: int = 20,
) -> Dict[int, Projection]:
    base = _base_url(str(host), year)
    players_param = _players_csv(player_ids)
    params = {
        "TYPE": "projectedScores",
        "L": str(league_mfl_id),
        "W": str(week),
        "PLAYERS": players_param,
        "JSON": "0",
    }
    qs = _encode_params_with_commas(params)
    url = f"{base}/export?{qs}"

    headers = {
        "User-Agent": "FantasyHub/1.0 (+projected-scores)",
        "Accept": "application/xml,text/xml,*/*;q=0.8",
    }
    if cookie:
        headers["Cookie"] = cookie

    log.debug("MFL projectedScores GET %s", url)

    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()

    out: Dict[int, Projection] = {}
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        root = ET.Element("projectedScores")

    for ps in root.findall(".//playerScore"):
        pid = ps.get("id")
        raw = ps.get("score")
        if not pid:
            continue
        try:
            pid_i = int(pid)
        except Exception:
            try:
                pid_i = int((pid or "").lstrip("0") or "0")
            except Exception:
                continue

        if raw is None or str(raw).strip() == "":
            projected: Optional[float] = None
        else:
            try:
                projected = float(raw)
            except Exception:
                projected = None

        out[pid_i] = Projection(player_id=pid_i, projected=projected)

    # Backfill requested ids not present in response
    for any_id in player_ids:
        try:
            pid_i = int(str(any_id))
        except Exception:
            continue
        if pid_i not in out:
            out[pid_i] = Projection(player_id=pid_i, projected=None)

    return out


# ----------------------------- Submit lineup --------------------------------

def submit_lineup(
    host: str,
    league_mfl_id: str | int,
    year: int | str,
    week: int | str,
    starters_player_ids: List[int | str],
    *,
    cookie: Optional[str] = None,
    timeout: int = 20,
) -> Tuple[bool, str]:
    """
    GET https://{host}/{year}/import?TYPE=lineup&L={L}&W={W}&STARTERS=pid1,pid2,...
    SUCCESS only if the body contains XML <status>OK</status>.
    We DO NOT pass FRANCHISE_ID (session auth).
    """
    base = _base_url(str(host), year)
    url = f"{base}/import"
    params = dict(
        TYPE="lineup",
        L=str(league_mfl_id),
        W=str(week),
        STARTERS=_players_csv(starters_player_ids),
    )

    headers = {"User-Agent": "FantasyHub/1.0 (+import-lineup)"}
    if cookie:
        headers["Cookie"] = cookie

    resp = requests.get(url, params=params, headers=headers, timeout=timeout)

    text = ""
    try:
        text = resp.text or ""
    except Exception:
        text = ""

    # Default to failure unless we positively see <status>OK</status>
    ok = False
    msg = ""

    # Try XML parse first
    try:
        root = ET.fromstring(resp.content or b"")
        # <status>OK</status> may be the root or nested
        st_el = root if (root.tag or "").lower() == "status" else root.find(".//status")
        if st_el is not None:
            st_txt = (st_el.text or "").strip()
            ok = st_txt.upper() == "OK"
            msg = st_txt or msg
        else:
            # Some MFL errors use <error>...</error>
            err_el = root.find(".//error")
            if err_el is not None:
                msg = (err_el.text or "").strip() or msg
    except Exception:
        # If not XML, fall through and treat as failure unless the raw body literally has <status>OK</status>
        pass

    # Last-ditch plain-text check for <status>OK</status>
    if not ok and text:
        compact = "".join(text.split()).lower()
        if "<status>ok</status>" in compact:
            ok = True

    # Build a friendly message
    if not msg:
        # Favor short body text if present; trim large responses
        t = (text or "").strip()
        if t:
            msg = t if len(t) <= 1900 else (t[:1900] + " … [truncated]")
        else:
            msg = f"HTTP {resp.status_code}"

    return (ok, msg if msg else ("Lineup submitted successfully" if ok else "Failed"))


# ------------------------ Utility for grouping/sort --------------------------

POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3}

def group_and_sort_players_for_review(
    players: List[Tuple[int, str, str, str]],
    projections: Dict[int, Projection],
    roster_statuses: Optional[Dict[int, str]] = None,
) -> Dict[str, List[Dict[str, object]]]:
    buckets: Dict[str, List[Dict[str, object]]] = {}

    for pid, name, pos, nfl in players:
        p = projections.get(pid)
        proj = p.projected if p else None
        key = pos.upper() if pos and pos.upper() in POS_ORDER else ("OTHER" if pos else "OTHER")

        status = (roster_statuses or {}).get(pid, ROSTER_STATUS_UNKNOWN)
        buckets.setdefault(key, []).append(dict(
            player_id=pid, name=name,
            position=key if key != "OTHER" else (pos or "OTHER"), team=nfl,
            projected=proj, roster_status=status,
            lineup_eligible=is_lineup_eligible_status(status),
        ))

    def sort_key(row: Dict[str, object]):
        proj = row.get("projected", None)
        if proj is None:
            return (1, 0.0, str(row.get("name") or ""))
        return (0, -float(proj), str(row.get("name") or ""))

    for k, rows in buckets.items():
        rows.sort(key=sort_key)

    ordered: Dict[str, List[Dict[str, object]]] = {}
    for k in ("QB", "RB", "WR", "TE"):
        if k in buckets:
            ordered[k] = buckets[k]
    others: List[Dict[str, object]] = []
    for k, rows in buckets.items():
        if k not in POS_ORDER:
            others.extend(rows)
    if others:
        ordered["Other"] = others

    return ordered


# ------------------------ Helpers for blueprint use --------------------------

def build_players_for_review(league_id_pk: int) -> List[Tuple[int, str, str, str]]:
    league: League | None = db.session.get(League, league_id_pk)
    if not league:
        return []

    team: Team | None = (
        db.session.query(Team)
        .filter(Team.league_id == league.id, Team.mfl_id == league.franchise_id)
        .first()
    )
    if not team:
        return []

    q = (
        db.session.query(Roster.player_id, Player.name, Player.position, Player.team)
        .join(Player, Player.id == Roster.player_id)
        .filter(Roster.team_id == team.id)
    )

    rows = []
    for pid, name, pos, nfl in q.all():
        try:
            rows.append((int(pid), name or "", (pos or "").upper(), (nfl or "").upper()))
        except Exception:
            continue
    return rows


# =================== NEW: requirements parse + auto-pick =====================

def parse_lineup_requirements(starters_label: str | None) -> Tuple[Optional[int], Dict[str, Tuple[int, int]]]:
    """
    Parse strings like:
      "11:QB:0-2,RB:2-4,WR:3-5,TE:1-3"
      "QB:1,RB:2-4,WR:3-5,TE:1-3"
    -> (total or None, {POS: (min,max), ...})
    """
    if not starters_label:
        return None, {}

    s = str(starters_label).strip()
    total: Optional[int] = None
    ranges: Dict[str, Tuple[int, int]] = {}

    # Strip a total prefix like "11:" if present
    first_colon = s.find(":")
    if first_colon > 0 and s[:first_colon].isdigit():
        try:
            total = int(s[:first_colon])
        except Exception:
            total = None
        s = s[first_colon + 1 :]

    # tokens "QB:1-2", "RB:2-4", ...
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" not in tok:
            continue
        pos, val = tok.split(":", 1)
        pos = pos.strip().upper()
        val = val.strip()
        if not pos or not val:
            continue
        if "-" in val:
            a, b = val.split("-", 1)
            try:
                lo, hi = int(a), int(b)
            except Exception:
                continue
        else:
            try:
                lo = hi = int(val)
            except Exception:
                continue
        ranges[pos] = (max(0, lo), max(0, hi if hi >= lo else lo))

    return total, ranges


def pick_optimal_lineup(
    players: List[Tuple[int, str, str, str]],  # (pid, name, pos, nfl)
    projections: Dict[int, Projection],
    total_required: Optional[int],
    ranges: Dict[str, Tuple[int, int]],
) -> List[int]:
    """
    Greedy auto-pick:
      1) For each position with a min requirement, take top 'min' by projection.
      2) Pool the remaining candidates from positions that still have room (<= max) and
         fill by best available until total_required is reached (or we run out).
    If total_required is None, fill up to the sum of max bounds (or just the min fill if no max).
    """
    # Build buckets by POS with sorted candidates
    by_pos: Dict[str, List[Tuple[int, float]]] = {}
    for pid, _name, pos, _nfl in players:
        proj = projections.get(pid).projected if projections.get(pid) else None
        score = float(proj) if proj is not None else float("-inf")  # None last
        key = (pos or "").upper() or "OTHER"
        by_pos.setdefault(key, []).append((pid, score))

    for key in by_pos:
        # sort by projection desc, then pid for stability
        by_pos[key].sort(key=lambda t: (-t[1], t[0]))

    selected: List[int] = []
    counts: Dict[str, int] = {}

    # Step A: satisfy mins
    for pos, (lo, _hi) in ranges.items():
        if pos not in by_pos or lo <= 0:
            if lo > 0 and pos not in by_pos:
                counts[pos] = 0
            continue
        take = min(lo, len(by_pos[pos]))
        for pid, _ in by_pos[pos][:take]:
            selected.append(pid)
        counts[pos] = take

    # Remove selected from buckets
    selected_set = set(selected)
    for pos in list(by_pos.keys()):
        by_pos[pos] = [(pid, sc) for (pid, sc) in by_pos[pos] if pid not in selected_set]

    # Decide target total
    if total_required is None:
        # best effort: sum of mins + as many as the max room allows
        total_required = sum(r[1] for r in ranges.values()) if ranges else len(players)
        # If all maxes are huge, cap by roster size
        total_required = min(total_required, len(players))

    # Step B: best-available while respecting max per position
    def has_room(pos: str) -> bool:
        lo, hi = ranges.get(pos, (0, 9999))
        return counts.get(pos, 0) < hi

    # Build a pooled candidate list that we can pop from
    pool: List[Tuple[int, float, str]] = []
    for pos, lst in by_pos.items():
        for pid, sc in lst:
            pool.append((pid, sc, pos))
    pool.sort(key=lambda t: (-t[1], t[0]))

    for pid, _sc, pos in pool:
        if len(selected) >= total_required:
            break
        if not has_room(pos):
            continue
        selected.append(pid)
        counts[pos] = counts.get(pos, 0) + 1

    return selected
