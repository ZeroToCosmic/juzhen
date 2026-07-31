# Ghost Cursor Trajectory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the saved `builtin:bezier` mouse trajectory implementation with the installed `ghost-cursor` package while preserving recorded mouse patterns.

**Architecture:** A persistent Node worker owns `ghost-cursor.path()` and exchanges one-line JSON messages with a thread-safe Python bridge. `actions_dom.py` requests generated points only for builtin trajectories, clips them to the live Playwright viewport, scales their timing to the saved duration, and leaves recorded-pattern replay unchanged.

**Tech Stack:** Python 3.13, asyncio, subprocess, Playwright, Node.js CommonJS, `ghost-cursor` 1.4.2, pytest, Node test runner.

## Global Constraints

- Keep the persisted builtin reference exactly `{"source": "builtin", "id": "bezier"}` so existing strategies require no migration.
- Keep the strategy UI label “内置拟人函数”.
- Recorded `pattern` trajectories must not call Ghost Cursor.
- Retry a failed worker request exactly once, then fail the current action without falling back to the removed Python curve.
- One window failure must not stop other window workers.
- Do not write account, Cookie, proxy, page-content, or settings data to the worker protocol or its errors.
- Do not perform Git commits during this task, per the user’s current instruction.

---

### Task 1: Install Ghost Cursor and add the Node path worker

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Create: `browser/ghost-cursor-worker.js`
- Create: `tests-js/ghost-cursor-worker.test.js`

**Interfaces:**
- Consumes: `ghost-cursor.path(start, endOrBoundingBox)`.
- Produces: `generatePath(request, pathFunction?) -> {id, points}` and a stdin/stdout JSON-lines worker.

- [ ] **Step 1: Write the failing Node tests**

Create `tests-js/ghost-cursor-worker.test.js`:

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");

test("ghost-cursor dependency exports path", () => {
  const {path} = require("ghost-cursor");
  assert.equal(typeof path, "function");
});

test("worker sends the target box to ghost-cursor and returns finite points", () => {
  const {generatePath} = require("../browser/ghost-cursor-worker");
  let receivedEnd;
  const fakePath = (start, end) => {
    receivedEnd = end;
    return [start, {x: 30, y: 40}];
  };
  const response = generatePath({
    id: "request-1",
    start: {x: 10, y: 20},
    end: {x: 30, y: 40},
    target: {x: 20, y: 30, width: 20, height: 20},
  }, fakePath);
  assert.deepEqual(receivedEnd, {x: 20, y: 30, width: 20, height: 20});
  assert.deepEqual(response, {
    id: "request-1",
    points: [{x: 10, y: 20}, {x: 30, y: 40}],
  });
});

test("worker rejects malformed and non-finite path results", () => {
  const {generatePath} = require("../browser/ghost-cursor-worker");
  assert.throws(
    () => generatePath({id: "bad", start: {x: 0, y: 0}, end: {x: 1, y: 1}},
      () => [{x: 0, y: 0}, {x: Number.NaN, y: 1}]),
    /finite coordinates/
  );
});
```

- [ ] **Step 2: Run the Node tests and verify RED**

Run:

```powershell
node --test tests-js/ghost-cursor-worker.test.js
```

Expected: FAIL because `ghost-cursor` and `browser/ghost-cursor-worker.js` are absent.

- [ ] **Step 3: Install the pinned dependency**

Run:

```powershell
npm install ghost-cursor@1.4.2 --save-exact
```

Expected: `package.json` and `package-lock.json` contain exact version `1.4.2`; npm reports no installation error.

- [ ] **Step 4: Implement the worker**

Create `browser/ghost-cursor-worker.js`:

```javascript
"use strict";

const readline = require("node:readline");
const {path} = require("ghost-cursor");

function finitePoint(value, label) {
  if (!value || !Number.isFinite(value.x) || !Number.isFinite(value.y)) {
    throw new Error(`${label} must contain finite coordinates`);
  }
  return {x: Number(value.x), y: Number(value.y)};
}

