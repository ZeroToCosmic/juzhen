# TikTok Statistics and Scroll Count Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent TikTok account statistics system backed by a locally deployed, pinned `Evil0ctal/Douyin_TikTok_Download_API` service, and make strategy scrolling use a clear random minimum/maximum wheel-event count.

**Architecture:** Keep the third-party Python 3.11 scraper in its own loopback-only Docker service. Add a focused `tiktok_stats` package for encrypted settings, SQLite storage, collection, scheduling, aggregation, queries, and a Flask blueprint. Serve a new table-first statistics page with account detail and trend views. Preserve the existing browser strategy schema and runtime, changing only the scroll form labels/visibility and regression coverage.

**Tech Stack:** Python 3.13, Flask 3, SQLite, `requests`, Windows DPAPI through `pywin32`, vanilla JavaScript, pytest, Node test runner, Docker Desktop, third-party Python 3.11 FastAPI service.

---

## Global Constraints

- Follow test-driven development: add a failing focused test, run it and observe the expected failure, implement the minimum behavior, then rerun it.
- Never use a real TikTok request or Cookie in automated tests; use fixed HTTP fixtures and a fake client.
- Never return, log, store in SQLite, or commit a plaintext Cookie.
- Bind the third-party service to `127.0.0.1` only and pin it to an immutable commit SHA plus archive SHA-256.
- Store UTC timestamps; derive business dates in `Asia/Shanghai`.
- Three-hour snapshots are retained for 90 days; daily summaries are not deleted.
- Only complete full-account calibrations may replace a daily final snapshot or daily aggregate.
- A page GET or browser refresh is read-only and must never enqueue collection or mutate statistics.
- Existing `/api/publish/stats` behavior remains available for compatibility, but the new statistics UI does not use it.
- Scroll `total_count` remains the canonical `[minimum, maximum]` schema. Old `burst_count` values remain intact when editing old strategies; new scroll actions default to `[1, 1]` and the normal form hides that field.
- Run Python tests with `python -m pytest ... -p no:cacheprovider`; the workspace contains an inaccessible `work/pytest-tmp` directory.
- Run frontend tests with `node --test <test-file>` or `npm run test:node`.
- Git metadata is currently absent. At every commit checkpoint, commit only when `git rev-parse --is-inside-work-tree` succeeds; otherwise print `SKIP: no Git repository` and continue.

## Confirmed Product Rules

- Track 50–500 accounts.
- Support pasted/imported usernames and selection from the existing account database.
- Collect profile and recent posts every three hours.
- Perform a full all-post calibration once per business day.
- Daily delta equals the current day's last complete snapshot minus the previous complete business day's last snapshot; negative values are valid.
- Default page is a sortable table; account details and date-by-account trends are secondary views.
- Sort by post, like, view, or comment delta in either direction; support single-date/range filters, search, status filters, and pagination.
- The Cookie is manually pasted in statistics settings, saved encrypted, displayed only as status, and explicitly validated.

## File Map

