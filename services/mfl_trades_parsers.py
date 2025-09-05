# services/mfl_trades_parsers.py
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple


# =========================
# Data containers
# =========================

@dataclass
class TradeSide:
    players: List[int]
    picks: List[Dict[str, object]]  # {"season": int, "round": int, "original_fid": "0001"}

@dataclass
class PendingTrade:
    trade_id: str
    offeringteam: str   # proposer fid
    offeredto: str      # proposee fid
    will_give_up: TradeSide
    will_receive: TradeSide
    comments: str
    description: str
    timestamp: Optional[datetime]
    expires: Optional[datetime]


# =========================
# Helpers
# =========================

def _fid(s: object) -> str:
    """Zero-pad numeric fids to 4 chars; return non-numeric as-is."""
    t = ("" if s is None else str(s)).strip()
    return t.zfill(4) if t.isdigit() else t

def _safe_epoch_to_dt(val: Optional[str]) -> Optional[datetime]:
    if not val:
        return None
    try:
        return datetime.fromtimestamp(int(val), tz=timezone.utc)
    except Exception:
        return None

def _parse_pick_token(tok: str) -> Optional[Tuple[int, int, str]]:
    """
    FP_<orig>_<season>_<round>  ->  (season, round, original_fid)
    """
    parts = tok.split("_")
    if len(parts) >= 4:
        try:
            season = int(parts[2])
            rnd = int(parts[3])
            original = _fid(parts[1])
            return season, rnd, original
        except Exception:
            return None
    return None

def _parse_assets_csv(csv: Optional[str]) -> TradeSide:
    """
    CSV can contain player IDs and FP_ codes:
      e.g. "16584,FP_0006_2026_1,FP_0005_2027_2," (note trailing comma)
    """
    players: List[int] = []
    picks: List[Dict[str, object]] = []
    if not csv:
        return TradeSide(players, picks)

    for raw in csv.split(","):
        tok = raw.strip()
        if not tok:
            continue
        if tok.startswith("FP_"):
            parsed = _parse_pick_token(tok)
            if parsed:
                season, rnd, orig = parsed
                picks.append({"season": season, "round": rnd, "original_fid": orig})
            continue
        try:
            players.append(int(tok))
        except Exception:
            # Unknown token — ignore
            continue

    return TradeSide(players=players, picks=picks)


# =========================
# Public parsers
# =========================

def parse_pending_trades(xml_bytes: bytes) -> List[PendingTrade]:
    """
    Parse MFL export?TYPE=pendingTrades into PendingTrade rows.
    Robust to trailing commas and missing attrs.
    If the payload contains <error>, returns an empty list (caller should surface a warning).
    """
    root = ET.fromstring(xml_bytes)

    # If MFL returns an <error> doc, don't raise here—let the caller decide how to notify the user.
    if root.tag.lower() == "error" or root.find(".//error") is not None:
        return []

    out: List[PendingTrade] = []

    for el in root.findall(".//pendingTrade"):
        trade_id = (el.get("trade_id") or "").strip()
        offeringteam = _fid(el.get("offeringteam"))
        offeredto    = _fid(el.get("offeredto"))

        will_give_up  = _parse_assets_csv(el.get("will_give_up"))
        will_receive  = _parse_assets_csv(el.get("will_receive"))

        comments    = (el.get("comments") or "").strip()
        description = (el.get("description") or "").strip()
        ts = _safe_epoch_to_dt(el.get("timestamp"))
        exp = _safe_epoch_to_dt(el.get("expires"))

        if trade_id and offeringteam and offeredto:
            out.append(
                PendingTrade(
                    trade_id=trade_id,
                    offeringteam=offeringteam,
                    offeredto=offeredto,
                    will_give_up=will_give_up,
                    will_receive=will_receive,
                    comments=comments,
                    description=description,
                    timestamp=ts,
                    expires=exp,
                )
            )

    return out


def normalize_trades_for_template(
    trades: List[PendingTrade],
    *,
    my_fid: str,
    league_id: str,
    league_name: str,
    base_url: str,
    year: int,
    team_name_by_fid: Optional[Dict[str, str]] = None,
) -> List[Dict[str, object]]:
    """
    Convert PendingTrade rows into template-friendly dicts:

    Returns list of dicts with:
      - direction: "received" (offered to you), "sent" (offered by you), or "other"
      - players_out / players_in: lists of player IDs (ints) from *your* perspective
      - picks_out / picks_in: list of {"season","round","original_fid"}
      - from_fid / to_fid + from_team_name / to_team_name
      - view_url: MFL Trades page for the league (no accept/reject in-app)
      - updated_at / expires_at (ISO strings) and comments

    Sorted with received first, then sent, then others.
    """
    my_fid = _fid(my_fid or "")
    rows: List[Dict[str, object]] = []
    team_name_by_fid = team_name_by_fid or {}

    for tr in trades:
        if my_fid and tr.offeredto == my_fid:
            direction = "received"
            players_out = tr.will_give_up.players
            players_in  = tr.will_receive.players
            picks_out   = tr.will_give_up.picks
            picks_in    = tr.will_receive.picks
            other_fid   = tr.offeringteam
            from_fid, to_fid = tr.offeringteam, tr.offeredto
        elif my_fid and tr.offeringteam == my_fid:
            direction = "sent"
            players_out = tr.will_give_up.players
            players_in  = tr.will_receive.players
            picks_out   = tr.will_give_up.picks
            picks_in    = tr.will_receive.picks
            other_fid   = tr.offeredto
            from_fid, to_fid = tr.offeringteam, tr.offeredto
        else:
            direction = "other"
            players_out = tr.will_give_up.players
            players_in  = tr.will_receive.players
            picks_out   = tr.will_give_up.picks
            picks_in    = tr.will_receive.picks
            other_fid   = tr.offeringteam
            from_fid, to_fid = tr.offeringteam, tr.offeredto

        rows.append({
            "league_id": league_id,
            "league_name": league_name,
            "base_url": base_url.rstrip("/"),
            "view_url": f"{base_url.rstrip('/')}/{year}/tradeProposals?L={league_id}",
            "trade_id": tr.trade_id,
            "from_fid": _fid(from_fid),
            "to_fid": _fid(to_fid),
            "from_team_name": team_name_by_fid.get(_fid(from_fid), ""),
            "to_team_name":   team_name_by_fid.get(_fid(to_fid), ""),
            "players_out": players_out,
            "players_in":  players_in,
            "picks_out":   picks_out,
            "picks_in":    picks_in,
            "updated_at":  tr.timestamp.isoformat() if tr.timestamp else None,
            "expires_at":  tr.expires.isoformat() if tr.expires else None,
            "comments":    tr.comments,
            "direction":   direction,
        })

    sort_key = {"received": 0, "sent": 1, "other": 2}
    rows.sort(key=lambda r: (sort_key.get(r["direction"], 9), r["league_name"], r["trade_id"]))
    return rows
