# Selector Settings Durable Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make selector-probe settings updates and protected backup restores crash-consistent across SQLite and the atomically replaced configuration file.

**Architecture:** SQLite owns the monotonic settings revision and a durable settings-publication intent containing only a safe projected candidate and its SHA-256 fingerprint. A request stages the next revision, audit record, and pending idempotency outcome in one SQLite transaction, atomically writes the configuration, then acknowledges the intent; reconciliation compares the current safe projection with pending fingerprints and completes writes that reached disk before an acknowledgement failure.

**Tech Stack:** Python 3, Flask, SQLite, JSON configuration files, pytest.

## Global Constraints

- Never persist submitted secrets or a full private candidate in SQLite.
- Consume the settings revision before the configuration-file mutation.
- Keep configuration writes atomic through the existing settings-store lock and `os.replace`.
- A restart must reconcile a successful file mutation whose SQLite acknowledgement failed.
- The generic restore route must preserve selector-probe, model, and AdsPower sections in the same atomic file replacement.

---

### Task 1: Durable settings intent

**Files:**
- Modify: `selector_probe/store.py`
- Test: `tests/test_selector_probe_store.py`

**Interfaces:**
- Consumes: existing `management_resource_revisions`, `management_idempotency_cache`, and audit table.
- Produces: `stage_settings_publication(...)`, `complete_settings_publication(...)`, `fail_settings_publication(...)`, and `pending_settings_publications()`.

- [ ] **Step 1: Add failing tests**

Assert that staging consumes exactly one revision, writes an audit event and pending intent, leaves idempotency pending, and stores neither a supplied secret sentinel nor any private candidate.

- [ ] **Step 2: Run the store tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_selector_probe_store.py -p no:cacheprovider -W error::ResourceWarning`

Expected: the new methods are missing.

- [ ] **Step 3: Implement the schema and transactional methods**

Add a `management_settings_publications` table keyed by intent ID with actor/idempotency identity, expected/staged revisions, safe candidate JSON, candidate fingerprint, status, error code, and timestamps. Stage with `BEGIN IMMEDIATE`; compare the expected revision, increment it, insert the safe intent and audit event, and retain the matching idempotency row in `pending`.

- [ ] **Step 4: Run the store tests**

Run the command from Step 2.

Expected: all tests pass.

### Task 2: Settings route coordination and reconciliation

**Files:**
- Modify: `selector_probe/blueprint.py`
- Test: `tests/test_selector_probe_management_routes.py`

**Interfaces:**
- Consumes: Task 1 store methods and existing `settings_provider`/`settings_mutator`.
- Produces: stage → atomic mutate → acknowledge flow and safe pending-intent reconciliation.

- [ ] **Step 1: Add failure-injection tests**

Cover configuration mutation failure, acknowledgement failure after mutation, stale preflight revision after either outcome, restart reconciliation, idempotent replay, and absence of a secret sentinel from SQLite bytes.

- [ ] **Step 2: Run the route tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_selector_probe_management_routes.py -p no:cacheprovider -W error::ResourceWarning`

Expected: failure-injection cases fail before implementation.

- [ ] **Step 3: Implement the coordinator**

Stage the safe projection and fingerprint before calling `settings_mutator`; on mutation failure mark the intent/idempotency failed without restoring the consumed revision; on success acknowledge in SQLite. Before settings reads and writes, reconcile pending intents by comparing their safe fingerprint with the current safe projection, completing matches and failing non-matches.

- [ ] **Step 4: Run the route tests**

Run the command from Step 2.

Expected: all tests pass.

### Task 3: Single-write protected restore

**Files:**
- Modify: `gateway/settings_store.py`
- Modify: `gateway/app.py`
- Test: `tests/test_settings_store.py`
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Consumes: valid backup discovery and `_save_settings`.
- Produces: `restore_latest_backup_preserving(section_names, path=None)`.

- [ ] **Step 1: Add a failing atomic-restore test**

Assert the selected backup and protected live sections are merged in memory and installed with one `_save_settings` call, including when selector-probe data contains secrets.

- [ ] **Step 2: Run the settings tests**

Run: `.\.venv\Scripts\python.exe -m pytest -q tests/test_settings_store.py tests/test_settings_routes.py -p no:cacheprovider -W error::ResourceWarning`

Expected: the preserving restore API is missing.

- [ ] **Step 3: Implement the preserving restore**

Under `_SETTINGS_LOCK`, read the newest valid backup, copy protected sections from the current live settings, merge them into the restored object, and call `_save_settings` once. Update `/api/settings/restore-latest` to use this operation instead of restore-then-mutate.

- [ ] **Step 4: Run focused and compile verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m py_compile selector_probe\store.py selector_probe\blueprint.py gateway\settings_store.py gateway\app.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_selector_probe_store.py tests/test_selector_probe_management_routes.py tests/test_settings_store.py tests/test_settings_routes.py -p no:cacheprovider -W error::ResourceWarning
```

Expected: compilation succeeds and all focused tests pass.
