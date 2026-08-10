"""Agent executor protocol and stub executor (M2 increment 3).

The Executor protocol is the seam where the existing execution_v2 /
comment_campaign kernels plug in: each executor consumes the frozen
config_snapshot and reports an outcome with handle evidence. The stub
executor is used for protocol-level integration tests; Playwright-backed
executors land when the legacy kernels are adapted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ExecutionOutcome:
    status: str
    error_category: str = ""
    error_code: str = ""
    result_data: dict = field(default_factory=dict)
    handle: dict | None = None
    handle_verification: str = "VERIFIED"
    text_hash: str = ""


class Executor(Protocol):
    def execute(self, subtask: dict) -> ExecutionOutcome: ...


class StubExecutor:
    """Deterministic executor for integration tests."""

    def __init__(self, *, fail: bool = False, error_category: str = ""):
        self.fail = fail
        self.error_category = error_category
        self.calls: list[dict] = []

    def execute(self, subtask: dict) -> ExecutionOutcome:
        self.calls.append(subtask)
        if self.fail:
            return ExecutionOutcome(
                status="FAILED",
                error_category=self.error_category or "retryable",
                error_code="stub_failure",
            )
        return ExecutionOutcome(
            status="SUCCESS",
            result_data={"stub": True, "task_type": subtask.get("config_snapshot", {}).get("params", {}).get("text", "")},
            handle={"comment_id": f"c-{subtask['subtask_id'][:8]}"},
        )
