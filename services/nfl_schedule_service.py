"""MFL NFL schedule ingestion and explicit, fail-closed game states."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

import requests

from app import db
from models import NflSchedule

UNLOCKED, LOCKED, BYE, NO_GAME, UNKNOWN = "UNLOCKED", "LOCKED", "BYE", "NO_GAME", "UNKNOWN"

_NO_GAME_TEAMS = {"FA"}

# MFL uses its historic abbreviations.  Player imports sometimes use modern ones.
_ALIASES = {
    "KC": "KCC", "GB": "GBP", "NE": "NEP", "TB": "TBB",
    "SF": "SFO", "NO": "NOS", "LV": "LVR", "OAK": "LVR",
    "JAX": "JAC", "WSH": "WAS",
}
_MFL_TEAMS = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GBP", "HOU", "IND", "JAC", "KCC", "LAC", "LAR", "LVR", "MIA",
    "MIN", "NEP", "NOS", "NYG", "NYJ", "PHI", "PIT", "SEA", "SFO", "TBB",
    "TEN", "WAS",
}


def normalize_nfl_team(team: Any) -> str | None:
    value = str(team or "").strip().upper()
    value = _ALIASES.get(value, value)
    return value if value in _MFL_TEAMS else None


def fetch_mfl_nfl_schedule(year: int, *, timeout: int = 20) -> dict:
    response = requests.get(
        f"https://api.myfantasyleague.com/{int(year)}/export",
        params={"TYPE": "nflSchedule", "JSON": "1"}, timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def parse_mfl_nfl_schedule_with_metadata(payload: dict, year: int) -> tuple[list[dict], dict[int, dict]]:
    """Parse schedule rows and prove week-level structural completeness.

    A week is complete only when every raw matchup produces two recognized,
    reciprocal teams.  Kickoff may be absent without hiding the teams; those
    teams receive UNKNOWN lock state later.
    """
    full_schedule = payload.get("fullNflSchedule")
    if isinstance(full_schedule, dict) and "nflSchedule" in full_schedule:
        weeks = full_schedule.get("nflSchedule", [])
    else:
        weeks = payload.get("nflSchedule", [])
    if isinstance(weeks, dict):
        weeks = [weeks]
    records: list[dict] = []
    metadata: dict[int, dict] = {}
    for week_node in weeks or []:
        try:
            week = int(week_node["week"])
        except (KeyError, TypeError, ValueError):
            continue
        matchups = week_node.get("matchup", [])
        if isinstance(matchups, dict):
            matchups = [matchups]
        info = metadata.setdefault(week, {"week_number": week, "raw_matchup_count": 0,
            "parsed_matchup_count": 0, "malformed_matchup_count": 0,
            "unique_team_count": 0, "structurally_complete": False})
        info["raw_matchup_count"] = len(matchups or [])
        parsed_teams: set[str] = set()
        for matchup in matchups or []:
            teams = matchup.get("team", [])
            if isinstance(teams, dict):
                teams = [teams]
            if len(teams) != 2:
                info["malformed_matchup_count"] += 1
                continue
            ids = [normalize_nfl_team(t.get("id")) for t in teams]
            if not all(ids) or ids[0] == ids[1]:
                info["malformed_matchup_count"] += 1
                continue
            info["parsed_matchup_count"] += 1
            parsed_teams.update(ids)
            try:
                kickoff = int(matchup["kickoff"])
            except (KeyError, TypeError, ValueError):
                kickoff = None
            for index, node in enumerate(teams):
                records.append({"year": int(year), "week": week, "team": ids[index],
                                "opponent": ids[1-index], "is_home": str(node.get("isHome", "0")) == "1",
                                "kickoff_unix": kickoff})
        info["unique_team_count"] = len(parsed_teams)
        info["structurally_complete"] = (
            info["raw_matchup_count"] > 0
            and info["parsed_matchup_count"] == info["raw_matchup_count"]
            and info["malformed_matchup_count"] == 0
            and info["unique_team_count"] == info["parsed_matchup_count"] * 2
        )
    return records, metadata


def parse_mfl_nfl_schedule(payload: dict, year: int) -> list[dict]:
    """Backward-compatible rows-only parser."""
    return parse_mfl_nfl_schedule_with_metadata(payload, year)[0]


def sync_nfl_schedule(year: int, records: Iterable[dict]) -> int:
    """Idempotently upsert this season only; historical seasons remain untouched."""
    changed = 0
    for data in records:
        if int(data["year"]) != int(year):
            continue
        key = (int(year), int(data["week"]), str(data["team"]))
        row = db.session.get(NflSchedule, key)
        if row is None:
            row = NflSchedule(year=key[0], week=key[1], team=key[2])
            db.session.add(row)
            changed += 1
        values = (str(data["opponent"]), bool(data["is_home"]), data.get("kickoff_unix"))
        if (row.opponent, row.is_home, row.kickoff_unix) != values:
            row.opponent, row.is_home, row.kickoff_unix = values
            changed += 1
    db.session.commit()
    return changed


def get_week_schedule(year: int, week: int) -> list[dict]:
    return [{"team": row.team, "opponent": row.opponent, "is_home": row.is_home,
             "kickoff_unix": row.kickoff_unix}
            for row in NflSchedule.query.filter_by(year=year, week=week).all()]


def build_team_game_states(rows: Iterable[dict], now_utc: datetime, *, schedule_verified: bool) -> dict[str, dict]:
    if now_utc.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    if not schedule_verified:
        return {}
    result = {}
    for row in rows:
        kickoff = row.get("kickoff_unix")
        if kickoff is None:
            result[row["team"]] = {"state": UNKNOWN, "kickoff_at_utc": None}
            continue
        at = datetime.fromtimestamp(int(kickoff), tz=timezone.utc)
        result[row["team"]] = {"state": UNLOCKED if now_utc < at else LOCKED,
                               "kickoff_at_utc": at.isoformat()}
    return result


def game_state_for_team(team: Any, states: dict, *, schedule_verified: bool, week_complete: bool) -> dict:
    if str(team or "").strip().upper() in _NO_GAME_TEAMS:
        return {"state": NO_GAME, "kickoff_at_utc": None}
    normalized = normalize_nfl_team(team)
    if not schedule_verified or not week_complete or normalized is None:
        return {"state": UNKNOWN, "kickoff_at_utc": None}
    return states.get(normalized, {"state": BYE, "kickoff_at_utc": None})
