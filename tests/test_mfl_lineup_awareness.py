from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, has_app_context

from app import db
from models import League, Player, Roster, Team, User
from services.lineups_service import (
    Projection,
    ensure_lineup_mode_resolved,
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


@pytest.mark.parametrize("mode", ["MANUAL", "BEST_BALL"])
def test_resolved_lineup_mode_makes_no_league_info_request(mode, db_app):
    with db_app.app_context():
        league = _league_with_roster({100: "ACTIVE"})
        league.lineup_mode = mode
        db.session.commit()
        with patch("services.lineups_service.MFLClient.get_league_info") as get_info:
            assert ensure_lineup_mode_resolved(
                league, host="www1.myfantasyleague.com", cookie="cookie"
            ) == mode
        get_info.assert_not_called()


@pytest.mark.parametrize(
    "best_lineup, expected",
    [("Yes", "BEST_BALL"), ("No", "MANUAL"), ("maybe", "UNKNOWN"), (None, "UNKNOWN")],
)
def test_unknown_lineup_mode_self_heals_once(best_lineup, expected, db_app, caplog):
    with db_app.app_context():
        league = _league_with_roster({110: "ACTIVE"})
        assert league.lineup_mode == "UNKNOWN"
        attr = f' bestLineup="{best_lineup}"' if best_lineup is not None else ""
        xml = f'<league taxiSquad="8"{attr}/>'.encode()
        with patch("services.lineups_service.MFLClient.get_league_info", return_value=xml) as get_info:
            mode = ensure_lineup_mode_resolved(
                league, host="www1.myfantasyleague.com", cookie="cookie"
            )
        assert mode == expected
        assert league.lineup_mode == expected
        assert league.taxi_slots_max == 8
        get_info.assert_called_once()
        if expected == "UNKNOWN":
            assert "proceeding with manual-lineup fallback" in caplog.text


def test_lineup_mode_request_failure_retains_unknown_and_logs(db_app, caplog):
    with db_app.app_context():
        league = _league_with_roster({120: "ACTIVE"})
        with patch(
            "services.lineups_service.MFLClient.get_league_info", side_effect=TimeoutError("timeout")
        ) as get_info:
            assert ensure_lineup_mode_resolved(
                league, host="www1.myfantasyleague.com", cookie="cookie"
            ) == "UNKNOWN"
        assert league.lineup_mode == "UNKNOWN"
        get_info.assert_called_once()
        assert "proceeding with manual-lineup fallback" in caplog.text


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


def _additional_league_with_roster(user, mfl_id, player_id):
    league = League(user=user, mfl_id=mfl_id, name=f"League {mfl_id}", year=2026,
                    franchise_id="0001", lineup_mode="MANUAL")
    team = Team(league=league, mfl_id="0001", name="Mine")
    player = Player(id=player_id, mfl_id=str(player_id), name=f"Player {player_id}", position="WR")
    db.session.add_all([league, team, player])
    db.session.add(Roster(team=team, player=player, roster_status="ACTIVE"))
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
        league.lineup_mode = "UNKNOWN"
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
        ) as submit, patch.object(lineup_routes, "pick_optimal_lineup") as optimizer, patch(
            "services.lineups_service.MFLClient.get_league_info",
            return_value=b'<league bestLineup="Yes"/>',
        ) as get_info:
            response = lineup_routes.lineups_auto_submit.__wrapped__()
        assert response == "lineups/summary.html"
        assert rendered["results"][0]["skipped"] is True
        assert "Best Ball" in rendered["results"][0]["message"]
        projections.assert_not_called()
        optimizer.assert_not_called()
        submit.assert_not_called()
        get_info.assert_called_once()
        assert league.lineup_mode == "BEST_BALL"


