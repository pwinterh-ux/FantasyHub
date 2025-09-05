# mfl/routes.py
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user

from app import db
from models import League, Team, Roster, DraftPick
from services.mfl_client import MFLClient
from services.mfl_parsers import (
    parse_user_leagues,
    parse_assets,
    parse_standings,
    parse_league_info,     # -> (franchise_meta_map, roster_slots_text, league_base_url)
    parse_rosters,         # fallback players-only
    parse_future_picks,    # fallback picks-only
)
from services.mfl_sync import (
    sync_league_info,
    sync_league_assets,
    sync_league_standings,
)

mfl_bp = Blueprint("mfl", __name__, url_prefix="/mfl")


def _require_mfl_cookie():
    if not getattr(current_user, "session_key", None):
        flash("Your MFL session has expired. Please sign in again.", "warning")
        return redirect(url_for("mfl.mfl_login"))
    return None


def _norm_fid(val) -> str | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    return s.zfill(4)


# --------------------------- Link / Login -----------------------------------

@mfl_bp.route("/login", methods=["GET", "POST"])
@login_required
def mfl_login():
    default_year = datetime.utcnow().year
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            year = int(request.form.get("year", default_year))
        except ValueError:
            year = default_year

        try:
            client = MFLClient(year=year)  # API host
            cookie = client.login(username, password)
        except Exception as e:
            flash(f"MFL login failed: {e}", "danger")
            return render_template("mfl_login.html", default_year=year)

        current_user.mfl_user = username
        current_user.session_key = cookie
        db.session.commit()

        flash("MFL linked successfully.", "success")
        return redirect(url_for("mfl.mfl_config", year=year))

    return render_template("mfl_login.html", default_year=default_year)


# --------------------------- Config / Select Leagues ------------------------

@mfl_bp.route("/config", methods=["GET"])
@login_required
def mfl_config():
    miss = _require_mfl_cookie()
    if miss:
        return miss

    try:
        year = int(request.args.get("year", datetime.utcnow().year))
    except ValueError:
        year = datetime.utcnow().year

    api_client = MFLClient(year=year)  # API host

    try:
        xml = api_client.get_user_leagues(current_user.session_key)
        raw_found = parse_user_leagues(xml)
    except Exception as e:
        flash(f"Could not fetch leagues from MFL: {e}", "danger")
        return redirect(url_for("mfl.mfl_login"))

    # Normalize parse_user_leagues into dicts with lid/name/year/fid
    found = []
    for rec in raw_found:
        lid = name = None
        fid = None
        yr = year

        if isinstance(rec, dict):
            lid = str(rec.get("league_id") or rec.get("id") or "").strip()
            name = (rec.get("name") or (f"League {lid}" if lid else "")).strip()
            yr_val = rec.get("year")
            try:
                if yr_val not in (None, ""):
                    yr = int(yr_val)
            except Exception:
                yr = year
            fid_val = rec.get("franchise_id") or rec.get("franchiseId")
            fid = _norm_fid(fid_val)
        else:
            try:
                parts = list(rec)
            except Exception:
                parts = []
            if len(parts) >= 1:
                lid = str(parts[0]).strip()
            if len(parts) >= 2:
                name = str(parts[1]).strip()
            if len(parts) >= 3:
                try:
                    yr = int(parts[2])
                except Exception:
                    yr = year
            if len(parts) >= 4 and parts[3] not in (None, ""):
                fid = _norm_fid(parts[3])

        if not lid or not name:
            continue

        found.append({"lid": lid, "name": name, "year": yr, "fid": fid})

    existing = {
        (lg.mfl_id, lg.year)
        for lg in League.query.filter_by(user_id=current_user.id, year=year).all()
    }

    leagues = []
    for item in found:
        if item["year"] == year or item["year"] == 0:
            lid = item["lid"]
            yr = item["year"]
            leagues.append({
                "id": lid,
                "name": item["name"],
                "year": yr,
                "franchise_id": item["fid"],  # optional in template
                "checked": (lid, yr) in existing,
            })

    return render_template("mfl_config.html", leagues=leagues, year=year)


