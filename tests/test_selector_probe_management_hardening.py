from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
from types import SimpleNamespace

from flask import Flask, g, request
import pytest

from gateway.settings_store import load_settings, save_settings
from selector_probe.blueprint import (
    RedisRunDispatcher,
    create_selector_probe_blueprint,
)
from selector_probe.gates import StrategyGateService
from selector_probe.store import SelectorProbeStore


def _settings() -> dict:
    return {
        "browser": {"task_goal": "legacy-compatible"},
        "models": {
            "default_model_id": "repair-model",
            "items": [
                {
                    "id": "repair-model",
                    "provider": "openai",
                    "mode": "repair_only",
                    "enabled": True,
                    "api_key": "configured-model-key",
                }
            ],
        },
        "selector_probe": {
            "enabled": True,
            "site": "tiktok",
            "environment": "production",
            "timezone": "Asia/Shanghai",
            "daily_time": "03:00",
            "target_url": "https://www.tiktok.com",
            "test_profile_ids": ["profile-alpha", "profile-beta"],
            "dedicated_test_profile_ids": [
                "profile-alpha",
                "profile-beta",
            ],
            "profile_health": {
                "profile-alpha": "healthy",
                "profile-beta": "healthy",
            },
            "model_id": "repair-model",
            "rollout_mode": "publish",
            "observe_only": False,
            "redis": {
                "namespace": "selector-probe",
                "aof_enabled": True,
                "eviction_policy": "noeviction",
                "status": "healthy",
            },
            "webhook": {
                "enabled": True,
                "url": "https://hooks.example.test/configured",
                "status": "passed",
                "type": "generic",
            },
        },
    }


def _probe_app(
    database: Path,
    settings: dict,
    *,
    run_dispatcher=None,
    webhook_test_dispatcher=None,
    utcnow_fn=None,
) -> Flask:
    class Registry:
        def get_active(self):
            return None

        def close(self):
            return None

    def provider():
        return copy.deepcopy(settings)

    def mutator(function):
        updated = function(copy.deepcopy(settings))
        settings.clear()
        settings.update(updated)
        return copy.deepcopy(settings)

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "hardening-test-secret"

    @app.before_request
    def actor():
        g.management_user = SimpleNamespace(
            id=int(request.headers.get("X-Test-Actor", "7")),
            username="admin",
            role="administrator",
        )

    options = {}
    if utcnow_fn is not None:
        options["utcnow_fn"] = utcnow_fn
    if webhook_test_dispatcher is not None:
        options["webhook_test_dispatcher"] = webhook_test_dispatcher
    app.register_blueprint(
        create_selector_probe_blueprint(
            store_factory=lambda: SelectorProbeStore(database),
            registry_factory=Registry,
            gate_service_factory=lambda: StrategyGateService(
                SelectorProbeStore(database)
            ),
            settings_provider=provider,
            settings_mutator=mutator,
            settings_preflight_runner=lambda _raw, _candidate: {
                "profiles": "passed",
                "redis_aof": "passed",
                "redis_eviction": "passed",
                "model": "passed",
                "webhook": "passed",
            },
            run_dispatcher=run_dispatcher
            or (lambda request_id, _done: {
                "status": "accepted",
                "request_id": request_id,
            }),
            **options,
        )
    )
    return app


def _candidate(client) -> dict:
    response = client.get("/api/selector-probe/settings")
    assert response.status_code == 200
    return response.get_json()


def _ui_candidate(value: dict) -> dict:
    return {
        "enabled": value["enabled"],
        "rollout_mode": value["rollout_mode"],
        "schedule_time": value["schedule_time"],
        "timezone": value["timezone"],
        "target_origin": value["target_origin"],
        "freshness_hours": value["freshness_hours"],
        "retry_policy": value["retry_policy"],
        "profiles": [
            {
                "profile_ref": item["profile_ref"],
                "dedicated_test": item["dedicated_test"],
            }
            for item in value["profiles"]
        ],
        "model": {"id": value["model"]["id"]},
        "redis": {"namespace": value["redis"]["namespace"]},
        "webhook": {
            "enabled": value["webhook"]["enabled"],
            "type": value["webhook"]["type"],
            "timeout_seconds": value["webhook"]["timeout_seconds"],
            "retry_policy": value["webhook"]["retry_policy"],
        },
    }


