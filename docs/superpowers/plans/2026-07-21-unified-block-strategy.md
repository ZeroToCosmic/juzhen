# Unified Block Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicate manual/automatic browser strategy systems with one persistent linear block builder, reusable behavior patterns, reliable migration, and AdsPower recording.

**Architecture:** Store elements, patterns, and versioned block strategies as separate persisted browser resources. Validate and migrate them in a focused configuration module, execute every block through the existing Python Playwright action layer, and expose one strategy list/detail UI. Record only normalized mouse motion and keyboard timing in the selected AdsPower page through CDP-injected controls.

**Tech Stack:** Python 3, Flask, Playwright async API, raw CDP client, vanilla JavaScript, pytest, Node test runner.

## Global Constraints

- Saving must survive page refresh, page close, and Flask process restart.
- “Saved” may appear only after backend validation and atomic disk write succeed.
- Mouse recordings contain normalized motion and timing only; keyboard recordings contain timing only.
- Never persist typed characters, passwords, clipboard data, page text, or absolute screen coordinates.
- Replay scales to the current content viewport and clamps every point inside it.
- Only blocks visible in a strategy execute, in visible order.
- `pause` means wait and continue; it never terminates a strategy.
- Each strategy selects `once` or `loop`; loop mode respects its sampled total duration.
- Legacy manual and automatic strategies migrate once and remain as read-only fallback data.
- The gateway uses one Python execution path; Node browser actions do not become a second strategy runtime.
- Run Python tests with `python -m pytest ... -p no:cacheprovider` because the workspace contains an inaccessible `work/pytest-tmp` directory.
- Git metadata is currently absent. Each commit step uses a conditional PowerShell command and prints `SKIP: no Git repository` until the user restores a repository.

---

## File Map

- Create `browser_strategy_config.py`: action catalog, normalization, reference checks, and idempotent legacy migration.
- Modify `gateway/settings_store.py`: defaults for schema version, patterns, and block strategies.
- Modify `gateway/app.py`: separate persistence APIs, unified execute/batch routes, recording routes, and new page markup.
- Modify `actions_dom.py`: parameterized mouse, click, typing, scrolling, and pause primitives.
- Modify `browser_actions.py`: six-block validation and async dispatch.
- Rewrite `browser_strategy_runtime.py`: once/loop orchestration and CDP Playwright connection; retain `build_batches`.
- Create `browser_pattern_recorder.py`: inject, query, finish, validate, and normalize recording data.
- Create `gateway/static/browser_strategy_ui.js`: strategy list/detail editor, resource saving, dirty state, and recording controls.
- Create `tests/test_browser_strategy_config.py`: schema and migration tests.
- Create `tests/test_browser_pattern_recorder.py`: recording and normalization tests.
- Modify `tests/test_actions.py`: parameterized DOM action tests.
- Modify `tests/test_browser_strategy_runtime.py`: replace old fixed-flow tests with unified block execution tests.
- Modify `tests/test_settings_routes.py`: independent persistence and restart-read tests.
- Modify `tests/test_app.py`: unified execution, batch, recording, and failure result tests.
- Modify `tests/test_console.py`: new UI structure and removal of legacy controls.
- Replace `tests-js/browser-auto-element-options.test.js` with `tests-js/browser-strategy-ui.test.js`: frontend persistence, selector sync, list/editor, and dirty-state tests.

---

### Task 1: Versioned Strategy Configuration and Migration

**Files:**
- Create: `browser_strategy_config.py`
- Modify: `gateway/settings_store.py:31-170`
- Create: `tests/test_browser_strategy_config.py`

**Interfaces:**
- Produces: `ACTION_CATALOG: dict[str, dict]`
- Produces: `normalize_elements(value) -> dict[str, str]`
- Produces: `normalize_patterns(value) -> list[dict]`
- Produces: `normalize_block_strategies(value, elements, patterns, *, allow_repair=False) -> list[dict]`
- Produces: `load_or_migrate_strategy_state(browser) -> tuple[dict, bool]`
- Produces: `element_references(strategies, alias) -> list[dict]`
- Produces: `pattern_references(strategies, pattern_id) -> list[dict]`

- [ ] **Step 1: Write failing schema and migration tests**

