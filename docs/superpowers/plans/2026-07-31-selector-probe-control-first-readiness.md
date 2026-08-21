# Selector Probe Control-First Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let stable comment input controls pass readiness while TikTok continues lazy-loading the comment list.

**Architecture:** Keep the existing `ProbeStateRunner` and retry flow. Change the scoped sampler so list loading markers remain diagnostic instead of short-circuiting control inspection, then classify timeout from panel, control, and fingerprint evidence gathered across the full deadline.

**Tech Stack:** Python 3, asyncio, Playwright async API, pytest

## Global Constraints

- Keep the readiness deadline at 60 seconds in production.
- Keep three complete comment-transition attempts.
- Keep page reload and state cleanup between attempts.
- Keep existing A11y extraction and Dry-Run after readiness.
- Keep publication fail-closed.
- Add no API, UI, Redis, database, configuration, worker, or Profile-sequencing changes.
- A disabled submit button is valid when the comment input is empty.
- Comment-list Skeletons are diagnostic only; panel `aria-busy="true"` remains blocking.

---

## File Structure

- Modify `selector_probe/state_runner.py`: sample critical controls despite list loading markers and classify readiness timeouts from accumulated evidence.
- Modify `tests/test_selector_probe_state_runner.py`: cover list Skeleton tolerance, busy-panel blocking, delayed controls, missing controls, and unstable fingerprints.

No new runtime module is needed. Both changes belong to the existing state transition boundary.

### Task 1: Inspect Critical Controls Despite List Skeletons

**Files:**
- Modify: `selector_probe/state_runner.py:598-722`
- Test: `tests/test_selector_probe_state_runner.py:830-890`

**Interfaces:**
- Consumes: `ProbeStateRunner._visible_panel_locator(page) -> locator | None` and the existing scoped locators for input, textbox, and submit.
- Produces: `ProbeStateRunner._comment_panel_readiness_sample(page) -> dict` with `loading_marker` retained as diagnostics and a non-empty `fingerprint_hash` when critical controls are valid.

- [ ] **Step 1: Write the failing Skeleton-tolerance test**

Add this focused test after `test_scoped_panel_sample_ignores_old_panel_and_dynamic_comments`:

```python
def test_comment_list_skeleton_does_not_hide_ready_controls():
    async def scenario():
        panel = FakePanel()
        panel.visible_markers.add('[class*="skeleton" i]')

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        sample = await runner._comment_panel_readiness_sample(object())

        assert sample["loading_marker"] == '[class*="skeleton" i]'
        assert sample["aria_busy"] is False
        assert sample["input_visible"] is True
        assert sample["textbox_visible"] is True
        assert sample["submit_visible"] is True
        assert sample["submit_disabled"] is True
        assert sample["fingerprint_hash"].startswith("sha256:")

    asyncio.run(scenario())
```

Update `test_scoped_panel_sample_ignores_old_panel_and_dynamic_comments` so a non-busy loading marker still inspects controls:

```python
assert loading["loading_marker"] == '[class*="spinner" i]'
assert loading["input_visible"] is True
assert loading["textbox_visible"] is True
assert loading["submit_visible"] is True
assert loading["fingerprint_hash"].startswith("sha256:")
assert panel.textbox.aria_calls == 4
assert panel.textbox.editable_calls == 4
assert panel.submit.aria_calls == 4
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run:

```powershell
python -m pytest tests/test_selector_probe_state_runner.py::test_comment_list_skeleton_does_not_hide_ready_controls tests/test_selector_probe_state_runner.py::test_scoped_panel_sample_ignores_old_panel_and_dynamic_comments -q
```

Expected: both tests fail because `_comment_panel_readiness_sample` returns before resolving controls when `loading_marker` is non-empty.

- [ ] **Step 3: Remove only the list-marker short circuit**

In `_comment_panel_readiness_sample`, preserve loading-marker discovery but change the early-return condition from:

```python
if loading_marker or aria_busy:
```

to:

```python
if aria_busy:
```

Keep the returned busy sample unchanged, including its `loading_marker` diagnostic. Let non-busy samples continue through the existing scoped input, textbox, submit, A11y, editable, disabled, and fingerprint checks.

- [ ] **Step 4: Run the focused tests and full state-runner tests**

Run:

```powershell
python -m pytest tests/test_selector_probe_state_runner.py::test_comment_list_skeleton_does_not_hide_ready_controls tests/test_selector_probe_state_runner.py::test_scoped_panel_sample_ignores_old_panel_and_dynamic_comments -q
python -m pytest tests/test_selector_probe_state_runner.py -q
```

Expected: focused tests and the full state-runner file pass. Task 1 changes only
sampler evidence; the existing wait loop still blocks `loading_marker` until
Task 2 changes eligibility.

- [ ] **Step 5: Commit the sampler change**

```powershell
git add -- selector_probe/state_runner.py tests/test_selector_probe_state_runner.py
git commit -m "fix: inspect controls during list loading"
```

### Task 2: Gate on Stable Controls and Preserve Precise Errors

**Files:**
- Modify: `selector_probe/state_runner.py:724-814`
- Test: `tests/test_selector_probe_state_runner.py:730-830`

**Interfaces:**
- Consumes: readiness sample keys `panel_visible`, `aria_busy`, `input_visible`, `textbox_visible`, `submit_visible`, and `fingerprint_hash`.
- Produces: `_wait_for_comment_panel_ready(page) -> dict` after three stable complete-control samples, or `ProbeSafetyError` with `comment_panel_readiness_timeout`, `comment_panel_element_missing`, `comment_panel_snapshot_unstable`, or `probe_panel_check_failed`.

- [ ] **Step 1: Write failing wait-loop tests**

Add a passing-Skeleton case after `test_comment_panel_requires_three_identical_eligible_samples`:

```python
def test_wait_accepts_stable_controls_while_comment_list_loads():
    async def scenario():
        samples = panel_sequence(
            panel_sample(loading_marker='[class*="skeleton" i]')
        )
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=20,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )

        result = await runner._wait_for_comment_panel_ready(object())

        assert result["stable_samples"] == 3
        assert result["loading_marker"] == '[class*="skeleton" i]'
        assert len(samples.calls) == 3

    asyncio.run(scenario())
