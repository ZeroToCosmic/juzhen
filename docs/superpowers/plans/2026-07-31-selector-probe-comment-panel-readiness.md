# Selector Probe Comment Panel Readiness Implementation Plan

> **Superseded:** The user selected the core minimal design after this plan was
> written. Do not execute this plan. Replace it after the revised design spec is
> approved.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent selector capture while TikTok's comment panel is still loading, retain actionable failure evidence, and pause only comment-dependent strategies after three failed readiness attempts.

**Architecture:** Keep the existing public API, DB schema, Redis format, and page-state names. Add private semantic-stability logic to `ProbeStateRunner`, propagate its safe failure code through validation/healing, then reuse existing validation, screenshot, alert, Webhook, and alias-gate storage paths.

**Tech Stack:** Python 3.11+, asyncio, Playwright/CDP accessibility snapshots, Flask management backend, SQLite, Redis, pytest.

## Global Constraints

- No new HTTP endpoint, DB table, Redis key format, management page, or UI setting.
- Comment readiness timeout is 60 seconds per attempt.
- Sample interval is 2 seconds.
- Three consecutive stable samples are required.
- The complete comment transition retries at most three times.
- Disabled comment-submit controls are valid before comment text exists.
- Comment text, comment count, avatars, timestamps, and list contents do not participate in the fingerprint.
- Page-readiness failures do not invoke the LLM.
- Stable absence of a required control is a selector failure and may invoke the existing LLM repair flow.
- Terminal failure keeps the previous stable selector and pauses only strategies depending on `visible_comment_panel` aliases.
- Manual failure holds the probe window for 60 seconds after evidence capture; scheduled failure closes after evidence capture.
- Two dedicated Profiles and two consistent rounds per Profile remain mandatory before atomic publication or automatic recovery.
- Every owned page must close and every test Profile must become inactive at terminal cleanup.

---

## File Map

- Modify `selector_probe/state_runner.py`: semantic readiness sampling, fingerprinting, three-attempt comment transition, bounded failure evidence.
- Modify `selector_probe/validator.py`: preserve safe state-runner failure codes and evidence through two-Profile/two-round validation.
- Modify `selector_probe/healing_runtime.py`: classify readiness versus selector failure and expose bounded terminal evidence/screenshot fallback.
- Modify `selector_probe/probe.py`: preserve readiness failure results, record failed observe validations, and prevent LLM invocation for readiness failures.
- Modify `selector_probe/worker.py`: persist terminal failure validation, capture screenshot, hold manual failure window, enqueue alias-isolated failure effect.
- Modify `selector_probe/store.py`: label reused alias-isolated effects as comment readiness failures when appropriate; no schema change.
- Modify `tests/test_selector_probe_state_runner.py`: readiness gate and retry unit tests.
- Modify `tests/test_selector_probe_validator.py`: safe failure propagation tests.
- Modify `tests/test_selector_probe_healing_runtime.py`: failure classification and screenshot fallback tests.
- Modify `tests/test_selector_probe_observe.py`: failed validation persistence and zero-record regression tests.
- Modify `tests/test_selector_probe_worker.py`: manual hold, failure persistence, and effect selection tests.
- Modify `tests/test_selector_probe_policy.py`: comment-only pause and recovery behavior tests.

---

### Task 1: Stable Comment-Panel Gate

**Files:**
- Modify: `selector_probe/state_runner.py:56-545`
- Test: `tests/test_selector_probe_state_runner.py`

**Interfaces:**
- Consumes: existing `extract_semantic_snapshot(page) -> SemanticSnapshot`, `resolve_scope(page, "visible_comment_panel")`, injected `sleep_fn`, and injected `monotonic_fn`.
- Produces: existing `ensure_state(..., "comment_panel_open", ...) -> dict`, with added bounded keys `attempt`, `stable_samples`, `required_samples`, and `fingerprint_hash`.
- Produces: `ProbeSafetyError.evidence: dict[str, object]`; this remains internal and is never an HTTP API.

- [ ] **Step 1: Write failing readiness tests**

Add helpers and tests to `tests/test_selector_probe_state_runner.py`. Use a fake readiness callback so tests do not require a real browser:

