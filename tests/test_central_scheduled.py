"""Central scheduled task tests (M3 increment 4, PRD F9a / transition #22)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from central import db
from central.models import Base, Task
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401
from tests.test_central_tasks import _activate  # noqa: F401


def _iso(dt):
    return dt.isoformat()


def _create_scheduled(client, tenant, account_ids, schedule=None, deadline="", **overrides):
    payload = {
        "task_type": "comment",
        "params": {"text": "hi"},
        "account_ids": account_ids,
        "strategy_version": "1.0.0",
        "priority": "medium",
    }
    payload.update(overrides)
    if schedule is not None:
        payload["schedule"] = schedule
    if deadline:
        payload["deadline"] = deadline
    return client.post(
        "/api/central/tasks",
        json=payload,
        headers={"X-Tenant-ID": tenant},
    )


def _scheduled_tick(client, tenant):
    return client.post(
        "/api/central/scheduler/scheduled",
        headers={"X-Tenant-ID": tenant},
    )


def test_scheduled_task_created_pending(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    future = _iso(datetime.now(timezone.utc) + timedelta(hours=1))
    response = _create_scheduled(
        central_client, "tenant-a", ["acc-1"], schedule={"run_at": future}
    )
    assert response.status_code == 200
    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == "PENDING"
        assert task.schedule["run_at"] == future


def test_scheduled_task_starts_when_due(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    _create_scheduled(
        central_client, "tenant-a", ["acc-1"], schedule={"run_at": past}
    )
    tick = _scheduled_tick(central_client, "tenant-a").json()
    assert tick["started"] == 1
    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == "QUEUED"


def test_scheduled_task_overrun_defaults_to_missed(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    long_past = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    _create_scheduled(
        central_client, "tenant-a", ["acc-1"], schedule={"run_at": long_past}
    )
    tick = _scheduled_tick(central_client, "tenant-a").json()
    assert tick["missed"] == 1
    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == "MISSED"


def test_scheduled_task_overrun_immediate_runs_late(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    long_past = _iso(datetime.now(timezone.utc) - timedelta(minutes=30))
    _create_scheduled(
        central_client,
        "tenant-a",
        ["acc-1"],
        schedule={"run_at": long_past, "missed_policy": "immediate"},
    )
    tick = _scheduled_tick(central_client, "tenant-a").json()
    assert tick["started"] == 1
    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == "QUEUED"


def test_deadline_makes_task_missed(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=5))
    _create_scheduled(central_client, "tenant-a", ["acc-1"], deadline=past)
    tick = _scheduled_tick(central_client, "tenant-a").json()
    assert tick["missed"] == 1
    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == "MISSED"


def test_future_deadline_keeps_pending(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    future = _iso(datetime.now(timezone.utc) + timedelta(hours=2))
    _create_scheduled(central_client, "tenant-a", ["acc-1"], deadline=future)
    tick = _scheduled_tick(central_client, "tenant-a").json()
    assert tick["started"] == 0
    assert tick["missed"] == 0
    with db.session_scope() as session:
        assert session.query(Task).one().status == "PENDING"


def test_scheduled_tick_is_idempotent(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    past = _iso(datetime.now(timezone.utc) - timedelta(minutes=1))
    _create_scheduled(central_client, "tenant-a", ["acc-1"], schedule={"run_at": past})
    first = _scheduled_tick(central_client, "tenant-a").json()
    assert first["started"] == 1
    second = _scheduled_tick(central_client, "tenant-a").json()
    assert second["started"] == 0


def test_invalid_missed_policy_rejected(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    response = _create_scheduled(
        central_client,
        "tenant-a",
        ["acc-1"],
        schedule={"run_at": _iso(datetime.now(timezone.utc)), "missed_policy": "bogus"},
    )
    assert response.status_code == 400


def test_invalid_deadline_rejected(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    response = _create_scheduled(central_client, "tenant-a", ["acc-1"], deadline="not-a-date")
    assert response.status_code == 400
