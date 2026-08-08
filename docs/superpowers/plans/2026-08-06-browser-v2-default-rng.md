# Browser V2 Default RNG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure production `StrategyExecutor` always passes a valid random source to real V2 actions while preserving explicitly injected random sources.

**Architecture:** Normalize `rng` once in `StrategyExecutor.__init__`. Leave action functions, strategy schema, API, UI, database, and AdsPower integration unchanged.

**Tech Stack:** Python standard-library `random`, asyncio, pytest.

## Global Constraints

- Modify only `execution_v2/executor.py` and `tests/test_execution_v2_executor.py`.
- Use `rng if rng is not None else random`; do not use truthiness because a custom injected object may be falsy.
- Do not change action parameters, timing ranges, result schema, error mapping, or retry behavior.
- `.git` metadata is read-only; report test evidence without claiming a commit.

---

### Task 1: Normalize the default executor random source

**Files:**
- Modify: `execution_v2/executor.py:3-45`
- Test: `tests/test_execution_v2_executor.py`

**Interfaces:**
- Consumes: optional `rng` constructor argument.
- Produces: `self._rng` implementing `uniform(low, high)` for every action and duration deadline.

- [ ] **Step 1: Write the failing real-action regression test**

```python
def test_default_executor_runs_real_wait_action_without_injected_rng():
    async def ready(*_args, **_kwargs):
        return None

    async def no_sleep(_seconds):
        return None

    outcome = asyncio.run(
        StrategyExecutor(
            _Resolver(), readiness_waiter=ready, sleep=no_sleep
        ).run(
            _binding(),
            _snapshot(actions=[
                {"id": "wait-1", "type": "wait", "duration_seconds": [0, 0]}
            ]),
        )
    )

    assert outcome.succeeded is True
    assert outcome.action_results == ({
        "index": 0,
        "action_id": "wait-1",
        "action_type": "wait",
        "status": "succeeded",
        "duration_seconds": 0.0,
    },)
```

Add a preservation test:

```python
def test_executor_preserves_explicitly_injected_rng():
    injected = object()
    seen = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(_page, item, *_args, **kwargs):
        seen.append(kwargs["rng"])
        return {"action_id": item["id"], "action_type": item["type"], "status": "succeeded"}

    outcome = asyncio.run(
        StrategyExecutor(
            _Resolver(), rng=injected, action_executor=action, readiness_waiter=ready
        ).run(_binding(), _snapshot(actions=[
            {"id": "wait-1", "type": "wait", "duration_seconds": [0, 0]}
        ]))
    )

    assert outcome.succeeded is True
    assert seen == [injected]
```

- [ ] **Step 2: Run tests and verify the production bug**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_executor.py -q -p no:cacheprovider
```

Expected: default real-action test fails with `action_execution_failed` and `NoneType.uniform`; injected-source test passes.

- [ ] **Step 3: Implement the one-point fallback**

Add the standard-library import:

```python
import random
```

Replace:

```python
self._rng = rng
```

with:

```python
self._rng = rng if rng is not None else random
```

- [ ] **Step 4: Run focused and dependent tests**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_executor.py tests/test_execution_v2_actions.py tests/test_execution_v2_service.py -q -p no:cacheprovider
& .\.venv\Scripts\python.exe -m py_compile execution_v2\executor.py
git diff --check -- execution_v2/executor.py tests/test_execution_v2_executor.py
```

Expected: all tests pass; compile and diff checks exit 0.

- [ ] **Step 5: Record Git limitation**

Do not stage or commit. Report the two modified paths because `.git/index.lock` creation is denied in this managed workspace.
