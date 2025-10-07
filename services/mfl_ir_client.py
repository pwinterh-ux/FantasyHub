"""Thin wrappers around MFL roster + IR endpoints."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

import requests
from flask import current_app

from app import db
from models import League, Roster, Team
from services.mfl_client import DEFAULT_HEADERS, _rl

DEFAULT_TIMEOUT = 20


def _normalize_host(host: str | None) -> str:
    text = str(host or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text.split("//", 1)[1].strip("/")
    return text.strip("/") or "api.myfantasyleague.com"


def _normalize_franchise_id(franchise_id: str | None) -> tuple[str, str]:
    raw = str(franchise_id or "").strip()
    normalized = raw
    try:
        normalized = str(int(raw))
    except (TypeError, ValueError):
        pass
    return raw, normalized


def _build_headers(cookie: Optional[str]) -> Dict[str, str]:
    headers = dict(DEFAULT_HEADERS)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _ensure_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def sync_in_ir_flags(*, league: League, franchise_id: str, host: str, cookie: Optional[str]) -> Dict[str, str]:
    """Fetch roster placements for one franchise and update rosters.in_ir."""

    if not league.year:
        raise ValueError("League year missing; cannot sync IR flags")

    host_clean = _normalize_host(host)
    raw_franchise, normalized_franchise = _normalize_franchise_id(franchise_id)

    params = {
        "TYPE": "rosters",
        "L": str(league.mfl_id),
        "FRANCHISE": raw_franchise,
        "JSON": "1",
    }
    url = f"https://{host_clean}/{league.year}/export"

    _rl.wait()
    resp = requests.get(url, params=params, headers=_build_headers(cookie), timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    target_franchise_ids = {raw_franchise, normalized_franchise}
    placements: Dict[str, str] = {}

    for entry in _ensure_list(data.get("rosters", {}).get("franchise")):
        fid = str(entry.get("id") or "").strip()
        if fid not in target_franchise_ids and _normalize_franchise_id(fid)[1] not in target_franchise_ids:
            continue
        for player in _ensure_list(entry.get("player")):
            pid = str(player.get("id") or "").strip()
            if not pid:
                continue
            status = str(player.get("status") or "").strip().upper()
            slot = str(player.get("slot") or "").strip().upper()
            roster_status = "IR" if "IR" in {status, slot} else status or slot
            placements[pid] = roster_status
        break

    team = (
        db.session.query(Team)
        .filter(Team.league_id == league.id, Team.mfl_id == raw_franchise)
        .first()
    )
    if not team:
        current_app.logger.warning("IR sync: no local team for league %s franchise %s", league.id, raw_franchise)
        return placements

    roster_rows: list[Roster] = (
        db.session.query(Roster)
        .filter(Roster.team_id == team.id)
        .all()
    )
    roster_map = {str(r.player_id): r for r in roster_rows}

    for roster in roster_rows:
        roster.in_ir = None

    for pid, roster_status in placements.items():
        roster = roster_map.get(pid)
        if not roster:
            continue
        if roster_status == "IR":
            roster.in_ir = True
        else:
            roster.in_ir = None

    db.session.commit()

    current_app.logger.info(
        "IR sync complete",
        extra={
            "league_id": league.id,
            "franchise": raw_franchise,
            "ir_players": [pid for pid, status in placements.items() if status == "IR"],
        },
    )
    return placements


def import_ir(
    *,
    league: League,
    franchise_id: str,
    host: str,
    cookie: Optional[str],
    activate_ids: Iterable[str],
    deactivate_ids: Iterable[str],
) -> Dict[str, Any]:
    """POST a combined IR transaction to MFL."""

    activate_list = [str(pid) for pid in activate_ids if str(pid)]
    deactivate_list = [str(pid) for pid in deactivate_ids if str(pid)]

    if not league.year:
        raise ValueError("League year missing; cannot submit IR changes")

    host_clean = _normalize_host(host)
    raw_franchise, _ = _normalize_franchise_id(franchise_id)

    payload = {
        "TYPE": "ir",
        "L": str(league.mfl_id),
        "FRANCHISE_ID": raw_franchise,
        "JSON": "1",
    }
    if activate_list:
        payload["ACTIVATE"] = ",".join(activate_list)
    if deactivate_list:
        payload["DEACTIVATE"] = ",".join(deactivate_list)

    url = f"https://{host_clean}/{league.year}/import"

    _rl.wait()
    resp = requests.post(url, data=payload, headers=_build_headers(cookie), timeout=DEFAULT_TIMEOUT)

    result: Dict[str, Any]
    try:
        result = resp.json()
    except ValueError:
        result = {"raw": resp.text}

    ok = resp.status_code < 400 and str(result.get("status", "")).lower() in {"ok", "success"}

    current_app.logger.info(
        "IR import",
        extra={
            "league_id": league.id,
            "franchise": raw_franchise,
            "activate": activate_list,
            "deactivate": deactivate_list,
            "status_code": resp.status_code,
            "response": result,
        },
    )

    return {"ok": ok, "status_code": resp.status_code, "payload": result}