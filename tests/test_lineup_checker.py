from services.lineup_check_service import (
    build_constrained_optimal_lineup, check_league_lineup, check_user_lineups,
)
from services.lineups_service import Projection


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
