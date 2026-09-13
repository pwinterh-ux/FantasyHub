from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import Flask, session

from app import db
import lineups.routes as routes
from models import League, Player, Roster, Team, User
from services.lineups_service import Projection
from services.lineup_lock_service import lock_violation, resolve_lineup_locks


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
               "locked_starters": set(), "locked_bench": set(),
               "unknown_starters": {1}, "unknown_bench": {2}, "bye_players": set()}
    grouped, selected, warning = routes._lock_safe_view(
        SimpleNamespace(), players, {}, 1, {"WR": (1, 1)},
        {1: "ACTIVE", 2: "ACTIVE"}, context, [2])
    assert selected == {1} and warning == "unavailable"
    assert all(not row["is_editable"] for row in grouped["WR"])


def test_checker_bridge_stores_one_league_and_exact_recommendation():
    app, (league_id, user_id) = _app_and_league()
    with app.test_request_context("/lineups/check/review", method="POST",
                                  data={"week": "2", "recommended_starters[]": ["1"]}):
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_pick_year_for_week_lookup", return_value=2026):
            response = routes.lineups_check_review.__wrapped__(league_id)
        assert response.status_code == 302
        assert session["checker_review_queue"] == [league_id]
        assert session["checker_review_prefills"] == {str(league_id): [1]}
        assert session["checker_review_idx"] == 0
        assert not any(key.startswith("rapid_") for key in session)


def test_checker_bridge_rejects_other_owner_and_non_rostered_ids():
    app, (league_id, user_id) = _app_and_league()
    for acting_user, recommendation in ((user_id + 99, "1"), (user_id, "999")):
        with app.test_request_context("/lineups/check/review", method="POST",
                                      data={"week": "2", "recommended_starters[]": recommendation}):
            with patch.object(routes, "current_user", SimpleNamespace(id=acting_user)), \
                 patch.object(routes, "_pick_year_for_week_lookup", return_value=2026):
                response = routes.lineups_check_review.__wrapped__(league_id)
            assert response.status_code == 302
            assert "checker_review_prefills" not in session


def test_normal_rapid_start_uses_only_normal_queue_state():
    app, (league_id, user_id) = _app_and_league()
    with app.test_request_context("/lineups/rapid", method="POST", data={"week": "2"}):
        session.update(checker_review_queue=[league_id], checker_review_idx=0,
                       checker_review_prefills={str(league_id): [1]})
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_require_recent_sync_or_gate", return_value=None), \
            patch.object(routes, "_user_synced_leagues",
                          return_value=[SimpleNamespace(id=league_id, user_id=user_id)]):
            response = routes.lineups_rapid_start.__wrapped__()
        assert response.status_code == 302
        assert session["rapid_queue"] == [league_id] and session["rapid_idx"] == 0
        assert not any(key.startswith("checker_review_") for key in session)


def test_checker_review_uses_exact_prefill_without_normal_rapid_state():
    app, (league_id, user_id) = _app_and_league()
    locks = {"safe": True, "warning": None, "current": {1},
             "states": {1: "UNLOCKED", 2: "UNLOCKED"}, "locked_starters": set(),
             "locked_bench": set(), "unknown_starters": set(), "unknown_bench": set(),
             "bye_players": set()}
    captured = {}
    with app.test_request_context("/lineups/check/review"):
        session.update(checker_review_queue=[league_id], checker_review_idx=0,
                       checker_review_week=2, checker_review_prefills={str(league_id): [1]})
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch.object(routes, "fetch_projected_scores",
                          return_value={1: Projection(1, 1), 2: Projection(2, 99)}), \
             patch.object(routes, "_live_lock_context", return_value=locks), \
             patch.object(routes, "render_template",
                          side_effect=lambda template, **values: captured.update(values) or template):
            response = routes.lineups_checker_review.__wrapped__()
        assert response == "lineups/checker_review.html"
        assert captured["auto_selected"] == {1}
        assert not any(key.startswith("rapid_") for key in session)


