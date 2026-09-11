"""Shared, fail-closed weekly-lineup and NFL game-lock helpers."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from services.nfl_schedule_service import (
    BYE, LOCKED, UNKNOWN, UNLOCKED, build_team_game_states, fetch_mfl_nfl_schedule,
    game_state_for_team, parse_mfl_nfl_schedule_with_metadata,
)


def fetch_player_roster_statuses(job: dict, week: int, *, timeout: int = 20) -> dict[int, str]:
    """Return this franchise's live MFL weekly S/NS statuses."""
    response = requests.get(
        f"https://{job['host']}/{job['year']}/export",
        params={"TYPE": "playerRosterStatus", "L": job["mfl_id"], "W": str(week),
                "P": ",".join(str(x) for x in job["player_ids"]), "JSON": "0"},
        headers={"Cookie": job.get("cookie", "")}, timeout=timeout,
    )
    response.raise_for_status()
    root = ET.fromstring(response.content)
    result = {}
    for node in root.findall(".//playerStatus"):
        if not str(node.get("id", "")).isdigit():
            continue
        match = next((entry for entry in node.findall("roster_franchise")
                      if str(entry.get("franchise_id", "")) == str(job["franchise_id"])), None)
        if match is not None and match.get("status"):
            result[int(node.get("id"))] = str(match.get("status")).upper()
    return result


def resolve_lineup_locks(job: dict, players: list[tuple], week: int) -> dict:
    """Resolve game locks, requiring weekly status only when a game is not editable."""
    ids = [int(row[0]) for row in players]
    try:
        payload = fetch_mfl_nfl_schedule(int(job["year"]))
        rows, metadata = parse_mfl_nfl_schedule_with_metadata(payload, int(job["year"]))
        # The live MFL shape contains only the current week.  Prefer its explicit
        # week marker; full-schedule/test payloads can still verify the requested
        # week directly when no current-week marker exists.
        live_node = payload.get("nflSchedule")
        raw_live_week = live_node.get("week") if isinstance(live_node, dict) else (
            payload.get("currentWeek") or payload.get("fullNflSchedule", {}).get("currentWeek")
            if isinstance(payload.get("fullNflSchedule", {}), dict) else None)
        try:
            live_week = int(raw_live_week)
        except (TypeError, ValueError):
            live_week = int(week) if int(week) in metadata else None
        if live_week is None:
            raise ValueError("NFL current week could not be determined")
        if int(week) < live_week:
            statuses = fetch_player_roster_statuses({**job, "player_ids": ids}, week)
            current = {pid for pid, status in statuses.items() if status == "S"}
            raise ValueError("Past-week lineup editing is not supported")
        if int(week) > live_week:
            states = {pid: UNLOCKED for pid in ids}
            return {"safe": True, "warning": None, "current": set(), "states": states,
                    "locked_starters": set(), "locked_bench": set(),
                    "unknown_starters": set(), "unknown_bench": set(), "bye_players": set()}
        complete = bool(metadata.get(int(week), {}).get("structurally_complete"))
        week_rows = [row for row in rows if row["week"] == int(week)]
        if not complete or not week_rows:
            raise ValueError("NFL schedule is incomplete")
        team_states = build_team_game_states(week_rows, datetime.now(timezone.utc), schedule_verified=True)
        states = {int(pid): game_state_for_team(team, team_states,
                  schedule_verified=True, week_complete=True)["state"]
                  for pid, _name, _pos, team in players}
        statuses = fetch_player_roster_statuses({**job, "player_ids": ids}, week)
        # Weekly S/NS is only needed to place players whose games can no longer
        # be changed on the correct side of the lineup.  An omitted status for
        # an unlocked player does not create a game-lock risk.
        missing_required = {pid for pid in ids
                            if states.get(pid) in {LOCKED, UNKNOWN}
                            and statuses.get(pid) not in {"S", "NS"}}
        if missing_required:
            raise ValueError("MFL weekly lineup status is incomplete for a locked or unknown player")
        current = {pid for pid in ids if statuses.get(pid) == "S"}
        unknown_starters = {pid for pid in current if states.get(pid) == UNKNOWN}
        unknown_bench = {pid for pid in ids if pid not in current and states.get(pid) == UNKNOWN}
        return {"safe": True, "warning": None, "current": current, "states": states,
                "locked_starters": {pid for pid in current if states.get(pid) == LOCKED},
                "locked_bench": {pid for pid in ids if pid not in current and states.get(pid) == LOCKED},
                "unknown_starters": unknown_starters, "unknown_bench": unknown_bench,
                "bye_players": {pid for pid in ids if states.get(pid) == BYE}}
    except Exception as exc:
        # If the schedule is unavailable we can still preserve live starters when
        # that first fetch succeeded; otherwise no edit or submit is safe.
        current = locals().get("current", set())
        return {"safe": False, "warning": f"Game lock status unavailable ({exc}). Current starters are preserved; editing and submission are disabled.",
                "current": current, "states": {pid: UNKNOWN for pid in ids},
                "locked_starters": set(), "locked_bench": set(),
                "unknown_starters": set(current), "unknown_bench": set(ids) - set(current),
                "bye_players": set()}


def lock_violation(lock_context: dict, submitted: list[int]) -> str | None:
    """Reject omissions/additions against state fetched immediately before import."""
    if not lock_context.get("safe"):
        return "Lineup not submitted because current game-lock status could not be verified. Refresh this lineup before submitting."
    selected = set(submitted)
    frozen = set(lock_context["locked_starters"]) | set(lock_context.get("unknown_starters", set()))
    forbidden = (set(lock_context["locked_bench"]) |
                 set(lock_context.get("unknown_bench", set())) |
                 set(lock_context.get("bye_players", set())))
    if not frozen.issubset(selected) or selected & forbidden:
        return "Lineup changed after a game locked. Refresh this lineup before submitting."
    return None