- Create `services/tiktok_api/docker-compose.yml`: loopback-only third-party container definition.
- Create `services/tiktok_api/VERSION.json`: pinned source commit, archive digest, repository URL, license, and install time.
- Create `scripts/install_tiktok_api.ps1`: preflight, pinned archive download/extract, license preservation, and image build.
- Create `scripts/start_tiktok_api.ps1`: validate installation and start/health-check the service.
- Modify `.gitignore`: exclude extracted third-party runtime source and plaintext runtime configuration while retaining version metadata.
- Create `tiktok_stats/db.py`: schema versioning, connections, transactions, WAL, and migrations.
- Create `tiktok_stats/store.py`: tracked accounts, runs, snapshots, current posts, daily metrics, leases, and retention.
- Create `tiktok_stats/secrets.py`: DPAPI Cookie encryption/decryption and masked status metadata.
- Create `tiktok_stats/client.py`: typed adapter for the three third-party endpoints and response validation.
- Create `tiktok_stats/imports.py`: username normalization, text import, and existing-account projection.
- Create `tiktok_stats/collector.py`: incremental/full collection, retries, transactional writes, and daily aggregation.
- Create `tiktok_stats/scheduler.py`: Asia/Shanghai due-slot calculation, leases, jitter, and cleanup.
- Create `tiktok_stats/queries.py`: table, summary, detail, post, and trend queries.
- Create `tiktok_stats/blueprint.py`: statistics page and `/api/tiktok-stats/*` routes.
- Create `tiktok_stats/worker.py`: separate scheduler/collector process and one-shot commands.
- Create `gateway/templates/tiktok_stats.html`: statistics shell, import dialog, and settings dialog.
- Create `gateway/static/tiktok_stats.js`: table-first controller plus detail/trend navigation.
- Create `gateway/static/tiktok_stats.css`: page layout and state styles.
- Modify `gateway/app.py`: register the statistics blueprint and point the statistics navigation entry to it.
- Modify `launcher.py`: supervise the statistics worker as a separate local process without coupling it to page requests.
- Modify `gateway/static/browser_strategy_ui.js`: scroll labels, hidden legacy field handling, and save parsing.
- Create focused `tests/test_tiktok_stats_*.py` modules and `tests-js/tiktok-stats-ui.test.js`.
- Modify `tests-js/browser-strategy-ui.test.js` and existing strategy runtime tests for scroll regression coverage.
- Create `docs/tiktok-stats.md`: installation, Cookie setup, operations, backup, troubleshooting, and upgrade procedure.

---

### Task 1: Third-Party Service Pinning and Docker Preflight

**Files:**
- Create: `tests/test_tiktok_api_install_assets.py`
- Create: `services/tiktok_api/docker-compose.yml`
- Create: `scripts/install_tiktok_api.ps1`
- Create: `scripts/start_tiktok_api.ps1`
- Modify: `.gitignore`

**Interfaces:**
- Installer arguments: `-CommitSha`, optional `-ArchiveSha256`, optional `-Force`.
- Runtime directory: `services/tiktok_api/vendor/Douyin_TikTok_Download_API`.
- Health base URL: `http://127.0.0.1:53281`.

- [ ] **Step 1: Write failing asset-contract tests**

Test that the compose file binds only `127.0.0.1`, does not use `latest`, mounts a generated runtime Cookie config outside version control, and that the installer requires a 40-character commit SHA.

Run: `python -m pytest tests/test_tiktok_api_install_assets.py -p no:cacheprovider -q`

Expected: FAIL because the service assets do not exist.

- [ ] **Step 2: Add minimal secure install and start assets**

The installer must:

1. Check Docker client and engine separately.
2. Resolve/download only the requested GitHub commit archive.
3. Verify an optional expected SHA-256 and always record the actual digest.
4. Extract to the ignored vendor directory without adding it to the main Python import path.
5. Preserve `LICENSE` and write `VERSION.json` atomically.
6. Build the local image with a deterministic tag derived from the commit SHA.
7. Stop with actionable messages when Docker Engine or GitHub access is unavailable.

The start script must refuse to run when `VERSION.json` and vendor source disagree, then start the compose service and poll a bounded health check.

- [ ] **Step 3: Run focused tests and script syntax checks**

Run: `python -m pytest tests/test_tiktok_api_install_assets.py -p no:cacheprovider -q`

Expected: PASS.

Run: `powershell -NoProfile -Command "[void][scriptblock]::Create((Get-Content -Raw scripts/install_tiktok_api.ps1)); [void][scriptblock]::Create((Get-Content -Raw scripts/start_tiktok_api.ps1))"`

Expected: exit 0.

- [ ] **Step 4: Perform non-destructive environment preflight**

