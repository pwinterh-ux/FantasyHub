# leagues/routes.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import login_required, current_user
from sqlalchemy import MetaData, Table, asc, or_, select

from app import db
from models import League, Team, Player, Roster, DraftPick, DynastyRankConsensusCurrent

leagues_bp = Blueprint("leagues", __name__, url_prefix="/leagues")


@dataclass
class LeagueRow:
    key: str
    platform: str
    db_id: int
    external_id: str | None
    name: str
    season: int | None
    roster_slots_display: str | None
    my_team_name: str | None
    my_team_record: str | None
    my_team_standing: int | None
    details_url: str
    submit_url: str | None
    delete_url: str | None
    platform_label: str


def _mapping_first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            val = mapping[key]
            if val not in (None, ""):
                return val
    return None


_RECORD_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?\s*$")

def _wins_losses_ties_from_str(record_str: str | None) -> tuple[int, int, int]:
    if not record_str:
        return (0, 0, 0)
    m = _RECORD_RE.match(str(record_str))
    if not m:
        return (0, 0, 0)
    w = int(m.group(1)); l = int(m.group(2)); t = int(m.group(3) or 0)
    return (w, l, t)


def _format_record_string(team: Any) -> str | None:
    if isinstance(team, Mapping):
        record_value = _mapping_first(team, "record")
        if record_value:
            return str(record_value)
        wins = _mapping_first(team, "wins", "win")
        losses = _mapping_first(team, "losses", "loss")
        ties = _mapping_first(team, "ties", "tie")
    else:
        record_value = getattr(team, "record", None)
        if record_value:
            return str(record_value)
        wins = getattr(team, "wins", None)
        losses = getattr(team, "losses", None)
        ties = getattr(team, "ties", None)

    try:
        if wins is None or losses is None:
            return None
        wins_i = int(wins); losses_i = int(losses)
        record_str = f"{wins_i}-{losses_i}"
        if ties not in (None, ""):
            ties_i = int(ties)
            if ties_i:
                record_str += f"-{ties_i}"
        return record_str
    except (TypeError, ValueError):
        return None


