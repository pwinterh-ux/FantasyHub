# services/mfl_live.py
from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import List, Optional, Dict, Any


SECONDS_PER_GAME = 60 * 60  # assume 60-minute games for progress


@dataclass
class LiveStarter:
    player_id: str
    score: float
    game_seconds_remaining: int

    def minutes_remaining(self) -> int:
        try:
            return int(math.ceil(max(0, self.game_seconds_remaining) / 60.0))
        except Exception:
            return 0


@dataclass
class LiveSide:
    fid: str
    score: float
    starters: List[LiveStarter]

    @property
    def progress_pct(self) -> int:
        """
        Team progress as % of minutes played by starters.
        For each starter, played = clamp(0, 3600 - secondsRemaining, 3600).
        Team progress = sum(played) / (numStarters * 3600).
        """
        n = len(self.starters)
        if n == 0:
            return 0
        total_played = 0
        for s in self.starters:
            rem = max(0, int(s.game_seconds_remaining or 0))
            played = max(0, min(SECONDS_PER_GAME, SECONDS_PER_GAME - rem))
            total_played += played
        pct = int(round((total_played / float(n * SECONDS_PER_GAME)) * 100))
        # keep within [0, 100]
        return max(0, min(100, pct))


@dataclass
class LiveMatchup:
    week: Optional[int]
    my: LiveSide
    opp: LiveSide


def _parse_float(x: Optional[str]) -> float:
    try:
        return float(x or 0.0)
    except Exception:
        return 0.0


def _parse_int(x: Optional[str]) -> int:
    try:
        return int(x or 0)
    except Exception:
        try:
            return int(float(x))  # handle "0.0" strings
        except Exception:
            return 0


def parse_live_scoring(xml_bytes: bytes, my_franchise_id: str) -> Optional[LiveMatchup]:
    """
    Given an MFL live scoring XML payload and the user's franchise id (zero-padded),
    return the matchup that includes the user's franchise with only STARTERS kept.

    Returns None if the user's matchup is not found.
    """
    if not xml_bytes:
        return None

    my_fid = str(my_franchise_id or "").zfill(4)
    root = ET.fromstring(xml_bytes)

    # week attribute is on <liveScoring week="1">
    week = None
    try:
        w = root.get("week")
        if w is not None:
            week = int(w)
    except Exception:
        week = None

    for mu in root.findall(".//matchup"):
        frs = mu.findall("./franchise")
        if not frs or len(frs) < 2:
            # some leagues include double-headers; we still expect 2-node franchise blocks here
            continue

        # Find side that is "me"
        idx_me = None
        for i, f in enumerate(frs):
            if (f.get("id") or "").zfill(4) == my_fid:
                idx_me = i
                break
        if idx_me is None:
            continue  # not my matchup

        f_me = frs[idx_me]
        f_opp = frs[1 - idx_me] if len(frs) >= 2 else None
        if f_opp is None:
            continue

        def extract_side(fnode: ET.Element) -> LiveSide:
            fid = (fnode.get("id") or "").zfill(4)
            score = _parse_float(fnode.get("score"))
            starters: List[LiveStarter] = []
            players_block = fnode.find("./players")
            if players_block is not None:
                for p in players_block.findall("./player"):
                    if (p.get("status") or "").lower() != "starter":
                        continue
                    starters.append(
                        LiveStarter(
                            player_id=str(p.get("id") or ""),
                            score=_parse_float(p.get("score")),
                            game_seconds_remaining=_parse_int(p.get("gameSecondsRemaining")),
                        )
                    )
            return LiveSide(fid=fid, score=score, starters=starters)

        my_side = extract_side(f_me)
        opp_side = extract_side(f_opp)
        return LiveMatchup(week=week, my=my_side, opp=opp_side)

    # If we got here, no matchup found that includes my_fid
    return None


def serialize_matchup(m: LiveMatchup) -> Dict[str, Any]:
    """JSON-serializable dict for storing in session."""
    return {
        "week": m.week,
        "my": {
            "fid": m.my.fid,
            "score": m.my.score,
            "progress_pct": m.my.progress_pct,
            "starters": [
                {
                    "pid": s.player_id,
                    "fp": s.score,
                    "sec_remaining": max(0, int(s.game_seconds_remaining or 0)),
                    "min_remaining": s.minutes_remaining(),
                }
                for s in m.my.starters
            ],
        },
        "opp": {
            "fid": m.opp.fid,
            "score": m.opp.score,
            "progress_pct": m.opp.progress_pct,
            "starters": [
                {
                    "pid": s.player_id,
                    "fp": s.score,
                    "sec_remaining": max(0, int(s.game_seconds_remaining or 0)),
                    "min_remaining": s.minutes_remaining(),
                }
                for s in m.opp.starters
            ],
        },
    }
