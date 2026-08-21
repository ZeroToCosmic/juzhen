# Selector Probe Stage Regression Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent navigation-shell snapshots from reaching element discovery, retry empty candidate extraction, and report comment-panel failures under the correct UI stage.

**Architecture:** Keep existing AdsPower, CDP, API, Redis, and worker contracts. Tighten the browser-side feed readiness signal, retry deterministic extraction on the same owned page, and stop dependent comment-panel contracts when the comment entry is unavailable.

**Tech Stack:** Python 3, Playwright async API, Flask UI JavaScript, pytest, Node test runner.

## Global Constraints

- Add no API, Redis, database, configuration, or queue changes.
- Keep at most three retries and reuse the probe-owned page.
- Never open the comment panel without a currently discovered comment entry.
- Preserve previous stable selectors and affected-strategy isolation.
- Keep all browser actions read-only except the approved comment-panel open/close transition.

---

### Task 1: Reject navigation-only readiness

**Files:**
- Modify: `selector_probe/readiness.py`
- Test: `tests/test_selector_probe_readiness.py`

**Interfaces:**
- Keeps: `wait_for_semantic_readiness(...) -> tuple[ReadinessToken, dict[str, object]]`
- Changes only the private browser sampler's `feed_visible` meaning.

- [ ] Add a failing test where stable navigation fingerprints with `feed_visible=False` time out.
- [ ] Change `_READINESS_SAMPLE_SCRIPT` so `feed_visible` requires a visible video/feed surface and a visible video interaction marker such as `comment-icon`, `like-icon`, `share-icon`, or their accessible-label equivalents. `<main>` alone cannot pass.
- [ ] Run `python -m pytest tests/test_selector_probe_readiness.py -q`; expect all tests pass.

### Task 2: Retry extraction and short-circuit missing dependencies

**Files:**
- Modify: `selector_probe/healing_runtime.py`
- Test: `tests/test_selector_probe_healing_runtime.py`

**Interfaces:**
- Keeps: `HealingRuntime.deterministic_candidates(...)` and its return contract.
- Keeps: three attempts through `_VALIDATION_ATTEMPTS`.
- Changes progress event name to `comment_panel_transition` when required state is `comment_panel_open`.

- [ ] Add a failing test proving empty comment-entry candidates reload and recapture up to three times, then succeed when the third snapshot contains the entry.
- [ ] Add a failing test proving three empty comment-entry attempts never call `ensure_state(..., "comment_panel_open", ...)` and return selector failure `zero_match` instead of infrastructure failure.
- [ ] Add a failing test proving comment-panel state errors emit `comment_panel_transition`, not `page_readiness`.
- [ ] Implement bounded candidate retries inside deterministic discovery. Reload between empty attempts, reset runner state, recapture A11y, and preserve each attempt's sanitized discoveries.
- [ ] If a contract that establishes `comment_panel_open` remains unmatched, remove its stale working definition and skip all contracts requiring that state.
- [ ] Run `python -m pytest tests/test_selector_probe_healing_runtime.py -q`; expect all tests pass.

### Task 3: Verify UI classification and regressions

**Files:**
- Test: `tests-js/selector-probe-operations.test.js`

**Interfaces:**
- Keeps existing `buildRunPresentation(raw)` shape.

- [ ] Add a presentation test where `page_readiness` passed and `comment_panel_transition` failed; page stage must remain successful and element stage must fail.
- [ ] Run `node --test tests-js/selector-probe-operations.test.js`; expect all tests pass.
- [ ] Run focused Selector Probe regressions and `git diff --check`.

