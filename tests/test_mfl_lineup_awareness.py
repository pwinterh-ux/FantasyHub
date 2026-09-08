from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask

from app import db
from models import League, Player, Roster, Team, User
from services.lineups_service import (
    Projection,
    ensure_roster_status_fresh,
    get_my_team_active_player_ids,
    get_my_team_player_ids,
    group_and_sort_players_for_review,
    pick_optimal_lineup,
    validate_lineup_starters,
)
from services.mfl_parsers import parse_assets, parse_league_info, parse_rosters_fallback
from services.mfl_sync import sync_league_assets
import lineups.routes as lineup_routes


@pytest.fixture()
def db_app():
    app = Flask(__name__)
    app.config.update(
        TESTING=True,
        SECRET_KEY="test",
        SQLALCHEMY_DATABASE_URI="sqlite://",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )
    db.init_app(app)
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize(
    "attribute, expected",
    [("Yes", "BEST_BALL"), (" no ", "MANUAL"), (None, "UNKNOWN"), ("maybe", "UNKNOWN")],
)
def test_parse_best_lineup_and_taxi_capacity(attribute, expected):
    best = f' bestLineup="{attribute}"' if attribute is not None else ""
    parsed = parse_league_info(f'<league taxiSquad="7"{best}/>'.encode())
    assert parsed[4] == expected
    assert parsed[5] == 7


@pytest.mark.parametrize("parser", [parse_assets, parse_rosters_fallback])
def test_roster_parsers_preserve_active_taxi_and_ir(parser):
    xml = b"""<rosters><franchise id="1"><players>
      <player id="101" status="ROSTER"/>
      <player id="102" status="TAXI_SQUAD"/>
      <player id="103" status="INJURED_RESERVE"/>
    </players></franchise></rosters>"""
    assets = parser(xml)[0]
    assert assets.player_ids == [101, 102, 103]
    assert [entry.roster_status for entry in assets.roster_players] == ["ACTIVE", "TAXI", "IR"]


def _league_with_roster(statuses):
    user = User(username="owner", email="owner@example.com")
    league = League(user=user, mfl_id="123", name="League", year=2026, franchise_id="0001")
    team = Team(league=league, mfl_id="0001", name="Mine")
    db.session.add_all([user, league, team])
    db.session.flush()
    for player_id, status in statuses.items():
        player = Player(id=player_id, mfl_id=str(player_id), name=f"Player {player_id}", position="WR")
        db.session.add(player)
        db.session.add(Roster(team=team, player=player, roster_status=status, in_ir=status == "IR"))
    db.session.commit()
    return league


def test_asset_sync_persists_status_ir_and_timestamp(db_app):
    with db_app.app_context():
        league = _league_with_roster({999: "ACTIVE"})
        assets = parse_assets(b"""<assets><franchise id="1"><players>
          <player id="201"/><player id="202" status="TAXI_SQUAD"/>
          <player id="203" status="INJURED_RESERVE"/>
        </players></franchise></assets>""")
        sync_league_assets(league, assets)
        rows = {row.player_id: row for row in get_team_rows(league)}
        assert {pid: row.roster_status for pid, row in rows.items()} == {201: "ACTIVE", 202: "TAXI", 203: "IR"}
        assert rows[203].in_ir is True
        assert rows[201].in_ir is False
        assert league.roster_status_synced_at is not None


def get_team_rows(league):
    team = Team.query.filter_by(league_id=league.id, mfl_id="0001").one()
    return Roster.query.filter_by(team_id=team.id).all()


def test_projection_display_and_optimizer_keep_owned_but_exclude_ineligible(db_app):
    with db_app.app_context():
        league = _league_with_roster({301: "ACTIVE", 302: "TAXI", 303: "IR"})
        players = [(301, "Active", "WR", "ARI"), (302, "Taxi", "WR", "ATL"), (303, "IR", "WR", "BAL")]
        projections = {pid: Projection(pid, score) for pid, score in [(301, 10), (302, 30), (303, 20)]}
        statuses = {row.player_id: row.roster_status for row in get_team_rows(league)}
        grouped = group_and_sort_players_for_review(players, projections, statuses)
        assert [row["player_id"] for row in grouped["WR"]] == [302, 303, 301]
        assert [row["projected"] for row in grouped["WR"]] == [30, 20, 10]
        assert [row["lineup_eligible"] for row in grouped["WR"]] == [False, False, True]
        eligible = [row for row in players if row[0] in get_my_team_active_player_ids(league.id)]
        assert pick_optimal_lineup(eligible, projections, 1, {"WR": (1, 1)}) == [301]
        assert set(get_my_team_player_ids(league.id)) == {301, 302, 303}