function generatePath(request, pathFunction = path) {
  if (!request || typeof request.id !== "string" || !request.id) {
    throw new Error("request id is required");
  }
  const start = finitePoint(request.start, "start");
  const end = finitePoint(request.end, "end");
  let destination = end;
  if (request.target != null) {
    const target = request.target;
    if (!Number.isFinite(target.x) || !Number.isFinite(target.y)
        || !Number.isFinite(target.width) || !Number.isFinite(target.height)
        || target.width <= 0 || target.height <= 0) {
      throw new Error("target must be a positive finite bounding box");
    }
    destination = {
      x: Number(target.x),
      y: Number(target.y),
      width: Number(target.width),
      height: Number(target.height),
    };
  }
  const points = pathFunction(start, destination).map(
    (point, index) => finitePoint(point, `point ${index}`)
  );
  if (points.length < 2) {
    throw new Error("ghost-cursor path must contain at least two points");
  }
  return {id: request.id, points};
}

function startWorker(input = process.stdin, output = process.stdout) {
  const lines = readline.createInterface({input, crlfDelay: Infinity});
  lines.on("line", (line) => {
    let id = "";
    try {
      const request = JSON.parse(line);
      id = typeof request.id === "string" ? request.id : "";
      output.write(`${JSON.stringify(generatePath(request))}\n`);
    } catch (error) {
      output.write(`${JSON.stringify({id, error: String(error.message || error)})}\n`);
    }
  });
}

if (require.main === module) {
  startWorker();
}

module.exports = {generatePath, startWorker};
```

- [ ] **Step 5: Run the focused and complete Node suites**

Run:

```powershell
node --test tests-js/ghost-cursor-worker.test.js
npm run test:node
```

Expected: focused tests PASS; complete Node suite PASS.

### Task 2: Add the persistent, restartable Python bridge

**Files:**
- Create: `ghost_cursor_bridge.py`
- Create: `tests/test_ghost_cursor_bridge.py`

**Interfaces:**
- Consumes: `browser/ghost-cursor-worker.js` JSON-lines protocol.
- Produces: `GhostCursorBridge.generate_path(start: tuple[float, float], end: tuple[float, float], target: dict[str, float] | None = None) -> list[dict[str, float]]`, `GhostCursorError`, and `generate_ghost_path(start, end, target=None)`.

- [ ] **Step 1: Write failing bridge tests**

Create `tests/test_ghost_cursor_bridge.py` with deterministic fake processes:

```python
import io
import json

import pytest


class FakeStdin(io.StringIO):
    def flush(self):
        return None


class FakeProcess:
    def __init__(self, responses, returncode=None):
        self.stdin = FakeStdin()
        self.stdout = io.StringIO("".join(json.dumps(item) + "\n" for item in responses))
        self._returncode = returncode
        self.terminated = False

    def poll(self):
        return self._returncode

    def terminate(self):
        self.terminated = True
        self._returncode = 0

    def wait(self, timeout=None):
        return self._returncode

    def kill(self):
        self._returncode = -9


def test_bridge_returns_valid_points_and_reuses_one_process():
    from ghost_cursor_bridge import GhostCursorBridge
    process = FakeProcess([
        {"id": "fixed-1", "points": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]},
        {"id": "fixed-2", "points": [{"x": 3, "y": 4}, {"x": 5, "y": 6}]},
    ])
    starts = []
    bridge = GhostCursorBridge(
        process_factory=lambda *_args, **_kwargs: starts.append(True) or process,
        id_factory=iter(["fixed-1", "fixed-2"]).__next__,
    )
    assert bridge.generate_path((1, 2), (3, 4))[-1] == {"x": 3.0, "y": 4.0}
    assert bridge.generate_path((3, 4), (5, 6))[-1] == {"x": 5.0, "y": 6.0}
    assert len(starts) == 1


def test_bridge_restarts_once_after_broken_response():
    from ghost_cursor_bridge import GhostCursorBridge
    broken = FakeProcess([{"id": "wrong", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 1}]}])
    healthy = FakeProcess([{"id": "retry", "points": [{"x": 0, "y": 0}, {"x": 2, "y": 2}]}])
    processes = iter([broken, healthy])
    bridge = GhostCursorBridge(
        process_factory=lambda *_args, **_kwargs: next(processes),
        id_factory=iter(["first", "retry"]).__next__,
    )
    assert bridge.generate_path((0, 0), (2, 2))[-1] == {"x": 2.0, "y": 2.0}
    assert broken.terminated


