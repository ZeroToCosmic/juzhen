from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, g, request

from selector_probe.blueprint import create_selector_probe_blueprint
from selector_probe.gates import StrategyGateService
from selector_probe.store import SelectorProbeStore
from gateway.auth_blueprint import install_management_guard


def _app(
    database: Path,
    *,
    settings_provider=None,
    settings_mutator=None,
    evidence_root=None,
    run_dispatcher=None,
    store_factory=None,
) -> Flask:
    class Registry:
        def get_active(self):
            return None

        def close(self):
            return None

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "management-test-secret"

    @app.before_request
    def actor():
        g.management_user = SimpleNamespace(
            id=int(request.headers.get("X-Test-Actor", "7")),
            username="admin",
            role="administrator",
        )

    app.register_blueprint(
        create_selector_probe_blueprint(
            store_factory=store_factory
            or (lambda: SelectorProbeStore(database)),
            registry_factory=Registry,
            gate_service_factory=lambda: StrategyGateService(
                SelectorProbeStore(database)
            ),
            settings_provider=settings_provider
            or (lambda: {"selector_probe": {}}),
            settings_mutator=settings_mutator
            or (lambda mutator: mutator({"selector_probe": {}})),
            evidence_root=evidence_root or database.parent / "evidence",
            run_dispatcher=run_dispatcher
            or (
                lambda request_id, done: {
                    "status": "accepted",
                    "request_id": request_id,
                }
            ),
        )
    )
    return app


def _insert_version(store: SelectorProbeStore, version_id: str) -> None:
    now = datetime.now(UTC).isoformat()
    store.connection.execute(
        """
        INSERT INTO selector_versions (
            id, site, environment, status, bundle_json, bundle_hash,
            evidence_json, created_at, validated_at, published_at
        ) VALUES (?, 'tiktok', 'production', 'published', ?,
                  ?, '{}', ?, ?, ?)
        """,
        (
            version_id,
            (
                '{"elements":{"comment_submit":{"scope":"page",'
                '"locators":[{"type":"role","role":"button",'
                '"name":"Submit"}]}},'
                f'"version":"{version_id}"'
                "}"
            ),
            "sha256:" + "a" * 64,
            now,
            now,
            now,
        ),
    )
    store.connection.commit()


def test_management_lists_use_page_contract(tmp_path):
    database = tmp_path / "probe.db"
    with SelectorProbeStore(database):
        pass
    client = _app(database).test_client()

    response = client.get("/api/selector-probe/runs?page=1&page_size=20")

    assert response.status_code == 200
    assert response.get_json() == {
        "items": [],
        "page": 1,
        "page_size": 20,
        "revision": 0,
        "total": 0,
    }
    assert (
        client.get(
            "/api/selector-probe/runs?page=1&page_size=25"
        ).status_code
        == 400
    )


def test_rollback_creates_draft_without_publication(tmp_path):
    database = tmp_path / "probe.db"
    with SelectorProbeStore(database) as store:
        _insert_version(store, "sel-source")
    client = _app(database).test_client()

    response = client.post(
        "/api/selector-probe/versions/sel-source/rollback-validation",
        json={
            "reason": "validate known good selectors",
            "idempotency_key": "rollback-1",
        },
    )

    assert response.status_code == 202
    draft_id = response.get_json()["draft_version"]
    with SelectorProbeStore(database) as store:
        draft = store.get_version(draft_id)
        outbox_count = store.connection.execute(
            "SELECT COUNT(*) FROM publication_outbox"
        ).fetchone()[0]
    assert draft["status"] == "rollback_draft"
    assert outbox_count == 0


def test_manual_resume_preserves_probe_gate_reason(tmp_path):
    database = tmp_path / "probe.db"
    with SelectorProbeStore(database) as store:
        store.replace_strategy_dependencies(
            [
                (
                    "comment_submit",
                    "comment-strategy",
                    "submit",
                    "click",
                    "Comment",
                )
            ]
        )
        store.upsert_gate_reasons(
            [
                {
                    "strategy_id": "comment-strategy",
                    "source": "probe",
                    "site": "tiktok",
                    "environment": "production",
                    "reason_code": "selector_validation_failed",
                    "aliases": ["comment_submit"],
                    "selector_version_id": "sel-bad",
                    "created_by": "probe",
                }
            ]
        )
        revision = store.gate_snapshot("comment-strategy")[0]
    client = _app(database).test_client()
    pause = client.post(
        "/api/selector-probe/strategies/comment-strategy/pause",
        json={
            "reason": "manual investigation",
            "expected_revision": revision,
            "idempotency_key": "gate-pause-1",
        },
    )
    resume = client.post(
        "/api/selector-probe/strategies/comment-strategy/resume",
        json={
            "reason": "manual hold removed",
            "expected_revision": pause.get_json()["revision"],
            "idempotency_key": "gate-resume-1",
        },
    )

    assert pause.status_code == 200
    assert resume.status_code == 200
    assert resume.get_json()["effective_status"] == "paused"
    assert [item["source"] for item in resume.get_json()["reasons"]] == [
        "probe"
    ]