def _cache_row(database: Path, key: str):
    with SelectorProbeStore(database) as store:
        return store.connection.execute(
            """
            SELECT state, response_json, request_json
            FROM management_idempotency_cache
            WHERE idempotency_key = ?
            """,
            (key,),
        ).fetchone()


def _assert_terminal_replay(client, path: str, payload: dict) -> None:
    first = client.open(path, method="PATCH", json=payload)
    replay = client.open(path, method="PATCH", json=payload)
    assert replay.status_code == first.status_code
    assert replay.get_json() == first.get_json()
    assert replay.get_json() != {"code": "operation_in_progress"}


def _insert_publication_blocker(
    store: SelectorProbeStore,
    blocker: str,
) -> None:
    now = datetime.now(UTC).isoformat()
    if blocker == "publication_outbox":
        store.connection.execute(
            """
            INSERT INTO publication_outbox (
                event_type, aggregate_id, payload_json, status,
                next_attempt_at, created_at
            ) VALUES (
                'selector_version_publish', 'sel-pending', '{}',
                'pending', ?, ?
            )
            """,
            (now, now),
        )
    else:
        store.connection.execute(
            """
            INSERT INTO element_request_outbox (
                request_id, request_type, element_id, expected_revision,
                contract_json, actor_user_id, actor_username, status,
                next_attempt_at, created_at, updated_at
            ) VALUES (
                'request-publishing', 'probe', 'comment-submit', 1,
                '{}', 7, 'admin', 'publishing', ?, ?, ?
            )
            """,
            (now, now, now),
        )
    store.connection.commit()


def test_management_idempotency_database_never_stores_submitted_secrets_or_url(
    tmp_path,
):
    database = tmp_path / "probe.db"
    settings = _settings()
    client = _probe_app(database, settings).test_client()
    candidate = _candidate(client)
    submitted = {
        "model_api_key": "MODEL_SECRET_4f8f0e",
        "redis_password": "REDIS_SECRET_91ac72",
        "webhook_signing_secret": "WEBHOOK_SECRET_764fd1",
        "webhook_url": (
            "https://hooks.example.test/"
            "FULL_PRIVATE_URL_8c971b?token=URL_TOKEN_381bd0"
        ),
    }

    response = client.patch(
        "/api/selector-probe/settings",
        json={
            "expected_revision": 0,
            "reason": "rotate write-only credentials",
            "idempotency_key": "secret-persistence-hardening",
            "settings": _ui_candidate(candidate),
            "secrets": submitted,
        },
    )

    assert response.status_code == 200
    raw_database = database.read_bytes()
    for secret in submitted.values():
        assert secret.encode() not in raw_database
    row = _cache_row(database, "secret-persistence-hardening")
    assert row["state"] == "completed"
    assert row["request_json"] == "{}"


def test_reserved_settings_failures_replay_terminal_response_not_pending(
    tmp_path,
):
    database = tmp_path / "probe.db"
    settings = _settings()
    client = _probe_app(database, settings).test_client()
    current = _candidate(client)

    stale = {
        "expected_revision": 99,
        "reason": "stale request",
        "idempotency_key": "terminal-stale",
        "settings": _ui_candidate(current),
    }
    _assert_terminal_replay(
        client, "/api/selector-probe/settings", stale
    )

    reason_candidate = _ui_candidate(current)
    reason_candidate["enabled"] = False
    missing_reason = {
        "expected_revision": 0,
        "reason": "",
        "idempotency_key": "terminal-reason",
        "settings": reason_candidate,
    }
    _assert_terminal_replay(
        client, "/api/selector-probe/settings", missing_reason
    )

    enforce_candidate = _ui_candidate(current)
    enforce_candidate["rollout_mode"] = "enforce"
    missing_preflight = {
        "expected_revision": 0,
        "reason": "enable enforce",
        "idempotency_key": "terminal-preflight",
        "settings": enforce_candidate,
    }
    _assert_terminal_replay(
        client, "/api/selector-probe/settings", missing_preflight
    )

    with SelectorProbeStore(database) as store:
        _insert_publication_blocker(store, "publication_outbox")
    mode_candidate = _ui_candidate(current)
    mode_candidate["rollout_mode"] = "observe"
    publication = {
        "expected_revision": 0,
        "reason": "switch mode",
        "idempotency_key": "terminal-publication",
        "settings": mode_candidate,
    }
    _assert_terminal_replay(
        client, "/api/selector-probe/settings", publication
    )

    for key in (
        "terminal-stale",
        "terminal-reason",
        "terminal-preflight",
        "terminal-publication",
    ):
        assert _cache_row(database, key)["state"] == "failed"


