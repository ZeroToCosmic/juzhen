"""Central control M1 skeleton tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from central import config, db
from central.models import Base, Device
from central.app import app


@pytest.fixture()
def central_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CENTRAL_DB_PATH", tmp_path / "central.db")
    db._engine = None
    db._session_factory = None
    Base.metadata.create_all(db.get_engine())
    return TestClient(app)


def test_healthz(central_client):
    response = central_client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_heartbeat_creates_online_device(central_client):
    response = central_client.post(
        "/api/central/devices/heartbeat",
        json={
            "tenant_id": "tenant-a",
            "device_id": "win-01",
            "session_id": "sess-1",
            "agent_version": "0.1.0",
            "capabilities": {"actions": ["open", "scroll", "submit"], "strategy_versions": ["1.0.0"]},
            "channel": "stable",
            "max_accounts": 300,
            "used_accounts": 12,
            "inventory_epoch": 3,
            "running_windows": 2,
            "queue_depth": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "online"
    assert body["device_id"] == "win-01"

    with db.session_scope() as session:
        device = (
            session.query(Device)
            .filter(Device.tenant_id == "tenant-a", Device.device_id == "win-01")
            .one()
        )
        assert device.status == "online"
        assert device.agent_version == "0.1.0"
        assert device.capabilities["actions"] == ["open", "scroll", "submit"]
        assert device.max_accounts == 300
        assert device.used_accounts == 12
        assert device.inventory_epoch == 3
        assert device.last_heartbeat_at is not None


def test_heartbeat_updates_existing_device(central_client):
    first = central_client.post(
        "/api/central/devices/heartbeat",
        json={"device_id": "win-01", "session_id": "sess-1", "agent_version": "0.1.0"},
    )
    assert first.status_code == 200

    second = central_client.post(
        "/api/central/devices/heartbeat",
        json={
            "device_id": "win-01",
            "session_id": "sess-2",
            "agent_version": "0.2.0",
            "used_accounts": 20,
            "inventory_epoch": 4,
        },
    )
    assert second.status_code == 200

    with db.session_scope() as session:
        devices = session.query(Device).all()
        assert len(devices) == 1
        assert devices[0].agent_version == "0.2.0"
        assert devices[0].used_accounts == 20
        assert devices[0].inventory_epoch == 4


def test_heartbeat_requires_device_id(central_client):
    response = central_client.post(
        "/api/central/devices/heartbeat",
        json={"session_id": "sess-1"},
    )
    assert response.status_code == 422


def test_tenant_isolation_fields_present(central_client):
    central_client.post(
        "/api/central/devices/heartbeat",
        json={"tenant_id": "tenant-a", "device_id": "win-01", "session_id": "s1"},
    )
    central_client.post(
        "/api/central/devices/heartbeat",
        json={"tenant_id": "tenant-b", "device_id": "win-02", "session_id": "s2"},
    )
    with db.session_scope() as session:
        tenants = {row[0] for row in session.query(Device.tenant_id).all()}
        assert tenants == {"tenant-a", "tenant-b"}
        assert session.query(Device).count() == 2


def test_device_status_marked_online_recent(central_client):
    central_client.post(
        "/api/central/devices/heartbeat",
        json={"device_id": "win-01", "session_id": "s1"},
    )
    with db.session_scope() as session:
        device = session.query(Device).one()
        assert device.status == "online"
        last = device.last_heartbeat_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last
        assert age.total_seconds() < 5
