from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from models import League, Player
from services.waivers_service import (
    perform_blind_bid_add,
    perform_fcfs_add,
    get_fcfs_drop_candidates,
    get_dynasty_targets,
    get_live_player_status,
    get_player_availability,
    get_players_availability,
    search_players,
)

from . import waivers_bp


@waivers_bp.route("/", methods=["GET"])
@login_required
def index():
    """
    Waiver discovery home.

    The template is added in the next implementation step.
    """
    current_year = datetime.now(timezone.utc).year
    leagues = League.query.filter_by(
        user_id=current_user.id,
        year=current_year,
    ).all()

    needs_waiver_sync = any(
        not str(league.waiver_type or "").strip()
        for league in leagues
    )

    deep_link = None
    player_id = request.args.get("player_id", type=int)
    league_id = request.args.get("league_id", type=int)

    # Both values must resolve together. In particular, scope the league query
    # to the signed-in user before exposing its database id to the browser.
    if player_id and league_id:
        player = Player.query.filter_by(id=player_id).first()
        league = League.query.filter_by(
            id=league_id,
            user_id=current_user.id,
            year=current_year,
        ).first()
        if player is not None and league is not None:
            deep_link = {
                "player_id": player.id,
                "league_id": league.id,
            }

    return render_template(
        "waivers/index.html",
        needs_waiver_sync=needs_waiver_sync,
        waiver_deep_link=deep_link,
    )



@waivers_bp.route("/api/targets", methods=["GET"])
@login_required
def api_targets():
    """
    Current-season cross-league dynasty waiver targets.

    Targets never falls back to an older MFL season.

    Never-synced current-season leagues block Targets.
    Stale leagues remain browseable, but freshness metadata
    tells the UI whether transactions should be allowed.

    No MFL requests are made here.
    """

    current_year = datetime.utcnow().year

    leagues = (
        League.query
        .filter_by(
            user_id=current_user.id,
            year=current_year,
        )
        .order_by(
            League.name,
            League.id,
        )
        .all()
    )

    # Current-season leagues are mandatory.
    if not leagues:
        return (
            jsonify(
                {
                    "ok": False,
                    "code": "CURRENT_SEASON_SYNC_REQUIRED",
                    "year": current_year,
                    "error": (
                        f"{current_year} league sync required."
                    ),
                    "message": (
                        f"Add or sync your {current_year} "
                        "MFL leagues before using Waiver Targets."
                    ),
                }
            ),
            409,
        )

    # Every current-season league must have at least one
    # completed asset refresh.
    unsynced = [
        league
        for league in leagues
        if league.synced_at is None
    ]

    if unsynced:
        return (
            jsonify(
                {
                    "ok": False,
                    "code": "ASSET_SYNC_REQUIRED",
                    "year": current_year,
                    "error": (
                        f"{current_year} roster refresh required."
                    ),
                    "message": (
                        f"{len(unsynced)} of {len(leagues)} "
                        "current-season leagues have not completed "
                        "an asset refresh."
                    ),
                    "league_count": len(leagues),
                    "unsynced_count": len(unsynced),
                    "unsynced_leagues": [
                        {
                            "league_id": league.id,
                            "mfl_id": league.mfl_id,
                            "name": league.name,
                        }
                        for league in unsynced
                    ],
                }
            ),
            409,
        )

    # Four-hour freshness window for transaction readiness.
    # Stale data can still be browsed.
    freshness_hours = 4

    cutoff = (
        datetime.utcnow()
        - timedelta(hours=freshness_hours)
    )

    stale = [
        league
        for league in leagues
        if league.synced_at < cutoff
    ]

    targets = get_dynasty_targets(
        current_user.id,
        year=current_year,
        per_position_per_league=5,
    )

    # Defensive invariant: this endpoint must never allow the
    # service to fall back to an old season.
    if targets.get("year") != current_year:
        return (
            jsonify(
                {
                    "ok": False,
                    "code": "TARGET_SEASON_MISMATCH",
                    "error": (
                        "Target season did not match "
                        "the current league season."
                    ),
                }
            ),
            500,
        )

    freshness = {
        "year": current_year,
        "league_count": len(leagues),
        "fresh_count": (
            len(leagues) - len(stale)
        ),
        "stale_count": len(stale),
        "unsynced_count": 0,
        "freshness_hours": freshness_hours,
        "transaction_ready": (
            len(stale) == 0
        ),
        "stale_leagues": [
            {
                "league_id": league.id,
                "mfl_id": league.mfl_id,
                "name": league.name,
                "synced_at": (
                    league.synced_at.isoformat()
                    if league.synced_at
                    else None
                ),
            }
            for league in stale
        ],
    }

    return jsonify(
        {
            "ok": True,
            "targets": targets,
            "freshness": freshness,
        }
    )


