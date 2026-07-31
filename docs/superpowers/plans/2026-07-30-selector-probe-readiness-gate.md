# Selector Probe Readiness Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the TikTok observe probe wait for conditional semantic readiness, reuse one page per Profile for two rounds, persist discovered elements, and expose the exact failed stage.

**Architecture:** Add one focused readiness module that separates navigation commit from page stability. Keep AdsPower ownership in `session.py`; change observe orchestration so each Profile opens once and runs two rounds on the same page, reloading between rounds. Reuse the existing validation table and bounded `details_json.stages`.

**Tech Stack:** Python 3, Playwright async CDP, AdsPower Local API, SQLite, Flask, pytest.

## Global Constraints

- Navigation waits for `commit`, never global `load` or `networkidle`.
- Poll once per second for at most 60 seconds per readiness attempt.
- Require three consecutive Skeleton-free samples.
- Require two semantic samples with Jaccard similarity at least `0.85`.
- Retry readiness at most three times on the same probe-owned page.
- Login wall and CAPTCHA fail immediately as `probe_page_blocked`.
- Each dedicated Profile starts at most once per logical run.
- Each Profile uses one probe-owned page for rounds 1 and 2.
- Observe mode never writes Redis selectors or changes strategy gates.
- Never type, submit, like, follow, publish, or modify an account.
- Persist no full Profile ID, CDP endpoint, cookie, raw DOM, or raw AX tree.
- The workspace is not recognized as a Git repository; do not initialize Git.

---

## File map

- Create `selector_probe/readiness.py`: pure readiness sample validation,
  semantic-set similarity, bounded polling, and the in-memory readiness token.
- Modify `selector_probe/state_runner.py`: navigate with `wait_until="commit"`
  and require a readiness token before reporting `feed_ready`.
- Modify `selector_probe/probe.py`: two rounds per Profile, same-page reload,
  correct round persistence, and sanitized per-stage progress.
- Modify `selector_probe/blueprint.py`: project the round number in stages.
- Test `tests/test_selector_probe_readiness.py`: slow load, stability, blocker,
  timeout, and token behavior.
- Test `tests/test_selector_probe_state_runner.py`: commit navigation and token
  enforcement.
- Test `tests/test_selector_probe_observe.py`: one page per Profile, two rounds,
  four evidence groups, and exact failure stages.
- Test `tests/test_selector_probe_management_routes.py`: stage round projection.

---

### Task 1: Conditional TikTok readiness gate

**Files:**
- Create: `selector_probe/readiness.py`
- Modify: `selector_probe/state_runner.py`
- Test: `tests/test_selector_probe_readiness.py`
- Test: `tests/test_selector_probe_state_runner.py`

**Interfaces:**
- Produces: `ReadinessToken`
- Produces: `semantic_similarity(left: frozenset[str], right: frozenset[str]) -> float`
- Produces: `async wait_for_semantic_readiness(page, *, expected_origin: str, timeout_seconds: float = 60.0, poll_interval_seconds: float = 1.0, required_skeleton_clear_samples: int = 3, required_stable_samples: int = 2, similarity_threshold: float = 0.85, sleep_fn=asyncio.sleep, monotonic_fn=time.monotonic) -> tuple[ReadinessToken, dict[str, object]]`
- Changes: `ProbeStateRunner.dispatch(..., {"type": "navigate"})` waits for navigation commit only.
- Changes: default readiness evidence contains a private `ReadinessToken`.

- [ ] **Step 1: Write failing readiness tests**

