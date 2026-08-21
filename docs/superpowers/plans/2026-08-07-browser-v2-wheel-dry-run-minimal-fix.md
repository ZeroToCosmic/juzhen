# Browser V2 Wheel Dry-Run Minimal Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish wheel calibration only after one synthetic wheel event demonstrably switches exactly one TikTok video.

**Architecture:** Keep current calibration API and session lifecycle. Extend `WheelCalibrationRunner.collect()` with bounded single-event Dry-Run candidates, mark persisted versions replay-validated, and reject legacy rows during execution preflight.

**Tech Stack:** Python 3.13, asyncio, Playwright, SQLite, Flask, vanilla JavaScript, pytest, Node test runner.

## Global Constraints

- Candidate multipliers are exactly `1.0`, `1.5`, `2.0`, `3.0`.
- One candidate means one `page.mouse.wheel(0, delta_y)` call; no burst.
- Runtime failure never sends a second event.
- Existing HTTP routes and request bodies stay unchanged.
- Legacy calibration rows default to `replay_validated=false` and cannot execute.
- No unrelated refactor or new dependency.

---

### Task 1: Single-event Dry-Run

**Files:**
- Modify: `execution_v2/wheel_calibration.py`
- Test: `tests/test_execution_v2_wheel_calibration.py`

**Interfaces:**
- Consumes: `normalize_wheel_samples(samples)`, `capture_feed_state(page)`, `observe_single_transition(...)`.
- Produces: `dry_run_wheel_calibration(page, normalized, progress, cancel_event, sleep_fn) -> dict[str, Any]` returning one validated event plus `replay_validated=True`.

- [x] **Step 1: Write failing candidate tests**

Add tests proving `100` fails, `150` switches once, exactly two wheel calls occur; first-candidate success makes one call; all candidates fail with `wheel_calibration_replay_not_observed`; multi-video result stops immediately.

- [x] **Step 2: Run focused test and verify failure**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py -q -p no:cacheprovider
```

Expected: new tests fail because Dry-Run function does not exist.

- [x] **Step 3: Implement bounded candidate loop**

Use representative total `sum(event["delta_y"] for event in normalized["events"])`. For each multiplier emit progress containing `status="dry_run"`, `candidate_index`, `candidate_multiplier`, `candidate_delta_y`, and accumulated public results. Move to feed center, send one event, observe for at most 8 seconds. Continue only for `wheel_calibration_video_not_changed`; propagate multiple-video/context errors. Return:

```python
{
    "direction": "down",
    "events": [{"delta_x": 0.0, "delta_y": candidate, "delta_mode": 0, "delay_ms": 0.0}],
    "sample_count": 3,
    "replay_validated": True,
}
```

If all four miss, raise `WheelCalibrationError("wheel_calibration_replay_not_observed")`.

- [x] **Step 4: Call Dry-Run inside `WheelCalibrationRunner.collect()` before cleanup**

Replace direct `return normalize_wheel_samples(samples)` with normalization followed by awaited Dry-Run. Preserve current `finally: await self.cleanup(page)`.

- [x] **Step 5: Run focused tests**

Run Task 1 command. Expected: PASS.

---

### Task 2: Persist only replay-validated versions

**Files:**
- Modify: `execution_v2/store.py`
- Modify: `execution_v2/service.py`
- Test: `tests/test_execution_v2_store.py`
- Test: `tests/test_execution_v2_service.py`

**Interfaces:**
- Consumes: Dry-Run result field `replay_validated: bool`.
- Produces: `publish_wheel_calibration(..., replay_validated: bool)` and `get_wheel_calibration()` returning only validated rows.

- [x] **Step 1: Write failing migration and preflight tests**

Cover existing table migration, legacy row exclusion, successful validated publish, service forwarding `replay_validated=True`, and scroll job rejection when only legacy current row exists.

- [x] **Step 2: Run focused tests and verify failure**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_store.py tests\test_execution_v2_service.py -q -p no:cacheprovider
```

Expected: new replay-validation assertions fail.

- [x] **Step 3: Add additive SQLite migration**

Add `replay_validated INTEGER NOT NULL DEFAULT 0` to new schema. In `initialize()`, inspect `PRAGMA table_info(wheel_calibrations)` and run this once when absent:

```sql
ALTER TABLE wheel_calibrations
ADD COLUMN replay_validated INTEGER NOT NULL DEFAULT 0
```

- [x] **Step 4: Gate publication and reads**

Require `replay_validated is True`, insert `1`, select the field, and add `c.replay_validated = 1` to current lookup. Update service publication call with the Dry-Run result flag. Existing job preflight then naturally returns `wheel_calibration_missing` for legacy rows.

- [x] **Step 5: Run focused tests**

Run Task 2 command. Expected: PASS.

---

### Task 3: Explain Dry-Run in UI

**Files:**
- Modify: `execution_v2/blueprint.py`
- Modify: `gateway/static/browser_v2.js`
- Test: `tests/test_execution_v2_routes.py`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: existing calibration GET response with active Dry-Run fields.
- Produces: fixed Chinese status/error copy; no API changes.

- [x] **Step 1: Write failing route and UI tests**

Assert `wheel_calibration_replay_not_observed` maps to “自动回放未能切换视频，请重新校准。” and `status="dry_run"` renders candidate number, multiplier, delta, and prior miss results.

- [x] **Step 2: Run tests and verify failure**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_routes.py -q -p no:cacheprovider
node --test tests-js\browser-v2-ui.test.js
```

Expected: new copy assertions fail.

- [x] **Step 3: Add fixed public copy**

Extend existing error map. Extend `renderWheelCalibration()` with one `active.status === "dry_run"` branch and append compact candidate result text. Do not add controls or markup.

- [x] **Step 4: Run UI and route tests**

Run Task 3 command. Expected: PASS.

---

### Task 4: Regression

**Files:**
- Modify only files listed above if tests expose a defect.

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
& .\.venv\Scripts\python.exe -m py_compile execution_v2\wheel_calibration.py execution_v2\store.py execution_v2\service.py execution_v2\blueprint.py
git diff --check
```

Expected: exit code 0. Current `.git` metadata is read-only, so this plan does not stage or commit files.
