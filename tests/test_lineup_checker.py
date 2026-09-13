from services.lineup_check_service import (
    build_constrained_optimal_lineup, check_league_lineup, check_user_lineups,
)
from services.lineups_service import Projection
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask
import lineups.routes as lineup_routes


def player(pid, pos="RB", projection=1, status="ACTIVE", team="PIT"):
    return {"player_id": pid, "name": f"P{pid}", "position": pos, "team": team,
            "roster_status": status, "projection": projection}


def test_constrained_optimizer_freezes_and_excludes_and_obeys_ranges():
    players = [player(1, "QB", 5), player(2, "QB", 20), player(3, "RB", 8),
               player(4, "RB", 30), player(5, "WR", 10)]
    result = build_constrained_optimal_lineup(players, 3, {"QB": (1, 1), "RB": (1, 2)}, {1}, {4})
    assert result["ok"]
    assert set(result["starter_ids"]) == {1, 3, 5}
    impossible = build_constrained_optimal_lineup(players, 2, {"QB": (2, 2)}, {1}, {2})
    assert not impossible["ok"] and impossible["reason"]


def test_composite_tdl_rule_returns_eleven_legal_starters():
    players = ([player(1, "QB", 30), player(2, "QB", 10)] +
               [player(pid, "RB", 30 - pid) for pid in range(3, 10)] +
               [player(10, "WR", 18), player(11, "WR", 17), player(12, "TE", 16)])
    ranges = {"QB": (1, 2), "RB": (1, 9), "WR+TE": (1, 9)}
    result = build_constrained_optimal_lineup(players, 11, ranges, set(), set())
    assert result["ok"] and len(result["starter_ids"]) == 11
    selected = [p for p in players if p["player_id"] in result["starter_ids"]]
    assert sum(p["position"] == "QB" for p in selected) >= 1
    assert 1 <= sum(p["position"] in {"WR", "TE"} for p in selected) <= 9


def test_overlapping_composite_constraints_enforce_all_minimums_and_maximums():
    players = ([player(1, "QB", 100), player(2, "QB", 90)] +
               [player(pid, "RB", 80 - pid) for pid in range(3, 9)] +
               [player(pid, "WR", 70 - pid) for pid in range(9, 15)] +
               [player(pid, "TE", 60 - pid) for pid in range(15, 19)])
    ranges = {"QB": (1, 2), "RB": (1, 4), "WR": (2, 5), "TE": (1, 3),
              "RB+WR+TE": (6, 9)}
    result = build_constrained_optimal_lineup(players, 10, ranges, set(), set())
    assert result["ok"]
    positions = [p["position"] for p in players if p["player_id"] in result["starter_ids"]]
    assert 1 <= positions.count("QB") <= 2
    assert 1 <= positions.count("RB") <= 4
    assert 2 <= positions.count("WR") <= 5
    assert 1 <= positions.count("TE") <= 3
    assert 6 <= sum(pos in {"RB", "WR", "TE"} for pos in positions) <= 9


def job(players, slots="1:RB:1"):
    return {"league_id": 1, "league_name": "League", "players": players,
            "roster_slots": slots, "player_ids": [p["player_id"] for p in players]}


def run(players, statuses, injuries=None, states=None, slots="1:RB:1"):
    projections = {p["player_id"]: Projection(p["player_id"], p.get("projection")) for p in players}
    return check_league_lineup(job(players, slots), week=2, injuries=injuries or {}, injuries_ok=True,
        game_states=states or {"PIT": {"state": "UNLOCKED", "kickoff_at_utc": "later"}},
        schedule_ok=True, week_complete=True, roster_statuses=statuses, projections=projections)


def test_live_status_wins_threshold_and_no_lineup():
    players = [player(1, projection=8), player(2, projection=12)]
    result = run(players, {1: "S", 2: "NS"})
    assert result["classification"] == "ACTION" and result["entering_player_ids"] == [2]
    assert run([player(1, projection=8), player(2, projection=8.5)], {1: "S", 2: "NS"})["classification"] == "GOOD"
    assert run(players, {1: "NS", 2: "NS"})["classification"] == "CRITICAL"


def test_taxi_ir_unknown_and_locked_bench_are_never_recommended():
    base = player(1, projection=5)
    for status in ("TAXI", "IR", "UNKNOWN"):
        result = run([base, player(2, projection=50, status=status)], {1: "S", 2: "NS"})
        assert result["entering_player_ids"] == []
    states = {"PIT": {"state": "UNLOCKED", "kickoff_at_utc": None},
              "BAL": {"state": "LOCKED", "kickoff_at_utc": None}}
    result = run([base, player(2, projection=50, team="BAL")], {1: "S", 2: "NS"}, states=states)
    assert result["entering_player_ids"] == []


