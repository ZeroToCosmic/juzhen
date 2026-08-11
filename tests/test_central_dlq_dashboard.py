"""Central DLQ + dashboard tests (M3 increment 1)."""

from __future__ import annotations

import pytest

from central import db
from central.models import Base, SubTask, TaskResult
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_scheduler import _tick  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401


def _send_result(central_client, assigned, **overrides):
    payload = {
        "subtask_id": assigned["subtask_id"],
        "device_id": assigned["device_id"],
        "generation": assigned["generation"],
        "status": "FAILED",
        "error_category": "account",
        "msg_id": "dlq-msg-1",
    }
    payload.update(overrides)
    return central_client.post(
        "/api/central/subtasks/result",
        json=payload,
        headers={"X-Tenant-ID": "tenant-a"},
    )


def _prepare_dlq(central_client):
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
    response = _send_result(central_client, assigned)
    assert response.json()["status"] == "DLQ"
    return assigned


def test_dlq_list_shows_manual_items(central_client):
    _prepare_dlq(central_client)
    response = central_client.get(
        "/api/central/dlq", headers={"X-Tenant-ID": "tenant-a"}
    )
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["error_category"] == "account"


def test_dlq_list_tenant_isolation(central_client):
    _prepare_dlq(central_client)
    response = central_client.get(
        "/api/central/dlq", headers={"X-Tenant-ID": "tenant-b"}
    )
    assert response.json()["count"] == 0


def test_dlq_requeue_resets_attempts(central_client):
    assigned = _prepare_dlq(central_client)
    response = central_client.post(
        f"/api/central/dlq/{assigned['subtask_id']}/requeue",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "QUEUED"
        assert subtask.attempts == 0
        assert subtask.lease_generation == 2


def test_dlq_terminate(central_client):
    assigned = _prepare_dlq(central_client)
    response = central_client.post(
        f"/api/central/dlq/{assigned['subtask_id']}/terminate",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "CANCELLED"


def test_dlq_operation_on_non_dlq_rejected(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    with db.session_scope() as session:
        subtask_id = session.query(SubTask).one().subtask_id
    response = central_client.post(
        f"/api/central/dlq/{subtask_id}/terminate",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 409


def test_dashboard_summary_counts(central_client):
    _prepare_dlq(central_client)
    response = central_client.get(
        "/api/central/dashboard/summary",
        headers={"X-Tenant-ID": "tenant-a"},
    )
    body = response.json()
    assert body["tasks_today"] == 1
    assert body["dlq"] == 1
    assert body["online_devices"] == 1
    assert body["total_devices"] == 1
    assert body["success_rate"] is None or isinstance(body["success_rate"], float)


def test_dashboard_success_rate_after_success(central_client):
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
            "status": "SUCCESS",
            "result_data": {},
            "msg_id": "ok-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    body = central_client.get(
        "/api/central/dashboard/summary",
        headers={"X-Tenant-ID": "tenant-a"},
    ).json()
    assert body["succeeded"] == 1
    assert body["success_rate"] == 1.0
