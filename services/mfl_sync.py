# services/mfl_sync.py
from __future__ import annotations

from typing import List, Dict, Optional, Any, Iterable, Tuple

from flask import current_app
from app import db
from models import League, Team, Player, Roster, DraftPick
from services.mfl_parsers import FranchiseAssets, StandingRow


# ------------------------- Small helpers ------------------------------------

def _get(fr: Any, attr: str, default=None):
    """Duck-typed getattr / dict.get."""
    if isinstance(fr, dict):
        return fr.get(attr, default)
    return getattr(fr, attr, default)

def _fid(x: Any) -> str:
    """
    Normalize MFL franchise ids to 4-char, zero-padded strings.
    If it's not purely digits, return as-is (rare).
    """
    s = str(x).strip()
    return s.zfill(4) if s.isdigit() else s

def _ensure_team(league_id: int, franchise_id: str, name_hint: Optional[str] = None) -> Team:
    """
    Ensure a Team exists for (league_id, franchise_id). If creating, use name_hint
    when provided; if an existing team still has a placeholder name, upgrade it.
    """
    fid = _fid(franchise_id)
    team = Team.query.filter_by(league_id=league_id, mfl_id=fid).first()
    if team:
        # opportunistic upgrade from placeholder
        if name_hint:
            nh = str(name_hint).strip()
            if nh and (not team.name or team.name.lower().startswith("franchise ")):
                team.name = nh
                db.session.flush()
        return team

    # create new
    display_name = (str(name_hint).strip() if name_hint and str(name_hint).strip() else f"Franchise {fid}")
    team = Team(league_id=league_id, mfl_id=fid, name=display_name)
    db.session.add(team)
    db.session.flush()
    return team

def _ensure_player(pid: int) -> Player:
    """Create a placeholder Player if the catalog doesn't have it yet."""
    p = Player.query.get(pid)
    if not p:
        p = Player(
            id=pid,
            mfl_id=str(pid),
            name=f"Player #{pid}",
            position=None,
            team=None,
            status=None,
        )
        db.session.add(p)
        db.session.flush()
    return p


# ------------------------- Extractors (robust) -------------------------------

def _split_csv(s: str) -> list[str]:
    return [tok.strip() for tok in s.split(",") if tok and tok.strip()]

def _iter_player_ids(fr: Any) -> Iterable[int]:
    """
    Accepts variants:
      - fr.player_ids -> [13593, 15241, ...]
      - fr.players    -> [13593, "15241", {"id": "14109"}, ...]  OR "13593,15241,14109"
      - fr.roster     -> "13593,15241,14109" (fallback if parser stashes CSV here)
    """
    vals = _get(fr, "player_ids") or _get(fr, "players") or _get(fr, "roster") or []
    # CSV fallback
    if isinstance(vals, str):
        vals = _split_csv(vals)

    for item in vals:
        raw = item.get("id") if isinstance(item, dict) else item
        if raw in (None, ""):
            continue
        try:
            yield int(raw)
        except (TypeError, ValueError):
            continue

def _parse_pick_code(code: str) -> Optional[Tuple[int, int, Optional[str]]]:
    """
    'FP_<orig>_<season>_<round>' -> (season, round, original_team)
    """
    parts = str(code).strip().split("_")
    if len(parts) >= 4:
        try:
            season = int(parts[2])
            rnd = int(parts[3])
            orig = parts[1]
            return (season, rnd, _fid(orig))
        except Exception:
            return None
    return None

def _iter_picks(fr: Any) -> Iterable[Tuple[int, int, Optional[str]]]:
    """
    Accepts variants:
      - fr.future_picks -> [(2026, 1, "0002"), ...]  (dataclass shape)
      - fr.picks        -> ["FP_0002_2026_1", ...]   (MFL string codes) or CSV string
      - fr.picks        -> [{"season":2026,"round":1,"original_team":"0002"}, ...]
    Yields (season, round, original_team) with pick_number=None (unknown).
    """
    # Preferred shape from dataclass
    fp = _get(fr, "future_picks")
    if isinstance(fp, list) and fp and isinstance(fp[0], tuple):
        for season, rnd, orig in fp:
            try:
                yield int(season), int(rnd), (_fid(orig) if orig is not None else None)
            except Exception:
                continue

    picks = _get(fr, "picks") or []
    if isinstance(picks, str):
        picks = _split_csv(picks)

    for item in picks:
        if isinstance(item, dict):
            try:
                season = int(item.get("season"))
                rnd = int(item.get("round"))
                original = item.get("original_team") or item.get("originalTeam")
                original = _fid(original) if original is not None else None
                yield season, rnd, original
            except Exception:
                continue
        else:
            parsed = _parse_pick_code(str(item))
            if parsed:
                yield parsed  # already normalized original team


# ------------------------- League metadata sync ------------------------------

