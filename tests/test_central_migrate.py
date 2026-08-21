"""Central migration tool tests (SQLite -> SQLite, logic parity for PG)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, text

from central.migrate import migrate
from central.action_models import ActionDefinition, ActionRevision
from central.models import Account, Base, Device, Tenant
from remote_actions.checksums import content_checksum, release_checksum


def _engine(tmp_path, name):
    engine = create_engine(f"sqlite:///{tmp_path / name}")
    Base.metadata.create_all(engine)
    return engine


def _seed(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO tenants (id, name, status, created_at) VALUES "
                "('tenant-a', 'Alpha', 'active', :now), ('tenant-b', 'Beta', 'active', :now)"
            ),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO devices "
                "(tenant_id, device_id, name, status, agent_version, capabilities,"
                " channel, max_accounts, used_accounts, inventory_epoch, enabled,"
                " created_at) VALUES "
                "('tenant-a', 'win-01', '', 'online', '0.1.0', '{}', 'stable',"
                " 300, 12, 3, 1, :now)"
            ),
            {"now": datetime.now(timezone.utc)},
        )
        connection.execute(
            text(
                "INSERT INTO accounts "
                "(tenant_id, account_id, profile_id, tiktok_identity, deploy_status,"
                " business_status, consecutive_failures, revision, created_at) VALUES "
                "('tenant-a', 'acc-1', 'p-1', '@alpha', 'ACTIVE', 'ACTIVE', 0, 2, :now)"
            ),
            {"now": datetime.now(timezone.utc)},
        )


def test_migrate_copies_all_tables(tmp_path):
    source = _engine(tmp_path, "source.db")
    target = _engine(tmp_path, "target.db")
    _seed(source)

    counts = migrate(source, target)

    assert counts["tenants"] == 2
    assert counts["devices"] == 1
    assert counts["accounts"] == 1

    with target.connect() as connection:
        rows = connection.execute(text("SELECT id, name FROM tenants ORDER BY id")).all()
        assert [(r[0], r[1]) for r in rows] == [("tenant-a", "Alpha"), ("tenant-b", "Beta")]
        device = connection.execute(
            text("SELECT used_accounts, inventory_epoch FROM devices WHERE device_id='win-01'")
        ).one()
        assert device[0] == 12
        assert device[1] == 3
        account = connection.execute(
            text("SELECT revision FROM accounts WHERE account_id='acc-1'")
        ).one()
        assert account[0] == 2


def test_migrate_respects_table_filter(tmp_path):
    source = _engine(tmp_path, "source.db")
    target = _engine(tmp_path, "target.db")
    _seed(source)

    counts = migrate(source, target, tables=["tenants"])

    assert counts == {"tenants": 2}
    with target.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM tenants")).scalar() == 2


def test_migrate_empty_source(tmp_path):
    source = _engine(tmp_path, "source.db")
    target = _engine(tmp_path, "target.db")

    counts = migrate(source, target)

    assert all(count == 0 for count in counts.values())
    with target.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM tenants")).scalar() == 0


def test_migrate_to_non_empty_target_is_rejected(tmp_path):
    source = _engine(tmp_path, "source.db")
    target = _engine(tmp_path, "target.db")
    _seed(source)

    first = migrate(source, target)
    assert first["tenants"] == 2

    with pytest.raises(Exception) as excinfo:
        migrate(source, target)
    assert "UNIQUE" in str(excinfo.value) or "duplicate" in str(excinfo.value).lower()


def test_migrate_preserves_immutable_action_revisions(tmp_path):
    source = _engine(tmp_path, "actions-source.db")
    target = _engine(tmp_path, "actions-target.db")
    content = {
        "executor_kind": "browser_strategy",
        "definition_schema_version": "1.0",
        "parameter_schema": {"type": "object", "additionalProperties": False},
        "result_schema": {"type": "object", "additionalProperties": True},
        "snapshot": {"nodes": []},
        "execution_defaults": {},
    }
    action_id = "act_0000000000000000000000000A"
    content_digest = content_checksum(content)
    release_digest = release_checksum(action_id, 1, content_digest)
    with source.begin() as connection:
        connection.execute(
            Tenant.__table__.insert(),
            {"id": "tenant-a", "name": "Tenant A", "status": "active"},
        )
        connection.execute(
            ActionDefinition.__table__.insert(),
            {
                "action_id": action_id,
                "tenant_id": "tenant-a",
                "executor_kind": "browser_strategy",
            },
        )
        connection.execute(
            ActionRevision.__table__.insert(),
            {
                "action_id": action_id,
                "revision": 1,
                "content_checksum": content_digest,
                "release_checksum": release_digest,
                "definition_schema_version": "1.0",
                "parameter_schema": content["parameter_schema"],
                "result_schema": content["result_schema"],
                "snapshot": content["snapshot"],
                "execution_defaults": {},
                "validation_status": "validated",
                "actor": "developer-1",
                "waiver_reason": "",
                "release_payload": {
                    **content,
                    "action_id": action_id,
                    "revision": 1,
                    "content_checksum": content_digest,
                    "release_checksum": release_digest,
                    "validation_status": "validated",
                    "actor": "developer-1",
                    "waiver_reason": "",
                },
            },
        )

    counts = migrate(source, target)

    assert counts["action_definitions"] == 1
    assert counts["action_revisions"] == 1
    with target.connect() as connection:
        copied = connection.execute(
            text("SELECT content_checksum, release_checksum FROM action_revisions")
        ).one()
        assert copied == (content_digest, release_digest)