@mfl_bp.route("/config", methods=["POST"])
@login_required
def mfl_config_submit():
    """
    Apply selection, then sync:
      - Upsert/delete leagues per checkbox selection
      - Persist user's franchise_id per league (from config form)
      - For each, load league info + assets + standings
      - Fallback to 'rosters' + 'futureDraftPicks' when 'assets' is blocked/empty
    """
    miss = _require_mfl_cookie()
    if miss:
        return miss

    try:
        year = int(request.form.get("year", datetime.utcnow().year))
    except ValueError:
        year = datetime.utcnow().year

    cookie = current_user.session_key

    # Selected league IDs
    selected_ids = set(request.form.getlist("league_id"))

    # Maps for names and franchise ids coming from the form
    name_map: dict[str, str] = {}
    fid_map: dict[str, str | None] = {}
    for key, val in request.form.items():
        if key.startswith("league_name_"):
            lid = key.replace("league_name_", "", 1)
            name_map[lid] = val
        elif key.startswith("franchise_id_"):
            lid = key.replace("franchise_id_", "", 1)
            fid_map[lid] = _norm_fid(val)

    existing = League.query.filter_by(user_id=current_user.id, year=year).all()
    existing_ids = {lg.mfl_id for lg in existing}

    to_delete = [lg for lg in existing if lg.mfl_id not in selected_ids]
    to_add = [lid for lid in selected_ids if lid not in existing_ids]
    to_resync = [lg for lg in existing if lg.mfl_id in selected_ids]

    # Delete children first (DBs without cascade can complain)
    for lg in to_delete:
        try:
            # collect team ids for this league
            team_ids = [tid for (tid,) in db.session.query(Team.id).filter(Team.league_id == lg.id).all()]

            if team_ids:
                Roster.query.filter(Roster.team_id.in_(team_ids)).delete(synchronize_session=False)
                DraftPick.query.filter(DraftPick.team_id.in_(team_ids)).delete(synchronize_session=False)

            Team.query.filter(Team.league_id == lg.id).delete(synchronize_session=False)
            db.session.delete(lg)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.info("delete cascade failed for L=%s: %s", lg.mfl_id, e)
            flash(f"Delete failed for league {lg.mfl_id}: {e}", "danger")

    created_leagues: list[League] = []
    for lid in to_add:
        league = League(
            user_id=current_user.id,
            mfl_id=lid,
            name=name_map.get(lid, f"League {lid}"),
            year=year,
            synced_at=None,
            franchise_id=fid_map.get(lid),  # persist user's franchise id
        )
        db.session.add(league)
        db.session.flush()
        current_app.logger.info("created league %s (year=%s) with franchise_id=%s", lid, year, league.franchise_id)
        created_leagues.append(league)
    db.session.commit()

    # Update franchise_id for existing selected leagues too (user might have changed it)
    for lg in to_resync:
        new_fid = fid_map.get(lg.mfl_id)
        if new_fid and new_fid != lg.franchise_id:
            current_app.logger.info("updating league %s franchise_id: %s -> %s", lg.mfl_id, lg.franchise_id, new_fid)
            lg.franchise_id = new_fid
    db.session.commit()

    # Targets to sync
    targets = to_resync + created_leagues

    # Base API client (used to discover league host)
    api_client = MFLClient(year=year)

    leagues_synced = 0
    teams_total = 0
    roster_rows_total = 0
    picks_total = 0

    for lg in targets:
        # 1) League info: discover baseURL and franchise meta from whichever host works
        try:
            info_xml = api_client.get_league_info(lg.mfl_id, cookie)
        except Exception:
            info_xml = None

        league_host = None
        try:
            franchise_meta, roster_text, league_host = parse_league_info(info_xml) if info_xml else ({}, None, None)
        except Exception as e:
            current_app.logger.info("parse_league_info failed for L=%s: %s", lg.mfl_id, e)
            franchise_meta, roster_text, league_host = {}, None, None

        # Prefer the league host for all league-scoped data (avoids cross-domain auth headaches)
        data_client = api_client
        if league_host:
            data_client = MFLClient(year=year, base_url=f"{league_host}/{year}/")

        # 2) Upsert franchise names/owners + roster slots
        try:
            sync_league_info(lg, franchise_meta, roster_slots=roster_text)
        except Exception as e:
            current_app.logger.info("sync_league_info error for L=%s: %s", lg.mfl_id, e)

        # 3) Assets & Standings (with fallback)
        try:
            # --- Primary attempt: assets on league host
            assets_xml = data_client.get_assets(lg.mfl_id, cookie)
            assets = parse_assets(assets_xml)

            # Detect "blocked/empty" assets: explicit <error>, or zero totals
            blocked = (assets_xml or b"").strip().lower().startswith(b"<error")
            if not blocked:
                total_players = sum(len(a.player_ids) for a in assets)
                total_picks = sum(len(a.future_picks) for a in assets)
                blocked = (total_players == 0 and total_picks == 0)

            # --- Fallback: rosters + futureDraftPicks
            if blocked:
                current_app.logger.info("assets blocked/empty for L=%s; using fallbacks", lg.mfl_id)

                # Players via 'rosters'
                try:
                    rosters_xml = data_client.get_rosters(lg.mfl_id, cookie)
                    roster_assets = parse_rosters(rosters_xml)  # players only
                except Exception as e_ro:
                    current_app.logger.info("fallback rosters failed for L=%s: %s", lg.mfl_id, e_ro)
                    roster_assets = []

                # Picks via 'futureDraftPicks' if available
                picks_map = {}
                try:
                    # Not all client versions have this; wrap to be safe
                    if hasattr(data_client, "get_future_picks"):
                        picks_xml = data_client.get_future_picks(lg.mfl_id, cookie)
                        picks_map = parse_future_picks(picks_xml)  # {fid: [(season, rnd, orig), ...]}
                except Exception as e_fp:
                    current_app.logger.info("fallback futureDraftPicks failed for L=%s: %s", lg.mfl_id, e_fp)
                    picks_map = {}

                # Merge players+picks into FranchiseAssets list
                if roster_assets:
                    merged = []
                    by_fid = {fa.franchise_id: fa for fa in roster_assets}
                    for fid, fa in by_fid.items():
                        fa.future_picks = picks_map.get(fid, [])
                        merged.append(fa)
                    assets = merged
                else:
                    assets = []  # nothing usable

            # Standings (league host)
            standings_xml = data_client.get_standings(lg.mfl_id, cookie)

            # --- Write to DB
            metrics = sync_league_assets(lg, assets)
            updated = sync_league_standings(lg, parse_standings(standings_xml))

            lg.synced_at = datetime.utcnow()
            db.session.commit()

            leagues_synced += 1
            teams_total += metrics.get("teams_touched", 0)
            roster_rows_total += metrics.get("rosters_inserted", 0)
            picks_total += metrics.get("picks_inserted", 0)

            current_app.logger.info(
                "synced L=%s: fid=%s teams=%s roster_rows=%s picks=%s standings_updated=%s",
                lg.mfl_id,
                lg.franchise_id,
                metrics.get("teams_touched", 0),
                metrics.get("rosters_inserted", 0),
                metrics.get("picks_inserted", 0),
                updated,
            )
        except Exception as e:
            db.session.rollback()
            current_app.logger.info("sync failed for L=%s: %s", lg.mfl_id, e)
            flash(f"Sync failed for league {lg.mfl_id}: {e}", "danger")

    # Consolidated banner (brief)
    flash(
        f"Synced {leagues_synced} leagues • {teams_total} teams • "
        f"{roster_rows_total} roster rows • {picks_total} draft picks",
        "success",
    )
    return redirect(url_for("leagues.my_leagues"))
