"""Readiness polling for V2 strategies."""

from __future__ import annotations

import asyncio
import time
from fnmatch import fnmatchcase
from typing import Any, Awaitable, Callable

from execution_v2.locator import LocatorResolutionError, ResolvedElement


class PageReadinessError(RuntimeError):
    def __init__(self, code: str, diagnostics: dict[str, Any] | None = None):
        self.code = code
        self.diagnostics = diagnostics or {}
        super().__init__(code)


class ReadinessTimeout(PageReadinessError):
    pass


async def wait_until_ready(
    page: Any,
    definition: dict[str, Any],
    resolver: Any,
    *,
    timeout_seconds: float,
    sample_interval: float = 0.5,
    required_stable_samples: int = 3,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> ResolvedElement:
    """Return after consecutive exact bounding-box samples; otherwise fail closed."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if sample_interval <= 0:
        raise ValueError("sample_interval must be positive")
    if required_stable_samples <= 0:
        raise ValueError("required_stable_samples must be positive")
    _validate_page_url(page, definition)

    deadline = clock() + timeout_seconds
    stable_count = 0
    previous_box: tuple[float, float, float, float] | None = None
    last_error: LocatorResolutionError | None = None
    last_box: tuple[float, float, float, float] | None = None

    while True:
        try:
            resolved = await resolver.resolve(page, definition)
            current_box = _box_tuple(resolved.box)
            if current_box == previous_box:
                stable_count += 1
            else:
                stable_count = 1
            previous_box = current_box
            last_box = current_box
            if stable_count >= required_stable_samples:
                return resolved
        except LocatorResolutionError as error:
            stable_count = 0
            previous_box = None
            last_error = error

        if clock() >= deadline:
            diagnostics: dict[str, Any] = {
                "required_stable_samples": required_stable_samples,
                "stable_samples": stable_count,
                "last_box": last_box,
            }
            if last_error is not None:
                diagnostics["last_locator_error"] = {
                    "code": last_error.code,
                    "diagnostics": last_error.diagnostics,
                }
            raise ReadinessTimeout("readiness_timeout", diagnostics)
        await sleep(sample_interval)


def _validate_page_url(page: Any, definition: dict[str, Any]) -> None:
    url = getattr(page, "url", "")
    if not isinstance(url, str) or not url or url.startswith("about:blank"):
        raise PageReadinessError("page_url_blank", {"url": url})
    pattern = definition.get("url_pattern")
    if not isinstance(pattern, str) or not fnmatchcase(url, pattern):
        raise PageReadinessError("page_url_mismatch", {"url": url, "url_pattern": pattern})


def _box_tuple(box: dict[str, float]) -> tuple[float, float, float, float]:
    return box["x"], box["y"], box["width"], box["height"]
