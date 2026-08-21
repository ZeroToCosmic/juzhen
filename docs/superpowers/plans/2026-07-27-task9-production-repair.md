# Task 9 Production Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mask public AdsPower Profile IDs, make wheel-driven TikTok video switching wait for asynchronous snap, re-resolve the comment entry after switching, preserve earlier action evidence on later failure, and rerun Task 9 on the two authorized profiles.

**Architecture:** Add one small shared public-identity helper and keep full Profile IDs inside operational code. Refine the existing verified-switch state machine with a per-pulse observation deadline inside the existing bounded switch loop. Add a comment-entry-only pre-dispatch readiness resolver and carry already-sanitized successful action measurements through the staged failure boundary.

**Tech Stack:** Python 3, Flask, Playwright async API over CDP, pytest, Node.js built-in test runner, Windows AdsPower Local API.

## Global Constraints

- Public Profile IDs are `***` plus the final four characters; identifiers shorter than four characters reveal no original characters.
- Internal AdsPower calls, locks, leases, configuration, and request values keep the full Profile ID.
- Saved scroll counts remain verified-video counts and are never converted.
- Every upward/downward wheel pulse remains exactly `-120`/`+120`.
- Per-pulse observation is 450 milliseconds; per-switch total deadline is 8 seconds.
- Comment-entry readiness waits at most 3 seconds before dispatch.
- A comment-entry click is dispatched at most once; no post-dispatch retry is allowed.
- No config schema or persistence migration is permitted.
- Task 9 may operate only on the two authorized profiles displayed publicly as `***xcto` and `***xctm`.
- Task 9 must not type or post a comment.
- The directory is not a Git repository by user direction. Do not initialize Git, create worktrees, stage files, or commit. Record checkpoints in `.superpowers/sdd/progress.md`.

---

## File structure

- Create `browser_public_identity.py`: shared, dependency-free Profile ID masking.
- Create `tests/test_browser_public_identity.py`: unit contract for the masking helper.
- Modify `gateway/browser_orchestrator.py`: recursively mask `profile_id` in direct public session results.
- Modify `gateway/app.py`: mask Profile IDs in generic and explicit browser result projectors, and preserve completed actions in failure responses.
- Modify `browser_video_switch.py`: bounded pulse observation and eight-second switch deadline.
- Modify `browser_actions.py`: comment-entry-only pre-dispatch readiness polling.
- Modify `browser_strategy_runtime.py`: transport sanitized completed actions through staged errors.
- Modify `tests/test_app.py`, `tests/test_browser_orchestrator.py`, `tests/test_browser_video_switch.py`, `tests/test_browser_actions.py`, and `tests/test_browser_strategy_runtime.py`: regression coverage.
- Modify `.superpowers/sdd/progress.md`: task checkpoints and fresh verification evidence.
- Modify `.superpowers/sdd/verified-scroll-task-9-report.md`: append the production-repair rerun; retain the original blocked attempt.
- Modify `.superpowers/sdd/verified-scroll-task-9-review.md`: append the rerun review outcome.
- Modify `docs/superpowers/reports/2026-07-25-verified-scroll-and-locators-verification.md`: link the rerun evidence and final acceptance state.

---

### Task 1: Public Profile ID masking

**Files:**
- Create: `browser_public_identity.py`
- Create: `tests/test_browser_public_identity.py`
- Modify: `gateway/browser_orchestrator.py:59-72`
- Modify: `gateway/app.py:3650-3687`
- Modify: `gateway/app.py:4326-4369`
- Modify: `tests/test_browser_orchestrator.py:253-278`
- Modify: `tests/test_app.py:1176-1245` and browser-route response assertions

**Interfaces:**
- Produces: `mask_profile_id(value: object) -> str`
- Consumes later: `gateway.app.public_browser_payload`, `gateway.app._public_identifier`, and `gateway.browser_orchestrator._sanitize_public_value`

- [ ] **Step 1: Write masking-helper tests**

