# Selector Probe Minimal Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make one probe task observable end to end, discover selectable interactive elements in observe mode, enter the TikTok comment panel safely, and connect selected drafts to the existing validation, publication, and strategy-gate paths.

**Architecture:** Keep the current Flask, Playwright, AdsPower, SQLite, Redis, and vanilla JavaScript implementation. Repair request-to-run persistence and CDP diagnostics in place; add one small pure discovery module; reuse existing semantic snapshots, element draft APIs, two-Profile/two-round validator, registry, alerts, and dependency service.

**Tech Stack:** Python 3, Flask, SQLite, Redis, Playwright async CDP, AdsPower Local API, vanilla JavaScript, pytest, Node.js built-in test runner.

## Global Constraints

- Daily schedule remains `03:00 Asia/Shanghai`.
- Use at least two dedicated AdsPower test Profiles.
- Observe mode never publishes selectors, changes Redis Active Bundle, or pauses strategies.
- Only navigation, reload, wait, bounded scroll, comment-panel open, and comment-panel close are permitted probe actions.
- Never input text, submit comments, like, follow, publish, or change account settings.
- Full Profile IDs, CDP endpoints, ports, API keys, cookies, comment text, raw DOM, raw AX trees, raw prompts, and raw model output remain private.
- Public Profile labels remain masked.
- Stop only Profiles started by the current probe; preserve pre-existing windows.
- Automatic recovery preserves manual pause reasons.
- Do not add a service, task queue, SSE, WebSocket, browser overlay, graphical tree editor, or general page-state engine.
- Use `active_video` for the comment entry and `visible_comment_panel` for comment input and submit.
- Use TDD for every production change.
- The current workspace has no Git metadata. Do not initialize a repository without user approval. Commit steps are checkpoints to run only if Git becomes available.

---

## File map

- `selector_probe/store.py`: run-request migration, request/run linking, progress persistence, logical run projection, legacy element seeding.
- `selector_probe/blueprint.py`: single active run API, dispatcher terminal callback, candidate projection.
- `selector_probe/worker.py`: pass logical request identity into probe execution; seed legacy elements.
- `selector_probe/session.py`: bounded Profile/CDP retries and sanitized stage callbacks.
- `selector_probe/probe.py`: progress recording, discovery evidence, two-state observe flow.
- `selector_probe/discovery.py`: new pure interactive-node filtering, fingerprinting, cross-Profile merge, safe comment-entry fallback definition.
- `selector_probe/state_runner.py`: accept a run-local comment-entry fallback while preserving action allowlist.
- `gateway/static/selector_probe_ui.js`: active-run polling, disabled run button, candidate sanitization/rendering/selection.
- `gateway/static/selector_probe.css`: minimal stage and candidate list styles.
- `gateway/app.py`: add small run-detail candidate containers; preserve existing strategy dependency sync.
- `tests/test_selector_probe_store.py`: request migration, linking, progress, logical list.
- `tests/test_selector_probe_dispatcher.py`: terminal callback and no swallowed failures.
- `tests/test_selector_probe_management_routes.py`: deduplicated run API and linked detail.
- `tests/test_selector_probe_session.py`: active-without-CDP polling, retries, ownership.
- `tests/test_selector_probe_discovery.py`: pure discovery and merge tests.
- `tests/test_selector_probe_observe.py`: feed and comment-panel discovery integration.
- `tests/test_selector_probe_catalog.py`: idempotent legacy seeding.
- `tests/test_app.py`: dependency rebuild regression.
- `tests-js/selector-probe-operations.test.js`: run UI and polling.
- `tests-js/selector-probe-elements.test.js`: discovered candidate selection into existing draft flow.

---

### Task 1: One logical run with durable terminal status

**Files:**
- Modify: `selector_probe/store.py`
- Modify: `selector_probe/worker.py`
- Modify: `selector_probe/blueprint.py`
- Test: `tests/test_selector_probe_store.py`
- Test: `tests/test_selector_probe_dispatcher.py`
- Test: `tests/test_selector_probe_management_routes.py`

**Interfaces:**
- Produces: `SelectorProbeStore.active_management_run_request() -> dict[str, object] | None`
- Produces: `SelectorProbeStore.expire_stale_management_run_requests(*, now: str, stale_after_seconds: int = 1800) -> int`
- Produces: `SelectorProbeStore.link_management_run(request_id: str, run_id: int) -> None`
- Produces: `SelectorProbeStore.finish_management_run_request(request_id: str, *, status: str, failure_code: str = "") -> None`
- Produces: `SelectorProbeStore.update_run_progress(run_id: int, *, attempt_token: str, stages: list[dict[str, object]]) -> None`
- Changes: `run_tick(..., management_request_id: str = "") -> dict`
- Changes: `RedisRunDispatcher(..., terminal_callback: Callable | None = None)`

- [ ] **Step 1: Write failing store migration and lifecycle tests**

Add focused tests:

