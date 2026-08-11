"""Human review center: DLQ list and manual operations (PRD F5/F16)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from central.db import get_session
from central.models import SubTask, TaskResult
from central.security import require_tenant

router = APIRouter(prefix="/api/central/dlq", tags=["dlq"])


def _get_dlq_subtask(session: Session, tenant_id: str, subtask_id: str) -> SubTask:
    subtask = (
        session.query(SubTask)
        .filter(SubTask.subtask_id == subtask_id, SubTask.tenant_id == tenant_id)
        .one_or_none()
    )
    if subtask is None:
        raise HTTPException(status_code=404, detail="subtask not found")
    return subtask


@router.get("")
def list_dlq(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    subtasks = (
        session.query(SubTask)
        .filter(SubTask.tenant_id == tenant_id, SubTask.status == "DLQ")
        .order_by(SubTask.id.desc())
        .all()
    )
    results = {
        row.subtask_id: row
        for row in (
            session.query(TaskResult)
            .filter(TaskResult.tenant_id == tenant_id)
            .all()
        )
    }
    return {
        "count": len(subtasks),
        "items": [
            {
                "subtask_id": s.subtask_id,
                "task_id": s.task_id,
                "account_id": s.account_id,
                "attempts": s.attempts,
                "lease_generation": s.lease_generation,
                "error_category": results[s.subtask_id].error_category if s.subtask_id in results else "",
                "error_code": results[s.subtask_id].error_code if s.subtask_id in results else "",
                "updated_at": s.last_progress_at.isoformat() if s.last_progress_at else None,
            }
            for s in subtasks
        ],
    }


@router.post("/{subtask_id}/requeue")
def requeue_dlq(
    subtask_id: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    subtask = _get_dlq_subtask(session, tenant_id, subtask_id)
    if subtask.status != "DLQ":
        raise HTTPException(status_code=409, detail="subtask not in DLQ")
    subtask.status = "QUEUED"
    subtask.lease_generation += 1
    subtask.attempts = 0
    subtask.lease_owner = ""
    subtask.assigned_device_id = None
    subtask.lease_timeout_at = None
    subtask.revision += 1
    return {"subtask_id": subtask_id, "status": "QUEUED"}


@router.post("/{subtask_id}/terminate")
def terminate_dlq(
    subtask_id: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    subtask = _get_dlq_subtask(session, tenant_id, subtask_id)
    if subtask.status != "DLQ":
        raise HTTPException(status_code=409, detail="subtask not in DLQ")
    subtask.status = "CANCELLED"
    subtask.lease_owner = ""
    subtask.assigned_device_id = None
    subtask.lease_timeout_at = None
    subtask.revision += 1
    return {"subtask_id": subtask_id, "status": "CANCELLED"}
