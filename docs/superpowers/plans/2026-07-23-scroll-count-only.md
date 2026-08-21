# Scroll Count Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove scroll distance from the strategy editor and make each configured wheel count produce exactly one fixed ±120 browser wheel event.

**Architecture:** Keep the persisted `distance` key only as a compatibility field. Frontend parsing, backend normalization, defaults, legacy migration, and runtime execution all converge on one `SCROLL_WHEEL_DELTA = 120` value; users configure only count range and interval range.

**Tech Stack:** Python 3.13, Flask configuration APIs, browser automation, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- Visible scroll fields are exactly minimum count, maximum count, minimum interval, and maximum interval.
- Each count equals one `page.mouse.wheel()` call.
- Runtime delta is always `+120` for down and `-120` for up.
- Old `distance` values are accepted but normalized to `120` on read/save.
- Keep persisted `distance` for backward schema compatibility.
- Keep hidden `burst_count` compatibility and existing count/interval persistence.
- No changes to other action types or Git state.

---

### Task 1: Frontend scroll editor

**Files:**
- Modify: `tests-js/browser-strategy-ui.test.js`
- Modify: `gateway/static/browser_strategy_ui.js`

- [ ] Add RED tests proving the editor exposes four fields, no `distance`, and parsing succeeds without a distance input while returning `distance: 120`.
- [ ] Run `node --test tests-js/browser-strategy-ui.test.js` and observe failures caused by the existing distance field/requirement.
- [ ] Remove the distance field and distance validation; set compatibility output to `120`.
- [ ] Rerun the focused Node test and verify green.

### Task 2: Backend normalization and runtime

**Files:**
- Modify: `tests/test_browser_strategy_config.py`
- Modify: `tests/test_actions.py`
- Modify: `browser_strategy_config.py`
- Modify: `browser_actions.py`

- [ ] Add RED tests proving defaults and legacy/current normalization output `distance: 120` even when input is `600`, and runtime always emits ±120 regardless of supplied legacy distance.
- [ ] Run focused pytest tests and observe failures showing old distances are preserved/executed.
- [ ] Define `SCROLL_WHEEL_DELTA = 120`, use it in defaults, normalization, legacy migration, and runtime execution.
- [ ] Rerun focused Python tests and verify green.

### Task 3: Persistence and complete regression

**Files:**
- Modify: `tests/test_browser_strategy_config.py`
- Modify: `docs/tiktok-stats.md`

- [ ] Update round-trip expectations to prove old distance becomes `120` while count/interval/burst parameters survive refresh and restart.
- [ ] Update the user-facing scroll documentation: count is configurable; delta is hidden and fixed.
- [ ] Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_browser_strategy_config.py tests\test_actions.py tests\test_tiktok_stats_restart_persistence.py -q -p no:cacheprovider
npm.cmd run test:node
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m py_compile browser_strategy_config.py browser_actions.py
```

- [ ] Perform final independent code review and record any manual UI acceptance gap.

