"""Central WebSocket channel tests (M3 increment 3, PRD F4a)."""

from __future__ import annotations

import json
import threading

import pytest
from fastapi.testclient import TestClient

from central import config, db
from central.events import MemoryEventStore
from central.models import Base, SubTask
from central.app import app

from tests.test_central_skeleton import central_client  # noqa: F401


@pytest.fixture()
def ws_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CENTRAL_DB_PATH", tmp_path / "central.db")
    db._engine = None
    db._session_factory = None
    Base.metadata.create_all(db.get_engine())
    store = MemoryEventStore()
    monkeypatch.setattr("central.app.event_store", store)
    return TestClient(app), store


def test_ws_handshake_sends_snapshot_then_live_events(ws_client):
    client, store = ws_client
    with client.websocket_connect("/ws/events?tenant_id=tenant-a") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "snapshot"
        store.publish("tenant-a", "task.created", {"task_id": "t-1"})
        store.publish("tenant-a", "subtask.result", {"subtask_id": "s-1", "status": "SUCCESS"})
        created = json.loads(ws.receive_text())
        assert created["type"] == "task.created"
        second = json.loads(ws.receive_text())
        assert second["type"] == "subtask.result"
        assert second["payload"]["status"] == "SUCCESS"
        assert "seq" in second


def test_ws_replays_missing_events_after_last_seq(ws_client):
    client, store = ws_client
    seq1 = store.publish("tenant-a", "task.created", {"task_id": "t-1"})
    seq2 = store.publish("tenant-a", "subtask.result", {"subtask_id": "s-1"})
    with client.websocket_connect(
        f"/ws/events?tenant_id=tenant-a&last_seq={seq1}"
    ) as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "snapshot"
        replay = json.loads(ws.receive_text())
        assert replay["seq"] == seq2
        assert replay["type"] == "subtask.result"


def test_ws_replays_all_when_no_last_seq(ws_client):
    client, store = ws_client
    store.publish("tenant-a", "task.created", {"task_id": "t-1"})
    with client.websocket_connect("/ws/events?tenant_id=tenant-a") as ws:
        first = json.loads(ws.receive_text())
        assert first["type"] == "snapshot"
        replay = json.loads(ws.receive_text())
        assert replay["type"] == "task.created"


def test_ws_snapshot_reflects_subtask_counts(ws_client):
    client, store = ws_client
    with db.session_scope() as session:
        session.add(
            SubTask(
                subtask_id="s-1",
                tenant_id="tenant-a",
                task_id="t-1",
                account_id="acc-1",
                config_snapshot={},
                status="QUEUED",
            )
        )
        session.add(
            SubTask(
                subtask_id="s-2",
                tenant_id="tenant-a",
                task_id="t-1",
                account_id="acc-2",
                config_snapshot={},
                status="RUNNING",
            )
        )
    with client.websocket_connect("/ws/events?tenant_id=tenant-a") as ws:
        snapshot = json.loads(ws.receive_text())
        assert snapshot["type"] == "snapshot"
        assert snapshot["payload"]["total_subtasks"] == 2
        assert snapshot["payload"]["subtask_counts"] == {"QUEUED": 1, "RUNNING": 1}


def test_ws_missing_tenant_rejected(ws_client):
    client, _ = ws_client
    with client.websocket_connect("/ws/events?tenant_id=") as ws:
        code = ws.receive()
        assert code["code"] == 4400


def test_ws_tenant_events_are_isolated(ws_client):
    client, store = ws_client
    with client.websocket_connect("/ws/events?tenant_id=tenant-a") as ws:
        json.loads(ws.receive_text())
        store.publish("tenant-b", "task.created", {"task_id": "t-b"})
        store.publish("tenant-a", "task.created", {"task_id": "t-a"})
        event = json.loads(ws.receive_text())
        assert event["type"] == "task.created"
        assert event["payload"]["task_id"] == "t-a"
