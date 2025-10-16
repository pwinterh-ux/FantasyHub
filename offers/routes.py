# offers/routes.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Tuple, Optional, Any, Set

from flask import Blueprint, render_template, request, session, current_app, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from models import League, Team, Roster, DraftPick, Player
from services.mfl_trade import send_trade_proposal, parse_mfl_import_response

# Entitlements & mass-offer guard
try:
    from services.entitlements import get_entitlements
except Exception:
    def get_entitlements(_user):  # soft fallback
        return {"plan_key": "free", "mass_offer_daily_cap": 0}

try:
    from services.guards import consume_mass_offer
except Exception:
    # soft fallback that always allows
    def consume_mass_offer(*args, **kwargs):
        return True, None

# Counter storage (safe fallback if not wired yet)
try:
    from services import store  # expects counter functions
except Exception:
    class _NoStore:
        @staticmethod
        def get_today_mass_offer_count(user_id: int, d: date) -> int: return 0
        @staticmethod
        def increment_today_mass_offer_count(user_id: int, d: date) -> None: return None
        @staticmethod
        def get_bonus_balance(user_id: int) -> int: return 0
        @staticmethod
        def use_one_bonus(user_id: int) -> int: return 0
        @staticmethod
        def get_weekly_free_used(user_id: int, monday: date) -> bool: return False
        @staticmethod
        def mark_weekly_free_used(user_id: int, monday: date) -> None: return None
    store = _NoStore()

offers_bp = Blueprint("offers", __name__, url_prefix="/offers")

# ----------------------------- helpers / constants ---------------------------

SYNC_MAX_AGE_HOURS = 4

PRICE_TEMPLATES = [
    # code, label, requirements as {round: count}
    ("2x1st", "Two 1sts", {1: 2}),
    ("1st+2nd", "1st + 2nd", {1: 1, 2: 1}),
    ("1st", "1st", {1: 1}),
    ("2x2nd", "Two 2nds", {2: 2}),
    ("2nd", "2nd", {2: 1}),
    ("2nd+3rd", "2nd + 3rd", {2: 1, 3: 1}),
    ("2x3rd", "Two 3rds", {3: 2}),
    ("3rd", "3rd", {3: 1}),
    ("3rd+4th", "3rd + 4th", {3: 1, 4: 1}),
    ("4th", "4th", {4: 1}),
    ("5th", "5th", {5: 1}),
    ("6th", "6th", {6: 1}),
    # NOTE: Pick Upgrade template is handled specially and not part of PRICE_TEMPLATES
]
PRICE_INDEX = {code: req for code, _label, req in PRICE_TEMPLATES}
PRICE_LABEL = {code: label for code, label, _ in PRICE_TEMPLATES}


def _now_utc():
    return datetime.now(timezone.utc)


def _require_recent_sync_or_gate():
    """
    Return None if OK. Otherwise, returns a rendered gate page telling the user to sync.
    Criteria: at least one of the user's leagues has synced_at within SYNC_MAX_AGE_HOURS.
    """
    cutoff = _now_utc() - timedelta(hours=SYNC_MAX_AGE_HOURS)
    exists = (
        db.session.query(League.id)
        .filter(League.user_id == current_user.id, League.synced_at != None, League.synced_at >= cutoff)
        .first()
    )
    if exists:
        return None

    # Gentle gate: show a page with link to config (not modifying existing files)
    return render_template(
        "offers/gate_sync.html",
        max_age_hours=SYNC_MAX_AGE_HOURS,
    )


def _get_my_team_in_league(lg: League) -> Team | None:
    """Find the Team row for the current user (by league.franchise_id)."""
    if not lg.franchise_id:
        return None
    return Team.query.filter_by(league_id=lg.id, mfl_id=str(lg.franchise_id).zfill(4)).first()


def _owns_player(team: Team, player_id: int) -> bool:
    return db.session.query(Roster.id).filter(Roster.team_id == team.id, Roster.player_id == player_id).first() is not None


def _team_for_player_in_league(lg: League, player_id: int) -> Team | None:
    """Which team currently rosters the player in this league (if any)."""
    return (
        db.session.query(Team)
        .join(Roster, Roster.team_id == Team.id)
        .filter(Team.league_id == lg.id, Roster.player_id == player_id)
        .first()
    )


def _resolve_host_and_cookie(league: League) -> Tuple[str, Optional[str]]:
    host = (league.league_host or "").strip() or "api.myfantasyleague.com"
    cookie = None
    get_host_cookies = getattr(current_user, "get_mfl_host_cookies", None)
    if callable(get_host_cookies):
        try:
            host_cookies = get_host_cookies() or {}
            cookie = host_cookies.get(host)
        except Exception:
            cookie = None
    if not cookie:
        cookie = getattr(current_user, "mfl_cookie_api", None) or getattr(current_user, "session_key", None)
    return host, cookie


def _pick_counts_by_round(team: Team) -> Dict[int, int]:
    rows = DraftPick.query.filter(DraftPick.team_id == team.id).all()
    out: Dict[int, int] = {}
    for p in rows:
        try:
            r = int(p.round)
        except Exception:
            continue
        out[r] = out.get(r, 0) + 1
    return out


def _pick_objects_by_round(team: Team) -> Dict[int, List[DraftPick]]:
    rows = DraftPick.query.filter(DraftPick.team_id == team.id).all()
    out: Dict[int, List[DraftPick]] = {}
    for p in rows:
        try:
            r = int(p.round)
        except Exception:
            continue
        out.setdefault(r, []).append(p)
    return out


def _meets_requirements(counts: Dict[int, int], req: Dict[int, int]) -> bool:
    for rnd, need in req.items():
        if counts.get(rnd, 0) < need:
            return False
    return True


def _session_key(mode: str, player_id: int, template_code: str) -> str:
    return f"tb_sent::{mode}::{player_id}::{template_code}"


def _get_sent_set(mode: str, player_id: int, template_code: str) -> set[str]:
    """
    Return set of league_ids (str) that we've already sent offers to (for this session context).
    Auto-expire after ~1 hour or on new search (we'll clear explicitly when new search starts).
    """
    key = _session_key(mode, player_id, template_code)
    data = session.get(key) or {"ts": _now_utc().timestamp(), "leagues": []}
    # TTL 1h
    ts = data.get("ts", 0)
    if (_now_utc().timestamp() - float(ts)) > 3600:
        session.pop(key, None)
        return set()
    return set(data.get("leagues") or [])


def _add_sent_leagues(mode: str, player_id: int, template_code: str, league_ids: List[str]) -> None:
    key = _session_key(mode, player_id, template_code)
    data = session.get(key) or {"ts": _now_utc().timestamp(), "leagues": []}
    cur = set(data.get("leagues") or [])
    cur.update(str(x) for x in league_ids)
    session[key] = {"ts": _now_utc().timestamp(), "leagues": sorted(cur)}


