from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, session

from app import db
import lineups.routes as routes
from models import League, Player, Roster, Team, User
from services.lineups_service import Projection


def _app_and_league():
    app = Flask(__name__)
    app.config.update(TESTING=True, SECRET_KEY="test", SQLALCHEMY_DATABASE_URI="sqlite://",
                      SQLALCHEMY_TRACK_MODIFICATIONS=False, MFL_CURRENT_WEEK=2)
    app.register_blueprint(routes.lineups_bp)
    db.init_app(app)
    with app.app_context():
        db.create_all()
        user = User(username="locks", email="locks@example.com")
        league = League(user=user, mfl_id="123", name="Locks", year=2026,
                        franchise_id="0001", roster_slots="1:WR:1", lineup_mode="MANUAL")
        team = Team(league=league, mfl_id="0001", name="Mine")
        db.session.add_all([user, league, team])
        db.session.flush()
        for pid in (1, 2):
            player = Player(id=pid, mfl_id=str(pid), name=f"Player {pid}", position="WR", team="TBB")
            db.session.add(player)
            db.session.add(Roster(team=team, player=player, roster_status="ACTIVE"))
        db.session.commit()
        ids = league.id, user.id
    return app, ids


def test_lock_safe_direct_prefill_keeps_locked_starter():
    players = [(1, "Evans", "WR", "TBB"), (2, "Higher", "WR", "ATL")]
    context = {"safe": True, "warning": None, "current": {1},
               "states": {1: "LOCKED", 2: "UNLOCKED"},
               "locked_starters": {1}, "locked_bench": set()}
    grouped, selected, warning = routes._lock_safe_view(
        SimpleNamespace(), players, {1: Projection(1, 5), 2: Projection(2, 30)},
        1, {"WR": (1, 1)}, {1: "ACTIVE", 2: "ACTIVE"}, context)
    assert selected == {1} and warning is None
    assert grouped["WR"][1]["locked_as_starter"] is True


def test_checker_prefill_is_exact_and_locked_bench_is_removed():
    players = [(1, "Starter", "WR", "TBB"), (2, "Bench", "WR", "ATL")]
    context = {"safe": True, "warning": None, "current": {1},
               "states": {1: "UNLOCKED", 2: "LOCKED"},
               "locked_starters": set(), "locked_bench": {2}}
    _grouped, selected, warning = routes._lock_safe_view(
        SimpleNamespace(), players, {1: Projection(1, 5), 2: Projection(2, 30)},
        1, {"WR": (1, 1)}, {1: "ACTIVE", 2: "ACTIVE"}, context, [1])
    assert selected == {1} and warning is None


def test_unknown_schedule_preserves_current_and_disables_every_player():
    players = [(1, "Starter", "WR", "TBB"), (2, "Bench", "WR", "ATL")]
    context = {"safe": False, "warning": "unavailable", "current": {1},
               "states": {1: "UNKNOWN", 2: "UNKNOWN"},
               "locked_starters": {1}, "locked_bench": {2}}
    grouped, selected, warning = routes._lock_safe_view(
        SimpleNamespace(), players, {}, 1, {"WR": (1, 1)},
        {1: "ACTIVE", 2: "ACTIVE"}, context, [2])
    assert selected == {1} and warning == "unavailable"
    assert all(row["is_locked"] for row in grouped["WR"])


def test_checker_bridge_stores_one_league_and_exact_recommendation():
    app, (league_id, user_id) = _app_and_league()
    with app.test_request_context("/lineups/check/review", method="POST",
                                  data={"week": "2", "recommended_starters[]": ["1"]}):
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_pick_year_for_week_lookup", return_value=2026):
            response = routes.lineups_check_review.__wrapped__(league_id)
        assert response.status_code == 302
        assert session["rapid_queue"] == [league_id]
        assert session["rapid_prefill"] == {str(league_id): [1]}
        assert session["rapid_source"] == "lineup_checker"


def test_rapid_submit_rejects_locked_tampering_before_import():
    app, (league_id, user_id) = _app_and_league()
    lock_context = {"safe": True, "locked_starters": {1}, "locked_bench": {2}}
    with app.test_request_context("/lineups/rapid/submit", method="POST",
                                  data={"league_id": league_id, "week": 2, "starters[]": "2"}):
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_require_recent_sync_or_gate", return_value=None), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch.object(routes, "_live_lock_context", return_value=lock_context), \
             patch.object(routes, "submit_lineup") as submit:
            response, code = routes.lineups_rapid_submit.__wrapped__()
        assert code == 409
        assert "game locked" in response.get_json()["message"]
        submit.assert_not_called()


def test_locked_templates_include_hidden_starter_without_counting_it_as_checkbox():
    for template in ("templates/lineups/rapid_league.html", "templates/lineups/single_league.html"):
        source = open(template, encoding="utf-8").read()
        assert 'type="hidden" name="starters[]"' in source
        assert 'type="checkbox"' in source
