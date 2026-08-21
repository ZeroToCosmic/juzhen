# AdsPower Window and Page Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reliably tile selected AdsPower windows on the Windows primary work area and keep multi-window block strategies running when TikTok replaces the active page Tab.

**Architecture:** Keep one independent Playwright/CDP connection per AdsPower profile for navigation and the complete action run. Add a focused page-lifecycle component that reselects valid pages and retries only safe actions, plus deterministic CDP-port-to-process-to-window mapping before Win32 tiling.

**Tech Stack:** Python 3, Flask, Playwright async API, pywin32, Windows `netstat`, pytest.

## Global Constraints

- Support 1–8 selected AdsPower windows.
- Use only the Windows primary monitor work area; do not span monitors.
- Two windows must be left/right halves; four windows must be four equal cells.
- Bring tiled windows to the foreground once; never use persistent `HWND_TOPMOST`.
- Each profile owns an independent Playwright instance, browser connection, context and page state.
- Navigation and action execution must share one Playwright/CDP connection.
- Recover `move`, `scroll_up`, `scroll_down` and `pause` at most once after a closed-target error.
- A recovered scroll resumes its sampled action and still emits exactly the sampled `N` wheel calls in total; it never restarts the full scroll action.
- Never automatically retry `click` or `keyboard_input`.
- Never close a user-owned AdsPower browser during ordinary strategy testing.
- Do not change strategy persistence, elements, recorded patterns, Ghost Cursor, wheel-count semantics or statistics modules.
- Do not add a new runtime dependency.
- This directory is not a Git repository. Do not initialize Git or attempt commits during implementation.

---

## File Structure

- Create `browser_page_lifecycle.py`: page selection, target-close classification, safe action recovery and in-connection page preparation.
- Modify `browser_strategy_runtime.py`: use the lifecycle object per action and expose a combined prepare-and-run CDP entry point.
- Modify `gateway/app.py`: route normal and batch strategy execution through the combined entry point.
- Modify `window_tiler.py`: map CDP ports to browser processes/windows and retry foreground activation safely.
- Modify `tests/test_browser_strategy_runtime.py`: page replacement, retry policy and one-connection tests.
- Modify `tests/test_app.py`: route integration, per-window isolation and no duplicate preparation tests.
- Modify `tests/test_window_tiler.py`: exact process mapping, ambiguous candidate rejection and foreground retry tests.
- Modify `docs/superpowers/reports/2026-07-24-adspower-window-lifecycle-verification.md`: final automated and live verification evidence.

### Task 1: Page Lifecycle Component

**Files:**
- Create: `browser_page_lifecycle.py`
- Create: `tests/test_browser_page_lifecycle.py`

**Interfaces:**
- Produces: `CLOSED_TARGET_MARKERS: tuple[str, ...]`
- Produces: `SAFE_RETRY_ACTIONS: frozenset[str]`
- Produces: `is_closed_target_error(error: BaseException) -> bool`
- Produces: `page_origin(page) -> str`
- Produces: `PageLifecycle(context, target_url, *, timeout_seconds=3.0, sleep_fn=asyncio.sleep, monotonic_fn=time.monotonic)`
- Produces: `await PageLifecycle.resolve(current=None, *, allow_blank=False) -> page`
- Produces: `await PageLifecycle.execute(current, action, callback) -> tuple[page, Any, dict | None]`
- Produces: `await prepare_target_page(lifecycle, target_url, *, wait_milliseconds=2000) -> tuple[page, dict]`

- [ ] **Step 1: Write failing selection and recovery tests**