```python
from browser_strategy_config import (
    ACTION_CATALOG,
    load_or_migrate_strategy_state,
    normalize_block_strategies,
    normalize_patterns,
)


def test_catalog_contains_only_six_visible_blocks():
    assert list(ACTION_CATALOG) == [
        "move", "click", "scroll_up", "scroll_down", "keyboard_input", "pause"
    ]


def test_keyboard_pattern_rejects_recorded_text():
    with pytest.raises(ValueError, match="不能包含输入内容"):
        normalize_patterns([{
            "id": "keys", "name": "节奏", "type": "keyboard",
            "data": {"intervals_ms": [80, 120], "text": "secret"},
        }])


def test_legacy_auto_strategy_migrates_to_six_ordered_blocks_once():
    browser = {
        "action_elements": {"entry": "//entry", "input": "//input", "submit": "//submit"},
        "action_strategies": [],
        "auto_strategies": [{
            "id": "comment", "name": "评论", "total_duration_minutes": [3, 5],
            "stay_seconds": [3, 10], "scrolls_per_round": [1, 3],
            "scroll_interval_seconds": [1, 3], "scroll_threshold": [30, 50],
            "pause_seconds": [3, 10], "scroll_distance": 600, "batch_size": 4,
            "entry_element": "entry", "input_element": "input",
            "submit_element": "submit", "comment_brand_id": "brand-a",
        }],
    }
    migrated, changed = load_or_migrate_strategy_state(browser)
    second, changed_again = load_or_migrate_strategy_state(migrated)

    assert changed is True
    assert changed_again is False
    assert [item["type"] for item in migrated["block_strategies"][0]["actions"]] == [
        "pause", "scroll_down", "pause", "click", "keyboard_input", "click"
    ]
    assert second["block_strategies"] == migrated["block_strategies"]
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m pytest tests/test_browser_strategy_config.py -q -p no:cacheprovider`

Expected: collection fails with `ModuleNotFoundError: No module named 'browser_strategy_config'`.

- [ ] **Step 3: Implement catalog, normalizers, references, and migration**

Use stable action defaults:

```python
ACTION_CATALOG = {
    "move": {"label": "移动", "pattern_type": "mouse"},
    "click": {"label": "点击", "pattern_type": "mouse"},
    "scroll_up": {"label": "向上滚动", "pattern_type": None},
    "scroll_down": {"label": "向下滚动", "pattern_type": None},
    "keyboard_input": {"label": "键盘输入", "pattern_type": "keyboard"},
    "pause": {"label": "停止（等待）", "pattern_type": None},
}

DEFAULT_ACTION_PARAMS = {
    "move": {"target_mode": "element", "element": "", "delta_viewport": [0.0, 0.0],
             "trajectory": {"source": "builtin", "id": "bezier"},
             "duration_seconds": [0.2, 0.8]},
    "click": {"element": "", "button": "left", "click_count": 1,
              "hold_seconds": [0.05, 0.15],
              "trajectory": {"source": "builtin", "id": "bezier"}},
    "scroll_up": {"distance": 600, "total_count": [1, 1], "burst_count": [1, 1],
                  "interval_seconds": [0.1, 0.3]},
    "scroll_down": {"distance": 600, "total_count": [1, 1], "burst_count": [1, 1],
                    "interval_seconds": [0.1, 0.3]},
    "keyboard_input": {"element": "", "content": {"source": "fixed", "text": "", "brand_id": ""},
                       "typing": {"source": "builtin", "interval_ms": [50, 250]}},
    "pause": {"duration_seconds": [1.0, 1.0]},
}
```

`load_or_migrate_strategy_state()` must copy the browser dictionary, return existing version-2 data unchanged, otherwise migrate every legacy manual and auto strategy, set `strategy_schema_version=2`, and retain legacy keys untouched. Missing element references set `status="needs_repair"` and `repair_errors`; valid strategies set `status="ready"`.

- [ ] **Step 4: Add browser defaults**

Add to `DEFAULT_SETTINGS["browser"]`:

