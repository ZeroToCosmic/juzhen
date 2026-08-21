"""Dependency activation and handle submission (PRD F10 / transitions #1 #2)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from central.models import DependencyEdge, Handle, SubTask

HANDLE_VERIFIED = "VERIFIED"


def submit_handle(
    session: Session,
    *,
    tenant_id: str,
    subtask_id: str,
    content: dict,
    verification_status: str,
    text_hash: str = "",
) -> SubTask:
    subtask = (
        session.query(SubTask)
        .filter(SubTask.subtask_id == subtask_id, SubTask.tenant_id == tenant_id)
        .one_or_none()
    )
    if subtask is None:
        raise KeyError("subtask not found")
    session.add(
        Handle(
            tenant_id=tenant_id,
            subtask_id=subtask_id,
            content=content,
            text_hash=text_hash,
            verification_status=verification_status,
        )
    )
    subtask.status = "SUCCESS"
    subtask.revision += 1
    return subtask


def activate_ready_dependents(session: Session, *, tenant_id: str | None = None) -> dict:
    query = session.query(SubTask).filter(SubTask.status == "WAITING_DEPENDENCY")
    if tenant_id is not None:
        query = query.filter(SubTask.tenant_id == tenant_id)
    activated = 0
    failed = 0
    for child in query.all():
        edges = (
            session.query(DependencyEdge)
            .filter(DependencyEdge.child_id == child.subtask_id)
            .all()
        )
        if not edges:
            child.status = "QUEUED"
            child.revision += 1
            activated += 1
            continue
        parent_ids = [edge.parent_id for edge in edges]
        parents = (
            session.query(SubTask)
            .filter(SubTask.subtask_id.in_(parent_ids))
            .all()
        )
        by_id = {p.subtask_id: p for p in parents}
        any_parent_failed = any(
            by_id.get(pid) is not None and by_id[pid].status in ("FAILED", "CANCELLED")
            for pid in parent_ids
        )
        if any_parent_failed:
            child.status = "FAILED"
            child.revision += 1
            failed += 1
            continue
        all_success = all(
            by_id.get(pid) is not None and by_id[pid].status == "SUCCESS"
            for pid in parent_ids
        )
        if not all_success:
            continue
        handles = (
            session.query(Handle)
            .filter(
                Handle.subtask_id.in_(parent_ids),
                Handle.verification_status == HANDLE_VERIFIED,
            )
            .all()
        )
        verified_parents = {handle.subtask_id for handle in handles}
        if parent_ids and verified_parents == set(parent_ids):
            child.status = "QUEUED"
            child.revision += 1
            activated += 1
    return {"activated": activated, "failed": failed}
