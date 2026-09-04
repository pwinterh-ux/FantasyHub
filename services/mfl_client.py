# services/mfl_client.py
from __future__ import annotations

import math
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Optional, Dict, Any
from urllib.parse import unquote_plus, urlparse

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
        self._lock = threading.Lock()

    def wait(self) -> None:
        min_spacing = (self.window / self.max_calls) if self.max_calls else 0.0

        while True:
            sleep_for = 0.0
            with self._lock:
                now = time.time()
                self._calls = [t for t in self._calls if now - t < self.window]

                if self._calls and min_spacing > 0:
                    next_allowed = self._calls[-1] + min_spacing
                    if next_allowed > now:
                        sleep_for = max(sleep_for, next_allowed - now)

                if self.max_calls and len(self._calls) >= self.max_calls:
                    window_wait = self.window - (now - self._calls[0])
                    if window_wait > sleep_for:
                        sleep_for = window_wait

                if sleep_for <= 0:
                    self._calls.append(now)
                    return

            time.sleep(sleep_for)


_rl = RateLimiter()


# ----------------------------- Logging Helpers -------------------------------

def _iso_utc(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    try:
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        else:
            ts = ts.astimezone(timezone.utc)
        return ts.isoformat(timespec="milliseconds")
    except Exception:
        try:
            return ts.isoformat()
        except Exception:
            return None


def _log_http_safe(
    label: str,
    resp: requests.Response,
    elapsed_ms: int,
    *,
    include_body: bool = True,
    context: Optional[Dict[str, Any]] = None,
    started_at: Optional[datetime] = None,
) -> None:
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

        payload: Dict[str, Any] = {
            "label": label,
            "status": getattr(resp, "status_code", "?"),
            "elapsed_ms": elapsed_ms,
            "method": getattr(resp.request, "method", "?"),
            "url": getattr(resp.request, "url", "<unknown>"),
            "response_bytes": len(getattr(resp, "content", b"")) if getattr(resp, "content", None) is not None else 0,
            "content_type": resp.headers.get("Content-Type"),
            "started_at": _iso_utc(started_at),
            "finished_at": _iso_utc(datetime.now(timezone.utc)),
            "body_snippet": body_snippet if include_body else "",
        }
        if context:
            payload["context"] = context

        current_app.logger.info("[MFL] | %s", payload)
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
            ("GET", "login"),
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

    def get_user_leagues(self, cookie: str, *, context: Optional[Dict[str, Any]] = None) -> bytes:
        ctx = {"resource": "myleagues"}
        if context:
            ctx.update(context)
        return self._export("myleagues", cookie=cookie, context=ctx)

    def get_assets(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        ctx = {"resource": "assets", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("assets", params={"L": league_id}, cookie=cookie, context=ctx)

    def get_standings(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        ctx = {"resource": "standings", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("leagueStandings", params={"L": league_id}, cookie=cookie, context=ctx)

    def get_league_info(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """League metadata, including <franchise ...> and baseURL."""
        ctx = {"resource": "league", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("league", params={"L": league_id}, cookie=cookie, context=ctx)

    def get_rosters(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Roster listing per franchise; useful fallback if assets is empty."""
        ctx = {"resource": "rosters", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("rosters", params={"L": league_id}, cookie=cookie, context=ctx)

    def get_player_status(
        self,
        league_id: str,
        player_ids: list[str | int],
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """
        Return MFL player status for one or more players in a league.

        Uses TYPE=playerStatus with a comma-separated list of player IDs.

        This method is intentionally batch-oriented: callers should pass all
        target players for a league in one request instead of making one
        request per player.
        """
        normalized_ids = []

        for player_id in player_ids or []:
            if player_id is None:
                continue

            value = str(player_id).strip()
            if value and value not in normalized_ids:
                normalized_ids.append(value)

        if not normalized_ids:
            raise ValueError("get_player_status requires at least one player id")

        ctx = {
            "resource": "playerStatus",
            "league_id": str(league_id),
            "player_count": len(normalized_ids),
        }

        if context:
            ctx.update(context)

        return self._export(
            "playerStatus",
            params={
                "L": str(league_id),
                "P": ",".join(normalized_ids),
            },
            cookie=cookie,
            context=ctx,
        )

    def get_schedule(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Season schedule for the given league (TYPE=schedule)."""
        ctx = {"resource": "schedule", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("schedule", params={"L": league_id}, cookie=cookie, context=ctx)

    def get_future_picks(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """Future draft picks per franchise (fallback when assets is blocked)."""
        ctx = {"resource": "futureDraftPicks", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("futureDraftPicks", params={"L": league_id}, cookie=cookie, context=ctx)

    def get_pending_trades(
        self,
        league_id: str,
        cookie: str,
        *,
        context: Optional[Dict[str, Any]] = None,
    ) -> bytes:
        """
        Open/pending trades only (no completed history).
        Maps to export TYPE=pendingTrades.
        """
        ctx = {"resource": "pendingTrades", "league_id": str(league_id)}
        if context:
            ctx.update(context)
        return self._export("pendingTrades", params={"L": league_id}, cookie=cookie, context=ctx)


    def submit_fcfs_waiver(
        self,
        league_id: str,
        add_player_id: str | int,
        cookie: str,
        *,
        drop_player_ids: Optional[list[str | int]] = None,
        franchise_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Execute an immediate MFL first-come, first-served add/drop.

        MFL import:
            TYPE=fcfsWaiver
            L=<league id>
            ADD=<player id>
            DROP=<comma-separated player ids>   optional
            FRANCHISE_ID=<franchise id>         optional

        IMPORTANT:
        This request is intentionally NEVER retried.

        A transaction-changing POST is not safely idempotent. If MFL
        processes a request but the response is lost, automatically
        submitting it again could create an unintended second action.

        Returns a normalized result:
            {
                "ok": True,
                "status": "OK",
                "message": "Added",
                "errors": [],
            }

        or:
            {
                "ok": False,
                "status": None,
                "message": "<MFL error text>",
                "errors": ["...", "..."],
            }
        """

        self._require_league_waiver_host()

        league_id_s = str(
            league_id or ""
        ).strip()

        add_player_id_s = str(
            add_player_id or ""
        ).strip()

        if not league_id_s:
            raise ValueError(
                "league_id is required."
            )

        if not add_player_id_s:
            raise ValueError(
                "add_player_id is required."
            )

        normalized_drops: list[str] = []

        for raw_id in (
            drop_player_ids or []
        ):
            value = str(
                raw_id or ""
            ).strip()

            if (
                value
                and value not in normalized_drops
            ):
                normalized_drops.append(
                    value
                )

        payload: Dict[str, Any] = {
            "TYPE": "fcfsWaiver",
            "L": league_id_s,
            "ADD": add_player_id_s,
            "XML": "1",
        }

        if normalized_drops:
            payload["DROP"] = ",".join(
                normalized_drops
            )

        # Normal owner transactions should not require this.
        # Keep support available for commissioner/impersonation use,
        # but callers must explicitly request it.
        franchise_id_s = str(
            franchise_id or ""
        ).strip()

        if franchise_id_s:
            payload[
                "FRANCHISE_ID"
            ] = franchise_id_s

        # Match the authentication helpers already used by export.
        user_id = self._extract_user_id(
            cookie
        )

        if user_id:
            payload[
                "MFL_USER_ID"
            ] = user_id

        # FCFS imports have been browser-tested successfully using
        # the user's MFL cookie together with the configured APIKEY.
        #
        # Keep both authentication values. The cookie identifies the
        # MFL user/session while the APIKEY is required by the working
        # RosterDash transaction path.
        try:
            apikey = current_app.config.get(
                "MFL_APIKEY"
            )

            if apikey:
                payload[
                    "APIKEY"
                ] = apikey

        except Exception:
            pass

        headers = {
            **DEFAULT_HEADERS,
            **self._cookie_header(cookie),
        }

        # This is a league-specific write, so use the same MFL host
        # represented by this client. For the normal Waivers workflow
        # that is the league's wwwXX host and its matching user cookie.
        url = (
            f"{self.base.rstrip('/')}/import"
        )

        ctx: Dict[str, Any] = {
            "operation": "fcfsWaiver",
            "league_id": league_id_s,
            "add_player_id": add_player_id_s,
            "drop_count": len(
                normalized_drops
            ),
        }

        if context:
            ctx.update(context)

        _rl.wait()

        start = time.time()

        started_at = datetime.now(
            timezone.utc
        )

        try:
            response = requests.post(
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )

        except requests.RequestException as exc:
            raise RuntimeError(
                f"MFL FCFS request failed: {exc}"
            ) from exc

        elapsed_ms = int(
            (time.time() - start) * 1000
        )

        _log_http_safe(
            "POST import:fcfsWaiver",
            response,
            elapsed_ms,
            include_body=True,
            context=ctx,
            started_at=started_at,
        )

        if 300 <= response.status_code < 400:
            message = (
                "MFL redirected the transaction instead of confirming it. "
                "Check MFL before retrying. If the action is not present, "
                "refresh or re-link your MFL connection and try again."
            )
            return {
                "ok": False, "status": None, "message": message,
                "errors": [message], "http_status": response.status_code,
            }

        if response.status_code == 200 and "text/html" in str(
            response.headers.get("Content-Type") or ""
        ).lower():
            message = self._unexpected_waiver_web_response_message()
            return {
                "ok": False, "status": None, "message": message,
                "errors": [message], "http_status": response.status_code,
            }

        self._raise_for_status(
            response
        )

        result = (
            self._parse_fcfs_import_response(
                response.content
            )
        )

        result["http_status"] = (
            response.status_code
        )

        return result


    def submit_blind_bid_waiver(
        self,
        league_id: str,
        bids: list[Dict[str, Any]],
        cookie: str,
        *,
        round_number: Optional[int] = None,
        franchise_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Add one or more MFL blind-bid waiver requests.

        Proven MFL import request shape:

            TYPE=blindBidWaiverRequest
            L=<league id>
            ROUND=<round or blank>
            PICKS=<player id>_<amount>_<drop player id>,...
            FRANCHISE_ID=<owner franchise id>

        A missing drop player is serialized as MFL player id 0000.

        IMPORTANT:
        This state-changing import follows the same authenticated POST
        transaction pattern as the existing FCFS implementation and is
        intentionally NEVER retried.
        """

        self._require_league_waiver_host()

        league_id_s = str(
            league_id or ""
        ).strip()

        if not league_id_s:
            raise ValueError(
                "league_id is required."
            )

        if not bids:
            raise ValueError(
                "At least one blind-bid waiver request is required."
            )

        franchise_id_s = str(
            franchise_id or ""
        ).strip()

        if not franchise_id_s:
            raise ValueError(
                "franchise_id is required for blind-bid waiver requests."
            )

        normalized_picks: list[str] = []

        for index, bid in enumerate(
            bids,
            start=1,
        ):
            if not isinstance(
                bid,
                dict,
            ):
                raise ValueError(
                    f"Bid {index} must be a dictionary."
                )

            player_id_s = str(
                bid.get("player_id") or ""
            ).strip()

            if (
                not player_id_s
                or not player_id_s.isdigit()
            ):
                raise ValueError(
                    f"Bid {index} requires a numeric MFL player_id."
                )

            raw_amount = bid.get(
                "amount"
            )

            if raw_amount in (
                None,
                "",
            ):
                raise ValueError(
                    f"Bid {index} requires an amount."
                )

            try:
                amount = Decimal(
                    str(raw_amount).strip()
                )

            except (
                InvalidOperation,
                ValueError,
                TypeError,
            ) as exc:
                raise ValueError(
                    f"Bid {index} has an invalid amount."
                ) from exc

            if (
                not amount.is_finite()
                or amount < 0
            ):
                raise ValueError(
                    f"Bid {index} amount must be zero or greater."
                )

            amount_s = format(
                amount,
                "f",
            )

            if "." in amount_s:
                amount_s = (
                    amount_s
                    .rstrip("0")
                    .rstrip(".")
                )

            if not amount_s:
                amount_s = "0"

            drop_player_id_s = str(
                bid.get("drop_player_id")
                or "0000"
            ).strip()

            if not drop_player_id_s:
                drop_player_id_s = "0000"

            if not drop_player_id_s.isdigit():
                raise ValueError(
                    f"Bid {index} has an invalid drop_player_id."
                )

            normalized_picks.append(
                "_".join(
                    (
                        player_id_s,
                        amount_s,
                        drop_player_id_s,
                    )
                )
            )

        round_value = ""

        if round_number is not None:
            try:
                round_i = int(
                    round_number
                )

            except (
                TypeError,
                ValueError,
            ) as exc:
                raise ValueError(
                    "round_number must be a positive integer."
                ) from exc

            if round_i < 1:
                raise ValueError(
                    "round_number must be a positive integer."
                )

            round_value = str(
                round_i
            )

        # REPLACE is deliberately omitted.  Its presence tells MFL to replace
        # the owner's existing queue; quick claim must append one request.
        payload: Dict[str, Any] = {
            "TYPE": "blindBidWaiverRequest",
            "L": league_id_s,
            "ROUND": round_value,
            "PICKS": ",".join(
                normalized_picks
            ),
            "FRANCHISE_ID": franchise_id_s,
            "XML": "1",
        }

        # Authenticate this transaction the same way as the other
        # authenticated MFL API calls: host cookie plus owner/API params.
        user_id = self._extract_user_id(
            cookie
        )

        if user_id:
            payload["MFL_USER_ID"] = (
                user_id
            )

        try:
            apikey = current_app.config.get(
                "MFL_APIKEY"
            )

            if apikey:
                payload["APIKEY"] = (
                    apikey
                )

        except Exception:
            pass

        headers = {
            **DEFAULT_HEADERS,
            **self._cookie_header(cookie),
        }

        # Use the league-specific wwwXX host represented by this client.
        url = (
            f"{self.base.rstrip('/')}/import"
        )

        ctx: Dict[str, Any] = {
            "operation": "blindBidWaiverRequest",
            "league_id": league_id_s,
            "bid_count": len(
                normalized_picks
            ),
            "has_round": (
                round_number is not None
            ),
            "franchise_id": franchise_id_s,
        }

        if context:
            ctx.update(
                context
            )

        _rl.wait()

        start = time.time()

        started_at = datetime.now(
            timezone.utc
        )

        try:
            # STATE-CHANGING REQUEST.
            # Match the existing authenticated MFL import transaction
            # pattern used by FCFS. Never retry this request.
            response = requests.post(
                url,
                data=payload,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )

        except requests.RequestException as exc:
            raise RuntimeError(
                f"MFL blind-bid waiver request failed: {exc}"
            ) from exc

        elapsed_ms = int(
            (time.time() - start) * 1000
        )

        _log_http_safe(
            "POST import:blindBidWaiverRequest",
            response,
            elapsed_ms,
            include_body=True,
            context=ctx,
            started_at=started_at,
        )

        if 300 <= response.status_code < 400:
            message = (
                "MFL redirected the transaction instead of confirming it. "
                "Check MFL before retrying. If the action is not present, "
                "refresh or re-link your MFL connection and try again."
            )
            return {
                "ok": False, "status": None, "message": message,
                "errors": [message], "http_status": response.status_code,
            }

        if response.status_code == 200 and "text/html" in str(
            response.headers.get("Content-Type") or ""
        ).lower():
            message = self._unexpected_waiver_web_response_message()
            return {
                "ok": False, "status": None, "message": message,
                "errors": [message], "http_status": response.status_code,
            }

        self._raise_for_status(
            response
        )

        result = (
            self._parse_fcfs_import_response(
                response.content
            )
        )

        if result.get("ok"):
            result["message"] = (
                "Waiver bid submitted"
            )

        result["http_status"] = (
            response.status_code
        )

        return result

    # ---------------------------- Internals ----------------------------------

    def _export(
        self,
        type_: str,
        params: Optional[Dict[str, Any]] = None,
        cookie: Optional[str] = None,
        retries: int = 3,
        backoff_base: float = 0.75,
        *,
        context: Optional[Dict[str, Any]] = None,
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
            started_at = datetime.now(timezone.utc)
            resp = requests.get(url, params=merged_params, headers=headers, timeout=self.timeout)
            elapsed_ms = int((time.time() - start) * 1000)

            # Retry on transient statuses
            if resp.status_code in RETRY_STATUSES and attempt <= retries:
                log_context = {"attempt": attempt, "type": type_}
                if context:
                    log_context.update(context)
                _log_http_safe(
                    f"GET export:{type_}",
                    resp,
                    elapsed_ms,
                    include_body=True,
                    context=log_context,
                    started_at=started_at,
                )
                if resp.status_code == 429:
                    retry_after = self._retry_after_seconds(resp.headers.get("Retry-After"))
                    if retry_after is not None:
                        time.sleep(retry_after)
                        continue
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue

            # Raise if not OK
            log_context = {"attempt": attempt, "type": type_}
            if context:
                log_context.update(context)
            _log_http_safe(
                f"GET export:{type_}",
                resp,
                elapsed_ms,
                include_body=True,
                context=log_context,
                started_at=started_at,
            )
            self._raise_for_status(resp)

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

    def _require_league_waiver_host(self) -> None:
        """Reject central-API waiver writes before any network activity."""
        host = (urlparse(self.base).hostname or "").lower()
        if host == "api.myfantasyleague.com":
            raise RuntimeError(
                "MFL waiver transaction requires the league-specific wwwXX host."
            )

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
    def _unexpected_waiver_web_response_message() -> str:
        return (
            "MFL returned an unexpected web response, so the transaction "
            "could not be confirmed. Check MFL before retrying. If the "
            "action is not present, refresh or re-link your MFL connection "
            "and try again."
        )

    @staticmethod
    def _parse_fcfs_import_response(
        content: bytes,
    ) -> Dict[str, Any]:
        """
        Normalize MFL fcfsWaiver XML.

        Known success:
            <status>OK</status>

        Known failure:
            <error>...</error>

        Multiple <error> nodes are preserved in order so the UI can
        display every reason returned by MFL.
        """

        if not content:
            return {
                "ok": False,
                "status": None,
                "message": (
                    "MFL returned an empty transaction response."
                ),
                "errors": [
                    "MFL returned an empty transaction response."
                ],
            }

        try:
            root = ET.fromstring(
                content
            )

        except ET.ParseError:
            try:
                raw = content.decode(
                    "utf-8",
                    errors="replace",
                ).strip()
            except Exception:
                raw = ""

            raw_lower = raw.lower()

            is_html = (
                "<!doctype html" in raw_lower
                or "<html" in raw_lower
                or "mfl developers program" in raw_lower
                or "<head>" in raw_lower
                or "<body" in raw_lower
            )

            if is_html:
                message = MFLClient._unexpected_waiver_web_response_message()

            elif raw:
                # Preserve useful plain-text failures, but never send a
                # huge unexpected response into the browser.
                snippet = raw[:500]

                if len(raw) > 500:
                    snippet += "…"

                message = snippet

            else:
                message = (
                    "MFL returned an invalid transaction response."
                )

            return {
                "ok": False,
                "status": None,
                "message": message,
                "errors": [message],
            }

        def local_name(tag: Any) -> str:
            return str(tag).rsplit(
                "}",
                1,
            )[-1].lower()

        errors: list[str] = []

        statuses: list[str] = []

        for element in root.iter():

            name = local_name(
                element.tag
            )

            value = "".join(
                element.itertext()
            ).strip()

            if not value:
                continue

            if name == "error":
                if value not in errors:
                    errors.append(
                        value
                    )

            elif name == "status":
                if value not in statuses:
                    statuses.append(
                        value
                    )

        if (
            local_name(root.tag)
            == "html"
        ):
            message = MFLClient._unexpected_waiver_web_response_message()

            return {
                "ok": False,
                "status": None,
                "message": message,
                "errors": [message],
            }

        if errors:
            return {
                "ok": False,
                "status": (
                    statuses[0]
                    if statuses
                    else None
                ),
                # Preserve all MFL errors visibly.
                "message": "\n".join(
                    errors
                ),
                "errors": errors,
            }

        status = (
            statuses[0]
            if statuses
            else None
        )

        if (
            status
            and status.strip().upper()
            in {
                "OK",
                "SUCCESS",
            }
        ):
            return {
                "ok": True,
                "status": status,
                "message": "Added",
                "errors": [],
            }

        if status:
            message = (
                f"MFL transaction returned status: {status}"
            )
        else:
            message = (
                "MFL returned an unexpected transaction response."
            )

        return {
            "ok": False,
            "status": status,
            "message": message,
            "errors": [message],
        }


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

    @staticmethod
    def _retry_after_seconds(header_value: Optional[str]) -> Optional[float]:
        if not header_value:
            return None
        value = header_value.strip()
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            seconds = None
        if seconds is not None and seconds >= 0 and math.isfinite(seconds):
            return seconds
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            retry_at = None
        if retry_at is None:
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        now = datetime.now(retry_at.tzinfo)
        delay = (retry_at - now).total_seconds()
        if delay < 0:
            return 0.0
        return delay
