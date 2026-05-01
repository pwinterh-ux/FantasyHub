# tools/routes.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any
import xml.etree.ElementTree as ET

import requests

from flask import Blueprint, render_template, current_app, jsonify
from flask_login import login_required, current_user
from sqlalchemy import func

from models import League, Team, db

try:
    # If your project has a SleeperLeague model, we’ll use it; otherwise we’ll noop gracefully.
    from models import SleeperLeague
except Exception:  # pragma: no cover
    SleeperLeague = None

# ✅ Reuse the exact week logic already vetted in lineups/routes.py
from lineups.routes import (
    _pick_year_for_week_lookup,
    _effective_current_week,
    _allowed_weeks_from,
    _league_host,
    _cookie_header_for_host,
    MFL_MAX_WEEKS_FALLBACK,
)

bp = Blueprint("tools", __name__, url_prefix="/tools")

MFL_HTTP_TIMEOUT = 15


def _fmt_dt(dt):
    try:
        # Normalize to UTC for label consistency
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return None


def _max_timestamp_for(model, user_id, candidate_cols):
    """
    Safely compute MAX(timestamp) for any of the provided columns on a model.
    Returns a timezone-aware datetime in UTC or None.
    """
    if model is None:
        return None
    best = None
    for col in candidate_cols:
        if not hasattr(model, col):
            continue
        try:
            val = (
                db.session.query(func.max(getattr(model, col)))
                .filter(getattr(model, "user_id") == user_id)
                .scalar()
            )
        except Exception:
            val = None
        if val:
            # try to normalize to aware UTC
            try:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                else:
                    val = val.astimezone(timezone.utc)
            except Exception:
                pass
            best = max(best, val) if best else val
    return best


def _week_bundle_from_lineups() -> tuple[int, list[int]]:
    """
    Produce (selected_week, weeks_list) using the same helpers as lineups/routes.py:
      - _pick_year_for_week_lookup()
      - _effective_current_week(year)
      - _allowed_weeks_from(current_week, max_week)
    """
    year_for_lookup = _pick_year_for_week_lookup()
    current_week = _effective_current_week(year_for_lookup)
    try:
        max_week = int(current_app.config.get("MFL_MAX_WEEKS", MFL_MAX_WEEKS_FALLBACK))
    except (TypeError, ValueError):
        max_week = MFL_MAX_WEEKS_FALLBACK
    weeks = _allowed_weeks_from(current_week, max_week)
    return current_week, weeks


def _draft_pick_label(round_no: int, pick_no: int) -> str:
    return f"{round_no}.{pick_no:02d}"


def _draft_url_for_league(league: League) -> str | None:
    base = None
    try:
        base = league._league_base()  # convenience helper on model
    except Exception:
        base = None
    if not base or not league.year or not league.mfl_id:
        return None
    return f"{base}/{league.year}/options?L={league.mfl_id}&O=17"


def _fetch_on_the_clock_for_league(league: League) -> dict[str, Any]:
    host = _league_host(league) or "api.myfantasyleague.com"
    cookie = _cookie_header_for_host(host)
    url = f"https://{host}/{league.year}/export"
    params = {"TYPE": "draftResults", "L": str(league.mfl_id), "JSON": "0"}
    headers = {"Accept": "application/xml, text/xml;q=0.9, */*;q=0.8"}
    if cookie:
        headers["Cookie"] = cookie

    resp = requests.get(url, params=params, headers=headers, timeout=MFL_HTTP_TIMEOUT)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    draft_unit = root.find(".//draftUnit")
    static_url = draft_unit.get("static_url") if draft_unit is not None else None

    teams = {
        (t.mfl_id or "").strip(): (t.name or f"Franchise {t.mfl_id}")
        for t in (
            db.session.query(Team)
            .filter(Team.league_id == league.id)
            .all()
        )
    }

    picks: list[dict[str, Any]] = []
    total_made = 0
    for p in root.findall(".//draftPick"):
        round_raw = p.get("round") or p.get("roundNumber") or ""
        pick_raw = p.get("pick") or p.get("pickNumber") or ""
        franchise = (p.get("franchise") or p.get("franchise_id") or "").strip()
        player = (p.get("player") or "").strip()
        if player:
            total_made += 1
        try:
            round_no = int(round_raw)
            pick_no = int(pick_raw)
        except (TypeError, ValueError):
            continue
        picks.append(
            {
                "round": round_no,
                "pick": pick_no,
                "franchise_id": franchise,
                "team_name": teams.get(franchise, f"Franchise {franchise or '—'}"),
                "is_made": bool(player),
                "label": _draft_pick_label(round_no, pick_no),
            }
        )

    remaining = [x for x in picks if not x["is_made"]]
    upcoming = remaining[:3]
    my_franchise = (league.franchise_id or "").strip()
    my_queue_position = None
    for idx, up in enumerate(upcoming, start=1):
        if my_franchise and up["franchise_id"] == my_franchise:
            my_queue_position = idx
            break

    next_user_pick = None
    picks_away = None
    if my_franchise:
        for idx, up in enumerate(remaining):
            if up["franchise_id"] == my_franchise:
                next_user_pick = up
                picks_away = idx
                break

    if upcoming:
        status = "in_progress" if total_made > 0 else "not_started"
    else:
        status = "complete"

    return {
        "provider": "mfl",
        "league_id": league.id,
        "league_name": league.name or f"League {league.mfl_id}",
        "league_mfl_id": league.mfl_id,
        "league_year": league.year,
        "my_franchise_id": my_franchise or None,
        "my_queue_position": my_queue_position,
        "queue_rank": (picks_away + 1) if picks_away is not None else 999,
        "picks_away": picks_away,
        "next_user_pick": next_user_pick,
        "status": status,
        "total_picks_made": total_made,
        "upcoming_picks": upcoming,
        "draft_url": _draft_url_for_league(league) or static_url,
    }



