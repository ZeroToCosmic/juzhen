"""Chaos protocol tests (PRD 15.4 acceptance 18, protocol level).

These verify the system converges after being "broken": lease tearing
(stale generations rejected end to end), duplicate/late replays, double
execution protection, and agent crash recovery via WAL + lease
alignment. No real AdsPower involved; the full central API is used.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from central import config, db
from central.models import Account, Base, SubTask, TaskResult
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_scheduler import _tick  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401


def _prepare_assigned(central_client, tenant="tenant-a", account="acc-1"):
    _activate(central_client, tenant, account)
    _create_task(central_client, tenant, [account])
    _tick(central_client, tenant)
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        return {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }


def _force_stale(central_client, tenant="tenant-a"):
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        subtask.last_progress_at = datetime.now(timezone.utc) - timedelta(
            seconds=config.LEASE_TIMEOUT_SECONDS + 1
        )
    _tick(central_client, tenant)


def _result(client, assigned, tenant="tenant-a", **overrides):
    payload = {
        "subtask_id": assigned["subtask_id"],
        "device_id": assigned["device_id"],
        "generation": assigned["generation"],
        "status": "SUCCESS",
        "msg_id": f"chaos-{assigned['subtask_id'][:8]}",
    }
    payload.update(overrides)
    return client.post(
        "/api/central/subtasks/result",
        json=payload,
        headers={"X-Tenant-ID": tenant},
    )


def test_lease_tear_old_result_rejected_after_reclaim(central_client):
    assigned = _prepare_assigned(central_client)
    _force_stale(central_client)
    stale = _result(central_client, assigned)
    assert stale.status_code == 409
    assert "stale generation" in stale.json()["detail"]
    with db.session_scope() as session:
        assert session.query(TaskResult).count() == 0


def test_lease_tear_old_renewal_rejected_after_reclaim(central_client):
    assigned = _prepare_assigned(central_client)
    _force_stale(central_client)
    renewal = central_client.post(
        "/api/central/subtasks/lease/renew",
        json={
            "subtask_id": assigned["subtask_id"],
            "device_id": assigned["device_id"],
            "generation": assigned["generation"],
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert renewal.status_code == 409


def test_new_generation_succeeds_after_reclaim(central_client):
    assigned = _prepare_assigned(central_client)
    _force_stale(central_client)
    _tick(central_client, "tenant-a")
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        fresh = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    assert fresh["generation"] > assigned["generation"]
    response = _result(central_client, fresh)
    assert response.status_code == 200
    assert response.json()["status"] == "SUCCESS"


def test_double_execution_protection(central_client):
    assigned = _prepare_assigned(central_client)
    _force_stale(central_client)
    stale = _result(central_client, assigned)
    assert stale.status_code == 409
    _tick(central_client, "tenant-a")
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        fresh = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    late = _result(central_client, assigned)
    assert late.status_code == 409
    ok = _result(central_client, fresh)
    assert ok.status_code == 200
    with db.session_scope() as session:
        assert session.query(TaskResult).count() == 1


def test_duplicate_message_replay_rejected(central_client):
    assigned = _prepare_assigned(central_client)
    first = _result(central_client, assigned)
    assert first.status_code == 200
    replay = _result(central_client, assigned)
    assert replay.status_code == 409
    assert "duplicate" in replay.json()["detail"]


def test_late_result_after_success_rejected(central_client):
    assigned = _prepare_assigned(central_client)
    first = _result(central_client, assigned)
    assert first.status_code == 200
    late = _result(
        central_client,
        assigned,
        msg_id="chaos-late-1",
        result_data={"late": True},
    )
    assert late.status_code == 409


def test_wal_recovery_after_crash(tmp_path):
    from agent.wal import STAGE_SUBMITTING, STAGE_VERIFYING, WindowWal

    wal = WindowWal(tmp_path / "wal.json")
    wal.set_stage("st-1", STAGE_SUBMITTING)
    wal.set_stage("st-2", STAGE_VERIFYING)
    wal.set_stage("st-3", "RUNNING")
    decisions = {d["subtask_id"]: d for d in wal.recover()}
    assert decisions["st-1"]["action"] == "unverified"
    assert decisions["st-1"]["retryable"] is False
    assert decisions["st-2"]["action"] == "unverified"
    assert decisions["st-3"]["action"] == "aborted"
    assert decisions["st-3"]["retryable"] is True


def test_agent_restart_lease_alignment(central_client, tmp_path, monkeypatch):
    """After a worker restart the new instance re-pulls assigned work and
    renews with the current generation (lease alignment, PRD 15.2 #1)."""
    assigned = _prepare_assigned(central_client)
    from agent.client import CentralClient
    from agent.protocol import StubExecutor
    from agent.worker import AgentWorker

    import agent.config as agent_config

    monkeypatch.setattr(agent_config, "CENTRAL_BASE_URL", "http://testserver")
    client = CentralClient(
        base_url="http://testserver",
        tenant_id="tenant-a",
        device_id="win-01",
        session_id="sess-restart",
    )
    executor = StubExecutor()
    worker = AgentWorker(client, executor)

    from fastapi.testclient import TestClient as FastTestClient

    with FastTestClient(app) as test_client:
        original_post = client._post
        original_get = client._get

        def fake_post(path, payload):
            return test_client.post(
                path, json=payload, headers={"X-Tenant-ID": "tenant-a"}
            ).json()

        def fake_get(path, params=None):
            return test_client.get(
                path, params=params or {}, headers={"X-Tenant-ID": "tenant-a"}
            ).json()

        client._post = fake_post
        client._get = fake_get
        result = worker.run_once()
        assert result["pulled"] == 1

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "SUCCESS"
