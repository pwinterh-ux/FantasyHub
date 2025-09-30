"""HTTP client for Sleeper public APIs with retry/backoff handling."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import time
from typing import Any

import requests
from flask import current_app

DEFAULT_BASE_URL = "https://api.sleeper.app/v1"
DEFAULT_TIMEOUT = 20
MAX_ATTEMPTS = 5
INITIAL_BACKOFF = 2.0
BACKOFF_CAP = 60.0


@dataclass
class SleeperUser:
    user_id: str
    username: str | None = None
    display_name: str | None = None


class SleeperClient:
    """Lightweight JSON client for Sleeper APIs."""

    def __init__(self, base_url: str | None = None, session: requests.Session | None = None):
        self.base_url = base_url or DEFAULT_BASE_URL.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = DEFAULT_TIMEOUT
        self._players_cache: dict[str, dict] | None = None

    # ------------------------------------------------------------------
    # Core request/JSON helpers
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, *, params: dict | None = None) -> Any:
        url = f"{self.base_url}{path}" if not path.startswith("http") else path
        delay = INITIAL_BACKOFF
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    timeout=self.timeout,
                    headers={"User-Agent": "FantasyHub SleeperClient"},
                )
            except requests.RequestException:
                if attempt >= MAX_ATTEMPTS:
                    raise
                time.sleep(min(delay, BACKOFF_CAP))
                delay *= 2
                continue

            if resp.status_code == 429:
                try:
                    logger = current_app.logger  # type: ignore[attr-defined]
                except Exception:
                    logger = None
                if logger:
                    logger.info("Sleeper 429 on %s %s (attempt %s)", method, path, attempt)
                time.sleep(min(delay, BACKOFF_CAP))
                delay *= 2
                if attempt >= MAX_ATTEMPTS:
                    resp.raise_for_status()
                continue

            try:
                resp.raise_for_status()
            except requests.HTTPError:
                try:
                    payload = resp.json()
                except ValueError:
                    raise
                raise requests.HTTPError(str(payload)) from None

            if resp.headers.get("Content-Type", "").startswith("application/json"):
                return resp.json()
            try:
                return resp.json()  # some endpoints return text/plain JSON
            except ValueError:
                return resp.text

    # ------------------------------------------------------------------
    # Users & Leagues
    # ------------------------------------------------------------------
    def get_user(self, username: str) -> SleeperUser:
        data = self._request("GET", f"/user/{username}") or {}
        user_id = str(data.get("user_id") or data.get("userId") or "").strip()
        if not user_id:
            raise ValueError("Sleeper user not found or missing user_id")
        return SleeperUser(
            user_id=user_id,
            username=(data.get("username") or data.get("name")),
            display_name=(data.get("display_name") or data.get("displayName") or data.get("username")),
        )

    def get_user_leagues(self, user_id: str, season: int, sport: str = "nfl") -> list[dict]:
        return self._request("GET", f"/user/{user_id}/leagues/{sport}/{season}") or []

    def get_league(self, league_id: str) -> dict:
        return self._request("GET", f"/league/{league_id}") or {}

    def get_league_rosters(self, league_id: str) -> list[dict]:
        return self._request("GET", f"/league/{league_id}/rosters") or []

    def get_league_users(self, league_id: str) -> list[dict]:
        return self._request("GET", f"/league/{league_id}/users") or []

    # ------------------------------------------------------------------
    # Drafts & Picks
    # ------------------------------------------------------------------
    def get_league_drafts(self, league_id: str) -> list[dict]:
        """List of drafts associated with the league across seasons."""
        return self._request("GET", f"/league/{league_id}/drafts") or []

    def get_draft(self, draft_id: str) -> dict:
        """Draft object: includes settings.rounds, season, status."""
        return self._request("GET", f"/draft/{draft_id}") or {}

    def get_draft_picks(self, draft_id: str) -> list[dict]:
        """The actual pick slots for a draft (when order is set); has pick_no, round, roster_id."""
        return self._request("GET", f"/draft/{draft_id}/picks") or []

    def get_traded_picks(self, league_id: str, *, season: int | None = None) -> list[dict]:
        """Ledger of traded picks. If season is provided, Sleeper filters on the server."""
        params = {"season": season} if season is not None else None
        return self._request("GET", f"/league/{league_id}/traded_picks", params=params) or []

    # ------------------------------------------------------------------
    # Matchups (live team totals)
    # ------------------------------------------------------------------
    def get_matchups(self, league_id: str, week: int) -> list[dict]:
        return self._request("GET", f"/league/{league_id}/matchups/{week}") or []

    # ------------------------------------------------------------------
    # Players
    # ------------------------------------------------------------------
    def get_players(self, sport: str = "nfl") -> dict:
        if self._players_cache is None:
            payload = self._request("GET", f"/players/{sport}") or {}
            if not isinstance(payload, dict):
                payload = {}
            self._players_cache = payload
        return self._players_cache


def combine_points(base: Any, decimal: Any) -> float:
    try:
        base_val = float(base or 0)
    except (TypeError, ValueError):
        base_val = 0.0
    try:
        dec_val = float(decimal or 0)
    except (TypeError, ValueError):
        dec_val = 0.0
    return round(base_val + (dec_val / 100.0), 2)


def season_default() -> int:
    return datetime.utcnow().year
