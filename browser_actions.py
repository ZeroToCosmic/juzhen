"""Compatibility validation and async execution for browser action blocks."""

from __future__ import annotations

import asyncio
import inspect
import random
import time
from typing import Any

from actions_dom import get_viewport, human_move_to, human_type
from browser_element_resolver import (
    LocatorResolutionError,
    ResolvedElement,
    resolve_element,
    resolve_scope,
)
from browser_page_lifecycle import (
    attach_page_recoveries,
)
from browser_strategy_config import (
    ACTION_CATALOG,
    SCROLL_WHEEL_DELTA,
    normalize_block_strategies,
    normalize_elements,
)
from browser_video_switch import VideoSwitchError, execute_verified_switches


ACTION_TYPES = set(ACTION_CATALOG)
ELEMENT_ACTION_TYPES = {"move", "click", "keyboard_input"}
_LEGACY_ACTION_TYPES = {"move", "click", "input", "scroll_up", "scroll_down", "pause"}
_KNOWN_COMMENT_ENTRY_ALIASES = frozenset({"评论入口"})
COMMENT_PANEL_TIMEOUT_SECONDS = 5.0
COMMENT_ENTRY_RESOLUTION_TIMEOUT_SECONDS = 3.0
COMMENT_ENTRY_RESOLUTION_POLL_SECONDS = 0.1
_TRANSIENT_COMMENT_ENTRY_CODES = frozenset(
    {"element_candidate_not_found", "element_scope_not_found"}
)


def validate_action_config(
    elements: Any, strategies: Any, patterns: Any | None = None
) -> tuple[dict[str, dict], list[dict[str, Any]]]:
    """Normalize v2 block strategies, while retaining the old manual form."""

    normalized_elements = normalize_elements(elements)
    if not isinstance(strategies, list):
        raise ValueError("action strategies must be a list")
    if strategies and all(isinstance(item, dict) and "params" in item for item in strategies):
        normalized = normalize_block_strategies(
            [{"id": "default", "name": "Default", "run_mode": "once", "actions": strategies}],
            normalized_elements,
            patterns or [],
        )
        return normalized_elements, normalized[0]["actions"]
    if strategies and not all(isinstance(item, dict) and "type" in item for item in strategies):
        normalized = normalize_block_strategies(strategies, normalized_elements, patterns or [])
        return normalized_elements, normalized
    return normalized_elements, _normalize_legacy_actions(strategies, normalized_elements)


def _normalize_legacy_actions(actions: list[Any], elements: dict[str, dict]) -> list[dict[str, Any]]:
    normalized = []
    for index, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            raise ValueError(f"legacy action {index} must be a JSON object")
        action_type = str(action.get("type") or "").lower()
        if action_type not in _LEGACY_ACTION_TYPES:
            raise ValueError(f"unsupported action type: {action_type}")
        element = str(action.get("element") or "").strip()
        if action_type in {"move", "click", "input"} and element not in elements:
            raise ValueError(f"action references missing element: {element}")
        item = {**action, "type": action_type, "element": element}
        if action_type == "input" and not action.get("text") and action.get("content_source") != "generated_comment":
            raise ValueError("input action requires text or generated_comment")
        if action_type in {"scroll_up", "scroll_down", "pause"}:
            duration = float(action.get("duration", 1))
            if not 0.1 <= duration <= 120:
                raise ValueError("legacy timed action duration must be between 0.1 and 120")
            item["duration"] = duration
            if action_type in {"scroll_up", "scroll_down"}:
                distance = int(action.get("distance", 600))
                if not 1 <= distance <= 10000:
                    raise ValueError("legacy scroll distance must be between 1 and 10000")
                item["distance"] = SCROLL_WHEEL_DELTA
        normalized.append(item)
    return normalized


async def _resolve_text(text_resolver, action: dict[str, Any]) -> str:
    value = text_resolver(action)
    if inspect.isawaitable(value):
        value = await value
    if isinstance(value, tuple):
        value = value[0]
    return str(value or "")


async def _read_keyboard_input(field) -> str:
    try:
        return str(await field.input_value() or "")
    except Exception:
        return str(await field.text_content() or "")


async def _verify_keyboard_input(field, before: str, expected: str) -> None:
    after = await _read_keyboard_input(field)
    if expected not in after or after.count(expected) <= before.count(expected):
        raise RuntimeError("keyboard input was not reflected in the target element")


