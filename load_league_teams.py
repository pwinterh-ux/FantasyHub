import xml.etree.ElementTree as ET
from datetime import datetime
from app import create_app, db
from models import League, Team

# Example XML (replace with real API response)
xml_data = """
<league id="61860" name="All Play League">
    <starters count="9">
        <position name="QB" limit="1"/>
        <position name="RB" limit="2-4"/>
        <position name="WR" limit="3-5"/>
        <position name="TE" limit="1-3"/>
    </starters>
    <franchises count="12">
        <franchise id="0001" name="Nate" username="false_shart"/>
        <franchise id="0002" name="Oklahoma GMen" username="jfence69"/>
        <franchise id="0003" name="Ray" username="whodatnation315"/>
        <!-- etc -->
    </franchises>
</league>
"""

root = ET.fromstring(xml_data)
app = create_app()

with app.app_context():
    league_mfl_id = root.get("id")
    year = 2025  # Hardcode for now

    # 🔑 Find or create league
    league = League.query.filter_by(mfl_id=league_mfl_id, year=year).first()
    if not league:
        league_name = root.get("name")
        league = League(
            mfl_id=league_mfl_id,
            franchise_id=None,  # <-- NEW: placeholder since commissioner is gone
            name=league_name,
            year=year,
            synced_at=datetime.utcnow()
        )
        db.session.add(league)
        db.session.commit()  # Commit so league.id exists

    # 🔑 Parse starters into roster_slots string
    starters_elem = root.find("starters")
    if starters_elem is not None:
        slots_str = ",".join(
            f"{pos.get('name')}:{pos.get('limit')}"
            for pos in starters_elem.findall("position")
        )
        league.roster_slots = slots_str

    # 🔑 Parse franchises -> Teams
    franchises_elem = root.find("franchises")
    if franchises_elem:
        for fr in franchises_elem.findall("franchise"):
            team_mfl_id = fr.get("id")
            name = fr.get("name")
            owner_name = fr.get("username", "")

            # Check if team already exists
            existing = Team.query.filter_by(
                mfl_id=team_mfl_id, league_id=league.id
            ).first()

            if existing:
                existing.name = name
                existing.owner_name = owner_name
            else:
                team = Team(
                    league_id=league.id,
                    mfl_id=team_mfl_id,
                    name=name,
                    owner_name=owner_name
                )
                db.session.add(team)

    db.session.commit()
    print("League and teams loaded successfully!")