```python
"strategy_schema_version": 0,
"action_elements": {},
"interaction_patterns": [],
"block_strategies": [],
```

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_browser_strategy_config.py tests/test_settings_store.py -q -p no:cacheprovider`

Expected: all selected tests pass.

- [ ] **Step 6: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add browser_strategy_config.py gateway/settings_store.py tests/test_browser_strategy_config.py; git commit -m "feat: add unified strategy schema" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 2: Independent Persistent Resource APIs

**Files:**
- Modify: `gateway/app.py:5316-5792`
- Modify: `tests/test_settings_routes.py:800-900`

**Interfaces:**
- Consumes: Task 1 normalizers and reference helpers.
- Produces: `GET/PUT /api/browser/elements`
- Produces: `GET/PUT /api/browser/patterns`
- Produces: `GET/PUT /api/browser/strategies`
- Produces: `load_persisted_strategy_state() -> dict`

- [ ] **Step 1: Write failing persistence and reference-integrity tests**

```python
def test_elements_save_survives_new_app_instance(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    first = create_app().test_client()
    saved = first.put("/api/browser/elements", json={"elements": {"入口": "//button"}})
    second = create_app().test_client()

    assert saved.status_code == 200
    assert second.get("/api/browser/elements").get_json()["elements"] == {"入口": "//button"}
    assert json.loads(config_path.read_text(encoding="utf-8"))["browser"]["action_elements"] == {"入口": "//button"}


def test_referenced_element_delete_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()
    client.put("/api/browser/elements", json={"elements": {"entry": "//entry"}})
    client.put("/api/browser/strategies", json={"strategies": [{
        "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
        "actions": [{"id": "a", "type": "click", "params": {"element": "entry"}}],
    }]})
    response = client.put("/api/browser/elements", json={"elements": {}})

    assert response.status_code == 409
    assert response.get_json()["references"] == [{"strategy_id": "s", "action_id": "a", "index": 1}]
```

- [ ] **Step 2: Run tests and verify 404 failures**

Run: `python -m pytest tests/test_settings_routes.py -k "elements_save_survives or referenced_element" -q -p no:cacheprovider`

Expected: both tests fail because `/api/browser/elements` does not exist.

- [ ] **Step 3: Implement migrated-state loader and three APIs**

Add a helper that persists migration only when needed:

```python
def load_persisted_strategy_state():
    settings = load_settings()
    browser, changed = load_or_migrate_strategy_state(settings.get("browser", {}))
    if changed:
        settings = merge_saved_settings({"browser": browser})
        browser = settings["browser"]
    return browser
```

For every PUT route: reject non-object bodies, normalize submitted data, compare removed IDs with current references, call `merge_saved_settings()` once, and return values from its result. Element rename accepts `rename_from`; it rewrites matching `params.element` references in the same atomic update. Pattern deletion rejects referenced IDs with HTTP 409. Strategy PUT rejects duplicate IDs and invalid references with HTTP 400.

- [ ] **Step 4: Preserve unrelated configuration in route tests**

Extend the existing `assert_existing_config_is_preserved()` coverage so each new PUT proves model keys, R2 credentials, AdsPower settings, and unrelated browser settings remain unchanged.

- [ ] **Step 5: Run API and settings tests**

Run: `python -m pytest tests/test_settings_routes.py tests/test_browser_strategy_config.py -q -p no:cacheprovider`

Expected: all selected tests pass, including restart reads and 409 reference conflicts.

- [ ] **Step 6: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add gateway/app.py tests/test_settings_routes.py; git commit -m "fix: persist browser strategy resources" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 3: Parameterized DOM Primitives and Six-Block Dispatcher

**Files:**
- Modify: `actions_dom.py`
- Modify: `browser_actions.py`
- Modify: `tests/test_actions.py`

**Interfaces:**
- Consumes: Task 1 action and pattern shapes.
- Produces: `human_move_to(page, x, y, *, duration_seconds, pattern, rng, sleep_fn)`
- Produces: `human_click(page, selector, *, button, click_count, hold_seconds, trajectory, patterns, rng, sleep_fn)`
- Produces: `human_type(page, text, *, timing, patterns, rng, sleep_fn)`
- Produces: `execute_action(page, action, elements, patterns, text_resolver, *, rng, sleep_fn)`

- [ ] **Step 1: Write failing primitive and dispatch tests**

```python
class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, character, **_kwargs):
        self.page.typed.append(character)
        self.page.input_text += character


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    async def focus(self):
        self.page.focused.append(self.selector)

    async def input_value(self):
        return self.page.input_text


class FakeAsyncPage:
    def __init__(self):
        self.typed = []
        self.clicked = []
        self.focused = []
        self.input_text = ""
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector):
        return FakeLocator(self, selector.removeprefix("xpath="))


def record_async_wait(values):
    async def wait(seconds):
        values.append(seconds)
    return wait


async def fixed_text_resolver(action):
    return action["params"]["content"]["text"]


