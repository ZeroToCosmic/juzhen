# Browser V2 Window Tiling and Current Viewport Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tile every connected V2 Profile batch with the existing Windows tiler and execute move, click, and input actions against the unique matching control in the current viewport.

**Architecture:** Add a narrow V2 adapter around `window_tiler.tile_browser_windows()` and inject it into `BatchScheduler` between CDP binding and strategy execution. Extend `StrictLocatorResolver` with an opt-in current-viewport fallback built only from allowlisted stable CSS attributes; action execution opts in while readiness remains unchanged.

**Tech Stack:** Python 3, asyncio, Playwright async API, Windows `pywin32`, Flask-served JavaScript, pytest, Node.js built-in test runner.

## Global Constraints

- Reuse `window_tiler.tile_browser_windows()`; do not duplicate or modify its layout algorithm.
- Keep strategy schema, element schema, database schema, legacy Gateway routes, and saved records unchanged.
- Current-viewport fallback may use only `data-e2e`, `data-testid`, `aria-label`, `name`, `placeholder`, `role`, and `contenteditable` CSS attributes.
- Never use element text, random class names, absolute XPath, fixed video item IDs, similarity scoring, or LLM matching.
- Accept exactly one visible, enabled, positive-area target whose center is inside the current viewport; zero or multiple candidates fail closed.
- Readiness resolution must not use current-viewport fallback.
- A tile failure blocks actions for that batch and still runs existing browser cleanup.
- `.git` metadata is read-only; do not stage or commit files.

---

### Task 1: Add the isolated V2 adapter for the legacy window tiler

**Files:**
- Create: `execution_v2/tiling.py`
- Create: `tests/test_execution_v2_tiling.py`

**Interfaces:**
- Consumes: `Sequence[BrowserBinding]` containing internal `profile_id` and `ws_url`.
- Produces: `async tile_browser_bindings(bindings: Sequence[BrowserBinding]) -> None`.
- Raises: `WindowTileError` with stable code and message `window_tile_failed`.

- [ ] **Step 1: Write failing adapter tests**

Create `tests/test_execution_v2_tiling.py`:

```python
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
```

- [ ] **Step 2: Run the adapter tests and verify missing module failure**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_tiling.py -q -p no:cacheprovider
```

Expected: collection fails because `execution_v2.tiling` does not exist.

- [ ] **Step 3: Implement the adapter**

Create `execution_v2/tiling.py`:

```python
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
        and all(isinstance(item, dict) and not item.get("overlap_detected") for item in layout)
        and isinstance(scales, list)
        and len(scales) == expected
        and all(isinstance(item, dict) and item.get("status") == "scaled" for item in scales)
    )


__all__ = ["WindowTileError", "tile_browser_bindings"]
```

- [ ] **Step 4: Run adapter tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_tiling.py -q -p no:cacheprovider
```

Expected: all tests pass.

---

### Task 2: Insert tiling into the V2 batch lifecycle and expose its stage

**Files:**
- Modify: `execution_v2/models.py:13-41`
- Modify: `execution_v2/scheduler.py:20-105,179-210`
- Modify: `execution_v2/service.py:92-210`
- Modify: `gateway/static/browser_v2.js:24-29`
- Modify: `tests/test_execution_v2_scheduler.py`
- Modify: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: optional `tile_batch: Callable[[Sequence[BrowserBinding]], Awaitable[None]]` injected into `BatchScheduler`.
- Produces: `ProfileStatus.TILING`, `Stage.WINDOW_TILE`, and persisted `window_tile_failed` outcomes.

- [ ] **Step 1: Write scheduler ordering and failure tests**

Add to `tests/test_execution_v2_scheduler.py`:

