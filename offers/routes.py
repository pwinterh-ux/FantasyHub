from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple, Optional, Any, Set

from flask import Blueprint, render_template, request, session, current_app, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from models import League, Team, Roster, DraftPick, Player

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
    ("2x3rd", "Two 3rds", {3: 2}),
    ("3rd", "3rd", {3: 1}),
    ("4th", "4th", {4: 1}),
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
        # ---------------------------- BUY (unchanged) -------------------------
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
                            # collect available years for preferred-year radios
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
    NOTE: Real submission is handled by the /offers/perform route in routes_confirm.py.
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
                        chosen_picks.extend([id_to_obj.get(pid) for pid in pick_ids if pid in id_to_obj])

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


# Import preview/perform routes (confirm screen + real send)
try:
    from .routes_confirm import *  # noqa: F401,F403
except Exception:
    from routes_confirm import *  # type: ignore  # noqa: F401,F403
