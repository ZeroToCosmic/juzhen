"""Async Playwright primitives for human-like DOM interactions."""

from __future__ import annotations

import asyncio
import inspect
import math
import random

from ghost_cursor_bridge import GhostCursorError, generate_ghost_path


TRAJECTORY_LENGTH_EPSILON = 1e-5


async def _run_before_side_effect(before_side_effect) -> None:
    if before_side_effect is None:
        return
    result = before_side_effect()
    if inspect.isawaitable(result):
        await result


async def get_viewport(page) -> tuple[float, float]:
    viewport = getattr(page, "viewport_size", None)
    if not viewport and hasattr(page, "evaluate"):
        viewport = await page.evaluate(
            "({width: window.innerWidth, height: window.innerHeight})"
        )
    viewport = viewport or {"width": 1280, "height": 720}
    return max(float(viewport["width"]), 1), max(float(viewport["height"]), 1)


def _clamp(value: float, maximum: float) -> float:
    upper = math.nextafter(float(maximum), -math.inf)
    return min(max(float(value), 0.0), upper)


def _pointer(page, width: float, height: float) -> tuple[float, float]:
    return getattr(page, "_human_pointer", (width / 2, height / 2))


def _set_pointer(page, x: float, y: float) -> None:
    page._human_pointer = (x, y)


def _sample_range(value, rng) -> float:
    return rng.uniform(float(value[0]), float(value[1]))


def _map_pattern_point(
    point: tuple[float, float],
    source_start: tuple[float, float],
    source_end: tuple[float, float],
    target_start: tuple[float, float],
    target_end: tuple[float, float],
    progress: float,
    stable_residual: tuple[float, float] | None = None,
) -> tuple[float, float]:
    source_dx = source_end[0] - source_start[0]
    source_dy = source_end[1] - source_start[1]
    source_length = math.hypot(source_dx, source_dy)
    source_length_squared = source_length * source_length
    target_dx = target_end[0] - target_start[0]
    target_dy = target_end[1] - target_start[1]
    if stable_residual is not None:
        residual_x = point[0] - source_start[0] - progress * source_dx
        residual_y = point[1] - source_start[1] - progress * source_dy
        stable_length = math.hypot(*stable_residual)
        if stable_length >= TRAJECTORY_LENGTH_EPSILON:
            stable_length_squared = stable_length * stable_length
            tangent = (
                residual_x * stable_residual[0]
                + residual_y * stable_residual[1]
            ) / stable_length_squared
            normal = (
                stable_residual[0] * residual_y
                - stable_residual[1] * residual_x
            ) / stable_length_squared
            return (
                target_start[0] + progress * target_dx + tangent * target_dx - normal * target_dy,
                target_start[1] + progress * target_dy + tangent * target_dy + normal * target_dx,
            )
    if source_length < TRAJECTORY_LENGTH_EPSILON:
        return (
            target_start[0] + target_dx * progress,
            target_start[1] + target_dy * progress,
        )
    offset_x = point[0] - source_start[0]
    offset_y = point[1] - source_start[1]
    tangent = (offset_x * source_dx + offset_y * source_dy) / source_length_squared
    normal = (source_dx * offset_y - source_dy * offset_x) / source_length_squared
    return (
        target_start[0] + tangent * target_dx - normal * target_dy,
        target_start[1] + tangent * target_dy + normal * target_dx,
    )


async def human_scroll(page, *, min_steps: int = 8, max_steps: int = 16) -> None:
    """Retained legacy upward scrolling primitive."""

    steps = random.randint(min_steps, max_steps)
    width, height = await get_viewport(page)
    center_x = width * random.uniform(0.35, 0.65)
    center_y = height * random.uniform(0.45, 0.75)
    amplitude = random.uniform(18, 70)
    for index in range(steps):
        progress = (index + 1) / steps
        x = center_x + math.sin(progress * math.pi) * amplitude
        y = center_y + random.uniform(-8, 8)
        await page.mouse.move(x, y, steps=random.randint(1, 3))
        await page.mouse.wheel(0, -random.randint(55, 145))
        await asyncio.sleep(random.uniform(0.08, 0.28))


async def random_pause(page=None, *, minimum: float = 5.0, maximum: float = 15.0) -> None:
    if minimum < 0 or maximum < minimum:
        raise ValueError("invalid pause range")
    duration = random.uniform(minimum, maximum)
    if page is not None and hasattr(page, "wait_for_timeout"):
        await page.wait_for_timeout(round(duration * 1000))
    else:
        await asyncio.sleep(duration)


