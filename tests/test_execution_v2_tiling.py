import asyncio

import pytest

from execution_v2.models import BrowserBinding
from execution_v2.tiling import WindowTileError, tile_browser_bindings


def binding(profile_id):
    return BrowserBinding(profile_id, f"ws://{profile_id}", object(), object(), object())


def passing_result(count):
    return {
        "count": count,
        "layout": [{"overlap_detected": False} for _ in range(count)],
        "missing": [],
        "scale_results": [
            {"profile_id": f"p{index}", "status": "scaled"}
            for index in range(count)
        ],
    }


def test_adapter_passes_the_complete_batch_to_the_legacy_tiler(monkeypatch):
    seen = []

    def legacy(hints):
        seen.append(hints)
        return passing_result(2)

    monkeypatch.setattr("execution_v2.tiling._legacy_tile", legacy)
    asyncio.run(tile_browser_bindings([binding("p0"), binding("p1")]))

    assert seen == [[
        {"profile_id": "p0", "ws_puppeteer": "ws://p0"},
        {"profile_id": "p1", "ws_puppeteer": "ws://p1"},
    ]]


@pytest.mark.parametrize("change", [
    {"count": 1},
    {"missing": ["one window missing"]},
    {"layout": [{"overlap_detected": True}, {"overlap_detected": False}]},
    {"scale_results": [
        {"profile_id": "p0", "status": "scaled"},
        {"profile_id": "p1", "status": "failed"},
    ]},
])
def test_adapter_fails_closed_on_incomplete_or_unsafe_layout(monkeypatch, change):
    result = passing_result(2)
    result.update(change)
    monkeypatch.setattr("execution_v2.tiling._legacy_tile", lambda _hints: result)

    with pytest.raises(WindowTileError, match="window_tile_failed"):
        asyncio.run(tile_browser_bindings([binding("p0"), binding("p1")]))