def _select_sleeper_draft_id(league: SleeperLeague, drafts: list[dict[str, Any]], fallback_draft_id: str | None = None) -> str | None:
    """Pick the best Sleeper draft for on-the-clock data.

    Prefer drafts that are not complete, then season match, then latest start/created time.
    """
    if not drafts:
        return fallback_draft_id

    current_season = str(league.year or "").strip()
    fallback = (fallback_draft_id or "").strip()

    def _score(d: dict[str, Any]) -> tuple[int, int, int, int]:
        status = str(d.get("status") or "").strip().lower()
        season = str(d.get("season") or "").strip()
        is_not_complete = 1 if status and status != "complete" else 0
        season_match = 1 if current_season and season == current_season else 0
        ts = d.get("start_time") or d.get("created") or 0
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            ts_int = 0
        is_fallback = 1 if fallback and str(d.get("draft_id") or "").strip() == fallback else 0
        return (is_not_complete, season_match, is_fallback, ts_int)

    best = max(drafts, key=_score)
    best_id = str(best.get("draft_id") or "").strip()
    return best_id or fallback or None

def _sleeper_pick_owner_map(traded_picks: list[dict[str, Any]], season: int | None) -> dict[tuple[int, int], int]:
    """Map (round, original_roster_id) -> current_owner_roster_id for a season."""
    owner_map: dict[tuple[int, int], int] = {}
    target_season = str(season) if season is not None else ""

    for pick in traded_picks or []:
        pick_season = str(pick.get("season") or "").strip()
        if target_season and pick_season and pick_season != target_season:
            continue

        try:
            round_no = int(pick.get("round"))
        except (TypeError, ValueError):
            continue

        original_raw = (
            pick.get("original_roster_id")
            or pick.get("roster_id")
            or pick.get("original_owner_id")
            or pick.get("originalOwnerId")
            or pick.get("originalRosterId")
        )
        owner_raw = (
            pick.get("owner_id")
            or pick.get("ownerId")
            or pick.get("to")
            or pick.get("previous_owner_id")
        )

        try:
            original_roster_id = int(original_raw)
            current_owner_id = int(owner_raw)
        except (TypeError, ValueError):
            continue

        owner_map[(round_no, original_roster_id)] = current_owner_id

    return owner_map


