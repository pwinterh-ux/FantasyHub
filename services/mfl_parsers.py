# services/mfl_parsers.py
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional, Any
from urllib.parse import urlparse


# ---------- Data containers returned by parsers -----------------------------

@dataclass
class FranchiseAssets:
    franchise_id: str
    player_ids: List[int]
    future_picks: List[Tuple[int, int, str]]  # (season, round, original_team)


@dataclass
class StandingRow:
    franchise_id: str
    record: str
    pf: float
    pa: float
    rank: int


# (Kept for compatibility if other code imports it; not required by sync now.)
@dataclass
class FranchiseMeta:
    name: Optional[str] = None
    owner_name: Optional[str] = None


# ---------- Trades (pending/open only) --------------------------------------

@dataclass
class TradeSide:
    franchise_id: str
    player_ids: List[int]
    future_picks: List[Tuple[int, int, str]]  # (season, round, original_team)
    faab: Optional[float]  # blind bid dollars, if present


@dataclass
class PendingTrade:
    trade_id: str
    franchises: List[str]             # all franchises involved
    sides: List[TradeSide]            # one entry per franchise (what they will GIVE)
    created_ts: Optional[str]         # timestamp (epoch as string) if present
    expires_ts: Optional[str]         # expiration timestamp if present
    status: str                       # usually "pending"
    comments: List[Dict[str, str]]    # [{"franchise":"0001","date":"...","text":"..."}]
    proposed_by: Optional[str] = None # proposer franchise id
    offered_to: Optional[str] = None  # offeree franchise id (when exposed)


# ---------- Small helpers ---------------------------------------------------

def _safe_int(x: Any, default: int = 0) -> int:
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return default


def _fid(x: Any) -> str:
    s = str(x or "").strip()
    return s.zfill(4) if s.isdigit() else s


def _host_only(url: str | None) -> str | None:
    if not url:
        return None
    try:
        u = urlparse(url)
        if u.netloc:
            return u.netloc
        return url.replace("https://", "").replace("http://", "").split("/", 1)[0]
    except Exception:
        return None


# ---------- User leagues (discovery) ----------------------------------------

def parse_user_leagues(xml_bytes: bytes) -> List[dict]:
    """
    Parse a user-leagues XML payload into a list of dicts:
      { "id": <league_id>, "name": <league_name>, "year": <int>, "franchise_id": <str|None>, "host": <str|None> }
    """
    out: List[dict] = []
    root = ET.fromstring(xml_bytes)

    for lg in root.findall(".//league"):
        lid = lg.get("id") or lg.get("league_id") or ""
        name = lg.get("name") or "Unnamed League"
        year_str = lg.get("year") or lg.get("season") or "0"

        # Preferred franchise id (attribute), or nested fallback
        fid = lg.get("franchise_id") or lg.get("franchiseId")
        if not fid:
            fr = lg.find(".//franchise")
            if fr is not None:
                fid = fr.get("id")

        # Host (from `url` if present)
        host = _host_only(lg.get("url") or lg.get("host") or None)

        try:
            year = int(year_str)
        except ValueError:
            year = 0

        if lid:
            out.append({"id": lid, "name": name, "year": year, "franchise_id": fid, "host": host})

    return out


# ---------- League info (franchise names/owners + optional lineup) ----------

def parse_league_info(xml: bytes) -> tuple[dict[str, dict], str | None, str | None]:
    """
    Returns:
      - franchise meta map: { "0001": {"name": "...", "owner_name": "...", "abbrev": "..."} , ...}
      - lineup/roster string e.g. "QB:1,RB:2-4,WR:3-5,TE:1-3"
      - baseURL e.g. "https://www43.myfantasyleague.com"
    """
    root = ET.fromstring(xml)
    league_el = root.find(".//league")
    base_url = (league_el.get("baseURL").strip() if league_el is not None and league_el.get("baseURL") else None)

    # 1) franchises
    meta: dict[str, dict] = {}
    for fr in root.findall(".//franchise"):
        fid = (fr.get("id") or fr.get("franchise_id") or "").strip()
        if not fid:
            continue
        name = (fr.get("name") or "").strip()
        owner = (fr.get("owner_name") or fr.get("ownerName") or "").strip()
        abbr = (fr.get("abbrev") or fr.get("abbreviation") or "").strip()
        meta[_fid(fid)] = {"name": name, "owner_name": owner, "abbrev": abbr}

    # 2) lineup string (best effort)
    lineup_str = _extract_lineup_string(root)

    return meta, lineup_str, base_url