def test_bridge_fails_after_exactly_one_retry():
    from ghost_cursor_bridge import GhostCursorBridge, GhostCursorError
    processes = iter([FakeProcess([]), FakeProcess([])])
    bridge = GhostCursorBridge(
        process_factory=lambda *_args, **_kwargs: next(processes),
        id_factory=iter(["first", "second"]).__next__,
    )
    with pytest.raises(GhostCursorError, match="after retry"):
        bridge.generate_path((0, 0), (2, 2))
```

- [ ] **Step 2: Run the bridge tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ghost_cursor_bridge.py -q -p no:cacheprovider
```

Expected: FAIL because `ghost_cursor_bridge` does not exist.

- [ ] **Step 3: Implement the bridge**

Create `ghost_cursor_bridge.py` with these exact public members and behavior:

```python
from __future__ import annotations

import atexit
import json
import math
import queue
import subprocess
import threading
from pathlib import Path
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parent
WORKER_PATH = PROJECT_ROOT / "browser" / "ghost-cursor-worker.js"


class GhostCursorError(RuntimeError):
    pass


class GhostCursorBridge:
    def __init__(self, *, process_factory=subprocess.Popen, id_factory=None, response_timeout=5.0):
        self._process_factory = process_factory
        self._id_factory = id_factory or (lambda: uuid4().hex)
        self._response_timeout = float(response_timeout)
        self._process = None
        self._responses = None
        self._lock = threading.RLock()

    @staticmethod
    def _read_responses(process, responses):
        for line in process.stdout:
            responses.put(line)
        responses.put(None)

    def _start_locked(self):
        if self._process is not None and self._process.poll() is None:
            return self._process
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        self._process = self._process_factory(
            ["node", str(WORKER_PATH)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
            creationflags=flags,
        )
        self._responses = queue.Queue()
        threading.Thread(
            target=self._read_responses,
            args=(self._process, self._responses),
            daemon=True,
        ).start()
        return self._process

    def _close_locked(self):
        process, self._process = self._process, None
        self._responses = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)

    @staticmethod
    def _point(value, label):
        if not isinstance(value, dict):
            raise GhostCursorError(f"{label} is invalid")
        x, y = value.get("x"), value.get("y")
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise GhostCursorError(f"{label} is invalid")
        if not math.isfinite(x) or not math.isfinite(y):
            raise GhostCursorError(f"{label} is invalid")
        return {"x": float(x), "y": float(y)}

    def _request_locked(self, start, end, target):
        process = self._start_locked()
        request_id = self._id_factory()
        request = {
            "id": request_id,
            "start": {"x": float(start[0]), "y": float(start[1])},
            "end": {"x": float(end[0]), "y": float(end[1])},
        }
        if target is not None:
            request["target"] = {key: float(target[key]) for key in ("x", "y", "width", "height")}
        process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        process.stdin.flush()
        try:
            line = self._responses.get(timeout=self._response_timeout)
        except queue.Empty as error:
            raise GhostCursorError("worker response timed out") from error
        if not line:
            raise GhostCursorError("worker closed without a response")
        response = json.loads(line)
        if response.get("id") != request_id:
            raise GhostCursorError("worker response id mismatch")
        if response.get("error"):
            raise GhostCursorError(str(response["error"]))
        points = [self._point(item, f"point {index}") for index, item in enumerate(response.get("points") or [])]
        if len(points) < 2:
            raise GhostCursorError("worker returned fewer than two points")
        return points

    def generate_path(self, start, end, target=None):
        failures = []
        with self._lock:
            for _attempt in range(2):
                try:
                    return self._request_locked(start, end, target)
                except Exception as error:
                    failures.append(str(error))
                    self._close_locked()
        raise GhostCursorError(f"Ghost Cursor path generation failed after retry: {failures[-1]}")

    def close(self):
        with self._lock:
            self._close_locked()


_BRIDGE = GhostCursorBridge()
atexit.register(_BRIDGE.close)


def generate_ghost_path(start, end, target=None):
    return _BRIDGE.generate_path(start, end, target)
```

