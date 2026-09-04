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
    assert "Submit waiver bid" in source
    assert "Perform FCFS Add" in source
    assert "Waiver claim must be managed in MFL." in source
    assert "Free Agent" not in source
    assert "FA · Locked" not in source


def test_bbid_browser_errors_distinguish_rejection_from_ambiguity():
    source = open("templates/waivers/index.html", encoding="utf-8").read()
    assert "error.definiteRejection = response.status === 400" in source
    assert 'button.dataset.submitting = "0"' in source
    assert "button.disabled = false" in source
    assert "The waiver bid was not confirmed. Check MFL before retrying." in source
    assert 'button.textContent = "Check MFL"' in source


def test_blank_bid_is_zero_and_faab_is_enforced():
    assert str(validate_bbid_amount("", 1000)) == "0"
    with pytest.raises(ValueError, match="exceeds"):
        validate_bbid_amount("1001", 1000)
    with pytest.raises(ValueError, match="Invalid"):
        validate_bbid_amount("three", 1000)
