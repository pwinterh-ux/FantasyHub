import xml.etree.ElementTree as ET
from datetime import datetime
from app import create_app, db
from models import League

# Load your XML from a file or paste as a string
xml_data = """
<leagues>
<league url="https://www43.myfantasyleague.com/2025/home/11376" league_id="11376" franchise_name="Paul Winterhalter (@Pwindynasty)" name="#SFB15 - Springfield Isotopes" franchise_id="0001"/>
<league franchise_id="0006" league_id="61860" franchise_name="Pwin" name="All Play League" url="https://www45.myfantasyleague.com/2025/home/61860"/>
<!-- etc -->
</leagues>
"""

root = ET.fromstring(xml_data)

app = create_app()
with app.app_context():
    year = 2025  # Hardcode for now

    for league_elem in root.findall("league"):
        mfl_id = league_elem.get("league_id")
        franchise_id = league_elem.get("franchise_id")
        name = league_elem.get("name")

        # Optional: extract roster slots if available
        roster_slots = None

        # Avoid duplicates (unique by league/year)
        existing = League.query.filter_by(mfl_id=mfl_id, year=year).first()
        if existing:
            print(f"Skipping existing league {name} ({mfl_id})")
            continue

        league = League(
            mfl_id=mfl_id,
            franchise_id=franchise_id,
            name=name,
            year=year,
            synced_at=datetime.utcnow(),
            roster_slots=roster_slots
        )
        db.session.add(league)
        print(f"Added league {name} ({mfl_id}) with franchise {franchise_id}")

    db.session.commit()
    print("✅ Leagues loaded successfully!")