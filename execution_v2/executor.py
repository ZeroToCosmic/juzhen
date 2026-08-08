"""Ordered V2 strategy execution for one bound AdsPower Profile."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import inspect
from pathlib import PurePosixPath
import random
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .actions import execute_action
from .models import BrowserBinding, ProfileOutcome, Stage
from .models import ProfileStatus
from .readiness import wait_until_ready


_WEBSOCKET_URL = re.compile(r"(?i)\bwss?://[^\s]+")


class StrategyExecutor:
    """Navigate once, verify readiness once, then run saved blocks in their stored order."""

    def __init__(
        self,
        resolver: Any,
        *,
        text_resolver: Callable[[dict[str, Any]], Awaitable[str]] | None = None,
        action_executor: Callable[..., Awaitable[dict[str, Any]]] = execute_action,
        readiness_waiter: Callable[..., Awaitable[Any]] = wait_until_ready,
        rng: Any = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        on_stage: Callable[[str, ProfileStatus, Stage], Awaitable[None] | None] | None = None,
        capture_failure: Callable[[BrowserBinding, ProfileOutcome], Awaitable[Any] | Any] | None = None,
    ) -> None:
        self._resolver = resolver
        self._text_resolver = text_resolver or _missing_text_resolver
        self._action_executor = action_executor
        self._readiness_waiter = readiness_waiter
        self._rng = rng if rng is not None else random
        self._sleep = sleep
        self._clock = clock
        self._on_stage = on_stage
        self._capture_failure = capture_failure

    async def run(self, binding: BrowserBinding, strategy_snapshot: dict[str, Any]) -> ProfileOutcome:
        try:
            strategy, elements = _snapshot_parts(strategy_snapshot)
        except Exception as error:
            return await self._failure(
                binding, Stage.EXECUTE_ACTION, "invalid_strategy_snapshot", error
            )

        try:
            await self._notify_stage(
                binding.profile_id, ProfileStatus.NAVIGATING, Stage.NAVIGATE
            )
            await binding.page.goto(strategy["target_url"])
        except Exception as error:
            return await self._failure(binding, Stage.NAVIGATE, "navigation_failed", error)

        ready_element = elements.get(strategy["ready_element_id"])
        try:
            await self._notify_stage(
                binding.profile_id, ProfileStatus.WAITING_READINESS, Stage.READINESS
            )
            await self._readiness_waiter(
                binding.page,
                ready_element["definition"],
                self._resolver,
                timeout_seconds=strategy["readiness_timeout_seconds"],
                sleep=self._sleep,
                clock=self._clock,
            )
        except Exception as error:
            return await self._failure(
                binding,
                Stage.READINESS,
                str(getattr(error, "code", "readiness_failed")),
                error,
            )

        results: list[dict[str, Any]] = []
        deadline = _deadline(strategy, self._clock, self._rng)
        await self._notify_stage(
            binding.profile_id, ProfileStatus.EXECUTING, Stage.EXECUTE_ACTION
        )
        while True:
            for index, action in enumerate(strategy["actions"]):
                try:
                    result = await self._action_executor(
                        binding.page,
                        action,
                        elements,
                        self._resolver,
                        self._text_resolver,
                        rng=self._rng,
                        sleep=self._sleep,
                        wheel_calibration=strategy_snapshot.get(
                            "wheel_calibration"
                        ),
                    )
                except Exception as error:
                    results.append(_failed_action(index, action, error))
                    return await self._failure(
                        binding,
                        Stage.EXECUTE_ACTION,
                        str(getattr(error, "code", "action_execution_failed")),
                        error,
                        tuple(results),
                    )
                results.append(_sanitized_result(index, action, result))
            if deadline is None or self._clock() >= deadline:
                break
        return ProfileOutcome(binding.profile_id, True, Stage.EXECUTE_ACTION, action_results=tuple(results))

    async def _notify_stage(
        self, profile_id: str, status: ProfileStatus, stage: Stage
    ) -> None:
        if self._on_stage is None:
            return
        try:
            value = self._on_stage(profile_id, status, stage)
            if inspect.isawaitable(value):
                await value
        except Exception:
            return

    async def _failure(
        self,
        binding: BrowserBinding,
        stage: Stage,
        code: str,
        error: Exception,
        action_results: tuple[dict[str, Any], ...] = (),
    ) -> ProfileOutcome:
        outcome = ProfileOutcome(
            binding.profile_id,
            False,
            stage,
            code,
            _summary(error),
            action_results,
        )
        return await self._capture_failure_evidence(binding, outcome)

    async def _capture_failure_evidence(
        self, binding: BrowserBinding, outcome: ProfileOutcome
    ) -> ProfileOutcome:
        if self._capture_failure is None:
            return outcome
        await self._notify_stage(
            binding.profile_id, ProfileStatus.CAPTURING_EVIDENCE, Stage.CAPTURE_EVIDENCE
        )
        try:
            value = self._capture_failure(binding, outcome)
            path = await value if inspect.isawaitable(value) else value
            safe_path = _safe_relative_evidence_path(path)
        except Exception:
            return outcome
        if safe_path is None:
            return outcome
        evidence = {
            "index": len(outcome.action_results),
            "action_id": "capture_failure",
            "action_type": "capture_evidence",
            "status": "succeeded",
            "stage": Stage.CAPTURE_EVIDENCE.value,
            "evidence_path": safe_path,
        }
        return replace(outcome, action_results=(*outcome.action_results, evidence))


def _snapshot_parts(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    strategy = snapshot["strategy"]
    elements = {element["id"]: element for element in snapshot["elements"]}
    if strategy["ready_element_id"] not in elements:
        raise ValueError("ready element is absent")
    return strategy, elements


def _deadline(strategy: dict[str, Any], clock: Callable[[], float], rng: Any) -> float | None:
    if strategy["run_mode"] == "once":
        return None
    duration = strategy["loop_duration_minutes"]
    sampler = rng or __import__("random")
    return clock() + sampler.uniform(float(duration[0]), float(duration[1])) * 60


def _sanitized_result(index: int, action: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "duration_seconds", "direction", "distance_pixels", "count", "interval_seconds",
        "requested_switches", "completed_switches", "wheel_events", "calibration_revision",
        "button", "click_count", "hold_seconds", "after_seconds", "content_source", "text_length",
    }
    details = {key: value for key, value in result.items() if key in allowed}
    return {
        "index": index,
        "action_id": action["id"],
        "action_type": action["type"],
        "status": "succeeded",
        **details,
    }


def _failed_action(index: int, action: dict[str, Any], error: Exception) -> dict[str, Any]:
    return {
        "index": index,
        "action_id": action["id"],
        "action_type": action["type"],
        "status": "failed",
        "error_code": str(getattr(error, "code", "action_execution_failed")),
    }


def _safe_relative_evidence_path(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("path")
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip().replace("\\", "/")
    if candidate.startswith(("/", "~")) or ":" in candidate:
        return None
    path = PurePosixPath(candidate)
    if any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _summary(error: Exception) -> str:
    text = str(error).strip() or error.__class__.__name__
    return _WEBSOCKET_URL.sub("[redacted-websocket]", text)[:240]


async def _missing_text_resolver(_action: dict[str, Any]) -> str:
    raise RuntimeError("content_library_unavailable")


__all__ = ["StrategyExecutor"]