def _clear_sent_contexts():
    # wipe all tb_sent::* keys (called on new search)
    for k in list(session.keys()):
        if str(k).startswith("tb_sent::"):
            session.pop(k, None)


def _league_franchise_maps(league: League) -> Tuple[Dict[str, str], Dict[str, str]]:
    names: Dict[str, str] = {}
    records: Dict[str, str] = {}
    teams = Team.query.filter(Team.league_id == league.id).all()
    for t in teams:
        fid = str(t.mfl_id).zfill(4)
        names[fid] = t.name or fid
        if t.record:
            records[fid] = t.record
    return names, records


def _draft_pick_to_token(pick: DraftPick) -> Optional[str]:
    try:
        orig = str(pick.original_team or "").zfill(4)
        return f"FP_{orig}_{int(pick.season)}_{int(pick.round)}"
    except Exception:
        return None


def _format_pick_label(pick: DraftPick, names: Dict[str, str], records: Dict[str, str]) -> str:
    base = f"{pick.season} R{pick.round}"
    orig = str(pick.original_team or "").zfill(4)
    if orig.strip("0"):
        label = names.get(orig, orig)
        rec = records.get(orig)
        if rec:
            return f"{base} (orig {label} — {rec})"
        return f"{base} (orig {label})"
    return base


