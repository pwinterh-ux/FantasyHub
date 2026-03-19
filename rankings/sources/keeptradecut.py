"""KeepTradeCut positional rankings scraper."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
import re
from typing import Any

import requests

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "RosterDash/1.0 (+https://rosterdash.example)"
SUPPORTED_POSITIONS = {"QB", "RB", "WR", "TE"}
POSITION_PAGE_URLS = {
    "QB": "https://keeptradecut.com/dynasty-rankings/qb-rankings",
    "RB": "https://keeptradecut.com/dynasty-rankings/rb-rankings",
    "WR": "https://keeptradecut.com/dynasty-rankings/wr-rankings",
    "TE": "https://keeptradecut.com/dynasty-rankings/te-rankings",
}


@dataclass(slots=True)
class KeepTradeCutRankRow:
    source: str
    position: str
    source_mfl_id: str | None
    name_raw: str
    name_last_first: str
    team: str | None
    rank: int
    value: int | None


def _to_last_first(full_name: str) -> str:
    s = (full_name or "").strip()
    if not s:
        return ""
    parts = [p for p in s.split() if p]
    if len(parts) == 1:
        return parts[0]
    return f"{parts[-1]}, {' '.join(parts[:-1])}"


def _extract_compact_team_suffix(name_text: str) -> tuple[str, str | None]:
    s = (name_text or "").strip()
    if not s:
        return "", None
    m = re.match(r"^(.*?)([A-Z]{2,4})$", s)
    if not m:
        return s, None
    name_part = (m.group(1) or "").strip()
    team_part = (m.group(2) or "").strip()
    if " " not in name_part:
        return s, None
    return name_part, team_part


def _strip_tags(text: str) -> str:
    if not text:
        return ""
    return unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _coerce_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _extract_next_data_json(html: str) -> dict[str, Any] | None:
    m = re.search(
        r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if not m:
        return None
    blob = (m.group(1) or "").strip()
    if not blob:
        return None
    try:
        parsed = json.loads(blob)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_js_json_array(html: str, var_name: str) -> list[dict[str, Any]]:
    # Example: var playersArray = [{...}, {...}];
    m = re.search(rf"var\s+{re.escape(var_name)}\s*=\s*(\[.*?\]);", html, flags=re.DOTALL)
    if not m:
        return []
    blob = (m.group(1) or "").strip()
    if not blob:
        return []
    try:
        parsed = json.loads(blob)
        if isinstance(parsed, list):
            return [x for x in parsed if isinstance(x, dict)]
    except Exception:
        return []
    return []


def _parse_players_array(html: str, position: str) -> list[KeepTradeCutRankRow]:
    players = _extract_js_json_array(html, "playersArray")
    if not players:
        return []

    # For now, KTC ingestion is pinned to 1QB values by product decision.
    value_key = "oneQBValues"

    out: list[KeepTradeCutRankRow] = []
    for item in players:
        pos = str(item.get("position") or "").upper().strip()
        if pos != position:
            continue

        raw_name = str(item.get("playerName") or item.get("name") or "").strip()
        if not raw_name:
            continue

        team = str(item.get("team") or "").strip().upper() or None

        vals = item.get(value_key) if isinstance(item.get(value_key), dict) else {}
        rank = _coerce_int(vals.get("positionalRank") or vals.get("rank"))
        if rank is None:
            continue

        value = _coerce_int(vals.get("value"))
        mfl_id_raw = item.get("mflid") or item.get("mflId")
        mfl_id = str(mfl_id_raw).strip() if mfl_id_raw not in (None, "") else None

        out.append(
            KeepTradeCutRankRow(
                source="keeptradecut",
                position=position,
                source_mfl_id=mfl_id,
                name_raw=raw_name,
                name_last_first=_to_last_first(raw_name),
                team=team,
                rank=rank,
                value=value,
            )
        )

    dedup: dict[tuple[int, str], KeepTradeCutRankRow] = {}
    for row in out:
        dedup[(row.rank, row.name_raw.lower())] = row
    return sorted(dedup.values(), key=lambda r: r.rank)


def _walk_for_rank_lists(node: Any) -> list[list[dict[str, Any]]]:
    found: list[list[dict[str, Any]]] = []
    if isinstance(node, list):
        if node and all(isinstance(item, dict) for item in node):
            keys = set().union(*[set(item.keys()) for item in node])
            if {"name", "rank"}.issubset(keys) or {"playerName", "rank"}.issubset(keys):
                found.append(node)
        for item in node:
            found.extend(_walk_for_rank_lists(item))
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_walk_for_rank_lists(value))
    return found


def _pick_best_rank_list(candidates: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    if not candidates:
        return []
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def _parse_rows_from_next_data(html: str, position: str) -> list[KeepTradeCutRankRow]:
    data = _extract_next_data_json(html)
    if not data:
        return []

    rank_list = _pick_best_rank_list(_walk_for_rank_lists(data))
    out: list[KeepTradeCutRankRow] = []

    for item in rank_list:
        raw_name = str(item.get("name") or item.get("playerName") or item.get("player") or "").strip()
        if not raw_name:
            continue

        rank = _coerce_int(item.get("rank") or item.get("playerRank") or item.get("posRank"))
        if rank is None:
            continue

        value = _coerce_int(item.get("value") or item.get("tradeValue") or item.get("score"))
        out.append(
            KeepTradeCutRankRow(
                source="keeptradecut",
                position=position,
                source_mfl_id=None,
                name_raw=raw_name,
                name_last_first=_to_last_first(raw_name),
                team=None,
                rank=rank,
                value=value,
            )
        )

    out.sort(key=lambda r: r.rank)
    return out


def _parse_rows_from_dom(html: str, position: str) -> list[KeepTradeCutRankRow]:
    out: list[KeepTradeCutRankRow] = []

    row_blocks = re.findall(
        r'<div[^>]*class=["\'][^"\']*\bonePlayer\b[^"\']*["\'][^>]*>(.*?)</div>\s*</div>',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    if not row_blocks:
        row_blocks = re.findall(
            r'<div[^>]*class=["\'][^"\']*\bonePlayer\b[^"\']*["\'][^>]*>(.*?)</div>',
            html,
            flags=re.DOTALL | re.IGNORECASE,
        )

    for block in row_blocks:
        rank_m = re.search(r'<div[^>]*class=["\'][^"\']*\brank-number\b[^"\']*["\'][^>]*>(.*?)</div>', block, flags=re.DOTALL | re.IGNORECASE)
        name_m = re.search(r'<div[^>]*class=["\'][^"\']*\bplayer-name\b[^"\']*["\'][^>]*>(.*?)</div>', block, flags=re.DOTALL | re.IGNORECASE)
        value_m = re.search(r'<div[^>]*class=["\'][^"\']*\bvalue\b[^"\']*["\'][^>]*>(.*?)</div>', block, flags=re.DOTALL | re.IGNORECASE)

        rank_txt = _strip_tags(rank_m.group(1)) if rank_m else ""
        name_txt = _strip_tags(name_m.group(1)) if name_m else ""
        value_txt = _strip_tags(value_m.group(1)) if value_m else ""

        if "\n" in name_txt:
            name_txt = name_txt.split("\n", 1)[0].strip()
        name_txt = re.sub(r"\s{2,}", " ", name_txt).strip()
        name_txt, team_txt = _extract_compact_team_suffix(name_txt)

        rank = _coerce_int(rank_txt)
        value = _coerce_int(re.sub(r"[^0-9.-]", "", value_txt) if value_txt else None)
        if not name_txt or rank is None:
            continue

        out.append(
            KeepTradeCutRankRow(
                source="keeptradecut",
                position=position,
                source_mfl_id=None,
                name_raw=name_txt,
                name_last_first=_to_last_first(name_txt),
                team=team_txt,
                rank=rank,
                value=value,
            )
        )

    dedup: dict[tuple[int, str], KeepTradeCutRankRow] = {}
    for row in out:
        dedup[(row.rank, row.name_raw.lower())] = row
    return sorted(dedup.values(), key=lambda r: r.rank)


def _parse_rows_from_html(html: str, position: str) -> list[KeepTradeCutRankRow]:
    # 1) Best path: playersArray JS payload (contains full pool + mflid).
    rows = _parse_players_array(html, position)
    if rows:
        return rows

    # 2) Fallback: NEXT_DATA payload
    rows = _parse_rows_from_next_data(html, position)
    if rows:
        return rows

    # 3) Fallback: rendered DOM rows
    return _parse_rows_from_dom(html, position)


def fetch_positional_rankings(
    position: str,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[KeepTradeCutRankRow]:
    pos = str(position).upper().strip()
    if pos not in SUPPORTED_POSITIONS:
        raise ValueError(f"Unsupported position '{position}'. Expected one of {sorted(SUPPORTED_POSITIONS)}")

    url = POSITION_PAGE_URLS[pos]
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    resp = requests.get(url, headers=headers, timeout=timeout_seconds)
    resp.raise_for_status()

    rows = _parse_rows_from_html(resp.text, position=pos)
    if not rows:
        raise RuntimeError(f"KTC parser returned zero rows for {pos} from {url}")
    return rows