def test_checker_review_reoptimizes_a_stale_prefill_for_current_locks():
    app, (league_id, user_id) = _app_and_league()
    locks = {"safe": True, "warning": None, "current": {1},
             "states": {1: "LOCKED", 2: "UNLOCKED"}, "locked_starters": {1},
             "locked_bench": set(), "unknown_starters": set(), "unknown_bench": set(),
             "bye_players": set(), "no_game_players": set()}
    captured = {}
    with app.test_request_context("/lineups/check/review"):
        session.update(checker_review_queue=[league_id], checker_review_idx=0,
                       checker_review_week=2, checker_review_prefills={str(league_id): [2]})
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch.object(routes, "fetch_projected_scores",
                          return_value={1: Projection(1, 1), 2: Projection(2, 99)}), \
             patch.object(routes, "_live_lock_context", return_value=locks), \
             patch.object(routes, "render_template",
                          side_effect=lambda template, **values: captured.update(values) or template):
            routes.lineups_checker_review.__wrapped__()
    assert captured["auto_selected"] == {1}
    assert "reconciliation_status" not in captured
    assert captured["blocking_status"] is None


@pytest.mark.parametrize("submitted", ([], [2]))
def test_rapid_submit_rejects_locked_tampering_before_import(submitted):
    app, (league_id, user_id) = _app_and_league()
    lock_context = {"safe": True, "locked_starters": {1}, "locked_bench": {2}}
    with app.test_request_context("/lineups/rapid/submit", method="POST",
                                  data={"league_id": league_id, "week": 2, "starters[]": submitted}):
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_require_recent_sync_or_gate", return_value=None), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch.object(routes, "_live_lock_context", return_value=lock_context), \
             patch.object(routes, "submit_lineup") as submit:
            response, code = routes.lineups_rapid_submit.__wrapped__()
        assert code == 409
        assert "game locked" in response.get_json()["message"]
        submit.assert_not_called()


@pytest.mark.parametrize("submitted", ([], [2]))
def test_single_submit_rejects_locked_tampering_before_import(submitted):
    app, (league_id, user_id) = _app_and_league()
    context = {"safe": True, "locked_starters": {1}, "locked_bench": {2}}
    with app.test_request_context("/lineups/league/1/submit", method="POST",
                                  data={"week": 2, "starters[]": submitted}):
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_require_recent_sync_or_gate", return_value=None), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch.object(routes, "_live_lock_context", return_value=context), \
             patch.object(routes, "submit_lineup") as submit:
            response, code = routes.lineups_single_submit.__wrapped__(league_id)
        assert code == 409
        submit.assert_not_called()


def test_checker_submit_and_skip_use_independent_queue_while_normal_skip_keeps_queue():
    app, (league_id, user_id) = _app_and_league()
    safe = {"safe": True, "locked_starters": set(), "locked_bench": set(),
            "unknown_starters": set(), "unknown_bench": set(), "bye_players": set()}
    with app.test_request_context("/lineups/check/review/submit", method="POST",
                                  data={"league_id": league_id, "week": 2, "starters[]": ["1"]}):
        session.update(checker_review_queue=[league_id], checker_review_idx=0,
                       checker_review_week=2)
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch.object(routes, "_live_lock_context", return_value=safe), \
             patch.object(routes, "submit_lineup", return_value=(True, "OK")):
            response = routes.lineups_checker_review_submit.__wrapped__()
        assert response.get_json()["redirect"].endswith("/lineups/check")
        assert "checker_review_queue" not in session
    with app.test_request_context("/lineups/check/review/skip", method="POST"):
        session.update(checker_review_queue=[league_id], checker_review_idx=0,
                       checker_review_week=2)
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)):
            response = routes.lineups_checker_review_skip.__wrapped__()
        assert response.get_json()["redirect"].endswith("/lineups/check")
    with app.test_request_context("/lineups/rapid/skip", method="POST"):
        session.update(rapid_queue=[league_id, league_id], rapid_idx=0)
        response = routes.lineups_rapid_skip.__wrapped__()
        assert response.get_json()["next"] is True
        assert "redirect" not in response.get_json()


def test_checker_submit_rejects_posted_week_tampering_without_mfl_submit():
    app, (league_id, user_id) = _app_and_league()
    with app.test_request_context("/lineups/check/review/submit", method="POST",
                                  data={"league_id": league_id, "week": 2,
                                        "starters[]": ["1"]}):
        session.update(checker_review_queue=[league_id], checker_review_idx=0,
                       checker_review_week=1)
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_effective_current_week", return_value=1), \
             patch.object(routes, "submit_lineup") as submit:
            response, code = routes.lineups_checker_review_submit.__wrapped__()
        assert code == 409
        submit.assert_not_called()


