import xml.etree.ElementTree as ET
from app import create_app, db
from models import League, Team

xml_data = """
<leagueStandings>
<franchise id="0012" h2hwlt="0-0-0" pf="0" pa="0"/>
<franchise id="0001" h2hwlt="0-0-0" pf="0" pa="0"/>
<franchise id="0002" h2hwlt="0-0-0" pf="0" pa="0"/>
<!-- etc -->
</leagueStandings>
"""

root = ET.fromstring(xml_data)
app = create_app()

with app.app_context():
    league_mfl_id = "61860"
    year = 2025

    league = League.query.filter_by(mfl_id=league_mfl_id, year=year).first()
    if not league:
        print(f"League {league_mfl_id} not found. Make sure leagues are loaded first.")
        exit()

    # Optional: assign standings based on XML order
    for standing_place, fr_elem in enumerate(root.findall('franchise'), start=1):
        team_mfl_id = fr_elem.get('id')
        team = Team.query.filter_by(league_id=league.id, mfl_id=team_mfl_id).first()
        if not team:
            print(f"Team {team_mfl_id} not found in league {league.name}. Skipping.")
            continue

        team.record = fr_elem.get('h2hwlt', "0-0-0")
        team.points_for = float(fr_elem.get('pf', 0.0))
        team.points_against = float(fr_elem.get('pa', 0.0))
        team.standing = standing_place

    db.session.commit()
    print("League standings loaded successfully!")
