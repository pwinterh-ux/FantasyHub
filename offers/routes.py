# offers/routes.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from typing import Dict, List, Tuple, Optional, Any, Set

from flask import Blueprint, render_template, request, session, current_app, redirect, url_for, flash
from flask_login import login_required, current_user

from app import db
from models import League, Team, Roster, DraftPick, Player

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
        def get_today_mass_offer_count(user_id: int, d: date) -> int:
            return 0

        @staticmethod
        def increment_today_mass_offer_count(user_id: int, d: date) -> None:
            return None

        @staticmethod
        def get_bonus_balance(user_id: int) -> int:
            return 0

        @staticmethod
        def use_one_bonus(user_id: int) -> int:
            return 0

        @staticmethod
        def get_weekly_free_used(user_id: int, monday: date) -> bool:
            return False

        @staticmethod
        def mark_weekly_free_used(user_id: int, monday: date) -> None:
            return None

    store = _NoStore()

offers_bp = Blueprint("offers", __name__, url_prefix="/offers")

# ----------------------------- helpers / constants ---------------------------

SYNC_MAX_AGE_HOURS = 4

PRICE_TEMPLATES = [
    # code, label, requirements as {round: count}
    ("2x1st", "Two 1sts", {1: 2}),
    ("1st+2nd", "1st + 2nd", {1: 1, 2: 1}),
    ("1st", "1st", {1: 1}),
    ("early1st", "Early 1st", {1: 1}),
    ("mid1st", "Mid 1st", {1: 1}),
    ("late1st", "Late 1st", {1: 1}),
    ("2x2nd", "Two 2nds", {2: 2}),
    ("2nd", "2nd", {2: 1}),
    ("early2nd", "Early 2nd", {2: 1}),
    ("mid2nd", "Mid 2nd", {2: 1}),
    ("late2nd", "Late 2nd", {2: 1}),
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

PICK_RANGE_BY_TEMPLATE: Dict[str, Tuple[int, str]] = {
    "early1st": (1, "early"),
    "mid1st": (1, "mid"),
    "late1st": (1, "late"),
    "early2nd": (2, "early"),
    "mid2nd": (2, "mid"),
    "late2nd": (2, "late"),
}

SELL_CURRENT_FOR_FUTURE_CODE = "sell_current_for_future"
SELL_CURRENT_PICK_OPTIONS: List[Tuple[str, str]] = [
    ("early1st", "Early 1st"),
    ("mid1st", "Mid 1st"),
    ("late1st", "Late 1st"),
    ("early2nd", "Early 2nd"),
    ("mid2nd", "Mid 2nd"),
    ("late2nd", "Late 2nd"),
    ("3rd", "3rd"),
    ("4th", "4th"),
]
SELL_CURRENT_PICK_LABELS: Dict[str, str] = dict(SELL_CURRENT_PICK_OPTIONS)


def _default_mfl_year() -> int:
    # Flip to 2026 when MFL year rolls
    return 2026


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


def _pick_range_bounds(team_count: int, tier: str) -> Tuple[int, int]:
    third = max(1, team_count // 3)
    early_end = third
    mid_end = max(early_end + 1, min(team_count, third * 2))

    if tier == "early":
        return 1, early_end
    if tier == "mid":
        return early_end + 1, mid_end
    return mid_end + 1, team_count


def _filter_picks_for_template(
    picks_by_round: Dict[int, List[DraftPick]],
    team_count: int,
    template_code: str,
    current_year: int
) -> Dict[int, List[DraftPick]]:
    pick_range = PICK_RANGE_BY_TEMPLATE.get(template_code)
    if not pick_range:
        return picks_by_round

    target_round, tier = pick_range
    start_pick, end_pick = _pick_range_bounds(max(1, team_count), tier)

    filtered = dict(picks_by_round)
    candidates = []
    for dp in picks_by_round.get(target_round, []):
        if dp.pick_number is None:
            continue
        if int(dp.season or 0) != int(current_year):
            continue
        if start_pick <= int(dp.pick_number) <= end_pick:
            candidates.append(dp)

    filtered[target_round] = candidates
    return filtered


def _filter_current_year_pick_sell_give(
    picks_by_round: Dict[int, List[DraftPick]],
    team_count: int,
    give_code: str,
    current_year: int,
) -> List[DraftPick]:
    if give_code in PICK_RANGE_BY_TEMPLATE:
        target_round, tier = PICK_RANGE_BY_TEMPLATE[give_code]
        start_pick, end_pick = _pick_range_bounds(max(1, team_count), tier)
    else:
        mapping = {"3rd": 3, "4th": 4}
        target_round = mapping.get(give_code)
        if not target_round:
            return []
        start_pick, end_pick = 1, max(1, team_count)

    out: List[DraftPick] = []
    for dp in picks_by_round.get(int(target_round), []):
        try:
            season = int(dp.season or 0)
            pn = int(dp.pick_number or 0)
        except Exception:
            continue
        if season != int(current_year):
            continue
        if start_pick <= pn <= end_pick:
            out.append(dp)
    return out


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


def _format_pick_label(dp: DraftPick, franchise_names: dict[str, str] | None = None) -> str:
    """
    Formats draft picks with pick_number when available.

    Examples:
      - '2026 1.05 (orig: Frank)'
      - '2026 R1 (orig: 0003)'
      - '— Pick (orig: —)'
    """
    if not dp:
        return "Pick(?)"

    season = dp.season if dp.season not in (None, "") else "—"

    # round
    rnd: Optional[int]
    try:
        rnd = int(dp.round) if dp.round not in (None, "") else None
    except Exception:
        rnd = None

    # pick number (within round)
    pn: Optional[int]
    try:
        pn = int(dp.pick_number) if dp.pick_number not in (None, "") else None
    except Exception:
        pn = None

    # original team
    orig_raw = (dp.original_team or "").strip() if hasattr(dp, "original_team") else ""
    orig_key = orig_raw.zfill(4) if orig_raw.isdigit() else orig_raw
    orig_name = None
    if franchise_names and orig_key:
        orig_name = franchise_names.get(orig_key)

    # pick part
    if rnd is not None and pn is not None:
        pick_part = f"{rnd}.{str(pn).zfill(2)}"
    elif rnd is not None:
        pick_part = f"R{rnd}"
    else:
        pick_part = "Pick"

    # origin part
    if orig_name:
        origin_part = f"(orig: {orig_name})"
    elif orig_raw:
        origin_part = f"(orig: {orig_raw})"
    else:
        origin_part = "(orig: —)"

    return f"{season} {pick_part} {origin_part}"


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
        is_current_for_future = (request.form.get("sell_current_for_future") or "").strip() == "1"
        player_id = request.form.get("player_id", "").strip()
        mode = (request.form.get("mode") or "buy").lower()
        template_code = request.form.get("template_code") or "2nd"  # default

        if is_current_for_future:
            give_code = (request.form.get("sell_current_pick_code") or "").strip()
            recv_round = (request.form.get("sell_future_round") or "").strip()
            if give_code not in SELL_CURRENT_PICK_LABELS:
                flash("Choose which current-year pick you are selling.", "warning")
                return redirect(url_for("offers.search"))
            try:
                recv_round_i = int(recv_round)
            except Exception:
                recv_round_i = 0
            if recv_round_i not in {1, 2, 3, 4}:
                flash("Choose a valid future round to receive.", "warning")
                return redirect(url_for("offers.search"))
            return redirect(url_for(
                "offers.build",
                mode="sell",
                template_code=SELL_CURRENT_FOR_FUTURE_CODE,
                sell_current_pick_code=give_code,
                sell_future_round=recv_round_i,
            ))

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
        sell_current_pick_options=SELL_CURRENT_PICK_OPTIONS,
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
    sell_current_pick_code = (request.args.get("sell_current_pick_code") or "").strip()
    try:
        sell_future_round = int(request.args.get("sell_future_round") or "0")
    except Exception:
        sell_future_round = 0

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
    is_sell_current_for_future = template_code == SELL_CURRENT_FOR_FUTURE_CODE
    valid_pick_sell = (
        is_sell_current_for_future
        and mode == "sell"
        and sell_current_pick_code in SELL_CURRENT_PICK_LABELS
        and sell_future_round in {1, 2, 3, 4}
    )
    if mode not in {"buy", "sell"} or (
        template_code not in PRICE_INDEX and template_code != "upgrade" and not valid_pick_sell
    ):
        flash("Invalid builder parameters.", "danger")
        return redirect(url_for("offers.search"))
    if not valid_pick_sell and not player_id:
        flash("Invalid builder parameters.", "danger")
        return redirect(url_for("offers.search"))
    if template_code == "upgrade" and (not upgrade_give_round or not upgrade_recv_round):
        flash("Pick Upgrade requires both give/receive rounds.", "warning")
        return redirect(url_for("offers.search"))

    player = Player.query.get(player_id) if player_id else None
    if not is_sell_current_for_future and not player:
        flash("Player not found.", "danger")
        return redirect(url_for("offers.search"))

    req = PRICE_INDEX.get(template_code, {})  # empty for 'upgrade'
    year_now = _default_mfl_year()

    # All leagues for this user/year
    leagues = League.query.filter_by(user_id=current_user.id, year=year_now).all()

    # ---- Precompute team counts per league (fixes lg.franchises_count)
    league_team_counts: Dict[int, int] = {}
    # ---- Global franchise_names map (franchise_id -> name) as a fallback for templates
    franchise_names: Dict[str, str] = {}

    if leagues:
        league_ids = [lg.id for lg in leagues]
        teams_all = Team.query.filter(Team.league_id.in_(league_ids)).all()

        for t in teams_all:
            league_team_counts[t.league_id] = league_team_counts.get(t.league_id, 0) + 1
            if t.mfl_id:
                franchise_names[str(t.mfl_id).zfill(4)] = t.name or str(t.mfl_id)

    def _league_team_count(lg: League) -> int:
        # Primary: precomputed count
        cnt = league_team_counts.get(lg.id)
        if cnt is not None and cnt > 0:
            return cnt
        # Fallback (should be rare)
        try:
            return int(Team.query.filter(Team.league_id == lg.id).count())
        except Exception:
            return 0

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
            picks_by_round = _filter_picks_for_template(
                picks_by_round,
                _league_team_count(lg),
                template_code,
                year_now
            )
            filtered_counts = {rnd: len(lst) for rnd, lst in picks_by_round.items()}
            if not _meets_requirements(filtered_counts, req):
                continue

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
        if template_code == SELL_CURRENT_FOR_FUTURE_CODE:
            for lg in leagues:
                my_team = _get_my_team_in_league(lg)
                if not my_team:
                    continue

                my_picks_by_round = _pick_objects_by_round(my_team)
                give_picks = _filter_current_year_pick_sell_give(
                    my_picks_by_round,
                    _league_team_count(lg),
                    sell_current_pick_code,
                    year_now,
                )
                if not give_picks:
                    continue

                teams = Team.query.filter(Team.league_id == lg.id, Team.id != my_team.id).all()
                buyers_detail: List[Dict[str, Any]] = []
                league_years: Set[int] = set()
                for t in teams:
                    pbr = _pick_objects_by_round(t)
                    recv_picks = [
                        dp for dp in pbr.get(int(sell_future_round), [])
                        if dp.season and int(dp.season) > int(year_now)
                    ]
                    if not recv_picks:
                        continue
                    for dp in recv_picks:
                        try:
                            y = int(dp.season)
                            sell_years_set.add(y)
                            league_years.add(y)
                        except Exception:
                            pass
                    buyers_detail.append({"team": t, "recv_picks": recv_picks})

                if not buyers_detail:
                    continue
                if str(lg.mfl_id) in sent_hide:
                    continue

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
                    "pick_sell": True,
                    "my_give_picks": give_picks,
                    "buyers": buyers_detail,
                    "years": sorted(league_years),
                    "franchise_names": league_fnames,
                    "franchise_records": league_frecords,
                })

        elif template_code != "upgrade":
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
                    pbr = _pick_objects_by_round(t)
                    pbr = _filter_picks_for_template(
                        pbr,
                        _league_team_count(lg),
                        template_code,
                        year_now
                    )
                    filtered_counts = {rnd: len(lst) for rnd, lst in pbr.items()}
                    if _meets_requirements(filtered_counts, req):
                        eligible_buyers.append(t)

                if not eligible_buyers:
                    continue

                if str(lg.mfl_id) in sent_hide:
                    continue

                buyers_detail = []
                league_years: Set[int] = set()
                for t in eligible_buyers:
                    pbr = _pick_objects_by_round(t)
                    pbr = _filter_picks_for_template(
                        pbr,
                        _league_team_count(lg),
                        template_code,
                        year_now
                    )
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
        template_label=(
            f"Sell {SELL_CURRENT_PICK_LABELS.get(sell_current_pick_code, 'Current-Year Pick')} for Future {sell_future_round}{'st' if sell_future_round == 1 else 'nd' if sell_future_round == 2 else 'rd' if sell_future_round == 3 else 'th'}"
            if is_sell_current_for_future
            else PRICE_LABEL.get(template_code, "Pick Upgrade" if template_code == "upgrade" else template_code)
        ),
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
        sell_current_pick_code=sell_current_pick_code,
        sell_future_round=sell_future_round,
        sell_current_for_future_code=SELL_CURRENT_FOR_FUTURE_CODE,
        sell_current_pick_label=SELL_CURRENT_PICK_LABELS.get(sell_current_pick_code, "Current-Year Pick"),
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
            # Support both naming conventions (wired store vs fallback)
            get_today_count=getattr(store, "get_today_count", None) or getattr(store, "get_today_mass_offer_count", None),
            increment_today_count=getattr(store, "increment_today_count", None) or getattr(store, "increment_today_mass_offer_count", None),
            get_bonus_balance=getattr(store, "get_bonus_balance", None),
            use_one_bonus=getattr(store, "use_one_bonus", None),
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

        # per-league franchise id -> name map for pick labels
        league_fnames: Dict[str, str] = {}
        for t in Team.query.filter(Team.league_id == lg.id).all():
            if t.mfl_id:
                league_fnames[str(t.mfl_id).zfill(4)] = t.name or str(t.mfl_id)

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
                "giving": [_format_pick_label(p, league_fnames) for p in chosen_picks],
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
                        # preserve user-chosen ordering from pick_ids
                        chosen_picks.extend([id_to_obj.get(pid) for pid in pick_ids if pid in id_to_obj])

                payload = {
                    "league_id": lg.mfl_id,
                    "offered_by_fid": offered_by.mfl_id,
                    "offered_to_fid": offered_to.mfl_id if offered_to else None,
                    "giving": [f"Player({player_id})"],
                    "getting": [_format_pick_label(p, league_fnames) for p in chosen_picks],
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
