from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from flask import Flask

from execution_v2.blueprint import (
    BrowserV2ConflictError,
    BrowserV2NotFoundError,
    BrowserV2UnavailableError,
    BrowserV2ValidationError,
    create_browser_v2_blueprint,
)
from execution_v2.picker import PickerError
from execution_v2.wheel_calibration import WheelCalibrationError


@dataclass
class FakeService:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(default_factory=list)
    error: Exception | None = None

    def _call(self, operation: str, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((operation, args, kwargs))
        if self.error is not None:
            raise self.error
        if operation in {"get_element", "get_strategy", "get_job", "get_picker", "get_results"} and args[0] == "missing":
            return None
        return {
            "operation": operation,
            "profile_id": "raw-profile-id",
            "nested": {
                "token": "should-not-leak",
                "cookies": ["also-not-leak"],
                "endpoint": "wss://secret.example.invalid/devtools",
                "safe": "kept",
            },
        }

    def list_elements(self): return self._call("list_elements")
    def create_element(self, **kwargs): return self._call("create_element", **kwargs)
    def get_element(self, element_id): return self._call("get_element", element_id)
    def update_element(self, element_id, **kwargs): return self._call("update_element", element_id, **kwargs)
    def delete_element(self, element_id, **kwargs): return self._call("delete_element", element_id, **kwargs)
    def validate_element(self, element_id, **kwargs): return self._call("validate_element", element_id, **kwargs)
    def list_profiles(self):
        self._call("list_profiles")
        return [{
            "profile_token": "profile_opaque_1",
            "label": "Profile 1",
            "raw_profile_id": "raw-profile-id",
        }]
    def list_content_libraries(self):
        self._call("list_content_libraries")
        return [{"id": "ofs", "name": "OFS", "copy_count": 40}]
    def start_picker(self, **kwargs): return self._call("start_picker", **kwargs)
    def get_picker(self, session_id): return self._call("get_picker", session_id)
    def save_picker_selection(self, session_id, **kwargs): return self._call("save_picker_selection", session_id, **kwargs)
    def finish_picker(self, session_id): return self._call("finish_picker", session_id)
    def cancel_picker(self, session_id): return self._call("cancel_picker", session_id)
    def get_wheel_calibration(self): return self._call("get_wheel_calibration")
    def start_wheel_calibration(self, **kwargs): return self._call("start_wheel_calibration", **kwargs)
    def cancel_wheel_calibration(self): return self._call("cancel_wheel_calibration")
    def list_strategies(self): return self._call("list_strategies")
    def create_strategy(self, **kwargs): return self._call("create_strategy", **kwargs)
    def get_strategy(self, strategy_id): return self._call("get_strategy", strategy_id)
    def update_strategy(self, strategy_id, **kwargs): return self._call("update_strategy", strategy_id, **kwargs)
    def delete_strategy(self, strategy_id, **kwargs): return self._call("delete_strategy", strategy_id, **kwargs)
    def start_job(self, **kwargs): return self._call("start_job", **kwargs)
    def get_job(self, job_id): return self._call("get_job", job_id)
    def cancel_job(self, job_id): return self._call("cancel_job", job_id)
    def get_results(self, job_id): return self._call("get_results", job_id)
    def history(self, **kwargs): return self._call("history", **kwargs)


@pytest.fixture
def service() -> FakeService:
    return FakeService()


@pytest.fixture
def client(service: FakeService):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(create_browser_v2_blueprint(lambda: service))
    return app.test_client()


ELEMENT = {
    "id": "comment_button",
    "name": "Comment button",
    "purpose": "action",
    "kind": "click",
    "definition": {"any": "definition-is-service-validated"},
}
STRATEGY = {"id": "comment", "name": "Comment", "definition": {"any": "definition"}}


@pytest.mark.parametrize(
    ("method", "path", "payload", "status", "operation"),
    [
        ("get", "/api/browser-v2/elements", None, 200, "list_elements"),
        ("post", "/api/browser-v2/elements", ELEMENT, 201, "create_element"),
        ("get", "/api/browser-v2/elements/one", None, 200, "get_element"),
        ("put", "/api/browser-v2/elements/one", {"expected_revision": 1, "name": "New"}, 200, "update_element"),
        ("delete", "/api/browser-v2/elements/one", {"expected_revision": 1}, 200, "delete_element"),
        ("post", "/api/browser-v2/elements/one/validate", {"profile_token": "profile_opaque_1"}, 200, "validate_element"),
        ("get", "/api/browser-v2/profiles", None, 200, "list_profiles"),
        ("get", "/api/browser-v2/content-libraries", None, 200, "list_content_libraries"),
        ("post", "/api/browser-v2/picker/start", {"profile_token": "profile_opaque_1", "target_url": "https://www.tiktok.com/"}, 202, "start_picker"),
        ("get", "/api/browser-v2/picker/pick", None, 200, "get_picker"),
        ("post", "/api/browser-v2/picker/pick/save", {"name": "Comment", "purpose": "action", "kind": "click"}, 201, "save_picker_selection"),
        ("post", "/api/browser-v2/picker/pick/finish", {}, 200, "finish_picker"),
        ("post", "/api/browser-v2/picker/pick/cancel", {}, 202, "cancel_picker"),
        ("get", "/api/browser-v2/wheel-calibration", None, 200, "get_wheel_calibration"),
        ("post", "/api/browser-v2/wheel-calibration/start", {"profile_token": "profile_opaque_1", "target_url": "https://www.tiktok.com/"}, 202, "start_wheel_calibration"),
        ("post", "/api/browser-v2/wheel-calibration/cancel", {}, 202, "cancel_wheel_calibration"),
        ("get", "/api/browser-v2/strategies", None, 200, "list_strategies"),
        ("post", "/api/browser-v2/strategies", STRATEGY, 201, "create_strategy"),
        ("get", "/api/browser-v2/strategies/one", None, 200, "get_strategy"),
        ("put", "/api/browser-v2/strategies/one", {"expected_revision": 1, "name": "New"}, 200, "update_strategy"),
        ("delete", "/api/browser-v2/strategies/one", {"expected_revision": 1}, 200, "delete_strategy"),
        ("post", "/api/browser-v2/jobs", {"strategy_id": "one", "profile_tokens": ["profile_opaque_1"], "batch_size": 3}, 202, "start_job"),
        ("get", "/api/browser-v2/jobs/one", None, 200, "get_job"),
        ("post", "/api/browser-v2/jobs/one/cancel", {}, 202, "cancel_job"),
        ("get", "/api/browser-v2/jobs/one/results", None, 200, "get_results"),
        ("get", "/api/browser-v2/history", None, 200, "history"),
    ],
)
def test_all_routes_call_service(client, service, method, path, payload, status, operation):
    response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)

    assert response.status_code == status
    if operation in {"delete_element", "delete_strategy"}:
        assert response.get_json()["data"] == {}
    elif operation == "list_profiles":
        assert response.get_json()["data"][0]["profile_token"] == "profile_opaque_1"
    elif operation == "list_content_libraries":
        assert response.get_json()["data"] == [
            {"id": "ofs", "name": "OFS", "copy_count": 40}
        ]
    else:
        assert response.get_json()["data"]["operation"] == operation
    assert service.calls[-1][0] == operation