def test_settings_token_and_opaque_refs_handle_tail_collision(tmp_path):
    database = tmp_path / "probe.db"
    settings = {
        "selector_probe": {
            "enabled": True,
            "site": "tiktok",
            "environment": "production",
            "timezone": "Asia/Shanghai",
            "daily_time": "03:00",
            "target_url": "https://www.tiktok.com",
            "test_profile_ids": ["alpha-SAME", "beta-SAME"],
            "dedicated_test_profile_ids": [
                "alpha-SAME",
                "beta-SAME",
            ],
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
                "url": "https://hooks.example.test/selector-probe",
                "status": "passed",
            },
        },
    }

    def provider():
        return copy.deepcopy(settings)

    def mutator(function):
        updated = function(copy.deepcopy(settings))
        settings.clear()
        settings.update(updated)
        return copy.deepcopy(settings)

    with SelectorProbeStore(database):
        pass
    client = _app(
        database,
        settings_provider=provider,
        settings_mutator=mutator,
    ).test_client()
    current = client.get("/api/selector-probe/settings").get_json()
    assert current["profiles"][0]["profile_mask"] == "***SAME"
    assert current["profiles"][1]["profile_mask"] == "***SAME"
    assert (
        current["profiles"][0]["profile_ref"]
        != current["profiles"][1]["profile_ref"]
    )
    candidate = copy.deepcopy(current)
    candidate["rollout_mode"] = "enforce"
    saved = client.patch(
        "/api/selector-probe/settings",
        json={
            "expected_revision": 0,
            "reason": "enable enforcement",
            "idempotency_key": "settings-save-1",
            "settings": candidate,
        },
    )

    assert saved.status_code == 200
    assert saved.get_json()["rollout_mode"] == "enforce"
    assert settings["selector_probe"]["test_profile_ids"] == [
        "alpha-SAME",
        "beta-SAME",
    ]
    private_value = "super-private-redis-key-8291"
    secret_candidate = copy.deepcopy(saved.get_json())
    secret_candidate["rollout_mode"] = "publish"
    secret_update = client.patch(
        "/api/selector-probe/settings",
        json={
            "expected_revision": 1,
            "reason": "rotate Redis credential",
            "idempotency_key": "settings-secret-no-db",
            "settings": secret_candidate,
            "secrets": {"redis_password": private_value},
        },
    )
    assert secret_update.status_code == 200
    assert private_value.encode() not in database.read_bytes()


def _durable_settings_state():
    return {
        "selector_probe": {
            "enabled": False,
            "site": "tiktok",
            "environment": "production",
            "rollout_mode": "observe",
            "observe_only": True,
            "webhook": {"enabled": False},
        },
        "adspower": {},
    }


def _secret_settings_update(client, *, key, secret):
    candidate = client.get(
        "/api/selector-probe/settings"
    ).get_json()
    return {
        "expected_revision": candidate["revision"],
        "reason": "rotate Redis credential",
        "idempotency_key": key,
        "settings": candidate,
        "secrets": {"redis_password": secret},
    }


def test_settings_write_failure_consumes_revision_and_finishes_intent(
    tmp_path,
):
    database = tmp_path / "probe.db"
    state = _durable_settings_state()

    def fail_mutator(_function):
        raise OSError("atomic replacement failed")

    client = _app(
        database,
        settings_provider=lambda: copy.deepcopy(state),
        settings_mutator=fail_mutator,
    ).test_client()
    body = _secret_settings_update(
        client,
        key="settings-write-failure",
        secret="private-write-failure-8821",
    )

    response = client.patch(
        "/api/selector-probe/settings", json=body
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "code": "settings_write_failed",
        "revision": 1,
    }
    with SelectorProbeStore(database) as store:
        publication = store.connection.execute(
            """
            SELECT status, error_code
            FROM management_settings_publications
            """
        ).fetchone()
        cache = store.connection.execute(
            """
            SELECT state
            FROM management_idempotency_cache
            WHERE idempotency_key = 'settings-write-failure'
            """
        ).fetchone()
        assert store.current_revision("settings") == 1
        assert dict(publication) == {
            "status": "failed",
            "error_code": "settings_write_failed",
        }
        assert cache["state"] == "failed"
    assert b"private-write-failure-8821" not in database.read_bytes()