Run Docker client/engine checks and test GitHub archive reachability. Do not paste a Cookie or start collection yet. Record exact blockers in `docs/tiktok-stats.md` if the environment is not ready.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: add pinned TikTok API service assets`

---

### Task 2: Statistics Database Schema and Transaction Layer

**Files:**
- Create: `tiktok_stats/__init__.py`
- Create: `tiktok_stats/db.py`
- Create: `tiktok_stats/store.py`
- Create: `tests/test_tiktok_stats_db.py`

**Interfaces:**
- `connect_stats_db(path) -> sqlite3.Connection`
- `migrate_stats_db(path) -> int`
- `StatsStore(path)` with transaction-scoped writes.
- Tables: `schema_migrations`, `tracked_accounts`, `collection_runs`, `account_snapshots`, `posts_current`, `daily_account_metrics`, `worker_leases`.

- [ ] **Step 1: Write failing schema tests**

Cover foreign keys, WAL, unique account normalization key, snapshot uniqueness, daily metric uniqueness, default statuses, indexes used by date/sort queries, and migration idempotence.

Run: `python -m pytest tests/test_tiktok_stats_db.py -p no:cacheprovider -q`

Expected: FAIL on missing package/schema.

- [ ] **Step 2: Implement versioned migrations and connection policy**

Use explicit SQL migrations, `PRAGMA foreign_keys=ON`, `journal_mode=WAL`, busy timeout, UTC ISO timestamps, and context-managed transactions. Never create schema at module import time.

- [ ] **Step 3: Implement atomic store primitives**

Add methods for account upsert/enable/disable, run lifecycle, snapshot insertion, staged full-post replacement, daily-metric upsert, lease acquire/renew/release, and snapshot cleanup. Full calibration writes must commit as one account-scoped transaction.

- [ ] **Step 4: Test rollback and restart persistence**

Add a forced mid-transaction exception and prove no partial snapshot/daily metric survives. Close and reopen the store and prove all committed resources remain.

Run: `python -m pytest tests/test_tiktok_stats_db.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: add persistent TikTok statistics store`

---

### Task 3: Encrypted Cookie Settings

**Files:**
- Create: `tiktok_stats/secrets.py`
- Create: `tests/test_tiktok_stats_secrets.py`
- Modify: `.gitignore`

**Interfaces:**
- `CookieSecretStore(path, protector=None)`
- `save_cookie(plaintext: str) -> CookieStatus`
- `load_cookie() -> str`
- `public_status() -> dict`
- `mark_validation(valid: bool, message: str, checked_at: datetime) -> None`

- [ ] **Step 1: Write failing security tests**

Cover encrypted round-trip through a fake protector, missing/corrupt secret handling, never returning plaintext from status, atomic replacement, file permission hardening attempt, and preserving the prior valid secret when a replacement write fails.

Run: `python -m pytest tests/test_tiktok_stats_secrets.py -p no:cacheprovider -q`

Expected: FAIL because the secret store is absent.

- [ ] **Step 2: Implement Windows DPAPI protection**

Use current-user DPAPI scope through `win32crypt`. Persist only encrypted bytes plus non-secret validation metadata. Fail closed on unsupported platforms unless tests inject a protector. Do not fall back to reversible base64 or plaintext.

- [ ] **Step 3: Add recursive redaction helper**

Redact keys and header names matching Cookie, Authorization, token, proxy credential, and session fields before logs or error samples are written.

- [ ] **Step 4: Run security tests**

Run: `python -m pytest tests/test_tiktok_stats_secrets.py -p no:cacheprovider -q`

Expected: PASS and fixture files contain no test plaintext.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: encrypt TikTok collection credentials`

---

### Task 4: Third-Party API Adapter and Contract Validation

**Files:**
- Create: `tiktok_stats/client.py`
- Create: `tests/fixtures/tiktok/profile.json`
- Create: `tests/fixtures/tiktok/sec_uid.json`
- Create: `tests/fixtures/tiktok/posts_page_1.json`
- Create: `tests/fixtures/tiktok/posts_page_2.json`
- Create: `tests/test_tiktok_stats_client.py`

**Interfaces:**
- `TikTokApiClient(base_url, cookie_provider, session=None, timeout=...)`
- `resolve_sec_uid(username) -> str`
- `fetch_profile(sec_uid) -> ProfileSnapshot`
- `iter_posts(sec_uid, *, cursor=None) -> Iterator[PostPage]`
- Stable exceptions: `CookieInvalid`, `AccountNotFound`, `AccountPrivate`, `UpstreamUnavailable`, `ContractChanged`.

