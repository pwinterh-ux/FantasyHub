"""Helpers for building MFL IR plans."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from flask import current_app
from sqlalchemy.orm import joinedload

from app import db
from models import League, Player, Roster, Team


@dataclass
class IRPlan:
    """Result of evaluating a league's IR moves."""

    activate: list[str]
    deactivate: list[str]
    ir_used_after: int
    roster_lookup: dict[str, Roster]

    @property
    def has_changes(self) -> bool:
        return bool(self.activate or self.deactivate)


def get_rostered_player_ids(league_id: int, franchise_id: str) -> set[str]:
    """Return all rostered player IDs (MFL) for the given league + franchise."""

    franchise = str(franchise_id or "").strip()
    if not franchise:
        return set()

    team = (
        db.session.query(Team)
        .filter(Team.league_id == league_id, Team.mfl_id == franchise)
        .first()
    )
    if not team:
        return set()

    rows: Sequence[tuple[int]] = (
        db.session.query(Roster.player_id)
        .filter(Roster.team_id == team.id)
        .all()
    )
    return {str(pid) for (pid,) in rows if pid is not None}


def plan_for_league(*, league: League, franchise_id: str, ir_eligible_ids: set[str]) -> IRPlan:
    """Build an activation/deactivation plan for the franchise."""

    franchise = str(franchise_id or "").strip()
    if not franchise:
        return IRPlan([], [], 0, {})

    team = (
        db.session.query(Team)
        .filter(Team.league_id == league.id, Team.mfl_id == franchise)
        .first()
    )
    if not team:
        current_app.logger.info("IR optimizer: no team for league %s franchise %s", league.id, franchise)
        return IRPlan([], [], 0, {})

    rosters: list[Roster] = (
        db.session.query(Roster)
        .options(joinedload(Roster.player))
        .filter(Roster.team_id == team.id)
        .all()
    )
    roster_lookup = {str(r.player_id): r for r in rosters}

    current_ir = [r for r in rosters if bool(r.in_ir)]
    must_demote = [r for r in current_ir if str(r.player_id) not in ir_eligible_ids]

    try:
        max_slots_raw = int(league.ir_slots_max) if league.ir_slots_max is not None else 0
    except (TypeError, ValueError):
        max_slots_raw = 0
    if max_slots_raw < 0:
        max_slots_raw = 0

    ir_after_demotions = max(0, len(current_ir) - len(must_demote))
    open_slots = max(0, max_slots_raw - ir_after_demotions)

    bench_candidates = [
        r
        for r in rosters
        if not bool(r.in_ir)
        and not bool(r.is_starter)
        and str(r.player_id) in ir_eligible_ids
    ]
    bench_candidates.sort(key=lambda r: str(r.player_id))
    to_promote = bench_candidates[:open_slots]

    activate_ids = [str(r.player_id) for r in must_demote]
    deactivate_ids = [str(r.player_id) for r in to_promote]
    ir_used_after = ir_after_demotions + len(to_promote)

    current_app.logger.info(
        "IR optimizer plan",
        extra={
            "league_id": league.id,
            "league_name": league.name,
            "activate": activate_ids,
            "deactivate": deactivate_ids,
            "ir_slots_max": max_slots_raw,
            "ir_after": ir_used_after,
        },
    )

    return IRPlan(
        activate=activate_ids,
        deactivate=deactivate_ids,
        ir_used_after=ir_used_after,
        roster_lookup=roster_lookup,
    )


def flip_ir_flags(plan: IRPlan) -> None:
    """Apply optimistic local updates after a successful IR import."""

    changed = False
    for pid in plan.activate:
        roster = plan.roster_lookup.get(str(pid))
        if roster and roster.in_ir:
            roster.in_ir = None
            changed = True
    for pid in plan.deactivate:
        roster = plan.roster_lookup.get(str(pid))
        if roster and not roster.in_ir:
            roster.in_ir = True
            changed = True
    if changed:
        db.session.commit()


def resolve_player_rows(plan: IRPlan, player_ids: Iterable[str]) -> list[dict[str, str]]:
    """Return display-ready dicts with name + ID for the requested players."""

    ordered: list[tuple[str, str | None]] = []
    missing: list[int] = []
    for pid in player_ids:
        key = str(pid)
        roster = plan.roster_lookup.get(key)
        if roster and roster.player:
            ordered.append((key, roster.player.name or ""))
        else:
            try:
                missing.append(int(key))
                ordered.append((key, None))
            except (TypeError, ValueError):
                ordered.append((key, key))

    name_map: dict[str, str] = {}
    if missing:
        players = (
            db.session.query(Player.id, Player.name)
            .filter(Player.id.in_(missing))
            .all()
        )
        name_map = {str(pid): name or "" for pid, name in players}

    results: list[dict[str, str]] = []
    for key, value in ordered:
        if value is None:
            results.append({"id": key, "name": name_map.get(key, key)})
        else:
            results.append({"id": key, "name": value})
    return results