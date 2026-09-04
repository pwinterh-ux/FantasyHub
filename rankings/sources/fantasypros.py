"""FantasyPros dynasty rankings parser (overall page)."""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import json
import re

import requests

from rankings.sources.fantasycalc import normalize_name_for_matching

DEFAULT_TIMEOUT_SECONDS = 20
DEFAULT_USER_AGENT = "RosterDash/1.0 (+https://rosterdash.example)"
SUPPORTED_POSITIONS = {"QB", "RB", "WR", "TE"}
FANTASYPROS_DYNASTY_OVERALL_URL = "https://www.fantasypros.com/nfl/rankings/dynasty-overall.php"


@dataclass(slots=True)
class FantasyProsRankRow:
    source: str
    source_mfl_id: str | None
    name_raw: str
    name_normalized: str
    position: str
    team: str | None
    rank: int
    value: float | None


def _strip_tags(text: str) -> str:
    if not text:
        return ""
    return unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _extract_rows(table_html: str) -> list[list[str]]:
    row_blocks = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, flags=re.IGNORECASE | re.DOTALL)
    rows: list[list[str]] = []
    for row_html in row_blocks:
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row_html, flags=re.IGNORECASE | re.DOTALL)
        cleaned = [_strip_tags(c) for c in cells]
        if cleaned:
            rows.append(cleaned)
    return rows


def _column_index(headers: list[str], patterns: list[str]) -> int | None:
    normalized = [re.sub(r"\s+", " ", h.lower()).strip() for h in headers]
    for i, h in enumerate(normalized):
        for pattern in patterns:
            if re.search(pattern, h):
                return i
    return None


def _parse_name_and_team(name_text: str, team_text: str | None) -> tuple[str, str | None]:
    raw = (name_text or "").strip()
    team = (team_text or "").strip().upper() or None

    # "Name (TEAM)"
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", raw)
    if m:
        raw = m.group(1).strip()
        if not team:
            team = m.group(2).strip().upper()

    # "Name - TEAM" or em-dash
    m = re.match(r"^(.*?)\s*[\-\u2014]\s*([A-Z]{2,4})$", raw)
    if m:
        raw = m.group(1).strip()
        if not team:
            team = m.group(2).strip().upper()

    return raw, team


def _parse_position_and_rank(pos_text: str) -> tuple[str | None, int | None]:
    s = (pos_text or "").upper().strip()
    if not s:
        return None, None

    # e.g. QB12, RB 3, WR, TE80
    m = re.search(r"\b(QB|RB|WR|TE)\s*([0-9]{1,3})?\b", s)
    if not m:
        return None, None

    pos = m.group(1)
    rank = int(m.group(2)) if m.group(2) else None
    return pos, rank


def _build_row(
    *,
    wanted: set[str],
    fallback_rank_by_pos: dict[str, int],
    pos_text: str,
    name_text: str,
    team_text: str | None,
) -> FantasyProsRankRow | None:
    pos, pos_rank = _parse_position_and_rank(pos_text)
    if not pos or pos not in wanted:
        return None

    name_raw, team = _parse_name_and_team(name_text, team_text)
    if not name_raw:
        return None

    if pos_rank is None:
        fallback_rank_by_pos[pos] += 1
        pos_rank = fallback_rank_by_pos[pos]
    else:
        fallback_rank_by_pos[pos] = max(fallback_rank_by_pos[pos], pos_rank)

    return FantasyProsRankRow(
        source="fantasypros",
        source_mfl_id=None,
        name_raw=name_raw,
        name_normalized=normalize_name_for_matching(name_raw),
        position=pos,
        team=team,
        rank=int(pos_rank),
        value=None,
    )