def test_human_type_uses_pattern_without_storing_characters(monkeypatch):
    page = FakeAsyncPage()
    waits = []
    timing = {"source": "pattern", "pattern_id": "keys"}
    patterns = {"keys": {"type": "keyboard", "data": {"intervals_ms": [80, 120], "hold_ms": [20, 30]}}}

    asyncio.run(human_type(page, "abc", timing=timing, patterns=patterns,
                           rng=random.Random(1), sleep_fn=record_async_wait(waits)))

    assert page.typed == ["a", "b", "c"]
    assert all(0.05 <= value <= 0.14 for value in waits)
    assert "text" not in patterns["keys"]["data"]


def test_keyboard_input_focuses_without_clicking():
    page = FakeAsyncPage()
    result = asyncio.run(execute_action(
        page,
        {"id": "type", "type": "keyboard_input", "params": {
            "element": "input", "content": {"source": "fixed", "text": "hello"},
            "typing": {"source": "builtin", "interval_ms": [0, 0]},
        }},
        {"input": "//textarea"}, {}, fixed_text_resolver,
    ))

    assert page.focused == ["//textarea"]
    assert page.clicked == []
    assert result["text"] == "hello"
```

- [ ] **Step 2: Run tests and verify signature/behavior failures**

Run: `python -m pytest tests/test_actions.py -q -p no:cacheprovider`

Expected: new tests fail because pattern parameters and `keyboard_input` are unsupported.

- [ ] **Step 3: Parameterize existing functions without duplicating runtimes**

Keep async Playwright primitives. Add injectable random/sleep dependencies for deterministic tests. Mouse pattern replay transforms normalized samples from the current pointer to the requested endpoint; relative moves scale `delta_viewport` by current viewport. Clamp coordinates to `[0, width-1]` and `[0, height-1]`.

Typing pattern selection must use a random contiguous segment when enough samples exist. When text is longer, restart from a random offset and apply bounded ±10% jitter. Built-in typing samples from `interval_ms`.

- [ ] **Step 4: Replace validation with registered block parameters**

`validate_action_config()` becomes a compatibility wrapper around Task 1 normalization. `execute_action()` dispatches exactly six types. Scroll actions execute until sampled `total_count`, sleep between events, and respect sampled `burst_count`. Pause samples `duration_seconds`. Each result contains `action_id`, `type`, `status`, `element`, and type-specific measurements.

- [ ] **Step 5: Run action tests**

Run: `python -m pytest tests/test_actions.py -q -p no:cacheprovider`

Expected: all action tests pass.

- [ ] **Step 6: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add actions_dom.py browser_actions.py tests/test_actions.py; git commit -m "feat: execute configurable action blocks" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 4: Unified Once/Loop Runtime and Gateway Execution

**Files:**
- Rewrite: `browser_strategy_runtime.py`
- Modify: `gateway/app.py:4520-4625,5445-5906`
- Rewrite: `tests/test_browser_strategy_runtime.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: Task 2 persisted state and Task 3 `execute_action`.
- Produces: `run_block_strategy(page, strategy, elements, patterns, text_resolver, *, rng, sleep_fn, monotonic_fn, execute_fn=execute_action) -> dict`
- Produces: `run_block_strategy_on_cdp(ws_url, strategy, elements, patterns, text_resolver) -> dict`
- Produces: `BlockExecutionError(action_id, action_index, action_type, reason)`
- Preserves: `build_batches(items, batch_size) -> list[list]`

- [ ] **Step 1: Write failing once, loop, order, and failure tests**