def test_dispatcher_failure_replays_same_terminal_response(tmp_path):
    database = tmp_path / "probe.db"

    def fail_dispatch(_request_id, _done):
        raise RuntimeError("dispatcher transport failed")

    client = _probe_app(
        database,
        _settings(),
        run_dispatcher=fail_dispatch,
    ).test_client()
    payload = {"idempotency_key": "terminal-dispatcher"}

    first = client.post("/api/selector-probe/run-now", json=payload)
    replay = client.post("/api/selector-probe/run-now", json=payload)

    assert first.status_code == 503
    assert replay.status_code == first.status_code
    assert replay.get_json() == first.get_json()
    assert replay.get_json() != {"code": "operation_in_progress"}
    assert _cache_row(database, "terminal-dispatcher")["state"] == "failed"


def _insert_malicious_management_rows(database: Path) -> tuple[int, int]:
    now = datetime.now(UTC).isoformat()
    malicious = {
        "raw_dom": "<html>RAW_DOM_LEAK_1001</html>",
        "a11y": "A11Y_LEAK_1002",
        "html": "HTML_LEAK_1003",
        "headers": {"Authorization": "TOKEN_LEAK_1004"},
        "cookies": "COOKIE_LEAK_1005",
        "token": "TOKEN_LEAK_1006",
        "api_key": "API_KEY_LEAK_1007",
        "absolute_path": "C:\\private\\ABS_PATH_LEAK_1008.txt",
        "url": "https://private.invalid/URL_LEAK_1009",
        "reason": "https://private.invalid/URL_IN_REASON_LEAK_1010",
        "severity": "TOKEN_IN_SEVERITY_LEAK_1011",
        "actor": "https://private.invalid/URL_IN_ACTOR_LEAK_1012",
        "due_slot": "https://private.invalid/DUE_SLOT_LEAK_1015",
        "next_retry_at": "Bearer NEXT_RETRY_LEAK_1016",
        "stages": [
            {
                "name": "Bearer.STAGE_NAME_LEAK_1017",
                "status": "passed",
                "failure_code": "api_key:STAGE_FAILURE_LEAK_1018",
            }
        ],
        "repairs": [
            {
                "attempt": 1,
                "validation_result": "failed",
                "model_id": "TOKENVALUE_REPAIR_MODEL_LEAK_1019",
            }
        ],
        "rounds": [
            {
                "round": 1,
                "status": "passed",
                "failure_code": (
                    "credential.ROUND_FAILURE_LEAK_1020"
                ),
            }
        ],
    }
    with SelectorProbeStore(database) as store:
        cursor = store.connection.execute(
            """
            INSERT INTO probe_runs (
                scheduled_for, started_at, finished_at, status,
                details_json
            ) VALUES (?, ?, ?, 'failed', ?)
            """,
            (
                "2026-07-29T03:00:00+00:00",
                now,
                now,
                json.dumps(malicious),
            ),
        )
        run_id = int(cursor.lastrowid)
        store.connection.commit()
        alert = store.open_or_update_alert(
            fingerprint="malicious-alert",
            failure_class="selector_failure",
            aliases=["comment_submit"],
            strategy_ids=[],
            active_version="sel-malicious",
            details=malicious,
            site="tiktok",
            environment="production",
            now=now,
        )
        store.connection.execute(
            "UPDATE probe_alerts SET screenshot_path = ? WHERE id = ?",
            ("C:\\private\\ABS_SCREENSHOT_LEAK_1013.jpg", alert["id"]),
        )
        store.record_management_audit(
            actor_user_id=7,
            actor_username="admin",
            event_type="malicious_details",
            target_type="probe",
            target_id="malicious",
            details=malicious,
        )
        store.connection.execute(
            """
            INSERT INTO selector_versions (
                id, site, environment, status, bundle_json, bundle_hash,
                evidence_json, created_at, validated_at, published_at
            ) VALUES (
                'sel-malicious', 'tiktok', 'production', 'published',
                '{}', ?, ?, ?, ?, ?
            )
            """,
            (
                "sha256:" + "a" * 64,
                json.dumps(malicious),
                now,
                now,
                now,
            ),
        )
        store.connection.execute(
            """
            INSERT INTO publication_outbox (
                event_type, aggregate_id, payload_json, status,
                next_attempt_at, last_error, created_at
            ) VALUES (
                'selector_version_publish', 'sel-malicious', '{}',
                'failed', ?, ?, ?
            )
            """,
            (
                now,
                (
                    "TOKEN_IN_LAST_ERROR_LEAK_1014 "
                    "C:\\private\\error.txt"
                ),
                now,
            ),
        )
        store.connection.commit()
    return run_id, int(alert["id"])


