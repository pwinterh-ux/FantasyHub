from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from flask import Flask

from services.mfl_client import MFLClient
from services.waivers_service import (
    classify_acquisition_status,
    classify_waiver_action,
    validate_bbid_amount,
)
import waivers.routes as routes


@pytest.fixture
def app():
    app = Flask(__name__)
    app.config.update(TESTING=True, MFL_APIKEY="key")
    return app


def response(xml=b"<status>OK</status>"):
    result = Mock(status_code=200, content=xml, text=xml.decode(), headers={})
    result.request = Mock(method="POST", url="https://www43.myfantasyleague.com/2026/import")
    return result


@pytest.mark.parametrize("drop,expected", [("14095", "16585_3_14095"), (None, "16585_3_0000")])
def test_bbid_payload_append_and_franchise(app, drop, expected):
    client = MFLClient(2026, "https://www43.myfantasyleague.com/2026/")
    with app.app_context(), patch("services.mfl_client._rl.wait"), patch(
        "services.mfl_client.requests.post", return_value=response()
    ) as post:
        client.submit_blind_bid_waiver(
            "71183", [{"player_id": "16585", "amount": 3, "drop_player_id": drop}],
            "MFL_USER_ID=owner", franchise_id="0004"
        )
    data = post.call_args.kwargs["data"]
    assert data["PICKS"] == expected
    assert data["FRANCHISE_ID"] == "0004"
    assert "REPLACE" not in data
    assert post.call_count == 1
    assert "params" not in post.call_args.kwargs


def test_bbid_request_is_never_retried(app):
    client = MFLClient(2026, "https://www43.myfantasyleague.com/2026/")
    with app.app_context(), patch("services.mfl_client._rl.wait"), patch(
        "services.mfl_client.requests.post", side_effect=__import__("requests").Timeout("lost")
    ) as post:
        with pytest.raises(RuntimeError, match="failed"):
            client.submit_blind_bid_waiver("71183", [{"player_id":"16585", "amount":0}], "cookie", franchise_id="0004")
    assert post.call_count == 1


@pytest.mark.parametrize("status,kind,conditional,expected", [
    ("FREE_AGENT", "FCFS", None, "FCFS"),
    ("FREE_AGENT_LOCKED", "FCFS", None, None),
    ("FREE_AGENT", "BBID", False, "BBID"),
    ("FREE_AGENT_LOCKED", "BBID", False, "BBID"),
    ("FREE_AGENT", "BBID_FCFS", False, "FCFS"),
    ("FREE_AGENT_LOCKED", "BBID_FCFS", False, "BBID"),
    ("FREE_AGENT", "BBID", True, None),
    ("FREE_AGENT_LOCKED", "BBID_FCFS", None, None),
    ("ROSTERED", "BBID", False, None),
    ("FREE_AGENT", None, None, None),
    ("FREE_AGENT", "LEGACY", None, None),
])
def test_waiver_action_classification(status, kind, conditional, expected):
    assert classify_waiver_action(status, kind, conditional) == expected


@pytest.mark.parametrize("status,kind,conditional,expected", [
    ("FREE_AGENT", "FCFS", None, "FA"),
    ("FREE_AGENT_LOCKED", "FCFS", None, "Waiver"),
    ("FREE_AGENT", "BBID", False, "BBID"),
    ("FREE_AGENT_LOCKED", "BBID", False, "BBID"),
    ("FREE_AGENT", "BBID_FCFS", False, "FA"),
    ("FREE_AGENT_LOCKED", "BBID_FCFS", False, "BBID"),
    ("FREE_AGENT", "BBID", True, "Waiver"),
    ("FREE_AGENT_LOCKED", "BBID_FCFS", None, "Waiver"),
    ("ROSTERED", "FCFS", None, "Rostered"),
    ("FREE_AGENT", None, None, "Waiver"),
    ("FREE_AGENT", "LEGACY", None, "Waiver"),
])
def test_acquisition_status_classification(status, kind, conditional, expected):
    assert classify_acquisition_status(status, kind, conditional) == expected


def test_league_45690_plain_fcfs_regression():
    assert classify_acquisition_status("FREE_AGENT", "FCFS", None) == "FA"
    assert classify_waiver_action("FREE_AGENT", "FCFS", None) == "FCFS"


