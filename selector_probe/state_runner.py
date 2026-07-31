from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from browser_element_resolver import (
    LocatorResolutionError,
    resolve_element,
    resolve_scope,
)
from browser_element_schema import normalize_element_definitions
from selector_probe.readiness import (
    ReadinessError,
    ReadinessToken,
    wait_for_semantic_readiness,
)


ALLOWED_ACTIONS = {
    "navigate",
    "reload",
    "wait_ready",
    "bounded_scroll",
    "open_comment_panel",
    "close_comment_panel",
}

FORBIDDEN_ACTIONS = {
    "keyboard_input",
    "submit",
    "like",
    "follow",
    "publish",
    "account_update",
}

SUPPORTED_STATES = {
    "feed_ready",
    "comment_panel_open",
    "comment_panel_closed",
}

_ACTION_FIELDS = {
    "navigate": {"type"},
    "reload": {"type"},
    "wait_ready": {"type"},
    "bounded_scroll": {"type", "steps", "delta_y"},
    "open_comment_panel": {"type"},
    "close_comment_panel": {"type"},
}

_BLOCK_MARKERS = (
    '[data-e2e*="captcha" i]',
    'iframe[src*="captcha" i]',
    '[id*="captcha" i]',
    '[data-e2e="login-modal"]',
    '[data-e2e="login-container"]',
)
_SKELETON_MARKERS = (
    '[data-e2e*="skeleton" i]',
    '[class*="skeleton" i]',
    '[aria-busy="true"]',
)
_COMMENT_PANEL_LOADING_MARKERS = (
    '[data-e2e*="skeleton" i]',
    '[class*="skeleton" i]',
    '[data-e2e*="loading" i]',
    '[data-e2e*="spinner" i]',
    '[class*="spinner" i]',
    '[role="progressbar"]',
    '[aria-busy="true"]',
)
_COMMENT_PANEL_SHELL_SELECTOR = (
    'section:has([data-e2e="comment-input"]), '
    'section:has([data-e2e="comment-post"]), '
    'section:has([data-e2e*="comment-list" i]), '
    '[role="dialog"]:has([data-e2e*="comment" i])'
)
_COMMENT_PANEL_STABLE_SAMPLES = 3
_COMMENT_INPUT_SELECTOR = '[data-e2e="comment-input"]'
_COMMENT_TEXTBOX_SELECTOR = (
    'textarea[data-e2e="comment-input"], '
    'input[data-e2e="comment-input"]:not([type="hidden"]), '
    '[data-e2e="comment-input"][role="textbox"], '
    '[data-e2e="comment-input"][contenteditable="true"], '
    '[data-e2e="comment-input"] [contenteditable="true"], '
    '[data-e2e="comment-input"] textarea, '
    '[data-e2e="comment-input"] input:not([type="hidden"]), '
    '[data-e2e="comment-input"] [role="textbox"]'
)
_COMMENT_SUBMIT_SELECTOR = '[data-e2e="comment-post"]'


class ProbeSafetyError(RuntimeError):
    def __init__(self, code: str, action: str):
        self.code = code
        self.action = action
        super().__init__(f"{code}: {action}")


ReadinessCheck = Callable[[Any], Awaitable[dict]]
PanelReadinessCheck = Callable[[Any], Awaitable[dict]]
ElementResolver = Callable[[Any, str, dict], Awaitable[Any]]
ScopeResolver = Callable[[Any, str], Awaitable[tuple[Any, dict]]]
SleepFn = Callable[[float], Awaitable[None]]
MonotonicFn = Callable[[], float]


