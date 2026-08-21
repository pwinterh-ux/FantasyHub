# exposure/routes.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from flask import Blueprint, current_app, render_template, request
from flask_login import login_required, current_user
from sqlalchemy import MetaData, Table, select, asc

from app import db
from models import League, Player, Roster, Team

exposure_bp = Blueprint(
    "exposure",
    __name__,
    url_prefix="/exposure",
    template_folder="../templates",
)

# ----------------------------
# Helpers
# ----------------------------

def _reflect_table(name: str) -> Optional[Table]:
    try:
        metadata = MetaData()
        engine = db.session.get_bind()
        return Table(name, metadata, autoload_with=engine)
    except Exception:
        current_app.logger.debug("Unable to reflect table %s", name, exc_info=True)
        db.session.rollback()
        return None

def _norm_id(val: Any) -> Optional[str]:
    if val in (None, ""):
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return str(int(s))
    except Exception:
        return s


def _load_draft_meta(mfl_ids: Iterable[str]) -> dict[str, dict]:
    """
    Load RosterDash-owned draft metadata keyed by canonical MFL id.

    This table is intentionally separate from the MFL players table so
    normal player-pool refreshes cannot overwrite draft-class metadata.
    """
    ids = sorted({
        str(mid).strip()
        for mid in mfl_ids
        if mid not in (None, "") and str(mid).strip()
    })

    if not ids:
        return {}

    tbl = _reflect_table("player_draft_meta")
    if tbl is None:
        return {}

    stmt = (
        select(
            tbl.c.mfl_id,
            tbl.c.draft_year,
            tbl.c.draft_round,
            tbl.c.draft_pick,
            tbl.c.draft_team,
            tbl.c.is_udfa,
            tbl.c.source,
        )
        .where(tbl.c.mfl_id.in_(ids))
    )

    out: dict[str, dict] = {}

    for row in db.session.execute(stmt).mappings().all():
        mid = str(row.get("mfl_id") or "").strip()

        if not mid:
            continue

        out[mid] = {
            "draft_year": (
                int(row["draft_year"])
                if row.get("draft_year") is not None
                else None
            ),
            "draft_round": (
                int(row["draft_round"])
                if row.get("draft_round") is not None
                else None
            ),
            "draft_pick": (
                int(row["draft_pick"])
                if row.get("draft_pick") is not None
                else None
            ),
            "draft_team": row.get("draft_team"),
            "is_udfa": bool(row.get("is_udfa")),
            "source": row.get("source"),
        }

    return out

@dataclass
class ExpoRow:
    key: str                     # canonical key (mfl:<id>) or np:<name>|<pos>
    name: str
    position: str | None
    team: str | None
    total: int
    starters: int
    bench: int
    platforms: set[str]          # {"mfl","sleeper"}
    holdings: list[dict]         # [{league, year, platform, is_starter, key}]

# ----------------------------
# Gather "my" players (MFL)
# ----------------------------

def _gather_mfl_holdings() -> list[dict]:
    """
    Returns rows like:
      { "platform":"mfl", "league_name":..., "league_year":..., "player_mfl_id": "1234",
        "name":..., "position":..., "team":..., "is_starter": bool }
    Only for the user's own franchise in each MFL league.
    """
    leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )
    out: list[dict] = []

    for lg in leagues:
        my_team: Optional[Team] = None
        if lg.franchise_id:
            my_team = (
                db.session.query(Team)
                .filter(Team.league_id == lg.id, Team.mfl_id == lg.franchise_id)
                .first()
            )
        if not my_team:
            continue

        rows = (
            db.session.query(Roster, Player)
            .join(Player, Player.id == Roster.player_id)
            .filter(Roster.team_id == my_team.id)
            .all()
        )
        for r, p in rows:
            out.append(
                {
                    "platform": "mfl",
                    "league_name": lg.name,
                    "league_year": lg.year,
                    "player_mfl_id": _norm_id(p.mfl_id),
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "is_starter": bool(r.is_starter),
                }
            )

    return out

# ----------------------------
# Gather "my" players (Sleeper)
# ----------------------------

def _sleeper_leagues_for_user(user_id: int) -> list[Mapping[str, Any]]:
    tbl = _reflect_table("sleeper_leagues")
    if tbl is None:
        return []
    stmt = select(tbl).where(tbl.c.user_id == user_id).order_by(asc(tbl.c.name))
    return db.session.execute(stmt).mappings().all()

def _sleeper_teams_for_league(league_db_id: int) -> list[Mapping[str, Any]]:
    teams = _reflect_table("sleeper_teams")
    if teams is None:
        return []
    stmt = select(teams).where(teams.c.league_id == league_db_id)
    return db.session.execute(stmt).mappings().all()