def _extract_lineup_string(root: ET.Element) -> Optional[str]:
    # Read total starters from <starters count="...">
    total_count = None
    starters_el = root.find(".//starters")
    if starters_el is not None:
        for attr in ("count", "total", "lineupCount", "numStarters"):
            v = starters_el.get(attr)
            if v:
                try:
                    total_count = int(v)
                    break
                except Exception:
                    pass

    # Only read positions under <starters> (ignore <rosterLimits>)
    positions = []
    for pos in root.findall(".//starters/position"):
        pname = (pos.get("name") or "").strip()
        if not pname:
            continue
        limit_attr = (pos.get("limit") or "").strip()  # handles "1-8" or "2"
        minv = (pos.get("min") or pos.get("minStarters") or "").strip()
        maxv = (pos.get("max") or "").strip()

        if minv and maxv and minv != maxv:
            val = f"{minv}-{maxv}"
        elif limit_attr:
            val = limit_attr
        elif minv:
            val = minv
        elif maxv:
            val = maxv
        else:
            val = "1"

        positions.append(f"{pname}:{val}")

    text = ",".join(positions) if positions else None

    # Fallback: if a league returns only a flat list of starter positions
    if not text and starters_el is not None:
        names = [(el.text or "").strip() for el in starters_el.findall(".//position")]
        names = [n for n in names if n]
        if names:
            counts: Dict[str, int] = {}
            for n in names:
                counts[n] = counts.get(n, 0) + 1
            text = ",".join(f"{k}:{v}" for k, v in counts.items())

    if text and total_count:
        return f"{total_count}:{text}"
    return text

# ---------- League assets (rosters + future picks in one call) --------------

def parse_assets(xml_bytes: bytes) -> List[FranchiseAssets]:
    root = ET.fromstring(xml_bytes)
    result: List[FranchiseAssets] = []

    for fr in root.findall(".//franchise"):
        fid = fr.get("id")
        if not fid:
            continue
        fid = _fid(fid)

        # Players
        player_ids: List[int] = []
        players_el = fr.find("players")
        if players_el is not None:
            for pe in players_el.findall("player"):
                pid = pe.get("id")
                if not pid:
                    continue
                try:
                    player_ids.append(int(pid))
                except ValueError:
                    continue

        # Future draft picks
        picks: List[Tuple[int, int, str]] = []
        picks_el = fr.find("futureYearDraftPicks")
        if picks_el is not None:
            for de in picks_el.findall("draftPick"):
                pick_str = de.get("pick", "")
                parts = pick_str.split("_")
                if len(parts) >= 4:
                    orig = _fid(parts[1])
                    season = _safe_int(parts[2], 0)
                    rnd = _safe_int(parts[3], 0)
                    if season and rnd:
                        picks.append((season, rnd, orig))

        result.append(FranchiseAssets(franchise_id=fid, player_ids=player_ids, future_picks=picks))

    return result


# ---------- Fallback parsers (when assets is blocked) -----------------------

def parse_future_picks_fallback(picks_xml: Optional[bytes]) -> Dict[str, List[Tuple[int, int, str]]]:
    result: Dict[str, List[Tuple[int, int, str]]] = {}
    if not picks_xml:
        return result

    root = ET.fromstring(picks_xml)
    for fr in root.findall(".//franchise"):
        fid = _fid(fr.get("id"))
        if not fid:
            continue
        lst: List[Tuple[int, int, str]] = []
        for pe in fr.findall(".//futureDraftPick"):
            season = _safe_int(pe.get("year"), 0)
            rnd = _safe_int(pe.get("round"), 0)
            orig = _fid(pe.get("originalPickFor") or pe.get("originalpickfor") or pe.get("original_pick_for") or "")
            if season and rnd and orig:
                lst.append((season, rnd, orig))
        result[fid] = lst
    return result


def parse_rosters_fallback(rosters_xml: bytes, picks_xml: Optional[bytes] = None) -> List[FranchiseAssets]:
    root = ET.fromstring(rosters_xml)

    # Pre-parse picks if provided
    picks_by_fid = parse_future_picks_fallback(picks_xml) if picks_xml else {}

    assets: Dict[str, FranchiseAssets] = {}

    # Players from <rosters>
    for fr in root.findall(".//franchise"):
        fid = _fid(fr.get("id"))
        if not fid:
            continue
        player_ids: List[int] = []
        for pe in fr.findall(".//player"):
            pid = pe.get("id")
            if pid:
                try:
                    player_ids.append(int(pid))
                except Exception:
                    continue
        assets[fid] = FranchiseAssets(franchise_id=fid, player_ids=player_ids, future_picks=[])

    # Attach picks (if any). Ensure franchises that only appear in picks are included too.
    for fid, picks in picks_by_fid.items():
        fa = assets.get(fid)
        if not fa:
            fa = FranchiseAssets(franchise_id=fid, player_ids=[], future_picks=[])
            assets[fid] = fa
        normalized: List[Tuple[int, int, str]] = []
        for season, rnd, orig in picks:
            season_i = _safe_int(season, 0)
            rnd_i = _safe_int(rnd, 0)
            orig_s = _fid(orig)
            if season_i and rnd_i and orig_s:
                normalized.append((season_i, rnd_i, orig_s))
        fa.future_picks = normalized

    return [assets[k] for k in sorted(assets.keys())]


