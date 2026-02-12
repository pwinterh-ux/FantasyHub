"""Thin wrappers around MFL roster + IR endpoints."""
from __future__ import annotations

from typing import Any, Dict, Iterable, Optional, Tuple

import requests
from flask import current_app

from app import db
from models import League, Roster, Team
from services.mfl_client import DEFAULT_HEADERS, _rl

DEFAULT_TIMEOUT = 20
IR_STATUSES = {"IR", "IR-PUP", "IR-NFI", "IR-R"}  # mainly for debug counts


# ------------------------ small helpers -------------------------------------

def _normalize_host(host: str | None) -> str:
    text = str(host or "").strip()
    if text.startswith("http://") or text.startswith("https://"):
        return text.split("//", 1)[1].strip("/")
    return text.strip("/") or "api.myfantasyleague.com"


def _normalize_franchise_id(franchise_id: str | None) -> Tuple[str, str]:
    """
    Return (raw, numeric_no_leading_zeros) so we can match either form against MFL.
    """
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


def _parse_import_body_for_status(body: str) -> Tuple[bool, str]:
    """
    Parse XML-ish body from MFL import endpoints to determine success:
      - Success examples often include <ok>...</ok> or <status>OK</status>
      - Failures often include <error>...</error>
    Returns (ok, message).
    """
    text = str(body or "")
    lower = text.lower()

    if "<ok" in lower:
        try:
            start = lower.index("<ok")
            gt = text.index(">", start) + 1
            end = lower.index("</ok>", gt)
            msg = text[gt:end].strip() or "OK"
            return True, msg
        except Exception:
            return True, "OK"

    if "<status" in lower:
        try:
            start = lower.index("<status")
            gt = text.index(">", start) + 1
            end = lower.index("</status>", gt)
            status_word = text[gt:end].strip()
            if status_word.lower() in {"ok", "success"}:
                return True, status_word
        except Exception:
            pass

    if "<error" in lower:
        try:
            start = lower.index("<error")
            gt = text.index(">", start) + 1
            end = lower.index("</error>", gt)
            msg = text[gt:end].strip()
            return False, msg or "Error"
        except Exception:
            return False, "Error"

    return False, text[:200]


# ------------------------ roster placement fetch -----------------------------

def _fetch_roster_placements(
    *,
    league: League,
    franchise_id: str,
    host: str,
    cookie: Optional[str],
) -> Dict[str, str]:
    """
    Returns {player_id: placement} where placement ∈ {"ROSTER","IR","TAXI_SQUAD",...}
    Normalizes from either "status" or "slot".
    """
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
        _, fid_norm = _normalize_franchise_id(fid)
        if fid not in target_franchise_ids and fid_norm not in target_franchise_ids:
            continue

        for player in _ensure_list(entry.get("player")):
            pid = str(player.get("id") or "").strip()
            if not pid:
                continue
            status = str(player.get("status") or "").strip().upper()     # e.g. ROSTER / INJURED_RESERVE / TAXI_SQUAD
            slot = str(player.get("slot") or "").strip().upper()         # sometimes used as placement
            placement = (
                "IR" if (status.startswith("INJURED") or slot == "IR") else
                "TAXI_SQUAD" if ("TAXI" in status or slot == "TAXI_SQUAD") else
                "ROSTER" if (status == "ROSTER" or slot == "ROSTER") else
                status or slot or ""
            )
            placements[pid] = placement
        break  # found our franchise

    return placements


# ------------------------ public functions ----------------------------------

