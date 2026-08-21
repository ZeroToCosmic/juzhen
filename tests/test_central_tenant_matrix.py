"""Multi-tenant activation matrix (M4 increment 4).

Every central route must enforce tenant isolation in the data access
layer. This matrix seeds tenant-a data, then replays the same requests
from tenant-b and asserts no tenant-a data leaks. Routes without tenant
context (healthz, ws without data) are excluded explicitly.
"""

from __future__ import annotations

import pytest

from central import config, db
from central.models import Account, Base, SubTask, Task
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_scheduler import _tick  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401


def _seed_tenant_a(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assigned = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    central_client.post(
        "/api/central/subtasks/result",
        json={
            **assigned,
            "status": "FAILED",
            "error_category": "account",
            "msg_id": "iso-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    central_client.put(
        "/api/central/configs/iso.setting",
        json={"value": {"secret": "tenant-a-only"}},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    return assigned


def _cross_tenant_requests(client, assigned, subtask_id):
    headers_b = {"X-Tenant-ID": "tenant-b"}
    subtask_id = assigned["subtask_id"]
    return [
        ("GET", "/api/central/devices", headers_b, {}),
        ("GET", f"/api/central/devices/{assigned['device_id']}", headers_b, {}),
        ("PATCH", f"/api/central/devices/{assigned['device_id']}", headers_b, {"json": {"enabled": False}}),
        ("GET", "/api/central/accounts", headers_b, {}),
        ("GET", "/api/central/accounts/import/does-not-exist", headers_b, {}),
        ("GET", "/api/central/tasks", headers_b, {}),
        ("GET", "/api/central/subtasks", headers_b, {}),
        ("POST", "/api/central/scheduler/tick", headers_b, {}),
        ("GET", "/api/central/dlq", headers_b, {}),
        ("POST", f"/api/central/dlq/{subtask_id}/terminate", headers_b, {}),
        ("GET", "/api/central/dashboard/summary", headers_b, {}),
        ("GET", "/api/central/configs/iso.setting", headers_b, {}),
        ("GET", "/api/central/configs", headers_b, {}),
        ("POST", f"/api/central/subtasks/lease/renew", headers_b, {"json": {"subtask_id": subtask_id, "device_id": "x", "generation": 1}}),
        ("POST", "/api/central/scheduler/scheduled", headers_b, {}),
        ("POST", "/api/central/scheduler/probe", headers_b, {}),
    ]


def test_tenant_isolation_matrix(central_client):
    assigned = _seed_tenant_a(central_client)
    for method, path, headers, kwargs in _cross_tenant_requests(
        central_client, assigned, assigned["subtask_id"]
    ):
        response = central_client.request(method, path, headers=headers, **kwargs)
        assert response.status_code in (200, 404, 409, 405), (
            f"{method} {path}: unexpected {response.status_code}"
        )
        if response.status_code == 200:
            body = response.json()
            text = str(body)
            assert "tenant-a" not in text, f"{method} {path} leaked tenant-a"
            assert "acc-1" not in text, f"{method} {path} leaked tenant-a account"
            assert "iso.setting" not in text, f"{method} {path} leaked tenant-a config"


def test_tenant_b_cannot_read_tenant_a_config_value(central_client):
    _seed_tenant_a(central_client)
    response = central_client.get(
        "/api/central/configs/iso.setting",
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert response.status_code == 404


def test_tenant_b_cannot_mutate_tenant_a_data(central_client):
    assigned = _seed_tenant_a(central_client)
    dlq_response = central_client.post(
        f"/api/central/dlq/{assigned['subtask_id']}/terminate",
        headers={"X-Tenant-ID": "tenant-b"},
    )
    assert dlq_response.status_code == 404
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "DLQ"
