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


# (compat)
@dataclass
class FranchiseMeta:
    name: Optional[str] = None
    owner_name: Optional[str] = None


# ---------- Trades (pending/open only) --------------------------------------

@dataclass
class TradeSide:
    franchise_id: str
    player_ids: List[int]
    future_picks: List[Tuple[int, int, str]]
    faab: Optional[float]


@dataclass
class PendingTrade:
    trade_id: str
    franchises: List[str]
    sides: List[TradeSide]
    created_ts: Optional[str]
    expires_ts: Optional[str]
    status: str
    comments: List[Dict[str, str]]
    proposed_by: Optional[str] = None
    offered_to: Optional[str] = None


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
    out: List[dict] = []
    root = ET.fromstring(xml_bytes)

    for lg in root.findall(".//league"):
        lid = lg.get("id") or lg.get("league_id") or ""
        name = lg.get("name") or "Unnamed League"
        year_str = lg.get("year") or lg.get("season") or "0"

        fid = lg.get("franchise_id") or lg.get("franchiseId")
        if not fid:
            fr = lg.find(".//franchise")
            if fr is not None:
                fid = fr.get("id")

        host = _host_only(lg.get("url") or lg.get("host") or None)

        try:
            year = int(year_str)
        except ValueError:
            year = 0

        if lid:
            out.append({"id": lid, "name": name, "year": year, "franchise_id": fid, "host": host})

    return out


# ---------- League info (franchise names/owners + lineup + IR) --------------

def parse_league_info(xml: bytes) -> tuple[dict[str, dict], str | None, str | None, Optional[int]]:
    """
    Returns:
      - franchise meta map
      - lineup/roster string
      - baseURL (host)
      - ir_slots_max (int|None)
    """
    root = ET.fromstring(xml)

    # DEBUG preview (first ~300 chars); harmless in prod logs
    preview = (xml[:300].decode("utf-8", "ignore") if isinstance(xml, (bytes, bytearray)) else str(xml)[:300])
    print(f"[parse_league_info] XML preview (first 300): {preview}\n")

    # --- IMPORTANT: pick the *root* league element when present
    if (root.tag or "").lower() == "league":
        league_el = root
    else:
        # fall back to first descendant (older approach)
        league_el = root.find(".//league")

    # Extra debug so we can confirm which node we grabbed
    if league_el is not None:
        print(f"[parse_league_info] league attrs: {dict(league_el.attrib)}")
    else:
        print("[parse_league_info] league element NOT FOUND")

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

    # 2) lineup
    lineup_str = _extract_lineup_string(root)

    # 3) IR slots + other league attrs (read from league tag attributes directly)
    raw_ir = raw_bbid_limit = raw_last_reg = raw_waiver_type = None
    if league_el is not None:
        attrs = dict(league_el.attrib or {})
        print(f"[parse_league_info] league attrs: {attrs}")

        raw_ir = attrs.get("injuredReserve") or attrs.get("injured_reserve") or attrs.get("injuryReserve")
        raw_bbid_limit = attrs.get("bbidSeasonLimit")
        raw_last_reg = attrs.get("lastRegularSeasonWeek")
        raw_waiver_type = attrs.get("currentWaiverType")

    ir_slots_max = None
    if raw_ir not in (None, ""):
        try:
            ir_slots_max = int(str(raw_ir).strip())
        except Exception:
            ir_slots_max = None

    print(
        f"[parse_league_info] raw fields | injuredReserve={repr(raw_ir)} | "
        f"bbidSeasonLimit={repr(raw_bbid_limit)} | lastRegularSeasonWeek={repr(raw_last_reg)} | "
        f"currentWaiverType={repr(raw_waiver_type)}"
    )
    print(f"[parse_league_info] parsed ir_slots_max={ir_slots_max} (from raw={repr(raw_ir)})")

    return meta, lineup_str, base_url, ir_slots_max


def _extract_lineup_string(root: ET.Element) -> Optional[str]:
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

    positions = []
    for pos in root.findall(".//starters/position"):
        pname = (pos.get("name") or "").strip()
        if not pname:
            continue
        limit_attr = (pos.get("limit") or "").strip()
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


# ---------- IR slots fallback helpers (kept as-is) --------------------------