def _parse_player_row_blocks(
    html: str,
    *,
    wanted: set[str],
    fallback_rank_by_pos: dict[str, int],
) -> list[FantasyProsRankRow]:
    """Parse current FP row markup: <tr class="player-row">...</tr>."""
    out: list[FantasyProsRankRow] = []
    row_blocks = re.findall(
        r'<tr[^>]*class=["\'][^"\']*\bplayer-row\b[^"\']*["\'][^>]*>(.*?)</tr>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )

    for row_html in row_blocks:
        # Anchor to the explicit link signature provided by FantasyPros.
        # Example:
        # <a class="player-cell-name fp-player-link ..." fp-player-name="Puka Nacua" ...>Puka Nacua</a>
        link_match = re.search(
            r'<a[^>]*class=["\'][^"\']*\bfp-player-link\b[^"\']*["\'][^>]*>(.*?)</a>',
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not link_match:
            continue

        attr_name_match = re.search(r'fp-player-name=["\']([^"\']+)["\']', link_match.group(0), flags=re.IGNORECASE)
        if attr_name_match:
            name_text = _strip_tags(attr_name_match.group(1))
        else:
            name_text = _strip_tags(link_match.group(1))

        # POS token appears in the row (e.g. WR1 / RB3). Grab first positional token.
        pos_match = re.search(r"\b(QB|RB|WR|TE)\s*([0-9]{1,3})?\b", row_html, flags=re.IGNORECASE)
        if not pos_match:
            continue
        pos_text = f"{pos_match.group(1)}{pos_match.group(2) or ''}"

        team_match = re.search(
            r'<span[^>]*class=["\'][^"\']*\bplayer-cell-team\b[^"\']*["\'][^>]*>(.*?)</span>',
            row_html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        team_text = _strip_tags(team_match.group(1)) if team_match else None

        row = _build_row(
            wanted=wanted,
            fallback_rank_by_pos=fallback_rank_by_pos,
            pos_text=pos_text,
            name_text=name_text,
            team_text=team_text,
        )
        if row is not None:
            out.append(row)

    return out


def _parse_tables(
    html: str,
    *,
    wanted: set[str],
    fallback_rank_by_pos: dict[str, int],
) -> list[FantasyProsRankRow]:
    out: list[FantasyProsRankRow] = []
    for tm in re.finditer(r"<table[^>]*>.*?</table>", html, flags=re.IGNORECASE | re.DOTALL):
        rows = _extract_rows(tm.group(0))
        if len(rows) < 2:
            continue

        headers = rows[0]
        body = rows[1:]

        name_idx = _column_index(headers, [r"player", r"name"])
        pos_idx = _column_index(headers, [r"^pos$", r"position"])
        team_idx = _column_index(headers, [r"\bteam\b", r"nfl"])

        if name_idx is None or pos_idx is None:
            continue

        for row_vals in body:
            if name_idx >= len(row_vals) or pos_idx >= len(row_vals):
                continue

            row = _build_row(
                wanted=wanted,
                fallback_rank_by_pos=fallback_rank_by_pos,
                pos_text=row_vals[pos_idx],
                name_text=row_vals[name_idx],
                team_text=row_vals[team_idx] if (team_idx is not None and team_idx < len(row_vals)) else None,
            )
            if row is not None:
                out.append(row)

    return out


def _parse_ecr_data(
    html: str,
    *,
    wanted: set[str],
) -> list[FantasyProsRankRow]:
    """Parse FantasyPros rankings embedded in ``var ecrData = {...};``."""
    marker = "var ecrData ="
    marker_idx = html.find(marker)
    if marker_idx == -1:
        return []

    json_start = html.find("{", marker_idx + len(marker))
    if json_start == -1:
        return []

    try:
        data, _ = json.JSONDecoder().raw_decode(html[json_start:])
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    players = data.get("players")
    if not isinstance(players, list):
        return []

    out: list[FantasyProsRankRow] = []

    for player in players:
        if not isinstance(player, dict):
            continue

        # FantasyPros legacy duplicate: Isaiah Williams (Maryland, born 1987).
        # The active Jets WR Isaiah Williams is FantasyPros player_id 26379.
        if player.get("player_id") == 10977:
            continue

        pos = str(player.get("player_position_id") or "").upper().strip()
        if pos not in wanted:
            continue

        name_raw = str(player.get("player_name") or "").strip()
        if not name_raw:
            continue

        pos_rank_text = str(player.get("pos_rank") or "").upper().strip()
        rank_match = re.fullmatch(r"(QB|RB|WR|TE)([0-9]{1,3})", pos_rank_text)
        if not rank_match:
            continue

        rank_pos = rank_match.group(1)
        if rank_pos != pos:
            continue

        team = str(player.get("player_team_id") or "").upper().strip() or None

        out.append(
            FantasyProsRankRow(
                source="fantasypros",
                source_mfl_id=None,
                name_raw=name_raw,
                name_normalized=normalize_name_for_matching(name_raw),
                position=pos,
                team=team,
                rank=int(rank_match.group(2)),
                value=None,
            )
        )

    return out


def parse_fantasypros_dynasty_overall(html: str, *, position: str | None = None) -> list[FantasyProsRankRow]:
    wanted = {position.upper()} if position else SUPPORTED_POSITIONS
    fallback_rank_by_pos = {"QB": 0, "RB": 0, "WR": 0, "TE": 0}

    rows: list[FantasyProsRankRow] = []
    rows.extend(_parse_ecr_data(html, wanted=wanted))

    # Legacy fallbacks in case FantasyPros changes formats again.
    if not rows:
        rows.extend(_parse_player_row_blocks(html, wanted=wanted, fallback_rank_by_pos=fallback_rank_by_pos))

    if not rows:
        rows.extend(_parse_tables(html, wanted=wanted, fallback_rank_by_pos=fallback_rank_by_pos))

    dedup: dict[tuple[str, int, str], FantasyProsRankRow] = {}
    for row in rows:
        dedup[(row.position, row.rank, row.name_normalized)] = row

    return sorted(dedup.values(), key=lambda r: (r.position, r.rank, r.name_raw.lower()))


def fetch_fantasypros_dynasty_trade_values(
    *,
    position: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[FantasyProsRankRow]:
    if position and position.upper() not in SUPPORTED_POSITIONS:
        raise ValueError(f"Unsupported position: {position}")

    headers = {"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"}
    response = requests.get(FANTASYPROS_DYNASTY_OVERALL_URL, headers=headers, timeout=timeout_seconds)
    response.raise_for_status()
    return parse_fantasypros_dynasty_overall(response.text, position=position)