async def _require_editable(field, resolved) -> None:
    try:
        editable = await field.evaluate(
            """element => {
                const tag = String(element?.tagName || '').toLowerCase();
                const formField = tag === 'input' || tag === 'textarea';
                return Boolean(
                    (formField && !element.disabled && !element.readOnly)
                    || element?.isContentEditable
                    || element?.getAttribute?.('contenteditable') === 'true'
                );
            }"""
        )
    except Exception as error:
        if is_closed_target_error(error):
            raise
        raise LocatorResolutionError(
            "element_not_actionable",
            resolved.alias,
            resolved.scope,
            {"phase": "editable_check"},
        ) from None
    if not editable:
        raise LocatorResolutionError(
            "element_not_actionable",
            resolved.alias,
            resolved.scope,
            {"phase": "editable_check"},
        )


async def _resolve_action_element(page, alias: str, elements: dict[str, dict]):
    definition = elements.get(alias)
    if definition is None:
        raise LocatorResolutionError("element_alias_missing", alias, "", {})
    definition = normalize_elements({alias: definition})[alias]
    return definition, await resolve_element(page, alias, definition)


async def _resolve_comment_entry_when_ready(
    page,
    alias: str,
    elements: dict[str, dict],
    *,
    sleep_fn,
    monotonic_fn,
    timeout_seconds=COMMENT_ENTRY_RESOLUTION_TIMEOUT_SECONDS,
) -> tuple[dict, ResolvedElement]:
    deadline = monotonic_fn() + timeout_seconds
    raw_definition = elements.get(alias)
    normalized_scope = (
        normalize_elements({alias: raw_definition})[alias]["scope"]
        if raw_definition is not None
        else ""
    )
    last_error = None

    def readiness_error():
        return last_error or LocatorResolutionError(
            "element_candidate_not_found",
            alias,
            normalized_scope,
            {},
        )

    while True:
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            raise readiness_error() from None
        try:
            result = await asyncio.wait_for(
                _resolve_action_element(page, alias, elements),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            raise readiness_error() from None
        except LocatorResolutionError as error:
            if error.code not in _TRANSIENT_COMMENT_ENTRY_CODES:
                raise
            last_error = error
            remaining = deadline - monotonic_fn()
            if remaining <= 0:
                raise
            try:
                await asyncio.wait_for(
                    sleep_fn(min(COMMENT_ENTRY_RESOLUTION_POLL_SECONDS, remaining)),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                raise error from None
        else:
            if monotonic_fn() > deadline:
                raise readiness_error() from None
            return result


def _safe_locator_result(resolved) -> dict[str, str]:
    return {
        "scope": resolved.scope,
        "candidate_id": resolved.candidate["id"],
        "candidate_type": resolved.candidate["type"],
    }


def _bind_switch_recoveries(events, action):
    return [
        {
            **event,
            "action_id": action["id"],
            "action_type": action["type"],
        }
        for event in events
    ]


async def _locator_viewport_target(
    locator,
    operation: str,
    viewport_size: tuple[float, float],
    *,
    before_side_effect=None,
) -> tuple[tuple[float, float], dict[str, float]]:
    if before_side_effect is not None:
        await before_side_effect()
    await locator.scroll_into_view_if_needed()
    box = await locator.bounding_box()
    if not box:
        raise RuntimeError(f"resolved element cannot be {operation}")
    width, height = viewport_size
    left = max(float(box["x"]), 0.0)
    top = max(float(box["y"]), 0.0)
    right = min(float(box["x"]) + float(box["width"]), width)
    bottom = min(float(box["y"]) + float(box["height"]), height)
    if right <= left or bottom <= top:
        raise RuntimeError("resolved element is outside the viewport")
    return (
        ((left + right) / 2, (top + bottom) / 2),
        {
            "x": left,
            "y": top,
            "width": right - left,
            "height": bottom - top,
        },
    )


async def _dispatch_resolved_click(
    page,
    locator,
    params: dict,
    patterns: dict[str, dict],
    *,
    rng,
    sleep_fn,
    before_side_effect=None,
) -> float:
    viewport_size = await get_viewport(page)
    (x, y), target_box = await _locator_viewport_target(
        locator,
        "clicked",
        viewport_size,
        before_side_effect=before_side_effect,
    )
    trajectory = params["trajectory"]
    pattern = (
        patterns[trajectory["id"]]
        if trajectory.get("source") == "pattern"
        else None
    )
    final_x, final_y = await human_move_to(
        page,
        x,
        y,
        pattern=pattern,
        target_box=target_box,
        viewport_size=viewport_size,
        rng=rng,
        sleep_fn=sleep_fn,
        before_side_effect=before_side_effect,
    )
    hold = float(rng.uniform(*params["hold_seconds"]))
    if before_side_effect is not None:
        await before_side_effect()
    await page.mouse.click(
        final_x,
        final_y,
        button=params["button"],
        click_count=params["click_count"],
        delay=round(hold * 1000),
    )
    return hold


def _is_comment_entry(alias: str, definition: dict) -> bool:
    for candidate in definition.get("locators", []):
        if str(candidate.get("id") or "").startswith("tiktok-comment-entry"):
            return True
        if (
            candidate.get("type") == "attribute"
            and candidate.get("name") == "data-e2e"
            and candidate.get("value") == "comment-icon"
        ):
            return True
    return alias in _KNOWN_COMMENT_ENTRY_ALIASES


async def _comment_panel_visible(page) -> bool:
    if await page.locator('[data-e2e="comment-input"]:visible').count():
        return True
    try:
        await resolve_scope(page, "visible_comment_panel")
    except LocatorResolutionError:
        return False
    return True


async def _observe_comment_panel(
    page,
    action,
    *,
    page_lifecycle,
    sleep_fn,
    monotonic_fn,
    timeout_seconds,
) -> tuple[Any, bool, list[dict]]:
    if page_lifecycle is not None:
        return await page_lifecycle.observe(
            page,
            action,
            _comment_panel_visible,
            timeout_seconds=timeout_seconds,
        )
    deadline = monotonic_fn() + timeout_seconds
    while True:
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            return page, False, []
        try:
            observed = await asyncio.wait_for(
                _comment_panel_visible(page),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return page, False, []
        if observed and monotonic_fn() <= deadline:
            return page, True, []
        remaining = deadline - monotonic_fn()
        if remaining <= 0:
            return page, False, []
        try:
            await asyncio.wait_for(
                sleep_fn(min(0.1, remaining)),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            return page, False, []


async def execute_action(
    page,
    action: dict[str, Any],
    elements: dict[str, dict],
    patterns: dict[str, dict],
    text_resolver,
    *,
    rng: random.Random | Any = random,
    sleep_fn=None,
    page_lifecycle=None,
    monotonic_fn=time.monotonic,
    before_side_effect=None,
) -> dict[str, Any]:
    """Execute one canonical v2 action block and return its measurements."""

    action_type = action["type"]
    if action_type not in ACTION_TYPES:
        raise ValueError(f"unsupported action type: {action_type}")
    if isinstance(patterns, list):
        patterns = {pattern["id"]: pattern for pattern in patterns}
    params = action["params"]
    element = str(params.get("element") or "")
    result = {"action_id": action["id"], "type": action_type, "status": "ok", "element": element}
    sleep_fn = sleep_fn or asyncio.sleep

    if action_type == "move":
        trajectory = params["trajectory"]
        pattern = patterns[trajectory["id"]] if trajectory.get("source") == "pattern" else None
        duration = float(rng.uniform(*params["duration_seconds"]))
        viewport_width, viewport_height = await get_viewport(page)
        target_box = None
        resolved = None
        if params["target_mode"] == "element":
            _definition, resolved = await _resolve_action_element(
                page,
                element,
                elements,
            )
            (x, y), target_box = await _locator_viewport_target(
                resolved.locator,
                "moved to",
                (viewport_width, viewport_height),
                before_side_effect=before_side_effect,
            )
        else:
            x, y = getattr(page, "_human_pointer", (viewport_width / 2, viewport_height / 2))
            x += float(params["delta_viewport"][0]) * viewport_width
            y += float(params["delta_viewport"][1]) * viewport_height
        await human_move_to(
            page,
            x,
            y,
            duration_seconds=duration,
            pattern=pattern,
            target_box=target_box,
            viewport_size=(viewport_width, viewport_height),
            rng=rng,
            sleep_fn=sleep_fn,
            before_side_effect=before_side_effect,
        )
        return {
            **result,
            "duration_seconds": duration,
            "trajectory_source": (
                "recorded-pattern"
                if trajectory.get("source") == "pattern"
                else "ghost-cursor"
            ),
            **(
                {"locator": _safe_locator_result(resolved)}
                if resolved is not None
                else {}
            ),
        }
    if action_type == "click":
        definition = elements.get(element)
        if definition is None:
            definition, resolved = await _resolve_action_element(page, element, elements)
        else:
            definition = normalize_elements({element: definition})[element]
            if _is_comment_entry(element, definition):
                definition, resolved = await _resolve_comment_entry_when_ready(
                    page,
                    element,
                    elements,
                    sleep_fn=sleep_fn,
                    monotonic_fn=monotonic_fn,
                )
            else:
                definition, resolved = await _resolve_action_element(
                    page,
                    element,
                    elements,
                )
        hold = await _dispatch_resolved_click(
            page,
            resolved.locator,
            params,
            patterns,
            rng=rng,
            sleep_fn=sleep_fn,
            before_side_effect=before_side_effect,
        )
        click_result = {
            **result,
            "button": params["button"],
            "click_count": params["click_count"],
            "hold_seconds": hold,
            "locator": _safe_locator_result(resolved),
            "postcondition": "not_configured",
            "trajectory_source": (
                "recorded-pattern"
                if params["trajectory"].get("source") == "pattern"
                else "ghost-cursor"
            ),
        }
        if not _is_comment_entry(element, definition):
            return click_result
        page, observed, recoveries = await _observe_comment_panel(
            page,
            action,
            page_lifecycle=page_lifecycle,
            sleep_fn=sleep_fn,
            monotonic_fn=monotonic_fn,
            timeout_seconds=COMMENT_PANEL_TIMEOUT_SECONDS,
        )
        if not observed:
            error = LocatorResolutionError(
                "element_postcondition_not_observed",
                element,
                resolved.scope,
                {
                    "candidate_id": resolved.candidate["id"],
                    "candidate_type": resolved.candidate["type"],
                    "timeout_seconds": COMMENT_PANEL_TIMEOUT_SECONDS,
                },
            )
            if recoveries:
                error = attach_page_recoveries(error, recoveries)
            raise error
        click_result["postcondition"] = "observed"
        if page_lifecycle is not None:
            click_result["_active_page"] = page
            click_result["_page_recoveries"] = recoveries
        return click_result
    if action_type == "keyboard_input":
        _definition, resolved = await _resolve_action_element(
            page,
            element,
            elements,
        )
        field = resolved.locator
        await _require_editable(field, resolved)
        before = await _read_keyboard_input(field)
        text = await _resolve_text(text_resolver, action)
        if before_side_effect is not None:
            await before_side_effect()
        await field.focus()
        await human_type(
            page,
            text,
            timing=params["typing"],
            patterns=patterns,
            rng=rng,
            sleep_fn=sleep_fn,
            before_side_effect=before_side_effect,
        )
        await _verify_keyboard_input(field, before, text)
        return {
            **result,
            "text": text,
            "locator": _safe_locator_result(resolved),
        }
    if action_type in {"scroll_up", "scroll_down"}:
        total_count = int(rng.randint(*params["total_count"]))
        try:
            switch_result = await execute_verified_switches(
                page,
                direction="up" if action_type == "scroll_up" else "down",
                requested=total_count,
                interval_range=params["interval_seconds"],
                lifecycle=page_lifecycle,
                rng=rng,
                sleep_fn=sleep_fn,
                before_side_effect=before_side_effect,
            )
        except VideoSwitchError as error:
            recoveries = list(getattr(error, "page_recoveries", []))
            if recoveries:
                error.page_recoveries = _bind_switch_recoveries(
                    recoveries,
                    action,
                )
            raise
        recoveries = switch_result.get("_page_recoveries", [])
        if recoveries:
            switch_result["_page_recoveries"] = _bind_switch_recoveries(
                recoveries,
                action,
            )
        return {**result, **switch_result}
    if action_type == "pause":
        duration = float(rng.uniform(*params["duration_seconds"]))
        await sleep_fn(duration)
        return {**result, "duration_seconds": duration}
    raise NotImplementedError(f"canonical action is not implemented yet: {action_type}")


__all__ = ["ACTION_TYPES", "validate_action_config", "execute_action"]