```python
def test_management_run_request_links_execution_and_is_only_visible_run(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        request = store.create_management_run_request(
            "request-a",
            actor_user_id=7,
            actor_username="operator",
        )
        run_id = store.start_run(
            scheduled_for="2026-07-30T03:00:00+00:00",
            active_version_before="",
            attempt_token="attempt-a",
            management_request_id=request["id"],
            trigger="manual",
        )
        store.finish_run(
            run_id,
            status="completed",
            details={"observe_only": True},
            attempt_token="attempt-a",
        )

        rows, total, _revision = store.list_management_rows(
            "runs", page=1, page_size=20
        )

    assert total == 1
    assert rows[0]["id"] == "request-a"
    assert rows[0]["probe_run_id"] == run_id
    assert rows[0]["status"] == "completed"


def test_second_active_request_returns_existing_request(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        first = store.create_management_run_request(
            "request-a",
            actor_user_id=7,
            actor_username="operator",
        )
        second = store.create_management_run_request(
            "request-b",
            actor_user_id=8,
            actor_username="other",
        )

    assert first["id"] == "request-a"
    assert second == {**first, "deduplicated": True}


def test_stale_active_request_does_not_block_future_run(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.create_management_run_request(
            "stale",
            actor_user_id=7,
            actor_username="operator",
        )
        store.connection.execute(
            """
            UPDATE management_run_requests
            SET created_at = ?, updated_at = ?
            WHERE id = 'stale'
            """,
            (
                "2026-07-30T02:00:00+00:00",
                "2026-07-30T02:00:00+00:00",
            ),
        )
        store.connection.commit()
        expired = store.expire_stale_management_run_requests(
            now="2026-07-30T03:00:01+00:00"
        )
        fresh = store.create_management_run_request(
            "fresh",
            actor_user_id=7,
            actor_username="operator",
        )

    assert expired == 1
    assert fresh["id"] == "fresh"


def test_legacy_accepted_rows_migrate_to_failed_without_deletion(tmp_path):
    path = tmp_path / "probe.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE probe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            attempt_token TEXT NOT NULL DEFAULT '',
            active_version_before TEXT NOT NULL DEFAULT '',
            published_version_after TEXT NOT NULL DEFAULT '',
            failed_aliases_json TEXT NOT NULL DEFAULT '[]',
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE management_run_requests (
            id TEXT PRIMARY KEY,
            actor_user_id INTEGER NOT NULL,
            actor_username TEXT NOT NULL,
            retry_of_run_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK (
                status IN ('accepted', 'dispatch_failed')
            ),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO management_run_requests
        VALUES ('old', 1, 'admin', '', 'accepted', ?, ?)
        """,
        ("2026-07-30T03:00:00+00:00", "2026-07-30T03:00:00+00:00"),
    )
    connection.commit()
    connection.close()

    with SelectorProbeStore(path) as store:
        row = store.management_run_detail("old")

    assert row["status"] == "failed"
    assert row["failure_code"] == "legacy_unlinked_request"
```

- [ ] **Step 2: Run store tests and observe RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_store.py -q -p no:cacheprovider
```

Expected: new tests fail because lifecycle columns and signatures do not exist.

- [ ] **Step 3: Rebuild `management_run_requests` safely**

Add `_migrate_management_run_lifecycle()` to `SelectorProbeStore.__init__` after existing management migrations.

Target schema:

```sql
CREATE TABLE management_run_requests (
    id TEXT PRIMARY KEY,
    actor_user_id INTEGER NOT NULL CHECK (actor_user_id > 0),
    actor_username TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'retry')),
    retry_of_run_id TEXT NOT NULL DEFAULT '',
    probe_run_id INTEGER REFERENCES probe_runs(id),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'running', 'completed', 'failed', 'dispatch_failed'
        )
    ),
    failure_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX idx_management_run_requests_active