def test_history_has_closed_query_schema_and_defaults(client, service):
    response = client.get("/api/browser-v2/history?limit=10&offset=3")

    assert response.status_code == 200
    assert service.calls[-1] == ("history", (), {"limit": 10, "offset": 3})
    invalid = client.get("/api/browser-v2/history?leak=1")
    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/browser-v2/elements", []),
        ("post", "/api/browser-v2/elements", {"id": "one", "extra": True}),
        ("put", "/api/browser-v2/elements/one", {"expected_revision": 0, "name": "name"}),
        ("post", "/api/browser-v2/picker/start", {"profile_token": "one", "target_url": "https://x", "extra": 1}),
        ("post", "/api/browser-v2/picker/pick/finish", {"extra": 1}),
        ("post", "/api/browser-v2/wheel-calibration/start", {"profile_token": "one", "target_url": "https://x", "raw_id": "secret"}),
        ("post", "/api/browser-v2/wheel-calibration/cancel", {"extra": 1}),
        ("post", "/api/browser-v2/jobs", {"strategy_id": "one", "profile_tokens": ["one", "one"]}),
        ("post", "/api/browser-v2/jobs", {"strategy_id": "one", "profile_tokens": ["one"], "batch_size": 9}),
    ],
)
def test_json_body_must_be_closed_object(client, method, path, payload):
    response = getattr(client, method)(path, json=payload)

    assert response.status_code == 400
    assert response.get_json() == {"error": {"code": "invalid_request", "message": "请求格式无效。"}}


def test_missing_json_is_not_accepted(client):
    response = client.post("/api/browser-v2/picker/pick/cancel")

    assert response.status_code == 400


def test_missing_resource_returns_safe_404(client):
    response = client.get("/api/browser-v2/elements/missing")

    assert response.status_code == 404
    assert response.get_json()["error"] == {"code": "not_found", "message": "请求资源不存在。"}


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (BrowserV2ConflictError("secret ws://private"), 409, "conflict"),
        (BrowserV2ValidationError("cookie=private"), 422, "validation_failed"),
        (BrowserV2UnavailableError("password=private"), 503, "runtime_unavailable"),
    ],
)
def test_service_errors_are_mapped_without_exception_echo(client, service, error, status, code):
    service.error = error
    response = client.get("/api/browser-v2/elements")

    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    assert "private" not in response.get_data(as_text=True)