```python
async def test_slow_page_waits_until_conditions_are_stable():
    page = SamplePage(
        [
            sample(ready_state="loading"),
            sample(skeleton_count=1),
            sample(fingerprints={"button:comments"}),
            sample(fingerprints={"button:comments"}),
            sample(fingerprints={"button:comments"}),
        ]
    )
    token, evidence = await wait_for_semantic_readiness(
        page,
        expected_origin="https://www.tiktok.com",
        timeout_seconds=60,
        poll_interval_seconds=1,
        sleep_fn=page.sleep,
        monotonic_fn=page.monotonic,
    )
    assert isinstance(token, ReadinessToken)
    assert evidence["ready"] is True
    assert page.sample_count == 5


async def test_visible_login_wall_fails_without_retrying():
    page = SamplePage([sample(blocked_marker="login")])
    with pytest.raises(ProbeSafetyError) as caught:
        await wait_for_semantic_readiness(
            page,
            expected_origin="https://www.tiktok.com",
        )
    assert caught.value.code == "probe_page_blocked"
    assert page.sample_count == 1


async def test_semantic_instability_times_out_without_token():
    page = AlternatingSamplePage()
    with pytest.raises(ProbeSafetyError) as caught:
        await wait_for_semantic_readiness(
            page,
            expected_origin="https://www.tiktok.com",
            timeout_seconds=3,
            poll_interval_seconds=1,
            sleep_fn=page.sleep,
            monotonic_fn=page.monotonic,
        )
    assert caught.value.code == "page_readiness_timeout"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_selector_probe_readiness.py tests\test_selector_probe_state_runner.py -q
```

Expected: FAIL because `selector_probe.readiness` and commit navigation do not
exist.

- [ ] **Step 3: Implement the readiness module**

The module must define a private JavaScript sampler and return only bounded
evidence:

```python
@dataclass(frozen=True)
class ReadinessToken:
    origin: str
    semantic_digest: str


def semantic_similarity(
    left: frozenset[str],
    right: frozenset[str],
) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    return len(left & right) / len(union) if union else 0.0


async def wait_for_semantic_readiness(
    page,
    *,
    expected_origin,
    timeout_seconds=60.0,
    poll_interval_seconds=1.0,
    required_skeleton_clear_samples=3,
    required_stable_samples=2,
    similarity_threshold=0.85,
    sleep_fn=asyncio.sleep,
    monotonic_fn=time.monotonic,
):
    deadline = monotonic_fn() + timeout_seconds
    skeleton_clear = 0
    stable = 0
    previous = frozenset()
    while monotonic_fn() <= deadline:
        raw = await page.evaluate(_READINESS_SAMPLE_SCRIPT)
        sample = _validated_sample(raw, expected_origin)
        if sample["blocked_marker"]:
            raise ProbeSafetyError("probe_page_blocked", "wait_ready")
        skeleton_clear = (
            skeleton_clear + 1 if sample["skeleton_count"] == 0 else 0
        )
        current = frozenset(sample["fingerprints"])
        stable = (
            stable + 1
            if previous
            and semantic_similarity(previous, current)
            >= similarity_threshold
            else 1
        )
        previous = current
        if (
            sample["structurally_ready"]
            and skeleton_clear >= required_skeleton_clear_samples
            and stable >= required_stable_samples
        ):
            digest = hashlib.sha256(
                "\n".join(sorted(current)).encode("utf-8")
            ).hexdigest()
            return ReadinessToken(expected_origin, f"sha256:{digest}"), {
                "ready": True,
                "origin": expected_origin,
                "semantic_count": len(current),
                "semantic_digest": f"sha256:{digest}",
            }
        await sleep_fn(poll_interval_seconds)
    raise ProbeSafetyError("page_readiness_timeout", "wait_ready")
```

The browser sampler returns only: origin, ready state, root/body visibility,
blocker code, Skeleton count, feed visibility, and at most 200 normalized
interactive fingerprints.

- [ ] **Step 4: Use commit navigation and token-gated readiness**

In `ProbeStateRunner.dispatch`:

```python
if action_type == "navigate":
    await page.goto(
        self.target_url,
        wait_until="commit",
        timeout=15_000,
    )
    self.current_state = None
    return {"state": None, "navigated_to": self.target_url}
```