def test_blank_bid_route_becomes_zero(app):
    result = {"ok": True}
    with app.test_request_context(json={"league_id":203, "add_player_id":1, "bid_amount":""}), patch.object(
        routes, "current_user", SimpleNamespace(id=1)
    ), patch.object(routes, "perform_blind_bid_add", return_value=result) as perform:
        reply = routes.api_bbid_add.__wrapped__()
    assert reply.get_json()["ok"] is True
    assert perform.call_args.args[3] == ""


def test_route_validation_and_service_error(app):
    with app.test_request_context(json={}), patch.object(routes, "current_user", SimpleNamespace(id=1)):
        reply, code = routes.api_bbid_add.__wrapped__()
        assert code == 400
    with app.test_request_context(json={"league_id":203, "add_player_id":1, "bid_amount":"bad"}), patch.object(
        routes, "current_user", SimpleNamespace(id=1)
    ), patch.object(routes, "perform_blind_bid_add", side_effect=ValueError("Invalid waiver bid amount.")):
        reply, code = routes.api_bbid_add.__wrapped__()
        assert code == 400
        assert "Invalid" in reply.get_json()["error"]


def test_live_status_cache_keeps_waiver_context():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert "waiver: waiver || {}" in source
    assert "cached.waiver" in source


def test_acquisition_ui_uses_authoritative_status_and_distinct_actions():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert 'const visibleStatus = status.visible_status || "Waiver"' in source
    assert 'quickAction === "FCFS"' in source
    assert 'quickAction === "BBID"' in source
    assert "Bid amount · Available FAAB" in source
    assert "Submit FAAB bid" in source
    assert "Perform FCFS Add" in source
    assert "Waiver claim must be managed in MFL." in source
    assert "Free Agent" not in source
    assert "FA · Locked" not in source


def test_bbid_browser_errors_distinguish_rejection_from_ambiguity():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert "error.definiteRejection = response.status === 400" in source
    assert 'button.dataset.submitting = "0"' in source
    assert "button.disabled = false" in source
    assert "The FAAB bid was not confirmed. Check MFL before retrying." in source
    assert 'button.textContent = "Check MFL"' in source


def test_blank_bid_is_zero_and_faab_is_enforced():
    assert str(validate_bbid_amount("", 1000)) == "0"
    with pytest.raises(ValueError, match="exceeds"):
        validate_bbid_amount("1001", 1000)
    with pytest.raises(ValueError, match="Invalid"):
        validate_bbid_amount("three", 1000)


def test_waiver_confirmation_ui_guards_both_transaction_posts():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert 'id="waiverConfirmMask"' in source
    assert 'title: "Confirm FA Add"' in source
    assert 'button: "Confirm Add"' in source
    assert 'title: "Confirm FAAB Bid"' in source
    assert 'button: "Confirm FAAB Bid"' in source
    assert 'drop: selectedDrop ? selected.textContent.trim() : "No drop"' in source
    assert 'const amount = actionCell.querySelector(".bbid-amount")?.value || "0"' in source
    assert 'confirmSubmit.addEventListener("click"' in source
    assert "submit();" in source
    assert 'onConfirm: () => submitFcfsAdd' in source
    assert 'onConfirm: async () =>' in source
    assert source.count('fetch("/waivers/api/bbid-add"') == 1
    assert source.count('"/waivers/api/fcfs-add"') == 1


def test_default_targets_view_uses_position_rank_sort():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert 'class="waiver-tab is-active"' in source
    assert 'data-waiver-view-tab="targets"' in source
    assert 'data-target-position="ALL"' in source
    assert '<option value="position_rank" selected>' in source
    assert 'targetSort?.value ||\n      "position_rank"' in source
    for option in ("top3", "top5", "position_rank", "unrostered"):
        assert f'<option value="{option}"' in source


def test_deep_link_populates_search_and_preserves_authoritative_flow():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    deep_link = source[source.index("async function loadDeepLinkedPlayer"):]
    assert 'setWaiverView("search")' in deep_link
    assert 'searchInput.value = payload.player.name || ""' in deep_link
    assert "renderPlayers([{" in deep_link
    assert "if (card) await togglePlayer(card)" in deep_link
    assert 'String(CONFIG.deepLink?.league_id)' in source
    assert 'class="${String(CONFIG.deepLink?.league_id) === String(league.league_id) ? "deep-link-league" : ""}"' in source
    assert 'method: "POST"' not in deep_link