```python
def test_complete_batch_tiles_after_connections_and_before_actions(tmp_path):
    events = []
    store = initialized_store(tmp_path)

    async def tile(bindings):
        events.append(("tile", [item.profile_id for item in bindings]))

    scheduler = BatchScheduler(
        store,
        FakeAdsPowerAdapter(events),
        FakeSessionFactory(events),
        successful_executor(events),
        tile_batch=tile,
    )
    asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3"], 3))

    assert events.index(("connect", "p3")) < events.index(("tile", ["p1", "p2", "p3"]))
    assert events.index(("tile", ["p1", "p2", "p3"])) < events.index(("execute", "p1"))


def test_tile_failure_blocks_actions_and_still_closes_the_batch(tmp_path):
    events = []
    store = initialized_store(tmp_path)

    async def failed_tile(_bindings):
        events.append(("tile", "failed"))
        raise RuntimeError("private tiler detail")

    scheduler = BatchScheduler(
        store,
        FakeAdsPowerAdapter(events),
        FakeSessionFactory(events),
        successful_executor(events),
        tile_batch=failed_tile,
    )
    asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2"], 2))
    rows = store.list_profile_results("job-1")

    assert not [event for event in events if event[0] == "execute"]
    assert [event for event in events if event[0] == "stop"] == [("stop", "p1"), ("stop", "p2")]
    assert {row["stage"] for row in rows} == {"window_tile"}
    assert {row["error_code"] for row in rows} == {"window_tile_failed"}
    assert all("private" not in row["error_summary"] for row in rows)
```

Add to `tests-js/browser-v2-ui.test.js`:

```javascript
test("window tiling stage has an explicit Chinese label", () => {
  assert.equal(stageLabel("window_tile"), "正在排列窗口");
});
```

- [ ] **Step 2: Run focused tests and verify constructor/stage failures**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_scheduler.py -q -p no:cacheprovider
node --test --test-name-pattern="window tiling stage" tests-js\browser-v2-ui.test.js
```

Expected: Python tests fail because `tile_batch`, `TILING`, and `WINDOW_TILE` do not exist; JavaScript test fails with the fallback label.

- [ ] **Step 3: Add model and UI stage values**

In `execution_v2/models.py`, add:

```python
class ProfileStatus(StrEnum):
    # existing values stay unchanged
    TILING = "tiling"


class Stage(StrEnum):
    # existing values stay unchanged
    WINDOW_TILE = "window_tile"
```

In `gateway/static/browser_v2.js`, add to `STAGES`:

```javascript
window_tile: "正在排列窗口",
```

- [ ] **Step 4: Add scheduler injection and fail-closed tiling**

Extend `BatchScheduler.__init__`:

```python
tile_batch: Callable[[Sequence[BrowserBinding]], Awaitable[None]] | None = None,
```

Store it as `self.tile_batch`. After `_start_and_bind_batch()` returns, replace the unconditional execution call with:

```python
if await self._tile_bindings(job_id, bindings, records):
    await self._execute_batch(job_id, snapshot, bindings, records)
```

Add:

```python
async def _tile_bindings(self, job_id, bindings, records):
    if not bindings or self.tile_batch is None:
        return True
    for binding in bindings:
        self.store.set_profile_status(
            job_id, binding.profile_id, ProfileStatus.TILING, Stage.WINDOW_TILE
        )
    try:
        await self.tile_batch(bindings)
    except Exception:
        for binding in bindings:
            outcome = ProfileOutcome(
                binding.profile_id,
                False,
                Stage.WINDOW_TILE,
                "window_tile_failed",
                "window_tile_failed",
            )
            records[binding.profile_id] = outcome
            self._store_outcome(job_id, outcome, close_confirmed=False)
        return False
    return True
```

- [ ] **Step 5: Inject the production adapter without affecting fake schedulers**

In `ExecutionV2Service.__init__`, add optional keyword:

```python
batch_tiler: Callable[
    [Sequence[BrowserBinding]], Awaitable[None]
] | None = None,
```

Import `Awaitable` and `Sequence` from `collections.abc`; `BrowserBinding` is already imported from `.models`.

Pass it when constructing the default scheduler:

```python
scheduler = BatchScheduler(
    self.store, adspower, sessions, execution.run, tile_batch=batch_tiler
)
```

In `create_default_execution_v2_service()`, import and pass:

```python
from execution_v2.tiling import tile_browser_bindings

# ExecutionV2Service arguments
batch_tiler=tile_browser_bindings,
```

- [ ] **Step 6: Run lifecycle and UI tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_scheduler.py tests\test_execution_v2_tiling.py tests\test_execution_v2_service.py -q -p no:cacheprovider
node --test --test-name-pattern="window tiling stage" tests-js\browser-v2-ui.test.js
```

Expected: all tests pass.

---

### Task 3: Resolve a unique stable-attribute target in the current viewport