```python
# tests/test_browser_page_lifecycle.py
import asyncio
from types import SimpleNamespace

import pytest

from browser_page_lifecycle import (
    PageLifecycle,
    is_closed_target_error,
    prepare_target_page,
)


def run(coro):
    return asyncio.run(coro)


class FakePage:
    def __init__(self, url, *, closed=False, visible=True):
        self.url = url
        self._closed = closed
        self.visible = visible
        self.goto_calls = []
        self.close_calls = 0

    def is_closed(self):
        return self._closed

    async def evaluate(self, _expression):
        return "visible" if self.visible else "hidden"

    async def goto(self, url, **options):
        self.goto_calls.append((url, options))
        self.url = url

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def close(self):
        self._closed = True
        self.close_calls += 1


class FakeBrowser:
    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.browser = FakeBrowser()

    async def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


def test_resolve_prefers_visible_target_host_over_blank_and_other_host():
    blank = FakePage("about:blank")
    other = FakePage("https://example.com/", visible=True)
    hidden_target = FakePage("https://www.tiktok.com/a", visible=False)
    visible_target = FakePage("https://www.tiktok.com/b", visible=True)
    lifecycle = PageLifecycle(
        FakeContext([blank, other, hidden_target, visible_target]),
        "https://www.tiktok.com/",
    )

    assert run(lifecycle.resolve()) is visible_target


def test_resolve_replaces_closed_current_page_with_new_target_page():
    closed = FakePage("https://www.tiktok.com/old", closed=True)
    replacement = FakePage("https://www.tiktok.com/new")
    lifecycle = PageLifecycle(
        FakeContext([closed, replacement]),
        "https://www.tiktok.com/",
    )

    assert run(lifecycle.resolve(closed)) is replacement


def test_resolve_can_return_blank_page_only_when_preparing_navigation():
    blank = FakePage("about:blank")
    lifecycle = PageLifecycle(
        FakeContext([blank]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="no active page"):
        run(lifecycle.resolve())
    assert run(lifecycle.resolve(allow_blank=True)) is blank


def test_safe_action_rebinds_and_retries_exactly_once():
    first = FakePage("https://www.tiktok.com/old")
    replacement = FakePage("https://www.tiktok.com/new")
    context = FakeContext([first])
    lifecycle = PageLifecycle(context, "https://www.tiktok.com/")
    calls = []

    async def callback(page):
        calls.append(page)
        if page is first:
            first._closed = True
            context.pages.append(replacement)
            raise RuntimeError("Mouse.wheel: Target page, context or browser has been closed")
        return "ok"

    page, result, event = run(
        lifecycle.execute(first, {"id": "move-1", "type": "move"}, callback)
    )

    assert page is replacement
    assert result == "ok"
    assert calls == [first, replacement]
    assert event == {
        "action_id": "move-1",
        "action_type": "move",
        "old_page_origin": "https://www.tiktok.com",
        "new_page_origin": "https://www.tiktok.com",
        "retry": 1,
        "status": "recovered",
    }


@pytest.mark.parametrize("action_type", ["click", "keyboard_input"])
def test_side_effect_action_never_retries(action_type):
    page = FakePage("https://www.tiktok.com/")
    lifecycle = PageLifecycle(FakeContext([page]), "https://www.tiktok.com/")
    calls = []

    async def callback(current):
        calls.append(current)
        raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(RuntimeError, match="Target page"):
        run(
            lifecycle.execute(
                page,
                {"id": "side-effect", "type": action_type},
                callback,
            )
        )

    assert calls == [page]


def test_closed_target_classifier_does_not_retry_unrelated_errors():
    assert is_closed_target_error(
        RuntimeError("Locator.click: Target page, context or browser has been closed")
    )
    assert not is_closed_target_error(RuntimeError("element not found"))
```

- [ ] **Step 2: Run tests and verify import failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_page_lifecycle.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'browser_page_lifecycle'`.

- [ ] **Step 3: Implement page selection and safe retry**

```python
# browser_page_lifecycle.py
from __future__ import annotations

import asyncio
import inspect
import time
from urllib.parse import urlsplit


CLOSED_TARGET_MARKERS = (
    "target page, context or browser has been closed",
    "page closed",
    "context closed",
    "browser has been closed",
    "target closed",
    "target detached",
)
SAFE_RETRY_ACTIONS = frozenset(
    {"move", "scroll_up", "scroll_down", "pause"}
)
WHOLE_ACTION_RETRY_ACTIONS = frozenset({"move"})


def is_closed_target_error(error: BaseException) -> bool:
    message = str(error).casefold()
    return any(marker in message for marker in CLOSED_TARGET_MARKERS)