ON management_run_requests((1))
WHERE status IN ('queued', 'running');
```

Migration rules:

```python
legacy_status = str(row["status"])
status = (
    "dispatch_failed"
    if legacy_status == "dispatch_failed"
    else "failed"
)
failure_code = (
    "dispatcher_unavailable"
    if legacy_status == "dispatch_failed"
    else "legacy_unlinked_request"
)
```

Copy every legacy row; do not delete evidence from `probe_runs`.

- [ ] **Step 4: Implement logical request methods and link in `start_run` / `finish_run`**

Use these signatures:

```python
def active_management_run_request(self) -> dict[str, object] | None:
    row = self.connection.execute(
        """
        SELECT * FROM management_run_requests
        WHERE status IN ('queued', 'running')
        ORDER BY created_at, id
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row is not None else None


def expire_stale_management_run_requests(
    self,
    *,
    now: str,
    stale_after_seconds: int = 1800,
) -> int:
    selected_now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    cutoff = (selected_now - timedelta(seconds=stale_after_seconds)).isoformat()
    with self.connection:
        cursor = self.connection.execute(
            """
            UPDATE management_run_requests
            SET status = 'failed',
                failure_code = 'stale_run_request',
                finished_at = ?,
                updated_at = ?
            WHERE status IN ('queued', 'running') AND updated_at <= ?
            """,
            (now, now, cutoff),
        )
    return int(cursor.rowcount)


def link_management_run(self, request_id: str, run_id: int) -> None:
    selected = _gate_text(request_id, "request_id")
    run = _positive_integer(run_id, "run_id")
    with self.connection:
        cursor = self.connection.execute(
            """
            UPDATE management_run_requests
            SET probe_run_id = ?, status = 'running', updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (run, _utc_now(), selected),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("management run request cannot be linked")


def finish_management_run_request(
    self,
    request_id: str,
    *,
    status: str,
    failure_code: str = "",
) -> None:
    if status not in {"completed", "failed", "dispatch_failed"}:
        raise ValueError("management run terminal status is invalid")
    selected = _gate_text(request_id, "request_id")
    code = _required_text_or_empty(failure_code, "failure_code")
    now = _utc_now()
    with self.connection:
        self.connection.execute(
            """
            UPDATE management_run_requests
            SET status = ?, failure_code = ?, updated_at = ?, finished_at = ?
            WHERE id = ? AND status IN ('queued', 'running')
            """,
            (status, code, now, now, selected),
        )
```

Extend `start_run` with `management_request_id=""` and `trigger="scheduled"`.
For manual runs, link the existing request after `probe_runs` insert. For
scheduled runs, create a system request inside the same transaction using
`uuid.uuid4().hex`, actor ID `1`, actor name `system`, and trigger `scheduled`.

Extend `finish_run` to update the linked request in the same transaction:

```python
logical_status = (
    "completed"
    if status in {"completed", "healthy", "published"}
    else "failed"
)
failure_code = str(details.get("failure_code") or "")
```

Every progress update also refreshes the linked request `updated_at`. Before
creating a manual or scheduled request, expire active rows with no progress for
30 minutes. This recovers a crashed process after its Redis lease expires.

- [ ] **Step 5: Project logical rows only**

Replace the current `stored + requested` merge in `list_management_rows("runs")`
with one query joining the linked execution:

```sql
SELECT request.*,
       run.scheduled_for,
       run.started_at,
       run.finished_at AS run_finished_at,
       run.active_version_before,
       run.published_version_after,
       run.failed_aliases_json,
       run.details_json
FROM management_run_requests request
LEFT JOIN probe_runs run ON run.id = request.probe_run_id
ORDER BY COALESCE(run.started_at, request.created_at) DESC, request.id DESC
```

`management_run_detail(request_id)` performs the same join and loads
`selector_validation_runs` by `probe_run_id`. Preserve `actor_username` and
`trigger`; remove the UI fallback that turns missing actor into `系统`.

- [ ] **Step 6: Write dispatcher terminal-callback tests**

Add:

```python
def test_dispatcher_reports_terminal_result_and_sanitized_failure():
    redis = SharedRedis()
    completed = threading.Event()
    terminals = []

    def fail_tick(*, force, management_request_id):
        assert force is True
        assert management_request_id == "request-a"
        raise RuntimeError("ws://127.0.0.1:9222 api_key=secret")

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=fail_tick,
        terminal_callback=lambda request_id, result, code: terminals.append(
            (request_id, result, code)
        ),
        environment="production",
        site="tiktok",
    )
    dispatcher("request-a", completed.set)
    assert completed.wait(0.5)
    assert terminals == [("request-a", None, "probe_unavailable")]
    assert "secret" not in repr(terminals)
```

- [ ] **Step 7: Remove swallowed exceptions and pass request identity**

In `RedisRunDispatcher.run`, pass `management_request_id=request_id` when the
tick runner supports it. Convert every terminal exception to a safe code:

```python
result = None
failure_code = ""
try:
    result = self.tick_runner(**kwargs)
except BaseException as error:
    failure_code = _safe_dispatch_failure_code(error)
finally:
    if self.terminal_callback is not None:
        self.terminal_callback(request_id, result, failure_code)
```

The callback updates a still-queued/running request. It never stores
`str(error)`.

- [ ] **Step 8: Make run-now deduplicate**

Before reserving a new management operation, query
`active_management_run_request()`. If present, return HTTP 202:

```json
{
  "status": "running",
  "request_id": "existing-id",
  "run_id": "existing-id",
  "deduplicated": true
}
```

Do not create a new request or audit row. Redis busy after request creation
must mark that request `failed`, never leave `queued`.

- [ ] **Step 9: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_store.py tests/test_selector_probe_dispatcher.py tests/test_selector_probe_management_routes.py -q -p no:cacheprovider -W error
```

Expected: PASS.

- [ ] **Step 10: Commit checkpoint if Git exists**

```powershell
git add selector_probe/store.py selector_probe/worker.py selector_probe/blueprint.py tests/test_selector_probe_store.py tests/test_selector_probe_dispatcher.py tests/test_selector_probe_management_routes.py
git commit -m "fix: link selector probe runs"
```

If `git rev-parse --is-inside-work-tree` fails, record the checkpoint in the
task report and do not initialize Git.

---

### Task 2: Profile/CDP retries and safe stage evidence

**Files:**
- Modify: `selector_probe/session.py`
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/store.py`
- Test: `tests/test_selector_probe_session.py`
- Test: `tests/test_selector_probe_observe.py`

**Interfaces:**
- Produces: `ProgressSink = Callable[[dict[str, object]], None]`
- Changes: `ProbeSessionManager(..., progress_sink=None, sleep_fn=time.sleep, readiness_attempts=3)`
- Consumes: `SelectorProbeStore.update_run_progress(...)` from Task 1.

- [ ] **Step 1: Write failing session retry tests**

```python
def test_active_profile_without_cdp_is_polled_not_started():
    class DelayedActive(FakeAdsPower):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def get_browser_active(self, profile_id):
            self.active_calls.append(profile_id)
            self.reads += 1
            if self.reads < 3:
                return {"data": {"status": "Active", "ws": {}}}
            return {
                "data": {
                    "status": "Active",
                    "ws": {"puppeteer": f"ws://{profile_id}"},
                }
            }

    client = DelayedActive()
    events = []
    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("profile-a", "profile-b"),
        wait_for_cdp=lambda _url: True,
        progress_sink=events.append,
        sleep_fn=lambda _seconds: None,
    )
    handles = manager.open_profiles(("profile-a", "profile-b"))

    assert client.started == []
    assert len(handles) == 2
    assert any(
        item["stage"] == "cdp_endpoint" and item["attempt"] == 3
        for item in events
    )


