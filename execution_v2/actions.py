"""Execute one validated Browser Execution V2 action block."""

from __future__ import annotations

import asyncio
import math
import random
from collections.abc import Awaitable, Callable
from typing import Any

from actions_dom import get_viewport, human_move_to, human_type
from browser_video_switch import (
    VideoSwitchError,
    capture_feed_state,
    wait_for_stable_changed_state,
)


_BLUR_EDITABLE_SCRIPT = """() => {
    const active = document.activeElement;
    if (active && (
        active.matches('input, textarea, select')
        || active.isContentEditable
    )) {
        active.blur();
    }
}"""


class ActionExecutionError(RuntimeError):
    """A single V2 action could not complete safely."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def execute_action(
    page: Any,
    action: dict[str, Any],
    elements_by_id: dict[str, dict[str, Any]],
    resolver: Any,
    text_resolver: Callable[[dict[str, Any]], Awaitable[str]],
    *,
    rng: Any = random,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wheel_calibration: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one normalized action, without retrying or replaying it."""

    action_type = action["type"]
    if action_type == "move":
        return await _move(page, action, elements_by_id, resolver, rng=rng, sleep=sleep)
    if action_type == "scroll":
        return await _scroll(
            page,
            action,
            wheel_calibration=wheel_calibration,
            rng=rng,
            sleep=sleep,
        )
    if action_type == "click":
        return await _click(page, action, elements_by_id, resolver, rng=rng, sleep=sleep)
    if action_type == "input":
        return await _input(page, action, elements_by_id, resolver, text_resolver, rng=rng, sleep=sleep)
    if action_type == "wait":
        return await _wait(action, rng=rng, sleep=sleep)
    raise ActionExecutionError("unsupported_action_type")


async def _move(page: Any, action: dict[str, Any], elements: dict[str, dict[str, Any]], resolver: Any, *, rng: Any, sleep: Callable[[float], Awaitable[None]]) -> dict[str, Any]:
    resolved = await _resolve(page, action["element_id"], elements, resolver)
    x, y, box = await _interior_point(page, resolved.box, rng)
    duration = _sample(action["duration_seconds"], rng)
    await human_move_to(
        page, x, y, duration_seconds=duration, target_box=box, rng=rng, sleep_fn=sleep
    )
    return _result(action, duration_seconds=duration)