@waivers_bp.route("/api/search", methods=["GET"])
@login_required
def api_search():
    """
    Search the local player table and attach local MFL availability counts.

    No external MFL request is made by this endpoint.
    """
    query = (request.args.get("q") or "").strip()

    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20

    limit = max(1, min(limit, 30))

    if len(query) < 2:
        return jsonify(
            {
                "ok": True,
                "query": query,
                "results": [],
                "message": "Enter at least 2 characters.",
            }
        )

    players = search_players(
        query,
        limit=limit,
    )

    if not players:
        return jsonify(
            {
                "ok": True,
                "query": query,
                "results": [],
            }
        )

    availability = get_players_availability(
        current_user.id,
        [player["id"] for player in players],
    )

    results = []

    for player in players:
        player_id = int(player["id"])
        avail = availability.get(str(player_id), {})

        results.append(
            {
                **player,
                "available_count": int(
                    avail.get("available_count", 0) or 0
                ),
                "total_leagues": int(
                    avail.get("total_leagues", 0) or 0
                ),
                "rostered_count": int(
                    avail.get("rostered_count", 0) or 0
                ),
            }
        )

    return jsonify(
        {
            "ok": True,
            "query": query,
            "results": results,
        }
    )


@waivers_bp.route(
    "/api/player/<int:player_id>",
    methods=["GET"],
)
@login_required
def api_player(player_id: int):
    """
    Return one player's local availability details, including the MFL league
    URLs for leagues where the player is currently unrostered in our DB.

    No external MFL request is made by this endpoint.
    """
    player = Player.query.filter_by(id=player_id).first()

    if player is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Player not found.",
                }
            ),
            404,
        )

    availability = get_player_availability(
        current_user.id,
        player_id,
    )

    if availability is None:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "Could not calculate player availability.",
                }
            ),
            400,
        )

    return jsonify(
        {
            "ok": True,
            "player": {
                "id": player.id,
                "mfl_id": player.mfl_id,
                "name": player.name,
                "position": player.position,
                "team": player.team,
                "status": player.status,
            },
            "availability": availability,
        }
    )



@waivers_bp.route(
    "/api/league/<int:league_id>/drop-candidates",
    methods=["GET"],
)
@login_required
def api_drop_candidates(
    league_id: int,
):
    """
    Return the user's locally-synced roster ordered for a rapid FCFS drop.

    Ordering is handled by waivers_service:
      - unranked mapped players first;
      - then worst positional consensus rank to best.

    No MFL request is made here.
    """

    try:
        result = get_fcfs_drop_candidates(
            current_user.id,
            league_id,
        )

    except LookupError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            404,
        )

    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            400,
        )

    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            500,
        )

    return jsonify(
        {
            "ok": True,
            **result,
        }
    )


@waivers_bp.route(
    "/api/fcfs-add",
    methods=["POST"],
)
@login_required
def api_fcfs_add():
    """
    Execute one MFL FCFS add/drop.

    Expected JSON:
        {
            "league_id": 188,
            "add_player_id": 17465,
            "drop_player_id": 16629
        }

    drop_player_id may be null/empty for an add with no drop.

    IMPORTANT:
      - No second playerStatus check occurs here.
      - MFL's fcfsWaiver response is authoritative.
      - No local roster refresh occurs after success.
    """

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    league_id = payload.get(
        "league_id"
    )

    add_player_id = payload.get(
        "add_player_id"
    )

    drop_player_id = payload.get(
        "drop_player_id"
    )

    if league_id in (
        None,
        "",
    ):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "league_id is required."
                    ),
                    "errors": [
                        "league_id is required."
                    ],
                }
            ),
            400,
        )

    if add_player_id in (
        None,
        "",
    ):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "add_player_id is required."
                    ),
                    "errors": [
                        "add_player_id is required."
                    ],
                }
            ),
            400,
        )

    try:
        league_id_int = int(
            league_id
        )

    except (TypeError, ValueError):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "league_id must be an integer."
                    ),
                    "errors": [
                        "league_id must be an integer."
                    ],
                }
            ),
            400,
        )

    try:
        result = perform_fcfs_add(
            current_user,
            league_id_int,
            add_player_id,
            drop_player_id,
        )

    except LookupError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            404,
        )

    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            400,
        )

    except RuntimeError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            502,
        )

    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            500,
        )

    # MFL rule/transaction rejection.
    #
    # Preserve every <error> returned by MFL. A 409 tells the browser
    # this was a valid request that MFL declined, rather than a broken
    # RosterDash endpoint.
    if not result.get(
        "ok"
    ):
        errors = [
            str(error)
            for error in (
                result.get(
                    "errors"
                )
                or []
            )
            if str(error).strip()
        ]

        message = str(
            result.get(
                "message"
            )
            or (
                "\n".join(errors)
                if errors
                else "MFL transaction failed."
            )
        )

        return (
            jsonify(
                {
                    **result,
                    "ok": False,
                    "message": message,
                    "errors": errors,
                }
            ),
            409,
        )

    return jsonify(
        {
            **result,
            "ok": True,
            "message": "Added",
        }
    )


