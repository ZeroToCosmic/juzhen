"""Central device management routes (M1)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central import config
from central.db import get_session
from central.models import Device
from central.security import require_tenant

router = APIRouter(prefix="/api/central/devices", tags=["devices"])


def device_view(device: Device, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    last = device.last_heartbeat_at
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    online = (
        device.status == "online"
        and last is not None
        and (now - last).total_seconds() <= config.HEARTBEAT_ONLINE_SECONDS
    )
    return {
        "tenant_id": device.tenant_id,
        "device_id": device.device_id,
        "name": device.name,
        "status": "online" if online else "offline",
        "agent_version": device.agent_version,
        "capabilities": device.capabilities,
        "channel": device.channel,
        "max_accounts": device.max_accounts,
        "used_accounts": device.used_accounts,
        "inventory_epoch": device.inventory_epoch,
        "enabled": device.enabled,
        "last_heartbeat_at": device.last_heartbeat_at.isoformat() if device.last_heartbeat_at else None,
    }


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    channel: str | None = Field(default=None, pattern="^(dev|canary|stable)$")
    max_accounts: int | None = Field(default=None, ge=1, le=10000)


def get_device(session: Session, tenant_id: str, device_id: str) -> Device:
    device = (
        session.query(Device)
        .filter(Device.tenant_id == tenant_id, Device.device_id == device_id)
        .one_or_none()
    )
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device


@router.get("")
def list_devices(
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    devices = (
        session.query(Device).filter(Device.tenant_id == tenant_id).all()
    )
    return {"count": len(devices), "devices": [device_view(d) for d in devices]}


@router.get("/{device_id}")
def get_device_detail(
    device_id: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    return device_view(get_device(session, tenant_id, device_id))


@router.patch("/{device_id}")
def update_device(
    device_id: str,
    payload: DeviceUpdate,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    device = get_device(session, tenant_id, device_id)
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(device, key, value)
    return device_view(device)


@router.post("/{device_id}/offline")
def report_offline(
    device_id: str,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    device = get_device(session, tenant_id, device_id)
    device.status = "offline"
    return {"device_id": device_id, "status": "offline"}
