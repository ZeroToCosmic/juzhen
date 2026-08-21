"""PostgreSQL acceptance tests for immutable concurrent action publication."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os

import pytest
from fastapi import Header
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from central import config, db
from central.action_models import ActionDefinition, ActionReleaseAuditEvent, ActionRevision
from central.app import app
from central.models import Base, Tenant
from central.security import ActorContext, require_actor_context
from remote_actions.checksums import content_checksum, release_checksum


POSTGRES_URL = os.getenv("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TEST_POSTGRES_URL is required for PostgreSQL acceptance",
)


def _test_actor(
    x_actor_id: str = Header(alias="X-Actor-ID"),
    x_actor_role: str = Header(alias="X-Actor-Role"),
) -> ActorContext:
    return ActorContext(x_actor_id, x_actor_role, authenticated=True)


def _payload(action_id: str, *, waived: bool = False) -> dict:
    content = {
        "executor_kind": "browser_strategy",
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
        "snapshot": {"target_url": ""},
        "execution_defaults": {},
    }
    digest = content_checksum(content)
    return {
        **content,
        "content_checksum": digest,
        "release_checksum": release_checksum(action_id, 1, digest),
        "validation_status": "waived" if waived else "validated",
        "actor": "admin-1" if waived else "developer-1",
        "waiver_reason": "incident recovery" if waived else "",
    }


@pytest.fixture(scope="module")
def postgres_client():
    if os.getenv("TEST_POSTGRES_ALLOW_RESET") != "1":
        pytest.fail("TEST_POSTGRES_ALLOW_RESET=1 is required for the dedicated test database")
    url = make_url(POSTGRES_URL)
    if url.get_backend_name() != "postgresql" or not str(url.database).endswith("_test"):
        pytest.fail("PostgreSQL acceptance requires a dedicated *_test database")

    original_url = config.CENTRAL_DB_URL
    original_engine = db._engine
    original_factory = db._session_factory
    config.CENTRAL_DB_URL = POSTGRES_URL
    db._engine = None
    db._session_factory = None
    engine = db.get_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with db.session_scope() as session:
        session.add(Tenant(id="tenant-a", name="Tenant A"))
    app.dependency_overrides[require_actor_context] = _test_actor
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_actor_context, None)
        Base.metadata.drop_all(engine)
        engine.dispose()
        config.CENTRAL_DB_URL = original_url
        db._engine = original_engine
        db._session_factory = original_factory


def _put(client: TestClient, action_id: str, payload: dict, *, admin: bool = False):
    return client.put(
        f"/api/central/actions/{action_id}/revisions/1",
        headers={
            "X-Tenant-ID": "tenant-a",
            "X-Actor-ID": "admin-1" if admin else "developer-1",
            "X-Actor-Role": "administrator" if admin else "operator",
        },
        json=payload,
    )


def test_postgresql_constraints_concurrency_and_waiver_audit(postgres_client) -> None:
    action_id = "act_00000000000000000000000020"
    payload = _payload(action_id)
    assert _put(postgres_client, action_id, payload).status_code == 200

    invalid = _payload(action_id)
    invalid["content_checksum"] = "sha256:" + ("G" * 64)
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

    concurrent_id = "act_00000000000000000000000021"
    concurrent_payload = _payload(concurrent_id)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _index: _put(postgres_client, concurrent_id, concurrent_payload),
                range(8),
            )
        )
    assert {response.status_code for response in responses} == {200}
    assert len({response.json()["record_id"] for response in responses}) == 1

    waived_id = "act_00000000000000000000000022"
    waived_payload = _payload(waived_id, waived=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(
            pool.map(
                lambda _index: _put(
                    postgres_client, waived_id, waived_payload, admin=True
                ),
                range(8),
            )
        )
    assert {response.status_code for response in responses} == {200}
    with db.session_scope() as session:
        audits = session.query(ActionReleaseAuditEvent).filter_by(
            action_id=waived_id,
            revision=1,
            event_type="validation_waived",
        ).all()
        assert len(audits) == 1