- [ ] **Step 4: Run bridge tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ghost_cursor_bridge.py -q -p no:cacheprovider
```

Expected: all bridge tests PASS.

### Task 3: Route builtin move and click trajectories through Ghost Cursor

**Files:**
- Modify: `actions_dom.py`
- Modify: `browser_actions.py`
- Modify: `tests/test_actions.py`

**Interfaces:**
- Consumes: `generate_ghost_path(start, end, target=None)`.
- Produces: `human_move_to(page, x, y, *, duration_seconds=0.3, pattern=None, target_box=None, rng=None, sleep_fn=None) -> tuple[float, float]`; action results contain `trajectory_source` equal to `ghost-cursor` or `recorded-pattern`.

- [ ] **Step 1: Add failing action tests**

Add focused tests to `tests/test_actions.py`:

```python
def test_builtin_move_uses_ghost_cursor_and_scales_duration(monkeypatch):
    from actions_dom import human_move_to
    page = _PointerPage()
    waits = []
    calls = []

    monkeypatch.setattr("actions_dom.generate_ghost_path", lambda start, end, target=None: (
        calls.append((start, end, target))
        or [{"x": start[0], "y": start[1]}, {"x": 70, "y": 80}, {"x": end[0], "y": end[1]}]
    ))

    async def record_wait(seconds):
        waits.append(seconds)

    final = asyncio.run(human_move_to(
        page, 90, 40, duration_seconds=0.6, sleep_fn=record_wait
    ))
    assert calls == [((10, 10), (90.0, 40.0), None)]
    assert final == (90.0, 40.0)
    assert page.moves[-1] == (90.0, 40.0)
    assert sum(waits) == pytest.approx(0.6)


def test_recorded_move_never_calls_ghost_cursor(monkeypatch):
    from actions_dom import human_move_to
    page = _PointerPage()
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not be called")),
    )
    pattern = {"data": {"points": [
        {"x_ratio": 0, "y_ratio": 0, "dt_ms": 0},
        {"x_ratio": 1, "y_ratio": 1, "dt_ms": 0},
    ]}}
    asyncio.run(human_move_to(page, 30, 20, pattern=pattern, sleep_fn=lambda _seconds: asyncio.sleep(0)))
    assert page.moves[-1] == (30.0, 20.0)


def test_builtin_click_reports_ghost_cursor_source(monkeypatch):
    from browser_actions import execute_action
    page = _PointerPage()
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": end[0], "y": end[1]},
        ],
    )
    result = asyncio.run(execute_action(
        page,
        {"id": "click", "type": "click", "params": {
            "element": "target", "button": "left", "click_count": 1,
            "hold_seconds": [0, 0],
            "trajectory": {"source": "builtin", "id": "bezier"},
        }},
        {"target": "//target"}, {}, _fixed_text,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))
    assert result["trajectory_source"] == "ghost-cursor"
```

- [ ] **Step 2: Run focused action tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_actions.py -q -p no:cacheprovider -k "ghost_cursor or recorded_move_never"
```

Expected: FAIL because builtin movement still uses the local quadratic curve and action results lack `trajectory_source`.

- [ ] **Step 3: Implement the smallest action integration**

In `actions_dom.py`:

```python
from ghost_cursor_bridge import generate_ghost_path
```

Add `element_viewport_target()` by moving the existing bounding-box clipping from `element_viewport_point()` into a helper that returns both center and visible box:

```python
async def element_viewport_target(page, selector: str, operation: str):
    field = page.locator(selector)
    await field.scroll_into_view_if_needed()
    box = await field.bounding_box()
    if not box:
        raise RuntimeError(f"element cannot be {operation}: {selector}")
    width, height = await get_viewport(page)
    left = max(float(box["x"]), 0.0)
    top = max(float(box["y"]), 0.0)
    right = min(float(box["x"]) + float(box["width"]), width - 1)
    bottom = min(float(box["y"]) + float(box["height"]), height - 1)
    if right < left or bottom < top:
        raise RuntimeError(f"element is outside the viewport: {selector}")
    target = {"x": left, "y": top, "width": max(right - left, 1), "height": max(bottom - top, 1)}
    return ((left + right) / 2, (top + bottom) / 2), target


async def element_viewport_point(page, selector: str, operation: str):
    point, _target = await element_viewport_target(page, selector, operation)
    return point
```

