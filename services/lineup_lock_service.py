"""Shared, fail-closed weekly-lineup and NFL game-lock helpers."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from services.nfl_schedule_service import (
    LOCKED, UNKNOWN, build_team_game_states, fetch_mfl_nfl_schedule,
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
    """Fetch authoritative weekly starters and schedule, failing closed on any gap."""
    ids = [int(row[0]) for row in players]
    try:
        statuses = fetch_player_roster_statuses({**job, "player_ids": ids}, week)
        current = {pid for pid, status in statuses.items() if status == "S"}
        payload = fetch_mfl_nfl_schedule(int(job["year"]))
        rows, metadata = parse_mfl_nfl_schedule_with_metadata(payload, int(job["year"]))
        complete = bool(metadata.get(int(week), {}).get("structurally_complete"))
        week_rows = [row for row in rows if row["week"] == int(week)]
        if not complete or not week_rows:
            raise ValueError("NFL schedule is incomplete")
        team_states = build_team_game_states(week_rows, datetime.now(timezone.utc), schedule_verified=True)
        states = {int(pid): game_state_for_team(team, team_states,
                  schedule_verified=True, week_complete=True)["state"]
                  for pid, _name, _pos, team in players}
        if any(state == UNKNOWN for state in states.values()):
            raise ValueError("NFL game state is unknown")
        return {"safe": True, "warning": None, "current": current, "states": states,
                "locked_starters": {pid for pid in current if states.get(pid) == LOCKED},
                "locked_bench": {pid for pid in ids if pid not in current and states.get(pid) == LOCKED}}
    except Exception as exc:
        # If the schedule is unavailable we can still preserve live starters when
        # that first fetch succeeded; otherwise no edit or submit is safe.
        current = locals().get("current", set())
        return {"safe": False, "warning": f"Game lock status unavailable ({exc}). Current starters are preserved; editing and submission are disabled.",
                "current": current, "states": {pid: UNKNOWN for pid in ids},
                "locked_starters": set(current), "locked_bench": set(ids) - set(current)}


def lock_violation(lock_context: dict, submitted: list[int]) -> str | None:
    """Reject omissions/additions against state fetched immediately before import."""
    if not lock_context.get("safe"):
        return "Lineup not submitted because current game-lock status could not be verified. Refresh this lineup before submitting."
    selected = set(submitted)
    if not set(lock_context["locked_starters"]).issubset(selected) or selected & set(lock_context["locked_bench"]):
        return "Lineup changed after a game locked. Refresh this lineup before submitting."
    return None