```

Add a deadline test proving missing controls do not fail after only three stable missing samples:

```python
def test_missing_controls_wait_until_deadline():
    async def scenario():
        samples = panel_sequence(
            panel_sample(
                submit_visible=False,
                fingerprint_hash="sha256:missing",
            )
        )
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=12,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )

        with pytest.raises(ProbeSafetyError) as caught:
            await runner._wait_for_comment_panel_ready(object())

        assert caught.value.code == "comment_panel_element_missing"
        assert len(samples.calls) == 4

    asyncio.run(scenario())
```

Add a delayed-control test proving three early incomplete samples do not close
the panel before controls become ready:

```python
def test_delayed_controls_can_become_stable_before_deadline():
    async def scenario():
        missing = panel_sample(
            submit_visible=False,
            fingerprint_hash="sha256:missing",
        )
        ready = panel_sample(fingerprint_hash="sha256:ready")
        samples = panel_sequence(
            missing,
            missing,
            missing,
            ready,
            ready,
            ready,
        )
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=30,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )

        result = await runner._wait_for_comment_panel_ready(object())

        assert result["fingerprint_hash"] == "sha256:ready"
        assert result["stable_samples"] == 3
        assert len(samples.calls) == 6

    asyncio.run(scenario())
```

In `test_comment_panel_rejects_loading_unstable_or_missing_controls`, replace the old perpetual-loading-marker case with a busy-panel case:

```python
(
    (
        panel_sample(
            aria_busy=True,
            fingerprint_hash="",
        ),
    ),
    2,
    "comment_panel_readiness_timeout",
),
```

Rename the test to `test_comment_panel_rejects_busy_unstable_or_missing_controls`.

- [ ] **Step 2: Run new tests and verify failure**

Run:

```powershell
python -m pytest tests/test_selector_probe_state_runner.py::test_wait_accepts_stable_controls_while_comment_list_loads tests/test_selector_probe_state_runner.py::test_missing_controls_wait_until_deadline tests/test_selector_probe_state_runner.py::test_delayed_controls_can_become_stable_before_deadline -q
```

Expected: Skeleton case times out; persistent-missing case fails after three
samples instead of waiting for the deadline; delayed-control case also fails
after its third incomplete sample.

- [ ] **Step 3: Track non-busy panel evidence for timeout classification**

In `_wait_for_comment_panel_ready`, add state beside `saw_eligible`:

```python
saw_non_busy_panel = False
```

Replace `timeout_error` with:

```python
def timeout_error() -> ProbeSafetyError:
    if saw_eligible:
        code = "comment_panel_snapshot_unstable"
    elif saw_non_busy_panel:
        code = "comment_panel_element_missing"
    else:
        code = "comment_panel_readiness_timeout"
    return ProbeSafetyError(code, "open_comment_panel")
```

This preserves readiness timeout for an absent or perpetually busy panel, distinguishes incomplete controls, and reserves instability for controls that were complete at least once.

- [ ] **Step 4: Make complete critical controls the eligibility boundary**

Replace the current `eligible` block with:

```python
panel_non_busy = (
    sample.get("panel_visible") is True
    and sample.get("aria_busy") is False
)
if panel_non_busy:
    saw_non_busy_panel = True

controls_complete = all(
    sample.get(key) is True
    for key in (
        "input_visible",
        "textbox_visible",
        "submit_visible",
    )
)
eligible = panel_non_busy and controls_complete
```

Do not include `loading_marker` in `eligible`. Keep `fingerprint_hash` required. When three identical eligible fingerprints are observed, return the sample directly:

```python
if eligible and fingerprint:
    saw_eligible = True
    stable = stable + 1 if fingerprint == previous else 1
    previous = fingerprint
    if stable >= _COMMENT_PANEL_STABLE_SAMPLES:
        return {
            **sample,
            "stable_samples": stable,
            "required_samples": _COMMENT_PANEL_STABLE_SAMPLES,
        }
else:
    previous = ""
    stable = 0
```

Remove the old early `comment_panel_element_missing` raise after three fingerprints. Incomplete controls now remain ineligible until the deadline.

- [ ] **Step 5: Run focused and regression tests**

Run:

```powershell
python -m pytest tests/test_selector_probe_state_runner.py -q
python -m pytest tests/test_selector_probe_state_runner.py tests/test_selector_probe_observe.py tests/test_selector_probe_validator.py tests/test_selector_probe_healing_runtime.py -q
```

Expected: all tests pass with zero failures. Existing retry, cleanup, error propagation, observe-only, and fail-closed publication behavior remain green.

- [ ] **Step 6: Review diff and commit**

Run:

```powershell
git diff --check
git diff -- selector_probe/state_runner.py tests/test_selector_probe_state_runner.py
```

Confirm the diff contains no API, UI, Redis, database, configuration, worker, or Profile-sequencing changes. Then commit:

```powershell
git add -- selector_probe/state_runner.py tests/test_selector_probe_state_runner.py
git commit -m "fix: gate comment readiness on controls"
```