def test_unhealthy_preexisting_profile_is_never_stopped():
    class BrokenActive(FakeAdsPower):
        def get_browser_active(self, profile_id):
            return {"data": {"status": "Active", "ws": {}}}

    client = BrokenActive()
    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("profile-a", "profile-b"),
        wait_for_cdp=lambda _url: True,
        sleep_fn=lambda _seconds: None,
    )
    with pytest.raises(ProbeSessionError) as caught:
        manager.open_profiles(("profile-a", "profile-b"))

    assert caught.value.code == "preexisting_profile_unhealthy"
    assert client.started == []
    assert client.stopped == []
```

- [ ] **Step 2: Run session tests and observe RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_session.py -q -p no:cacheprovider
```

Expected: constructor rejects new parameters or active Profile is incorrectly
started.

- [ ] **Step 3: Add bounded safe progress projection**

Add:

```python
_SAFE_STAGE_SUMMARIES = {
    "profile_start_rejected": "AdsPower rejected Profile start",
    "active_cdp_unavailable": "Active Profile did not expose a CDP endpoint",
    "cdp_unavailable": "CDP endpoint did not become reachable",
    "cdp_connect_failed": "Playwright could not connect over CDP",
}


def _progress(
    sink: Callable[[dict[str, object]], None] | None,
    *,
    stage: str,
    profile_mask: str,
    status: str,
    attempt: int,
    failure_code: str = "",
) -> None:
    if sink is None:
        return
    sink({
        "name": stage,
        "profile_mask": mask_profile_id(profile_mask),
        "status": status,
        "attempt_count": attempt,
        "failure_code": failure_code,
        "summary": _SAFE_STAGE_SUMMARIES.get(failure_code, ""),
    })
```

Keep summaries from an allowlist. Never project caught exception text.

- [ ] **Step 4: Correct active/start behavior**

Refactor profile opening into one helper:

```python
def _profile_endpoint(self, profile_id: str, profile_mask: str) -> tuple[str, bool]:
    for attempt in range(1, self._readiness_attempts + 1):
        active = self._client.get_browser_active(profile_id)
        is_active, ws_url = _active_browser(active)
        if ws_url:
            return ws_url, False
        if is_active:
            _progress(
                self._progress_sink,
                stage="cdp_endpoint",
                profile_mask=profile_mask,
                status="running",
                attempt=attempt,
            )
            if attempt < self._readiness_attempts:
                self._sleep_fn(min(attempt, 2))
                continue
            raise ProbeSessionError(
                "preexisting_profile_unhealthy", profile_mask
            )
        break

    for attempt in range(1, self._readiness_attempts + 1):
        try:
            candidate = self._client.start_browser(profile_id)
        except Exception:
            candidate = ""
        if _is_cdp_url(candidate):
            return str(candidate), True
        if attempt < self._readiness_attempts:
            self._sleep_fn(min(attempt, 2))
    raise ProbeSessionError("profile_open_failed", profile_mask)
```

Preserve current cleanup semantics for Profiles started by the probe.

- [ ] **Step 5: Persist bounded progress from observe**

In `run_observe_probe`, maintain at most 30 stage entries. Merge repeated
updates by `(name, profile_mask)` and persist through
`store.update_run_progress` when available.

```python
def record_progress(event):
    key = (event.get("name"), event.get("profile_mask", ""))
    stage_map[key] = _sanitize_progress_event(event)
    updater = getattr(store, "update_run_progress", None)
    if callable(updater) and run_id is not None:
        updater(
            run_id,
            attempt_token=attempt_token,
            stages=list(stage_map.values())[-30:],
        )
```

Add navigate, page-ready, snapshot, validation, cleanup, and lease-release
events around existing operations. Final `details` contains the same stages.

- [ ] **Step 6: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_session.py tests/test_selector_probe_observe.py -q -p no:cacheprovider -W error
```

Expected: PASS; existing ownership and redaction tests remain green.

- [ ] **Step 7: Commit checkpoint if Git exists**

```powershell
git add selector_probe/session.py selector_probe/probe.py selector_probe/store.py tests/test_selector_probe_session.py tests/test_selector_probe_observe.py
git commit -m "fix: diagnose probe profile startup"
```

---

### Task 3: Interactive discovery and safe comment-panel state

**Files:**
- Create: `selector_probe/discovery.py`
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/state_runner.py`
- Modify: `selector_probe/blueprint.py`
- Test: `tests/test_selector_probe_discovery.py`
- Test: `tests/test_selector_probe_observe.py`
- Test: `tests/test_selector_probe_state_runner.py`

**Interfaces:**
- Produces: `discover_interactive_candidates(snapshot: Mapping, *, page_state: str, profile_mask: str) -> list[dict[str, object]]`
- Produces: `merge_discovery_candidates(validations: Sequence[Mapping]) -> list[dict[str, object]]`
- Produces: `comment_entry_definition(candidates: Sequence[Mapping]) -> dict[str, object] | None`
- Adds projected run field: `discoveries: list[dict[str, object]]`.

