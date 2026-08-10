"""Task/SubTask/DAG creation (PRD F3/F9/F10, M2).

Task creation validates account ownership, DAG acyclicity (Kahn
topological sort, multi-parent support), freezes config_snapshot, and
creates SubTasks (QUEUED when dependency-free, WAITING_DEPENDENCY when
parents exist) plus dependency edges in one transaction with the
task.created outbox message.
"""

from __future__ import annotations

import uuid
from collections import deque
from collections.abc import Iterable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central.db import get_session
from central.models import Account, DependencyEdge, SubTask, Task
from central.outbox import add_outbox
from central.security import require_tenant

router = APIRouter(prefix="/api/central/tasks", tags=["tasks"])

TASK_TYPES = frozenset({"publish", "browse", "comment", "like", "follow", "deploy"})
PRIORITIES = frozenset({"high", "medium", "low"})


class DependencySpec(BaseModel):
    parent_account_id: str = Field(min_length=1, max_length=128)
    child_account_id: str = Field(min_length=1, max_length=128)
    required_handle_schema: dict = Field(default_factory=dict)


class TaskCreateRequest(BaseModel):
    task_type: str = Field(min_length=1, max_length=32)
    params: dict = Field(default_factory=dict)
    account_ids: list[str] = Field(min_length=1, max_length=1000)
    strategy_version: str = Field(default="", max_length=32)
    priority: str = Field(default="medium", max_length=8)
    deadline: str = Field(default="", max_length=64)
    config_snapshot: dict = Field(default_factory=dict)
    dependencies: list[DependencySpec] = Field(default_factory=list)


def detect_cycle(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[str]:
    nodes = set(nodes)
    adjacency = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for parent, child in edges:
        if parent not in nodes or child not in nodes:
            raise ValueError(f"dependency references unknown account: {parent}->{child}")
        adjacency[parent].append(child)
        indegree[child] += 1
    queue = deque(sorted(node for node in nodes if indegree[node] == 0))
    order: list[str] = []
    while queue:
        node = queue.popleft()
        order.append(node)
        for child in adjacency[node]:
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(nodes):
        remaining = sorted(nodes - set(order))
        return remaining
    return []


@router.post("")
def create_task(
    payload: TaskCreateRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    if payload.task_type not in TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported task_type: {payload.task_type}")
    if payload.priority not in PRIORITIES:
        raise HTTPException(status_code=400, detail=f"invalid priority: {payload.priority}")

    accounts = (
        session.query(Account)
        .filter(
            Account.tenant_id == tenant_id,
            Account.account_id.in_(payload.account_ids),
            Account.deploy_status == "ACTIVE",
        )
        .all()
    )
    found = {account.account_id for account in accounts}
    missing = [account_id for account_id in payload.account_ids if account_id not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"accounts not active or not found in tenant: {missing[:10]}",
        )

    edges = [(spec.parent_account_id, spec.child_account_id) for spec in payload.dependencies]
    try:
        cycle = detect_cycle(payload.account_ids, edges)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if cycle:
        raise HTTPException(
            status_code=400,
            detail=f"DAG cycle detected involving: {cycle}",
        )

    task_id = uuid.uuid4().hex
    snapshot = {
        "strategy_version": payload.strategy_version,
        "priority": payload.priority,
        "params": payload.params,
        **payload.config_snapshot,
    }
    task = Task(
        task_id=task_id,
        tenant_id=tenant_id,
        task_type=payload.task_type,
        params=payload.params,
        strategy_version=payload.strategy_version,
        config_snapshot=snapshot,
        schedule={},
        priority=payload.priority,
        status="QUEUED",
    )
    session.add(task)
    session.flush()

    subtask_ids = {}
    for account_id in payload.account_ids:
        subtask_id = uuid.uuid4().hex
        subtask = SubTask(
            subtask_id=subtask_id,
            tenant_id=tenant_id,
            task_id=task_id,
            account_id=account_id,
            config_snapshot=snapshot,
            status="WAITING_DEPENDENCY",
        )
        session.add(subtask)
        subtask_ids[account_id] = subtask_id

    dependency_edges = []
    for spec in payload.dependencies:
        edge = DependencyEdge(
            tenant_id=tenant_id,
            parent_id=subtask_ids[spec.parent_account_id],
            child_id=subtask_ids[spec.child_account_id],
            condition="AND",
            required_handle_schema=spec.required_handle_schema,
        )
        session.add(edge)
        dependency_edges.append(
            {
                "parent_subtask_id": edge.parent_id,
                "child_subtask_id": edge.child_id,
            }
        )

    for account_id in payload.account_ids:
        subtask_id = subtask_ids[account_id]
        has_parents = any(
            spec.child_account_id == account_id for spec in payload.dependencies
        )
        if not has_parents:
            subtask = (
                session.query(SubTask)
                .filter(SubTask.subtask_id == subtask_id)
                .one()
            )
            subtask.status = "QUEUED"

    add_outbox(
        session,
        tenant_id=tenant_id,
        aggregate="task",
        subject=f"{tenant_id}/task.created",
        payload={
            "task_id": task_id,
            "task_type": payload.task_type,
            "subtask_count": len(payload.account_ids),
        },
    )

    return {
        "task_id": task_id,
        "status": "QUEUED",
        "subtask_count": len(payload.account_ids),
        "dependency_edge_count": len(dependency_edges),
    }
