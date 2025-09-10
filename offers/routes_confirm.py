# offers/routes_confirm.py
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from flask import render_template, request, current_app, flash, redirect, url_for
from flask_login import login_required, current_user

from app import db
from models import League, Team, DraftPick, Player, Roster
from services.mfl_trade import send_trade_proposal

# Attach to the existing offers blueprint
try:
    from .routes import offers_bp  # normal package layout
except Exception:
    from routes import offers_bp  # fallback if colocated in dev


def _now_utc_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _resolve_host_and_cookie(league: League) -> Tuple[str, Optional[str]]:
    """Prefer per-host cookie for league.league_host; fall back to API cookie."""
    host = (league.league_host or "").strip() or "api.myfantasyleague.com"
    cookie = None
    get_host_cookies = getattr(current_user, "get_mfl_host_cookies", None)
    if callable(get_host_cookies):
        host_cookies = get_host_cookies() or {}
        cookie = host_cookies.get(host)
    if not cookie:
        cookie = getattr(current_user, "mfl_cookie_api", None) or getattr(current_user, "session_key", None)
    return host, cookie


def _my_team_for_league(league: League) -> Optional[Team]:
    fid = (league.franchise_id or "").strip()
    if not fid:
        return None
    return Team.query.filter_by(league_id=league.id, mfl_id=str(fid).zfill(4)).first()


def _owner_team_for_player(league: League, player_id: int) -> Optional[Team]:
    return (
        Team.query.join(Roster, Roster.team_id == Team.id)
        .filter(Team.league_id == league.id, Roster.player_id == player_id)
        .first()
    )


def _league_maps(league: League) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Return (fid->name, fid->record) for a league.
    """
    fnames: Dict[str, str] = {}
    frecords: Dict[str, str] = {}
    for t in Team.query.filter(Team.league_id == league.id).all():
        fid = str(t.mfl_id).zfill(4)
        fnames[fid] = t.name or fid
        frecords[fid] = t.record or ""
    return fnames, frecords


def _fmt_pick(token: str, fnames: Dict[str, str], frecords: Dict[str, str]) -> str:
    # FP_<origfid>_<year>_<round>
    try:
        _, orig, yr, rnd = token.split("_")
        nm = fnames.get(orig, orig)
        rc = frecords.get(orig)
        return f"{yr} R{rnd} (orig {nm}{f' ({rc})' if rc else ''})"
    except Exception:
        return token


def _draftpick_to_token(p: DraftPick) -> Optional[str]:
    """Turn a DraftPick row into an MFL token FP_<origfid>_<year>_<round>."""
    try:
        return f"FP_{(p.original_team or '').zfill(4)}_{int(p.season)}_{int(p.round)}"
    except Exception:
        return None


def _draftpick_tokens_from_ids(ids: List[str]) -> List[str]:
    """Turn DraftPick DB ids into MFL tokens FP_<origfid>_<year>_<round>."""
    if not ids:
        return []
    tokens: List[str] = []
    found = DraftPick.query.filter(DraftPick.id.in_(ids)).all()
    for p in found:
        tok = _draftpick_to_token(p)
        if tok:
            tokens.append(tok)
    return tokens


def _extract_buy_picks_for_league(form: Dict[str, Any], lid: str, required_rounds: Dict[int, int]) -> List[str]:
    """
    BUY mode:
      - If required_rounds provided, read pick_{lid}_{rnd} for each required round.
      - If not provided, accept ANY key that starts with pick_{lid}_ (covers all displayed rounds).
    """
    picks: List[str] = []
    if required_rounds:
        for rnd in required_rounds.keys():
            picks.extend(form.getlist(f"pick_{lid}_{rnd}"))
    else:
        prefix = f"pick_{lid}_"
        for key in form.keys():
            if key.startswith(prefix):
                picks.extend(form.getlist(key))
    # de-dup preserve order
    seen = set()
    return [x for x in picks if not (x in seen or seen.add(x))]


def _extract_sell_picks_for_buyer(form: Dict[str, Any], lid: str, buyer_fid_or_id: str, required_rounds: Dict[int, int]) -> List[str]:
    """
    SELL mode:
      - If required_rounds provided, read pick_{lid}_{buyer}_{rnd}.
      - If not provided, accept ANY key that starts with pick_{lid}_{buyer}_.
    """
    picks: List[str] = []
    if required_rounds:
        for rnd in required_rounds.keys():
            picks.extend(form.getlist(f"pick_{lid}_{buyer_fid_or_id}_{rnd}"))
    else:
        prefix = f"pick_{lid}_{buyer_fid_or_id}_"
        for key in form.keys():
            if key.startswith(prefix):
                picks.extend(form.getlist(key))
    seen = set()
    return [x for x in picks if not (x in seen or seen.add(x))]


def _choose_buyer_pick_for_round(buyer: Team, recv_round: int, pref_year: Optional[int]) -> Optional[DraftPick]:
    """Pick exactly one of buyer's picks in the target round. Prefer pref_year; else earliest season."""
    pbr: Dict[int, List[DraftPick]] = {}
    for p in DraftPick.query.filter(DraftPick.team_id == buyer.id).all():
        try:
            r = int(p.round)
        except Exception:
            continue
        pbr.setdefault(r, []).append(p)

    candidates = pbr.get(int(recv_round), []) if recv_round else []
    if not candidates:
        return None

    # try preferred year
    if pref_year is not None:
        for p in candidates:
            try:
                if int(p.season) == int(pref_year):
                    return p
            except Exception:
                pass

    # else earliest by season asc
    try:
        return sorted(candidates, key=lambda x: int(x.season))[0]
    except Exception:
        return candidates[0]