def test_checker_review_exit_clears_state_and_redirects_to_checker():
    app, _ids = _app_and_league()
    with app.test_request_context("/lineups/check/review/exit"):
        session.update(checker_review_queue=[1], checker_review_idx=0,
                       checker_review_week=2, checker_review_prefills={"1": [1]})
        response = routes.lineups_checker_review_exit.__wrapped__()
        assert response.status_code == 302 and response.location.endswith("/lineups/check")
        assert not any(key.startswith("checker_review_") for key in session)


def test_checker_review_stale_get_clears_deleted_league_state():
    app, (_league_id, user_id) = _app_and_league()
    with app.test_request_context("/lineups/check/review"):
        session.update(checker_review_queue=[999], checker_review_idx=0,
                       checker_review_week=2, checker_review_prefills={"999": [1]})
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)):
            response = routes.lineups_checker_review.__wrapped__()
        assert response.status_code == 302 and response.location.endswith("/lineups/check")
        assert not any(key.startswith("checker_review_") for key in session)


def test_locked_templates_include_hidden_starter_without_counting_it_as_checkbox():
    for template in ("templates/lineups/_compact_lineup_editor.html", "templates/lineups/single_league.html"):
        source = open(template, encoding="utf-8").read()
        assert 'type="hidden" name="starters[]"' in source
        assert 'type="checkbox"' in source


def _schedule(week=1, kickoff="9999999999"):
    return {"nflSchedule": {"week": str(week), "matchup": {
        "kickoff": kickoff, "team": [{"id": "TBB"}, {"id": "ATL"}]}}}


def _lock_job():
    return {"host": "host", "year": 2026, "mfl_id": "123",
            "franchise_id": "0001", "cookie": "cookie"}


