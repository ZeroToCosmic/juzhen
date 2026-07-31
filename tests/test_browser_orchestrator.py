import json

import pytest

from adspower import AdsPowerError
from gateway.browser_orchestrator import ensure_profile_session, public_session_result


PROFILE = {"profile_id": "profile-1", "profile_no": "001", "name": "Test profile"}


class CustomNestedError(Exception):
    pass


class FakeController:
    def __init__(self, *, start_results=None, active_results=None, stop_results=None):
        self.start_results = list(start_results or [])
        self.active_results = list(active_results or [])
        self.stop_results = list(stop_results or [])
        self.started = []
        self.stopped = []
        self.active_checked = []
        self.events = []

    def start_browser(self, profile_id):
        self.started.append(profile_id)
        self.events.append("start")
        result = self.start_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def stop_browser(self, profile_id):
        self.stopped.append(profile_id)
        self.events.append("stop")
        result = self.stop_results.pop(0) if self.stop_results else {"status": "stopped"}
        if isinstance(result, Exception):
            raise result
        return result

    def get_browser_active(self, profile_id):
        self.active_checked.append(profile_id)
        self.events.append("active")
        result = self.active_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_reuses_healthy_current_ws_without_starting_browser():
    controller = FakeController()
    checked = []

    result = ensure_profile_session(
        PROFILE,
        "ws://secret-current",
        controller,
        lambda ws_url: checked.append(ws_url) or True,
        sleep_fn=lambda _seconds: None,
    )

    assert result == {
        "profile_id": "profile-1",
        "profile_no": "001",
        "name": "Test profile",
        "status": "ready",
        "stage": "session_check",
        "attempts": 0,
        "ws_url": "ws://secret-current",
        "error": "",
    }
    assert checked == ["ws://secret-current"]
    assert controller.started == []


def test_restarts_when_current_ws_is_not_ready():
    controller = FakeController(
        start_results=["ws://secret-new"],
        active_results=[AdsPowerError("active check failed")],
        stop_results=[AdsPowerError("stop failed")],
    )
    checked = []

    result = ensure_profile_session(
        PROFILE,
        "ws://secret-old",
        controller,
        lambda ws_url: checked.append(ws_url) or ws_url == "ws://secret-new",
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "ready"
    assert result["stage"] == "session_start"
    assert result["attempts"] == 1
    assert result["ws_url"] == "ws://secret-new"
    assert checked == ["ws://secret-old", "ws://secret-new"]
    assert controller.started == ["profile-1"]
    assert controller.events == ["active", "stop", "start"]


def test_starts_when_current_ws_is_empty():
    controller = FakeController(start_results=["ws://secret-new"])
    checked = []

    result = ensure_profile_session(
        PROFILE,
        "",
        controller,
        lambda ws_url: checked.append(ws_url) or True,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "ready"
    assert result["attempts"] == 1
    assert checked == ["ws://secret-new"]
    assert controller.started == ["profile-1"]


def test_retries_cdp_failure_three_times_and_stops_residual_profile():
    controller = FakeController(
        start_results=["ws://secret-1", "ws://secret-2", "ws://secret-3"],
        active_results=[{"status": "active"}] * 3,
    )
    sleeps = []

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: False,
        sleep_fn=sleeps.append,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "wait_for_cdp"
    assert result["attempts"] == 3
    assert result["ws_url"] == "ws://secret-3"
    assert "ws://" not in result["error"]
    assert controller.active_checked == ["profile-1"] * 3
    assert controller.stopped == ["profile-1", "profile-1"]
    assert sleeps == [2.0, 2.0]


def test_returns_failed_result_when_active_status_lookup_fails():
    controller = FakeController(
        start_results=[AdsPowerError("start failed")] * 3,
        active_results=[AdsPowerError("active lookup failed")] * 3,
    )

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: True,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "start_browser"
    assert result["attempts"] == 3
    assert "active lookup failed" in result["error"]
    assert controller.active_checked == ["profile-1"] * 3
    assert controller.stopped == ["profile-1", "profile-1"]


def test_failed_start_does_not_return_previous_stopped_ws_url():
    controller = FakeController(
        start_results=[
            "ws://stopped-secret",
            AdsPowerError("second start failed"),
            AdsPowerError("third start failed"),
        ],
        active_results=[{"status": "active"}] * 3,
    )

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: False,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "failed"
    assert result["attempts"] == 3
    assert result["ws_url"] == ""


def test_stop_failure_does_not_block_next_start_attempt():
    controller = FakeController(
        start_results=[AdsPowerError("first start failed"), "ws://ready-secret"],
        active_results=[{"status": "active"}],
        stop_results=[AdsPowerError("stop failed")],
    )
    sleeps = []

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: True,
        sleep_fn=sleeps.append,
    )

    assert result["status"] == "ready"
    assert result["attempts"] == 2
    assert controller.started == ["profile-1", "profile-1"]
    assert controller.stopped == ["profile-1"]
    assert sleeps == [2.0]


def test_never_attempts_a_profile_more_than_three_times():
    controller = FakeController(
        start_results=[AdsPowerError("start failed")] * 5,
        active_results=[{"status": "inactive"}] * 5,
    )

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: True,
        retries=5,
        sleep_fn=lambda _seconds: None,
    )

    assert result["attempts"] == 3
    assert controller.started == ["profile-1"] * 3


