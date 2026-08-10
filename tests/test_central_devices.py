"""Central device management tests (M1): CRUD + tenant isolation."""

from __future__ import annotations

import pytest

from central import config, db
from central.models import Base, Device
from central.app import app

from tests.test_central_skeleton import central_client  # noqa: F401


def _heartbeat(client, tenant, device_id, **overrides):
    payload = {"tenant_id": tenant, "device_id": device_id, "session_id": f"s-{device_id}"}
    payload.update(overrides)
    return client.post("/api/central/devices/heartbeat", json=payload)


def test_list_devices_is_tenant_filtered(central_client):
    _heartbeat(central_client, "tenant-a", "win-01")
    _heartbeat(central_client, "tenant-a", "win-02")
    _heartbeat(central_client, "tenant-b", "win-99")

    response = central_client.get(
        "/api/central/devices", headers={"X-Tenant-ID": "tenant-a"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    ids = {item["device_id"] for item in body["devices"]}
    assert ids == {"win-01", "win-02"}


def test_tenant_b_cannot_see_tenant_a_devices(central_client):
    _heartbeat(central_client, "tenant-a", "win-01")
    response = central_client.get(
        "/api/central/devices", headers={"X-Tenant-ID": "tenant-b"}
    )
    assert response.json()["count"] == 0


def test_tenant_b_cannot_patch_tenant_a_device(central_client):
    _heartbeat(central_client, "tenant-a", "win-01")
    response = central_client.patch(
        "/api/central/devices/win-01",
        json={"enabled": False},
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert response.status_code == 404


def test_patch_device_fields(central_client):
    _heartbeat(central_client, "tenant-a", "win-01", channel="stable", max_accounts=300)
    response = central_client.patch(
        "/api/central/devices/win-01",
        json={"enabled": False, "channel": "canary", "name": "Worker 01"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["channel"] == "canary"
    assert body["name"] == "Worker 01"


def test_patch_device_rejects_invalid_channel(central_client):
    _heartbeat(central_client, "tenant-a", "win-01")
    response = central_client.patch(
        "/api/central/devices/win-01",
        json={"channel": "bogus"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 422


def test_offline_report(central_client):
    _heartbeat(central_client, "tenant-a", "win-01")
    response = central_client.post(
        "/api/central/devices/win-01/offline",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "offline"


def test_device_view_marks_stale_heartbeat_offline(central_client):
    _heartbeat(central_client, "tenant-a", "win-01")
    with db.session_scope() as session:
        device = session.query(Device).one()
        device.last_heartbeat_at = None
    response = central_client.get(
        "/api/central/devices/win-01", headers={"X-Tenant-ID": "tenant-a"}
    )
    assert response.json()["status"] == "offline"


def test_permission_matrix():
    from central.permissions import has_permission

    assert has_permission("administrator", "tenant:manage") is True
    assert has_permission("operator", "tenant:manage") is False
    assert has_permission("operator", "task:cancel") is True
    assert has_permission("viewer", "device:view") is True
    assert has_permission("viewer", "account:manage") is False
