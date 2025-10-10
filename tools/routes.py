# tools/routes.py
from datetime import datetime, timedelta, timezone
from flask import Blueprint, render_template, current_app
from flask_login import login_required, current_user
from sqlalchemy import func

from models import League, db

try:
    # If your project has a SleeperLeague model, we’ll use it; otherwise we’ll noop gracefully.
    from models import SleeperLeague
except Exception:  # pragma: no cover
    SleeperLeague = None

bp = Blueprint("tools", __name__, url_prefix="/tools")


def _fmt_dt(dt):
    try:
        # Normalize to UTC for label consistency
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return None


def _max_timestamp_for(model, user_id, candidate_cols):
    """
    Safely compute MAX(timestamp) for any of the provided columns on a model.
    Returns a timezone-aware datetime in UTC or None.
    """
    if model is None:
        return None
    best = None
    for col in candidate_cols:
        if not hasattr(model, col):
            continue
        try:
            val = (
                db.session.query(func.max(getattr(model, col)))
                .filter(getattr(model, "user_id") == user_id)
                .scalar()
            )
        except Exception:
            val = None
        if val:
            # try to normalize to aware UTC
            try:
                if val.tzinfo is None:
                    val = val.replace(tzinfo=timezone.utc)
                else:
                    val = val.astimezone(timezone.utc)
            except Exception:
                pass
            best = max(best, val) if best else val
    return best


def _current_week_bundle():
    """
    Produce (current_week, weeks_list) using config knobs, clamped to max weeks,
    and only showing current week and forward.
    """
    # Prefer the same keys you already use elsewhere
    cfg_week = (
        current_app.config.get("MFL_CURRENT_WEEK")
        or current_app.config.get("CURRENT_NFL_WEEK")
        or current_app.config.get("CURRENT_WEEK")
        or 1
    )
    try:
        current_week = int(cfg_week)
    except Exception:
        current_week = 1

    try:
        max_week = int(current_app.config.get("MFL_MAX_WEEKS", 18))
    except Exception:
        max_week = 18

    if current_week < 1:
        current_week = 1
    if max_week < current_week:
        max_week = current_week

    weeks = list(range(current_week, max_week + 1))
    return current_week, weeks


@bp.route("/")
@login_required
def index():
    user_id = current_user.id

    # ----- Has MFL? (League table is MFL)
    try:
        has_mfl = (
            db.session.query(League.id)
            .filter(League.user_id == user_id)
            .limit(1)
            .count()
            > 0
        )
    except Exception:
        has_mfl = False

    # ----- Build MFL list for client-side "Refresh Assets" (id/name/year/mfl_id)
    mfl_leagues = []
    try:
        leagues = (
            db.session.query(League)
            .filter(League.user_id == user_id)
            .order_by(League.year.desc(), League.name.asc())
            .all()
        )
        for lg in leagues:
            mfl_leagues.append(
                dict(
                    db_id=lg.id,
                    mfl_id=getattr(lg, "mfl_id", None) or getattr(lg, "external_id", None) or "",
                    year=getattr(lg, "year", None),
                    name=getattr(lg, "name", None) or "League",
                )
            )
    except Exception:
        pass

    # ----- Last sync across BOTH sources (MFL + Sleeper)
    mfl_last = _max_timestamp_for(
        League,
        user_id,
        ["synced_at", "last_synced_at", "assets_synced_at", "updated_at"],
    )
    slpr_last = _max_timestamp_for(
        SleeperLeague,
        user_id,
        ["synced_at", "last_synced_at", "assets_synced_at", "updated_at"],
    )

    # Pick the freshest timestamp of the two; show a dash if none
    last_sync = None
    if mfl_last and slpr_last:
        last_sync = mfl_last if mfl_last >= slpr_last else slpr_last
    else:
        last_sync = mfl_last or slpr_last

    last_sync_label = _fmt_dt(last_sync) if last_sync else "—"

    # Refresh required if older than 4 hours (or never synced)
    refresh_cutoff = datetime.now(timezone.utc) - timedelta(hours=4)
    refresh_required = (not last_sync) or (last_sync < refresh_cutoff)

    # ----- Week dropdown (current and forward only)
    selected_week, weeks = _current_week_bundle()

    return render_template(
        "tools/index.html",
        has_mfl_leagues=has_mfl,
        page_visible=bool(current_app.config.get("TOOLS_PAGE_VISIBLE", False)),
        last_sync_label=last_sync_label,
        refresh_required=refresh_required,
        weeks=weeks,
        selected_week=selected_week,
        mfl_leagues=mfl_leagues,
    )