```python
def panel_sample_sequence(*samples):
    calls = []

    async def check(_page):
        index = min(len(calls), len(samples) - 1)
        calls.append(index)
        return dict(samples[index])

    check.calls = calls
    return check


def stable_panel_sample(fingerprint="sha256:stable", **overrides):
    value = {
        "panel_visible": True,
        "input_visible": True,
        "textbox_visible": True,
        "submit_visible": True,
        "submit_disabled": True,
        "loading_marker": "",
        "aria_busy": False,
        "fingerprint_hash": fingerprint,
        "panel_bounds": (1200.0, 0.0, 720.0, 1080.0),
    }
    value.update(overrides)
    return value


def ready_runner_for_panel(**overrides):
    page = FakePage()
    page.url = "https://www.tiktok.com/"
    page.panel_open = True
    locator = FakeClickLocator(page, opens=True)

    async def resolver(*_args):
        return SimpleNamespace(locator=locator, candidate={"id": "entry"})

    values = {
        "target_url": "https://www.tiktok.com/",
        "readiness_check": readiness(),
        "element_resolver": resolver,
        "scope_resolver": panel_scope,
        "sleep_fn": no_sleep,
        "monotonic_fn": StepClock(step=1),
    }
    values.update(overrides)
    runner = ProbeStateRunner(**values)
    runner.current_state = "feed_ready"
    return runner, page


def test_comment_panel_requires_three_stable_samples():
    async def scenario():
        check = panel_sample_sequence(
            stable_panel_sample("sha256:a"),
            stable_panel_sample("sha256:b"),
            stable_panel_sample("sha256:b"),
            stable_panel_sample("sha256:b"),
        )
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            panel_readiness_check=check,
            panel_timeout_seconds=60,
            poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {runner.comment_entry_alias: entry_definition()},
        )
        assert result["stable_samples"] == 3
        assert result["fingerprint_hash"] == "sha256:b"
        assert len(check.calls) == 4

    asyncio.run(scenario())


def test_loading_marker_never_allows_snapshot_ready():
    async def scenario():
        check = panel_sample_sequence(
            stable_panel_sample(
                loading_marker="spinner",
                fingerprint_hash="",
            )
        )
        runner, page = ready_runner_for_panel(
            panel_readiness_check=check,
            panel_timeout_seconds=2,
            monotonic_fn=StepClock(step=1),
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {runner.comment_entry_alias: entry_definition()},
            )
        assert caught.value.code == "comment_panel_readiness_timeout"
        assert caught.value.evidence["loading_marker"] == "spinner"

    asyncio.run(scenario())


def test_stable_missing_submit_is_selector_failure():
    async def scenario():
        missing = stable_panel_sample(
            submit_visible=False,
            fingerprint="sha256:missing-submit",
        )
        check = panel_sample_sequence(missing, missing, missing)
        runner, page = ready_runner_for_panel(panel_readiness_check=check)
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {runner.comment_entry_alias: entry_definition()},
            )
        assert caught.value.code == "comment_panel_element_missing"
        assert caught.value.evidence["stable_samples"] == 3

    asyncio.run(scenario())


def test_comment_list_changes_do_not_change_control_fingerprint():
    controls = {
        "panel_role": "section",
        "aria_busy": False,
        "input_count": 1,
        "input_role": "textbox",
        "input_name": "Add comment",
        "input_data_e2e": "comment-input",
        "contenteditable": "true",
        "submit_count": 1,
        "submit_role": "button",
        "submit_name": "Post",
        "submit_data_e2e": "comment-post",
        "submit_disabled": True,
    }
    first = ProbeStateRunner._hash_panel_controls(
        {**controls, "ignored_comment_text": "first comment"}
    )
    second = ProbeStateRunner._hash_panel_controls(
        {**controls, "ignored_comment_text": "different comment"}
    )
    assert first == second
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_state_runner.py -q -p no:cacheprovider
```

Expected: new tests fail because `panel_readiness_check`, readiness evidence, and stable sampling do not exist.

- [ ] **Step 3: Extend safe error evidence and constructor defaults**

In `selector_probe/state_runner.py`, import hashing/snapshot utilities and add the internal constants:

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
_COMMENT_PANEL_ATTEMPTS = 3
_COMMENT_PANEL_STABLE_SAMPLES = 3


class ProbeSafetyError(RuntimeError):
    def __init__(
        self,
        code: str,
        action: str,
        *,
        evidence: Mapping[str, object] | None = None,
    ):
        self.code = code
        self.action = action
        self.evidence = dict(evidence or {})
        super().__init__(f"{code}: {action}")
```

Add the injectable internal callback while keeping existing callers valid:

```python
PanelReadinessCheck = Callable[[Any], Awaitable[dict]]


def __init__(
    self,
    *,
    target_url: str,
    readiness_check: ReadinessCheck | None = None,
    panel_readiness_check: PanelReadinessCheck | None = None,
    # existing parameters remain in their current order
    panel_timeout_seconds: float = 60.0,
    poll_interval_seconds: float = 2.0,
    # remaining existing parameters
):
    # existing validation and assignments
    self.panel_readiness_check = (
        panel_readiness_check or self._comment_panel_readiness_sample
    )
    self.last_panel_readiness: dict[str, object] = {}
