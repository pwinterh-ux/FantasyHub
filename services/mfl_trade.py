# services/mfl_trade.py
from __future__ import annotations

import time
from typing import Iterable, Optional, Tuple, Dict, Any, Union
from urllib.parse import quote_plus

import requests

# ---- Public helpers ---------------------------------------------------------

def build_trade_proposal_url(
    *,
    host: str,
    year: Union[int, str],
    league_id: Union[int, str],
    offered_to: str,                      # target franchise id (e.g., "0001")
    will_give_up: Union[str, Iterable[str]],
    will_receive: Union[str, Iterable[str]],
    comments: str = "",
    expires_ts: Optional[int] = None,     # if None -> now + 7 days
    apikey: Optional[str] = None,
    mfl_user_id: Optional[str] = None,    # if you parse from cookie elsewhere
) -> str:
    """
    Returns the full GET URL for:
      https://{host}/{year}/import?TYPE=tradeProposal&...
    """
    if expires_ts is None:
        expires_ts = int(time.time()) + 7 * 24 * 3600

    def to_csv(v: Union[str, Iterable[str]]) -> str:
        if isinstance(v, (list, tuple, set)):
            return ",".join([str(x) for x in v])
        return str(v or "")

    base = f"https://{host}/{year}/import"
    params = [
        ("TYPE", "tradeProposal"),
        ("L", str(league_id)),
        ("OFFEREDTO", str(offered_to).zfill(4)),
        ("WILL_GIVE_UP", to_csv(will_give_up)),
        ("WILL_RECEIVE", to_csv(will_receive)),
        ("COMMENTS", comments or ""),
        ("EXPIRES", str(int(expires_ts))),
    ]

    if mfl_user_id:
        params.append(("MFL_USER_ID", mfl_user_id))
    if apikey:
        params.append(("APIKEY", apikey))

    # Encode values but keep commas in list params
    qs = "&".join(f"{k}={quote_plus(v, safe=',')}" for k, v in params if v is not None)
    return f"{base}?{qs}"


def send_trade_proposal(
    *,
    host: str,
    year: Union[int, str],
    league_id: Union[int, str],
    offered_to: str,
    will_give_up: Union[str, Iterable[str]],
    will_receive: Union[str, Iterable[str]],
    comments: str = "",
    expires_ts: Optional[int] = None,
    apikey: Optional[str] = None,
    mfl_user_id: Optional[str] = None,
    cookie: Optional[str] = None,              # e.g., "MFL_USER_ID=...; MFL_SESSION=..."
    timeout: int = 20,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """
    Builds the URL and performs the GET against MFL.
    - If you pass a cookie, it is sent via the Cookie header.
    - If you pass an API key, it's appended as &APIKEY=...
    Returns: {"ok": bool, "status_code": int, "url": str, "text": str}
    Raises: requests.RequestException on transport errors (timeouts, DNS, etc.)
    """
    url = build_trade_proposal_url(
        host=host,
        year=year,
        league_id=league_id,
        offered_to=offered_to,
        will_give_up=will_give_up,
        will_receive=will_receive,
        comments=comments,
        expires_ts=expires_ts,
        apikey=apikey,
        mfl_user_id=mfl_user_id,
    )

    headers = {"User-Agent": "FantasyHub/1.0 (+import-trade-proposal)"}
    if extra_headers:
        headers.update(extra_headers)
    if cookie:
        headers["Cookie"] = cookie

    resp = requests.get(url, headers=headers, timeout=timeout)
    ok = 200 <= resp.status_code < 300
    return {
        "ok": ok,
        "status_code": resp.status_code,
        "url": url,
        "text": resp.text or "",
    }