- [ ] **Step 1: Write pure discovery tests**

```python
def semantic_snapshot(*nodes):
    return {"scope": "page", "viewport": [1280, 720], "nodes": list(nodes)}


def test_discovery_keeps_interactive_safe_nodes_and_rejects_hidden_nodes():
    result = discover_interactive_candidates(
        semantic_snapshot(
            {
                "backend_node_id": 10,
                "parent_backend_node_id": 1,
                "tag": "button",
                "role": "button",
                "name": "Comments",
                "states": {},
                "attributes": {"data-e2e": "comment-icon"},
                "bounds": [10, 10, 40, 40],
                "visible": True,
                "in_viewport": True,
                "actionable": True,
            },
            {
                "backend_node_id": 11,
                "parent_backend_node_id": 1,
                "tag": "button",
                "role": "button",
                "name": "Hidden",
                "states": {},
                "attributes": {},
                "bounds": None,
                "visible": False,
                "in_viewport": False,
                "actionable": False,
            },
        ),
        page_state="feed_ready",
        profile_mask="***0001",
    )

    assert len(result) == 1
    assert result[0]["role"] == "button"
    assert result[0]["attributes"] == {"data-e2e": "comment-icon"}
    assert "backend_node_id" not in result[0]


def test_cross_profile_merge_reports_consistency():
    comment = {
        "fingerprint": "sha256:comment",
        "page_state": "feed_ready",
        "scope": "active_video",
        "role": "button",
        "name": "Comments",
        "attributes": {"data-e2e": "comment-icon"},
        "actionable": True,
    }
    merged = merge_discovery_candidates([
        {
            "profile_mask": "***0001",
            "page_state": "feed_ready",
            "evidence": {"discoveries": [comment]},
        },
        {
            "profile_mask": "***0002",
            "page_state": "feed_ready",
            "evidence": {"discoveries": [comment]},
        },
    ])
    assert merged[0]["profile_count"] == 2
    assert merged[0]["profile_masks"] == ["***0001", "***0002"]
```

- [ ] **Step 2: Run discovery tests and observe RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_discovery.py -q -p no:cacheprovider
```

Expected: import fails because `selector_probe.discovery` does not exist.

- [ ] **Step 3: Implement pure bounded discovery**

Create `selector_probe/discovery.py` with:

```python
INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "checkbox", "radio",
    "combobox", "menuitem", "tab", "switch",
})
STABLE_ATTRIBUTE_NAMES = (
    "data-e2e", "data-testid", "aria-label", "name", "placeholder",
    "contenteditable", "type", "id",
)