```python
from browser_public_identity import mask_profile_id


def test_profile_id_keeps_only_last_four_characters():
    assert mask_profile_id("profile-k1dxxcto") == "***xcto"


def test_profile_id_mask_is_idempotent():
    assert mask_profile_id("***xcto") == "***xcto"


def test_short_profile_id_reveals_no_characters():
    assert mask_profile_id("abc") == "***"
```

- [ ] **Step 2: Write public-boundary failures before implementation**

Add assertions proving:

```python
assert app_module.public_browser_payload(
    {"results": [{"profile_id": "profile-k1dxxcto"}]}
) == {"results": [{"profile_id": "***xcto"}]}

assert public_session_result(
    {"profile_id": "profile-k1dxxctm", "status": "ok"}
)["profile_id"] == "***xctm"
```

Also update one execute-route test to assert that the fake controller receives
`profile-k1dxxcto` while the JSON response returns `***xcto`.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_public_identity.py tests/test_browser_orchestrator.py tests/test_app.py -q
```

Expected: the new tests fail because `mask_profile_id` is absent and existing
public response paths preserve full Profile IDs.

- [ ] **Step 4: Add the minimal shared masker**

Create:

```python
"""Public presentation helpers for internal browser identifiers."""

from __future__ import annotations

import re

_PUBLIC_PROFILE_ID = re.compile(r"^\*\*\*.{4}$", re.DOTALL)