@offers_bp.route("/preview", methods=["POST"])
@login_required
def preview_offers():
    """
    Build a pending-offers JSON from builder POST, then render confirm.html.
    Includes SELL 'upgrade' flow (player + your pick for buyer's pick in a round).
    """
    mode = (request.form.get("mode") or "buy").strip()
    template_code = (request.form.get("template_code") or "").strip()
    pref_year_raw = request.form.get("pref_year")
    try:
        pref_year = int(pref_year_raw) if pref_year_raw else None
    except Exception:
        pref_year = None

    player_id_raw = request.form.get("player_id") or request.form.get("player")
    try:
        player_id = int(player_id_raw) if player_id_raw is not None else None
    except Exception:
        player_id = None

    player = Player.query.get(player_id) if player_id else None

    pending: List[Dict[str, Any]] = []
    count = 0

    if mode == "buy":
        league_ids = request.form.getlist("league_id")
        for lid in league_ids:
            lg = League.query.filter_by(user_id=current_user.id, mfl_id=str(lid)).first()
            if not lg:
                continue
            my_team = _my_team_for_league(lg)
            if not my_team:
                continue
            owner_team = _owner_team_for_player(lg, player_id) if player_id else None
            if not owner_team:
                continue

            pick_ids = _extract_buy_picks_for_league(request.form, str(lid), required_rounds={})
            will_give = _draftpick_tokens_from_ids(pick_ids)
            if not will_give:
                continue

            will_recv = [str(player_id)] if player_id else []
            host = (lg.league_host or "").strip() or "api.myfantasyleague.com"
            fnames, frecords = _league_maps(lg)

            give_names = {tok: _fmt_pick(tok, fnames, frecords) for tok in will_give}
            recv_names = {}
            if player:
                recv_names[str(player_id)] = player.name

            pending.append({
                "host": host,
                "league_id": lg.mfl_id,
                "league_name": lg.name,
                "year": lg.year,
                "offered_by_fid": my_team.mfl_id,
                "offered_by_name": fnames.get(str(my_team.mfl_id).zfill(4), str(my_team.mfl_id)),
                "offered_by_record": frecords.get(str(my_team.mfl_id).zfill(4), ""),
                "offered_to_fid": owner_team.mfl_id,
                "offered_to_name": fnames.get(str(owner_team.mfl_id).zfill(4), str(owner_team.mfl_id)),
                "offered_to_record": frecords.get(str(owner_team.mfl_id).zfill(4), ""),
                "will_give_up": will_give,
                "will_receive": will_recv,
                "will_give_up_names": give_names,
                "will_receive_names": recv_names,
                "expires_unix": _now_utc_ts() + 7*24*3600,
            })
            count += 1

    elif mode == "sell" and template_code != "upgrade":
        # Enforce exactly one league selected via buyer_* keys
        lids: List[str] = []
        for key in request.form.keys():
            m = re.match(r"buyer_(\d+)$", key)
            if m:
                lids.append(m.group(1))
        lids = list(dict.fromkeys(lids))
        if len(lids) != 1:
            flash("Please select exactly one league for SELL offers.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code=template_code))

        lid = lids[0]
        lg = League.query.filter_by(user_id=current_user.id, mfl_id=str(lid)).first()
        if not lg:
            flash("League not found.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code=template_code))
        my_team = _my_team_for_league(lg)
        if not my_team:
            flash("Your franchise in this league isn’t set.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code=template_code))

        buyer_team_ids = request.form.getlist(f"buyer_{lid}")
        if not buyer_team_ids:
            flash("Select at least one buyer team.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code=template_code))

        fnames, frecords = _league_maps(lg)
        host = (lg.league_host or "").strip() or "api.myfantasyleague.com"

        for bt in buyer_team_ids:
            buyer_team = Team.query.get(bt)
            if not buyer_team or buyer_team.league_id != lg.id:
                continue

            pick_ids = _extract_sell_picks_for_buyer(request.form, str(lid), str(buyer_team.id), required_rounds={})
            will_recv = _draftpick_tokens_from_ids(pick_ids)
            if not will_recv:
                continue

            will_give = [str(player_id)] if player_id else []
            give_names = {str(player_id): player.name} if player else {}
            recv_names = {tok: _fmt_pick(tok, fnames, frecords) for tok in will_recv}

            pending.append({
                "host": host,
                "league_id": lg.mfl_id,
                "league_name": lg.name,
                "year": lg.year,
                "offered_by_fid": my_team.mfl_id,
                "offered_by_name": fnames.get(str(my_team.mfl_id).zfill(4), str(my_team.mfl_id)),
                "offered_by_record": frecords.get(str(my_team.mfl_id).zfill(4), ""),
                "offered_to_fid": buyer_team.mfl_id,
                "offered_to_name": fnames.get(str(buyer_team.mfl_id).zfill(4), str(buyer_team.mfl_id)),
                "offered_to_record": frecords.get(str(buyer_team.mfl_id).zfill(4), ""),
                "will_give_up": will_give,
                "will_receive": will_recv,
                "will_give_up_names": give_names,
                "will_receive_names": recv_names,
                "expires_unix": _now_utc_ts() + 7*24*3600,
            })
            count += 1

    elif mode == "sell" and template_code == "upgrade":
        # --- SELL: Pick Upgrade ---
        # Get upgrade params
        try:
            recv_round = int(request.form.get("upgrade_recv_round") or "0")
            give_round = int(request.form.get("upgrade_give_round") or "0")
        except Exception:
            recv_round = 0
            give_round = 0
        if not (recv_round and give_round):
            flash("Pick Upgrade needs both Give and Receive rounds.", "warning")
            return redirect(url_for("offers.search"))

        # Enforce exactly one league via buyer_* keys
        lids: List[str] = []
        for key in request.form.keys():
            m = re.match(r"buyer_(\d+)$", key)
            if m:
                lids.append(m.group(1))
        lids = list(dict.fromkeys(lids))
        if len(lids) != 1:
            flash("Please select exactly one league for Pick Upgrade.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))

        lid = lids[0]
        lg = League.query.filter_by(user_id=current_user.id, mfl_id=str(lid)).first()
        if not lg:
            flash("League not found.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))
        my_team = _my_team_for_league(lg)
        if not my_team:
            flash("Your franchise in this league isn’t set.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))

        # Your selected pick (required)
        my_pick_id = request.form.get(f"upgrade_my_pick_{lid}")
        if not my_pick_id:
            flash("Select one of your picks to include.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))
        my_pick_row = DraftPick.query.get(my_pick_id)
        if not my_pick_row or my_pick_row.team_id != my_team.id or int(my_pick_row.round) != int(give_round):
            flash("Invalid pick selection.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))

        my_pick_token = _draftpick_to_token(my_pick_row)
        if not my_pick_token:
            flash("Could not encode your pick.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))

        buyer_team_ids = request.form.getlist(f"buyer_{lid}")
        if not buyer_team_ids:
            flash("Select at least one buyer team.", "warning")
            return redirect(url_for("offers.build", player_id=player_id, mode="sell", template_code="upgrade",
                                    upgrade_give_round=give_round, upgrade_recv_round=recv_round))

        fnames, frecords = _league_maps(lg)
        host = (lg.league_host or "").strip() or "api.myfantasyleague.com"

        for bt in buyer_team_ids:
            buyer_team = Team.query.get(bt)
            if not buyer_team or buyer_team.league_id != lg.id:
                continue

            # pick one receive-round pick for this buyer (prefer preferred year; else earliest)
            chosen = _choose_buyer_pick_for_round(buyer_team, recv_round, pref_year)
            if not chosen:
                # Per your directive, you don't expect this case; skip silently if it occurs.
                continue
            buyer_token = _draftpick_to_token(chosen)
            if not buyer_token:
                continue

            will_give = [str(player_id)] if player_id else []
            if my_pick_token:
                will_give.append(my_pick_token)

            will_recv = [buyer_token]

            give_names = {}
            if player:
                give_names[str(player_id)] = player.name
            if my_pick_token:
                give_names[my_pick_token] = _fmt_pick(my_pick_token, fnames, frecords)
            recv_names = {buyer_token: _fmt_pick(buyer_token, fnames, frecords)}

            pending.append({
                "host": host,
                "league_id": lg.mfl_id,
                "league_name": lg.name,
                "year": lg.year,
                "offered_by_fid": my_team.mfl_id,
                "offered_by_name": fnames.get(str(my_team.mfl_id).zfill(4), str(my_team.mfl_id)),
                "offered_by_record": frecords.get(str(my_team.mfl_id).zfill(4), ""),
                "offered_to_fid": buyer_team.mfl_id,
                "offered_to_name": fnames.get(str(buyer_team.mfl_id).zfill(4), str(buyer_team.mfl_id)),
                "offered_to_record": frecords.get(str(buyer_team.mfl_id).zfill(4), ""),
                "will_give_up": will_give,
                "will_receive": will_recv,
                "will_give_up_names": give_names,
                "will_receive_names": recv_names,
                "expires_unix": _now_utc_ts() + 7*24*3600,
                "comments": "",  # optional
            })
            count += 1

    else:
        flash("Unknown mode.", "warning")
        return redirect(url_for("offers.search"))

    if not pending:
        flash("No valid offers to preview.", "warning")
        return redirect(url_for("offers.search"))

    return render_template(
        "offers/confirm.html",
        count=count,
        player=player,
        template_label=template_code,
        pending_json=json.dumps(pending),
    )


@offers_bp.route("/perform", methods=["POST"])
@login_required
def perform_offers():
    """
    Read `pending_json` from confirm.html and send real proposals via MFL import.
    Renders offers/send_result.html with a per-league result log.
    """
    raw = request.form.get("pending_json") or "[]"
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("pending_json must be an array")
    except Exception:
        flash("Malformed request. Please rebuild your offers.", "warning")
        return redirect(url_for("offers.search"))

    apikey = None
    try:
        apikey = current_app.config.get("MFL_APIKEY")
    except Exception:
        apikey = None

    offers_log: List[Dict[str, Any]] = []
    for it in items:
        lid = str(it.get("league_id") or "").strip()
        lg = League.query.filter_by(user_id=current_user.id, mfl_id=lid).first()
        if not lg:
            offers_log.append({"league": {"name": "(unknown)", "mfl_id": lid}, "status": "error", "detail": "League not found for current user."})
            continue

        host, cookie = _resolve_host_and_cookie(lg)
        try:
            res = send_trade_proposal(
                host=host,
                year=it.get("year") or lg.year,
                league_id=lid,
                offered_to=str(it.get("offered_to_fid") or "").zfill(4),
                will_give_up=it.get("will_give_up") or [],
                will_receive=it.get("will_receive") or [],
                comments=it.get("comments") or "",
                expires_ts=int(it.get("expires_unix") or (_now_utc_ts() + 7*24*3600)),
                apikey=apikey,
                cookie=cookie,
            )
            detail = {
                "host": host,
                "http": res.get("status_code"),
                "url": res.get("url"),
                "body": (res.get("text") or "")[:400],
            }
            status = "ok" if res.get("ok") else "error"
        except Exception as e:
            status = "error"
            detail = str(e)

        offers_log.append({"league": lg, "status": status, "detail": detail})

    return render_template(
        "offers/send_result.html",
        mode=None,
        template_code=None,
        player_id=None,
        offers_log=offers_log,
    )