def _walk(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def test_management_api_safe_projection_rejects_malicious_database_details(
    tmp_path,
):
    database = tmp_path / "probe.db"
    run_id, alert_id = _insert_malicious_management_rows(database)
    client = _probe_app(database, _settings()).test_client()
    responses = [
        client.get("/api/selector-probe/runs"),
        client.get(f"/api/selector-probe/runs/{run_id}"),
        client.get("/api/selector-probe/alerts"),
        client.get(f"/api/selector-probe/alerts/{alert_id}"),
        client.get("/api/selector-probe/audit"),
        client.get("/api/selector-probe/versions"),
        client.get("/api/selector-probe/versions/sel-malicious"),
    ]

    assert all(response.status_code == 200 for response in responses)
    serialized = json.dumps(
        [response.get_json() for response in responses],
        sort_keys=True,
    )
    forbidden_markers = (
        "RAW_DOM_LEAK",
        "A11Y_LEAK",
        "HTML_LEAK",
        "TOKEN_LEAK",
        "COOKIE_LEAK",
        "API_KEY_LEAK",
        "ABS_PATH_LEAK",
        "ABS_SCREENSHOT_LEAK",
        "URL_LEAK",
        "URL_IN_REASON_LEAK",
        "URL_IN_ACTOR_LEAK",
        "TOKEN_IN_SEVERITY_LEAK",
        "TOKEN_IN_LAST_ERROR_LEAK",
        "DUE_SLOT_LEAK",
        "NEXT_RETRY_LEAK",
        "STAGE_NAME_LEAK",
        "STAGE_FAILURE_LEAK",
        "REPAIR_MODEL_LEAK",
        "ROUND_FAILURE_LEAK",
    )
    leaked_markers = [
        marker for marker in forbidden_markers if marker in serialized
    ]
    assert not leaked_markers, (
        f"management API leaked persisted markers: {leaked_markers}"
    )
    for response in responses:
        for key, value in _walk(response.get_json()):
            assert key not in {
                "raw_dom",
                "a11y",
                "html",
                "headers",
                "cookies",
                "token",
                "api_key",
                "absolute_path",
                "url",
            }
            if key == "last_error":
                assert isinstance(value, str)
                assert re.fullmatch(r"[a-z0-9_]{1,64}", value)


def test_webhook_failure_is_terminal_and_replay_does_not_resend(tmp_path):
    database = tmp_path / "probe.db"
    calls = []

    def unavailable(payload):
        calls.append(payload)
        raise RuntimeError("webhook unavailable")

    client = _probe_app(
        database,
        _settings(),
        webhook_test_dispatcher=unavailable,
    ).test_client()
    body = {
        "idempotency_key": "webhook-terminal-failure",
        "payload": {
            "event": "selector_probe.webhook_test",
            "environment": "production",
            "site": "tiktok",
            "synthetic": True,
        },
    }

    first = client.post("/api/selector-probe/webhook-test", json=body)
    replay = client.post("/api/selector-probe/webhook-test", json=body)

    assert first.status_code == 503
    assert replay.status_code == 503
    assert replay.get_json() == first.get_json()
    assert len(calls) == 1
    assert _cache_row(
        database, "webhook-terminal-failure"
    )["state"] == "failed"


def test_alert_transition_is_monotonic_and_gate_scope_is_exact(tmp_path):
    database = tmp_path / "probe.db"
    now = datetime.now(UTC).isoformat()
    with SelectorProbeStore(database) as store:
        store.replace_strategy_dependencies(
            [("comment_submit", "comment-strategy", "submit", "click")]
        )
        store.upsert_gate_reasons(
            [
                {
                    "strategy_id": "comment-strategy",
                    "source": "probe",
                    "site": "tiktok",
                    "environment": "staging",
                    "reason_code": "selector_validation_failed",
                    "aliases": ["comment_submit"],
                    "selector_version_id": "sel-staging",
                    "created_by": "probe",
                }
            ]
        )
        first = store.open_or_update_alert(
            fingerprint="scope-first",
            failure_class="selector_failure",
            aliases=["comment_submit"],
            strategy_ids=["comment-strategy"],
            active_version="sel-production",
            details={},
            site="tiktok",
            environment="production",
            now=now,
        )
    client = _probe_app(database, _settings()).test_client()
    resolved = client.post(
        f"/api/selector-probe/alerts/{first['id']}/resolve",
        json={
            "reason": "production recovered",
            "expected_revision": first["revision"],
            "idempotency_key": "resolve-other-probe-scope",
        },
    )
    acknowledge_after_resolve = client.post(
        f"/api/selector-probe/alerts/{first['id']}/acknowledge",
        json={"idempotency_key": "ack-after-resolve"},
    )

    assert resolved.status_code == 200
    assert resolved.get_json()["status"] == "resolved"
    assert acknowledge_after_resolve.status_code == 400
    assert client.get(
        f"/api/selector-probe/alerts/{first['id']}"
    ).get_json()["status"] == "resolved"

    with SelectorProbeStore(database) as store:
        revision = store.gate_snapshot("comment-strategy")[0]
        store.set_manual_gate_cas(
            "comment-strategy",
            paused=True,
            expected_revision=revision,
            actor_user_id=7,
            actor_username="admin",
            reason="global manual hold",
        )
        second = store.open_or_update_alert(
            fingerprint="scope-second",
            failure_class="selector_failure",
            aliases=["comment_submit"],
            strategy_ids=["comment-strategy"],
            active_version="sel-production",
            details={},
            site="tiktok",
            environment="production",
            now=now,
        )
    blocked = client.post(
        f"/api/selector-probe/alerts/{second['id']}/resolve",
        json={
            "reason": "manual hold remains",
            "expected_revision": second["revision"],
            "idempotency_key": "resolve-manual-global",
        },
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "gate_still_active"


def test_generic_settings_and_restore_cannot_modify_probe_controlled_state(
    monkeypatch,
    tmp_path,
    admin_client,
):
    config_path = tmp_path / "hardening-config.json"
    restored_source = _settings()
    restored_source["browser"]["task_goal"] = "restored legacy value"
    restored_source["selector_probe"]["enabled"] = False
    restored_source["selector_probe"]["rollout_mode"] = "observe"
    save_settings(restored_source, config_path)
    current = _settings()
    current["selector_probe"]["rollout_mode"] = "enforce"
    save_settings(current, config_path)
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    direct = admin_client.put(
        "/api/settings",
        json={
            "selector_probe": {
                "enabled": False,
                "rollout_mode": "observe",
            }
        },
    )
    restored = admin_client.post("/api/settings/restore-latest", json={})
    compatible = admin_client.put(
        "/api/settings",
        json={"browser": {"task_goal": "legacy update still works"}},
    )
    final = load_settings(config_path)

    assert direct.status_code == 409
    assert direct.get_json()["code"] == "selector_probe_settings_managed"
    assert restored.status_code == 200
    assert (
        restored.get_json()["settings"]["browser"]["task_goal"]
        == "restored legacy value"
    )
    assert compatible.status_code == 200
    assert final["browser"]["task_goal"] == "legacy update still works"
    assert final["selector_probe"]["enabled"] is True
    assert final["selector_probe"]["rollout_mode"] == "enforce"


@pytest.mark.parametrize(
    "blocker",
    ["publication_outbox", "element_request_publishing"],
)
def test_mode_switch_is_blocked_by_all_publication_work(tmp_path, blocker):
    database = tmp_path / f"{blocker}.db"
    settings = _settings()
    client = _probe_app(database, settings).test_client()
    candidate = _ui_candidate(_candidate(client))
    candidate["rollout_mode"] = "observe"
    with SelectorProbeStore(database) as store:
        _insert_publication_blocker(store, blocker)

    response = client.patch(
        "/api/selector-probe/settings",
        json={
            "expected_revision": 0,
            "reason": "mode switch must wait",
            "idempotency_key": f"mode-blocker-{blocker}",
            "settings": candidate,
        },
    )
    replay = client.patch(
        "/api/selector-probe/settings",
        json={
            "expected_revision": 0,
            "reason": "mode switch must wait",
            "idempotency_key": f"mode-blocker-{blocker}",
            "settings": candidate,
        },
    )

    assert response.status_code == 409
    assert response.get_json()["code"] == "publication_in_progress"
    assert replay.get_json() == response.get_json()
    assert _cache_row(
        database, f"mode-blocker-{blocker}"
    )["state"] == "failed"


def test_preflight_health_requires_matching_revision_fingerprint_and_ttl(
    tmp_path,
):
    database = tmp_path / "probe.db"
    now = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
    settings = _settings()
    settings["selector_probe"].pop("profile_health")
    client = _probe_app(
        database,
        settings,
        utcnow_fn=lambda: now,
    ).test_client()
    current = _candidate(client)
    preflight = client.post(
        "/api/selector-probe/settings/preflight",
        json={
            "expected_revision": 0,
            "candidate_fingerprint": "fnv1a-ui-contract",
            "settings": _ui_candidate(current),
        },
    )
    assert preflight.status_code == 200
    healthy = client.get("/api/selector-probe/settings").get_json()
    assert [item["status"] for item in healthy["profiles"]] == [
        "healthy",
        "healthy",
    ]

    with SelectorProbeStore(database) as store:
        saved = store.management_preflight_health(
            "tiktok:production"
        )
        canonical = saved["canonical_fingerprint"]
        store.bump_resource_revision("settings")
    stale_revision = client.get(
        "/api/selector-probe/settings"
    ).get_json()
    assert [item["status"] for item in stale_revision["profiles"]] == [
        "unknown",
        "unknown",
    ]

    with SelectorProbeStore(database) as store:
        store.save_management_preflight_health(
            "tiktok:production",
            {
                "checks": saved["checks"],
                "profiles": saved["profiles"],
                "base_revision": 1,
                "canonical_fingerprint": "wrong-fingerprint",
            },
            checked_at=now.isoformat(),
        )
    wrong_fingerprint = client.get(
        "/api/selector-probe/settings"
    ).get_json()
    assert [item["status"] for item in wrong_fingerprint["profiles"]] == [
        "unknown",
        "unknown",
    ]

    with SelectorProbeStore(database) as store:
        store.save_management_preflight_health(
            "tiktok:production",
            {
                "checks": saved["checks"],
                "profiles": saved["profiles"],
                "base_revision": 1,
                "canonical_fingerprint": canonical,
            },
            checked_at=(now - timedelta(seconds=601)).isoformat(),
        )
    expired = client.get("/api/selector-probe/settings").get_json()
    assert [item["status"] for item in expired["profiles"]] == [
        "unknown",
        "unknown",
    ]


def test_redis_busy_response_exposes_valid_active_run_id():
    class BusyRedis:
        def set(self, *_args, **_kwargs):
            return False

        def get(self, _key):
            return b"active-run_20260729"

        def close(self):
            return None

    dispatcher = RedisRunDispatcher(
        redis_factory=BusyRedis,
        tick_runner=lambda **_kwargs: None,
        environment="production",
        site="tiktok",
        ttl_seconds=30,
    )

    assert dispatcher("new-run", lambda: None) == {
        "status": "busy",
        "active_run_id": "active-run_20260729",
    }


def test_backend_accepts_exact_ui_preflight_json_contract(tmp_path):
    database = tmp_path / "probe.db"
    client = _probe_app(database, _settings()).test_client()
    candidate = _ui_candidate(_candidate(client))
    body = {
        "expected_revision": 0,
        "candidate_fingerprint": "fnv1a-7d9e2c4a",
        "settings": candidate,
    }

    response = client.post(
        "/api/selector-probe/settings/preflight",
        json=body,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["base_revision"] == body["expected_revision"]
    assert (
        payload["candidate_fingerprint"]
        == body["candidate_fingerprint"]
    )
    assert payload["preflight_token"]
    assert payload["checked_at"]
    assert payload["status"] == "passed"