```

- [ ] **Step 4: Implement bounded sample and fingerprint**

Add private helpers to `ProbeStateRunner`. Keep raw node IDs, DOM, names unrelated to the controls, and comment contents out of the payload:

```python
@staticmethod
def _hash_panel_controls(value: Mapping[str, object]) -> str:
    selected = {
        key: value.get(key)
        for key in (
            "panel_role",
            "aria_busy",
            "input_count",
            "input_role",
            "input_name",
            "input_data_e2e",
            "contenteditable",
            "submit_count",
            "submit_role",
            "submit_name",
            "submit_data_e2e",
            "submit_disabled",
        )
    }
    encoded = json.dumps(
        selected,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


async def _comment_panel_readiness_sample(self, page: Any) -> dict:
    snapshot = await extract_semantic_snapshot(page)
    visible = [
        node
        for node in snapshot.nodes
        if node.visible and node.in_viewport
    ]
    input_containers = [
        node for node in visible
        if node.attributes.get("data-e2e") == "comment-input"
    ]
    textboxes = [
        node for node in visible
        if node.role == "textbox"
        and node.attributes.get("contenteditable") == "true"
    ]
    submits = [
        node for node in visible
        if node.attributes.get("data-e2e") == "comment-post"
    ]
    panel_locator = None
    panel_visible = False
    panel_bounds = None
    try:
        panel_locator, _diagnostics = await self.scope_resolver(
            page, "visible_comment_panel"
        )
        panel_visible = True
        panel_bounds = await panel_locator.bounding_box()
    except LocatorResolutionError as error:
        if error.code != "element_scope_not_found":
            raise
        shells = page.locator(_COMMENT_PANEL_SHELL_SELECTOR)
        visible_shells = [
            shells.nth(index)
            for index in range(min(await shells.count(), 10))
            if await shells.nth(index).is_visible()
        ]
        if len(visible_shells) == 1:
            panel_locator = visible_shells[0]
            panel_visible = True
            panel_bounds = await panel_locator.bounding_box()

    loading_marker = ""
    loading_root = panel_locator if panel_locator is not None else page
    for selector in _COMMENT_PANEL_LOADING_MARKERS:
        locator = loading_root.locator(selector)
        for index in range(min(await locator.count(), 20)):
            if await locator.nth(index).is_visible():
                loading_marker = selector[:80]
                break
        if loading_marker:
            break

    input_container = (
        input_containers[0] if len(input_containers) == 1 else None
    )
    input_node = textboxes[0] if len(textboxes) == 1 else None
    submit_node = submits[0] if len(submits) == 1 else None
    semantic = {
        "panel_role": "section" if panel_visible else "",
        "aria_busy": any(
            node.states.get("busy") is True
            for node in visible
            if node.role in {"dialog", "region"}
        ),
        "input_count": len(input_containers),
        "input_role": input_node.role if input_node else "",
        "input_name": input_node.name[:160] if input_node else "",
        "input_data_e2e": (
            input_container.attributes.get("data-e2e", "")
            if input_container else ""
        ),
        "contenteditable": (
            input_node.attributes.get("contenteditable", "")
            if input_node else ""
        ),
        "submit_count": len(submits),
        "submit_role": submit_node.role if submit_node else "",
        "submit_name": submit_node.name[:160] if submit_node else "",
        "submit_data_e2e": (
            submit_node.attributes.get("data-e2e", "")
            if submit_node else ""
        ),
        "submit_disabled": (
            submit_node.states.get("disabled") is True
            if submit_node else False
        ),
    }
    return {
        "panel_visible": panel_visible,
        "input_visible": len(input_containers) == 1,
        "textbox_visible": len(textboxes) == 1,
        "submit_visible": len(submits) == 1,
        "submit_disabled": semantic["submit_disabled"],
        "loading_marker": loading_marker,
        "aria_busy": semantic["aria_busy"],
        "fingerprint_hash": self._hash_panel_controls(semantic),
        "panel_bounds": panel_bounds,
    }
```

- [ ] **Step 5: Implement three-sample stability and stable absence**

Add:

```python
async def _wait_for_comment_panel_ready(self, page: Any) -> dict:
    deadline = self.monotonic_fn() + self.panel_timeout_seconds
    previous = ""
    stable = 0
    eligible_seen = False
    last: dict[str, object] = {}

    while True:
        sample = await self.panel_readiness_check(page)
        last = {
            key: sample.get(key)
            for key in (
                "panel_visible",
                "input_visible",
                "textbox_visible",
                "submit_visible",
                "submit_disabled",
                "loading_marker",
                "aria_busy",
                "fingerprint_hash",
                "panel_bounds",
            )
        }
        eligible = (
            last["panel_visible"] is True
            and not last["loading_marker"]
            and last["aria_busy"] is False
        )
        fingerprint = str(last["fingerprint_hash"] or "")
        if eligible and fingerprint:
            eligible_seen = True
            stable = stable + 1 if fingerprint == previous else 1
            previous = fingerprint
            required_missing = not all(
                last[key] is True
                for key in (
                    "input_visible",
                    "textbox_visible",
                    "submit_visible",
                )
            )
            if stable >= _COMMENT_PANEL_STABLE_SAMPLES:
                evidence = {
                    **last,
                    "stable_samples": stable,
                    "required_samples": _COMMENT_PANEL_STABLE_SAMPLES,
                }
                self.last_panel_readiness = dict(evidence)
                if required_missing:
                    raise ProbeSafetyError(
                        "comment_panel_element_missing",
                        "open_comment_panel",
                        evidence=evidence,
                    )
                return evidence
        else:
            stable = 0
            previous = ""

        if self.monotonic_fn() >= deadline:
            evidence = {
                **last,
                "stable_samples": stable,
                "required_samples": _COMMENT_PANEL_STABLE_SAMPLES,
            }
            self.last_panel_readiness = dict(evidence)
            raise ProbeSafetyError(
                (
                    "comment_panel_snapshot_unstable"
                    if eligible_seen
                    else "comment_panel_readiness_timeout"
                ),
                "open_comment_panel",
                evidence=evidence,
            )
        await self.sleep_fn(self.poll_interval_seconds)
```

- [ ] **Step 6: Retry the complete comment transition three times**

Refactor `_open_comment_panel` so locator resolution/click happens once per attempt, but page reset happens between attempts:

```python
retryable = {
    "comment_panel_readiness_timeout",
    "comment_panel_snapshot_unstable",
}
for attempt in range(1, _COMMENT_PANEL_ATTEMPTS + 1):
    if attempt > 1:
        await self.dispatch(page, {"type": "reload"}, elements)
        await self.dispatch(page, {"type": "wait_ready"}, elements)
    resolved = await self.element_resolver(
        page, self.comment_entry_alias, definition
    )
    await resolved.locator.click()
    await self._require_safe_origin(page, "open_comment_panel")
    try:
        readiness = await self._wait_for_comment_panel_ready(page)
    except ProbeSafetyError as error:
        evidence = {**error.evidence, "attempt": attempt}
        if error.code not in retryable or attempt == _COMMENT_PANEL_ATTEMPTS:
            raise ProbeSafetyError(
                error.code,
                error.action,
                evidence=evidence,
            ) from None
        continue
    self.current_state = "comment_panel_open"
    return {
        "state": self.current_state,
        "clicked": True,
        "alias": self.comment_entry_alias,
        "panel_visible": True,
        "attempt": attempt,
        **{
            key: readiness[key]
            for key in (
                "stable_samples",
                "required_samples",
                "fingerprint_hash",
            )
        },
    }
```

Retained-panel handling must call `_wait_for_comment_panel_ready` before
returning `comment_panel_open`; it must not toggle the panel closed.

- [ ] **Step 7: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_state_runner.py -q -p no:cacheprovider
```

Expected: all state-runner tests pass. Existing click-once tests must still prove one click per attempt, not repeated clicks inside one attempt.

- [ ] **Step 8: Commit**

```powershell
git add selector_probe\state_runner.py tests\test_selector_probe_state_runner.py
git commit -m "fix: wait for stable comment panel"
```

---

### Task 2: Preserve Readiness Failure Classification

**Files:**
- Modify: `selector_probe/validator.py:127-921,1231-1365`
- Modify: `selector_probe/healing_runtime.py:46-65,424-466,716-760`
- Modify: `selector_probe/probe.py:1345-1435,1680-1725`
- Test: `tests/test_selector_probe_validator.py`
- Test: `tests/test_selector_probe_healing_runtime.py`

**Interfaces:**
- Consumes: `ProbeSafetyError.code` and `.evidence` from Task 1.
- Produces: `ValidationRejected.evidence`.
- Produces: healing result keys `failure_code`, `failure_evidence`, `required_state`, and `proposed_pause_aliases`.

- [ ] **Step 1: Write failing propagation tests**

Add:

```python
def test_required_state_preserves_comment_readiness_failure():
    async def scenario():
        class Runner:
            async def ensure_state(self, *_args, **_kwargs):
                raise ProbeSafetyError(
                    "comment_panel_readiness_timeout",
                    "open_comment_panel",
                    evidence={"attempt": 3, "stable_samples": 1},
                )

        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(
                    {
                        ALIAS: {
                            "scope": "visible_comment_panel",
                            "locators": definition()["locators"],
                        }
                    }
                ),
                {
                    ALIAS: contract(
                        required_state="comment_panel_open",
                        scope="visible_comment_panel",
                    )
                },
                Runner(),
            )
        assert caught.value.code == "comment_panel_readiness_timeout"
        assert caught.value.evidence["attempt"] == 3

    asyncio.run(scenario())


def test_readiness_failure_skips_llm_and_preserves_aliases():
    model_calls = []
    active_result = {
            "status": "failed",
            "failure_class": "infrastructure",
            "failed_aliases": ["评论输入框", "评论提交按钮"],
            "code": "comment_panel_readiness_timeout",
            "required_state": "comment_panel_open",
            "failure_evidence": {
                "profile_mask": "***xctm",
                "round_number": 1,
                "attempt": 3,
            },
        }
    runtime = SimpleNamespace(
        model_call=lambda *_args: model_calls.append(True),
        validate_active=lambda: active_result,
        deterministic_candidates=lambda **_kwargs: (
            model_calls.append("deterministic")
        ),
        fresh_validation_context=lambda **_kwargs: {},
        validate_candidate=lambda _value: {},
        repair_candidate=lambda **_kwargs: model_calls.append("repair"),
        full_validate=lambda _value: {},
        store_and_publish=lambda _value, _evidence: {},
    )
    result = run_healing_probe(runtime)
    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "comment_panel_readiness_timeout"
    assert result["proposed_pause_aliases"] == [
        "评论输入框",
        "评论提交按钮",
    ]
    assert model_calls == []
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_validator.py tests\test_selector_probe_healing_runtime.py -q -p no:cacheprovider
```

Expected: new assertions fail because state errors become `required_state_failed` and infrastructure results lose their code.

- [ ] **Step 3: Add bounded evidence to `ValidationRejected`**

Extend its constructor:

```python
evidence: Mapping[str, object] | None = None,
```

Store only these keys:

```python
allowed = {
    "attempt",
    "elapsed_seconds",
    "input_visible",
    "textbox_visible",
    "submit_visible",
    "submit_disabled",
    "loading_marker",
    "aria_busy",
    "stable_samples",
    "required_samples",
    "fingerprint_hash",
}
self.evidence = {
    key: value
    for key, value in dict(evidence or {}).items()
    if key in allowed
}
```

When `validate_two_rounds` wraps a `ValidationRejected`, copy
`evidence=error.evidence` together with profile mask, round, alias, count, and
required state.

- [ ] **Step 4: Preserve state-runner codes**

Change `_ensure_state`:

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
        evidence=getattr(error, "evidence", {}),
    ) from None
