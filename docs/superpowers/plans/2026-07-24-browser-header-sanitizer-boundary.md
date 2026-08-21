# Browser Header Sanitizer Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Treat Cookie and Authorization `:`/`=` values as atomic through end of line before generic credential assignment projection.

**Architecture:** Add the same narrow header-first line projector to the gateway public sanitizer and strategy-runtime reason sanitizer. Reuse each module's existing safe-status set, but require an exact whole header value rather than the broader scheme-plus-status credential rule.

**Tech Stack:** Python, `re`, pytest.

## Global Constraints

- Recognize only Cookie and Authorization with `:` or `=`.
- Treat the remainder of the current line as one value.
- Preserve only whole-value `missing`, `expired`, `invalid`, or `not configured`.
- Do not expand any other input syntax.
- Use TDD and do not use Git.

---

### Task 1: Header boundary regression matrices

**Files:**
- Modify: `tests/test_app.py`
- Modify: `tests/test_browser_strategy_runtime.py`

**Interfaces:**
- Consumes: `gateway.app.public_browser_payload(value)` and `StrategyRuntimeError(stage, reason).reason`.
- Produces: matching gateway/runtime matrices for four header forms and nested diagnostic-looking assignments.

- [x] **Step 1: Add failing matrices**

Parameterize `Cookie:`, `Cookie=`, `Authorization:`, and `Authorization=`
against `status`, `reason`, `error`, `message`, and `stage`, comma/semicolon,
quoted/unquoted values, and a per-case random secret. Assert the output is the
unchanged prefix/header separator plus exactly `[redacted]`, with no secret.
Parameterize the four exact safe statuses and assert they remain unchanged.
Assert `Authorization: Basic missing` is redacted.

- [x] **Step 2: Verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -p no:cacheprovider -q -W error -k "header_value"
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py -p no:cacheprovider -q -W error -k "header_value"
```

Expected: nested diagnostic-looking fields expose the random secret and fail;
`Authorization: Basic missing` is incorrectly preserved and fails.

### Task 2: Minimal header-first projectors

**Files:**
- Modify: `gateway/app.py`
- Modify: `browser_strategy_runtime.py`

**Interfaces:**
- Produces: one module-local line projector that preserves the header prefix
  and replaces its complete non-safe value with `[redacted]`.

- [x] **Step 1: Implement the gateway projector**

Add a case-insensitive Cookie/Authorization `:`/`=` pattern, an exact
whole-header safe-value predicate, and a line projector. Apply it before
`sanitize_public_browser_assignments`.

- [x] **Step 2: Implement the runtime projector**

Add the equivalent module-local pattern, predicate, and line projector. Apply
it before `_sanitize_runtime_assignments`.

- [x] **Step 3: Verify GREEN**

Run both commands from Task 1 and require zero warnings or failures.

### Task 3: Mutation and final verification

**Files:**
- Modify: `docs/superpowers/reports/2026-07-24-adspower-window-lifecycle-verification.md`

**Interfaces:**
- Consumes: the completed implementation and regression matrices.
- Produces: fresh mutation and suite evidence in the verification report.

- [x] **Step 1: Kill focused mutations**

Temporarily move each header projector after generic assignment projection,
then temporarily restore the broader scheme-plus-status preservation behavior.
The new matrices must fail for both gateway and runtime. Revert every mutation.

- [x] **Step 2: Run final verification**

Run the new matrices, the strict focused Python suite with `-W error`, the full
`tests` root, `npm.cmd run test:node`, and `py_compile` including gateway,
runtime, lifecycle, actions, tiler, `init_db.py`, and `account_store.py`.

- [x] **Step 3: Update the report**

Record RED/GREEN, killed mutations, exact commands, counts, durations, and the
no-Git constraint.
