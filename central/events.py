"""Event bus for WebSocket delivery (PRD F4a, M3).

Events are appended to a per-tenant Redis Stream (bounded by MAXLEN, the
replay window). Stream IDs are the monotonic event_seq delivered to
clients; reconnect replays from last_seq and falls back to a full
snapshot when the window has been truncated.
"""

from __future__ import annotations

import time
from typing import Protocol

EVENT_STREAM_PREFIX = "bcs:events:"
WINDOW_MAXLEN = 1000
WINDOW_SECONDS = 300


class EventStore(Protocol):
    def publish(self, tenant_id: str, event_type: str, payload: dict) -> str: ...
    def read_after(self, tenant_id: str, last_seq: str, count: int = 500) -> list[tuple[str, dict]]: ...


class RedisEventStore:
    def __init__(self, redis_client):
        self._redis = redis_client

    def _key(self, tenant_id: str) -> str:
        return f"{EVENT_STREAM_PREFIX}{tenant_id}"

    def publish(self, tenant_id: str, event_type: str, payload: dict) -> str:
        entry = {"type": event_type, "payload": payload}
        seq = self._redis.xadd(
            self._key(tenant_id),
            entry,
            maxlen=WINDOW_MAXLEN,
        )
        return seq

    def read_after(self, tenant_id: str, last_seq: str, count: int = 500) -> list[tuple[str, dict]]:
        if not last_seq:
            return []
        entries = self._redis.xrange(
            self._key(tenant_id),
            min=f"({last_seq}",
            max="+",
            count=count,
        )
        return [(seq, entry) for seq, entry in entries]


class MemoryEventStore:
    """Deterministic in-process store for tests and local runs."""

    def __init__(self):
        self._streams: dict[str, list[tuple[str, dict]]] = {}
        self._seq_counter = 0

    def _key(self, tenant_id: str) -> str:
        return f"{EVENT_STREAM_PREFIX}{tenant_id}"

    def publish(self, tenant_id: str, event_type: str, payload: dict) -> str:
        self._seq_counter += 1
        seq = f"{int(time.time() * 1000)}-{self._seq_counter}"
        entries = self._streams.setdefault(self._key(tenant_id), [])
        entries.append((seq, {"type": event_type, "payload": payload}))
        if len(entries) > WINDOW_MAXLEN:
            del entries[: len(entries) - WINDOW_MAXLEN]
        return seq

    def read_after(self, tenant_id: str, last_seq: str, count: int = 500) -> list[tuple[str, dict]]:
        entries = self._streams.get(self._key(tenant_id), [])
        if not last_seq:
            return entries[:count]
        result = []
        for seq, entry in entries:
            if seq > last_seq:
                result.append((seq, entry))
            if len(result) >= count:
                break
        return result
