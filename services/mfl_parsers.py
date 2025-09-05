# services/mfl_parsers.py
from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Tuple, Dict, Optional


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


@dataclass
class FranchiseMeta:
    name: Optional[str] = None
    owner_name: Optional[str] = None


# ---------- Small helpers ----------------------------------------------------

def _to_int(val, default=0) -> int:
    try:
        return int(val)
    except Exception:
        try:
            return int(float(val))
        except Exception:
            return default

def _split_csv(s: str) -> List[str]:
    return [tok.strip() for tok in str(s).split(",") if tok and tok.strip()]

def _parse_pick_token(pick_str: str) -> Optional[Tuple[int, int, str]]:
    """
    Accepts tokens like 'FP_0002_2026_1' and returns (season, round, original_team).
    """
    parts = str(pick_str).strip().split("_")
    if len(parts) >= 4:
        orig = parts[1]
        try:
            season = int(parts[2])
            rnd = int(parts[3])
        except Exception:
            return None
        return (season, rnd, orig)
    return None


# ---------- User leagues (discovery) ----------------------------------------

def parse_user_leagues(xml_bytes: bytes) -> List[dict]:
    """
    Parse a user-leagues XML payload into a list of dicts:
      { "id": <league_id>, "name": <league_name>, "year": <int>, "franchise_id": <str|None> }

    Tolerant to variants:
      - <league id="..." name="..." year="..." franchise_id="0006" />
      - <league id="..." ...><franchise id="0006"/></league>
      - Some feeds use league_id / season attribute names.
    """
    root = ET.fromstring(xml_bytes)
    if root.tag.lower() == "error":
        return []

    out: List[dict] = []
    for lg in root.findall(".//league"):
        lid = lg.get("id") or lg.get("league_id") or ""
        if not lid:
            continue
        name = lg.get("name") or "Unnamed League"
        year_str = lg.get("year") or lg.get("season") or "0"

        # Prefer attribute franchise id if present
        fid = lg.get("franchise_id") or lg.get("franchiseId")

        # Fallback: nested <franchise id="...">
        if not fid:
            fr = lg.find(".//franchise")
            if fr is not None:
                fid = fr.get("id")

        try:
            year = int(year_str)
        except ValueError:
            year = 0

        out.append({"id": lid, "name": name, "year": year, "franchise_id": fid})

    return out


# ---------- League info (franchise names/owners + optional lineup) ----------

def parse_league_info(xml: bytes) -> tuple[Dict[str, FranchiseMeta], Optional[str], Optional[str]]:
    """
    Returns:
      (franchise_meta_map, roster_slots_text, base_url)

    - franchise_meta_map: { "0001": FranchiseMeta(name=..., owner_name=...), ... }
    - roster_slots_text:  best-effort like 'QB:1,RB:2,WR:3,TE:1' (or None)
    - base_url:           e.g. 'https://www45.myfantasyleague.com' (or None)
    """
    root = ET.fromstring(xml)
    if root.tag.lower() == "error":
        return {}, None, None

    # base URL for league host (lets you switch clients)
    league_el = root.find(".//league")
    base_url = (league_el.get("baseURL").strip()
                if league_el is not None and league_el.get("baseURL")
                else None)

    # 1) franchises -> FranchiseMeta dataclasses
    meta: Dict[str, FranchiseMeta] = {}
    for fr in root.findall(".//franchise"):
        fid = (fr.get("id") or fr.get("franchise_id") or "").strip()
        if not fid:
            continue
        name = (fr.get("name") or "").strip()
        owner = (fr.get("owner_name") or fr.get("ownerName") or "").strip()
        meta[fid] = FranchiseMeta(name=name or None, owner_name=owner or None)

    # 2) lineup string (best effort)
    lineup_str = _extract_lineup_string(root)

    return meta, lineup_str, base_url


def _extract_lineup_string(root: ET.Element) -> Optional[str]:
    """
    Build a concise starters string like 'QB:1,RB:2-4,WR:3-5,TE:1'.
    Strategy:
      1) Prefer explicit lineup sections ('positionRules' / 'rosterRequirements')
      2) Fall back to generic './/position' but de-dupe and drop 0/0 entries
      3) If none found, try 'starterPositions' counted frequency
    """
    def collect_positions(nodes: List[ET.Element]) -> Dict[str, str]:
        seen: Dict[str, str] = {}
        for pos in nodes:
            pname = (pos.get("name") or pos.get("position") or "").strip()
            if not pname:
                continue
            minv = (pos.get("min") or pos.get("minStarters") or "").strip()
            maxv = (pos.get("max") or pos.get("limit") or pos.get("required") or pos.get("count") or "").strip()

            # Normalize value to 'X' or 'X-Y'
            if minv and maxv and minv != maxv:
                val = f"{minv}-{maxv}"
            elif maxv:
                val = maxv
            elif minv:
                val = minv
            else:
                val = "1"

            # Skip obvious non-starters like '0' or '0-0'
            if val in {"0", "0-0"}:
                continue

            # De-dupe: prefer non-zero values; don't overwrite a good value with a worse one
            prev = seen.get(pname)
            if not prev:
                seen[pname] = val
            else:
                # keep the more permissive/explicit range
                if prev in {"0", "0-0"} and val not in {"0", "0-0"}:
                    seen[pname] = val
                elif "-" in val and "-" not in prev:
                    seen[pname] = val
        return seen

    # 1) Prefer scoped sections if present
    blocks = []
    pr = root.find(".//positionRules")
    if pr is not None:
        blocks.append(pr.findall(".//position"))
    rr = root.find(".//rosterRequirements")
    if rr is not None:
        blocks.append(rr.findall(".//position"))

    for nodes in blocks:
        pos_map = collect_positions(nodes)
        if pos_map:
            return ",".join(f"{k}:{v}" for k, v in pos_map.items())

    # 2) Fallback: any './/position', with de-dupe
    any_positions = root.findall(".//position")
    pos_map = collect_positions(any_positions)
    if pos_map:
        return ",".join(f"{k}:{v}" for k, v in pos_map.items())

    # 3) Last resort: count repeated starter slots
    starter_block = root.find(".//starterPositions") or root.find(".//starters")
    if starter_block is not None:
        names = [el.text.strip() for el in starter_block.findall(".//position") if (el.text or "").strip()]
        counts: Dict[str, int] = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        if counts:
            return ",".join(f"{k}:{v}" for k, v in counts.items())

    return None