```

When aggregating alias failures, retain the first bounded evidence object and
pass it to the final `ValidationRejected`.

- [ ] **Step 5: Classify readiness separately in `HealingRuntime`**

Add:

```python
_COMMENT_READINESS_CODES = {
    "comment_panel_readiness_timeout",
    "comment_panel_snapshot_unstable",
}
```

In `_validation_failure`, return infrastructure without converting the code to
`zero_match`:

```python
if error.code in _COMMENT_READINESS_CODES:
    return {
        "status": "failed",
        "failure_class": "infrastructure",
        "failed_aliases": [
            alias
            for alias, contract in self.contracts.items()
            if contract.required_state == "comment_panel_open"
        ],
        "code": error.code,
        "required_state": "comment_panel_open",
        "failure_evidence": {
            **error.evidence,
            "profile_mask": error.profile_mask,
            "round_number": error.round_number,
        },
    }
```

`comment_panel_element_missing` must remain selector-class:

```python
_SELECTOR_FAILURE_CODES.add("comment_panel_element_missing")
```

- [ ] **Step 6: Preserve infrastructure details in `run_healing_probe`**

Extend `_healing_result` with:

```python
failure_evidence: object = None,
```

and copy it only when it is a mapping. Replace the generic infrastructure
return:

```python
if not active_passed and not _selector_failure(active_result):
    return _healing_result(
        "infrastructure_unavailable",
        failed_aliases=_failed_aliases(active_result),
        failure_code=str(active_result.get("code") or "probe_unavailable"),
        required_state=str(active_result.get("required_state") or ""),
        failure_evidence=active_result.get("failure_evidence"),
    )
