from __future__ import annotations

from datetime import UTC, datetime, time
import json
from types import SimpleNamespace

from flask import Flask
import pytest
from werkzeug.security import generate_password_hash

from gateway.app import create_app
from gateway.auth_blueprint import install_management_guard
from gateway.auth_store import AuthStore
from gateway.management_db import open_management_db
from selector_probe.catalog import ElementCatalog
from selector_probe.blueprint import (
    _profile_ref,
    create_selector_probe_blueprint,
    default_gate_service_factory,
)
from selector_probe.picker import PickerError
from selector_probe.store import SelectorProbeStore


ACTIVE_BUNDLE = {
    "version": "sel-1",
    "bundle_hash": "sha256:" + "a" * 64,
    "elements": {
        "comment_entry": {
            "scope": "active_video",
            "locators": [
                {
                    "id": "loc-1",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-icon",
                    "enabled": True,
                }
            ],
        }
    },
}


class FakeStore:
    def __init__(self):
        self.calls = []
        self.runs = [
            {
                "id": 7,
                "scheduled_for": "2026-07-29T03:00:00+08:00",
                "started_at": "2026-07-29T03:00:01+08:00",
                "finished_at": "2026-07-29T03:01:00+08:00",
                "status": "completed",
                "active_version_before": "sel-0",
                "published_version_after": "sel-1",
                "failed_aliases": [],
                "details": {
                    "profiles_observed": 2,
                    "raw_snapshot": {"profile_id": "profile-a"},
                    "cdp_url": "ws://secret",
                },
            }
        ]
        self.versions = [
            {
                "id": "sel-1",
                "site": "tiktok",
                "environment": "production",
                "status": "published",
                "base_version_id": "sel-0",
                "bundle_hash": "sha256:" + "a" * 64,
                "created_at": "2026-07-29T03:00:30+08:00",
                "validated_at": "2026-07-29T03:00:40+08:00",
                "published_at": "2026-07-29T03:00:50+08:00",
                "bundle": ACTIVE_BUNDLE,
                "evidence": {
                    "profile_id": "profile-a",
                    "raw_snapshot": {"nodes": []},
                    "private_token": "secret",
                },
            }
        ]
        self.overview = {
            "health": {
                "status": "healthy",
                "failure_started_at": "",
                "retry_count": 0,
                "next_retry_at": "",
                "last_validated_at": "2026-07-29T03:01:00+08:00",
                "raw_snapshot": {"secret": True},
            },
            "current_version": {
                "id": "sel-1",
                "published_at": "2026-07-29T03:00:50+08:00",
            },
            "last_successful": self.runs[0],
            "element_counts": {"all": 6, "healthy": 4, "failed": 2},
            "priority_elements": [
                {
                    "id": f"element-{index}",
                    "display_name": f"Element {index}",
                    "management_source": "automatic",
                    "published_status": "failed",
                    "draft_status": None,
                    "scope": "active_video",
                    "primary_locator_type": "attribute",
                    "last_validated_at": None,
                    "revision": index,
                    "dependency_count": 0,
                    "raw_dom": "secret",
                }
                for index in range(1, 7)
            ],
            "gate_counts": {"automatic": 2, "manual": 1},
            "alert_summary": {
                "open": 1,
                "acknowledged": 1,
                "resolved": 2,
                "active": 2,
                "latest": {
                    "id": 9,
                    "status": "open",
                    "failure_class": "selector_validation_failed",
                    "last_seen_at": "2026-07-29T03:02:00+08:00",
                    "occurrence_count": 2,
                    "screenshot_path": "C:/secret/failure.jpg",
                },
            },
            "webhook_status": {
                "status": "completed",
                "event_type": "alert_opened",
                "attempt_count": 1,
                "created_at": "2026-07-29T03:02:00+08:00",
                "completed_at": "2026-07-29T03:02:01+08:00",
                "payload_json": '{"secret": true}',
            },
            "recent_events": [
                {
                    "event_type": "element_validate_completed",
                    "target_type": "element",
                    "target_id": "element-1",
                    "result": "succeeded",
                    "created_at": "2026-07-29T03:02:00+08:00",
                    "details_json": '{"secret": true}',
                }
            ],
            "revision": 12,
        }

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def list_runs(self, *, limit, offset):
        self.calls.append(("runs", limit, offset))
        return self.runs[offset : offset + limit]

    def list_versions(self, *, limit, offset):
        self.calls.append(("versions", limit, offset))
        return self.versions[offset : offset + limit]

    def selector_probe_overview(self, *, site, environment):
        assert (site, environment) == ("tiktok", "production")
        return self.overview