def test_settings_ack_failure_reconciles_after_restart_without_secret(
    tmp_path,
):
    database = tmp_path / "probe.db"
    state = _durable_settings_state()

    class AckFailStore(SelectorProbeStore):
        def complete_settings_publication(self, *_args, **_kwargs):
            raise OSError("database acknowledgement failed")

    def provider():
        return copy.deepcopy(state)

    def mutator(function):
        updated = function(copy.deepcopy(state))
        state.clear()
        state.update(updated)
        return copy.deepcopy(state)

    failing_client = _app(
        database,
        settings_provider=provider,
        settings_mutator=mutator,
        store_factory=lambda: AckFailStore(database),
    ).test_client()
    secret = "private-ack-failure-7294"
    body = _secret_settings_update(
        failing_client,
        key="settings-ack-failure",
        secret=secret,
    )

    first = failing_client.patch(
        "/api/selector-probe/settings", json=body
    )

    assert first.status_code == 202
    assert first.get_json() == {
        "code": "settings_reconcile_pending",
        "revision": 1,
    }
    with SelectorProbeStore(database) as store:
        assert store.current_revision("settings") == 1
        assert (
            store.pending_settings_publications()[0]["status"]
            == "pending"
        )
    assert secret.encode("utf-8") not in database.read_bytes()

    restarted = _app(
        database,
        settings_provider=provider,
        settings_mutator=mutator,
    ).test_client()
    replay = restarted.patch(
        "/api/selector-probe/settings", json=body
    )

    assert replay.status_code == 200
    assert replay.get_json()["revision"] == 1
    with SelectorProbeStore(database) as store:
        publication = store.connection.execute(
            """
            SELECT status
            FROM management_settings_publications
            """
        ).fetchone()
        cache = store.connection.execute(
            """
            SELECT state
            FROM management_idempotency_cache
            WHERE idempotency_key = 'settings-ack-failure'
            """
        ).fetchone()
        assert publication["status"] == "completed"
        assert cache["state"] == "completed"


def test_run_now_idempotency_and_gate_cas_are_exact(tmp_path):
    database = tmp_path / "probe.db"
    calls = []
    with SelectorProbeStore(database) as store:
        store.replace_strategy_dependencies(
            [("comment_submit", "comment-strategy", "submit", "click")]
        )
    client = _app(
        database,
        run_dispatcher=lambda request_id, done: calls.append(request_id)
        or {"status": "accepted"},
    ).test_client()

    first = client.post(
        "/api/selector-probe/run-now",
        json={"idempotency_key": "run-key-1"},
    )
    replay = client.post(
        "/api/selector-probe/run-now",
        json={"idempotency_key": "run-key-1"},
    )
    conflict = client.post(
        "/api/selector-probe/run-now",
        json={
            "idempotency_key": "run-key-1",
            "retry_of_run_id": "other-run",
        },
    )
    detail = client.get(
        f"/api/selector-probe/runs/{first.get_json()['run_id']}"
    )
    stale = client.post(
        "/api/selector-probe/strategies/comment-strategy/pause",
        json={
            "reason": "investigate",
            "expected_revision": 9,
            "idempotency_key": "gate-stale",
        },
    )

    assert first.status_code == 202
    assert replay.get_json() == first.get_json()
    assert detail.status_code == 200
    assert detail.get_json()["status"] == "queued"
    assert len(calls) == 1
    assert conflict.status_code == 409
    assert conflict.get_json()["code"] == "idempotency_conflict"
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "stale_revision"