@pytest.mark.parametrize("status, allowed", [("ACTIVE", True), ("TAXI", False), ("IR", False), ("UNKNOWN", False)])
def test_submission_guard(status, allowed, db_app):
    with db_app.app_context():
        league = _league_with_roster({401: status})
        error = validate_lineup_starters(league.id, [401])
        assert (error is None) is allowed
        assert validate_lineup_starters(league.id, [9999]) == "Lineup not submitted: player 9999 is not on your roster."


def test_roster_status_ttl_reuses_fresh_and_refreshes_stale_once(db_app):
    with db_app.app_context():
        league = _league_with_roster({501: "TAXI"})
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        with patch("services.lineups_service.MFLClient.get_rosters") as get_rosters:
            assert ensure_roster_status_fresh(league, host="www1.myfantasyleague.com", cookie="cookie")[0]
            get_rosters.assert_not_called()

        league.roster_status_synced_at = datetime.utcnow() - timedelta(minutes=6)
        db.session.commit()
        xml = b'<rosters><franchise id="1"><player id="501" status="ROSTER"/></franchise></rosters>'
        with patch("services.lineups_service.MFLClient.get_rosters", return_value=xml) as get_rosters:
            ok, error = ensure_roster_status_fresh(league, host="www1.myfantasyleague.com", cookie="cookie")
            assert ok and error is None
            get_rosters.assert_called_once()
            assert get_team_rows(league)[0].roster_status == "ACTIVE"


def test_failed_required_refresh_keeps_stale_data_and_fails_closed(db_app):
    with db_app.app_context():
        league = _league_with_roster({601: "ACTIVE"})
        league.roster_status_synced_at = datetime.utcnow() - timedelta(minutes=6)
        db.session.commit()
        with patch("services.lineups_service.MFLClient.get_rosters", side_effect=RuntimeError("down")):
            ok, error = ensure_roster_status_fresh(league, host="www1.myfantasyleague.com", cookie="cookie")
        assert not ok
        assert "Could not refresh" in error
        assert league.roster_status_synced_at < datetime.utcnow() - timedelta(minutes=5)


def test_best_ball_auto_submit_skips_projection_and_import(db_app):
    with db_app.test_request_context("/lineups/auto-submit", method="POST", data={"week": "1"}):
        league = _league_with_roster({701: "ACTIVE"})
        league.lineup_mode = "BEST_BALL"
        db.session.commit()
        rendered = {}

        def capture(template, **context):
            rendered.update(context)
            return template

        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=league.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "_user_synced_leagues", return_value=[league]), patch.object(
            lineup_routes, "render_template", side_effect=capture
        ), patch.object(lineup_routes, "fetch_projected_scores") as projections, patch.object(
            lineup_routes, "submit_lineup"
        ) as submit:
            response = lineup_routes.lineups_auto_submit.__wrapped__()
        assert response == "lineups/summary.html"
        assert rendered["results"][0]["skipped"] is True
        assert "Best Ball" in rendered["results"][0]["message"]
        projections.assert_not_called()
        submit.assert_not_called()


def test_rapid_renders_best_ball_state_without_projection(db_app):
    with db_app.test_request_context("/lineups/rapid/league"):
        league = _league_with_roster({801: "ACTIVE"})
        league.lineup_mode = "BEST_BALL"
        db.session.commit()
        from flask import session
        session["rapid_queue"] = [league.id]
        session["rapid_idx"] = 0
        session["rapid_week"] = 1
        context = {}

        def capture(template, **values):
            context.update(values)
            return template

        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=league.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "render_template", side_effect=capture), patch.object(
            lineup_routes, "fetch_projected_scores"
        ) as projections, patch.object(lineup_routes, "submit_lineup") as submit:
            response = lineup_routes.lineups_rapid_league.__wrapped__()
        assert response == "lineups/rapid_league.html"
        assert context["best_ball"] is True
        projections.assert_not_called()
        submit.assert_not_called()


@pytest.mark.parametrize("status", ["TAXI", "IR", "UNKNOWN"])
def test_rapid_submission_guard_rejects_ineligible_before_import(status, db_app):
    with db_app.app_context():
        league = _league_with_roster({901: status})
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        league_id = league.id
        user_id = league.user_id
    with db_app.test_request_context(
        "/lineups/rapid/submit", method="POST",
        data={"league_id": str(league_id), "week": "1", "starters[]": "901"},
    ):
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "submit_lineup") as submit:
            response, status_code = lineup_routes.lineups_rapid_submit.__wrapped__()
        assert status_code == 400
        assert "Lineup not submitted" in response.get_json()["message"]
        submit.assert_not_called()
