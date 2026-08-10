"""Scheduler tick + subtask lifecycle endpoints (M2 increment 2/3/4)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central.assignment import dispatch_queued
from central.db import get_session
from central.dependencies import activate_ready_dependents, submit_handle
from central.inbox import try_dedupe
from central.leases import reclaim_stale, renew_lease
from central.models import SubTask, TaskResult
from central.security import require_tenant

router = APIRouter(prefix="/api/central", tags=["scheduler"])

RETRYABLE_CATEGORIES = frozenset({"retryable", "environment"})


@router.post("/scheduler/tick")
def scheduler_tick(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    reclaim = reclaim_stale(session, tenant_id=tenant_id)
    activation = activate_ready_dependents(session, tenant_id=tenant_id)
    dispatch = dispatch_queued(session, tenant_id=tenant_id)
    return {"reclaim": reclaim, "activation": activation, "dispatch": dispatch}


@router.get("/agent/subtasks")
def agent_pull_subtasks(
    device_id: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    subtasks = (
        session.query(SubTask)
        .filter(
            SubTask.tenant_id == tenant_id,
            SubTask.assigned_device_id == device_id,
            SubTask.status.in_(("ASSIGNED", "RUNNING")),
        )
        .all()
    )
    return {
        "count": len(subtasks),
        "subtasks": [
            {
                "subtask_id": s.subtask_id,
                "task_id": s.task_id,
                "account_id": s.account_id,
                "profile_id": s.profile_id,
                "status": s.status,
                "lease_generation": s.lease_generation,
                "lease_timeout_at": s.lease_timeout_at.isoformat() if s.lease_timeout_at else None,
                "config_snapshot": s.config_snapshot,
            }
            for s in subtasks
        ],
    }


class ResultSubmitRequest(BaseModel):
    subtask_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)
    status: str = Field(pattern="^(SUCCESS|FAILED)$")
    error_category: str = Field(default="", max_length=32)
    error_code: str = Field(default="", max_length=64)
    result_data: dict = Field(default_factory=dict)
    duration_ms: int = Field(default=0, ge=0)
    msg_id: str = Field(min_length=1, max_length=128)


@router.post("/subtasks/result")
def submit_result(
    payload: ResultSubmitRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    dedupe_key = f"{tenant_id}/{payload.subtask_id}"
    if not try_dedupe(
        session,
        msg_id=payload.msg_id,
        subject=dedupe_key,
        payload=payload.model_dump(),
    ):
        raise HTTPException(status_code=409, detail="duplicate result message")

    subtask = (
        session.query(SubTask)
        .filter(
            SubTask.subtask_id == payload.subtask_id,
            SubTask.tenant_id == tenant_id,
        )
        .one_or_none()
    )
    if subtask is None:
        raise HTTPException(status_code=404, detail="subtask not found")
    if subtask.lease_generation != payload.generation:
        raise HTTPException(
            status_code=409, detail="stale generation, result rejected"
        )
    if subtask.status not in ("ASSIGNED", "RUNNING"):
        raise HTTPException(status_code=409, detail="subtask not running")

    session.add(
        TaskResult(
            tenant_id=tenant_id,
            subtask_id=payload.subtask_id,
            generation=payload.generation,
            device_id=payload.device_id,
            status=payload.status,
            error_category=payload.error_category,
            result_data=payload.result_data,
            duration_ms=payload.duration_ms,
            error_code=payload.error_code,
        )
    )
    if payload.status == "SUCCESS":
        subtask.status = "SUCCESS"
    else:
        subtask.attempts += 1
        if payload.error_category in RETRYABLE_CATEGORIES and subtask.attempts <= 3:
            subtask.status = "QUEUED"
            subtask.lease_owner = ""
            subtask.assigned_device_id = None
            subtask.lease_timeout_at = None
            subtask.lease_generation += 1
        else:
            subtask.status = "DLQ"
    subtask.last_progress_at = None
    subtask.revision += 1
    return {"subtask_id": payload.subtask_id, "status": subtask.status}


class LeaseRenewRequest(BaseModel):
    subtask_id: str = Field(min_length=1, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0)


@router.post("/subtasks/lease/renew")
def lease_renew(
    payload: LeaseRenewRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    try:
        subtask = renew_lease(
            session,
            subtask_id=payload.subtask_id,
            device_id=payload.device_id,
            generation=payload.generation,
            tenant_id=tenant_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="subtask not found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="subtask not leased") from None
    except PermissionError:
        raise HTTPException(
            status_code=409, detail="stale generation, lease rejected"
        ) from None
    return {
        "subtask_id": subtask.subtask_id,
        "status": subtask.status,
        "generation": subtask.lease_generation,
        "lease_timeout_at": subtask.lease_timeout_at.isoformat() if subtask.lease_timeout_at else None,
    }


class HandleSubmitRequest(BaseModel):
    subtask_id: str = Field(min_length=1, max_length=64)
    verification_status: str = Field(pattern="^(VERIFIED|UNVERIFIED)$")
    content: dict = Field(default_factory=dict)
    text_hash: str = Field(default="", max_length=64)


@router.post("/subtasks/handle")
def handle_submit(
    payload: HandleSubmitRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    try:
        subtask = submit_handle(
            session,
            tenant_id=tenant_id,
            subtask_id=payload.subtask_id,
            content=payload.content,
            verification_status=payload.verification_status,
            text_hash=payload.text_hash,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="subtask not found") from None
    return {"subtask_id": subtask.subtask_id, "status": "SUCCESS"}


@router.get("/subtasks")
def list_subtasks(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    subtasks = (
        session.query(SubTask).filter(SubTask.tenant_id == tenant_id).all()
    )
    return {
        "count": len(subtasks),
        "subtasks": [
            {
                "subtask_id": s.subtask_id,
                "task_id": s.task_id,
                "account_id": s.account_id,
                "status": s.status,
                "assigned_device_id": s.assigned_device_id,
                "lease_generation": s.lease_generation,
                "attempts": s.attempts,
                "revision": s.revision,
            }
            for s in subtasks
        ],
    }