```python
def test_once_strategy_runs_visible_actions_once_in_order():
    calls = []
    class FakePage:
        pass
    async def resolver(_action):
        return ""
    def action(action_id, action_type):
        return {"id": action_id, "type": action_type, "params": {"duration_seconds": [0, 0]}}
    async def record_action(_page, item, _elements, _patterns, _resolver, **_kwargs):
        calls.append(item["id"])
        return {"action_id": item["id"], "type": item["type"], "status": "ok"}
    result = asyncio.run(run_block_strategy(
        FakePage(),
        {"id": "s", "run_mode": "once", "actions": [
            action("a", "pause"), action("b", "pause"), action("c", "pause")
        ]},
        {"entry": "//entry"}, {}, resolver,
        execute_fn=record_action,
    ))
    assert calls == ["a", "b", "c"]
    assert result["cycles"] == 1


def test_action_failure_stops_only_current_window(monkeypatch, tmp_path):
    from browser_strategy_runtime import BlockExecutionError

    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings({"browser": {
        "strategy_schema_version": 2,
        "action_elements": {"entry": "//entry"},
        "interaction_patterns": [],
        "block_strategies": [{
            "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
            "actions": [{"id": "broken", "type": "click", "params": {"element": "entry"}}],
        }],
    }})
    client = create_app().test_client()
    monkeypatch.setattr("gateway.app.ensure_browser_profile_sessions", lambda profiles, lease_sessions: ([
        {"profile_id": "one", "status": "ready", "stage": "ready", "attempts": 1, "ws_url": "ws://one"},
        {"profile_id": "two", "status": "ready", "stage": "ready", "attempts": 1, "ws_url": "ws://two"},
    ], {"layout": [], "missing": []}))
    monkeypatch.setattr("gateway.app.prepare_browser_page", lambda _ws, url: {
        "current_url": url, "closed_tabs": 0, "stages": []
    })
    def fake_run(ws_url, *_args, **_kwargs):
        if ws_url == "ws://one":
            raise BlockExecutionError("broken", 1, "click", "boom")
        return {"status": "ok", "actions": []}
    monkeypatch.setattr("browser_strategy_runtime.run_block_strategy_on_cdp", fake_run)
    response = client.post("/api/browser/execute-strategy", json={
        "strategy_id": "s", "windows": [{"profile_id": "one"}, {"profile_id": "two"}]
    })
    assert [item["status"] for item in response.get_json()["results"]] == ["failed", "ok"]
    assert response.get_json()["results"][0]["action_id"] == "broken"
```

- [ ] **Step 2: Run focused tests and verify failures**

Run: `python -m pytest tests/test_browser_strategy_runtime.py tests/test_app.py -k "once_strategy or action_failure_stops" -q -p no:cacheprovider`

Expected: tests fail because the old runtime accepts fixed automatic strategies only.

- [ ] **Step 3: Implement async once/loop orchestration**

`run_block_strategy()` validates before executing. Once mode runs one full action array. Loop mode samples one deadline per window and starts another cycle only while `monotonic_fn() < deadline`; an action already started may finish. Wrap failures with action ID, one-based index, type, and original reason.

`run_block_strategy_on_cdp()` uses `async_playwright()`, `chromium.connect_over_cdp()`, the first context/page, and the same `run_block_strategy()` path. Always close Playwright in `finally` without closing the user’s AdsPower browser.

- [ ] **Step 4: Replace dual execute branches and batch runner**

`POST /api/browser/execute-strategy` loads only `block_strategies`, rejects `needs_repair`, prepares each page, and invokes `run_block_strategy_on_cdp`. Remove `auto:` routing. Batch tasks load the same strategy and call the same runtime. Retain per-profile leases, tiling, navigation, tab cleanup, sanitization, and logging.

- [ ] **Step 5: Run runtime and browser route tests**

Run: `python -m pytest tests/test_browser_strategy_runtime.py tests/test_app.py tests/test_browser_routes.py -q -p no:cacheprovider`

Expected: selected suites pass; result payloads identify failing blocks precisely.

- [ ] **Step 6: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add browser_strategy_runtime.py gateway/app.py tests/test_browser_strategy_runtime.py tests/test_app.py; git commit -m "feat: unify browser strategy execution" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 5: AdsPower Behavior Pattern Recorder

**Files:**
- Create: `browser_pattern_recorder.py`
- Create: `tests/test_browser_pattern_recorder.py`
- Modify: `gateway/app.py:3994-4002,5316-6085`
- Modify: `tests/test_app.py`

**Interfaces:**
- Produces: `prepare_recording(ws_url, recording_id, pattern_type) -> dict`
- Produces: `read_recording(ws_url, recording_id) -> dict`
- Produces: `finish_recording(ws_url, recording_id) -> dict`
- Produces: `normalize_recording_sample(raw) -> dict`
- Produces: recording prepare/status/finish API routes.

- [ ] **Step 1: Write failing normalization and route tests**

```python
def test_mouse_recording_normalizes_viewport_and_drops_absolute_coordinates():
    sample = normalize_recording_sample({
        "type": "mouse", "viewport": {"width": 1000, "height": 500},
        "points": [{"x": 100, "y": 50, "dt_ms": 0}, {"x": 600, "y": 300, "dt_ms": 90}],
    })
    assert sample["points"] == [
        {"x_ratio": 0.1, "y_ratio": 0.1, "dt_ms": 0},
        {"x_ratio": 0.6, "y_ratio": 0.6, "dt_ms": 90},
    ]
    assert "x" not in sample["points"][0]


def test_keyboard_recording_never_returns_keys_or_text():
    sample = normalize_recording_sample({
        "type": "keyboard", "events": [
            {"key": "s", "interval_ms": 80, "hold_ms": 20},
            {"key": "e", "interval_ms": 120, "hold_ms": 30},
        ],
    })
    assert sample == {"intervals_ms": [80, 120], "hold_ms": [20, 30], "sample_count": 2}
```

