"""Flask-safe service boundary for isolated browser execution V2.

Only this module translates public Profile tokens into raw AdsPower IDs.  The
raw values stay inside the owned asyncio runtime and SQLite records; all return
values are redacted before reaching an HTTP route.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Sequence
from concurrent.futures import Future
from contextvars import ContextVar
from dataclasses import dataclass
import inspect
from pathlib import Path
import re
import threading
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

from browser_public_identity import mask_profile_id
from remote_actions.publication import PublicationActor

from .executor import StrategyExecutor
from .locator import LocatorResolutionError, StrictLocatorResolver
from .models import BrowserBinding, JobStatus, ProfileStatus, Stage
from .picker import PickerService, PickerSession
from .runtime import AsyncRuntime
from .scheduler import BatchScheduler
from .session import PlaywrightSessionFactory
from .store import ExecutionStore
from .tiling import tile_browser_bindings
from .wheel_calibration import WheelCalibrationRunner
from .wheel_calibration import WheelCalibrationError


_SECRET_KEY = re.compile(r"(?i)(api[_-]?key|authorization|cookie|password|websocket|ws_url)")
_WEBSOCKET = re.compile(r"(?i)\bwss?://[^\s]+")
_TERMINAL_JOB = {"completed", "cancelled", "cleanup_blocked"}
_SAFE_EVIDENCE_PATH = re.compile(r"^evidence/[A-Za-z0-9][A-Za-z0-9_.-]{0,239}\.png$")
_SAFE_ERROR_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


class ProfileTokenError(ValueError):
    """A request supplied a stale, unknown, or ambiguous public Profile token."""


class PickerSessionNotFoundError(KeyError):
    """Picker session has already been finished, cancelled, or never existed."""


class ExecutionConflictError(RuntimeError):
    """A V2 job or Profile lease conflicts with active browser work."""


@dataclass(slots=True)
class _Profile:
    raw_id: str
    token: str
    display_id: str
    label: str
    status: str


@dataclass(slots=True)
class _PickerRecord:
    raw_profile_id: str
    picker: PickerSession
    binding: BrowserBinding
    current_selection: dict[str, Any] | None = None


@dataclass(slots=True)
class _WheelCalibrationRecord:
    session_id: str
    raw_profile_id: str
    binding: BrowserBinding
    lease_owner: str
    cancel_event: asyncio.Event
    state: dict[str, Any]
    task: asyncio.Task[Any] | None = None


class _OwnedPlaywrightSessions:
    """Start and stop Playwright only on the service's owned runtime loop."""

    def __init__(self, playwright_factory: Callable[[], Any]) -> None:
        self._playwright_factory = playwright_factory
        self._playwright: Any | None = None
        self._factory: PlaywrightSessionFactory | None = None
        self._lock = asyncio.Lock()

    async def connect(self, profile_id: str, ws_url: str) -> BrowserBinding:
        async with self._lock:
            if self._factory is None:
                starter = self._playwright_factory()
                self._playwright = await starter.start()
                self._factory = PlaywrightSessionFactory(self._playwright)
        return await self._factory.connect(profile_id, ws_url)

    async def close(self) -> None:
        async with self._lock:
            playwright, self._playwright, self._factory = self._playwright, None, None
        if playwright is not None:
            value = playwright.stop()
            if inspect.isawaitable(value):
                await value


def create_default_execution_v2_service(
    *,
    db_path: str | Path = "data/execution_v2/execution_v2.db",
    evidence_dir: str | Path = "data/execution_v2/evidence",
    controller: Any | None = None,
    profile_provider: Callable[[], Any] | None = None,
    content_library_provider: Callable[[], Any] | None = None,
    text_resolver: Callable[[dict[str, Any]], Any] | None = None,
    runtime: AsyncRuntime | None = None,
) -> "ExecutionV2Service":
    """Build the production V2 service without importing gateway application code.

    The repository's current ``AdsPowerController`` has no list_profiles
    method.  Callers can inject the gateway's safe provider until that narrow
    controller method is added; no duplicate Local-API HTTP client is hidden
    here.
    """

    from adspower import AdsPowerController
    from execution_v2.adspower_adapter import RateLimitedAdsPowerAdapter

    actual_controller = controller or AdsPowerController()
    if profile_provider is None:
        provider = getattr(actual_controller, "list_profiles", None)
        if not callable(provider):
            def unavailable_provider() -> list[dict[str, str]]:
                raise RuntimeError("adspower_profile_listing_unavailable")
            profile_provider = unavailable_provider
        else:
            async def threaded_provider() -> Any:
                return await asyncio.to_thread(provider)
            profile_provider = threaded_provider
    if runtime is None:
        runtime = AsyncRuntime()
    # Import lazily: unit tests and non-browser commands must not require the
    # Playwright package to load just by importing this module.
    def playwright_factory() -> Any:
        from playwright.async_api import async_playwright
        return async_playwright()

    return ExecutionV2Service(
        store=ExecutionStore(db_path),
        adspower=RateLimitedAdsPowerAdapter(actual_controller),
        sessions=_OwnedPlaywrightSessions(playwright_factory),
        profile_provider=profile_provider,
        content_library_provider=content_library_provider,
        text_resolver=text_resolver,
        runtime=runtime,
        close_runtime=True,
        evidence_dir=evidence_dir,
        batch_tiler=tile_browser_bindings,
    )