def page_origin(page) -> str:
    try:
        parsed = urlsplit(str(page.url or ""))
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class PageLifecycle:
    def __init__(
        self,
        context,
        target_url,
        *,
        timeout_seconds=3.0,
        sleep_fn=asyncio.sleep,
        monotonic_fn=time.monotonic,
    ):
        self.context = context
        self.target_url = str(target_url)
        self.target_host = (urlsplit(self.target_url).hostname or "").casefold()
        self.timeout_seconds = float(timeout_seconds)
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn

    def _browser_connected(self):
        browser = getattr(self.context, "browser", None)
        checker = getattr(browser, "is_connected", None)
        return browser is None or not callable(checker) or bool(checker())

    @staticmethod
    def _open_pages(pages):
        return [
            page for page in pages
            if not bool(getattr(page, "is_closed", lambda: False)())
        ]

    async def _visible(self, page):
        try:
            return (
                await page.evaluate("document.visibilityState")
            ) == "visible"
        except Exception:
            return False

    async def _pick(self, current=None, *, allow_blank=False):
        pages = self._open_pages(list(getattr(self.context, "pages", [])))
        if current in pages and page_origin(current):
            return current
        usable = [page for page in pages if page_origin(page)]
        target = [
            page for page in usable
            if (urlsplit(str(page.url)).hostname or "").casefold() == self.target_host
        ]
        candidates = target or usable
        for page in reversed(candidates):
            if await self._visible(page):
                return page
        if candidates:
            return candidates[-1]
        return pages[-1] if allow_blank and pages else None

    async def resolve(self, current=None, *, allow_blank=False):
        deadline = self.monotonic_fn() + self.timeout_seconds
        while True:
            if not self._browser_connected():
                raise RuntimeError("browser disconnected")
            page = await self._pick(current, allow_blank=allow_blank)
            if page is not None:
                return page
            if self.monotonic_fn() >= deadline:
                raise RuntimeError("no active page available before timeout")
            await self.sleep_fn(0.1)
            current = None

    async def execute(self, current, action, callback):
        current = await self.resolve(current)
        try:
            result = callback(current)
            if inspect.isawaitable(result):
                result = await result
            return current, result, None
        except Exception as error:
            if (
                action["type"] not in WHOLE_ACTION_RETRY_ACTIONS
                or not is_closed_target_error(error)
            ):
                raise
            old_origin = page_origin(current)
            replacement = await self.resolve(None)
            result = callback(replacement)
            if inspect.isawaitable(result):
                result = await result
            return replacement, result, {
                "action_id": action["id"],
                "action_type": action["type"],
                "old_page_origin": old_origin,
                "new_page_origin": page_origin(replacement),
                "retry": 1,
                "status": "recovered",
            }


async def prepare_target_page(
    lifecycle,
    target_url,
    *,
    wait_milliseconds=2000,
):
    page = await lifecycle.resolve(allow_blank=True)
    closed_tabs = 0
    for other in list(lifecycle.context.pages):
        if other is not page and not other.is_closed():
            await other.close()
            closed_tabs += 1
    await page.goto(target_url, wait_until="commit", timeout=30_000)
    if wait_milliseconds > 0:
        await page.wait_for_timeout(wait_milliseconds)
    page = await lifecycle.resolve(page)
    for other in list(lifecycle.context.pages):
        if other is not page and not other.is_closed():
            await other.close()
            closed_tabs += 1
    return page, {
        "target_url": target_url,
        "current_url": str(page.url or ""),
        "closed_tabs": closed_tabs,
        "stages": [
            {"stage": "wait_for_cdp", "status": "ok"},
            {"stage": "close_other_tabs", "status": "ok", "closed_tabs": closed_tabs},
            {
                "stage": "navigate",
                "status": "ok",
                "target_url": target_url,
                "current_url": str(page.url or ""),
            },
        ],
    }
```

- [ ] **Step 4: Run lifecycle tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_page_lifecycle.py -q
```

Expected: all tests pass.

### Task 2: Dynamic Strategy Page Binding

**Files:**
- Modify: `browser_actions.py:95-207`
- Modify: `browser_strategy_runtime.py:67-180`
- Modify: `tests/test_browser_actions.py`
- Modify: `tests/test_browser_strategy_runtime.py`

**Interfaces:**
- Consumes: `PageLifecycle.execute(current, action, callback)`
- Produces: optional `page_lifecycle` parameter on `run_block_strategy(...)`
- Produces: optional `page_lifecycle` parameter on `execute_action(...)`
- Produces: result field `page_recoveries: list[dict]`
- Produces: `run_prepared_block_strategy_on_cdp(ws_url, target_url, strategy, elements, patterns, text_resolver) -> dict`

- [ ] **Step 1: Add failing tests for action-boundary rebinding and one CDP connection**

Append tests that:

```python
def test_strategy_uses_replacement_page_after_pause():
    first = object()
    second = object()
    pages = [first, second]
    calls = []

    class Lifecycle:
        async def execute(self, current, item, callback):
            selected = pages.pop(0)
            result = await callback(selected)
            event = (
                {"action_id": item["id"], "status": "recovered"}
                if selected is second else None
            )
            return selected, result, event

    async def execute(page, item, *_args, **_kwargs):
        calls.append((page, item["id"]))
        return {"action_id": item["id"], "status": "ok"}

    result = run(
        run_block_strategy(
            first,
            strategy(action("pause"), action("scroll", "scroll_down")),
            {},
            [],
            lambda _item: "",
            execute_fn=execute,
            page_lifecycle=Lifecycle(),
        )
    )

    assert calls == [(first, "pause"), (second, "scroll")]
    assert result["page_recoveries"] == [
        {"action_id": "scroll", "status": "recovered"}
    ]
```

