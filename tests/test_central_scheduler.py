"""Central lease/dispatch/handle-gate tests (M2 increment 2)."""

from __future__ import annotations

import pytest

from central import config, db
from central.models import Account, Base, Handle, SubTask, Task
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401


def _tick(client, tenant):
    return client.post(
        "/api/central/scheduler/tick", headers={"X-Tenant-ID": tenant}
    )


def _renew(client, tenant, subtask_id, device_id, generation):
    return client.post(
        "/api/central/subtasks/lease/renew",
        json={"subtask_id": subtask_id, "device_id": device_id, "generation": generation},
        headers={"X-Tenant-ID": tenant},
    )


def _handle(client, tenant, subtask_id, status="VERIFIED", content=None):
    return client.post(
        "/api/central/subtasks/handle",
        json={
            "subtask_id": subtask_id,
            "verification_status": status,
            "content": content or {"comment_id": "c-1"},
        },
        headers={"X-Tenant-ID": tenant},
    )


def _subtask_rows(tenant="tenant-a"):
    with db.session_scope() as session:
        return {
            s.subtask_id: {
                "status": s.status,
                "device": s.assigned_device_id,
                "generation": s.lease_generation,
                "attempts": s.attempts,
                "profile_id": s.profile_id,
            }
            for s in session.query(SubTask).all()
            if s.tenant_id == tenant
        }


def _first_subtask_id(tenant="tenant-a"):
    with db.session_scope() as session:
        return (
            session.query(SubTask)
            .filter(SubTask.tenant_id == tenant)
            .order_by(SubTask.id)
            .first()
            .subtask_id
        )


def test_dispatch_assigns_queued_subtask_with_lease(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    tick = _tick(central_client, "tenant-a").json()
    assert tick["dispatch"]["assigned"] == 1

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "ASSIGNED"
        assert subtask.assigned_device_id == "win-01"
        assert subtask.lease_owner == "win-01"
        assert subtask.lease_generation == 1
        assert subtask.lease_timeout_at is not None
        assert subtask.profile_id == ""


def test_lease_renew_updates_progress(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    subtask_id = _first_subtask_id()
    response = _renew(central_client, "tenant-a", subtask_id, "win-01", 1)
    assert response.status_code == 200
    assert response.json()["generation"] == 1
    assert response.json()["lease_timeout_at"] is not None


def test_lease_renew_rejects_stale_generation(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    subtask_id = _first_subtask_id()
    response = _renew(central_client, "tenant-a", subtask_id, "win-01", 999)
    assert response.status_code == 409
    assert "stale generation" in response.json()["detail"]


def test_lease_renew_rejects_wrong_device(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    subtask_id = _first_subtask_id()
    response = _renew(central_client, "tenant-a", subtask_id, "win-99", 1)
    assert response.status_code == 409


def test_reclaimer_requeues_stale_lease_with_generation_bump(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    subtask_id = _first_subtask_id()

    from datetime import datetime, timedelta, timezone

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        subtask.last_progress_at = datetime.now(timezone.utc) - timedelta(
            seconds=config.LEASE_TIMEOUT_SECONDS + 1
        )

    from central.leases import reclaim_stale

    with db.session_scope() as session:
        result = reclaim_stale(session, tenant_id="tenant-a")
    assert result == {"reclaimed": 1, "dlq": 0}
    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "QUEUED"
        assert subtask.lease_generation == 2
        assert subtask.attempts == 1
        assert subtask.lease_owner == ""


def test_reclaimer_dlq_after_retry_limit(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")

    from datetime import datetime, timedelta, timezone

    for _ in range(config.MAX_RETRY_ATTEMPTS + 1):
        with db.session_scope() as session:
            subtask = session.query(SubTask).one()
            subtask.last_progress_at = datetime.now(timezone.utc) - timedelta(
                seconds=config.LEASE_TIMEOUT_SECONDS + 1
            )
        tick = _tick(central_client, "tenant-a").json()
        assert tick["reclaim"]["reclaimed"] + tick["reclaim"]["dlq"] == 1

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assert subtask.status == "DLQ"
        assert subtask.attempts == config.MAX_RETRY_ATTEMPTS + 1


def test_handle_gate_activates_child_on_verified_parents(central_client):
    _activate(central_client, "tenant-a", "parent-acc")
    _activate(central_client, "tenant-a", "child-acc")
    _create_task(
        central_client,
        "tenant-a",
        ["parent-acc", "child-acc"],
        dependencies=[
            {
                "parent_account_id": "parent-acc",
                "child_account_id": "child-acc",
                "required_handle_schema": {"kind": "comment_id"},
            }
        ],
    )
    rows = _subtask_rows()
    parent_id = next(k for k, v in rows.items() if v["status"] == "QUEUED")
    child_id = next(k for k, v in rows.items() if v["status"] == "WAITING_DEPENDENCY")

    tick = _tick(central_client, "tenant-a").json()
    assert tick["dispatch"]["assigned"] == 1

    _handle(central_client, "tenant-a", parent_id, content={"comment_id": "c-1"})
    assert _subtask_rows()[parent_id]["status"] == "SUCCESS"

    tick = _tick(central_client, "tenant-a").json()
    assert tick["activation"]["activated"] == 1
    assert _subtask_rows()[child_id]["status"] in ("QUEUED", "ASSIGNED")


def test_handle_gate_blocks_on_unverified_parent(central_client):
    _activate(central_client, "tenant-a", "parent-acc")
    _activate(central_client, "tenant-a", "child-acc")
    _create_task(
        central_client,
        "tenant-a",
        ["parent-acc", "child-acc"],
        dependencies=[{"parent_account_id": "parent-acc", "child_account_id": "child-acc"}],
    )
    rows = _subtask_rows()
    parent_id = next(k for k, v in rows.items() if v["status"] == "QUEUED")
    child_id = next(k for k, v in rows.items() if v["status"] == "WAITING_DEPENDENCY")

    _handle(central_client, "tenant-a", parent_id, status="UNVERIFIED")
    tick = _tick(central_client, "tenant-a").json()
    assert tick["activation"]["activated"] == 0
    assert _subtask_rows()[child_id]["status"] == "WAITING_DEPENDENCY"


def test_handle_gate_fails_child_when_parent_fails(central_client):
    _activate(central_client, "tenant-a", "parent-acc")
    _activate(central_client, "tenant-a", "child-acc")
    _create_task(
        central_client,
        "tenant-a",
        ["parent-acc", "child-acc"],
        dependencies=[{"parent_account_id": "parent-acc", "child_account_id": "child-acc"}],
    )
    rows = _subtask_rows()
    child_id = next(k for k, v in rows.items() if v["status"] == "WAITING_DEPENDENCY")

    with db.session_scope() as session:
        parent = (
            session.query(SubTask)
            .filter(SubTask.status == "QUEUED")
            .one()
        )
        parent.status = "FAILED"
        parent.revision += 1

    tick = _tick(central_client, "tenant-a").json()
    assert tick["activation"]["failed"] == 1
    assert _subtask_rows()[child_id]["status"] == "FAILED"


def test_profile_exclusive_lock_blocks_second_subtask(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _activate(central_client, "tenant-a", "acc-2")
    _create_task(central_client, "tenant-a", ["acc-1", "acc-2"])

    with db.session_scope() as session:
        for account in session.query(Account).all():
            account.profile_id = "profile-shared"

    tick = _tick(central_client, "tenant-a").json()
    assert tick["dispatch"]["assigned"] == 1
    assert tick["dispatch"]["skipped"] == 1