def test_current_week_uses_verified_kickoffs_and_future_week_needs_no_schedule_rows():
    players = [(1, "Starter", "WR", "TBB"), (2, "Bench", "WR", "ATL")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses",
               return_value={1: "S", 2: "NS"}), patch(
               "services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule()):
        current = resolve_lineup_locks(_lock_job(), players, 1)
        future = resolve_lineup_locks(_lock_job(), players, 2)
    assert current["safe"] and current["states"] == {1: "UNLOCKED", 2: "UNLOCKED"}
    assert future["safe"] and future["states"] == {1: "UNLOCKED", 2: "UNLOCKED"}
    with patch("services.lineup_lock_service.fetch_player_roster_statuses",
               return_value={1: "S", 2: "NS"}), patch(
               "services.lineup_lock_service.fetch_mfl_nfl_schedule",
               return_value=_schedule(kickoff="1")):
        locked = resolve_lineup_locks(_lock_job(), players, 1)
    assert locked["locked_starters"] == {1} and locked["locked_bench"] == {2}


def test_future_week_does_not_require_weekly_statuses_and_is_editable():
    players = [(1, "One", "WR", "TBB"), (2, "Two", "WR", "ATL")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses") as statuses, \
         patch("services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule(1)):
        result = resolve_lineup_locks(_lock_job(), players, 4)

    assert result == {
        "safe": True, "warning": None, "current": set(),
        "states": {1: "UNLOCKED", 2: "UNLOCKED"},
        "locked_starters": set(), "locked_bench": set(),
        "unknown_starters": set(), "unknown_bench": set(), "bye_players": set(),
        "no_game_players": set(),
    }
    statuses.assert_not_called()
    grouped, _selected, warning = routes._lock_safe_view(
        SimpleNamespace(), players, {1: Projection(1, 10), 2: Projection(2, 5)},
        1, {"WR": (1, 1)}, {1: "ACTIVE", 2: "ACTIVE"}, result)
    assert warning is None
    assert all(row["is_editable"] for row in grouped["WR"])


def test_current_week_missing_unlocked_status_is_safe_but_missing_locked_status_fails():
    players = [(1, "One", "WR", "TBB"), (2, "Two", "WR", "ATL")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses", return_value={1: "S"}), \
         patch("services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule()):
        unlocked = resolve_lineup_locks(_lock_job(), players, 1)
    assert unlocked["safe"]
    assert unlocked["states"] == {1: "UNLOCKED", 2: "UNLOCKED"}
    grouped, _selected, warning = routes._lock_safe_view(
        SimpleNamespace(), players, {1: Projection(1, 10), 2: Projection(2, 5)},
        1, {"WR": (1, 1)}, {1: "ACTIVE", 2: "ACTIVE"}, unlocked)
    assert warning is None
    assert all(row["is_editable"] for row in grouped["WR"])

    with patch("services.lineup_lock_service.fetch_player_roster_statuses", return_value={1: "S"}), \
         patch("services.lineup_lock_service.fetch_mfl_nfl_schedule",
               return_value=_schedule(kickoff="1")):
        locked = resolve_lineup_locks(_lock_job(), players, 1)
    assert not locked["safe"]
    assert "incomplete for a locked or unknown player" in locked["warning"]


def test_past_week_fails_closed():
    players = [(1, "Starter", "WR", "TBB")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses", return_value={1: "S"}), \
         patch("services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule(2)):
        result = resolve_lineup_locks(_lock_job(), players, 1)
    assert not result["safe"]
    assert result["unknown_starters"] == {1}


def test_future_week_rapid_renders_and_submits_through_live_lock_guard():
    app, (league_id, user_id) = _app_and_league()
    common = [
        patch.object(routes, "current_user", SimpleNamespace(id=user_id)),
        patch.object(routes, "_require_recent_sync_or_gate", return_value=None),
        patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")),
        patch("services.lineup_lock_service.fetch_player_roster_statuses"),
        patch("services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule(1)),
    ]
    with app.test_request_context("/lineups/rapid/league"):
        session.update(rapid_queue=[league_id], rapid_idx=0, rapid_week=2)
        with common[0], common[1], common[2], common[3] as statuses, common[4], \
             patch.object(routes, "fetch_projected_scores",
                          return_value={1: Projection(1, 10), 2: Projection(2, 5)}), \
             patch.object(routes, "render_template",
                          side_effect=lambda _template, **values: values):
            rendered = routes.lineups_rapid_league.__wrapped__()
        assert rendered["auto_selected"] == {1}
        statuses.assert_not_called()
    with app.test_request_context("/lineups/rapid/submit", method="POST",
                                  data={"league_id": league_id, "week": 2, "starters[]": ["1"]}):
        session.update(rapid_queue=[league_id], rapid_idx=0, rapid_week=2)
        with patch.object(routes, "current_user", SimpleNamespace(id=user_id)), \
             patch.object(routes, "_require_recent_sync_or_gate", return_value=None), \
             patch.object(routes, "_refresh_lineup_roster", return_value=(True, None, "host", "cookie")), \
             patch("services.lineup_lock_service.fetch_player_roster_statuses") as statuses, \
             patch("services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule(1)), \
             patch.object(routes, "submit_lineup", return_value=(True, "OK")) as submit:
            response = routes.lineups_rapid_submit.__wrapped__()
        assert response.get_json()["ok"] is True
        statuses.assert_not_called()
        submit.assert_called_once()


def test_no_game_player_is_per_player_and_known_players_remain_editable():
    players = [(1, "Known Starter", "WR", "TBB"), (2, "No Game Bench", "WR", "FA")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses",
               return_value={1: "S", 2: "NS"}), patch(
               "services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule()):
        result = resolve_lineup_locks(_lock_job(), players, 1)
    assert result["safe"]
    assert result["unknown_bench"] == set()
    assert result["no_game_players"] == {2}
    grouped, selected, _ = routes._lock_safe_view(
        SimpleNamespace(), players, {1: Projection(1, 5)}, 1, {"WR": (1, 1)},
        {1: "ACTIVE", 2: "ACTIVE"}, result, [1])
    assert selected == {1}
    assert next(row for row in grouped["WR"] if row["player_id"] == 1)["is_editable"]
    assert not next(row for row in grouped["WR"] if row["player_id"] == 2)["is_editable"]
    assert lock_violation(result, [1, 2]) is not None


def test_no_game_needs_no_weekly_status_and_does_not_fail_lock_resolution():
    players = [(1, "Known Starter", "WR", "TBB"), (2, "No Game Bench", "WR", "fA")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses", return_value={1: "S"}), \
         patch("services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule()):
        result = resolve_lineup_locks(_lock_job(), players, 1)
    assert result["safe"] and result["states"][2] == "NO_GAME"
    assert result["unknown_bench"] == set()


def test_no_game_current_starter_is_not_frozen_and_can_be_replaced():
    players = [(1, "No Game Starter", "WR", "FA"), (2, "Replacement", "WR", "ATL")]
    with patch("services.lineup_lock_service.fetch_player_roster_statuses",
               return_value={1: "S", 2: "NS"}), patch(
               "services.lineup_lock_service.fetch_mfl_nfl_schedule", return_value=_schedule()):
        context = resolve_lineup_locks(_lock_job(), players, 1)
    grouped, selected, warning = routes._lock_safe_view(SimpleNamespace(), players,
        {1: Projection(1, 50), 2: Projection(2, 5)}, 1, {"WR": (1, 1)},
        {1: "ACTIVE", 2: "ACTIVE"}, context)
    assert warning is None and selected == {2}
    no_game = next(row for row in grouped["WR"] if row["player_id"] == 1)
    assert not no_game["is_locked"] and no_game["game_state"] == "NO_GAME"


def test_composite_checker_prefill_is_legal_and_survives_rapid_validation():
    players = ([(1, "QB", "QB", "TBB")] +
               [(pid, f"RB{pid}", "RB", "ATL") for pid in range(2, 9)] +
               [(9, "WR1", "WR", "TBB"), (10, "WR2", "WR", "ATL"),
                (11, "TE1", "TE", "TBB")])
    ranges = {"QB": (1, 2), "RB": (1, 9), "WR+TE": (1, 9)}
    ids = set(range(1, 12))
    assert routes._lineup_is_legal(ids, players, 11, ranges)
    context = {"safe": True, "warning": None, "current": ids,
               "states": {pid: "UNLOCKED" for pid in ids}, "locked_starters": set(),
               "locked_bench": set(), "unknown_starters": set(), "unknown_bench": set(),
               "bye_players": set(), "no_game_players": set()}
    _grouped, selected, warning = routes._lock_safe_view(SimpleNamespace(), players, {}, 11,
        ranges, {pid: "ACTIVE" for pid in ids}, context, sorted(ids))
    assert selected == ids and warning is None


def test_unknown_starter_is_frozen_without_disabling_known_bench():
    context = {"safe": True, "warning": None, "current": {1},
               "states": {1: "UNKNOWN", 2: "UNLOCKED"}, "locked_starters": set(),
               "locked_bench": set(), "unknown_starters": {1}, "unknown_bench": set(),
               "bye_players": set()}
    players = [(1, "Unknown Starter", "WR", "FA"), (2, "Known Bench", "WR", "ATL")]
    grouped, selected, _ = routes._lock_safe_view(SimpleNamespace(), players,
        {1: Projection(1, 1), 2: Projection(2, 30)}, 1, {"WR": (1, 1)},
        {1: "ACTIVE", 2: "ACTIVE"}, context)
    assert selected == {1}
    assert next(row for row in grouped["WR"] if row["player_id"] == 2)["is_editable"]


def test_bye_is_forbidden_but_current_bye_starter_is_not_locked():
    context = {"safe": True, "warning": None, "current": {1},
               "states": {1: "BYE", 2: "UNLOCKED"}, "locked_starters": set(),
               "locked_bench": set(), "unknown_starters": set(), "unknown_bench": set(),
               "bye_players": {1}}
    players = [(1, "Bye", "WR", "TBB"), (2, "Replacement", "WR", "ATL")]
    grouped, selected, _ = routes._lock_safe_view(SimpleNamespace(), players,
        {1: Projection(1, 40), 2: Projection(2, 5)}, 1, {"WR": (1, 1)},
        {1: "ACTIVE", 2: "ACTIVE"}, context)
    bye = next(row for row in grouped["WR"] if row["player_id"] == 1)
    assert selected == {2}
    assert not bye["is_locked"]
    assert lock_violation(context, [1]) is not None


def test_no_game_templates_have_badge_but_no_bye_or_lock_branch():
    for template in ("templates/lineups/_compact_lineup_editor.html", "templates/lineups/single_league.html"):
        source = open(template, encoding="utf-8").read()
        assert "r.game_state == 'NO_GAME'" in source
        assert ">NO GAME</span>" in source
        assert "Lineup Checker recommendation" not in source


def test_rapid_and_checker_share_compact_grid_without_checker_banner():
    rapid = open("templates/lineups/rapid_league.html", encoding="utf-8").read()
    checker = open("templates/lineups/checker_review.html", encoding="utf-8").read()
    shared = open("templates/lineups/_compact_lineup_editor.html", encoding="utf-8").read()
    assert '_compact_lineup_editor.html' in rapid and '_compact_lineup_editor.html' in checker
    assert "Lineup Checker recommendation" not in rapid + checker + shared
    assert 'aria-label="Locked"' in shared and ">NO GAME</span>" in shared
    assert "blocking_status" in shared and "action-bar" in shared
    assert "Updated for current game locks" not in shared
