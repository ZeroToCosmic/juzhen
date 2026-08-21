"""Agent main loop: heartbeat, pull, execute, renew, report (M2 increment 3)."""

from __future__ import annotations

import logging
import threading
import time
import uuid

from agent import config
from agent.client import CentralClient, CentralError
from agent.protocol import Executor, ExecutionOutcome

log = logging.getLogger(__name__)


class AgentWorker:
    def __init__(
        self,
        client: CentralClient,
        executor: Executor,
        *,
        capabilities: dict | None = None,
        heartbeat_interval: float | None = None,
        renew_interval: float | None = None,
        stop_event: threading.Event | None = None,
    ):
        self.client = client
        self.executor = executor
        self.capabilities = capabilities or {
            "actions": ["open", "scroll", "submit"],
            "strategy_versions": ["1.0.0"],
        }
        self.heartbeat_interval = heartbeat_interval or config.HEARTBEAT_INTERVAL_SECONDS
        self.renew_interval = renew_interval or config.RENEW_INTERVAL_SECONDS
        self.stop_event = stop_event or threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def start(self) -> None:
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="agent-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def stop(self) -> None:
        self.stop_event.set()

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.client.heartbeat(
                    capabilities=self.capabilities,
                    running_windows=self._running_windows(),
                    queue_depth=self._queue_depth(),
                )
            except CentralError as error:
                log.warning("heartbeat failed: %s", error)
            self.stop_event.wait(self.heartbeat_interval)

    def _running_windows(self) -> int:
        return 0

    def _queue_depth(self) -> int:
        return 0

    def run_once(self) -> dict:
        try:
            subtasks = self.client.pull_subtasks()
        except CentralError as error:
            return {"pulled": 0, "processed": 0, "error": str(error)}
        processed = 0
        for subtask in subtasks:
            self._process(subtask)
            processed += 1
        return {"pulled": len(subtasks), "processed": processed}

    def run_forever(self, interval: float = 5.0) -> None:
        while not self.stop_event.is_set():
            try:
                self.run_once()
            except CentralError as error:
                log.warning("pull failed: %s", error)
            self.stop_event.wait(interval)

    def _process(self, subtask: dict) -> None:
        subtask_id = subtask["subtask_id"]
        generation = subtask["lease_generation"]
        started = time.monotonic()
        outcome = self.executor.execute(subtask)
        duration_ms = int((time.monotonic() - started) * 1000)

        if outcome.status == "SUCCESS":
            try:
                self.client.submit_result(
                    subtask_id=subtask_id,
                    generation=generation,
                    status="SUCCESS",
                    result_data=outcome.result_data,
                    duration_ms=duration_ms,
                    msg_id=f"r-{uuid.uuid4().hex}",
                )
            except CentralError as error:
                log.warning("result submit failed for %s: %s", subtask_id, error)
            if outcome.handle is not None:
                try:
                    self.client.submit_handle(
                        subtask_id=subtask_id,
                        verification_status=outcome.handle_verification,
                        content=outcome.handle,
                        text_hash=outcome.text_hash,
                    )
                except CentralError:
                    log.exception("handle submit failed for %s", subtask_id)
        else:
            try:
                self.client.submit_result(
                    subtask_id=subtask_id,
                    generation=generation,
                    status="FAILED",
                    error_category=outcome.error_category,
                    error_code=outcome.error_code,
                    duration_ms=duration_ms,
                    msg_id=f"r-{uuid.uuid4().hex}",
                )
            except CentralError as error:
                log.warning("failure report failed for %s: %s", subtask_id, error)