def _player_assets_for_team(team: Team) -> List[Dict[str, Any]]:
    rows = (
        db.session.query(Player)
        .join(Roster, Roster.player_id == Player.id)
        .filter(Roster.team_id == team.id)
        .order_by(Player.position.asc(), Player.name.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for pl in rows:
        out.append(
            {
                "id": pl.id,
                "name": pl.name or "(unknown)",
                "position": pl.position or "--",
                "nfl_team": pl.team or "FA",
            }
        )
    return out


def _pick_assets_for_team(team: Team, names: Dict[str, str], records: Dict[str, str]) -> List[Dict[str, Any]]:
    picks = (
        DraftPick.query.filter(DraftPick.team_id == team.id)
        .order_by(DraftPick.season.asc(), DraftPick.round.asc(), DraftPick.pick_number.asc())
        .all()
    )
    out: List[Dict[str, Any]] = []
    for pick in picks:
        token = _draft_pick_to_token(pick)
        out.append(
            {
                "id": pick.id,
                "label": _format_pick_label(pick, names, records),
                "token": token,
                "usable": token is not None,
            }
        )
    return out


def _validate_player_ids(team: Team, player_ids: List[int]) -> List[int]:
    if not player_ids:
        return []
    rows = (
        db.session.query(Roster.player_id)
        .filter(Roster.team_id == team.id, Roster.player_id.in_(player_ids))
        .all()
    )
    valid = {int(pid) for (pid,) in rows}
    return [pid for pid in player_ids if pid in valid]


def _pick_tokens_for_team(team: Team, pick_ids: List[int]) -> List[str]:
    if not pick_ids:
        return []
    picks = DraftPick.query.filter(
        DraftPick.team_id == team.id, DraftPick.id.in_(pick_ids)
    ).all()
    by_id = {int(p.id): p for p in picks}
    tokens: List[str] = []
    for pid in pick_ids:
        pick = by_id.get(pid)
        if not pick:
            continue
        token = _draft_pick_to_token(pick)
        if token:
            tokens.append(token)
    return tokens


def _validate_pick_ids(team: Team, pick_ids: List[int]) -> List[int]:
    if not pick_ids:
        return []
    picks = DraftPick.query.filter(
        DraftPick.team_id == team.id, DraftPick.id.in_(pick_ids)
    ).all()
    valid = {int(p.id) for p in picks}
    return [pid for pid in pick_ids if pid in valid]


# ------------------------------- routes --------------------------------------

@offers_bp.route("/", methods=["GET", "POST"])
@login_required
def search():
    """
    Step 0/1: Gate on recent sync, then show a simple search + mode + template picker.
    POST submits player_id/mode/template -> /offers/build
    """
    # Gate
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    # If user started a new search, wipe per-session 'sent' contexts
    if request.method == "POST":
        _clear_sent_contexts()
        player_id = request.form.get("player_id", "").strip()
        mode = (request.form.get("mode") or "buy").lower()
        template_code = request.form.get("template_code") or "2nd"  # default

        if not player_id:
            flash("Pick a player from the search results.", "warning")
            return redirect(url_for("offers.search"))

        # Carry upgrade params when applicable (SELL-only template)
        if template_code == "upgrade":
            upgrade_give_round = request.form.get("upgrade_give_round", "").strip()
            upgrade_recv_round = request.form.get("upgrade_recv_round", "").strip()
            if mode != "sell":
                flash("Pick Upgrade is only available in SELL mode.", "warning")
                return redirect(url_for("offers.search"))
            if not upgrade_give_round or not upgrade_recv_round:
                flash("Select both 'Give round' and 'Receive round' for Pick Upgrade.", "warning")
                return redirect(url_for("offers.search"))
            return redirect(url_for(
                "offers.build",
                player_id=player_id,
                mode=mode,
                template_code=template_code,
                upgrade_give_round=upgrade_give_round,
                upgrade_recv_round=upgrade_recv_round,
            ))

        # Non-upgrade flow
        template_code = template_code if template_code in PRICE_INDEX else "2nd"
        return redirect(url_for("offers.build", player_id=player_id, mode=mode, template_code=template_code))

    # live-ish search (server-side after submit)
    q = (request.args.get("q") or "").strip()
    players = []
    if q:
        like = f"%{q}%"
        players = (
            Player.query.filter(Player.name.ilike(like))
            .order_by(Player.name.asc())
            .limit(50)
            .all()
        )

    return render_template(
        "offers/search.html",
        q=q,
        players=players,
        price_templates=PRICE_TEMPLATES,
    )


@offers_bp.route("/build", methods=["GET"])
@login_required
def build():
    """
    Step 2/3: Given player_id + mode + template_code,
    compose candidate leagues (and counterparties) and render a selection list.
    Includes special SELL 'upgrade' template gating and data.
    """
    # Gate
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    # Params
    try:
        player_id = int(request.args.get("player_id", "0"))
    except Exception:
        player_id = 0
    mode = (request.args.get("mode") or "buy").lower()
    template_code = request.args.get("template_code") or "2nd"

    # Upgrade-specific params (from Offers page)
    upgrade_give_round: Optional[int] = None
    upgrade_recv_round: Optional[int] = None
    if template_code == "upgrade":
        try:
            upgrade_give_round = int(request.args.get("upgrade_give_round", "0"))
        except Exception:
            upgrade_give_round = 0
        try:
            upgrade_recv_round = int(request.args.get("upgrade_recv_round", "0"))
        except Exception:
            upgrade_recv_round = 0

    # Validations
    if not player_id or mode not in {"buy", "sell"} or (template_code not in PRICE_INDEX and template_code != "upgrade"):
        flash("Invalid builder parameters.", "danger")
        return redirect(url_for("offers.search"))
    if template_code == "upgrade" and (not upgrade_give_round or not upgrade_recv_round):
        flash("Pick Upgrade requires both give/receive rounds.", "warning")
        return redirect(url_for("offers.search"))

    player = Player.query.get(player_id)
    if not player:
        flash("Player not found.", "danger")
        return redirect(url_for("offers.search"))

    req = PRICE_INDEX.get(template_code, {})  # empty for 'upgrade'
    year_now = datetime.utcnow().year

    # All leagues for this user/year
    leagues = League.query.filter_by(user_id=current_user.id, year=year_now).all()

    # ---- Global franchise_names map (franchise_id -> name) as a fallback for templates
    franchise_names: Dict[str, str] = {}
    if leagues:
        league_ids = [lg.id for lg in leagues]
        teams_all = Team.query.filter(Team.league_id.in_(league_ids)).all()
        for t in teams_all:
            if t.mfl_id:
                franchise_names[str(t.mfl_id).zfill(4)] = t.name or str(t.mfl_id)

    sent_hide = _get_sent_set(mode, player_id, template_code)

    # Preferred-year union containers
    buy_years_set: Set[int] = set()
    sell_years_set: Set[int] = set()

    rows: List[Dict[str, Any]] = []  # per-league blocks for the template

    if mode == "buy":
        # ---------------------------- BUY -------------------------
        for lg in leagues:
            my_team = _get_my_team_in_league(lg)
            if not my_team:
                continue

            # You must NOT already own the player
            if _owns_player(my_team, player_id):
                continue

            # Find current owner of the player in this league
            owner_team = _team_for_player_in_league(lg, player_id)
            if not owner_team or owner_team.id == my_team.id:
                continue

            # Do I have the required picks (ignoring years)?
            counts = _pick_counts_by_round(my_team)
            if not _meets_requirements(counts, req):
                continue

            if str(lg.mfl_id) in sent_hide:
                continue

            # Exact picks available (for UI)
            picks_by_round = _pick_objects_by_round(my_team)

            # collect years
            for lst in picks_by_round.values():
                for dp in lst:
                    if dp.season:
                        try:
                            buy_years_set.add(int(dp.season))
                        except Exception:
                            pass

            # per-league franchise maps (name + record)
            league_fnames: Dict[str, str] = {}
            league_frecords: Dict[str, str] = {}
            for t in Team.query.filter(Team.league_id == lg.id).all():
                if t.mfl_id:
                    fid = str(t.mfl_id).zfill(4)
                    league_fnames[fid] = t.name or fid
                    league_frecords[fid] = t.record or ""

            rows.append({
                "league": lg,
                "my_team": my_team,
                "counterparty": owner_team,
                "picks_by_round": picks_by_round,
                "franchise_names": league_fnames,      # per-league map
                "franchise_records": league_frecords,  # fid -> record
            })

    else:
        # ---------------------------- SELL -----------------------------------
        if template_code != "upgrade":
            # --------- SELL (standard templates) ----------
            for lg in leagues:
                my_team = _get_my_team_in_league(lg)
                if not my_team:
                    continue

                # I must own the player
                if not _owns_player(my_team, player_id):
                    continue

                teams = Team.query.filter(Team.league_id == lg.id, Team.id != my_team.id).all()
                eligible_buyers: List[Team] = []
                for t in teams:
                    if _meets_requirements(_pick_counts_by_round(t), req):
                        eligible_buyers.append(t)

                if not eligible_buyers:
                    continue

                if str(lg.mfl_id) in sent_hide:
                    continue

                buyers_detail = []
                league_years: Set[int] = set()
                for t in eligible_buyers:
                    pbr = _pick_objects_by_round(t)
                    for lst in pbr.values():
                        for dp in lst:
                            if dp.season:
                                try:
                                    y = int(dp.season)
                                    sell_years_set.add(y)
                                    league_years.add(y)
                                except Exception:
                                    pass
                    buyers_detail.append({
                        "team": t,
                        "picks_by_round": pbr,
                    })

                league_fnames: Dict[str, str] = {}
                league_frecords: Dict[str, str] = {}
                for t in Team.query.filter(Team.league_id == lg.id).all():
                    if t.mfl_id:
                        fid = str(t.mfl_id).zfill(4)
                        league_fnames[fid] = t.name or fid
                        league_frecords[fid] = t.record or ""

                rows.append({
                    "league": lg,
                    "my_team": my_team,
                    "buyers": buyers_detail,
                    "years": sorted(league_years),         # per-league preferred-year options
                    "franchise_names": league_fnames,      # per-league map
                    "franchise_records": league_frecords,  # fid -> record
                })

        else:
            # --------- SELL (PICK UPGRADE) ----------
            # Only show leagues where (a) you own the player and (b) you have at least one pick in the give round.
            for lg in leagues:
                my_team = _get_my_team_in_league(lg)
                if not my_team:
                    continue

                # Must own the player in this league
                if not _owns_player(my_team, player_id):
                    continue

                # My picks by round, and filter to the give round
                my_picks_by_round = _pick_objects_by_round(my_team)
                my_give_list: List[DraftPick] = my_picks_by_round.get(int(upgrade_give_round), []) if upgrade_give_round else []

                # Per-league name/record maps
                league_fnames: Dict[str, str] = {}
                league_frecords: Dict[str, str] = {}
                for t in Team.query.filter(Team.league_id == lg.id).all():
                    if t.mfl_id:
                        fid = str(t.mfl_id).zfill(4)
                        league_fnames[fid] = t.name or fid
                        league_frecords[fid] = t.record or ""

                # Buyers and their receive-round picks
                buyers_detail: List[Dict[str, Any]] = []
                teams_others = Team.query.filter(Team.league_id == lg.id, Team.id != my_team.id).all()
                for t in teams_others:
                    pbr = _pick_objects_by_round(t)
                    recv_list = pbr.get(int(upgrade_recv_round), []) if upgrade_recv_round else []
                    if recv_list:
                        for dp in recv_list:
                            if dp.season:
                                try:
                                    sell_years_set.add(int(dp.season))
                                except Exception:
                                    pass
                        buyers_detail.append({
                            "team": t,
                            "recv_picks": recv_list,   # only the target receive round
                        })

                # If I have NO give-round pick, we still render a disabled card with the note.
                disabled_reason: Optional[str] = None
                if not my_give_list:
                    disabled_reason = f"Player on this roster, however no round {upgrade_give_round} pick available for upgrade."

                # Hide already-sent leagues in this session context
                if str(lg.mfl_id) in sent_hide:
                    disabled_reason = (disabled_reason or "") + " (Already sent in this session.)"

                rows.append({
                    "league": lg,
                    "my_team": my_team,
                    "upgrade": True,
                    "upgrade_give_round": upgrade_give_round,
                    "upgrade_recv_round": upgrade_recv_round,
                    "my_give_picks": my_give_list,     # list[DraftPick] in the give round
                    "buyers": buyers_detail,           # list of {team, recv_picks}
                    "franchise_names": league_fnames,
                    "franchise_records": league_frecords,
                    "disabled_reason": disabled_reason,  # render as disabled if set
                })

    # Global preferred-year options:
    #  - BUY uses a single global control across all leagues (union of my picks)
    #  - SELL standard uses union of buyer picks (already collected)
    #  - SELL upgrade uses union of buyers' receive-round pick years (collected above)
    year_options = sorted(buy_years_set.union(sell_years_set)) if (buy_years_set or sell_years_set) else []
    default_preferred_year = year_options[0] if year_options else None

    return render_template(
        "offers/build.html",
        mode=mode,
        template_code=template_code,
        template_label=PRICE_LABEL.get(template_code, "Pick Upgrade" if template_code == "upgrade" else template_code),
        player=player,
        req=req,
        rows=rows,
        price_templates=PRICE_TEMPLATES,
        franchise_names=franchise_names,           # global fallback map
        year_options=year_options,                 # for global Preferred-Year radios
        default_preferred_year=default_preferred_year,
        # upgrade params for template JS/labels
        upgrade_give_round=upgrade_give_round,
        upgrade_recv_round=upgrade_recv_round,
    )


@offers_bp.route("/send", methods=["POST"])
@login_required
def send_offers():
    """
    Step 4/5: Mock 'send' — log would-be proposeTrade API calls and show a result page.
    Also update session cache to hide these leagues for this (mode, player, template) context.

    IMPORTANT: No recent-sync gate here (by design).
    We only check mass-send caps for Free/limited plans.
    """
    try:
        player_id = int(request.form.get("player_id", "0"))
    except Exception:
        player_id = 0
    mode = (request.form.get("mode") or "buy").lower()
    template_code = request.form.get("template_code") or "2nd"

    if not player_id or mode not in {"buy", "sell"} or (template_code not in PRICE_INDEX and template_code != "upgrade"):
        flash("Invalid send parameters.", "danger")
        return redirect(url_for("offers.search"))

    # selected leagues come as league_id strings
    league_ids = request.form.getlist("league_id")
    if not league_ids:
        flash("No leagues selected.", "warning")
        return redirect(url_for("offers.build", player_id=player_id, mode=mode, template_code=template_code))

    # -------- LIMITED-PLANS ONLY: mass-send gate --------
    ent = get_entitlements(current_user) or {}
    plan_key = str(ent.get("plan_key", "free")).lower()
    try:
        daily_cap = int(ent.get("mass_offer_daily_cap", 0))
    except Exception:
        daily_cap = 0

    # Gate if Free OR finite daily cap
    should_gate = (plan_key == "free") or (daily_cap < 9999)
    if should_gate:
        ok, msg = consume_mass_offer(
            current_user,
            recipients_count=len(league_ids),
            get_today_count=store.get_today_count,                # <— change to this
            increment_today_count=store.increment_today_count,    # <— and this
            get_bonus_balance=store.get_bonus_balance,
            use_one_bonus=store.use_one_bonus,
            get_weekly_free_used=getattr(store, "get_weekly_free_used", None),
            mark_weekly_free_used=getattr(store, "mark_weekly_free_used", None),
        )

        if not ok:
            flash(msg or "Your plan limits have been reached.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode=mode, template_code=template_code))
    # -----------------------------------------------

    req = PRICE_INDEX.get(template_code, {})
    offers_log = []
    for lid in league_ids:
        lg = League.query.filter_by(user_id=current_user.id, mfl_id=str(lid)).first()
        if not lg:
            continue

        my_team = _get_my_team_in_league(lg)
        if not my_team:
            continue

        if mode == "buy":
            # counterparty: current owner of the player
            owner_team = _team_for_player_in_league(lg, player_id)
            if not owner_team:
                continue
            offered_by = my_team
            offered_to = owner_team

            # --- BUY: collect chosen picks per needed round (name = pick_{lid}_{rnd})
            chosen_picks: List[DraftPick] = []
            for rnd, need in req.items():
                form_key = f"pick_{lid}_{rnd}"
                pick_ids = request.form.getlist(form_key)[:need]
                if pick_ids:
                    found = DraftPick.query.filter(DraftPick.id.in_(pick_ids)).all()
                    id_to_obj = {str(p.id): p for p in found}
                    chosen_picks.extend([id_to_obj.get(pid) for pid in pick_ids if pid in id_to_obj])

            payload = {
                "league_id": lg.mfl_id,
                "offered_by_fid": offered_by.mfl_id,
                "offered_to_fid": offered_to.mfl_id,
                "giving": [f"Pick({p.season} R{p.round} from {p.original_team})" for p in chosen_picks],
                "getting": [f"Player({player_id})"],
            }
            current_app.logger.info("[MOCK PROPOSE] %s", payload)
            offers_log.append({"league": lg, "status": "ok", "detail": payload})

        else:
            # SELL (mock path retained for completeness; real path is /perform)
            offered_by = my_team
            # buyer chosen per team checkbox: buyer_<league_id>=<team_id> (multi)
            buyer_team_ids = request.form.getlist(f"buyer_{lid}")
            if not buyer_team_ids:
                continue

            for bt in buyer_team_ids:
                offered_to = Team.query.get(bt)
                chosen_picks: List[DraftPick] = []
                for rnd, need in req.items():
                    form_key = f"pick_{lid}_{bt}_{rnd}"
                    pick_ids = request.form.getlist(form_key)[:need]
                    if pick_ids:
                        found = DraftPick.query.filter(DraftPick.id.in_(pick_ids)).all()
                        id_to_obj = {str(p.id): p for p in found}
                        chosen_picks.extend([id_to_obj.get(pid) for pid in id_to_obj])

                payload = {
                    "league_id": lg.mfl_id,
                    "offered_by_fid": offered_by.mfl_id,
                    "offered_to_fid": offered_to.mfl_id if offered_to else None,
                    "giving": [f"Player({player_id})"],
                    "getting": [f"Pick({p.season} R{p.round} from {p.original_team})" for p in chosen_picks],
                }
                current_app.logger.info("[MOCK PROPOSE] %s", payload)
                offers_log.append({"league": lg, "status": "ok", "detail": payload})

    # hide these leagues for this session context
    _add_sent_leagues(mode, player_id, template_code, league_ids)

    return render_template(
        "offers/send_result.html",
        mode=mode,
        template_code=template_code,
        player_id=player_id,
        offers_log=offers_log,
    )


@offers_bp.route("/rapid", methods=["GET", "POST"])
@login_required
def rapid_start():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    if request.method == "POST":
        session.pop("rapid_trade", None)
        mode = (request.form.get("mode") or "buy").lower()
        if mode not in {"buy", "sell"}:
            mode = "buy"

        try:
            player_id = int(request.form.get("player_id", "0"))
        except Exception:
            player_id = 0

        player = Player.query.get(player_id) if player_id else None
        if not player:
            flash("Pick a player to start the rapid flow.", "warning")
            return redirect(url_for("offers.rapid_start"))

        leagues = (
            League.query.filter_by(user_id=current_user.id)
            .order_by(League.name.asc())
            .all()
        )

        if mode == "buy":
            league_ids: List[str] = []
            for lg in leagues:
                if not lg.mfl_id:
                    continue
                my_team = _get_my_team_in_league(lg)
                if not my_team:
                    continue
                owner_team = _team_for_player_in_league(lg, player.id)
                if not owner_team or owner_team.id == my_team.id:
                    continue
                league_ids.append(str(lg.mfl_id))

            if not league_ids:
                flash("No MFL leagues found where you can buy this player right now.", "info")
                return redirect(
                    url_for(
                        "offers.rapid_start",
                        q=player.name,
                        player_id=player.id,
                        mode="buy",
                    )
                )

            session["rapid_trade"] = {
                "mode": "buy",
                "player_id": player.id,
                "player_name": player.name,
                "player_position": player.position,
                "player_team": player.team,
                "league_ids": league_ids,
                "total": len(league_ids),
                "summary": [],
                "started": int(_now_utc().timestamp()),
            }
            session.modified = True
            return redirect(url_for("offers.rapid_run"))

        sell_league_id = (request.form.get("sell_league_id") or "").strip()
        if not sell_league_id:
            flash("Choose a league to start the sell flow.", "warning")
            return redirect(
                url_for(
                    "offers.rapid_start",
                    q=player.name,
                    player_id=player.id,
                    mode="sell",
                )
            )

        league = League.query.filter_by(user_id=current_user.id, mfl_id=sell_league_id).first()
        if not league:
            flash("League not found for your account.", "warning")
            return redirect(
                url_for(
                    "offers.rapid_start",
                    q=player.name,
                    player_id=player.id,
                    mode="sell",
                )
            )

        my_team = _get_my_team_in_league(league)
        if not my_team:
            flash("We could not locate your franchise in that league.", "warning")
            return redirect(
                url_for(
                    "offers.rapid_start",
                    q=player.name,
                    player_id=player.id,
                    mode="sell",
                    sell_league_id=sell_league_id,
                )
            )

        owner_team = _team_for_player_in_league(league, player.id)
        if not owner_team or owner_team.id != my_team.id:
            flash("You do not roster that player in the selected league.", "warning")
            return redirect(
                url_for(
                    "offers.rapid_start",
                    q=player.name,
                    player_id=player.id,
                    mode="sell",
                    sell_league_id=sell_league_id,
                )
            )

        buyers = (
            Team.query.filter(Team.league_id == league.id, Team.id != my_team.id)
            .order_by(Team.name.asc())
            .all()
        )
        if not buyers:
            flash("No other teams found in that league.", "info")
            return redirect(
                url_for(
                    "offers.rapid_start",
                    q=player.name,
                    player_id=player.id,
                    mode="sell",
                    sell_league_id=sell_league_id,
                )
            )

        preset_mode = (request.form.get("preset_mode") or "custom").lower()
        default_player_ids: List[int] = []
        default_pick_ids: List[int] = []

        if preset_mode == "preset":
            for raw in request.form.getlist("default_player"):
                try:
                    default_player_ids.append(int(raw))
                except Exception:
                    continue
            for raw in request.form.getlist("default_pick"):
                try:
                    default_pick_ids.append(int(raw))
                except Exception:
                    continue

            default_player_ids = _validate_player_ids(my_team, default_player_ids)
            default_pick_ids = _validate_pick_ids(my_team, default_pick_ids)
        else:
            preset_mode = "custom"

        team_queue = [str(t.id) for t in buyers]

        session["rapid_trade"] = {
            "mode": "sell",
            "league_id": str(league.mfl_id),
            "league_name": league.name,
            "team_queue": team_queue,
            "total": len(team_queue),
            "summary": [],
            "preset_mode": preset_mode,
            "preset_players": default_player_ids,
            "preset_picks": default_pick_ids,
            "my_team_id": my_team.id,
            "my_team_name": my_team.name,
            "player_id": player.id,
            "player_name": player.name,
            "player_position": player.position,
            "player_team": player.team,
            "started": int(_now_utc().timestamp()),
        }
        session.modified = True
        return redirect(url_for("offers.rapid_run"))

    q = (request.args.get("q") or "").strip()
    mode = (request.args.get("mode") or "buy").lower()
    if mode not in {"buy", "sell"}:
        mode = "buy"

    players: List[Player] = []
    if q:
        like = f"%{q}%"
        players = (
            Player.query.filter(Player.name.ilike(like))
            .order_by(Player.name.asc())
            .limit(50)
            .all()
        )

    try:
        selected_player_id = int(request.args.get("player_id", "0"))
    except Exception:
        selected_player_id = 0

    if players and not selected_player_id:
        selected_player_id = players[0].id

    selected_player = Player.query.get(selected_player_id) if selected_player_id else None

    user_leagues = (
        League.query.filter_by(user_id=current_user.id)
        .order_by(League.name.asc())
        .all()
    )

    buy_league_ids: List[str] = []
    sell_leagues: List[Dict[str, Any]] = []
    if selected_player:
        for lg in user_leagues:
            if not lg.mfl_id:
                continue
            my_team = _get_my_team_in_league(lg)
            if not my_team:
                continue
            owner_team = _team_for_player_in_league(lg, selected_player.id)
            if not owner_team:
                continue
            if owner_team.id == my_team.id:
                sell_leagues.append(
                    {
                        "mfl_id": str(lg.mfl_id),
                        "name": lg.name,
                        "team_id": my_team.id,
                        "team_name": my_team.name,
                    }
                )
            else:
                buy_league_ids.append(str(lg.mfl_id))

    sell_league_id = (request.args.get("sell_league_id") or "").strip()
    sell_assets: Optional[Dict[str, Any]] = None
    selected_league = None
    if mode == "sell" and not sell_league_id and len(sell_leagues) == 1:
        sell_league_id = sell_leagues[0]["mfl_id"]
    if selected_player and sell_league_id:
        selected_league = League.query.filter_by(user_id=current_user.id, mfl_id=sell_league_id).first()
        if selected_league:
            my_team = _get_my_team_in_league(selected_league)
            owner_team = _team_for_player_in_league(selected_league, selected_player.id)
            if not my_team or not owner_team or owner_team.id != my_team.id:
                selected_league = None
            else:
                names, records = _league_franchise_maps(selected_league)
                sell_assets = {
                    "players": _player_assets_for_team(my_team),
                    "picks": _pick_assets_for_team(my_team, names, records),
                }

    if selected_league is None:
        sell_league_id = ""

    buy_count = len(buy_league_ids)

    return render_template(
        "offers/rapid_start.html",
        q=q,
        players=players,
        mode=mode,
        selected_player=selected_player,
        selected_player_id=selected_player_id,
        buy_count=buy_count,
        sell_leagues=sell_leagues,
        sell_league_id=sell_league_id,
        selected_league=selected_league,
        sell_assets=sell_assets,
    )


@offers_bp.route("/rapid/run", methods=["GET", "POST"])
@login_required
def rapid_run():
    gate = _require_recent_sync_or_gate()
    if gate:
        return gate

    context = session.get("rapid_trade") or {}
    mode = context.get("mode")
    if mode not in {"buy", "sell"}:
        session.pop("rapid_trade", None)
        flash("Start a rapid trade session to continue.", "warning")
        return redirect(url_for("offers.rapid_start"))

    queue_key = "league_ids" if mode == "buy" else "team_queue"
    queue: List[Any] = list(context.get(queue_key) or [])

    def _player_detail(asset: Optional[Dict[str, Any]], *, is_core: bool = False) -> Dict[str, Any]:
        if not asset:
            return {
                "type": "player",
                "label": "Unknown player",
                "is_core": is_core,
            }
        pos = asset.get("position") or "–"
        name = asset.get("name") or "Player"
        team = asset.get("nfl_team") or "FA"
        return {
            "type": "player",
            "label": f"{pos} {name} ({team})",
            "is_core": is_core,
        }

    def _pick_detail(asset: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not asset:
            return {"type": "pick", "label": "Draft pick", "is_core": False}
        return {
            "type": "pick",
            "label": asset.get("label") or "Draft pick",
            "is_core": False,
        }

    def _asset_map(assets: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        mapped: Dict[int, Dict[str, Any]] = {}
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            raw_id = asset.get("id")
            try:
                mapped[int(raw_id)] = asset
            except Exception:
                continue
        return mapped

    if request.method == "POST":
        action = (request.form.get("action") or "submit").lower()

        if not queue:
            return redirect(url_for("offers.rapid_summary"))

        summary: List[Dict[str, Any]] = context.get("summary", [])

        if mode == "buy":
            league_mfl_id = str(queue[0])
            league = League.query.filter_by(user_id=current_user.id, mfl_id=league_mfl_id).first()
            player_id = int(context.get("player_id") or 0)
            my_team = _get_my_team_in_league(league) if league else None
            owner_team = _team_for_player_in_league(league, player_id) if league else None

            if not league or not my_team or not owner_team:
                queue.pop(0)
                summary.append(
                    {
                        "status": "error",
                        "league": league.name if league else f"League {league_mfl_id}",
                        "target": owner_team.name if owner_team else "Unknown",
                        "message": "Missing league or roster data.",
                        "mode": "buy",
                        "give": [],
                        "receive": [],
                        "comments": "",
                    }
                )
                context[queue_key] = queue
                context["summary"] = summary
                session["rapid_trade"] = context
                session.modified = True
                flash("Missing league or roster data. Skipping.", "warning")
                return redirect(url_for("offers.rapid_run"))

            if action == "skip":
                queue.pop(0)
                summary.append(
                    {
                        "status": "skipped",
                        "league": league.name,
                        "target": owner_team.name,
                        "message": "Skipped",
                        "mode": "buy",
                        "give": [],
                        "receive": [],
                        "comments": "",
                    }
                )
                flash(f"Skipped {league.name} — {owner_team.name}.", "info")
            else:
                give_players_raw = request.form.getlist("give_player")
                give_picks_raw = request.form.getlist("give_pick")
                recv_players_raw = request.form.getlist("receive_player")
                recv_picks_raw = request.form.getlist("receive_pick")

                give_player_ids: List[int] = []
                give_pick_ids: List[int] = []
                recv_player_ids: List[int] = []
                recv_pick_ids: List[int] = []

                for raw in give_players_raw:
                    try:
                        give_player_ids.append(int(raw))
                    except Exception:
                        continue
                for raw in give_picks_raw:
                    try:
                        give_pick_ids.append(int(raw))
                    except Exception:
                        continue
                for raw in recv_players_raw:
                    try:
                        recv_player_ids.append(int(raw))
                    except Exception:
                        continue
                for raw in recv_picks_raw:
                    try:
                        recv_pick_ids.append(int(raw))
                    except Exception:
                        continue

                give_player_ids = _validate_player_ids(my_team, give_player_ids)
                give_pick_ids = _validate_pick_ids(my_team, give_pick_ids)
                recv_player_ids = _validate_player_ids(owner_team, recv_player_ids)
                recv_pick_ids = _validate_pick_ids(owner_team, recv_pick_ids)

                names, records = _league_franchise_maps(league)
                my_player_assets = _asset_map(_player_assets_for_team(my_team))
                my_pick_assets = _asset_map(
                    _pick_assets_for_team(my_team, names, records)
                )
                their_player_assets = _asset_map(_player_assets_for_team(owner_team))
                their_pick_assets = _asset_map(
                    _pick_assets_for_team(owner_team, names, records)
                )

                if not give_player_ids and not give_pick_ids:
                    flash("Select at least one of your assets to include in the offer.", "warning")
                    context["summary"] = summary
                    context[queue_key] = queue
                    session["rapid_trade"] = context
                    session.modified = True
                    return redirect(url_for("offers.rapid_run"))

                will_give = [str(pid) for pid in give_player_ids]
                will_give.extend(_pick_tokens_for_team(my_team, give_pick_ids))

                will_receive: List[str] = []
                core_pid = str(player_id)
                will_receive.append(core_pid)
                for pid in recv_player_ids:
                    spid = str(pid)
                    if spid != core_pid:
                        will_receive.append(spid)
                will_receive.extend(_pick_tokens_for_team(owner_team, recv_pick_ids))

                comments = (request.form.get("comments") or "").strip()

                give_details: List[Dict[str, Any]] = []
                for pid in give_player_ids:
                    give_details.append(_player_detail(my_player_assets.get(pid)))
                for pkid in give_pick_ids:
                    give_details.append(_pick_detail(my_pick_assets.get(pkid)))

                receive_details: List[Dict[str, Any]] = []
                core_asset = their_player_assets.get(player_id)
                receive_details.append(_player_detail(core_asset, is_core=True))
                for pid in recv_player_ids:
                    if pid == player_id:
                        continue
                    receive_details.append(_player_detail(their_player_assets.get(pid)))
                for pkid in recv_pick_ids:
                    receive_details.append(_pick_detail(their_pick_assets.get(pkid)))

                host, cookie = _resolve_host_and_cookie(league)
                apikey = current_app.config.get("MFL_APIKEY") if current_app else None

                status = "error"
                message = ""
                try:
                    res = send_trade_proposal(
                        host=host,
                        year=league.year,
                        league_id=league.mfl_id,
                        offered_to=str(owner_team.mfl_id).zfill(4),
                        will_give_up=will_give,
                        will_receive=will_receive,
                        comments=comments,
                        apikey=apikey,
                        cookie=cookie,
                    )
                    parsed_ok, parsed_msg = parse_mfl_import_response(res.get("text") or "")
                    http_ok = bool(res.get("ok"))
                    status_ok = http_ok and parsed_ok
                    status = "sent" if status_ok else "error"
                    message = parsed_msg or (res.get("text") or "").strip() or f"HTTP {res.get('status_code')}"
                    flash(f"{league.name}: {message or 'Offer submitted.'}", "success" if status_ok else "danger")
                except Exception as exc:
                    message = str(exc)
                    flash(f"{league.name}: {message}", "danger")

                queue.pop(0)
                summary.append(
                    {
                        "status": status,
                        "league": league.name,
                        "target": owner_team.name,
                        "message": message or ("Offer submitted." if status == "sent" else ""),
                        "mode": "buy",
                        "give": give_details,
                        "receive": receive_details,
                        "comments": comments,
                    }
                )

            context[queue_key] = queue
            context["summary"] = summary
            session["rapid_trade"] = context
            session.modified = True
            return redirect(url_for("offers.rapid_run"))

        league_mfl_id = str(context.get("league_id") or "")
        league = League.query.filter_by(user_id=current_user.id, mfl_id=league_mfl_id).first()
        my_team_id = context.get("my_team_id")
        my_team = Team.query.get(my_team_id) if my_team_id else None

        if not queue or not league or not my_team:
            session.pop("rapid_trade", None)
            flash("Sell session data was missing. Start over.", "warning")
            return redirect(url_for("offers.rapid_start"))

        target_team_id = int(queue[0])
        target_team = Team.query.get(target_team_id)
        if not target_team or target_team.league_id != league.id:
            queue.pop(0)
            summary.append(
                {
                    "status": "error",
                    "league": league.name,
                    "target": "Unknown",
                    "message": "Target team missing.",
                    "mode": "sell",
                    "give": [],
                    "receive": [],
                    "comments": "",
                }
            )
            context[queue_key] = queue
            context["summary"] = summary
            session["rapid_trade"] = context
            session.modified = True
            flash("Missing target team data. Skipping.", "warning")
            return redirect(url_for("offers.rapid_run"))

        if action == "skip":
            queue.pop(0)
            summary.append(
                {
                    "status": "skipped",
                    "league": league.name,
                    "target": target_team.name,
                    "message": "Skipped",
                    "mode": "sell",
                    "give": [],
                    "receive": [],
                    "comments": "",
                }
            )
            flash(f"Skipped {league.name} — {target_team.name}.", "info")
            context[queue_key] = queue
            context["summary"] = summary
            session["rapid_trade"] = context
            session.modified = True
            return redirect(url_for("offers.rapid_run"))

        give_players_raw = request.form.getlist("give_player")
        give_picks_raw = request.form.getlist("give_pick")
        recv_players_raw = request.form.getlist("receive_player")
        recv_picks_raw = request.form.getlist("receive_pick")

        give_player_ids: List[int] = []
        give_pick_ids: List[int] = []
        recv_player_ids: List[int] = []
        recv_pick_ids: List[int] = []

        for raw in give_players_raw:
            try:
                give_player_ids.append(int(raw))
            except Exception:
                continue
        for raw in give_picks_raw:
            try:
                give_pick_ids.append(int(raw))
            except Exception:
                continue
        for raw in recv_players_raw:
            try:
                recv_player_ids.append(int(raw))
            except Exception:
                continue
        for raw in recv_picks_raw:
            try:
                recv_pick_ids.append(int(raw))
            except Exception:
                continue

        give_player_ids = _validate_player_ids(my_team, give_player_ids)
        give_pick_ids = _validate_pick_ids(my_team, give_pick_ids)
        recv_player_ids = _validate_player_ids(target_team, recv_player_ids)
        recv_pick_ids = _validate_pick_ids(target_team, recv_pick_ids)

        names, records = _league_franchise_maps(league)
        my_player_assets = _asset_map(_player_assets_for_team(my_team))
        my_pick_assets = _asset_map(
            _pick_assets_for_team(my_team, names, records)
        )
        their_player_assets = _asset_map(_player_assets_for_team(target_team))
        their_pick_assets = _asset_map(
            _pick_assets_for_team(target_team, names, records)
        )

        if not give_player_ids and not give_pick_ids:
            flash("Select at least one asset from your side to send.", "warning")
            context[queue_key] = queue
            context["summary"] = summary
            session["rapid_trade"] = context
            session.modified = True
            return redirect(url_for("offers.rapid_run"))

        will_give = [str(pid) for pid in give_player_ids]
        will_give.extend(_pick_tokens_for_team(my_team, give_pick_ids))

        will_receive: List[str] = [str(pid) for pid in recv_player_ids]
        will_receive.extend(_pick_tokens_for_team(target_team, recv_pick_ids))

        comments = (request.form.get("comments") or "").strip()

        give_details: List[Dict[str, Any]] = []
        for pid in give_player_ids:
            give_details.append(_player_detail(my_player_assets.get(pid)))
        for pkid in give_pick_ids:
            give_details.append(_pick_detail(my_pick_assets.get(pkid)))

        receive_details: List[Dict[str, Any]] = []
        for pid in recv_player_ids:
            receive_details.append(_player_detail(their_player_assets.get(pid)))
        for pkid in recv_pick_ids:
            receive_details.append(_pick_detail(their_pick_assets.get(pkid)))

        host, cookie = _resolve_host_and_cookie(league)
        apikey = current_app.config.get("MFL_APIKEY") if current_app else None

        status = "error"
        message = ""
        try:
            res = send_trade_proposal(
                host=host,
                year=league.year,
                league_id=league.mfl_id,
                offered_to=str(target_team.mfl_id).zfill(4),
                will_give_up=will_give,
                will_receive=will_receive,
                comments=comments,
                apikey=apikey,
                cookie=cookie,
            )
            parsed_ok, parsed_msg = parse_mfl_import_response(res.get("text") or "")
            http_ok = bool(res.get("ok"))
            status_ok = http_ok and parsed_ok
            status = "sent" if status_ok else "error"
            message = parsed_msg or (res.get("text") or "").strip() or f"HTTP {res.get('status_code')}"
            flash(f"{league.name}: {message or 'Offer submitted.'}", "success" if status_ok else "danger")
        except Exception as exc:
            message = str(exc)
            flash(f"{league.name}: {message}", "danger")

        queue.pop(0)
        summary.append(
            {
                "status": status,
                "league": league.name,
                "target": target_team.name,
                "message": message or ("Offer submitted." if status == "sent" else ""),
                "mode": "sell",
                "give": give_details,
                "receive": receive_details,
                "comments": comments,
            }
        )

        context[queue_key] = queue
        context["summary"] = summary
        session["rapid_trade"] = context
        session.modified = True
        return redirect(url_for("offers.rapid_run"))

    while queue:
        if mode == "buy":
            league_mfl_id = str(queue[0])
            league = League.query.filter_by(user_id=current_user.id, mfl_id=league_mfl_id).first()
            player_id = int(context.get("player_id") or 0)
            my_team = _get_my_team_in_league(league) if league else None
            owner_team = _team_for_player_in_league(league, player_id) if league else None

            if not league or not my_team or not owner_team:
                queue.pop(0)
                context["summary"] = context.get("summary", []) + [
                    {
                        "status": "error",
                        "league": league.name if league else f"League {league_mfl_id}",
                        "target": owner_team.name if owner_team else "Unknown",
                        "message": "Missing league or roster data.",
                        "mode": "buy",
                        "give": [],
                        "receive": [],
                        "comments": "",
                    }
                ]
                context[queue_key] = queue
                session["rapid_trade"] = context
                session.modified = True
                continue

            names, records = _league_franchise_maps(league)
            my_players = _player_assets_for_team(my_team)
            my_picks = [p for p in _pick_assets_for_team(my_team, names, records) if p.get("usable")]
            their_players = []
            for asset in _player_assets_for_team(owner_team):
                asset_copy = dict(asset)
                asset_copy["is_core"] = asset["id"] == player_id
                their_players.append(asset_copy)
            their_picks = [p for p in _pick_assets_for_team(owner_team, names, records) if p.get("usable")]

            my_record = records.get(str(my_team.mfl_id).zfill(4)) if my_team and my_team.mfl_id else None
            opp_record = (
                records.get(str(owner_team.mfl_id).zfill(4))
                if owner_team and owner_team.mfl_id
                else None
            )

            step_number = context.get("total", len(queue)) - len(queue) + 1
            return render_template(
                "offers/rapid_step.html",
                mode="buy",
                player_id=player_id,
                context=context,
                league=league,
                my_team=my_team,
                counter_team=owner_team,
                my_players=my_players,
                my_picks=my_picks,
                their_players=their_players,
                their_picks=their_picks,
                preset_mode="custom",
                preset_players=set(),
                preset_picks=set(),
                my_team_record=my_record,
                counter_team_record=opp_record,
                step=step_number,
                remaining=len(queue) - 1,
            )

        league_mfl_id = str(context.get("league_id") or "")
        league = League.query.filter_by(user_id=current_user.id, mfl_id=league_mfl_id).first()
        my_team_id = context.get("my_team_id")
        my_team = Team.query.get(my_team_id) if my_team_id else None

        if not league or not my_team:
            session.pop("rapid_trade", None)
            flash("Sell session expired. Start over.", "warning")
            return redirect(url_for("offers.rapid_start"))

        target_team_id = int(queue[0])
        target_team = Team.query.get(target_team_id)
        if not target_team or target_team.league_id != league.id:
            queue.pop(0)
            context["summary"] = context.get("summary", []) + [
                {
                    "status": "error",
                    "league": league.name,
                    "target": "Unknown",
                    "message": "Missing target team data.",
                    "mode": "sell",
                    "give": [],
                    "receive": [],
                    "comments": "",
                }
            ]
            context[queue_key] = queue
            session["rapid_trade"] = context
            session.modified = True
            continue

        names, records = _league_franchise_maps(league)
        my_players = _player_assets_for_team(my_team)
        my_picks = [p for p in _pick_assets_for_team(my_team, names, records) if p.get("usable")]
        their_players = _player_assets_for_team(target_team)
        their_picks = [p for p in _pick_assets_for_team(target_team, names, records) if p.get("usable")]

        my_record = records.get(str(my_team.mfl_id).zfill(4)) if my_team and my_team.mfl_id else None
        opp_record = (
            records.get(str(target_team.mfl_id).zfill(4))
            if target_team and target_team.mfl_id
            else None
        )

        preset_mode = context.get("preset_mode")
        preset_players = set(context.get("preset_players") or [])
        preset_picks = set(context.get("preset_picks") or [])

        step_number = context.get("total", len(queue)) - len(queue) + 1

        return render_template(
            "offers/rapid_step.html",
            mode="sell",
            context=context,
            league=league,
            my_team=my_team,
            counter_team=target_team,
            my_players=my_players,
            my_picks=my_picks,
            their_players=their_players,
            their_picks=their_picks,
            preset_mode=preset_mode,
            preset_players=preset_players,
            preset_picks=preset_picks,
            my_team_record=my_record,
            counter_team_record=opp_record,
            step=step_number,
            remaining=len(queue) - 1,
        )

    return redirect(url_for("offers.rapid_summary"))


@offers_bp.route("/rapid/summary")
@login_required
def rapid_summary():
    context = session.get("rapid_trade") or {}
    summary = context.get("summary", [])
    mode = context.get("mode")
    player = None
    player_id = context.get("player_id")
    if player_id:
        try:
            player = Player.query.get(int(player_id))
        except Exception:
            player = None

    totals = {
        "sent": sum(1 for item in summary if (item or {}).get("status") == "sent"),
        "skipped": sum(1 for item in summary if (item or {}).get("status") == "skipped"),
        "errors": sum(1 for item in summary if (item or {}).get("status") == "error"),
        "total": len(summary),
    }

    session.pop("rapid_trade", None)
    return render_template(
        "offers/rapid_summary.html",
        summary=summary,
        mode=mode,
        player=player,
        totals=totals,
    )


# Import preview/perform routes (confirm screen + real send)
try:
    from .routes_confirm import *  # noqa: F401,F403
except Exception:
    from routes_confirm import *  # type: ignore  # noqa: F401,F403
