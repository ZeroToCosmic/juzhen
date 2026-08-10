"""Capacity-aware target device selection (PRD F26 / F11).

Selection picks the device with the lowest capacity water level
(used_accounts / max_accounts) among enabled devices that still have headroom
(used_accounts < max_accounts). The used_accounts increment, the DeployTask
creation and the outbox row share one transaction, so concurrent imports
cannot oversell capacity (unique constraint on tenant+account backs it up).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from central.models import Device


def select_device(session: Session, *, tenant_id: str) -> Device | None:
    devices = (
        session.query(Device)
        .filter(
            Device.tenant_id == tenant_id,
            Device.enabled.is_(True),
        )
        .all()
    )
    candidates = [d for d in devices if d.used_accounts < d.max_accounts]
    if not candidates:
        return None

    def water_level(device: Device) -> float:
        if device.max_accounts <= 0:
            return float("inf")
        return device.used_accounts / device.max_accounts

    return min(candidates, key=lambda d: (water_level(d), d.id))
