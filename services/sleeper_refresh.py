from __future__ import annotations

from typing import Any, Dict, List, Optional

from flask import current_app
from app import db
from models import SleeperLeague, User
from services.sleeper_client import SleeperClient
from services.sleeper_sync import sync_league_via_client


def refresh_all_sleeper_for_user(
    user: User,
    *,
    client: Optional[SleeperClient] = None,
) -> Dict[str, Any]:
    """
    Best-effort refresh of *all* Sleeper leagues owned by `user`.

    Returns:
      {
        "ok": bool,
        "refreshed": int,        # count of leagues successfully synced
        "errors": [str, ...],    # short error messages (max a few, but all are returned)
      }
    """
    logger = None
    try:
        logger = current_app.logger  # type: ignore[attr-defined]
    except Exception:
        pass

    leagues: List[SleeperLeague] = (
        SleeperLeague.query.filter_by(user_id=user.id).order_by(SleeperLeague.year.desc()).all()
    )

    if not leagues:
        return {"ok": True, "refreshed": 0, "errors": []}

    client = client or SleeperClient()
    ok = 0
    errors: List[str] = []

    for lg in leagues:
        try:
            sync_league_via_client(lg, client, site_user=user)
            ok += 1
        except Exception as e:
            # Never let a single league kill the whole refresh
            db.session.rollback()
            msg = f"{lg.name or lg.sleeper_id}: {e}"
            errors.append(msg)
            if logger:
                logger.exception("Sleeper refresh failed for league %s (user %s): %s", lg.sleeper_id, user.id, e)

    return {"ok": ok == len(leagues), "refreshed": ok, "errors": errors}
