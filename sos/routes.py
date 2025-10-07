from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, current_app, render_template, request
from flask_login import login_required, current_user

from app import db
from models import League, Team
from services.mfl_client import MFLClient

sos_bp = Blueprint(
    "sos",
    __name__,
    url_prefix="/tools",
    template_folder="../templates",
)


# --------------------------- Host & cookie helpers ---------------------------

def _norm_host(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    host = str(raw).strip()
    if host.startswith("http://"):
        host = host[7:]
    elif host.startswith("https://"):
        host = host[8:]
    return host.rstrip("/") or None


def _league_host(league: League) -> Optional[str]:
    for attr in ("league_host", "host", "base_url"):
        val = getattr(league, attr, None)
        if not val:
            continue
        host = _norm_host(val)
        if host:
            return host
    return "api.myfantasyleague.com"


def _cookie_header_for_host(host: str) -> Optional[str]:
    host = _norm_host(host) or "api.myfantasyleague.com"
    try:
        if hasattr(current_user, "get_mfl_cookie_header"):
            header = current_user.get_mfl_cookie_header(host)  # type: ignore[attr-defined]
            if header:
                return str(header)
    except Exception:
        pass

    for attr in ("mfl_cookie_api", "mfl_cookie"):
        val = getattr(current_user, attr, None)
        if isinstance(val, dict) and val:
            return "; ".join(f"{k}={v}" for k, v in val.items())
        if isinstance(val, str) and val:
            return val

    for attr in ("session_key", "mfl_session"):
        val = getattr(current_user, attr, None)
        if isinstance(val, str) and val:
            return f"MFLSESSION={val}"

    return None


# ------------------------------ Record helpers ------------------------------

_RECORD_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)(?:\s*-\s*(\d+))?\s*$")


def _record_tuple(record: Optional[str]) -> Tuple[int, int, int]:
    if not record:
        return (0, 0, 0)
    m = _RECORD_RE.match(str(record))
    if not m:
        return (0, 0, 0)
    wins = int(m.group(1))
    losses = int(m.group(2))
    ties = int(m.group(3) or 0)
    return wins, losses, ties


def _win_pct(record: Optional[str]) -> Optional[float]:
    wins, losses, ties = _record_tuple(record)
    total = wins + losses + ties
    if total <= 0:
        return None
    return (wins + 0.5 * ties) / total


# ----------------------------- Schedule parsing -----------------------------

def _parse_schedule(xml_bytes: bytes) -> Dict[int, List[List[str]]]:
    mapping: Dict[int, List[List[str]]] = {}
    if not xml_bytes:
        return mapping

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return mapping

    weekly_nodes = root.findall(".//weeklySchedule") if root is not None else []
    for weekly in weekly_nodes:
        week_attr = weekly.get("week")
        try:
            week = int(str(week_attr))
        except (TypeError, ValueError):
            continue

        entries: List[List[str]] = []
        for matchup in weekly.findall("matchup"):
            ids: List[str] = []
            home = matchup.get("home")
            away = matchup.get("away")
            if home:
                ids.append(str(home))
            if away:
                ids.append(str(away))

            for franchise in matchup.findall("franchise"):
                fid = franchise.get("id") or franchise.get("franchise")
                if fid:
                    ids.append(str(fid))

            cleaned: List[str] = []
            for fid in ids:
                fid_clean = str(fid).strip()
                if not fid_clean:
                    continue
                fid_norm = fid_clean.zfill(4) if fid_clean.isdigit() else fid_clean
                if fid_norm not in cleaned:
                    cleaned.append(fid_norm)

            if cleaned:
                entries.append(cleaned)

        if entries:
            mapping.setdefault(week, []).extend(entries)

    return mapping


# ------------------------- Strength aggregation helpers ---------------------

def _difficulty_tag(avg_win_pct: Optional[float]) -> Tuple[str, str]:
    if avg_win_pct is None:
        return "unknown", "Unknown"
    if avg_win_pct >= 0.60:
        return "hard", "Tough"
    if avg_win_pct <= 0.40:
        return "easy", "Favorable"
    return "medium", "Balanced"