Add a fake Playwright test for `run_prepared_block_strategy_on_cdp` asserting:

```python
assert events.count(("connect", "ws://profile", 10_000)) == 1
assert events.count(("stop",)) == 1
assert ("forbidden", "browser.close") not in events
assert prepared_before_actions is True
```

Add an exact-count scroll recovery test:

```python
def test_scroll_recovery_resumes_without_duplicating_wheel_count():
    first = FakePageThatClosesOnSecondWheel()
    second = FakePage()
    lifecycle = FakeLifecycle(replacement=second)
    item = action(
        "scroll",
        "scroll_down",
        total_count=[3, 3],
        burst_count=[1, 1],
        interval_seconds=[0, 0],
    )

    result = run(
        execute_action(
            first,
            item,
            {},
            {},
            lambda _item: "",
            page_lifecycle=lifecycle,
            sleep_fn=no_sleep,
        )
    )

    assert first.mouse.wheel_calls == [(0, 120)]
    assert second.mouse.wheel_calls == [(0, 120), (0, 120)]
    assert result["count"] == 3
    assert result["_active_page"] is second
    assert result["_page_recoveries"][0]["retry"] == 1
```

- [ ] **Step 2: Run focused runtime tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py -q
```

Expected: failures report missing `page_lifecycle` and missing `run_prepared_block_strategy_on_cdp`.

- [ ] **Step 3: Make scroll recovery resume the remaining wheel count**

Add `page_lifecycle=None` to `execute_action`. In the scroll loop, retry only the failed wheel call:

```python
from browser_page_lifecycle import (
    is_closed_target_error,
    page_origin,
)

recoveries = []
recovery_used = False
while completed < total_count:
    burst_total += 1
    for _ in range(min(burst_count, total_count - completed)):
        try:
            await page.mouse.wheel(0, direction * distance)
        except Exception as error:
            if (
                page_lifecycle is None
                or recovery_used
                or not is_closed_target_error(error)
            ):
                raise
            old_origin = page_origin(page)
            page = await page_lifecycle.resolve(None)
            recovery_used = True
            await page.mouse.wheel(0, direction * distance)
            recoveries.append({
                "action_id": action["id"],
                "action_type": action_type,
                "old_page_origin": old_origin,
                "new_page_origin": page_origin(page),
                "retry": 1,
                "status": "recovered",
            })
        completed += 1
        if completed < total_count:
            await sleep_fn(float(rng.uniform(*params["interval_seconds"])))
return {
    **result,
    "distance": distance,
    "count": completed,
    "burst_count": burst_count,
    "burst_total": burst_total,
    "_active_page": page,
    "_page_recoveries": recoveries,
}
```

This samples `total_count` once and increments `completed` only after a successful wheel call.

- [ ] **Step 4: Route each action through the lifecycle**

In `run_block_strategy`, add `page_lifecycle=None`, initialize `page_recoveries = []`, and replace the direct `execute_fn` call with:

```python
async def invoke(selected_page):
    action_result = await execute_fn(
        selected_page,
        action,
        elements,
        pattern_map,
        text_resolver,
        rng=rng,
        sleep_fn=sleep_fn,
        page_lifecycle=page_lifecycle,
    )
    return action_result

if page_lifecycle is None:
    result = await invoke(page)
else:
    page, result, recovery = await page_lifecycle.execute(
        page, action, invoke
    )
    if recovery:
        recovery["action_index"] = index
        page_recoveries.append(recovery)

page = result.pop("_active_page", page)
for recovery in result.pop("_page_recoveries", []):
    recovery["action_index"] = index
    page_recoveries.append(recovery)
```

Return:

```python
{
    "status": "ok",
    "strategy_id": normalized["id"],
    "run_mode": normalized["run_mode"],
    "cycles": cycles,
    "sampled_duration_minutes": sampled_duration,
    "actions": results,
    "page_recoveries": page_recoveries,
}
```

- [ ] **Step 5: Add combined prepare-and-run entry point**

Add:

```python
async def _run_prepared_block_strategy_on_cdp(
    ws_url,
    target_url,
    strategy,
    elements,
    patterns,
    text_resolver,
):
    _validate_strategy(strategy, elements, patterns)
    from playwright.async_api import async_playwright
    from browser_page_lifecycle import PageLifecycle, prepare_target_page

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(
            ws_url, timeout=10_000
        )
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError(
                "ws.puppeteer did not return an operable browser context"
            )
        context = contexts[0]
        if not context.pages:
            await context.new_page()
        lifecycle = PageLifecycle(context, target_url)
        page, prepared = await prepare_target_page(lifecycle, target_url)
        executed = await run_block_strategy(
            page,
            strategy,
            elements,
            patterns,
            text_resolver,
            page_lifecycle=lifecycle,
        )
        prepared["stages"].append(
            {"stage": "execute_actions", "status": "ok"}
        )
        return {**executed, **prepared}
    finally:
        await playwright.stop()


