"""Map external ranking rows to canonical MFL players."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from models import DynastyRankMapOverride, Player
from rankings.sources.fantasycalc import FantasyCalcPlayerValue, normalize_name_for_matching
from rankings.sources.keeptradecut import KeepTradeCutRankRow
from rankings.sources.fantasypros import FantasyProsRankRow


@dataclass(slots=True)
class ExternalRankRow:
    source: str
    position: str
    source_mfl_id: str | None
    name_raw: str
    name_normalized: str
    team: str | None
    rank: int
    value: float | None


@dataclass(slots=True)
class PlayerCandidate:
    mfl_id: str
    name: str | None
    position: str | None
    team: str | None


@dataclass(slots=True)
class MappedRankRow:
    source: str
    position: str
    mfl_id: str
    player_name: str | None
    source_rank: int
    source_value: float | None


@dataclass(slots=True)
class UnmatchedRankRow:
    source: str
    position: str
    name_raw: str
    name_normalized: str
    team: str | None
    value: float | None
    pos_rank: int
    reason: str


def adapt_fantasycalc_rows(rows: Iterable[FantasyCalcPlayerValue]) -> list[ExternalRankRow]:
    return [
        ExternalRankRow(
            source=r.source,
            position=r.position.upper(),
            source_mfl_id=r.source_mfl_id,
            name_raw=r.name_raw,
            name_normalized=r.name_normalized,
            team=(r.team.upper() if r.team else None),
            rank=int(r.pos_rank),
            value=(float(r.value) if r.value is not None else None),
        )
        for r in rows
    ]


def adapt_keeptradecut_rows(rows: Iterable[KeepTradeCutRankRow]) -> list[ExternalRankRow]:
    out: list[ExternalRankRow] = []
    for r in rows:
        out.append(
            ExternalRankRow(
                source=r.source,
                position=r.position.upper(),
                source_mfl_id=r.source_mfl_id,
                name_raw=r.name_raw,
                name_normalized=normalize_name_for_matching(r.name_raw),
                team=(r.team.upper() if r.team else None),
                rank=int(r.rank),
                value=(float(r.value) if r.value is not None else None),
            )
        )
    return out


def adapt_fantasypros_rows(rows: Iterable[FantasyProsRankRow]) -> list[ExternalRankRow]:
    out: list[ExternalRankRow] = []
    for r in rows:
        out.append(
            ExternalRankRow(
                source=r.source,
                position=r.position.upper(),
                source_mfl_id=r.source_mfl_id,
                name_raw=r.name_raw,
                name_normalized=r.name_normalized,
                team=(r.team.upper() if r.team else None),
                rank=int(r.rank),
                value=(float(r.value) if r.value is not None else None),
            )
        )
    return out




def _normalized_name_keys(name: str) -> set[str]:
    """Return possible normalized keys, including Last, First -> First Last swap."""
    keys: set[str] = set()
    n = normalize_name_for_matching(name)
    if n:
        keys.add(n)

    raw = (name or "").strip()
    if "," in raw:
        parts = [s.strip() for s in raw.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            swapped = f"{parts[1]} {parts[0]}".strip()
            ns = normalize_name_for_matching(swapped)
            if ns:
                keys.add(ns)
    return keys

def _build_lookups() -> tuple[dict[tuple[str, str], list[PlayerCandidate]], dict[str, PlayerCandidate], dict[tuple[str, str, str], str]]:
    """Build lookups by name/pos, by mfl id, and manual overrides."""
    by_name: dict[tuple[str, str], list[PlayerCandidate]] = {}
    by_mfl: dict[str, PlayerCandidate] = {}

    players = Player.query.with_entities(Player.mfl_id, Player.name, Player.position, Player.team).all()
    for mfl_id, name, pos, team in players:
        if not mfl_id:
            continue
        cand = PlayerCandidate(
            mfl_id=str(mfl_id),
            name=name,
            position=(str(pos).upper() if pos else None),
            team=(str(team).upper() if team else None),
        )
        by_mfl[cand.mfl_id] = cand

        if not name or not pos:
            continue
        for nk in _normalized_name_keys(name):
            key = (nk, str(pos).upper())
            by_name.setdefault(key, []).append(cand)

    override_rows = DynastyRankMapOverride.query.filter_by(is_active=True).all()
    overrides: dict[tuple[str, str, str], str] = {}
    for row in override_rows:
        overrides[(str(row.source).lower(), str(row.position).upper(), str(row.source_name_norm))] = str(row.mapped_mfl_id)

    return by_name, by_mfl, overrides


def map_rows_to_mfl(rows: Iterable[ExternalRankRow]) -> tuple[list[MappedRankRow], list[UnmatchedRankRow]]:
    """Map rows to MFL players; source mflId first, then override, then name+position."""
    by_name, by_mfl, overrides = _build_lookups()
    matched: list[MappedRankRow] = []
    unmatched: list[UnmatchedRankRow] = []

    for row in rows:
        if row.source_mfl_id:
            cand = by_mfl.get(row.source_mfl_id)
            if cand and (not cand.position or cand.position == row.position.upper()):
                matched.append(MappedRankRow(row.source, row.position, cand.mfl_id, cand.name, row.rank, row.value))
                continue

        override_key = (row.source.lower(), row.position.upper(), row.name_normalized)
        override_mfl = overrides.get(override_key)
        if override_mfl and override_mfl in by_mfl:
            cand = by_mfl[override_mfl]
            matched.append(MappedRankRow(row.source, row.position, cand.mfl_id, cand.name, row.rank, row.value))
            continue

        key = (row.name_normalized, row.position.upper())
        candidates = by_name.get(key, [])

        if not candidates:
            unmatched.append(UnmatchedRankRow(row.source, row.position, row.name_raw, row.name_normalized, row.team, row.value, row.rank, "no_candidate"))
            continue

        if len(candidates) == 1:
            cand = candidates[0]
            matched.append(MappedRankRow(row.source, row.position, cand.mfl_id, cand.name, row.rank, row.value))
            continue

        if row.team:
            by_team = [c for c in candidates if (c.team or "").upper() == row.team.upper()]
            if len(by_team) == 1:
                cand = by_team[0]
                matched.append(MappedRankRow(row.source, row.position, cand.mfl_id, cand.name, row.rank, row.value))
                continue

        unmatched.append(UnmatchedRankRow(row.source, row.position, row.name_raw, row.name_normalized, row.team, row.value, row.rank, "ambiguous_candidate"))

    return matched, unmatched