async def _scroll(
    page: Any,
    action: dict[str, Any],
    *,
    wheel_calibration: dict[str, Any] | None,
    rng: Any,
    sleep: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    requested = int(_sample(action["count"], rng, integer=True))
    switched = await execute_arrow_key_switches(
        page,
        direction=action["direction"],
        requested=requested,
        interval_range=action["interval_seconds"],
        rng=rng,
        sleep_fn=sleep,
    )
    return _result(
        action,
        direction=action["direction"],
        count=int(switched["completed_switches"]),
        interval_seconds=action["interval_seconds"],
        requested_switches=int(switched["requested_switches"]),
        completed_switches=int(switched["completed_switches"]),
    )


async def execute_arrow_key_switches(
    page: Any,
    *,
    direction: str,
    requested: int,
    interval_range: list[float] | list[int],
    rng: Any,
    sleep_fn: Callable[[float], Awaitable[None]],
) -> dict[str, Any]:
    """Send one arrow key per requested video and verify every transition."""

    if direction not in {"up", "down"}:
        raise ValueError("direction must be 'up' or 'down'")
    requested = int(requested)
    completed = 0
    switches = []
    key = "ArrowDown" if direction == "down" else "ArrowUp"
    while completed < requested:
        before = await capture_feed_state(page)
        await page.evaluate(_BLUR_EDITABLE_SCRIPT)
        await page.keyboard.press(key)
        after = await wait_for_stable_changed_state(
            page,
            before,
            timeout=8.0,
            sleep_fn=sleep_fn,
        )
        if after is None:
            raise VideoSwitchError(
                "video_switch_not_observed",
                requested_switches=requested,
                completed_switches=completed,
                safe_fingerprint=before.safe_fingerprint,
                switches=switches,
            )
        completed += 1
        switches.append(
            {
                "from": before.safe_fingerprint,
                "to": after.safe_fingerprint,
            }
        )
        if completed < requested:
            await sleep_fn(float(rng.uniform(*interval_range)))
    return {
        "requested_switches": requested,
        "completed_switches": completed,
        "switches": switches,
    }


async def _click(page: Any, action: dict[str, Any], elements: dict[str, dict[str, Any]], resolver: Any, *, rng: Any, sleep: Callable[[float], Awaitable[None]]) -> dict[str, Any]:
    resolved = await _resolve(page, action["element_id"], elements, resolver)
    x, y, box = await _interior_point(page, resolved.box, rng)
    await human_move_to(page, x, y, target_box=box, rng=rng, sleep_fn=sleep)
    hold = _sample(action["hold_seconds"], rng)
    for _ in range(action["click_count"]):
        await page.mouse.down(button=action["button"])
        await sleep(hold)
        await page.mouse.up(button=action["button"])
    after = _sample(action["after_seconds"], rng)
    if after:
        await sleep(after)
    return _result(
        action,
        button=action["button"],
        click_count=action["click_count"],
        hold_seconds=hold,
        after_seconds=after,
    )


async def _input(page: Any, action: dict[str, Any], elements: dict[str, dict[str, Any]], resolver: Any, text_resolver: Callable[[dict[str, Any]], Awaitable[str]], *, rng: Any, sleep: Callable[[float], Awaitable[None]]) -> dict[str, Any]:
    resolved = await _resolve(page, action["element_id"], elements, resolver, require_editable=True)
    text = action["fixed_text"] if action["content_source"] == "fixed" else await text_resolver(action)
    if not isinstance(text, str) or not text:
        raise ActionExecutionError("input_text_unavailable")
    before = _normalize_input_text(await _read_input_value(resolved.handle))
    await resolved.handle.focus()
    await human_type(
        page,
        text,
        timing={"source": "builtin", "interval_ms": action["interval_ms"]},
        rng=rng,
        sleep_fn=sleep,
    )
    after = _normalize_input_text(await _read_input_value(resolved.handle))
    expected = _normalize_input_text(text)
    if not expected or after.count(expected) <= before.count(expected):
        raise ActionExecutionError("input_verification_failed")
    return _result(action, content_source=action["content_source"], text_length=len(text))


async def _wait(action: dict[str, Any], *, rng: Any, sleep: Callable[[float], Awaitable[None]]) -> dict[str, Any]:
    duration = _sample(action["duration_seconds"], rng)
    await sleep(duration)
    return _result(action, duration_seconds=duration)


async def _resolve(page: Any, element_id: str, elements: dict[str, dict[str, Any]], resolver: Any, *, require_editable: bool = False) -> Any:
    element = elements.get(element_id)
    if not isinstance(element, dict) or not isinstance(element.get("definition"), dict):
        raise ActionExecutionError("element_definition_missing")
    try:
        return await resolver.resolve(
            page,
            element["definition"],
            require_editable=require_editable,
            require_in_viewport=True,
            allow_viewport_fallback=True,
        )
    except ActionExecutionError:
        raise
    except Exception as error:
        code = getattr(error, "code", "element_resolution_failed")
        raise ActionExecutionError(str(code)) from error


async def _interior_point(page: Any, box: dict[str, Any], rng: Any) -> tuple[float, float, dict[str, float]]:
    width, height = await get_viewport(page)
    left = max(float(box["x"]), 0.0)
    top = max(float(box["y"]), 0.0)
    right = min(float(box["x"]) + float(box["width"]), width)
    bottom = min(float(box["y"]) + float(box["height"]), height)
    if right <= left or bottom <= top:
        raise ActionExecutionError("element_outside_viewport")
    visible_box = {"x": left, "y": top, "width": right - left, "height": bottom - top}
    # Stay away from the edges while still varying the point inside a real target.
    x = left + visible_box["width"] * rng.uniform(0.35, 0.65)
    y = top + visible_box["height"] * rng.uniform(0.35, 0.65)
    return x, y, visible_box


async def _read_input_value(handle: Any) -> str:
    try:
        value = await handle.evaluate(
            """element => {
                const tag = String(element?.tagName || '').toLowerCase();
                if (tag === 'input' || tag === 'textarea') {
                    return String(element.value || '');
                }
                return String(element.innerText ?? element.textContent ?? '');
            }"""
        )
    except Exception as error:
        raise ActionExecutionError("input_verification_unavailable") from error
    return value if isinstance(value, str) else ""


def _normalize_input_text(value: str) -> str:
    return " ".join(
        value.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\u00a0", " ")
        .split()
    )


def _sample(value: list[float] | list[int], rng: Any, *, integer: bool = False) -> float | int:
    sampled = rng.uniform(float(value[0]), float(value[1]))
    return int(math.floor(sampled)) if integer else sampled


def _result(action: dict[str, Any], **details: Any) -> dict[str, Any]:
    return {
        "action_id": action["id"],
        "action_type": action["type"],
        "status": "succeeded",
        **details,
    }


__all__ = ["ActionExecutionError", "execute_action", "execute_arrow_key_switches"]
