# Browser Execution V2 Core Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the isolated V2 persistence, AdsPower lifecycle adapter, immutable Profile/CDP binding, and batch scheduler, proven with 300 fake Profiles in batches of three.

**Architecture:** Add a new `execution_v2` Python package that does not import `selector_probe`, V1 strategy state, Redis, LLM, or the existing strategy gate. The core is async at its boundaries: synchronous AdsPower calls run through a rate-limited adapter, each Profile receives one immutable session binding, Profile work runs concurrently inside a batch, and cleanup completes before the next batch starts.

**Tech Stack:** Python 3.11+, asyncio, sqlite3, dataclasses, Playwright public CDP API, pytest.

## Global Constraints

- Default `batch_size` is exactly `3`; the supported range is `1..8`.
- AdsPower Local API calls are serialized and start at least one second apart.
- Start and stop may make at most three total attempts (initial call plus two retries).
- Locator and action work never retries or replays completed actions.
- Every successful or failed Profile is closed; the next batch cannot start until the current batch is confirmed closed.
- Three failed close confirmations produce `cleanup_blocked` and prevent later batches.
- Runtime state is stored only in `data/execution_v2/execution_v2.db`, with foreign keys, WAL, and transactions enabled.
- V1 data is read-only and is never migrated into V2.
- Do not modify `gateway/app.py`, `selector_probe/**`, or existing selector-probe tests in Phase 1.
- Reuse `adspower.AdsPowerController`; do not reimplement HTTP or expose its API key or WebSocket URL in public results.

---

### Task 1: Define V2 states and immutable session contracts

**Files:**
- Create: `execution_v2/__init__.py`
- Create: `execution_v2/models.py`
- Test: `tests/test_execution_v2_models.py`

**Interfaces:**
- Produces: `JobStatus`, `ProfileStatus`, `Stage`, `BrowserBinding`, `ProfileOutcome`, `utc_now_iso()`.
- `BrowserBinding` keeps `profile_id`, `ws_url`, `browser`, `context`, and `page` in one frozen record.

- [ ] **Step 1: Write the failing model tests**

```python
from dataclasses import FrozenInstanceError

import pytest

from execution_v2.models import BrowserBinding, JobStatus, ProfileStatus, Stage


def test_browser_binding_is_immutable_and_keeps_one_profile_chain():
    binding = BrowserBinding("profile-1", "ws://one", object(), object(), object())
    assert binding.profile_id == "profile-1"
    assert binding.ws_url == "ws://one"
    with pytest.raises(FrozenInstanceError):
        binding.profile_id = "profile-2"


def test_public_state_values_are_stable():
    assert JobStatus.CLEANUP_BLOCKED.value == "cleanup_blocked"
    assert ProfileStatus.WAITING_READINESS.value == "waiting_readiness"
    assert Stage.ADSPOWER_STOP.value == "adspower_stop"
```

- [ ] **Step 2: Run the tests and confirm the import fails**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_models.py -q -p no:cacheprovider`

Expected: FAIL with `ModuleNotFoundError: No module named 'execution_v2'`.

- [ ] **Step 3: Implement only the shared types**

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CLEANUP_BLOCKED = "cleanup_blocked"


class ProfileStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    CONNECTING_CDP = "connecting_cdp"
    NAVIGATING = "navigating"
    WAITING_READINESS = "waiting_readiness"
    EXECUTING = "executing"
    CAPTURING_EVIDENCE = "capturing_evidence"
    CLOSING = "closing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CLEANUP_FAILED = "cleanup_failed"


class Stage(StrEnum):
    ADSPOWER_START = "adspower_start"
    CDP_CONNECT = "cdp_connect"
    NAVIGATE = "navigate"
    READINESS = "readiness"
    LOCATE_ELEMENT = "locate_element"
    EXECUTE_ACTION = "execute_action"
    CAPTURE_EVIDENCE = "capture_evidence"
    ADSPOWER_STOP = "adspower_stop"


@dataclass(frozen=True, slots=True)
class BrowserBinding:
    profile_id: str
    ws_url: str
    browser: Any
    context: Any
    page: Any


@dataclass(frozen=True, slots=True)
class ProfileOutcome:
    profile_id: str
    succeeded: bool
    stage: Stage
    error_code: str = ""
    error_summary: str = ""
    action_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

- [ ] **Step 4: Run the focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_models.py -q -p no:cacheprovider`

Expected: PASS.

---

### Task 2: Add the independent transactional SQLite store

