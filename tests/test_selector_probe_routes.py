from __future__ import annotations

from datetime import UTC, datetime, time
import hashlib
import json
from types import SimpleNamespace

from flask import Flask
from werkzeug.security import generate_password_hash

from gateway.app import create_app
from gateway.auth_store import AuthStore
from gateway.management_db import open_management_db
from selector_probe.catalog import ElementCatalog
from selector_probe import worker as selector_worker
from selector_probe.blueprint import (
    create_selector_probe_blueprint,
    default_gate_service_factory,
)
from selector_probe.store import SelectorProbeStore
from selector_probe.probe import run_healing_probe
from selector_probe.store import _validated_bundle, _validated_evidence


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
                "model_id": "gpt-main",
                "prompt_version": "selector-repair-v1",
                "created_at": "2026-07-29T03:00:30+08:00",
                "validated_at": "2026-07-29T03:00:40+08:00",
                "published_at": "2026-07-29T03:00:50+08:00",
                "bundle": ACTIVE_BUNDLE,
                "evidence": {
                    "profile_id": "profile-a",
                    "raw_snapshot": {"nodes": []},
                    "model_api_key": "secret",
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


def _element_payload(**changes):
    payload = {
        "display_name": "Share entry",
        "intent": "find the share entry for the active video",
        "required_state": "feed_ready",
        "scope": "active_video",
        "probe_action": "inspect_only",
        "accepted_roles": ["button"],
        "accepted_names": ["Share"],
        "name_mode": "exact",
        "preferred_attributes": ["data-e2e", "aria-label"],
        "postcondition": "",
    }
    payload.update(changes)
    return payload


def _authenticated_element_client(
    tmp_path,
    role,
    *,
    legacy_elements=None,
    registry=None,
    use_default_dispatcher=False,
    dispatcher_raises=False,
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
    dispatched = []

    def element_dispatcher(request_id):
        with SelectorProbeStore(selector_path) as store:
            request = store.get_element_request(request_id)
        assert request is not None
        assert request["status"] == "pending"
        dispatched.append(request)
        if dispatcher_raises:
            raise RuntimeError("wake unavailable")
        return {"status": "accepted"}

    config = {
        "TESTING": True,
        "MANAGEMENT_STATE_DIR": state_dir,
        "MANAGEMENT_DB_PATH": management_path,
        "SELECTOR_PROBE_STORE_FACTORY": (
            lambda: SelectorProbeStore(selector_path)
        ),
        "SELECTOR_PROBE_LEGACY_ELEMENTS_PROVIDER": (
            lambda: legacy_elements or {}
        ),
        "SELECTOR_PROBE_REGISTRY_FACTORY": (
            lambda: registry or FakeRegistry()
        ),
    }
    if not use_default_dispatcher:
        config["SELECTOR_PROBE_ELEMENT_REQUEST_DISPATCHER"] = (
            element_dispatcher
        )
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
        dispatched,
    )


def test_active_returns_complete_published_bundle_and_drops_hostile_fields():
    hostile = {
        **ACTIVE_BUNDLE,
        "draft": {"version": "draft-secret"},
        "profile_id": "profile-a",
        "raw_snapshot": {"nodes": [{"cdp_url": "ws://secret"}]},
        "webhook": {"signing_secret": "webhook-secret"},
        "model_api_key": "model-secret",
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
        "model-secret",
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
    assert "model_api_key" not in second.get_data(as_text=True)
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
        raise RuntimeError("model_api_key=secret")

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
    assert probe.status_code == 202
    assert dispatched[0]["request_type"] == "probe"
    for path, method in (
        (f"/api/selector-probe/elements/{record.id}/draft", "patch"),
        (f"/api/selector-probe/elements/{record.id}", "delete"),
        (f"/api/selector-probe/elements/{record.id}/validate", "post"),
        (f"/api/selector-probe/elements/{record.id}/migrate", "post"),
    ):
        response = getattr(client, method)(
            path,
            json={"expected_revision": record.revision},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 403
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
    assert created["contract"]["probe_action"] == "inspect_only"

    unsafe = client.post(
        "/api/selector-probe/elements",
        json={**_element_payload(), "xpath": "/html/body/button"},
        headers={"X-CSRF-Token": csrf},
    )
    immutable = client.patch(
        f"/api/selector-probe/elements/{created['id']}/draft",
        json={
            "expected_revision": created["revision"],
            "id": "replacement",
            "contract": created["contract"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    stale = client.patch(
        f"/api/selector-probe/elements/{created['id']}/draft",
        json={
            "expected_revision": created["revision"] + 1,
            "contract": created["contract"],
        },
        headers={"X-CSRF-Token": csrf},
    )
    validation = client.post(
        f"/api/selector-probe/elements/{created['id']}/validate",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert unsafe.status_code == 400
    assert immutable.status_code == 400
    assert stale.status_code == 409
    assert stale.get_json() == {"code": "stale_revision"}
    assert validation.status_code == 202
    assert dispatched[-1]["request_type"] == "validate"
    with SelectorProbeStore(selector_path) as store:
        audit = store.connection.execute(
            """
            SELECT actor_username, event_type, target_id
            FROM selector_management_audit_events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        assert tuple(audit) == (
            "administrator",
            "element_validation_requested",
            created["id"],
        )


def test_element_request_returns_durable_202_when_best_effort_wake_fails(
    tmp_path,
):
    client, csrf, selector_path, dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
        dispatcher_raises=True,
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    response = client.post(
        f"/api/selector-probe/elements/{created['id']}/validate",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 202
    assert dispatched[0]["status"] == "pending"
    with SelectorProbeStore(selector_path) as store:
        request = store.get_element_request(
            response.get_json()["request_id"]
        )
        assert request["status"] == "pending"


def test_element_request_status_route_returns_only_safe_polling_projection(
    tmp_path,
):
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    accepted = client.post(
        f"/api/selector-probe/elements/{created['id']}/validate",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    with SelectorProbeStore(selector_path) as store:
        claimed = store.claim_element_request(claim_token="worker")
        assert store.complete_element_request(
            claimed["request_id"],
            claimed["claim_token"],
            claimed["claim_generation"],
            result={
                "status": "published",
                "published": True,
                "reconciled": True,
                "new_version": "sel-safe",
                "candidate": {
                    "elements": {
                        created["id"]: {
                            "scope": "active_video",
                            "locators": [
                                {
                                    "id": "safe-primary",
                                    "type": "attribute",
                                    "name": "data-e2e",
                                    "value": "share",
                                    "enabled": True,
                                }
                            ],
                        }
                    }
                },
                "rounds": [
                    {
                        "profile_mask": "***A123",
                        "round_number": 1,
                        "status": "passed",
                        "raw_dom": "secret-dom",
                    }
                ],
                "repairs": [
                    {
                        "attempt": 1,
                        "failure_code": "zero_match",
                        "new_method": "attribute",
                        "prompt": "secret-prompt",
                    }
                ],
                "raw_snapshot": {"secret": True},
            },
        )

    response = client.get(
        f"/api/selector-probe/element-requests/{accepted['request_id']}"
    )
    payload = response.get_json()

    assert response.status_code == 200
    assert set(payload) == {
        "request_id",
        "request_type",
        "element_id",
        "status",
        "attempt_count",
        "error_code",
        "result",
    }
    assert payload["result"]["candidate"]["locators"][0]["value"] == "share"
    assert payload["result"]["rounds"][0]["profile_mask"] == "***A123"
    assert payload["result"]["repairs"][0]["attempt"] == 1
    body = response.get_data(as_text=True)
    assert "secret-dom" not in body
    assert "secret-prompt" not in body
    assert "raw_snapshot" not in body


def test_create_app_default_element_dispatcher_does_not_return_503(tmp_path):
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
        use_default_dispatcher=True,
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()

    response = client.post(
        f"/api/selector-probe/elements/{created['id']}/probe",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 202
    with SelectorProbeStore(selector_path) as store:
        assert (
            store.get_element_request(response.get_json()["request_id"])
            is not None
        )


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


def test_legacy_migration_keeps_dependency_snapshot_unchanged(tmp_path):
    legacy_xpath = "/html/body/main/div[2]/button"
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
        legacy_elements={"legacy-share": legacy_xpath},
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
    listed = client.get("/api/selector-probe/elements")
    legacy_item = next(
        item
        for item in listed.get_json()["items"]
        if item["id"] == "legacy-share"
    )
    assert legacy_item["migration_available"] is True
    assert legacy_item["management_source"] == "legacy_manual"
    assert legacy_item["draft_status"] is None
    assert legacy_item["revision"] == 0

    response = client.post(
        "/api/selector-probe/elements/legacy-share/migrate",
        json={"expected_revision": 0},
        headers={"X-CSRF-Token": csrf},
    )

    assert response.status_code == 200
    assert response.get_json()["management_source"] == "legacy_manual"
    assert response.get_json()["migration_available"] is False
    assert response.get_json()["candidates"][0]["value"] == legacy_xpath
    with SelectorProbeStore(selector_path) as store:
        assert [
            tuple(row)
            for row in store.dependency_rows_for_aliases(["legacy-share"])
        ] == before


def test_element_detail_returns_real_safe_candidate_comparison_and_history(
    tmp_path,
):
    registry = FakeRegistry(active={})
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
        registry=registry,
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    element_id = created["id"]
    active_definition = {
        "scope": "active_video",
        "locators": [
            {
                "id": "active-role",
                "type": "role",
                "role": "button",
                "name": "Share",
                "name_mode": "exact",
                "enabled": True,
            }
        ],
    }
    registry.active = {
        "version": "sel-detail",
        "bundle_hash": "sha256:" + "a" * 64,
        "elements": {element_id: active_definition},
    }
    repaired = {
        "id": "repaired-attribute",
        "type": "attribute",
        "name": "data-e2e",
        "value": "share-icon",
        "enabled": True,
    }
    with SelectorProbeStore(selector_path) as store:
        with store.connection:
            store.connection.execute(
                """
                UPDATE element_drafts
                SET candidates_json = ?, validation_json = ?
                WHERE element_id = ?
                """,
                (
                    json.dumps([repaired]),
                    json.dumps(
                        {
                            "status": "passed",
                            "repairs": [
                                {
                                    "attempt": index,
                                    "failure_code": "zero_match",
                                    "new_method": "attribute",
                                    "result": "passed",
                                    "prompt": "secret prompt",
                                }
                                for index in range(1, 5)
                            ],
                            "raw_dom": "secret dom",
                        }
                    ),
                    element_id,
                ),
            )
            store.connection.execute(
                """
                INSERT INTO selector_versions (
                    id, site, environment, status, base_version_id,
                    bundle_json, bundle_hash, evidence_json, model_id,
                    prompt_version, created_at, validated_at, published_at
                ) VALUES (?, 'tiktok', 'production', 'published', '',
                          ?, ?, ?, 'model-safe', 'prompt-safe',
                          ?, ?, ?)
                """,
                (
                    "sel-detail",
                    json.dumps(
                        {
                            "elements": {element_id: active_definition},
                            "raw_dom": "secret version dom",
                        }
                    ),
                    "sha256:" + "a" * 64,
                    json.dumps({"raw_dom": "secret evidence"}),
                    "2026-07-29T03:00:00+00:00",
                    "2026-07-29T03:00:01+00:00",
                    "2026-07-29T03:00:02+00:00",
                ),
            )

    response = client.get(f"/api/selector-probe/elements/{element_id}")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["candidate_comparison"] == {
        "active": [active_definition["locators"][0]],
        "deterministic": [],
        "repaired": [repaired],
    }
    assert payload["deterministic_candidates"] == []
    assert payload["repaired_candidates"] == [repaired]
    assert len(payload["repairs"]) == 3
    assert payload["history"] == [
        {
            "version_id": "sel-detail",
            "status": "published",
            "base_version_id": "",
            "bundle_hash": "sha256:" + "a" * 64,
            "created_at": "2026-07-29T03:00:00+00:00",
            "validated_at": "2026-07-29T03:00:01+00:00",
            "published_at": "2026-07-29T03:00:02+00:00",
        }
    ]
    body = response.get_data(as_text=True)
    for forbidden in (
        "secret prompt",
        "secret dom",
        "secret version dom",
        "secret evidence",
    ):
        assert forbidden not in body


def test_pending_and_processing_element_request_lock_mutations_until_terminal(
    tmp_path,
):
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    element_id = created["id"]
    original_contract = created["contract"]
    accepted = client.post(
        f"/api/selector-probe/elements/{element_id}/validate",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )
    request_revision = accepted.get_json()["expected_revision"]
    changed_contract = {
        **original_contract,
        "intent": "must wait until request completes",
    }

    pending_responses = (
        client.patch(
            f"/api/selector-probe/elements/{element_id}/draft",
            json={
                "expected_revision": request_revision,
                "contract": changed_contract,
            },
            headers={"X-CSRF-Token": csrf},
        ),
        client.delete(
            f"/api/selector-probe/elements/{element_id}",
            json={"expected_revision": request_revision},
            headers={"X-CSRF-Token": csrf},
        ),
        client.post(
            f"/api/selector-probe/elements/{element_id}/probe",
            json={"expected_revision": request_revision},
            headers={"X-CSRF-Token": csrf},
        ),
    )
    assert all(response.status_code == 409 for response in pending_responses)
    assert all(
        response.get_json() == {"code": "element_request_in_progress"}
        for response in pending_responses
    )
    with SelectorProbeStore(selector_path) as store:
        pending_element = store.get_managed_element_row(element_id)
        pending_draft = store.managed_element_draft_row(element_id)
        claimed = store.claim_element_request(claim_token="worker-lock")
    assert pending_element["revision"] == request_revision
    assert json.loads(pending_draft["contract_json"]) == original_contract

    processing_patch = client.patch(
        f"/api/selector-probe/elements/{element_id}/draft",
        json={
            "expected_revision": request_revision,
            "contract": changed_contract,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert processing_patch.status_code == 409
    assert processing_patch.get_json() == {
        "code": "element_request_in_progress"
    }

    with SelectorProbeStore(selector_path) as store:
        assert store.complete_element_request(
            claimed["request_id"],
            claimed["claim_token"],
            claimed["claim_generation"],
            result={
                "status": "published",
                "published": True,
                "reconciled": True,
                "new_version": "sel-lock-complete",
            },
        )
    after_terminal = client.patch(
        f"/api/selector-probe/elements/{element_id}/draft",
        json={
            "expected_revision": request_revision,
            "contract": changed_contract,
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert after_terminal.status_code == 200
    assert after_terminal.get_json()["revision"] == request_revision + 1
    assert after_terminal.get_json()["contract"]["intent"] == (
        "must wait until request completes"
    )


def test_repaired_runtime_result_flows_through_worker_store_and_detail(
    tmp_path,
):
    registry = FakeRegistry(active={})
    client, csrf, selector_path, _dispatched = _authenticated_element_client(
        tmp_path,
        "administrator",
        registry=registry,
    )
    created = client.post(
        "/api/selector-probe/elements",
        json=_element_payload(),
        headers={"X-CSRF-Token": csrf},
    ).get_json()
    element_id = created["id"]
    accepted = client.post(
        f"/api/selector-probe/elements/{element_id}/validate",
        json={"expected_revision": created["revision"]},
        headers={"X-CSRF-Token": csrf},
    )
    assert accepted.status_code == 202

    deterministic, _deterministic_hash = _validated_bundle(
        {
            "elements": {
                element_id: {
                    "scope": "active_video",
                    "locators": [
                        {
                            "id": "deterministic-role",
                            "type": "role",
                            "role": "button",
                            "name": "Share",
                            "name_mode": "exact",
                            "enabled": True,
                        }
                    ],
                }
            }
        }
    )
    repaired, _repaired_hash = _validated_bundle(
        {
            "elements": {
                element_id: {
                    "scope": "active_video",
                    "locators": [
                        {
                            "id": "repaired-attribute",
                            "type": "attribute",
                            "name": "data-e2e",
                            "value": "share-icon",
                            "enabled": True,
                        }
                    ],
                }
            }
        }
    )

    def evidence(bundle):
        alias_result = {
            element_id: {
                "status": "ok",
                "candidate_id": (
                    bundle["elements"][element_id]["locators"][0]["id"]
                ),
            }
        }
        validations = []
        for round_number in (1, 2):
            for profile_number in (1, 2):
                marker = f"{profile_number}:{round_number}"
                validations.append(
                    {
                        "profile_mask": f"***P{profile_number:03d}",
                        "round_number": round_number,
                        "reset_evidence_hash": "sha256:"
                        + hashlib.sha256(
                            f"reset:{marker}".encode()
                        ).hexdigest(),
                        "snapshot_hash": "sha256:"
                        + hashlib.sha256(
                            f"snapshot:{marker}".encode()
                        ).hexdigest(),
                        "page_generation": "sha256:"
                        + hashlib.sha256(
                            f"generation:{marker}".encode()
                        ).hexdigest(),
                            "aliases": json.loads(json.dumps(alias_result)),
                    }
                )
        return {
            "status": "passed",
            "bundle_hash": bundle["bundle_hash"],
            "profiles_passed": 2,
            "rounds_passed": 2,
            "validations": validations,
        }

    class RepairingRuntime:
        model_call = None
        config = SimpleNamespace(model_id="model-safe")

        def __init__(self):
            self.context_number = 0

        def validate_active(self):
            return {"status": "healthy"}

        def deterministic_candidates(self, **_kwargs):
            return deterministic

        def deterministic_failure(self):
            return None

        def validate_candidate(self, candidate):
            locator_id = (
                candidate["elements"][element_id]["locators"][0]["id"]
            )
            if locator_id == "deterministic-role":
                return {
                    "status": "selector_validation_failed",
                    "failure_class": "selector",
                    "failed_aliases": [element_id],
                    "code": "zero_match",
                    "match_count": 0,
                }
            return {"status": "passed"}

        def fresh_validation_context(self, **_kwargs):
            self.context_number += 1
            snapshot = {
                "nodes": [],
                "capture": self.context_number,
            }
            encoded = json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode()
            return {
                "snapshot": snapshot,
                "_snapshot": object(),
                "snapshot_hash": "sha256:"
                + hashlib.sha256(encoded).hexdigest(),
                "page_generation": "sha256:"
                + hashlib.sha256(
                    f"page:{self.context_number}".encode()
                ).hexdigest(),
            }

        def repair_candidate(self, **_kwargs):
            return repaired

        def full_validate(self, candidate):
            return evidence(candidate)

        def store_and_publish(self, _candidate, _evidence):
            return {
                "version": "sel-repaired",
                "published": True,
                "reconciled": True,
            }

    _validated_evidence(
        evidence(repaired),
        repaired["bundle_hash"],
        repaired["elements"],
    )
    runtime = RepairingRuntime()
    healing_results = []
    worker_result = selector_worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda _request: (
            healing_results.append(
                run_healing_probe(
                    runtime,
                    force_requested_candidate=True,
                    initial_failed_aliases=(element_id,),
                )
            )
            or healing_results[-1]
        ),
        db_path=selector_path,
        clock=SimpleNamespace(
            now=lambda: datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
        ),
    )
    registry.active = {
        "version": "sel-repaired",
        "bundle_hash": repaired["bundle_hash"],
        "elements": repaired["elements"],
    }

    assert worker_result["completed"] == 1, (
        worker_result,
        healing_results,
    )
    response = client.get(f"/api/selector-probe/elements/{element_id}")
    assert response.status_code == 200
    detail = response.get_json()
    assert detail["candidate_comparison"]["deterministic"] == []
    assert detail["candidate_comparison"]["repaired"] == [
        repaired["elements"][element_id]["locators"][0]
    ]
    assert detail["repairs"] == [
        {
            "attempt": 1,
            "previous_method": "role",
            "failure_code": "zero_match",
            "match_count": 0,
            "new_method": "attribute",
            "prompt_version": "selector-repair-v1",
            "model_id": "model-safe",
            "result": "passed",
        }
    ]
    body = response.get_data(as_text=True)
    assert "snapshot" not in body
    assert "nodes" not in body
