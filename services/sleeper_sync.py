# services/sleeper_sync.py
"""Database sync helpers for Sleeper leagues."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from flask import current_app

from app import db
from models import (
    SleeperDraftPick,
    SleeperLeague,
    SleeperPlayer,
    SleeperRoster,
    SleeperTeam,
    User,
)
from services.sleeper_client import SleeperClient, combine_points


def _safe_int(val: Any) -> int | None:
    try:
        if val in (None, ""):
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def _clean_str(val: Any) -> str | None:
    if val in (None, ""):
        return None
    s = str(val).strip()
    return s or None


def _record_string(settings: dict | None) -> str | None:
    if not settings:
        return None
    wins = _safe_int(settings.get("wins")) or 0
    losses = _safe_int(settings.get("losses")) or 0
    ties = _safe_int(settings.get("ties")) or 0
    record = f"{wins}-{losses}"
    if ties:
        record = f"{record}-{ties}"
    return record


def _ensure_player(player_sid: str, catalog: dict | None) -> SleeperPlayer:
    """
    Ensure a SleeperPlayer row exists and (lightly) hydrate from the players catalog.
    """
    player = SleeperPlayer.query.filter_by(sleeper_id=player_sid).first()
    if not player:
        player = SleeperPlayer(sleeper_id=player_sid)
        db.session.add(player)

    data = catalog.get(player_sid) if isinstance(catalog, dict) else None
    if data:
        # Name preference order similar to Sleeper exports
        name = _clean_str(
            data.get("full_name")
            or data.get("first_name")
            or data.get("last_name")
            or data.get("search_full_name")
        )
        if name and player.name != name:
            player.name = name

        position = _clean_str(
            data.get("position")
            or (data.get("fantasy_positions") or [None])[0]
        )
        if position and player.position != position:
            player.position = position

        team = _clean_str(data.get("team") or data.get("real_team"))
        if team and player.team != team:
            player.team = team
        # status optional; can be populated separately if desired

    return player


def ensure_sleeper_league(user: User, sleeper_id: str, name: str, year: int) -> SleeperLeague:
    league = (
        SleeperLeague.query.filter_by(user_id=user.id, sleeper_id=sleeper_id, year=year)
        .first()
    )
    if not league:
        league = SleeperLeague(
            user_id=user.id,
            sleeper_id=sleeper_id,
            name=name,
            year=year,
        )
        db.session.add(league)
        db.session.flush()
    elif name and league.name != name:
        league.name = name
    return league


def _season_rounds_for_targets(
    client: SleeperClient,
    league_info: dict,
    league_id_str: str,
    target_seasons: list[int],
) -> dict[int, int]:
    """
    Decide rounds per target season (N+1..N+3 only):
    - Prefer draft.settings.rounds for that season
    - Fallback to league.settings.draft_rounds
    - Fallback to 4
    """
    rounds_by_season: dict[int, int] = {}

    drafts = client.get_league_drafts(league_id_str) or []
    by_season: dict[int, dict] = {}
    for d in drafts:
        s = _safe_int(d.get("season"))
        if s is None:
            continue
        # keep the latest draft object per season
        by_season[s] = d

    for season in target_seasons:
        rounds = None
        d = by_season.get(season)
        if d:
            d_id = _clean_str(d.get("draft_id") or d.get("id"))
            if d_id:
                d_obj = client.get_draft(d_id) or {}
                rounds = _safe_int((d_obj.get("settings") or {}).get("rounds"))
        if rounds is None:
            rounds = _safe_int((league_info.get("settings") or {}).get("draft_rounds"))
        rounds_by_season[season] = rounds or 4

    return rounds_by_season


def sync_league_from_payload(
    league: SleeperLeague,
    *,
    league_info: dict,
    rosters: Iterable[dict],
    users: Iterable[dict],
    picks: Iterable[dict],
    allowed_pick_seasons: set[int],
    season_rounds: dict[int, int],
    matchups: Iterable[dict],
    players_catalog: dict,
    site_user: User | None = None,
) -> dict:
    """Apply Sleeper payloads to the database for a single league."""

    # -------- League metadata --------
    try:
        league_year = int(league_info.get("season"))
    except Exception:
        league_year = league.year
    if league_year:
        league.year = league_year

    name = _clean_str(league_info.get("name"))
    if name:
        league.name = name

    league.draft_id = _clean_str(league_info.get("draft_id"))
    settings = league_info.get("settings") or {}
    league.waiver_budget = _safe_int(settings.get("waiver_budget"))

    roster_positions = league_info.get("roster_positions") or []
    if isinstance(roster_positions, list):
        league.roster_slots = ",".join(str(pos) for pos in roster_positions if pos)

    scoring = league_info.get("scoring_settings")
    if scoring:
        league.scoring_json = scoring

    # -------- Build user display map --------
    user_map: dict[str, dict] = {}
    for row in users or []:
        uid = _clean_str(row.get("user_id") or row.get("userId"))
        if not uid:
            continue
        user_map[uid] = row

    # -------- Opponent mapping from matchups --------
    opponent_by_roster: dict[int, int] = {}
    match_map: dict[int, list[int]] = {}
    for row in matchups or []:
        mid = _safe_int(row.get("matchup_id"))
        rid = _safe_int(row.get("roster_id"))
        if mid is None or rid is None:
            continue
        match_map.setdefault(mid, []).append(rid)
    for mids in match_map.values():
        if len(mids) < 2:
            continue
        if len(mids) == 2:
            a, b = mids
            opponent_by_roster[a] = b
            opponent_by_roster[b] = a
        else:
            base = mids[0]
            for rid in mids[1:]:
                opponent_by_roster[rid] = base

    # -------- Teams --------
    existing_teams = {team.sleeper_roster_id: team for team in league.teams}
    seen_roster_ids: set[int] = set()
    team_payloads: dict[int, dict] = {}
    teams_created = teams_updated = 0

    for roster in rosters or []:
        roster_id = _safe_int(roster.get("roster_id") or roster.get("rosterId"))
        if roster_id is None:
            continue
        seen_roster_ids.add(roster_id)
        team = existing_teams.get(roster_id)
        if not team:
            team = SleeperTeam(league_id=league.id, sleeper_roster_id=roster_id)
            db.session.add(team)
            teams_created += 1
        else:
            teams_updated += 1

        owner_id = _clean_str(roster.get("owner_id") or roster.get("ownerId"))
        metadata = roster.get("metadata") or {}
        r_settings = roster.get("settings") or {}

        display_name = _clean_str(
            metadata.get("team_name")
            or metadata.get("teamName")
            or (user_map.get(owner_id) or {}).get("display_name")
            or (user_map.get(owner_id) or {}).get("username")
        )
        if display_name and team.name != display_name:
            team.name = display_name

        owner_name = _clean_str(
            (user_map.get(owner_id) or {}).get("display_name")
            or (user_map.get(owner_id) or {}).get("username")
        )
        if owner_name and team.owner_name != owner_name:
            team.owner_name = owner_name

        team.owner_user_id = owner_id
        if site_user and owner_id and owner_id == (site_user.sleeper_user_id or ""):
            team.user_id = site_user.id
        else:
            team.user_id = None

        team.record = _record_string(r_settings)
        team.points_for = combine_points(r_settings.get("fpts"), r_settings.get("fpts_decimal"))
        team.points_against = combine_points(
            r_settings.get("fpts_against"),
            r_settings.get("fpts_against_decimal"),
        )
        team.standing = _safe_int(r_settings.get("rank"))
        team.waiver_balance = _safe_int(r_settings.get("waiver_budget") or r_settings.get("waiver_balance"))
        team.current_opponent_id = opponent_by_roster.get(roster_id)
        if metadata:
            team.meta_json = metadata

        team_payloads[roster_id] = {
            "team": team,
            "players": roster.get("players") or [],
            "starters": roster.get("starters") or [],
        }

    # Remove teams that disappeared
    for roster_id, team in list(existing_teams.items()):
        if roster_id not in seen_roster_ids:
            db.session.delete(team)

    db.session.flush()

    # -------- Rosters --------
    roster_rows = 0
    players_catalog = players_catalog or {}
    for payload in team_payloads.values():
        team = payload["team"]
        players = payload.get("players") or []
        starters = {str(s) for s in (payload.get("starters") or []) if s is not None}

        SleeperRoster.query.filter_by(team_id=team.id).delete(synchronize_session=False)

        for idx, pid in enumerate(players):
            pid_str = _clean_str(pid)
            if not pid_str:
                continue
            _ensure_player(pid_str, players_catalog)
            is_starter = pid_str in starters
            roster_row = SleeperRoster(
                team_id=team.id,
                player_sid=pid_str,
                is_starter=is_starter,
                order_index=idx,
                lineup_slot="starter" if is_starter else "bench",
            )
            db.session.add(roster_row)
            roster_rows += 1

    # -------- Draft picks (STRICT: only N+1, N+2, N+3) --------
    SleeperDraftPick.query.filter_by(league_id=league.id).delete(synchronize_session=False)
    picks_rows = 0
    team_by_roster = {team.sleeper_roster_id: team for team in league.teams}

    if team_by_roster and allowed_pick_seasons:
        roster_ids_sorted = sorted(team_by_roster.keys())

        # Baseline for allowed seasons
        baseline: dict[tuple[int, int, int], dict[str, int | str | None]] = {}
        for season in sorted(allowed_pick_seasons):
            rounds = int(season_rounds.get(season) or 0)
            if rounds <= 0:
                continue
            for rnd in range(1, rounds + 1):
                for idx, original_roster_id in enumerate(roster_ids_sorted, start=1):
                    orig_team = team_by_roster.get(original_roster_id)
                    if not orig_team:
                        continue
                    pick_number = (rnd - 1) * len(roster_ids_sorted) + idx  # placeholder
                    baseline[(season, rnd, original_roster_id)] = {
                        "team_id": orig_team.id,  # initial owner is the original team
                        "season": season,
                        "round": rnd,
                        "pick_number": pick_number,
                        "original_team": orig_team.name or orig_team.owner_name,
                        "original_roster_id": original_roster_id,
                    }

        # Apply traded picks (already filtered by allowed seasons upstream)
        for pick in picks or []:
            season = _safe_int(pick.get("season"))
            if season is None or season not in allowed_pick_seasons:
                continue

            rnd = _safe_int(pick.get("round"))
            original_roster_id = _safe_int(
                pick.get("original_roster_id")
                or pick.get("roster_id")
                or pick.get("original_owner_id")
                or pick.get("originalOwnerId")
                or pick.get("originalRosterId")
            )
            current_owner_roster_id = _safe_int(
                pick.get("owner_id")
                or pick.get("ownerId")
                or pick.get("to")
                or pick.get("previous_owner_id")  # last resort
            )

            if rnd is None or original_roster_id is None:
                continue

            key = (season, rnd, original_roster_id)

            if key not in baseline:
                orig_team = team_by_roster.get(original_roster_id)
                baseline[key] = {
                    "team_id": (orig_team.id if orig_team else None),
                    "season": season,
                    "round": rnd,
                    "pick_number": _safe_int(pick.get("pick")) or _safe_int(pick.get("overall")),
                    "original_team": (orig_team.name or orig_team.owner_name) if orig_team else None,
                    "original_roster_id": original_roster_id,
                }

            # Update current owner
            if current_owner_roster_id is not None:
                current_team = team_by_roster.get(current_owner_roster_id)
                if current_team:
                    baseline[key]["team_id"] = current_team.id

            # Backfill pick_number if provided
            if baseline[key].get("pick_number") is None:
                pn = _safe_int(pick.get("pick")) or _safe_int(pick.get("overall"))
                if pn is not None:
                    baseline[key]["pick_number"] = pn

            # Ensure original team display present
            if not baseline[key].get("original_team"):
                orig_team = team_by_roster.get(original_roster_id)
                if orig_team:
                    baseline[key]["original_team"] = orig_team.name or orig_team.owner_name

        # Persist snapshot
        for payload in baseline.values():
            if int(payload["season"]) not in allowed_pick_seasons:
                continue
            team_id = payload.get("team_id")
            if not team_id:
                continue
            draft_pick = SleeperDraftPick(
                league_id=league.id,
                team_id=int(team_id),
                season=int(payload["season"]),
                round=int(payload["round"]),
                pick_number=_safe_int(payload.get("pick_number")),
                original_team=_clean_str(payload.get("original_team")),
                original_roster_id=int(payload["original_roster_id"]),
            )
            db.session.add(draft_pick)
            picks_rows += 1

    league.synced_at = datetime.utcnow()
    db.session.commit()

    try:
        logger = current_app.logger  # type: ignore[attr-defined]
    except Exception:
        logger = None
    if logger:
        logger.info(
            "Synced Sleeper league %s (%s): teams=%s roster_rows=%s picks=%s",
            league.sleeper_id,
            league.name,
            len(team_payloads),
            roster_rows,
            picks_rows,
        )

    return {
        "teams": len(team_payloads),
        "roster_rows": roster_rows,
        "picks": picks_rows,
        "teams_created": teams_created,
    }


def sync_league_via_client(league: SleeperLeague, client: SleeperClient, site_user: User | None = None) -> dict:
    """
    Fetch all Sleeper payloads for a league and apply them.
    IMPORTANT: traded picks are fetched ONCE and filtered by season; we do not
    override the 'season' field to avoid smearing ownership across years.
    """
    league_info = client.get_league(league.sleeper_id)
    rosters     = client.get_league_rosters(league.sleeper_id)
    users       = client.get_league_users(league.sleeper_id)

    base_year = (
        _safe_int((league_info or {}).get("season"))
        or _safe_int(league.year)
        or datetime.utcnow().year
    )

    # STRICT target: future picks only (exclude current year)
    target_pick_seasons = {base_year + 1, base_year + 2, base_year + 3}

    # Determine rounds per target season
    season_rounds = _season_rounds_for_targets(
        client, league_info, league.sleeper_id, sorted(target_pick_seasons)
    )

    # 🔧 KEY: fetch traded picks ONCE; keep their native 'season' and filter
    all_traded = client.get_traded_picks(league.sleeper_id) or []
    picks = [p for p in all_traded if _safe_int(p.get("season")) in target_pick_seasons]

    # Optional: log observed seasons for debugging
    try:
        seasons_present = sorted({ _safe_int(p.get('season')) for p in all_traded if _safe_int(p.get('season')) })
        current_app.logger.info("Sleeper traded_picks seasons present for league %s: %s", league.sleeper_id, seasons_present)
    except Exception:
        pass

    # Matchups (optional)
    current_week = _safe_int((league_info.get("settings") or {}).get("current_week"))
    matchups = client.get_matchups(league.sleeper_id, current_week) if current_week else []

    players_catalog = client.get_players()

    return sync_league_from_payload(
        league,
        league_info=league_info,
        rosters=rosters,
        users=users,
        picks=picks,                              # ← filtered; seasons intact
        allowed_pick_seasons=target_pick_seasons, # N+1..N+3 only
        season_rounds=season_rounds,
        matchups=matchups,
        players_catalog=players_catalog,
        site_user=site_user,
    )
