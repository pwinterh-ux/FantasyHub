import xml.etree.ElementTree as ET
from app import create_app, db
from models import League, Team, Player, Roster, DraftPick

# -------------------------------
# Config for the league context
# -------------------------------
LEAGUE_MFL_ID = "61860"  # <-- set to the league you're loading assets for
LEAGUE_YEAR = 2025       # <-- season

# Sample XML for testing (one league at a time)
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
    # 1) Resolve the league context
    league = League.query.filter_by(mfl_id=LEAGUE_MFL_ID, year=LEAGUE_YEAR).first()
    if not league:
        print(f"League {LEAGUE_MFL_ID} ({LEAGUE_YEAR}) not found. Load leagues first.")
        raise SystemExit(1)

    # 2) Iterate franchises in the XML (each 'franchise' is a team in this league)
    for franchise_elem in root.findall("franchise"):
        franchise_id = franchise_elem.get("id")  # e.g., "0002"
        if not franchise_id:
            print("Encountered a franchise node without an 'id' attribute. Skipping.")
            continue

        # IMPORTANT: scope by league_id + mfl_id (franchise id)
        team = Team.query.filter_by(league_id=league.id, mfl_id=franchise_id).first()
        if not team:
            print(f"Team {franchise_id} not found in league '{league.name}' ({league.mfl_id}). Skipping.")
            continue

        # --- Clear existing rosters and draft picks for this team ---
        Roster.query.filter_by(team_id=team.id).delete()
        DraftPick.query.filter_by(team_id=team.id).delete()
        db.session.commit()  # commit the deletes before adding fresh rows

        # --- Players / Rosters ---
        players_el = franchise_elem.find("players")
        if players_el is not None:
            for player_elem in players_el.findall("player"):
                pid_str = player_elem.get("id")
                if not pid_str:
                    continue
                try:
                    player_id = int(pid_str)
                except ValueError:
                    print(f"Skipping player with non-integer id: {pid_str}")
                    continue

                # Insert placeholder player if missing
                player = Player.query.get(player_id)
                if not player:
                    player = Player(
                        id=player_id,              # PK matches MFL id
                        mfl_id=str(player_id),
                        name="Unknown Player",
                        team="NA",
                        position="NA",
                    )
                    db.session.add(player)
                    db.session.flush()  # ensure FK integrity for upcoming roster row

                # Add roster entry
                db.session.add(Roster(team_id=team.id, player_id=player_id, is_starter=False))

        # --- Draft Picks ---
        picks_el = franchise_elem.find("futureYearDraftPicks")
        if picks_el is not None:
            for draft_elem in picks_el.findall("draftPick"):
                pick_str = draft_elem.get("pick")  # e.g., FP_0002_2026_1
                if not pick_str:
                    continue

                parts = pick_str.split("_")
                # Expecting ["FP", "<orig_team>", "<season>", "<round>"]
                if len(parts) < 4:
                    print(f"Unexpected draft pick format: {pick_str}. Skipping.")
                    continue

                original_team_id = parts[1]  # keeps '0002'
                try:
                    season = int(parts[2])
                    round_num = int(parts[3])
                except ValueError:
                    print(f"Non-integer season/round in pick: {pick_str}. Skipping.")
                    continue

                db.session.add(
                    DraftPick(
                        team_id=team.id,
                        season=season,
                        round=round_num,
                        pick_number=None,          # unknown until assigned
                        original_team=original_team_id,
                    )
                )

        # Commit after each franchise to keep changes incremental/safe
        db.session.commit()

    print(f"Assets (rosters and draft picks) refreshed successfully for league '{league.name}' ({league.mfl_id})!")