def sync_in_ir_flags(
    *,
    league: League,
    franchise_id: str,
    host: str,
    cookie: Optional[str],
) -> Dict[str, str]:
    """
    Fetch roster placements for one franchise and update rosters.in_ir.
    We treat only canonical placement "IR" as True; everything else -> None.
    """
    if not league.year:
        raise ValueError("League year missing; cannot sync IR flags")

    placements = _fetch_roster_placements(
        league=league, franchise_id=franchise_id, host=host, cookie=cookie
    )

    raw_franchise, _ = _normalize_franchise_id(franchise_id)
    team = (
        db.session.query(Team)
        .filter(Team.league_id == league.id, Team.mfl_id == raw_franchise)
        .first()
    )
    if not team:
        current_app.logger.warning(
            "IR sync: no local team",
            extra={"league_id": league.id, "franchise": raw_franchise},
        )
        return placements

    roster_rows: list[Roster] = db.session.query(Roster).filter(Roster.team_id == team.id).all()
    roster_map = {str(r.player_id): r for r in roster_rows}

    # clear and set current flags
    for roster in roster_rows:
        roster.in_ir = None
    for pid, placement in placements.items():
        roster = roster_map.get(pid)
        if not roster:
            continue
        roster.in_ir = True if placement == "IR" else None

    db.session.commit()

    current_app.logger.info(
        "IR sync complete",
        extra={
            "league_id": league.id,
            "franchise": raw_franchise,
            "ir_players": [pid for pid, status in placements.items() if status == "IR"],
            "total_players": len(placements),
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
    """
    POST a combined IR transaction to MFL.

    Safety net + taxi/roster checks:
      - Pre-fetch current placements.
      - Only ACTIVATE players who are currently on IR.
      - Only DEACTIVATE players who are currently on ROSTER.
      - Players on TAXI_SQUAD (or anything else) are skipped.
      - Parse 200 responses for <ok>/<error> to determine success & human message.
    """
    act_in = [str(pid).strip() for pid in activate_ids if str(pid).strip()]
    deact_in = [str(pid).strip() for pid in deactivate_ids if str(pid).strip()]

    if not league.year:
        raise ValueError("League year missing; cannot submit IR changes")

    placements = _fetch_roster_placements(league=league, franchise_id=franchise_id, host=host, cookie=cookie)

    act_final: list[str] = []
    deact_final: list[str] = []
    skipped: list[str] = []

    for pid in act_in:
        status = placements.get(pid, "")
        if status == "IR":
            act_final.append(pid)
        else:
            skipped.append(f"{pid}: requested ACTIVATE but current={status or 'UNKNOWN'}")

    for pid in deact_in:
        status = placements.get(pid, "")
        if status == "ROSTER":
            deact_final.append(pid)
        elif status == "IR":
            skipped.append(f"{pid}: requested DEACTIVATE but on IR (needs ACTIVATE instead)")
        elif status == "TAXI_SQUAD":
            skipped.append(f"{pid}: requested DEACTIVATE but on TAXI_SQUAD (skipping)")
        else:
            skipped.append(f"{pid}: requested DEACTIVATE but current={status or 'UNKNOWN'} (skipping)")

    # De-dupe
    act_seen, de_seen = set(), set()
    act_final = [p for p in act_final if not (p in act_seen or act_seen.add(p))]
    deact_final = [p for p in deact_final if not (p in de_seen or de_seen.add(p))]

    host_clean = _normalize_host(host)
    raw_franchise, _ = _normalize_franchise_id(franchise_id)

    payload = {
        "TYPE": "ir",
        "L": str(league.mfl_id),
        "FRANCHISE": raw_franchise,
        "FRANCHISE_ID": raw_franchise,
        "JSON": "1",
    }
    if act_final:
        payload["ACTIVATE"] = ",".join(act_final)
    if deact_final:
        payload["DEACTIVATE"] = ",".join(deact_final)

    url = f"https://{host_clean}/{league.year}/import"

    _rl.wait()
    resp = requests.post(url, data=payload, headers=_build_headers(cookie), timeout=DEFAULT_TIMEOUT)

    # Prefer JSON if present, else parse XML-ish body
    result_json: Dict[str, Any] | None = None
    try:
        result_json = resp.json()
    except ValueError:
        result_json = None

    ok = False
    msg = ""
    if result_json:
        status_txt = str(result_json.get("status", "")).lower()
        ok = resp.status_code < 400 and status_txt in {"ok", "success"}
        msg = result_json.get("message") or result_json.get("error") or result_json.get("status") or ""
        if not msg:
            msg = str(result_json)
    else:
        ok, msg = _parse_import_body_for_status(resp.text)

    current_app.logger.info(
        "IR import",
        extra={
            "league_id": league.id,
            "franchise": raw_franchise,
            "activate": act_final,
            "deactivate": deact_final,
            "skipped": skipped,
            "status_code": resp.status_code,
            "ok": ok,
            "resp_message": msg,
        },
    )

    return {
        "ok": ok,
        "status_code": resp.status_code,
        "payload": (result_json if result_json is not None else {"raw": resp.text}),
        "message": msg,
        "skipped": skipped,
    }
