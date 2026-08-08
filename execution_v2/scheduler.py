"""Bounded, cleanup-first batch execution for isolated browser execution V2."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
import re
import sqlite3
from typing import Any

from .models import BrowserBinding, JobStatus, ProfileOutcome, ProfileStatus, Stage


_MAX_CLOSE_ATTEMPTS = 3
_UNEXPECTED_ERROR_CODE = "unexpected_execution_error"
_WEBSOCKET_URL = re.compile(r"(?i)\bwss?://[^\s]+")
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|password)\s*[:=]\s*[^\s,;]+"
)


class BatchScheduler:
    """Run a saved strategy snapshot in bounded AdsPower Profile batches."""

    def __init__(
        self,
        store: Any,
        adspower: Any,
        sessions: Any,
        execute_profile: Callable[[BrowserBinding, dict[str, Any]], Awaitable[ProfileOutcome]],
        tile_batch: Callable[[Sequence[BrowserBinding]], Awaitable[None]] | None = None,
    ) -> None:
        self.store = store
        self.adspower = adspower
        self.sessions = sessions
        self.execute_profile = execute_profile
        self.tile_batch = tile_batch
        self._cancelled: set[str] = set()

    async def cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)
        self.store.request_cancel(job_id)

    async def run(
        self,
        job_id: str,
        strategy_id: str,
        snapshot: dict[str, Any],
        profile_ids: Sequence[str],
        batch_size: int = 3,
    ) -> dict[str, Any]:
        profiles = self._validate(profile_ids, batch_size)
        if self.store.get_job(job_id) is None:
            try:
                self.store.prepare_job(job_id, strategy_id, snapshot, profiles, batch_size)
            except sqlite3.IntegrityError:
                if self.store.get_job(job_id) is None:
                    raise
        return await self.run_existing(job_id)

    async def run_existing(self, job_id: str) -> dict[str, Any]:
        """Run already-persisted work once; concurrent callers cannot replay it."""
        job = self.store.get_job_or_raise(job_id)
        profiles = [row["profile_id"] for row in self.store.list_profile_results(job_id)]
        batch_size = int(job["batch_size"])
        total_batches = (len(profiles) + batch_size - 1) // batch_size

        if not self.store.claim_queued_job(job_id):
            return {
                "job_id": job_id,
                "status": self.store.get_job_or_raise(job_id)["status"],
                "total_batches": total_batches,
            }

        snapshot = job["strategy_snapshot"]

        for first_index in range(0, len(profiles), batch_size):
            if self._is_cancelled(job_id):
                return self._finish(job_id, JobStatus.CANCELLED, total_batches)

            started: list[str] = []
            records: dict[str, ProfileOutcome] = {}
            cleanup_confirmed = True
            try:
                bindings = await self._start_and_bind_batch(
                    job_id,
                    snapshot,
                    profiles[first_index : first_index + batch_size],
                    started,
                    records,
                )
                if await self._tile_bindings(job_id, bindings, records):
                    await self._execute_batch(job_id, snapshot, bindings, records)
            except Exception as exc:
                self._mark_unhandled_batch_error(
                    job_id, profiles[first_index : first_index + batch_size], records, exc
                )
            finally:
                cleanup_confirmed = await self._cleanup_batch(job_id, started, records)

            if not cleanup_confirmed:
                return self._finish(job_id, JobStatus.CLEANUP_BLOCKED, total_batches)
            if self._is_cancelled(job_id):
                return self._finish(job_id, JobStatus.CANCELLED, total_batches)

        return self._finish(job_id, JobStatus.COMPLETED, total_batches)

    async def cleanup_after_restart(self) -> list[dict[str, Any]]:
        """Stop orphaned queued/running work without ever executing it again."""
        recovered: list[dict[str, Any]] = []
        for job in self.store.list_recoverable_jobs():
            job_id = job["id"]
            confirmed = True
            for row in self.store.list_profile_results(job_id):
                profile_id = row["profile_id"]
                self.store.set_profile_status(
                    job_id, profile_id, ProfileStatus.CLOSING, Stage.ADSPOWER_STOP
                )
                inactive = await self._stop_and_confirm(profile_id)
                if inactive:
                    self.store.set_profile_status(
                        job_id,
                        profile_id,
                        ProfileStatus.FAILED,
                        Stage.ADSPOWER_STOP,
                        error_code="service_restarted",
                        error_summary="Service restarted before this job could finish.",
                        close_confirmed=True,
                    )
                else:
                    confirmed = False
                    self.store.set_profile_status(
                        job_id,
                        profile_id,
                        ProfileStatus.CLEANUP_FAILED,
                        Stage.ADSPOWER_STOP,
                        error_code="cleanup_blocked",
                        error_summary="Service restart cleanup could not confirm the browser inactive.",
                        close_confirmed=False,
                    )
            status = JobStatus.CANCELLED if confirmed else JobStatus.CLEANUP_BLOCKED
            self.store.set_job_status(job_id, status)
            recovered.append({"job_id": job_id, "status": status.value})
        return recovered

    async def _start_and_bind_batch(
        self,
        job_id: str,
        snapshot: dict[str, Any],
        profile_ids: Sequence[str],
        started: list[str],
        records: dict[str, ProfileOutcome],
    ) -> list[BrowserBinding]:
        del snapshot
        bindings: list[BrowserBinding] = []
        for profile_id in profile_ids:
            if self._is_cancelled(job_id):
                break
            self.store.set_profile_status(
                job_id, profile_id, ProfileStatus.STARTING, Stage.ADSPOWER_START
            )
            # A Local API request can start the browser then fail before returning.
            # Track it first so cleanup still closes that browser.
            started.append(profile_id)
            try:
                ws_url = await self.adspower.start(profile_id)
            except Exception as exc:
                records[profile_id] = self._failed(profile_id, Stage.ADSPOWER_START, exc)
                self._store_outcome(job_id, records[profile_id], close_confirmed=False)
                continue

            self.store.set_profile_status(
                job_id, profile_id, ProfileStatus.CONNECTING_CDP, Stage.CDP_CONNECT
            )
            try:
                bindings.append(await self.sessions.connect(profile_id, ws_url))
            except Exception as exc:
                records[profile_id] = self._failed(profile_id, Stage.CDP_CONNECT, exc)
                self._store_outcome(job_id, records[profile_id], close_confirmed=False)
        return bindings

    async def _tile_bindings(
        self,
        job_id: str,
        bindings: Sequence[BrowserBinding],
        records: dict[str, ProfileOutcome],
    ) -> bool:
        if not bindings or self.tile_batch is None:
            return True
        for binding in bindings:
            self.store.set_profile_status(
                job_id, binding.profile_id, ProfileStatus.TILING, Stage.WINDOW_TILE
            )
        try:
            await self.tile_batch(bindings)
        except Exception:
            for binding in bindings:
                outcome = ProfileOutcome(
                    binding.profile_id,
                    False,
                    Stage.WINDOW_TILE,
                    "window_tile_failed",
                    "window_tile_failed",
                )
                records[binding.profile_id] = outcome
                self._store_outcome(job_id, outcome, close_confirmed=False)
            return False
        return True

    async def _execute_batch(
        self,
        job_id: str,
        snapshot: dict[str, Any],
        bindings: Sequence[BrowserBinding],
        records: dict[str, ProfileOutcome],
    ) -> None:
        for binding in bindings:
            self.store.set_profile_status(
                job_id, binding.profile_id, ProfileStatus.EXECUTING, Stage.EXECUTE_ACTION
            )
        results = await asyncio.gather(
            *(self.execute_profile(binding, snapshot) for binding in bindings),
            return_exceptions=True,
        )
        for binding, result in zip(bindings, results, strict=True):
            if isinstance(result, Exception):
                records[binding.profile_id] = self._failed(
                    binding.profile_id, Stage.EXECUTE_ACTION, result
                )
            elif result.profile_id != binding.profile_id:
                records[binding.profile_id] = ProfileOutcome(
                    binding.profile_id,
                    False,
                    Stage.EXECUTE_ACTION,
                    "profile_binding_mismatch",
                    "Executor returned a result for another Profile.",
                )
            else:
                records[binding.profile_id] = result
            self._store_outcome(job_id, records[binding.profile_id], close_confirmed=False)

    async def _cleanup_batch(
        self,
        job_id: str,
        started: Sequence[str],
        records: dict[str, ProfileOutcome],
    ) -> bool:
        all_confirmed = True
        for profile_id in started:
            self.store.set_profile_status(
                job_id, profile_id, ProfileStatus.CLOSING, Stage.ADSPOWER_STOP
            )
            confirmed = await self._stop_and_confirm(profile_id)
            outcome = records.get(profile_id)
            if confirmed:
                if outcome is None:
                    outcome = ProfileOutcome(
                        profile_id,
                        False,
                        Stage.ADSPOWER_STOP,
                        _UNEXPECTED_ERROR_CODE,
                        "Profile ended without an execution result.",
                    )
                    self._store_outcome(job_id, outcome, close_confirmed=False)
                self._set_outcome_status(job_id, outcome, close_confirmed=True)
            else:
                all_confirmed = False
                self.store.set_profile_status(
                    job_id,
                    profile_id,
                    ProfileStatus.CLEANUP_FAILED,
                    Stage.ADSPOWER_STOP,
                    error_code="cleanup_not_confirmed",
                    error_summary="AdsPower did not confirm the browser inactive after three checks.",
                    close_confirmed=False,
                )
        return all_confirmed

    async def _stop_and_confirm(self, profile_id: str) -> bool:
        for _ in range(_MAX_CLOSE_ATTEMPTS):
            try:
                await self.adspower.stop(profile_id)
            except Exception:
                pass
            try:
                inactive = not await self.adspower.is_active(profile_id)
            except Exception:
                inactive = False
            if inactive:
                return True
        return False

    def _store_outcome(
        self, job_id: str, outcome: ProfileOutcome, *, close_confirmed: bool
    ) -> None:
        self._set_outcome_status(job_id, outcome, close_confirmed=close_confirmed)
        for index, result in enumerate(outcome.action_results):
            self.store.append_action_result(
                job_id,
                outcome.profile_id,
                index,
                str(result.get("action_type", result.get("type", "action"))),
                str(result.get("status", "succeeded" if outcome.succeeded else "failed")),
                result.get("stage", outcome.stage),
                result,
            )

    def _set_outcome_status(
        self, job_id: str, outcome: ProfileOutcome, *, close_confirmed: bool
    ) -> None:
        status = ProfileStatus.SUCCEEDED if outcome.succeeded else ProfileStatus.FAILED
        self.store.set_profile_status(
            job_id,
            outcome.profile_id,
            status,
            outcome.stage,
            error_code=outcome.error_code,
            error_summary=outcome.error_summary,
            close_confirmed=close_confirmed,
        )

    def _mark_unhandled_batch_error(
        self,
        job_id: str,
        profile_ids: Sequence[str],
        records: dict[str, ProfileOutcome],
        exc: Exception,
    ) -> None:
        for profile_id in profile_ids:
            if profile_id not in records:
                records[profile_id] = self._failed(profile_id, Stage.EXECUTE_ACTION, exc)
                self._store_outcome(job_id, records[profile_id], close_confirmed=False)

    def _failed(self, profile_id: str, stage: Stage, exc: Exception) -> ProfileOutcome:
        return ProfileOutcome(
            profile_id,
            False,
            stage,
            _UNEXPECTED_ERROR_CODE,
            self._summary(exc),
        )

    def _is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled or self.store.is_cancel_requested(job_id)

    def _finish(
        self, job_id: str, status: JobStatus, total_batches: int
    ) -> dict[str, Any]:
        self.store.set_job_status(job_id, status)
        return {"job_id": job_id, "status": status.value, "total_batches": total_batches}

    @staticmethod
    def _validate(profile_ids: Sequence[str], batch_size: int) -> list[str]:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 8:
            raise ValueError("batch_size must be an integer between 1 and 8")
        profiles = list(profile_ids)
        if not profiles or any(not isinstance(profile, str) or not profile.strip() for profile in profiles):
            raise ValueError("profile_ids must contain non-empty strings")
        if len(set(profiles)) != len(profiles):
            raise ValueError("profile_ids must be unique")
        return profiles

    @staticmethod
    def _summary(exc: Exception) -> str:
        text = str(exc).strip() or exc.__class__.__name__
        text = _WEBSOCKET_URL.sub("[redacted-websocket]", text)
        text = _SENSITIVE_ASSIGNMENT.sub(r"\1=[redacted]", text)
        return text[:240]


__all__ = ["BatchScheduler"]
