# Browser V2 Arrow-Key Video Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace unreliable wheel replay with one verified `ArrowDown` or `ArrowUp` press per TikTok video switch.

**Architecture:** Keep stored action type `scroll` for compatibility, but execute it through Playwright keyboard input and existing feed identity checks. Remove calibration from job preflight and management UI while retaining backend calibration code and SQLite history untouched.

**Tech Stack:** Python 3.13, asyncio, Playwright, Flask, vanilla JavaScript, pytest, Node test runner.

## Global Constraints

- One requested switch sends exactly one direction-key press.
- No wheel, burst, retry, next-button fallback, Windows input, or new dependency.
- `direction=down` maps to `ArrowDown`; `direction=up` maps to `ArrowUp`.
- Existing strategy type `scroll`, action IDs, `count`, and `interval_seconds` remain compatible.
- Existing wheel calibration routes, tables, and records are retained but unused.
- One Profile failure does not stop other Profiles.

---

### Task 1: Verified arrow-key execution

**Files:**
- Modify: `execution_v2/actions.py`
- Test: `tests/test_execution_v2_actions.py`

**Interfaces:**
- Consumes: `capture_feed_state(page)` and `wait_for_stable_changed_state(page, before, timeout=8.0, sleep_fn=sleep)` from `browser_video_switch.py`.
- Produces: `execute_arrow_key_switches(page, direction, requested, interval_range, rng, sleep_fn) -> dict[str, Any]`.

- [x] **Step 1: Replace wheel mocks with failing keyboard tests**

Add a fake `page.keyboard.press()` recorder and `page.evaluate()` recorder. Test:

```python
assert page.keyboard.presses == ["ArrowDown"]
assert result["requested_switches"] == 1
assert result["completed_switches"] == 1
assert "calibration_revision" not in result
assert "wheel_events" not in result
```

Add `direction="up"` assertion for `ArrowUp`. Add failure test where identity never changes; assert one press and `video_switch_not_observed`.

- [x] **Step 2: Run focused tests and confirm failure**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py -q -p no:cacheprovider
```

Expected: FAIL because scroll still calls `execute_calibrated_switches`.

- [x] **Step 3: Implement minimal keyboard helper**

Before each press, capture current feed and run:

```python
await page.evaluate("""() => {
  const active = document.activeElement;
  if (active && (active.matches('input, textarea, select') || active.isContentEditable)) {
    active.blur();
  }
}""")
await page.keyboard.press("ArrowDown" if direction == "down" else "ArrowUp")
```

Wait for stable changed identity. If absent, raise `VideoSwitchError("video_switch_not_observed", requested_switches=requested, completed_switches=completed, switches=records)`. On success, append one sanitized `{from, to}` record. Sleep configured interval only between successful switches.

- [x] **Step 4: Rewire `_scroll()`**

Call `execute_arrow_key_switches()` and return only `direction`, `count`, `interval_seconds`, `requested_switches`, and `completed_switches`. Keep outer `execute_action(..., wheel_calibration=None)` signature temporarily for executor compatibility, but ignore its calibration argument.

- [x] **Step 5: Run focused tests**

Run Task 1 command. Expected: PASS.

---

### Task 2: Remove calibration execution dependency

**Files:**
- Modify: `execution_v2/service.py`
- Test: `tests/test_execution_v2_service.py`
- Test: `tests/test_execution_v2_integration.py`

**Interfaces:**
- Consumes: existing `scroll` strategy schema.
- Produces: job snapshots without `wheel_calibration`.

- [x] **Step 1: Write failing no-calibration job test**

Create a strategy containing one `scroll` action without publishing any wheel calibration. Start job and assert:

```python
job = store.get_job("job-1")
assert "wheel_calibration" not in job["strategy_snapshot"]
assert job["status"] in {"queued", "running", "completed"}
```

- [x] **Step 2: Run service tests and confirm failure**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_service.py tests\test_execution_v2_integration.py -q -p no:cacheprovider
```

Expected: FAIL with `wheel_calibration_missing`.

- [x] **Step 3: Delete only job preflight block**

Remove the `_start_job()` branch that calls `store.get_wheel_calibration()` and inserts `snapshot["wheel_calibration"]`. Keep calibration service methods and shutdown cleanup unchanged.

- [x] **Step 4: Run service tests**

Run Task 2 command. Expected: PASS after updating obsolete missing-calibration expectation.

---

### Task 3: Retire calibration UI and rename action

**Files:**
- Modify: `gateway/templates/browser_v2.html`
- Modify: `gateway/static/browser_v2.js`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: existing action editor and picker UI.
- Produces: “切换视频” editor without calibration controls or requests.

- [x] **Step 1: Write failing UI source tests**

Assert template and JavaScript exclude `v2-wheel-calibration`, `/wheel-calibration`, and “滚轮校准”. Assert palette contains `data-action-type="scroll">切换视频` and action label is `切换视频`.

- [x] **Step 2: Run UI tests and confirm failure**

```powershell
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: FAIL because calibration controls and “滚动” remain.

- [x] **Step 3: Remove calibration markup and JavaScript wiring**

Remove calibration button/card, calibration state, GET polling, render function, start/cancel functions, and event bindings. Do not modify backend routes. Change palette and `ACTIONS.scroll.label` to “切换视频”. Change help text to “每次发送一个方向键，并在确认视频切换后继续。”

- [x] **Step 4: Run UI tests**

Run Task 3 command. Expected: PASS.

---

### Task 4: Regression and real acceptance

**Files:**
- Modify only files listed above if a test identifies a defect.

- [x] **Step 1: Run V2 Python regression**

```powershell
$tests = rg --files tests | Where-Object { $_ -match 'test_execution_v2_|test_browser_video_switch' }
& .\.venv\Scripts\python.exe -m pytest $tests -q -p no:cacheprovider
```

Expected: PASS.

- [x] **Step 2: Run V2 frontend regression**

```powershell
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: PASS.

- [x] **Step 3: Run syntax and diff checks**

```powershell
& .\.venv\Scripts\python.exe -m py_compile execution_v2\actions.py execution_v2\service.py
git diff --check
```

Expected: exit code 0. Current `.git` metadata is read-only, so no stage or commit step runs.

- [ ] **Step 4: Run real acceptance**

Restart launcher. Run one-switch strategy on one Profile, then two Profiles with batch size 2. Expected: each requested switch produces one direction-key press and one stable video identity change; no wheel calibration required; both windows close after completion.