- [ ] **Step 2: Run tests and verify module failure**

Run: `python -m pytest tests/test_browser_pattern_recorder.py -q -p no:cacheprovider`

Expected: collection fails with `ModuleNotFoundError: No module named 'browser_pattern_recorder'`.

- [ ] **Step 3: Implement CDP-injected recorder**

Use `CdpClient.page_session()` plus `Runtime.evaluate`. Store page state under a recording-ID-specific key. Inject a shadow-DOM floating control with “开始录制” and “结束录制”. Mouse mode listens to `pointermove`; keyboard mode listens to `keydown`/`keyup` and stores only timestamps and opaque event sequence numbers. Filter events whose composed path contains the recorder host. `Ctrl+Shift+F10` stops recording; this shortcut and all control events are excluded.

`finish_recording()` reads the stopped data, removes listeners and the overlay, deletes page state, validates minimum samples, and normalizes before returning. Navigation or missing state returns `录制上下文已失效`.

- [ ] **Step 4: Add recording session routes**

Keep `ACTIVE_PATTERN_RECORDINGS` and a lock beside existing browser session state. Prepare accepts exactly one open profile and `mouse|keyboard`, injects controls, stores `{profile_id, ws_url, type}`, then releases the temporary lease. Status and finish reacquire the matching active session and reject changed/missing CDP sessions. Finish returns a temporary sample; saving its name and data uses Task 2 `/api/browser/patterns`.

- [ ] **Step 5: Run recorder and route tests**

Run: `python -m pytest tests/test_browser_pattern_recorder.py tests/test_app.py -k "recording or pattern" -q -p no:cacheprovider`

Expected: normalization, privacy, lifecycle, closed-window, navigation-loss, and short-sample tests pass.

- [ ] **Step 6: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add browser_pattern_recorder.py gateway/app.py tests/test_browser_pattern_recorder.py tests/test_app.py; git commit -m "feat: record browser behavior patterns" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 6: Strategy List and Independent Block Editor Markup

**Files:**
- Modify: `gateway/app.py:1139-1218,1880-2090`
- Modify: `tests/test_console.py:650-700`

**Interfaces:**
- Produces DOM sections: `browser-elements-manager`, `browser-pattern-library`, `browser-strategy-list-view`, `browser-strategy-editor-view`.
- Produces editor controls consumed by Task 7 JavaScript.

- [ ] **Step 1: Replace old console assertions with failing new-layout assertions**

```python
def test_dashboard_has_one_block_strategy_manager():
    page = create_app().test_client().get("/").data.decode("utf-8")
    assert 'id="browser-strategy-list-view"' in page
    assert 'id="browser-strategy-editor-view"' in page
    assert 'id="browser-block-palette"' in page
    assert 'id="browser-pattern-library"' in page
    assert "策略管理" not in page
    assert 'id="browser-action-add"' not in page
    assert 'id="browser-auto-strategy-manager"' not in page
    assert 'id="browser-auto-entry-element"' not in page
```

- [ ] **Step 2: Run UI structure test and verify failure**

Run: `python -m pytest tests/test_console.py::test_dashboard_has_one_block_strategy_manager -q -p no:cacheprovider`

Expected: fails because the old manual and nine-step automatic sections remain.

- [ ] **Step 3: Implement confirmed layout A**

Keep element management at the top. Add pattern cards and record buttons. Add strategy cards showing name, mode, action count, repair/save state, and create button. Add a hidden independent editor view with back, rename, delete, save, once/loop, duration, batch size, block palette, ordered action cards, and a type-specific parameter dialog.

Remove old manual select/add controls, old save/load controls, old nine-step automatic editor, and their CSS. Include `<script src="/static/browser_strategy_ui.js"></script>` after shared helpers.

- [ ] **Step 4: Run console tests**

Run: `python -m pytest tests/test_console.py -q -p no:cacheprovider`

Expected: all console tests pass with no legacy strategy controls.

- [ ] **Step 5: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add gateway/app.py tests/test_console.py; git commit -m "feat: add block strategy management layout" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 7: Frontend Persistence, List/Editor State, and Recording Controls

