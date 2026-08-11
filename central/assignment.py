"""Subtask dispatcher: priority queue + profile-aware assignment (PRD F11 / transition #3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from central import config
from central.models import Account, Device, SubTask, Task
from central.outbox import add_outbox

PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def dispatch_queued(session: Session, *, tenant_id: str | None = None, limit: int = 50) -> dict:
    query = session.query(SubTask).filter(SubTask.status == "QUEUED")
    if tenant_id is not None:
        query = query.filter(SubTask.tenant_id == tenant_id)
    queued = query.all()
    queued.sort(
        key=lambda s: (
            PRIORITY_ORDER.get(_task_priority(session, s), 1),
            s.created_at or datetime.min.replace(tzinfo=timezone.utc),
        )
    )
    assigned = 0
    skipped = 0
    for subtask in queued[:limit]:
        account = (
            session.query(Account)
            .filter(
                Account.tenant_id == subtask.tenant_id,
                Account.account_id == subtask.account_id,
            )
            .one_or_none()
        )
        is_probe = bool(subtask.config_snapshot.get("params", {}).get("probe"))
        if (
            account is None
            or account.deploy_status != "ACTIVE"
            or (not is_probe and account.business_status != "ACTIVE")
        ):
            skipped += 1
            continue
        subtask.profile_id = account.profile_id
        if _profile_busy(session, subtask.tenant_id, subtask.profile_id, exclude=subtask.id):
            skipped += 1
            continue
        device = _pick_device(session, subtask.tenant_id, account.authoritative_device_id)
        if device is None:
            skipped += 1
            continue
        now = datetime.now(timezone.utc)
        subtask.status = "ASSIGNED"
        subtask.assigned_device_id = device.device_id
        subtask.lease_owner = device.device_id
        subtask.lease_generation += 1
        subtask.lease_timeout_at = now + timedelta(seconds=config.LEASE_TIMEOUT_SECONDS)
        subtask.last_progress_at = now
        subtask.revision += 1
        add_outbox(
            session,
            tenant_id=subtask.tenant_id,
            aggregate="subtask",
            subject=f"{subtask.tenant_id}/subtask.assigned",
            payload={
                "subtask_id": subtask.subtask_id,
                "account_id": subtask.account_id,
                "device_id": device.device_id,
                "lease_generation": subtask.lease_generation,
                "config_snapshot": subtask.config_snapshot,
            },
        )
        assigned += 1
    return {"assigned": assigned, "skipped": skipped}


def _task_priority(session: Session, subtask: SubTask) -> str:
    task = session.get(Task, subtask.task_id)
    return task.priority if task is not None else "medium"


def _profile_busy(session: Session, tenant_id: str, profile_id: str, exclude: int) -> bool:
    if not profile_id:
        return False
    count = (
        session.query(SubTask)
        .filter(
            SubTask.tenant_id == tenant_id,
            SubTask.profile_id == profile_id,
            SubTask.status.in_(("ASSIGNED", "RUNNING")),
            SubTask.id != exclude,
        )
        .count()
    )
    return count > 0


def _pick_device(session: Session, tenant_id: str, preferred: str | None) -> Device | None:
    devices = (
        session.query(Device)
        .filter(Device.tenant_id == tenant_id, Device.enabled.is_(True))
        .all()
    )
    online = []
    for device in devices:
        last = device.last_heartbeat_at
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - last).total_seconds() <= config.HEARTBEAT_ONLINE_SECONDS:
            online.append(device)
    if not online:
        return None
    if preferred is not None:
        for device in online:
            if device.device_id == preferred:
                return device
    return min(online, key=lambda d: d.id)
