import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from app import db
from models import NflSchedule
from services.nfl_schedule_service import (
    BYE, LOCKED, UNKNOWN, UNLOCKED, build_team_game_states, game_state_for_team,
    normalize_nfl_team, parse_mfl_nfl_schedule, sync_nfl_schedule,
    parse_mfl_nfl_schedule_with_metadata,
)


@pytest.fixture
def app():
    application = Flask(__name__)
    application.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", TESTING=True)
    db.init_app(application)
    with application.app_context():
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


def test_real_repository_fixture_parses_two_rows_per_game():
    payload = json.loads(Path("static/schedule.json").read_text())
    records = parse_mfl_nfl_schedule(payload, 2025)
    first = records[:2]
    assert len(records) > 500
    assert {x["team"] for x in first} == {"DAL", "PHI"}
    assert {x["opponent"] for x in first} == {"DAL", "PHI"}
    assert {x["is_home"] for x in first} == {True, False}
    assert {x["kickoff_unix"] for x in first} == {1757031600}


def test_schedule_upsert_is_idempotent_updates_and_keeps_seasons(app):
    with app.app_context():
        old = NflSchedule(year=2024, week=1, team="DAL", opponent="CLE", is_home=False, kickoff_unix=1)
        from app import db
        db.session.add(old); db.session.commit()
        rows = [{"year": 2026, "week": 2, "team": "PIT", "opponent": "BAL", "is_home": False, "kickoff_unix": 10},
                {"year": 2026, "week": 2, "team": "BAL", "opponent": "PIT", "is_home": True, "kickoff_unix": 10}]
        sync_nfl_schedule(2026, rows); sync_nfl_schedule(2026, rows)
        assert NflSchedule.query.count() == 3
        rows[0]["kickoff_unix"] = 20
        sync_nfl_schedule(2026, rows)
        assert db.session.get(NflSchedule, (2026, 2, "PIT")).kickoff_unix == 20
        assert db.session.get(NflSchedule, (2024, 1, "DAL")) is old


def test_lock_boundary_bye_unknown_and_aliases():
    now = datetime.fromtimestamp(100, timezone.utc)
    assert build_team_game_states([{"team": "PIT", "kickoff_unix": 101}], now, schedule_verified=True)["PIT"]["state"] == UNLOCKED
    assert build_team_game_states([{"team": "PIT", "kickoff_unix": 100}], now, schedule_verified=True)["PIT"]["state"] == LOCKED
    assert build_team_game_states([{"team": "PIT", "kickoff_unix": 99}], now, schedule_verified=True)["PIT"]["state"] == LOCKED
    assert game_state_for_team("KC", {}, schedule_verified=True, week_complete=True)["state"] == BYE
    assert game_state_for_team("KC", {}, schedule_verified=False, week_complete=True)["state"] == UNKNOWN
    assert game_state_for_team("???", {}, schedule_verified=True, week_complete=True)["state"] == UNKNOWN
    assert normalize_nfl_team("KC") == "KCC"
    assert normalize_nfl_team("GB") == "GBP"
    assert normalize_nfl_team("LV") == "LVR"


def test_malformed_matchup_makes_week_incomplete_and_absence_unknown():
    payload = {"fullNflSchedule": {"nflSchedule": [{"week": "2", "matchup": [
        {"kickoff": "100", "team": [{"id": "PIT", "isHome": "0"}, {"id": "BAL", "isHome": "1"}]},
        {"kickoff": "200", "team": [{"id": "NOT_A_TEAM", "isHome": "0"}]},
    ]}]}}
    records, metadata = parse_mfl_nfl_schedule_with_metadata(payload, 2026)
    assert len(records) == 2
    assert metadata[2] == {"week_number": 2, "raw_matchup_count": 2,
        "parsed_matchup_count": 1, "malformed_matchup_count": 1,
        "unique_team_count": 2, "structurally_complete": False}
    assert game_state_for_team("KC", {}, schedule_verified=True,
                               week_complete=metadata[2]["structurally_complete"])["state"] == UNKNOWN


def test_complete_week_can_assign_bye_and_invalid_alias_fails_closed():
    payload = {"fullNflSchedule": {"nflSchedule": [{"week": "2", "matchup":
        {"team": [{"id": "PIT"}, {"id": "BAL"}]}}]}}
    records, metadata = parse_mfl_nfl_schedule_with_metadata(payload, 2026)
    assert len(records) == 2 and metadata[2]["structurally_complete"]
    assert game_state_for_team("KC", {}, schedule_verified=True, week_complete=True)["state"] == BYE
    assert game_state_for_team("malformed", {}, schedule_verified=True, week_complete=True)["state"] == UNKNOWN


def test_fresh_payload_states_do_not_include_stale_database_row(app):
    with app.app_context():
        db.session.add(NflSchedule(year=2026, week=2, team="KCC", opponent="LVR", is_home=True, kickoff_unix=999))
        db.session.commit()
        fresh = [{"year": 2026, "week": 2, "team": "PIT", "opponent": "BAL", "is_home": False, "kickoff_unix": 101},
                 {"year": 2026, "week": 2, "team": "BAL", "opponent": "PIT", "is_home": True, "kickoff_unix": 101}]
        states = build_team_game_states(fresh, datetime.fromtimestamp(100, timezone.utc), schedule_verified=True)
        assert "KCC" not in states
        assert game_state_for_team("KC", states, schedule_verified=True, week_complete=True)["state"] == BYE


def test_live_2026_top_level_nflschedule_shape():
    payload = {
        "encoding": "utf-8",
        "nflSchedule": {
            "week": "1",
            "matchup": [
                {
                    "kickoff": "1788999600",
                    "gameSecondsRemaining": "0",
                    "team": [
                        {"id": "NEP", "isHome": "0"},
                        {"id": "SEA", "isHome": "1"},
                    ],
                },
                {
                    "kickoff": "1789086900",
                    "gameSecondsRemaining": "0",
                    "team": [
                        {"id": "SFO", "isHome": "0"},
                        {"id": "LAR", "isHome": "1"},
                    ],
                },
            ],
        },
    }

    records, metadata = parse_mfl_nfl_schedule_with_metadata(payload, 2026)

    assert metadata[1] == {
        "week_number": 1,
        "raw_matchup_count": 2,
        "parsed_matchup_count": 2,
        "malformed_matchup_count": 0,
        "unique_team_count": 4,
        "structurally_complete": True,
    }

    assert len(records) == 4

    by_team = {record["team"]: record for record in records}

    assert by_team["NEP"]["opponent"] == "SEA"
    assert by_team["SEA"]["opponent"] == "NEP"
    assert by_team["SFO"]["opponent"] == "LAR"
    assert by_team["LAR"]["opponent"] == "SFO"

    assert by_team["NEP"]["kickoff_unix"] == 1788999600
    assert by_team["SEA"]["kickoff_unix"] == 1788999600
    assert by_team["SFO"]["kickoff_unix"] == 1789086900
    assert by_team["LAR"]["kickoff_unix"] == 1789086900


def test_unknown_schedule_root_shape_fails_closed():
    records, metadata = parse_mfl_nfl_schedule_with_metadata(
        {"encoding": "utf-8", "schedule": {}},
        2026,
    )

    assert records == []
    assert metadata == {}