# ---------- League assets (rosters + future picks in one call) --------------

def parse_assets(xml_bytes: bytes) -> List[FranchiseAssets]:
    """
    Parse the 'assets' export (players + future picks) into FranchiseAssets rows.
    Tolerates empty lists and returns [] on <error>.
    """
    root = ET.fromstring(xml_bytes)
    if root.tag.lower() == "error":
        return []

    result: List[FranchiseAssets] = []

    for fr in root.findall(".//franchise"):
        fid = fr.get("id")
        if not fid:
            continue

        # Players: support nested <players><player id="..."/></players>
        #          (some sites may also put <player id="..."/> directly under <franchise>)
        player_ids: List[int] = []
        players_el = fr.find("players")
        if players_el is not None:
            player_nodes = players_el.findall("player")
        else:
            player_nodes = fr.findall("player")

        for pe in player_nodes:
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
                token = de.get("pick", "")
                parsed = _parse_pick_token(token)
                if parsed:
                    picks.append(parsed)

        result.append(FranchiseAssets(franchise_id=fid, player_ids=player_ids, future_picks=picks))

    return result


# ---------- Rosters fallback (players only) ----------------------------------

def parse_rosters(xml_bytes: bytes) -> List[FranchiseAssets]:
    """
    Parse the 'rosters' export into FranchiseAssets (players only, no picks).
    Handles a few shapes:
      - <franchise id="0001"><player id="..."/><player id="..."/></franchise>
      - <franchise id="0001"><players><player id="..."/></players></franchise>
      - <franchise id="0001" players="1,2,3"> (CSV attribute, seen on some feeds)
      - <franchise id="0001" player="1,2,3">  (singular attribute variant)
    Returns [] on <error>.
    """
    root = ET.fromstring(xml_bytes)
    if root.tag.lower() == "error":
        return []

    out: List[FranchiseAssets] = []

    for fr in root.findall(".//franchise"):
        fid = fr.get("id")
        if not fid:
            continue

        player_ids: List[int] = []

        # A) nested nodes
        players_parent = fr.find("players")
        nodes = []
        if players_parent is not None:
            nodes = players_parent.findall("player")
        else:
            nodes = fr.findall("player")

        for pe in nodes:
            pid = pe.get("id")
            if pid:
                try:
                    player_ids.append(int(pid))
                except Exception:
                    pass

        # B) CSV attribute fallback (players="1,2,3" or player="1,2,3")
        if not player_ids:
            csv_attr = fr.get("players") or fr.get("player")
            if csv_attr:
                for tok in _split_csv(csv_attr):
                    try:
                        player_ids.append(int(tok))
                    except Exception:
                        pass

        out.append(FranchiseAssets(franchise_id=fid, player_ids=player_ids, future_picks=[]))

    return out


# ---------- Future picks fallback (picks only) --------------------------------

def parse_future_picks(xml_bytes: bytes) -> Dict[str, List[Tuple[int, int, str]]]:
    """
    Parse the 'futureDraftPicks' export into a dict:
      { "0001": [(2026, 1, "0002"), ...], ... }
    Returns {} on <error>.
    """
    root = ET.fromstring(xml_bytes)
    if root.tag.lower() == "error":
        return {}

    out: Dict[str, List[Tuple[int, int, str]]] = {}

    for fr in root.findall(".//franchise"):
        fid = fr.get("id")
        if not fid:
            continue
        picks: List[Tuple[int, int, str]] = []
        # Common shape: <futureYearDraftPicks><draftPick pick="FP_0002_2026_1"/></...>
        picks_el = fr.find("futureYearDraftPicks")
        nodes = []
        if picks_el is not None:
            nodes = picks_el.findall("draftPick")
        else:
            # Some feeds may place <draftPick .../> directly under <franchise>
            nodes = fr.findall("draftPick")

        for dp in nodes:
            token = dp.get("pick", "")
            parsed = _parse_pick_token(token)
            if parsed:
                picks.append(parsed)

        # Extremely rare variants:
        # <pick>FP_0002_2026_1</pick> or attribute 'picks="FP_... , FP_..."'
        if not picks:
            for dp in fr.findall("pick"):
                token = (dp.text or "").strip()
                parsed = _parse_pick_token(token)
                if parsed:
                    picks.append(parsed)
        if not picks:
            inline = fr.get("picks")
            if inline:
                for tok in _split_csv(inline):
                    parsed = _parse_pick_token(tok)
                    if parsed:
                        picks.append(parsed)

        out[fid] = picks

    return out


# ---------- League standings (record, PF/PA, rank) --------------------------

def parse_standings(xml_bytes: bytes) -> List[StandingRow]:
    root = ET.fromstring(xml_bytes)
    if root.tag.lower() == "error":
        return []

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

        rows.append(StandingRow(franchise_id=fid, record=record, pf=pf, pa=pa, rank=rank))

    return rows
