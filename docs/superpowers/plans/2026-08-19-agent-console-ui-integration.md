# Agent Console UI Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce the approved Agent console shell and split TikTok collection operations from business-facing collection results without replacing existing business backends.

**Architecture:** Add a small console Blueprint and shared Jinja base, then reuse existing APIs for overview and collection operations. Add one read-only video projection to `StatisticsQueryService`; the collection-results page consumes that API and keeps display logic in an isolated JavaScript module.

**Tech Stack:** Flask, Jinja2, SQLite, vanilla JavaScript, CSS, pytest, Node test runner.

## Global Constraints

- Preserve the legacy `/` dashboard and all existing business routes.
- Do not modify existing write paths or database migrations.
- Do not use placeholder business data.
- Missing video metrics remain null and render as `—`.
- Default video ordering is `last_collected_at DESC, video_id ASC`.

---

### Task 1: Video results read model

**Files:**
- Modify: `tiktok_stats/queries.py`
- Modify: `tiktok_stats/blueprint.py`
- Test: `tests/test_tiktok_stats_queries.py`
- Test: `tests/test_tiktok_stats_routes.py`

**Interfaces:**
- Produces: `StatisticsQueryService.query_video_table(filters, sort, direction, page, page_size) -> dict`
- Produces: `GET /api/tiktok-stats/videos`

- [ ] Add failing query tests for projection, filters, allow-listed sort, stable pagination, and null metrics.
- [ ] Run the focused query tests and confirm they fail because `query_video_table` is absent.
- [ ] Implement the minimal read-only SQL projection and bounded validation.
- [ ] Add route contract tests and the Blueprint endpoint.
- [ ] Run focused Python tests and confirm they pass.

### Task 2: Console routes and shared shell

**Files:**
- Create: `gateway/routes_console.py`
- Create: `gateway/templates/console_base.html`
- Create: `gateway/templates/console_overview.html`
- Create: `gateway/static/console.css`
- Create: `gateway/static/console_overview.js`
- Modify: `gateway/app.py`
- Modify: `gateway/templates/_dashboard_sidebar.html`
- Test: `tests/test_console_pages.py`

**Interfaces:**
- Produces: `/console/overview`, `/console/collection`, `/console/collection-results`
- Produces: redirect adapters for the remaining approved module URLs.

- [ ] Add failing route and navigation tests.
- [ ] Register a console Blueprint without changing existing Blueprint factories.
- [ ] Build the shared shell and grouped sidebar.
- [ ] Build the overview using `/api/status`, `/api/publish/stats`, `/api/tiktok-stats/status`, and `/api/browser-v2/history` with independent failure handling.
- [ ] Run route tests and confirm legacy entry points still return their existing responses.

### Task 3: Data collection operations page

**Files:**
- Create: `gateway/templates/console_collection.html`
- Create: `gateway/static/console_collection.js`
- Test: `tests-js/console-collection.test.js`

**Interfaces:**
- Consumes: `/api/tiktok-stats/status`, `/accounts`, `/runs`
- Produces: manual debug run requests through `POST /api/tiktok-stats/runs`

- [ ] Add controller tests proving the page does not request result metrics.
- [ ] Implement status, source, and recent-run loading.
- [ ] Implement incremental/full manual dispatch with CSRF-aware `managementFetch`.
- [ ] Render empty and error states without synthetic rows.
- [ ] Run the focused Node test.

### Task 4: Collection results page

**Files:**
- Create: `gateway/templates/console_collection_results.html`
- Create: `gateway/static/console_collection_results.js`
- Test: `tests-js/console-collection-results.test.js`

**Interfaces:**
- Consumes: `GET /api/tiktok-stats/videos` and `GET /api/tiktok-stats/accounts`
- Produces: URL-backed filters and pagination for the approved field layout.

- [ ] Add formatter, query, and stale-response tests.
- [ ] Implement ordered filters: search, publish date, account, reset.
- [ ] Implement summary cards and the approved single-header data table.
- [ ] Format numbers with thousands separators and nulls as `—`.
- [ ] Run focused Node and Python route tests.

### Task 5: Regression and visual verification

**Files:**
- Modify only files created by Tasks 1-4 if failures require fixes.

- [ ] Run focused Python tests for TikTok stats and console routes.
- [ ] Run focused Node tests for dashboard navigation, TikTok stats, and new console modules.
- [ ] Start the Flask app and inspect `/console/overview`, `/console/collection`, and `/console/collection-results` at desktop and narrow widths.
- [ ] Run a Sol read-only architecture and code review; fix blocking findings and review again.