**Files:**
- Create: `gateway/static/browser_strategy_ui.js`
- Create: `tests-js/browser-strategy-ui.test.js`
- Delete: `tests-js/browser-auto-element-options.test.js`
- Modify: `gateway/app.py:2096-3895`

**Interfaces:**
- Consumes: Tasks 2, 5, and 6 APIs/DOM.
- Produces: `window.BrowserStrategyUI.init()`.
- Produces testable helpers: `syncElementOptions`, `serializeStrategyForm`, `markDirty`, `saveElements`, `saveStrategy`, `pollRecording`.

- [ ] **Step 1: Write failing frontend persistence tests**

```javascript
const {createBrowserStrategyUI} = require("../gateway/static/browser_strategy_ui.js");

test("element dialog closes only after backend persistence succeeds", async () => {
  const calls = [];
  const ui = createBrowserStrategyUI({fetchJson: async (url, options) => {
    calls.push([url, JSON.parse(options.body)]);
    return {elements: {entry: "//entry"}};
  }});
  await ui.saveElements({entry: "//entry"});
  assert.deepEqual(calls, [["/api/browser/elements", {elements: {entry: "//entry"}}]]);
  assert.equal(ui.state.elements.entry, "//entry");
  assert.equal(ui.elementDialog.open, false);
});

test("failed strategy save keeps dirty editor and never says saved", async () => {
  const ui = createBrowserStrategyUI({fetchJson: async () => { throw new Error("磁盘写入失败"); }});
  ui.markDirty();
  await assert.rejects(ui.saveStrategy(), /磁盘写入失败/);
  assert.equal(ui.state.dirty, true);
  assert.notEqual(ui.status.textContent, "已保存");
});
```

- [ ] **Step 2: Run Node test and verify missing module failure**

Run: `node --test tests-js/browser-strategy-ui.test.js`

Expected: fails because `gateway/static/browser_strategy_ui.js` does not exist.

- [ ] **Step 3: Implement one frontend state controller**

On init, load elements, patterns, strategies, action catalog, and brands. Render strategy list first. Opening a card switches to the editor without navigating away from the panel. Adding a palette item creates an ID and default params from the catalog. Actions support edit/up/down/delete. Only relevant fields render for each type.

Element add/edit/delete always awaits `/api/browser/elements`; only a successful response replaces `state.elements`, closes the dialog, and rebuilds every target select. Strategy save awaits `/api/browser/strategies`; only its response clears dirty state. Pattern save and delete follow the same rule. Install `beforeunload` only while dirty.

- [ ] **Step 4: Implement recording UI flow**

Prepare recording for exactly one selected AdsPower window. Poll status every 500 ms while armed or recording. On stopped status, call finish, show sample count/duration, require a non-empty name, then append and PUT the complete pattern list. Cancel discards the temporary sample. Show CDP/window/navigation errors without creating a pattern.

- [ ] **Step 5: Remove legacy inline functions and listeners**

Delete `browserActionConfig`, `browserAutoStrategies`, both old render/save flows, fixed entry/input/submit synchronizers, and old event listeners from `gateway/app.py`. Keep only shared browser-window selection and execution helpers; initialize the new external controller once.

- [ ] **Step 6: Run Node and console tests**

Run: `npm run test:node`

Expected: all Node tests pass, including new element persistence, selector synchronization, dirty warning, ordered blocks, and recording flow tests.

Run: `python -m pytest tests/test_console.py -q -p no:cacheprovider`

Expected: all console tests pass.

- [ ] **Step 7: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add gateway/static/browser_strategy_ui.js tests-js/browser-strategy-ui.test.js tests-js/browser-auto-element-options.test.js gateway/app.py; git commit -m "feat: build persistent strategy editor" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 8: Remove Legacy Write/Execute Surfaces and Verify Migration Compatibility

**Files:**
- Modify: `gateway/app.py:5445-5906`
- Modify: `browser_strategy_runtime.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_settings_routes.py`

**Interfaces:**
- Consumes: unified APIs and runtime from Tasks 1-7.
- Removes active use of `/api/browser/action-config`, `/api/browser/auto-strategies`, and `/api/browser/auto-strategies/generate`.
- Keeps legacy configuration keys only as migration inputs/read-only fallback.

- [ ] **Step 1: Write failing legacy-removal and migration endpoint tests**

