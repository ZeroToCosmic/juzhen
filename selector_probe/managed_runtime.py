"""Runtime for validating and publishing manually managed elements."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
import copy
from datetime import UTC, datetime
import hashlib
import inspect
import json
from typing import Any
from urllib.parse import urlsplit

from browser_cdp import wait_for_cdp as default_wait_for_cdp

from .inventory import _safe_locator_syntax
from .registry import reconcile_registry
from .session import ProbeSessionManager
from .validator import validate_element


RUNTIME_STATUSES = frozenset({"draft", "healthy", "degraded", "validating"})
PUBLIC_LOCATOR_TYPES = frozenset({"css", "xpath"})
SAVED_LOCATOR_LIMIT = 6
PUBLISHED_LOCATOR_LIMIT = 5
_PAGE_STABILITY_SCRIPT = """
() => {
  const body = document.body;
  if (!body) {
    return {origin: location.origin, body_visible: false, interactive_count: 0};
  }
  const bodyStyle = getComputedStyle(body);
  const bodyRect = body.getBoundingClientRect();
  const bodyVisible = bodyStyle.display !== "none"
    && bodyStyle.visibility !== "hidden"
    && bodyRect.width > 0
    && bodyRect.height > 0;
  const selector = [
    "a[href]", "button", "input", "textarea", "select",
    "[role='button']", "[role='link']", "[role='textbox']",
    "[contenteditable='true']", "[tabindex]"
  ].join(",");
  let interactiveCount = 0;
  for (const node of document.querySelectorAll(selector)) {
    const style = getComputedStyle(node);
    const rect = node.getBoundingClientRect();
    if (style.display !== "none" && style.visibility !== "hidden"
        && rect.width > 0 && rect.height > 0) {
      interactiveCount += 1;
    }
  }
  return {
    origin: location.origin,
    body_visible: bodyVisible,
    interactive_count: interactiveCount,
  };
}
"""


class PageReadinessTimeout(RuntimeError):
    code = "page_readiness_timeout"

    def __init__(self) -> None:
        super().__init__(self.code)


async def _start_playwright() -> object:
    from playwright.async_api import async_playwright

    return await async_playwright().start()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _default_version_id(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("selector-%Y%m%dT%H%M%S%fZ")


def _locator_id(element_id: str, locator: Mapping[str, object]) -> str:
    material = "\0".join(
        (element_id, str(locator["type"]), str(locator["value"]))
    )
    return "locator-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


async def _await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _row_value(row: object, key: str, default: object = None) -> object:
    if isinstance(row, Mapping):
        return row.get(key, default)
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        return getattr(row, key, default)


def _normalized_locator(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping) or set(value) != {"type", "value"}:
        return None
    locator_type = value.get("type")
    locator_value = value.get("value")
    if (
        not isinstance(locator_type, str)
        or not isinstance(locator_value, str)
        or locator_type not in PUBLIC_LOCATOR_TYPES
        or locator_type != locator_type.strip()
        or locator_value != locator_value.strip()
        or not locator_value
        or not _safe_locator_syntax(locator_type, locator_value)
    ):
        return None
    return {"type": locator_type, "value": locator_value}


def _operation_steps(definition: Mapping[str, object]) -> tuple[dict[str, object], ...]:
    raw_steps = definition.get("operation_steps", [])
    if not isinstance(raw_steps, Sequence) or isinstance(
        raw_steps, (str, bytes, bytearray)
    ):
        raise ValueError("operation_steps are invalid")
    steps: list[dict[str, object]] = []
    for raw in raw_steps:
        if not isinstance(raw, Mapping):
            raise ValueError("operation_steps are invalid")
        locator = _normalized_locator(raw.get("locator"))
        sequence = raw.get("sequence")
        if (
            locator is None
            or isinstance(sequence, bool)
            or not isinstance(sequence, int)
            or sequence != len(steps) + 1
        ):
            raise ValueError("operation_steps are invalid")
        steps.append(
            {
                "sequence": sequence,
                "locator": locator,
                "frame_key": str(raw.get("frame_key") or "main"),
                "shadow": raw.get("shadow") is True,
                "shadow_key": str(raw.get("shadow_key") or "document"),
            }
        )
    return tuple(steps)


async def _frame_context(page: object, frame_key: str) -> object:
    custom = getattr(page, "resolve_frame_context", None)
    if callable(custom):
        resolved = await _await(custom(frame_key))
        if resolved is None:
            raise LookupError("frame unavailable")
        return resolved
    parts = frame_key.split("/")
    if not parts or parts[0] != "main":
        raise LookupError("frame unavailable")
    current = getattr(page, "main_frame", page)
    if callable(current):
        current = current()
    current = await _await(current)
    for part in parts[1:]:
        if not part.startswith("frame:") or not part[6:].isdigit():
            raise LookupError("frame unavailable")
        index = int(part[6:]) - 1
        children = getattr(current, "child_frames", None)
        if callable(children):
            children = children()
        children = await _await(children)
        if (
            isinstance(index, bool)
            or index < 0
            or not isinstance(children, Sequence)
            or index >= len(children)
        ):
            raise LookupError("frame unavailable")
        current = children[index]
    return current


async def _collection_matches(collection: object) -> list[object]:
    all_method = getattr(collection, "all", None)
    if callable(all_method):
        matches = await _await(all_method())
        if isinstance(matches, Sequence) and not isinstance(
            matches, (str, bytes, bytearray)
        ):
            return list(matches)
        raise TypeError("invalid locator collection")
    count = getattr(collection, "count", None)
    nth = getattr(collection, "nth", None)
    if not callable(count) or not callable(nth):
        raise TypeError("invalid locator collection")
    size = await _await(count())
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise TypeError("invalid locator count")
    return [nth(index) for index in range(size)]


async def _shadow_root_locator(
    frame: object,
    shadow_key: str,
) -> object | None:
    if shadow_key == "document":
        return None
    parts = shadow_key.split("/")
    if not parts or parts[0] != "document" or len(parts) < 2:
        raise LookupError("shadow root unavailable")
    current: object | None = None
    for host_selector in parts[1:]:
        if not _safe_locator_syntax("css", host_selector):
            raise LookupError("shadow root unavailable")
        owner = frame if current is None else current
        locator_factory = getattr(owner, "locator", None)
        if not callable(locator_factory):
            raise LookupError("shadow root unavailable")
        collection = locator_factory(host_selector)
        matches = await _collection_matches(collection)
        if len(matches) != 1:
            raise LookupError("shadow root unavailable")
        current = matches[0]
    return current


class _DeferredLocator:
    def __init__(self, adapter: "_ScopedPageAdapter", selector: str) -> None:
        self.adapter = adapter
        self.selector = selector

    async def all(self) -> list[object]:
        if self.selector.startswith("xpath=") and self.adapter.shadow_key != "document":
            raise TypeError("xpath is unavailable in shadow root")
        return await self.adapter._matches(self.selector)

    async def count(self) -> int:
        return len(await self.all())

    def nth(self, index: int) -> object:
        raise TypeError("use all for deferred locator")


class _ScopedPageAdapter:
    def __init__(self, page: object, frame_key: str, shadow_key: str) -> None:
        self.page = page
        self.frame_key = frame_key
        self.shadow_key = shadow_key

    async def _matches(self, selector: str) -> list[object]:
        frame = await _frame_context(self.page, self.frame_key)
        shadow_root = await _shadow_root_locator(frame, self.shadow_key)
        if shadow_root is None:
            if selector.startswith("xpath="):
                locator_factory = getattr(frame, "locator", None)
                if not callable(locator_factory):
                    raise TypeError("xpath query unavailable")
                return await _collection_matches(locator_factory(selector))
            query = getattr(frame, "query_selector_all", None)
            if not callable(query):
                raise TypeError("css query unavailable")
            matches = await _await(query(selector))
            if not isinstance(matches, Sequence) or isinstance(
                matches, (str, bytes, bytearray)
            ):
                raise TypeError("invalid css matches")
            return list(matches)
        if selector.startswith("xpath="):
            raise TypeError("xpath is unavailable in shadow root")
        locator_factory = getattr(shadow_root, "locator", None)
        if not callable(locator_factory):
            raise TypeError("shadow query unavailable")
        return await _collection_matches(locator_factory(selector))

    async def query_selector_all(self, selector: str) -> list[object]:
        return await self._matches(selector)

    def locator(self, selector: str) -> _DeferredLocator:
        return _DeferredLocator(self, selector)


def _context_adapter(page: object, value: Mapping[str, object]) -> _ScopedPageAdapter:
    frame_key = value.get("frame_key", "main")
    shadow_key = value.get("shadow_key", "document")
    if not isinstance(frame_key, str) or not frame_key:
        raise LookupError("frame unavailable")
    if not isinstance(shadow_key, str) or not shadow_key:
        raise LookupError("shadow root unavailable")
    if value.get("shadow") is not True:
        shadow_key = "document"
    return _ScopedPageAdapter(page, frame_key, shadow_key)


async def _query_step_target(page: object, locator: Mapping[str, str]) -> object | None:
    locator_type = locator["type"]
    value = locator["value"]
    if locator_type == "css":
        query = getattr(page, "query_selector_all", None)
        if not callable(query):
            return None
        matches = await _await(query(value))
        if not isinstance(matches, Sequence) or isinstance(
            matches, (str, bytes, bytearray)
        ):
            return None
        return matches[0] if len(matches) == 1 else None

    locator_factory = getattr(page, "locator", None)
    if not callable(locator_factory):
        return None
    collection = locator_factory("xpath=" + value)
    all_method = getattr(collection, "all", None)
    if callable(all_method):
        matches = await _await(all_method())
        if not isinstance(matches, Sequence) or isinstance(
            matches, (str, bytes, bytearray)
        ):
            return None
        return matches[0] if len(matches) == 1 else None
    count = getattr(collection, "count", None)
    nth = getattr(collection, "nth", None)
    if not callable(count) or not callable(nth) or await _await(count()) != 1:
        return None
    return nth(0)


async def _node_ready(node: object) -> bool:
    if isinstance(node, Mapping):
        return node.get("visible") is True and node.get("enabled") is True
    visible = getattr(node, "is_visible", None)
    enabled = getattr(node, "is_enabled", None)
    if not callable(visible) or not callable(enabled):
        return False
    return await _await(visible()) is True and await _await(enabled()) is True


async def _wait_for_step_target(
    page: object,
    locator: Mapping[str, str],
) -> object:
    while True:
        target = await _query_step_target(page, locator)
        if target is not None and await _node_ready(target):
            return target
        await asyncio.sleep(0.05)


async def _wait_page_ready(
    page: object,
    next_locator: Mapping[str, str] | None,
    timeout_seconds: float,
) -> None:
    custom = getattr(page, "wait_until_ready", None)
    if callable(custom):
        await asyncio.wait_for(
            _await(
                custom(
                    next_locator=copy.deepcopy(next_locator),
                    timeout_ms=int(timeout_seconds * 1000),
                )
            ),
            timeout=timeout_seconds,
        )
        return

    load_state = getattr(page, "wait_for_load_state", None)
    if callable(load_state):
        await asyncio.wait_for(
            _await(load_state("domcontentloaded", timeout=int(timeout_seconds * 1000))),
            timeout=timeout_seconds,
        )
    if next_locator is None:
        return
    wait_for_selector = getattr(page, "wait_for_selector", None)
    if not callable(wait_for_selector):
        return
    selector = next_locator["value"]
    if next_locator["type"] == "xpath":
        selector = "xpath=" + selector
    await asyncio.wait_for(
        _await(
            wait_for_selector(
                selector,
                state="attached",
                timeout=int(timeout_seconds * 1000),
            )
        ),
        timeout=timeout_seconds,
    )


async def _replay_steps(
    page: object,
    steps: Sequence[Mapping[str, object]],
    timeout_seconds: float,
) -> dict[str, object] | None:
    for index, step in enumerate(steps):
        locator = step["locator"]
        assert isinstance(locator, Mapping)

        async def perform_step() -> None:
            if step.get("shadow") is True and locator.get("type") == "xpath":
                raise LookupError
            adapter = _context_adapter(page, step)
            target = await _wait_for_step_target(adapter, locator)  # type: ignore[arg-type]
            scroll = getattr(target, "scroll_into_view_if_needed", None)
            click = getattr(target, "click", None)
            if not callable(scroll) or not callable(click):
                raise LookupError
            await _await(scroll())
            await _await(click())
            await _wait_page_ready(page, None, timeout_seconds)
            if index + 1 < len(steps):
                raw_next = steps[index + 1]
                next_locator = raw_next.get("locator")
                if not isinstance(next_locator, Mapping):
                    raise LookupError
                if (
                    raw_next.get("shadow") is True
                    and next_locator.get("type") == "xpath"
                ):
                    raise LookupError
                next_adapter = _context_adapter(page, raw_next)
                await _wait_for_step_target(
                    next_adapter,
                    next_locator,  # type: ignore[arg-type]
                )

        try:
            await asyncio.wait_for(perform_step(), timeout=timeout_seconds)
        except (asyncio.TimeoutError, LookupError, RuntimeError, TypeError, ValueError):
            return {
                "status": "failed",
                "failure_code": "recorded_step_unavailable",
                "failed_step": step.get("sequence", index + 1),
            }
    return None


class ManagedElementRuntime:
    def __init__(
        self,
        store: object,
        *,
        page_provider: Callable[[Mapping[str, object]], object] | None = None,
        validator_fn: Callable[
            [object, Mapping[str, object]],
            Awaitable[dict[str, object]] | dict[str, object],
        ] = validate_element,
        version_id_factory: Callable[[datetime], str] = _default_version_id,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        page_ready_timeout_seconds: float = 90.0,
    ) -> None:
        if page_ready_timeout_seconds <= 0:
            raise ValueError("page_ready_timeout_seconds must be positive")
        self.store = store
        self.page_provider = page_provider
        self.validator_fn = validator_fn
        self.version_id_factory = version_id_factory
        self.now = now
        self.page_ready_timeout_seconds = float(page_ready_timeout_seconds)

    def load_candidate(self) -> dict[str, object]:
        loader = getattr(self.store, "list_managed_element_rows", None)
        definition_loader = getattr(self.store, "manual_element_definition", None)
        if not callable(loader) or not callable(definition_loader):
            raise RuntimeError("managed element store is unavailable")

        elements: dict[str, dict[str, object]] = {}
        page = 1
        while True:
            rows, total, _revision = loader(
                page=page,
                page_size=100,
                search="",
                status="all",
                referenced="all",
            )
            for row in rows:
                status = str(_row_value(row, "status", ""))
                element_id = str(_row_value(row, "id", ""))
                if status not in RUNTIME_STATUSES or not element_id:
                    continue
                definition = definition_loader(element_id)
                if not isinstance(definition, Mapping):
                    continue
                elements[element_id] = {
                    "definition": copy.deepcopy(dict(definition)),
                    "status": status,
                    "revision": int(_row_value(row, "revision", 0)),
                    "display_name": str(_row_value(row, "display_name", "")),
                }
            if page * 100 >= int(total):
                break
            page += 1
        return {"elements": dict(sorted(elements.items()))}

    async def validate_candidate(
        self,
        candidate: object,
        *,
        page: object | None = None,
    ) -> dict[str, object]:
        elements = self._candidate_elements(candidate)
        grouped: dict[str, list[tuple[str, dict[str, object]]]] = {}
        results: dict[str, dict[str, object]] = {}
        for element_id, item in elements.items():
            definition = item["definition"]
            assert isinstance(definition, Mapping)
            try:
                steps = _operation_steps(definition)
            except ValueError:
                results[element_id] = {
                    "status": "failed",
                    "failure_code": "recorded_step_unavailable",
                    "failed_step": 1,
                }
                continue
            group_key = _canonical_json(
                {
                    "page_key": definition.get("page_key", ""),
                    "target_origin": definition.get("target_origin", ""),
                    "url_pattern": definition.get("url_pattern", ""),
                    "operation_steps": steps,
                }
            )
            grouped.setdefault(group_key, []).append((element_id, item))

        for group in grouped.values():
            first_definition = group[0][1]["definition"]
            assert isinstance(first_definition, Mapping)
            selected_page = page
            if selected_page is None and self.page_provider is not None:
                selected_page = await _await(self.page_provider(first_definition))
            if selected_page is None:
                for element_id, _item in group:
                    results[element_id] = {
                        "status": "failed",
                        "failure_code": "page_unavailable",
                    }
                continue

            step_failure = await _replay_steps(
                selected_page,
                _operation_steps(first_definition),
                self.page_ready_timeout_seconds,
            )
            if step_failure is not None:
                for element_id, _item in group:
                    results[element_id] = copy.deepcopy(step_failure)
                continue

            for element_id, item in group:
                definition = item["definition"]
                assert isinstance(definition, Mapping)
                try:
                    fingerprint = definition.get("fingerprint")
                    scoped_page = _context_adapter(
                        selected_page,
                        fingerprint if isinstance(fingerprint, Mapping) else {},
                    )
                    result = await _await(
                        self.validator_fn(scoped_page, definition)
                    )
                except Exception:
                    result = {
                        "status": "failed",
                        "failure_code": "selector_query_invalid",
                    }
                if not isinstance(result, Mapping):
                    result = {
                        "status": "failed",
                        "failure_code": "selector_query_invalid",
                    }
                results[element_id] = copy.deepcopy(dict(result))

        overall = "passed" if results and all(
            item.get("status") == "passed" for item in results.values()
        ) else "failed"
        return {"status": overall, "elements": results}

    def promote_saved_fallbacks(
        self,
        candidate: object,
        validation: Mapping[str, object],
    ) -> dict[str, object]:
        promoted = {"elements": copy.deepcopy(self._candidate_elements(candidate))}
        raw_results = validation.get("elements")
        if not isinstance(raw_results, Mapping):
            return promoted
        for element_id, item in promoted["elements"].items():
            result = raw_results.get(element_id)
            if not isinstance(result, Mapping) or result.get("status") != "passed":
                continue
            index = result.get("selected_locator_index")
            definition = item.get("definition")
            locators = definition.get("locators") if isinstance(definition, dict) else None
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(locators, list)
                or not 0 <= index < len(locators)
                or index == 0
            ):
                continue
            locators.insert(0, locators.pop(index))
        return promoted

    def prepare_publication(
        self,
        candidate: object,
        validation: Mapping[str, object],
    ) -> dict[str, object]:
        promoted = {"elements": copy.deepcopy(self._candidate_elements(candidate))}
        raw_results = validation.get("elements")
        if not isinstance(raw_results, Mapping):
            raise ValueError("validation elements are required")

        public_elements: dict[str, dict[str, object]] = {}
        for element_id, item in promoted["elements"].items():
            result = raw_results.get(element_id)
            if not isinstance(result, Mapping) or result.get("status") != "passed":
                raise ValueError("all candidate elements must pass validation")
            definition = item["definition"]
            assert isinstance(definition, Mapping)
            raw_locators = definition.get("locators")
            if (
                not isinstance(raw_locators, list)
                or not 1 <= len(raw_locators) <= SAVED_LOCATOR_LIMIT
            ):
                raise ValueError("saved locators are invalid for publication")
            locators: list[dict[str, object]] = []
            for raw_locator in raw_locators[:PUBLISHED_LOCATOR_LIMIT]:
                locator = _normalized_locator(raw_locator)
                if locator is None:
                    raise ValueError("only saved CSS/XPath locators may be published")
                locators.append(
                    {
                        "id": _locator_id(element_id, locator),
                        "type": locator["type"],
                        "value": locator["value"],
                        "enabled": True,
                    }
                )
            public_elements[element_id] = {"scope": "page", "locators": locators}

        if not public_elements:
            raise ValueError("candidate elements are empty")
        moment = self.now()
        if not isinstance(moment, datetime):
            raise TypeError("now must return datetime")
        version = self.version_id_factory(moment)
        if not isinstance(version, str) or not version:
            raise ValueError("version ID is invalid")
        ordered = dict(sorted(public_elements.items()))
        return {
            "version": version,
            "bundle_hash": _sha256(ordered),
            "elements": ordered,
        }

    @staticmethod
    def _candidate_elements(candidate: object) -> dict[str, dict[str, object]]:
        if not isinstance(candidate, Mapping) or set(candidate) != {"elements"}:
            raise ValueError("candidate is invalid")
        raw_elements = candidate.get("elements")
        if not isinstance(raw_elements, Mapping):
            raise ValueError("candidate is invalid")
        elements: dict[str, dict[str, object]] = {}
        for element_id, raw in raw_elements.items():
            if not isinstance(element_id, str) or not element_id or not isinstance(raw, Mapping):
                raise ValueError("candidate is invalid")
            definition = raw.get("definition")
            if not isinstance(definition, Mapping):
                raise ValueError("candidate is invalid")
            raw_locators = definition.get("locators")
            if (
                not isinstance(raw_locators, list)
                or not 1 <= len(raw_locators) <= SAVED_LOCATOR_LIMIT
                or any(_normalized_locator(locator) is None for locator in raw_locators)
            ):
                raise ValueError("candidate locators are invalid")
            elements[element_id] = {
                "definition": copy.deepcopy(dict(definition)),
                "status": str(raw.get("status", "")),
                "revision": int(raw.get("revision", 0)),
                "display_name": str(raw.get("display_name", "")),
            }
        return dict(sorted(elements.items()))


class ManagedProbeRuntime(ManagedElementRuntime):
    """Own AdsPower probe resources and run the two-profile validation matrix."""

    def __init__(
        self,
        *,
        config: object,
        settings: Mapping[str, object],
        store: object,
        registry: object,
        adspower_client: object,
        stop_event: object | None = None,
        lease_guard: Callable[..., object] | None = None,
        probe_run_id: int | None = None,
        attempt_token: str = "",
        progress_sink: Callable[[Mapping[str, object]], None] | None = None,
        reconciler: Callable[..., object] = reconcile_registry,
        session_manager_factory: Callable[..., object] = ProbeSessionManager,
        playwright_starter: Callable[[], object] = _start_playwright,
        wait_for_cdp: Callable[[str], object] = default_wait_for_cdp,
        validator_fn: Callable[
            [object, Mapping[str, object]],
            Awaitable[dict[str, object]] | dict[str, object],
        ] = validate_element,
        page_ready_timeout_seconds: float = 90.0,
        element_poll_interval_seconds: float = 0.25,
        page_stability_interval_seconds: float = 1.0,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(
            store,
            validator_fn=validator_fn,
            now=now,
            page_ready_timeout_seconds=page_ready_timeout_seconds,
        )
        if (probe_run_id is None) != (attempt_token == ""):
            raise ValueError("probe_run_id and attempt_token must be supplied together")
        self.config = config
        self.settings = dict(settings)
        self.registry = registry
        self.adspower_client = adspower_client
        self.stop_event = stop_event
        self.lease_guard = lease_guard
        self.probe_run_id = probe_run_id
        self.attempt_token = attempt_token
        self.progress_sink = progress_sink
        self.reconciler = reconciler
        self.session_manager_factory = session_manager_factory
        self.playwright_starter = playwright_starter
        self.wait_for_cdp = wait_for_cdp
        if element_poll_interval_seconds <= 0:
            raise ValueError("element_poll_interval_seconds must be positive")
        if page_stability_interval_seconds <= 0:
            raise ValueError("page_stability_interval_seconds must be positive")
        self.element_poll_interval_seconds = float(element_poll_interval_seconds)
        self.page_stability_interval_seconds = float(
            page_stability_interval_seconds
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_manager: object | None = None
        self._profile_handles: list[object] = []
        self._page_handles: list[object] = []
        self._playwright: object | None = None
        self._evidence_counter = 0

    def __enter__(self) -> "ManagedProbeRuntime":
        if self._loop is not None:
            raise RuntimeError("managed probe runtime is already open")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("managed probe runtime cannot nest an event loop")
        self._loop = asyncio.new_event_loop()
        try:
            self._run_sync(self._open_resources())
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

    def _run_sync(self, awaitable: Awaitable[Any], *, guard: bool = True) -> Any:
        if self._loop is None:
            raise RuntimeError("managed probe runtime is not open")

        async def guarded() -> Any:
            target = asyncio.ensure_future(awaitable)
            try:
                while not target.done():
                    await asyncio.wait({target}, timeout=0.05)
                    if guard and not target.done():
                        try:
                            self._require_owned()
                        except BaseException:
                            target.cancel()
                            await asyncio.gather(target, return_exceptions=True)
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
        if self._stopped():
            raise asyncio.CancelledError()
        if self.lease_guard is not None:
            self.lease_guard(renew=renew)

    def _stop_requested(self) -> bool:
        try:
            self._require_owned()
        except BaseException:
            return True
        return False

    def _progress(self, name: str, status: str, **details: object) -> None:
        if not callable(self.progress_sink):
            return
        try:
            self.progress_sink({"name": name, "status": status, **details})
        except Exception:
            return

    def _session_progress(self, event: object) -> None:
        if not callable(self.progress_sink) or not isinstance(event, Mapping):
            return
        try:
            self.progress_sink(dict(event))
        except Exception:
            return

    def record_business_stage(
        self,
        name: str,
        status: str,
        **details: object,
    ) -> None:
        self._progress(name, status, **details)

    def _profile_ids(self) -> tuple[str, ...]:
        raw = getattr(self.config, "test_profile_ids", None)
        if (
            not isinstance(raw, Sequence)
            or isinstance(raw, (str, bytes, bytearray))
            or len(raw) < 2
            or any(not isinstance(item, str) or not item for item in raw)
        ):
            raise ValueError("at least two test profiles are required")
        return tuple(raw)

    def _target_url(self) -> str:
        selected = getattr(self.config, "target_url", "")
        if not isinstance(selected, str) or not selected:
            selected = self.settings.get("target_url", "")
        if not isinstance(selected, str) or not selected:
            probe_settings = self.settings.get("selector_probe")
            if isinstance(probe_settings, Mapping):
                selected = probe_settings.get("target_url", "")
        if not isinstance(selected, str) or not selected:
            raise ValueError("target_url is required")
        return selected

    async def _wait_for_page_stability(self, page: object) -> None:
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            raise RuntimeError("probe page cannot evaluate readiness")
        target = urlsplit(self._target_url())
        expected_origin = f"{target.scheme.lower()}://{target.netloc.lower()}"
        previous_count: int | None = None
        while True:
            sample = await _await(evaluate(_PAGE_STABILITY_SCRIPT))
            origin = sample.get("origin") if isinstance(sample, Mapping) else None
            body_visible = (
                sample.get("body_visible") if isinstance(sample, Mapping) else None
            )
            count = (
                sample.get("interactive_count")
                if isinstance(sample, Mapping)
                else None
            )
            stable_sample = (
                isinstance(origin, str)
                and origin.lower() == expected_origin
                and body_visible is True
                and isinstance(count, int)
                and not isinstance(count, bool)
                and count > 0
            )
            if stable_sample:
                if previous_count == count:
                    return
                previous_count = count
            else:
                previous_count = None
            await asyncio.sleep(self.page_stability_interval_seconds)

    async def _page_ready(
        self,
        page: object,
        *,
        reload_page: bool,
        profile_mask: str = "",
    ) -> None:
        timeout_ms = int(self.page_ready_timeout_seconds * 1000)
        self._progress(
            "page_readiness", "running", profile_mask=profile_mask
        )

        async def navigate_and_wait() -> None:
            if reload_page:
                reload_method = getattr(page, "reload", None)
                if not callable(reload_method):
                    raise RuntimeError("probe page cannot reload")
                await _await(
                    reload_method(
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                )
            else:
                goto = getattr(page, "goto", None)
                if not callable(goto):
                    raise RuntimeError("probe page cannot navigate")
                await _await(
                    goto(
                        self._target_url(),
                        wait_until="domcontentloaded",
                        timeout=timeout_ms,
                    )
                )
            load_state = getattr(page, "wait_for_load_state", None)
            if callable(load_state):
                await _await(
                    load_state("domcontentloaded", timeout=timeout_ms)
                )
            await self._wait_for_page_stability(page)

        try:
            await asyncio.wait_for(
                navigate_and_wait(),
                timeout=self.page_ready_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._progress(
                "page_readiness",
                "failed",
                profile_mask=profile_mask,
                failure_code="page_readiness_timeout",
            )
            raise PageReadinessTimeout() from None
        except BaseException as error:
            self._progress(
                "page_readiness",
                "failed",
                profile_mask=profile_mask,
                failure_code=str(
                    getattr(error, "code", "probe_navigation_failed")
                ),
            )
            raise
        self._progress(
            "page_readiness", "passed", profile_mask=profile_mask
        )

    async def _open_resources(self) -> None:
        self._require_owned(renew=True)
        self._progress("prepare_environment", "running")
        profile_ids = self._profile_ids()
        manager_kwargs = {
            "allowed_profile_ids": profile_ids,
            "wait_for_cdp": self.wait_for_cdp,
            "stop_requested": self._stop_requested,
            "progress_sink": self._session_progress,
        }
        try:
            parameters = inspect.signature(self.session_manager_factory).parameters
        except (TypeError, ValueError):
            parameters = {}
        if parameters and not any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        ):
            manager_kwargs = {
                key: value for key, value in manager_kwargs.items() if key in parameters
            }
        self._session_manager = self.session_manager_factory(
            self.adspower_client,
            **manager_kwargs,
        )
        self._profile_handles = list(
            self._session_manager.open_profiles(profile_ids)
        )
        if len(self._profile_handles) < 2:
            raise RuntimeError("at least two probe profiles must open")
        self._playwright = await _await(self.playwright_starter())
        for profile in self._profile_handles:
            handle = await _await(
                self._session_manager.open_probe_page(self._playwright, profile)
            )
            self._page_handles.append(handle)
        self._progress("prepare_environment", "passed")
        self._progress("open_and_replay", "running")
        navigation_tasks = [
            asyncio.create_task(
                self._page_ready(
                    handle.page,
                    reload_page=False,
                    profile_mask=str(
                        getattr(handle.profile, "profile_mask", "")
                    ),
                )
            )
            for handle in self._page_handles
        ]
        try:
            await asyncio.gather(*navigation_tasks)
        except BaseException as error:
            for task in navigation_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*navigation_tasks, return_exceptions=True)
            self._progress(
                "open_and_replay",
                "failed",
                failure_code=str(
                    getattr(error, "code", "probe_navigation_failed")
                ),
            )
            raise
        self._progress("open_and_replay", "passed")
        self._require_owned()

    def close(self) -> None:
        if self._loop is None:
            return
        cleanup_failed = False
        try:
            if self._session_manager is not None and self._page_handles:
                try:
                    results = self._run_sync(
                        self._session_manager.close_owned_pages(self._page_handles),
                        guard=False,
                    )
                    cleanup_failed = cleanup_failed or any(
                        isinstance(item, Mapping) and item.get("ok") is not True
                        for item in results
                    )
                except BaseException:
                    cleanup_failed = True
            if self._playwright is not None:
                try:
                    stop = getattr(self._playwright, "stop", None)
                    if callable(stop):
                        self._run_sync(_await(stop()), guard=False)
                except BaseException:
                    cleanup_failed = True
            if self._session_manager is not None and self._profile_handles:
                try:
                    results = self._session_manager.stop_owned_profiles(
                        self._profile_handles
                    )
                    cleanup_failed = cleanup_failed or any(
                        isinstance(item, Mapping) and item.get("ok") is not True
                        for item in results
                    )
                except BaseException:
                    cleanup_failed = True
        finally:
            self._page_handles = []
            self._profile_handles = []
            self._playwright = None
            self._session_manager = None
            loop = self._loop
            self._loop = None
            loop.close()
        if cleanup_failed:
            raise RuntimeError("managed_probe_cleanup_failed")

    def validate_matrix(
        self,
        candidate: object,
        max_attempts: int = 3,
    ) -> dict[str, object]:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= 3
        ):
            raise ValueError("max_attempts must be between 1 and 3")
        return self._run_sync(self._validate_matrix(candidate, max_attempts))

    async def _validate_matrix(
        self,
        candidate: object,
        max_attempts: int,
    ) -> dict[str, object]:
        elements = self._candidate_elements(candidate)
        if not elements or len(self._page_handles) < 2:
            raise RuntimeError("managed probe resources are not open")
        self._progress("validate_elements", "running")
        profile_results: list[dict[str, object]] = []
        observations: dict[str, list[dict[str, object]]] = {
            element_id: [] for element_id in elements
        }
        exhausted: dict[str, dict[str, object]] = {}
        for round_number in (1, 2):
            for handle in self._page_handles:
                self._require_owned()
                page = handle.page
                profile_mask = str(
                    getattr(handle.profile, "profile_mask", "***")
                )
                if round_number == 2:
                    await self._page_ready(
                        page,
                        reload_page=True,
                        profile_mask=profile_mask,
                    )
                round_elements: dict[str, dict[str, object]] = {}
                for element_id, item in elements.items():
                    if element_id in exhausted:
                        result = copy.deepcopy(exhausted[element_id])
                        round_elements[element_id] = result
                        observations[element_id].append(result)
                        continue
                    result: dict[str, object] = {
                        "status": "failed",
                        "failure_code": "selector_query_invalid",
                    }
                    for attempt in range(1, max_attempts + 1):
                        if attempt > 1:
                            await self._page_ready(
                                page,
                                reload_page=True,
                                profile_mask=profile_mask,
                            )
                        result = await self._validate_element_attempt(
                            page,
                            item,
                        )
                        result["attempts"] = attempt
                        if result.get("status") == "passed":
                            break
                    if result.get("status") != "passed":
                        exhausted[element_id] = copy.deepcopy(result)
                    round_elements[element_id] = result
                    observations[element_id].append(result)
                self._evidence_counter += 1
                marker = {
                    "counter": self._evidence_counter,
                    "profile_mask": profile_mask,
                    "round": round_number,
                    "elements": round_elements,
                }
                profile_results.append(
                    {
                        "profile_mask": profile_mask,
                        "round_number": round_number,
                        "reset_evidence_hash": _sha256({"reset": marker}),
                        "snapshot_hash": _sha256({"snapshot": marker}),
                        "page_generation": _sha256({"generation": marker}),
                        "elements": round_elements,
                    }
                )

        element_results: dict[str, dict[str, object]] = {}
        for element_id, results in observations.items():
            indices = [
                result.get("selected_locator_index")
                for result in results
                if result.get("status") == "passed"
            ]
            passed = len(results) == len(self._page_handles) * 2 and all(
                result.get("status") == "passed" for result in results
            )
            consistent = passed and len(set(indices)) == 1
            element_results[element_id] = {
                "status": "passed" if consistent else "failed",
                "failure_code": "" if consistent else (
                    "selector_inconsistent" if passed else next(
                        (
                            str(result.get("failure_code") or "selector_query_invalid")
                            for result in results
                            if result.get("status") != "passed"
                        ),
                        "selector_query_invalid",
                    )
                ),
                "selected_locator_index": indices[0] if consistent else None,
                "attempt_count": max(
                    (
                        int(result.get("attempts", 1))
                        for result in results
                        if isinstance(result.get("attempts", 1), int)
                        and not isinstance(result.get("attempts", 1), bool)
                    ),
                    default=1,
                ),
                "profile_results": [
                    {
                        "profile_mask": row["profile_mask"],
                        "round_number": row["round_number"],
                        "status": row["elements"][element_id].get("status", "failed"),
                        "failure_code": row["elements"][element_id].get("failure_code", ""),
                        "selected_locator_index": row["elements"][element_id].get(
                            "selected_locator_index"
                        ),
                    }
                    for row in profile_results
                    if element_id in row["elements"]
                ],
            }
        overall_passed = all(
            result["status"] == "passed" for result in element_results.values()
        )
        profiles_passed = len(self._page_handles) if overall_passed else 0
        rounds_passed = 2 if overall_passed else 0
        validations: list[dict[str, object]] = []
        for row in profile_results:
            aliases: dict[str, dict[str, str]] = {}
            for element_id, item in elements.items():
                checked = row["elements"][element_id]
                selected_index = checked.get("selected_locator_index")
                definition = item["definition"]
                raw_locators = definition.get("locators") if isinstance(definition, Mapping) else None
                candidate_id = "unavailable"
                if (
                    checked.get("status") == "passed"
                    and isinstance(selected_index, int)
                    and not isinstance(selected_index, bool)
                    and isinstance(raw_locators, list)
                    and 0 <= selected_index < len(raw_locators)
                    and isinstance(raw_locators[selected_index], Mapping)
                ):
                    candidate_id = _locator_id(
                        element_id,
                        raw_locators[selected_index],
                    )
                aliases[element_id] = {
                    "status": "ok" if candidate_id != "unavailable" else "failed",
                    "candidate_id": candidate_id,
                }
            validations.append(
                {
                    "profile_mask": row["profile_mask"],
                    "round_number": row["round_number"],
                    "reset_evidence_hash": row["reset_evidence_hash"],
                    "snapshot_hash": row["snapshot_hash"],
                    "page_generation": row["page_generation"],
                    "aliases": aliases,
                }
            )
        result = {
            "status": "passed" if overall_passed else "failed",
            "consistent": overall_passed,
            "elements": element_results,
            "profile_results": profile_results,
            "profiles_passed": profiles_passed,
            "rounds_passed": rounds_passed,
            "validations": validations,
        }
        self._progress(
            "validate_elements",
            "passed" if overall_passed else "failed",
        )
        return result

    async def _validate_element_attempt(
        self,
        page: object,
        item: Mapping[str, object],
    ) -> dict[str, object]:
        definition = item.get("definition")
        if not isinstance(definition, Mapping):
            return {
                "status": "failed",
                "failure_code": "selector_query_invalid",
            }
        try:
            steps = _operation_steps(definition)
        except ValueError:
            return {
                "status": "failed",
                "failure_code": "recorded_step_unavailable",
                "failed_step": 1,
            }
        last_result: dict[str, object] = {
            "status": "failed",
            "failure_code": "selector_zero_match",
        }
        retryable_readiness = {
            "selector_zero_match",
            "selector_ambiguous",
            "selector_hidden",
            "selector_disabled",
            "selector_hit_test_failed",
        }

        async def prepare_and_poll() -> dict[str, object]:
            nonlocal last_result
            step_failure = await _replay_steps(
                page,
                steps,
                self.page_ready_timeout_seconds,
            )
            if step_failure is not None:
                return step_failure
            fingerprint = definition.get("fingerprint")
            scoped_page = _context_adapter(
                page,
                fingerprint if isinstance(fingerprint, Mapping) else {},
            )
            while True:
                try:
                    raw = await _await(self.validator_fn(scoped_page, definition))
                except Exception:
                    raw = {
                        "status": "failed",
                        "failure_code": "selector_query_invalid",
                    }
                if not isinstance(raw, Mapping):
                    raw = {
                        "status": "failed",
                        "failure_code": "selector_query_invalid",
                    }
                last_result = copy.deepcopy(dict(raw))
                if last_result.get("status") == "passed":
                    return last_result
                if last_result.get("failure_code") not in retryable_readiness:
                    return last_result
                await asyncio.sleep(self.element_poll_interval_seconds)

        try:
            return await asyncio.wait_for(
                prepare_and_poll(),
                timeout=self.page_ready_timeout_seconds,
            )
        except asyncio.TimeoutError:
            return last_result

    def _publication_evidence(
        self,
        bundle: Mapping[str, object],
        validation: Mapping[str, object],
    ) -> dict[str, object]:
        if validation.get("status") != "passed":
            raise ValueError("validation must pass before publication")
        elements = bundle.get("elements")
        rows = validation.get("validations")
        if not isinstance(elements, Mapping) or not isinstance(rows, list):
            raise ValueError("publication validation is invalid")
        expected_aliases: dict[str, dict[str, str]] = {}
        for element_id, definition in elements.items():
            locators = definition.get("locators") if isinstance(definition, Mapping) else None
            if not isinstance(locators, list) or not locators:
                raise ValueError("publication bundle is invalid")
            candidate_id = locators[0].get("id") if isinstance(locators[0], Mapping) else None
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError("publication bundle is invalid")
            expected_aliases[str(element_id)] = {
                "status": "ok",
                "candidate_id": candidate_id,
            }
        validations = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("publication validation is invalid")
            aliases = row.get("aliases")
            if aliases != expected_aliases:
                raise ValueError(
                    "publication candidate IDs do not match validated locators"
                )
            validations.append(
                {
                    "profile_mask": row.get("profile_mask"),
                    "round_number": row.get("round_number"),
                    "reset_evidence_hash": row.get("reset_evidence_hash"),
                    "snapshot_hash": row.get("snapshot_hash"),
                    "page_generation": row.get("page_generation"),
                    "aliases": copy.deepcopy(expected_aliases),
                }
            )
        return {
            "status": "passed",
            "bundle_hash": bundle.get("bundle_hash"),
            "profiles_passed": validation.get("profiles_passed"),
            "rounds_passed": 2,
            "validations": validations,
        }

    def store_and_publish(
        self,
        bundle: object,
        validation: Mapping[str, object],
    ) -> dict[str, object]:
        self._require_owned(renew=True)
        if not isinstance(bundle, Mapping):
            raise ValueError("bundle is invalid")
        canonical = {
            "bundle_hash": bundle.get("bundle_hash"),
            "elements": copy.deepcopy(bundle.get("elements")),
        }
        evidence = self._publication_evidence(canonical, validation)
        active = self.registry.get_active()
        base_version = (
            str(active.get("version") or "") if isinstance(active, Mapping) else ""
        )
        version = self.store.store_validated_version(
            bundle=canonical,
            evidence=evidence,
            base_version_id=base_version,
            model_id="",
            prompt_version="",
            site=str(getattr(self.config, "site", "tiktok")),
            environment=str(getattr(self.config, "environment", "production")),
            probe_run_id=self.probe_run_id,
            attempt_token=self.attempt_token,
        )
        self._require_owned(renew=True)
        reconciliation = self.reconciler(self.store, self.registry)
        self._require_owned(renew=True)
        stored = self.store.get_version(version)
        if (
            not isinstance(reconciliation, Mapping)
            or reconciliation.get("version") != version
            or not isinstance(stored, Mapping)
            or stored.get("status") != "published"
        ):
            raise RuntimeError("selector_publish_failed")
        return {
            "version": version,
            "published": True,
            "reconciled": True,
        }

    def capture_failure_screenshot(
        self,
        *,
        failed_aliases: object = (),
        target_path: object,
        evidence_root: object,
        regions: object = (),
        page_index: int = 0,
    ) -> object:
        if not isinstance(failed_aliases, Sequence) or isinstance(
            failed_aliases, (str, bytes, bytearray)
        ):
            raise ValueError("failed_aliases is invalid")
        if not 0 <= page_index < len(self._page_handles):
            raise ValueError("page_index is invalid")
        from .redaction import capture_redacted_screenshot

        return self._run_sync(
            capture_redacted_screenshot(
                self._page_handles[page_index].page,
                regions,
                target_path,
                evidence_root=evidence_root,
            )
        )


__all__ = [
    "ManagedElementRuntime",
    "ManagedProbeRuntime",
    "PUBLISHED_LOCATOR_LIMIT",
    "PUBLIC_LOCATOR_TYPES",
    "RUNTIME_STATUSES",
    "SAVED_LOCATOR_LIMIT",
]
