# services/mfl_parsers.py
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional, Any
from urllib.parse import urlparse


# ---------- Data containers returned by parsers -----------------------------
# Picks are normalized to: (season, round, pick_number_1based_or_none, original_team_fid)
# - round is ALWAYS 1-based (round 1 == 1)
# - pick_number (when present) is ALWAYS 1-based (pick 1 == 1, pick 3 == 3)
# - future picks often have no pick_number => None
DraftPickT = Tuple[int, int, Optional[int], Optional[str]]


@dataclass
class FranchiseAssets:
    franchise_id: str
    player_ids: List[int]
    draft_picks: List[DraftPickT]
    faab_balance: Optional[Decimal] = None


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
    draft_picks: List[DraftPickT]
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


def _safe_decimal(x: Any) -> Optional[Decimal]:
    if x in (None, ""):
        return None
    try:
        return Decimal(str(x).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None

def _safe_bool(x: Any) -> Optional[bool]:
    if x in (None, ""):
        return None
    value = str(x).strip().lower()
    if value in {"1", "true", "yes", "y"}:
        return True
    if value in {"0", "false", "no", "n"}:
        return False
    return None

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


def _desc_from(el: ET.Element) -> str:
    """MFL varies: sometimes 'description', sometimes 'desc', sometimes the element text."""
    return (el.get("description") or el.get("desc") or (el.text or "")).strip()


# ---------- Draft pick parsing helpers --------------------------------------

# DP description example: "Year 2026 Draft Pick 1.03"
# IMPORTANT:
# - round is 1-based
# - pick-in-round is 1-based (1.03 => pick_number = 3)
_DP_DESC_RE = re.compile(
    r"Year\s+(?P<year>\d{4}).*?\b(?P<round>\d+)\.(?P<pick>\d+)\b",
    re.IGNORECASE,
)

# DP token example: "DP_0_2" where:
#   round_index=0 => round=1
#   pick_index=2  => pick_number=3 (1-based for storage/display)
_DP_TOKEN_RE = re.compile(r"^DP_(?P<round_idx>\d+)_(?P<pick_idx>\d+)$", re.IGNORECASE)

# FP token example: "FP_0001_2027_1" (future pick with no pick number)
_FP_TOKEN_RE = re.compile(r"^FP_(?P<orig>\d{1,4})_(?P<year>\d{4})_(?P<round>\d+)$", re.IGNORECASE)


def _parse_dp_from_description(desc: str) -> Optional[DraftPickT]:
    """
    Returns (season, round, pick_number_1based, original_team=None) for DP description-style picks.
    """
    if not desc:
        return None
    m = _DP_DESC_RE.search(desc)
    if not m:
        return None
    season = _safe_int(m.group("year"), 0)
    rnd = _safe_int(m.group("round"), 0)
    pick_1based = _safe_int(m.group("pick"), 0)

    # Store pick_number as 1-based (3 for "1.03")
    if season and rnd and pick_1based > 0:
        return (season, rnd, pick_1based, None)
    if season and rnd:
        # If somehow pick isn't present/valid, keep None
        return (season, rnd, None, None)
    return None


def _parse_dp_from_token(token: str, desc: str = "", default_year: Optional[int] = None) -> Optional[DraftPickT]:
    """
    Returns (season, round, pick_number_1based, original_team=None) for DP tokens.

    Uses:
      - token DP_<roundIdx>_<pickIdx> (0-based indices)
      - year from description when available, else default_year when provided

    Storage contract:
      - round is 1-based
      - pick_number is 1-based
    """
    if not token:
        return None

    # Prefer extracting year from description if available
    season = None
    if desc:
        d = _parse_dp_from_description(desc)
        if d:
            season = d[0]

    if season is None and default_year:
        season = int(default_year)

    m = _DP_TOKEN_RE.match(token.strip())
    if not m:
        # If token doesn't parse but description does, use that.
        if desc:
            d = _parse_dp_from_description(desc)
            if d:
                return d
        return None

    rnd_idx = _safe_int(m.group("round_idx"), -1)
    pick_idx = _safe_int(m.group("pick_idx"), -1)
    if rnd_idx < 0 or pick_idx < 0:
        return None

    rnd = rnd_idx + 1
    pick_1based = pick_idx + 1  # IMPORTANT: store 1-based

    if season and rnd:
        return (int(season), int(rnd), int(pick_1based), None)

    # If we still couldn't resolve season, fall back to description parsing
    if desc:
        d = _parse_dp_from_description(desc)
        if d:
            return d

    return None


def _parse_fp_from_token(token: str, desc: str = "") -> Optional[DraftPickT]:
    """
    Returns (season, round, pick_number=None, original_team) for FP style picks.

    IMPORTANT:
    - future picks typically do not include pick-in-round, so pick_number stays None
    """
    if not token:
        return None

    m = _FP_TOKEN_RE.match(token.strip())
    if m:
        orig = _fid(m.group("orig"))
        season = _safe_int(m.group("year"), 0)
        rnd = _safe_int(m.group("round"), 0)
        if season and rnd:
            return (season, rnd, None, orig or None)

    # fallback: sometimes token isn't in FP_* format; try to infer from description
    dp = _parse_dp_from_description(desc or "")
    if dp:
        return dp
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
        waiver_sort_raw = fr.get("waiverSortOrder")
        waiver_sort_order = (
            _safe_int(waiver_sort_raw, 0)
            if waiver_sort_raw not in (None, "")
            else None
        )
        faab_balance = _safe_decimal(fr.get("bbidAvailableBalance"))

        meta[_fid(fid)] = {
            "name": name,
            "owner_name": owner,
            "abbrev": abbr,
            "waiver_sort_order": waiver_sort_order,
            "faab_balance": faab_balance,
        }

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


def parse_league_waiver_settings(xml: bytes) -> dict[str, Any]:
    """Extract nullable waiver configuration from an MFL TYPE=league payload."""
    root = ET.fromstring(xml)
    league_el = root if (root.tag or "").lower() == "league" else root.find(".//league")
    if league_el is None:
        return {}

    attrs = league_el.attrib or {}
    return {
        "waiver_type": (attrs.get("currentWaiverType") or "").strip() or None,
        "faab_starting_balance": _safe_decimal(attrs.get("bbidSeasonLimit")),
        "faab_minimum": _safe_decimal(attrs.get("bbidMinimum")),
        "faab_increment": _safe_decimal(attrs.get("bbidIncrement")),
        "faab_fcfs_charge": _safe_decimal(attrs.get("bbidFCFSCharge")),
        "max_waiver_rounds": (
            _safe_int(attrs.get("maxWaiverRounds"), 0)
            if attrs.get("maxWaiverRounds") not in (None, "")
            else None
        ),
        "bbid_conditional": _safe_bool(attrs.get("bbidConditional")),
    }


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

    ir = (
        root.find(".//injuredReserve")
        or root.find(".//injured_reserve")
        or root.find(".//injuryReserve")
        or root.find(".//injuryreserve")
    )
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

    # Try to infer season default from the payload (best-effort)
    default_year = _safe_int(root.get("year") or root.get("season") or 0, 0) or None

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

        picks: List[DraftPickT] = []

        # 1) currentYearDraftPicks
        curr_el = fr.find("currentYearDraftPicks")
        if curr_el is not None:
            for de in curr_el.findall("draftPick"):
                token = de.get("pick") or ""
                desc = _desc_from(de)
                parsed = None
                if token and str(token).upper().startswith("DP_"):
                    parsed = _parse_dp_from_token(token, desc=desc, default_year=default_year)
                if not parsed:
                    parsed = _parse_dp_from_description(desc)
                if parsed:
                    picks.append(parsed)

        # 2) futureYearDraftPicks
        fut_el = fr.find("futureYearDraftPicks")
        if fut_el is not None:
            for de in fut_el.findall("draftPick"):
                token = de.get("pick", "") or ""
                desc = _desc_from(de)
                parsed = _parse_fp_from_token(token, desc=desc)
                if parsed:
                    picks.append(parsed)

        blind_bidding_el = fr.find(".//blindBiddingDollars")
        faab_balance = (
            _safe_decimal(blind_bidding_el.get("amount"))
            if blind_bidding_el is not None
            else None
        )

        result.append(
            FranchiseAssets(
                franchise_id=fid,
                player_ids=player_ids,
                draft_picks=picks,
                faab_balance=faab_balance,
            )
        )

    return result


# ---------- Fallback parsers ------------------------------------------------

def parse_future_picks_fallback(picks_xml: Optional[bytes]) -> Dict[str, List[DraftPickT]]:
    """
    Older endpoint fallback that only provides future picks.
    We normalize to DraftPickT with pick_number=None.
    """
    result: Dict[str, List[DraftPickT]] = {}
    if not picks_xml:
        return result

    root = ET.fromstring(picks_xml)
    for fr in root.findall(".//franchise"):
        fid = _fid(fr.get("id"))
        if not fid:
            continue
        lst: List[DraftPickT] = []
        for pe in fr.findall(".//futureDraftPick"):
            season = _safe_int(pe.get("year"), 0)
            rnd = _safe_int(pe.get("round"), 0)
            orig = _fid(pe.get("originalPickFor") or pe.get("originalpickfor") or pe.get("original_pick_for") or "")
            if season and rnd and orig:
                lst.append((season, rnd, None, orig))
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
        assets[fid] = FranchiseAssets(franchise_id=fid, player_ids=player_ids, draft_picks=[])

    for fid, picks in picks_by_fid.items():
        fa = assets.get(fid)
        if not fa:
            fa = FranchiseAssets(franchise_id=fid, player_ids=[], draft_picks=[])
            assets[fid] = fa
        normalized: List[DraftPickT] = []
        for season, rnd, pick_no, orig in picks:
            season_i = _safe_int(season, 0)
            rnd_i = _safe_int(rnd, 0)
            orig_s = _fid(orig) if orig else None
            # pick_no is expected None for future picks; if present, treat it as already 1-based
            pn_i = _safe_int(pick_no, 0) if pick_no is not None else None
            if season_i and rnd_i:
                normalized.append((season_i, rnd_i, pn_i if pick_no is not None else None, orig_s))
        fa.draft_picks = normalized

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

def parse_pending_trades(xml_bytes: bytes, *, default_year: Optional[int] = None) -> List[PendingTrade]:
    root = ET.fromstring(xml_bytes)
    out: List[PendingTrade] = []

    # Best-effort season default for DP tokens with no description:
    # 1) caller-supplied default_year (best)
    # 2) root attr year/season (sometimes absent on pendingTrades)
    # 3) None => DP picks without year are ignored (safer than inventing)
    inferred = _safe_int(root.get("year") or root.get("season") or 0, 0) or None
    season_default = int(default_year) if default_year else inferred

    def _parse_asset_tokens(csv: str) -> tuple[List[int], List[DraftPickT]]:
        players: List[int] = []
        picks: List[DraftPickT] = []
        if not csv:
            return players, picks
        for tok in str(csv).split(","):
            tok = tok.strip()
            if not tok:
                continue
            up = tok.upper()
            if up.startswith("FP_"):
                parsed = _parse_fp_from_token(tok, desc="")
                if parsed:
                    picks.append(parsed)
                continue
            if up.startswith("DP_"):
                parsed = _parse_dp_from_token(tok, desc="", default_year=season_default)
                if parsed:
                    picks.append(parsed)
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

                picks: List[DraftPickT] = []

                # Collect pick elements from multiple possible locations.
                pick_els: list[ET.Element] = []
                pick_els.extend(side.findall(".//draftPicks/draftPick"))
                pick_els.extend(side.findall(".//futureDraftPick"))
                pick_els.extend(side.findall(".//draftPick"))  # catches current-year DP_... in some leagues

                # De-dupe
                seen = set()
                uniq: list[ET.Element] = []
                for el in pick_els:
                    oid = id(el)
                    if oid in seen:
                        continue
                    seen.add(oid)
                    uniq.append(el)

                for de in uniq:
                    # Explicit future-pick fields
                    season = _safe_int(de.get("year"), 0)
                    rnd = _safe_int(de.get("round"), 0)
                    orig_raw = de.get("originalPickFor") or de.get("originalpickfor") or de.get("original_pick_for") or ""
                    orig = _fid(orig_raw) if orig_raw not in (None, "") else ""
                    if season and rnd and orig:
                        picks.append((season, rnd, None, orig))
                        continue

                    # Token-based picks
                    pick_token = de.get("pick") or de.get("id") or ""
                    desc = _desc_from(de)
                    if pick_token:
                        up = str(pick_token).upper()
                        if up.startswith("FP_"):
                            parsed = _parse_fp_from_token(str(pick_token), desc=desc)
                            if parsed:
                                picks.append(parsed)
                        elif up.startswith("DP_"):
                            parsed = _parse_dp_from_token(str(pick_token), desc=desc, default_year=season_default)
                            if parsed:
                                picks.append(parsed)
                        else:
                            parsed = _parse_dp_from_description(desc)
                            if parsed:
                                picks.append(parsed)
                    else:
                        parsed = _parse_dp_from_description(desc)
                        if parsed:
                            picks.append(parsed)

                # Some formats include CSV assets
                give_csv = side.get("will_give_up") or side.get("willGiveUp") or ""
                recv_csv = side.get("will_receive") or side.get("willReceive") or ""
                if give_csv or recv_csv:
                    extra_players, extra_picks = _parse_asset_tokens(give_csv)
                    player_ids.extend(extra_players)
                    picks.extend(extra_picks)

                faab: Optional[float] = None
                bb = side.find(".//blindBidDollars")
                if bb is not None:
                    amt = bb.get("amount") or (bb.text or "").strip()
                    try:
                        faab = float(amt)
                    except Exception:
                        faab = None

                sides.append(TradeSide(franchise_id=fid, player_ids=player_ids, draft_picks=picks, faab=faab))

        # (rest unchanged)
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

                picks: List[DraftPickT] = []

                pick_els: list[ET.Element] = []
                pick_els.extend(give.findall(".//draftPicks/draftPick"))
                pick_els.extend(give.findall(".//futureDraftPick"))
                pick_els.extend(give.findall("./draftPick"))
                pick_els.extend(give.findall(".//draftPick"))

                seen = set()
                uniq: list[ET.Element] = []
                for el in pick_els:
                    oid = id(el)
                    if oid in seen:
                        continue
                    seen.add(oid)
                    uniq.append(el)

                for de in uniq:
                    season = _safe_int(de.get("year"), 0)
                    rnd = _safe_int(de.get("round"), 0)
                    orig_raw = de.get("originalPickFor") or de.get("originalpickfor") or de.get("original_pick_for") or ""
                    orig = _fid(orig_raw) if orig_raw not in (None, "") else ""
                    if season and rnd and orig:
                        picks.append((season, rnd, None, orig))
                        continue

                    pick_token = de.get("pick") or de.get("id") or ""
                    desc = _desc_from(de)
                    if pick_token:
                        up = str(pick_token).upper()
                        if up.startswith("FP_"):
                            parsed = _parse_fp_from_token(str(pick_token), desc=desc)
                            if parsed:
                                picks.append(parsed)
                        elif up.startswith("DP_"):
                            parsed = _parse_dp_from_token(str(pick_token), desc=desc, default_year=season_default)
                            if parsed:
                                picks.append(parsed)
                        else:
                            parsed = _parse_dp_from_description(desc)
                            if parsed:
                                picks.append(parsed)
                    else:
                        parsed = _parse_dp_from_description(desc)
                        if parsed:
                            picks.append(parsed)

                # CSV fallback on franchise
                give_csv = fr_side.get("will_give_up") or fr_side.get("willGiveUp") or ""
                recv_csv = fr_side.get("will_receive") or fr_side.get("willReceive") or ""
                if give_csv or recv_csv:
                    extra_players, extra_picks = _parse_asset_tokens(give_csv)
                    player_ids.extend(extra_players)
                    picks.extend(extra_picks)

                faab: Optional[float] = None
                bb = give.find(".//blindBidDollars") or fr_side.find(".//blindBidDollars")
                if bb is not None:
                    amt = bb.get("amount") or (bb.text or "").strip()
                    try:
                        faab = float(amt)
                    except Exception:
                        faab = None

                if player_ids or picks or faab is not None:
                    sides.append(TradeSide(franchise_id=fid, player_ids=player_ids, draft_picks=picks, faab=faab))

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

                sides.append(TradeSide(franchise_id=proposed_by, player_ids=p_players, draft_picks=p_picks, faab=None))
                sides.append(TradeSide(franchise_id=offered_to, player_ids=o_players, draft_picks=o_picks, faab=None))

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
