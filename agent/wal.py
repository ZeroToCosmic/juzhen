"""Agent local WAL for window stage recovery (PRD 15.3, M4).

Each stage transition is persisted before execution. After a crash or
forced kill the agent replays the WAL and applies the recovery rules:

- NEW / STARTING  -> abandoned and re-pulled
- RUNNING         -> marked ABORTED (retryable), no lease renewal
- SUBMITTING / VERIFYING -> UNVERIFIED (the write may have landed),
                           human review, never auto-re-submitted
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

STAGE_NEW = "NEW"
STAGE_STARTING = "STARTING"
STAGE_RUNNING = "RUNNING"
STAGE_SUBMITTING = "SUBMITTING"
STAGE_VERIFYING = "VERIFYING"
STAGE_DONE = "DONE"

RECOVERY_ABANDON = frozenset({STAGE_NEW, STAGE_STARTING})
RECOVERY_ABORT = frozenset({STAGE_RUNNING})
RECOVERY_UNVERIFIED = frozenset({STAGE_SUBMITTING, STAGE_VERIFYING})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WindowWal:
    def __init__(self, path: Path):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (ValueError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set_stage(self, subtask_id: str, stage: str, *, meta: dict | None = None) -> None:
        data = self._read()
        entry = data.setdefault(subtask_id, {})
        entry["stage"] = stage
        entry["updated_at"] = _now()
        if meta:
            entry["meta"] = meta
        entry["generation"] = entry.get("generation", 0)
        self._write(data)

    def set_generation(self, subtask_id: str, generation: int) -> None:
        data = self._read()
        entry = data.setdefault(subtask_id, {})
        entry["generation"] = generation
        self._write(data)

    def get(self, subtask_id: str) -> dict | None:
        return self._read().get(subtask_id)

    def clear(self, subtask_id: str) -> None:
        data = self._read()
        data.pop(subtask_id, None)
        self._write(data)

    def active_entries(self) -> dict:
        return self._read()

    def recover(self) -> list[dict]:
        """Apply PRD 15.3 recovery rules; returns decisions per subtask."""
        data = self._read()
        decisions = []
        for subtask_id, entry in data.items():
            stage = entry.get("stage", STAGE_NEW)
            if stage in RECOVERY_ABANDON:
                decision = {"subtask_id": subtask_id, "action": "abandon"}
            elif stage in RECOVERY_ABORT:
                decision = {"subtask_id": subtask_id, "action": "aborted", "retryable": True}
            elif stage in RECOVERY_UNVERIFIED:
                decision = {
                    "subtask_id": subtask_id,
                    "action": "unverified",
                    "retryable": False,
                }
            else:
                decision = {"subtask_id": subtask_id, "action": "done"}
            decisions.append(decision)
        return decisions
