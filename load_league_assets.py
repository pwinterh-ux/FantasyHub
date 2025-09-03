import xml.etree.ElementTree as ET
from app import create_app, db
from models import Team, Player, Roster, DraftPick

# Sample XML for testing team 2
xml_data = """
<assets>
    <franchise id="0002">
        <players>
            <player id="13593"/>
            <player id="15241"/>
            <player id="14109"/>
        </players>
        <futureYearDraftPicks>
            <draftPick pick="FP_0002_2026_1" description="Year 2026 Round 1 Draft Pick from Oklahoma GMen"/>
            <draftPick pick="FP_0002_2026_2" description="Year 2026 Round 2 Draft Pick from Oklahoma GMen"/>
            <draftPick pick="FP_0002_2027_1" description="Year 2027 Round 1 Draft Pick from Oklahoma GMen"/>
        </futureYearDraftPicks>
    </franchise>
</assets>
"""

root = ET.fromstring(xml_data)
app = create_app()

with app.app_context():
    # Load team 2
    team = Team.query.filter_by(mfl_id='0002').first()
    if not team:
        print("Team 2 not found! Make sure leagues/teams are loaded first.")
        exit()

    for franchise_elem in root.findall('franchise'):

        # --- Clear existing rosters and draft picks for this team ---
        Roster.query.filter_by(team_id=team.id).delete()
        DraftPick.query.filter_by(team_id=team.id).delete()
        db.session.commit()

        # --- Players / Rosters ---
        for player_elem in franchise_elem.find('players').findall('player'):
            player_id = int(player_elem.get('id'))

            # Insert placeholder player if missing
            player = Player.query.get(player_id)
            if not player:
                player = Player(
                    id=player_id,
                    mfl_id=str(player_id),
                    name="Unknown Player",
                    team="NA",
                    position="NA"
                )
                db.session.add(player)
                db.session.flush()  # ensure FK works

            # Add roster entry
            roster = Roster(
                team_id=team.id,
                player_id=player_id,
                is_starter=False
            )
            db.session.add(roster)

        # --- Draft Picks ---
        for draft_elem in franchise_elem.find('futureYearDraftPicks').findall('draftPick'):
            pick_str = draft_elem.get('pick')  # e.g., FP_0002_2026_1
            pick_parts = pick_str.split('_')
            original_team_id = pick_parts[1]  # keeps '0002'
            season = int(pick_parts[2])
            round_num = int(pick_parts[3])

            draft_pick = DraftPick(
                team_id=team.id,
                season=season,
                round=round_num,
                pick_number=None,  # leave empty for now
                original_team=original_team_id  # store franchise ID
            )
            db.session.add(draft_pick)

    db.session.commit()
    print("Assets (rosters and draft picks) refreshed successfully for team 2!")