class ProbeStateRunner:
    def __init__(
        self,
        *,
        target_url: str,
        readiness_check: ReadinessCheck | None = None,
        element_resolver: ElementResolver = resolve_element,
        scope_resolver: ScopeResolver = resolve_scope,
        comment_entry_alias: str = "评论入口",
        comment_close_alias: str | None = None,
        readiness_timeout_ms: int = 60_000,
        readiness_poll_interval_seconds: float = 1.0,
        panel_readiness_check: PanelReadinessCheck | None = None,
        comment_readiness_timeout_seconds: float = 60.0,
        comment_readiness_poll_interval_seconds: float = 2.0,
        panel_timeout_seconds: float = 15.0,
        poll_interval_seconds: float = 0.25,
        max_skeleton_nodes: int = 100,
        max_scroll_steps: int = 3,
        max_scroll_delta: int = 1_200,
        sleep_fn: SleepFn = asyncio.sleep,
        monotonic_fn: MonotonicFn = time.monotonic,
    ):
        parsed = urlsplit(target_url)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("target_url must have an HTTPS origin")
        if max_scroll_steps < 1 or max_scroll_delta < 1:
            raise ValueError("scroll limits must be positive")
        if readiness_timeout_ms < 1:
            raise ValueError("readiness_timeout_ms must be positive")
        if (
            panel_timeout_seconds <= 0
            or poll_interval_seconds <= 0
            or readiness_poll_interval_seconds <= 0
            or comment_readiness_timeout_seconds <= 0
            or comment_readiness_poll_interval_seconds <= 0
        ):
            raise ValueError("polling timeouts must be positive")
        if max_skeleton_nodes < 1:
            raise ValueError("max_skeleton_nodes must be positive")

        self.target_url = target_url
        self.expected_origin = self._origin_from_url(target_url)
        self._requires_readiness_token = readiness_check is None
        self.readiness_check = readiness_check or self._default_readiness_check
        self.element_resolver = element_resolver
        self.scope_resolver = scope_resolver
        self.comment_entry_alias = comment_entry_alias
        self.comment_close_alias = comment_close_alias
        self.readiness_timeout_ms = readiness_timeout_ms
        self.readiness_poll_interval_seconds = (
            readiness_poll_interval_seconds
        )
        self.panel_readiness_check = (
            panel_readiness_check or self._comment_panel_readiness_sample
        )
        self.comment_readiness_timeout_seconds = (
            comment_readiness_timeout_seconds
        )
        self.comment_readiness_poll_interval_seconds = (
            comment_readiness_poll_interval_seconds
        )
        self.panel_timeout_seconds = panel_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_skeleton_nodes = max_skeleton_nodes
        self.max_scroll_steps = max_scroll_steps
        self.max_scroll_delta = max_scroll_delta
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self.current_state: str | None = None
        self._panel_sampler_poisoned = False

    async def ensure_state(
        self,
        page: Any,
        state: str,
        elements: dict | None,
        *,
        comment_entry_override: dict | None = None,
        initial_action: str = "navigate",
    ) -> dict:
        selected = dict(elements or {})
        if state not in SUPPORTED_STATES:
            raise ProbeSafetyError("probe_state_unsupported", str(state))
        if comment_entry_override is not None:
            if state != "comment_panel_open":
                raise ProbeSafetyError(
                    "probe_override_forbidden",
                    str(state),
                )
            try:
                normalized = normalize_element_definitions(
                    {
                        self.comment_entry_alias: (
                            comment_entry_override
                        )
                    }
                )
            except (TypeError, ValueError):
                raise ProbeSafetyError(
                    "probe_override_invalid",
                    "open_comment_panel",
                ) from None
            definition = normalized[self.comment_entry_alias]
            if definition["scope"] != "active_video":
                raise ProbeSafetyError(
                    "probe_override_invalid",
                    "open_comment_panel",
                )
            selected[self.comment_entry_alias] = definition

        if state == "feed_ready":
            if initial_action not in {"navigate", "reload"}:
                raise ProbeSafetyError(
                    "probe_action_invalid",
                    str(initial_action),
                )
            await self.dispatch(page, {"type": initial_action}, selected)
            return await self.dispatch(page, {"type": "wait_ready"}, selected)

        if state == "comment_panel_open":
            if self.current_state is None:
                await self.ensure_state(page, "feed_ready", selected)
            if self.current_state == "comment_panel_open":
                await self._require_safe_origin(page, "open_comment_panel")
                readiness = await self._wait_for_comment_panel_ready(page)
                return {
                    "state": "comment_panel_open",
                    "clicked": False,
                    "panel_visible": True,
                    "stable_samples": readiness["stable_samples"],
                    "required_samples": readiness["required_samples"],
                    "fingerprint_hash": readiness["fingerprint_hash"],
                }
            if self.current_state not in {"feed_ready", "comment_panel_closed"}:
                raise ProbeSafetyError(
                    "probe_transition_forbidden",
                    "open_comment_panel",
                )
            if await self._visible_panel_locator(page) is not None:
                readiness = await self._wait_for_comment_panel_ready(page)
                self.current_state = "comment_panel_open"
                return {
                    "state": "comment_panel_open",
                    "clicked": False,
                    "panel_visible": True,
                    "stable_samples": readiness["stable_samples"],
                    "required_samples": readiness["required_samples"],
                    "fingerprint_hash": readiness["fingerprint_hash"],
                }
            return await self.dispatch(
                page,
                {"type": "open_comment_panel"},
                selected,
            )

        if self.current_state != "comment_panel_open":
            raise ProbeSafetyError(
                "probe_transition_forbidden",
                "close_comment_panel",
            )
        return await self.dispatch(
            page,
            {"type": "close_comment_panel"},
            selected,
        )

    async def dispatch(
        self,
        page: Any,
        action: dict,
        elements: dict | None = None,
    ) -> dict:
        elements = elements or {}
        action_type = action.get("type") if isinstance(action, dict) else None
        if not isinstance(action_type, str) or action_type not in ALLOWED_ACTIONS:
            raise ProbeSafetyError(
                "probe_action_forbidden",
                str(action_type or ""),
            )
        if set(action) - _ACTION_FIELDS[action_type]:
            raise ProbeSafetyError("probe_action_invalid", action_type)

        if action_type == "navigate":
            try:
                await page.goto(
                    self.target_url,
                    wait_until="commit",
                    timeout=30_000,
                )
            except Exception as error:
                raise ProbeSafetyError(
                    (
                        "probe_navigation_timeout"
                        if error.__class__.__name__ == "TimeoutError"
                        else "probe_navigation_failed"
                    ),
                    "navigate",
                ) from None
            self.current_state = None
            return {"state": None, "navigated_to": self.target_url}

        if action_type == "wait_ready":
            return await self._wait_ready(page)

        await self._require_safe_origin(page, action_type)

        if action_type == "reload":
            try:
                await page.reload(
                    wait_until="commit",
                    timeout=30_000,
                )
            except Exception as error:
                raise ProbeSafetyError(
                    (
                        "probe_navigation_timeout"
                        if error.__class__.__name__ == "TimeoutError"
                        else "probe_navigation_failed"
                    ),
                    "reload",
                ) from None
            self.current_state = None
            return {"state": None, "reloaded": True}
        if action_type == "bounded_scroll":
            return await self._bounded_scroll(page, action)
        if action_type == "open_comment_panel":
            return await self._open_comment_panel(page, elements)
        return await self._close_comment_panel(page, elements)

    async def _wait_ready(self, page: Any) -> dict:
        raw = await self.readiness_check(page)
        if not isinstance(raw, dict):
            raise ProbeSafetyError("probe_readiness_invalid", "wait_ready")

        page_origin = self._origin_from_url(getattr(page, "url", ""))
        reported_origin = self._origin_from_url(raw.get("origin", ""))
        origin = page_origin
        title_or_root = bool(
            raw.get("title_or_root", raw.get("ready", False))
        )
        blocked_marker = raw.get("blocked_marker")
        skeleton_timed_out = bool(raw.get("skeleton_timed_out", False))
        ready = bool(raw.get("ready", False))
        if self._requires_readiness_token and not isinstance(
            raw.get("readiness_token"),
            ReadinessToken,
        ):
            raise ProbeSafetyError(
                "probe_readiness_invalid",
                "wait_ready",
            )
        evidence = {
            "ready": ready,
            "expected_origin": self.expected_origin,
            "origin": origin,
            "origin_ok": (
                origin == self.expected_origin
                and (
                    not reported_origin
                    or reported_origin == self.expected_origin
                )
            ),
            "title_or_root": title_or_root,
            "blocked_marker": blocked_marker,
            "skeleton_timed_out": skeleton_timed_out,
            "state": "feed_ready",
        }

        if not evidence["origin_ok"]:
            raise ProbeSafetyError("probe_origin_mismatch", "wait_ready")
        if blocked_marker:
            raise ProbeSafetyError("probe_page_blocked", "wait_ready")
        if skeleton_timed_out:
            raise ProbeSafetyError("probe_readiness_timeout", "wait_ready")
        if not title_or_root or not ready:
            raise ProbeSafetyError("probe_page_not_ready", "wait_ready")

        self.current_state = "feed_ready"
        return evidence

    async def _default_readiness_check(self, page: Any) -> dict:
        try:
            _token, evidence = await wait_for_semantic_readiness(
                page,
                expected_origin=self.expected_origin,
                timeout_seconds=self.readiness_timeout_ms / 1_000,
                poll_interval_seconds=(
                    self.readiness_poll_interval_seconds
                ),
                sleep_fn=self.sleep_fn,
                monotonic_fn=self.monotonic_fn,
            )
            return evidence
        except ReadinessError as error:
            raise ProbeSafetyError(error.code, "wait_ready") from None

    async def _wait_for_skeletons_hidden(self, page: Any) -> bool:
        deadline = (
            self.monotonic_fn() + self.readiness_timeout_ms / 1_000
        )
        selector = ", ".join(_SKELETON_MARKERS)
        while True:
            skeletons = page.locator(selector)
            count = await skeletons.count()
            if count > self.max_skeleton_nodes:
                return True

            visible = False
            for index in range(count):
                try:
                    if await skeletons.nth(index).is_visible():
                        visible = True
                except Exception:
                    visible = True
            if not visible:
                return False
            if self.monotonic_fn() >= deadline:
                return True
            await self.sleep_fn(self.poll_interval_seconds)

    async def _first_visible_marker(
        self,
        page: Any,
        selectors: tuple[str, ...],
    ) -> str | None:
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 10)):
                if await locator.nth(index).is_visible():
                    return selector
        return None

    async def _bounded_scroll(self, page: Any, action: dict) -> dict:
        if self.current_state not in {"feed_ready", "comment_panel_closed"}:
            raise ProbeSafetyError(
                "probe_transition_forbidden",
                "bounded_scroll",
            )
        steps = action.get("steps", 1)
        delta_y = action.get("delta_y", self.max_scroll_delta)
        valid_steps = (
            isinstance(steps, int)
            and not isinstance(steps, bool)
            and 1 <= steps <= self.max_scroll_steps
        )
        valid_delta = (
            isinstance(delta_y, int)
            and not isinstance(delta_y, bool)
            and 0 < abs(delta_y) <= self.max_scroll_delta
        )
        if not valid_steps or not valid_delta:
            raise ProbeSafetyError(
                "probe_action_invalid",
                "bounded_scroll",
            )
        for _ in range(steps):
            await page.mouse.wheel(0, delta_y)
        return {
            "state": self.current_state,
            "steps": steps,
            "delta_y": delta_y,
        }

    async def _open_comment_panel(
        self,
        page: Any,
        elements: dict,
    ) -> dict:
        if self.current_state not in {"feed_ready", "comment_panel_closed"}:
            raise ProbeSafetyError(
                "probe_transition_forbidden",
                "open_comment_panel",
            )
        definition = elements.get(self.comment_entry_alias)
        if not isinstance(definition, dict):
            raise ProbeSafetyError(
                "probe_element_missing",
                "open_comment_panel",
            )

        resolved = await self.element_resolver(
            page,
            self.comment_entry_alias,
            definition,
        )
        if (
            await resolved.locator.get_attribute("aria-expanded")
            == "true"
        ):
            readiness = await self._wait_for_comment_panel_ready(page)
            self.current_state = "comment_panel_open"
            return {
                "state": self.current_state,
                "clicked": False,
                "alias": self.comment_entry_alias,
                "panel_visible": True,
                "stable_samples": readiness["stable_samples"],
                "required_samples": readiness["required_samples"],
                "fingerprint_hash": readiness["fingerprint_hash"],
            }
        await resolved.locator.click()
        await self._require_safe_origin(page, "open_comment_panel")
        readiness = await self._wait_for_comment_panel_ready(page)

        self.current_state = "comment_panel_open"
        return {
            "state": self.current_state,
            "clicked": True,
            "alias": self.comment_entry_alias,
            "panel_visible": True,
            "stable_samples": readiness["stable_samples"],
            "required_samples": readiness["required_samples"],
            "fingerprint_hash": readiness["fingerprint_hash"],
        }

    @staticmethod
    def _hash_panel_controls(value: Mapping[str, object]) -> str:
        fields = (
            "panel_role",
            "aria_busy",
            "input_count",
            "input_a11y",
            "input_data_e2e",
            "input_aria_label",
            "contenteditable",
            "textbox_editable",
            "submit_count",
            "submit_a11y",
            "submit_data_e2e",
            "submit_aria_label",
            "submit_disabled",
        )
        encoded = json.dumps(
            {key: value.get(key) for key in fields},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    async def _unique_visible(locator: Any) -> tuple[int, Any | None]:
        selected = None
        visible = 0
        count = await locator.count()
        if count > 20:
            return 2, None
        for index in range(count):
            candidate = locator.nth(index)
            if await candidate.is_visible():
                visible += 1
                selected = candidate
        return visible, selected if visible == 1 else None

    async def _visible_panel_locator(self, page: Any) -> Any | None:
        try:
            locator, _diagnostics = await self.scope_resolver(
                page,
                "visible_comment_panel",
            )
            return locator
        except LocatorResolutionError as error:
            if error.code != "element_scope_not_found":
                raise ProbeSafetyError(
                    "probe_panel_check_failed",
                    "verify_comment_panel",
                ) from None

        shells = page.locator(_COMMENT_PANEL_SHELL_SELECTOR)
        visible = []
        for index in range(min(await shells.count(), 10)):
            candidate = shells.nth(index)
            if await candidate.is_visible():
                visible.append(candidate)
        return visible[0] if len(visible) == 1 else None

    async def _comment_panel_readiness_sample(self, page: Any) -> dict:
        panel = await self._visible_panel_locator(page)
        if panel is None:
            return {
                "panel_visible": False,
                "input_visible": False,
                "textbox_visible": False,
                "submit_visible": False,
                "submit_disabled": False,
                "loading_marker": "",
                "aria_busy": False,
                "fingerprint_hash": "",
            }

        aria_busy = (await panel.get_attribute("aria-busy")) == "true"
        loading_marker = ""
        for selector in _COMMENT_PANEL_LOADING_MARKERS:
            markers = panel.locator(selector)
            marker_count = await markers.count()
            for index in range(marker_count):
                if await markers.nth(index).is_visible():
                    loading_marker = selector
                    break
            if loading_marker:
                break
        if aria_busy:
            return {
                "panel_visible": True,
                "input_visible": False,
                "textbox_visible": False,
                "submit_visible": False,
                "submit_disabled": False,
                "loading_marker": loading_marker,
                "aria_busy": aria_busy,
                "fingerprint_hash": "",
            }

        input_count, input_container = await self._unique_visible(
            panel.locator(_COMMENT_INPUT_SELECTOR)
        )
        textbox_count, textbox = await self._unique_visible(
            panel.locator(_COMMENT_TEXTBOX_SELECTOR)
        )
        submit_count, submit = await self._unique_visible(
            panel.locator(_COMMENT_SUBMIT_SELECTOR)
        )

        async def a11y_text(
            locator: Any | None,
            expected_role: str,
        ) -> str:
            if locator is None:
                return ""
            value = await locator.aria_snapshot()
            if not isinstance(value, str):
                return ""
            bounded = value.strip()[:512]
            first = next(
                (
                    line.strip()
                    for line in bounded.splitlines()
                    if line.strip()
                ),
                "",
            )
            prefix = f"- {expected_role}"
            return (
                bounded
                if first == prefix or first.startswith(prefix + " ")
                else ""
            )

        async def attribute(locator: Any | None, name: str) -> str:
            if locator is None:
                return ""
            value = await locator.get_attribute(name)
            return value.strip()[:160] if isinstance(value, str) else ""

        input_a11y = await a11y_text(textbox, "textbox")
        submit_a11y = await a11y_text(submit, "button")
        textbox_editable = (
            await textbox.is_editable()
            if textbox is not None
            else False
        )
        panel_role = str(await panel.get_attribute("role") or "")[:160]
        semantic = {
            "panel_role": panel_role,
            "aria_busy": aria_busy,
            "input_count": input_count,
            "input_a11y": input_a11y,
            "input_data_e2e": await attribute(
                input_container,
                "data-e2e",
            ),
            "input_aria_label": await attribute(textbox, "aria-label"),
            "contenteditable": await attribute(
                textbox,
                "contenteditable",
            ),
            "textbox_editable": textbox_editable,
            "submit_count": submit_count,
            "submit_a11y": submit_a11y,
            "submit_data_e2e": await attribute(submit, "data-e2e"),
            "submit_aria_label": await attribute(submit, "aria-label"),
            "submit_disabled": (
                await submit.is_disabled()
                if submit is not None
                else False
            ),
        }
        return {
            "panel_visible": True,
            "input_visible": input_count == 1,
            "textbox_visible": (
                textbox_count == 1
                and bool(input_a11y)
                and textbox_editable
            ),
            "submit_visible": submit_count == 1 and bool(submit_a11y),
            "submit_disabled": semantic["submit_disabled"],
            "loading_marker": loading_marker,
            "aria_busy": aria_busy,
            "fingerprint_hash": self._hash_panel_controls(semantic),
        }

    async def _wait_for_comment_panel_ready(self, page: Any) -> dict:
        if self._panel_sampler_poisoned:
            raise ProbeSafetyError(
                "probe_panel_check_failed",
                "verify_comment_panel",
            )
        deadline = (
            self.monotonic_fn()
            + self.comment_readiness_timeout_seconds
        )
        previous = ""
        stable = 0
        saw_eligible = False

        def timeout_error() -> ProbeSafetyError:
            return ProbeSafetyError(
                (
                    "comment_panel_snapshot_unstable"
                    if saw_eligible
                    else "comment_panel_readiness_timeout"
                ),
                "open_comment_panel",
            )

        def consume_cancelled_task(done: asyncio.Task) -> None:
            try:
                done.result()
            except BaseException:
                pass

        while True:
            remaining = deadline - self.monotonic_fn()
            if remaining <= 0:
                raise timeout_error()
            task = asyncio.create_task(
                self.panel_readiness_check(page)
            )
            try:
                done, _pending = await asyncio.wait(
                    {task},
                    timeout=remaining,
                )
                if not done:
                    self._panel_sampler_poisoned = True
                    task.cancel()
                    task.add_done_callback(consume_cancelled_task)
                    raise ProbeSafetyError(
                        "probe_panel_check_failed",
                        "verify_comment_panel",
                    )
                sample = task.result()
            except asyncio.CancelledError:
                self._panel_sampler_poisoned = True
                task.cancel()
                task.add_done_callback(consume_cancelled_task)
                raise
            except ProbeSafetyError:
                raise
            except Exception:
                raise ProbeSafetyError(
                    "probe_panel_check_failed",
                    "verify_comment_panel",
                ) from None
            if self.monotonic_fn() >= deadline:
                raise timeout_error()
            if not isinstance(sample, Mapping):
                raise ProbeSafetyError(
                    "probe_panel_check_failed",
                    "verify_comment_panel",
                )

            eligible = (
                sample.get("panel_visible") is True
                and not sample.get("loading_marker")
                and sample.get("aria_busy") is False
            )
            fingerprint = str(sample.get("fingerprint_hash") or "")
            if eligible and fingerprint:
                saw_eligible = True
                stable = stable + 1 if fingerprint == previous else 1
                previous = fingerprint
                if stable >= _COMMENT_PANEL_STABLE_SAMPLES:
                    if not all(
                        sample.get(key) is True
                        for key in (
                            "input_visible",
                            "textbox_visible",
                            "submit_visible",
                        )
                    ):
                        raise ProbeSafetyError(
                            "comment_panel_element_missing",
                            "open_comment_panel",
                        )
                    return {
                        **sample,
                        "stable_samples": stable,
                        "required_samples": _COMMENT_PANEL_STABLE_SAMPLES,
                    }
            else:
                previous = ""
                stable = 0

            remaining = deadline - self.monotonic_fn()
            if remaining <= 0:
                raise timeout_error()
            await self.sleep_fn(
                min(
                    self.comment_readiness_poll_interval_seconds,
                    remaining,
                )
            )

    async def _close_comment_panel(
        self,
        page: Any,
        elements: dict,
    ) -> dict:
        if self.current_state != "comment_panel_open":
            raise ProbeSafetyError(
                "probe_transition_forbidden",
                "close_comment_panel",
            )
        if not await self._panel_visible(page):
            raise ProbeSafetyError(
                "probe_state_verification_failed",
                "close_comment_panel",
            )

        closed_with = "escape"
        if self.comment_close_alias is not None:
            definition = elements.get(self.comment_close_alias)
            if not isinstance(definition, dict):
                raise ProbeSafetyError(
                    "probe_element_missing",
                    "close_comment_panel",
                )
            resolved = await self.element_resolver(
                page,
                self.comment_close_alias,
                definition,
            )
            await resolved.locator.click()
            closed_with = "locator"
        else:
            await page.keyboard.press("Escape")

        await self._require_safe_origin(page, "close_comment_panel")
        if not await self._wait_for_panel_state(page, visible=False):
            raise ProbeSafetyError(
                "probe_state_verification_failed",
                "close_comment_panel",
            )
        self.current_state = "comment_panel_closed"
        return {
            "state": self.current_state,
            "closed_with": closed_with,
            "panel_visible": False,
        }

    async def _wait_for_panel_state(
        self,
        page: Any,
        *,
        visible: bool,
    ) -> bool:
        deadline = self.monotonic_fn() + self.panel_timeout_seconds
        while True:
            if await self._panel_visible(page) is visible:
                return True
            if self.monotonic_fn() >= deadline:
                return False
            await self.sleep_fn(self.poll_interval_seconds)

    async def _panel_visible(self, page: Any) -> bool:
        try:
            await self.scope_resolver(page, "visible_comment_panel")
            return True
        except LocatorResolutionError as error:
            if error.code == "element_scope_not_found":
                return False
            raise ProbeSafetyError(
                "probe_panel_check_failed",
                "verify_comment_panel",
            ) from None

    async def _require_safe_origin(self, page: Any, action: str) -> None:
        origin = self._origin_from_url(getattr(page, "url", ""))
        if origin != self.expected_origin:
            raise ProbeSafetyError("probe_origin_mismatch", action)

    @staticmethod
    def _origin_from_url(value: object) -> str:
        parsed = urlsplit(str(value or ""))
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not hostname:
            return ""
        try:
            port = parsed.port
        except ValueError:
            return ""
        default_port = 443 if parsed.scheme == "https" else 80
        port_suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parsed.scheme}://{hostname}{port_suffix}"