```

- [ ] **Step 7: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_validator.py tests\test_selector_probe_healing_runtime.py -q -p no:cacheprovider
```

Expected: all selected tests pass; readiness failure produces zero model calls.

- [ ] **Step 8: Commit**

```powershell
git add selector_probe\validator.py selector_probe\healing_runtime.py selector_probe\probe.py tests\test_selector_probe_validator.py tests\test_selector_probe_healing_runtime.py
git commit -m "fix: preserve comment readiness failures"
```

---

### Task 3: Persist Failure, Capture Evidence, and Pause Only Comment Strategies

**Files:**
- Modify: `selector_probe/probe.py:424-710,782-900,980-1340`
- Modify: `selector_probe/worker.py:250-370,510-545,1400-1660`
- Modify: `selector_probe/healing_runtime.py:574-670`
- Modify: `selector_probe/store.py:8994-9135`
- Test: `tests/test_selector_probe_observe.py`
- Test: `tests/test_selector_probe_worker.py`
- Test: `tests/test_selector_probe_policy.py`

**Interfaces:**
- Consumes: healing `failure_evidence` from Task 2.
- Produces: at least one failed `selector_validation_runs` row for terminal readiness failure.
- Reuses: existing `selector_failure` outbox effect with alias-specific dependencies; no new outbox event type or schema migration.

