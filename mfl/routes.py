# mfl/routes.py
from __future__ import annotations

from datetime import datetime, timezone
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify
from flask_login import login_required, current_user

from app import db
from models import League, Team, Roster, DraftPick, Player
from services.mfl_client import MFLClient
from services.mfl_parsers import (
    parse_user_leagues,
    parse_assets,
    parse_standings,
    parse_league_info,           # (franchise_meta_map, roster_slots_text, league_base_url)
    parse_rosters_fallback,      # used when assets is blocked
    parse_future_picks_fallback, # used when assets is blocked
    parse_pending_trades,        # parses export?TYPE=pendingTrades (open trades only)
)
from services.mfl_sync import (
    sync_league_info,
    sync_league_assets,
    sync_league_standings,
)

# ---- NEW: lightweight per-host threading utilities
from concurrent.futures import ThreadPoolExecutor, as_completed

mfl_bp = Blueprint("mfl", __name__, url_prefix="/mfl")

# --------------------------- lightweight in-proc cache -----------------------
# key: (user_id:int, year:int) -> {"ts": float, "data": dict}
_TRADES_CACHE: dict[tuple[int, int], dict] = {}
_TRADES_CACHE_TTL_SEC = 15 * 60


def _cache_get(user_id: int, year: int) -> tuple[dict | None, float]:
    key = (user_id, year)
    item = _TRADES_CACHE.get(key)
    if not item:
        return None, 0.0
    age = time.time() - float(item.get("ts", 0))
    if age > _TRADES_CACHE_TTL_SEC:
        return None, age
    return item.get("data"), age


def _cache_set(user_id: int, year: int, data: dict) -> None:
    _TRADES_CACHE[(user_id, year)] = {"ts": time.time(), "data": data}


def _require_mfl_cookie():
    """
    Prefer new cookie fields; fall back to the legacy session_key.
    """
    if not (getattr(current_user, "mfl_cookie_api", None) or getattr(current_user, "session_key", None)):
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


def _host_only(url: str | None) -> str | None:
    """
    Normalize a baseURL like 'https://www43.myfantasyleague.com' to 'www43.myfantasyleague.com'.
    Accepts already-host strings and returns them unchanged.
    """
    if not url:
        return None
    try:
        u = urlparse(url)
        if u.netloc:
            return u.netloc
        # handle cases where url is already just a host
        return url.replace("https://", "").replace("http://", "").split("/", 1)[0]
    except Exception:
        return None

