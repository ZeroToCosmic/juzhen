# Selector Probe Early-Close Regression Implementation Plan

> **For agentic workers:** Implement inline with TDD; do not refactor unrelated probe code.

**Goal:** Stop premature Profile cleanup and make environment/page stage reporting truthful.

**Architecture:** Split local candidate preflight from browser resource ownership. Add a generic stability gate inside page navigation, leaving selector validation and publication unchanged.

**Tech Stack:** Python, pytest, Playwright async API, Flask management UI.

## Global Constraints

- No semantic or LLM matching.
- Page readiness budget remains 90 seconds from configuration.
- Two Profile × two round validation remains unchanged.
- No live database mutation during tests.

---

### Task 1: Reproduce empty-candidate lifecycle regression

**Files:**
- Modify: `tests/test_selector_probe_worker.py`
- Modify: `tests/test_selector_probe_managed_probe.py`

- [ ] Add a worker-level test proving empty candidates open zero Profiles.
- [ ] Add assertions that empty candidates return `awaiting_element_selection` without environment failure.
- [ ] Run focused tests and confirm failure before implementation.

### Task 2: Move candidate preflight before browser ownership

**Files:**
- Modify: `selector_probe/worker.py`
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/managed_runtime.py`

- [ ] Add a lightweight candidate loader that requires no open runtime.
- [ ] Pass the preloaded candidate to the managed runner.
- [ ] Return `awaiting_element_selection` for an empty candidate.
- [ ] Ensure only browser lifecycle code writes environment/navigation stages.
- [ ] Run focused lifecycle tests.

### Task 3: Add generic stable-page readiness

**Files:**
- Modify: `selector_probe/managed_runtime.py`
- Modify: `tests/test_selector_probe_managed_runtime.py`

- [ ] Add failing tests for unstable and stable interactive-element samples.
- [ ] Implement expected-origin, visible-body, positive-count, two-sample stability checks.
- [ ] Preserve the configured readiness timeout.
- [ ] Run runtime tests.

### Task 4: Correct operator-facing state

**Files:**
- Modify: `gateway/static/selector_probe_ui.js`
- Modify: relevant Node tests.

- [ ] Render `awaiting_element_selection` as an instruction to collect/rebind.
- [ ] Do not label it infrastructure failure.
- [ ] Run Node tests.

### Task 5: Regression verification

- [ ] Run focused Python suites.
- [ ] Run all Node tests.
- [ ] Run Python and JavaScript syntax checks.
- [ ] Run `git diff --check`.

