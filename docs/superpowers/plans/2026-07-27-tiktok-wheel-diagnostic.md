# TikTok Wheel Diagnostic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add opt-in, sanitized per-pulse wheel telemetry and run one bounded downward diagnostic on each authorized Profile.

**Architecture:** Keep production switch semantics unchanged and inject a diagnostic collector only when explicitly requested. The collector installs page-side event/mutation probes per pulse, always removes them in `finally`, and returns a fixed safe schema. A separate live harness calls the production verified-switch function directly with `diagnostic=True`; no strategy, comment action, or configuration mutation is needed.

**Tech Stack:** Python 3, Playwright async CDP, pytest, AdsPower Local API.

## Global Constraints

- One diagnostic execution on `***xcto` and one on `***xctm`; no retries.
- Each execution requests one downward switch and may emit only the existing bounded `+120` pulse sequence.
- No upward action, element locator, mouse click, keyboard input, submit, or comment publication.
- Diagnostics are opt-in and absent from normal execution.
- Diagnostic data is boolean, nonnegative numeric, enum, or twelve-character hash only.
- No raw IDs, text, URL/origin, selector/XPath, HTML/attributes, cookie, or credential.
- Listener/observer/page key cleanup is mandatory in `finally`.
- No schema, UI, persistence, or saved-strategy changes.
- No Git operations.

---

### Task 1: Per-pulse diagnostic collector

**Files:**
- Modify: `browser_video_switch.py`
- Modify: `tests/test_browser_video_switch.py`

**Interfaces:**
- Produces: `execute_verified_switches(..., diagnostic: bool = False) -> dict`
- Produces on success/failure: optional `pulse_diagnostics: list[dict]`
- Internal collector: install, snapshot, and cleanup functions with a fixed safe schema

- [ ] Write RED tests for opt-in absence/presence, safe schema, success/failure partial records, and cleanup on success/error/timeout/cancellation.
- [ ] Run `.\.venv\Scripts\python.exe -m pytest tests/test_browser_video_switch.py -q -p no:cacheprovider` and record expected failures.
- [ ] Implement the minimal page-side collector and Python safe projector.
- [ ] Thread `diagnostic=False` through `switch_once` and `execute_verified_switches` without changing non-diagnostic results.
- [ ] Attach safe partial records to `VideoSwitchError`.
- [ ] Run video-switch, action, and strategy-runtime related tests plus `py_compile`.
- [ ] Write `.superpowers/sdd/tiktok-wheel-diagnostic-task-1-report.md`.

### Task 2: Automated verification and independent review

**Files:**
- Modify: `.superpowers/sdd/progress.md`

- [ ] Run changed-module compilation.
- [ ] Run `tests/test_browser_video_switch.py`, `tests/test_browser_actions.py`, `tests/test_browser_strategy_runtime.py`, and security/public-projection tests.
- [ ] Prove diagnostic-off results contain no diagnostic field.
- [ ] Prove serialized diagnostic records contain none of the forbidden keys or raw fixture identities.
- [ ] Obtain independent spec and code-quality approval before live access.

### Task 3: One authorized diagnostic per Profile

**Files:**
- Create: `.superpowers/sdd/tiktok-wheel-diagnostic-live-report.md`
- Modify: `.superpowers/sdd/progress.md`

- [ ] Confirm the repaired service is healthy and both authorized profiles are `Inactive`.
- [ ] Record config and log baselines without enumerating other profiles.
- [ ] Start/tile only the two authorized profiles once.
- [ ] Attach to each active page and call `execute_verified_switches` once with `direction="down"`, `requested=1`, `diagnostic=True`, and zero configured interval.
- [ ] Do not retry either call, regardless of result.
- [ ] Stop each opened profile once and poll until `Inactive`.
- [ ] Validate config unchanged, JSONL validity, and zero full authorized IDs.
- [ ] Report the supported diagnosis separately for each masked Profile.
- [ ] Obtain an independent evidence/safety review.

