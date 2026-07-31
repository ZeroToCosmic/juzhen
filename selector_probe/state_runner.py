from __future__ import annotations

import asyncio
import time
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


class ProbeSafetyError(RuntimeError):
    def __init__(self, code: str, action: str):
        self.code = code
        self.action = action
        super().__init__(f"{code}: {action}")


ReadinessCheck = Callable[[Any], Awaitable[dict]]
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
        self.panel_timeout_seconds = panel_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.max_skeleton_nodes = max_skeleton_nodes
        self.max_scroll_steps = max_scroll_steps
        self.max_scroll_delta = max_scroll_delta
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self.current_state: str | None = None

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
                if not await self._panel_visible(page):
                    raise ProbeSafetyError(
                        "probe_state_verification_failed",
                        "open_comment_panel",
                    )
                return {
                    "state": "comment_panel_open",
                    "clicked": False,
                    "panel_visible": True,
                }
            if self.current_state not in {"feed_ready", "comment_panel_closed"}:
                raise ProbeSafetyError(
                    "probe_transition_forbidden",
                    "open_comment_panel",
                )
            if await self._panel_visible(page):
                self.current_state = "comment_panel_open"
                return {
                    "state": "comment_panel_open",
                    "clicked": False,
                    "panel_visible": True,
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
        await resolved.locator.click()
        await self._require_safe_origin(page, "open_comment_panel")
        if not await self._wait_for_panel_state(page, visible=True):
            raise ProbeSafetyError(
                "probe_state_verification_failed",
                "open_comment_panel",
            )

        self.current_state = "comment_panel_open"
        return {
            "state": self.current_state,
            "clicked": True,
            "alias": self.comment_entry_alias,
            "panel_visible": True,
        }

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