def _avg(values: Iterable[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _ensure_int(val: Optional[int]) -> Optional[int]:
    try:
        if val is None:
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def _team_lookup(league_id: int) -> Dict[str, Team]:
    rows = Team.query.filter(Team.league_id == league_id).all()
    out: Dict[str, Team] = {}
    for row in rows:
        fid = getattr(row, "mfl_id", None)
        if fid in (None, ""):
            continue
        fid_str = str(fid).strip()
        if fid_str.isdigit():
            fid_str = fid_str.zfill(4)
        out[fid_str] = row
    return out


# ------------------------------- Main route ---------------------------------

@sos_bp.route("/strength-of-schedule", methods=["GET"])
@login_required
def strength_of_schedule():
    leagues: List[League] = (
        db.session.query(League)
        .filter(League.user_id == current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )

    if not leagues:
        return render_template(
            "sos/index.html",
            results=[],
            weeks=[],
            start_week=None,
            end_week=None,
            span_weeks=None,
            has_mfl=False,
            max_week=_max_week_limit(),
        )

    latest_year = max(_ensure_int(lg.year) or 0 for lg in leagues)
    default_start_week = _effective_week_for_year(latest_year) if latest_year else 1

    try:
        start_week = int(request.args.get("start_week", default_start_week))
    except (TypeError, ValueError):
        start_week = default_start_week
    try:
        span_weeks = int(request.args.get("span", 4))
    except (TypeError, ValueError):
        span_weeks = 4

    max_week_cfg = _max_week_limit()
    if start_week < 1:
        start_week = 1
    if start_week > max_week_cfg:
        start_week = max_week_cfg

    if span_weeks < 1:
        span_weeks = 1
    if span_weeks > 8:
        span_weeks = 8

    end_week = min(max_week_cfg, start_week + span_weeks - 1)
    week_numbers = list(range(start_week, end_week + 1))

    results: List[dict] = []
    has_data = False

    for league in leagues:
        league_year = _ensure_int(league.year)
        franchise_id = getattr(league, "franchise_id", None)
        if not league_year or not league.mfl_id or not franchise_id:
            results.append(
                {
                    "league": league,
                    "error": "Missing league year, MFL id, or your franchise id.",
                    "weeks": [],
                    "overall": None,
                }
            )
            continue

        host = _league_host(league) or "api.myfantasyleague.com"
        cookie = _cookie_header_for_host(host)

        base_host = _norm_host(host) or "api.myfantasyleague.com"
        base_url = f"https://{base_host}/{league_year}/"
        client = MFLClient(league_year, base_url=base_url)

        try:
            schedule_bytes = client.get_schedule(str(league.mfl_id), cookie=cookie)
        except Exception as exc:
            results.append(
                {
                    "league": league,
                    "error": f"Unable to fetch schedule: {exc}",
                    "weeks": [],
                    "overall": None,
                }
            )
            continue

        schedule_map = _parse_schedule(schedule_bytes)
        team_map = _team_lookup(league.id)
        my_id = str(franchise_id).zfill(4)
        my_team = team_map.get(my_id)

        week_rows: List[dict] = []
        for week in week_numbers:
            opponent_ids: List[str] = []
            for matchup in schedule_map.get(week, []):
                if my_id in matchup:
                    opponent_ids.extend(fid for fid in matchup if fid != my_id)

            # dedupe while preserving order
            seen: set[str] = set()
            unique_opps: List[str] = []
            for fid in opponent_ids:
                if fid not in seen:
                    seen.add(fid)
                    unique_opps.append(fid)

            opponent_rows: List[dict] = []
            for opp_id in unique_opps:
                team = team_map.get(opp_id)
                win_pct = _win_pct(team.record) if team else None
                pf = None
                pa = None
                if team:
                    try:
                        pf = float(team.points_for) if team.points_for is not None else None
                    except (TypeError, ValueError):
                        pf = None
                    try:
                        pa = float(team.points_against) if team.points_against is not None else None
                    except (TypeError, ValueError):
                        pa = None
                opponent_rows.append(
                    {
                        "id": opp_id,
                        "name": team.name if team and team.name else f"Franchise {opp_id}",
                        "record": team.record if team and team.record else "—",
                        "win_pct": win_pct,
                        "pf": pf,
                        "pa": pa,
                    }
                )

            avg_win_pct = _avg(row["win_pct"] for row in opponent_rows)
            difficulty_tag, difficulty_label = _difficulty_tag(avg_win_pct)
            avg_pf = _avg(row["pf"] for row in opponent_rows)

            week_rows.append(
                {
                    "week": week,
                    "opponents": opponent_rows,
                    "is_bye": not opponent_rows,
                    "avg_win_pct": avg_win_pct,
                    "avg_pf": avg_pf,
                    "difficulty_tag": difficulty_tag,
                    "difficulty_label": difficulty_label,
                }
            )

        overall_avg = _avg(row["avg_win_pct"] for row in week_rows if not row["is_bye"])
        overall_tag, overall_label = _difficulty_tag(overall_avg)
        hardest = None
        try:
            hardest = max(
                (row for row in week_rows if row["avg_win_pct"] is not None),
                key=lambda r: r["avg_win_pct"],
            )
        except ValueError:
            hardest = None

        if week_rows:
            has_data = True

        results.append(
            {
                "league": league,
                "my_team": my_team,
                "weeks": week_rows,
                "overall": {
                    "avg_win_pct": overall_avg,
                    "tag": overall_tag,
                    "label": overall_label,
                },
                "hardest_week": hardest,
                "error": None,
            }
        )

    # Sort leagues by toughest overall schedule (highest avg win pct first)
    def _sort_key(row: dict) -> Tuple[float, str]:
        overall = row.get("overall", {}) or {}
        avg = overall.get("avg_win_pct")
        score = avg if isinstance(avg, (int, float)) else -1.0
        return (-score, str(row.get("league").name or ""))

    results.sort(key=_sort_key)

    return render_template(
        "sos/index.html",
        results=results,
        weeks=week_numbers,
        start_week=start_week,
        end_week=end_week,
        span_weeks=span_weeks,
        has_mfl=True,
        has_data=has_data,
        max_week=max_week_cfg,
    )


# ----------------------------- Shared week logic ----------------------------

def _effective_week_for_year(year: int) -> int:
    from lineups.routes import _effective_current_week

    try:
        return _effective_current_week(year)
    except Exception:
        return 1


def _max_week_limit() -> int:
    try:
        val = int(current_app.config.get("MFL_MAX_WEEKS", 18))
        return max(1, min(val, 22))
    except Exception:
        return 18