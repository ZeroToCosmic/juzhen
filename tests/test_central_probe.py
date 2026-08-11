"""Central PROBE gating tests (M4 increment 2, PRD F14 transitions #13-#15)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from central import config, db
from central.models import Account, Base, SubTask, Task
from central.app import app

from tests.test_central_account_states import _status  # noqa: F401
from tests.test_central_scheduler import _tick  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate, _create_task  # noqa: F401


def _probe_tick(client, tenant):
    return client.post(
        "/api/central/scheduler/probe",
        headers={"X-Tenant-ID": tenant},
    )


def _captcha_then_verify(client, tenant, account_id):
    _activate(client, tenant, account_id)
    _status(client, tenant, account_id, "CAPTCHA")
    _status(client, tenant, account_id, "MANUAL_VERIFIED")


def test_probe_tick_creates_probe_task_after_cooldown(central_client):
    _captcha_then_verify(central_client, "tenant-a", "acc-1")
    with db.session_scope() as session:
        account = session.query(Account).one()
        account.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)

    response = _probe_tick(central_client, "tenant-a")
    assert response.status_code == 200
    assert response.json()["created"] == 1

    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.task_type == "browse"
        assert task.params["probe"] is True
        assert task.priority == "low"
        subtask = session.query(SubTask).one()
        assert subtask.status == "QUEUED"
        assert subtask.config_snapshot["params"]["probe"] is True


def test_probe_tick_skips_during_cooldown(central_client):
    _captcha_then_verify(central_client, "tenant-a", "acc-1")
    response = _probe_tick(central_client, "tenant-a")
    body = response.json()
    assert body["created"] == 0
    assert body["skipped"] == 1


def test_probe_tick_does_not_duplicate_active_probe(central_client):
    _captcha_then_verify(central_client, "tenant-a", "acc-1")
    with db.session_scope() as session:
        account = session.query(Account).one()
        account.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert _probe_tick(central_client, "tenant-a").json()["created"] == 1
    assert _probe_tick(central_client, "tenant-a").json()["created"] == 0


def test_probe_success_activates_account(central_client):
    _captcha_then_verify(central_client, "tenant-a", "acc-1")
    with db.session_scope() as session:
        account = session.query(Account).one()
        account.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    _probe_tick(central_client, "tenant-a")
    _tick(central_client, "tenant-a")

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assigned = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    response = central_client.post(
        "/api/central/subtasks/result",
        json={**assigned, "status": "SUCCESS", "msg_id": "probe-ok-1"},
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.json()["probe_resolved"] is True
    with db.session_scope() as session:
        account = session.query(Account).one()
        assert account.business_status == "ACTIVE"


def test_probe_failure_returns_to_captcha(central_client):
    _captcha_then_verify(central_client, "tenant-a", "acc-1")
    with db.session_scope() as session:
        account = session.query(Account).one()
        account.cooldown_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    _probe_tick(central_client, "tenant-a")
    _tick(central_client, "tenant-a")

    with db.session_scope() as session:
        subtask = session.query(SubTask).one()
        assigned = {
            "subtask_id": subtask.subtask_id,
            "generation": subtask.lease_generation,
            "device_id": subtask.assigned_device_id,
        }
    response = central_client.post(
        "/api/central/subtasks/result",
        json={
            **assigned,
            "status": "FAILED",
            "error_category": "account",
            "msg_id": "probe-fail-1",
        },
        headers={"X-Tenant-ID": "tenant-a"},
    )
    assert response.json()["probe_resolved"] is True
    with db.session_scope() as session:
        account = session.query(Account).one()
        assert account.business_status == "CAPTCHA"


def test_business_task_not_dispatched_while_manual_verified(central_client):
    _captcha_then_verify(central_client, "tenant-a", "acc-1")
    _create_task(central_client, "tenant-a", ["acc-1"])
    tick = _tick(central_client, "tenant-a").json()
    assert tick["dispatch"]["assigned"] == 0
    assert tick["dispatch"]["skipped"] == 1
    with db.session_scope() as session:
        account = session.query(Account).one()
        assert account.business_status == "MANUAL_VERIFIED"
