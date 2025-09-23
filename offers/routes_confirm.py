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
from services.mfl_trade import send_trade_proposal, parse_mfl_import_response

# NEW: terms + mass-offer gating
from services.guards import require_terms, consume_mass_offer
from services.store import (
    get_today_count,
    increment_today_count,
    get_bonus_balance,
    use_one_bonus,
    get_weekly_free_used,
    mark_weekly_free_used,
)

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
    """
    Turn DraftPick DB ids into tokens, preserving the *input order* of ids.
    """
    if not ids:
        return []
    found = DraftPick.query.filter(DraftPick.id.in_(ids)).all()
    by_id = {str(p.id): p for p in found}
    tokens: List[str] = []
    for pid in ids:
        p = by_id.get(str(pid))
        if not p:
            continue
        tok = _draftpick_to_token(p)
        if tok:
            tokens.append(tok)
    return tokens


def _extract_buy_picks_for_league(form: Dict[str, Any], lid: str) -> List[str]:
    """
    BUY mode:
      Accept ANY key that starts with pick_{lid}_ (covers all displayed rounds).
      Return DraftPick ids in the order they appear in the form.
    """
    picks: List[str] = []
    prefix = f"pick_{lid}_"
    for key in form.keys():
        if key.startswith(prefix):
            picks.extend(form.getlist(key))
    # de-dup preserve order
    seen = set()
    return [x for x in picks if not (x in seen or seen.add(x))]


def _extract_sell_picks_for_buyer(form: Dict[str, Any], lid: str, buyer_fid_or_id: str) -> List[str]:
    """
    SELL (standard or upgrade):
      Accept ANY key that starts with pick_{lid}_{buyer}_ (covers all displayed rounds).
      Return DraftPick ids in the order they appear in the form.
    """
    picks: List[str] = []
    prefix = f"pick_{lid}_{buyer_fid_or_id}_"
    for key in form.keys():
        if key.startswith(prefix):
            picks.extend(form.getlist(key))
    seen = set()
    return [x for x in picks if not (x in seen or seen.add(x))]