- [ ] **Step 1: Write failing fixture-driven adapter tests**

Cover query parameter mapping, profile counters, post counters, cursor termination, timeout/5xx retries delegated to collector, invalid Cookie, private/not-found accounts, and a changed response shape.

Run: `python -m pytest tests/test_tiktok_stats_client.py -p no:cacheprovider -q`

Expected: FAIL because the adapter is absent.

- [ ] **Step 2: Implement the narrow adapter**

Call only:

- `/api/tiktok/web/get_sec_user_id`
- `/api/tiktok/web/fetch_user_profile`
- `/api/tiktok/web/fetch_user_post`

Normalize third-party payloads into internal dataclasses/dicts at this boundary. Do not let raw upstream shapes spread into storage, APIs, or UI.

- [ ] **Step 3: Add bounded error summaries and redaction**

Store status code, endpoint name, response shape keys, and a redacted/truncated message. Never retain request headers or the raw Cookie.

- [ ] **Step 4: Run adapter tests**

Run: `python -m pytest tests/test_tiktok_stats_client.py -p no:cacheprovider -q`

Expected: PASS without network access.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: add TikTok scraper service adapter`

---

### Task 5: Account Import and Tracking Management

**Files:**
- Create: `tiktok_stats/imports.py`
- Create: `tests/test_tiktok_stats_imports.py`
- Modify: `tiktok_stats/store.py`

**Interfaces:**
- `normalize_tiktok_username(value) -> tuple[display_name, username_key]`
- `parse_username_text(text) -> list[NormalizedUsername]`
- `existing_account_candidates(accounts_db_path, query=None) -> list[dict]`
- `import_tracked_accounts(store, values, source, source_ids=None) -> ImportResult`

- [ ] **Step 1: Write failing normalization/import tests**

Cover `username`, `@username`, profile URLs, whitespace, duplicate case variants, invalid hosts/paths, mixed newline/CSV/TSV text, partial invalid input, existing account selection, and disabled-account reactivation.

Run: `python -m pytest tests/test_tiktok_stats_imports.py -p no:cacheprovider -q`

Expected: FAIL.

- [ ] **Step 2: Implement strict normalization and idempotent writes**

Return per-line results (`added`, `existing`, `reactivated`, `invalid`) without rolling back valid usernames. Do not resolve `secUid` synchronously in the HTTP request.

- [ ] **Step 3: Implement read-only projection from `accounts.db`**

Inspect the current account schema through existing account-store helpers where possible. Do not duplicate or mutate the existing account database. Persist only the chosen source account ID and normalized TikTok username in the statistics database.

- [ ] **Step 4: Run import tests**

Run: `python -m pytest tests/test_tiktok_stats_imports.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: add TikTok tracking account imports`

---

### Task 6: Incremental and Full Collection Pipelines

**Files:**
- Create: `tiktok_stats/collector.py`
- Create: `tests/test_tiktok_stats_collector.py`
- Modify: `tiktok_stats/store.py`

**Interfaces:**
- `Collector(store, client, clock, sleeper, rng)`
- `collect_incremental(account_id, run_id) -> AccountCollectionResult`
- `collect_full(account_id, run_id, business_date) -> AccountCollectionResult`
- `run_collection(run_type, account_ids=None) -> RunResult`

- [ ] **Step 1: Write failing incremental tests**

Prove `secUid` resolution/caching, profile + recent-post fetch, stop-on-known-post pagination, `profile_recent` coverage, no daily aggregate creation, bounded retry with jitter, and per-account failure isolation.

- [ ] **Step 2: Implement incremental collection**

Use a small configurable worker pool but serialize requests within one account. Write the snapshot and recent post updates transactionally. A partial recent-post response must not be labeled full coverage.

- [ ] **Step 3: Write failing full-calibration and daily-delta tests**

Cover all-page traversal, deletion detection only after complete traversal, first-day baseline, normal positive delta, negative delta, missing previous day, current-day replacement by a later complete snapshot, and interrupted traversal retaining prior complete data.

- [ ] **Step 4: Implement full calibration and daily aggregation**

Stage all posts/counters in memory or temporary tables, then atomically update `posts_current`, insert a `full` snapshot, and upsert `daily_account_metrics`. Compute deltas only against the previous complete business date; preserve `NULL` plus `baseline_status` when comparison is unavailable.

- [ ] **Step 5: Test global Cookie circuit breaker**

When one request proves the Cookie invalid, stop creating new upstream requests, mark the run partial/failed consistently, keep history queryable, and update public Cookie status.

Run: `python -m pytest tests/test_tiktok_stats_collector.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 6: Commit checkpoint**

