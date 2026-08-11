"""Dashboard summary API (PRD F4, M3)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from central import config
from central.db import get_session
from central.models import Device, SubTask, Task, TaskResult
from central.security import require_tenant

router = APIRouter(prefix="/api/central/dashboard", tags=["dashboard"])


@router.get("/summary")
def dashboard_summary(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    now = datetime.now(timezone.utc)
    day_start = now - timedelta(days=1)

    tasks_today = (
        session.query(Task)
        .filter(Task.tenant_id == tenant_id, Task.created_at >= day_start)
        .count()
    )
    results_today = (
        session.query(TaskResult)
        .filter(TaskResult.tenant_id == tenant_id, TaskResult.created_at >= day_start)
        .all()
    )
    succeeded = sum(1 for row in results_today if row.status == "SUCCESS")
    failed = sum(1 for row in results_today if row.status == "FAILED")
    success_rate = round(succeeded / (succeeded + failed), 4) if (succeeded + failed) else None

    running_windows = (
        session.query(SubTask)
        .filter(
            SubTask.tenant_id == tenant_id,
            SubTask.status.in_(("ASSIGNED", "RUNNING")),
        )
        .count()
    )
    queued = (
        session.query(SubTask)
        .filter(SubTask.tenant_id == tenant_id, SubTask.status == "QUEUED")
        .count()
    )
    dlq = (
        session.query(SubTask)
        .filter(SubTask.tenant_id == tenant_id, SubTask.status == "DLQ")
        .count()
    )

    online_devices = 0
    devices = (
        session.query(Device).filter(Device.tenant_id == tenant_id).all()
    )
    for device in devices:
        last = device.last_heartbeat_at
        if last is None:
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() <= config.HEARTBEAT_ONLINE_SECONDS:
            online_devices += 1

    return {
        "tasks_today": tasks_today,
        "success_rate": success_rate,
        "succeeded": succeeded,
        "failed": failed,
        "running_windows": running_windows,
        "queued": queued,
        "dlq": dlq,
        "online_devices": online_devices,
        "total_devices": len(devices),
        "generated_at": now.isoformat(),
    }