**Files:**
- Create: `execution_v2/store.py`
- Test: `tests/test_execution_v2_store.py`

**Interfaces:**
- Consumes: enum values from `execution_v2.models`.
- Produces: `ExecutionStore(db_path)`, `initialize()`, `create_job(...)`, `set_job_status(...)`, `set_profile_status(...)`, `append_action_result(...)`, `get_job(...)`, and `list_profile_results(...)`.

- [ ] **Step 1: Write failing store tests for schema, restart persistence, and rollback**

```python
import sqlite3

import pytest

from execution_v2.models import JobStatus, ProfileStatus, Stage
from execution_v2.store import ExecutionStore


def test_store_enables_wal_foreign_keys_and_survives_restart(tmp_path):
    path = tmp_path / "execution_v2.db"
    store = ExecutionStore(path)
    store.initialize()
    store.create_job("job-1", "strategy-1", {"revision": 4}, ["p1", "p2"], 3)
    store.set_profile_status("job-1", "p1", ProfileStatus.STARTING, Stage.ADSPOWER_START)

    reopened = ExecutionStore(path)
    reopened.initialize()
    assert reopened.get_job("job-1")["status"] == JobStatus.QUEUED.value
    assert reopened.list_profile_results("job-1")[0]["stage"] == Stage.ADSPOWER_START.value

    with reopened.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_create_job_rolls_back_when_a_profile_insert_fails(tmp_path, monkeypatch):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    with pytest.raises(sqlite3.IntegrityError):
        store.create_job("job-1", "strategy-1", {"revision": 1}, ["same", "same"], 3)
    assert store.get_job("job-1") is None
```

