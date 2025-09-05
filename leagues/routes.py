# leagues/routes.py
from flask import Blueprint, render_template, jsonify, abort, current_app
from flask_login import login_required, current_user
from sqlalchemy import asc

from app import db
from models import League, Team, Player, Roster, DraftPick

leagues_bp = Blueprint("leagues", __name__, url_prefix="/leagues")


@leagues_bp.route("", methods=["GET"])
@login_required
def my_leagues():
    # ONLY leagues for the current user
    leagues = (
        League.query
        .filter_by(user_id=current_user.id)
        .order_by(League.year.desc(), League.name.asc())
        .all()
    )

    # map: league_id -> the user's team in that league (by franchise_id)
    my_teams = {}
    for lg in leagues:
        my_team = None
        if lg.franchise_id:
            my_team = Team.query.filter_by(
                league_id=lg.id, mfl_id=lg.franchise_id
            ).first()
        my_teams[lg.id] = my_team

    return render_template("my_leagues.html", leagues=leagues, my_teams=my_teams)


@leagues_bp.route("/<int:league_id>/details.json", methods=["GET"])
@login_required
def league_details_json(league_id: int):
    """
    Return JSON details (league + teams + *my* roster and draft picks).
    Access is restricted to the logged-in owner of the league.
    """
    try:
        league = League.query.filter_by(id=league_id, user_id=current_user.id).first()
        if not league:
            abort(404)

        # Teams ordered: ranked first, then by name
        teams = (
            Team.query
            .filter_by(league_id=league.id)
            .order_by(Team.standing.is_(None), asc(Team.standing), asc(Team.name))
            .all()
        )

        # My team (by franchise_id from myleagues)
        my_team = None
        if league.franchise_id:
            my_team = Team.query.filter_by(
                league_id=league.id, mfl_id=league.franchise_id
            ).first()

        # My roster (join to Player for details)
        roster_items = []
        if my_team:
            rows = (
                db.session.query(Roster, Player)
                .join(Player, Player.id == Roster.player_id)
                .filter(Roster.team_id == my_team.id)
                .order_by(asc(Player.position), asc(Player.name))
                .all()
            )
            for r, p in rows:
                roster_items.append({
                    "player_id": p.id,
                    "mfl_id": p.mfl_id,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "status": p.status,
                    "is_starter": bool(r.is_starter),
                })

        # My future draft picks
        draft_picks = []
        if my_team:
            picks = (
                DraftPick.query
                .filter_by(team_id=my_team.id)
                .order_by(asc(DraftPick.season), asc(DraftPick.round), asc(DraftPick.pick_number))
                .all()
            )
            for dp in picks:
                draft_picks.append({
                    "season": dp.season,
                    "round": dp.round,
                    "pick_number": dp.pick_number,
                    "original_team": dp.original_team,
                })

        payload = {
            "league": {
                "id": league.id,
                "name": league.name,
                "mfl_id": league.mfl_id,
                "year": league.year,
                "roster_slots": league.roster_slots,
                "franchise_id": league.franchise_id,
                "synced_at": league.synced_at.isoformat() if league.synced_at else None,
            },
            "teams": [
                {
                    "mfl_id": t.mfl_id,
                    "name": t.name,
                    "owner_name": t.owner_name,
                    "record": t.record,
                    "points_for": t.points_for,
                    "points_against": t.points_against,
                    "standing": t.standing,
                }
                for t in teams
            ],
            "my_team": (
                {
                    "mfl_id": my_team.mfl_id,
                    "name": my_team.name,
                    "record": my_team.record,
                    "points_for": my_team.points_for,
                    "points_against": my_team.points_against,
                    "standing": my_team.standing,
                } if my_team else None
            ),
            "my_roster": roster_items,
            "my_draft_picks": draft_picks,
            "counts": {
                "teams": len(teams),
                "roster": len(roster_items),
                "draft_picks": len(draft_picks),
            }
        }
        return jsonify(payload)

    except Exception:
        current_app.logger.exception("league_details_json failed")
        return jsonify({"error": "internal"}), 500
