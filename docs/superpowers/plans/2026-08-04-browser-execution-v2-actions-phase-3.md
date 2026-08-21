# Browser Execution V2 Actions Phase 3 Implementation Plan

> **For agentic workers:** Implement task-by-task with tests first.

**Goal:** Persist five freely ordered action blocks and execute them against strict V2 elements using the existing human mouse and typing implementations.

**Architecture:** `strategy.py` owns closed-schema validation, Store owns transactional strategy/action persistence, `actions.py` executes exactly one normalized block, and `executor.py` owns ordered once/duration loops. No V1 comment, semantic scope, gate, pattern recorder, or page-switch recovery is imported.

**Tech Stack:** Python asyncio, Playwright public mouse/keyboard APIs, `actions_dom.human_move_to`, `actions_dom.human_type`, SQLite, pytest.

## Constraints

- Only `move`, `scroll`, `click`, `input`, `wait` exist; every type may repeat and reorder.
- Click and move resolve `element_id` through `StrictLocatorResolver`; input additionally requires editable.
- Mouse trajectory and typing delay must call existing helpers, never a second implementation.
- An action failure ends only that Profile and never runs later actions.
- `once` runs one round. `duration` samples one Profile deadline and begins a new round only when below it.
- Batch size is job input, never stored in strategy.

### Task 1: Strategy schema and Store

**Files:** create `execution_v2/strategy.py`, modify `execution_v2/store.py`, add `tests/test_execution_v2_strategy.py` and strategy Store tests.

Test first: exact keys; five action types; ranges; duplicate types allowed; order preserved; invalid element references rejected; create/get/list/update/delete; transaction rollback; immutable `snapshot_strategy()` includes referenced element revisions.

Interfaces:

```python
normalize_strategy(value, elements) -> dict
ExecutionStore.create_strategy(id, definition)
ExecutionStore.get_strategy(id)
ExecutionStore.list_strategies()
ExecutionStore.update_strategy(id, definition, expected_revision)
ExecutionStore.delete_strategy(id, expected_revision)
ExecutionStore.snapshot_strategy(id) -> dict
```

Run focused tests; expected PASS.

### Task 2: Five single-action executors

**Files:** create `execution_v2/actions.py`, add `tests/test_execution_v2_actions.py`.

Inject RNG and sleep. `move` selects an interior point and calls `human_move_to(page, x, y, duration_seconds=..., target_box=...)`. `scroll` samples count/distance/interval and calls `page.mouse.wheel(0, signed_distance)`. `click` calls `human_move_to`, then `page.mouse.down`, sleeps sampled hold, `page.mouse.up`, then sampled after delay. `input` focuses the strict handle, resolves fixed/library text, calls `human_type(page, text, timing={"source":"builtin","interval_ms": range})`, and verifies the final value contains the full text. `wait` only awaits sampled duration.

Tests monkeypatch `execution_v2.actions.human_move_to` and `human_type` and assert those exact existing functions are called. Also assert no V1 comment/panel/scope code executes.

### Task 3: Ordered once/duration executor and evidence

**Files:** create `execution_v2/executor.py`, add `tests/test_execution_v2_executor.py`.

Interfaces:

```python
StrategyExecutor.run(binding, strategy_snapshot) -> ProfileOutcome
```

For each action append a sanitized result with index/type/status/timing. Stop immediately after the first failure and return completed prior actions plus a stable failure code. For duration mode sample the deadline once, complete an already-started action, and never begin a new round after the deadline. Inject clock/sleep/RNG; tests use no real waiting.

Completion gate: repeated scroll/wait order is exact; all five types execute; existing human helpers are directly called; failure prevents later actions; once/duration semantics pass; full V2 and adjacent action/ghost-cursor tests pass.
