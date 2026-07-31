# Selector Probe Comment Panel Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent final A11y capture and Dry-Run while TikTok's comment panel is loading, retry the complete transition three times, and keep readiness failures out of LLM repair.

**Architecture:** Add a private semantic stability gate to `ProbeStateRunner`, then let the existing observe loop reload and retry that transition up to three times. Preserve safe state-runner codes through validation and healing without changing any external interface or persistence path.

**Tech Stack:** Python 3, asyncio, Playwright/CDP semantic snapshots, pytest.

## Global Constraints

- Each readiness attempt lasts at most 60 seconds.
- Sample every two seconds.
- Require three consecutive identical eligible fingerprints.
- Retry the complete comment-panel transition at most three times.
- Do not add an endpoint, database schema, Redis format, setting, or UI.
- Do not change `worker.py`, `store.py`, alert policy, screenshots, or cleanup.
- Readiness failures must not invoke LLM repair.
- Existing selector failures retain the existing repair behavior.

## File Map

- Modify `selector_probe/state_runner.py`: semantic sample, fingerprint, and 60-second stability gate.
- Modify `selector_probe/probe.py`: three complete comment-transition attempts and safe infrastructure result propagation.
- Modify `selector_probe/validator.py`: retain safe state-transition codes during alias aggregation.
- Modify `selector_probe/healing_runtime.py`: classify comment readiness as infrastructure.
- Modify `tests/test_selector_probe_state_runner.py`: stability-gate tests.
- Modify `tests/test_selector_probe_observe.py`: reload/retry and capture-order test.
- Modify `tests/test_selector_probe_validator.py`: state code propagation test.
- Modify `tests/test_selector_probe_healing_runtime.py`: readiness classification and no-LLM tests.

---

### Task 1: Semantic Stability Gate and Observe Retry

**Files:**
- Modify: `selector_probe/state_runner.py:1-545`
- Modify: `selector_probe/probe.py:424-535`
- Test: `tests/test_selector_probe_state_runner.py`
- Test: `tests/test_selector_probe_observe.py`

**Interfaces:**
- Consumes: `extract_semantic_snapshot(page) -> SemanticSnapshot`, existing `scope_resolver(page, "visible_comment_panel")`, `sleep_fn`, and `monotonic_fn`.
- Produces: existing `ensure_state(..., "comment_panel_open", ...) -> dict`; successful result also contains `stable_samples`, `required_samples`, and `fingerprint_hash`.
- Produces: existing `ProbeSafetyError.code`, using `comment_panel_readiness_timeout`, `comment_panel_snapshot_unstable`, or `comment_panel_element_missing`.
- Keeps all constructor callers valid; new constructor parameters are optional and internal.

- [ ] **Step 1: Add failing state-runner tests**

Add these helpers near existing `no_sleep` and `readiness` helpers in
`tests/test_selector_probe_state_runner.py`:

```python
def panel_sample(**overrides):
    value = {
        "panel_visible": True,
        "input_visible": True,
        "textbox_visible": True,
        "submit_visible": True,
        "submit_disabled": True,
        "loading_marker": "",
        "aria_busy": False,
        "fingerprint_hash": "sha256:stable",
    }
    value.update(overrides)
    return value


def panel_sequence(*samples):
    calls = []

    async def check(_page):
        index = min(len(calls), len(samples) - 1)
        calls.append(index)
        return dict(samples[index])

    check.calls = calls
    return check


async def stable_panel(_page):
    return panel_sample()
```

Add these tests:

