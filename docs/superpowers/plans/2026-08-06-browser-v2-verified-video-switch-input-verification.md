# Browser V2 Verified Video Switch and Input Verification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make each V2 scroll count represent one verified TikTok video switch and stop `contenteditable` formatting from falsely failing successful input.

**Architecture:** Reuse `browser_video_switch.execute_verified_switches` as the only video-switch primitive and adapt its bounded result into V2 action evidence. Keep V2 input execution local, but compare normalized before/after editor text. Preserve stored strategy compatibility by retaining a hidden fixed `distance_pixels` field.

**Tech Stack:** Python 3, asyncio, Playwright async API, Flask, SQLite, browser JavaScript, Node test runner, pytest.

## Global Constraints

- Do not duplicate or rewrite `browser_video_switch`.
- One scroll count means one completed, stable video identity change.
- Keep `distance_pixels` in stored V2 payloads; runtime ignores it.
- No SQLite migration.
- Do not automate TikTok login or bypass authentication controls.
- Stop the strategy after an unverified switch or input.
- Never expose Profile IDs, input copy, video IDs, or raw fingerprints in public action results.
- Current `.git` metadata is read-only. Run commit steps only after write access is restored; otherwise record each checkpoint without staging.

---

## File map

- `execution_v2/actions.py`: adapt V2 scroll to the legacy verified switcher; normalize and verify editor content.
- `execution_v2/executor.py`: retain new safe video-switch measurements in stored action results.
- `execution_v2/service.py`: expose only bounded numeric switch measurements through public job/history APIs.
- `gateway/static/browser_v2.js`: rename scroll controls, hide pixel distance, serialize compatibility value, render switch progress.
- `tests/test_execution_v2_actions.py`: action-level verified-switch and input-format regression tests.
- `tests/test_execution_v2_executor.py`: error propagation and safe result retention tests.
- `tests/test_execution_v2_service.py`: public action-result allowlist tests.
- `tests-js/browser-v2-ui.test.js`: editor serialization, terminology, and history presentation tests.

### Task 1: Reuse verified video switching in V2

**Files:**
- Modify: `execution_v2/actions.py:10-75`
- Modify: `execution_v2/executor.py:187-204`
- Modify: `execution_v2/service.py:771-839`
- Test: `tests/test_execution_v2_actions.py`
- Test: `tests/test_execution_v2_executor.py`
- Test: `tests/test_execution_v2_service.py`

**Interfaces:**
- Consumes: `browser_video_switch.execute_verified_switches(page, *, direction, requested, interval_range, lifecycle, rng, sleep_fn)`.
- Produces: successful scroll result keys `requested_switches`, `completed_switches`, `wheel_events`, `direction`, `distance_pixels`, `count`, and `interval_seconds`.
- Preserves: `VideoSwitchError.code` for `StrategyExecutor` failure handling.

- [ ] **Step 1: Write failing action tests**

Add a test double around the imported switch primitive. Assert V2 samples `count`, ignores configured 400–600 pixels, passes `lifecycle=None`, and dispatches no direct wheel event:

```python
def test_scroll_reuses_verified_video_switcher_and_counts_completed_videos(monkeypatch):
    page = _Page()
    calls = []

    async def verified(page_arg, **kwargs):
        calls.append((page_arg, kwargs))
        return {
            "count": 2,
            "distance": 120,
            "requested_switches": 2,
            "completed_switches": 2,
            "wheel_events": 5,
            "switches": [],
        }

    monkeypatch.setattr("execution_v2.actions.execute_verified_switches", verified)
    result = asyncio.run(execute_action(
        page,
        {
            "id": "scroll-1", "type": "scroll", "direction": "down",
            "distance_pixels": [400, 600], "count": [2, 2],
            "interval_seconds": [0.2, 0.5],
        },
        _elements(), _Resolver({}), _text_resolver,
        rng=random.Random(1), sleep=_no_sleep,
    ))

    assert calls[0][0] is page
    assert calls[0][1] == {
        "direction": "down", "requested": 2,
        "interval_range": [0.2, 0.5], "lifecycle": None,
        "rng": calls[0][1]["rng"], "sleep_fn": _no_sleep,
    }
    assert page.mouse.wheels == []
    assert result["distance_pixels"] == 120
    assert result["completed_switches"] == 2
    assert result["wheel_events"] == 5
```

Add an error test using `VideoSwitchError("video_switch_not_observed")` and assert the exact error escapes `_scroll` unchanged.