```python
def test_legacy_strategy_routes_are_not_active_writers():
    client = create_app().test_client()
    assert client.get("/api/browser/action-config").status_code == 410
    assert client.put("/api/browser/action-config", json={}).status_code == 410
    assert client.get("/api/browser/auto-strategies").status_code == 410
    assert client.put("/api/browser/auto-strategies", json={}).status_code == 410


def test_first_unified_read_migrates_and_persists_legacy_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"browser": {
        "action_elements": {"entry": "//entry", "input": "//input", "submit": "//submit"},
        "auto_strategies": [{
            "id": "legacy", "entry_element": "entry", "input_element": "input",
            "submit_element": "submit", "total_duration_minutes": [3, 5],
        }],
    }}), encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    response = create_app().test_client().get("/api/browser/strategies")
    persisted = json.loads(config_path.read_text(encoding="utf-8"))

    assert response.status_code == 200
    assert persisted["browser"]["strategy_schema_version"] == 2
    assert persisted["browser"]["block_strategies"]
    assert persisted["browser"]["auto_strategies"]
```

- [ ] **Step 2: Run focused tests and verify old routes still write**

Run: `python -m pytest tests/test_app.py tests/test_settings_routes.py -k "legacy_strategy_routes or first_unified_read" -q -p no:cacheprovider`

Expected: legacy writer test fails because current routes still return 200.

- [ ] **Step 3: Retire legacy APIs safely**

All legacy action-config and auto-strategy GET/PUT/generation routes return HTTP 410 with a migration message. Remove their execution branches and batch dependency. Keep only migration readers in `browser_strategy_config.py`. Remove unused fixed-auto runtime functions and imports while retaining any compatibility normalizer used exclusively by migration tests.

- [ ] **Step 4: Update old tests to assert unified behavior**

Replace fixed-auto API and UI expectations with migration assertions, one runtime, one selector source, and explicit 410 responses. Preserve unrelated AdsPower/session/navigation regression tests.

- [ ] **Step 5: Run app and settings suites**

Run: `python -m pytest tests/test_app.py tests/test_settings_routes.py tests/test_browser_strategy_runtime.py -q -p no:cacheprovider`

Expected: all selected tests pass.

- [ ] **Step 6: Commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add gateway/app.py browser_strategy_runtime.py tests/test_app.py tests/test_settings_routes.py; git commit -m "refactor: retire duplicate strategy systems" } else { Write-Output "SKIP: no Git repository" }
```

---

### Task 9: Full Verification and Persistence Acceptance

**Files:**
- Modify only files requiring corrections exposed by full verification.

**Interfaces:**
- Verifies all prior tasks as one system.

- [ ] **Step 1: Run focused strategy suites**

Run:

```powershell
python -m pytest tests/test_browser_strategy_config.py tests/test_actions.py tests/test_browser_strategy_runtime.py tests/test_browser_pattern_recorder.py tests/test_settings_routes.py tests/test_console.py tests/test_app.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 2: Run complete Python suite**

Run: `python -m pytest tests -q -p no:cacheprovider`

Expected: all Python tests pass with zero failures.

- [ ] **Step 3: Run complete Node suite**

Run: `npm run test:node`

Expected: all Node tests pass with zero failures.

- [ ] **Step 4: Verify syntax**

Run:

```powershell
python -m py_compile actions_dom.py browser_actions.py browser_strategy_config.py browser_strategy_runtime.py browser_pattern_recorder.py gateway/app.py gateway/settings_store.py
```

Expected: command exits 0 with no output.

- [ ] **Step 5: Perform isolated persistence round trip**

Run a temporary-config test through pytest, not the user’s `config.json`:

```powershell
python -m pytest tests/test_settings_routes.py -k "survives_new_app_instance" -q -p no:cacheprovider
```

Expected: element, pattern, and strategy restart-read tests all pass.

- [ ] **Step 6: Inspect legacy UI and active route references**

Run:

```powershell
rg -n "browser-action-add|browser-auto-strategy-manager|browser-auto-entry-element|auto:" gateway tests tests-js
```

Expected: no product-code matches; test matches only where asserting absence or migration input.

- [ ] **Step 7: Final commit checkpoint when Git exists**

```powershell
if (Test-Path .git) { git add actions_dom.py browser_actions.py browser_strategy_config.py browser_strategy_runtime.py browser_pattern_recorder.py gateway/app.py gateway/settings_store.py gateway/static/browser_strategy_ui.js tests tests-js; git commit -m "feat: deliver unified block strategies" } else { Write-Output "SKIP: no Git repository" }
```
