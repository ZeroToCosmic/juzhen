# Console Comment Campaign Create Page Design

## Problem

The “新建评论 Campaign” button in the Action Library currently links directly to the legacy `/comment-campaigns` workbench. The browser-strategy create flow already uses a native Console child page, so the Campaign create entry feels inconsistent and unexpectedly switches UI systems.

## Goal

Provide one native Console page for creating a Comment Campaign and point the Action Library create button to it. Creation continues to use the existing Comment Campaign APIs and returns to the Action Library after success.

## Minimal Scope

### In scope

- Add `/console/actions/comment-campaigns/new`.
- Render the page inside `console_base.html` with “动作库” active.
- Provide the same fields and Profile-selection behavior required by the existing create API.
- Change only the Action Library “新建评论 Campaign” button to the new route.
- Preserve the complete local draft when validation, API, or network errors occur.
- Return to `/console/actions` after a successful create request.

### Out of scope

- Native Console Campaign maintenance or detail pages.
- Changing Campaign-row maintenance links; they continue to open `/comment-campaigns`.
- Migrating planning, allocation maintenance, approval, pause, resume, cancellation, Assignment, attempt, receipt, or evidence interfaces.
- Migrating comment-tree management, Profile metadata management, or comment element settings.
- Changing the Comment Campaign API, service, database, worker, queue, or state machine.
- Removing, redirecting, embedding, or restyling the legacy `/comment-campaigns` workbench.
- Adding scheduling, Central synchronization, or new configuration options.

## Page Layout

The page is a full-width operational form, not a drawer, wizard, dashboard, or conceptual process view.

### Header

- “← 返回动作库” link.
- “新建评论 Campaign” title.
- Primary “创建 Campaign” button.

### Basic information

- Campaign name.
- Direct HTTPS TikTok video URL.

### Comment configuration

- Mode: independent comments or threaded replies.
- Enabled comment tree/template.
- Batch size from 1 through 8, default 3.

### Profile allocation

- Automatic or manual selection.
- Automatic selection displays required, eligible, selected, and shortage counts inline.
- Manual selection uses a searchable compact Profile table.
- The form prevents submission until enough unique Profiles are selected for the chosen comment tree.

The page does not contain Campaign statistics, a Campaign list, approvals, execution state, receipts, or explanatory process cards.

## Data Flow

On initialization, load only:

- `GET /api/browser-v2/comment-templates`
- `GET /api/browser-v2/comment-profile-metadata`

The page does not poll.

When the selected template, template revision, mode, or automatic-selection mode changes, call:

- `POST /api/browser-v2/comment-profile-selection/preview`

Each preview request receives a monotonically increasing request version. A response updates the draft only when its version and selection inputs still match the current page state, preventing an older response from replacing a newer selection.

Creation uses:

- `POST /api/browser-v2/comment-campaigns`

The payload contains only existing schema fields:

- `name`
- `mode`
- `target_source: "manual_url"`
- `target_reference`
- `template_id`
- `template_revision`
- unique `profile_refs`
- `batch_size`
- `start_mode: "manual"`

The create action is disabled while the request is active. On `201`, navigate to `/console/actions`. On failure, preserve every form value and current Profile selection.

## Validation and Errors

- Name is required and limited to 100 characters.
- Target must satisfy the existing direct TikTok video URL requirement.
- The selected template must be enabled and compatible with the selected mode.
- Batch size must be an integer from 1 through 8.
- Profile references must be unique and sufficient for the selected template.
- Automatic preview failure or shortage blocks creation.
- `403`, `422`, and `503` responses display a concise actionable message without clearing the draft.
- Network failure retains the draft and provides a retry action.

All requests continue through the existing same-origin CSRF-aware management request layer. No new authentication or backend contract is introduced.

## Implementation Boundary

Expected production changes are limited to:

- One route in `gateway/routes_console.py`.
- One native Console template.
- One small page-specific stylesheet.
- One testable create-page JavaScript controller.
- The Action Library create link in `gateway/templates/console_actions.html`.

The implementation may extract a small page-neutral create helper only when doing so prevents business validation from diverging from the legacy create flow. It must not refactor unrelated legacy Campaign behavior.

## Verification

### Flask tests

- The new route returns 200 inside the shared Console shell.
- “动作库” is the only active navigation item.
- The Action Library create button targets `/console/actions/comment-campaigns/new`.
- Campaign-row maintenance links remain `/comment-campaigns`.
- The new page contains no legacy Campaign workbench markers.

### JavaScript tests

- Initial template and Profile loading.
- Automatic and manual Profile selection.
- Stale preview response rejection.
- Field and Profile-shortage validation.
- Exact create payload, including `template_revision`.
- Repeated-submit blocking.
- Successful navigation to `/console/actions`.
- Draft preservation for validation, API, and network failures.

### Regression

- Existing Comment Campaign API and integration tests remain green.
- `/comment-campaigns` remains available and unchanged.
- Existing Campaign maintenance links remain unchanged.
- Existing browser-strategy Console create and edit pages remain unchanged.
- Final implementation receives a read-only Sol architecture and code review.

## Acceptance Criteria

1. Clicking “新建评论 Campaign” in the Action Library opens a native Console page, never the legacy workbench.
2. A valid Campaign can be created with automatic or manual Profile selection.
3. Successful creation returns to the Action Library, where the new Campaign can be loaded by the existing refresh flow.
4. Failed creation preserves the complete draft.
5. Existing Campaign maintenance and legacy workbench behavior are unchanged.