Commit message: `feat: collect and aggregate TikTok account metrics`

---

### Task 7: Scheduler, Worker Lease, Retention, and Launcher Integration

**Files:**
- Create: `tiktok_stats/scheduler.py`
- Create: `tiktok_stats/worker.py`
- Create: `tests/test_tiktok_stats_scheduler.py`
- Modify: `launcher.py`

**Interfaces:**
- `due_incremental_slots(now_utc, last_slot, timezone) -> list[datetime]`
- `full_calibration_due(account_id, business_date, store) -> bool`
- `run_worker_once(...) -> WorkerTickResult`
- CLI: `python -m tiktok_stats.worker [serve|tick|incremental|full|cleanup|validate-cookie]`.

- [ ] **Step 1: Write failing time-boundary tests**

Cover eight three-hour slots per Shanghai business day, midnight boundaries, process restart catch-up without duplicate runs, daily full-calibration idempotence, randomized account jitter, and a test clock without real sleeping.

- [ ] **Step 2: Implement durable scheduling and lease ownership**

Use database run rows and `worker_leases` as the source of truth. Only one worker may own a scheduled slot. Renew leases during long full calibrations and allow takeover only after expiry.

- [ ] **Step 3: Implement 90-day cleanup**

Delete only expired `account_snapshots` that are not required by a retained daily record. Never delete `daily_account_metrics`, current posts, tracked accounts, or run audit rows.

- [ ] **Step 4: Supervise the separate worker from the launcher**

Start at most one worker process, report its state, and terminate it cleanly when the application launcher closes. Flask page requests must not own or restart the worker.

- [ ] **Step 5: Run scheduler/launcher tests**