def _fetch_on_the_clock_for_sleeper_league(league: SleeperLeague, sleeper_user_id: str | None) -> dict[str, Any]:
    if not league.sleeper_id:
        raise ValueError("Sleeper league is missing sleeper_id")

    drafts_url = f"https://api.sleeper.app/v1/league/{league.sleeper_id}/drafts"
    drafts_resp = requests.get(drafts_url, timeout=MFL_HTTP_TIMEOUT)
    drafts_resp.raise_for_status()
    drafts = drafts_resp.json() or []

    draft_id = _select_sleeper_draft_id(league, drafts, league.draft_id)
    if not draft_id:
        return {
            "provider": "sleeper",
            "league_id": league.id,
            "league_name": league.name or f"Sleeper League {league.sleeper_id}",
            "league_sleeper_id": league.sleeper_id,
            "league_year": league.year,
            "my_queue_position": None,
            "queue_rank": 999,
            "picks_away": None,
            "next_user_pick": None,
            "status": "no_draft",
            "total_picks_made": 0,
            "upcoming_picks": [],
            "draft_url": None,
        }

    draft_url = f"https://api.sleeper.app/v1/draft/{draft_id}"
    picks_url = f"https://api.sleeper.app/v1/draft/{draft_id}/picks"
    draft_resp = requests.get(draft_url, timeout=MFL_HTTP_TIMEOUT)
    draft_resp.raise_for_status()
    draft_data = draft_resp.json() or {}

    picks_resp = requests.get(picks_url, timeout=MFL_HTTP_TIMEOUT)
    picks_resp.raise_for_status()
    made_picks = picks_resp.json() or []

    traded_url = f"https://api.sleeper.app/v1/league/{league.sleeper_id}/traded_picks"
    traded_resp = requests.get(traded_url, timeout=MFL_HTTP_TIMEOUT)
    traded_resp.raise_for_status()
    traded_picks = traded_resp.json() or []

    total_teams = int((draft_data.get("settings") or {}).get("teams") or 0)
    total_rounds = int((draft_data.get("settings") or {}).get("rounds") or 0)
    total_slots = total_teams * total_rounds

    user_roster_ids: set[int] = set()
    if sleeper_user_id:
        user_roster_ids = {
            int(t.sleeper_roster_id)
            for t in league.teams
            if t.sleeper_roster_id is not None and (t.owner_user_id or "") == str(sleeper_user_id)
        }

    slot_to_roster = {int(k): v for k, v in (draft_data.get("slot_to_roster_id") or {}).items() if str(k).isdigit()}
    team_names = {int(t.sleeper_roster_id): (t.name or t.owner_name or f"Roster {t.sleeper_roster_id}") for t in league.teams}
    traded_owner_map = _sleeper_pick_owner_map(traded_picks, league.year)

    made_pick_count = len(made_picks)
    next_pick_no = made_pick_count + 1
    upcoming = []
    for pick_no in range(next_pick_no, min(total_slots, next_pick_no + 2) + 1):
        if total_teams <= 0:
            break
        round_no = ((pick_no - 1) // total_teams) + 1
        offset = (pick_no - 1) % total_teams
        draft_slot = offset + 1 if round_no % 2 == 1 else (total_teams - offset)
        original_roster_id = slot_to_roster.get(draft_slot)
        try:
            original_roster_id_int = int(original_roster_id) if original_roster_id is not None else None
        except (TypeError, ValueError):
            original_roster_id_int = None
        roster_id = traded_owner_map.get((round_no, original_roster_id_int)) if original_roster_id_int is not None else None
        if roster_id is None:
            roster_id = original_roster_id_int
        try:
            roster_id_int = int(roster_id) if roster_id is not None else None
        except (TypeError, ValueError):
            roster_id_int = None
        upcoming.append({
            "round": round_no,
            "pick": offset + 1,
            "franchise_id": str(roster_id_int) if roster_id_int is not None else None,
            "team_name": team_names.get(roster_id_int, f"Roster {roster_id_int or '—'}"),
            "is_made": False,
            "label": _draft_pick_label(round_no, offset + 1),
            "draft_slot": draft_slot,
        })

    my_queue_position = None
    picks_away = None
    next_user_pick = None
    if user_roster_ids and total_teams > 0 and total_slots > 0:
        for idx, pick_no in enumerate(range(next_pick_no, total_slots + 1)):
            round_no = ((pick_no - 1) // total_teams) + 1
            offset = (pick_no - 1) % total_teams
            draft_slot = offset + 1 if round_no % 2 == 1 else (total_teams - offset)
            original_roster = slot_to_roster.get(draft_slot)
            try:
                original_roster_int = int(original_roster) if original_roster is not None else None
            except (TypeError, ValueError):
                original_roster_int = None
            effective_owner = traded_owner_map.get((round_no, original_roster_int)) if original_roster_int is not None else None
            if effective_owner is None:
                effective_owner = original_roster_int
            if effective_owner in user_roster_ids:
                picks_away = idx
                next_user_pick = {
                    "round": round_no,
                    "pick": offset + 1,
                    "label": _draft_pick_label(round_no, offset + 1),
                }
                break

        for idx, up in enumerate(upcoming, start=1):
            try:
                up_roster = int(up.get("franchise_id")) if up.get("franchise_id") is not None else None
            except (TypeError, ValueError):
                up_roster = None
            if up_roster in user_roster_ids:
                my_queue_position = idx
                break

    status = draft_data.get("status") or ("complete" if made_pick_count >= total_slots and total_slots > 0 else "in_progress")
    return {
        "provider": "sleeper",
        "league_id": league.id,
        "league_name": league.name or f"Sleeper League {league.sleeper_id}",
        "league_sleeper_id": league.sleeper_id,
        "league_year": league.year,
        "my_queue_position": my_queue_position,
        "queue_rank": (picks_away + 1) if picks_away is not None else 999,
        "picks_away": picks_away,
        "next_user_pick": next_user_pick,
        "status": status,
        "total_picks_made": made_pick_count,
        "upcoming_picks": upcoming,
        "draft_url": f"https://sleeper.com/draft/nfl/{draft_id}",
    }



@bp.route("/")
@login_required
def index():
    user_id = current_user.id

    # ----- Has MFL? (League table is MFL)
    try:
        has_mfl = (
            db.session.query(League.id)
            .filter(League.user_id == user_id)
            .limit(1)
            .count()
            > 0
        )
    except Exception:
        has_mfl = False

    # ----- Build MFL list for client-side "Refresh Assets" (id/name/year/mfl_id)
    mfl_leagues: List[Dict[str, Any]] = []
    try:
        leagues = (
            db.session.query(League)
            .filter(League.user_id == user_id)
            .order_by(League.year.desc(), League.name.asc())
            .all()
        )
        for lg in leagues:
            mfl_leagues.append(
                dict(
                    db_id=lg.id,
                    mfl_id=getattr(lg, "mfl_id", None) or getattr(lg, "external_id", None) or "",
                    year=getattr(lg, "year", None),
                    name=getattr(lg, "name", None) or "League",
                )
            )
    except Exception:
        pass

    # ----- Last sync across BOTH sources (MFL + Sleeper)
    mfl_last = _max_timestamp_for(
        League,
        user_id,
        ["synced_at", "last_synced_at", "assets_synced_at", "updated_at"],
    )
    slpr_last = _max_timestamp_for(
        SleeperLeague,
        user_id,
        ["synced_at", "last_synced_at", "assets_synced_at", "updated_at"],
    )

    # Pick the freshest timestamp of the two; show a dash if none
    last_sync = mfl_last if (mfl_last and (not slpr_last or mfl_last >= slpr_last)) else slpr_last
    last_sync_label = _fmt_dt(last_sync) if last_sync else "—"

    # Refresh required if older than 4 hours (or never synced)
    refresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
    refresh_required = (not last_sync) or (last_sync < refresh_cutoff)

    # ----- Week dropdown (current and forward only) — pulled from lineups helpers
    selected_week, weeks = _week_bundle_from_lineups()

    return render_template(
        "tools/index.html",
        has_mfl_leagues=has_mfl,
        page_visible=bool(current_app.config.get("TOOLS_PAGE_VISIBLE", False)),
        last_sync_label=last_sync_label,
        refresh_required=refresh_required,
        weeks=weeks,
        selected_week=selected_week,
        mfl_leagues=mfl_leagues,
    )


@bp.route("/on-the-clock")
@login_required
def on_the_clock():
    leagues = (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )
    mfl_leagues = [
        {
            "db_id": lg.id,
            "provider": "mfl",
            "mfl_id": lg.mfl_id,
            "year": lg.year,
            "name": lg.name or f"League {lg.mfl_id}",
        }
        for lg in leagues
    ]
    sleeper_leagues = []
    if SleeperLeague is not None:
        sleeper_rows = (
            db.session.query(SleeperLeague)
            .filter(SleeperLeague.user_id == current_user.id)
            .order_by(SleeperLeague.year.desc(), SleeperLeague.name.asc())
            .all()
        )
        sleeper_leagues = [
            {
                "db_id": lg.id,
                "provider": "sleeper",
                "sleeper_id": lg.sleeper_id,
                "year": lg.year,
                "name": lg.name or f"Sleeper League {lg.sleeper_id}",
            }
            for lg in sleeper_rows
        ]

    return render_template("tools/on_the_clock.html", leagues=(mfl_leagues + sleeper_leagues))


@bp.route("/on-the-clock/league/<int:league_id>", methods=["GET"])
@login_required
def on_the_clock_league(league_id: int):
    league = (
        db.session.query(League)
        .filter(League.id == league_id, League.user_id == current_user.id)
        .first()
    )
    provider = "mfl"
    if not league and SleeperLeague is not None:
        league = (
            db.session.query(SleeperLeague)
            .filter(SleeperLeague.id == league_id, SleeperLeague.user_id == current_user.id)
            .first()
        )
        provider = "sleeper" if league else provider

    if not league:
        return jsonify({"ok": False, "error": "League not found."}), 404

    try:
        if provider == "sleeper":
            payload = _fetch_on_the_clock_for_sleeper_league(league, current_user.sleeper_user_id)
        else:
            payload = _fetch_on_the_clock_for_league(league)
        return jsonify({"ok": True, "league": payload})
    except requests.RequestException as exc:
        current_app.logger.warning("on_the_clock network error provider=%s league=%s: %s", provider, league_id, exc)
        return jsonify({"ok": False, "error": f"{provider.upper()} request failed for this league."}), 502
    except ET.ParseError:
        return jsonify({"ok": False, "error": "MFL returned an invalid draft response."}), 502
    except Exception as exc:
        current_app.logger.exception("on_the_clock error provider=%s league=%s: %s", provider, league_id, exc)
        return jsonify({"ok": False, "error": "Unable to load draft data for this league."}), 500