def test_confirmation_dialog_is_compact_and_conditionally_hides_bid():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert 'width: min(380px, calc(100vw - 32px))' in source
    assert 'grid-template-columns: 64px 1fr' in source
    assert '.waiver-confirm-review > div[hidden] { display: none; }' in source
    assert '<dt>Add</dt>' in source
    assert '<dt>Drop</dt>' in source
    assert '<dt>League</dt>' in source
    assert '<dt>Bid</dt>' in source
    assert 'id="waiverConfirmAction"' not in source
    assert 'bidRow.hidden = options.bid === undefined' in source
    assert 'document.getElementById("waiverConfirmTitle").textContent = options.title' in source


def test_my_leagues_best_available_links_to_real_waivers_flow():
    source = open("templates/my_leagues.html", encoding="utf-8").read()
    assert 'data-league-row="{{ league.key }}"' in source
    assert 'data-db-id="{{ league.db_id }}"' in source
    assert "const leagueKey = rowEl.getAttribute('data-league-row')" in source
    assert "const leagueDbId = rowEl.getAttribute('data-db-id')" in source
    assert 'data-for-league="${leagueKey}"' in source
    assert "leagueDetailsCache.set(String(leagueKey)" in source
    assert "renderBestAvailable(bestAvailable, leagueDbId, data.league?.platform)" in source
    assert "renderBestAvailable(bestAvailable, leagueKey" not in source
    assert "url_for('waivers.index')" in source
    assert "?player_id=${encodeURIComponent(player.player_id)}&league_id=${encodeURIComponent(leagueDbId)}" in source
    assert "renderBestAvailable(bestAvailable, leagueId" not in source
    assert "Demo flow only" not in source
    assert "js-best-available-add" not in source
    assert "/waivers/api/fcfs-add" not in source
    assert "/waivers/api/bbid-add" not in source


def test_tools_hub_activates_mfl_only_waivers():
    source = open("templates/tools/index.html", encoding="utf-8").read()
    assert "href=\"{{ url_for('waivers.index') }}\"" in source
    assert 'data-tool="Waivers"' in source
    assert 'data-mfl-only="1"' in source
    assert "Waivers (coming soon)" not in source
    assert "Find dynasty targets across your leagues" in source


def test_waivers_deep_link_and_sync_notice_are_safe_server_inputs(app):
    ready = SimpleNamespace(id=7, waiver_type="FCFS")
    legacy = SimpleNamespace(id=8, waiver_type=None)
    owned = SimpleNamespace(id=7)
    player = SimpleNamespace(id=44)
    league_query = Mock()
    league_query.filter_by.side_effect = [
        Mock(all=Mock(return_value=[ready, legacy])),
        Mock(first=Mock(return_value=owned)),
    ]
    player_query = Mock()
    player_query.filter_by.return_value.first.return_value = player

    with app.test_request_context("/?player_id=44&league_id=7"), patch.object(
        routes, "current_user", SimpleNamespace(id=1)
    ), patch.object(routes.League, "query", league_query), patch.object(
        routes.Player, "query", player_query
    ), patch.object(routes, "render_template", return_value="rendered") as render:
        assert routes.index.__wrapped__() == "rendered"

    context = render.call_args.kwargs
    assert context["needs_waiver_sync"] is True
    assert context["waiver_deep_link"] == {"player_id": 44, "league_id": 7}
    assert league_query.filter_by.call_args_list[1].kwargs["user_id"] == 1


def test_invalid_or_unowned_deep_link_falls_back(app):
    league_query = Mock()
    league_query.filter_by.side_effect = [
        Mock(all=Mock(return_value=[])),
        Mock(first=Mock(return_value=None)),
    ]
    player_query = Mock()
    player_query.filter_by.return_value.first.return_value = SimpleNamespace(id=44)
    with app.test_request_context("/?player_id=44&league_id=999"), patch.object(
        routes, "current_user", SimpleNamespace(id=1)
    ), patch.object(routes.League, "query", league_query), patch.object(
        routes.Player, "query", player_query
    ), patch.object(routes, "render_template", return_value="rendered") as render:
        routes.index.__wrapped__()
    assert render.call_args.kwargs["waiver_deep_link"] is None


def test_sync_notice_copy_and_deep_link_do_not_write():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert "Some leagues need a one-time league sync before RosterDash can determine their waiver rules." in source
    assert "In Add/Delete Leagues, select those leagues and choose Sync selected." in source
    assert "if (CONFIG.deepLink)" in source
    assert "loadDeepLinkedPlayer" in source
