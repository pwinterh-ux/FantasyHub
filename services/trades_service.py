# services/trades_service.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime

import requests
from flask import current_app

from app import db
from models import League, Team
from services.mfl_client import MFLClient
from services.mfl_parsers import parse_league_info  # returns (meta_map, roster_text, base_url)
from services.mfl_trades_parsers import (
    parse_pending_trades,
    normalize_trades_for_template,
)

DEFAULT_TIMEOUT = 20
RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class TradesFetchSummary:
    rows: List[Dict[str, Any]]               # flattened, normalized, sorted
    per_league: Dict[str, Dict[str, Any]]    # {league_id: {"name":..., "errors":[...], "count": int}}
    errors: List[str]
    fetched_at: datetime


# ------------------ lightweight HTTP helpers (no client changes) ------------------

def _cookie_header(cookie: Optional[str]) -> Dict[str, str]:
    return {"Cookie": cookie} if cookie else {}

def _extract_user_id(cookie: Optional[str]) -> Optional[str]:
    if not cookie:
        return None
    for part in str(cookie).split(";"):
        k, _, v = part.strip().partition("=")
        if k == "MFL_USER_ID" and v:
            return v
    return None

def _get_with_retry(url: str, params: Dict[str, Any], headers: Dict[str, str], timeout: int) -> requests.Response:
    attempt = 0
    backoff = 0.75
    while True:
        attempt += 1
        resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        if resp.status_code in RETRY_STATUSES and attempt < 4:
            try:
                current_app.logger.info("[trades] retrying %s (%s) in %.2fs", url, resp.status_code, backoff)
            except Exception:
                pass
            import time as _t
            _t.sleep(backoff)
            backoff *= 2
            continue
        resp.raise_for_status()
        return resp

def _export(base: str, type_: str, params: Dict[str, Any], cookie: Optional[str], timeout: int = DEFAULT_TIMEOUT) -> bytes:
    url = f"{base.rstrip('/')}/export"
    merged = {"TYPE": type_, "XML": "1", **(params or {})}
    # pass user id cross-domain when possible
    uid = _extract_user_id(cookie)
    if uid and "MFL_USER_ID" not in merged:
        merged["MFL_USER_ID"] = uid
    headers = {"User-Agent": "FantasyHub/0.1 (+support@fantasyhub.local)", **_cookie_header(cookie)}
    resp = _get_with_retry(url, merged, headers, timeout)
    # brief log
    try:
        current_app.logger.info("[trades] GET %s (%s) %s", type_, resp.status_code, resp.url)
    except Exception:
        pass
    return resp.content


# ------------------ public entrypoint ------------------

def fetch_open_trades_for_user(
    *,
    user_id: int,
    year: int,
    cookie: str,
    timeout: int = DEFAULT_TIMEOUT,
) -> TradesFetchSummary:
    """
    Read-only: queries DB for leagues/teams, fetches pending trades per league,
    normalizes using DB team names + the user's franchise id, and returns rows
    ready for a template. Zero changes to existing sync code.

    Sorting: "received" first, then "sent", newest first within each.
    """
    # 1) Gather leagues for this user/year
    leagues: List[League] = (
        League.query.filter_by(user_id=user_id, year=year)
        .order_by(League.mfl_id.asc())
        .all()
    )

    rows: List[Dict[str, Any]] = []
    per_league: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    api_client = MFLClient(year=year)

    for lg in leagues:
        lid = str(lg.mfl_id)
        league_errors: List[str] = []
        league_name = lg.name

        # 2) Build team map from DB (read-only)
        teams: List[Team] = Team.query.filter_by(league_id=lg.id).all()
        team_name_by_fid: Dict[str, str] = {t.mfl_id: (t.name or f"Franchise {t.mfl_id}") for t in teams}

        # 3) Resolve league host (using existing get_league_info + parser)
        base_url = None
        try:
            info_xml = api_client.get_league_info(lid, cookie)
            _, _, base_url = parse_league_info(info_xml)
        except Exception as e:
            league_errors.append(f"league_info failed: {e}")

        # Compose host/{year}/
        if not base_url:
            base = f"https://api.myfantasyleague.com/{year}/"
        else:
            base = f"{base_url.rstrip('/')}/{year}/"

        # 4) Fetch + parse pendingTrades
        try:
            xml = _export(base, "pendingTrades", {"L": lid}, cookie=cookie, timeout=timeout)
            raw = parse_pending_trades(xml)
        except Exception as e:
            league_errors.append(f"pendingTrades failed: {e}")
            per_league[lid] = {"name": league_name, "errors": league_errors, "count": 0}
            errors.extend(league_errors)
            continue

        # 5) Normalize for display (direction, assets, team names, link)
        try:
            my_fid = (lg.franchise_id or "").zfill(4)
            # Derive a base league URL to link out (drop /{year} if present)
            link_base = base.rstrip("/")
            if link_base.endswith(f"/{year}"):
                link_base = link_base[: -(len(str(year)) + 1)]  # remove "/{year}"

            normalized = normalize_trades_for_template(
                raw,
                my_fid=my_fid,
                league_id=lid,
                league_name=league_name or f"League {lid}",
                base_url=link_base,
                year=year,
                team_name_by_fid=team_name_by_fid,
            )

            # received first (0), sent second (1); newer first
            def sort_key(row: Dict[str, Any]) -> Tuple[int, int]:
                bucket = 0 if row.get("direction") == "received" else 1
                ts = int(row.get("timestamp") or 0)
                return (bucket, -ts)

            normalized.sort(key=sort_key)
            rows.extend(normalized)
            per_league[lid] = {"name": league_name, "errors": league_errors, "count": len(normalized)}
        except Exception as e:
            league_errors.append(f"normalize failed: {e}")
            per_league[lid] = {"name": league_name, "errors": league_errors, "count": 0}

        errors.extend(league_errors)

    # Global sort to keep UX consistent across leagues
    def global_sort_key(row: Dict[str, Any]) -> Tuple[int, int]:
        bucket = 0 if row.get("direction") == "received" else 1
        ts = int(row.get("timestamp") or 0)
        return (bucket, -ts)

    rows.sort(key=global_sort_key)
    return TradesFetchSummary(
        rows=rows,
        per_league=per_league,
        errors=errors,
        fetched_at=datetime.utcnow(),
    )