class FakeRegistry:
    def __init__(self, active=None, error=None):
        self.active = ACTIVE_BUNDLE if active is None else active
        self.error = error

    def get_active(self):
        if self.error is not None:
            raise self.error
        return self.active


class FakeGateService:
    def __init__(self):
        self.store = self
        self.calls = []
        self.decisions = {
            "strategy-a": {
                "strategy_id": "strategy-a",
                "allowed": False,
                "effective_status": "paused",
                "reasons": [
                    {
                        "source": "probe",
                        "reason_code": "selector_validation_failed",
                        "aliases": ["comment_entry"],
                        "selector_version_id": "sel-2",
                        "created_at": "2026-07-29T03:00:00+08:00",
                    }
                ],
            }
        }

    def managed_strategy_ids(self):
        return tuple(sorted(self.decisions))

    def check(self, strategy_id):
        return self.decisions.get(
            strategy_id,
            {
                "strategy_id": strategy_id,
                "allowed": True,
                "effective_status": "active",
                "reasons": [],
            },
        )

    def set_manual_pause(self, strategy_id, paused, actor):
        self.calls.append((strategy_id, paused, actor))
        current = self.check(strategy_id)
        reasons = [
            item
            for item in current["reasons"]
            if item["source"] != "manual"
        ]
        if paused:
            reasons.insert(
                0,
                {
                    "source": "manual",
                    "reason_code": "operator_pause",
                    "aliases": [],
                    "selector_version_id": "",
                    "created_at": "2026-07-29T04:00:00+08:00",
                },
            )
        decision = {
            "strategy_id": strategy_id,
            "allowed": not reasons,
            "effective_status": "active" if not reasons else "paused",
            "reasons": reasons,
        }
        self.decisions[strategy_id] = decision
        return decision


def make_client(*, store=None, registry=None, dispatcher=None, gate_service=None):
    app = Flask(__name__)
    selected_store = store or FakeStore()
    selected_registry = registry or FakeRegistry()
    app.register_blueprint(
        create_selector_probe_blueprint(
            store_factory=lambda: selected_store,
            registry_factory=lambda: selected_registry,
            run_dispatcher=dispatcher or (lambda _request_id, _done: True),
            gate_service_factory=lambda: gate_service or FakeGateService(),
            status_config_provider=lambda: SimpleNamespace(
                site="tiktok",
                environment="production",
                timezone="Asia/Shanghai",
                daily_time=time(3, 0),
                webhook=SimpleNamespace(enabled=True),
            ),
            utcnow_fn=lambda: datetime(2026, 7, 29, 0, 0, tzinfo=UTC),
        )
    )
    return app.test_client(), selected_store


class FakePickerRouteService:
    def __init__(self, *, owner_user_id=7, failure=None):
        self.owner_user_id = owner_user_id
        self.failure = failure
        self.calls = []

    def _check(self, actor_user_id):
        if self.failure is not None:
            raise self.failure
        if actor_user_id != self.owner_user_id:
            raise PickerError("picker_not_found", 404)

    def start(self, **kwargs):
        self._check(kwargs["actor_user_id"])
        self.calls.append(("start", kwargs))
        return {"session_id": "picker-1", "status": "starting", "revision": 1}

    def get(self, session_id, **kwargs):
        self._check(kwargs["actor_user_id"])
        self.calls.append(("get", session_id, kwargs))
        return {"session_id": session_id, "status": "ready", "revision": 2}

    def confirm(self, session_id, **kwargs):
        self._check(kwargs["actor_user_id"])
        if kwargs["expected_revision"] != 2:
            raise PickerError("stale_picker_revision", 409)
        self.calls.append(("confirm", session_id, kwargs))
        return {"session_id": session_id, "status": "confirmed", "revision": 3}

    def cancel(self, session_id, **kwargs):
        self._check(kwargs["actor_user_id"])
        if kwargs["expected_revision"] != 2:
            raise PickerError("stale_picker_revision", 409)
        self.calls.append(("cancel", session_id, kwargs))
        return {"session_id": session_id, "status": "cancelled", "revision": 3}