Add `target_box=None` to the keyword parameters of `human_move_to()`. Immediately before its existing `if points:` statement, initialize:

```python
    final_x, final_y = target_x, target_y
```

Leave the complete existing `if points:` recorded-pattern block unchanged. Replace its existing quadratic-curve `else:` block with:

```python
    else:
        route = await asyncio.to_thread(
            generate_ghost_path,
            (start_x, start_y),
            (target_x, target_y),
            target_box,
        )
        delay = max(float(duration_seconds), 0.0) / len(route)
        for index, point in enumerate(route):
            current_x = _clamp(point["x"], width)
            current_y = _clamp(point["y"], height)
            if target_box is None and index == len(route) - 1:
                current_x, current_y = target_x, target_y
            await page.mouse.move(current_x, current_y)
            await sleep(delay)
            final_x, final_y = current_x, current_y
```

Replace the existing final pointer assignment with:

```python
    _set_pointer(page, final_x, final_y)
    return final_x, final_y
```

Update `human_click()` and element-target movement to call `element_viewport_target()`, pass the box, and use the returned final point. Recorded patterns continue passing no Ghost Cursor request.

In `browser_actions.py`, add this field to successful move and click results:

```python
"trajectory_source": (
    "recorded-pattern"
    if params["trajectory"].get("source") == "pattern"
    else "ghost-cursor"
),
```

- [ ] **Step 4: Run all action/config/runtime tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_actions.py tests/test_browser_strategy_config.py tests/test_browser_strategy_runtime.py -q -p no:cacheprovider
```

Expected: all selected tests PASS; recorded-pattern expectations remain unchanged.

### Task 4: Verify the installed integration and persistence contract

**Files:**
- Modify only if a failing assertion exposes a Ghost Cursor integration defect in files from Tasks 1–3.

**Interfaces:**
- Consumes: completed Node worker, Python bridge, and action integration.
- Produces: test evidence and the manual AdsPower validation checklist.

- [ ] **Step 1: Verify the actual worker process**

Run:

```powershell
$request = '{"id":"smoke","start":{"x":10,"y":10},"end":{"x":300,"y":200}}'
$request | node browser/ghost-cursor-worker.js
```

Expected: one JSON response with `id` equal to `smoke` and at least two finite points.

- [ ] **Step 2: Run all Python and Node tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
npm run test:node
```

Expected: both suites PASS with no test failures.

- [ ] **Step 3: Check persistence and UI compatibility**

Run:

```powershell
rg -n '"id": "bezier"|builtin:bezier|内置拟人函数' browser_strategy_config.py gateway/static/browser_strategy_ui.js tests tests-js
```

Expected: persisted builtin identifier and UI label remain present; no migration changes appear.

- [ ] **Step 4: Perform live two-window AdsPower acceptance**

With AdsPower Local API and two test profiles open:

1. Save a strategy containing builtin move and click actions.
2. Execute it against both profiles.
3. Confirm both result objects contain `trajectory_source: "ghost-cursor"`.
4. Confirm each window reaches its own target without sharing pointer coordinates.
5. Refresh the console and restart the launcher.
6. Confirm the strategy and recorded patterns remain present.

Expected: both windows complete independently; any single-window error names its action and does not cancel the other window.

- [ ] **Step 5: Inspect final changed-file scope**

Run:

```powershell
git diff -- package.json package-lock.json browser/ghost-cursor-worker.js ghost_cursor_bridge.py actions_dom.py browser_actions.py tests-js/ghost-cursor-worker.test.js tests/test_ghost_cursor_bridge.py tests/test_actions.py docs/superpowers/specs/2026-07-23-ghost-cursor-trajectory-design.md docs/superpowers/plans/2026-07-23-ghost-cursor-trajectory.md
```

Expected: every changed line maps to dependency installation, Ghost Cursor generation, recorded-pattern compatibility, tests, or the approved documents. Do not commit.