def test_no_game_starter_is_critical_and_replaceable_but_bench_is_forbidden():
    starter = run([player(1, projection=50, team="FA"), player(2, projection=5)],
                  {1: "S", 2: "NS"})
    assert starter["classification"] == "CRITICAL"
    assert starter["recommended_starter_ids"] == [2]
    assert any(f["type"] == "NO_GAME_STARTER" for f in starter["findings"])
    bench = run([player(1, projection=5), player(2, projection=50, team="fa")],
                {1: "S", 2: "NS"})
    assert bench["recommended_starter_ids"] == [1]


def test_locked_out_starter_is_critical_frozen_and_missing_projection_has_no_fake_gain():
    players = [player(1, projection=None), player(2, projection=20, team="BAL")]
    states = {"PIT": {"state": "LOCKED", "kickoff_at_utc": None},
              "BAL": {"state": "UNLOCKED", "kickoff_at_utc": None}}
    result = run(players, {1: "S", 2: "NS"}, injuries={1: {"status": "Out"}}, states=states)
    assert result["classification"] == "CRITICAL"
    assert result["recommended_starter_ids"] == [1]
    assert result["projected_gain"] is None
    assert "No lineup change" in result["findings"][0]["message"]


def test_questionable_and_doubtful_are_watch_not_removed():
    for status in ("Questionable", "Doubtful"):
        result = run([player(1, projection=10)], {1: "S"}, injuries={1: {"status": status}})
        assert result["classification"] == "WATCH" and result["recommended_starter_ids"] == [1]


def test_missing_projection_starter_is_watch_preserved_and_has_no_fake_gain():
    result = run([player(1, projection=None), player(2, projection=50)], {1: "S", 2: "NS"})
    assert result["classification"] == "WATCH"
    assert result["recommended_starter_ids"] == [1]
    assert result["projected_gain"] is None
    assert result["leaving_player_ids"] == result["entering_player_ids"] == []
    assert any(f["type"] == "NO_PROJECTION_STARTER" for f in result["findings"])


def test_numeric_zero_is_known_but_sub_threshold_projection_swap_is_hidden():
    action = run([player(1, projection=0.0), player(2, projection=3)], {1: "S", 2: "NS"})
    assert action["classification"] == "ACTION" and action["entering_player_ids"] == [2]
    quiet = run([player(1, projection=0.0), player(2, projection=1.9)], {1: "S", 2: "NS"})
    assert quiet["classification"] == "GOOD"
    assert quiet["recommended_starter_ids"] == [1]
    assert quiet["projected_gain"] is None


def test_critical_repair_is_exposed_even_below_projection_threshold():
    result = run([player(1, projection=10, team="FA"), player(2, projection=9)],
                 {1: "S", 2: "NS"})
    assert result["classification"] == "CRITICAL"
    assert result["leaving_player_ids"] == [1] and result["entering_player_ids"] == [2]


def test_scan_fetches_each_league_once_skips_best_ball_isolates_failure_and_is_plain():
    jobs = [dict(job([player(1)]), host="example", mfl_id="1", year=2026, cookie="", franchise_id="1", best_ball=False),
            dict(job([]), league_id=2, league_name="Best", host="example", mfl_id="2", year=2026, cookie="", franchise_id="2", best_ball=True)]
    calls = {"status": 0, "projection": 0}
    def statuses(snapshot, week):
        assert isinstance(snapshot, dict) and not hasattr(snapshot, "_sa_instance_state")
        calls["status"] += 1; return {1: "S"}
    def projections(*args, **kwargs):
        calls["projection"] += 1; return {1: Projection(1, 1)}
    result = check_user_lineups(jobs, season=2026, week=2, injuries={}, injuries_ok=True,
        game_states={"PIT": {"state": "UNLOCKED", "kickoff_at_utc": None}}, schedule_ok=True,
        week_complete=True, status_fetcher=statuses, projection_fetcher=projections)
    assert calls == {"status": 1, "projection": 1}
    assert result["summary"]["best_ball_skipped"] == 1


def test_schedule_or_roster_refresh_failure_fails_closed():
    jobs = [dict(job([player(1)]), host="x", mfl_id="1", year=2026, cookie="", franchise_id="1",
                 best_ball=False, refresh_error="roster refresh failed")]
    result = check_user_lineups(jobs, season=2026, week=2, injuries={}, injuries_ok=True,
        game_states={}, schedule_ok=False, week_complete=False,
        status_fetcher=lambda *_: (_ for _ in ()).throw(AssertionError()),
        projection_fetcher=lambda *_args, **_kwargs: {})
    assert result["leagues"][0]["classification"] == "ERROR"


def test_engine_never_calls_lineup_submit(monkeypatch):
    calls = []
    monkeypatch.setattr("services.lineups_service.submit_lineup", lambda *a, **k: calls.append((a, k)))
    result = run([player(1, projection=5), player(2, projection=10)], {1: "S", 2: "NS"})
    assert result["classification"] == "ACTION"
    assert calls == []