def test_auto_submit_workers_use_snapshots_across_orm_expiring_commits(db_app):
    with db_app.test_request_context("/lineups/auto-submit", method="POST", data={"week": "1"}):
        first = _league_with_roster({711: "ACTIVE"})
        first.lineup_mode = "MANUAL"
        second = _additional_league_with_roster(first.user, "456", 712)
        first.roster_slots = second.roster_slots = "1 WR"
        db.session.commit()
        rendered = {}

        def refresh(league):
            db.session.commit()  # Expire every loaded League before worker threads start.
            return True, None, "www1.myfantasyleague.com", "cookie"

        def projections(host, mfl_id, year, week, player_ids, cookie=None):
            assert not has_app_context()
            return {player_ids[0]: Projection(player_ids[0], 10)}

        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=first.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "_user_synced_leagues", return_value=[first, second]), patch.object(
            lineup_routes, "_refresh_lineup_roster", side_effect=refresh
        ), patch.object(lineup_routes, "fetch_projected_scores", side_effect=projections) as fetch, patch.object(
            lineup_routes, "pick_optimal_lineup", side_effect=lambda players, *_: [players[0][0]]
        ), patch.object(lineup_routes, "submit_lineup", return_value=(True, "OK")), patch.object(
            lineup_routes, "render_template", side_effect=lambda template, **context: rendered.update(context) or template
        ):
            assert lineup_routes.lineups_auto_submit.__wrapped__() == "lineups/summary.html"

        assert fetch.call_count == 2
        assert len(rendered["results"]) == 2
        assert all(result["ok"] for result in rendered["results"])


def test_batch_review_projection_exception_is_per_league_error(db_app):
    with db_app.test_request_context("/lineups/review", method="POST", data={"week": "1"}):
        league = _league_with_roster({721: "ACTIVE"})
        league.lineup_mode = "MANUAL"
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        rendered = {}
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=league.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "_user_synced_leagues", return_value=[league]), patch.object(
            lineup_routes, "fetch_projected_scores", side_effect=RuntimeError("projection down")
        ), patch.object(
            lineup_routes, "render_template", side_effect=lambda template, **context: rendered.update(context) or template
        ):
            assert lineup_routes.lineups_review.__wrapped__() == "lineups/review.html"

        assert rendered["items"][0]["refresh_warning"] == "Projection error: projection down"


def test_batch_review_threaded_projection_worker_uses_primitive_snapshot(db_app):
    with db_app.test_request_context("/lineups/review", method="POST", data={"week": "2"}):
        first = _league_with_roster({731: "ACTIVE"})
        first.lineup_mode = "MANUAL"
        second = _additional_league_with_roster(first.user, "457", 732)
        db.session.commit()
        calls = []
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=first.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "_user_synced_leagues", return_value=[first, second]), patch.object(
            lineup_routes, "_refresh_lineup_roster",
            side_effect=lambda league: (db.session.commit() or True, None, "host", "cookie")
        ), patch.object(
            lineup_routes, "fetch_projected_scores",
            side_effect=lambda host, mfl_id, year, week, ids, cookie=None: calls.append((mfl_id, year, ids)) or {}
        ), patch.object(lineup_routes, "render_template", return_value="review"):
            assert lineup_routes.lineups_review.__wrapped__() == "review"

        assert {call[0] for call in calls} == {"123", "457"}


def test_batch_submit_threaded_worker_maps_result_to_league_on_main_thread(db_app):
    with db_app.test_request_context(
        "/lineups/submit", method="POST",
        data={"week": "3", "include_1": "1", "starters_1[]": "741"},
    ):
        league = _league_with_roster({741: "ACTIVE"})
        league.lineup_mode = "MANUAL"
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        rendered = {}
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=league.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "_user_synced_leagues", return_value=[league]), patch.object(
            lineup_routes, "submit_lineup", return_value=(True, "OK")
        ) as submit, patch.object(
            lineup_routes, "render_template", side_effect=lambda template, **context: rendered.update(context) or template
        ):
            assert lineup_routes.lineups_submit.__wrapped__() == "lineups/summary.html"

        submit.assert_called_once_with("api.myfantasyleague.com", "123", 2026, 3, [741], cookie=None)
        assert rendered["results"][0]["league"] is league


