"""Central account state machine + circuit breaker tests (M4, F14/F23)."""

from __future__ import annotations

import pytest

from central import db
from central.models import Account, Base, SubTask
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_scheduler import _tick  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401


def _status(client, tenant, account_id, status):
    return client.post(
        f"/api/central/accounts/{account_id}/status",
        json={"status": status},
        headers={"X-Tenant-ID": tenant},
    )


def _active_account(client, tenant, account_id):
    _activate(client, tenant, account_id)


def test_captcha_to_manual_verified(central_client):
    _active_account(central_client, "tenant-a", "acc-1")
    assert _status(central_client, "tenant-a", "acc-1", "CAPTCHA").status_code == 200
    response = _status(central_client, "tenant-a", "acc-1", "MANUAL_VERIFIED")
    assert response.status_code == 200
    body = response.json()
    assert body["business_status"] == "MANUAL_VERIFIED"
    assert body["revision"] == 3
    with db.session_scope() as session:
        account = session.query(Account).one()
        assert account.manual_verified_at is not None


def test_manual_review_is_only_exit_from_suspended(central_client):
    _active_account(central_client, "tenant-a", "acc-1")
    _status(central_client, "tenant-a", "acc-1", "SUSPENDED")
    direct = _status(central_client, "tenant-a", "acc-1", "ACTIVE")
    assert direct.status_code == 409
    review = _status(central_client, "tenant-a", "acc-1", "MANUAL_REVIEW")
    assert review.status_code == 200
    back = _status(central_client, "tenant-a", "acc-1", "ACTIVE")
    assert back.status_code == 200


def test_invalid_transition_rejected(central_client):
    _active_account(central_client, "tenant-a", "acc-1")
    response = _status(central_client, "tenant-a", "acc-1", "PROBE")
    assert response.status_code == 409


def test_account_status_tenant_isolation(central_client):
    _active_account(central_client, "tenant-a", "acc-1")
    response = _status(central_client, "tenant-b", "acc-1", "SUSPENDED")
    assert response.status_code == 404


def _run_subtask_to_failure(central_client, error_category="account"):
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    with db.session_scope() as session:
        subtask = (
            session.query(SubTask)
            .order_by(SubTask.id.desc())
            .first()
        )
        assigned = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    return central_client.post(
        "/api/central/subtasks/result",
        json={
            **assigned,
            "status": "FAILED",
            "error_category": error_category,
            "msg_id": f"fail-{error_category}-{assigned['subtask_id'][:8]}",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )


def test_circuit_breaker_suspends_after_three_failures(central_client):
    for i in range(3):
        response = _run_subtask_to_failure(central_client, error_category=f"account-{i}")
        assert response.status_code == 200
        assert response.json()["circuit_broken"] is (i >= 2)
    with db.session_scope() as session:
        account = session.query(Account).one()
        assert account.consecutive_failures == 3
        assert account.business_status == "SUSPENDED"


def test_success_resets_failure_counter(central_client):
    _run_subtask_to_failure(central_client)
    _run_subtask_to_failure(central_client)
    _activate(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    _tick(central_client, "tenant-a")
    with db.session_scope() as session:
        subtask = (
            session.query(SubTask)
            .order_by(SubTask.id.desc())
            .first()
        )
        assigned = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    central_client.post(
        "/api/central/subtasks/result",
        json={**assigned, "status": "SUCCESS", "msg_id": "ok-2"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    with db.session_scope() as session:
        account = session.query(Account).one()
        assert account.consecutive_failures == 0
        assert account.business_status == "ACTIVE"


def test_failed_accounts_block_dispatch(central_client):
    for _ in range(3):
        _run_subtask_to_failure(central_client)
    _create_task(central_client, "tenant-a", ["acc-1"])
    tick = _tick(central_client, "tenant-a").json()
    assert tick["dispatch"]["assigned"] == 0
    assert tick["dispatch"]["skipped"] == 1
