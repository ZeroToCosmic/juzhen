"""Central immutable action revision API tests."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from central import config, db
from central.action_models import ActionDefinition, ActionReleaseAuditEvent, ActionRevision
from central.app import app
from central.models import Base, Tenant
from central.security import ActorContext, require_actor_context
from remote_actions.checksums import content_checksum, release_checksum


TENANT_HEADERS = {
    "X-Tenant-ID": "tenant-a",
    "X-Actor-ID": "developer-1",
    "X-Actor-Role": "operator",
}


def _test_actor_context(
    x_actor_id: str = Header(alias="X-Actor-ID"),
    x_actor_role: str = Header(alias="X-Actor-Role"),
) -> ActorContext:
    return ActorContext(x_actor_id, x_actor_role, authenticated=True)


@pytest.fixture()
def central_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CENTRAL_DB_PATH", tmp_path / "central-actions.db")
    db._engine = None
    db._session_factory = None
    Base.metadata.create_all(db.get_engine())
    with db.session_scope() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A"))
        session.add(Tenant(id="tenant-b", name="Tenant B"))
    app.dependency_overrides[require_actor_context] = _test_actor_context
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_actor_context, None)


def _payload(
    action_id: str = "act_00000000000000000000000001",
    revision: int = 1,
    *,
    executor_kind: str = "browser_strategy",
) -> dict:
    content = {
        "executor_kind": executor_kind,
        "definition_schema_version": "1.0",
        "parameter_schema": {
            "type": "object",
            "properties": {"target_url": {"type": "string", "format": "https-url"}},
            "required": ["target_url"],
            "additionalProperties": False,
            "bindings": {
                "target_url": {"pointer": "/target_url", "type": "string"}
            },
        },
        "result_schema": {"type": "object", "additionalProperties": True},
        "snapshot": {
            "target_url": "",
            "nodes": [{"id": "open", "type": "open"}],
        },
        "execution_defaults": {"timeout_seconds": 30},
    }
    digest = content_checksum(content)
    return {
        **content,
        "content_checksum": digest,
        "release_checksum": release_checksum(action_id, revision, digest),
        "validation_status": "validated",
        "actor": "developer-1",
        "waiver_reason": "",
    }


def _put(client: TestClient, action_id: str, revision: int, payload: dict):
    return client.put(
        f"/api/central/actions/{action_id}/revisions/{revision}",
        headers=TENANT_HEADERS,
        json=payload,
    )


def test_same_release_is_idempotent_and_different_payload_conflicts(central_client) -> None:
    action_id = "act_00000000000000000000000001"
    payload = _payload(action_id)
    first = _put(central_client, action_id, 1, payload)
    second = _put(central_client, action_id, 1, payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["record_id"] == second.json()["record_id"]

    changed = copy.deepcopy(payload)
    changed["snapshot"] = {
        "target_url": "",
        "nodes": [{"id": "changed", "type": "open"}],
    }
    changed["content_checksum"] = content_checksum(changed)
    changed["release_checksum"] = release_checksum(
        action_id, 1, changed["content_checksum"]
    )
    conflict = _put(central_client, action_id, 1, changed)
    assert conflict.status_code == 409


def test_published_revision_cannot_be_updated_or_deleted(central_client) -> None:
    action_id = "act_00000000000000000000000002"
    assert _put(central_client, action_id, 1, _payload(action_id)).status_code == 200
    assert central_client.patch(
        f"/api/central/actions/{action_id}/revisions/1",
        headers=TENANT_HEADERS,
        json={"snapshot": {}},
    ).status_code == 405
    assert central_client.delete(
        f"/api/central/actions/{action_id}/revisions/1",
        headers=TENANT_HEADERS,
    ).status_code == 405


def test_action_ids_are_unique_across_executor_kinds(central_client) -> None:
    action_id = "act_00000000000000000000000003"
    assert _put(
        central_client, action_id, 1, _payload(action_id, executor_kind="browser_strategy")
    ).status_code == 200
    conflict = _put(
        central_client,
        action_id,
        2,
        _payload(action_id, 2, executor_kind="comment_campaign"),
    )
    assert conflict.status_code == 409


def test_release_checksum_and_payload_are_verified(central_client) -> None:
    action_id = "act_00000000000000000000000004"
    payload = _payload(action_id)
    payload["release_checksum"] = "sha256:" + "0" * 64
    assert _put(central_client, action_id, 1, payload).status_code == 422

    invalid_ulid = "act_" + ("Z" * 26)
    assert _put(central_client, invalid_ulid, 1, _payload()).status_code == 422


def test_get_revision_is_tenant_scoped(central_client) -> None:
    action_id = "act_00000000000000000000000005"
    assert _put(central_client, action_id, 1, _payload(action_id)).status_code == 200
    own = central_client.get(
        f"/api/central/actions/{action_id}/revisions/1", headers=TENANT_HEADERS
    )
    assert own.status_code == 200
    assert own.json()["release_checksum"] == _payload(action_id)["release_checksum"]
    other = central_client.get(
        f"/api/central/actions/{action_id}/revisions/1",
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert other.status_code == 404


def test_sync_inventory_returns_missing_and_checksum_mismatch(central_client) -> None:
    action_a = "act_00000000000000000000000006"
    action_b = "act_00000000000000000000000007"
    action_c = "act_00000000000000000000000008"
    payload_a = _payload(action_a)
    payload_b = _payload(action_b)
    assert _put(central_client, action_a, 1, payload_a).status_code == 200
    assert _put(central_client, action_b, 1, payload_b).status_code == 200

    response = central_client.post(
        "/api/central/actions/sync/compare",
        headers=TENANT_HEADERS,
        json={
            "inventory": [
                {
                    "action_id": action_a,
                    "revision": 1,
                    "release_checksum": payload_a["release_checksum"],
                },
                {
                    "action_id": action_b,
                    "revision": 1,
                    "release_checksum": "sha256:" + "f" * 64,
                },
                {
                    "action_id": action_c,
                    "revision": 1,
                    "release_checksum": _payload(action_c)["release_checksum"],
                },
            ]
        },
    )
    assert response.status_code == 200
    states = {
        (item["action_id"], item["revision"]): item["state"]
        for item in response.json()["items"]
    }
    assert states == {
        (action_a, 1): "in_sync",
        (action_b, 1): "checksum_mismatch",
        (action_c, 1): "missing_remote",
    }

    response = central_client.post(
        "/api/central/actions/sync/compare",
        headers=TENANT_HEADERS,
        json={"inventory": []},
    )
    assert {
        (item["action_id"], item["revision"], item["state"])
        for item in response.json()["items"]
    } == {(action_a, 1, "missing_local"), (action_b, 1, "missing_local")}


def test_action_models_store_full_prefixed_checksums(central_client) -> None:
    action_id = "act_00000000000000000000000009"
    payload = _payload(action_id)
    assert _put(central_client, action_id, 1, payload).status_code == 200
    with db.session_scope() as session:
        definition = session.get(ActionDefinition, action_id)
        revision = session.query(ActionRevision).one()
        assert definition.executor_kind == "browser_strategy"
        assert len(revision.content_checksum) == 71
        assert revision.release_checksum == payload["release_checksum"]


def test_concurrent_same_release_is_idempotent(central_client) -> None:
    action_id = "act_0000000000000000000000000B"
    payload = _payload(action_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(lambda _index: _put(central_client, action_id, 1, payload), range(8))
        )
    assert {response.status_code for response in responses} == {200}
    assert len({response.json()["record_id"] for response in responses}) == 1


def test_waiver_requires_admin_context_and_creates_audit_event(central_client) -> None:
    action_id = "act_0000000000000000000000000C"
    payload = _payload(action_id)
    payload.update(
        validation_status="waived",
        actor="admin-1",
        waiver_reason="incident recovery",
    )
    forbidden = central_client.put(
        f"/api/central/actions/{action_id}/revisions/1",
        headers={**TENANT_HEADERS, "X-Actor-ID": "admin-1"},
        json=payload,
    )
    assert forbidden.status_code == 403

    response = central_client.put(
        f"/api/central/actions/{action_id}/revisions/1",
        headers={
            **TENANT_HEADERS,
            "X-Actor-ID": "admin-1",
            "X-Actor-Role": "administrator",
        },
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["waiver_reason"] == "incident recovery"
    with db.session_scope() as session:
        audit = session.query(ActionReleaseAuditEvent).one()
        assert audit.actor == "admin-1"
        assert audit.actor_role == "administrator"
        assert audit.reason == "incident recovery"


def test_release_actor_must_match_trusted_request_context(central_client) -> None:
    action_id = "act_0000000000000000000000000D"
    response = central_client.put(
        f"/api/central/actions/{action_id}/revisions/1",
        headers={**TENANT_HEADERS, "X-Actor-ID": "someone-else"},
        json=_payload(action_id),
    )
    assert response.status_code == 403


def test_actor_headers_cannot_bypass_missing_authentication_middleware(
    central_client,
) -> None:
    override = app.dependency_overrides.pop(require_actor_context)
    try:
        action_id = "act_0000000000000000000000000G"
        payload = _payload(action_id)
        payload.update(
            actor="admin-1",
            validation_status="waived",
            waiver_reason="forged",
        )
        response = central_client.put(
            f"/api/central/actions/{action_id}/revisions/1",
            headers={
                "X-Tenant-ID": "tenant-a",
                "X-Actor-ID": "admin-1",
                "X-Actor-Role": "administrator",
            },
            json=payload,
        )
    finally:
        app.dependency_overrides[require_actor_context] = override
    assert response.status_code == 401


def test_database_rejects_invalid_checksums_and_revision_mutation(central_client) -> None:
    action_id = "act_0000000000000000000000000E"
    payload = _payload(action_id)
    assert _put(central_client, action_id, 1, payload).status_code == 200

    invalid = _payload(action_id, 2)
    invalid["content_checksum"] = "sha256:" + "G" * 64
    invalid["actor"] = "developer-1"
    with pytest.raises(IntegrityError):
        with db.get_engine().begin() as connection:
            connection.execute(
                ActionRevision.__table__.insert(),
                {
                    "action_id": action_id,
                    "revision": 2,
                    "content_checksum": invalid["content_checksum"],
                    "release_checksum": invalid["release_checksum"],
                    "definition_schema_version": invalid["definition_schema_version"],
                    "parameter_schema": invalid["parameter_schema"],
                    "result_schema": invalid["result_schema"],
                    "snapshot": invalid["snapshot"],
                    "execution_defaults": invalid["execution_defaults"],
                    "validation_status": "validated",
                    "actor": "developer-1",
                    "waiver_reason": "",
                    "release_payload": invalid,
                },
            )

    with pytest.raises(IntegrityError):
        with db.get_engine().begin() as connection:
            connection.execute(
                ActionDefinition.__table__.insert(),
                {
                    "action_id": "act_" + ("Z" * 26),
                    "tenant_id": "tenant-a",
                    "executor_kind": "browser_strategy",
                },
            )

    for statement in (
        "UPDATE action_revisions SET actor = 'tampered' WHERE action_id = :action_id",
        "DELETE FROM action_revisions WHERE action_id = :action_id",
    ):
        with pytest.raises(IntegrityError, match="immutable"):
            with db.get_engine().begin() as connection:
                connection.execute(text(statement), {"action_id": action_id})


def test_central_rejects_release_that_cannot_fit_a_work_order(central_client) -> None:
    action_id = "act_0000000000000000000000000F"
    payload = _payload(action_id)
    payload["snapshot"] = {"data": "x" * (512 * 1024)}
    payload["content_checksum"] = content_checksum(payload)
    payload["release_checksum"] = release_checksum(
        action_id, 1, payload["content_checksum"]
    )
    assert _put(central_client, action_id, 1, payload).status_code == 422

    action_id = "act_0000000000000000000000000J"
    payload = _payload(action_id)
    payload["result_schema"] = {"type": "not-a-json-schema-type"}
    payload["content_checksum"] = content_checksum(payload)
    payload["release_checksum"] = release_checksum(
        action_id, 1, payload["content_checksum"]
    )
    assert _put(central_client, action_id, 1, payload).status_code == 422


def test_central_rejects_unbindable_or_invalid_release_schemas(central_client) -> None:
    action_id = "act_0000000000000000000000000H"
    payload = _payload(action_id)
    payload["parameter_schema"].pop("bindings")
    payload["content_checksum"] = content_checksum(payload)
    payload["release_checksum"] = release_checksum(
        action_id, 1, payload["content_checksum"]
    )
    assert _put(central_client, action_id, 1, payload).status_code == 422


def test_init_db_upgrades_legacy_action_definition_guards(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config, "CENTRAL_DB_PATH", tmp_path / "legacy-actions.db")
    monkeypatch.setattr(config, "CENTRAL_DB_URL", "sqlite:///legacy-actions.db")
    db._engine = None
    db._session_factory = None
    engine = db.get_engine()
    Tenant.__table__.create(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE action_definitions ("
            "action_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, "
            "executor_kind TEXT NOT NULL, created_at DATETIME NOT NULL)"
        )
        connection.execute(
            Tenant.__table__.insert(),
            {"id": "tenant-a", "name": "Tenant A", "status": "active"},
        )

    db.init_db()
    for action_id in ("act_" + ("Z" * 26), None):
        with pytest.raises(IntegrityError, match="invalid action_id"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO action_definitions"
                        "(action_id, tenant_id, executor_kind, created_at) "
                        "VALUES (:action_id, 'tenant-a', 'browser_strategy', CURRENT_TIMESTAMP)"
                    ),
                    {"action_id": action_id},
                )
    engine.dispose()
    db._engine = None
    db._session_factory = None