def test_global_feed_failure_returns_no_swap():
    players = [player(1, projection=5), player(2, projection=50)]
    projections = {p["player_id"]: Projection(p["player_id"], p["projection"]) for p in players}
    result = check_league_lineup(job(players), week=2, injuries={}, injuries_ok=False,
        game_states={}, schedule_ok=False, week_complete=False, roster_statuses={1: "S", 2: "NS"},
        projections=projections)
    assert result["recommended_starter_ids"] == [1]
    assert result["entering_player_ids"] == []


def test_missing_projection_healthy_and_questionable_starters_are_frozen():
    players = [player(1, projection=None), player(2, projection=20)]
    healthy = run(players, {1: "S", 2: "NS"})
    assert healthy["recommended_starter_ids"] == [1]
    assert healthy["entering_player_ids"] == healthy["leaving_player_ids"] == []
    watch = run(players, {1: "S", 2: "NS"}, injuries={1: {"status": "Questionable"}})
    assert watch["classification"] == "WATCH" and watch["recommended_starter_ids"] == [1]


def test_missing_projection_out_starter_can_be_repaired_without_fake_gain():
    players = [player(1, projection=None), player(2, projection=20)]
    result = run(players, {1: "S", 2: "NS"}, injuries={1: {"status": "Out"}})
    assert result["entering_player_ids"] == [2] and result["leaving_player_ids"] == [1]
    assert result["projected_gain"] is None and result["classification"] == "CRITICAL"


def test_incomplete_lineup_is_critical_and_has_entering_only_recommendation():
    players = [player(1, projection=10), player(2, projection=9)]
    result = run(players, {1: "S", 2: "NS"}, slots="2:RB:1-2")
    assert result["classification"] == "CRITICAL"
    assert result["leaving_player_ids"] == [] and result["entering_player_ids"] == [2]
    assert any(f["type"] == "INCOMPLETE_LINEUP" for f in result["findings"])


def test_overfilled_lineup_fails_safely_without_recommendation():
    result = run([player(1), player(2)], {1: "S", 2: "S"}, slots="1:RB:1")
    assert result["classification"] == "CRITICAL"
    assert result["recommended_starter_ids"] == [1, 2]
    assert result["entering_player_ids"] == result["leaving_player_ids"] == []
    assert any(f["type"] == "INVALID_LINEUP_COUNT" for f in result["findings"])


def test_route_fetches_sitewide_feeds_once_and_filters_historical_leagues():
    app = Flask(__name__); app.config.update(SECRET_KEY="test", TESTING=True)
    current = SimpleNamespace(id=1, year=2026, mfl_id="1", name="Current", league_host="api.myfantasyleague.com",
        franchise_id="0001", roster_slots="1:RB:1", lineup_mode="MANUAL")
    historical = SimpleNamespace(id=2, year=2025, mfl_id="2", name="Old", league_host="api.myfantasyleague.com",
        franchise_id="0002", roster_slots="1:RB:1", lineup_mode="MANUAL")
    payload = {"fullNflSchedule": {"nflSchedule": [{"week": "2", "matchup":
        {"kickoff": "9999999999", "team": [{"id": "PIT"}, {"id": "BAL"}]}}]}}
    captured = {}
    def checker(jobs, **kwargs):
        captured["jobs"] = jobs
        return {"week": 2, "summary": {}, "leagues": []}
    with app.test_request_context("/lineups/check"), \
         patch.object(lineup_routes, "_require_recent_sync_or_gate", return_value=None), \
         patch.object(lineup_routes, "_pick_year_for_week_lookup", return_value=2026), \
         patch.object(lineup_routes, "_effective_current_week", return_value=2), \
         patch.object(lineup_routes, "_user_synced_leagues", return_value=[current, historical]), \
         patch.object(lineup_routes, "_resolve_lineup_mode", return_value="MANUAL"), \
         patch.object(lineup_routes, "ensure_roster_status_fresh", return_value=(True, None)), \
         patch.object(lineup_routes, "build_players_for_review", return_value=[]), \
         patch.object(lineup_routes, "get_my_team_roster_statuses", return_value={}), \
         patch.object(lineup_routes, "render_template", side_effect=lambda _name, result: result), \
         patch("services.nfl_schedule_service.fetch_mfl_nfl_schedule", return_value=payload) as schedule_fetch, \
         patch("services.nfl_schedule_service.sync_nfl_schedule"), \
         patch("services.lineup_check_service.fetch_injuries", return_value={}) as injury_fetch, \
         patch("services.lineup_check_service.check_user_lineups", side_effect=checker):
        response = lineup_routes.lineups_check.__wrapped__()
    schedule_fetch.assert_called_once_with(2026)
    injury_fetch.assert_called_once_with(2026, 2)
    assert [item["league_id"] for item in captured["jobs"]] == [1]
    assert response["week"] == 2