- [ ] **Step 1: Write failing persistence and isolation tests**

Add tests proving:

```python
def test_observe_readiness_failure_records_failed_validation():
    async def failed_observer(page, _config, _elements):
        return [{
            "page_state": "comment_panel_open",
            "result": "failed",
            "failure_code": "comment_panel_readiness_timeout",
            "evidence": {
                "readiness": {"attempt": 3, "stable_samples": 1},
                "snapshot_hash": page.profile_mask,
                "aliases": {},
            },
        }]

    result, store, _lease, _factory = run_with_fakes(
        observer=failed_observer,
    )
    assert len(store.validations) >= 1
    assert store.validations[-1]["result"] == "failed"
    assert (
        store.validations[-1]["failure_code"]
        == "comment_panel_readiness_timeout"
    )
    assert result["validations_recorded"] >= 1


def test_terminal_readiness_failure_pauses_only_comment_dependency(tmp_path):
    database = tmp_path / "probe.db"

    class Runtime:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    result = worker.run_tick(
        settings_loader=_settings,
        store_factory=_seeded_store_factory,
        redis_factory=lambda _url: PolicyRedis(),
        registry_factory=lambda *_args, **_kwargs: PolicyRegistry(
            {"version": "sel-old"}
        ),
        reconcile_runner=lambda *_args: {},
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: Runtime(),
        healing_runner=lambda _runtime: {
            "status": "infrastructure_unavailable",
            "proposed_pause_aliases": ["comment-entry"],
            "failure_code": "comment_panel_readiness_timeout",
            "required_state": "comment_panel_open",
            "failure_evidence": {
                "profile_mask": "***p000",
                "round_number": 1,
                "attempt": 3,
            },
        },
        lease_factory=lambda *_args, **_kwargs: PolicyLease(),
        db_path=database,
        evidence_root=tmp_path / "evidence",
        clock=_clock(),
        force=True,
    )
    assert result["paused_strategies"] == ["comment-flow"]
    with SelectorProbeStore(database) as store:
        reasons = store.connection.execute(
            """
            SELECT strategy_id
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            ORDER BY strategy_id
            """
        ).fetchall()
    assert [row["strategy_id"] for row in reasons] == ["comment-flow"]


def test_manual_failure_holds_after_screenshot_before_cleanup(monkeypatch):
    events = []
    monkeypatch.setattr(
        "selector_probe.worker._wait_manual_failure_window",
        lambda *_args, **_kwargs: events.append("hold"),
    )
    class Runtime:
        def __enter__(self):
            return self

        def capture_failure_screenshot(self, **payload):
            target = Path(payload["target_path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"redacted")
            events.append("screenshot")
            return target

        def __exit__(self, *_args):
            events.append("profiles:stop")

    result = worker.run_tick(
        settings_loader=_settings,
        store_factory=_seeded_store_factory,
        redis_factory=lambda _url: PolicyRedis(),
        registry_factory=lambda *_args, **_kwargs: PolicyRegistry(
            {"version": "sel-old"}
        ),
        reconcile_runner=lambda *_args: {},
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: Runtime(),
        healing_runner=lambda _runtime: {
            "status": "infrastructure_unavailable",
            "proposed_pause_aliases": ["comment-entry"],
            "failure_code": "comment_panel_readiness_timeout",
            "required_state": "comment_panel_open",
            "failure_evidence": {
                "profile_mask": "***p000",
                "round_number": 1,
                "attempt": 3,
            },
        },
        lease_factory=lambda *_args, **_kwargs: PolicyLease(),
        db_path=tmp_path / "probe.db",
        evidence_root=tmp_path / "evidence",
        clock=_clock(),
        force=True,
        management_request_id="manual-request",
    )
    assert events.index("screenshot") < events.index("hold")
    assert events.index("hold") < events.index("profiles:stop")
    assert result["status"] == "infrastructure_unavailable"
```