def _picker_route_client(*, role="administrator", actor_user_id=7, service=None):
    selected_service = service or FakePickerRouteService(
        owner_user_id=actor_user_id
    )
    user = SimpleNamespace(
        id=actor_user_id,
        username=role,
        role=role,
        must_change_password=False,
    )

    class AuthService:
        def validate_session(self, _session, _now):
            return user

    settings = {
        "selector_probe": {
            "test_profile_ids": ["profile-secret"],
            "dedicated_test_profile_ids": ["profile-secret"],
        }
    }
    app = Flask(__name__)
    app.secret_key = "picker-route-test"
    app.register_blueprint(
        create_selector_probe_blueprint(
            picker_service_factory=lambda: selected_service,
            settings_provider=lambda: settings,
        )
    )
    install_management_guard(app, lambda: AuthService())
    with app.app_context():
        profile_ref = _profile_ref("profile-secret")
    client = app.test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = "picker-csrf"
    return client, "picker-csrf", profile_ref, selected_service


def test_picker_routes_accept_named_selections_without_trimming_names():
    client, csrf, profile_ref, service = _picker_route_client()

    started = client.post(
        "/api/selector-probe/picker/start",
        json={"profile_ref": profile_ref, "page_state": "feed_ready"},
        headers={"X-CSRF-Token": csrf},
    )
    status = client.get("/api/selector-probe/picker/picker-1")
    confirmed = client.post(
        "/api/selector-probe/picker/picker-1/confirm",
        json={
            "expected_revision": 2,
            "selections": [
                {
                    "selection_id": "selection-1",
                    "display_name": "  评论入口  ",
                }
            ],
        },
        headers={"X-CSRF-Token": csrf},
    )
    cancelled = client.post(
        "/api/selector-probe/picker/picker-1/cancel",
        json={"expected_revision": 2},
        headers={"X-CSRF-Token": csrf},
    )

    assert [started.status_code, status.status_code] == [202, 200]
    assert [confirmed.status_code, cancelled.status_code] == [200, 200]
    assert service.calls[0][1]["profile_id"] == "profile-secret"
    confirm_call = next(item for item in service.calls if item[0] == "confirm")
    assert confirm_call[2]["selections"] == [
        {"selection_id": "selection-1", "display_name": "  评论入口  "}
    ]
    assert "profile-secret" not in started.get_data(as_text=True)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (
            {
                "expected_revision": 2,
                "selections": [
                    {
                        "selection_id": "selection-1",
                        "display_name": "评论入口",
                        "unknown": True,
                    }
                ],
            },
            "invalid_request",
        ),
        (
            {
                "expected_revision": 2,
                "selections": [],
                "unknown": True,
            },
            "invalid_request",
        ),
        (
            {"expected_revision": True, "selections": []},
            "invalid_expected_revision",
        ),
    ],
)
def test_picker_confirm_rejects_unknown_keys_and_invalid_revision(
    payload, expected_code
):
    client, csrf, _profile_ref_value, service = _picker_route_client()

    response = client.post(
        "/api/selector-probe/picker/picker-1/confirm",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 400
    assert response.get_json() == {"code": expected_code}
    assert service.calls == []


def test_picker_routes_are_administrator_only():
    client, csrf, profile_ref, service = _picker_route_client(role="operator")
    requests = (
        client.post(
            "/api/selector-probe/picker/start",
            json={"profile_ref": profile_ref, "page_state": "feed_ready"},
            headers={"X-CSRF-Token": csrf},
        ),
        client.get("/api/selector-probe/picker/picker-1"),
        client.post(
            "/api/selector-probe/picker/picker-1/confirm",
            json={"expected_revision": 2, "selections": []},
            headers={"X-CSRF-Token": csrf},
        ),
        client.post(
            "/api/selector-probe/picker/picker-1/cancel",
            json={"expected_revision": 2},
            headers={"X-CSRF-Token": csrf},
        ),
    )

    assert [response.status_code for response in requests] == [403] * 4
    assert all(response.get_json() == {"code": "forbidden"} for response in requests)
    assert service.calls == []


def test_picker_routes_hide_another_owners_session_and_report_stale_revision():
    service = FakePickerRouteService(owner_user_id=7)
    other, csrf, _profile_ref_value, _service = _picker_route_client(
        actor_user_id=8,
        service=service,
    )
    wrong_owner = (
        other.get("/api/selector-probe/picker/picker-1"),
        other.post(
            "/api/selector-probe/picker/picker-1/confirm",
            json={"expected_revision": 2, "selections": []},
            headers={"X-CSRF-Token": csrf},
        ),
        other.post(
            "/api/selector-probe/picker/picker-1/cancel",
            json={"expected_revision": 2},
            headers={"X-CSRF-Token": csrf},
        ),
    )
    assert [response.status_code for response in wrong_owner] == [404] * 3

    owner, owner_csrf, _profile_ref_value, _service = _picker_route_client(
        service=service
    )
    stale_confirm = owner.post(
        "/api/selector-probe/picker/picker-1/confirm",
        json={"expected_revision": 1, "selections": []},
        headers={"X-CSRF-Token": owner_csrf},
    )
    stale_cancel = owner.post(
        "/api/selector-probe/picker/picker-1/cancel",
        json={"expected_revision": 1},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert [stale_confirm.status_code, stale_cancel.status_code] == [409, 409]
    assert stale_confirm.get_json() == {"code": "stale_picker_revision"}


def test_picker_route_sanitizes_unknown_service_failure():
    service = FakePickerRouteService(failure=RuntimeError("cdp secret"))
    client, _csrf, _profile_ref_value, _service = _picker_route_client(
        service=service
    )

    response = client.get("/api/selector-probe/picker/picker-1")

    assert response.status_code == 503
    assert response.get_json() == {"code": "picker_unavailable"}
    assert "secret" not in response.get_data(as_text=True)


def _element_payload(**changes):
    payload = {
        "display_name": "Share entry",
        "page_key": "tiktok.feed",
        "target_origin": "https://www.tiktok.com",
        "url_pattern": "https://www.tiktok.com/*",
        "operation_steps": [],
        "fingerprint": {
            "tag": "button",
            "role": "button",
            "name": "Share",
        },
        "locators": [
            {"type": "css", "value": '[data-e2e="share-icon"]'},
            {"type": "xpath", "value": '//*[@aria-label="Share"]'},
        ],
    }
    payload.update(changes)
    return payload


def _authenticated_element_client(
    tmp_path,
    role,
    *,
    registry=None,
):
    state_dir = tmp_path / f"{role}-management"
    management_path = state_dir / "management.db"
    selector_path = state_dir / "selector-probe.db"
    connection = open_management_db(management_path)
    try:
        AuthStore(connection).create_user(
            role,
            generate_password_hash("valid password 123", method="scrypt"),
            role,
            must_change_password=False,
        )
    finally:
        connection.close()
    config = {
        "TESTING": True,
        "MANAGEMENT_STATE_DIR": state_dir,
        "MANAGEMENT_DB_PATH": management_path,
        "SELECTOR_PROBE_STORE_FACTORY": (
            lambda: SelectorProbeStore(selector_path)
        ),
        "SELECTOR_PROBE_REGISTRY_FACTORY": (
            lambda: registry or FakeRegistry()
        ),
    }
    app = create_app(config)
    client = app.test_client()
    assert client.get("/login").status_code == 200
    with client.session_transaction() as values:
        csrf_token = values["csrf_token"]
    login = client.post(
        "/api/auth/login",
        json={
            "username": role,
            "password": "valid password 123",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert login.status_code == 200
    return (
        client,
        login.get_json()["csrf_token"],
        selector_path,
        [],
    )


def test_active_returns_complete_published_bundle_and_drops_hostile_fields():
    hostile = {
        **ACTIVE_BUNDLE,
        "draft": {"version": "draft-secret"},
        "profile_id": "profile-a",
        "raw_snapshot": {"nodes": [{"cdp_url": "ws://secret"}]},
        "webhook": {"signing_secret": "webhook-secret"},
        "private_token": "private-secret",
    }
    client, _store = make_client(registry=FakeRegistry(active=hostile))

    response = client.get("/api/selector-probe/active")

    assert response.status_code == 200
    assert response.get_json() == ACTIVE_BUNDLE
    body = response.get_data(as_text=True)
    for forbidden in (
        "draft-secret",
        "profile-a",
        "ws://secret",
        "webhook-secret",
        "private-secret",
        "raw_snapshot",
    ):
        assert forbidden not in body

def test_active_returns_503_when_registry_empty_or_unavailable():
    empty, _store = make_client(registry=FakeRegistry(active=False))
    unavailable, _store = make_client(
        registry=FakeRegistry(error=RuntimeError("redis://secret"))
    )

    for client in (empty, unavailable):
        response = client.get("/api/selector-probe/active")
        assert response.status_code == 503
        assert response.get_json() == {"error": "registry_unavailable"}
        assert "secret" not in response.get_data(as_text=True)


def test_history_pagination_defaults_to_50_and_caps_at_200():
    client, store = make_client()

    first = client.get("/api/selector-probe/runs")
    second = client.get("/api/selector-probe/versions?limit=999&offset=0")

    assert first.status_code == 200
    assert second.status_code == 200
    assert store.calls == [("runs", 50, 0), ("versions", 200, 0)]
    assert first.get_json()["pagination"] == {
        "limit": 50,
        "offset": 0,
        "count": 1,
    }
    assert "raw_snapshot" not in first.get_data(as_text=True)
    assert "profile-a" not in first.get_data(as_text=True)
    assert "private_token" not in second.get_data(as_text=True)
    assert "bundle" not in second.get_json()["items"][0]
    assert "evidence" not in second.get_json()["items"][0]


def test_history_rejects_non_integer_or_negative_pagination():
    client, _store = make_client()

    for path in (
        "/api/selector-probe/runs?limit=abc",
        "/api/selector-probe/runs?limit=0",
        "/api/selector-probe/versions?offset=-1",
    ):
        response = client.get(path)
        assert response.status_code == 400
        assert response.get_json() == {"error": "invalid_pagination"}


def test_status_is_sanitized_and_reports_active_version():
    client, _store = make_client()

    response = client.get("/api/selector-probe/status")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["registry"] == {
        "available": True,
        "active_version": "sel-1",
        "bundle_hash": "sha256:" + "a" * 64,
    }
    assert payload["latest_run"]["id"] == 7
    assert payload["health"] == "healthy"
    assert payload["current_version"] == "sel-1"
    assert payload["last_successful_validation_at"] == (
        "2026-07-29T03:01:00+08:00"
    )
    assert payload["next_run_at"] == "2026-07-30T03:00:00+08:00"
    assert payload["element_counts"]["all"] == 6
    assert payload["element_counts"]["failed"] == 2
    assert len(payload["priority_elements"]) == 5
    assert payload["gate_counts"] == {"automatic": 2, "manual": 1}
    assert payload["alert_summary"]["active"] == 2
    assert payload["alert_summary"]["webhook_status"] == "completed"
    assert payload["webhook_status"] == "completed"
    assert payload["recent_events"] == [
        {
            "type": "element_validate_completed",
            "summary": "element element-1: succeeded",
            "occurred_at": "2026-07-29T03:02:00+08:00",
        }
    ]
    assert payload["revision"] == 12
    body = response.get_data(as_text=True)
    assert "profile-a" not in body
    assert "ws://secret" not in body
    assert "raw_snapshot" not in body
    assert "raw_dom" not in body
    assert "screenshot_path" not in body
    assert "payload_json" not in body
    assert "details_json" not in body


def test_run_now_is_async_and_rejects_busy_until_dispatcher_completes():
    releases = []

    def dispatcher(request_id, done):
        assert request_id
        releases.append(done)
        return {
            "status": "accepted",
            "completion_managed": True,
        }

    client, _store = make_client(dispatcher=dispatcher)

    first = client.post("/api/selector-probe/run-now")
    second = client.post("/api/selector-probe/run-now")

    assert first.status_code == 202
    assert first.get_json()["status"] == "accepted"
    assert first.get_json()["request_id"]
    assert second.status_code == 409
    assert second.get_json() == {"error": "probe_busy"}

    releases[0]()
    third = client.post("/api/selector-probe/run-now")
    assert third.status_code == 202


def test_run_now_dispatcher_error_is_sanitized_and_releases_lock():
    calls = []

    def dispatcher(_request_id, _done):
        calls.append("dispatch")
        raise RuntimeError("private_token=secret")

    client, _store = make_client(dispatcher=dispatcher)

    first = client.post("/api/selector-probe/run-now")
    second = client.post("/api/selector-probe/run-now")

    assert first.status_code == 503
    assert second.status_code == 503
    assert first.get_json() == {"error": "dispatcher_unavailable"}
    assert "secret" not in first.get_data(as_text=True)
    assert calls == ["dispatch", "dispatch"]


def test_same_app_process_lock_recovers_after_ttl_without_done_callback():
    clock = [0.0]
    dispatches = []

    def dispatcher(request_id, _done):
        dispatches.append(request_id)
        return {
            "status": "accepted",
            "completion_managed": True,
        }

    app = Flask(__name__)
    app.register_blueprint(
        create_selector_probe_blueprint(
            store_factory=FakeStore,
            registry_factory=FakeRegistry,
            run_dispatcher=dispatcher,
            monotonic_fn=lambda: clock[0],
            local_busy_ttl_seconds=30,
        )
    )
    client = app.test_client()

    assert client.post("/api/selector-probe/run-now").status_code == 202
    assert client.post("/api/selector-probe/run-now").status_code == 409

    clock[0] = 31.0
    assert client.post("/api/selector-probe/run-now").status_code == 202
    assert len(dispatches) == 2


def test_gate_routes_list_pause_and_resume_only_manual_reason():
    gates = FakeGateService()
    client, _store = make_client(gate_service=gates)

    listed = client.get("/api/selector-probe/gates")
    paused = client.post(
        "/api/selector-probe/strategies/strategy-a/pause",
        json={"reason": "operator_pause"},
    )
    resumed = client.post(
        "/api/selector-probe/strategies/strategy-a/resume",
    )

    assert listed.status_code == 200
    assert listed.get_json()["items"][0]["strategy_id"] == "strategy-a"
    assert paused.status_code == 200
    assert [item["source"] for item in paused.get_json()["reasons"]] == [
        "manual",
        "probe",
    ]
    assert resumed.status_code == 200
    assert resumed.get_json()["allowed"] is False
    assert resumed.get_json()["reasons"] == [
        {
            "source": "probe",
            "reason_code": "selector_validation_failed",
            "aliases": ["comment_entry"],
            "selector_version_id": "sel-2",
            "created_at": "2026-07-29T03:00:00+08:00",
        }
    ]
    assert gates.calls == [
        ("strategy-a", True, "operator"),
        ("strategy-a", False, "operator"),
    ]


def test_manual_pause_rejects_any_reason_other_than_operator_pause():
    client, _store = make_client(gate_service=FakeGateService())

    response = client.post(
        "/api/selector-probe/strategies/strategy-a/pause",
        json={"reason": "selector_failed", "secret": "do-not-reflect"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_pause_request"}
    assert "do-not-reflect" not in response.get_data(as_text=True)


def test_default_gate_factory_closes_store_if_redis_construction_fails():
    events = []

    class Store:
        def close(self):
            events.append("store_closed")

    def fail_redis():
        events.append("redis_construct")
        raise RuntimeError("redis unavailable")

    try:
        default_gate_service_factory(
            store_factory=Store,
            redis_factory=fail_redis,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("factory should preserve construction failure")

    assert events == ["redis_construct", "store_closed"]


def test_gate_cleanup_closes_store_even_when_redis_close_raises():
    events = []

    class Redis:
        def close(self):
            events.append("redis_closed")
            raise RuntimeError("redis close failed")

    class Store:
        def managed_strategy_ids(self):
            return ()

        def close(self):
            events.append("store_closed")

    class Service:
        redis = Redis()
        store = Store()

    client, _store = make_client(gate_service=Service())

    response = client.get("/api/selector-probe/gates")

    assert response.status_code == 503
    assert response.get_json() == {"error": "gate_registry_unavailable"}
    assert events == ["redis_closed", "store_closed"]


def test_operator_can_read_and_probe_but_cannot_mutate_elements(tmp_path):
    client, csrf, selector_path, dispatched = _authenticated_element_client(
        tmp_path,
        "operator",
    )
    with SelectorProbeStore(selector_path) as store:
        record = ElementCatalog(
            store,
            element_id_factory=lambda: "element-operator",
        ).create_draft(_element_payload(), 7, "seed-admin")

    assert client.get("/api/selector-probe/elements").status_code == 200
    assert (
        client.get(f"/api/selector-probe/elements/{record.id}").status_code
        == 200
    )
    assert (
        client.post(
            "/api/selector-probe/elements",
            json=_element_payload(),
            headers={"X-CSRF-Token": csrf},
        ).status_code
        == 403
    )
    probe = client.post(
        f"/api/selector-probe/elements/{record.id}/probe",
        json={"expected_revision": record.revision},
        headers={"X-CSRF-Token": csrf},
    )
    assert probe.status_code == 404
    assert dispatched == []
    for path, method, expected_status in (
        (f"/api/selector-probe/elements/{record.id}", "patch", 403),
        (f"/api/selector-probe/elements/{record.id}", "delete", 403),
        (f"/api/selector-probe/elements/{record.id}/validate", "post", 404),
        (f"/api/selector-probe/elements/{record.id}/migrate", "post", 404),
    ):
        response = getattr(client, method)(
            path,
            json={
                "operation": "rename",
                "display_name": "Renamed",
                "expected_revision": record.revision,
            } if method == "patch" else {
                "expected_revision": record.revision
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == expected_status
        if expected_status == 403:
            assert response.get_json() == {"code": "forbidden"}


def test_element_route_accepts_frontend_referenced_yes_no_query(tmp_path):
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
    )
    referenced = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(display_name="Referenced element"),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    unreferenced = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(display_name="Unreferenced element"),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    with SelectorProbeStore(selector_path) as store:
        store.replace_strategy_dependencies(
            [
                (
                    referenced["id"],
                    "share-flow",
                    "open-share",
                    "click",
                    "Share flow",
                )
            ]
        )

    yes = client.get(
        "/api/selector-probe/elements"
        "?page=1&page_size=20&referenced=yes"
    )
    no = client.get(
        "/api/selector-probe/elements"
        "?page=1&page_size=20&referenced=no"
    )

    assert yes.status_code == 200
    assert no.status_code == 200
    assert [item["id"] for item in yes.get_json()["items"]] == [
        referenced["id"]
    ]
    assert [item["id"] for item in no.get_json()["items"]] == [
        unreferenced["id"]
    ]


def test_administrator_element_flow_enforces_shape_cas_and_validation_audit(
    tmp_path,
):
    client, csrf, selector_path, dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
    )
    created_response = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    )
    created = created_response.get_json()

    assert created_response.status_code == 201
    assert created["id"].startswith("element-")
    assert created["definition"] == {
        key: value
        for key, value in _element_payload().items()
        if key != "display_name"
    }

    unsafe = client.post(
        "/api/selector-probe/elements",
        json={
            "display_name": "Invalid entry",
            "unexpected": True,
        },
        headers={"X-CSRF-Token": csrf},
    )
    immutable = client.patch(
        f"/api/selector-probe/elements/{created['id']}",
        json={
            "operation": "rename",
            "expected_revision": created["revision"],
            "id": "replacement",
            "display_name": "Replacement",
        },
        headers={"X-CSRF-Token": csrf},
    )
    stale = client.patch(
        f"/api/selector-probe/elements/{created['id']}",
        json={
            "operation": "rename",
            "expected_revision": created["revision"] + 1,
            "display_name": "Replacement",
        },
        headers={"X-CSRF-Token": csrf},
    )
    validation = client.post(
        f"/api/selector-probe/elements/{created['id']}/validate",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert unsafe.status_code == 400
    assert unsafe.get_json() == {"code": "invalid_element_payload"}
    assert immutable.status_code == 400
    assert stale.status_code == 409
    assert stale.get_json() == {"code": "stale_revision"}
    assert validation.status_code == 404
    assert dispatched == []


def test_administrator_can_rename_and_rebind_manual_element(tmp_path):
    client, csrf, _selector_path, _dispatched = (
        _authenticated_element_client(tmp_path, "administrator")
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    renamed = client.patch(
        f"/api/selector-probe/elements/{created['id']}",
        json={
            "operation": "rename",
            "display_name": "Share control",
            "expected_revision": created["revision"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    rebound_definition = {
        key: value
        for key, value in _element_payload(
            page_key="tiktok.video",
            url_pattern="https://www.tiktok.com/@user/video/*",
            locators=[
                {"type": "css", "value": '[data-e2e="share"]'},
            ],
        ).items()
        if key != "display_name"
    }
    rebound = client.patch(
        f"/api/selector-probe/elements/{created['id']}",
        json={
            "operation": "rebind",
            "definition": rebound_definition,
            "expected_revision": renamed.get_json()["revision"],
        },
        headers={"X-CSRF-Token": csrf},
    )

    assert renamed.status_code == 200
    assert renamed.get_json()["display_name"] == "Share control"
    assert rebound.status_code == 200
    assert rebound.get_json()["definition"] == rebound_definition
    assert rebound.get_json()["status"] == "draft"


@pytest.mark.parametrize("query", ["source=automatic", "scope=active_video"])
def test_removed_catalog_filters_do_not_change_manual_catalog(query, tmp_path):
    client, csrf, _selector_path, _dispatched = (
        _authenticated_element_client(tmp_path, "administrator")
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    response = client.get(f"/api/selector-probe/elements?{query}")

    assert response.status_code == 200
    assert [item["id"] for item in response.get_json()["items"]] == [
        created["id"]
    ]


@pytest.mark.parametrize("operation", ["probe", "validate"])
def test_removed_single_element_execution_routes_return_not_found(
    operation,
    tmp_path,
):
    client, csrf, _selector_path, dispatched = (
        _authenticated_element_client(tmp_path, "administrator")
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    response = client.post(
        f"/api/selector-probe/elements/{created['id']}/{operation}",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 404
    assert dispatched == []


def test_removed_element_request_status_route_returns_not_found(tmp_path):
    client, _csrf, _selector_path, _dispatched = (
        _authenticated_element_client(tmp_path, "operator")
    )

    response = client.get(
        "/api/selector-probe/element-requests/legacy-request"
    )

    assert response.status_code == 404


def test_administrator_cannot_delete_element_with_dependencies(tmp_path):
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    with SelectorProbeStore(selector_path) as store:
        store.replace_strategy_dependencies(
            [
                (
                    created["id"],
                    "share-flow",
                    "open-share",
                    "click",
                    "Share flow",
                )
            ]
        )

    response = client.delete(
        f"/api/selector-probe/elements/{created['id']}",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 409
    assert response.get_json() == {"code": "element_has_dependencies"}


def test_removed_migration_route_does_not_mutate_dependencies(
    tmp_path,
):
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
    )
    with SelectorProbeStore(selector_path) as store:
        store.replace_strategy_dependencies(
            [
                (
                    "legacy-share",
                    "share-flow",
                    "open-share",
                    "click",
                    "Share flow",
                )
            ]
        )
        before = [
            tuple(row)
            for row in store.dependency_rows_for_aliases(["legacy-share"])
        ]
    response = client.post(
        "/api/selector-probe/elements/legacy-share/migrate",
        json={"expected_revision": 0},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 404
    with SelectorProbeStore(selector_path) as store:
        assert [
            tuple(row)
            for row in store.dependency_rows_for_aliases(["legacy-share"])
        ] == before


def test_element_detail_exposes_only_manual_v2_definition(tmp_path):
    client, csrf, _selector_path, _dispatched = (
        _authenticated_element_client(tmp_path, "administrator")
    )
    expected = _element_payload()
    created = client.post(
        "/api/selector-probe/elements",
        json=expected,
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    response = client.get(
        f"/api/selector-probe/elements/{created['id']}"
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["definition"] == {
        key: value for key, value in expected.items() if key != "display_name"
    }
    assert set(payload) == {
        "id",
        "display_name",
        "status",
        "page_key",
        "primary_locator_type",
        "dependency_count",
        "last_validated_at",
        "revision",
        "definition",
        "dependencies",
        "validation",
        "history",
        "alerts",
        "strategy_controls",
    }
    body = response.get_data(as_text=True)
    for forbidden in (
        "contract",
        "raw_dom",
        "profile_id",
    ):
        assert forbidden not in body
