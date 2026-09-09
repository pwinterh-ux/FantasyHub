from types import SimpleNamespace
from unittest.mock import patch

from services.mfl_client import MFLClient
import services.waivers_service as service


def test_top_adds_parser_preserves_order_and_normalizes_rows():
    parsed = MFLClient.parse_top_adds({
        "topAdds": {"week": "1", "player": [
            {"id": "17545", "percent": "18.42"},
            {"id": "17480", "percent": "12.10"},
            {"id": "017545", "percent": "99"},  # duplicate
            {"id": "bad", "percent": "4"},
        ]}
    })
    assert parsed["week"] == 1
    assert parsed["players"] == [
        {"mfl_id": "17545", "add_percent": 18.42, "trend_rank": 1},
        {"mfl_id": "17480", "add_percent": 12.1, "trend_rank": 2},
    ]


def test_top_adds_parser_handles_single_player_and_invalid_percent():
    parsed = MFLClient.parse_top_adds(
        '{"topAdds":{"week":"2","player":{"id":"00123","percent":"n/a"}}}'
    )
    assert parsed == {"week": 2, "players": [
        {"mfl_id": "123", "add_percent": None, "trend_rank": 1}
    ]}


def test_top_adds_cache_reuses_fresh_refetches_expired_and_keys_by_week():
    service._MFL_TRENDING_CACHE.clear()
    calls = []

    class Client:
        def __init__(self, year):
            self.year = year

        def get_top_adds(self, week):
            calls.append((self.year, week))
            return {"week": week, "players": []}

    with patch.object(service, "MFLClient", Client), patch.object(
        service.time, "time", side_effect=[100, 101, 1000, 1001]
    ):
        service.get_mfl_trending_adds(2026, 1)
        service.get_mfl_trending_adds(2026, 1)
        service.get_mfl_trending_adds(2026, 1)
        service.get_mfl_trending_adds(2026, 2)
    assert calls == [(2026, 1), (2026, 1), (2026, 2)]


def test_trending_builder_bulk_availability_keeps_rank_and_missing_data():
    class Field:
        def in_(self, values):
            return values

    class Query:
        def __init__(self, rows):
            self.rows = rows

        def filter(self, *args):
            return self

        def all(self):
            return self.rows

    fake_players = SimpleNamespace(
        mfl_id=Field(),
        query=Query([
            SimpleNamespace(id=1, mfl_id="1", name="A", position="WR", team="A", status=None),
            SimpleNamespace(id=2, mfl_id="2", name="B", position="RB", team="B", status=None),
            SimpleNamespace(id=3, mfl_id="3", name="C", position="WR", team="C", status=None),
        ]),
    )
    fake_ranks = SimpleNamespace(
        mfl_id=Field(),
        query=Query([SimpleNamespace(mfl_id="1", player_name="A", position="WR", positional_rank=7)]),
    )
    availability = {
        "1": {"available_count": 1, "total_leagues": 18, "rostered_count": 17},
        "2": {"available_count": 0, "total_leagues": 18, "rostered_count": 18},
        "3": {"available_count": 15, "total_leagues": 18, "rostered_count": 3},
        "4": {"available_count": 0, "total_leagues": 18, "rostered_count": 18},
    }
    trends = {"players": [
        {"mfl_id": "1", "trend_rank": 1, "add_percent": 20.0},
        {"mfl_id": "2", "trend_rank": 2, "add_percent": 10.0},
        {"mfl_id": "3", "trend_rank": 3, "add_percent": 5.0},
        {"mfl_id": "4", "trend_rank": 4, "add_percent": None},
    ]}
    with patch.object(service, "Player", fake_players), patch.object(
        service, "DynastyRankConsensusCurrent", fake_ranks
    ), patch.object(service, "get_players_availability", return_value=availability) as bulk:
        result = service.build_trending_waiver_targets(9, trends, year=2026)

    assert [row["mfl_id"] for row in result] == ["1", "2", "3", "4"]
    assert [row["available_count"] for row in result] == [1, 0, 15, 0]
    assert result[1]["positional_rank"] is None
    assert result[3]["name"] == "MFL Player 4"
    bulk.assert_called_once_with(9, ["1", "2", "3", "4"], year=2026)


def test_trending_ui_keeps_zero_available_and_filters_client_side():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert 'value="trending_adds"' in source
    assert "Hide unavailable everywhere" in source
    assert "Number(player.available_count || 0) > 0" in source
    assert 'targetReasonFilter?.value === "trending_adds"' in source
    assert '["trend_rank", "Trend Rank"]' in source
