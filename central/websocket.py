"""WebSocket event channel (PRD F4a, M3).

Handshake: full snapshot first, then replay of events after last_seq from
the tenant stream, then live events as they are published. If the client
detects a seq gap (window truncated), it reconnects with a lower
last_seq; the server answers with a snapshot payload when the stream has
no more entries than requested.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from central.events import EventStore
from central.models import SubTask
from central.db import session_scope

router = APIRouter()

SNAPSHOT_EVENT = "snapshot"
MAX_SEND_QUEUE = 2000


def build_snapshot(tenant_id: str) -> dict:
    with session_scope() as session:
        subtasks = (
            session.query(SubTask).filter(SubTask.tenant_id == tenant_id).all()
        )
        counts: dict[str, int] = {}
        for subtask in subtasks:
            counts[subtask.status] = counts.get(subtask.status, 0) + 1
        return {
            "type": SNAPSHOT_EVENT,
            "payload": {
                "subtask_counts": counts,
                "total_subtasks": len(subtasks),
            },
        }


async def websocket_events(
    websocket: WebSocket,
    event_store: EventStore,
) -> None:
    await websocket.accept()
    tenant_id = websocket.query_params.get("tenant_id", "")
    last_seq = websocket.query_params.get("last_seq", "")
    if not tenant_id:
        await websocket.close(code=4400)
        return
    try:
        snapshot = build_snapshot(tenant_id)
        await websocket.send_text(json.dumps(snapshot))

        missed = event_store.read_after(tenant_id, last_seq)
        for seq, entry in missed:
            await websocket.send_text(
                json.dumps({"seq": seq, **entry})
            )
        last_seq = missed[-1][0] if missed else last_seq

        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_SEND_QUEUE)
        stop = asyncio.Event()

        async def watcher():
            try:
                while not stop.is_set():
                    latest = event_store.read_after(tenant_id, last_seq)
                    for seq, entry in latest:
                        try:
                            queue.put_nowait((seq, entry))
                        except asyncio.QueueFull:
                            await websocket.close(code=4401)
                            return
                    await asyncio.sleep(1.0)
            finally:
                stop.set()

        task = asyncio.create_task(watcher())
        try:
            while not stop.is_set():
                try:
                    seq, entry = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    await websocket.send_text(json.dumps({"type": "ping"}))
                    continue
                await websocket.send_text(json.dumps({"seq": seq, **entry}))
                if seq > last_seq:
                    last_seq = seq
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            task.cancel()
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except RuntimeError:
            pass