def test_input_picker_error_has_safe_specific_message(client, service):
    service.error = PickerError("picker_input_target_not_editable")

    response = client.post(
        "/api/browser-v2/picker/pick/save",
        json={"name": "评论输入", "purpose": "action", "kind": "input"},
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "error": {
            "code": "input_target_not_editable",
            "message": "未能定位唯一可编辑输入框，请点选输入文字区域后重试。",
        }
    }


def test_wheel_replay_miss_has_safe_specific_message(client, service):
    service.error = WheelCalibrationError("wheel_calibration_replay_not_observed")

    response = client.post(
        "/api/browser-v2/wheel-calibration/start",
        json={"profile_token": "one", "target_url": "https://www.tiktok.com/"},
    )

    assert response.status_code == 422
    assert response.get_json()["error"] == {
        "code": "wheel_calibration_replay_not_observed",
        "message": "自动回放未能切换视频，请重新校准。",
    }


def test_recursive_response_redaction(client):
    response = client.get("/api/browser-v2/elements")
    body = response.get_json()["data"]

    assert "profile_id" not in body
    assert body["nested"]["token"] == "[redacted]"
    assert body["nested"]["cookies"] == "[redacted]"
    assert body["nested"]["endpoint"] == "[redacted]"
    assert body["nested"]["safe"] == "kept"
    encoded = response.get_data(as_text=True)
    assert "raw-profile-id" not in encoded
    assert "secret.example.invalid" not in encoded


def test_profile_token_is_public_handle_for_picker_and_jobs(client, service):
    profiles = client.get("/api/browser-v2/profiles")

    assert profiles.status_code == 200
    profile = profiles.get_json()["data"][0]
    assert profile["profile_token"] == "profile_opaque_1"
    assert "raw_profile_id" not in profile
    assert "raw-profile-id" not in profiles.get_data(as_text=True)

    picker = client.post(
        "/api/browser-v2/picker/start",
        json={"profile_token": profile["profile_token"], "target_url": "https://www.tiktok.com/"},
    )
    job = client.post(
        "/api/browser-v2/jobs",
        json={"strategy_id": "one", "profile_tokens": [profile["profile_token"]]},
    )

    assert picker.status_code == 202
    assert job.status_code == 202
    assert service.calls[-2] == (
        "start_picker", (), {"profile_token": "profile_opaque_1", "target_url": "https://www.tiktok.com/"}
    )
    assert service.calls[-1] == (
        "start_job", (), {"strategy_id": "one", "profile_tokens": ["profile_opaque_1"]}
    )


def test_picker_save_supports_create_and_repick_contracts(client, service):
    create_body = {"name": "Comment", "purpose": "action", "kind": "click"}
    repick_body = {
        **create_body,
        "element_id": "comment_button",
        "expected_revision": 4,
    }

    created = client.post("/api/browser-v2/picker/pick/save", json=create_body)
    repicked = client.post("/api/browser-v2/picker/pick/save", json=repick_body)

    assert created.status_code == 201
    assert repicked.status_code == 201
    assert service.calls[-2] == ("save_picker_selection", ("pick",), create_body)
    assert service.calls[-1] == ("save_picker_selection", ("pick",), repick_body)


@pytest.mark.parametrize(
    "extra",
    [
        {"element_id": "comment_button"},
        {"expected_revision": 1},
        {"element_id": "", "expected_revision": 1},
        {"element_id": "comment_button", "expected_revision": 0},
    ],
)
def test_picker_save_repick_fields_must_be_valid_pair(client, extra):
    body = {"name": "Comment", "purpose": "action", "kind": "click", **extra}

    response = client.post("/api/browser-v2/picker/pick/save", json=body)

    assert response.status_code == 400


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/api/browser-v2/picker/start", {"profile_id": "raw", "target_url": "https://www.tiktok.com/"}),
        ("/api/browser-v2/elements/one/validate", {"profile_id": "raw"}),
        ("/api/browser-v2/jobs", {"strategy_id": "one", "profile_ids": ["raw"]}),
    ],
)
def test_raw_profile_identity_fields_are_rejected(client, path, payload):
    response = client.post(path, json=payload)

    assert response.status_code == 400


def test_service_instance_is_supported():
    service = FakeService()
    app = Flask(__name__)
    app.register_blueprint(create_browser_v2_blueprint(service))

    response = app.test_client().get("/api/browser-v2/profiles")

    assert response.status_code == 200
    assert service.calls[-1][0] == "list_profiles"