def test_rapid_renders_best_ball_state_without_projection(db_app):
    with db_app.test_request_context("/lineups/rapid/league"):
        league = _league_with_roster({801: "ACTIVE"})
        league.lineup_mode = "UNKNOWN"
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
            with patch.object(lineup_routes, "pick_optimal_lineup") as optimizer, patch(
                "services.lineups_service.MFLClient.get_league_info",
                return_value=b'<league bestLineup="Yes"/>',
            ) as get_info:
                response = lineup_routes.lineups_rapid_league.__wrapped__()
        assert response == "lineups/rapid_league.html"
        assert context["best_ball"] is True
        projections.assert_not_called()
        optimizer.assert_not_called()
        submit.assert_not_called()
        get_info.assert_called_once()
        assert league.lineup_mode == "BEST_BALL"


@pytest.mark.parametrize(
    "league_info, expected_mode",
    [(b'<league bestLineup="No"/>', "MANUAL"), (b"<league/>", "UNKNOWN")],
)
def test_rapid_unknown_manual_or_unresolved_uses_manual_fallback(league_info, expected_mode, db_app):
    with db_app.test_request_context("/lineups/rapid/league"):
        league = _league_with_roster({850: "ACTIVE"})
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        from flask import session
        session.update(rapid_queue=[league.id], rapid_idx=0, rapid_week=1)
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=league.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "render_template", return_value="rapid"), patch.object(
            lineup_routes, "fetch_projected_scores", return_value={}
        ) as projections, patch(
            "services.lineups_service.MFLClient.get_league_info", return_value=league_info
        ) as get_info:
            assert lineup_routes.lineups_rapid_league.__wrapped__() == "rapid"
        get_info.assert_called_once()
        projections.assert_called_once()
        assert league.lineup_mode == expected_mode


def test_rapid_lineup_mode_refresh_failure_continues_manual_fallback(db_app, caplog):
    with db_app.test_request_context("/lineups/rapid/league"):
        league = _league_with_roster({875: "ACTIVE"})
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        from flask import session
        session.update(rapid_queue=[league.id], rapid_idx=0, rapid_week=1)
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=league.user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch.object(lineup_routes, "render_template", return_value="rapid"), patch.object(
            lineup_routes, "fetch_projected_scores", return_value={}
        ) as projections, patch(
            "services.lineups_service.MFLClient.get_league_info", side_effect=TimeoutError("timeout")
        ):
            assert lineup_routes.lineups_rapid_league.__wrapped__() == "rapid"
        projections.assert_called_once()
        assert league.lineup_mode == "UNKNOWN"
        assert "proceeding with manual-lineup fallback" in caplog.text


def test_stale_rapid_form_resolving_best_ball_skips_before_import(db_app):
    with db_app.app_context():
        league = _league_with_roster({890: "ACTIVE"})
        league.roster_status_synced_at = datetime.utcnow()
        db.session.commit()
        league_id, user_id = league.id, league.user_id
    with db_app.test_request_context(
        "/lineups/rapid/submit", method="POST",
        data={"league_id": str(league_id), "week": "1", "starters[]": "890"},
    ):
        from flask import session
        session.update(rapid_queue=[league_id], rapid_idx=0, rapid_week=1)
        with patch.object(lineup_routes, "current_user", SimpleNamespace(id=user_id)), patch.object(
            lineup_routes, "_require_recent_sync_or_gate", return_value=None
        ), patch("services.lineups_service.MFLClient.get_league_info", return_value=b'<league bestLineup="Yes"/>'), patch.object(
            lineup_routes, "submit_lineup"
        ) as submit:
            response = lineup_routes.lineups_rapid_submit.__wrapped__()
        assert response.get_json()["ok"] is True
        assert response.get_json()["skipped"] is True
        assert "Best Ball" in response.get_json()["message"]
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
