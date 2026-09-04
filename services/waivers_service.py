from __future__ import annotations

from models import DynastyRankConsensusCurrent

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func

from app import db
from models import League, Player, Roster, Team
from services.mfl_client import MFLClient


# ---------------------------------------------------------------------------
# Player search
# ---------------------------------------------------------------------------

def search_players(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """
    Search the local MFL player table.

    No external API calls are made here.
    """
    q = (query or "").strip()
    if not q:
        return []

    limit = max(1, min(int(limit or 20), 50))

    # Pull a somewhat larger candidate set, then sort in Python so exact and
    # prefix matches appear before generic contains matches.
    rows = (
        Player.query
        .filter(
            Player.name.isnot(None),
            Player.name.ilike(f"%{q}%"),
        )
        .limit(max(limit * 5, 50))
        .all()
    )

    q_lower = q.lower()

    def sort_key(player: Player):
        name = (player.name or "").strip()
        name_lower = name.lower()

        if name_lower == q_lower:
            match_rank = 0
        elif name_lower.startswith(q_lower):
            match_rank = 1
        else:
            match_rank = 2

        return (
            match_rank,
            name_lower,
            str(player.position or ""),
            str(player.mfl_id or ""),
        )

    rows.sort(key=sort_key)

    return [
        {
            "id": player.id,
            "mfl_id": player.mfl_id,
            "name": player.name,
            "position": player.position,
            "team": player.team,
            "status": player.status,
        }
        for player in rows[:limit]
    ]


# ---------------------------------------------------------------------------
# League helpers
# ---------------------------------------------------------------------------

def get_current_mfl_year(user_id: int) -> int | None:
    """
    Return the newest MFL season stored for this user.

    This prevents old/historical league rows from inflating the X/X
    denominator if multiple seasons exist in the database.
    """
    return (
        db.session.query(func.max(League.year))
        .filter(League.user_id == user_id)
        .scalar()
    )


def get_user_mfl_leagues(
    user_id: int,
    *,
    year: int | None = None,
) -> list[League]:
    """
    Return the user's MFL leagues for one season.

    If no year is supplied, use the newest season stored for that user.
    """
    if year is None:
        year = get_current_mfl_year(user_id)

    if year is None:
        return []

    return (
        League.query
        .filter(
            League.user_id == user_id,
            League.year == year,
        )
        .order_by(League.name.asc())
        .all()
    )


def _league_home_url(league: League) -> str | None:
    """
    Prefer the League model's host-aware URL builder.
    Fall back to the cached home_url if necessary.
    """
    try:
        url = league.url_for_league_home()
        if url:
            return url
    except Exception:
        pass

    return getattr(league, "home_url", None)


# ---------------------------------------------------------------------------
# Local availability
# ---------------------------------------------------------------------------

def get_players_availability(
    user_id: int,
    player_ids: list[int | str],
    *,
    year: int | None = None,
) -> dict[str, dict[str, Any]]:
    """
    Return availability for many players across all of a user's MFL leagues.

    IMPORTANT:
    This performs LOCAL DATABASE queries only.

    It does not call MFL once per league or once per player.

    Query pattern:
      1. Load the user's current leagues.
      2. One roster query for ALL requested players across ALL those leagues.
      3. Build the player x league availability matrix in memory.
    """
    normalized_player_ids: list[int] = []

    for raw_id in player_ids or []:
        try:
            pid = int(raw_id)
        except (TypeError, ValueError):
            continue

        if pid not in normalized_player_ids:
            normalized_player_ids.append(pid)

    if not normalized_player_ids:
        return {}

    leagues = get_user_mfl_leagues(user_id, year=year)
    league_ids = [league.id for league in leagues]

    if not league_ids:
        return {
            str(pid): {
                "player_id": pid,
                "available_count": 0,
                "total_leagues": 0,
                "rostered_count": 0,
                "available_leagues": [],
                "rostered_leagues": [],
            }
            for pid in normalized_player_ids
        }

    # One query finds every occurrence of all requested players across all of
    # this user's current MFL rosters.
    roster_rows = (
        db.session.query(
            Roster.player_id,
            Team.league_id,
            Team.mfl_id,
            Team.name,
        )
        .join(Team, Team.id == Roster.team_id)
        .filter(
            Team.league_id.in_(league_ids),
            Roster.player_id.in_(normalized_player_ids),
        )
        .all()
    )

    # player_id -> league_id -> roster owner details
    rostered_by_player: dict[int, dict[int, dict[str, Any]]] = {
        pid: {} for pid in normalized_player_ids
    }

    for player_id, league_id, team_mfl_id, team_name in roster_rows:
        rostered_by_player.setdefault(int(player_id), {})[int(league_id)] = {
            "team_mfl_id": team_mfl_id,
            "team_name": team_name,
        }

    results: dict[str, dict[str, Any]] = {}

    for player_id in normalized_player_ids:
        rostered_map = rostered_by_player.get(player_id, {})

        available_leagues = []
        rostered_leagues = []

        for league in leagues:
            owner = rostered_map.get(league.id)

            league_data = {
                "league_id": league.id,
                "mfl_id": league.mfl_id,
                "name": league.name,
                "year": league.year,
                "franchise_id": league.franchise_id,
                "synced_at": (
                    league.synced_at.isoformat()
                    if league.synced_at is not None
                    else None
                ),
                "home_url": _league_home_url(league),
            }

            if owner is None:
                available_leagues.append(league_data)
                continue

            owner_fid = str(owner.get("team_mfl_id") or "").zfill(4)
            my_fid = str(league.franchise_id or "").zfill(4)

            league_data.update(
                {
                    "rostered_by_franchise_id": owner.get("team_mfl_id"),
                    "rostered_by_team": owner.get("team_name"),
                    "rostered_by_me": bool(
                        owner_fid
                        and my_fid
                        and owner_fid == my_fid
                    ),
                }
            )

            rostered_leagues.append(league_data)

        results[str(player_id)] = {
            "player_id": player_id,
            "available_count": len(available_leagues),
            "total_leagues": len(leagues),
            "rostered_count": len(rostered_leagues),
            "available_leagues": available_leagues,
            "rostered_leagues": rostered_leagues,
        }

    return results


def get_player_availability(
    user_id: int,
    player_id: int | str,
    *,
    year: int | None = None,
) -> dict[str, Any] | None:
    """
    Convenience wrapper for one player.
    """
    try:
        pid = int(player_id)
    except (TypeError, ValueError):
        return None

    result = get_players_availability(
        user_id,
        [pid],
        year=year,
    )

    return result.get(str(pid))


# ---------------------------------------------------------------------------
# Live MFL playerStatus verification
# ---------------------------------------------------------------------------

def _normalize_host(value: str | None) -> str | None:
    if not value:
        return None

    raw = str(value).strip()
    if not raw:
        return None

    try:
        parsed = urlparse(raw)

        if parsed.netloc:
            return parsed.netloc

        return (
            raw
            .replace("https://", "")
            .replace("http://", "")
            .split("/", 1)[0]
            .strip()
        ) or None
    except Exception:
        return None


def _user_host_cookies(user) -> dict[str, str]:
    """
    Read the host-cookie bundle using the User helper when available.
    Fall back to the JSON field defensively.
    """
    try:
        helper = getattr(user, "get_mfl_host_cookies", None)
        if callable(helper):
            result = helper()
            if isinstance(result, dict):
                return {
                    str(k): str(v)
                    for k, v in result.items()
                    if k and v
                }
    except Exception:
        pass

    try:
        raw = getattr(user, "mfl_cookie_hosts_json", None) or "{}"
        result = json.loads(raw)

        if isinstance(result, dict):
            return {
                str(k): str(v)
                for k, v in result.items()
                if k and v
            }
    except Exception:
        pass

    return {}


def _player_status_candidates(user, league: League):
    """
    Yield client/cookie combinations for a live status request.

    Prefer the league host when we have a host-specific cookie, then fall
    back to the central API host.
    """
    host = _normalize_host(getattr(league, "league_host", None))
    host_cookies = _user_host_cookies(user)

    seen: set[tuple[str, str]] = set()

    if host and host_cookies.get(host):
        base_url = f"https://{host}/{league.year}/"
        cookie = host_cookies[host]

        key = (base_url, cookie)
        if key not in seen:
            seen.add(key)
            yield (
                MFLClient(
                    year=league.year,
                    base_url=base_url,
                ),
                cookie,
                host,
            )

    api_cookie = (
        getattr(user, "mfl_cookie_api", None)
        or getattr(user, "session_key", None)
    )

    if api_cookie:
        base_url = f"https://api.myfantasyleague.com/{league.year}/"
        key = (base_url, api_cookie)

        if key not in seen:
            yield (
                MFLClient(year=league.year),
                api_cookie,
                "api.myfantasyleague.com",
            )


def _resolve_waiver_transaction_client(user, league: League):
    """Resolve one authenticated client for an FCFS/BBID write.

    Authentication may fall back, but a waiver transaction's destination
    must always remain the league's own MFL host.
    """
    league_host = _normalize_host(getattr(league, "league_host", None))
    if not league_host or not re.fullmatch(
        r"www\d+\.myfantasyleague\.com",
        league_host,
        flags=re.IGNORECASE,
    ):
        raise RuntimeError(
            "MFL waiver transaction requires a valid league-specific wwwXX host."
        )

    league_host = league_host.lower()
    host_cookies = {
        str(host).lower(): cookie
        for host, cookie in _user_host_cookies(user).items()
    }

    cookie = host_cookies.get(league_host)
    auth_source = "host_cookie"
    if not str(cookie or "").strip():
        cookie = getattr(user, "mfl_cookie_api", None)
        auth_source = "api_cookie"
    if not str(cookie or "").strip():
        cookie = getattr(user, "session_key", None)
        auth_source = "legacy_session"
    if not str(cookie or "").strip():
        raise RuntimeError(
            "No usable MFL authentication cookie is available. "
            "Please re-link the MFL account."
        )

    client = MFLClient(
        year=league.year,
        base_url=f"https://{league_host}/{league.year}/",
    )
    return client, str(cookie), auth_source


def _classify_mfl_player_status(raw_status: str) -> dict[str, Any]:
    """
    Normalize MFL's human-readable playerStatus value.

    Observed MFL examples:
        "FA (Locked)"  -> unrostered, currently locked
        "FA"           -> unrostered
        "Owner - Team" -> rostered

    MFL does not reliably return a franchise id in playerStatus, so a
    non-FA status is retained as rostered_label rather than guessed apart.
    """
    value = (raw_status or "").strip()
    lower = value.lower()

    if not value:
        return {
            "classification": "UNKNOWN",
            "is_free_agent": None,
            "is_locked": None,
            "rostered_label": None,
        }

    is_free_agent = (
        lower == "fa"
        or lower.startswith("fa ")
        or lower.startswith("fa(")
        or lower.startswith("free agent")
    )

    is_locked = "locked" in lower

    if is_free_agent:
        classification = (
            "FREE_AGENT_LOCKED"
            if is_locked
            else "FREE_AGENT"
        )
        rostered_label = None
    else:
        classification = "ROSTERED"
        rostered_label = value

    return {
        "classification": classification,
        "is_free_agent": is_free_agent,
        "is_locked": is_locked,
        "rostered_label": rostered_label,
    }


def classify_waiver_action(
    classification: str,
    waiver_type: str | None,
    bbid_conditional: bool | None,
) -> str | None:
    """Return the only legal quick action for live player/league state."""
    status = str(classification or "").strip().upper()
    kind = str(waiver_type or "").strip().upper()
    if status not in {"FREE_AGENT", "FREE_AGENT_LOCKED"}:
        return None
    if kind == "FCFS":
        return "FCFS" if status == "FREE_AGENT" else None
    if kind == "BBID":
        return None if bbid_conditional is not False else "BBID"
    if kind == "BBID_FCFS":
        if status == "FREE_AGENT":
            return "FCFS"
        return None if bbid_conditional is not False else "BBID"
    return None


def classify_acquisition_status(
    classification: str,
    waiver_type: str | None,
    bbid_conditional: bool | None,
) -> str:
    """Return the concise, user-facing acquisition mechanism."""
    status = str(classification or "").strip().upper()
    kind = str(waiver_type or "").strip().upper()
    if status == "ROSTERED":
        return "Rostered"
    if status not in {"FREE_AGENT", "FREE_AGENT_LOCKED"}:
        return "Waiver"
    action = classify_waiver_action(status, kind, bbid_conditional)
    if action == "FCFS":
        return "FA"
    if action == "BBID":
        return "BBID"
    return "Waiver"


def validate_bbid_amount(bid_amount, faab_balance, minimum=None):
    """Normalize a quick-claim bid and enforce locally known FAAB limits."""
    from decimal import Decimal, InvalidOperation
    raw = str(bid_amount if bid_amount is not None else "").strip() or "0"
    try:
        bid = Decimal(raw)
        balance = Decimal(str(faab_balance))
        floor = Decimal(str(minimum)) if minimum is not None else None
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Invalid waiver bid amount.") from exc
    if not bid.is_finite() or bid < 0:
        raise ValueError("Waiver bid must be zero or greater.")
    if floor is not None and bid < floor:
        raise ValueError(f"Waiver bid must be at least {format(floor, 'f')}.")
    if bid > balance:
        raise ValueError("Waiver bid exceeds your current FAAB balance.")
    return bid


def parse_player_status_xml(
    xml_bytes: bytes,
    requested_player_ids: list[int | str],
) -> dict[str, dict[str, Any]]:
    """
    Parse MFL playerStatus XML defensively.

    MFL describes these statuses as indicating whether a player is locked,
    a free agent, or rostered. We retain the raw returned attributes so we
    can inspect real league responses during staging.
    """
    requested = {
        str(pid).strip()
        for pid in requested_player_ids or []
        if pid is not None and str(pid).strip()
    }

    if not xml_bytes:
        raise RuntimeError("MFL playerStatus returned an empty response")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise RuntimeError(
            f"Could not parse MFL playerStatus XML: {exc}"
        ) from exc

    # MFL sometimes returns an XML <error> payload with HTTP 200.
    error_node = None

    if root.tag.lower() == "error":
        error_node = root
    else:
        error_node = root.find(".//error")

    if error_node is not None:
        message = (
            error_node.attrib.get("message")
            or error_node.attrib.get("error")
            or (error_node.text or "").strip()
            or "Unknown MFL API error"
        )
        raise RuntimeError(f"MFL playerStatus error: {message}")

    results: dict[str, dict[str, Any]] = {}

    for elem in root.iter():
        attrs = dict(elem.attrib or {})

        player_id = (
            attrs.get("id")
            or attrs.get("player_id")
            or attrs.get("playerId")
            or attrs.get("player")
        )

        if not player_id:
            continue

        player_id = str(player_id).strip()

        if requested and player_id not in requested:
            continue

        status = (
            attrs.get("status")
            or attrs.get("playerStatus")
            or attrs.get("player_status")
            or ""
        )

        status = str(status).strip()
        interpreted = _classify_mfl_player_status(status)

        franchise_id = (
            attrs.get("franchise")
            or attrs.get("franchise_id")
            or attrs.get("franchiseId")
            or ""
        )

        results[player_id] = {
            "player_id": player_id,
            "status": status or "unknown",
            "classification": interpreted["classification"],
            "is_free_agent": interpreted["is_free_agent"],
            "is_locked": interpreted["is_locked"],
            "rostered_label": interpreted["rostered_label"],
            "franchise_id": str(franchise_id).strip() or None,
            "raw_attributes": attrs,
        }

    # Preserve requested players even if MFL omitted one from the response.
    for player_id in requested:
        results.setdefault(
            player_id,
            {
                "player_id": player_id,
                "status": "unknown",
                "classification": "UNKNOWN",
                "is_free_agent": None,
                "is_locked": None,
                "rostered_label": None,
                "franchise_id": None,
                "raw_attributes": {},
            },
        )

    return results


def get_live_player_status(
    user,
    league_id: int,
    player_ids: list[int | str],
) -> dict[str, Any]:
    """
    Make ONE batched playerStatus request for a specific RosterDash league.

    This function is intended for explicit live verification of one league.
    It should NOT be called automatically across every league during a
    normal player search.
    """
    league = (
        League.query
        .filter(
            League.id == int(league_id),
            League.user_id == user.id,
        )
        .first()
    )

    if league is None:
        raise LookupError("League not found for this user")

    # Waiver settings and current FAAB are already synchronized locally.
    # Include them with live playerStatus so the browser can decide which
    # quick action to render without making another MFL request.
    franchise_id = str(
        league.franchise_id or ""
    ).strip()

    team = None

    if franchise_id:
        team = (
            Team.query
            .filter_by(
                league_id=league.id,
                mfl_id=franchise_id,
            )
            .first()
        )

    def decimal_text(value):
        if value is None:
            return None

        return format(
            value,
            "f",
        )

    normalized_ids = []

    for raw_id in player_ids or []:
        if raw_id is None:
            continue

        value = str(raw_id).strip()

        if value and value not in normalized_ids:
            normalized_ids.append(value)

    if not normalized_ids:
        raise ValueError("At least one player id is required")

    candidates = list(_player_status_candidates(user, league))

    if not candidates:
        raise RuntimeError(
            "No usable MFL authentication cookie is available. "
            "Please re-link the MFL account."
        )

    errors = []

    for client, cookie, host_label in candidates:
        try:
            xml_bytes = client.get_player_status(
                league_id=league.mfl_id,
                player_ids=normalized_ids,
                cookie=cookie,
                context={
                    "operation": "waivers_live_player_status",
                    "rosterdash_league_id": league.id,
                },
            )

            statuses = parse_player_status_xml(
                xml_bytes,
                normalized_ids,
            )

            for status in statuses.values():
                status["quick_action"] = classify_waiver_action(
                    status.get("classification"),
                    league.waiver_type,
                    league.bbid_conditional,
                )
                status["visible_status"] = classify_acquisition_status(
                    status.get("classification"),
                    league.waiver_type,
                    league.bbid_conditional,
                )

            return {
                "league_id": league.id,
                "mfl_id": league.mfl_id,
                "league_name": league.name,
                "year": league.year,
                "home_url": _league_home_url(league),
                "source_host": host_label,
                "checked_at_utc": datetime.now(
                    timezone.utc
                ).isoformat(),
                "waiver": {
                    "waiver_type": (
                        league.waiver_type
                    ),
                    "bbid_conditional": (
                        league.bbid_conditional
                    ),
                    "faab_starting_balance": (
                        decimal_text(
                            league.faab_starting_balance
                        )
                    ),
                    "faab_minimum": (
                        decimal_text(
                            league.faab_minimum
                        )
                    ),
                    "faab_increment": (
                        decimal_text(
                            league.faab_increment
                        )
                    ),
                    "faab_fcfs_charge": (
                        decimal_text(
                            league.faab_fcfs_charge
                        )
                    ),
                    "max_waiver_rounds": (
                        league.max_waiver_rounds
                    ),
                    "faab_balance": (
                        decimal_text(
                            team.faab_balance
                        )
                        if team is not None
                        else None
                    ),
                    "waiver_sort_order": (
                        team.waiver_sort_order
                        if team is not None
                        else None
                    ),
                },
                "statuses": statuses,
            }

        except Exception as exc:
            errors.append(f"{host_label}: {exc}")

    raise RuntimeError(
        "MFL playerStatus failed for all available hosts: "
        + " | ".join(errors)
    )
# ------------------------- Dynasty target discovery --------------------------

_TARGET_POSITIONS = ("QB", "RB", "WR", "TE")
_DEFAULT_TARGETS_PER_POSITION_PER_LEAGUE = 5
_TOP_TARGET_THRESHOLD = 3


def _target_rank_sort_value(value):
    """Return a stable sortable value for positional ranks."""
    if value in (None, ""):
        return 10**9

    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 10**9


def get_dynasty_targets(
    user_id,
    year=None,
    per_position_per_league=_DEFAULT_TARGETS_PER_POSITION_PER_LEAGUE,
):
    """
    Build the cross-league dynasty waiver target pool.

    IMPORTANT:
    - No overall cross-position dynasty ranking is used.
    - Players are ranked only against others at the same position.
    - For EACH league independently, find the top N locally-unrostered
      players at QB/RB/WR/TE by positional dynasty consensus rank.
    - Union those league-level target lists into one user-level target pool.

    Returned player metrics:
      positional_rank
          Dynasty consensus rank within the player's position.

      top3_league_count
          Number of leagues where the player is one of the top three
          locally-unrostered players at his position.

      target_league_count
          Number of leagues where the player is in the top N
          locally-unrostered players at his position. With the default
          N=5, this is the "Top 5" count.

      unrostered_count
          Number of current MFL leagues where the local roster snapshot
          says the player is unrostered, whether or not he is top five.

      best_available_rank
          Best league-specific availability rank (#1 through #N).

      target_leagues
          League-specific detail including available_rank.

    This function is LOCAL DATABASE ONLY. It does not call MFL.
    """

    try:
        limit = int(per_position_per_league)
    except (TypeError, ValueError):
        limit = _DEFAULT_TARGETS_PER_POSITION_PER_LEAGUE

    limit = max(1, min(limit, 25))

    if year is None:
        year = get_current_mfl_year(user_id)

    if year is None:
        return {
            "year": None,
            "total_leagues": 0,
            "per_position_per_league": limit,
            "positions": [
                {
                    "position": position,
                    "count": 0,
                    "players": [],
                }
                for position in _TARGET_POSITIONS
            ],
        }

    leagues = get_user_mfl_leagues(
        user_id,
        year=year,
    )

    if not leagues:
        return {
            "year": year,
            "total_leagues": 0,
            "per_position_per_league": limit,
            "positions": [
                {
                    "position": position,
                    "count": 0,
                    "players": [],
                }
                for position in _TARGET_POSITIONS
            ],
        }

    league_ids = [
        league.id
        for league in leagues
    ]

    league_by_id = {
        league.id: league
        for league in leagues
    }

    # --------------------------------------------------------
    # 1. Load the positional dynasty rankings.
    #
    # We intentionally read the full ranked pool rather than
    # imposing an arbitrary top-100/top-200 cutoff. A deep league
    # may require us to scan well down a positional ranking before
    # finding five players who are actually unrostered.
    # --------------------------------------------------------

    consensus_rows = (
        DynastyRankConsensusCurrent.query
        .filter(
            DynastyRankConsensusCurrent.position.in_(
                _TARGET_POSITIONS
            )
        )
        .filter(
            DynastyRankConsensusCurrent.positional_rank.isnot(None)
        )
        .filter(
            DynastyRankConsensusCurrent.mfl_id.isnot(None)
        )
        .all()
    )

    ranked_by_position = {
        position: []
        for position in _TARGET_POSITIONS
    }

    seen_ranked_players = {
        position: set()
        for position in _TARGET_POSITIONS
    }

    for row in consensus_rows:
        position = str(
            row.position or ""
        ).upper().strip()

        if position not in ranked_by_position:
            continue

        mfl_id = str(
            row.mfl_id or ""
        ).strip()

        if not mfl_id:
            continue

        # Defensive duplicate protection.
        if mfl_id in seen_ranked_players[position]:
            continue

        seen_ranked_players[position].add(
            mfl_id
        )

        ranked_by_position[position].append(
            row
        )

    for position in _TARGET_POSITIONS:
        ranked_by_position[position].sort(
            key=lambda row: (
                _target_rank_sort_value(
                    row.positional_rank
                ),
                str(
                    row.player_name or ""
                ).lower(),
            )
        )

    # --------------------------------------------------------
    # 2. Load ALL locally-rostered MFL player IDs for the user's
    # current leagues in one query.
    # --------------------------------------------------------

    roster_rows = (
        Team.query
        .with_entities(
            Team.league_id,
            Player.mfl_id,
        )
        .join(
            Roster,
            Roster.team_id == Team.id,
        )
        .join(
            Player,
            Player.id == Roster.player_id,
        )
        .filter(
            Team.league_id.in_(
                league_ids
            )
        )
        .all()
    )

    owned_by_league = {
        league_id: set()
        for league_id in league_ids
    }

    for league_id, mfl_id in roster_rows:
        if (
            league_id not in owned_by_league
            or mfl_id in (None, "")
        ):
            continue

        owned_by_league[league_id].add(
            str(mfl_id).strip()
        )

    # --------------------------------------------------------
    # 3. For each league + position, walk down the positional
    # consensus until we have N ACTUALLY AVAILABLE players.
    #
    # This is what protects deep leagues. We do not stop scanning
    # just because the first 50/100 players are rostered.
    # --------------------------------------------------------

    targets_by_mfl_id = {}

    for league in leagues:
        league_owned = owned_by_league.get(
            league.id,
            set(),
        )

        for position in _TARGET_POSITIONS:
            available_rank = 0

            for consensus in ranked_by_position[position]:
                mfl_id = str(
                    consensus.mfl_id or ""
                ).strip()

                if not mfl_id:
                    continue

                if mfl_id in league_owned:
                    continue

                available_rank += 1

                if available_rank > limit:
                    break

                target = targets_by_mfl_id.get(
                    mfl_id
                )

                if target is None:
                    target = {
                        "player_id": None,
                        "mfl_id": mfl_id,
                        "name": (
                            consensus.player_name
                            or f"Player {mfl_id}"
                        ),
                        "position": position,
                        "team": None,
                        "status": None,
                        "positional_rank": (
                            consensus.positional_rank
                        ),
                        "top3_league_count": 0,
                        "target_league_count": 0,
                        "unrostered_count": 0,
                        "other_unrostered_count": 0,
                        "best_available_rank": None,
                        "target_leagues": [],
                        "reasons": [
                            {
                                "key": "dynasty_rank",
                                "label": "Dynasty Rank",
                            }
                        ],
                    }

                    targets_by_mfl_id[
                        mfl_id
                    ] = target

                target[
                    "target_league_count"
                ] += 1

                if (
                    available_rank
                    <= _TOP_TARGET_THRESHOLD
                ):
                    target[
                        "top3_league_count"
                    ] += 1

                previous_best = target.get(
                    "best_available_rank"
                )

                if (
                    previous_best is None
                    or available_rank < previous_best
                ):
                    target[
                        "best_available_rank"
                    ] = available_rank

                target[
                    "target_leagues"
                ].append(
                    {
                        "league_id": league.id,
                        "mfl_id": league.mfl_id,
                        "name": league.name,
                        "year": league.year,
                        "synced_at": (
                            league.synced_at.isoformat()
                            if league.synced_at
                            else None
                        ),
                        "home_url": (
                            getattr(
                                league,
                                "home_url",
                                None,
                            )
                            or (
                                league.url_for_league_home()
                                if hasattr(
                                    league,
                                    "url_for_league_home",
                                )
                                else None
                            )
                        ),
                        "available_rank": (
                            available_rank
                        ),
                        "available_rank_label": (
                            f"#{available_rank} "
                            f"{position}"
                        ),
                    }
                )

    if not targets_by_mfl_id:
        return {
            "year": year,
            "total_leagues": len(leagues),
            "per_position_per_league": limit,
            "positions": [
                {
                    "position": position,
                    "count": 0,
                    "players": [],
                }
                for position in _TARGET_POSITIONS
            ],
        }

    # --------------------------------------------------------
    # 4. Enrich targets with the local Player row.
    # --------------------------------------------------------

    target_mfl_ids = list(
        targets_by_mfl_id.keys()
    )

    local_players = (
        Player.query
        .filter(
            Player.mfl_id.in_(
                target_mfl_ids
            )
        )
        .all()
    )

    local_player_by_mfl_id = {
        str(player.mfl_id): player
        for player in local_players
        if player.mfl_id not in (None, "")
    }

    # --------------------------------------------------------
    # 5. Calculate portfolio-wide unrostered counts.
    # --------------------------------------------------------

    for mfl_id, target in targets_by_mfl_id.items():
        local_player = local_player_by_mfl_id.get(
            mfl_id
        )

        if local_player is not None:
            target["player_id"] = local_player.id
            target["team"] = local_player.team
            target["status"] = local_player.status

            # Prefer canonical local player name when available.
            if local_player.name:
                target["name"] = local_player.name

        unrostered_count = sum(
            1
            for league in leagues
            if mfl_id
            not in owned_by_league.get(
                league.id,
                set(),
            )
        )

        target[
            "unrostered_count"
        ] = unrostered_count

        target[
            "other_unrostered_count"
        ] = max(
            0,
            unrostered_count
            - target[
                "target_league_count"
            ],
        )

        target["target_leagues"].sort(
            key=lambda item: (
                item.get(
                    "available_rank",
                    999,
                ),
                str(
                    item.get("name") or ""
                ).lower(),
            )
        )

    # --------------------------------------------------------
    # 6. Group by position.
    #
    # DEFAULT ORDER WITHIN EACH POSITION:
    #   1. Most Top-3 target leagues
    #   2. Most Top-5 target leagues
    #   3. Better positional consensus rank
    #   4. More leagues where locally unrostered
    #   5. Player name
    #
    # We intentionally DO NOT compare one position against another.
    # --------------------------------------------------------

    grouped = {
        position: []
        for position in _TARGET_POSITIONS
    }

    for target in targets_by_mfl_id.values():
        position = target.get(
            "position"
        )

        if position in grouped:
            grouped[position].append(
                target
            )

    position_groups = []

    for position in _TARGET_POSITIONS:
        players = grouped[position]

        players.sort(
            key=lambda player: (
                -int(
                    player.get(
                        "top3_league_count",
                        0,
                    )
                    or 0
                ),
                -int(
                    player.get(
                        "target_league_count",
                        0,
                    )
                    or 0
                ),
                _target_rank_sort_value(
                    player.get(
                        "positional_rank"
                    )
                ),
                -int(
                    player.get(
                        "unrostered_count",
                        0,
                    )
                    or 0
                ),
                str(
                    player.get(
                        "name"
                    )
                    or ""
                ).lower(),
            )
        )

        position_groups.append(
            {
                "position": position,
                "count": len(players),
                "players": players,
            }
        )

    return {
        "year": year,
        "total_leagues": len(leagues),
        "per_position_per_league": limit,
        "top_target_threshold": (
            _TOP_TARGET_THRESHOLD
        ),
        "reason_filters": [
            {
                "key": "dynasty_rank",
                "label": "Dynasty Rank",
            }
        ],
        "positions": position_groups,
    }

# ------------------------- FCFS drop candidates ------------------------------


def get_fcfs_drop_candidates(
    user_id: int,
    league_id: int,
) -> dict:
    """
    Return the logged-in user's locally-synced roster for one MFL league,
    ordered for a rapid FCFS add/drop workflow.

    Ordering:
      1. Players with no positional dynasty consensus rank.
      2. Ranked players from worst positional consensus rank to best.
      3. Name as a stable tiebreaker.

    Notes:
      - No cross-position overall ranking is used.
      - Roster entries without a usable mapped Player identity are ignored.
      - This function is LOCAL DATABASE ONLY and makes no MFL calls.
      - "None" is a UI transaction option and is not returned as a fake player.
    """

    # Local imports intentionally keep this helper independent of the
    # existing top-of-file import layout.
    from datetime import datetime

    from models import (
        DynastyRankConsensusCurrent,
        League,
        Player,
        Roster,
        Team,
    )

    try:
        league_id_int = int(league_id)
    except (TypeError, ValueError):
        raise LookupError("Invalid league ID.")

    current_year = datetime.utcnow().year

    league = (
        League.query
        .filter_by(
            id=league_id_int,
            user_id=user_id,
            year=current_year,
        )
        .first()
    )

    if league is None:
        raise LookupError(
            f"{current_year} MFL league not found."
        )

    franchise_id = str(
        league.franchise_id or ""
    ).strip()

    if not franchise_id:
        raise LookupError(
            "Your franchise could not be identified for this league."
        )

    team = (
        Team.query
        .filter_by(
            league_id=league.id,
            mfl_id=franchise_id,
        )
        .first()
    )

    if team is None:
        raise LookupError(
            "Your synced team roster could not be found."
        )

    roster_rows = (
        Roster.query
        .filter_by(
            team_id=team.id,
        )
        .all()
    )

    if not roster_rows:
        return {
            "league_id": league.id,
            "mfl_id": league.mfl_id,
            "league_name": league.name,
            "franchise_id": franchise_id,
            "team_id": team.id,
            "team_name": team.name,
            "candidates": [],
            "ignored_count": 0,
        }

    player_ids = [
        row.player_id
        for row in roster_rows
        if row.player_id is not None
    ]

    players = (
        Player.query
        .filter(
            Player.id.in_(player_ids)
        )
        .all()
    )

    player_by_id = {
        player.id: player
        for player in players
    }

    usable_players = []

    ignored_count = 0

    for roster_row in roster_rows:
        player = player_by_id.get(
            roster_row.player_id
        )

        if player is None:
            ignored_count += 1
            continue

        mfl_id = str(
            player.mfl_id or ""
        ).strip()

        name = str(
            player.name or ""
        ).strip()

        position = str(
            player.position or ""
        ).strip().upper()

        # Asset sync can create placeholder Player records for an MFL ID
        # before the master player pool has supplied useful identity data.
        # Those should never be suggested as a drop candidate.
        if (
            not mfl_id
            or not name
            or not position
        ):
            ignored_count += 1
            continue

        usable_players.append(
            {
                "roster": roster_row,
                "player": player,
                "mfl_id": mfl_id,
                "name": name,
                "position": position,
            }
        )

    if not usable_players:
        return {
            "league_id": league.id,
            "mfl_id": league.mfl_id,
            "league_name": league.name,
            "franchise_id": franchise_id,
            "team_id": team.id,
            "team_name": team.name,
            "candidates": [],
            "ignored_count": ignored_count,
        }

    roster_mfl_ids = [
        item["mfl_id"]
        for item in usable_players
    ]

    consensus_rows = (
        DynastyRankConsensusCurrent.query
        .filter(
            DynastyRankConsensusCurrent.mfl_id.in_(
                roster_mfl_ids
            )
        )
        .all()
    )

    # Normally there is one consensus row per player because the mapped
    # position is stable. If multiple rows ever exist, prefer the row whose
    # position matches the local Player position.
    consensus_by_mfl_id = {}

    for consensus in consensus_rows:
        mfl_id = str(
            consensus.mfl_id or ""
        ).strip()

        if not mfl_id:
            continue

        current = consensus_by_mfl_id.get(
            mfl_id
        )

        if current is None:
            consensus_by_mfl_id[mfl_id] = consensus
            continue

        local_position = next(
            (
                item["position"]
                for item in usable_players
                if item["mfl_id"] == mfl_id
            ),
            None,
        )

        if (
            local_position
            and str(consensus.position or "").upper()
            == local_position
        ):
            consensus_by_mfl_id[mfl_id] = consensus

    candidates = []

    for item in usable_players:
        player = item["player"]
        roster_row = item["roster"]

        consensus = consensus_by_mfl_id.get(
            item["mfl_id"]
        )

        positional_rank = None

        if (
            consensus is not None
            and consensus.positional_rank is not None
        ):
            try:
                positional_rank = int(
                    consensus.positional_rank
                )
            except (TypeError, ValueError):
                positional_rank = None

        is_unranked = (
            positional_rank is None
        )

        rank_label = (
            "Unranked"
            if is_unranked
            else f"{item['position']}{positional_rank}"
        )

        candidates.append(
            {
                "player_id": player.id,
                "mfl_id": item["mfl_id"],
                "name": item["name"],
                "position": item["position"],
                "team": player.team,
                "status": player.status,
                "is_unranked": is_unranked,
                "positional_rank": positional_rank,
                "rank_label": rank_label,
                "is_starter": bool(
                    getattr(
                        roster_row,
                        "is_starter",
                        False,
                    )
                ),
                "in_ir": bool(
                    getattr(
                        roster_row,
                        "in_ir",
                        False,
                    )
                ),
            }
        )

    # Unranked first.
    #
    # Ranked players then use INVERSE positional consensus:
    # RB109 is offered before RB35 because RB109 is the weaker
    # dynasty hold within his position.
    candidates.sort(
        key=lambda player: (
            0
            if player["is_unranked"]
            else 1,

            0
            if player["is_unranked"]
            else -int(
                player["positional_rank"]
            ),

            str(
                player["position"] or ""
            ),

            str(
                player["name"] or ""
            ).lower(),
        )
    )

    return {
        "league_id": league.id,
        "mfl_id": league.mfl_id,
        "league_name": league.name,
        "franchise_id": franchise_id,
        "team_id": team.id,
        "team_name": team.name,
        "candidates": candidates,
        "ignored_count": ignored_count,
    }

# ------------------------- FCFS transaction execution ------------------------


def _resolve_fcfs_player(
    raw_player_id,
) -> Player | None:
    """
    Resolve either a local Player.id or an MFL player id.

    Targets normally supplies the local Player.id, but accepting either
    keeps the transaction service explicit and reusable.
    """

    if raw_player_id in (
        None,
        "",
    ):
        return None

    raw = str(
        raw_player_id
    ).strip()

    if not raw:
        return None

    player = None

    try:
        local_id = int(raw)

        player = (
            Player.query
            .filter_by(
                id=local_id,
            )
            .first()
        )

    except (TypeError, ValueError):
        player = None

    if player is not None:
        return player

    return (
        Player.query
        .filter_by(
            mfl_id=raw,
        )
        .first()
    )


def perform_fcfs_add(
    user,
    league_id: int,
    add_player_id,
    drop_player_id=None,
) -> dict[str, Any]:
    """
    Execute one immediate MFL FCFS add/drop transaction.

    This function intentionally DOES NOT:
      - call playerStatus again;
      - refresh MFL assets after success;
      - mutate the local RosterDash roster;
      - try a second authentication cookie after a transaction attempt.

    The UI has just performed a live playerStatus check. MFL's
    fcfsWaiver response is the authoritative final result.

    Success:
        <status>OK</status>
        -> message "Added"

    Failure:
        <error>...</error>
        -> preserve every MFL error string.
    """

    from datetime import timedelta

    try:
        league_id_int = int(
            league_id
        )
    except (TypeError, ValueError):
        raise ValueError(
            "Invalid league ID."
        )

    current_year = datetime.now(
        timezone.utc
    ).year

    league = (
        League.query
        .filter(
            League.id == league_id_int,
            League.user_id == user.id,
            League.year == current_year,
        )
        .first()
    )

    if league is None:
        raise LookupError(
            f"{current_year} league not found for this user."
        )

    # Transaction execution requires a known current roster snapshot.
    if league.synced_at is None:
        raise ValueError(
            "Roster refresh required before performing an FCFS add."
        )

    # Match the existing Waiver Targets transaction-readiness window.
    now_naive_utc = datetime.utcnow()

    synced_at = league.synced_at

    if getattr(
        synced_at,
        "tzinfo",
        None,
    ) is not None:
        synced_at = (
            synced_at
            .astimezone(
                timezone.utc
            )
            .replace(
                tzinfo=None
            )
        )

    if (
        now_naive_utc
        - synced_at
        > timedelta(hours=4)
    ):
        raise ValueError(
            "Roster refresh required before performing an FCFS add."
        )

    # --------------------------------------------------------
    # Resolve the player being added.
    # --------------------------------------------------------

    add_player = _resolve_fcfs_player(
        add_player_id
    )

    if add_player is None:
        raise LookupError(
            "Add player not found."
        )

    add_mfl_id = str(
        add_player.mfl_id or ""
    ).strip()

    if (
        not add_mfl_id
        or not str(
            add_player.name or ""
        ).strip()
    ):
        raise ValueError(
            "Add player is not mapped to the MFL player pool."
        )

    # --------------------------------------------------------
    # Optional drop.
    #
    # We do not merely trust a browser-supplied player ID.
    # The selected player must still exist on this user's
    # locally-synced franchise roster for this league.
    # --------------------------------------------------------

    drop_player = None

    drop_mfl_id = None

    if drop_player_id not in (
        None,
        "",
        "none",
        "NONE",
        0,
        "0",
    ):

        drop_player = _resolve_fcfs_player(
            drop_player_id
        )

        if drop_player is None:
            raise LookupError(
                "Drop player not found."
            )

        drop_mfl_id = str(
            drop_player.mfl_id or ""
        ).strip()

        if (
            not drop_mfl_id
            or not str(
                drop_player.name or ""
            ).strip()
        ):
            raise ValueError(
                "Drop player is not mapped to the MFL player pool."
            )

        if drop_mfl_id == add_mfl_id:
            raise ValueError(
                "The add and drop player cannot be the same player."
            )

        franchise_id = str(
            league.franchise_id or ""
        ).strip()

        if not franchise_id:
            raise LookupError(
                "Your franchise could not be identified for this league."
            )

        team = (
            Team.query
            .filter_by(
                league_id=league.id,
                mfl_id=franchise_id,
            )
            .first()
        )

        if team is None:
            raise LookupError(
                "Your synced team roster could not be found."
            )

        membership = (
            Roster.query
            .filter_by(
                team_id=team.id,
                player_id=drop_player.id,
            )
            .first()
        )

        if membership is None:
            raise ValueError(
                "The selected drop player is not on your roster in this league."
            )

    # --------------------------------------------------------
    # Authentication may fall back; the transaction host may not.
    # --------------------------------------------------------

    # Select exactly one cookie before the write. If the FCFS POST fails or
    # its response is ambiguous, never replay it with another cookie.
    client, cookie, auth_source = (
        _resolve_waiver_transaction_client(user, league)
    )

    # --------------------------------------------------------
    # REAL WRITE.
    #
    # No playerStatus request happens here.
    # No automatic retry happens inside submit_fcfs_waiver().
    # --------------------------------------------------------

    result = client.submit_fcfs_waiver(
        league_id=league.mfl_id,
        add_player_id=add_mfl_id,
        cookie=cookie,
        drop_player_ids=(
            [drop_mfl_id]
            if drop_mfl_id
            else []
        ),
        context={
            "operation": (
                "waivers_perform_fcfs_add"
            ),
            "rosterdash_league_id": (
                league.id
            ),
            "intended_transaction_host": _normalize_host(league.league_host),
            "auth_source": auth_source,
        },
    )

    errors = [
        str(error)
        for error in (
            result.get("errors")
            or []
        )
        if str(error).strip()
    ]

    ok = bool(
        result.get("ok")
    )

    message = str(
        result.get("message")
        or (
            "Added"
            if ok
            else "MFL transaction failed."
        )
    )

    return {
        "ok": ok,
        "message": (
            "Added"
            if ok
            else message
        ),
        "errors": errors,
        "mfl_status": (
            result.get("status")
        ),
        "http_status": (
            result.get("http_status")
        ),
        "league": {
            "league_id": league.id,
            "mfl_id": league.mfl_id,
            "name": league.name,
            "year": league.year,
        },
        "add_player": {
            "player_id": add_player.id,
            "mfl_id": add_mfl_id,
            "name": add_player.name,
            "position": add_player.position,
        },
        "drop_player": (
            {
                "player_id": (
                    drop_player.id
                ),
                "mfl_id": (
                    drop_mfl_id
                ),
                "name": (
                    drop_player.name
                ),
                "position": (
                    drop_player.position
                ),
            }
            if drop_player
            else None
        ),
        "auth_source": auth_source,

        # Explicitly documenting the rapid-fire behavior in the
        # service response helps keep future callers from assuming
        # we refreshed assets after the write.
        "local_roster_refreshed": False,
    }
# ------------------------- BBID transaction execution ------------------------


def perform_blind_bid_add(
    user,
    league_id: int,
    add_player_id,
    bid_amount,
    drop_player_id=None,
) -> dict[str, Any]:
    """
    Submit one non-conditional MFL blind-bid waiver request.

    This is the quick-claim BBID path. It intentionally DOES NOT:
      - support conditional BBID leagues;
      - use ROUND;
      - use REPLACE;
      - call playerStatus again;
      - refresh MFL assets after success;
      - mutate local FAAB or roster state;
      - retry with another authentication cookie after a write attempt.

    The caller is responsible for performing the immediately preceding
    live playerStatus check. MFL's transaction response remains the
    authoritative final result.
    """

    from datetime import timedelta
    from decimal import Decimal, InvalidOperation

    # --------------------------------------------------------
    # League ownership/current-season validation.
    # --------------------------------------------------------
    try:
        league_id_int = int(
            league_id
        )
    except (TypeError, ValueError):
        raise ValueError(
            "Invalid league ID."
        )

    current_year = datetime.now(
        timezone.utc
    ).year

    league = (
        League.query
        .filter(
            League.id == league_id_int,
            League.user_id == user.id,
            League.year == current_year,
        )
        .first()
    )

    if league is None:
        raise LookupError(
            f"{current_year} league not found for this user."
        )

    # --------------------------------------------------------
    # Require the new waiver settings.
    # --------------------------------------------------------
    waiver_type = str(
        getattr(
            league,
            "waiver_type",
            None,
        )
        or ""
    ).strip().upper()

    if not waiver_type:
        raise ValueError(
            "Waiver settings are not available for this league."
        )

    if waiver_type not in {
        "BBID",
        "BBID_FCFS",
    }:
        raise ValueError(
            "This league does not use blind-bid waivers."
        )

    conditional = getattr(
        league,
        "bbid_conditional",
        None,
    )

    if conditional is None:
        raise ValueError(
            "Blind-bid waiver settings are incomplete for this league."
        )

    if bool(conditional):
        raise ValueError(
            "Conditional blind-bid waivers are not supported "
            "by quick claims yet."
        )

    # --------------------------------------------------------
    # Require the same fresh roster snapshot used by FCFS.
    # --------------------------------------------------------
    if league.synced_at is None:
        raise ValueError(
            "Roster refresh required before submitting a waiver bid."
        )

    now_naive_utc = datetime.utcnow()
    synced_at = league.synced_at

    if getattr(
        synced_at,
        "tzinfo",
        None,
    ) is not None:
        synced_at = (
            synced_at
            .astimezone(
                timezone.utc
            )
            .replace(
                tzinfo=None
            )
        )

    if (
        now_naive_utc
        - synced_at
        > timedelta(hours=4)
    ):
        raise ValueError(
            "Roster refresh required before submitting a waiver bid."
        )

    # --------------------------------------------------------
    # Identify the user's synced team.
    # BBID needs this even when there is no drop because current
    # available FAAB is stored on Team.
    # --------------------------------------------------------
    franchise_id = str(
        league.franchise_id or ""
    ).strip()

    if not franchise_id:
        raise LookupError(
            "Your franchise could not be identified for this league."
        )

    team = (
        Team.query
        .filter_by(
            league_id=league.id,
            mfl_id=franchise_id,
        )
        .first()
    )

    if team is None:
        raise LookupError(
            "Your synced team roster could not be found."
        )

    # --------------------------------------------------------
    # Validate bid amount using Decimal.
    # --------------------------------------------------------
    raw_bid = str(
        bid_amount
        if bid_amount is not None
        else ""
    ).strip()

    if not raw_bid:
        raw_bid = "0"

    faab_balance_raw = getattr(
        team,
        "faab_balance",
        None,
    )

    if faab_balance_raw is None:
        raise ValueError(
            "Current FAAB balance is not available for this league."
        )

    try:
        faab_balance = Decimal(
            str(
                faab_balance_raw
            )
        )
    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ) as exc:
        raise ValueError(
            "Current FAAB balance is invalid."
        ) from exc

    minimum_raw = getattr(
        league,
        "faab_minimum",
        None,
    )

    minimum = None

    if minimum_raw is not None:
        try:
            minimum = Decimal(
                str(
                    minimum_raw
                )
            )
        except (
            InvalidOperation,
            ValueError,
            TypeError,
        ) as exc:
            raise ValueError(
                "League minimum waiver bid is invalid."
            ) from exc

    bid = validate_bbid_amount(raw_bid, faab_balance, minimum)

    # Do not guess at bbidIncrement semantics here.
    # MFL remains authoritative for any additional bid constraint.

    # --------------------------------------------------------
    # Resolve player being added.
    # --------------------------------------------------------
    add_player = _resolve_fcfs_player(
        add_player_id
    )

    if add_player is None:
        raise LookupError(
            "Add player not found."
        )

    add_mfl_id = str(
        add_player.mfl_id or ""
    ).strip()

    if (
        not add_mfl_id
        or not str(
            add_player.name or ""
        ).strip()
    ):
        raise ValueError(
            "Add player is not mapped to the MFL player pool."
        )

    # --------------------------------------------------------
    # Optional drop.
    # Never trust a browser-supplied player ID without verifying
    # that the player is actually on this user's synced roster.
    # --------------------------------------------------------
    drop_player = None
    drop_mfl_id = None

    if drop_player_id not in (
        None,
        "",
        "none",
        "NONE",
        0,
        "0",
    ):
        drop_player = _resolve_fcfs_player(
            drop_player_id
        )

        if drop_player is None:
            raise LookupError(
                "Drop player not found."
            )

        drop_mfl_id = str(
            drop_player.mfl_id or ""
        ).strip()

        if (
            not drop_mfl_id
            or not str(
                drop_player.name or ""
            ).strip()
        ):
            raise ValueError(
                "Drop player is not mapped to the MFL player pool."
            )

        if drop_mfl_id == add_mfl_id:
            raise ValueError(
                "The add and drop player cannot be the same player."
            )

        membership = (
            Roster.query
            .filter_by(
                team_id=team.id,
                player_id=drop_player.id,
            )
            .first()
        )

        if membership is None:
            raise ValueError(
                "The selected drop player is not on your roster "
                "in this league."
            )

    # --------------------------------------------------------
    # Authentication.
    #
    # Authentication may fall back; the transaction host may not. Select
    # exactly one combination before the write and never replay the bid.
    # --------------------------------------------------------
    client, cookie, auth_source = (
        _resolve_waiver_transaction_client(user, league)
    )

    # --------------------------------------------------------
    # REAL WRITE.
    #
    # No ROUND.
    # No REPLACE.
    # No retry.
    # --------------------------------------------------------
    result = client.submit_blind_bid_waiver(
        league_id=league.mfl_id,
        bids=[
            {
                "player_id": add_mfl_id,
                "amount": bid,
                "drop_player_id": (
                    drop_mfl_id
                    if drop_mfl_id
                    else None
                ),
            }
        ],
        cookie=cookie,
        franchise_id=franchise_id,
        context={
            "operation": (
                "waivers_perform_blind_bid_add"
            ),
            "rosterdash_league_id": (
                league.id
            ),
            "intended_transaction_host": _normalize_host(league.league_host),
            "auth_source": auth_source,
        },
    )

    errors = [
        str(error)
        for error in (
            result.get("errors")
            or []
        )
        if str(error).strip()
    ]

    ok = bool(
        result.get("ok")
    )

    message = str(
        result.get("message")
        or (
            "Waiver bid submitted"
            if ok
            else "MFL transaction failed."
        )
    )

    return {
        "ok": ok,
        "message": (
            "Waiver bid submitted"
            if ok
            else message
        ),
        "errors": errors,
        "mfl_status": (
            result.get("status")
        ),
        "http_status": (
            result.get("http_status")
        ),
        "waiver_type": waiver_type,
        "bid_amount": format(
            bid,
            "f",
        ),
        "faab_balance_before": format(
            faab_balance,
            "f",
        ),
        "league": {
            "league_id": league.id,
            "mfl_id": league.mfl_id,
            "name": league.name,
            "year": league.year,
        },
        "add_player": {
            "player_id": add_player.id,
            "mfl_id": add_mfl_id,
            "name": add_player.name,
            "position": add_player.position,
        },
        "drop_player": (
            {
                "player_id": (
                    drop_player.id
                ),
                "mfl_id": (
                    drop_mfl_id
                ),
                "name": (
                    drop_player.name
                ),
                "position": (
                    drop_player.position
                ),
            }
            if drop_player
            else None
        ),
        "auth_source": auth_source,

        # A pending BBID does not mean the roster or FAAB balance
        # should be mutated locally.
        "local_roster_refreshed": False,
        "local_faab_updated": False,
    }