- [ ] **Step 2: Run the store tests and confirm they fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_store.py -q -p no:cacheprovider`

Expected: FAIL because `execution_v2.store` does not exist.

- [ ] **Step 3: Implement the final Phase 1 schema and transaction boundary**

Use one `SCHEMA` executed by `initialize()` containing these exact tables:

```sql
CREATE TABLE IF NOT EXISTS elements (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, purpose TEXT NOT NULL,
  kind TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL,
  definition_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS element_revisions (
  element_id TEXT NOT NULL, revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(element_id, revision),
  FOREIGN KEY(element_id) REFERENCES elements(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS strategies (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
  revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_actions (
  strategy_id TEXT NOT NULL, position INTEGER NOT NULL, action_json TEXT NOT NULL,
  PRIMARY KEY(strategy_id, position),
  FOREIGN KEY(strategy_id) REFERENCES strategies(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS execution_jobs (
  id TEXT PRIMARY KEY, strategy_id TEXT NOT NULL, status TEXT NOT NULL,
  batch_size INTEGER NOT NULL CHECK(batch_size BETWEEN 1 AND 8),
  strategy_snapshot_json TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS execution_profiles (
  job_id TEXT NOT NULL, profile_id TEXT NOT NULL, position INTEGER NOT NULL,
  status TEXT NOT NULL, stage TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
  error_summary TEXT NOT NULL DEFAULT '', close_confirmed INTEGER NOT NULL DEFAULT 0,
  started_at TEXT, finished_at TEXT, PRIMARY KEY(job_id, profile_id),
  FOREIGN KEY(job_id) REFERENCES execution_jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS action_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, profile_id TEXT NOT NULL,
  action_index INTEGER NOT NULL, action_type TEXT NOT NULL, status TEXT NOT NULL,
  stage TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
  FOREIGN KEY(job_id, profile_id) REFERENCES execution_profiles(job_id, profile_id) ON DELETE CASCADE
);
```

Every public write method must use `with self.connect() as connection:` and parameterized SQL. `create_job()` inserts the job and every Profile in the same transaction; JSON uses `ensure_ascii=False` and deterministic key sorting.

- [ ] **Step 4: Run store tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_store.py -q -p no:cacheprovider`

Expected: PASS.

---

### Task 3: Wrap AdsPower and create strict CDP bindings

**Files:**
- Create: `execution_v2/adspower_adapter.py`
- Create: `execution_v2/session.py`
- Test: `tests/test_execution_v2_adspower_adapter.py`
- Test: `tests/test_execution_v2_session.py`

**Interfaces:**
- Produces protocol `AdsPowerAdapter` with async `start()`, `stop()`, `is_active()`.
- Produces `RateLimitedAdsPowerAdapter(controller, clock, sleep, minimum_interval=1.0)` that delegates HTTP to `AdsPowerController` through `asyncio.to_thread`.
- Produces protocol `SessionFactory` and `PlaywrightSessionFactory.connect(profile_id, ws_url) -> BrowserBinding`.

- [ ] **Step 1: Write failing tests for rate limiting and one-to-one binding**

```python
import asyncio

from execution_v2.adspower_adapter import RateLimitedAdsPowerAdapter
from execution_v2.session import PlaywrightSessionFactory


def test_adspower_calls_are_serialized_one_second_apart():
    events = []
    now = [0.0]
    class Controller:
        def start_browser(self, profile_id):
            events.append((profile_id, now[0]))
            return f"ws://{profile_id}"
    async def sleep(seconds):
        now[0] += seconds
    adapter = RateLimitedAdsPowerAdapter(Controller(), clock=lambda: now[0], sleep=sleep)
    assert asyncio.run(adapter.start("p1")) == "ws://p1"
    assert asyncio.run(adapter.start("p2")) == "ws://p2"
    assert events == [("p1", 0.0), ("p2", 1.0)]


def test_session_factory_uses_the_profile_ws_and_one_target_page():
    # Fake Playwright objects expose exactly one context and two pages.
    # The factory keeps the selected non-blank page and closes only the extra page.
    binding = asyncio.run(factory.connect("p1", "ws://p1"))
    assert binding.profile_id == "p1"
    assert binding.ws_url == "ws://p1"
    assert binding.page.url != "about:blank"
    assert extra_page.closed is True
```

- [ ] **Step 2: Run both focused files and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_adspower_adapter.py tests/test_execution_v2_session.py -q -p no:cacheprovider`

Expected: FAIL because the modules do not exist.

- [ ] **Step 3: Implement the async protocols and adapter**

```python
class AdsPowerAdapter(Protocol):
    async def start(self, profile_id: str) -> str: ...
    async def stop(self, profile_id: str) -> None: ...
    async def is_active(self, profile_id: str) -> bool: ...


class SessionFactory(Protocol):
    async def connect(self, profile_id: str, ws_url: str) -> BrowserBinding: ...
```

The adapter owns one `asyncio.Lock`, waits until `last_call + minimum_interval`, and then invokes only `AdsPowerController.start_browser`, `stop_browser`, or `get_browser_active`. `is_active()` accepts only the controller's documented active indicators; it must not return credentials or the raw response.

The session factory calls `playwright.chromium.connect_over_cdp(ws_url, timeout=...)`, rejects zero or multiple contexts, selects the sole non-blank live page (or the sole live page when all are blank), closes all other pages, and returns a frozen `BrowserBinding`. It never pairs Profiles by array index or a global current page.

- [ ] **Step 4: Run adapter/session tests and existing boundary tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_adspower_adapter.py tests/test_execution_v2_session.py tests/test_adspower.py tests/test_browser_cdp.py -q -p no:cacheprovider`

Expected: PASS.

---

### Task 4: Implement the batch scheduler and prove 300 Profiles

**Files:**
- Create: `execution_v2/scheduler.py`
- Test: `tests/test_execution_v2_scheduler.py`

**Interfaces:**
- Consumes: `ExecutionStore`, `AdsPowerAdapter`, `SessionFactory`, and an injected async `execute_profile(binding, strategy_snapshot) -> ProfileOutcome`.
- Produces: `BatchScheduler.run(job_id, strategy_id, snapshot, profile_ids, batch_size=3)` and `BatchScheduler.cancel(job_id)`.

- [ ] **Step 1: Write the 300-Profile failing test with interface-shaped fakes**

```python
def test_300_profiles_run_in_100_batches_of_three_and_close_before_next_batch(tmp_path):
    profiles = [f"profile-{index:03d}" for index in range(300)]
    events = []
    adapter = FakeAdsPowerAdapter(events)
    sessions = FakeSessionFactory(events)
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    scheduler = BatchScheduler(store, adapter, sessions, successful_executor(events))

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {"revision": 1}, profiles, 3))

    assert result["status"] == "completed"
    assert result["total_batches"] == 100
    assert adapter.max_active == 3
    assert adapter.started == profiles
    assert adapter.stopped == profiles
    for boundary in range(3, 300, 3):
        assert events.index(("closed", profiles[boundary - 1])) < events.index(("start", profiles[boundary]))
```

Add these independent tests using the same deterministic fakes:

```python
def test_one_profile_failure_does_not_cancel_its_siblings(tmp_path):
    async def execute(binding, _snapshot):
        if binding.profile_id == "p2":
            raise RuntimeError("planned failure")
        return ProfileOutcome(binding.profile_id, True, Stage.EXECUTE_ACTION)
    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([])
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), execute)
    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3"], 3))
    rows = {row["profile_id"]: row for row in store.list_profile_results("job-1")}
    assert result["status"] == "completed"
    assert rows["p1"]["status"] == "succeeded"
    assert rows["p2"]["status"] == "failed"
    assert rows["p3"]["status"] == "succeeded"


def test_close_failure_blocks_later_batches(tmp_path):
    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([], never_closes={"p2"})
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), successful_executor([]))
    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3", "p4"], 3))
    assert result["status"] == "cleanup_blocked"
    assert "p4" not in adapter.started
    assert adapter.active_checks["p2"] == 3


def test_cancel_request_closes_current_batch_without_starting_the_next(tmp_path):
    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([])
    async def execute(binding, _snapshot):
        store.request_cancel("job-1")
        return ProfileOutcome(binding.profile_id, True, Stage.EXECUTE_ACTION)
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), execute)
    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3", "p4"], 3))
    assert result["status"] == "cancelled"
    assert adapter.started == ["p1", "p2", "p3"]
    assert adapter.stopped == ["p1", "p2", "p3"]
```

Use controllable async events; do not call real `sleep()`.

- [ ] **Step 2: Run the scheduler tests and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_scheduler.py -q -p no:cacheprovider`

Expected: FAIL because `BatchScheduler` does not exist.

- [ ] **Step 3: Implement the minimal scheduler state machine**

```python
class BatchScheduler:
    def __init__(self, store, adspower, sessions, execute_profile):
        self.store = store
        self.adspower = adspower
        self.sessions = sessions
        self.execute_profile = execute_profile
        self._cancelled: set[str] = set()

    async def cancel(self, job_id: str) -> None:
        self._cancelled.add(job_id)
        self.store.request_cancel(job_id)
```

`run()` validates unique non-empty Profile IDs and `batch_size`, creates one immutable job snapshot, slices Profiles without dropping a remainder, and for each batch performs these fixed boundaries:

1. Start Profiles serially and store `starting`.
2. Bind each returned WebSocket to the same Profile and store `connecting_cdp`.
3. Execute all successfully bound Profiles with `asyncio.gather(..., return_exceptions=True)` so one failure does not cancel siblings.
4. Store each terminal execution result.
5. Stop every started Profile serially, query active state up to three times, and mark `close_confirmed` only after inactive.
6. If any Profile remains active, set it to `cleanup_failed`, set job to `cleanup_blocked`, and return without starting another batch.
7. If cancellation is requested, close the current batch and set `cancelled`; otherwise continue.

Public summaries contain no raw WebSocket URL. Unexpected exceptions are converted to a stable code and short summary, while `finally` always attempts cleanup for the current batch.

- [ ] **Step 4: Run the Phase 1 suite**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_models.py tests/test_execution_v2_store.py tests/test_execution_v2_adspower_adapter.py tests/test_execution_v2_session.py tests/test_execution_v2_scheduler.py -q -p no:cacheprovider`

Expected: PASS, including exactly 100 batches and maximum active Profiles of 3.

- [ ] **Step 5: Run adjacent regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_adspower.py tests/test_browser_cdp.py tests/test_browser_strategy_runtime.py tests/test_ghost_cursor_bridge.py -q -p no:cacheprovider`

Expected: PASS; Phase 1 introduces no changes to V1 behavior.

---

## Phase 1 Completion Gate

- `execution_v2` has no imports from `selector_probe`, Redis clients, model clients, or strategy gates.
- 300 fake Profiles produce exactly 100 batches at size 3.
- The next batch begins only after every Profile in the current batch is confirmed inactive.
- One Profile execution failure does not stop its siblings.
- Cleanup failure blocks later batches.
- SQLite data survives a new `ExecutionStore` instance.
- No existing dirty file is modified.
- Git commit is attempted only if `.git` becomes writable; otherwise record the permission blocker without altering the worktree.

## Later Plans

After this gate passes, write separate plans for:

1. Phase 2: picker overlay, multi-locator validation, element CRUD, readiness.
2. Phase 3: five action blocks, strict resolution, `human_move_to()` and `human_type()` reuse, once/duration execution, evidence.
3. Phase 4: `/api/browser-v2`, five-page UI, loopback-only direct access, persistent history, and real 6+2 Profile acceptance.
4. Post-acceptance cleanup: remove V1 probe/LLM/Redis/gate/auth calls in a separate reversible change.
