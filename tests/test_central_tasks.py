"""Central task/DAG tests (M2 increment 1, PRD F3/F9/F10)."""

from __future__ import annotations

import pytest

from central import config, db
from central.models import Account, Base, DependencyEdge, SubTask, Task
from central.outbox import OutboxMessage
from central.app import app

from tests.test_central_accounts import _register_device, _import  # noqa: F401
from tests.test_central_skeleton import central_client  # noqa: F401


def _activate(client, tenant, account_id):
    _register_device(client, tenant, "win-01")
    _import(client, tenant, [{"account_id": account_id}])
    with db.session_scope() as session:
        account = (
            session.query(Account)
            .filter(Account.account_id == account_id)
            .one()
        )
        account.deploy_status = "ACTIVE"


def _create_task(client, tenant, account_ids, **overrides):
    payload = {
        "task_type": "comment",
        "params": {"text": "hello"},
        "account_ids": account_ids,
        "strategy_version": "1.0.0",
        "priority": "medium",
    }
    payload.update(overrides)
    return client.post(
        "/api/central/tasks",
        json=payload,
        headers={"X-Tenant-ID": tenant},
    )


def test_create_task_creates_queued_subtasks(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    _activate(central_client, "tenant-a", "acc-2")
    response = _create_task(central_client, "tenant-a", ["acc-1", "acc-2"])
    assert response.status_code == 200
    body = response.json()
    assert body["subtask_count"] == 2
    assert body["dependency_edge_count"] == 0

    with db.session_scope() as session:
        task = session.query(Task).one()
        assert task.status == "QUEUED"
        assert task.config_snapshot["strategy_version"] == "1.0.0"
        subtasks = session.query(SubTask).all()
        assert len(subtasks) == 2
        assert all(s.status == "QUEUED" for s in subtasks)
        created = (
            session.query(OutboxMessage)
            .filter(OutboxMessage.subject == "tenant-a/task.created")
            .count()
        )
        assert created == 1


def test_create_task_with_dag_dependency(central_client):
    _activate(central_client, "tenant-a", "parent-acc")
    _activate(central_client, "tenant-a", "child-acc")
    response = _create_task(
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
    assert response.status_code == 200
    body = response.json()
    assert body["dependency_edge_count"] == 1

    with db.session_scope() as session:
        by_account = {
            s.account_id: s for s in session.query(SubTask).all()
        }
        assert by_account["parent-acc"].status == "QUEUED"
        assert by_account["child-acc"].status == "WAITING_DEPENDENCY"
        edges = session.query(DependencyEdge).all()
        assert len(edges) == 1
        assert edges[0].parent_id == by_account["parent-acc"].subtask_id
        assert edges[0].child_id == by_account["child-acc"].subtask_id
        assert edges[0].required_handle_schema == {"kind": "comment_id"}


def test_create_task_rejects_cycle(central_client):
    _activate(central_client, "tenant-a", "a1")
    _activate(central_client, "tenant-a", "a2")
    response = _create_task(
        central_client,
        "tenant-a",
        ["a1", "a2"],
        dependencies=[
            {"parent_account_id": "a1", "child_account_id": "a2"},
            {"parent_account_id": "a2", "child_account_id": "a1"},
        ],
    )
    assert response.status_code == 400
    assert "cycle" in response.json()["detail"]


def test_create_task_rejects_unknown_dependency_account(central_client):
    _activate(central_client, "tenant-a", "a1")
    _activate(central_client, "tenant-a", "a2")
    response = _create_task(
        central_client,
        "tenant-a",
        ["a1", "a2"],
        dependencies=[{"parent_account_id": "ghost", "child_account_id": "a2"}],
    )
    assert response.status_code == 400
    assert "unknown account" in response.json()["detail"]


def test_create_task_rejects_inactive_accounts(central_client):
    _register_device(central_client, "tenant-a", "win-01")
    _import(central_client, "tenant-a", [{"account_id": "acc-1"}])
    response = _create_task(central_client, "tenant-a", ["acc-1"])
    assert response.status_code == 400
    assert "not active" in response.json()["detail"]


def test_create_task_rejects_invalid_type_and_priority(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    bad_type = _create_task(central_client, "tenant-a", ["acc-1"], task_type="hack")
    assert bad_type.status_code == 400
    bad_priority = _create_task(central_client, "tenant-a", ["acc-1"], priority="urgent")
    assert bad_priority.status_code == 400


def test_create_task_tenant_isolation(central_client):
    _activate(central_client, "tenant-a", "acc-1")
    response = _create_task(central_client, "tenant-b", ["acc-1"])
    assert response.status_code == 400
    assert "not active" in response.json()["detail"]


def test_detect_cycle_unit():
    from central.tasks import detect_cycle

    assert detect_cycle(["a", "b"], [("a", "b")]) == []
    assert detect_cycle(["a", "b", "c"], [("a", "b"), ("b", "c")]) == []
    assert detect_cycle(["a", "b"], [("a", "b"), ("b", "a")]) == ["a", "b"]
    assert detect_cycle(["a", "b", "c"], [("a", "b"), ("c", "b")]) == []
