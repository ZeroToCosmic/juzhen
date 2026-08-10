"""Lease management and reclaimer (PRD F13 / state transitions #4 #5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from central import config
from central.models import Device, SubTask

ACTIVE_LEASES = ("ASSIGNED", "RUNNING")


def renew_lease(
    session: Session,
    *,
    subtask_id: str,
    device_id: str,
    generation: int,
    tenant_id: str,
) -> SubTask:
    subtask = (
        session.query(SubTask)
        .filter(SubTask.subtask_id == subtask_id, SubTask.tenant_id == tenant_id)
        .one_or_none()
    )
    if subtask is None:
        raise KeyError("subtask not found")
    if subtask.status not in ACTIVE_LEASES:
        raise ValueError("subtask not leased")
    if subtask.lease_generation != generation or subtask.lease_owner != device_id:
        raise PermissionError("stale generation")
    now = datetime.now(timezone.utc)
    subtask.last_progress_at = now
    subtask.lease_timeout_at = now + timedelta(seconds=config.LEASE_TIMEOUT_SECONDS)
    subtask.revision += 1
    return subtask


def _device_online(device: Device | None, now: datetime) -> bool:
    if device is None or not device.enabled:
        return False
    last = device.last_heartbeat_at
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last).total_seconds() <= config.HEARTBEAT_ONLINE_SECONDS


def reclaim_stale(
    session: Session,
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    query = session.query(SubTask).filter(SubTask.status.in_(ACTIVE_LEASES))
    if tenant_id is not None:
        query = query.filter(SubTask.tenant_id == tenant_id)
    stale = []
    for subtask in query.all():
        device = (
            session.query(Device)
            .filter(
                Device.tenant_id == subtask.tenant_id,
                Device.device_id == subtask.lease_owner,
            )
            .one_or_none()
        )
        last = subtask.last_progress_at or subtask.lease_timeout_at
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        expired = (
            last is not None
            and (now - last).total_seconds() > config.LEASE_TIMEOUT_SECONDS
        )
        if not _device_online(device, now) or expired:
            stale.append(subtask)
    reclaimed = 0
    dlq = 0
    for subtask in stale:
        subtask.lease_generation += 1
        subtask.attempts += 1
        subtask.lease_owner = ""
        subtask.assigned_device_id = None
        subtask.lease_timeout_at = None
        subtask.last_progress_at = None
        if subtask.attempts > config.MAX_RETRY_ATTEMPTS:
            subtask.status = "DLQ"
            dlq += 1
        else:
            subtask.status = "QUEUED"
            reclaimed += 1
        subtask.revision += 1
    return {"reclaimed": reclaimed, "dlq": dlq}
