"""Agent-Central integration tests (M2 increments 3+4)."""

from __future__ import annotations

import threading

import pytest

from central import config, db
from central.models import Account, Base, SubTask, TaskResult
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401
from tests.test_central_scheduler import _tick  # noqa: F401


def _prepare_assigned(central_client, tenant="tenant-a", account="acc-1", task_accounts=None):
    _activate(central_client, tenant, account)
    _create_task(central_client, tenant, task_accounts or [account])
    _tick(central_client, tenant)
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        return {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }


def test_agent_pull_returns_assigned_subtasks(central_client):
    assigned = _prepare_assigned(central_client)
    response = central_client.get(
        "/api/central/agent/subtasks",
        params={"device_id": assigned["device_id"]},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    body = response.json()
    assert body["count"] == 1
    item = body["subtasks"][0]
    assert item["subtask_id"] == assigned["subtask_id"]
    assert item["lease_generation"] == 1
    assert "config_snapshot" in item


def test_agent_pull_excludes_other_devices(central_client):
    assigned = _prepare_assigned(central_client)
    response = central_client.get(
        "/api/central/agent/subtasks",
        params={"device_id": "other-device"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.json()["count"] == 0


def test_result_success_marks_subtask_success(central_client):
    assigned = _prepare_assigned(central_client)
    response = central_client.post(
        "/api/central/subtasks/result",
        json={
            "subtask_id": assigned["subtask_id"],
            "device_id": assigned["device_id"],
            "generation": assigned["generation"],
            "status": "SUCCESS",
            "result_data": {"comment_id": "c-1"},
            "duration_ms": 120,
            "msg_id": "msg-success-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "SUCCESS"
        result = session.query(TaskResult).one()
        assert result.status == "SUCCESS"
        assert result.duration_ms == 120


def test_result_rejects_stale_generation(central_client):
    assigned = _prepare_assigned(central_client)
    response = central_client.post(
        "/api/central/subtasks/result",
        json={
            "subtask_id": assigned["subtask_id"],
            "device_id": assigned["device_id"],
            "generation": assigned["generation"] + 5,
            "status": "SUCCESS",
            "msg_id": "msg-stale-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 409
    assert "stale generation" in response.json()["detail"]
    with db.session_scope() as session:
        assert session.query(TaskResult).count() == 0


def test_result_duplicate_message_rejected(central_client):
    assigned = _prepare_assigned(central_client)
    payload = {
        "subtask_id": assigned["subtask_id"],
        "device_id": assigned["device_id"],
        "generation": assigned["generation"],
        "status": "SUCCESS",
        "msg_id": "msg-dup-1",
    }
    first = central_client.post(
        "/api/central/subtasks/result", json=payload, headers={"X-Tenant-ID": "tenant-a"}
    )
    assert first.status_code == 200
    second = central_client.post(
        "/api/central/subtasks/result", json=payload, headers={"X-Tenant-ID": "tenant-a"}
    )
    assert second.status_code == 409
    assert "duplicate" in second.json()["detail"]


def test_result_retryable_failure_requeues(central_client):
    assigned = _prepare_assigned(central_client)
    response = central_client.post(
        "/api/central/subtasks/result",
        json={
            "subtask_id": assigned["subtask_id"],
            "device_id": assigned["device_id"],
            "generation": assigned["generation"],
            "status": "FAILED",
            "error_category": "retryable",
            "msg_id": "msg-fail-retry-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "QUEUED"
        assert subtask.lease_generation == 2
        assert subtask.attempts == 1
        assert subtask.lease_owner == ""


def test_result_non_retryable_failure_goes_dlq(central_client):
    assigned = _prepare_assigned(central_client)
    response = central_client.post(
        "/api/central/subtasks/result",
        json={
            "subtask_id": assigned["subtask_id"],
            "device_id": assigned["device_id"],
            "generation": assigned["generation"],
            "status": "FAILED",
            "error_category": "account",
            "msg_id": "msg-fail-account-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.json()["status"] == "DLQ"


def test_agent_worker_end_to_end(central_client, tmp_path, monkeypatch):
    from agent.client import CentralClient
    from agent.protocol import StubExecutor
    from agent.worker import AgentWorker

    monkeypatch.setattr(config, "CENTRAL_DB_PATH", tmp_path / "central.db")
    db._engine = None
    db._session_factory = None
    Base.metadata.create_all(db.get_engine())

    _register_device(central_client, "tenant-a", "win-01")
    _import(central_client, "tenant-a", [{"account_id": "acc-1"}])
    with db.session_scope() as session:
        account = (
            session.query(Account)
            .filter(Account.account_id == "acc-1")
            .one()
        )
        account.deploy_status = "ACTIVE"
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "ASSIGNED"

    import agent.config as agent_config

    monkeypatch.setattr(agent_config, "CENTRAL_BASE_URL", "http://testserver")
    client = CentralClient(
        base_url="http://testserver",
        tenant_id="tenant-a",
        device_id="win-01",
        session_id="sess-1",
    )
    executor = StubExecutor()
    worker = AgentWorker(client, executor, stop_event=threading.Event())

    from fastapi.testclient import TestClient as FastTestClient

    with FastTestClient(app) as test_client:
        original_post = client._post
        original_get = client._get

        def fake_post(path, payload):
            return test_client.post(
                path,
                json=payload,
                headers={"X-Tenant-ID": "tenant-a"},
            ).json()

        def fake_get(path, params=None):
            return test_client.get(
                path,
                params=params or {},
                headers={"X-Tenant-ID": "tenant-a"},
            ).json()

        client._post = fake_post
        client._get = fake_get
        result = worker.run_once()
        assert result["pulled"] == 1
        assert result["processed"] == 1

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "SUCCESS"
        assert session.query(TaskResult).count() == 1
        result_row = session.query(TaskResult).one()
        assert result_row.status == "SUCCESS"
        assert result_row.result_data.get("stub") is True