def _pattern_intervals(text: str, timing: dict, patterns: dict[str, dict], rng) -> list[float]:
    if timing.get("source") != "pattern":
        interval = timing.get("interval_ms", [50, 250])
        return [rng.uniform(float(interval[0]), float(interval[1])) for _ in text]
    samples = patterns[timing["id"]]["data"]["intervals_ms"]
    if not samples:
        raise ValueError("keyboard pattern must contain timing samples")
    count = len(text)
    if count <= len(samples):
        start = rng.randint(0, len(samples) - count)
        selected = samples[start : start + count]
    else:
        selected = []
        while len(selected) < count:
            start = rng.randint(0, len(samples) - 1)
            selected.extend(samples[start:] + samples[:start])
        selected = selected[:count]
    return [float(value) * rng.uniform(0.9, 1.1) for value in selected]


def _pattern_timing_pairs(text: str, timing: dict, patterns: dict[str, dict], rng) -> list[tuple[float, float]]:
    data = patterns[timing["id"]]["data"]
    intervals = data["intervals_ms"]
    holds = data["hold_ms"]
    if not intervals or len(intervals) != len(holds):
        raise ValueError("keyboard pattern must contain matching timing samples")
    count = len(text)
    samples = list(zip(intervals, holds))
    if count <= len(samples):
        start = rng.randint(0, len(samples) - count)
        selected = samples[start : start + count]
    else:
        selected = []
        while len(selected) < count:
            start = rng.randint(0, len(samples) - 1)
            selected.extend(samples[start:] + samples[:start])
        selected = selected[:count]
    return [
        (
            float(interval_ms) * rng.uniform(0.9, 1.1),
            float(hold_ms) * rng.uniform(0.9, 1.1),
        )
        for interval_ms, hold_ms in selected
    ]


async def _replay_pattern_character(keyboard, character: str) -> None:
    if character.isascii() and character.isprintable():
        await keyboard.down(character)
        return
    if hasattr(keyboard, "insert_text"):
        await keyboard.insert_text(character)
        return
    await keyboard.type(character)


async def _release_pattern_character(keyboard, character: str) -> None:
    if character.isascii() and character.isprintable():
        await keyboard.up(character)


async def human_type(
    page,
    text: str,
    *,
    timing: dict | None = None,
    patterns: dict[str, dict] | None = None,
    rng=None,
    sleep_fn=None,
    before_side_effect=None,
) -> None:
    """Type text with timing-only keyboard patterns; patterns never carry content."""

    rng = rng or random
    sleep = sleep_fn or asyncio.sleep
    text = str(text)
    timing = timing or {"source": "builtin", "interval_ms": [50, 250]}
    if timing.get("source") == "pattern":
        previous_hold_ms = 0.0
        for index, (character, (interval_ms, hold_ms)) in enumerate(zip(
            text, _pattern_timing_pairs(text, timing, patterns or {}, rng)
        )):
            delay_ms = interval_ms if index == 0 else max(0.0, interval_ms - previous_hold_ms)
            await sleep(delay_ms / 1000)
            await _run_before_side_effect(before_side_effect)
            await _replay_pattern_character(page.keyboard, character)
            await sleep(hold_ms / 1000)
            await _release_pattern_character(page.keyboard, character)
            previous_hold_ms = hold_ms
        return
    intervals = _pattern_intervals(text, timing, patterns or {}, rng)
    for character, interval_ms in zip(text, intervals):
        await _run_before_side_effect(before_side_effect)
        await page.keyboard.type(character)
        await sleep(interval_ms / 1000)