def test_alert_ack_does_not_clear_gate_and_resolve_requires_clear(
    tmp_path,
):
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
                    "environment": "production",
                    "reason_code": "selector_validation_failed",
                    "aliases": ["comment_submit"],
                    "selector_version_id": "sel-bad",
                    "created_by": "probe",
                }
            ]
        )
        alert = store.open_or_update_alert(
            fingerprint="failure-1",
            failure_class="selector_failure",
            aliases=["comment_submit"],
            strategy_ids=["comment-strategy"],
            active_version="sel-lkg",
            details={"severity": "critical"},
            site="tiktok",
            environment="production",
            now=now,
        )
    client = _app(database).test_client()
    acknowledged = client.post(
        f"/api/selector-probe/alerts/{alert['id']}/acknowledge",
        json={"idempotency_key": "ack-1"},
    )
    blocked = client.post(
        f"/api/selector-probe/alerts/{alert['id']}/resolve",
        json={
            "reason": "recovered",
            "expected_revision": acknowledged.get_json()["revision"],
            "idempotency_key": "resolve-1",
        },
    )
    with SelectorProbeStore(database) as store:
        assert store.open_gate_reason_rows("comment-strategy")
        store.clear_gate_reasons(
            ("comment-strategy",),
            source="probe",
            cleared_by="test",
            site="tiktok",
            environment="production",
        )
    resolved = client.post(
        f"/api/selector-probe/alerts/{alert['id']}/resolve",
        json={
            "reason": "recovered",
            "expected_revision": acknowledged.get_json()["revision"],
            "idempotency_key": "resolve-2",
        },
    )

    assert acknowledged.status_code == 200
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "gate_still_active"
    assert resolved.status_code == 200
    assert resolved.get_json()["status"] == "resolved"


def test_alert_screenshot_uses_record_path_and_private_no_store(tmp_path):
    database = tmp_path / "probe.db"
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "alert-1.jpg").write_bytes(b"\xff\xd8safe\xff\xd9")
    now = datetime.now(UTC).isoformat()
    with SelectorProbeStore(database) as store:
        alert = store.open_or_update_alert(
            fingerprint="screenshot-1",
            failure_class="selector_failure",
            aliases=["comment_submit"],
            strategy_ids=[],
            active_version="sel-lkg",
            details={},
            site="tiktok",
            environment="production",
            now=now,
        )
        store.connection.execute(
            """
            UPDATE probe_alerts SET screenshot_path = 'alert-1.jpg'
            WHERE id = ?
            """,
            (alert["id"],),
        )
        store.connection.commit()
    client = _app(
        database, evidence_root=evidence
    ).test_client()

    response = client.get(
        f"/api/selector-probe/alerts/{alert['id']}/screenshot"
    )

    assert response.status_code == 200
    assert response.data == b"\xff\xd8safe\xff\xd9"
    assert response.headers["Cache-Control"] == "private, no-store"
    response.close()


def test_management_rbac_metadata_matches_mutation_policy(tmp_path):
    app = _app(tmp_path / "probe.db")
    views = app.view_functions

    assert views["selector_probe.run_now_route"].management_roles == {
        "administrator",
        "operator",
    }
    assert views[
        "selector_probe.acknowledge_alert_route"
    ].management_roles == {"administrator", "operator"}
    for endpoint in (
        "pause_strategy_route",
        "resume_strategy_route",
        "rollback_validation_route",
        "resolve_alert_route",
        "update_settings_route",
        "clear_settings_secret_route",
    ):
        assert views[
            f"selector_probe.{endpoint}"
        ].management_roles == {"administrator"}


def test_real_management_guard_enforces_admin_operator_roles(tmp_path):
    database = tmp_path / "probe.db"
    with SelectorProbeStore(database) as store:
        store.replace_strategy_dependencies(
            [("comment_submit", "comment-strategy", "submit", "click")]
        )
    app = _app(database)

    class Auth:
        def validate_session(self, payload, _now):
            return SimpleNamespace(
                id=7,
                username=str(payload["role"]),
                role=str(payload["role"]),
                must_change_password=False,
            )

    install_management_guard(app, lambda: Auth())
    client = app.test_client()
    with client.session_transaction() as session:
        session.update(
            {
                "user_id": 7,
                "role": "operator",
                "csrf_token": "csrf",
            }
        )
    forbidden = client.post(
        "/api/selector-probe/strategies/comment-strategy/pause",
        json={
            "reason": "operator cannot pause",
            "expected_revision": 1,
            "idempotency_key": "operator-pause",
        },
        headers={"X-CSRF-Token": "csrf"},
    )
    allowed = client.post(
        "/api/selector-probe/run-now",
        json={"idempotency_key": "operator-run"},
        headers={"X-CSRF-Token": "csrf"},
    )

    assert forbidden.status_code == 403
    assert forbidden.get_json()["code"] == "forbidden"
    assert allowed.status_code == 202