# ---------- League standings (record, PF/PA, rank) --------------------------

def parse_standings(xml_bytes: bytes) -> List[StandingRow]:
    root = ET.fromstring(xml_bytes)
    rows: List[StandingRow] = []

    for rank, fr in enumerate(root.findall(".//franchise"), start=1):
        fid = fr.get("id")
        if not fid:
            continue
        record = fr.get("h2hwlt") or "0-0-0"
        try:
            pf = float(fr.get("pf", 0))
        except ValueError:
            pf = 0.0
        try:
            pa = float(fr.get("pa", 0))
        except ValueError:
            pa = 0.0

        rows.append(StandingRow(franchise_id=_fid(fid), record=record, pf=pf, pa=pa, rank=rank))

    return rows


# ---------- Pending trades (open only via export?TYPE=pendingTrades) --------

def parse_pending_trades(xml_bytes: bytes) -> List[PendingTrade]:
    """
    Parse export?TYPE=pendingTrades into a list of PendingTrade objects (open only).

    Supports shapes:
      A) <trade><offer><franchise id="...">...</franchise></offer></trade>
      B) <trade><franchise id="..."><willGive>...</willGive></franchise>...</trade>
      C) <pendingTrade offeringteam="0008" offeredto="0001"
           will_give_up="123,FP_0001_2026_2," will_receive="456,..."/>
    """
    root = ET.fromstring(xml_bytes)
    out: List[PendingTrade] = []

    def _parse_asset_tokens(csv: str) -> tuple[List[int], List[Tuple[int, int, str]]]:
        """Return (player_ids, picks) from a CSV like '15261,FP_0010_2026_2,'."""
        players: List[int] = []
        picks: List[Tuple[int, int, str]] = []
        if not csv:
            return players, picks
        for tok in str(csv).split(","):
            tok = tok.strip()
            if not tok:
                continue
            if tok.upper().startswith("FP_"):
                parts = tok.split("_")
                # FP_<orig_fid>_<year>_<round>
                if len(parts) >= 4:
                    orig = _fid(parts[1])
                    season = _safe_int(parts[2], 0)
                    rnd = _safe_int(parts[3], 0)
                    if season and rnd and orig:
                        picks.append((season, rnd, orig))
                continue
            # else: assume player id
            try:
                players.append(int(tok))
            except Exception:
                pass
        return players, picks

    # Node names vary: <trade> or <pendingTrade>
    trade_nodes = list(root.findall(".//trade")) + list(root.findall(".//pendingTrade"))

    for tr in trade_nodes:
        status = (tr.get("status") or "pending").lower()
        if status in {"completed", "accepted", "processed", "rejected", "declined", "cancelled", "canceled"}:
            continue

        trade_id = tr.get("id") or tr.get("trade_id") or ""
        created_ts = tr.get("timestamp") or tr.get("date") or tr.get("created") or None
        expires_ts = tr.get("willExpire") or tr.get("expires") or tr.get("expiration") or None
        proposed_by = _fid(
            tr.get("proposedBy") or tr.get("proposer") or tr.get("initiatedBy") or tr.get("proposingFranchise") or ""
        ) or None
        offered_to = _fid(tr.get("offeredto") or tr.get("offeredTo") or "") or None

        # Collect all franchises mentioned at the top level
        fids: Dict[str, None] = {}
        for fe in tr.findall("./franchise"):
            fid = _fid(fe.get("id"))
            if fid:
                fids[fid] = None

        sides: List[TradeSide] = []

        # -------- Shape A: <offer><franchise>…</franchise></offer> ----------
        offer = tr.find("./offer") or tr.find("./offers")
        if offer is not None:
            for side in offer.findall("./franchise"):
                fid = _fid(side.get("id"))
                if not fid:
                    continue
                fids[fid] = None

                player_ids: List[int] = []
                for pe in side.findall(".//players/player"):
                    pid = pe.get("id")
                    if pid:
                        try:
                            player_ids.append(int(pid))
                        except Exception:
                            pass

                picks: List[Tuple[int, int, str]] = []
                for de in side.findall(".//draftPicks/draftPick") + side.findall(".//futureDraftPick"):
                    season = _safe_int(de.get("year"), 0)
                    rnd = _safe_int(de.get("round"), 0)
                    orig = _fid(de.get("originalPickFor") or de.get("originalpickfor") or de.get("original_pick_for") or "")
                    if not (season and rnd and orig):
                        pick_token = de.get("pick")
                        if pick_token:
                            parts = str(pick_token).split("_")
                            if len(parts) >= 4:
                                orig = _fid(parts[1])
                                season = _safe_int(parts[2], 0)
                                rnd = _safe_int(parts[3], 0)
                    if season and rnd and orig:
                        picks.append((season, rnd, orig))

                faab: Optional[float] = None
                bb = side.find(".//blindBidDollars")
                if bb is not None:
                    amt = bb.get("amount") or (bb.text or "").strip()
                    try:
                        faab = float(amt)
                    except Exception:
                        faab = None

                sides.append(TradeSide(franchise_id=fid, player_ids=player_ids, future_picks=picks, faab=faab))

        # -------- Shape B: <franchise><willGive>…</willGive></franchise> ----
        if not sides and offer is None:
            for fr_side in tr.findall("./franchise"):
                fid = _fid(fr_side.get("id"))
                if not fid:
                    continue
                fids[fid] = None

                give = (
                    fr_side.find("./willGive")
                    or fr_side.find("./give")
                    or fr_side.find("./giving")
                    or fr_side.find("./offer")
                    or fr_side
                )

                player_ids: List[int] = []
                for pe in give.findall(".//players/player") + give.findall("./player"):
                    pid = pe.get("id")
                    if pid:
                        try:
                            player_ids.append(int(pid))
                        except Exception:
                            pass

                picks: List[Tuple[int, int, str]] = []
                for de in (
                    give.findall(".//draftPicks/draftPick")
                    + give.findall(".//futureDraftPick")
                    + give.findall("./draftPick")
                ):
                    season = _safe_int(de.get("year"), 0)
                    rnd = _safe_int(de.get("round"), 0)
                    orig = _fid(de.get("originalPickFor") or de.get("originalpickfor") or de.get("original_pick_for") or "")
                    if not (season and rnd and orig):
                        pick_token = de.get("pick")
                        if pick_token:
                            parts = str(pick_token).split("_")
                            if len(parts) >= 4:
                                orig = _fid(parts[1])
                                season = _safe_int(parts[2], 0)
                                rnd = _safe_int(parts[3], 0)
                    if season and rnd and orig:
                        picks.append((season, rnd, orig))

                faab: Optional[float] = None
                bb = give.find(".//blindBidDollars") or fr_side.find(".//blindBidDollars")
                if bb is not None:
                    amt = bb.get("amount") or (bb.text or "").strip()
                    try:
                        faab = float(amt)
                    except Exception:
                        faab = None

                if player_ids or picks or faab is not None:
                    sides.append(TradeSide(franchise_id=fid, player_ids=player_ids, future_picks=picks, faab=faab))

        # -------- Shape C: attribute-only variant ---------------------------
        if not sides:
            proposer = _fid(tr.get("offeringteam") or tr.get("offeringTeam") or "")
            offeree  = _fid(tr.get("offeredto") or tr.get("offeredTo") or "")
            give_csv = tr.get("will_give_up") or tr.get("willGiveUp") or ""
            recv_csv = tr.get("will_receive") or tr.get("willReceive") or ""

            if proposer or proposed_by:
                proposed_by = proposed_by or proposer
            if offeree or offered_to:
                offered_to = offered_to or offeree

            if proposed_by and offered_to:
                fids[proposed_by] = None
                fids[offered_to] = None

                # what proposer gives vs receives
                p_players, p_picks = _parse_asset_tokens(give_csv)  # proposer gives
                o_players, o_picks = _parse_asset_tokens(recv_csv)  # proposer receives => offeree gives

                sides.append(TradeSide(franchise_id=proposed_by, player_ids=p_players, future_picks=p_picks, faab=None))
                sides.append(TradeSide(franchise_id=offered_to,  player_ids=o_players, future_picks=o_picks, faab=None))

        # comments (optional)
        comments: List[Dict[str, str]] = []
        com_block = tr.find("./comments")
        if com_block is not None:
            for ce in com_block.findall(".//comment"):
                comments.append(
                    {
                        "franchise": _fid(ce.get("franchise") or ce.get("fid") or ""),
                        "date": ce.get("date") or ce.get("timestamp") or "",
                        "text": (ce.text or "").strip(),
                    }
                )
        attr_comment = tr.get("comments")
        if attr_comment:
            comments.append({"franchise": "", "date": "", "text": attr_comment})

        out.append(
            PendingTrade(
                trade_id=trade_id or "",
                franchises=sorted(fids.keys()),
                sides=sides,
                created_ts=created_ts,
                expires_ts=expires_ts,
                status=status or "pending",
                comments=comments,
                proposed_by=proposed_by or None,
                offered_to=offered_to or None,
            )
        )

    return out