async def human_move_to(
    page,
    x: float,
    y: float,
    *,
    duration_seconds: float = 0.3,
    pattern: dict | None = None,
    target_box: dict[str, float] | None = None,
    viewport_size: tuple[float, float] | None = None,
    rng=None,
    sleep_fn=None,
    before_side_effect=None,
) -> tuple[float, float]:
    """Move from the tracked pointer to a clipped viewport coordinate."""

    rng = rng or random
    sleep = sleep_fn or asyncio.sleep
    width, height = (
        viewport_size if viewport_size is not None else await get_viewport(page)
    )
    target_x, target_y = _clamp(x, width), _clamp(y, height)
    start_x, start_y = _pointer(page, width, height)
    points = (pattern or {}).get("data", {}).get("points", [])
    final_x, final_y = target_x, target_y
    if points:
        first = points[0]
        last = points[-1]
        source_start = (float(first["x_ratio"]), float(first["y_ratio"]))
        source_end = (float(last["x_ratio"]), float(last["y_ratio"]))
        source_dx = source_end[0] - source_start[0]
        source_dy = source_end[1] - source_start[1]
        source_length = math.hypot(source_dx, source_dy)
        residuals = [
            (
                float(point["x_ratio"]) - source_start[0] - index / max(len(points) - 1, 1) * source_dx,
                float(point["y_ratio"]) - source_start[1] - index / max(len(points) - 1, 1) * source_dy,
            )
            for index, point in enumerate(points)
        ]
        stable_residual = max(residuals, key=lambda item: item[0] * item[0] + item[1] * item[1])
        stable_length = math.hypot(*stable_residual)
        if source_length >= TRAJECTORY_LENGTH_EPSILON or stable_length < TRAJECTORY_LENGTH_EPSILON:
            stable_residual = None
        for index, point in enumerate(points):
            progress = index / max(len(points) - 1, 1)
            mapped_x, mapped_y = _map_pattern_point(
                (float(point["x_ratio"]), float(point["y_ratio"])),
                source_start,
                source_end,
                (start_x, start_y),
                (target_x, target_y),
                progress,
                stable_residual,
            )
            current_x = _clamp(
                mapped_x,
                width,
            )
            current_y = _clamp(
                mapped_y,
                height,
            )
            if index == len(points) - 1:
                current_x, current_y = target_x, target_y
            await _run_before_side_effect(before_side_effect)
            await page.mouse.move(current_x, current_y)
            await sleep(float(point["dt_ms"]) / 1000)
    else:
        route = await asyncio.to_thread(
            generate_ghost_path,
            (start_x, start_y),
            (target_x, target_y),
            target_box,
        )
        if len(route) < 2:
            raise GhostCursorError("Ghost Cursor returned fewer than two path points")
        delay = max(float(duration_seconds), 0.0) / len(route)
        for index, point in enumerate(route):
            current_x = _clamp(point["x"], width)
            current_y = _clamp(point["y"], height)
            if index == len(route) - 1:
                current_x, current_y = target_x, target_y
            await _run_before_side_effect(before_side_effect)
            await page.mouse.move(current_x, current_y)
            await sleep(delay)
            final_x, final_y = current_x, current_y
    _set_pointer(page, final_x, final_y)
    return final_x, final_y


async def element_viewport_target(
    page,
    selector: str,
    operation: str,
    viewport_size: tuple[float, float] | None = None,
) -> tuple[tuple[float, float], dict[str, float]]:
    field = page.locator(selector)
    await field.scroll_into_view_if_needed()
    box = await field.bounding_box()
    if not box:
        raise RuntimeError(f"element cannot be {operation}: {selector}")
    width, height = (
        viewport_size if viewport_size is not None else await get_viewport(page)
    )
    left = max(float(box["x"]), 0.0)
    top = max(float(box["y"]), 0.0)
    right = min(float(box["x"]) + float(box["width"]), width)
    bottom = min(float(box["y"]) + float(box["height"]), height)
    if right <= left or bottom <= top:
        raise RuntimeError(f"element is outside the viewport: {selector}")
    point = ((left + right) / 2, (top + bottom) / 2)
    target = {
        "x": left,
        "y": top,
        "width": right - left,
        "height": bottom - top,
    }
    return point, target


async def element_viewport_point(
    page, selector: str, operation: str
) -> tuple[float, float]:
    point, _target = await element_viewport_target(page, selector, operation)
    return point


async def human_move(page, selector: str, *, duration_seconds: float = 0.3, pattern: dict | None = None, rng=None, sleep_fn=None) -> None:
    viewport_size = await get_viewport(page)
    (x, y), target_box = await element_viewport_target(
        page, selector, "moved to", viewport_size
    )
    await human_move_to(
        page,
        x,
        y,
        duration_seconds=duration_seconds,
        pattern=pattern,
        target_box=target_box,
        viewport_size=viewport_size,
        rng=rng,
        sleep_fn=sleep_fn,
    )


async def human_click(page, selector: str, *, button: str = "left", click_count: int = 1, hold_seconds=(0.05, 0.15), trajectory: dict | None = None, patterns: dict[str, dict] | None = None, rng=None, sleep_fn=None) -> tuple[float, float, float]:
    """Move to an element then click it with the requested mouse parameters."""

    rng = rng or random
    sleep = sleep_fn or asyncio.sleep
    viewport_size = await get_viewport(page)
    (x, y), target_box = await element_viewport_target(
        page, selector, "clicked", viewport_size
    )
    pattern = None
    if trajectory and trajectory.get("source") == "pattern":
        pattern = (patterns or {})[trajectory["id"]]
    final_x, final_y = await human_move_to(
        page,
        x,
        y,
        pattern=pattern,
        target_box=target_box,
        viewport_size=viewport_size,
        rng=rng,
        sleep_fn=sleep,
    )
    hold = _sample_range(hold_seconds, rng)
    await page.mouse.click(
        final_x,
        final_y,
        button=button,
        click_count=click_count,
        delay=round(hold * 1000),
    )
    return final_x, final_y, hold


async def type_comment(page, selector: str, comment: str) -> None:
    field = page.locator(selector)
    await field.click()
    await human_type(page, comment)


__all__ = [
    "human_scroll",
    "random_pause",
    "human_type",
    "human_move_to",
    "element_viewport_target",
    "human_move",
    "human_click",
    "type_comment",
]
