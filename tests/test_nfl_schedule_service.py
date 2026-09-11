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