- [ ] **Step 2: Run tests and verify failure**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_observe.py tests\test_selector_probe_worker.py tests\test_selector_probe_policy.py -q -p no:cacheprovider
```

Expected: failure record count remains zero, no readiness screenshot is linked,
and infrastructure failure does not pause the comment-only dependency.

- [ ] **Step 3: Return a failed observe record instead of losing evidence**

In `_default_observe_page`, catch terminal readiness codes around the comment
state transition and return a bounded failed record:

```python
except ProbeSafetyError as error:
    if state == "comment_panel_open" and error.code in {
        "comment_panel_readiness_timeout",
        "comment_panel_snapshot_unstable",
    }:
        evidence = {
            "readiness": {
                key: value
                for key, value in error.evidence.items()
                if key != "panel_bounds"
            },
            "semantic_snapshot": {"scope": state, "nodes": []},
            "discoveries": [],
            "aliases": {},
        }
        records.append(
            {
                "page_state": state,
                "result": "failed",
                "failure_code": error.code,
                "evidence": evidence,
            }
        )
        progress(stage_name, "failed", error.code, 3)
        break
    raise
```

In `_observe_profiles`, remember the first failed record while continuing safe
Profile/round collection. Return `(profiles_observed, validations_recorded,
first_failure_code)` and let `run_observe_probe` finish with a non-success
status after all records have been persisted.

- [ ] **Step 4: Persist healing-path failure validation**

Add to `selector_probe/worker.py`:

```python
def _record_terminal_failure_evidence(
    store: object,
    *,
    run_id: int,
    attempt_token: str,
    result: Mapping[str, object],
) -> int:
    evidence = result.get("failure_evidence")
    if not isinstance(evidence, Mapping):
        return 0
    profile_mask = evidence.get("profile_mask")
    round_number = evidence.get("round_number")
    if not isinstance(profile_mask, str) or round_number not in {1, 2}:
        return 0
    store.record_validation(
        run_id=run_id,
        attempt_token=attempt_token,
        profile_mask=profile_mask,
        round_number=round_number,
        page_state="comment_panel_open",
        result="failed",
        failure_code=str(
            result.get("failure_code")
            or "comment_panel_readiness_timeout"
        ),
        evidence={
            key: value
            for key, value in evidence.items()
            if key not in {"profile_mask", "round_number", "panel_bounds"}
        },
    )
    return 1
```

Set:

```python
validation_records = (
    _record_healthy_evidence(...)
    or _record_terminal_failure_evidence(...)
)
```

Terminal readiness failure must therefore never persist
`validation_records=0`.

- [ ] **Step 5: Extend screenshot fallback without storing raw page data**

In `HealingRuntime.capture_failure_screenshot`, when semantic alias bounds are
unavailable, use `runner.last_panel_readiness["panel_bounds"]` only as an
in-memory crop. Redact the rest of the viewport and every visible textbox/input
inside the crop. Do not copy `panel_bounds` into stored public evidence.

The worker must call `_capture_terminal_screenshot` for both:

```python
terminal_evidence_failure = (
    result.get("status") == "selector_validation_failed"
    or result.get("failure_code") in {
        "comment_panel_readiness_timeout",
        "comment_panel_snapshot_unstable",
    }
)
```

- [ ] **Step 6: Hold manual failures for 60 seconds**

Add:

```python
MANUAL_FAILURE_HOLD_SECONDS = 60.0


def _wait_manual_failure_window(stop_event: object | None) -> None:
    wait = getattr(stop_event, "wait", None)
    if callable(wait):
        wait(MANUAL_FAILURE_HOLD_SECONDS)
        return
    time.sleep(MANUAL_FAILURE_HOLD_SECONDS)
```

Call it inside `with runtime as opened_runtime`, after screenshot capture and
only when `management_request_id` is non-empty and the result is terminal
failure. Scheduled runs skip it. Lease heartbeat remains active during the
hold.

- [ ] **Step 7: Reuse alias-isolated effect and label it correctly**

Treat terminal readiness failure with aliases as an isolated failure when
building policy/effect:

```python
readiness_failure = (
    result.get("failure_code") in {
        "comment_panel_readiness_timeout",
        "comment_panel_snapshot_unstable",
    }
    and bool(failed_aliases)
)
isolated_failure = (
    status == "selector_validation_failed" or readiness_failure
)
```

Use existing `selector_failure` effect for `isolated_failure`; include the real
failure code and comment aliases. In `_apply_selector_failure_effect`, derive:

```python
readiness = failure_code in {
    "comment_panel_readiness_timeout",
    "comment_panel_snapshot_unstable",
}
reason_code = (
    "comment_panel_readiness_failed"
    if readiness
    else "selector_validation_failed"
)
failure_class = reason_code
```

Use `reason_code` for the gate reason/fingerprint and `failure_class` for the
alert row. Update the existing recovery queries without changing schema:

```python
recoverable_reasons = {
    "selector_validation_failed",
    "comment_panel_readiness_failed",
    "probe_validation_stale",
    "registry_unavailable",
}
recoverable_alerts = {
    "selector_validation_failed",
    "comment_panel_readiness_failed",
    "probe_unavailable",
}
```

Apply these values in `recovery_pending` and `_apply_recovery_effect`. Resolve a
`comment_panel_readiness_failed` alert only when its aliases are non-empty and
all are covered by the newly validated/published bundle. Manual pause remains.

- [ ] **Step 8: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_selector_probe_observe.py tests\test_selector_probe_worker.py tests\test_selector_probe_policy.py -q -p no:cacheprovider
```