@offers_bp.route("/preview", methods=["POST"])
@login_required
def preview_offers():
    """
    Build a pending-offers JSON from builder POST, then render confirm.html.

    STRICT behavior for all templates:
      - Use exactly the posted checkbox/radio values (no auto-selection, no Preferred-Year logic).
      - If a selected league/buyer has no picks checked, it is skipped for preview.
    """
    mode = (request.form.get("mode") or "buy").strip()
    template_code = (request.form.get("template_code") or "").strip()

    # Player (if present in template)
    player_id_raw = request.form.get("player_id") or request.form.get("player")
    try:
        player_id = int(player_id_raw) if player_id_raw is not None else None
    except Exception:
        player_id = None
    player = Player.query.get(player_id) if player_id else None

    pending: List[Dict[str, Any]] = []
    count = 0

    if mode == "buy":
        skipped: List[str] = []

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

            # STRICT: posted checkboxes only
            pick_ids = _extract_buy_picks_for_league(request.form, str(lid))
            will_give = _draftpick_tokens_from_ids(pick_ids)

            if not will_give:
                skipped.append(f"{lg.name} (ID {lg.mfl_id})")
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

        if skipped:
            flash(f"Skipped (no picks selected): {', '.join(skipped)}", "info")

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

        skipped_buyers: List[str] = []

        for bt in buyer_team_ids:
            buyer_team = Team.query.get(bt)
            if not buyer_team or buyer_team.league_id != lg.id:
                continue

            # STRICT: posted checkboxes only
            pick_ids = _extract_sell_picks_for_buyer(request.form, str(lid), str(buyer_team.id))
            will_recv = _draftpick_tokens_from_ids(pick_ids)

            if not will_recv:
                skipped_buyers.append(buyer_team.name or f"FID {buyer_team.mfl_id}")
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

        if skipped_buyers:
            flash(f"Skipped buyer(s) with no picks selected: {', '.join(skipped_buyers)}", "info")

    elif mode == "sell" and template_code == "upgrade":
        # --- SELL: Pick Upgrade (now also uses posted buyer pick checkboxes) ---
        try:
            recv_round = int(request.form.get("upgrade_recv_round") or "0")
            give_round = int(request.form.get("upgrade_give_round") or "0")
        except Exception:
            recv_round = 0
            give_round = 0
        if not (recv_round and give_round):
            flash("Pick Upgrade needs both Give and Receive rounds.", "warning")
            return redirect(url_for("offers.search"))

        # Exactly one league via buyer_* keys (your flow requirement)
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

        # Your selected pick (radio required)
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

        def _draftpick_to_token_local(p: DraftPick) -> Optional[str]:
            try:
                return f"FP_{(p.original_team or '').zfill(4)}_{int(p.season)}_{int(p.round)}"
            except Exception:
                return None

        my_pick_token = _draftpick_to_token_local(my_pick_row)
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

        skipped_buyers: List[str] = []

        for bt in buyer_team_ids:
            buyer_team = Team.query.get(bt)
            if not buyer_team or buyer_team.league_id != lg.id:
                continue

            # STRICT: posted buyer-pick checkboxes only (no auto-choosing)
            pick_ids = _extract_sell_picks_for_buyer(request.form, str(lid), str(buyer_team.id))
            will_recv = _draftpick_tokens_from_ids(pick_ids)

            if not will_recv:
                skipped_buyers.append(buyer_team.name or f"FID {buyer_team.mfl_id}")
                continue

            will_give = [str(player_id)] if player_id else []
            will_give.append(my_pick_token)

            give_names = {}
            if player:
                give_names[str(player_id)] = player.name
            give_names[my_pick_token] = _fmt_pick(my_pick_token, fnames, frecords)
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
                "comments": "",
            })
            count += 1

        if skipped_buyers:
            flash(f"Upgrade: skipped buyer(s) with no picks selected: {', '.join(skipped_buyers)}", "info")

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
@require_terms   # NEW: must accept ToS/Privacy/AUP before sending real proposals
def perform_offers():
    """
    Read `pending_json` from confirm.html and send real proposals via MFL import.
    Enforces mass-offer gating (daily cap / weekly free / bonus).
    """
    raw = request.form.get("pending_json") or "[]"
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            raise ValueError("pending_json must be an array")
    except Exception:
        flash("Malformed request. Please rebuild your offers.", "warning")
        return redirect(url_for("offers.search"))

    # -------- Mass-offer gate: count this as ONE action regardless of N items --------
    recipients_count = len(items)  # for messaging only; cap consumption is 1 per perform
    ok, msg = consume_mass_offer(
        user=current_user,
        recipients_count=recipients_count,
        get_today_count=get_today_count,
        increment_today_count=increment_today_count,
        get_bonus_balance=get_bonus_balance,
        use_one_bonus=use_one_bonus,
        get_weekly_free_used=get_weekly_free_used,
        mark_weekly_free_used=mark_weekly_free_used,
    )
    if not ok:
        flash(msg or "Your plan doesn’t allow more mass offers today.", "warning")
        return redirect(url_for("offers.search"))

    # ------------------------------------------------------------------------
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
        status_msg = ""
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
            body_text = res.get("text") or ""
            http_ok = bool(res.get("ok"))
            parsed_ok, parsed_msg = parse_mfl_import_response(body_text)
            status_ok = http_ok and parsed_ok
            status_msg = parsed_msg.strip() if isinstance(parsed_msg, str) else ""
            if not status_msg:
                status_msg = body_text.strip()
            if not status_msg and not status_ok:
                status_msg = f"HTTP {res.get('status_code')}"
            detail = {
                "host": host,
                "http": res.get("status_code"),
                "url": res.get("url"),
                "body": (res.get("text") or "")[:400],
            }
            if status_msg:
                detail["status_message"] = status_msg
            status = "ok" if status_ok else "error"
        except Exception as e:
            status = "error"
            detail = str(e)
            status_msg = str(e)

        offers_log.append(
            {
                "league": lg,
                "status": status,
                "detail": detail,
                "status_message": status_msg if status != "ok" else "",
            }
        )

    return render_template(
        "offers/send_result.html",
        mode=None,
        template_code=None,
        player_id=None,
        offers_log=offers_log,
    )