def run_prepared_block_strategy_on_cdp(
    ws_url,
    target_url,
    strategy,
    elements,
    patterns,
    text_resolver,
):
    return asyncio.run(
        _run_prepared_block_strategy_on_cdp(
            ws_url,
            target_url,
            strategy,
            elements,
            patterns,
            text_resolver,
        )
    )
```

Export the new public function in `__all__`. Keep `run_block_strategy_on_cdp` for compatibility.

- [ ] **Step 6: Run runtime tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_page_lifecycle.py tests/test_browser_strategy_runtime.py tests/test_browser_actions.py -q
```

Expected: all tests pass, including exact wheel-count tests.

### Task 3: Gateway Uses One Connection Per Profile

**Files:**
- Modify: `gateway/app.py:3888-3987`
- Modify: `gateway/app.py:4823-4953`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `run_prepared_block_strategy_on_cdp(...)`
- Preserves: `ensure_browser_profile_sessions(..., lease_sessions=True)`
- Preserves: per-profile result fields and public secret redaction.

- [ ] **Step 1: Add failing route tests**

Add one normal execution test and one batch test that monkeypatch:

```python
monkeypatch.setattr(
    app_module,
    "prepare_browser_page",
    lambda *_args: pytest.fail("separate page preparation is forbidden"),
)

calls = []

def fake_combined(ws_url, target_url, strategy, elements, patterns, resolver):
    calls.append((ws_url, target_url, strategy["id"]))
    return {
        "status": "ok",
        "actions": [],
        "page_recoveries": [],
        "current_url": target_url,
        "closed_tabs": 1,
        "stages": [
            {"stage": "navigate", "status": "ok"},
            {"stage": "execute_actions", "status": "ok"},
        ],
    }

monkeypatch.setattr(
    "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
    fake_combined,
)
```

Assert two selected profiles produce two calls with distinct `ws_url` values and both results remain successful.

Add a mixed-result test where one `ws_url` raises `RuntimeError("browser disconnected")`; assert the other profile still returns `status == "ok"`.

- [ ] **Step 2: Run focused gateway tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -q
```

Expected: new tests fail because routes still call `prepare_browser_page` and `run_block_strategy_on_cdp` separately.

- [ ] **Step 3: Replace separate preparation in normal execution**

Inside `execute_browser_strategy_route.execute_one`, replace:

```python
prepared = prepare_browser_page(ws_url, target_url)
result = run_block_strategy_on_cdp(...)
```

with:

```python
from browser_strategy_runtime import run_prepared_block_strategy_on_cdp

result = run_prepared_block_strategy_on_cdp(
    ws_url,
    target_url,
    strategy,
    elements,
    patterns,
    build_strategy_text_resolver(app.config["CONTENT_DATA_DIR"]),
)
```

Build the response from `result["current_url"]`, `result["closed_tabs"]`, `result["stages"]`, `result["actions"]` and `result["page_recoveries"]`. Do not append a second duplicate `execute_actions` stage.

Use this response shape:

```python
return {
    **result,
    "profile_id": profile_id,
    "status": "ok",
    "stage": "execute_actions",
    "attempts": attempts,
    "target_url": target_url,
}
```

- [ ] **Step 4: Replace separate preparation in batch execution**

Make the same change in `run_browser_batch_task.run_one`. Preserve:

```python
release_browser_session_results(
    session_results, request_close=True
)
```

for batch-only close behavior.

Use this batch result shape:

```python
return {
    **result,
    "profile_id": item["profile_id"],
    "status": "ok",
    "stage": "execute_actions",
}
```

- [ ] **Step 5: Run gateway and lease tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_browser_routes.py -q
```

Expected: all tests pass; ordinary execution releases leases without requesting browser close, while batch execution still requests close.

### Task 4: Exact CDP-Port-to-Window Mapping

**Files:**
- Modify: `window_tiler.py:136-240`
- Modify: `tests/test_window_tiler.py`