In the default readiness check, call `wait_for_semantic_readiness`; in
`_wait_ready`, require `isinstance(raw.get("readiness_token"),
ReadinessToken)` before setting `current_state = "feed_ready"`. Injected test
readiness checks may return a token explicitly.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_selector_probe_readiness.py tests\test_selector_probe_state_runner.py -q
```

Expected: all tests PASS.

---

### Task 2: One Profile page, two rounds, and exact progress

**Files:**
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/blueprint.py`
- Test: `tests/test_selector_probe_observe.py`
- Test: `tests/test_selector_probe_management_routes.py`

**Interfaces:**
- Consumes: `ReadinessToken` and `ProbeStateRunner`.
- Changes: `_observe_profiles(...) -> tuple[int, int]` records round numbers
  1 and 2.
- Changes: `_default_observe_page(..., round_number: int = 1,
  progress_sink: Callable | None = None)`.
- Changes: sanitized progress includes `round`.

- [ ] **Step 1: Write failing two-round lifecycle tests**

```python
def test_each_profile_opens_once_and_persists_two_rounds():
    result, store, sessions = run_with_fakes(two_profiles=True)
    assert result["status"] == "completed"
    assert sessions.open_page_count == 2
    assert sessions.stop_profile_count == 2
    assert len(store.validations) == 8
    assert {
        (item["profile_mask"], item["round_number"])
        for item in store.validations
    } == {
        ("***p001", 1),
        ("***p001", 2),
        ("***p002", 1),
        ("***p002", 2),
    }


def test_round_two_reloads_same_page_instead_of_restarting_profile():
    result, _store, sessions = run_with_fakes(two_profiles=True)
    assert result["status"] == "completed"
    assert sessions.page_reload_count == 2
    assert sessions.open_page_count == 2
```

Add a failure-stage test asserting a readiness timeout projects:

```python
assert detail["stages"][-1] == {
    "name": "page_readiness",
    "profile_mask": "***p001",
    "round": 1,
    "status": "failed",
    "attempt_count": 3,
    "failure_code": "page_readiness_timeout",
}
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_selector_probe_observe.py tests\test_selector_probe_management_routes.py -q
```

Expected: FAIL because only round 1 is recorded and progress has no round.

- [ ] **Step 3: Implement two rounds on one page**

Inside `_observe_profiles`, open the page once, then:

```python
for round_number in (1, 2):
    raw_records = await observe_page(
        page_handle.page,
        config,
        elements,
        profile_mask=profile.profile_mask,
        round_number=round_number,
    )
    for raw_record in raw_records:
        record = _validation_record(raw_record)
        store.record_validation(
            run_id=run_id,
            attempt_token=attempt_token,
            profile_mask=profile.profile_mask,
            round_number=round_number,
            page_state=record["page_state"],
            result=record["result"],
            failure_code=record["failure_code"],
            evidence=record["evidence"],
        )
```

For round 2, `_default_observe_page` dispatches one bounded `reload` and then
`wait_ready`; it does not call `goto` again and does not create another page.

- [ ] **Step 4: Add exact sanitized stages**

Extend `_PROGRESS_FIELDS` and `_sanitize_progress_event` with integer `round`
restricted to 1 or 2. Record running/passed/failed events around page open,
navigate/reload, readiness, A11y snapshot, candidate filtering, Dry-Run, and
round persistence. Update `_management_project_run` to include `round`.

- [ ] **Step 5: Run focused and full probe regressions**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_selector_probe_observe.py tests\test_selector_probe_state_runner.py tests\test_selector_probe_management_routes.py -q
$testFiles = Get-ChildItem -LiteralPath tests -Filter 'test_selector_probe_*.py' -File | ForEach-Object { $_.FullName }
& .venv\Scripts\python.exe -m pytest @testFiles -q --disable-warnings
```

Expected: focused tests PASS; full Selector Probe suite PASS.

- [ ] **Step 6: Restart the 53330 service and perform one bounded acceptance run**

Before the run, record the current active Redis selector version and strategy
gate state. Run observe against the two dedicated test Profiles. Accept only
if the run produces four round groups, non-empty discoveries, and no Redis or
strategy-gate change. If it fails, preserve the exact stage evidence and do
not repeat automatically.

