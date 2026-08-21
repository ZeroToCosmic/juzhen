# Browser Execution V2 API and UI Phase 4 Plan

> Agent split: Sol owns boundaries/review; Terra implements bounded tasks with tests.

**Goal:** Expose V2 through `/api/browser-v2`, provide direct local management UI, and prepare real 6+2 Profile acceptance without calling V1 APIs.

**Architecture:** `ExecutionV2Service` owns one background asyncio loop so Playwright objects never cross Flask request loops. `execution_v2.blueprint` maps HTTP to service/store and returns sanitized JSON. Dedicated template/JS/CSS implement five views. Gateway integration is limited to configuration and blueprint registration; V1 remains untouched until V2 acceptance.

## Global constraints

- API prefix exactly `/api/browser-v2`.
- SQLite path defaults to `data/execution_v2/execution_v2.db`.
- No Redis, LLM, probe, self-healing, gate, V1 strategy state, or old `/api/browser/*` calls.
- API never returns API keys, cookies, passwords, raw WebSocket endpoints, raw Profile IDs in artifacts, or exception dumps.
- Jobs and picker browser work run on one owned asyncio loop thread.
- Frontend polls once per second; no WebSocket.
- Local direct access accepts loopback clients and Host `localhost`, `127.0.0.1`, `[::1]` with current port; rejects non-loopback/foreign Host.
- Preserve dirty files; `gateway/app.py` receives only integration edits after independent modules pass.

### Task 1: Runtime service and adapters

**Files:** create `execution_v2/runtime.py`, `execution_v2/service.py`; modify `execution_v2/adspower_adapter.py` only if Profile listing needs a production method; add `tests/test_execution_v2_service.py`.

Interfaces:

```python
runtime.submit(coroutine) -> concurrent.futures.Future
service.list_profiles() -> list[masked summary]
service.start_picker(profile_id, target_url) -> session_id
service.get_picker(session_id) -> status
service.save_picker_selection(session_id, name, purpose, kind) -> element
service.finish_picker(session_id) / cancel_picker(session_id)
service.start_job(strategy_id, profile_ids, batch_size=3) -> job_id
service.cancel_job(job_id)
service.get_job(job_id) / get_results(job_id)
service.close()
```

Tests: one loop/thread; no Page crosses loops; picker stays open across selections; finish/cancel closes; background job uses immutable snapshot; restart reads persisted terminal history; active-on-restart becomes explicit interrupted/cleanup-required state; Fake adapters only, no browser.

Before routing, split scheduler submission so the job row is committed before background execution. Add Store history/action-result reads, executor stage callbacks, failure screenshot artifacts, and restart cleanup without replay.

### Task 2: V2 Blueprint and API contracts

**Files:** create `execution_v2/blueprint.py`; add `tests/test_execution_v2_routes.py`.

Routes:

```text
GET/POST      /api/browser-v2/elements
GET/PUT/DELETE /api/browser-v2/elements/<id>
POST          /api/browser-v2/elements/<id>/validate
GET           /api/browser-v2/profiles
POST          /api/browser-v2/picker/start
GET           /api/browser-v2/picker/<session_id>
POST          /api/browser-v2/picker/<session_id>/finish
POST          /api/browser-v2/picker/<session_id>/cancel
POST          /api/browser-v2/picker/<session_id>/save
GET/POST      /api/browser-v2/strategies
GET/PUT/DELETE /api/browser-v2/strategies/<id>
POST          /api/browser-v2/jobs
GET           /api/browser-v2/jobs/<id>
POST          /api/browser-v2/jobs/<id>/cancel
GET           /api/browser-v2/jobs/<id>/results
GET           /api/browser-v2/history
```

Use closed JSON schemas, HTTP 400 validation, 404 missing, 409 revision/reference conflict, 202 background start/cancel. Test redaction recursively. Blueprint accepts injected service factory.

### Task 3: Five-view management UI

**Files:** create `gateway/templates/browser_v2.html`, `gateway/static/browser_v2.js`, `gateway/static/browser_v2.css`; add `tests-js/browser-v2-ui.test.js`, `tests/test_execution_v2_page.py`.

Views: execution center, element library, strategy library/editor, run history, settings. Strategy editor supports add/copy/delete/reorder/edit for move/scroll/click/input/wait. Execution center selects many Profiles, defaults batch size 3, displays remaining/current batch/success/failure and Chinese Profile stages. Element page starts continuous picker, names/saves multiple picks, rename/repick/disable/delete/validate. History shows stage, action index, error summary, screenshot and close result. No `unknown`, probe, repairs, publish, reconcile, lease, version, gate, or semantic fields.

JS uses only `/api/browser-v2/*`, one-second polling, disabled/loading states, visible validation errors, and no optimistic “saved” state before HTTP success.

### Task 4: Gateway registration and local direct access

**Files:** create `gateway/local_only.py`; minimally modify `gateway/app.py`; modify `gateway/auth_blueprint.py` only where needed; add `tests/test_execution_v2_integration.py`, update focused auth tests.

Register V2 service/blueprint and `/browser-v2` page. `LOCAL_DIRECT_MODE=True` skips account-login blueprint/management DB/session-key dependency and installs loopback+Host guard; launcher enables it. `LOCAL_DIRECT_MODE=False` preserves legacy authentication for old regression tests. `/` opens `/browser-v2` in direct mode. Non-loopback and malformed Host return 403. Server binding remains `127.0.0.1`, never `0.0.0.0`.

### Task 5: Automated acceptance gate

Run all V2 Python tests, V2 Node tests, adjacent AdsPower/actions/ghost-cursor tests, page/auth integration tests, then full relevant suite. Verify 300 Fake Profiles, 100 batches, max active 3, cleanup barrier, persistence, API redaction, UI five views. Real 6+2 Profile execution remains manual final acceptance because Profile IDs/login state belong to user.