def _extract_ir_slots(root: ET.Element) -> Optional[int]:
    # (unused now but kept for compatibility)
    def _try_int(val: Any) -> Optional[int]:
        if val is None:
            return None
        try:
            return int(str(val).strip())
        except Exception:
            return None

    league_el = root.find(".//league")
    if league_el is not None:
        attr_map = {(k or "").lower(): v for k, v in (league_el.attrib or {}).items()}
        for key in ("injuredreserve", "injured_reserve", "injuryreserve"):
            iv = _try_int(attr_map.get(key))
            if iv is not None:
                return iv

    pos = root.find(".//rosterPositions/position[@name='IR']") or root.find(".//rosterPositions/position[@id='IR']")
    if pos is not None:
        for key in ("limit", "max", "count"):
            iv = _try_int(pos.get(key))
            if iv is not None:
                return iv

    pos = root.find(".//rosterLimits/position[@name='IR']") or root.find(".//rosterLimits/position[@id='IR']")
    if pos is not None:
        for key in ("limit", "max", "count"):
            iv = _try_int(pos.get(key))
            if iv is not None:
                return iv

    ir = root.find(".//injuredReserve") or root.find(".//injured_reserve") or root.find(".//injuryReserve") or root.find(".//injuryreserve")
    if ir is not None:
        for key in ("limit", "max", "count"):
            iv = _try_int(ir.get(key))
            if iv is not None:
                return iv

    return None


# ---------- League assets ----------------------------------------------------

def parse_assets(xml_bytes: bytes) -> List[FranchiseAssets]:
    root = ET.fromstring(xml_bytes)
    result: List[FranchiseAssets] = []

    for fr in root.findall(".//franchise"):
        fid = fr.get("id")
        if not fid:
            continue
        fid = _fid(fid)

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


# ---------- Fallback parsers ------------------------------------------------

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

    picks_by_fid = parse_future_picks_fallback(picks_xml) if picks_xml else {}

    assets: Dict[str, FranchiseAssets] = {}

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


# ---------- League standings -------------------------------------------------

def parse_standings(xml_bytes: bytes) -> List[StandingRow]:
    root = ET.fromstring(xml_bytes)
    rows: List[StandingRow] = []

    for rank, fr in enumerate(root.findall(".//franchise"), start=1):
        fid = fr.get("id")
        if not fid:
            continue
        record = fr.get("h2hwlt")
        if not record:
            wins = fr.get("h2hw")
            losses = fr.get("h2hl")
            ties = fr.get("h2ht")
            if any(v not in (None, "") for v in (wins, losses, ties)):
                record = f"{_safe_int(wins)}-{_safe_int(losses)}-{_safe_int(ties)}"
        if not record:
            record = "0-0-0"
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


# ---------- Pending trades ---------------------------------------------------

def parse_pending_trades(xml_bytes: bytes) -> List[PendingTrade]:
    root = ET.fromstring(xml_bytes)
    out: List[PendingTrade] = []

    def _parse_asset_tokens(csv: str) -> tuple[List[int], List[Tuple[int, int, str]]]:
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
                if len(parts) >= 4:
                    orig = _fid(parts[1])
                    season = _safe_int(parts[2], 0)
                    rnd = _safe_int(parts[3], 0)
                    if season and rnd and orig:
                        picks.append((season, rnd, orig))
                continue
            try:
                players.append(int(tok))
            except Exception:
                pass
        return players, picks

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

        fids: Dict[str, None] = {}
        for fe in tr.findall("./franchise"):
            fid = _fid(fe.get("id"))
            if fid:
                fids[fid] = None

        sides: List[TradeSide] = []

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

        if not sides:
            proposer = _fid(tr.get("offeringteam") or tr.get("offeringTeam") or "")
            offeree = _fid(tr.get("offeredto") or tr.get("offeredTo") or "")
            give_csv = tr.get("will_give_up") or tr.get("willGiveUp") or ""
            recv_csv = tr.get("will_receive") or tr.get("willReceive") or ""

            if proposer or proposed_by:
                proposed_by = proposed_by or proposer
            if offeree or offered_to:
                offered_to = offered_to or offeree

            if proposed_by and offered_to:
                fids[proposed_by] = None
                fids[offered_to] = None

                p_players, p_picks = _parse_asset_tokens(give_csv)
                o_players, o_picks = _parse_asset_tokens(recv_csv)

                sides.append(TradeSide(franchise_id=proposed_by, player_ids=p_players, future_picks=p_picks, faab=None))
                sides.append(TradeSide(franchise_id=offered_to, player_ids=o_players, future_picks=o_picks, faab=None))

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
