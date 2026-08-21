"""Execution V2 kernel adapter for the Agent (M4 increment 3).

Wraps the existing execution_v2 components (AdsPowerAdapter,
SessionFactory, StrategyExecutor) behind the agent Executor protocol so
the central pipeline can drive real browser work. The adapter owns the
lifecycle: start profile -> connect CDP -> run strategy -> evidence ->
stop + confirm. Lease renewal hooks run between stages for long tasks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from execution_v2.executor import StrategyExecutor
from execution_v2.models import ProfileOutcome
from execution_v2.session import SessionFactory

from agent.protocol import ExecutionOutcome, Executor

LeaseRenewer = Callable[[], None]

STAGE_ERROR_CATEGORY = {
    "navigation_failed": "retryable",
    "readiness_failed": "retryable",
    "action_execution_failed": "retryable",
    "invalid_strategy_snapshot": "strategy",
    "adspower_start_failed": "environment",
    "session_connect_failed": "environment",
    "adspower_stop_failed": "environment",
}


class ExecutionV2Executor:
    def __init__(
        self,
        adspower: Any,
        session_factory: SessionFactory,
        *,
        strategy_executor: StrategyExecutor | None = None,
        lease_renewer: LeaseRenewer | None = None,
        close_browser: Callable[[Any], Awaitable[None]] | None = None,
    ) -> None:
        self._adspower = adspower
        self._session_factory = session_factory
        self._strategy_executor = strategy_executor
        self._lease_renewer = lease_renewer
        self._close_browser = close_browser

    def execute(self, subtask: dict) -> ExecutionOutcome:
        return asyncio.run(self._execute(subtask))

    def _renew(self) -> None:
        if self._lease_renewer is not None:
            try:
                self._lease_renewer()
            except BaseException:
                pass

    async def _on_stage(self, _profile_id, _status, _stage) -> None:
        self._renew()

    async def _execute(self, subtask: dict) -> ExecutionOutcome:
        snapshot = subtask.get("config_snapshot") or {}
        profile_id = str(subtask.get("profile_id") or "").strip()
        if not profile_id:
            return ExecutionOutcome(
                status="FAILED",
                error_category="environment",
                error_code="missing_profile_id",
            )
        if "strategy" not in snapshot or "elements" not in snapshot:
            return ExecutionOutcome(
                status="FAILED",
                error_category="strategy",
                error_code="missing_strategy_snapshot",
            )

        ws_url = None
        binding = None
        try:
            try:
                ws_url = await self._adspower.start(profile_id)
            except BaseException as error:
                return self._failure("adspower_start_failed", error)
            try:
                binding = await self._session_factory.connect(profile_id, ws_url)
            except BaseException as error:
                return self._failure("session_connect_failed", error)

            executor = self._strategy_executor or StrategyExecutor(
                binding.resolver,
                on_stage=self._on_stage,
            )
            try:
                outcome = await executor.run(binding, snapshot)
            except BaseException as error:
                return self._failure("v2_execution_failed", error)
            return self._map_outcome(outcome)
        finally:
            if binding is not None:
                try:
                    if self._close_browser is not None:
                        await self._close_browser(binding.browser)
                    else:
                        close = getattr(binding.browser, "close", None)
                        if callable(close):
                            await close()
                except BaseException:
                    pass
            if ws_url:
                try:
                    await self._adspower.stop(profile_id)
                except BaseException:
                    pass

    def _failure(self, code: str, error: BaseException) -> ExecutionOutcome:
        return ExecutionOutcome(
            status="FAILED",
            error_category=STAGE_ERROR_CATEGORY.get(code, "retryable"),
            error_code=code,
            result_data={"detail": str(error)[:200]},
        )

    def _map_outcome(self, outcome: ProfileOutcome) -> ExecutionOutcome:
        results = list(getattr(outcome, "action_results", ()) or ())
        succeeded = bool(getattr(outcome, "succeeded", getattr(outcome, "success", False)))
        if succeeded:
            return ExecutionOutcome(
                status="SUCCESS",
                result_data={
                    "stage": str(outcome.stage),
                    "action_count": len(results),
                    "actions": [dict(item) for item in results],
                },
            )
        return ExecutionOutcome(
            status="FAILED",
            error_category="retryable",
            error_code="v2_execution_failed",
            result_data={
                "stage": str(outcome.stage),
                "action_results": [dict(item) for item in results],
            },
        )