@waivers_bp.route(
    "/api/bbid-add",
    methods=["POST"],
)
@login_required
def api_bbid_add():
    """
    Execute one non-conditional MFL blind-bid waiver request.

    Expected JSON:
        {
            "league_id": 203,
            "add_player_id": 17465,
            "bid_amount": 17,
            "drop_player_id": 16629
        }

    drop_player_id may be null/empty.

    IMPORTANT:
      - This is the quick-claim, non-conditional BBID path.
      - No second playerStatus check occurs here.
      - ROUND is not used.
      - REPLACE is not used.
      - MFL's blindBidWaiverRequest response is authoritative.
      - No local roster or FAAB mutation occurs after success.
    """

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    league_id = payload.get(
        "league_id"
    )

    add_player_id = payload.get(
        "add_player_id"
    )

    bid_amount = payload.get(
        "bid_amount"
    )

    drop_player_id = payload.get(
        "drop_player_id"
    )

    if league_id in (
        None,
        "",
    ):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "league_id is required."
                    ),
                    "errors": [
                        "league_id is required."
                    ],
                }
            ),
            400,
        )

    if add_player_id in (
        None,
        "",
    ):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "add_player_id is required."
                    ),
                    "errors": [
                        "add_player_id is required."
                    ],
                }
            ),
            400,
        )

    try:
        league_id_int = int(
            league_id
        )

    except (TypeError, ValueError):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": (
                        "league_id must be an integer."
                    ),
                    "errors": [
                        "league_id must be an integer."
                    ],
                }
            ),
            400,
        )

    try:
        result = perform_blind_bid_add(
            current_user,
            league_id_int,
            add_player_id,
            bid_amount,
            drop_player_id,
        )

    except LookupError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            404,
        )

    except ValueError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            400,
        )

    except RuntimeError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            502,
        )

    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                    "message": str(exc),
                    "errors": [
                        str(exc)
                    ],
                }
            ),
            500,
        )

    # MFL understood the transaction request but declined it.
    if not result.get(
        "ok"
    ):
        errors = [
            str(error)
            for error in (
                result.get(
                    "errors"
                )
                or []
            )
            if str(error).strip()
        ]

        message = str(
            result.get(
                "message"
            )
            or (
                "\n".join(errors)
                if errors
                else "MFL transaction failed."
            )
        )

        return (
            jsonify(
                {
                    **result,
                    "ok": False,
                    "message": message,
                    "errors": errors,
                }
            ),
            409,
        )

    return jsonify(
        {
            **result,
            "ok": True,
            "message": (
                "Waiver bid submitted"
            ),
        }
    )


@waivers_bp.route(
    "/api/live-status",
    methods=["POST"],
)
@login_required
def api_live_status():
    """
    Explicitly verify one league using MFL playerStatus.

    Expected JSON:
        {
            "league_id": 38,
            "player_ids": [16686]
        }

    This makes ONE batched MFL playerStatus request for the selected league.
    It is never triggered automatically across all of the user's leagues.
    """
    payload = request.get_json(silent=True) or {}

    league_id = payload.get("league_id")
    player_ids = payload.get("player_ids")

    if league_id in (None, ""):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "league_id is required.",
                }
            ),
            400,
        )

    if not isinstance(player_ids, list) or not player_ids:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "player_ids must be a non-empty list.",
                }
            ),
            400,
        )

    # Keep this endpoint small and intentional. We do not want a UI bug
    # turning one request into an oversized live-status query.
    if len(player_ids) > 50:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "A maximum of 50 player IDs may be checked at once.",
                }
            ),
            400,
        )

    try:
        league_id_int = int(league_id)
    except (TypeError, ValueError):
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "league_id must be an integer.",
                }
            ),
            400,
        )

    normalized_player_ids = []

    for raw_id in player_ids:
        try:
            value = int(raw_id)
        except (TypeError, ValueError):
            continue

        if value not in normalized_player_ids:
            normalized_player_ids.append(value)

    if not normalized_player_ids:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": "No valid player IDs were supplied.",
                }
            ),
            400,
        )

    try:
        result = get_live_player_status(
            current_user,
            league_id_int,
            normalized_player_ids,
        )
    except LookupError as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                }
            ),
            404,
        )
    except Exception as exc:
        return (
            jsonify(
                {
                    "ok": False,
                    "error": str(exc),
                }
            ),
            502,
        )

    return jsonify(
        {
            "ok": True,
            **result,
        }
    )