**Files:**
- Modify: `execution_v2/locator.py:1-160`
- Modify: `tests/test_execution_v2_locator.py`

**Interfaces:**
- Extends: `StrictLocatorResolver.resolve(..., require_in_viewport=False, allow_viewport_fallback=False)`.
- Produces: current viewport `ResolvedElement` or `LocatorResolutionError` codes `current_viewport_target_not_found` and `current_viewport_target_ambiguous`.

- [ ] **Step 1: Extend locator test doubles for viewport and indexed candidates**

In `tests/test_execution_v2_locator.py`, add `nth()` to `FakeLocator`:

```python
def nth(self, index):
    return FakeLocator(self.handles[index:index + 1])
```

Give `FakeFrame` a viewport and update `evaluate()`:

```python
def __init__(self, css=None, xpath=None, roles=None, viewport=None):
    self.css = css or {}
    self.xpath = xpath or {}
    self.roles = roles or {}
    self.viewport = viewport or {"width": 200, "height": 120}
    self.calls = []

async def evaluate(self, script, pair=None):
    if "window.innerWidth" in script:
        return self.viewport
    assert script == "(pair) => pair[0] === pair[1]"
    return pair[0] is pair[1]
```

- [ ] **Step 2: Write current-viewport fallback tests**

Add:

```python
def test_action_resolution_replaces_fixed_video_item_with_current_viewport_anchor():
    old = FakeHandle("old", box={"x": 10, "y": 300, "width": 100, "height": 80})
    current = FakeHandle("current", box={"x": 20, "y": 10, "width": 120, "height": 90})
    fixed = '#one-column-item-0 > [data-e2e="feed-video"] > .random-class'
    page = FakeFrame(css={
        fixed: FakeLocator([old]),
        '[data-e2e="feed-video"]': FakeLocator([old, current]),
    })

    resolved = resolve(
        page,
        definition({"type": "css", "value": fixed, "priority": 10}),
        require_in_viewport=True,
        allow_viewport_fallback=True,
    )

    assert resolved.handle is current
    assert resolved.locator_type == "css_viewport"


@pytest.mark.parametrize(
    ("handles", "code"),
    [
        ([], "current_viewport_target_not_found"),
        (
            [
                FakeHandle("one", box={"x": 10, "y": 10, "width": 30, "height": 30}),
                FakeHandle("two", box={"x": 60, "y": 10, "width": 30, "height": 30}),
            ],
            "current_viewport_target_ambiguous",
        ),
    ],
)
def test_current_viewport_fallback_fails_closed_for_zero_or_many_candidates(handles, code):
    fixed = '#one-column-item-0 [data-e2e="comment-icon"]'
    page = FakeFrame(css={
        fixed: FakeLocator(),
        '[data-e2e="comment-icon"]': FakeLocator(handles),
    })

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(
            page,
            definition({"type": "css", "value": fixed, "priority": 10}),
            require_in_viewport=True,
            allow_viewport_fallback=True,
        )

    assert caught.value.code == code


def test_readiness_resolution_never_uses_current_viewport_fallback():
    old = FakeHandle("old", box={"x": 10, "y": 300, "width": 100, "height": 80})
    fixed = '#one-column-item-0 [data-e2e="feed-video"]'
    page = FakeFrame(css={
        fixed: FakeLocator([old]),
        '[data-e2e="feed-video"]': FakeLocator([FakeHandle("current")]),
    })

    resolved = resolve(page, definition({"type": "css", "value": fixed, "priority": 10}))

    assert resolved.handle is old
    assert page.calls == [("locator", fixed)]
```

- [ ] **Step 3: Run locator tests and verify unsupported keyword failures**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_locator.py -q -p no:cacheprovider
```

Expected: new tests fail because resolver does not accept viewport options.

- [ ] **Step 4: Add allowlisted stable-attribute extraction**

In `execution_v2/locator.py`, add:

```python
import re

_STABLE_ATTRIBUTE = re.compile(
    r'''\[(?:data-e2e|data-testid|aria-label|name|placeholder|role|contenteditable)'''
    r'''\s*=\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\]'''
)