# --- entry point to the Offers flow from the Trades home ---
@mfl_bp.route("/offers", methods=["GET"])
@login_required
def offers_entry():
    # keep the nav under the mfl/ namespace, but land in the offers app
    return redirect(url_for("offers.search"))

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

        # 1) Login on API host
        try:
            api_client = MFLClient(year=year)  # https://api.myfantasyleague.com/{year}/
            api_cookie = api_client.login(username, password)
        except Exception as e:
            flash(f"MFL login failed: {e}", "danger")
            return render_template("mfl_login.html", default_year=year)

        # 2) Discover league hosts from myleagues (API host; OK with API cookie)
        hostnames: set[str] = set()
        try:
            xml = api_client.get_user_leagues(api_cookie)
            root = ET.fromstring(xml)
            for lg in root.findall(".//league"):
                url_attr = lg.get("url") or ""
                if url_attr:
                    host = _host_only(url_attr)
                    if host:
                        hostnames.add(host)
        except Exception as e:
            current_app.logger.info("myleagues discovery failed during login: %s", e)

        # 3) Obtain cookies per league host (best-effort)
        host_cookie_map: dict[str, str] = {}
        for host in sorted(hostnames):
            try:
                # league-scoped base like https://www43.myfantasyleague.com/{year}/
                host_client = MFLClient(year=year, base_url=f"https://{host}/{year}/")
                host_cookie = host_client.login(username, password)
                if host_cookie:
                    host_cookie_map[host] = host_cookie
            except Exception as e:
                current_app.logger.info("host login failed for %s: %s", host, e)
                continue

        # 4) Store everything on the user
        current_user.mfl_user = username
        # Keep legacy session_key as a fallback (use API cookie)
        current_user.session_key = api_cookie
        # New fields: API cookie + per-host cookies + timestamp
        try:
            current_user.set_mfl_cookie_bundle(api_cookie=api_cookie, host_cookie_map=host_cookie_map)
        except Exception:
            # Fallback if helper not available
            current_user.mfl_cookie_api = api_cookie
            current_user.mfl_cookie_updated_at = datetime.utcnow()
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
    api_cookie = getattr(current_user, "mfl_cookie_api", None) or getattr(current_user, "session_key", None)

    try:
        xml = api_client.get_user_leagues(api_cookie)
        raw_found = parse_user_leagues(xml)
    except Exception as e:
        flash(f"Could not fetch leagues from MFL: {e}", "danger")
        return redirect(url_for("mfl.mfl_login"))

    # Build a {league_id -> host} map from myleagues (new fields in parser)
    host_by_lid: dict[str, str] = {}
    for rec in raw_found:
        if isinstance(rec, dict):
            lid = str(rec.get("league_id") or rec.get("id") or "").strip()
            host = rec.get("host")
            if lid and host:
                host_by_lid[lid] = host

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

    # Opportunistically stamp league_host on existing rows if missing
    try:
        existing_rows = League.query.filter_by(user_id=current_user.id, year=year).all()
        changed = False
        for row in existing_rows:
            if not getattr(row, "league_host", None):
                host = host_by_lid.get(row.mfl_id)
                if host:
                    row.league_host = host
                    changed = True
        if changed:
            db.session.commit()
    except Exception as e:
        current_app.logger.info("could not opportunistically set league_host from myleagues: %s", e)

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

    UPDATED: Network fetches are grouped by host and run with one worker per host.
    DB writes (sync_* fns) occur on the main thread to keep SQLAlchemy session safe.
    """
    miss = _require_mfl_cookie()
    if miss:
        return miss

    try:
        year = int(request.form.get("year", datetime.utcnow().year))
    except ValueError:
        year = datetime.utcnow().year

    # Prefer new API cookie; fall back to legacy session_key
    api_cookie = getattr(current_user, "mfl_cookie_api", None) or getattr(current_user, "session_key", None)
    host_cookies = current_user.get_mfl_host_cookies() if hasattr(current_user, "get_mfl_host_cookies") else {}

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

    # Delete children first (safe across MySQL/SQLite/Postgres)
    for lg in to_delete:
        try:
            # 1) collect team ids for this league (avoid JOIN deletes)
            team_ids = [tid for (tid,) in db.session.query(Team.id)
                        .filter(Team.league_id == lg.id).all()]

            # 2) delete rows that depend on teams
            if team_ids:
                Roster.query.filter(Roster.team_id.in_(team_ids)).delete(synchronize_session=False)
                DraftPick.query.filter(DraftPick.team_id.in_(team_ids)).delete(synchronize_session=False)
                Team.query.filter(Team.id.in_(team_ids)).delete(synchronize_session=False)

            # 3) finally delete the league
            db.session.delete(lg)
            db.session.flush()
        except Exception as e:
            db.session.rollback
            current_app.logger.exception("Failed deleting league %s: %s", lg.mfl_id, e)
            flash(f"Failed deleting league {lg.mfl_id}: {e}", "danger")
            continue

    db.session.commit()

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
    if not targets:
        flash("No leagues selected.", "warning")
        return redirect(url_for("mfl.mfl_config", year=year))

    # Base API client (used to discover league host, assets/standings fallback)
    api_client = MFLClient(year=year)

    # --- build a host map like trades does (once) ---
    host_by_lid: dict[str, str] = {}
    try:
        xml = api_client.get_user_leagues(api_cookie)
        for rec in parse_user_leagues(xml):
            if isinstance(rec, dict):
                lid = str(rec.get("league_id") or rec.get("id") or "").strip()
                host = rec.get("host")
                if lid and host:
                    host_by_lid[lid] = host
    except Exception as e:
        current_app.logger.info("could not build myleagues host map for config sync: %s", e)

    # small helper to ensure the cookie we send carries MFL_USER_ID
    def _append_user_id_cookie(base_cookie: str | None, api_cookie_val: str | None) -> str | None:
        if not api_cookie_val:
            return base_cookie
        try:
            uid = MFLClient._extract_user_id(api_cookie_val)
        except Exception:
            uid = None
        if not uid:
            return base_cookie
        base_cookie = base_cookie or ""
        if "MFL_USER_ID=" not in base_cookie:
            base_cookie = (base_cookie + "; " if base_cookie else "") + f"MFL_USER_ID={uid}"
        return base_cookie

    # -------------------- FETCH PHASE (per-host threads) ---------------------
    # Prepare per-league immutable fetch spec (avoid ORM objects in threads)
    fetch_specs: list[dict] = []
    league_by_lid: dict[str, League] = {lg.mfl_id: lg for lg in targets}
    for lg in targets:
        spec = {
            "lid": lg.mfl_id,
            "year": year,
            "franchise_id": lg.franchise_id,
            "prefer_host": getattr(lg, "league_host", None) or host_by_lid.get(lg.mfl_id),
        }
        fetch_specs.append(spec)

    # Group by host key (use 'api.myfantasyleague.com' when unknown)
    groups: dict[str, list[dict]] = {}
    for spec in fetch_specs:
        host = spec["prefer_host"] or "api.myfantasyleague.com"
        groups.setdefault(host, []).append(spec)

    # Make thread-safe clients/cookies per host group (strings only)
    clients_by_host: dict[str, MFLClient] = {}
    cookie_by_host: dict[str, str | None] = {}
    for host in groups.keys():
        if host == "api.myfantasyleague.com":
            clients_by_host[host] = api_client
            cookie_by_host[host] = _append_user_id_cookie(api_cookie, api_cookie)
        else:
            clients_by_host[host] = MFLClient(year=year, base_url=f"https://{host}/{year}/")
            cookie_by_host[host] = _append_user_id_cookie(host_cookies.get(host), api_cookie)

    # Worker: fetch info/assets/standings (no DB access here!)
    def _worker(host_key: str, specs: list[dict]) -> list[dict]:
        results: list[dict] = []
        data_client = clients_by_host[host_key]
        data_cookie = cookie_by_host[host_key]
        for spec in specs:
            lid = spec["lid"]
            out: dict = {"lid": lid, "host_used": host_key, "errors": [], "fallback_used": False}
            try:
                # 1) league info from API host (consistent source)
                info_xml = api_client.get_league_info(lid, api_cookie)
                try:
                    franchise_meta, roster_text, league_base_url = parse_league_info(info_xml) if info_xml else ({}, None, None)
                except Exception as e:
                    franchise_meta, roster_text, league_base_url = {}, None, None
                    out["errors"].append(f"parse_league_info:{e}")
                out["franchise_meta"] = franchise_meta
                out["roster_text"] = roster_text
                out["resolved_host"] = spec["prefer_host"] or _host_only(league_base_url) or host_by_lid.get(lid) or host_key

                # 2) assets (host first, fallback to API if blocked)
                assets_xml = data_client.get_assets(lid, data_cookie)
                want_api_fallback = bool(assets_xml and b"<error" in assets_xml and b"API requires logged in user" in assets_xml)
                if want_api_fallback:
                    assets_xml = api_client.get_assets(lid, api_cookie)
                use_fallbacks = bool(assets_xml and b"<error" in assets_xml and b"API requires logged in user" in assets_xml)
                if use_fallbacks:
                    out["fallback_used"] = True
                    rosters_xml = data_client.get_rosters(lid, data_cookie)
                    try:
                        picks_xml = data_client.get_future_picks(lid, data_cookie)
                    except Exception:
                        picks_xml = None
                    assets = parse_rosters_fallback(rosters_xml, picks_xml)
                else:
                    assets = parse_assets(assets_xml)
                out["assets"] = assets

                # 3) standings (host first, fallback to API if blocked)
                standings_xml = data_client.get_standings(lid, data_cookie)
                if standings_xml and b"<error" in standings_xml and b"API requires logged in user" in standings_xml:
                    standings_xml = api_client.get_standings(lid, api_cookie)
                out["standings"] = parse_standings(standings_xml)
            except Exception as e:
                out["errors"].append(str(e))
            results.append(out)
        return results

    # Execute per-host workers
    fetched: dict[str, dict] = {}  # lid -> result bundle
    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        futures = [ex.submit(_worker, host, specs) for host, specs in groups.items()]
        for fut in as_completed(futures):
            try:
                for bundle in fut.result():
                    fetched[bundle["lid"]] = bundle
            except Exception as e:
                current_app.logger.info("fetch worker failed: %s", e)

    # -------------------- SYNC PHASE (main thread DB writes) -----------------
    leagues_synced = 0
    teams_total = 0
    roster_rows_total = 0
    picks_total = 0
    any_fallback = False

    for lg in targets:
        bundle = fetched.get(lg.mfl_id)
        if not bundle:
            flash(f"Sync skipped for league {lg.mfl_id} (no data fetched).", "warning")
            continue

        try:
            # Stamp host/home if changed
            resolved_host = bundle.get("resolved_host")
            if resolved_host and getattr(lg, "league_host", None) != resolved_host:
                lg.league_host = resolved_host
            if hasattr(lg, "home_url"):
                lg.home_url = lg.url_for_league_home()
            db.session.commit()
        except Exception as e:
            current_app.logger.info("could not stamp league_host/home_url for L=%s: %s", lg.mfl_id, e)

        # Sync info
        try:
            sync_league_info(lg, bundle.get("franchise_meta") or {}, roster_slots=bundle.get("roster_text"))
        except Exception as e:
            current_app.logger.info("sync_league_info error for L=%s: %s", lg.mfl_id, e)

        # Sync assets + standings
        try:
            metrics = sync_league_assets(lg, bundle.get("assets") or {})
            updated = sync_league_standings(lg, bundle.get("standings") or {})

            lg.synced_at = datetime.utcnow()
            db.session.commit()

            leagues_synced += 1
            teams_total += metrics.get("teams_touched", 0)
            roster_rows_total += metrics.get("rosters_inserted", 0)
            picks_total += metrics.get("picks_inserted", 0)
            any_fallback = any_fallback or bool(bundle.get("fallback_used"))

            if bundle.get("errors"):
                current_app.logger.info("sync L=%s completed with warnings: %s", lg.mfl_id, "; ".join(bundle["errors"]))
        except Exception as e:
            db.session.rollback()
            current_app.logger.info("sync failed for L=%s: %s", lg.mfl_id, e)
            flash(f"Sync failed for league {lg.mfl_id}: {e}", "danger")

    if any_fallback:
        flash("Some league data required a per-league login; used roster/picks fallbacks for one or more leagues.", "warning")

    # Consolidated banner (brief)
    flash(
        f"Synced {leagues_synced} leagues • {teams_total} teams • "
        f"{roster_rows_total} roster rows • {picks_total} draft picks",
        "success",
    )
    return redirect(url_for("leagues.my_leagues"))


# --------------------------- Trades: shared fetcher --------------------------

def _gather_open_trades(year: int) -> dict:
    """
    Core fetcher: returns the same shape the JSON endpoint exposes.
    Uses league-host cookie when available, else falls back to API host.

    UPDATED: per-host concurrency (one worker per distinct host). Threads do
    network + parsing only; final packaging is assembled in the main thread.
    """
    # Cookies
    api_cookie = getattr(current_user, "mfl_cookie_api", None) or getattr(current_user, "session_key", None)
    host_cookies = current_user.get_mfl_host_cookies() if hasattr(current_user, "get_mfl_host_cookies") else {}

    # Client pinned to API host (we'll swap to league host when we can)
    api_client = MFLClient(year=year)

    # Build a fresh map of league_id -> host from myleagues (helps when league_host isn't stamped yet)
    host_by_lid: dict[str, str] = {}
    try:
        xml = api_client.get_user_leagues(api_cookie)
        for rec in parse_user_leagues(xml):
            if isinstance(rec, dict):
                lid = str(rec.get("league_id") or rec.get("id") or "").strip()
                host = rec.get("host")
                if lid and host:
                    host_by_lid[lid] = host
    except Exception as e:
        current_app.logger.info("could not build myleagues host map for trades sync: %s", e)

    # All leagues for this user/season
    leagues = League.query.filter_by(user_id=current_user.id, year=year).all()

    # Group by resolved host
    groups: dict[str, list[str]] = {}
    for lg in leagues:
        host = getattr(lg, "league_host", None) or host_by_lid.get(lg.mfl_id) or "api.myfantasyleague.com"
        groups.setdefault(host, []).append(lg.mfl_id)

    # Prepare clients/cookies per host
    clients_by_host: dict[str, MFLClient] = {}
    cookie_by_host: dict[str, str | None] = {}
    for host in groups.keys():
        if host == "api.myfantasyleague.com":
            clients_by_host[host] = api_client
            cookie_by_host[host] = api_cookie
        else:
            clients_by_host[host] = MFLClient(year=year, base_url=f"https://{host}/{year}/")
            cookie_by_host[host] = host_cookies.get(host) or api_cookie

    # Worker: fetch + parse pending trades for the leagues on a host
    def _worker(host_key: str, lids: list[str]) -> list[dict]:
        results: list[dict] = []
        client = clients_by_host[host_key]
        cookie = cookie_by_host[host_key]
        for lid in lids:
            league_payload = {
                "league_id": lid,
                "host_used": host_key,
                "trades": [],
                "error": None,
            }
            try:
                xml = client.get_pending_trades(lid, cookie)
                trades = parse_pending_trades(xml)

                def _side_to_dict(side):
                    return {
                        "franchise_id": side.franchise_id,
                        "player_ids": side.player_ids,
                        "future_picks": [
                            {"season": s, "round": r, "original_team": o} for (s, r, o) in side.future_picks
                        ],
                        "faab": side.faab,
                    }

                t_list = []
                for t in trades:
                    offered_to = None
                    if getattr(t, "proposed_by", None) and t.franchises and len(t.franchises) == 2:
                        others = [f for f in t.franchises if f != t.proposed_by]
                        if len(others) == 1:
                            offered_to = others[0]
                    t_list.append({
                        "trade_id": t.trade_id,
                        "status": t.status,
                        "created_ts": t.created_ts,
                        "expires_ts": t.expires_ts,
                        "franchises": t.franchises,
                        "sides": [_side_to_dict(s) for s in t.sides],
                        "comments": t.comments,
                        "proposed_by": t.proposed_by,
                        "offered_to": offered_to,
                    })
                league_payload["trades"] = t_list
            except Exception as e:
                league_payload["error"] = str(e)
            results.append(league_payload)
        return results

    flat = []
    results = []
    total_trades = 0

    # Execute workers
    with ThreadPoolExecutor(max_workers=len(groups)) as ex:
        futures = [ex.submit(_worker, host, lids) for host, lids in groups.items()]
        for fut in as_completed(futures):
            try:
                for lp in fut.result():
                    # Attach name/year/host from DB row (main thread, safe)
                    lg = next((L for L in leagues if L.mfl_id == lp["league_id"]), None)
                    if lg:
                        lp["league_name"] = lg.name
                        lp["year"] = lg.year
                        lp["franchise_id"] = lg.franchise_id
                        lp["host"] = getattr(lg, "league_host", None) or host_by_lid.get(lg.mfl_id)
                    results.append(lp)
                    for t in (lp.get("trades") or []):
                        flat.append({
                            "league_id": lp["league_id"],
                            "league_name": lg.name if lg else "",
                            "host_used": lp.get("host_used"),
                            **t,
                        })
                    total_trades += len(lp.get("trades") or [])
            except Exception as e:
                current_app.logger.info("trades worker failed: %s", e)

    return {
        "ok": True,
        "year": year,
        "count_leagues": len(results),
        "count_trades": total_trades,
        "trades_flat": flat,   # flattened for finder UIs
        "leagues": results,    # grouped by league (with errors if any)
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


# --------------------------- Trades: Home (boxes) ----------------------------

@mfl_bp.route("/trades", methods=["GET"])
@login_required
def trades_home():
    """
    Landing page showing two big boxes:
      - Open Trades
      - Automated Offers (Coming Soon)
    """
    miss = _require_mfl_cookie()
    if miss:
        return miss

    try:
        year = int(request.args.get("year", datetime.utcnow().year))
    except ValueError:
        year = datetime.utcnow().year

    # check cache age for Open Trades box
    cached, age_sec = _cache_get(current_user.id, year)
    next_refresh_in = max(0, int(_TRADES_CACHE_TTL_SEC - age_sec)) if cached else 0

    return render_template(
        "trades_home.html",
        year=year,
        has_cached=bool(cached),
        next_refresh_in=next_refresh_in,
    )


# --------------------------- Trades: Open (HTML view) -----------------------

@mfl_bp.route("/trades/open", methods=["GET"])
@login_required
def trades_open():
    """
    If cache older than 15 minutes (or ?force=1), refresh. Then render HTML list
    with 'Trades Received' and 'Trades Sent' sections, enriched with team/player names.
    """
    miss = _require_mfl_cookie()
    if miss:
        return miss

    try:
        year = int(request.args.get("year", datetime.utcnow().year))
    except ValueError:
        year = datetime.utcnow().year

    force = request.args.get("force") in {"1", "true", "yes"}

    cached, age_sec = _cache_get(current_user.id, year)
    if not cached or force:
        data = _gather_open_trades(year)
        _cache_set(current_user.id, year, data)
        cached = data
        age_sec = 0.0  # just fetched

    # --- helpers
    def _ts_to_dt(s):
        try:
            n = int(str(s))
            # accept either seconds or milliseconds
            if n < 1_000_000_000_000:
                return datetime.fromtimestamp(n, tz=timezone.utc)
            return datetime.fromtimestamp(n / 1000.0, tz=timezone.utc)
        except Exception:
            return None

    def _pad(fid):
        if fid is None:
            return None
        s = str(fid).strip()
        return s.zfill(4) if s.isdigit() else s

    # --- map: my franchise id per league
    leagues = League.query.filter_by(user_id=current_user.id, year=year).all()
    my_fid_by_league = {lg.mfl_id: _pad(lg.franchise_id) for lg in leagues}

    # --- build team_names {league_id: {fid: team_name}}
    team_names: dict[str, dict[str, str]] = {}
    rows = (
        db.session.query(Team, League)
        .join(League, Team.league_id == League.id)
        .filter(League.user_id == current_user.id, League.year == year)
        .all()
    )
    for team, lg in rows:
        inner = team_names.setdefault(str(lg.mfl_id), {})
        inner[_pad(team.mfl_id)] = team.name

    # --- split trades + collect player ids for lookup
    to_you, from_you = [], []
    player_ids_needed: set[int] = set()

    def classify(tr: dict) -> str:
        my_fid = my_fid_by_league.get(tr["league_id"])
        if not my_fid:
            return "to_you"
        if tr.get("proposed_by") and tr["proposed_by"] == my_fid:
            return "from_you"
        if tr.get("offered_to") and tr["offered_to"] == my_fid:
            return "to_you"
        # fallback when proposer/offeree unknown: treat as "received"
        if tr.get("franchises") and my_fid in tr["franchises"]:
            return "to_you"
        return "to_you"

    # enrich + split
    for tr in (cached.get("trades_flat") or []):
        # gather players for lookup
        for s in (tr.get("sides") or []):
            for pid in (s.get("player_ids") or []):
                try:
                    player_ids_needed.add(int(pid))
                except Exception:
                    pass

        enriched = {
            **tr,
            "my_franchise_id": my_fid_by_league.get(tr["league_id"]),
            "created_dt": _ts_to_dt(tr.get("created_ts")),
            "expires_dt": _ts_to_dt(tr.get("expires_ts")),
        }
        (to_you if classify(tr) == "to_you" else from_you).append(enriched)

    # newest first
    def _sort_key(x):
        c = x.get("created_dt")
        return c.timestamp() if c else 0
    to_you.sort(key=_sort_key, reverse=True)
    from_you.sort(key=_sort_key, reverse=True)

    # --- player lookup (provide both 'player_map' and 'player_lookup' for safety)
    player_map: dict = {}
    if player_ids_needed:
        players = Player.query.filter(Player.id.in_(player_ids_needed)).all()
        for p in players:
            data = {"name": p.name or f"Player #{p.id}", "pos": p.position, "nfl_team": p.team}
            player_map[int(p.id)] = data
            player_map[str(p.id)] = data  # make string keys available too

    next_refresh_in = max(0, int(_TRADES_CACHE_TTL_SEC - age_sec))

    return render_template(
        "open_trades.html",
        year=year,
        fetched_at=cached.get("fetched_at"),
        count_total=cached.get("count_trades", 0),
        to_you=to_you,
        from_you=from_you,
        team_names=team_names,
        player_map=player_map,        # <- what the template expects
        player_lookup=player_map,     # <- also provided in case your template used this name
        next_refresh_in=next_refresh_in,
    )


# --------------------------- Trades: JSON sync (ALL leagues) -----------------

@mfl_bp.route("/trades/sync", methods=["GET"])
@login_required
def mfl_trades_sync():
    """
    JSON endpoint to fetch OPEN (pending) trades across **all connected leagues**
    for the selected season (?year=YYYY, defaults to current UTC year).

    This is intentionally NOT scoped to a specific league. It's a finder.
    """
    miss = _require_mfl_cookie()
    if miss:
        return miss

    # Year param
    try:
        year = int(request.args.get("year", datetime.utcnow().year))
    except ValueError:
        year = datetime.utcnow().year

    force = request.args.get("force") in {"1", "true", "yes"}

    # read/refresh cache
    cached, _age = _cache_get(current_user.id, year)
    if not cached or force:
        data = _gather_open_trades(year)
        _cache_set(current_user.id, year, data)
        cached = data

    return jsonify(cached)
