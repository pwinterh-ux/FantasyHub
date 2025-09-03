# fantasyhub/leagues/routes.py
from flask import Blueprint, render_template
from datetime import datetime
from models import League, Team

leagues_bp = Blueprint('leagues', __name__)

@leagues_bp.route('/leagues')
def my_leagues():
    leagues = League.query.all()

    league_data = []
    for league in leagues:
        # Your team in this league = the team whose mfl_id matches league.franchise_id
        my_team = None
        if getattr(league, "franchise_id", None):
            my_team = Team.query.filter_by(
                league_id=league.id,
                mfl_id=league.franchise_id
            ).first()

        league_data.append({
            "league_name": league.name,
            "team_name": (my_team.name if my_team else "N/A"),
            "record": (my_team.record if my_team and my_team.record else "--"),
            "standing": (my_team.standing if my_team and my_team.standing is not None else "--"),
            "roster_spots": (league.roster_slots or "Not set"),
        })

    return render_template('my_leagues.html', leagues=league_data, current_year=datetime.now().year)