**Interfaces:**
- Produces: `debug_port_from_ws_url(ws_url: str) -> int`
- Produces: `listening_pid_for_port(port: int, *, run_command=subprocess.run) -> int | None`
- Produces: `process_family(root_pid: int) -> set[int]`
- Changes: `find_browser_windows(...)` must map each hint independently and reject ambiguity.

- [ ] **Step 1: Add failing mapping tests**

```python
def test_debug_port_is_parsed_from_adspower_ws_url():
    assert window_tiler.debug_port_from_ws_url(
        "ws://127.0.0.1:55001/devtools/browser/abc"
    ) == 55001


def test_find_browser_windows_maps_each_profile_by_debug_port_pid(monkeypatch):
    windows = [
        window_tiler.BrowserWindow(101, "TikTok - SunBrowser", 9001, "sunbrowser.exe"),
        window_tiler.BrowserWindow(102, "TikTok - SunBrowser", 9002, "sunbrowser.exe"),
    ]
    monkeypatch.setattr(window_tiler, "list_visible_windows", lambda: windows)
    monkeypatch.setattr(
        window_tiler,
        "listening_pid_for_port",
        lambda port: {55001: 9002, 55002: 9001}[port],
    )
    monkeypatch.setattr(window_tiler, "process_family", lambda pid: {pid})

    matched, missing = window_tiler.find_browser_windows(
        [
            {"profile_id": "a", "ws_puppeteer": "ws://127.0.0.1:55001/devtools/browser/a"},
            {"profile_id": "b", "ws_puppeteer": "ws://127.0.0.1:55002/devtools/browser/b"},
        ],
        timeout=0,
    )

    assert [item.hwnd for item in matched] == [102, 101]
    assert missing == []


def test_find_browser_windows_rejects_ambiguous_unmapped_candidates(monkeypatch):
    monkeypatch.setattr(
        window_tiler,
        "list_visible_windows",
        lambda: [
            window_tiler.BrowserWindow(101, "SunBrowser", 1, "sunbrowser.exe"),
            window_tiler.BrowserWindow(102, "SunBrowser", 2, "sunbrowser.exe"),
        ],
    )
    monkeypatch.setattr(window_tiler, "listening_pid_for_port", lambda _port: None)

    matched, missing = window_tiler.find_browser_windows(
        [{"profile_id": "a", "ws_puppeteer": "ws://127.0.0.1:55001/devtools/browser/a"}],
        timeout=0,
    )

    assert matched == []
    assert missing == ["a: window mapping ambiguous"]
```

- [ ] **Step 2: Run mapping tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_window_tiler.py -q
```

Expected: failures report missing parser/PID functions and current arbitrary fallback behavior.

- [ ] **Step 3: Implement debug-port and listening-PID lookup**

Use `urlsplit` for the port. Execute Windows `netstat -ano -p tcp` with:

```python
creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
completed = run_command(
    ["netstat", "-ano", "-p", "tcp"],
    capture_output=True,
    text=True,
    check=False,
    creationflags=creationflags,
)
```

For each output line, split on whitespace, parse the local-address final `:<port>`, and accept the final field only when it is an integer PID. Return the unique PID for the requested listening port; return `None` for zero or multiple PIDs.

```python
from urllib.parse import urlsplit
import subprocess


def debug_port_from_ws_url(ws_url: str) -> int:
    parsed = urlsplit(str(ws_url or ""))
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1", "localhost", "::1"
    }:
        raise ValueError("AdsPower CDP endpoint must use a local ws URL")
    if not parsed.port:
        raise ValueError("AdsPower CDP endpoint has no debug port")
    return int(parsed.port)


def listening_pid_for_port(port: int, *, run_command=subprocess.run) -> int | None:
    completed = run_command(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    matches = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4 or not fields[-1].isdigit():
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if local_port == int(port):
            matches.add(int(fields[-1]))
    return next(iter(matches)) if len(matches) == 1 else None
```

- [ ] **Step 4: Implement Windows process-family lookup**

Use `ctypes` Toolhelp snapshot with `TH32CS_SNAPPROCESS = 0x00000002` and `PROCESSENTRY32W`. Build `parent_pid -> child_pid` relationships, then breadth-first collect `root_pid` and all descendants. Always close the snapshot handle in `finally`. On non-Windows or API failure, return `{root_pid}`.

```python
def process_family(root_pid: int) -> set[int]:
    if os.name != "nt":
        return {int(root_pid)}
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if snapshot == invalid_handle:
        return {int(root_pid)}
    children = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        available = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            parent = int(entry.th32ParentProcessID)
            children.setdefault(parent, set()).add(int(entry.th32ProcessID))
            available = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)

    family = {int(root_pid)}
    pending = [int(root_pid)]
    while pending:
        for child in children.get(pending.pop(), set()):
            if child not in family:
                family.add(child)
                pending.append(child)
    return family