@pytest.mark.parametrize("retries", [None, "3", 1.5, 0, -1, True])
def test_invalid_retries_returns_structured_validation_failure(retries):
    controller = FakeController()

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: True,
        retries=retries,
        sleep_fn=lambda _seconds: None,
    )

    assert result["status"] == "failed"
    assert result["stage"] == "validation"
    assert result["attempts"] == 0
    assert result["ws_url"] == ""
    assert "retries" in result["error"]
    assert controller.started == []


def test_public_session_result_removes_ws_url_and_redacts_ws_in_errors():
    public = public_session_result(
        {
            "profile_id": "profile-k1dxxctm",
            "ws_url": "ws://secret",
            "error": "CDP unavailable at ws://secret?token=private",
        }
    )

    assert public["profile_id"] == "***xctm"
    assert "ws_url" not in public
    assert "ws://secret" not in public["error"]


def test_public_session_result_recursively_removes_secrets():
    public = public_session_result(
        {
            "profile_id": "profile-1",
            "metadata": {
                "API-Key": "api-key-secret",
                "secret_key": "variant-secret",
                "ws_puppeteer": "raw-ws-secret",
                "nested": [
                    {"access_token": "token-secret"},
                    (
                        {"Authorization": "Bearer bearer-secret"},
                        {"clientSecret": "client-secret"},
                        {"WsUrl": "wss://socket-secret/path"},
                    ),
                ],
            },
            "error": (
                "connect ws://error-secret/path; "
                "fallback wss://secure-error-secret/path; "
                "Authorization: Bearer inline-bearer-secret; "
                "api_key=inline-api-secret"
            ),
        }
    )

    serialized = json.dumps(public, ensure_ascii=False)
    for secret in (
        "api-key-secret",
        "variant-secret",
        "raw-ws-secret",
        "token-secret",
        "bearer-secret",
        "client-secret",
        "socket-secret",
        "error-secret",
        "secure-error-secret",
        "inline-bearer-secret",
        "inline-api-secret",
    ):
        assert secret not in serialized


def test_nested_active_error_is_sanitized_before_string_conversion():
    controller = FakeController(
        start_results=[AdsPowerError("start failed")],
        active_results=[
            {
                "message": {
                    "accessToken": "nested-token-secret",
                    "details": ["wss://nested-socket-secret/path"],
                }
            }
        ],
    )

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: True,
        retries=1,
        sleep_fn=lambda _seconds: None,
    )

    serialized = json.dumps(public_session_result(result), ensure_ascii=False)
    assert "nested-token-secret" not in result["error"]
    assert "nested-socket-secret" not in result["error"]
    assert "nested-token-secret" not in serialized
    assert "nested-socket-secret" not in serialized


@pytest.mark.parametrize("error_type", [Exception, CustomNestedError])
def test_exception_args_are_recursively_sanitized_in_public_result(error_type):
    error = error_type(
        "ordinary failure remains readable",
        {
            "api_key": "exception-api-secret",
            "nested": [
                {"token": "exception-token-secret"},
                {"authorization": "Bearer exception-auth-secret"},
                {"secret": "exception-value-secret"},
                {"ws_url": "exception-ws-key-secret"},
                (
                    "connect ws://exception-ws-secret/path",
                    "Bearer exception-bearer-secret",
                    "api_key=exception-inline-api-secret",
                ),
            ],
        },
    )
    controller = FakeController(
        start_results=[error],
        active_results=[{"status": "inactive"}],
    )

    result = ensure_profile_session(
        PROFILE,
        None,
        controller,
        lambda _ws_url: True,
        retries=1,
        sleep_fn=lambda _seconds: None,
    )

    serialized = json.dumps(public_session_result(result), ensure_ascii=False)
    assert "ordinary failure remains readable" in serialized
    for secret in (
        "exception-api-secret",
        "exception-token-secret",
        "exception-auth-secret",
        "exception-value-secret",
        "exception-ws-key-secret",
        "exception-ws-secret",
        "exception-bearer-secret",
        "exception-inline-api-secret",
    ):
        assert secret not in serialized
