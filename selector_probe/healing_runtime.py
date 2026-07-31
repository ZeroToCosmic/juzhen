"""Production resources and adapters for selector healing."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import hashlib
import inspect
import json
import queue
import threading
from typing import Callable

from browser_cdp import wait_for_cdp as default_wait_for_cdp
from browser_element_schema import TIKTOK_COMMENT_TEMPLATE
from selector_probe.candidates import generate_candidates
from selector_probe.candidates import _matching_node
from selector_probe.contracts import (
    default_tiktok_contracts,
    normalize_contracts,
)
from selector_probe.model_client import ask_model_json, select_model
from selector_probe.probe import ModelOutputFormatError
from selector_probe.registry import reconcile_registry
from selector_probe.redaction import capture_redacted_screenshot
from selector_probe.repair import (
    _candidate_anchors,
    _candidate_signature,
    repair_candidates,
)
from selector_probe.session import ProbeSessionManager
from selector_probe.snapshot import (
    SemanticSnapshot,
    extract_semantic_snapshot,
)
from selector_probe.state_runner import ProbeStateRunner
from selector_probe.store import _validated_bundle, _validated_evidence
from selector_probe.validator import (
    ResetCapture,
    ValidationRejected,
    validate_bundle_on_page,
    validate_two_rounds,
)


_SELECTOR_FAILURE_CODES = {
    "bundle_invalid",
    "candidate_changed",
    "contracts_invalid",
    "element_identity_changed",
    "element_inspection_failed",
    "element_not_actionable",
    "element_resolution_failed",
    "element_unstable",
    "postcondition_failed",
    "required_state_failed",
    "profile_validation_failed",
    "semantic_attribute_mismatch",
    "semantic_name_mismatch",
    "semantic_role_mismatch",
    "selector_validation_failed",
    "wrong_semantics",
    "zero_match",
    "multiple_match",
}


async def _start_playwright() -> object:
    from playwright.async_api import async_playwright

    return await async_playwright().start()


def _hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bundle_value(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("selector bundle must be an object")
    raw = dict(value)
    raw.pop("version", None)
    canonical, _bundle_hash = _validated_bundle(raw)
    return canonical


class HealingRuntime:
    """Own dedicated browser resources for one healing probe run."""

    def __init__(
        self,
        *,
        config: object,
        settings: Mapping[str, object],
        store: object,
        registry: object,
        adspower_client: object,
        elements: Mapping[str, object],
        stop_event: object | None = None,
        session_manager_factory: Callable[..., object] = ProbeSessionManager,
        playwright_starter: Callable[..., object] = _start_playwright,
        state_runner_factory: Callable[..., object] = ProbeStateRunner,
        snapshot_extractor: Callable[..., object] = (
            extract_semantic_snapshot
        ),
        candidate_generator: Callable[..., object] = generate_candidates,
        repair_generator: Callable[..., object] = repair_candidates,
        model_selector: Callable[..., object] = select_model,
        model_request: Callable[..., object] = ask_model_json,
        page_validator: Callable[..., object] = validate_bundle_on_page,
        full_validator: Callable[..., object] = validate_two_rounds,
        reconciler: Callable[..., object] = reconcile_registry,
        wait_for_cdp: Callable[[str], object] = default_wait_for_cdp,
        lease_guard: Callable[..., object] | None = None,
        probe_run_id: int | None = None,
        attempt_token: str = "",
        element_request_id: str = "",
        element_request_claim_token: str = "",
        element_request_generation: int = 0,
        contracts_override: Mapping[str, object] | None = None,
    ) -> None:
        self.config = config
        self.settings = dict(settings)
        self.store = store
        self.registry = registry
        self.adspower_client = adspower_client
        self.saved_elements = dict(elements)
        self.stop_event = stop_event
        self.session_manager_factory = session_manager_factory
        self.playwright_starter = playwright_starter
        self.state_runner_factory = state_runner_factory
        self.snapshot_extractor = snapshot_extractor
        self.candidate_generator = candidate_generator
        self.repair_generator = repair_generator
        self.model_selector = model_selector
        self.model_request = model_request
        self.page_validator = page_validator
        self.full_validator = full_validator
        self.reconciler = reconciler
        self.wait_for_cdp = wait_for_cdp
        self.lease_guard = lease_guard
        self.probe_run_id = probe_run_id
        self.attempt_token = attempt_token
        if (probe_run_id is None) != (attempt_token == ""):
            raise ValueError(
                "probe_run_id and attempt_token must be supplied together"
            )
        self.element_request_id = element_request_id
        self.element_request_claim_token = element_request_claim_token
        self.element_request_generation = element_request_generation
        if bool(element_request_id) != bool(element_request_claim_token):
            raise ValueError("element request publication context is invalid")
        if element_request_id and (
            isinstance(element_request_generation, bool)
            or not isinstance(element_request_generation, int)
            or element_request_generation < 1
        ):
            raise ValueError("element request generation is invalid")
        self._staged_element_result: dict[str, object] | None = None

        raw_probe = self.settings.get("selector_probe", {})
        raw_contracts = (
            raw_probe.get("contracts")
            if isinstance(raw_probe, Mapping)
            else None
        )
        self._contracts_override = contracts_override is not None
        if contracts_override is not None:
            self.contracts = normalize_contracts(contracts_override)
        else:
            self.contracts = default_tiktok_contracts()
            if raw_contracts is not None:
                self.contracts.update(normalize_contracts(raw_contracts))

        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_manager: object | None = None
        self._profile_handles: list[object] = []
        self._page_handles: list[object] = []
        self._playwright: object | None = None
        self._runners: dict[int, object] = {}
        self._active_bundle: dict[str, object] | None = None
        self._candidate_elements: dict[str, object] = {}
        self._capture_counter = 0
        self._model_config: object | None = None
        self._repair_history: dict[str, list[Mapping[str, object]]] = {}
        self._repair_signatures: dict[str, set[str]] = {}
        self._repair_anchors: dict[str, set[str]] = {}
        self._repair_prohibited: dict[str, set[str]] = {}
        self._deterministic_failure: dict[str, object] | None = None
        self._lease_error: BaseException | None = None
        self._latest_snapshots: dict[
            tuple[int, str],
            SemanticSnapshot,
        ] = {}

    def __enter__(self) -> HealingRuntime:
        if self._loop is not None:
            raise RuntimeError("healing runtime is already open")
        self._loop = asyncio.new_event_loop()
        try:
            self._run(self._open())
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            self.close()
        except Exception:
            if exc_value is None:
                raise
        return False

    def _run(self, awaitable, *, guard: bool = True):
        if self._loop is None:
            raise RuntimeError("healing runtime is not open")

        async def guarded():
            target = asyncio.ensure_future(awaitable)
            try:
                while not target.done():
                    await asyncio.wait({target}, timeout=0.05)
                    if guard and not target.done():
                        try:
                            self._require_owned()
                        except BaseException:
                            target.cancel()
                            await asyncio.gather(
                                target,
                                return_exceptions=True,
                            )
                            raise
                return target.result()
            finally:
                if not target.done():
                    target.cancel()

        return self._loop.run_until_complete(guarded())

    def _stopped(self) -> bool:
        is_set = getattr(self.stop_event, "is_set", None)
        return callable(is_set) and is_set()

    def _require_owned(self, *, renew: bool = False) -> None:
        if self._lease_error is not None:
            raise self._lease_error
        if self._stopped():
            raise asyncio.CancelledError()
        if self.lease_guard is not None:
            self.lease_guard(renew=renew)

    def _stop_requested(self) -> bool:
        if self._stopped():
            return True
        try:
            self._require_owned()
        except BaseException as error:
            self._lease_error = error
            return True
        return False

    async def _open(self) -> None:
        self._require_owned(renew=True)
        if not self._contracts_override:
            seed_contracts = {
                alias: {
                    **contract.public_dict(),
                    "site": self.config.site,
                    "environment": self.config.environment,
                    "enabled": True,
                }
                for alias, contract in self.contracts.items()
            }
            self.store.seed_contracts(seed_contracts)
            stored_contracts = self.store.list_contracts(
                site=self.config.site,
                environment=self.config.environment,
            )
            if not stored_contracts:
                raise RuntimeError("selector_contract_catalog_empty")
            self.contracts = normalize_contracts(stored_contracts)
        self._require_owned()
        self._session_manager = self.session_manager_factory(
            self.adspower_client,
            allowed_profile_ids=self.config.test_profile_ids,
            wait_for_cdp=self.wait_for_cdp,
            stop_requested=self._stop_requested,
        )
        self._require_owned()
        self._profile_handles = list(
            self._session_manager.open_profiles(
                self.config.test_profile_ids
            )
        )
        self._require_owned()
        self._playwright = await self.playwright_starter()
        for profile in self._profile_handles:
            self._require_owned()
            page_handle = await self._session_manager.open_probe_page(
                self._playwright,
                profile,
            )
            self._require_owned()
            self._page_handles.append(page_handle)
            self._runners[id(page_handle.page)] = (
                self.state_runner_factory(
                    target_url=self.config.target_url
                )
            )

    def close(self) -> None:
        if self._loop is None:
            return
        failures = False
        try:
            if self._page_handles and self._session_manager is not None:
                try:
                    results = self._run(
                        self._session_manager.close_owned_pages(
                            self._page_handles
                        ),
                        guard=False,
                    )
                    failures = failures or any(
                        item.get("ok") is not True
                        for item in results
                        if isinstance(item, Mapping)
                    )
                except BaseException:
                    failures = True
            if self._playwright is not None:
                try:
                    stop = getattr(self._playwright, "stop", None)
                    if callable(stop):
                        self._run(stop(), guard=False)
                except BaseException:
                    failures = True
            if self._profile_handles and self._session_manager is not None:
                try:
                    results = self._session_manager.stop_owned_profiles(
                        self._profile_handles
                    )
                    failures = failures or any(
                        item.get("ok") is not True
                        for item in results
                        if isinstance(item, Mapping)
                    )
                except BaseException:
                    failures = True
        finally:
            self._page_handles = []
            self._profile_handles = []
            self._playwright = None
            self._session_manager = None
            self._runners = {}
            loop = self._loop
            self._loop = None
            loop.close()
        if failures:
            raise RuntimeError("healing_runtime_cleanup_failed")

    def _primary(self) -> object:
        if not self._page_handles:
            raise RuntimeError("healing runtime has no probe page")
        return self._page_handles[0]

    async def _capture(
        self,
        page_handle: object,
        *,
        reload_page: bool,
        state: str = "feed_ready",
    ) -> tuple[SemanticSnapshot, str, str]:
        self._require_owned()
        page = page_handle.page
        runner = self._runners[id(page)]
        if reload_page:
            reload_method = getattr(page, "reload", None)
            if callable(reload_method):
                await reload_method()
            self._require_owned()
            if hasattr(runner, "current_state"):
                runner.current_state = None
        await runner.ensure_state(
            page,
            state,
            self._active_elements(),
        )
        self._require_owned()
        snapshot = await self.snapshot_extractor(page)
        self._require_owned()
        if not isinstance(snapshot, SemanticSnapshot):
            raise RuntimeError("semantic_snapshot_invalid")
        self._latest_snapshots[(id(page), state)] = snapshot
        snapshot_hash = _hash(snapshot.model_payload())
        self._capture_counter += 1
        page_generation = _hash(
            {
                "counter": self._capture_counter,
                "profile_mask": page_handle.profile.profile_mask,
                "snapshot_hash": snapshot_hash,
                "url": getattr(page, "url", ""),
            }
        )
        return snapshot, snapshot_hash, page_generation

    def _active_elements(self) -> dict[str, object]:
        if self._candidate_elements:
            return dict(self._candidate_elements)
        if self._active_bundle is not None:
            elements = self._active_bundle.get("elements")
            if isinstance(elements, Mapping):
                return dict(elements)
        return dict(self.saved_elements)

    def validate_active(self) -> dict[str, object]:
        self._require_owned()
        try:
            active = self.registry.get_active()
        except Exception:
            return {
                "status": "unavailable",
                "failure_class": "infrastructure",
            }
        if not isinstance(active, Mapping):
            return {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": list(self.contracts),
                "code": "zero_match",
                "match_count": 0,
            }
        try:
            self._active_bundle = {
                **_bundle_value(active),
                "version": active.get("version", ""),
            }
        except ValueError:
            return {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": list(self.contracts),
                "code": "wrong_semantics",
                "match_count": 0,
            }
        try:
            evidence = self.full_validate(self._active_bundle)
            canonical = _bundle_value(self._active_bundle)
            validated = _validated_evidence(
                evidence,
                canonical["bundle_hash"],
                canonical["elements"],
            )
        except ValidationRejected as error:
            return self._validation_failure(error)
        except Exception:
            return {
                "status": "unavailable",
                "failure_class": "infrastructure",
            }
        return {
            "status": "passed",
            "version": self._active_bundle.get("version"),
            "evidence": validated,
        }

    def deterministic_candidates(
        self,
        *,
        candidate_fn: Callable[..., object] | None = None,
    ) -> object:
        self._require_owned()
        self._deterministic_failure = None
        bundle = self._run(self._deterministic_bundle())
        if bundle is None:
            return None
        self._candidate_elements = dict(bundle["elements"])
        return candidate_fn(bundle) if callable(candidate_fn) else bundle

    def deterministic_failure(self) -> dict[str, object] | None:
        return copy_mapping(self._deterministic_failure) if (
            self._deterministic_failure is not None
        ) else None

    async def _deterministic_bundle(self) -> dict[str, object] | None:
        page_handle = self._primary()
        page = page_handle.page
        runner = self._runners[id(page)]
        active_elements = self._active_elements()
        historical = {
            alias: active_elements[alias]
            for alias in self.contracts
            if alias in active_elements
        }
        working = dict(historical)
        definitions: dict[str, object] = dict(historical)
        failures: list[tuple[str, str]] = []
        for alias, contract in self.contracts.items():
            self._require_owned()
            await runner.ensure_state(
                page,
                contract.required_state,
                working,
            )
            self._require_owned()
            snapshot = await self.snapshot_extractor(page)
            self._require_owned()
            if not isinstance(snapshot, SemanticSnapshot):
                raise RuntimeError("semantic_snapshot_invalid")
            self._latest_snapshots[
                (id(page), contract.required_state)
            ] = snapshot
            historical_definition = historical.get(alias, {})
            template_definition = TIKTOK_COMMENT_TEMPLATE.get(alias)
            if isinstance(template_definition, Mapping):
                seed_locators = [
                    dict(item)
                    for item in template_definition.get("locators", ())
                    if isinstance(item, Mapping)
                ]
                if (
                    isinstance(historical_definition, Mapping)
                    and historical_definition.get("scope")
                    == contract.scope
                ):
                    seed_locators.extend(
                        dict(item)
                        for item in historical_definition.get(
                            "locators",
                            (),
                        )
                        if isinstance(item, Mapping)
                    )
                historical_definition = {
                    "scope": contract.scope,
                    "locators": seed_locators,
                }
            candidates = self.candidate_generator(
                contract,
                snapshot,
                historical_definition,
            )
            self._require_owned()
            if not candidates:
                failures.append((alias, contract.required_state))
                continue
            definition = {
                "scope": contract.scope,
                "locators": candidates,
            }
            definitions[alias] = definition
            working[alias] = definition
        if failures:
            self._candidate_elements = dict(definitions)
            self._deterministic_failure = {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": [alias for alias, _state in failures],
                "code": "zero_match",
                "match_count": 0,
                "required_state": failures[0][1],
            }
            return None
        return _bundle_value({"elements": definitions})

    def capture_failure_screenshot(
        self,
        *,
        failed_aliases: object,
        target_path: object,
        evidence_root: object,
    ) -> object:
        """Capture only a safely isolated failed-element viewport region."""

        if not isinstance(failed_aliases, (list, tuple)):
            raise ValueError("failed_aliases must be an array")
        aliases = tuple(
            dict.fromkeys(
                alias
                for alias in failed_aliases
                if isinstance(alias, str) and alias in self.contracts
            )
        )
        if not aliases:
            raise RuntimeError("failure screenshot has no known aliases")
        states = {
            self.contracts[alias].required_state for alias in aliases
        }
        if len(states) != 1:
            raise RuntimeError("failure screenshot spans page states")
        page = self._primary().page
        state = next(iter(states))
        snapshot = self._latest_snapshots.get((id(page), state))
        if snapshot is None or snapshot.viewport is None:
            raise RuntimeError("failure screenshot has no safe snapshot")
        viewport_width, viewport_height = snapshot.viewport
        selected_bounds: list[tuple[float, float, float, float]] = []
        for alias in aliases:
            contract = self.contracts[alias]
            matches = [
                node
                for node in snapshot.nodes
                if node.actionable
                and node.bounds is not None
                and _matching_node(contract, node)
            ]
            if len(matches) != 1:
                raise RuntimeError(
                    "failure screenshot element scope is ambiguous"
                )
            assert matches[0].bounds is not None
            selected_bounds.append(matches[0].bounds)
        margin = 20.0
        left = max(0.0, min(item[0] for item in selected_bounds) - margin)
        top = max(0.0, min(item[1] for item in selected_bounds) - margin)
        right = min(
            float(viewport_width),
            max(item[0] + item[2] for item in selected_bounds) + margin,
        )
        bottom = min(
            float(viewport_height),
            max(item[1] + item[3] for item in selected_bounds) + margin,
        )
        width = right - left
        height = bottom - top
        if (
            width <= 0
            or height <= 0
            or width * height
            > float(viewport_width * viewport_height) * 0.5
        ):
            raise RuntimeError("failure screenshot scope is not safely bounded")
        regions: list[dict[str, float]] = []

        def redact(x: float, y: float, w: float, h: float) -> None:
            if w > 0 and h > 0:
                regions.append(
                    {"x": x, "y": y, "width": w, "height": h}
                )

        redact(0.0, 0.0, float(viewport_width), top)
        redact(0.0, bottom, float(viewport_width), viewport_height - bottom)
        redact(0.0, top, left, height)
        redact(right, top, viewport_width - right, height)
        for node in snapshot.nodes:
            if (
                node.bounds is not None
                and node.visible
                and node.in_viewport
                and (
                    node.tag in {"input", "textarea"}
                    or node.role in {"searchbox", "textbox"}
                    or node.attributes.get("contenteditable") == "true"
                    or node.states.get("editable") is True
                )
            ):
                redact(*node.bounds)
        return self._run(
            capture_redacted_screenshot(
                page,
                regions,
                target_path,
                evidence_root=evidence_root,
            )
        )

    def fresh_validation_context(
        self,
        *,
        failed_aliases: object = (),
    ) -> dict[str, object]:
        aliases = (
            tuple(
                item for item in failed_aliases
                if isinstance(item, str) and item in self.contracts
            )
            if isinstance(failed_aliases, (list, tuple))
            else ()
        )
        selected_aliases = aliases or (next(iter(self.contracts)),)
        states = tuple(
            dict.fromkeys(
                self.contracts[alias].required_state
                for alias in selected_aliases
            )
        )
        captures: dict[str, tuple[SemanticSnapshot, str, str]] = {}
        for state in states:
            captures[state] = self._run(
                self._capture(
                    self._primary(),
                    reload_page=True,
                    state=state,
                )
            )
        primary_state = states[0]
        snapshot, snapshot_hash, page_generation = captures[primary_state]
        return {
            "active_bundle": copy_mapping(self._active_bundle or {}),
            "snapshot": snapshot.model_payload(),
            "snapshot_hash": snapshot_hash,
            "page_generation": page_generation,
            "contracts": {
                alias: contract.public_dict()
                for alias, contract in self.contracts.items()
            },
            "_snapshot": snapshot,
            "_snapshots_by_state": {
                state: capture[0]
                for state, capture in captures.items()
            },
        }

    def _validation_failure(
        self,
        error: ValidationRejected,
    ) -> dict[str, object]:
        selector = error.code in _SELECTOR_FAILURE_CODES
        repair_code = (
            error.code
            if error.code in {
                "zero_match",
                "multiple_match",
                "postcondition_failed",
            }
            else (
                "wrong_semantics"
                if error.code.startswith("semantic_")
                else "zero_match"
            )
        )
        alias = getattr(error, "alias", "")
        required_state = getattr(error, "required_state", "")
        failures = getattr(error, "failures", ())
        failed_aliases = [
            str(item.get("alias"))
            for item in failures
            if isinstance(item, Mapping) and item.get("alias")
        ]
        result = {
            "status": "failed",
            "failure_class": "selector" if selector else "infrastructure",
            "failed_aliases": (
                list(dict.fromkeys(failed_aliases))
                if selector and failed_aliases
                else [alias]
                if selector and alias
                else (list(self.contracts) if selector else [])
            ),
            "code": repair_code,
            "match_count": getattr(error, "match_count", 0),
            "required_state": required_state,
        }
        if failures:
            result["alias_failures"] = [dict(item) for item in failures]
        return result

    def validate_candidate(self, bundle: object) -> dict[str, object]:
        self._require_owned()
        try:
            canonical = _bundle_value(bundle)
            self._candidate_elements = dict(canonical["elements"])
            evidence = self._run(
                self.page_validator(
                    self._primary().page,
                    canonical,
                    self.contracts,
                    self._runners[id(self._primary().page)],
                )
            )
            self._require_owned()
        except ValidationRejected as error:
            return self._validation_failure(error)
        except Exception:
            return {
                "status": "unavailable",
                "failure_class": "infrastructure",
            }
        if not isinstance(evidence, Mapping) or evidence.get("status") != "passed":
            return {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": list(self.contracts),
                "code": "wrong_semantics",
                "match_count": 0,
            }
        return {"status": "passed"}

    def model_call(self, messages: object, schema: object) -> object:
        self._require_owned()
        if self._model_config is None:
            try:
                self._model_config = self.model_selector(
                    self.settings,
                    self.config.model_id,
                )
            except ValueError:
                raise RuntimeError("model_configuration_invalid") from None
        completed = threading.Event()
        output: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

        def request_model() -> None:
            try:
                output.put(
                    (
                        True,
                        self.model_request(
                            self._model_config,
                            messages,
                            schema,
                        ),
                    )
                )
            except BaseException as error:
                output.put((False, error))
            finally:
                completed.set()

        threading.Thread(
            target=request_model,
            name="selector-probe-model-request",
            daemon=True,
        ).start()
        while not completed.wait(0.1):
            self._require_owned()
        self._require_owned()
        succeeded, value = output.get_nowait()
        if not succeeded:
            raise value
        return value

    def repair_candidate(
        self,
        *,
        attempt: int,
        context: Mapping[str, object],
        failure: Mapping[str, object],
        previous_candidate: object,
        model_call: Callable[..., object] | None,
        prohibited_methods: object = (),
        **_kwargs,
    ) -> dict[str, object] | None:
        default_snapshot = context.get("_snapshot")
        snapshots_by_state = context.get("_snapshots_by_state", {})
        if not isinstance(default_snapshot, SemanticSnapshot):
            raise RuntimeError("semantic_snapshot_invalid")
        previous = (
            _bundle_value(previous_candidate).get("elements", {})
            if isinstance(previous_candidate, Mapping)
            else self._active_elements()
        )
        failed = failure.get("failed_aliases", list(self.contracts))
        failed_aliases = set(failed) if isinstance(failed, list) else set()
        definitions: dict[str, object] = dict(previous)
        selected_call = model_call or self.model_call
        repair_failure = {
            "code": (
                failure.get("code")
                if failure.get("code")
                in {
                    "zero_match",
                    "multiple_match",
                    "wrong_semantics",
                    "postcondition_failed",
                }
                else "zero_match"
            ),
            "match_count": (
                failure.get("match_count")
                if isinstance(failure.get("match_count"), int)
                and not isinstance(failure.get("match_count"), bool)
                else 0
            ),
        }
        for alias, contract in self.contracts.items():
            self._require_owned()
            snapshot = (
                snapshots_by_state.get(contract.required_state)
                if isinstance(snapshots_by_state, Mapping)
                else None
            )
            if not isinstance(snapshot, SemanticSnapshot):
                snapshot = default_snapshot
            old = previous.get(alias, {})
            old_locators = (
                old.get("locators", [])
                if isinstance(old, Mapping)
                else []
            )
            locators = old_locators
            if not failed_aliases or alias in failed_aliases:
                history = self._repair_history.setdefault(
                    alias,
                    [
                        item
                        for item in old_locators
                        if isinstance(item, Mapping)
                    ],
                )
                signatures = self._repair_signatures.setdefault(
                    alias,
                    {
                        _candidate_signature(item)
                        for item in history
                    },
                )
                anchors = self._repair_anchors.setdefault(
                    alias,
                    {
                        anchor
                        for item in history
                        for anchor in _candidate_anchors(item)
                    },
                )
                prohibited = self._repair_prohibited.setdefault(alias, set())
                if isinstance(prohibited_methods, (list, tuple)):
                    prohibited.update(
                        item
                        for item in prohibited_methods
                        if isinstance(item, str) and item
                    )
                    if len(prohibited) > 128:
                        prohibited.intersection_update(
                            sorted(prohibited)[:128]
                        )
                bounded_history = (
                    history
                    if len(history) <= 5
                    else [*history[:2], *history[-3:]]
                )
                try:
                    repair_arguments = (
                        contract,
                        snapshot,
                        bounded_history,
                        repair_failure,
                        attempt,
                        selected_call,
                    )
                    try:
                        signature = inspect.signature(
                            self.repair_generator
                        )
                    except (TypeError, ValueError):
                        signature = None
                    supports_prohibited = (
                        signature is not None
                        and (
                            "prohibited_methods" in signature.parameters
                            or any(
                                parameter.kind
                                is inspect.Parameter.VAR_KEYWORD
                                for parameter in signature.parameters.values()
                            )
                        )
                    )
                    if supports_prohibited:
                        locators = self.repair_generator(
                            *repair_arguments,
                            prohibited_methods=tuple(sorted(prohibited)),
                        )
                    else:
                        locators = self.repair_generator(*repair_arguments)
                except ValueError:
                    raise ModelOutputFormatError(
                        "model_output_format_invalid"
                    ) from None
                self._require_owned()
                new_signatures = {
                    _candidate_signature(item)
                    for item in locators
                    if isinstance(item, Mapping)
                }
                new_anchors = {
                    anchor
                    for item in locators
                    if isinstance(item, Mapping)
                    for anchor in _candidate_anchors(item)
                }
                locator_types = {
                    str(item.get("type") or "")
                    for item in locators
                    if isinstance(item, Mapping)
                }
                blocked_report = bool(
                    prohibited.intersection(
                        new_signatures | new_anchors | locator_types
                    )
                )
                if (
                    new_signatures.intersection(signatures)
                    or new_anchors.intersection(anchors)
                    or blocked_report
                ):
                    return None
                signatures.update(new_signatures)
                anchors.update(new_anchors)
                history.extend(
                    item for item in locators if isinstance(item, Mapping)
                )
            if not locators:
                return None
            definitions[alias] = {
                "scope": contract.scope,
                "locators": locators,
            }
        return _bundle_value({"elements": definitions})

    async def _reset_for_full(
        self,
        target: object,
        _round_number: int,
        _profile_mask: str,
    ) -> ResetCapture:
        self._require_owned()
        page_handle = next(
            item for item in self._page_handles if item.page is target
        )
        snapshot, _snapshot_hash, generation = await self._capture(
            page_handle,
            reload_page=True,
        )
        return ResetCapture(
            snapshot=snapshot,
            page_generation=generation,
        )

    async def _inspect_for_full(
        self,
        handle: object,
        _round_number: int,
        bundle: object,
        _challenge: str,
        _reset_evidence: object,
    ) -> dict[str, object]:
        self._require_owned()
        evidence = await self.page_validator(
            handle.page,
            bundle,
            self.contracts,
            self._runners[id(handle.page)],
        )
        self._require_owned()
        aliases = evidence.get("aliases", {})
        return {
            "status": evidence.get("status"),
            "bundle_hash": evidence.get("bundle_hash"),
            "aliases": {
                alias: {
                    "status": item.get("status"),
                    "candidate_id": item.get("candidate_id"),
                }
                for alias, item in aliases.items()
                if isinstance(item, Mapping)
            },
            "actions": evidence.get("actions", []),
        }

    def full_validate(self, bundle: object) -> dict[str, object]:
        self._require_owned()
        canonical = _bundle_value(bundle)
        result = self._run(
            self.full_validator(
                handles=self._page_handles,
                bundle=canonical,
                contracts=self.contracts,
                inspect_fn=self._inspect_for_full,
                reset_fn=self._reset_for_full,
            )
        )
        self._require_owned()
        return result

    def store_and_publish(
        self,
        bundle: object,
        full_evidence: Mapping[str, object],
    ) -> dict[str, object]:
        self._require_owned(renew=True)
        canonical = _bundle_value(bundle)
        base_version = (
            self._active_bundle.get("version", "")
            if self._active_bundle is not None
            else ""
        )
        version = self.store.store_validated_version(
            bundle=canonical,
            evidence=full_evidence,
            base_version_id=base_version,
            model_id=(
                getattr(self._model_config, "id", "")
                if self._model_config is not None
                else ""
            ),
            prompt_version="selector-repair-v1",
            site=self.config.site,
            environment=self.config.environment,
            probe_run_id=self.probe_run_id,
            attempt_token=self.attempt_token,
            element_request_id=self.element_request_id,
            element_request_claim_token=self.element_request_claim_token,
            element_request_generation=self.element_request_generation,
            staged_result=(
                self._staged_element_result
                if self.element_request_id
                else None
            ),
        )
        try:
            self._require_owned(renew=True)
        except BaseException:
            if not self.element_request_id:
                self.store.cancel_validated_version(
                    version,
                    probe_run_id=self.probe_run_id,
                    attempt_token=self.attempt_token,
                )
            raise
        reconciliation = self.reconciler(self.store, self.registry)
        if self.element_request_id:
            completed = self.store.element_request_publication_is_complete(
                self.element_request_id,
                self.element_request_generation,
                version,
            )
            if not completed:
                raise RuntimeError(
                    "element request publication was not acknowledged"
                )
        else:
            self._require_owned(renew=True)
        stored = self.store.get_version(version)
        reconciled = (
            isinstance(reconciliation, Mapping)
            and reconciliation.get("version") == version
            and isinstance(stored, Mapping)
            and stored.get("status") == "published"
        )
        return {
            "version": version,
            "published": reconciled,
            "reconciled": reconciled,
        }

    def prepare_publication(
        self,
        candidate: Mapping[str, object],
        full_evidence: Mapping[str, object],
        repairs: object,
    ) -> None:
        self._staged_element_result = {
            "candidate": dict(candidate),
            "validation_evidence": dict(full_evidence),
            "repairs": repairs,
        }


def copy_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
        )
    )


__all__ = ["HealingRuntime"]