- [ ] **Step 2: Run action tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py -q -p no:cacheprovider
```

Expected: new switch tests fail because `_scroll` still calls `page.mouse.wheel` directly.

- [ ] **Step 3: Replace direct wheel dispatch with the existing primitive**

In `execution_v2/actions.py`, import and call the existing function:

```python
from browser_video_switch import execute_verified_switches


async def _scroll(page: Any, action: dict[str, Any], *, rng: Any, sleep: Callable[[float], Awaitable[None]]) -> dict[str, Any]:
    requested = int(_sample(action["count"], rng, integer=True))
    switched = await execute_verified_switches(
        page,
        direction=action["direction"],
        requested=requested,
        interval_range=action["interval_seconds"],
        lifecycle=None,
        rng=rng,
        sleep_fn=sleep,
    )
    return _result(
        action,
        direction=action["direction"],
        distance_pixels=int(switched["distance"]),
        count=int(switched["completed_switches"]),
        interval_seconds=action["interval_seconds"],
        requested_switches=int(switched["requested_switches"]),
        completed_switches=int(switched["completed_switches"]),
        wheel_events=int(switched["wheel_events"]),
    )
```

Do not catch `VideoSwitchError`; `StrategyExecutor` already reads its stable `.code`.

- [ ] **Step 4: Retain safe measurements through executor and service**

Extend `_sanitized_result` in `execution_v2/executor.py`:

```python
allowed = {
    "duration_seconds", "direction", "distance_pixels", "count", "interval_seconds",
    "requested_switches", "completed_switches", "wheel_events",
    "button", "click_count", "hold_seconds", "after_seconds", "content_source", "text_length",
}
```

Extend `_public_action_result` in `execution_v2/service.py` with the same three numeric keys. `_safe_action_detail` already accepts finite numeric values through its final numeric branch; keep fingerprints and `switches` excluded.

- [ ] **Step 5: Test failure propagation and public redaction**

Add an executor test whose second action raises `VideoSwitchError("video_switch_not_observed")`; assert later actions do not run and `outcome.error_code` remains `video_switch_not_observed`.

Extend `test_public_action_results_flatten_safe_failure_evidence_without_nested_secrets` with:

```python
store.append_action_result(
    "job-evidence", raw, 2, "scroll", "succeeded", Stage.EXECUTE_ACTION,
    {
        "requested_switches": 2,
        "completed_switches": 2,
        "wheel_events": 5,
        "switches": [{"from": "private-a", "to": "private-b"}],
    },
)
```

Assert the three counts are public and `switches`, `private-a`, and `private-b` are absent.

- [ ] **Step 6: Run Task 1 tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py tests\test_execution_v2_executor.py tests\test_execution_v2_service.py tests\test_browser_video_switch.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 7: Checkpoint**

If `.git` becomes writable:

```powershell
git add execution_v2/actions.py execution_v2/executor.py execution_v2/service.py tests/test_execution_v2_actions.py tests/test_execution_v2_executor.py tests/test_execution_v2_service.py
git commit -m "fix: verify V2 video switches"
```

Otherwise leave files unstaged and record Task 1 tests as the checkpoint.

### Task 2: Normalize native and contenteditable input verification

**Files:**
- Modify: `execution_v2/actions.py:92-165`
- Test: `tests/test_execution_v2_actions.py`

**Interfaces:**
- Produces: `_read_input_value(handle) -> str` using DOM-type-aware reading.
- Produces: `_normalize_input_text(value: str) -> str` for verification only.
- Preserves: `ActionExecutionError("input_verification_failed")` and no later action after failure.

- [ ] **Step 1: Write failing contenteditable tests**

Add a successful regression where the expected text contains a newline and the editor returns non-breaking/consecutive spaces:

```python
def test_input_accepts_equivalent_contenteditable_whitespace(monkeypatch):
    page = _Page()
    handle = _Handle("")
    resolver = _Resolver({"input": (handle, {"x": 20, "y": 10, "width": 80, "height": 40})})

    async def type_text(_page, _text, **_kwargs):
        handle.value = "first\u00a0  line second line"

    monkeypatch.setattr("execution_v2.actions.human_type", type_text)
    result = asyncio.run(execute_action(
        page,
        {
            "id": "input-1", "type": "input", "element_id": "input",
            "content_source": "fixed", "fixed_text": "first line\nsecond line",
            "content_library_id": "", "interval_ms": [20, 20],
        },
        _elements(), resolver, _text_resolver,
        rng=random.Random(1), sleep=_no_sleep,
    ))
    assert result["status"] == "succeeded"