class ExecutionV2Service:
    """Own V2 execution state without importing Flask or legacy gateway code."""

    def __init__(
        self,
        *,
        store: ExecutionStore | None = None,
        db_path: str | Path = "data/execution_v2/execution_v2.db",
        adspower: Any,
        sessions: Any,
        scheduler: BatchScheduler | None = None,
        picker: PickerService | None = None,
        resolver: Any | None = None,
        profile_provider: Callable[[], Any] | None = None,
        content_library_provider: Callable[[], Any] | None = None,
        text_resolver: Callable[[dict[str, Any]], Any] | None = None,
        runtime: AsyncRuntime | None = None,
        close_runtime: bool | None = None,
        id_factory: Callable[[], str] | None = None,
        evidence_dir: str | Path = "data/execution_v2/evidence",
        batch_tiler: Callable[
            [Sequence[BrowserBinding]], Awaitable[None]
        ] | None = None,
        wheel_runner: Any | None = None,
    ) -> None:
        self.store = store or ExecutionStore(db_path)
        self.store.initialize()
        self.adspower = adspower
        self.sessions = sessions
        self.runtime = runtime or AsyncRuntime()
        self._owns_runtime = runtime is None if close_runtime is None else close_runtime
        self._resolver = resolver or StrictLocatorResolver()
        self._picker_service = picker or PickerService(resolver=self._resolver)
        self._profile_provider = profile_provider or (lambda: [])
        self._content_library_provider = content_library_provider or (lambda: [])
        self._id_factory = id_factory or (lambda: str(uuid4()))
        self._evidence_dir = Path(evidence_dir)
        self._profiles_by_token: dict[str, _Profile] = {}
        self._raw_to_token: dict[str, str] = {}
        self._pickers: dict[str, _PickerRecord] = {}
        self._wheel_runner = wheel_runner or WheelCalibrationRunner()
        self._wheel_calibration: _WheelCalibrationRecord | None = None
        self._wheel_calibration_starting = False
        self._jobs: dict[str, Future[Any]] = {}
        self._job_profile_leases: dict[str, tuple[str, ...]] = {}
        self._profile_leases: dict[str, str] = {}
        self._running_job_id: ContextVar[str | None] = ContextVar(
            "execution_v2_running_job_id", default=None
        )
        self._closed = False
        self._closing = False
        self._close_lock = threading.Lock()

        if scheduler is None:
            execution = StrategyExecutor(
                self._resolver,
                text_resolver=text_resolver,
                on_stage=self._persist_executor_stage,
                capture_failure=self._capture_failure,
            )
            scheduler = BatchScheduler(
                self.store,
                adspower,
                sessions,
                execution.run,
                tile_batch=batch_tiler,
            )
        self.scheduler = scheduler
        # Recovery is deliberate cleanup, never a replay.  Later calls await it
        # before opening a Profile so a restart cannot overlap old browser work.
        self._startup_future = self.runtime.submit(self.scheduler.cleanup_after_restart())

    # -- Profile discovery -------------------------------------------------

    def list_profiles(self) -> list[dict[str, str]]:
        return self._run(self._list_profiles())

    async def _list_profiles(self) -> list[dict[str, str]]:
        source = self._profile_provider()
        values = await source if inspect.isawaitable(source) else source
        if not isinstance(values, (list, tuple)):
            raise ValueError("profile_provider_invalid")
        discovered: dict[str, _Profile] = {}
        for item in values:
            raw_id, label, status = self._profile_fields(item)
            if not raw_id:
                continue
            token = self._raw_to_token.get(raw_id)
            if token is None:
                token = f"profile_{uuid4().hex}"
                self._raw_to_token[raw_id] = token
            discovered[token] = _Profile(raw_id, token, mask_profile_id(raw_id), label, status)
        self._profiles_by_token = discovered
        return [
            {
                "profile_token": item.token,
                "display_id": item.display_id,
                "name": item.label,
                "status": item.status,
            }
            for item in discovered.values()
        ]

    def list_content_libraries(self) -> list[dict[str, Any]]:
        return self._run(self._list_content_libraries())

    async def _list_content_libraries(self) -> list[dict[str, Any]]:
        source = self._content_library_provider()
        values = await source if inspect.isawaitable(source) else source
        if not isinstance(values, (list, tuple)):
            raise ValueError("content_library_provider_invalid")
        libraries: list[dict[str, Any]] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            library_id = _clean_text(item.get("id"), 120)
            if not library_id:
                continue
            count = item.get("copy_count", 0)
            if isinstance(count, bool):
                count = 0
            try:
                count = max(0, int(count))
            except (TypeError, ValueError):
                count = 0
            libraries.append(
                {
                    "id": library_id,
                    "name": _clean_text(item.get("name"), 120) or library_id,
                    "copy_count": count,
                }
            )
        return libraries

    @staticmethod
    def _profile_fields(item: Any) -> tuple[str, str, str]:
        if isinstance(item, str):
            return item.strip(), "", ""
        if not isinstance(item, dict):
            return "", "", ""
        raw = item.get("id", item.get("profile_id", ""))
        label = item.get("name", item.get("label", ""))
        status = item.get("status", "")
        return (
            raw.strip() if isinstance(raw, str) else "",
            _clean_text(label, 120),
            _clean_text(status, 40),
        )

    async def _resolve_profiles(self, tokens: list[str]) -> list[str]:
        await self._wait_startup()
        await self._list_profiles()
        raw_ids: list[str] = []
        for token in tokens:
            profile = self._profiles_by_token.get(token) if isinstance(token, str) else None
            if profile is None:
                raise ProfileTokenError("profile_token_invalid_or_expired")
            raw_ids.append(profile.raw_id)
        if not raw_ids or len(set(raw_ids)) != len(raw_ids):
            raise ProfileTokenError("profile_tokens_must_be_unique")
        return raw_ids

    # -- Picker lifecycle --------------------------------------------------

    def start_picker(self, profile_token: str, target_url: str) -> dict[str, str]:
        return self._run(self._start_picker(profile_token, target_url), timeout=60)

    async def _start_picker(self, profile_token: str, target_url: str) -> dict[str, str]:
        raw_id = (await self._resolve_profiles([profile_token]))[0]
        lease_owner = f"picker:{profile_token}"
        self._acquire_profiles([raw_id], lease_owner)
        started = False
        try:
            ws_url = await self.adspower.start(raw_id)
            started = True
            binding = await self.sessions.connect(raw_id, ws_url)
            await binding.page.goto(target_url)
            session = await self._picker_service.start(binding, target_url)
            session_id = self._new_id()
            self._pickers[session_id] = _PickerRecord(raw_id, session, binding)
            return {"session_id": session_id, "status": "waiting_for_selection"}
        except Exception:
            if started:
                await self._stop_and_confirm(raw_id)
            self._release_profiles([raw_id], lease_owner)
            raise

    def get_picker(self, session_id: str) -> dict[str, Any]:
        return self._run(self._get_picker(session_id))

    async def _get_picker(self, session_id: str) -> dict[str, Any]:
        record = self._picker_record(session_id)
        if record.current_selection is None:
            try:
                record.current_selection = await asyncio.wait_for(
                    record.picker.next_selection(), timeout=0.001
                )
            except TimeoutError:
                pass
        return self._public(
            {
                "id": session_id,
                "status": "selection_ready" if record.current_selection else "waiting_for_selection",
                "selection": record.current_selection,
                "saved_count": len(record.picker.selections),
            }
        )

    def save_picker_selection(
        self,
        session_id: str,
        name: str,
        purpose: str,
        kind: str,
        element_id: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        return self._run(
            self._save_picker_selection(
                session_id, name, purpose, kind, element_id, expected_revision
            )
        )

    async def _save_picker_selection(
        self,
        session_id: str,
        name: str,
        purpose: str,
        kind: str,
        element_id: str | None,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        record = self._picker_record(session_id)
        if (element_id is None) != (expected_revision is None):
            raise ValueError("picker_repick_requires_element_id_and_expected_revision")
        existing: dict[str, Any] | None = None
        if element_id is not None:
            if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
                raise ValueError("picker_repick_expected_revision_invalid")
            existing = self.store.get_element_or_raise(element_id)
            if (name, purpose, kind) != (
                existing["name"], existing["purpose"], existing["kind"]
            ):
                raise ValueError("picker_repick_fields_must_match_existing_element")
        if record.current_selection is None:
            await self._get_picker(session_id)
        if record.current_selection is None:
            raise ValueError("picker_selection_required")
        saved = await record.picker.save_selection(name, purpose, kind)
        record.current_selection = None
        if existing is None:
            element = self.store.create_element(
                self._new_id(), saved["name"], saved["purpose"], saved["kind"], saved["definition"]
            )
        else:
            element = self.store.repick_element(
                existing["id"], saved["definition"], expected_revision=expected_revision
            )
        return self._public(element)

    def finish_picker(self, session_id: str) -> dict[str, Any]:
        return self._run(self._close_picker(session_id, finish=True), timeout=30)

    def cancel_picker(self, session_id: str) -> dict[str, Any]:
        return self._run(self._close_picker(session_id, finish=False), timeout=30)

    async def _close_picker(self, session_id: str, *, finish: bool) -> dict[str, Any]:
        record = self._picker_record(session_id)
        error = None
        try:
            if finish:
                await record.picker.finish()
            else:
                await record.picker.cancel()
        except Exception:
            error = "picker_close_failed"
        cleanup = await self._stop_and_confirm(record.raw_profile_id)
        self._pickers.pop(session_id, None)
        self._release_profiles([record.raw_profile_id])
        return self._public(
            {
                "id": session_id,
                "status": "finished" if finish and error is None else "cancelled" if not finish else "close_failed",
                "cleanup": cleanup,
                "error_code": error or "",
            }
        )

    def _picker_record(self, session_id: str) -> _PickerRecord:
        record = self._pickers.get(session_id)
        if record is None:
            raise PickerSessionNotFoundError("picker_session_not_found")
        return record

    # -- Physical wheel calibration ---------------------------------------

    def start_wheel_calibration(
        self, profile_token: str, target_url: str
    ) -> dict[str, Any]:
        return self._run(
            self._start_wheel_calibration(profile_token, target_url), timeout=60
        )

    async def _start_wheel_calibration(
        self, profile_token: str, target_url: str
    ) -> dict[str, Any]:
        parsed_url = urlparse(str(target_url or ""))
        if parsed_url.scheme != "https" or not parsed_url.hostname:
            raise ValueError("wheel_calibration_target_url_invalid")
        active = self._wheel_calibration
        if self._wheel_calibration_starting or active is not None and active.state.get("status") in {
            "preparing",
            "waiting_for_sample",
            "validating",
            "dry_run",
            "cancelling",
        }:
            raise ExecutionConflictError("wheel_calibration_already_active")
        self._wheel_calibration_starting = True
        self._wheel_calibration = None
        raw_id = ""
        owner = ""
        started = False
        try:
            raw_id = (await self._resolve_profiles([profile_token]))[0]
            session_id = self._new_id()
            owner = f"wheel-calibration:{session_id}"
            self._acquire_profiles([raw_id], owner)
            ws_url = await self.adspower.start(raw_id)
            started = True
            binding = await self.sessions.connect(raw_id, ws_url)
            await binding.page.goto(target_url)
            await self._wheel_runner.prepare(binding.page)
            record = _WheelCalibrationRecord(
                session_id=session_id,
                raw_profile_id=raw_id,
                binding=binding,
                lease_owner=owner,
                cancel_event=asyncio.Event(),
                state={
                    "session_id": session_id,
                    "status": "waiting_for_sample",
                    "sample_index": 0,
                    "samples": ["waiting", "pending", "pending"],
                    "error_code": "",
                },
            )
            self._wheel_calibration = record
            record.task = asyncio.create_task(self._run_wheel_calibration(record))
            return self._public(record.state)
        except Exception:
            if started:
                await self._stop_and_confirm(raw_id)
            if raw_id:
                self._release_profiles([raw_id], owner)
            raise
        finally:
            self._wheel_calibration_starting = False

    async def _run_wheel_calibration(
        self, record: _WheelCalibrationRecord
    ) -> None:
        async def progress(update: dict[str, Any]) -> None:
            record.state.update(update)

        try:
            normalized = await self._wheel_runner.collect(
                record.binding.page, progress, record.cancel_event
            )
            published = self.store.publish_wheel_calibration(
                "tiktok_feed",
                normalized["direction"],
                normalized["events"],
                normalized["sample_count"],
                replay_validated=normalized.get("replay_validated") is True,
            )
            record.state.update(
                {
                    "status": "completed",
                    "sample_index": 3,
                    "samples": ["passed", "passed", "passed"],
                    "revision": published["revision"],
                    "error_code": "",
                }
            )
        except asyncio.CancelledError:
            record.state.update({"status": "cancelled", "error_code": ""})
        except Exception as error:
            record.state.update(
                {
                    "status": "failed",
                    "error_code": str(
                        getattr(error, "code", "wheel_calibration_context_lost")
                    ),
                }
            )
        finally:
            record.state["cleanup"] = await self._stop_and_confirm(
                record.raw_profile_id
            )
            self._release_profiles([record.raw_profile_id], record.lease_owner)

    def get_wheel_calibration(self) -> dict[str, Any]:
        return self._run(self._get_wheel_calibration())

    async def _get_wheel_calibration(self) -> dict[str, Any]:
        current = self.store.get_wheel_calibration("tiktok_feed")
        if current is not None:
            current = dict(current)
            current["event_count"] = len(current.pop("events", []))
        active = self._wheel_calibration
        return self._public(
            {"current": current, "active": dict(active.state) if active else None}
        )

    def cancel_wheel_calibration(self) -> dict[str, Any]:
        return self._run(self._cancel_wheel_calibration(), timeout=30)

    async def _cancel_wheel_calibration(self) -> dict[str, Any]:
        record = self._wheel_calibration
        if record is None or record.state.get("status") not in {
            "preparing",
            "waiting_for_sample",
            "validating",
            "dry_run",
            "cancelling",
        }:
            return {"status": "idle"}
        record.state["status"] = "cancelling"
        record.cancel_event.set()
        if record.task is not None:
            await record.task
        return self._public(record.state)

    # -- Job lifecycle -----------------------------------------------------

    def start_job(self, strategy_id: str, profile_tokens: list[str], batch_size: int = 3) -> dict[str, str]:
        return self._run(self._start_job(strategy_id, profile_tokens, batch_size))

    async def _start_job(self, strategy_id: str, profile_tokens: list[str], batch_size: int) -> str:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= 8:
            raise ValueError("batch_size_invalid")
        if not isinstance(profile_tokens, list) or any(not isinstance(item, str) for item in profile_tokens):
            raise ProfileTokenError("profile_tokens_invalid")
        raw_ids = await self._resolve_profiles(profile_tokens)
        if self.store.list_recoverable_jobs():
            raise ExecutionConflictError("execution_job_already_active")
        snapshot = self.store.build_execution_snapshot(strategy_id)
        job_id = self._new_id()
        lease_owner = f"job:{job_id}"
        self._acquire_profiles(raw_ids, lease_owner)
        # This synchronous transaction is intentionally before submission: a
        # GET immediately after POST must see a queued job, even if the worker
        # has not received a timeslice yet.
        try:
            self.store.prepare_job(job_id, strategy_id, snapshot, raw_ids, batch_size)
            self._job_profile_leases[job_id] = tuple(raw_ids)
            future = self.runtime.submit(self._run_job(job_id))
            self._jobs[job_id] = future
        except Exception:
            self._job_profile_leases.pop(job_id, None)
            self._release_profiles(raw_ids, lease_owner)
            raise
        return {"job_id": job_id, "status": "queued"}

    async def _run_job(self, job_id: str) -> None:
        token = self._running_job_id.set(job_id)
        try:
            await self.scheduler.run_existing(job_id)
        except Exception:
            job = self.store.get_job(job_id)
            if job is not None and job["status"] not in _TERMINAL_JOB:
                for row in self.store.list_profile_results(job_id):
                    if row["status"] not in {"succeeded", "failed", "cleanup_failed"}:
                        self.store.set_profile_status(
                            job_id, row["profile_id"], ProfileStatus.FAILED,
                            Stage.EXECUTE_ACTION, error_code="background_worker_failed",
                            error_summary="Background worker stopped unexpectedly.", close_confirmed=False,
                        )
                self.store.set_job_status(job_id, JobStatus.CLEANUP_BLOCKED)
        finally:
            self._running_job_id.reset(token)
            raw_ids = self._job_profile_leases.pop(job_id, ())
            self._release_profiles(raw_ids, f"job:{job_id}")

    async def _persist_executor_stage(
        self, raw_profile_id: str, status: ProfileStatus, stage: Stage
    ) -> None:
        """Persist executor stages using task-local job context, never Profile lookup."""

        job_id = self._running_job_id.get()
        if job_id is not None:
            self.store.set_profile_status(job_id, raw_profile_id, status, stage)

    def cancel_job(self, job_id: str) -> None:
        if self.store.get_job(job_id) is None:
            raise KeyError("execution_job_not_found")
        self._run(self.scheduler.cancel(job_id))

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError("execution_job_not_found")
        return self._public_job(job)

    def list_history(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        return [self._public_job(job) for job in self.store.list_jobs(limit=limit, offset=offset)]

    def history(self, *, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        """Blueprint-facing alias; retain list_history for direct callers."""

        return self.list_history(limit=limit, offset=offset)

    def get_results(self, job_id: str) -> dict[str, Any]:
        if self.store.get_job(job_id) is None:
            raise KeyError("execution_job_not_found")
        return {
            "job_id": job_id,
            "profiles": [self._public_profile_result(row) for row in self.store.list_profile_results(job_id)],
            "actions": [self._public_action_result(row) for row in self.store.list_action_results(job_id)],
        }

    # -- Element / strategy store delegates --------------------------------

    def list_elements(self) -> list[dict[str, Any]]:
        return self._public(self.store.list_elements())

    def get_element(self, element_id: str) -> dict[str, Any]:
        return self._public(self.store.get_element_or_raise(element_id))

    def create_element(
        self,
        name: str,
        purpose: str,
        kind: str,
        definition: dict[str, Any],
        id: str | None = None,
        status: str = "active",
    ) -> dict[str, Any]:
        return self._public(
            self.store.create_element(id or self._new_id(), name, purpose, kind, definition, status=status)
        )

    def rename_element(self, element_id: str, name: str, expected_revision: int) -> dict[str, Any]:
        return self._public(self.store.rename_element(element_id, name, expected_revision=expected_revision))

    def repick_element(self, element_id: str, definition: dict[str, Any], expected_revision: int) -> dict[str, Any]:
        return self._public(self.store.repick_element(element_id, definition, expected_revision=expected_revision))

    def set_element_status(self, element_id: str, status: str, expected_revision: int) -> dict[str, Any]:
        return self._public(self.store.set_element_status(element_id, status, expected_revision=expected_revision))

    def update_element(
        self,
        element_id: str,
        expected_revision: int,
        *,
        name: str | None = None,
        definition: dict[str, Any] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        updates = [name is not None, definition is not None, status is not None]
        if sum(updates) != 1:
            raise ValueError("element_update_requires_exactly_one_field")
        if name is not None:
            return self.rename_element(element_id, name, expected_revision)
        if definition is not None:
            return self.repick_element(element_id, definition, expected_revision)
        return self.set_element_status(element_id, status, expected_revision)  # type: ignore[arg-type]

    def delete_element(self, element_id: str, expected_revision: int) -> None:
        self.store.delete_element(element_id, expected_revision=expected_revision)

    def validate_element(self, element_id: str, profile_token: str) -> dict[str, Any]:
        return self._run(self._validate_element(element_id, profile_token), timeout=60)

    async def _validate_element(self, element_id: str, profile_token: str) -> dict[str, Any]:
        element = self.store.get_element_or_raise(element_id)
        raw_id = (await self._resolve_profiles([profile_token]))[0]
        lease_owner = f"validate:{profile_token}"
        self._acquire_profiles([raw_id], lease_owner)
        started = False
        cleanup: dict[str, Any] = {"close_confirmed": False, "attempts": 0}
        result: dict[str, Any]
        try:
            ws_url = await self.adspower.start(raw_id)
            started = True
            binding = await self.sessions.connect(raw_id, ws_url)
            await binding.page.goto(element["definition"]["url_pattern"])
            resolved = await self._resolver.resolve(
                binding.page,
                element["definition"],
                require_editable=element["kind"] == "input",
            )
            result = {
                "valid": True,
                "diagnostics": list(getattr(resolved, "diagnostics", ())),
            }
        except LocatorResolutionError as error:
            result = {"valid": False, "diagnostics": list(error.diagnostics), "error_code": error.code}
        except Exception:
            result = {"valid": False, "diagnostics": [], "error_code": "validation_runtime_failed"}
        finally:
            if started:
                cleanup = await self._stop_and_confirm(raw_id)
            self._release_profiles([raw_id], lease_owner)
        result["cleanup"] = cleanup
        return self._public(result)

    def list_strategies(self) -> list[dict[str, Any]]:
        return self._public(self.store.list_strategies())

    def get_strategy(self, strategy_id: str) -> dict[str, Any]:
        return self._public(self.store.get_strategy_or_raise(strategy_id))

    def create_strategy(
        self, name: str, definition: dict[str, Any], enabled: bool = True, id: str | None = None
    ) -> dict[str, Any]:
        return self._public(self.store.create_strategy(id or self._new_id(), name, definition, enabled))

    def update_strategy(self, strategy_id: str, name: str, definition: dict[str, Any], enabled: bool, expected_revision: int) -> dict[str, Any]:
        return self._public(self.store.update_strategy(strategy_id, name, definition, enabled, expected_revision=expected_revision))

    def set_strategy_enabled(self, strategy_id: str, enabled: bool, expected_revision: int) -> dict[str, Any]:
        return self._public(self.store.set_strategy_enabled(strategy_id, enabled, expected_revision=expected_revision))

    def delete_strategy(self, strategy_id: str, expected_revision: int) -> None:
        self.store.delete_strategy(strategy_id, expected_revision=expected_revision)

    def get_strategy_publication_metadata(self, strategy_id: str) -> dict[str, Any]:
        return self._public(self.store.get_action_publication_metadata(strategy_id))

    def begin_strategy_debug_run(
        self, strategy_id: str, *, run_id: str
    ) -> dict[str, Any]:
        return self._public(self.store.begin_debug_run(strategy_id, run_id))

    def complete_strategy_debug_run(
        self, run_id: str, *, status: str, finished_at: str
    ) -> dict[str, Any]:
        return self._public(self.store.complete_debug_run(run_id, status, finished_at))

    def prepare_strategy_release(
        self,
        strategy_id: str,
        *,
        actor: PublicationActor,
        waive_validation: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        metadata = self.store.get_action_publication_metadata(strategy_id)
        return self._public(
            self.store.prepare_release(
                metadata["action_id"],
                metadata["action_revision"],
                actor,
                waive_validation=waive_validation,
                reason=reason,
            )
        )

    def mark_strategy_release_synced(
        self,
        action_id: str,
        release_revision: int,
        *,
        central_revision: int,
        synced_at: str,
    ) -> dict[str, Any]:
        return self._public(
            self.store.mark_release_synced(
                action_id,
                release_revision,
                central_revision,
                synced_at,
            )
        )

    # -- Shutdown / sanitization -------------------------------------------

    def close(self) -> None:
        with self._close_lock:
            if self._closed or self._closing:
                return
            self._closing = True
        try:
            self._run(self._close_all(), timeout=10)
        except Exception:
            pass
        finally:
            with self._close_lock:
                self._closed = True
                self._closing = False
            if self._owns_runtime:
                self.runtime.close()

    async def _close_all(self) -> None:
        for job_id in list(self._jobs):
            try:
                await self.scheduler.cancel(job_id)
            except Exception:
                pass
        for session_id in list(self._pickers):
            try:
                await self._close_picker(session_id, finish=False)
            except Exception:
                pass
        if self._wheel_calibration is not None:
            try:
                await self._cancel_wheel_calibration()
            except Exception:
                pass
        close_sessions = getattr(self.sessions, "close", None)
        if callable(close_sessions):
            try:
                value = close_sessions()
                if inspect.isawaitable(value):
                    await value
            except Exception:
                pass

    async def _wait_startup(self) -> None:
        await asyncio.wrap_future(self._startup_future)

    async def _stop_and_confirm(self, raw_profile_id: str) -> dict[str, Any]:
        attempts = 0
        for attempts in range(1, 4):
            try:
                await self.adspower.stop(raw_profile_id)
            except Exception:
                pass
            try:
                if not await self.adspower.is_active(raw_profile_id):
                    return {"close_confirmed": True, "attempts": attempts}
            except Exception:
                pass
        return {"close_confirmed": False, "attempts": attempts}

    def _acquire_profiles(self, raw_profile_ids: list[str], owner: str) -> None:
        conflicts = [
            raw_id for raw_id in raw_profile_ids
            if raw_id in self._profile_leases and self._profile_leases[raw_id] != owner
        ]
        if conflicts:
            raise ExecutionConflictError("profile_already_in_use")
        for raw_id in raw_profile_ids:
            self._profile_leases[raw_id] = owner

    def _release_profiles(self, raw_profile_ids: Any, owner: str | None = None) -> None:
        for raw_id in raw_profile_ids:
            if owner is None or self._profile_leases.get(raw_id) == owner:
                self._profile_leases.pop(raw_id, None)

    async def _capture_failure(self, binding: BrowserBinding, _outcome: Any) -> str | None:
        page = binding.page
        screenshot = getattr(page, "screenshot", None)
        if not callable(screenshot):
            return None
        relative = f"evidence/{uuid4().hex}.png"
        destination = self._evidence_dir / Path(relative).name
        self._evidence_dir.mkdir(parents=True, exist_ok=True)
        value = screenshot(path=str(destination))
        if inspect.isawaitable(value):
            await value
        return relative

    def _run(self, coroutine: Any, *, timeout: float = 15) -> Any:
        if self._closed:
            if inspect.iscoroutine(coroutine):
                coroutine.close()
            raise RuntimeError("execution_v2_service_closed")
        return self.runtime.submit(coroutine).result(timeout=timeout)

    def _new_id(self) -> str:
        value = self._id_factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError("id_factory_invalid")
        return value

    def _public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        result = dict(job)
        result.pop("strategy_snapshot", None)
        raw_profiles = self.store.list_profile_results(job["id"])
        raw_actions = self.store.list_action_results(job["id"])
        batch_size = int(result["batch_size"])
        total = len(raw_profiles)
        total_batches = (total + batch_size - 1) // batch_size if total else 0
        terminal = {"succeeded", "failed", "cleanup_failed"}
        remaining = sum(1 for item in raw_profiles if item["status"] not in terminal)
        active_positions = [
            int(item["position"]) for item in raw_profiles if item["status"] not in terminal
        ]
        current_batch = (
            (active_positions[0] - 1) // batch_size + 1
            if active_positions
            else total_batches
        )
        result["summary"] = {
            "total": total,
            "remaining": remaining,
            "current_batch": current_batch,
            "total_batches": total_batches,
            "succeeded": sum(1 for item in raw_profiles if item["status"] == "succeeded"),
            "failed": sum(
                1 for item in raw_profiles if item["status"] in {"failed", "cleanup_failed"}
            ),
        }
        result["profiles"] = [self._public_profile_result(row) for row in raw_profiles]
        result["actions"] = [self._public_action_result(row) for row in raw_actions]
        return self._public(result)

    def _public_profile_result(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        raw_id = result.pop("profile_id", "")
        result["display_id"] = mask_profile_id(raw_id)
        token = self._raw_to_token.get(raw_id)
        if token:
            result["profile_token"] = token
        return self._public(result)

    def _public_action_result(self, row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        raw_id = result.pop("profile_id", "")
        details = result.pop("result", {})
        result["display_id"] = mask_profile_id(raw_id)
        token = self._raw_to_token.get(raw_id)
        if token:
            result["profile_token"] = token
        if isinstance(details, dict):
            evidence_path = details.get("evidence_path")
            if isinstance(evidence_path, str) and _SAFE_EVIDENCE_PATH.fullmatch(
                evidence_path.replace("\\", "/")
            ):
                result["evidence_path"] = evidence_path.replace("\\", "/")
            error_code = details.get("error_code")
            if isinstance(error_code, str) and _SAFE_ERROR_CODE.fullmatch(error_code):
                result["error_code"] = error_code
            for key in (
                "duration_seconds", "direction", "distance_pixels", "count", "interval_seconds",
                "requested_switches", "completed_switches", "wheel_events", "calibration_revision",
                "button", "click_count", "hold_seconds", "after_seconds", "content_source", "text_length",
            ):
                value = details.get(key)
                if _safe_action_detail(key, value):
                    result[key] = value
        return self._public(result)

    def _public(self, value: Any) -> Any:
        return _redact(value, raw_ids=set(self._raw_to_token))


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(value.replace("\x00", "").split())[:limit] if isinstance(value, str) else ""


def _redact(value: Any, *, raw_ids: set[str], key: str = "") -> Any:
    if _SECRET_KEY.search(key):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item): _redact(child, raw_ids=raw_ids, key=str(item)) for item, child in value.items()}
    if isinstance(value, list):
        return [_redact(item, raw_ids=raw_ids, key=key) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, raw_ids=raw_ids, key=key) for item in value]
    if isinstance(value, str):
        if value in raw_ids:
            return mask_profile_id(value)
        text = _WEBSOCKET.sub("[redacted-websocket]", value)
        for raw_id in sorted(raw_ids, key=len, reverse=True):
            if raw_id:
                text = text.replace(raw_id, mask_profile_id(raw_id))
        return text
    return value


def _safe_action_detail(key: str, value: Any) -> bool:
    if key in {"direction"}:
        return value in {"up", "down"}
    if key in {"button"}:
        return value in {"left", "middle", "right"}
    if key == "content_source":
        return value in {"fixed", "library"}
    return isinstance(value, (int, float)) and not isinstance(value, bool)


__all__ = [
    "ExecutionV2Service",
    "PickerSessionNotFoundError",
    "ExecutionConflictError",
    "ProfileTokenError",
    "create_default_execution_v2_service",
]