def _pf_value(row: Mapping[str, Any]) -> float:
    pf = _mapping_first(row, "points_for", "pf")
    try:
        return float(pf) if pf not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _compute_sorted_standings(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    enriched = []
    for r in rows:
        rec = _format_record_string(r)
        w, l, t = _wins_losses_ties_from_str(rec)
        pf = _pf_value(r)
        enriched.append((w, l, t, pf, r))
    enriched.sort(key=lambda tup: (-tup[0], tup[1], -tup[2], -tup[3]))
    out = []
    for idx, (_, _, _, _, row) in enumerate(enriched, start=1):
        new_row = dict(row)
        new_row["computed_standing"] = idx
        out.append(new_row)
    return out


def _format_sleeper_roster_slots(raw: Any) -> str | None:
    if not raw:
        return None
    bench_tokens = {"BN", "BENCH", "BE", "RESERVE", "BN_SUPER"}

    def _join_tokens(tokens: Iterable[str]) -> str | None:
        cleaned = [t for t in (tok.strip() for tok in tokens) if t]
        return ", ".join(cleaned) if cleaned else None

    def _is_bench(label: str) -> bool:
        upper = label.upper()
        if any(upper.startswith(b) for b in bench_tokens):
            return True
        return "BENCH" in upper or upper == "BN"

    if isinstance(raw, (list, tuple)):
        tokens: list[str] = []
        for item in raw:
            if isinstance(item, Mapping):
                pos = str(item.get("position") or item.get("slot") or "").strip()
                if not pos or _is_bench(pos):
                    continue
                count = item.get("count") or item.get("slots") or item.get("limit")
                suffix = f":{count}" if count not in (None, "", 0) else ""
                tokens.append(f"{pos}{suffix}")
            elif isinstance(item, str):
                if not _is_bench(item):
                    tokens.append(item)
        return _join_tokens(tokens)

    if isinstance(raw, Mapping):
        tokens = []
        for key, value in raw.items():
            if key is None:
                continue
            label = str(key)
            if _is_bench(label):
                continue
            suffix = ""
            try:
                if value not in (None, ""):
                    v_int = int(value)
                    if v_int:
                        suffix = f":{v_int}"
            except (TypeError, ValueError):
                suffix = f":{value}"
            tokens.append(f"{label}{suffix}")
        return _join_tokens(tokens)

    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return None
        if stripped.startswith("[") or stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                return _format_sleeper_roster_slots(parsed)
            except json.JSONDecodeError:
                pass
        tokens = []
        for part in stripped.split(","):
            if not part:
                continue
            label = part.strip()
            if not label:
                continue
            head = label.split(":", 1)[0]
            if _is_bench(head):
                continue
            tokens.append(label)
        return _join_tokens(tokens)

    return None


def _reflect_table(name: str) -> Table | None:
    try:
        metadata = MetaData()
        engine = db.session.get_bind()
        return Table(name, metadata, autoload_with=engine)
    except Exception:
        current_app.logger.debug("Unable to reflect table %s", name, exc_info=True)
        db.session.rollback()
        return None


def _delete_mfl_leagues_with_children(leagues: Sequence[League]) -> int:
    """Delete MFL leagues and dependent team/roster/pick rows."""
    removed = 0
    for league in leagues:
        team_ids = [
            tid
            for (tid,) in db.session.query(Team.id)
            .filter(Team.league_id == league.id)
            .all()
        ]
        if team_ids:
            Roster.query.filter(Roster.team_id.in_(team_ids)).delete(synchronize_session=False)
            DraftPick.query.filter(DraftPick.team_id.in_(team_ids)).delete(synchronize_session=False)
            Team.query.filter(Team.id.in_(team_ids)).delete(synchronize_session=False)

        db.session.delete(league)
        db.session.flush()
        removed += 1

    return removed


# ---------- Sleeper helpers ----------

def _sleeper_rows_for_user(user_id: int) -> list[Mapping[str, Any]]:
    table = _reflect_table("sleeper_leagues")
    if table is None:
        return []
    stmt = select(table)
    if "user_id" in table.c:
        stmt = stmt.where(table.c.user_id == user_id)
    rows = db.session.execute(stmt).mappings().all()
    if "user_id" not in table.c:
        rows = [row for row in rows if str(row.get("user_id")) == str(user_id)]
    return rows


def _sleeper_teams_for_league(league_row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    table = _reflect_table("sleeper_teams")
    if table is None:
        return []

    league_db_id = league_row.get("id")
    league_external_id = _mapping_first(league_row, "sleeper_id", "league_id", "external_id")

    filters: list[Any] = []
    if league_db_id is not None and "league_id" in table.c:
        filters.append(table.c.league_id == league_db_id)
    if league_external_id is not None:
        for col in ("sleeper_league_id", "league_key", "league_id_str"):
            if col in table.c:
                filters.append(table.c[col] == str(league_external_id))

    stmt = select(table)
    if filters:
        stmt = stmt.where(or_(*filters))

    rows = db.session.execute(stmt).mappings().all()

    if not rows and league_db_id is not None and "league_id" in table.c:
        stmt = select(table).where(table.c.league_id == league_db_id)
        rows = db.session.execute(stmt).mappings().all()

    return rows


def _identify_my_sleeper_team(
    league_row: Mapping[str, Any],
    team_rows: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    # 1) team.user_id == current_user.id
    for team in team_rows:
        if "user_id" in team and team["user_id"] is not None:
            if str(team["user_id"]) == str(current_user.id):
                return team

    # 2) owner_user_id == user's Sleeper ID
    my_sid = getattr(current_user, "sleeper_user_id", None)
    if my_sid not in (None, ""):
        for team in team_rows:
            if str(team.get("owner_user_id")) == str(my_sid):
                return team

    # 3) roster-id hints on league row
    candidate = _mapping_first(league_row, "user_roster_id", "my_roster_id", "roster_id")
    if candidate not in (None, ""):
        cand_str = str(candidate)
        for team in team_rows:
            identifier = _mapping_first(team, "sleeper_roster_id", "roster_id", "team_id", "id")
            if identifier is not None and str(identifier) == cand_str:
                return team

    return None


def _sleeper_roster_rows(team_row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    table = _reflect_table("sleeper_rosters")
    if table is None:
        return []
    team_db_id = team_row.get("id")
    stmt = select(table)
    if team_db_id is not None and "team_id" in table.c:
        stmt = stmt.where(table.c.team_id == team_db_id)
    return db.session.execute(stmt).mappings().all()


def _sleeper_pick_rows(team_row: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    table = _reflect_table("sleeper_draft_picks")
    if table is None:
        return []
    team_db_id = team_row.get("id")
    stmt = select(table)
    if team_db_id is not None and "team_id" in table.c:
        stmt = stmt.where(table.c.team_id == team_db_id)
    return db.session.execute(stmt).mappings().all()


def _serialize_sleeper_roster(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        player_identifier = _mapping_first(row, "player_sid", "player_id", "sleeper_player_id", "player")
        if player_identifier in (None, ""):
            continue
        position = _mapping_first(row, "position", "pos", "slot", "slot_position")
        team = _mapping_first(row, "team", "nfl_team")
        status = row.get("status")
        starter_flag = _mapping_first(row, "is_starter", "starter", "starting", "is_starting", "active")
        is_starter = False
        if starter_flag not in (None, ""):
            if isinstance(starter_flag, bool):
                is_starter = starter_flag
            else:
                is_starter = str(starter_flag).lower() not in ("0", "false", "no")
        name = _mapping_first(row, "player_name", "name")
        if not name:
            name = str(player_identifier)
        items.append({
            "player_id": str(player_identifier),
            "mfl_id": None,
            "name": name,
            "position": position,
            "team": team,
            "status": status,
            "is_starter": is_starter,
        })
    return items


def _serialize_sleeper_picks(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        season = _mapping_first(row, "season", "year")
        round_no = _mapping_first(row, "round", "round_number")
        pick_number = _mapping_first(row, "pick_number", "pick", "overall")
        original_team = _mapping_first(row, "original_team", "original_roster_id", "origin_roster_id", "owner_roster_id")
        items.append({
            "season": season,
            "round": round_no,
            "pick_number": pick_number,
            "original_team": original_team,
        })
    return items


def _compose_platform_label(platform: str) -> str:
    return "Sleeper" if platform == "sleeper" else "MFL"


# ---------- Views ----------

@leagues_bp.route("", methods=["GET"])
@login_required
def my_leagues():
    # MFL
    leagues = (
        League.query
        .filter_by(user_id=current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )
    my_teams = {}
    for lg in leagues:
        my_team = None
        if lg.franchise_id:
            my_team = Team.query.filter_by(league_id=lg.id, mfl_id=lg.franchise_id).first()
        my_teams[lg.id] = my_team

    rows: list[LeagueRow] = []
    for lg in leagues:
        my_team = my_teams.get(lg.id)
        rows.append(
            LeagueRow(
                key=f"mfl-{lg.id}",
                platform="mfl",
                db_id=lg.id,
                external_id=lg.mfl_id,
                name=lg.name,
                season=lg.year,
                roster_slots_display=lg.roster_slots,
                my_team_name=getattr(my_team, "name", None),
                my_team_record=_format_record_string(my_team) if my_team else None,
                my_team_standing=getattr(my_team, "standing", None) if my_team else None,
                details_url=url_for("leagues.league_details_json", platform="mfl", league_id=lg.id),
                submit_url=url_for("lineups.lineups_single_league", league_id=lg.id, next=request.url),
                delete_url=url_for("leagues.delete_mfl_league", league_id=lg.id),
                platform_label=_compose_platform_label("mfl"),
            )
        )

    # Sleeper
    sleeper_leagues = _sleeper_rows_for_user(current_user.id)
    for sl_row in sleeper_leagues:
        league_id = sl_row.get("id")
        if league_id is None:
            continue
        try:
            league_pk = int(league_id)
        except Exception:
            current_app.logger.warning("Skipping Sleeper league with non-integer id: %s", league_id)
            continue

        name = sl_row.get("name") or sl_row.get("display_name") or "Sleeper League"
        season_raw = sl_row.get("season") or sl_row.get("year")
        try:
            season_val = int(season_raw)
        except (TypeError, ValueError):
            season_val = season_raw
        external_id = _mapping_first(sl_row, "sleeper_id", "league_id", "external_id")
        roster_slots = _mapping_first(sl_row, "roster_slots", "lineup_slots")
        roster_slots_display = _format_sleeper_roster_slots(roster_slots) or (roster_slots if isinstance(roster_slots, str) else None)

        teams = _sleeper_teams_for_league(sl_row)
        teams_sorted = _compute_sorted_standings(teams) if teams else []
        my_team = _identify_my_sleeper_team(sl_row, teams_sorted) if teams_sorted else None

        my_name = None
        my_record = None
        my_standing = None
        if my_team:
            my_name = _mapping_first(my_team, "name", "team_name", "display_name")
            my_record = _mapping_first(my_team, "record") or _format_record_string(my_team)
            standing_val = _mapping_first(my_team, "standing", "rank", "seed", "playoff_seed")
            if standing_val in (None, ""):
                standing_val = my_team.get("computed_standing")
            if standing_val not in (None, ""):
                try:
                    my_standing = int(standing_val)
                except (TypeError, ValueError):
                    my_standing = standing_val

        rows.append(
            LeagueRow(
                key=f"sleeper-{league_pk}",
                platform="sleeper",
                db_id=league_pk,
                external_id=str(external_id) if external_id is not None else None,
                name=name,
                season=season_val if isinstance(season_val, (int, float)) else season_val,
                roster_slots_display=roster_slots_display,
                my_team_name=my_name,
                my_team_record=my_record,
                my_team_standing=my_standing,
                details_url=url_for("leagues.league_details_json", platform="sleeper", league_id=league_pk),
                submit_url=None,
                delete_url=None,
                platform_label=_compose_platform_label("sleeper"),
            )
        )

    rows.sort(key=lambda r: (-(r.season or 0) if isinstance(r.season, (int, float)) else 0, (r.name or "").lower()))
    return render_template("my_leagues.html", leagues=rows)


@leagues_bp.route("/mfl/<int:league_id>/delete", methods=["POST"])
@login_required
def delete_mfl_league(league_id: int):
    league = League.query.filter_by(id=league_id, user_id=current_user.id).first()
    if not league:
        if "application/json" in (request.headers.get("Accept") or "").lower():
            return jsonify({"ok": False, "error": "League not found"}), 404
        abort(404)

    league_name = league.name
    try:
        _delete_mfl_leagues_with_children([league])
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.exception(
            "delete_mfl_league failed for league=%s user=%s: %s",
            league_id,
            current_user.id,
            exc,
        )
        if "application/json" in (request.headers.get("Accept") or "").lower():
            return jsonify({"ok": False, "error": "Unable to delete league."}), 500
        flash("Unable to delete league. Please try again.", "danger")
        return redirect(url_for("leagues.my_leagues"))

    if "application/json" in (request.headers.get("Accept") or "").lower():
        return jsonify({"ok": True, "league_id": league_id, "league_name": league_name})

    flash(f"Deleted league {league_name}.", "success")
    return redirect(url_for("leagues.my_leagues"))


@leagues_bp.route("/<platform>/<int:league_id>/details.json", methods=["GET"])
@leagues_bp.route("/<int:league_id>/details.json", methods=["GET"], defaults={"platform": "mfl"})
@login_required
def league_details_json(league_id: int, platform: str):
    platform_normalized = (platform or "mfl").lower()
    if platform_normalized not in {"mfl", "sleeper"}:
        abort(404)
    if platform_normalized == "mfl":
        return _league_details_json_mfl(league_id)
    return _league_details_json_sleeper(league_id)


def _league_details_json_mfl(league_id: int):
    try:
        league = League.query.filter_by(id=league_id, user_id=current_user.id).first()
        if not league:
            abort(404)

        teams = (
            Team.query
            .filter_by(league_id=league.id)
            .order_by(Team.standing.is_(None), asc(Team.standing), asc(Team.name))
            .all()
        )

        my_team = None
        if league.franchise_id:
            my_team = Team.query.filter_by(league_id=league.id, mfl_id=league.franchise_id).first()

        roster_items = []
        if my_team:
            rows = (
                db.session.query(Roster, Player)
                .join(Player, Player.id == Roster.player_id)
                .filter(Roster.team_id == my_team.id)
                .order_by(asc(Player.position), asc(Player.name))
                .all()
            )
            mfl_ids = [str(p.mfl_id) for _, p in rows if p.mfl_id]
            positions = [str(p.position).upper() for _, p in rows if p.position]
            consensus_by_key: dict[tuple[str, str], DynastyRankConsensusCurrent] = {}
            if mfl_ids:
                consensus_rows = (
                    DynastyRankConsensusCurrent.query
                    .filter(DynastyRankConsensusCurrent.mfl_id.in_(mfl_ids))
                    .filter(DynastyRankConsensusCurrent.position.in_(positions))
                    .all()
                )
                consensus_by_key = {
                    (str(c.position).upper(), str(c.mfl_id)): c for c in consensus_rows
                }
            for r, p in rows:
                lookup_key = (str(p.position).upper(), str(p.mfl_id)) if (p.position and p.mfl_id) else None
                consensus = consensus_by_key.get(lookup_key) if lookup_key else None
                roster_items.append({
                    "player_id": p.id,
                    "mfl_id": p.mfl_id,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "status": p.status,
                    "position_rank": (consensus.positional_rank if consensus else None),
                    "is_starter": bool(r.is_starter),
                })

        draft_picks = []
        if my_team:
            picks = (
                DraftPick.query
                .filter_by(team_id=my_team.id)
                .order_by(asc(DraftPick.season), asc(DraftPick.round), asc(DraftPick.pick_number))
                .all()
            )
            for dp in picks:
                draft_picks.append({
                    "season": dp.season,
                    "round": dp.round,
                    "pick_number": dp.pick_number,
                    "original_team": dp.original_team,
                })

        payload = {
            "league": {
                "id": league.id,
                "name": league.name,
                "mfl_id": league.mfl_id,
                "year": league.year,
                "roster_slots": league.roster_slots,
                "franchise_id": league.franchise_id,
                "synced_at": league.synced_at.isoformat() if league.synced_at else None,
                "platform": "mfl",
                "external_id_label": f"MFL {league.mfl_id}" if league.mfl_id else None,
                "my_team_identifier": league.franchise_id,
            },
            "teams": [
                {
                    "mfl_id": t.mfl_id,
                    "name": t.name,
                    "owner_name": t.owner_name,
                    "record": t.record,
                    "points_for": t.points_for,
                    "points_against": t.points_against,
                    "standing": t.standing,
                    "identifier": t.mfl_id,
                }
                for t in teams
            ],
            "my_team": (
                {
                    "mfl_id": my_team.mfl_id,
                    "name": my_team.name,
                    "record": my_team.record,
                    "points_for": my_team.points_for,
                    "points_against": my_team.points_against,
                    "standing": my_team.standing,
                    "identifier": my_team.mfl_id,
                } if my_team else None
            ),
            "my_roster": roster_items,
            "my_draft_picks": draft_picks,
            "counts": {
                "teams": len(teams),
                "roster": len(roster_items),
                "draft_picks": len(draft_picks),
            }
        }
        return jsonify(payload)

    except Exception:
        current_app.logger.exception("league_details_json failed")
        return jsonify({"error": "internal"}), 500


def _league_details_json_sleeper(league_id: int):
    try:
        table = _reflect_table("sleeper_leagues")
        if table is None:
            abort(404)

        stmt = select(table).where(table.c.id == league_id)
        if "user_id" in table.c:
            stmt = stmt.where(table.c.user_id == current_user.id)

        league_row = db.session.execute(stmt).mappings().first()
        if not league_row:
            abort(404)

        if "user_id" in league_row and str(league_row["user_id"]) != str(current_user.id):
            abort(404)

        teams = _sleeper_teams_for_league(league_row)
        teams_sorted = _compute_sorted_standings(teams) if teams else []

        def serialize_team(row: Mapping[str, Any]) -> dict[str, Any]:
            record = _format_record_string(row)
            standing_val = _mapping_first(row, "standing", "rank", "seed", "playoff_seed")
            if standing_val in (None, ""):
                standing_val = row.get("computed_standing")
            try:
                standing_int = int(standing_val) if standing_val not in (None, "") else None
            except (TypeError, ValueError):
                standing_int = None
            return {
                "mfl_id": None,
                "name": _mapping_first(row, "name", "team_name", "display_name"),
                "owner_name": _mapping_first(row, "owner_name", "owner", "manager"),
                "record": record,
                "points_for": _mapping_first(row, "points_for", "pf"),
                "points_against": _mapping_first(row, "points_against", "pa"),
                "standing": standing_int,
                "identifier": _mapping_first(row, "sleeper_roster_id", "roster_id", "team_id", "id"),
            }

        teams_payload = [serialize_team(row) for row in teams_sorted]

        my_team_row = _identify_my_sleeper_team(league_row, teams_sorted)
        my_team_payload = serialize_team(my_team_row) if my_team_row else None

        roster_rows: list[Mapping[str, Any]] = []
        pick_rows: list[Mapping[str, Any]] = []
        if my_team_row:
            roster_rows = _sleeper_roster_rows(my_team_row)
            pick_rows = _sleeper_pick_rows(my_team_row)

        roster_items = _serialize_sleeper_roster(roster_rows)
        draft_picks = _serialize_sleeper_picks(pick_rows)

        roster_slots = _mapping_first(league_row, "roster_slots", "lineup_slots")
        roster_slots_display = _format_sleeper_roster_slots(roster_slots) or (
            roster_slots if isinstance(roster_slots, str) else None
        )

        synced_at = _mapping_first(league_row, "synced_at", "updated_at")
        synced_value = synced_at.isoformat() if hasattr(synced_at, "isoformat") else synced_at

        external_identifier = _mapping_first(league_row, "sleeper_id", "league_id", "external_id")

        payload = {
            "league": {
                "id": league_row.get("id"),
                "name": league_row.get("name") or league_row.get("display_name"),
                "mfl_id": None,
                "year": _mapping_first(league_row, "season", "year"),
                "roster_slots": roster_slots_display,
                "franchise_id": _mapping_first(league_row, "user_roster_id", "my_roster_id"),
                "synced_at": synced_value,
                "platform": "sleeper",
                "external_id_label": (f"Sleeper {external_identifier}" if external_identifier else None),
                "my_team_identifier": _mapping_first(league_row, "user_roster_id", "my_roster_id", "roster_id"),
            },
            "teams": teams_payload,
            "my_team": my_team_payload,
            "my_roster": roster_items,
            "my_draft_picks": draft_picks,
            "counts": {
                "teams": len(teams_payload),
                "roster": len(roster_items),
                "draft_picks": len(draft_picks),
            },
        }
        return jsonify(payload)

    except Exception:
        current_app.logger.exception("league_details_json_sleeper failed")
        db.session.rollback()
        return jsonify({"error": "internal"}), 500


@leagues_bp.get("/players_lookup")
@leagues_bp.get("/players-lookup")  # alias
@login_required
def players_lookup():
    """
    GET /leagues/players_lookup?platform=sleeper&ids=ID1,ID2,ID3
    Returns: { "ID1": {"name": "...", "position": "...", "team": "...", "status": "..."}, ... }
    """
    try:
        platform = (request.args.get("platform") or "").lower()
        ids_raw = request.args.get("ids") or ""
        ids = [s.strip() for s in ids_raw.split(",") if s.strip()]
        if not ids or platform != "sleeper":
            return jsonify({})

        def build_map_from_table(tablename: str) -> dict[str, dict[str, Any]]:
            table = _reflect_table(tablename)
            if table is None:
                return {}
            cols = table.c.keys()

            def first_exist(*cands: str) -> str | None:
                for c in cands:
                    if c in cols:
                        return c
                return None

            key_col_name = first_exist("sleeper_id", "player_sid", "player_id", "id")
            if key_col_name is None:
                return {}

            name_col   = first_exist("name", "full_name", "display_name")
            first_col  = first_exist("first_name", "fname")
            last_col   = first_exist("last_name", "lname")
            pos_col    = first_exist("position", "pos")
            team_col   = first_exist("team", "team_abbr", "nfl_team")
            status_col = first_exist("status", "injury_status", "injury")

            key_col = getattr(table.c, key_col_name)

            sel_cols = [key_col]
            for nm in (name_col, first_col, last_col, pos_col, team_col, status_col):
                if nm:
                    sel_cols.append(getattr(table.c, nm))
            stmt = select(*sel_cols).where(key_col.in_(ids))

            rows = db.session.execute(stmt).mappings().all()
            out: dict[str, dict[str, Any]] = {}
            for r in rows:
                key_val = r.get(key_col_name)
                if key_val is None:
                    continue
                pid = str(key_val)
                # Name preference: name -> first/last -> display fallback
                nm = r.get(name_col) if name_col else None
                if not nm and (first_col or last_col):
                    first = (r.get(first_col) or "").strip() if first_col else ""
                    last = (r.get(last_col) or "").strip() if last_col else ""
                    nm = (first + " " + last).strip() or None
                out[pid] = {
                    "name": nm,
                    "position": r.get(pos_col) if pos_col else None,
                    "team": r.get(team_col) if team_col else None,
                    "status": r.get(status_col) if status_col else None,
                }
            return out

        # Try sleeper_players first, then a generic players table as a fallback
        result = build_map_from_table("sleeper_players")
        missing = [i for i in ids if i not in result]
        if missing:
            more = build_map_from_table("players")
            result.update({k: v for k, v in more.items() if k in missing})

        return jsonify(result)

    except Exception:
        current_app.logger.exception("players_lookup failed")
        db.session.rollback()
        return jsonify({}), 500