```python
def test_comment_panel_requires_three_identical_eligible_samples():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)
        samples = panel_sequence(
            panel_sample(fingerprint_hash="sha256:a"),
            panel_sample(fingerprint_hash="sha256:b"),
            panel_sample(fingerprint_hash="sha256:b"),
            panel_sample(fingerprint_hash="sha256:b"),
        )

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=60,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {runner.comment_entry_alias: entry_definition()},
        )

        assert result["state"] == "comment_panel_open"
        assert result["stable_samples"] == 3
        assert result["fingerprint_hash"] == "sha256:b"
        assert len(samples.calls) == 4
        assert locator.click_count == 1

    asyncio.run(scenario())


def test_comment_panel_spinner_times_out_without_becoming_open():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)
        samples = panel_sequence(
            panel_sample(
                loading_marker='[role="progressbar"]',
                fingerprint_hash="",
            )
        )

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=2,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {runner.comment_entry_alias: entry_definition()},
            )

        assert caught.value.code == "comment_panel_readiness_timeout"
        assert runner.current_state == "feed_ready"
        assert locator.click_count == 1

    asyncio.run(scenario())


def test_comment_panel_changing_snapshot_is_unstable():
    async def scenario():
        samples = panel_sequence(
            panel_sample(fingerprint_hash="sha256:a"),
            panel_sample(fingerprint_hash="sha256:b"),
            panel_sample(fingerprint_hash="sha256:c"),
        )
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=3,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {runner.comment_entry_alias: entry_definition()},
            )

        assert caught.value.code == "comment_panel_snapshot_unstable"

    asyncio.run(scenario())


def test_stable_panel_missing_submit_is_selector_failure():
    async def scenario():
        samples = panel_sequence(
            panel_sample(
                submit_visible=False,
                fingerprint_hash="sha256:missing",
            )
        )
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {runner.comment_entry_alias: entry_definition()},
            )

        assert caught.value.code == "comment_panel_element_missing"

    asyncio.run(scenario())


def test_control_fingerprint_ignores_dynamic_comment_content():
    controls = {
        "panel_role": "region",
        "aria_busy": False,
        "input_count": 1,
        "input_role": "textbox",
        "input_name": "Add comment",
        "input_data_e2e": "comment-input",
        "input_aria_label": "Add comment",
        "contenteditable": "true",
        "submit_count": 1,
        "submit_role": "button",
        "submit_name": "Post",
        "submit_data_e2e": "comment-post",
        "submit_aria_label": "Post",
        "submit_disabled": True,
    }
    assert ProbeStateRunner._hash_panel_controls(
        {**controls, "comment_text": "first"}
    ) == ProbeStateRunner._hash_panel_controls(
        {**controls, "comment_text": "changed"}
    )
```

Pass `panel_readiness_check=stable_panel` to existing tests that successfully
open or reuse a comment panel. This keeps those tests focused on click,
override, close, and transition behavior.

- [ ] **Step 2: Run state-runner tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_state_runner.py -q -p no:cacheprovider
```

Expected: new tests fail because the constructor and stability gate do not yet
exist.

- [ ] **Step 3: Add semantic sampling and stability gate**

In `selector_probe/state_runner.py`, add imports and constants:

```python
import hashlib
import json
from collections.abc import Mapping

from selector_probe.snapshot import extract_semantic_snapshot