def _fingerprint(page_state, role, name, attributes):
    stable = {
        key: attributes[key]
        for key in STABLE_ATTRIBUTE_NAMES
        if isinstance(attributes.get(key), str) and attributes[key]
    }
    canonical = json.dumps(
        {
            "page_state": page_state,
            "role": role,
            "name": name,
            "attributes": stable,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def discover_interactive_candidates(snapshot, *, page_state, profile_mask):
    nodes = snapshot.get("nodes", []) if isinstance(snapshot, Mapping) else []
    result = []
    for node in nodes[:500]:
        if not isinstance(node, Mapping):
            continue
        role = str(node.get("role") or "").casefold()
        if role not in INTERACTIVE_ROLES:
            continue
        if node.get("visible") is not True or node.get("in_viewport") is not True:
            continue
        attributes = {
            key: value
            for key in STABLE_ATTRIBUTE_NAMES
            if isinstance((value := node.get("attributes", {}).get(key)), str)
            and value
        }
        name = str(node.get("name") or "")[:160]
        result.append({
            "fingerprint": _fingerprint(page_state, role, name, attributes),
            "page_state": page_state,
            "scope": (
                "visible_comment_panel"
                if page_state == "comment_panel_open"
                else "active_video"
            ),
            "profile_mask": mask_profile_id(profile_mask),
            "role": role,
            "name": name,
            "states": dict(node.get("states") or {}),
            "attributes": attributes,
            "visible": True,
            "in_viewport": True,
            "actionable": node.get("actionable") is True,
        })
    return result[:200]
```

`merge_discovery_candidates` groups by fingerprint and strips duplicate
Profile entries. Never return backend node IDs, bounds, or raw snapshot.

- [ ] **Step 4: Generate a safe comment-entry transition definition**

Use the existing canonical comment contract from
`default_tiktok_contracts()`. Accept only one candidate that:

- has page state `feed_ready`;
- role `button`;
- is actionable;
- has `data-e2e=comment-icon`, or its normalized Name is one of the canonical
  accepted Names;
- produces a structured attribute or role Locator.

Return:

```python
{
    "scope": "active_video",
    "locators": [
        {
            "id": "probe-comment-entry",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-icon",
            "enabled": True,
        }
    ],
}
```

Return `None` for zero or multiple matches.

- [ ] **Step 5: Store discovery evidence in observe records**

After each semantic snapshot in `_default_observe_page`:

```python
discoveries = discover_interactive_candidates(
    snapshot_payload,
    page_state=state,
    profile_mask=profile_mask,
)
```

Pass `profile_mask` into the observer call. Store `discoveries` beside
`semantic_snapshot` in `evidence_json`.

After `feed_ready`, derive a run-local comment-entry definition. If the saved
definition is absent or fails Dry-Run, use the unique fallback. If none exists,
record `comment_entry_confirmation_required`, retain feed discoveries, and do
not click.

- [ ] **Step 6: Keep state runner allowlisted**

Add an optional run-local definition argument:

```python
async def ensure_state(
    self,
    page,
    state,
    elements,
    *,
    comment_entry_override=None,
):
    selected = dict(elements or {})
    if comment_entry_override is not None:
        selected[self.comment_entry_alias] = comment_entry_override
```

The override is accepted only for `comment_panel_open`, validated by
`normalize_element_definitions`, and cannot change the action type.

- [ ] **Step 7: Project merged discoveries**

In `_management_project_run`, decode validation evidence internally and call
`merge_discovery_candidates`. Project only:

- fingerprint;
- page state;
- scope;
- Role and Name;
- states and stable attributes;
- actionable;
- Profile masks/count;
- recommended structured Locators.

Cap at 200 candidates.

- [ ] **Step 8: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_discovery.py tests/test_selector_probe_state_runner.py tests/test_selector_probe_observe.py tests/test_selector_probe_management_routes.py -q -p no:cacheprovider -W error
```

Expected: PASS. Assertions confirm no input or submit call occurs.

- [ ] **Step 9: Commit checkpoint if Git exists**

```powershell
git add selector_probe/discovery.py selector_probe/probe.py selector_probe/state_runner.py selector_probe/blueprint.py tests/test_selector_probe_discovery.py tests/test_selector_probe_observe.py tests/test_selector_probe_state_runner.py
git commit -m "feat: discover probe element candidates"
```

---

### Task 4: Minimal run and candidate UI

**Files:**
- Modify: `gateway/app.py`
- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Test: `tests-js/selector-probe-operations.test.js`
- Test: `tests-js/selector-probe-elements.test.js`
- Test: `tests/test_selector_probe_console_shell.py`

**Interfaces:**
- Consumes: logical run `status`, `stages`, and `discoveries` from Tasks 1–3.
- Reuses: existing `openElementWizard()` and `createElementDraft(form)`.
- Produces: `openDiscoveryCandidate(candidate)` UI controller method.

- [ ] **Step 1: Write failing JavaScript tests**

```javascript
test("active run disables run-now and terminal detail stops polling", async () => {
  const timers = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url) => {
      if (url === "/api/auth/session") return response({role: "operator"});
      if (url.endsWith("/status")) return response({});
      if (url.endsWith("/runs/run-a")) {
        return response({id: "run-a", status: "running", stages: []});
      }
      return response({items: [{id: "run-a", status: "running"}]});
    },
    setInterval: (callback, milliseconds) => {
      timers.push({callback, milliseconds});
      return timers.length;
    },
    clearInterval() {},
    render() {},
  });
  await ui.init();
  await ui.openRunDetail("run-a");
  assert.equal(ui.state.operationWorkspace.busy, true);
  assert.equal(timers.at(-1).milliseconds, 1000);
});


test("discovered candidate opens prefilled existing element wizard", async () => {
  const ui = createSelectorProbeUI({
    requestJson: async (url) => {
      if (url === "/api/auth/session") {
        return response({role: "administrator"});
      }
      if (url.endsWith("/status")) return response({});
      return response({items: [], page: 1, page_size: 20, total: 0});
    },
    setInterval: () => 1,
    clearInterval() {},
    render() {},
  });
  await ui.init();
  ui.openDiscoveryCandidate({
    fingerprint: "sha256:safe",
    page_state: "comment_panel_open",
    scope: "visible_comment_panel",
    role: "textbox",
    name: "Add comment",
    attributes: {"data-e2e": "comment-input"},
    recommended_locators: [{
      id: "probe-input",
      type: "attribute",
      name: "data-e2e",
      value: "comment-input",
      enabled: true,
    }],
  });
  assert.equal(ui.state.selected.kind, "wizard");
  assert.equal(ui.state.selected.form.requiredState, "comment_panel_open");
  assert.equal(ui.state.selected.form.scope, "visible_comment_panel");
  assert.deepEqual(ui.state.selected.form.acceptedRoles, ["textbox"]);
});
```

- [ ] **Step 2: Run Node tests and observe RED**

```powershell
node --test tests-js/selector-probe-operations.test.js tests-js/selector-probe-elements.test.js
```

Expected: missing discovery method or active polling behavior.

- [ ] **Step 3: Sanitize discoveries**

Add:

```javascript
function sanitizeDiscovery(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  return {
    fingerprint: safeText(source.fingerprint, 80),
    page_state: safeCode(source.page_state),
    scope: safeCode(source.scope),
    role: safeCode(source.role),
    name: safeText(source.name, 160),
    states: source.states && typeof source.states === "object"
      ? {...source.states}
      : {},
    attributes: sanitizeStableAttributes(source.attributes),
    actionable: source.actionable === true,
    profile_masks: safeStringList(source.profile_masks, 8)
      .filter(validProfileMask),
    profile_count: Math.max(Number(source.profile_count) || 0, 0),
    recommended_locators: sanitizeStructuredLocators(
      source.recommended_locators || [],
      {editable: false},
    ),
  };
}
```

Extend `sanitizeRun` with at most 200 discoveries.

- [ ] **Step 4: Poll active detail and disable duplicate run**

Treat `queued` and `running` as active. Reuse existing polling helpers:

```javascript
const ACTIVE_RUN_STATUSES = new Set(["queued", "running"]);

function runIsActive(run) {
  return ACTIVE_RUN_STATUSES.has(safeCode(run?.status));
}
```

While active:

- `operationWorkspace.busy = true`;
- poll detail every 1000 ms;
- disable `#selector-run-now`;
- replace detail on each response;
- stop polling on terminal status or workspace close.

A deduplicated HTTP 202 response opens the existing run without showing
`probe_busy`.

- [ ] **Step 5: Render compact stages and discoveries**

Add two containers to the existing run detail workspace in `gateway/app.py`:

```html
<section id="selector-run-stage-detail"></section>
<section id="selector-run-discoveries"></section>
```

Render discoveries grouped by page state. Each row contains:

- checkbox/button `加入元素目录`;
- Role and Name;
- safe stable attributes;
- `Profile 2/2`;
- actionable state;
- expandable safe node details.

No tree framework or raw JSON dump.

- [ ] **Step 6: Prefill existing draft wizard**

`openDiscoveryCandidate(candidate)` maps:

```javascript
{
  displayName: candidate.name || candidate.role,
  intent: `locate ${candidate.name || candidate.role}`,
  requiredState: candidate.page_state,
  scope: candidate.scope,
  probeAction: (
    candidate.page_state === "feed_ready"
    && candidate.attributes["data-e2e"] === "comment-icon"
  ) ? "open_read_only" : "inspect_only",
  acceptedRoles: [candidate.role],
  acceptedNames: candidate.name ? [candidate.name] : [],
  preferredAttributes: Object.keys(candidate.attributes),
  postcondition: (
    candidate.attributes["data-e2e"] === "comment-icon"
      ? "comment_panel_open"
      : ""
  ),
}
```

The existing POST `/api/selector-probe/elements` remains the save endpoint.

- [ ] **Step 7: Add minimal CSS**

Use current selector card styles. Add only:

```css
.selector-run-stage-list,
.selector-discovery-list {
  display: grid;
  gap: 0.75rem;
}

.selector-discovery-row {
  border: 1px solid var(--border-color);
  border-radius: 0.75rem;
  padding: 0.75rem;
}
```

Match existing CSS variables; do not introduce a design system.

- [ ] **Step 8: Run UI tests**

```powershell
node --test tests-js/selector-probe-operations.test.js tests-js/selector-probe-elements.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_console_shell.py -q -p no:cacheprovider -W error
```

Expected: PASS.

- [ ] **Step 9: Commit checkpoint if Git exists**

```powershell
git add gateway/app.py gateway/static/selector_probe_ui.js gateway/static/selector_probe.css tests-js/selector-probe-operations.test.js tests-js/selector-probe-elements.test.js tests/test_selector_probe_console_shell.py
git commit -m "feat: show probe discoveries"
```

---

### Task 5: Seed legacy elements and preserve automatic dependencies

**Files:**
- Modify: `selector_probe/store.py`
- Modify: `selector_probe/worker.py`
- Test: `tests/test_selector_probe_catalog.py`
- Test: `tests/test_selector_probe_worker.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Produces: `SelectorProbeStore.seed_legacy_elements(elements: Mapping, contracts: Mapping) -> int`
- Reuses: `default_tiktok_contracts()`
- Reuses unchanged: `StrategyGateService.rebuild_dependencies(strategies)`.

- [ ] **Step 1: Write failing idempotent seed tests**

```python
def test_seed_legacy_comment_elements_is_idempotent(tmp_path):
    elements = {
        "评论入口": {
            "scope": "active_video",
            "locators": [{
                "id": "legacy-entry",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }],
        },
        "评论输入框": {
            "scope": "visible_comment_panel",
            "locators": [{
                "id": "legacy-input",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-input",
                "enabled": True,
            }],
        },
        "评论提交按钮": {
            "scope": "visible_comment_panel",
            "locators": [{
                "id": "legacy-submit",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-post",
                "enabled": True,
            }],
        },
    }
    contracts = default_tiktok_contracts()
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        assert store.seed_legacy_elements(elements, contracts) == 3
        assert store.seed_legacy_elements(elements, contracts) == 0
        rows = store.connection.execute(
            """
            SELECT id, management_source, draft_status, scope
            FROM managed_elements ORDER BY id
            """
        ).fetchall()
        drafts = store.connection.execute(
            "SELECT element_id, candidates_json FROM element_drafts"
        ).fetchall()

    assert len(rows) == 3
    assert {row["management_source"] for row in rows} == {"legacy_manual"}
    assert {row["draft_status"] for row in rows} == {"draft"}
    assert {row["scope"] for row in rows} == {
        "active_video", "visible_comment_panel"
    }
    assert all(json.loads(row["candidates_json"]) for row in drafts)
```

- [ ] **Step 2: Run catalog tests and observe RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_catalog.py -q -p no:cacheprovider
```

Expected: `seed_legacy_elements` missing.

- [ ] **Step 3: Implement one-transaction legacy seed**

Normalize `elements` with `normalize_element_definitions`. For each alias absent
from `managed_elements`:

- use canonical contract when available;
- override comment scopes to canonical contract scope;
- write `managed_elements` as `legacy_manual`, published status `using_lkg`,
  draft status `draft`;
- write the original ordered Locators into `element_drafts.candidates_json`;
- write canonical contract JSON;
- add one sanitized `legacy_element_seeded` audit event.

Skip existing rows without modification. Commit all inserted rows together.
Return inserted count.

- [ ] **Step 4: Seed before observe**

In `run_tick`, after normalizing `browser.action_elements` and opening the
store, call:

```python
seeder = getattr(store, "seed_legacy_elements", None)
if callable(seeder):
    seeder(elements, default_tiktok_contracts())
```

Run before observe/healing dispatch. No settings rewrite occurs.

- [ ] **Step 5: Prove existing dependency sync remains correct**

Run existing regression tests
`test_strategy_save_rebuilds_dependency_index_before_persisting`,
`test_strategy_save_rejects_dependency_failure_without_persisting`,
`test_strategy_write_failure_rolls_dependency_index_back_to_old_config`, and
the `StrategyGateService.rebuild_dependencies` tests. These already prove that
strategy save replaces the alias index before persistence and compensates a
settings-write failure.

Do not add a second dependency mechanism. Leave `gateway/app.py` unchanged
when these tests pass.

- [ ] **Step 6: Run focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_catalog.py tests/test_selector_probe_worker.py tests/test_app.py -q -p no:cacheprovider -W error
```

Expected: PASS.

- [ ] **Step 7: Commit checkpoint if Git exists**

```powershell
git add selector_probe/store.py selector_probe/worker.py tests/test_selector_probe_catalog.py tests/test_selector_probe_worker.py tests/test_app.py
git commit -m "feat: seed selector element drafts"
```

---

### Task 6: Regression, security, and bounded live acceptance

**Files:**
- Modify only if a failing focused assertion requires it.
- Test: existing Python and Node suites.
- Create: `docs/superpowers/reports/2026-07-30-selector-probe-minimal-repair-verification.md`

**Interfaces:**
- Consumes all prior tasks.
- Produces sanitized verification evidence and go/no-go decision.

- [ ] **Step 1: Run complete selector-probe Python suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_*.py -q -p no:cacheprovider -W error
```

Expected: PASS.

- [ ] **Step 2: Run browser strategy dependency regression**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_browser_strategy_config.py tests/test_browser_strategy_runtime.py -q -p no:cacheprovider -W error
```

Expected: PASS.

- [ ] **Step 3: Run selector UI suite**

```powershell
node --test tests-js/selector-probe-console.test.js tests-js/selector-probe-elements.test.js tests-js/selector-probe-operations.test.js tests-js/selector-probe-settings.test.js
```

Expected: PASS.

- [ ] **Step 4: Run repository secret scan**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository_secret_scan.py tests/test_selector_probe_redaction.py -q -p no:cacheprovider -W error
```

Expected: PASS; no complete dedicated Profile ID, CDP URL, API key, Cookie, raw
DOM, or raw AX tree appears.

- [ ] **Step 5: Start one observe-mode live task**

Preconditions:

- rollout mode `observe`;
- probe enabled;
- exactly the two approved dedicated test Profiles configured;
- Redis reachable;
- AdsPower Local API reachable;
- no production queue execution.

Submit one run. Do not click again while active.

Verify:

- one logical task row;
- status progresses from queued/running to terminal;
- both masked Profiles appear;
- Profile/CDP stages have attempts and durations;
- `feed_ready` candidates exist;
- comment entry opens only through the allowlisted transition;
- `comment_panel_open` candidates include textbox and submit button;
- cleanup and lease release finish;
- no Redis selector version publishes;
- no strategy gate changes.

- [ ] **Step 6: Create three drafts from discovered candidates**

Select:

- comment entry;
- comment input;
- comment submit.

Verify:

- all save as drafts;
- entry uses `active_video`;
- input and submit use `visible_comment_panel`;
- none appears in strategy picker as published;
- no text input or submit action occurs during probe.

- [ ] **Step 7: Verify dependency and gate isolation with test data**

Using non-production strategies:

- strategy A references comment input;
- strategy B references an unrelated healthy alias;
- add a probe gate reason for comment input;
- confirm only A pauses;
- add a manual pause to A;
- clear the probe reason;
- confirm A remains paused by manual reason;
- remove test gate data through supported APIs.

Do not mutate production queue state.

- [ ] **Step 8: Write verification report**

Create report with:

- exact test commands and exit codes;
- logical run ID;
- masked Profile labels only;
- stage statuses and durations;
- candidate counts per page state;
- draft IDs and scopes;
- Redis publication count before/after;
- strategy gate results;
- cleanup result;
- any remaining failure code;
- explicit statement that production queue was untouched.

Do not include screenshots containing account content unless redacted by the
existing evidence pipeline.

- [ ] **Step 9: Final commit checkpoint if Git exists**

```powershell
git add docs/superpowers/reports/2026-07-30-selector-probe-minimal-repair-verification.md
git commit -m "test: verify selector probe repair"
```

---

## Final success gate

Implementation is complete only when:

- all focused and regression suites pass;
- one visible task maps to one execution;
- Profile/CDP failure detail is actionable and sanitized;
- both test Profiles expose feed and comment-panel candidates;
- selected candidates save as drafts;
- existing two-Profile/two-round publication remains the only publication
  path;
- dependency isolation and manual-pause preservation pass;
- observe mode publishes nothing and pauses nothing;
- cleanup succeeds;
- verification report contains no secret or production data.