def _identify_my_sleeper_team(team_rows: list[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    # 1) linked to site user
    for t in team_rows:
        if "user_id" in t and t["user_id"] is not None and str(t["user_id"]) == str(current_user.id):
            return t
    # 2) owner_user_id matches my sleeper id
    my_sid = getattr(current_user, "sleeper_user_id", None)
    if my_sid not in (None, ""):
        for t in team_rows:
            if str(t.get("owner_user_id")) == str(my_sid):
                return t
    # 3) best-effort fallback
    return team_rows[0] if team_rows else None

def _gather_sleeper_holdings() -> list[dict]:
    """
    Returns rows like MFL path, but platform="sleeper".
    Uses sleeper_players.mfl_id mapping when available; else falls back to name+pos key later.
    """
    rosters_tbl = _reflect_table("sleeper_rosters")
    players_tbl = _reflect_table("sleeper_players")
    if rosters_tbl is None or players_tbl is None:
        return []

    out: list[dict] = []
    for lg in _sleeper_leagues_for_user(current_user.id):
        teams = _sleeper_teams_for_league(int(lg["id"]))
        my_team = _identify_my_sleeper_team(teams)
        if not my_team:
            continue

        q = (
            select(
                rosters_tbl.c.is_starter,
                rosters_tbl.c.lineup_slot,
                rosters_tbl.c.player_sid,
                players_tbl.c.mfl_id,
                players_tbl.c.name,
                players_tbl.c.position,
                players_tbl.c.team,
            )
            .select_from(rosters_tbl.join(players_tbl, rosters_tbl.c.player_sid == players_tbl.c.sleeper_id))
            .where(rosters_tbl.c.team_id == my_team["id"])
        )
        rows = db.session.execute(q).mappings().all()
        for r in rows:
            is_starter = bool(r.get("is_starter"))
            slot = (r.get("lineup_slot") or "").strip().lower()
            if slot == "starter":
                is_starter = True
            out.append(
                {
                    "platform": "sleeper",
                    "league_name": lg.get("name") or lg.get("display_name") or "Sleeper League",
                    "league_year": lg.get("year"),
                    "player_mfl_id": _norm_id(r.get("mfl_id")),  # may be None for a few odd cases
                    "name": r.get("name") or str(r.get("player_sid")),
                    "position": r.get("position"),
                    "team": r.get("team"),
                    "is_starter": is_starter,
                }
            )

    return out

# ----------------------------
# Aggregation
# ----------------------------

def _make_key(row: dict) -> Tuple[str, str]:
    """
    Prefer canonical MFL id; fallback to (NAME|POS) which is stable enough for display.
    Returns (key, kind) where kind is "mfl" or "np" (name/pos).
    """
    mfl_id = row.get("player_mfl_id")
    if mfl_id:
        return (f"mfl:{mfl_id}", "mfl")
    name = (row.get("name") or "").strip()
    pos = (row.get("position") or "").strip()
    return (f"np:{name}|{pos}", "np")

def _aggregate(
    holdings: list[dict],
    *,
    pos_filter: Optional[str],
    search: Optional[str],
) -> list[ExpoRow]:
    buckets: dict[str, ExpoRow] = {}

    s_term = (search or "").strip().lower() or None
    p_term = (pos_filter or "").strip().upper() or None

    for h in holdings:
        # filters first
        if p_term and (h.get("position") or "").upper() != p_term:
            continue
        if s_term:
            blob = f"{h.get('name','')} {h.get('team','')} {h.get('position','')}".lower()
            if s_term not in blob:
                continue

        key, _kind = _make_key(h)
        cur = buckets.get(key)
        if not cur:
            cur = ExpoRow(
                key=key,
                name=h.get("name") or "Unknown",
                position=h.get("position"),
                team=h.get("team"),
                total=0,
                starters=0,
                bench=0,
                platforms=set(),
                holdings=[],
            )
            buckets[key] = cur

        cur.total += 1
        if h.get("is_starter"):
            cur.starters += 1
        else:
            cur.bench += 1
        cur.platforms.add(h.get("platform") or "")
        # Add a normalized league key for client-side overlap logic
        lg_name = h.get("league_name") or ""
        lg_year = h.get("league_year") or ""
        plat = h.get("platform") or ""
        lg_key = f"{plat}:{lg_name}|{lg_year}"
        cur.holdings.append(
            {
                "league": lg_name,
                "year": lg_year,
                "platform": plat,
                "is_starter": bool(h.get("is_starter")),
                "key": lg_key,
            }
        )

    rows = list(buckets.values())
    # Sort: most owned → highest start rate → name
    rows.sort(key=lambda r: (-r.total, -(r.starters / r.total if r.total else 0.0), (r.name or "").lower()))
    return rows

def _unique_league_counts(holdings: list[dict]) -> dict[str, int]:
    mfl = set()
    slp = set()
    for h in holdings:
        plat = h.get("platform")
        nm = h.get("league_name") or ""
        yr = h.get("league_year") or ""
        key = f"{plat}:{nm}|{yr}"
        if plat == "mfl":
            mfl.add(key)
        elif plat == "sleeper":
            slp.add(key)
    return {
        "mfl": len(mfl),
        "sleeper": len(slp),
        "total": len(mfl | slp),
    }

# ----------------------------
# View
# ----------------------------

@exposure_bp.route("", methods=["GET"])
@login_required
def exposure_index():
    pos = request.args.get("pos")  # QB/RB/WR/TE/...
    q = request.args.get("q")
    anchor = request.args.get("anchor")  # optional preselect player key (e.g., mfl:1234)

    # Draft-class selection is a DISPLAY filter only.
    # Exposure totals/league denominators are calculated before this is applied.
    selected_draft_set: set[str] = set()

    for raw in request.args.getlist("draft"):
        value = str(raw or "").strip().lower()

        if value == "unknown" or value.isdigit():
            selected_draft_set.add(value)

    # gather both platforms
    mfl = _gather_mfl_holdings()
    sleeper = _gather_sleeper_holdings()
    all_holdings = mfl + sleeper

    # league totals for % shares
    league_counts = _unique_league_counts(all_holdings)

    rows = _aggregate(all_holdings, pos_filter=pos, search=q)

    # ---- JSON-safe payload ----
    rows_payload = [{
        "key": r.key,
        "name": r.name,
        "position": r.position,
        "team": r.team,
        "total": r.total,
        "starters": r.starters,
        "bench": r.bench,
        "platforms": sorted(list(r.platforms)),
        "holdings": list(r.holdings or []),
        # breakdown by platform so we can print MFL/Sleeper counts quickly
        "mfl_count": sum(1 for h in r.holdings if h.get("platform") == "mfl"),
        "sleeper_count": sum(1 for h in r.holdings if h.get("platform") == "sleeper"),
    } for r in rows]

    # ---------------------------------------------------------
    # Draft metadata enrichment
    #
    # IMPORTANT:
    # rows were already aggregated above across ALL leagues.
    # Filtering below only determines which player cards render.
    # It does not alter exposure counts, league denominators,
    # platform counts, or exposure percentages.
    # ---------------------------------------------------------
    mfl_ids = [
        r["key"].split(":", 1)[1]
        for r in rows_payload
        if str(r.get("key") or "").startswith("mfl:")
    ]

    draft_meta = _load_draft_meta(mfl_ids)

    for r in rows_payload:
        key = str(r.get("key") or "")
        mid = (
            key.split(":", 1)[1]
            if key.startswith("mfl:")
            else None
        )

        meta = draft_meta.get(mid or "", {})

        r["mfl_id"] = mid
        r["draft_year"] = meta.get("draft_year")
        r["draft_round"] = meta.get("draft_round")
        r["draft_pick"] = meta.get("draft_pick")
        r["draft_team"] = meta.get("draft_team")
        r["is_udfa"] = bool(meta.get("is_udfa"))
        r["draft_source"] = meta.get("source")

    # Build the checkbox menu from draft classes that are actually
    # represented in the full aggregated exposure result.
    draft_years = sorted(
        {
            int(r["draft_year"])
            for r in rows_payload
            if r.get("draft_year") is not None
        },
        reverse=True,
    )

    unknown_available = any(
        r.get("draft_year") is None
        for r in rows_payload
    )

    draft_options = [
        {
            "value": str(year),
            "label": str(year),
            "selected": str(year) in selected_draft_set,
        }
        for year in draft_years
    ]

    if unknown_available:
        draft_options.append(
            {
                "value": "unknown",
                "label": "Unknown",
                "selected": "unknown" in selected_draft_set,
            }
        )

    selected_labels = [
        opt["label"]
        for opt in draft_options
        if opt["selected"]
    ]

    if not selected_labels:
        draft_filter_label = "Draft Class"
    elif len(selected_labels) == 1:
        draft_filter_label = selected_labels[0]
    else:
        draft_filter_label = (
            f"{selected_labels[0]} + {len(selected_labels) - 1}"
        )

    def _draft_visible(row: dict) -> bool:
        if not selected_draft_set:
            return True

        year = row.get("draft_year")

        if year is None:
            return "unknown" in selected_draft_set

        return str(year) in selected_draft_set

    visible_rows = [
        r
        for r in rows_payload
        if _draft_visible(r)
    ]

    # header summary
    summary = {
        "total_players": len(rows_payload),
        "visible_players": len(visible_rows),
        "total_holdings": sum(r["total"] for r in rows_payload),
        "unique_platforms": sorted({p for r in rows_payload for p in r["platforms"] if p}),
        "leagues_total": league_counts["total"],
        "leagues_mfl": league_counts["mfl"],
        "leagues_sleeper": league_counts["sleeper"],
    }

    return render_template(
        "exposure/index.html",
        rows=visible_rows,
        summary=summary,
        filters={
            "pos": pos or "",
            "q": q or "",
            "anchor": anchor or "",
            "drafts": sorted(selected_draft_set),
            "draft_options": draft_options,
            "draft_label": draft_filter_label,
        },
    )