Expected: all selected tests pass. Assert no sleep occurs for scheduled runs
and no unrelated strategy is paused.

- [ ] **Step 9: Commit**

```powershell
git add selector_probe\probe.py selector_probe\worker.py selector_probe\healing_runtime.py selector_probe\store.py tests\test_selector_probe_observe.py tests\test_selector_probe_worker.py tests\test_selector_probe_policy.py
git commit -m "fix: isolate comment readiness failures"
```

---

### Task 4: Regression and Live Observe Verification

**Files:**
- Modify only if a failing regression exposes a defect in Task 1-3 files.
- Create: `docs/superpowers/reports/2026-07-31-selector-probe-comment-panel-readiness-verification.md`

**Interfaces:**
- Consumes: completed Tasks 1-3.
- Produces: test report and live evidence summary without raw Profile IDs, DOM, Cookies, tokens, or comment text.

- [ ] **Step 1: Run focused selector/browser suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -k "selector_probe or browser_element" -q -p no:cacheprovider
```

Expected: all selected tests pass; existing skips remain documented.

- [ ] **Step 2: Run JavaScript management regression**

```powershell
npm run test:node
```

Expected: all JavaScript tests pass. No API/UI shape changed.

- [ ] **Step 3: Run repository secret scan**

```powershell
.\.venv\Scripts\python.exe scripts\scan_repository_secrets.py
```

Expected:

```text
secret scan passed: <count> text files
```

- [ ] **Step 4: Run one manual observe-mode live probe**

Temporarily use the existing in-memory observe override; do not write
`config.json`, publish Redis selectors, or mutate gates. Verify:

```text
2 dedicated Profiles
2 rounds per Profile
comment readiness attempt <= 3
stable_samples = 3
input and submit present in final semantic snapshot
Dry-Run passes for comment entry, input, submit
no published_version_after
no strategy gate mutation
both Profiles inactive after cleanup
```

- [ ] **Step 5: Verify a controlled loading failure**

Using test doubles, not production TikTok mutation, keep the panel loading
marker visible through three attempts. Verify:

```text
failure_code = comment_panel_readiness_timeout
validation_records >= 1
screenshot path exists and is inside evidence root
no LLM call
previous selector remains active
only comment-dependent strategy paused
manual failure hold invoked
all Profiles inactive after cleanup
```

- [ ] **Step 6: Write verification report**

Record exact test counts, live run ID, masked Profile evidence, selector paths,
publication/gate invariants, cleanup state, and any remaining limitation in:

```text
docs/superpowers/reports/2026-07-31-selector-probe-comment-panel-readiness-verification.md
```

- [ ] **Step 7: Commit**

```powershell
git add docs\superpowers\reports\2026-07-31-selector-probe-comment-panel-readiness-verification.md
git commit -m "docs: verify comment panel readiness"
```

---

## Final Acceptance Checklist

- [ ] Loading panel never reaches final A11y extraction or Dry-Run.
- [ ] Three stable samples at two-second intervals are mandatory.
- [ ] Each attempt has a 60-second maximum.
- [ ] Complete transition retries exactly three times.
- [ ] Disabled submit control is accepted.
- [ ] Dynamic comment content cannot destabilize control fingerprint.
- [ ] Stable missing control becomes `comment_panel_element_missing`.
- [ ] Readiness failure never invokes LLM.
- [ ] Terminal failure records validation evidence and screenshot.
- [ ] `validation_records=0` cannot occur for terminal readiness failure.
- [ ] Previous selector version remains active.
- [ ] Only `visible_comment_panel` dependent strategies pause.
- [ ] Manual pause remains after automatic recovery.
- [ ] Two Profiles and two rounds remain required for atomic recovery.
- [ ] Manual failure window holds 60 seconds; scheduled failure does not.
- [ ] Original failure survives cleanup errors.
- [ ] Both AdsPower test Profiles end inactive.