```

- [ ] **Step 5: Replace arbitrary fallback**

For each hint:

1. Parse `ws_puppeteer`.
2. Resolve listening PID and process family.
3. Select unused visible SunBrowser/Chromium windows whose `process_id` belongs to that family.
4. Accept only one exact process candidate.
5. If no process candidate, accept a title/profile match only when unique.
6. If all remaining browser candidates contain exactly one window, accept it.
7. Otherwise append `"<profile_id>: window mapping ambiguous"` and do not move a window.

Add `"sunbrowser"` to `_is_browser_window`.

Replace per-hint selection with:

```python
def precise_candidate(hint, available, used):
    remaining = [item for item in available if item.hwnd not in used]
    try:
        port = debug_port_from_ws_url(hint.get("ws_puppeteer", ""))
        root_pid = listening_pid_for_port(port)
    except (TypeError, ValueError, OSError):
        root_pid = None
    if root_pid:
        family = process_family(root_pid)
        exact = [item for item in remaining if item.process_id in family]
        if len(exact) == 1:
            return exact[0], ""
        if len(exact) > 1:
            return None, "window mapping ambiguous"
    titled = [item for item in remaining if _matches(item, hint)]
    if len(titled) == 1:
        return titled[0], ""
    if len(remaining) == 1:
        return remaining[0], ""
    return None, "window mapping ambiguous"
```

- [ ] **Step 6: Run all tiler tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_window_tiler.py -q
```

Expected: all tests pass; existing 2/4-window rectangles remain unchanged.

### Task 5: Foreground Retry Without Persistent Topmost

**Files:**
- Modify: `window_tiler.py:244-395`
- Modify: `tests/test_window_tiler.py`

**Interfaces:**
- Produces: `_activate_window(win32gui, window, *, user32=None, kernel32=None) -> dict[str, str]`
- Preserves: `SetWindowPos(..., HWND_TOP, ..., SWP_NOACTIVATE | SWP_SHOWWINDOW)`
- Forbids: `HWND_TOPMOST`.

- [ ] **Step 1: Add failing foreground retry test**

```python
def test_activate_window_retries_with_temporary_thread_input_attachment(monkeypatch):
    calls = []
    attempts = {"count": 0}

    def set_foreground(hwnd):
        attempts["count"] += 1
        calls.append(("foreground", hwnd, attempts["count"]))
        if attempts["count"] == 1:
            raise RuntimeError("foreground denied")

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda hwnd: calls.append(("top", hwnd)),
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd, _pid: {999: 11, 101: 22}[hwnd],
        AttachThreadInput=lambda source, target, attach: calls.append(
            ("attach", source, target, bool(attach))
        ) or 1,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == "ok-after-retry"
    assert ("attach", 33, 11, True) in calls
    assert ("attach", 33, 11, False) in calls
    assert attempts["count"] == 2
```

Add a test that inspects every `SetWindowPos` call and asserts:

```python
assert insert_after == win32con.HWND_TOP
assert not hasattr(win32con, "HWND_TOPMOST") or insert_after != win32con.HWND_TOPMOST
```

- [ ] **Step 2: Run focused foreground tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_window_tiler.py -q
```

Expected: the retry test fails because `_activate_window` has no injected Windows API parameters and no retry.

- [ ] **Step 3: Implement safe foreground retry**

Attempt `BringWindowToTop` and `SetForegroundWindow` normally. If `SetForegroundWindow` fails:

1. Read the current foreground window.
2. Obtain the foreground thread ID.
3. Obtain the current thread ID.
4. Call `AttachThreadInput(current_thread, foreground_thread, True)`.
5. Retry `BringWindowToTop` and `SetForegroundWindow`.
6. In `finally`, call `AttachThreadInput(current_thread, foreground_thread, False)`.

Return `ok`, `ok-after-retry`, `unsupported` or `failed: <message>` for each operation. Never leave thread input attached after success or failure.

```python
def _activate_window(
    win32gui,
    window: BrowserWindow,
    *,
    user32=None,
    kernel32=None,
) -> dict[str, str]:
    import ctypes

    user32 = user32 or ctypes.windll.user32
    kernel32 = kernel32 or ctypes.windll.kernel32
    result = {}
    try:
        win32gui.BringWindowToTop(window.hwnd)
        result["bring_to_top"] = "ok"
    except Exception as error:
        result["bring_to_top"] = f"failed: {error}"
    try:
        win32gui.SetForegroundWindow(window.hwnd)
        result["set_foreground"] = "ok"
        return result
    except Exception as first_error:
        foreground = win32gui.GetForegroundWindow()
        foreground_thread = user32.GetWindowThreadProcessId(
            foreground, None
        )
        current_thread = kernel32.GetCurrentThreadId()
        attached = False
        try:
            attached = bool(
                user32.AttachThreadInput(
                    current_thread, foreground_thread, True
                )
            )
            win32gui.BringWindowToTop(window.hwnd)
            win32gui.SetForegroundWindow(window.hwnd)
            result["set_foreground"] = "ok-after-retry"
        except Exception as retry_error:
            result["set_foreground"] = (
                f"failed: {first_error}; retry failed: {retry_error}"
            )
        finally:
            if attached:
                user32.AttachThreadInput(
                    current_thread, foreground_thread, False
                )
        return result
