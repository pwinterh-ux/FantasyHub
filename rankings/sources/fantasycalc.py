"""FantasyCalc dynasty values adapter.

This module fetches current dynasty values and derives positional ranks for
QB/RB/WR/TE. It intentionally does not require Flask app/request context.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Any

import requests

FANTASYCALC_VALUES_URL = (
    "https://api.fantasycalc.com/values/current"
    "?isDynasty=true&ppr=1&numTeams=12&numQbs=1"
)
SUPPORTED_POSITIONS = {"QB", "RB", "WR", "TE"}
DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "RosterDash/1.0 (+https://rosterdash.example)"


@dataclass(slots=True)
class FantasyCalcPlayerValue:
    source: str
    source_mfl_id: str | None
    name_raw: str
    name_normalized: str
    position: str
    team: str | None
    value: float
    pos_rank: int


def normalize_name_for_matching(name: str) -> str:
    """Normalize player names for matching across external sources."""
    if not name:
        return ""

    s = name.lower()
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\s+(jr|sr|ii|iii|iv|v)$", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _extract_items(payload: Any) -> list[dict]:
    """Return list rows from common FantasyCalc payload shapes."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if isinstance(payload, dict):
        for key in ("players", "values", "data"):
            val = payload.get(key)
            if isinstance(val, list):
                return [item for item in val if isinstance(item, dict)]

        for val in payload.values():
            if isinstance(val, list):
                candidate = [item for item in val if isinstance(item, dict)]
                if candidate:
                    return candidate
    return []


def _coerce_float(v: object) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _raw_rows_from_payload(payload: Any) -> list[dict]:
    items = _extract_items(payload)
    rows: list[dict] = []

    for item in items:
        player_obj = item.get("player") if isinstance(item.get("player"), dict) else {}

        name = str(
            item.get("name")
            or item.get("player_name")
            or player_obj.get("name")
            or player_obj.get("fullName")
            or item.get("player")
            or ""
        ).strip()
        position = str(
            item.get("position")
            or item.get("pos")
            or player_obj.get("position")
            or player_obj.get("pos")
            or ""
        ).strip().upper()
        if not name or position not in SUPPORTED_POSITIONS:
            continue

        value = _coerce_float(
            item.get("value")
            or item.get("marketValue")
            or item.get("overallValue")
            or item.get("fcValue")
            or player_obj.get("value")
        )
        if value is None:
            continue

        team_raw = (
            item.get("team")
            or item.get("nflTeam")
            or item.get("teamAbbr")
            or player_obj.get("team")
            or player_obj.get("maybeTeam")
            or player_obj.get("nflTeam")
            or player_obj.get("teamAbbr")
        )
        team = str(team_raw).strip().upper() if team_raw else None

        source_mfl_id_raw = player_obj.get("mflId") or item.get("mflId")
        source_mfl_id = str(source_mfl_id_raw).strip() if source_mfl_id_raw not in (None, "") else None

        rows.append(
            {
                "source_mfl_id": source_mfl_id,
                "name_raw": name,
                "name_normalized": normalize_name_for_matching(name),
                "position": position,
                "team": team,
                "value": value,
            }
        )

    return rows


def rank_rows_by_position(rows: Iterable[dict]) -> list[FantasyCalcPlayerValue]:
    """Assign 1..N positional ranks based on descending value."""
    grouped: dict[str, list[dict]] = {p: [] for p in SUPPORTED_POSITIONS}
    for row in rows:
        pos = str(row.get("position") or "").upper()
        if pos in grouped:
            grouped[pos].append(row)

    out: list[FantasyCalcPlayerValue] = []
    for pos, entries in grouped.items():
        entries.sort(key=lambda r: float(r["value"]), reverse=True)
        for idx, row in enumerate(entries, start=1):
            out.append(
                FantasyCalcPlayerValue(
                    source="fantasycalc",
                    source_mfl_id=(str(row["source_mfl_id"]) if row.get("source_mfl_id") else None),
                    name_raw=str(row["name_raw"]),
                    name_normalized=str(row["name_normalized"]),
                    position=pos,
                    team=(str(row["team"]).upper() if row.get("team") else None),
                    value=float(row["value"]),
                    pos_rank=idx,
                )
            )
    return out


def fetch_and_rank_dynasty_values(
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[FantasyCalcPlayerValue]:
    """Fetch FantasyCalc dynasty values and return ranked rows across positions."""
    headers = {"User-Agent": user_agent, "Accept": "application/json"}
    response = requests.get(FANTASYCALC_VALUES_URL, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    rows = _raw_rows_from_payload(payload)
    return rank_rows_by_position(rows)