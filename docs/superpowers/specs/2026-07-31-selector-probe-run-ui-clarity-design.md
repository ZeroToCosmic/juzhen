# Selector Probe Run UI Clarity Design

**Date:** 2026-07-31

## Context

The probe run UI currently exposes internal fields such as `repairs=0`,
`publish: unknown`, `reconcile: unknown`, `cleanup: unknown`, and
`lease: unknown`. Empty data is rendered as missing Profile, round, stage, or
element evidence. These labels do not tell an operator whether work has not
started, was skipped, succeeded, failed, or is unnecessary for the current
rollout mode.

The page also contains two run buttons. The runs-panel button is wired to
`requestRunNow()`, but the top-level `selector-probe-run-now` button has no
click handler or shared disabled state.

## Goal

Make the run page answer four questions immediately:

1. Is the probe running?
2. Which user-facing step is active?
3. What is the purpose and result of each step?
4. If the run failed, what is affected and what happens next?

Both run buttons must start the same operation and share visibility and busy
state.

## Scope

Change the existing selector-probe frontend only. Reuse current run-list and
run-detail API payloads.

Allowed files:

- `gateway/static/selector_probe_ui.js`
- `gateway/app.py` only when container markup or existing page CSS needs a
  small adjustment
- existing JavaScript and Flask page tests

Do not change probe execution, API contracts, Redis, SQLite, scheduling,
publication rules, Profile sequencing, or strategy pause/resume behavior.

## Information Model

Replace raw or ambiguous states with exactly these user-facing lifecycle
states:

- `等待执行`
- `运行中`
- `已跳过`
- `成功`
- `失败`

Do not use `unknown` for ordinary lifecycle display. Derive a status from the
run lifecycle and rollout mode:

- missing evidence on a new or active run means `等待执行`;
- an operation that is currently active means `运行中`;
- an operation not required by observe-only mode means `已跳过`;
- a completed operation with positive evidence means `成功`;
- an operation or run with a failure code means `失败`.

### Existing Field Meanings

- Profile evidence proves that each dedicated AdsPower Profile participated in
  the run. Empty evidence becomes `Profile 验证尚未开始`.
- Round evidence proves two consecutive validations per Profile. Empty evidence
  becomes `稳定性验证尚未开始`.
- Stage evidence records navigation, readiness, extraction, validation, and
  cleanup progress. Raw stages move to technical details.
- Element results contain discovered aliases, locator validation, and failures.
  Empty results become `尚未发现可用元素`.
- Repairs count means feedback-loop/self-healing attempts. Zero becomes
  `未触发自愈`.
- Publication means atomic selector-version publication. Observe mode becomes
  `观察模式，不发布`.
- Reconciliation means synchronizing affected strategy gates after publication.
  When unnecessary it becomes `本次无需协调`.
- Cleanup means closing probe pages and releasing temporary resources.
- Lease means the backend lock that prevents concurrent probe runs. It remains
  hidden in normal display and appears only in technical details or as
  `已有探针正在运行` on conflict.

## Run List

Each run card shows only:

- run identifier;
- translated overall status;
- current user-facing step;
- progress as completed steps out of five;
- one concise current-result or failure sentence;
- a `查看运行` action.

The list must not dump Profile, round, stage, element, repair, publication,
reconciliation, cleanup, and lease rows inline.

## Run Detail

Use the approved status-summary layout. The header shows run status, elapsed or
completion time, rollout mode, current step, and progress.

Group existing evidence into five user-facing stages.

### 1. Prepare Test Environment

Chinese label: `准备测试环境`

Purpose: connect the two dedicated AdsPower Profiles, obtain the concurrency
lease, and open an isolated probe page.

Includes:

- lease acquisition;
- `cdp_endpoint`;
- `cdp_ready`;
- `probe_page_open`.

### 2. Load TikTok

Chinese label: `加载 TikTok 页面`

Purpose: verify that the page is not blank, blocked by login/CAPTCHA, or still
in its initial loading state.

Includes `page_readiness`.

### 3. Discover and Validate Elements

Chinese label: `发现并验证元素`

Purpose: extract semantic evidence, find comment entry/input/submit controls,
and perform Dry-Run validation.

Includes:

- `a11y_snapshot`;
- `candidate_filter`;
- `element_dry_run`;
- `comment_panel_transition`;
- element results;
- repair attempts.

### 4. Confirm Two Profiles and Two Rounds

Chinese label: `两个 Profile 连续两轮确认`

Purpose: reject one-account, one-round, or transient network false positives.

Show each masked Profile and its two round states. If a Profile is connected but
has not entered observation, show `等待开始`, not missing evidence.

### 5. Publish and Clean Up

Chinese label: `发布结果并清理`

Purpose: publish a validated selector version when allowed, reconcile affected
strategies, close owned pages/resources, and release the lease.

Includes:

- publication;
- reconciliation;
- cleanup;
- lease release.

Observe-only runs must explicitly show publication and reconciliation as
skipped, while cleanup remains required.

## Failure Presentation

When a stage fails, show three plain-language fields:

- `失败原因`: translated failure code with the raw code available in technical
  details;
- `影响范围`: affected Profile, round, aliases, or strategies when present;
- `系统下一步`: retry, keep last stable selectors, pause affected strategies,
  or require manual review according to existing backend evidence.

The frontend must not invent an effect or recovery action when the API does not
provide evidence. In that case show `等待系统确认`.

## Technical Details

Keep a collapsed `技术详情` section containing:

- raw stage names and statuses;
- failure codes;
- attempt counts;
- durations;
- lease, publication, reconciliation, and cleanup operation records;
- discovery attributes and recommended locators.

Failure may automatically expand the relevant technical subsection, but normal
successful or active runs keep it collapsed.

## Run Buttons

Keep both buttons:

- top-level `selector-probe-run-now` labelled `立即运行探针`;
- runs-panel `selector-run-now` labelled `立即探测`.

Both call the same `controller.requestRunNow()` operation. Both buttons:

- are visible only to administrator and operator roles;
- share the same disabled state while a request or active run exists;
- cannot create duplicate requests from one click;
- lead to the runs tab and show the same error state when the request fails.

## Testing

Add or update tests proving:

- each button issues exactly one `POST /api/selector-probe/run-now` request;
- starting from either button disables both while busy;
- both buttons follow the same role visibility rules;
- observe mode displays `观察模式，不发布` and `本次无需协调`;
- not-started, running, skipped, success, and failure remain distinct;
- Profile and two-round evidence aggregate into the fourth stage;
- failure output includes reason, impact, and next action when evidence exists;
- normal run cards no longer render raw `repairs=0`, operation `unknown`, or
  missing-evidence placeholders;
- technical details retain raw stages, attempts, durations, and codes;
- existing JavaScript and Flask page tests pass.

## Non-Goals

- changing backend stage generation;
- changing run status codes or API schemas;
- adding a new monitoring or polling endpoint;
- changing selector validation or publication behavior;
- changing AdsPower Profile lifecycle;
- redesigning other selector-probe tabs.