def mask_profile_id(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _PUBLIC_PROFILE_ID.fullmatch(text):
        return text
    if len(text) < 4:
        return "***"
    return f"***{text[-4:]}"
```

- [ ] **Step 5: Apply the masker at every public boundary**

In `gateway/browser_orchestrator.py`, pass the parent key recursively and mask
normalized `profile_id` scalar values:

```python
def _sanitize_public_value(value: Any, key: object = "") -> Any:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized == "profileid":
        return mask_profile_id(value)
    if isinstance(value, dict):
        return {
            item_key: _sanitize_public_value(item, item_key)
            for item_key, item in value.items()
            if not _is_sensitive_key(item_key)
        }
```

In `gateway/app.py`, mask a string before generic text sanitization when
`normalize_sensitive_browser_key(key) == "profileid"`. In
`_public_identifier`, return `mask_profile_id(value)` when `prefix ==
"profile"`. Remove Profile ID interpolation from
`BrowserExecutionBusyError.reason` so a legacy error string cannot bypass the
structured field boundary.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the Step 3 command again.

Expected: all selected tests pass; fake internal controller assertions still see
the full test ID, while public assertions see only `***xcto`/`***xctm`.

- [ ] **Step 7: Scan new public projections**

Run:

```powershell
rg -n '"profile_id": profile_id|profile \{profile_id\}|item\.profile_id' gateway browser_*.py
```

Inspect every hit. Internal request/session usage may remain full; every
response/log path must pass through `public_browser_payload`,
`public_session_result`, or `_public_identifier(..., "profile")`.

- [ ] **Step 8: Record the non-Git checkpoint**

Append Task 1 RED/GREEN counts and the no-Git constraint to
`.superpowers/sdd/progress.md`.

---

### Task 2: TikTok asynchronous switch observation

**Files:**
- Modify: `browser_video_switch.py:19-29`
- Modify: `browser_video_switch.py:169-276`
- Modify: `tests/test_browser_video_switch.py`

**Interfaces:**
- Produces: `_wait_for_stable_changed_state_until(page, before, *, deadline, sleep_fn) -> FeedState | None`
- Produces: `switch_once(page, direction, *, sleep_fn) -> tuple[FeedState, FeedState, int]`
- Preserves: `execute_verified_switches(...)` public measurements and error types

- [ ] **Step 1: Add a RED test for delayed feed commit**

Add a fake page whose first two captures after a wheel still return the old
identity, followed by two stable captures of the new identity:

```python
class DelayedCommitFeedPage(FakeFeedPage):
    def __init__(self):
        super().__init__(pulses_per_switch=1)
        self.pending_captures = 0

    def apply_wheel(self, delta_y):
        self.pending_captures = 2
        self.pending_delta = 1 if delta_y > 0 else -1

    async def evaluate(self, expression):
        if self.pending_captures:
            self.pending_captures -= 1
        elif hasattr(self, "pending_delta"):
            self.video_index += self.pending_delta
            del self.pending_delta
        return await super().evaluate(expression)


def test_switch_waits_for_async_identity_change_after_one_wheel(monkeypatch):
    page = DelayedCommitFeedPage()
    result = execute(page, monkeypatch, requested=1, direction="down")
    assert result["completed_switches"] == 1
    assert result["wheel_events"] == 1
    assert page.mouse.wheel_calls == [(0, 120)]
```

- [ ] **Step 2: Add RED tests for pulse and total bounds**

Add tests that prove:

```python
assert page.mouse.wheel_calls == [(0, 120), (0, 120)]
assert caught.value.completed_switches == 0
assert caught.value.wheel_events == 2
```

when the first 450 ms observation expires unchanged and the second pulse
changes the feed. Add an always-unchanged fake-clock test asserting termination
at the eight-second deadline without exceeding the existing viewport-derived
pulse bound. Repeat one direction assertion with `(0, -120)`.

- [ ] **Step 3: Run the new video-switch tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_video_switch.py -q
```

Expected: the delayed-commit test shows more than one wheel event because the
current observer returns immediately on its first unchanged sample.

- [ ] **Step 4: Add the timing constants**

```python
PULSE_OBSERVATION_SECONDS = 0.45
SWITCH_TIMEOUT_SECONDS = 8.0
```

Keep `WHEEL_DELTA = 120`, `STATE_POLL_SECONDS = 0.05`, and the existing
viewport-derived pulse formula unchanged.

- [ ] **Step 5: Make unchanged samples continue polling**

Refactor the observer so the old identity clears any transient candidate but
does not immediately return:

```python
async def _wait_for_stable_changed_state_until(
    page, before, *, deadline, sleep_fn
):
    candidate = None
    while _monotonic() < deadline:
        try:
            current = await _within_deadline(capture_feed_state(page), deadline)
        except _SwitchDeadlineExceeded:
            return None
        if current.fingerprint == before.fingerprint:
            candidate = None
        elif (
            candidate is not None
            and current.fingerprint == candidate.fingerprint
            and abs(current.scroll_top - candidate.scroll_top)
            <= STABILITY_TOLERANCE_PX
        ):
            return current
        else:
            candidate = current
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return None
        try:
            await _within_deadline(
                sleep_fn(min(STATE_POLL_SECONDS, remaining)),
                deadline,
            )
        except _SwitchDeadlineExceeded:
            return None
    return None
```

- [ ] **Step 6: Give each pulse its own observation window**

Inside `switch_once`, retain one total deadline and compute:

```python
pulse_deadline = min(
    deadline,
    _monotonic() + PULSE_OBSERVATION_SECONDS,
)
after = await _wait_for_stable_changed_state_until(
    page,
    before,
    deadline=pulse_deadline,
    sleep_fn=sleep_fn,
)
```

If `after` is `None` and total time remains, issue the next pulse. If total time
is exhausted, raise `video_switch_timeout`; if the pulse bound is exhausted
first, retain `video_switch_not_observed`. Count a wheel only after
`page.mouse.wheel` completes.

- [ ] **Step 7: Run focused and related tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_video_switch.py tests/test_browser_actions.py tests/test_browser_strategy_runtime.py -q
```

Expected: all tests pass, exact delta remains `120`, and ignored pulses do not
increase `completed_switches`.

- [ ] **Step 8: Record the non-Git checkpoint**

Append Task 2 RED/GREEN evidence to `.superpowers/sdd/progress.md`.

---

### Task 3: Comment-entry readiness before a single click

**Files:**
- Modify: `browser_actions.py:29-34`
- Modify: `browser_actions.py:138-151`
- Modify: `browser_actions.py:370-425`
- Modify: `tests/test_browser_actions.py:479-665`

**Interfaces:**
- Produces: `_resolve_comment_entry_when_ready(page, alias, elements, *, sleep_fn, monotonic_fn, timeout_seconds=3.0) -> tuple[dict, ResolvedElement]`
- Consumes: `_resolve_action_element`, `_is_comment_entry`, `LocatorResolutionError`
- Preserves: `_dispatch_resolved_click` and `_observe_comment_panel` one-click postcondition contract

- [ ] **Step 1: Add a delayed-comment-entry RED test**

Patch `resolve_element` to raise `element_candidate_not_found` twice, then
return a valid comment entry:

```python
assert resolution_attempts == 3
assert page.mouse.click_calls == 1
assert result["postcondition"] == "observed"
```

The fake clock/sleep advances by 0.1 seconds per readiness poll.

- [ ] **Step 2: Add non-retry and timeout RED tests**

Add separate tests proving:

```python
assert ambiguous_resolution_attempts == 1
assert page.mouse.click_calls == 0
```

for `element_candidate_ambiguous`, and:

```python
assert page.mouse.click_calls == 0
assert caught.value.code == "element_candidate_not_found"
```

when the full three-second readiness window expires. Keep the existing
postcondition-timeout test asserting exactly one click.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_actions.py -q
```

Expected: delayed resolution fails immediately with one resolver attempt.

- [ ] **Step 4: Add the bounded pre-dispatch helper**

```python
COMMENT_ENTRY_RESOLUTION_TIMEOUT_SECONDS = 3.0
COMMENT_ENTRY_RESOLUTION_POLL_SECONDS = 0.1
_TRANSIENT_COMMENT_ENTRY_CODES = frozenset(
    {"element_candidate_not_found", "element_scope_not_found"}
)


async def _resolve_comment_entry_when_ready(
    page,
    alias,
    elements,
    *,
    sleep_fn,
    monotonic_fn,
    timeout_seconds=COMMENT_ENTRY_RESOLUTION_TIMEOUT_SECONDS,
):
    deadline = monotonic_fn() + timeout_seconds
    while True:
        try:
            return await _resolve_action_element(page, alias, elements)
        except LocatorResolutionError as error:
            if error.code not in _TRANSIENT_COMMENT_ENTRY_CODES:
                raise
            remaining = deadline - monotonic_fn()
            if remaining <= 0:
                raise
            await asyncio.wait_for(
                sleep_fn(min(COMMENT_ENTRY_RESOLUTION_POLL_SECONDS, remaining)),
                timeout=remaining,
            )
```

Convert a sleep timeout back to the most recent `LocatorResolutionError` rather
than exposing `asyncio.TimeoutError`.

- [ ] **Step 5: Select readiness only for recognized comment entries**

Normalize the definition before resolving. For a click whose alias/definition
matches `_is_comment_entry`, call the readiness helper. For every other move,
click, or keyboard-input action, retain one immediate resolution. Pass the
returned fresh locator to `_dispatch_resolved_click` exactly once.

- [ ] **Step 6: Run focused and related tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_actions.py tests/test_browser_element_resolver.py tests/test_browser_strategy_runtime.py -q
```

Expected: all tests pass; readiness retries only the two transient resolution
codes, and all click-count assertions remain one or zero.

- [ ] **Step 7: Record the non-Git checkpoint**

Append Task 3 RED/GREEN evidence to `.superpowers/sdd/progress.md`.

---

### Task 4: Preserve completed actions on later failure

**Files:**
- Modify: `browser_strategy_runtime.py:202-230`
- Modify: `browser_strategy_runtime.py:454-479`
- Modify: `browser_strategy_runtime.py:526-630`
- Modify: `gateway/app.py:4903-4995`
- Modify: `tests/test_browser_strategy_runtime.py:138-190`
- Modify: `tests/test_app.py` near `public_strategy_failure_result` tests

**Interfaces:**
- Produces on `BlockExecutionError`: `completed_actions: list[dict]`
- Produces on `StrategyRuntimeError`: `completed_actions: list[dict]`
- Produces in public failed result: `actions: list[dict]`
- Consumes: existing `_safe_action_result` and `public_strategy_action_result`

- [ ] **Step 1: Add runtime RED coverage**

Run a successful scroll followed by a failing click and assert:

```python
assert caught.value.completed_actions == [
    {
        "action_id": "scroll-1",
        "type": "scroll_down",
        "status": "ok",
        "cycle": 1,
        "action_index": 1,
        "requested_switches": 1,
        "completed_switches": 1,
        "wheel_events": 6,
        "switches": [
            {"from": "a1b2c3d4e5f6", "to": "c3d4e5f6a7b8", "wheel_events": 6}
        ],
    }
]
```

Construct `StrategyRuntimeError` from it and assert the same safe list is
available there.

- [ ] **Step 2: Add public failure projection RED coverage**

Call `public_strategy_failure_result` for action 2 and assert that its
`actions[0]` contains the earlier scroll counts and hashed switch identities,
while serialized output excludes input text, selectors, and full fingerprints.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py tests/test_app.py -q
```

Expected: `completed_actions` is absent and the public failure result has no
`actions`.

- [ ] **Step 4: Carry only already-safe action results**

Add `completed_actions` to `BlockExecutionError` and copy `results` at the
failure point:

```python
raise BlockExecutionError(
    action["id"],
    index,
    action["type"],
    str(error),
    error_recoveries,
    locator=_safe_locator_error(error),
    source=error,
    cycle=cycles,
    completed_actions=[dict(item) for item in results],
) from error
```

Copy that list through `StrategyRuntimeError`. Do not copy raw action return
values; `results` already contains `_safe_action_result` projections.

- [ ] **Step 5: Project ordered completed actions in the failure response**

For each stored safe action, call `_canonical_action_occurrence`. Require that
its `(cycle, action_index)` precedes the failing occurrence, remains strictly
ordered, and matches the canonical strategy action. Project it through
`public_strategy_action_result`. If any item fails validation, return an empty
`actions` list rather than exposing the raw item.

- [ ] **Step 6: Run focused and related tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py tests/test_app.py tests/test_browser_routes.py -q
```

Expected: all tests pass and later locator failure responses retain prior safe
scroll measurements.

- [ ] **Step 7: Record the non-Git checkpoint**

Append Task 4 RED/GREEN evidence to `.superpowers/sdd/progress.md`.

---

### Task 5: Full automated verification

**Files:**
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: all production changes from Tasks 1-4
- Produces: fresh whole-project verification evidence

- [ ] **Step 1: Compile all changed Python modules**

Run:

```powershell
.\.venv\Scripts\python.exe -m py_compile browser_public_identity.py browser_video_switch.py browser_actions.py browser_strategy_runtime.py gateway/app.py gateway/browser_orchestrator.py
```

Expected: exit code 0 and no output.

- [ ] **Step 2: Run the complete Python suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Expected: exit code 0 with zero failures. If the known inaccessible
`work/pytest-tmp` prevents root discovery, run the repository's established
supported test-file selection and record both the discovery blocker and exact
passing count without claiming root discovery succeeded.

- [ ] **Step 3: Run the complete JavaScript suite**

Run:

```powershell
node --test tests-js/*.test.js
```

Expected: exit code 0 with zero failures.

- [ ] **Step 4: Run a synthetic privacy scan**

Use only fake identifiers:

```powershell
.\.venv\Scripts\python.exe -c "from gateway.app import public_browser_payload; import json; raw='profile-synthetic-abcd'; value=public_browser_payload({'profile_id':raw,'results':[{'profile_id':raw}]}); text=json.dumps(value); assert raw not in text; assert text.count('***abcd')==2"
```

Expected: exit code 0.

- [ ] **Step 5: Record exact verification evidence**

Append compile status, Python/Node pass counts, warnings, and any pre-existing
environment blocker to `.superpowers/sdd/progress.md`.

---

### Task 6: Authorized Task 9 rerun and cleanup

**Files:**
- Modify: `.superpowers/sdd/verified-scroll-task-9-report.md`
- Modify: `.superpowers/sdd/verified-scroll-task-9-review.md`
- Modify: `docs/superpowers/reports/2026-07-25-verified-scroll-and-locators-verification.md`
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Consumes: canonical browser routes, existing TikTok locator template, the two authorized internal Profile IDs, and production runtime from Tasks 1-5
- Produces: masked live evidence and final acceptance status

- [ ] **Step 1: Establish the safety baseline**

Record:

- active `config.json` SHA-256;
- canonical element and strategy signatures;
- pre-run state of only the two authorized profiles;
- current `logs/browser_operations.jsonl` line count and JSON validity;
- absence of the two full authorized Profile IDs from current public logs.

Do not enumerate, start, stop, navigate, or inspect another profile.

- [ ] **Step 2: Start and tile only the authorized profiles**

Use the canonical product start/tile route with exactly the two authorized
internal IDs. Confirm the public response contains `***xcto` and `***xctm` and
contains neither full ID.

- [ ] **Step 3: Perform the read-only locator draft check**

Fetch the TikTok comment template and call
`POST /api/browser/elements/test` without saving. Require the active-video
comment entry to resolve independently on both profiles. Input and submit may
remain unresolved while the panel is closed.

- [ ] **Step 4: Run exact-three downward switching once**

Save a collision-free temporary strategy whose only action is:

```json
{
  "type": "scroll_down",
  "params": {
    "total_count": [3, 3],
    "interval_seconds": [0.1, 0.3]
  }
}
```

Dispatch it once per profile through the canonical execute route. Do not retry
an execution. Record requested/completed switches, wheel events, safe switch
hashes, recovery count, and public Profile ID.

- [ ] **Step 5: Run exact-three upward switching only with a valid precondition**

For each profile that completed a downward transition and remains on the
resulting non-initial video, dispatch one canonical upward `[3, 3]` action.
Record a profile without that precondition as `precondition-not-met` and send no
upward action to it.

- [ ] **Step 6: Verify post-switch comment-entry readiness without posting**

After one verified downward switch, execute only a comment-entry click action.
Do not include keyboard input or submit actions. Require either:

- one click followed by the visible-comment-panel postcondition; or
- a bounded locator failure with zero clicks.

Record locator attempts, final candidate, postcondition, and duplicate-click
count. Close the comment panel only if the product provides an existing safe
close action; otherwise leave the page unchanged for cleanup.

- [ ] **Step 7: Verify persistence**

Refresh the dashboard and restart once through:

```powershell
.\.venv\Scripts\pythonw.exe launcher.py
```

Compare the temporary strategy/element signatures before and after restart.
Then remove only the temporary Task 9 aliases and strategies through canonical
settings APIs. Confirm unrelated canonical state and active `config.json`
return byte-for-byte to their pre-run signatures. Do not delete managed config
backups.

- [ ] **Step 8: Stop and verify only the two Task 9 profiles**

Send one canonical stop request for each profile opened by this rerun. Poll
read-only active state until both are `Inactive`; do not resend stop merely
because shutdown is asynchronous.

- [ ] **Step 9: Verify log privacy and structure**

Parse every line of `logs/browser_operations.jsonl` as JSON. Confirm neither
full authorized ID appears and the rerun entries contain only `***xcto` and
`***xctm`. Do not create an unredacted backup.

- [ ] **Step 10: Append the rerun report**

Retain the original blocked attempt. Append:

- production revision scope;
- automated test evidence;
- exact live dispatch counts;
- down/up measurements;
- comment-entry click count and postcondition;
- persistence signatures;
- cleanup state;
- log privacy evidence;
- an honest PASS, FAIL, or BLOCKED verdict.

- [ ] **Step 11: Final verification checkpoint**

Re-run the changed-module compilation and focused Python tests after cleanup.
Update `.superpowers/sdd/progress.md`,
`.superpowers/sdd/verified-scroll-task-9-review.md`, and the main verification
report with exact evidence. Do not state Task 9 passed unless all success
criteria in the approved design are met.
