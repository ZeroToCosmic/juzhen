"""Async V2 boundary around the existing Windows browser-window tiler."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from .models import BrowserBinding


class WindowTileError(RuntimeError):
    code = "window_tile_failed"

    def __init__(self) -> None:
        super().__init__(self.code)


def _legacy_tile(hints: list[dict[str, str]]) -> dict[str, Any]:
    from window_tiler import tile_browser_windows

    return tile_browser_windows(hints)


async def tile_browser_bindings(bindings: Sequence[BrowserBinding]) -> None:
    hints = [
        {"profile_id": binding.profile_id, "ws_puppeteer": binding.ws_url}
        for binding in bindings
    ]
    try:
        result = await asyncio.to_thread(_legacy_tile, hints)
    except Exception as error:
        raise WindowTileError() from error
    if not _valid_result(result, len(bindings)):
        raise WindowTileError()


def _valid_result(result: Any, expected: int) -> bool:
    if not isinstance(result, dict) or result.get("count") != expected:
        return False
    layout = result.get("layout")
    scales = result.get("scale_results")
    return (
        isinstance(layout, list)
        and len(layout) == expected
        and not result.get("missing")
        and all(
            isinstance(item, dict) and not item.get("overlap_detected")
            for item in layout
        )
        and isinstance(scales, list)
        and len(scales) == expected
        and all(
            isinstance(item, dict) and item.get("status") == "scaled"
            for item in scales
        )
    )


__all__ = ["WindowTileError", "tile_browser_bindings"]