def sync_league_info(
    league: League,
    franchise_meta: Dict[str, Dict[str, Any]] | Dict[str, Any],
    roster_slots: Optional[str] = None,
) -> Dict[str, int]:
    """
    Upsert franchise rows (names/owners/abbrev) and optionally update league.roster_slots.
    Accepts meta values as dicts OR dataclasses (via _get).
    """
    created = updated = roster_updated = 0

    for fid_raw, meta in (franchise_meta or {}).items():
        fid = _fid(fid_raw)
        team = Team.query.filter_by(league_id=league.id, mfl_id=fid).first()
        name  = (str(_get(meta, "name", "")).strip())
        owner = (str(_get(meta, "owner_name", _get(meta, "ownerName", ""))).strip())
        abbr  = (str(_get(meta, "abbrev", _get(meta, "abbreviation", ""))).strip())

        if not team:
            team = Team(
                league_id=league.id,
                mfl_id=fid,
                name=(name or f"Franchise {fid}"),
            )
            if hasattr(team, "owner_name") and owner:
                team.owner_name = owner
            if hasattr(team, "abbrev") and abbr:
                team.abbrev = abbr
            db.session.add(team)
            created += 1
            continue

        changed = False
        if name and team.name != name:
            team.name = name
            changed = True
        if owner and hasattr(team, "owner_name") and team.owner_name != owner:
            team.owner_name = owner
            changed = True
        if abbr and hasattr(team, "abbrev") and team.abbrev != abbr:
            team.abbrev = abbr
            changed = True
        if changed:
            updated += 1

    # roster slots text (starters)
    if roster_slots is not None and hasattr(league, "roster_slots"):
        if league.roster_slots != roster_slots:
            league.roster_slots = roster_slots
            roster_updated = 1

    db.session.commit()
    return {"teams_created": created, "teams_updated": updated, "roster_text_updated": roster_updated}


# ------------------------- Assets (rosters + picks) --------------------------

def sync_league_assets(league: League, franchises: List[FranchiseAssets] | List[Dict[str, Any]]) -> Dict[str, int]:
    """
    Idempotent write of league-wide assets:
      - For each franchise (team), delete existing Roster & DraftPick rows
      - Re-insert rosters from player_ids / players / roster (csv)
      - Re-insert future draft picks from future_picks / picks (list/dict/csv)

    Accepts either our FranchiseAssets dataclass OR a dict with similar keys.

    Returns metrics: {teams_touched, rosters_inserted, picks_inserted}
    """
    inserted_rosters = 0
    inserted_picks = 0
    teams_touched = 0

    for fr in franchises:
        # Accept 'franchise_id' (preferred) or 'id'
        franchise_id_raw = _get(fr, "franchise_id") or _get(fr, "id")
        if not franchise_id_raw:
            continue
        franchise_id = _fid(franchise_id_raw)

        # Opportunistically use a name if the object carries one (dict variant).
        name_hint = _get(fr, "name") or _get(fr, "team_name")
        team = _ensure_team(league.id, franchise_id, name_hint=name_hint)

        # Preview counts (diagnostics)
        player_ids = list(_iter_player_ids(fr))
        pick_items = list(_iter_picks(fr))
        current_app.logger.info(
            "assets: league %s (%s) fid=%s -> players=%d, picks=%d",
            league.id, league.mfl_id, franchise_id, len(player_ids), len(pick_items)
        )

        # Clear existing data (both children of Team)
        Roster.query.filter_by(team_id=team.id).delete(synchronize_session=False)
        DraftPick.query.filter_by(team_id=team.id).delete(synchronize_session=False)
        db.session.commit()   # keep per-team ops safe and incremental

        # Rebuild roster
        for pid in player_ids:
            _ensure_player(pid)
            db.session.add(Roster(team_id=team.id, player_id=pid, is_starter=False))
            inserted_rosters += 1

        # Rebuild future picks
        for season, rnd, original in pick_items:
            db.session.add(
                DraftPick(
                    team_id=team.id,
                    season=int(season),
                    round=int(rnd),
                    pick_number=None,
                    original_team=(original if original is None else _fid(original)),
                )
            )
            inserted_picks += 1

        db.session.commit()
        teams_touched += 1

    return {
        "teams_touched": teams_touched,
        "rosters_inserted": inserted_rosters,
        "picks_inserted": inserted_picks,
    }


# ------------------------- Standings (record, PF/PA, rank) -------------------

def sync_league_standings(league: League, rows: List[StandingRow] | List[Dict[str, Any]]) -> int:
    """
    Apply standings for a league:
      - Ensures each franchise Team exists (normalized fid)
      - Updates record, points_for, points_against, standing

    Returns number of teams updated.
    """
    updated = 0

    for row in rows:
        # Duck-typed read to tolerate dict rows if needed
        fr_id = _get(row, "franchise_id")
        if not fr_id:
            continue
        fid = _fid(fr_id)

        # If row carries a team name in some variants (e.g., dict rows with 'name' or 'fname'),
        # pass it as a hint to upgrade placeholders.
        name_hint = _get(row, "name") or _get(row, "fname")
        team = _ensure_team(league.id, fid, name_hint=name_hint)

        # Record text
        rec = _get(row, "record") or "0-0-0"
        if hasattr(team, "record"):
            team.record = rec

        # PF/PA may be str/float; store as ints (rounding)
        pf_val = _get(row, "pf", 0)
        pa_val = _get(row, "pa", 0)
        if hasattr(team, "points_for"):
            try:
                team.points_for = int(round(float(pf_val)))
            except Exception:
                team.points_for = 0
        if hasattr(team, "points_against"):
            try:
                team.points_against = int(round(float(pa_val)))
            except Exception:
                team.points_against = 0

        rank_val = _get(row, "rank", None)
        if hasattr(team, "standing") and rank_val is not None:
            try:
                team.standing = int(rank_val)
            except Exception:
                # keep previous if unparsable
                pass

        updated += 1

    db.session.commit()
    return updated
