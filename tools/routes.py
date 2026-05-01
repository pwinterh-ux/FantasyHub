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
            "mfl_id": lg.mfl_id,
            "year": lg.year,
            "name": lg.name or f"League {lg.mfl_id}",
        }
        for lg in leagues
    ]
    return render_template("tools/on_the_clock.html", leagues=mfl_leagues)


@bp.route("/on-the-clock/league/<int:league_id>", methods=["GET"])
@login_required
def on_the_clock_league(league_id: int):
    league = (
        db.session.query(League)
        .filter(League.id == league_id, League.user_id == current_user.id)
        .first()
    )
    if not league:
        return jsonify({"ok": False, "error": "League not found."}), 404

    try:
        payload = _fetch_on_the_clock_for_league(league)
        return jsonify({"ok": True, "league": payload})
    except requests.RequestException as exc:
        current_app.logger.warning("on_the_clock network error league=%s: %s", league_id, exc)
        return jsonify({"ok": False, "error": "MFL request failed for this league."}), 502
    except ET.ParseError:
        return jsonify({"ok": False, "error": "MFL returned an invalid draft response."}), 502
    except Exception as exc:
        current_app.logger.exception("on_the_clock error league=%s: %s", league_id, exc)
        return jsonify({"ok": False, "error": "Unable to load draft data for this league."}), 500