Run: `python -m pytest tests/test_tiktok_stats_scheduler.py tests/test_console.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 6: Commit checkpoint**

Commit message: `feat: schedule durable TikTok collection jobs`

---

### Task 8: Read-Only Statistics Query Service

**Files:**
- Create: `tiktok_stats/queries.py`
- Create: `tests/test_tiktok_stats_queries.py`

**Interfaces:**
- `query_summary(filters) -> dict`
- `query_account_table(filters, sort, direction, page, page_size) -> dict`
- `query_account_detail(account_id, start_date, end_date) -> dict`
- `query_trend_matrix(metric, start_date, end_date, account_query, page, page_size) -> dict`
- Allowed sorts: `posts_delta`, `likes_delta`, `views_delta`, `comments_delta`.

- [ ] **Step 1: Write failing table query tests**

Seed positive, negative, zero, and missing values. Cover single date, range semantics, all four sorts in both directions, stable account-ID tie-break, missing values after valid values, status/search filters, pagination, and summary totals.

- [ ] **Step 2: Implement parameterized, indexed queries**

Whitelist sort columns/directions; never interpolate request-provided SQL. Range mode compares the end day's complete total to the complete day before the requested start and also returns daily series for detail/trend views.

- [ ] **Step 3: Write and implement detail/trend query tests**

Cover current totals, daily series, post list, growth ranking, suspected deletion flags, error history, metric switching, and date-by-account cells.

- [ ] **Step 4: Prove GET/query purity**

Snapshot database bytes or row counts before and after all query calls and assert that no run, snapshot, or daily row changes.

Run: `python -m pytest tests/test_tiktok_stats_queries.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: query daily TikTok account statistics`

---

### Task 9: Flask Blueprint and API Contracts

**Files:**
- Create: `tiktok_stats/blueprint.py`
- Create: `tests/test_tiktok_stats_routes.py`
- Modify: `gateway/app.py`

**Routes:**
- `GET /tiktok-stats`
- `GET|POST /api/tiktok-stats/accounts`
- `POST /api/tiktok-stats/accounts/from-existing`
- `PATCH /api/tiktok-stats/accounts/<id>`
- `GET|PUT /api/tiktok-stats/settings/cookie`
- `POST /api/tiktok-stats/settings/cookie/validate`
- `GET /api/tiktok-stats/status`
- `POST /api/tiktok-stats/runs`
- `GET /api/tiktok-stats/runs`
- `GET /api/tiktok-stats/summary`
- `GET /api/tiktok-stats/table`
- `GET /api/tiktok-stats/accounts/<id>/detail`
- `GET /api/tiktok-stats/trends`

- [ ] **Step 1: Write failing route contract tests**

Inject temporary database/secret paths and fake collector dispatch. Cover JSON object validation, import responses, enable/disable, Cookie masking, validation state, explicit manual run enqueue, query filters, sort whitelist, pagination limits, and stable errors.

Run: `python -m pytest tests/test_tiktok_stats_routes.py -p no:cacheprovider -q`

Expected: FAIL because the blueprint is absent.

- [ ] **Step 2: Implement blueprint factory with injected dependencies**

Avoid module-global database connections. Register the blueprint from `create_app()` after config paths are set. Keep write routes explicit; no GET route may dispatch work.

- [ ] **Step 3: Add response redaction guard**

As a defense in depth, recursively reject/redact secret keys in `/api/tiktok-stats/*` JSON responses. Assert the literal test Cookie never appears in response bodies, logs, stats DB, or run errors.

- [ ] **Step 4: Preserve legacy publishing statistics compatibility**

Keep `/api/publish/stats` tests passing, while ensuring the new page and endpoints rely only on the statistics database.

Run: `python -m pytest tests/test_tiktok_stats_routes.py tests/test_content_publish.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

Commit message: `feat: expose TikTok statistics APIs`

---

### Task 10: Table-First Statistics Page, Import, and Settings

**Files:**
- Create: `gateway/templates/tiktok_stats.html`
- Create: `gateway/static/tiktok_stats.js`
- Create: `gateway/static/tiktok_stats.css`
- Create: `tests-js/tiktok-stats-ui.test.js`
- Modify: `gateway/app.py`
- Modify: `tests/test_console.py`

- [ ] **Step 1: Write failing frontend controller tests**

Use injected request/render/history dependencies. Cover initial table query, single/range date switching, search debounce, four sort metrics, direction, status/completeness filters, pagination, loading/empty/error states, stale-response suppression, and URL state restoration.

Run: `node --test tests-js/tiktok-stats-ui.test.js`

Expected: FAIL because the controller is absent.

- [ ] **Step 2: Implement accessible table-first UI**

Render summary cards, filters, sort controls, paginated account table, explicit incomplete/missing states, and red negative deltas. Keep DOM text escaped; never inject API values into HTML strings without escaping.

- [ ] **Step 3: Implement account import dialog**

Support pasted TXT/CSV/TSV-like content and an existing-account candidate selector. Show per-item added/existing/reactivated/invalid results and refresh the tracked-account table only after the server confirms persistence.

- [ ] **Step 4: Implement settings and operations dialog**

Allow one-way Cookie paste/save, masked status, last validation time, validate button, service/worker status, immediate incremental collection, and explicit daily full calibration. Never repopulate the Cookie input.

- [ ] **Step 5: Replace navigation target without deleting compatibility APIs**

Change the main “数据统计” entry to open `/tiktok-stats`. Remove the obsolete inline publish-statistics panel wiring from the visible UI only after new route tests pass.

- [ ] **Step 6: Run frontend and server-render tests**

Run: `node --test tests-js/tiktok-stats-ui.test.js`

Run: `python -m pytest tests/test_console.py tests/test_tiktok_stats_routes.py -p no:cacheprovider -q`

Expected: PASS.

- [ ] **Step 7: Commit checkpoint**

Commit message: `feat: add table-first TikTok statistics page`

---

### Task 11: Account Detail and Trend Views

**Files:**
- Modify: `gateway/templates/tiktok_stats.html`
- Modify: `gateway/static/tiktok_stats.js`
- Modify: `gateway/static/tiktok_stats.css`
- Modify: `tests-js/tiktok-stats-ui.test.js`

- [ ] **Step 1: Write failing navigation/detail tests**

Cover opening an account from the table, restoring date filters, current totals, daily series, post sorting, deleted-post warnings, collection errors, back navigation, and direct URL loading.

- [ ] **Step 2: Implement account detail view**

Use the same daily metric semantics as the table. Render a lightweight accessible SVG or CSS chart without introducing a large chart dependency unless existing project code already provides one.

- [ ] **Step 3: Write failing trend matrix tests**

Cover metric switching among posts/likes/views/comments, date range, account search, missing cells, negative cells, pagination, and clicking a cell into the corresponding account/date detail.

- [ ] **Step 4: Implement trend matrix view**

Keep table/detail/trend filters in shared controller state and browser URL parameters. Avoid fetching all 500 accounts for every page; use server pagination.

- [ ] **Step 5: Run UI tests**

Run: `node --test tests-js/tiktok-stats-ui.test.js`

Expected: PASS.

- [ ] **Step 6: Commit checkpoint**

Commit message: `feat: add TikTok detail and trend views`

---

### Task 12: Scroll Wheel Count Form and Runtime Regression

**Files:**
- Modify: `gateway/static/browser_strategy_ui.js`
- Modify: `tests-js/browser-strategy-ui.test.js`
- Modify: `tests/test_browser_strategy_config.py`
- Modify: `tests/test_browser_strategy_runtime.py`

- [ ] **Step 1: Write failing frontend scroll-form tests**

Assert visible labels are exactly:

- `单次滚动距离`
- `最小滚轮次数`
- `最大滚轮次数`
- `最小间隔秒数`
- `最大间隔秒数`

Assert the normal form does not expose `burst_count`, new actions save it as `[1, 1]`, and editing/saving an old action preserves its pre-existing hidden value.

Run: `node --test tests-js/browser-strategy-ui.test.js`

Expected: FAIL on current “总次数/每组次数” fields.

- [ ] **Step 2: Implement the minimum UI change**

Update parameter metadata, dialog rendering, and parsing so the visible min/max count serializes to `total_count`. Carry hidden legacy `burst_count` from the draft object instead of recreating or overwriting it.

- [ ] **Step 3: Add backend validation regression tests**

Cover positive integer minimum/maximum, `min <= max`, missing/extra keys, refresh/restart persistence, upward/downward direction signs, and legacy `burst_count` retention.

- [ ] **Step 4: Prove exact runtime wheel-event count**

Inject a fixed RNG so sampled `N` is known. Assert `page.mouse.wheel` is called exactly `N` times and each call uses the configured absolute distance with the correct direction. Clarify in test names that this is a synthetic browser wheel-event count, not a physical hardware notch count.

Run: `python -m pytest tests/test_browser_strategy_config.py tests/test_browser_strategy_runtime.py -p no:cacheprovider -q`

Run: `node --test tests-js/browser-strategy-ui.test.js`

Expected: PASS.

- [ ] **Step 5: Commit checkpoint**

Commit message: `fix: configure random scroll wheel counts`

---

### Task 13: Documentation and Automated Regression Suite

**Files:**
- Create: `docs/tiktok-stats.md`
- Modify: `config.example.json` only if non-secret statistics paths/base URL belong in normal config.

- [ ] **Step 1: Write operations documentation**

Document:

- Docker Desktop/engine access and TikTok-reachable network prerequisites.
- Pinned third-party installation and license attribution.
- Cookie acquisition responsibility, paste/validation, expiry, and rotation.
- Starting/stopping the scraper and statistics worker.
- Three-hour and daily collection semantics.
- 90-day cleanup and permanent daily data.
- Database and encrypted-secret backup/restore caveats (DPAPI is tied to the current Windows user).
- Common failures: Docker permission, GitHub download, TikTok reachability, Cookie invalid, private/not-found accounts, upstream contract change.
- Safe upgrade: explicitly choose a new commit, review upstream changes, update fixtures, verify digest, rebuild, and only then replace `VERSION.json`.

- [ ] **Step 2: Run the complete automated suites**

Run: `python -m pytest tests -p no:cacheprovider -q`

Expected: all Python tests PASS.

Run: `npm run test:node`

Expected: all Node tests PASS.

- [ ] **Step 3: Run static secret scan**

Search tracked source, tests, docs, generated logs, and statistics fixtures for the test Cookie literals and known secret-field serialization. Confirm runtime secret/vendor paths are ignored.

- [ ] **Step 4: Restart persistence smoke test**

With temporary paths, save a tracked account, strategy scroll range, encrypted Cookie fixture, snapshot, and daily metric; recreate the Flask app/store and verify all public non-secret state remains while plaintext secrets remain unavailable.

- [ ] **Step 5: Commit checkpoint**

Commit message: `docs: add TikTok statistics operations guide`

---

### Task 14: Real Local Integration and User Acceptance

**Prerequisites supplied/confirmed by user:**

- Docker Desktop engine is running and accessible to the current Windows user.
- The machine or configured proxy can reach TikTok reliably.
- A valid logged-in TikTok Web Cookie is available for manual paste.

- [ ] **Step 1: Install the pinned third-party source**

Choose and record an immutable upstream commit SHA, run the installer, verify archive SHA-256 and preserved Apache-2.0 license, build the image, and start it on loopback.

- [ ] **Step 2: Validate service and Cookie through the UI**

Paste the Cookie only in statistics settings. Confirm the UI shows validity state and timestamp, never the plaintext. Confirm logs and both databases contain no Cookie literal.

- [ ] **Step 3: Import a small controlled account set**

Test all three accepted username formats plus one existing-account selection. Confirm duplicates do not create extra rows and all records survive browser refresh and Flask restart.

- [ ] **Step 4: Run one incremental and one full calibration**

Confirm profile/recent-post data arrives for incremental collection, only the full run creates/replaces the daily aggregate, failures remain per-account, and page refresh does not enqueue another run.

- [ ] **Step 5: Validate A/B/C pages**

Check table sorting and date filters, one account detail, and one trend matrix. Use seeded data or a second business-day run to confirm positive, negative, and missing daily values are distinguishable.

- [ ] **Step 6: Validate scroll action in an AdsPower execution**

Save a visible min/max wheel count, refresh and restart, run the strategy with diagnostic event counting, and confirm the sampled number of browser wheel events falls within the saved range with the correct direction.

- [ ] **Step 7: Final evidence report**

Create `docs/superpowers/reports/2026-07-22-tiktok-stats-and-scroll-verification.md` listing commands, results, screenshots where useful, environment blockers if any, source commit/digest, and outstanding operational risks.

- [ ] **Step 8: Final commit checkpoint**

Commit message: `test: verify TikTok statistics integration`

---

## Completion Gate

Do not claim completion until all of the following are true:

- All focused and full Python/Node tests pass from fresh processes.
- Saved accounts, statistics, settings metadata, and scroll ranges survive page refresh and process restart.
- No plaintext Cookie appears in responses, logs, SQLite, source, docs, or version control.
- The pinned third-party service runs on loopback and returns contract-valid data with a user-provided Cookie.
- A complete full calibration produces the expected permanent daily row; an incomplete run cannot replace it.
- Table, account detail, and trend pages use the same daily calculation semantics.
- The scroll runtime emits exactly the sampled number of synthetic wheel events.
- Any remaining external blocker is explicitly reported rather than represented as a passing result.
