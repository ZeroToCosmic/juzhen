# Console Operational Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace four Console compatibility launchers with complete operational pages that reuse existing Agent APIs.

**Architecture:** Keep each page in a focused Jinja template and vanilla-JavaScript controller. Reuse current read/write endpoints and specialist editors, with no new persistence layer; receipts are normalized client-side from three existing sources.

**Tech Stack:** Flask, Jinja2, vanilla JavaScript, CSS, pytest, Node test runner.

## Global Constraints

- Preserve legacy pages and API contracts.
- Use revision-checked existing mutations and CSRF-aware fetch.
- Do not invent Central synchronization state or endpoints.
- Do not expose secrets or raw evidence filesystem paths.
- Do not use a right-side detail drawer.

---

### Task 1: Publishing operations

**Files:**
- Create: `gateway/templates/console_publishing.html`
- Create: `gateway/static/console_publishing.js`
- Modify: `gateway/routes_console.py`
- Modify: `gateway/static/console.css`
- Test: `tests-js/console-publishing.test.js`

**Interfaces:**
- Consumes: `/api/accounts`, `/api/content/videos`, `/api/content/brands`, `/api/content/brands/<id>/copy`, `/api/publish/results`, `/api/publish/queue/batches`, `/api/publish/schedule/daily`.
- Produces: batch and manual-debug publish requests through the existing queue endpoints.

- [ ] Add formatter, result-filter, and batch-payload tests.
- [ ] Build the dense publishing overview and real result table.
- [ ] Add batch creation and manual-debug dialogs using existing endpoints.
- [ ] Add content readiness, batch-run, and daily-schedule sections.
- [ ] Run focused Node and Flask route tests.

### Task 2: Unified action library

**Files:**
- Create: `gateway/templates/console_actions.html`
- Create: `gateway/static/console_actions.js`
- Modify: `gateway/routes_console.py`
- Test: `tests-js/console-actions.test.js`

**Interfaces:**
- Consumes: `/api/browser-v2/strategies` and `/api/browser-v2/comment-campaigns`.
- Produces: normalized action rows and revision-checked strategy enable/disable requests.

- [ ] Add action normalization and filtering tests.
- [ ] Render both action types as equal-level records.
- [ ] Add local lifecycle controls where an existing endpoint supports them.
- [ ] Route create/edit actions to the authoritative specialist editor.
- [ ] Run focused Node and Flask route tests.

### Task 3: Accounts and windows workspace

**Files:**
- Create: `gateway/templates/console_accounts_windows.html`
- Create: `gateway/static/console_accounts_windows.js`
- Modify: `gateway/routes_console.py`
- Test: `tests-js/console-accounts-windows.test.js`

**Interfaces:**
- Consumes: `/api/accounts`, `/api/proxy-pool/status`, `/api/browser/adspower-windows`.
- Produces: existing account save/discovery/proxy writes and AdsPower open-and-tile requests.

- [ ] Add account/window normalization and selection tests.
- [ ] Build roster and window sections with real summary counts.
- [ ] Add centered account and proxy dialogs.
- [ ] Add selected/all sync and selected-window open-and-tile actions.
- [ ] Run focused Node and Flask route tests.

### Task 4: Unified receipts and evidence

**Files:**
- Create: `gateway/templates/console_receipts.html`
- Create: `gateway/static/console_receipts.js`
- Modify: `gateway/routes_console.py`
- Test: `tests-js/console-receipts.test.js`

**Interfaces:**
- Consumes: `/api/browser-v2/history`, `/api/browser-v2/comment-campaigns`, `/api/publish/results`, and Campaign detail/receipt/attempt endpoints.
- Produces: a normalized local record list and full-width record detail workspace.

- [ ] Add record normalization, filtering, and evidence-link tests.
- [ ] Load all three sources independently and preserve partial availability.
- [ ] Render the unified table and full-width detail workspace.
- [ ] Fetch Campaign receipts and attempts only on detail selection.
- [ ] Run focused Node and Flask route tests.

### Task 5: Regression, visual verification, and Sol review

**Files:**
- Modify only files created in Tasks 1-4 when verification finds a defect.

- [ ] Run focused Python and Node suites.
- [ ] Inspect all four pages at desktop and narrow widths.
- [ ] Exercise one safe representative interaction per page.
- [ ] Request a read-only Sol architecture and code review.
- [ ] Fix blocking findings and request re-review.