_COMMENT_PANEL_LOADING_MARKERS = (
    '[data-e2e*="skeleton" i]',
    '[class*="skeleton" i]',
    '[data-e2e*="loading" i]',
    '[role="progressbar"]',
    '[aria-busy="true"]',
)
_COMMENT_PANEL_SHELL_SELECTOR = (
    'section:has([data-e2e="comment-input"]), '
    'section:has([data-e2e="comment-post"]), '
    'section:has([data-e2e*="comment-list" i]), '
    '[role="dialog"]:has([data-e2e*="comment" i])'
)
_COMMENT_PANEL_STABLE_SAMPLES = 3
```

Add the callback type:

```python
PanelReadinessCheck = Callable[[Any], Awaitable[dict]]
```

Extend `ProbeStateRunner.__init__` with optional parameters:

```python
panel_readiness_check: PanelReadinessCheck | None = None,
comment_readiness_timeout_seconds: float = 60.0,
comment_readiness_poll_interval_seconds: float = 2.0,
```

Validate both values are positive, then assign:

```python
self.panel_readiness_check = (
    panel_readiness_check or self._comment_panel_readiness_sample
)
self.comment_readiness_timeout_seconds = (
    comment_readiness_timeout_seconds
)
self.comment_readiness_poll_interval_seconds = (
    comment_readiness_poll_interval_seconds
)
```

Add these helpers to `ProbeStateRunner`:

```python
@staticmethod
def _hash_panel_controls(value: Mapping[str, object]) -> str:
    fields = (
        "panel_role",
        "aria_busy",
        "input_count",
        "input_role",
        "input_name",
        "input_data_e2e",
        "input_aria_label",
        "contenteditable",
        "submit_count",
        "submit_role",
        "submit_name",
        "submit_data_e2e",
        "submit_aria_label",
        "submit_disabled",
    )
    encoded = json.dumps(
        {key: value.get(key) for key in fields},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()

@staticmethod
def _node_inside_panel(node: object, bounds: Mapping[str, object]) -> bool:
    node_bounds = getattr(node, "bounds", None)
    if (
        not isinstance(node_bounds, tuple)
        or len(node_bounds) != 4
        or not all(isinstance(item, (int, float)) for item in node_bounds)
    ):
        return False
    x, y, width, height = node_bounds
    center_x = x + width / 2
    center_y = y + height / 2
    left = float(bounds["x"])
    top = float(bounds["y"])
    return (
        left <= center_x <= left + float(bounds["width"])
        and top <= center_y <= top + float(bounds["height"])
    )

async def _visible_panel_locator(self, page: Any) -> Any | None:
    try:
        locator, _diagnostics = await self.scope_resolver(
            page,
            "visible_comment_panel",
        )
        return locator
    except LocatorResolutionError as error:
        if error.code != "element_scope_not_found":
            raise ProbeSafetyError(
                "probe_panel_check_failed",
                "verify_comment_panel",
            ) from None

    shells = page.locator(_COMMENT_PANEL_SHELL_SELECTOR)
    visible = []
    for index in range(min(await shells.count(), 10)):
        candidate = shells.nth(index)
        if await candidate.is_visible():
            visible.append(candidate)
    return visible[0] if len(visible) == 1 else None

async def _comment_panel_readiness_sample(self, page: Any) -> dict:
    panel = await self._visible_panel_locator(page)
    if panel is None:
        return {
            "panel_visible": False,
            "input_visible": False,
            "textbox_visible": False,
            "submit_visible": False,
            "submit_disabled": False,
            "loading_marker": "",
            "aria_busy": False,
            "fingerprint_hash": "",
        }

    bounds = await panel.bounding_box()
    if not isinstance(bounds, Mapping):
        return {
            "panel_visible": False,
            "input_visible": False,
            "textbox_visible": False,
            "submit_visible": False,
            "submit_disabled": False,
            "loading_marker": "",
            "aria_busy": False,
            "fingerprint_hash": "",
        }

    loading_marker = ""
    for selector in _COMMENT_PANEL_LOADING_MARKERS:
        markers = panel.locator(selector)
        for index in range(min(await markers.count(), 20)):
            if await markers.nth(index).is_visible():
                loading_marker = selector
                break
        if loading_marker:
            break

    snapshot = await extract_semantic_snapshot(page)
    visible_nodes = [
        node
        for node in snapshot.nodes
        if node.visible
        and node.in_viewport
        and self._node_inside_panel(node, bounds)
    ]
    inputs = [
        node
        for node in visible_nodes
        if node.attributes.get("data-e2e") == "comment-input"
    ]
    textboxes = [
        node
        for node in visible_nodes
        if node.role == "textbox"
        and (
            node.attributes.get("contenteditable") == "true"
            or node.states.get("editable") is True
        )
    ]
    submits = [
        node
        for node in visible_nodes
        if node.attributes.get("data-e2e") == "comment-post"
    ]
    input_node = textboxes[0] if len(textboxes) == 1 else None
    submit_node = submits[0] if len(submits) == 1 else None
    aria_busy = (await panel.get_attribute("aria-busy")) == "true"
    panel_role = str(await panel.get_attribute("role") or "")
    semantic = {
        "panel_role": panel_role,
        "aria_busy": aria_busy,
        "input_count": len(inputs),
        "input_role": input_node.role if input_node else "",
        "input_name": input_node.name[:160] if input_node else "",
        "input_data_e2e": (
            input_node.attributes.get("data-e2e", "") if input_node else ""
        ),
        "input_aria_label": (
            input_node.attributes.get("aria-label", "") if input_node else ""
        ),
        "contenteditable": (
            input_node.attributes.get("contenteditable", "")
            if input_node
            else ""
        ),
        "submit_count": len(submits),
        "submit_role": submit_node.role if submit_node else "",
        "submit_name": submit_node.name[:160] if submit_node else "",
        "submit_data_e2e": (
            submit_node.attributes.get("data-e2e", "")
            if submit_node
            else ""
        ),
        "submit_aria_label": (
            submit_node.attributes.get("aria-label", "")
            if submit_node
            else ""
        ),
        "submit_disabled": (
            submit_node.states.get("disabled") is True
            if submit_node
            else False
        ),
    }
    return {
        "panel_visible": True,
        "input_visible": len(inputs) == 1,
        "textbox_visible": len(textboxes) == 1,
        "submit_visible": len(submits) == 1,
        "submit_disabled": semantic["submit_disabled"],
        "loading_marker": loading_marker,
        "aria_busy": aria_busy,
        "fingerprint_hash": self._hash_panel_controls(semantic),
    }

async def _wait_for_comment_panel_ready(self, page: Any) -> dict:
    deadline = (
        self.monotonic_fn()
        + self.comment_readiness_timeout_seconds
    )
    previous = ""
    stable = 0
    saw_eligible = False

    while True:
        try:
            sample = await self.panel_readiness_check(page)
        except asyncio.CancelledError:
            raise
        except Exception:
            sample = {}

        eligible = (
            sample.get("panel_visible") is True
            and not sample.get("loading_marker")
            and sample.get("aria_busy") is False
        )
        fingerprint = str(sample.get("fingerprint_hash") or "")
        if eligible and fingerprint:
            saw_eligible = True
            stable = stable + 1 if fingerprint == previous else 1
            previous = fingerprint
            if stable >= _COMMENT_PANEL_STABLE_SAMPLES:
                if not all(
                    sample.get(key) is True
                    for key in (
                        "input_visible",
                        "textbox_visible",
                        "submit_visible",
                    )
                ):
                    raise ProbeSafetyError(
                        "comment_panel_element_missing",
                        "open_comment_panel",
                    )
                return {
                    **sample,
                    "stable_samples": stable,
                    "required_samples": _COMMENT_PANEL_STABLE_SAMPLES,
                }
        else:
            previous = ""
            stable = 0

        if self.monotonic_fn() >= deadline:
            raise ProbeSafetyError(
                (
                    "comment_panel_snapshot_unstable"
                    if saw_eligible
                    else "comment_panel_readiness_timeout"
                ),
                "open_comment_panel",
            )
        await self.sleep_fn(
            self.comment_readiness_poll_interval_seconds
        )
```

Replace `_open_comment_panel`'s `_wait_for_panel_state(..., visible=True)`
check with:

```python
readiness = await self._wait_for_comment_panel_ready(page)
self.current_state = "comment_panel_open"
return {
    "state": self.current_state,
    "clicked": True,
    "alias": self.comment_entry_alias,
    "panel_visible": True,
    "stable_samples": readiness["stable_samples"],
    "required_samples": readiness["required_samples"],
    "fingerprint_hash": readiness["fingerprint_hash"],
}
```

In both retained-panel branches of `ensure_state`, call
`_wait_for_comment_panel_ready(page)` before setting or returning
`comment_panel_open`. Do not click an already open panel.

- [ ] **Step 4: Run state-runner tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_state_runner.py -q -p no:cacheprovider
```

Expected: all tests pass. Existing click tests still show one click per
attempt.

- [ ] **Step 5: Add failing observe retry test**

Add to `tests/test_selector_probe_observe.py`:

```python
def test_comment_readiness_reloads_three_times_before_snapshot():
    calls = []
    snapshots = []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            calls.append((state, kwargs.get("initial_action", "")))
            comment_calls = sum(
                item[0] == "comment_panel_open" for item in calls
            )
            if state == "comment_panel_open" and comment_calls < 3:
                raise ProbeSafetyError(
                    "comment_panel_readiness_timeout",
                    "open_comment_panel",
                )
            return {"state": state, "ready": True}

    class Snapshot:
        def model_payload(self):
            return {
                "scope": "page",
                "nodes": [
                    {
                        "role": "button",
                        "name": "comments",
                        "states": {},
                        "attributes": {"data-e2e": "comment-icon"},
                        "visible": True,
                        "in_viewport": True,
                        "actionable": True,
                    }
                ],
            }

    async def snapshot(_page):
        snapshots.append(len(calls))
        return Snapshot()

    async def scenario():
        records = await probe_module._default_observe_page(
            object(),
            config(),
            {
                "feed": {"scope": "page"},
                "panel": {"scope": "visible_comment_panel"},
            },
            state_runner_factory=lambda _config: Runner(),
            snapshot_extractor=snapshot,
            element_inspector=lambda _page, alias, definition: (
                asyncio.sleep(
                    0,
                    result={
                        "status": "ok",
                        "alias": alias,
                        "scope": definition["scope"],
                    },
                )
            ),
            heartbeat=SimpleNamespace(
                require_owned=lambda renew=False: None
            ),
            stop_event=None,
        )

        assert len(records) == 2
        assert [item for item in calls if item[0] == "comment_panel_open"] == [
            ("comment_panel_open", ""),
            ("comment_panel_open", ""),
            ("comment_panel_open", ""),
        ]
        assert [item for item in calls if item[0] == "feed_ready"] == [
            ("feed_ready", "navigate"),
            ("feed_ready", "reload"),
            ("feed_ready", "reload"),
        ]
        assert len(snapshots) == 2

    asyncio.run(scenario())
```

- [ ] **Step 6: Run observe test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_observe.py::test_comment_readiness_reloads_three_times_before_snapshot -q -p no:cacheprovider
```

Expected: test fails because comment transition currently has one attempt.

- [ ] **Step 7: Implement three complete observe attempts**

In `_default_observe_page`, use:

```python
attempts = 3
```

for both `feed_ready` and `comment_panel_open`. Add:

```python
retryable_comment_codes = {
    "comment_panel_readiness_timeout",
    "comment_panel_snapshot_unstable",
}
```

In the `ProbeSafetyError` handler, before terminal progress and `raise`, add:

```python
if (
    state == "comment_panel_open"
    and code in retryable_comment_codes
    and attempt < attempts
):
    await runner.ensure_state(
        page,
        "feed_ready",
        dict(elements),
        initial_action="reload",
    )
    continue
```

Do not retry `comment_panel_element_missing`; that is a selector failure.
Snapshot extraction remains after the transition loop, so failed attempts
cannot capture an incomplete A11y tree.

- [ ] **Step 8: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_state_runner.py tests\test_selector_probe_observe.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```powershell
git add selector_probe\state_runner.py selector_probe\probe.py tests\test_selector_probe_state_runner.py tests\test_selector_probe_observe.py
git commit -m "fix: wait for stable comment panel"
```

---

### Task 2: Preserve Readiness Classification and Skip LLM

**Files:**
- Modify: `selector_probe/validator.py:40-160,1231-1410`
- Modify: `selector_probe/healing_runtime.py:40-70,716-760`
- Modify: `selector_probe/probe.py:1345-1430,1680-1730`
- Test: `tests/test_selector_probe_validator.py`
- Test: `tests/test_selector_probe_healing_runtime.py`
- Test: `tests/test_selector_probe_observe.py`

**Interfaces:**
- Consumes: safe `ProbeSafetyError.code` from Task 1.
- Produces: existing `ValidationRejected.code` and `required_state`.
- Produces: existing healing result fields `failure_code`, `required_state`, and `proposed_pause_aliases`.
- Does not add evidence, persistence, API, or UI fields.

- [ ] **Step 1: Add failing validator code-propagation test**

Add these imports in `tests/test_selector_probe_validator.py`:

```python
from selector_probe.state_runner import ProbeSafetyError
from selector_probe.validator import _ensure_state
```

Then add:

```python
def test_ensure_state_preserves_safe_comment_readiness_code():
    class Runner:
        async def ensure_state(self, *_args, **_kwargs):
            raise ProbeSafetyError(
                "comment_panel_readiness_timeout",
                "open_comment_panel",
            )

    async def scenario():
        with pytest.raises(ValidationRejected) as caught:
            await _ensure_state(
                Runner(),
                object(),
                "comment_panel_open",
                {},
                "required_state_failed",
            )

        assert caught.value.code == "comment_panel_readiness_timeout"
        assert caught.value.required_state == "comment_panel_open"

    asyncio.run(scenario())
```

- [ ] **Step 2: Run validator test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_validator.py::test_ensure_state_preserves_safe_comment_readiness_code -q -p no:cacheprovider
```

Expected: failure because `_ensure_state` returns `required_state_failed`.

- [ ] **Step 3: Preserve safe state errors and aggregate readiness first**

In `selector_probe/validator.py`, add:

```python
_COMMENT_READINESS_CODES = {
    "comment_panel_readiness_timeout",
    "comment_panel_snapshot_unstable",
}
```

Replace `_ensure_state`'s generic exception branch with:

```python
except Exception as error:
    selected = getattr(error, "code", "")
    selected_code = (
        selected
        if isinstance(selected, str) and _SAFE_CODE.fullmatch(selected)
        else code
    )
    raise ValidationRejected(
        selected_code,
        required_state=state,
    ) from None
```

Before final `ValidationRejected` in `validate_bundle_on_page`, select a
readiness failure ahead of selector aggregation:

```python
readiness_failure = next(
    (
        item
        for item in alias_failures
        if item["code"] in _COMMENT_READINESS_CODES
    ),
    None,
)
first = readiness_failure or alias_failures[0]
final_code = (
    str(first["code"])
    if readiness_failure is not None or len(alias_failures) == 1
    else "selector_validation_failed"
)
raise ValidationRejected(
    final_code,
    alias=str(first["alias"]),
    match_count=int(first["match_count"]),
    required_state=str(first["required_state"]),
    failures=alias_failures,
)
```

- [ ] **Step 4: Add failing healing classification tests**

Add to `tests/test_selector_probe_healing_runtime.py`:

```python
def test_runtime_classifies_comment_readiness_as_infrastructure():
    runtime = object.__new__(HealingRuntime)
    runtime.contracts = {
        "feed": SimpleNamespace(required_state="feed_ready"),
        "comment-input": SimpleNamespace(
            required_state="comment_panel_open"
        ),
        "comment-submit": SimpleNamespace(
            required_state="comment_panel_open"
        ),
    }

    result = runtime._validation_failure(
        ValidationRejected(
            "comment_panel_readiness_timeout",
            required_state="comment_panel_open",
        )
    )

    assert result == {
        "status": "failed",
        "failure_class": "infrastructure",
        "failed_aliases": ["comment-input", "comment-submit"],
        "code": "comment_panel_readiness_timeout",
        "required_state": "comment_panel_open",
    }


def test_comment_readiness_failure_never_calls_repair_or_model():
    calls = []
    runtime = SimpleNamespace(
        model_call=lambda *_args, **_kwargs: calls.append("model"),
        validate_active=lambda: {
            "status": "failed",
            "failure_class": "infrastructure",
            "failed_aliases": ["comment-input", "comment-submit"],
            "code": "comment_panel_snapshot_unstable",
            "required_state": "comment_panel_open",
        },
        deterministic_candidates=lambda **_kwargs: calls.append(
            "deterministic"
        ),
        fresh_validation_context=lambda **_kwargs: {},
        validate_candidate=lambda _value: {},
        repair_candidate=lambda **_kwargs: calls.append("repair"),
        full_validate=lambda _value: {},
        store_and_publish=lambda _value, _evidence: {},
    )

    result = run_healing_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "comment_panel_snapshot_unstable"
    assert result["required_state"] == "comment_panel_open"
    assert result["proposed_pause_aliases"] == [
        "comment-input",
        "comment-submit",
    ]
    assert calls == []
```

- [ ] **Step 5: Run healing tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_healing_runtime.py::test_runtime_classifies_comment_readiness_as_infrastructure tests\test_selector_probe_healing_runtime.py::test_comment_readiness_failure_never_calls_repair_or_model -q -p no:cacheprovider
```

Expected: first test fails because the error becomes `zero_match`; second fails
because generic infrastructure return discards its code and aliases.

- [ ] **Step 6: Classify readiness without selector conversion**

In `selector_probe/healing_runtime.py`, add:

```python
_COMMENT_READINESS_CODES = {
    "comment_panel_readiness_timeout",
    "comment_panel_snapshot_unstable",
}
```

At the start of `HealingRuntime._validation_failure`, add:

```python
if error.code in _COMMENT_READINESS_CODES:
    failures = getattr(error, "failures", ())
    failed_aliases = [
        str(item.get("alias"))
        for item in failures
        if isinstance(item, Mapping) and item.get("alias")
    ]
    if not failed_aliases:
        failed_aliases = [
            alias
            for alias, contract in self.contracts.items()
            if contract.required_state == "comment_panel_open"
        ]
    return {
        "status": "failed",
        "failure_class": "infrastructure",
        "failed_aliases": list(dict.fromkeys(failed_aliases)),
        "code": error.code,
        "required_state": (
            error.required_state or "comment_panel_open"
        ),
    }
```

Add `"comment_panel_element_missing"` to `_SELECTOR_FAILURE_CODES` so a stable
missing control remains repairable.

- [ ] **Step 7: Preserve infrastructure details in healing result**

In `run_healing_probe`, replace:

```python
if not active_passed and not _selector_failure(active_result):
    return _healing_result("infrastructure_unavailable")
```

with:

```python
if not active_passed and not _selector_failure(active_result):
    return _healing_result(
        "infrastructure_unavailable",
        failed_aliases=_failed_aliases(active_result),
        failure_code=str(
            active_result.get("code") or "probe_unavailable"
        ),
        required_state=str(active_result.get("required_state") or ""),
    )
```

This branch returns before deterministic generation, repair, model calls, or
publication.

- [ ] **Step 8: Run focused regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_validator.py tests\test_selector_probe_healing_runtime.py tests\test_selector_probe_observe.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 9: Commit**

```powershell
git add selector_probe\validator.py selector_probe\healing_runtime.py selector_probe\probe.py tests\test_selector_probe_validator.py tests\test_selector_probe_healing_runtime.py tests\test_selector_probe_observe.py
git commit -m "fix: preserve comment readiness failures"
```

---

## Final Verification

- [ ] Run the complete selector-probe regression set:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_state_runner.py tests\test_selector_probe_observe.py tests\test_selector_probe_validator.py tests\test_selector_probe_healing_runtime.py -q -p no:cacheprovider
```

Expected: all tests pass.

- [ ] Run one manual observe probe with both dedicated AdsPower Profiles.

Verify:

```text
comment panel remains open until three stable samples
no A11y snapshot or Dry-Run occurs during failed readiness attempts
at most three comment transition attempts per Profile/round
readiness failure_code is preserved
no LLM repair occurs for readiness timeout/instability
all probe-owned pages and Profiles close after terminal result
```

## Deferred by Core Minimal Scope

- Failed-run screenshot.
- Failed validation row when capture never begins.
- 60-second manual debug window hold.
- New worker/store pause or recovery behavior.
- Guaranteed comment-only strategy pause.
- UI or API changes.