```

- [ ] **Step 4: Run tiler tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_window_tiler.py -q
```

Expected: all tests pass.

### Task 6: Error Isolation and Sanitized Lifecycle Logs

**Files:**
- Modify: `gateway/app.py:3316-3420`
- Modify: `gateway/app.py:4823-4953`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: result field `page_recoveries`.
- Produces: browser operation log containing profile/action/origin/retry data.
- Preserves: `public_browser_payload(...)` redaction before response and logging.

- [ ] **Step 1: Add failing log-isolation test**

Use two ready sessions. Make the first combined runner return a recovery event and the second raise a closed-browser error. Assert:

```python
assert results[0]["status"] == "ok"
assert results[0]["page_recoveries"][0]["retry"] == 1
assert results[1]["status"] == "failed"
assert results[1]["profile_id"] == "profile-2"
assert "browser disconnected" in results[1]["error"]

log_text = log_path.read_text(encoding="utf-8")
assert "profile-1" in log_text
assert "scroll-1" in log_text
assert "api-key" not in log_text.casefold()
assert "devtools/browser" not in log_text.casefold()
```

- [ ] **Step 2: Run focused test**

Run the exact new test with:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -k "lifecycle and log" -q
```

Expected: fail until combined results and lifecycle events are retained by the route.

- [ ] **Step 3: Preserve lifecycle events in public results**

Keep `page_recoveries` inside each per-profile result. Run the full response through `public_browser_payload` before `record_browser_log`. Do not add raw exceptions, CDP URLs, cookies or API credentials to event data.

- [ ] **Step 4: Run app tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_browser_routes.py -q
```

Expected: all tests pass.

### Task 7: Full Verification and Real AdsPower Smoke Test

**Files:**
- Create: `docs/superpowers/reports/2026-07-24-adspower-window-lifecycle-verification.md`

**Interfaces:**
- Consumes: completed implementation and user-selected AdsPower profiles.
- Produces: evidence report; does not alter saved strategy data.

- [ ] **Step 1: Run focused Python suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_page_lifecycle.py tests/test_browser_strategy_runtime.py tests/test_browser_actions.py tests/test_window_tiler.py tests/test_browser_routes.py tests/test_app.py -q
```

Expected: all pass.

- [ ] **Step 2: Run full Python suite**

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: all pass.

- [ ] **Step 3: Run full Node suite**

```powershell
npm test -- --runInBand
```

If the package test script does not accept `--runInBand`, run:

```powershell
npm test
```

Expected: all pass.

- [ ] **Step 4: Start the application through the normal hidden launcher**

Use the existing launcher path. Confirm the dashboard responds and no extra command windows remain visible. Do not change launcher behavior.

- [ ] **Step 5: Perform user-authorized two-profile smoke test**

In the dashboard:

1. Select exactly two disposable/test AdsPower profiles.
2. Open them with “打开窗口”.
3. Verify measured rectangles are left and right halves of the primary work area.
4. Switch to another application and verify browsers do not remain persistently topmost.
5. Execute a strategy containing `pause` followed by `scroll_down`.
6. Verify both profiles execute independently.
7. Verify the configured random wheel count equals the number of emitted wheel calls reported for each profile.
8. Verify both AdsPower windows remain open after ordinary strategy execution.
9. If TikTok replaces a Tab, verify `page_recoveries` records the replacement and the safe action continues.

- [ ] **Step 6: Write verification report**

Record:

- exact commands and pass counts;
- Windows work-area dimensions;
- selected profile IDs masked except final four characters;
- requested and actual window rectangles;
- foreground result per window;
- action counts and page-recovery events;
- any live-test limitation.

Do not claim real AdsPower success if the user has not selected test profiles or AdsPower is unavailable.
