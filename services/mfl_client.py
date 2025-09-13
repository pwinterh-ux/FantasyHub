# services/mfl_client.py
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Optional, Dict, Any
from urllib.parse import unquote_plus

import requests
from flask import current_app

DEFAULT_TIMEOUT = 20  # seconds
RATE_MAX_CALLS = 60
RATE_WINDOW_SEC = 60
RETRY_STATUSES = {429, 500, 502, 503, 504}
DEFAULT_HEADERS = {
    # A UA helps some deployments log/allow requests more cleanly
    "User-Agent": "FantasyHub/0.1 (+support@fantasyhub.local)"
}


# ----------------------------- Rate Limiter ----------------------------------

class RateLimiter:
    def __init__(self, max_calls: int = RATE_MAX_CALLS, window: int = RATE_WINDOW_SEC):
        self.max_calls = max_calls
        self.window = window
        self._calls: list[float] = []

    def wait(self) -> None:
        now = time.time()
        self._calls = [t for t in self._calls if now - t < self.window]
        if len(self._calls) >= self.max_calls:
            sleep_for = self.window - (now - self._calls[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._calls.append(time.time())


_rl = RateLimiter()


# ----------------------------- Logging Helpers -------------------------------

def _log_http_safe(label: str, resp: requests.Response, elapsed_ms: int, include_body: bool = True) -> None:
    """Log URL (with query), status, elapsed, and truncated body. Never logs credentials/cookies."""
    try:
        body_snippet = ""
        if include_body:
            limit = 800
            try:
                # allow overriding in config
                limit = int(getattr(current_app.config, "MFL_LOG_BODY_CHARS", 800))
            except Exception:
                pass
            txt = resp.text or ""
            body_snippet = txt[:limit] + (f"... [truncated {len(txt) - limit} chars]" if len(txt) > limit else "")

        current_app.logger.info(
            "[MFL] | %s",
            {
                "label": label,
                "status": getattr(resp, "status_code", "?"),
                "elapsed_ms": elapsed_ms,
                "url": getattr(resp.request, "url", "<unknown>"),
                "body_snippet": body_snippet if include_body else "",
            },
        )
    except Exception:
        # logging must never crash request path
        pass


def _log_login_attempt(method: str, url: str, status: Optional[int] = None) -> None:
    """Login log without sensitive params or cookies."""
    try:
        # Strip query entirely to avoid logging USERNAME/PASSWORD
        safe_url = url.split("?", 1)[0]
        payload = {"label": f"{method} login", "url": safe_url}
        if status is not None:
            payload["status"] = status
        current_app.logger.info("[MFL] | %s", payload)
    except Exception:
        pass


# --------------------------------- Client ------------------------------------

class MFLClient:
    """
    Cookie-first MFL client. Uses XML export endpoints.
    """

    def __init__(self, year: int, base_url: Optional[str] = None, timeout: int = DEFAULT_TIMEOUT):
        self.year = year
        self.base = base_url or f"https://api.myfantasyleague.com/{year}/"
        self.timeout = timeout
        self.default_params = {"XML": "1"}

    # ---------------------------- Public API ---------------------------------

    def login(self, username: str, password: str) -> str:
        """
        Try common login variants and return a raw Cookie header string.
        """
        _rl.wait()
        candidates = [
            ("POST", "login"),
            ("POST", "account/login"),
            ("GET", "login"),
            ("GET", "account/login"),
        ]

        last_error = None
        for method, path in candidates:
            try:
                url = f"{self.base}{path}"
                params = {"USERNAME": username, "PASSWORD": password, "XML": "1"}

                if method == "POST":
                    _log_login_attempt(method, url)
                    resp = requests.post(url, data=params, timeout=self.timeout, headers=DEFAULT_HEADERS)
                else:
                    # Avoid logging query string with credentials
                    _log_login_attempt(method, url)
                    resp = requests.get(url, params=params, timeout=self.timeout, headers=DEFAULT_HEADERS)

                _log_login_attempt(method, url, status=resp.status_code)

                if resp.status_code >= 400:
                    last_error = f"{path} {resp.status_code}"
                    continue

                cookie_header = self._extract_cookie(resp)
                if cookie_header:
                    # If XML present and says success, great. If not, we still accept cookie presence.
                    if self._xml_login_success(resp.content):
                        return cookie_header
                    return cookie_header

            except requests.RequestException as e:
                last_error = str(e)
                continue

        raise RuntimeError(
            f"MFL login failed: no session cookie returned (tried multiple endpoints: {last_error or 'unknown error'})."
        )

    def get_user_leagues(self, cookie: str) -> bytes:
        return self._export("myleagues", cookie=cookie)

    def get_assets(self, league_id: str, cookie: str) -> bytes:
        return self._export("assets", params={"L": league_id}, cookie=cookie)

    def get_standings(self, league_id: str, cookie: str) -> bytes:
        return self._export("leagueStandings", params={"L": league_id}, cookie=cookie)

    def get_league_info(self, league_id: str, cookie: str) -> bytes:
        """League metadata, including <franchise ...> and baseURL."""
        return self._export("league", params={"L": league_id}, cookie=cookie)

    def get_rosters(self, league_id: str, cookie: str) -> bytes:
        """Roster listing per franchise; useful fallback if assets is empty."""
        return self._export("rosters", params={"L": league_id}, cookie=cookie)

    def get_future_picks(self, league_id: str, cookie: str) -> bytes:
        """Future draft picks per franchise (fallback when assets is blocked)."""
        return self._export("futureDraftPicks", params={"L": league_id}, cookie=cookie)

    def get_pending_trades(self, league_id: str, cookie: str) -> bytes:
        """
        Open/pending trades only (no completed history).
        Maps to export TYPE=pendingTrades.
        """
        return self._export("pendingTrades", params={"L": league_id}, cookie=cookie)

    # ---------------------------- Internals ----------------------------------

    def _export(
        self,
        type_: str,
        params: Optional[Dict[str, Any]] = None,
        cookie: Optional[str] = None,
        retries: int = 3,
        backoff_base: float = 0.75,
    ) -> bytes:
        """
        Core GET wrapper with retry, logging, and cross-subdomain auth helpers.
        """
        _rl.wait()
        url = f"{self.base}export"
        merged_params: Dict[str, Any] = {"TYPE": type_, **self.default_params, **(params or {})}

        # --- Cross-subdomain auth helpers ---
        # 1) If the cookie contains MFL_USER_ID, also pass it as a query param (decoded to prevent double-encoding)
        user_id = self._extract_user_id(cookie)
        if user_id and "MFL_USER_ID" not in merged_params:
            merged_params["MFL_USER_ID"] = user_id

        # 2) Optional APIKEY from config (works both on api host and league hosts)
        try:
            apikey = current_app.config.get("MFL_APIKEY")
            if apikey and "APIKEY" not in merged_params:
                merged_params["APIKEY"] = apikey
        except Exception:
            # no app context; ignore
            pass

        headers = {**DEFAULT_HEADERS, **self._cookie_header(cookie)}

        attempt = 0
        while True:
            attempt += 1
            start = time.time()
            resp = requests.get(url, params=merged_params, headers=headers, timeout=self.timeout)
            elapsed_ms = int((time.time() - start) * 1000)

            # Retry on transient statuses
            if resp.status_code in RETRY_STATUSES and attempt <= retries:
                _log_http_safe(f"GET export:{type_}", resp, elapsed_ms, include_body=True)
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue

            # Raise if not OK
            self._raise_for_status(resp)

            # Log success
            _log_http_safe(f"GET export:{type_}", resp, elapsed_ms, include_body=True)

            return resp.content

    # ---------------------------- Helpers ------------------------------------

    @staticmethod
    def _cookie_header(cookie: Optional[str]) -> Dict[str, str]:
        return {"Cookie": cookie} if cookie else {}

    @staticmethod
    def _extract_cookie(resp: requests.Response) -> str:
        """
        Build a Cookie header from either Set-Cookie headers or the cookie jar.
        """
        # Prefer cookie jar (handles multiple Set-Cookie entries robustly)
        jar = resp.cookies.get_dict()
        if jar:
            return "; ".join(f"{k}={v}" for k, v in jar.items())

        # Fallback to raw header (best effort)
        set_cookie = resp.headers.get("Set-Cookie")
        if set_cookie:
            # Split on comma to approximate multiple Set-Cookie entries; not perfect but fallback only
            return "; ".join([c.split(";", 1)[0] for c in set_cookie.split(",")])

        return ""

    @staticmethod
    def _extract_user_id(cookie: Optional[str]) -> Optional[str]:
        """
        Pull MFL_USER_ID out of the cookie string, decoding any % encodings so
        we don't double-encode when requests adds it to the query.
        """
        if not cookie:
            return None
        for part in str(cookie).split(";"):
            k, _, v = part.strip().partition("=")
            if k == "MFL_USER_ID" and v:
                try:
                    return unquote_plus(v)
                except Exception:
                    return v
        return None

    @staticmethod
    def _xml_login_success(content: bytes) -> bool:
        """
        Accept explicit <login status="success"> if present; otherwise assume success when cookies exist.
        """
        if not content:
            return True
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            return True  # not XML; rely on cookies

        status = (root.attrib.get("status") or "").lower()
        if status in {"success", "ok", "1", "true"}:
            return True
        el = root.find(".//login")
        if el is not None:
            s = (el.attrib.get("status") or "").lower()
            if s in {"success", "ok", "1", "true"}:
                return True
        return True

    @staticmethod
    def _raise_for_status(resp: requests.Response) -> None:
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            code = resp.status_code
            if code in (401, 403):
                raise RuntimeError("MFL auth failed or session expired. Please re-link your MFL account.") from e
            text = (resp.text or "").strip()
            if text:
                raise RuntimeError(f"MFL request failed ({code}): {text[:300]}") from e
            raise