```

Add a failure regression where the full expected text already existed before typing and remains unchanged. Assert `input_verification_failed`.

- [ ] **Step 2: Run input tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py -q -p no:cacheprovider
```

Expected: whitespace-equivalence test fails under exact substring comparison; pre-existing-text test exposes missing before/after comparison.

- [ ] **Step 3: Read DOM values by element kind**

Replace `_read_input_value` with:

```python
async def _read_input_value(handle: Any) -> str:
    try:
        value = await handle.evaluate(
            """element => {
                const tag = String(element?.tagName || '').toLowerCase();
                if (tag === 'input' || tag === 'textarea') {
                    return String(element.value || '');
                }
                return String(element.innerText ?? element.textContent ?? '');
            }"""
        )
    except Exception as error:
        raise ActionExecutionError("input_verification_unavailable") from error
    return value if isinstance(value, str) else ""


def _normalize_input_text(value: str) -> str:
    return " ".join(
        value.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").split()
    )
```

- [ ] **Step 4: Require observable before/after growth**

In `_input`, read before focusing and compare normalized occurrence counts after typing:

```python
before = _normalize_input_text(await _read_input_value(resolved.handle))
await resolved.handle.focus()
await human_type(...)
after = _normalize_input_text(await _read_input_value(resolved.handle))
expected = _normalize_input_text(text)
if not expected or after.count(expected) <= before.count(expected):
    raise ActionExecutionError("input_verification_failed")
```

Keep copy out of action results; retain only `text_length`.

- [ ] **Step 5: Run Task 2 tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_actions.py tests\test_execution_v2_executor.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 6: Checkpoint**

If `.git` becomes writable:

```powershell
git add execution_v2/actions.py tests/test_execution_v2_actions.py
git commit -m "fix: verify contenteditable input"
```

Otherwise leave files unstaged and record Task 2 tests as the checkpoint.

### Task 3: Make UI and history describe verified video switches

**Files:**
- Modify: `gateway/static/browser_v2.js:79-83, 339-347, 439-449, 420-429`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Produces: scroll payload with `distance_pixels: [120, 120]` and user-entered `count` as video switches.
- Consumes: public action result fields `requested_switches`, `completed_switches`, and `wheel_events`.

- [ ] **Step 1: Write failing UI contract tests**

Add tests for the default compatibility value and source terminology:

```javascript
test("scroll editor defines verified video switches without pixel controls", () => {
  assert.deepEqual(actionTemplate("scroll", "scroll-1"), {
    id: "scroll-1", type: "scroll", direction: "down",
    distance_pixels: [120, 120], count: [1, 2], interval_seconds: [0.2, 0.5],
  });
  const source = fs.readFileSync(path.join(__dirname, "..", "gateway", "static", "browser_v2.js"), "utf8");
  assert.equal(source.includes("滚动像素范围"), false);
  assert.equal(source.includes("视频切换次数范围"), true);
  assert.equal(source.includes("一次代表成功切换一个视频"), true);
});
```

Extend the existing closed-schema save test with a scroll action whose draft contains `distance_pixels: [400, 600]`; assert the submitted action contains `[120, 120]` and retains the selected `count`.

Add a history-render test with `completed_switches: 2`, `requested_switches: 2`, and `wheel_events: 5`; assert visible text distinguishes `视频切换 2/2` from `滚轮事件 5`.

- [ ] **Step 2: Run UI tests and verify failure**

Run:

```powershell
node --test tests-js/browser-v2-ui.test.js
```

Expected: new terminology, compatibility serialization, and history tests fail.

- [ ] **Step 3: Remove pixel editing and set compatibility serialization**

Change the scroll template to:

```javascript
if (type === "scroll") return {
  id: actionId, type, direction: "down",
  distance_pixels: [120, 120], count: [1, 2], interval_seconds: [0.2, 0.5],
};
```

Render only:

```javascript
field("视频切换次数范围，例如 1-2", "count");
card.append(node("p", "一次代表成功切换一个视频", "v2-help"));
field("切换完成后的间隔秒数范围，例如 0.2-0.5", "interval_seconds");
```

Serialize scroll actions with:

```javascript
return {
  id: action.id,
  type: "scroll",
  direction: action.direction,
  distance_pixels: [120, 120],
  count: parseRange(action.count, "视频切换次数范围", true),
  interval_seconds: parseRange(action.interval_seconds, "视频切换间隔范围"),
};
```