def _stable_css_chain(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(match.group(0) for match in _STABLE_ATTRIBUTE.finditer(value))
```

- [ ] **Step 5: Add viewport-aware normal validation and strict fallback**

Extend `resolve()` with the two Boolean keywords. When `require_in_viewport` is true, read the live viewport once:

```python
viewport = None
if require_in_viewport:
    viewport = await frame.evaluate(
        "({width: window.innerWidth, height: window.innerHeight})"
    )
```

Pass `viewport` to `_validate_candidate()`. After bounding-box validation, reject a normal candidate whose center is outside:

```python
if viewport is not None and not self._center_in_viewport(box, viewport):
    diagnostic["code"] = "locator_outside_viewport"
    return diagnostic, None, None
```

Add helpers:

```python
@staticmethod
def _center_in_viewport(box, viewport):
    center_x = float(box["x"]) + float(box["width"]) / 2
    center_y = float(box["y"]) + float(box["height"]) / 2
    return 0 <= center_x < float(viewport["width"]) and 0 <= center_y < float(viewport["height"])

async def _viewport_fallback(self, frame, candidates, viewport, *, require_editable):
    passing = []
    diagnostics = []
    saw_ambiguous = False
    seen_chains = set()
    for _, candidate in sorted(
        enumerate(candidates), key=lambda entry: (entry[1].get("priority", 0), entry[0])
    ):
        if candidate.get("type") != "css":
            continue
        chain = _stable_css_chain(candidate.get("value"))
        if not chain or chain in seen_chains:
            continue
        seen_chains.add(chain)
        locator = frame.locator(chain)
        handles = []
        for index in range(await locator.count()):
            handle = await locator.nth(index).element_handle()
            if handle is None or not await handle.is_visible() or await handle.is_disabled():
                continue
            box = await handle.bounding_box()
            if not self._has_area(box) or not self._center_in_viewport(box, viewport):
                continue
            if require_editable and not await handle.is_editable():
                continue
            handles.append((handle, box))
        diagnostic = {"locator_type": "css_viewport", "value": chain}
        if not handles:
            diagnostic["code"] = "current_viewport_target_not_found"
        elif len(handles) > 1:
            diagnostic["code"] = "current_viewport_target_ambiguous"
            saw_ambiguous = True
        else:
            diagnostic["code"] = "valid"
            passing.append((handles[0][0], handles[0][1]))
        diagnostics.append(diagnostic)
    if not passing:
        code = (
            "current_viewport_target_ambiguous"
            if saw_ambiguous
            else "current_viewport_target_not_found"
        )
        raise LocatorResolutionError(code, tuple(diagnostics))
    reference, box = passing[0]
    for handle, _ in passing[1:]:
        if not await self._same_handle(frame, reference, handle):
            raise LocatorResolutionError(
                "current_viewport_target_ambiguous", tuple(diagnostics)
            )
    return ResolvedElement(reference, "css_viewport", box, tuple(diagnostics))
```

When normal candidates produce no passing target and `allow_viewport_fallback` is true, return `_viewport_fallback(...)`; otherwise retain existing `no_valid_locator` behavior.

- [ ] **Step 6: Run locator tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_locator.py -q -p no:cacheprovider
```

Expected: all locator tests pass.

---

### Task 4: Opt action execution into live-viewport targeting and complete verification

**Files:**
- Modify: `execution_v2/actions.py:110-129`
- Modify: `actions_dom.py:24-33`
- Modify: `tests/test_execution_v2_actions.py`
- Modify: `tests/test_actions.py`

**Interfaces:**
- Action resolution calls `StrictLocatorResolver.resolve()` with `require_in_viewport=True` and `allow_viewport_fallback=True`.
- `get_viewport(page)` returns live `window.innerWidth/innerHeight` before falling back to `page.viewport_size`.

- [ ] **Step 1: Write action opt-in and live viewport tests**

In `tests/test_execution_v2_actions.py`, update `_Resolver.resolve()` to accept and record the new keywords, then assert in the move test:

```python
async def resolve(
    self,
    _page,
    definition,
    *,
    require_editable=False,
    require_in_viewport=False,
    allow_viewport_fallback=False,
):
    self.calls.append((
        definition["id"],
        require_editable,
        require_in_viewport,
        allow_viewport_fallback,
    ))
    handle, box = self.handles[definition["id"]]
    return ResolvedElement(handle, "css", box, ())
```

Expected move assertion:

```python
assert resolver.calls == [("button", False, True, True)]
```

Expected input assertion:

```python
assert resolver.calls == [("input", True, True, True)]
```

Add to `tests/test_actions.py`:

```python
def test_get_viewport_prefers_live_inner_size_after_window_resize():
    from actions_dom import get_viewport

    class Page:
        viewport_size = {"width": 1280, "height": 720}

        async def evaluate(self, expression):
            assert "window.innerWidth" in expression
            return {"width": 640, "height": 900}

    assert asyncio.run(get_viewport(Page())) == (640.0, 900.0)
```

- [ ] **Step 2: Run focused tests and verify failures**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py tests\test_actions.py -q -p no:cacheprovider
```

Expected: new assertions fail because actions do not pass viewport flags and `get_viewport()` returns the stale `viewport_size` value.

- [ ] **Step 3: Opt every V2 action element into current-viewport resolution**

In `execution_v2/actions.py`, change `_resolve()`:

```python
return await resolver.resolve(
    page,
    element["definition"],
    require_editable=require_editable,
    require_in_viewport=True,
    allow_viewport_fallback=True,
)
```

Readiness remains unchanged because it calls the resolver independently without these keywords.

- [ ] **Step 4: Prefer the browser's live inner viewport**

Replace `get_viewport()` in `actions_dom.py`:

```python
async def get_viewport(page) -> tuple[float, float]:
    viewport = None
    if hasattr(page, "evaluate"):
        try:
            viewport = await page.evaluate(
                "({width: window.innerWidth, height: window.innerHeight})"
            )
        except Exception:
            viewport = None
    if not viewport:
        viewport = getattr(page, "viewport_size", None)
    viewport = viewport or {"width": 1280, "height": 720}
    return max(float(viewport["width"]), 1), max(float(viewport["height"]), 1)
```

- [ ] **Step 5: Run focused and dependent tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py tests\test_execution_v2_locator.py tests\test_execution_v2_scheduler.py tests\test_execution_v2_tiling.py tests\test_actions.py tests\test_window_tiler.py -q -p no:cacheprovider
```

Expected: all tests pass.

- [ ] **Step 6: Run complete automated acceptance**

```powershell
& npm.cmd run test:node
$v2Tests = Get-ChildItem -LiteralPath tests -Filter 'test_execution_v2_*.py' | ForEach-Object { $_.FullName }
& .\.venv\Scripts\python.exe -m pytest @v2Tests tests\test_window_tiler.py -q -p no:cacheprovider
& .\.venv\Scripts\python.exe -m py_compile execution_v2\tiling.py execution_v2\models.py execution_v2\scheduler.py execution_v2\service.py execution_v2\locator.py execution_v2\actions.py actions_dom.py
node --check gateway\static\browser_v2.js
git diff --check -- execution_v2 gateway/static/browser_v2.js actions_dom.py tests/test_execution_v2_tiling.py tests/test_execution_v2_scheduler.py tests/test_execution_v2_locator.py tests/test_execution_v2_actions.py tests/test_actions.py tests-js/browser-v2-ui.test.js
```

Expected: all Node and Python tests pass; compile, syntax, and diff checks exit 0.

- [ ] **Step 7: Perform user-observed local acceptance after restart**

1. Restart Flask and press `Ctrl+F5` on `/browser-v2`.
2. Run the saved strategy with 2 Profiles and batch size 2.
3. Verify both windows occupy half the Windows work area and history contains successful wait, move, and scroll records for both Profiles.
4. Run with 3 Profiles and batch size 3.
5. Verify three equal-area, non-overlapping windows and that all close before any next batch starts.
6. If a target still fails, verify history reports its exact action index, action type, error code, and evidence screenshot.

- [ ] **Step 8: Record workspace state**

```powershell
git status --short -- execution_v2 gateway/static/browser_v2.js actions_dom.py tests tests-js/browser-v2-ui.test.js docs/superpowers/specs/2026-08-06-browser-v2-window-tiling-current-viewport-target-design.md docs/superpowers/plans/2026-08-06-browser-v2-window-tiling-current-viewport-target.md
```

Expected: changed and new paths are listed. Do not run `git add` or `git commit` because `.git` metadata is read-only.
