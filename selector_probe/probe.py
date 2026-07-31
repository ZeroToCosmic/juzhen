"""Observe-only selector probe orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
import hashlib
import inspect
import json
import re
import threading
import time
import uuid

from browser_cdp import wait_for_cdp as default_wait_for_cdp
from browser_element_resolver import inspect_visible_element
from browser_element_schema import TIKTOK_COMMENT_TEMPLATE
from browser_public_identity import mask_profile_id
from selector_probe.candidates import generate_candidates
from selector_probe.scheduler import RedisLease, due_daily_slot
from selector_probe.session import ProbeSessionError, ProbeSessionManager
from selector_probe.discovery import (
    comment_entry_definition,
    discover_interactive_candidates,
)
from selector_probe.contracts import default_tiktok_contracts
from selector_probe.snapshot import SemanticSnapshot, extract_semantic_snapshot
from selector_probe.state_runner import ProbeSafetyError, ProbeStateRunner


LEASE_TTL_SECONDS = 120
LEASE_HEARTBEAT_SECONDS = 30
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 5.0
ASYNC_WATCH_INTERVAL_SECONDS = 0.05
CDP_WAIT_SLICE_SECONDS = 1.0
CDP_WAIT_TOTAL_SECONDS = 30.0
_PAGE_STATES = (
    "feed_ready",
    "comment_panel_open",
    "comment_panel_closed",
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
ELEMENT_REQUEST_TYPES = frozenset({"probe", "validate"})
_PROGRESS_STATUSES = frozenset({"queued", "running", "passed", "failed"})
_PROGRESS_FIELDS = frozenset(
    {
        "name",
        "profile_mask",
        "status",
        "attempt_count",
        "round",
        "failure_code",
        "summary",
    }
)


class ProbeLeaseLost(RuntimeError):
    code = "probe_lease_lost"

    def __init__(self) -> None:
        super().__init__(self.code)


class ProbeValidationFailed(RuntimeError):
    code = "selector_validation_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class ProbeCleanupFailed(RuntimeError):
    code = "probe_cleanup_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


class ModelOutputFormatError(RuntimeError):
    """A model response was received but cannot satisfy the repair schema."""


class ElementRequestBusy(RuntimeError):
    pass


def unavailable_element_request_dispatcher(_payload: object) -> object:
    raise RuntimeError("element probe dispatcher is unavailable")


def dispatch_element_request(
    dispatcher: Callable[[Mapping[str, object]], object],
    *,
    request_type: str,
    element_id: str,
    contract: Mapping[str, object],
    expected_revision: int,
    actor_user_id: int,
    request_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
) -> dict[str, object]:
    if request_type not in ELEMENT_REQUEST_TYPES:
        raise ValueError("element request type is invalid")
    if (
        not isinstance(element_id, str)
        or not element_id
        or element_id != element_id.strip()
    ):
        raise ValueError("element_id is invalid")
    if not isinstance(contract, Mapping) or not contract:
        raise ValueError("element contract is required")
    if (
        isinstance(expected_revision, bool)
        or not isinstance(expected_revision, int)
        or expected_revision < 1
    ):
        raise ValueError("expected_revision is invalid")
    if (
        isinstance(actor_user_id, bool)
        or not isinstance(actor_user_id, int)
        or actor_user_id < 1
    ):
        raise ValueError("actor_user_id is invalid")
    request_id = request_id_factory()
    if (
        not isinstance(request_id, str)
        or not request_id
        or request_id != request_id.strip()
        or len(request_id) > 128
    ):
        raise ValueError("request_id is invalid")
    payload = {
        "request_id": request_id,
        "request_type": request_type,
        "element_id": element_id,
        "contract": dict(contract),
        "expected_revision": expected_revision,
        "actor_user_id": actor_user_id,
    }
    result = dispatcher(payload)
    if result is False or (
        isinstance(result, Mapping)
        and result.get("status") == "busy"
    ):
        raise ElementRequestBusy(request_id)
    if result is not None and result is not True and not isinstance(
        result,
        Mapping,
    ):
        raise RuntimeError("element probe dispatcher returned an invalid result")
    return {
        "status": "accepted",
        "request_id": request_id,
        "element_id": element_id,
        "request_type": request_type,
        "expected_revision": expected_revision,
    }


def run_element_probe(runtime: object) -> dict[str, object]:
    candidate = runtime.deterministic_candidates()
    if candidate is None:
        failure = runtime.deterministic_failure()
        return {
            "status": "selector_validation_failed",
            "failure_code": (
                failure.get("code", "zero_match")
                if isinstance(failure, Mapping)
                else "zero_match"
            ),
        }
    validation = runtime.validate_candidate(candidate)
    if (
        not isinstance(validation, Mapping)
        or validation.get("status") != "passed"
    ):
        return {
            "status": "selector_validation_failed",
            "failure_code": (
                validation.get("code", "wrong_semantics")
                if isinstance(validation, Mapping)
                else "wrong_semantics"
            ),
        }
    return {
        "status": "probe_completed",
        "candidate": candidate,
    }


class _LeaseHeartbeat:
    def __init__(self, lease: object) -> None:
        self._lease = lease
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._renew_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="selector-probe-lease-heartbeat",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        self._thread.join(HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        stopped = not self._thread.is_alive()
        if not stopped:
            self._lost.set()
        return stopped

    def require_owned(self, *, renew: bool = False) -> None:
        if self.lost:
            raise ProbeLeaseLost()
        if renew and not self._renew(
            lock_timeout=HEARTBEAT_JOIN_TIMEOUT_SECONDS
        ):
            raise ProbeLeaseLost()

    def _renew(self, *, lock_timeout: float | None = None) -> bool:
        if lock_timeout is None:
            acquired = self._renew_lock.acquire()
        else:
            acquired = self._renew_lock.acquire(timeout=lock_timeout)
        if not acquired:
            self._lost.set()
            return False
        try:
            if self.lost:
                return False
            try:
                owned = self._lease.renew() is True
            except Exception:
                owned = False
            if not owned:
                self._lost.set()
            return owned
        finally:
            self._renew_lock.release()

    def _run(self) -> None:
        while not self._stop.wait(LEASE_HEARTBEAT_SECONDS):
            if not self._renew():
                return


def _stop_requested(stop_event: object | None) -> bool:
    if stop_event is None:
        return False
    is_set = getattr(stop_event, "is_set", None)
    return callable(is_set) and is_set()


def _require_continue(
    heartbeat: _LeaseHeartbeat,
    stop_event: object | None,
) -> None:
    heartbeat.require_owned()
    if _stop_requested(stop_event):
        raise asyncio.CancelledError()


async def _watch_operation(
    awaitable: Awaitable[tuple[int, int]],
    heartbeat: _LeaseHeartbeat,
    stop_event: object | None,
) -> tuple[int, int]:
    task = asyncio.create_task(awaitable)
    cancellation: BaseException | None = None
    while not task.done():
        if heartbeat.lost:
            cancellation = ProbeLeaseLost()
            task.cancel()
            break
        if _stop_requested(stop_event):
            cancellation = asyncio.CancelledError()
            task.cancel()
            break
        await asyncio.wait(
            {task},
            timeout=ASYNC_WATCH_INTERVAL_SECONDS,
        )

    try:
        result = await task
    except asyncio.CancelledError:
        if isinstance(cancellation, ProbeLeaseLost):
            raise cancellation
        raise
    if cancellation is not None:
        raise cancellation
    _require_continue(heartbeat, stop_event)
    return result


def _wait_for_cdp_in_slices(
    ws_url: str,
    *,
    heartbeat: _LeaseHeartbeat,
    stop_event: object | None,
    wait_fn: Callable[..., object] = default_wait_for_cdp,
) -> bool:
    deadline = time.monotonic() + CDP_WAIT_TOTAL_SECONDS
    while True:
        _require_continue(heartbeat, stop_event)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        timeout = min(CDP_WAIT_SLICE_SECONDS, remaining)
        try:
            if wait_fn(
                ws_url,
                timeout=timeout,
                interval=min(0.2, timeout),
            ) is True:
                _require_continue(heartbeat, stop_event)
                return True
        except Exception:
            pass
        _require_continue(heartbeat, stop_event)


def _element_page_state(
    definition: object,
    alias: str = "",
) -> str:
    contract = default_tiktok_contracts().get(alias)
    if contract is not None and contract.required_state in _PAGE_STATES:
        return contract.required_state
    if not isinstance(definition, Mapping):
        return "feed_ready"
    required = definition.get("required_state")
    if required in _PAGE_STATES:
        return str(required)
    if definition.get("scope") == "visible_comment_panel":
        return "comment_panel_open"
    return "feed_ready"


def _safe_readiness(value: object, state: str) -> dict[str, object]:
    result: dict[str, object] = {"state": state}
    if not isinstance(value, Mapping):
        return result
    for key in (
        "ready",
        "blocked",
        "skeleton_ready",
        "panel_visible",
        "clicked",
    ):
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
    origin = value.get("origin")
    if isinstance(origin, str) and origin.startswith("https://"):
        result["origin"] = origin[:256]
    return result


def _snapshot_payload(snapshot: object) -> tuple[dict[str, object], str]:
    model_payload = getattr(snapshot, "model_payload", None)
    if not callable(model_payload):
        raise TypeError("semantic snapshot has no model_payload")
    payload = model_payload()
    if not isinstance(payload, dict):
        raise TypeError("semantic snapshot payload must be a JSON object")
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return payload, hashlib.sha256(encoded).hexdigest()


def _observe_candidate_definition(
    alias: str,
    state: str,
    snapshot: object,
    historical_definition: object,
) -> dict[str, object] | None:
    if not isinstance(snapshot, SemanticSnapshot):
        return None
    contract = default_tiktok_contracts().get(alias)
    if contract is None or contract.required_state != state:
        return None
    template = TIKTOK_COMMENT_TEMPLATE.get(alias)
    locators = [
        dict(item)
        for item in (
            template.get("locators", ())
            if isinstance(template, Mapping)
            else ()
        )
        if isinstance(item, Mapping)
    ]
    if (
        isinstance(historical_definition, Mapping)
        and historical_definition.get("scope") == contract.scope
    ):
        locators.extend(
            dict(item)
            for item in historical_definition.get("locators", ())
            if isinstance(item, Mapping)
        )
    candidates = generate_candidates(
        contract,
        snapshot,
        {"scope": contract.scope, "locators": locators},
    )
    if not candidates:
        return None
    return {"scope": contract.scope, "locators": candidates}


async def _default_observe_page(
    page: object,
    config: object,
    elements: Mapping[str, object],
    *,
    state_runner_factory: Callable[[object], object],
    snapshot_extractor: Callable[[object], Awaitable[object]],
    element_inspector: Callable[[object, str, dict], Awaitable[dict]],
    heartbeat: _LeaseHeartbeat,
    stop_event: object | None,
    profile_mask: str = "",
    round_number: int = 1,
    progress_sink: Callable[[Mapping[str, object]], None] | None = None,
) -> list[dict[str, object]]:
    runner = state_runner_factory(config)

    def progress(
        name: str,
        status: str,
        failure_code: str = "",
        attempt_count: int = 1,
    ) -> None:
        if progress_sink is not None:
            progress_sink(
                {
                    "name": name,
                    "profile_mask": profile_mask,
                    "round": round_number,
                    "status": status,
                    "attempt_count": attempt_count,
                    "failure_code": failure_code,
                }
            )
    grouped: dict[str, list[tuple[str, dict]]] = {
        state: [] for state in _PAGE_STATES
    }
    for alias, definition in elements.items():
        if not isinstance(alias, str) or not isinstance(definition, dict):
            continue
        grouped[_element_page_state(definition, alias)].append(
            (alias, definition)
        )

    states = ["feed_ready", "comment_panel_open"]
    records: list[dict[str, object]] = []
    comment_override: dict[str, object] | None = None
    transition_available = True
    retryable_comment_codes = {
        "comment_panel_readiness_timeout",
        "comment_panel_snapshot_unstable",
    }
    retryable_feed_codes = {
        "page_readiness_timeout",
        "probe_navigation_timeout",
        "probe_readiness_timeout",
    }
    for state in states:
        if state == "comment_panel_open" and not transition_available:
            break
        _require_continue(heartbeat, stop_event)
        stage_name = (
            "page_readiness"
            if state == "feed_ready"
            else "comment_panel_transition"
        )
        attempts = 3
        for attempt in range(1, attempts + 1):
            progress(stage_name, "running", attempt_count=attempt)
            try:
                if state == "comment_panel_open" and attempt > 1:
                    await runner.ensure_state(
                        page,
                        "feed_ready",
                        dict(elements),
                        initial_action="reload",
                    )
                if (
                    state == "comment_panel_open"
                    and comment_override is not None
                ):
                    readiness = await runner.ensure_state(
                        page,
                        state,
                        dict(elements),
                        comment_entry_override=comment_override,
                    )
                else:
                    readiness = await runner.ensure_state(
                        page,
                        state,
                        dict(elements),
                        initial_action=(
                            "reload"
                            if state == "feed_ready" and attempt > 1
                            else "navigate"
                        ),
                    )
                progress(
                    stage_name,
                    "passed",
                    attempt_count=attempt,
                )
                break
            except ProbeSafetyError as error:
                code = _safe_error_code(error)
                if (
                    state == "comment_panel_open"
                    and code
                    in retryable_comment_codes | retryable_feed_codes
                    and attempt < attempts
                ):
                    continue
                if (
                    state == "feed_ready"
                    and code in retryable_feed_codes
                    and attempt < attempts
                ):
                    continue
                progress(
                    stage_name,
                    "failed",
                    code,
                    attempt,
                )
                raise
            except BaseException as error:
                progress(
                    stage_name,
                    "failed",
                    _safe_error_code(error),
                    attempt,
                )
                raise
        _require_continue(heartbeat, stop_event)
        progress("a11y_snapshot", "running")
        try:
            snapshot = await snapshot_extractor(page)
        except BaseException as error:
            progress("a11y_snapshot", "failed", _safe_error_code(error))
            raise
        progress("a11y_snapshot", "passed")
        _require_continue(heartbeat, stop_event)
        snapshot_payload, snapshot_hash = _snapshot_payload(snapshot)
        progress("candidate_filter", "running")
        discoveries = discover_interactive_candidates(
            snapshot_payload,
            page_state=state,
            profile_mask=profile_mask,
        )
        progress("candidate_filter", "passed")
        aliases: dict[str, dict] = {}
        proposed_definitions: dict[str, dict[str, object]] = {}
        failure_code = ""
        progress("element_dry_run", "running")
        for alias, definition in grouped[state]:
            _require_continue(heartbeat, stop_event)
            proposed = _observe_candidate_definition(
                alias,
                state,
                snapshot,
                definition,
            )
            if proposed is not None:
                proposed_definitions[alias] = proposed
            inspected = await element_inspector(
                page,
                alias,
                proposed or definition,
            )
            _require_continue(heartbeat, stop_event)
            if not isinstance(inspected, dict):
                inspected = {
                    "status": "error",
                    "code": "element_inspection_failed",
                    "alias": alias,
                }
            elif proposed is not None:
                inspected = {
                    **inspected,
                    "recommended_locators": proposed["locators"],
                }
            aliases[alias] = inspected
            if inspected.get("status") != "ok" and not failure_code:
                code = inspected.get("code")
                failure_code = (
                    code
                    if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
                    else "element_inspection_failed"
                )
        progress(
            "element_dry_run",
            "failed" if failure_code else "passed",
            failure_code,
        )
        evidence = {
            "readiness": _safe_readiness(readiness, state),
            "snapshot_hash": snapshot_hash,
            "semantic_snapshot": snapshot_payload,
            "discoveries": discoveries,
            "aliases": aliases,
        }
        if state == "feed_ready":
            entry_alias = getattr(runner, "comment_entry_alias", "")
            saved_entry = aliases.get(entry_alias)
            saved_entry_ok = (
                isinstance(saved_entry, Mapping)
                and saved_entry.get("status") == "ok"
            )
            if saved_entry_ok:
                comment_override = proposed_definitions.get(entry_alias)
            if not saved_entry_ok:
                comment_override = comment_entry_definition(
                    discoveries,
                    allow_unverified=True,
                )
                if comment_override is not None:
                    verified_entry = await element_inspector(
                        page,
                        entry_alias or "comment_entry",
                        comment_override,
                    )
                    if (
                        not isinstance(verified_entry, Mapping)
                        or verified_entry.get("status") != "ok"
                    ):
                        comment_override = None
                    else:
                        for candidate in discoveries:
                            attributes = candidate.get("attributes")
                            if (
                                isinstance(attributes, Mapping)
                                and attributes.get("data-e2e")
                                == "comment-icon"
                            ):
                                candidate["actionable"] = True
                if comment_override is None:
                    transition_available = False
                    evidence["transition_failure_code"] = (
                        "comment_entry_confirmation_required"
                    )
                else:
                    evidence["transition_selector_source"] = (
                        "discovery_fallback"
                    )
                    failure_code = ""
                    for alias, inspected in aliases.items():
                        if alias == entry_alias or inspected.get("status") == "ok":
                            continue
                        code = inspected.get("code")
                        failure_code = (
                            code
                            if isinstance(code, str)
                            and _SAFE_CODE.fullmatch(code)
                            else "element_inspection_failed"
                        )
                        break
        records.append(
            {
                "page_state": state,
                "result": "failed" if failure_code else "passed",
                "failure_code": failure_code,
                "evidence": evidence,
            }
        )
        if state == "comment_panel_open":
            progress("comment_panel_cleanup", "running")
            try:
                await runner.ensure_state(
                    page,
                    "comment_panel_closed",
                    dict(elements),
                )
                evidence["panel_cleanup"] = {
                    "status": "passed",
                    "method": "state_transition",
                }
            except ProbeSafetyError as error:
                if (
                    error.code != "probe_state_verification_failed"
                    or error.action != "close_comment_panel"
                ):
                    progress(
                        "comment_panel_cleanup",
                        "failed",
                        _safe_error_code(error),
                    )
                    raise
                evidence["panel_cleanup"] = {
                    "status": "fallback",
                    "method": "profile_window_close",
                }
            except BaseException as error:
                progress(
                    "comment_panel_cleanup",
                    "failed",
                    _safe_error_code(error),
                )
                raise
            progress("comment_panel_cleanup", "passed")
        _require_continue(heartbeat, stop_event)
    return records


def _validation_record(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("profile observation must return validation objects")
    page_state = value.get("page_state")
    result = value.get("result")
    failure_code = value.get("failure_code", "")
    evidence = value.get("evidence")
    if page_state not in _PAGE_STATES:
        raise ValueError("validation page_state is unsupported")
    if result not in {"passed", "failed"}:
        raise ValueError("validation result must be passed or failed")
    if (
        not isinstance(failure_code, str)
        or (failure_code and not _SAFE_CODE.fullmatch(failure_code))
    ):
        raise ValueError("validation failure_code is invalid")
    if not isinstance(evidence, Mapping):
        raise ValueError("validation evidence must be a JSON object")
    return {
        "page_state": page_state,
        "result": result,
        "failure_code": failure_code,
        "evidence": dict(evidence),
    }


def _sanitize_cleanup(results: object) -> list[dict[str, object]]:
    if not isinstance(results, Sequence) or isinstance(
        results, (str, bytes, bytearray)
    ):
        return []
    sanitized: list[dict[str, object]] = []
    for value in results:
        if not isinstance(value, Mapping):
            continue
        code = value.get("code", "")
        sanitized.append(
            {
                "profile_mask": mask_profile_id(
                    value.get("profile_mask", "")
                ),
                "stage": (
                    value["stage"]
                    if isinstance(value.get("stage"), str)
                    and _SAFE_CODE.fullmatch(str(value["stage"]))
                    else "cleanup"
                ),
                "ok": value.get("ok") is True,
                "code": (
                    code
                    if isinstance(code, str)
                    and (not code or _SAFE_CODE.fullmatch(code))
                    else "cleanup_failed"
                ),
            }
        )
    return sanitized


async def _start_playwright() -> object:
    from playwright.async_api import async_playwright

    return await async_playwright().start()


async def _observe_profiles(
    *,
    config: object,
    store: object,
    run_id: int,
    attempt_token: str,
    session_manager: object,
    heartbeat: _LeaseHeartbeat,
    elements: Mapping[str, object],
    playwright_starter: Callable[[], Awaitable[object]],
    observe_page: Callable[
        [object, object, Mapping[str, object]],
        Awaitable[Sequence[Mapping[str, object]]],
    ],
    cleanup: list[dict[str, object]],
    stop_event: object | None,
    progress_sink: Callable[[Mapping[str, object]], None] | None = None,
) -> tuple[int, int]:
    profile_handles: list[object] = []
    page_handles: list[object] = []
    playwright: object | None = None
    profiles_observed = 0
    validations_recorded = 0
    cleanup_cancellation: asyncio.CancelledError | None = None
    try:
        _require_continue(heartbeat, stop_event)
        try:
            profile_handles = list(
                session_manager.open_profiles(config.test_profile_ids)
            )
        except ProbeSessionError as error:
            cleanup.extend(_sanitize_cleanup(error.cleanup_results))
            raise
        _require_continue(heartbeat, stop_event)
        playwright = await playwright_starter()
        _require_continue(heartbeat, stop_event)
        for profile in profile_handles:
            _require_continue(heartbeat, stop_event)
            if progress_sink is not None:
                progress_sink(
                    {
                        "name": "probe_page_open",
                        "profile_mask": profile.profile_mask,
                        "status": "running",
                        "attempt_count": 1,
                    }
                )
            try:
                page_handle = await session_manager.open_probe_page(
                    playwright,
                    profile,
                )
            except BaseException as error:
                if progress_sink is not None:
                    progress_sink(
                        {
                            "name": "probe_page_open",
                            "profile_mask": profile.profile_mask,
                            "status": "failed",
                            "attempt_count": 1,
                            "failure_code": _safe_error_code(error),
                        }
                    )
                raise
            if progress_sink is not None:
                progress_sink(
                    {
                        "name": "probe_page_open",
                        "profile_mask": profile.profile_mask,
                        "status": "passed",
                        "attempt_count": 1,
                    }
                )
            _require_continue(heartbeat, stop_event)
            page_handles.append(page_handle)
            try:
                observer_parameters = inspect.signature(
                    observe_page
                ).parameters
            except (TypeError, ValueError):
                observer_parameters = {}
            observer_kwargs = {}
            if "profile_mask" in observer_parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in observer_parameters.values()
            ):
                observer_kwargs["profile_mask"] = profile.profile_mask
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in observer_parameters.values()
            )
            for round_number in (1, 2):
                _require_continue(heartbeat, stop_event)
                round_kwargs = dict(observer_kwargs)
                if "round_number" in observer_parameters or accepts_kwargs:
                    round_kwargs["round_number"] = round_number
                raw_records = await observe_page(
                    page_handle.page,
                    config,
                    elements,
                    **round_kwargs,
                )
                _require_continue(heartbeat, stop_event)
                if not isinstance(raw_records, Sequence) or isinstance(
                    raw_records, (str, bytes, bytearray)
                ):
                    raise TypeError(
                        "profile observation must return a validation sequence"
                    )
                for raw_record in raw_records:
                    _require_continue(heartbeat, stop_event)
                    record = _validation_record(raw_record)
                    _require_continue(heartbeat, stop_event)
                    store.record_validation(
                        run_id=run_id,
                        attempt_token=attempt_token,
                        profile_mask=profile.profile_mask,
                        round_number=round_number,
                        page_state=record["page_state"],
                        result=record["result"],
                        failure_code=record["failure_code"],
                        evidence=record["evidence"],
                    )
                    _require_continue(heartbeat, stop_event)
                    validations_recorded += 1
            profiles_observed += 1
            _require_continue(heartbeat, stop_event)
        return profiles_observed, validations_recorded
    finally:
        if page_handles:
            try:
                cleanup.extend(
                    _sanitize_cleanup(
                        await session_manager.close_owned_pages(page_handles)
                    )
                )
            except asyncio.CancelledError as error:
                cleanup_cancellation = error
                cleanup.append(
                    {
                        "profile_mask": "",
                        "stage": "close_page",
                        "ok": False,
                        "code": "page_close_failed",
                    }
                )
            except Exception:
                cleanup.append(
                    {
                        "profile_mask": "",
                        "stage": "close_page",
                        "ok": False,
                        "code": "page_close_failed",
                    }
                )
        if playwright is not None:
            stop = getattr(playwright, "stop", None)
            if callable(stop):
                try:
                    await stop()
                except asyncio.CancelledError as error:
                    if cleanup_cancellation is None:
                        cleanup_cancellation = error
                    cleanup.append(
                        {
                            "profile_mask": "",
                            "stage": "playwright_stop",
                            "ok": False,
                            "code": "playwright_stop_failed",
                        }
                    )
                except Exception:
                    cleanup.append(
                        {
                            "profile_mask": "",
                            "stage": "playwright_stop",
                            "ok": False,
                            "code": "playwright_stop_failed",
                        }
                    )
        if profile_handles:
            try:
                cleanup.extend(
                    _sanitize_cleanup(
                        session_manager.stop_owned_profiles(profile_handles)
                    )
                )
            except asyncio.CancelledError as error:
                if cleanup_cancellation is None:
                    cleanup_cancellation = error
                cleanup.append(
                    {
                        "profile_mask": "",
                        "stage": "stop_profile",
                        "ok": False,
                        "code": "profile_stop_cancelled",
                    }
                )
            except Exception:
                cleanup.append(
                    {
                        "profile_mask": "",
                        "stage": "stop_profile",
                        "ok": False,
                        "code": "profile_stop_failed",
                    }
                )
        if cleanup_cancellation is not None:
            raise cleanup_cancellation


def _status_for_error(error: BaseException) -> tuple[str, str]:
    if isinstance(error, asyncio.CancelledError):
        return "probe_cancelled", "probe_cancelled"
    if isinstance(error, ProbeLeaseLost):
        return error.code, error.code
    if isinstance(error, ProbeValidationFailed):
        return error.code, error.code
    if isinstance(error, ProbeCleanupFailed):
        return error.code, error.code
    if isinstance(error, ProbeSafetyError):
        return "probe_safety_violation", _safe_error_code(error)
    return "probe_unavailable", _safe_error_code(error)


def _safe_error_code(error: BaseException) -> str:
    code = getattr(error, "code", "")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return "probe_unavailable"


def _sanitize_progress_event(
    value: Mapping[str, object],
) -> dict[str, object]:
    name = str(value.get("name") or "").strip()
    if not _SAFE_CODE.fullmatch(name):
        name = "probe"
    status = str(value.get("status") or "").strip()
    if status not in _PROGRESS_STATUSES:
        status = "running"
    profile_mask = mask_profile_id(value.get("profile_mask"))
    attempt = value.get("attempt_count", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        attempt = 1
    failure_code = str(value.get("failure_code") or "").strip()
    if failure_code and not _SAFE_CODE.fullmatch(failure_code):
        failure_code = "probe_unavailable"
    summary = str(value.get("summary") or "").strip()[:160]
    round_number = value.get("round")
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number not in {1, 2}
    ):
        round_number = None
    return {
        key: item
        for key, item in {
            "name": name,
            "profile_mask": profile_mask,
            "status": status,
            "attempt_count": max(1, min(attempt, 99)),
            "round": round_number,
            "failure_code": failure_code,
            "summary": summary,
        }.items()
        if key in _PROGRESS_FIELDS
    }


def _cleanup_has_failure(cleanup: Sequence[Mapping[str, object]]) -> bool:
    return any(item.get("ok") is not True for item in cleanup)


def _unique_profiles(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        raise ValueError("selector probe profiles must be a sequence")
    profiles = tuple(
        dict.fromkeys(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )
    if len(profiles) < 2:
        raise ValueError("selector probe requires at least two profiles")
    return profiles


def run_observe_probe(
    config: object,
    store: object,
    redis_client: object,
    adspower_client: object,
    clock: object,
    *,
    elements: Mapping[str, object] | None = None,
    lease_factory: Callable[..., object] = RedisLease,
    session_manager_factory: Callable[..., object] = ProbeSessionManager,
    wait_for_cdp: Callable[[str], object] = default_wait_for_cdp,
    playwright_starter: Callable[[], Awaitable[object]] = _start_playwright,
    observe_page: Callable[
        [object, object, Mapping[str, object]],
        Awaitable[Sequence[Mapping[str, object]]],
    ]
    | None = None,
    state_runner_factory: Callable[[object], object] | None = None,
    snapshot_extractor: Callable[[object], Awaitable[object]] = (
        extract_semantic_snapshot
    ),
    element_inspector: Callable[[object, str, dict], Awaitable[dict]] = (
        inspect_visible_element
    ),
    owner_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    stop_event: object | None = None,
    force: bool = False,
    management_request_id: str = "",
) -> dict[str, object]:
    """Run one due observe-only probe and persist its sanitized evidence."""

    assert config.observe_only is True
    if not isinstance(force, bool):
        raise ValueError("force must be a boolean")
    if config.enabled is not True:
        return {"status": "disabled", "observe_only": True}

    profiles = _unique_profiles(config.test_profile_ids)
    now = clock.now()
    if not isinstance(now, datetime):
        raise ValueError("clock.now() must return a datetime")
    slot = (
        now
        if force
        else due_daily_slot(
            now,
            store.last_completed_slot(),
            config.timezone,
            config.daily_time,
        )
    )
    if slot is None:
        return {"status": "not_due", "observe_only": True}

    attempt_token = owner_id_factory()
    lease = lease_factory(
        redis_client,
        (
            f"selector_registry:{config.environment}:"
            f"{config.site}:lease"
        ),
        attempt_token,
        ttl_seconds=LEASE_TTL_SECONDS,
        heartbeat_seconds=LEASE_HEARTBEAT_SECONDS,
    )
    if lease.acquire() is not True:
        return {"status": "lease_busy", "observe_only": True}

    run_id: int | None = None
    heartbeat: _LeaseHeartbeat | None = None
    cleanup: list[dict[str, object]] = []
    profiles_observed = 0
    validations_recorded = 0
    primary_error: BaseException | None = None
    lease_released = False
    stage_map: dict[tuple[str, str, object], dict[str, object]] = {}

    def record_progress(event: Mapping[str, object]) -> None:
        sanitized = _sanitize_progress_event(event)
        key = (
            str(sanitized["name"]),
            str(sanitized["profile_mask"]),
            sanitized.get("round"),
        )
        stage_map[key] = sanitized
        while len(stage_map) > 30:
            stage_map.pop(next(iter(stage_map)))
        updater = getattr(store, "update_run_progress", None)
        if callable(updater) and run_id is not None:
            try:
                updater(
                    run_id,
                    attempt_token=attempt_token,
                    stages=list(stage_map.values()),
                )
            except Exception:
                pass

    try:
        run_id = store.start_run(
            scheduled_for=slot.isoformat(),
            active_version_before="",
            attempt_token=attempt_token,
            management_request_id=management_request_id,
            trigger="manual" if management_request_id else "scheduled",
        )
        heartbeat = _LeaseHeartbeat(lease)
        heartbeat.start()
        record_progress(
            {
                "name": "profile_session",
                "status": "running",
                "attempt_count": 1,
            }
        )
        session_wait_for_cdp = wait_for_cdp
        if wait_for_cdp is default_wait_for_cdp:
            session_wait_for_cdp = lambda ws_url: _wait_for_cdp_in_slices(
                ws_url,
                heartbeat=heartbeat,
                stop_event=stop_event,
            )
        manager_kwargs = {
            "allowed_profile_ids": profiles,
            "wait_for_cdp": session_wait_for_cdp,
            "stop_requested": lambda: _stop_requested(stop_event),
        }
        try:
            manager_parameters = inspect.signature(
                session_manager_factory
            ).parameters
        except (TypeError, ValueError):
            manager_parameters = {}
        if "progress_sink" in manager_parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in manager_parameters.values()
        ):
            manager_kwargs["progress_sink"] = record_progress
        manager = session_manager_factory(
            adspower_client,
            **manager_kwargs,
        )
        runner_factory = state_runner_factory or (
            lambda value: ProbeStateRunner(target_url=value.target_url)
        )
        page_observer = observe_page
        if page_observer is None:
            async def page_observer(
                page,
                value,
                saved_elements,
                *,
                profile_mask="",
                round_number=1,
            ):
                return await _default_observe_page(
                    page,
                    value,
                    saved_elements,
                    state_runner_factory=runner_factory,
                    snapshot_extractor=snapshot_extractor,
                    element_inspector=element_inspector,
                    heartbeat=heartbeat,
                    stop_event=stop_event,
                    profile_mask=profile_mask,
                    round_number=round_number,
                    progress_sink=record_progress,
                )

        profiles_observed, validations_recorded = asyncio.run(
            _watch_operation(
                _observe_profiles(
                    config=config,
                    store=store,
                    run_id=run_id,
                    attempt_token=attempt_token,
                    session_manager=manager,
                    heartbeat=heartbeat,
                    elements=dict(elements or {}),
                    playwright_starter=playwright_starter,
                    observe_page=page_observer,
                    cleanup=cleanup,
                    stop_event=stop_event,
                    progress_sink=record_progress,
                ),
                heartbeat,
                stop_event,
            )
        )
        record_progress(
            {
                "name": "profile_session",
                "status": "passed",
                "attempt_count": 1,
            }
        )
        heartbeat.require_owned(renew=True)
    except BaseException as error:
        primary_error = error
        record_progress(
            {
                "name": "profile_session",
                "status": "failed",
                "attempt_count": 1,
                "failure_code": _safe_error_code(error),
            }
        )
    finally:
        if heartbeat is not None:
            heartbeat.stop()
            if heartbeat.lost and primary_error is None:
                primary_error = ProbeLeaseLost()
        if _cleanup_has_failure(cleanup) and primary_error is None:
            primary_error = ProbeCleanupFailed()
        record_progress(
            {
                "name": "cleanup",
                "status": (
                    "failed" if _cleanup_has_failure(cleanup) else "passed"
                ),
                "attempt_count": 1,
                "failure_code": (
                    "cleanup_failed"
                    if _cleanup_has_failure(cleanup)
                    else ""
                ),
            }
        )
        record_progress(
            {
                "name": "lease_release",
                "status": "running",
                "attempt_count": 1,
            }
        )

        if run_id is not None:
            if primary_error is None:
                status = "completed"
                failure_code = ""
            else:
                status, failure_code = _status_for_error(primary_error)
            details = {
                "observe_only": True,
                "profiles_observed": profiles_observed,
                "validations_recorded": validations_recorded,
                "cleanup": _sanitize_cleanup(cleanup),
                "lease_release": "best_effort_after_terminal",
                "stages": list(stage_map.values()),
            }
            if failure_code:
                details["failure_code"] = failure_code
            try:
                store.finish_run(
                    run_id,
                    status=status,
                    details=details,
                    attempt_token=attempt_token,
                )
            except Exception:
                if primary_error is None:
                    primary_error = RuntimeError(
                        "probe terminal persistence failed"
                    )
        try:
            lease_released = lease.release() is True
        except Exception:
            lease_released = False

    if primary_error is not None:
        raise primary_error
    return {
        "status": "completed",
        "observe_only": True,
        "run_id": run_id,
        "profiles_observed": profiles_observed,
        "validations_recorded": validations_recorded,
        "lease_released": lease_released,
    }


def _healing_result(
    status: str,
    *,
    published: bool = False,
    reconciled: bool | None = None,
    new_version: str | None = None,
    failed_aliases: object = (),
    failure_code: str = "",
    validation_evidence: object = None,
    match_count: object = None,
    required_state: str = "",
    repairs: object = (),
    candidate: object = None,
) -> dict[str, object]:
    aliases: list[str] = []
    if isinstance(failed_aliases, Sequence) and not isinstance(
        failed_aliases,
        (str, bytes, bytearray),
    ):
        aliases = list(
            dict.fromkeys(
                item.strip()
                for item in failed_aliases
                if isinstance(item, str) and item.strip()
            )
        )
    result: dict[str, object] = {
        "status": status,
        "published": published,
        "new_version": new_version,
        "proposed_pause_aliases": aliases,
    }
    if reconciled is not None:
        result["reconciled"] = reconciled
    if failure_code:
        result["failure_code"] = failure_code
    if isinstance(validation_evidence, Mapping):
        result["validation_evidence"] = dict(validation_evidence)
    if (
        isinstance(match_count, int)
        and not isinstance(match_count, bool)
        and match_count >= 0
    ):
        result["match_count"] = match_count
    if isinstance(required_state, str) and required_state in _PAGE_STATES:
        result["required_state"] = required_state
    safe_repairs = _safe_repair_attempts(repairs)
    if safe_repairs:
        result["repairs"] = safe_repairs
    if isinstance(candidate, Mapping):
        result["candidate"] = dict(candidate)
    return result


def _healing_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    return {"status": "unavailable", "failure_class": "infrastructure"}


def _healing_passed(value: object) -> bool:
    return _healing_mapping(value).get("status") in {
        "healthy",
        "passed",
        "validated",
    }


def _selector_failure(value: object) -> bool:
    result = _healing_mapping(value)
    failure_class = result.get("failure_class")
    if failure_class == "selector":
        return True
    if failure_class == "infrastructure":
        return False
    status = result.get("status")
    return status in {
        "selector_failed",
        "selector_validation_failed",
    } or (
        status == "failed"
        and isinstance(result.get("failed_aliases"), Sequence)
        and not isinstance(
            result.get("failed_aliases"),
            (str, bytes, bytearray),
        )
    )


def _candidate_methods(bundle: object) -> set[str]:
    methods: set[str] = set()
    if not isinstance(bundle, Mapping):
        return methods
    elements = bundle.get("elements")
    if not isinstance(elements, Mapping):
        return methods
    for definition in elements.values():
        if not isinstance(definition, Mapping):
            continue
        locators = definition.get("locators")
        if not isinstance(locators, Sequence) or isinstance(
            locators,
            (str, bytes, bytearray),
        ):
            continue
        for locator in locators:
            if not isinstance(locator, Mapping):
                continue
            locator_type = locator.get("type")
            if locator_type == "attribute":
                name = locator.get("name")
                if isinstance(name, str) and name:
                    methods.add(f"attribute:{name}")
            elif locator_type == "role":
                role = locator.get("role")
                name_mode = locator.get("name_mode")
                name = locator.get("name")
                if isinstance(role, str) and role:
                    name_value = (
                        name[:160]
                        if isinstance(name, str) and name
                        else ""
                    )
                    methods.add(
                        f"role:{role}:{name_mode or 'exact'}:{name_value}"
                    )
            elif locator_type in {"css", "xpath"}:
                methods.add(str(locator_type))
    if not methods:
        version = bundle.get("version")
        if isinstance(version, str) and version:
            methods.add(f"candidate:{version}")
    return methods


def _candidate_method(
    bundle: object,
    failed_aliases: object,
) -> str:
    if not isinstance(bundle, Mapping):
        return ""
    elements = bundle.get("elements")
    if not isinstance(elements, Mapping):
        return ""
    selected_aliases = (
        [
            alias
            for alias in failed_aliases
            if isinstance(alias, str) and alias in elements
        ]
        if isinstance(failed_aliases, Sequence)
        and not isinstance(failed_aliases, (str, bytes, bytearray))
        else []
    )
    for alias in [*selected_aliases, *elements]:
        definition = elements.get(alias)
        locators = (
            definition.get("locators")
            if isinstance(definition, Mapping)
            else None
        )
        if not isinstance(locators, Sequence) or isinstance(
            locators,
            (str, bytes, bytearray),
        ):
            continue
        for locator in locators:
            method = (
                locator.get("type")
                if isinstance(locator, Mapping)
                else None
            )
            if isinstance(method, str) and _SAFE_CODE.fullmatch(method):
                return method
    return ""


def _safe_repair_attempts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    fields = (
        "attempt",
        "previous_method",
        "failure_code",
        "match_count",
        "new_method",
        "prompt_version",
        "model_id",
        "result",
    )
    repairs: list[dict[str, object]] = []
    for raw in value[:3]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, object] = {}
        for field in fields:
            selected = raw.get(field)
            if isinstance(selected, int) and not isinstance(selected, bool):
                item[field] = selected
            elif isinstance(selected, str) and len(selected) <= 128:
                item[field] = selected
        repairs.append(item)
    return repairs


def _reported_prohibitions(value: object) -> set[str]:
    result = _healing_mapping(value)
    methods: set[str] = set()
    for key in ("prohibited_methods", "failed_methods"):
        raw = result.get(key)
        if not isinstance(raw, Sequence) or isinstance(
            raw,
            (str, bytes, bytearray),
        ):
            continue
        methods.update(
            item.strip()
            for item in raw
            if isinstance(item, str) and item.strip()
        )
    return methods


def _failed_aliases(value: object) -> object:
    return _healing_mapping(value).get("failed_aliases", ())


def _fresh_validation_context(
    method: Callable,
    failed_aliases: object,
) -> object:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "failed_aliases" in parameters:
        return method(failed_aliases=failed_aliases)
    return method()


def _retryable_repair_error(error: BaseException) -> bool:
    if isinstance(error, ModelOutputFormatError):
        return True
    return getattr(error, "code", "") in {
        "model_timeout",
        "model_network_error",
        "model_http_error",
        "model_invalid_json",
        "model_invalid_response",
        "model_output_too_large",
    }


def _runtime_contract(runtime: object) -> dict[str, Callable] | None:
    required = (
        "validate_active",
        "deterministic_candidates",
        "fresh_validation_context",
        "validate_candidate",
        "repair_candidate",
        "full_validate",
        "store_and_publish",
    )
    methods = {
        name: getattr(runtime, name, None)
        for name in required
    }
    if not all(callable(method) for method in methods.values()):
        return None
    return methods


def _fresh_context_identity(
    value: object,
) -> tuple[str, str, object] | None:
    if not isinstance(value, Mapping):
        return None
    snapshot_hash = value.get("snapshot_hash")
    page_generation = value.get("page_generation")
    if not all(
        isinstance(item, str) and _EVIDENCE_HASH.fullmatch(item)
        for item in (snapshot_hash, page_generation)
    ):
        return None
    snapshot = value.get("snapshot")
    try:
        canonical_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    except (RecursionError, TypeError, ValueError):
        return None
    if snapshot_hash != canonical_hash:
        return None
    capture = value.get("_snapshot", snapshot)
    return str(snapshot_hash), str(page_generation), capture


def _strict_full_evidence(
    bundle: object,
    evidence: object,
) -> dict[str, object] | None:
    from selector_probe.store import (
        _validated_bundle,
        _validated_evidence,
    )

    try:
        value = dict(bundle) if isinstance(bundle, Mapping) else bundle
        if isinstance(value, dict):
            value.pop("version", None)
        canonical, bundle_hash = _validated_bundle(value)
        return _validated_evidence(
            evidence,
            bundle_hash,
            canonical["elements"],
        )
    except (TypeError, ValueError):
        return None


def run_healing_probe(
    runtime: object,
    *,
    candidate_fn: Callable[..., object] | None = None,
    model_call: Callable[..., object] | None = None,
    repair_fn: Callable[..., object] | None = None,
    force_requested_candidate: bool = False,
    initial_failed_aliases: object = (),
) -> dict[str, object]:
    """Validate active selectors and publish only a fully validated repair."""

    if not isinstance(force_requested_candidate, bool):
        raise ValueError("force_requested_candidate must be a boolean")
    requested_aliases = [
        alias.strip()
        for alias in (
            initial_failed_aliases
            if isinstance(initial_failed_aliases, Sequence)
            and not isinstance(
                initial_failed_aliases,
                (str, bytes, bytearray),
            )
            else ()
        )
        if isinstance(alias, str) and alias.strip()
    ]
    methods = _runtime_contract(runtime)
    if methods is None:
        return _healing_result(
            "infrastructure_unavailable",
            failure_code="runtime_contract_invalid",
        )
    try:
        active_result = _healing_mapping(methods["validate_active"]())
    except Exception:
        return _healing_result(
            "infrastructure_unavailable",
            failure_code="validate_active_unavailable",
        )
    active_passed = _healing_passed(active_result)
    if active_passed and not force_requested_candidate:
        return _healing_result(
            "healthy",
            validation_evidence=active_result.get("evidence"),
        )
    if not active_passed and not _selector_failure(active_result):
        return _healing_result("infrastructure_unavailable")

    repair = repair_fn or methods["repair_candidate"]
    selected_model_call = model_call or getattr(runtime, "model_call", None)
    if active_passed:
        last_failure: Mapping[str, object] = {
            "status": "failed",
            "failure_class": "selector",
            "failed_aliases": requested_aliases,
            "code": "requested_candidate_required",
        }
        failed_aliases: object = requested_aliases
    else:
        last_failure = active_result
        failed_aliases = (
            requested_aliases or _failed_aliases(active_result)
        )
    prohibited: set[str] = set()
    candidate: object = None

    try:
        candidate = methods["deterministic_candidates"](
            candidate_fn=candidate_fn,
        )
    except Exception as error:
        failure_code = _safe_error_code(error)
        if failure_code == "probe_unavailable":
            failure_code = "deterministic_candidates_unavailable"
        return _healing_result(
            "infrastructure_unavailable",
            failure_code=failure_code,
        )
    if candidate is None:
        deterministic_failure = getattr(
            runtime,
            "deterministic_failure",
            None,
        )
        if callable(deterministic_failure):
            reported = _healing_mapping(deterministic_failure())
            if _selector_failure(reported):
                last_failure = reported
                failed_aliases = (
                    _failed_aliases(reported) or failed_aliases
                )

    validated_candidate: object = None
    full_evidence: Mapping[str, object] | None = None

    def validate_fully(value: object):
        try:
            validation = _healing_mapping(
                methods["validate_candidate"](value)
            )
        except Exception:
            return "infrastructure", _healing_result(
                "infrastructure_unavailable",
                failure_code="candidate_validation_unavailable",
            ), None
        if not _healing_passed(validation):
            if _selector_failure(validation):
                return "selector", validation, None
            return "infrastructure", _healing_result(
                "infrastructure_unavailable"
            ), None
        try:
            full_result = _healing_mapping(
                methods["full_validate"](value)
            )
        except Exception:
            return "infrastructure", _healing_result(
                "infrastructure_unavailable",
                failure_code="full_validation_unavailable",
            ), None
        if not _healing_passed(full_result):
            if _selector_failure(full_result):
                return "selector", full_result, None
            return "infrastructure", _healing_result(
                "infrastructure_unavailable"
            ), None
        evidence = _strict_full_evidence(value, full_result)
        if evidence is None:
            return "infrastructure", _healing_result(
                "infrastructure_unavailable",
                failure_code="full_validation_invalid",
            ), None
        return "passed", full_result, evidence

    if candidate is not None:
        outcome, result, evidence = validate_fully(candidate)
        if outcome == "infrastructure":
            return result
        if outcome == "passed":
            validated_candidate = candidate
            full_evidence = evidence
        else:
            last_failure = result
            failed_aliases = _failed_aliases(result) or failed_aliases
            prohibited.update(_candidate_methods(candidate))
            prohibited.update(_reported_prohibitions(result))

    fresh_captures: list[object] = []
    page_generations: set[str] = set()
    repair_attempts: list[dict[str, object]] = []
    if validated_candidate is None:
        previous_candidate = candidate
        for attempt in range(1, 4):
            try:
                context = _healing_mapping(
                    _fresh_validation_context(
                        methods["fresh_validation_context"],
                        failed_aliases,
                    )
                )
            except Exception:
                return _healing_result(
                    "infrastructure_unavailable",
                    failure_code="validation_context_unavailable",
                )
            identity = _fresh_context_identity(context)
            if (
                identity is None
                or any(identity[2] is item for item in fresh_captures)
                or identity[1] in page_generations
            ):
                return _healing_result(
                    "infrastructure_unavailable",
                    failure_code="validation_context_not_fresh",
                )
            fresh_captures.append(identity[2])
            page_generations.add(identity[1])
            failure_code = last_failure.get("code")
            repair_record: dict[str, object] = {
                "attempt": attempt,
                "previous_method": _candidate_method(
                    previous_candidate,
                    failed_aliases,
                ),
                "failure_code": (
                    failure_code
                    if isinstance(failure_code, str)
                    and _SAFE_CODE.fullmatch(failure_code)
                    else "zero_match"
                ),
                "new_method": "",
                "prompt_version": "selector-repair-v1",
                "model_id": str(
                    getattr(
                        getattr(runtime, "config", None),
                        "model_id",
                        "",
                    )
                    or ""
                )[:128],
                "result": "rejected",
            }
            failure_match_count = last_failure.get("match_count")
            if (
                isinstance(failure_match_count, int)
                and not isinstance(failure_match_count, bool)
                and failure_match_count >= 0
            ):
                repair_record["match_count"] = failure_match_count
            try:
                candidate = repair(
                    attempt=attempt,
                    prohibited_methods=tuple(sorted(prohibited)),
                    context=context,
                    failure=last_failure,
                    previous_candidate=previous_candidate,
                    model_call=selected_model_call,
                )
            except Exception as error:
                repair_attempts.append(repair_record)
                if not _retryable_repair_error(error):
                    return _healing_result(
                        "infrastructure_unavailable",
                        failure_code="repair_unavailable",
                        repairs=repair_attempts,
                    )
                prohibited.add(f"repair_parse:attempt-{attempt}")
                prohibited.add(f"candidate:attempt-{attempt}")
                continue
            previous_candidate = candidate
            if candidate is None:
                repair_attempts.append(repair_record)
                prohibited.add(f"candidate:attempt-{attempt}")
                continue
            repair_record["new_method"] = _candidate_method(
                candidate,
                failed_aliases,
            )
            outcome, result, evidence = validate_fully(candidate)
            repair_record["result"] = (
                "passed"
                if outcome == "passed"
                else "failed"
                if outcome == "selector"
                else "unavailable"
            )
            repair_attempts.append(repair_record)
            if outcome == "infrastructure":
                return {
                    **dict(result),
                    "repairs": _safe_repair_attempts(repair_attempts),
                }
            if outcome == "passed":
                validated_candidate = candidate
                full_evidence = evidence
                break
            last_failure = result
            failed_aliases = _failed_aliases(result) or failed_aliases
            prohibited.update(_candidate_methods(candidate))
            prohibited.update(_reported_prohibitions(result))

    if validated_candidate is None or full_evidence is None:
        return _healing_result(
            "selector_validation_failed",
            failed_aliases=failed_aliases,
            failure_code=str(last_failure.get("code") or "zero_match"),
            match_count=last_failure.get("match_count"),
            required_state=str(last_failure.get("required_state") or ""),
            repairs=repair_attempts,
        )

    prepare_publication = getattr(runtime, "prepare_publication", None)
    if callable(prepare_publication):
        prepare_publication(
            validated_candidate,
            full_evidence,
            _safe_repair_attempts(repair_attempts),
        )
    try:
        publication = methods["store_and_publish"](
            validated_candidate,
            full_evidence,
        )
    except Exception:
        return _healing_result(
            "publication_failed",
            failure_code="publication_unavailable",
            repairs=repair_attempts,
        )
    if not isinstance(publication, Mapping):
        return _healing_result(
            "publication_failed",
            repairs=repair_attempts,
        )
    published = publication.get("published") is True
    reconciled = publication.get("reconciled")
    version = publication.get("version")
    if (
        not published
        or reconciled is not True
        or not isinstance(version, str)
        or not version
    ):
        return _healing_result(
            "publication_failed",
            repairs=repair_attempts,
        )
    return _healing_result(
        "published",
        published=True,
        reconciled=True,
        new_version=version,
        validation_evidence=full_evidence,
        repairs=repair_attempts,
        candidate=(
            validated_candidate
            if force_requested_candidate
            else None
        ),
    )


__all__ = [
    "ELEMENT_REQUEST_TYPES",
    "ElementRequestBusy",
    "LEASE_HEARTBEAT_SECONDS",
    "LEASE_TTL_SECONDS",
    "ProbeCleanupFailed",
    "ProbeLeaseLost",
    "ProbeValidationFailed",
    "ModelOutputFormatError",
    "dispatch_element_request",
    "run_element_probe",
    "run_healing_probe",
    "run_observe_probe",
    "unavailable_element_request_dispatcher",
]