- [ ] **Step 4: Render switch measurements in history**

Build one safe summary only for scroll action records:

```javascript
const switchProgress = record.action_type === "scroll"
  && Number.isInteger(record.completed_switches)
  && Number.isInteger(record.requested_switches)
  ? "视频切换 " + record.completed_switches + "/" + record.requested_switches
  : "";
const wheelProgress = record.action_type === "scroll"
  && Number.isInteger(record.wheel_events)
  ? "滚轮事件 " + record.wheel_events
  : "";
```

Append both values to the existing `meta.textContent` list. Do not render fingerprints or per-video records.

- [ ] **Step 5: Run Task 3 tests**

Run:

```powershell
node --check gateway\static\browser_v2.js
node --test tests-js/browser-v2-ui.test.js
```

Expected: syntax check succeeds and all focused UI tests pass.

- [ ] **Step 6: Checkpoint**

If `.git` becomes writable:

```powershell
git add gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js
git commit -m "fix: clarify verified video scrolling"
```

Otherwise leave files unstaged and record Task 3 tests as the checkpoint.

### Task 4: Regression and real AdsPower acceptance

**Files:**
- Verify only: `browser_video_switch.py`
- Verify only: `execution_v2/`
- Verify only: `gateway/static/browser_v2.js`
- Evidence output: `data/execution_v2/evidence/`

**Interfaces:**
- Consumes: two logged-in independent AdsPower test Profiles.
- Produces: one terminal V2 job with per-Profile switch counts, input result, submission action, cleanup status, and failure screenshots when applicable.

- [ ] **Step 1: Run Python regression suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_video_switch.py tests\test_execution_v2_actions.py tests\test_execution_v2_executor.py tests\test_execution_v2_service.py tests\test_execution_v2_scheduler.py tests\test_execution_v2_tiling.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 2: Run all V2 and Node tests**

Run:

```powershell
$v2Tests = Get-ChildItem tests -Filter 'test_execution_v2_*.py' | ForEach-Object { $_.FullName }
.\.venv\Scripts\python.exe -m pytest $v2Tests tests\test_browser_video_switch.py tests\test_window_tiler.py -q -p no:cacheprovider
npm.cmd run test:node
```

Expected: all tests pass.

- [ ] **Step 3: Run syntax and whitespace checks**

Run:

```powershell
.\.venv\Scripts\python.exe -m py_compile execution_v2\actions.py execution_v2\executor.py execution_v2\service.py
node --check gateway\static\browser_v2.js
git diff --check
```

Expected: all commands exit 0; line-ending warnings are informational.

- [ ] **Step 4: Restart local Flask and refresh UI**

Use the project launcher so only this project's old Flask process is stopped. Open `/browser-v2`, then force refresh with `Ctrl+F5`.

Expected: scroll editor shows video-switch count and no pixel-distance field.

- [ ] **Step 5: Run two logged-in Profile acceptance**

Use one strategy containing:

1. wait;
2. scroll down with video-switch count `[2, 2]`;
3. click comment entry;
4. input from the selected content library;
5. click submit.

Expected for each Profile:

- exactly two stable video changes are counted;
- `completed_switches=2` and `requested_switches=2`;
- `wheel_events` may exceed two but is reported separately;
- comment editor visibly receives the selected copy;
- input action succeeds before submit starts;
- Profile result succeeds and cleanup confirms closure.

- [ ] **Step 6: Verify logged-out failure semantics separately**

Run the same strategy on one known logged-out test Profile.

Expected: no submission is claimed; input fails because no editable comment target exists; evidence shows the login requirement. Do not treat this as a video-switch or input-verification regression.

- [ ] **Step 7: Final checkpoint**

If `.git` becomes writable:

```powershell
git status --short
git add execution_v2/actions.py execution_v2/executor.py execution_v2/service.py gateway/static/browser_v2.js tests/test_execution_v2_actions.py tests/test_execution_v2_executor.py tests/test_execution_v2_service.py tests-js/browser-v2-ui.test.js docs/superpowers/specs/2026-08-06-browser-v2-verified-video-switch-input-verification-design.md docs/superpowers/plans/2026-08-06-browser-v2-verified-video-switch-input-verification.md
git commit -m "fix: verify V2 video interactions"
```

Otherwise report changed files, automated results, and real acceptance outcome without claiming a commit.
