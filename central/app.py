"""Central control FastAPI application (M1 skeleton).

Endpoints:
- GET  /healthz                 central liveness
- POST /api/central/devices/heartbeat   device heartbeat + capabilities + capacity
- POST /api/central/devices/{device_id}/offline  explicit offline report
- WS   /ws/events               realtime event channel (PRD F4a)
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, WebSocket
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central import config
from central.accounts import router as accounts_router
from central.dashboard import router as dashboard_router
from central.db import get_session, init_db
from central.devices import router as devices_router
from central.events import EventStore, MemoryEventStore, RedisEventStore
from central.human_review import router as human_review_router
from central.models import Device, DeviceSession
from central.scheduler import router as scheduler_router
from central.settings import router as settings_router
from central.tasks import router as tasks_router
from central.websocket import websocket_events

app = FastAPI(title="Business Control Central", version="0.1.0")

app.include_router(devices_router)
app.include_router(accounts_router)
app.include_router(tasks_router)
app.include_router(scheduler_router)
app.include_router(human_review_router)
app.include_router(dashboard_router)
app.include_router(settings_router)


def _create_event_store() -> EventStore:
    try:
        import redis

        client = redis.Redis.from_url(
            config.REDIS_URL, socket_timeout=2, decode_responses=True
        )
        client.ping()
        return RedisEventStore(client)
    except BaseException:
        return MemoryEventStore()


event_store = _create_event_store()


@app.websocket("/ws/events")
async def ws_events(websocket: WebSocket) -> None:
    await websocket_events(websocket, event_store)


@app.on_event("startup")
def startup() -> None:
    init_db()


class HeartbeatRequest(BaseModel):
    tenant_id: str = Field(default=config.DEFAULT_TENANT_ID, max_length=64)
    device_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=64)
    agent_version: str = Field(default="", max_length=32)
    capabilities: dict = Field(default_factory=dict)
    channel: str = Field(default="stable", max_length=16)
    max_accounts: int = Field(default=300, ge=1, le=10000)
    used_accounts: int = Field(default=0, ge=0)
    inventory_epoch: int = Field(default=0, ge=0)
    running_windows: int = Field(default=0, ge=0)
    queue_depth: int = Field(default=0, ge=0)


class HeartbeatResponse(BaseModel):
    device_id: str
    status: str
    lease_renew_ok: bool = True


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "service": "central"}


@app.post("/api/central/devices/heartbeat", response_model=HeartbeatResponse)
def heartbeat(payload: HeartbeatRequest, session: Session = Depends(get_session)) -> HeartbeatResponse:
    now = datetime.now(timezone.utc)
    device = (
        session.query(Device)
        .filter(
            Device.tenant_id == payload.tenant_id,
            Device.device_id == payload.device_id,
        )
        .one_or_none()
    )
    if device is None:
        device = Device(
            tenant_id=payload.tenant_id,
            device_id=payload.device_id,
        )
        session.add(device)
    device.agent_version = payload.agent_version
    device.capabilities = payload.capabilities
    device.channel = payload.channel
    device.max_accounts = payload.max_accounts
    device.used_accounts = payload.used_accounts
    device.inventory_epoch = payload.inventory_epoch
    device.last_heartbeat_at = now
    device.status = "online"

    session.add(
        DeviceSession(
            tenant_id=payload.tenant_id,
            device_id=payload.device_id,
            session_id=payload.session_id,
            agent_version=payload.agent_version,
        )
    )
    return HeartbeatResponse(device_id=payload.device_id, status="online")
