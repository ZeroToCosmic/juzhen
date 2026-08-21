# Selector Discovery and Contract Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve safe A11y discoveries on failed healing runs and add administrator edit/delete controls for semantic contracts.

**Architecture:** Reuse `discover_interactive_candidates`, the existing run `details_json`, and the existing PATCH/DELETE routes. Extend only the current runtime projection and element-detail UI; do not add tables, routes, or cascading behavior.

**Tech Stack:** Python 3, Flask, SQLite, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- No new HTTP endpoint or database table.
- Only administrators mutate contracts.
- Delete only when dependency count is zero; backend remains authoritative.
- Preserve active/LKG selectors until a new draft passes existing publication rules.
- Keep existing discovery allowlists, redaction, masking, and 200-item bound.

---

### Task 1: Preserve failed-run discoveries

**Files:**
- Modify: `selector_probe/healing_runtime.py`
- Modify: `selector_probe/worker.py`
- Modify: `selector_probe/blueprint.py`
- Test: `tests/test_selector_probe_healing_runtime.py`
- Test: `tests/test_selector_probe_worker.py`
- Test: `tests/test_selector_probe_management_routes.py`

**Interfaces:**
- Produces: `HealingRuntime.discovery_candidates() -> list[dict[str, object]]`
- Persists: `probe_runs.details_json.discoveries`
- Exposes: existing run detail field `discoveries`

- [ ] **Step 1: Add failing tests**

Test that a valid snapshot followed by deterministic `zero_match` retains only safe interactive discoveries, that worker final details contain them, and that management projection merges them when `selector_validation_runs` is empty.

- [ ] **Step 2: Verify the focused tests fail**

Run: `python -m pytest tests/test_selector_probe_healing_runtime.py tests/test_selector_probe_worker.py tests/test_selector_probe_management_routes.py -q`

Expected: new discovery assertions fail because healing-run details currently contain no discoveries.

- [ ] **Step 3: Implement the minimal discovery buffer**

Import and call `discover_interactive_candidates(snapshot.model_payload(), page_state=contract.required_state, profile_mask=profile_mask)` after every valid snapshot. Merge by fingerprint in the runtime, expose a defensive list copy, copy it into the healing result before the runtime closes, persist it in final run details, and merge it with validation evidence in `_management_project_run`.

- [ ] **Step 4: Verify focused tests pass**

Run the same pytest command. Expected: PASS.

### Task 2: Edit semantic contracts with the existing PATCH route

**Files:**
- Modify: `selector_probe/catalog.py`
- Modify: `selector_probe/store.py`
- Modify: `selector_probe/blueprint.py`
- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/app.py`
- Test: `tests/test_selector_probe_catalog.py`
- Test: `tests/test_selector_probe_routes.py`
- Test: `tests-js/selector-probe-elements.test.js`

**Interfaces:**
- Consumes: `PATCH /api/selector-probe/elements/<id>/draft`
- Accepts: `{expected_revision, display_name?, contract}`
- Produces: updated element detail with incremented revision and draft status.

- [ ] **Step 1: Add failing API and UI tests**

Cover optional display-name update, complete contract sanitization/prefill, correct PATCH revision, and operator-hidden edit control.

- [ ] **Step 2: Verify tests fail**

Run: `python -m pytest tests/test_selector_probe_catalog.py tests/test_selector_probe_routes.py -q`

Run: `node --test tests-js/selector-probe-elements.test.js`

Expected: new edit assertions fail.

- [ ] **Step 3: Implement edit reuse**

Allow optional `display_name` on the existing PATCH payload, update it in the same SQLite transaction, and keep old callers valid when omitted. Extend `sanitizeElementDetail` to preserve accepted roles/names, preferred attributes, name mode, and postcondition. Add an administrator-only edit button that opens the existing wizard prefilled and PATCHes instead of POSTing.

- [ ] **Step 4: Verify tests pass**

Run the same Python and Node commands. Expected: PASS.

### Task 3: Delete only unreferenced elements

**Files:**
- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/app.py`
- Test: `tests-js/selector-probe-elements.test.js`

**Interfaces:**
- Consumes: existing `DELETE /api/selector-probe/elements/<id>` with `{expected_revision}`.

- [ ] **Step 1: Add failing UI tests**

Cover confirmation plus DELETE for zero dependencies, disabled/no request for referenced elements, visible dependency strategy/action rows, successful directory refresh, and safe 409 error display.

- [ ] **Step 2: Verify the Node test fails**

Run: `node --test tests-js/selector-probe-elements.test.js`

Expected: new delete assertions fail.

- [ ] **Step 3: Implement minimal delete controls**

Add administrator-only edit/delete buttons and a dependency list to element detail. Use injected `confirm`, send the current revision only for dependency-free elements, close detail and refresh the directory on 204, otherwise retain detail and show the backend error.

- [ ] **Step 4: Verify the Node test passes**

Run the same Node command. Expected: PASS.

### Task 4: Regression verification

**Files:**
- Test only.

- [ ] **Step 1: Run selector-probe Python tests**

Run: `python -m pytest tests/test_selector_probe_*.py -q`

Expected: PASS.

- [ ] **Step 2: Run selector-probe JavaScript tests**

Run: `node --test tests-js/selector-probe-*.test.js`

Expected: PASS.

- [ ] **Step 3: Inspect the final diff**

Run: `git diff --check`

Expected: no whitespace errors; every changed line maps to discovery persistence or contract edit/delete.
