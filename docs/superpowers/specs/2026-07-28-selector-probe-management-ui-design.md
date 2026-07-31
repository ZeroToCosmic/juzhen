# Selector Probe Management UI Design

Date: 2026-07-28  
Status: Approved design supplement  
Parent specification:
[`2026-07-28-tiktok-selector-probe-self-healing-design.md`](2026-07-28-tiktok-selector-probe-self-healing-design.md)

## Purpose

Extend the existing “网页元素与执行策略” page into one integrated operations
console for selector discovery, validation, publication, strategy isolation,
recovery, and alert handling.

The UI is an operational projection of durable server state. It never becomes
the authority for selector versions, gates, probe runs, permissions, or alert
lifecycle.

## Approved decisions

- Use one integrated console, not a separate selector-probe application.
- Top-level tabs:
  `总览`, `元素`, `策略门禁`, `探针运行`, `版本`, `告警`, `设置`.
- Element count is dynamic. Administrators can add elements at any time.
- New and manually edited elements remain drafts until the full validation and
  atomic publication path succeeds.
- There is no force-publish or force-activate control.
- Administrators and operators have different server-enforced permissions.
- Identity comes from built-in local accounts; the existing application has no
  external identity provider.
- Alert acknowledgement does not clear strategy gates.
- Automatic recovery never clears a manual pause.

## Local identity and session security

The authentication boundary covers the whole management dashboard, all
selector-probe routes, and existing element/strategy read and mutation routes.
Protecting only new selector-probe endpoints would leave an authorization
bypass through existing APIs.

Unauthenticated access is limited to:

- login-page assets;
- login endpoint;
- `GET /healthz`, returning only `{"status": "ok"}`.

All other HTML and API routes require an enabled local user.

### First administrator

There is no default user or default password.

The first administrator is created on the host:

```powershell
.\.venv\Scripts\python.exe -m gateway.admin_users create-admin --username admin
```

The command reads the password twice from an interactive hidden prompt. It
rejects command-line and environment-variable passwords so the credential does
not enter shell history or process listings.

The command fails if the username exists. Later users are managed by an
administrator in `设置 / 权限`.

### User storage

Store local users in the same durable SQLite database boundary as management
audit state:

```sql
CREATE TABLE IF NOT EXISTS management_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('administrator', 'operator')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    session_version INTEGER NOT NULL DEFAULT 1,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Passwords use Werkzeug `generate_password_hash(..., method="scrypt")` and
`check_password_hash`. Plaintext and password hashes never enter responses,
logs, audit details, webhook payloads, or settings JSON.

### Account lifecycle

- An administrator creates a user with a cryptographically generated temporary
  password of at least 20 printable characters.
- The temporary password is displayed once and is not persisted separately from
  its hash. Browser-based creation is allowed only over HTTPS or a loopback
  Origin; otherwise the administrator uses the hidden-prompt CLI.
- The user must change it at first login before accessing other dashboard
  routes.
- Usernames are case-insensitively unique and cannot be renamed.
- Disabling, password-resetting, or changing a role increments
  `session_version`, invalidating every existing session for that user.
- The last enabled administrator cannot be disabled, deleted, or demoted.
- Users are disabled rather than physically deleted so audit actor references
  remain valid.

Passwords require at least 12 characters. Login failures use one generic
message. Five consecutive failures lock the account for 15 minutes; successful
login clears the counter. Disabled and unknown users run a dummy password-hash
check to reduce timing differences.

### Session

Use Flask's signed session cookie with a 64-byte random signing secret stored in
a private application-state file outside public settings. Generate it once
with an atomic create; never regenerate it on ordinary restart.

Cookie policy:

- `HttpOnly`;
- `SameSite=Lax`;
- `Secure` when the configured public Origin is HTTPS;
- no Domain attribute;
- idle timeout: 30 minutes;
- absolute timeout: 8 hours.

The signed session contains only user ID, role snapshot, `session_version`,
issued time, last activity, and CSRF token. Each protected request reloads the
enabled user and compares `session_version`; role and enabled state therefore
cannot remain stale until cookie expiry.

### CSRF

Generate a random CSRF token for the pre-login session and rotate it after
login, privilege change, password change, and logout.

Every `POST`, `PUT`, `PATCH`, and `DELETE`, including login, requires the token:

- HTML forms use a hidden field;
- JSON requests use `X-CSRF-Token`;
- server compares with `hmac.compare_digest`;
- missing or invalid tokens return `403 csrf_failed`.

Authentication cookies are never accepted as authorization for a cross-origin
mutation without the CSRF token.

### Login and account UI

The login page contains username and password only. It never confirms whether a
username exists. Locked accounts see the same generic login failure response.

`设置 / 权限` lists:

- username;
- role;
- enabled/locked/must-change-password state;
- last login;
- row actions allowed for the current administrator.

Actions:

- create user;
- change role;
- enable/disable;
- generate a new one-time temporary password;
- revoke sessions.

Every action uses a confirmation dialog and audit event. A user cannot disable
or demote their own account when doing so would remove the last enabled
administrator.

## Users and permissions

### Administrator

May:

- view all public probe state and sanitized evidence;
- create, edit, and execute existing browser strategies;
- run a probe immediately;
- acknowledge alerts and test a webhook;
- create or edit an element semantic-contract draft;
- change probe settings;
- add or remove dedicated AdsPower test profiles;
- create or clear a manual strategy pause;
- start a historical-version rollback validation;
- resolve a stale alert when its underlying gate is no longer active.

May not:

- bypass two-profile/two-round validation;
- force-publish a selector bundle;
- clear a probe gate directly;
- view stored secrets, full Profile IDs, CDP URLs, cookies, raw DOM, or raw AX
  payloads.

### Operator

May:

- view status, element health, sanitized evidence, versions, runs, gates, and
  alerts;
- run a probe immediately;
- acknowledge an alert;
- test the configured webhook with a sanitized sample.

May not:

- create, edit, delete, or execute an existing browser strategy;
- change settings;
- create or edit element drafts;
- add or remove test profiles;
- create or clear manual pauses;
- start rollback validation;
- publish, delete, or migrate an element.

All permissions are checked by the backend. Disabled or hidden buttons are
usability aids, not authorization controls. Every state-changing request uses
the authenticated actor identity and creates an audit event.

## Information architecture

```text
网页元素与执行策略
├── 总览
├── 元素
│   ├── 动态元素目录
│   ├── 新增元素向导
│   └── 元素详情
│       ├── 概览与证据
│       ├── 候选对比
│       ├── 修正记录
│       └── 版本历史
├── 策略门禁
├── 探针运行
├── 版本
├── 告警
└── 设置
    ├── 基本设置
    ├── 测试 Profiles
    ├── 模型
    ├── Redis
    ├── Webhook
    └── 权限
```

The existing strategy editor remains available under the strategy portion of
the page. Selector-probe state augments it; it does not replace canonical
strategy editing.

## Global shell

The page header contains:

- page title;
- global probe-health badge with text and icon;
- manual refresh;
- `立即探测`, visible to administrators and operators;
- unread-alert count.

The tab bar remains visible while navigating detail views. Detail views use a
breadcrumb and preserve the active list filters when the user returns.

Status is never encoded by color alone. Each badge includes a label and icon.

### Refresh behavior

- Status, gate summary, and unread-alert count poll every 15 seconds while the
  page is visible.
- Polling stops when `document.visibilityState != "visible"`.
- Runs, versions, elements, and alerts use bounded paginated requests.
- A manual refresh cancels stale in-flight reads and reloads the current view.
- A mutation response updates its affected object immediately, then refreshes
  the related summary.
- Responses carry a server revision. An older response cannot overwrite newer
  client state.

## 总览

The overview answers four questions within ten seconds:

1. Is the probe healthy?
2. Which selector version is active?
3. Are any elements using LKG or failing?
4. Which strategies are paused, and why?

### Health banner

Shows:

- overall status;
- last successful validation;
- next scheduled run at `03:00 Asia/Shanghai`;
- current rollout mode;
- whether the Active Bundle, SQLite published version, and Redis projection
  agree.

### Summary cards

- current Active Bundle version and publish time;
- last successful two-profile/two-round validation;
- element totals grouped by health;
- automatic and manual gate counts shown separately;
- open-alert count and latest webhook-delivery status.

### Overview lists

`关键元素健康` is not a fixed three-element list. It shows at most five
priority items in this order:

1. failed;
2. using LKG;
3. draft awaiting validation;
4. probe unavailable;
5. oldest successful validation.

A `查看全部元素` action opens the filtered element directory.

`最近事件` shows sanitized publication, recovery, gate, run, and alert events.
It never shows raw selectors, full profile identifiers, or model output.

## 动态元素目录

The directory supports an arbitrary practical number of elements through
server-side pagination. Default page size is 20; allowed values are 20, 50,
and 100.

### Summary

Show counts for:

- all;
- healthy;
- using LKG;
- draft awaiting validation;
- failed;
- disabled.

### Search and filters

Search:

- display name;
- immutable alias/ID;
- Role;
- stable attribute name;
- strategy ID or strategy name.

Filters:

- health status;
- management source: automatic, legacy manual, or disabled;
- required page state;
- scope;
- referenced/unreferenced by an enabled strategy.

### Columns

- display name and scope;
- health status;
- current primary locator type;
- strategy dependency count;
- last validation time;
- row menu.

The row menu contains only actions allowed for the current role and state.

### Element status model

```text
draft
queued
probing
validating
healthy
using_lkg
failed
probe_unavailable
disabled
```

`healthy`, `using_lkg`, and `failed` describe published runtime state.
`draft`, `queued`, `probing`, and `validating` describe an unpublished draft.
The UI may show one published status and one draft status on the same element.

## Adding an element

An administrator opens a right-side three-step wizard.

### Step 1: 描述目标

Required:

- display name;
- semantic intent;
- required page state;
- scope;
- one action from the closed read-only probe vocabulary.

Optional advanced constraints:

- accepted Roles;
- exact, contains, or locale-map Name policy;
- preferred stable attributes;
- postcondition.

The form never accepts executable JavaScript, coordinates, an arbitrary browser
action, or an absolute XPath.

### Step 2: 探针建议

The first dedicated test profile prepares the required state and captures a
fresh sanitized AX+DOM snapshot. The UI displays:

- proposed Role and Name policy;
- stable attributes;
- deterministic candidate selectors;
- whether an LLM repair was needed;
- sanitized warnings and rejected methods.

The administrator confirms or edits the semantic contract. Direct selector
editing is available only in the advanced draft editor and remains subject to
the same schema and validation.

### Step 3: 验证发布

The UI shows progress for:

- both masked test profiles;
- both fresh rounds;
- uniqueness;
- Role and Name policy;
- visibility and viewport state;
- actionability and obstruction;
- stability interval;
- postcondition;
- atomic publication and registry reconciliation.

The completion state is either:

- published and available to strategies;
- saved draft with actionable validation errors;
- infrastructure failure with retry schedule.

There is no partial publication.

## Existing-element migration

Existing action elements appear in the directory as `legacy_manual`.

Migration is non-destructive:

1. retain the current structured locator as historical/LKG evidence;
2. create a semantic-contract draft;
3. run observe-only validation;
4. require administrator confirmation of the proposed contract;
5. publish through the normal two-profile/two-round path;
6. change management source to `automatic`.

Enforcement remains off until the environment rollout mode reaches `enforce`.

## Element detail

The header shows:

- element name and immutable alias;
- published health;
- unpublished-draft status;
- active selector version;
- dependency count;
- last validation.

### 概览与证据

Show the ordered structured Locator candidates, not a single XPath:

- locator type;
- normalized selector representation;
- priority;
- uniqueness result;
- fallback status.

The two-profile/two-round matrix uses masked profile identifiers and includes:

- match count;
- Role/Name result;
- visible/in-viewport result;
- actionability result;
- postcondition result.

### 候选对比

Compare:

- active candidate;
- deterministic draft;
- each LLM-repaired candidate;
- prohibited prior method;
- validation result.

Candidates rejected by schema or safety checks are shown by safe reason code,
not raw model output.

### 修正记录

For each of at most three attempts:

- attempt number;
- previous method;
- failure code and match count;
- new method;
- prompt version and model ID;
- validation result.

The view excludes prompt content, API keys, raw DOM/AX, and raw response bodies.

### 版本历史

Shows versions containing the element and a diff of semantic contract, locator
order, and validation evidence.

## 策略门禁

Summary cards show:

- automatic probe-paused strategies;
- manually paused strategies;
- healthy strategies;
- unmanaged strategies.

Each strategy row shows:

- effective state;
- all uncleared reason sources;
- failed aliases;
- selector version;
- affected action IDs;
- last change time and actor where applicable.

Rules:

- a probe reason cannot be cleared by a UI action;
- a manual reason can be created or cleared only by an administrator;
- clearing a manual reason does not clear a probe reason;
- when both exist, the strategy remains paused until both are cleared;
- an unrelated strategy remains visibly executable;
- a partial run is terminal and is never offered a “continue” button.

Manual pause and resume require a reason and a confirmation dialog. The dialog
states the exact strategy and effective result after the change.

## 探针运行

The runs page shows:

- scheduled, manual, and retry runs;
- due slot and trigger actor;
- rollout mode;
- masked profiles;
- per-stage timing;
- per-element outcome;
- repair-attempt count;
- publish/reconcile result;
- cleanup and lease result.

Filters:

- time range;
- trigger type;
- run status;
- failure class;
- affected alias.

`立即探测` returns a request ID and opens the new run detail. Concurrent
requests display `probe_busy` and link to the active run. Infrastructure
failures show the next retry at 15, 30, or 60 minutes. Selector failures show
the bounded three-attempt history.

## 版本

The version list distinguishes:

- current Active Bundle;
- current LKG;
- validated/publish-pending;
- superseded;
- failed draft;
- publication conflict.

The detail view shows:

- bundle metadata;
- changed elements;
- affected strategies;
- two-profile/two-round evidence;
- SQLite/outbox/Redis publication stages;
- reconciliation result;
- diff from the prior LKG.

### Rollback

There is no direct `激活此版本` action.

`基于此版本发起回滚验证`:

1. copies the historical bundle into a new draft;
2. assigns a new version ID;
3. executes two profiles and two fresh rounds;
4. publishes atomically;
5. reconciles the registry;
6. clears only covered probe reasons;
7. leaves manual reasons unchanged.

## 告警

The list supports status, severity, failure-class, alias, strategy, and time
filters.

Alert lifecycle:

```text
open -> acknowledged -> resolved
```

Repeated matching failures update the same open alert and occurrence count.

The detail view shows:

- safe failure class;
- affected aliases and strategies;
- active/LKG version;
- three repair attempts;
- retry summary;
- webhook-delivery state;
- sanitized screenshot;
- event timeline.

Rules:

- acknowledgement records ownership only;
- acknowledgement never clears a gate;
- re-run creates a new probe run only;
- a probe recovery resolves matching alerts after atomic publication;
- a manual resolve is unavailable while the underlying effective gate remains
  active;
- screenshot access uses authenticated numeric alert IDs;
- screenshot expiry does not remove the structured alert audit.

## 设置

### 基本设置

- enabled;
- rollout mode: observe, publish, or enforce;
- schedule: default `03:00 Asia/Shanghai`;
- approved HTTPS target Origin;
- infrastructure retry policy;
- 36-hour freshness policy.

Changing to `enforce` requires a successful preflight.

### 测试 Profiles

- masked profile list;
- dedicated-test marker;
- connection health;
- add/remove controls for administrators;
- connection test.

Saving requires at least two unique profiles and rejects any ID classified as a
production profile.

### 模型

- select an enabled model from existing model settings;
- show provider, mode, and test status;
- show that LLM is repair-only;
- never display the API key.

### Redis

- connection status;
- namespace;
- AOF diagnostic;
- eviction-policy diagnostic;
- last reconciliation;
- no password display.

Production `enforce` preflight fails if the required dedicated persistence and
`noeviction` policy cannot be confirmed.

### Webhook

- enabled;
- type;
- sanitized URL display;
- signing-secret set/unset indicator;
- delivery timeout and retry policy;
- test action;
- most recent delivery state.

Testing sends a sanitized synthetic payload and creates an auditable delivery
record. It never sends a real screenshot or production failure payload.

### 权限

Show the administrator/operator matrix from this specification. Backend policy
is authoritative.

### Dangerous changes

Changing a test profile, target Origin, Redis configuration, rollout mode, or
disabling the probe requires:

- administrator role;
- reason;
- second confirmation;
- configuration validation;
- preflight where applicable;
- audit event.

Secrets use write-only fields. Blank means unchanged; a separate explicit
control clears a secret.

## Error presentation

Use stable public error codes and user actions:

- `probe_busy`: link to the active run;
- `profile_unavailable`: show masked profile and next retry;
- `page_not_ready`: show infrastructure classification and retry;
- `selector_validation_failed`: link to element and alert;
- `publication_conflict`: keep Active unchanged and link to reconciliation;
- `registry_unavailable`: show managed-strategy fail-closed state;
- `strategy_paused`: list all reason sources;
- `forbidden`: explain the required role;
- `stale_revision`: reload the changed record without discarding an unsaved
  draft.

Raw exception strings are never rendered.

## Confirmation dialogs

Every dialog names the exact target and outcome.

- `人工暂停`: strategy, reason, actor, immediate effect.
- `人工恢复`: reason source being cleared and whether another source remains.
- `删除元素`: dependency count; blocked until dependencies are removed.
- `回滚验证`: source version and the fact that it creates a new draft.
- `切换 Enforce`: preflight results and managed-strategy fail-closed behavior.
- `移除 Profile`: remaining profile count; blocked below two.

No dialog offers force publication or direct probe-gate clearing.

## Accessibility and responsive behavior

- Full keyboard navigation.
- Visible focus.
- Semantic headings, tabs, tables, dialogs, and live regions.
- Status text and icon in addition to color.
- Form errors associated with fields.
- Progress updates announced without moving focus.
- Tables use horizontal scrolling below 900 pixels.
- Summary cards collapse from five columns to two, then one.
- The tab bar scrolls horizontally on narrow displays.
- No critical action exists only in a hover menu.

## API surface required by the UI

Existing approved probe APIs remain. Add or refine:

```text
GET    /api/selector-probe/elements
POST   /api/selector-probe/elements
GET    /api/selector-probe/elements/<element_id>
PATCH  /api/selector-probe/elements/<element_id>/draft
DELETE /api/selector-probe/elements/<element_id>
POST   /api/selector-probe/elements/<element_id>/probe
POST   /api/selector-probe/elements/<element_id>/validate
GET    /api/selector-probe/elements/<element_id>/evidence
GET    /api/selector-probe/elements/<element_id>/dependencies
GET    /api/selector-probe/versions/<version_id>
GET    /api/selector-probe/versions/<version_id>/diff
POST   /api/selector-probe/versions/<version_id>/rollback-validation
GET    /api/selector-probe/runs/<run_id>
GET    /api/selector-probe/audit-events
GET    /api/selector-probe/alerts/<alert_id>/screenshot
```

Mutations require:

- the exact CSRF protection defined in this specification;
- role authorization;
- expected revision;
- actor identity;
- reason for dangerous actions;
- idempotency key for run, validation, pause/resume, and rollback requests.

List responses are paginated and sanitized. Secrets and internal browser
identifiers are omitted server-side.

### Authentication and account routes

```text
GET    /login
GET    /healthz
GET    /api/auth/session
POST   /api/auth/login
POST   /api/auth/logout
POST   /api/auth/change-password
GET    /api/admin/users
POST   /api/admin/users
PATCH  /api/admin/users/<user_id>
POST   /api/admin/users/<user_id>/reset-password
POST   /api/admin/users/<user_id>/revoke-sessions
```

`/api/auth/session` returns only the current username, role, permission names,
must-change-password flag, and CSRF token. Account-management routes require
administrator role.

## Existing-UI integration

Follow the current vanilla JavaScript controller pattern:

- extend `createBrowserStrategyUI` state with bounded probe view models;
- keep API normalization separate from rendering;
- use `textContent`, DOM constructors, and `replaceChildren`;
- do not render server strings through `innerHTML`;
- preserve element and strategy editor drafts during background refresh;
- route mutations through injected `requestJson` for Node tests.

Large view renderers are split by tab. The existing
`gateway/static/browser_strategy_ui.js` remains the composition point; probe
view-model normalization and render helpers move into a focused
`gateway/static/selector_probe_ui.js` module to avoid further growth of the
existing file.

## Audit events

Record:

- settings change;
- role-denied mutation;
- login success/failure/lockout and logout;
- user create/enable/disable/role change/password reset/session revoke;
- password change without password material;
- manual pause/resume;
- element create/edit/delete/migrate;
- manual run;
- validation request;
- rollback-validation request;
- alert acknowledgement/resolve;
- webhook test;
- secret set/clear.

Audit views show actor, action, safe target identifier, reason, result, and
timestamp. They omit secret values and raw browser/model data.

## Testing

### Python route and authorization tests

- no default account exists;
- first-admin CLI uses a hidden interactive prompt and rejects duplicate users;
- login, lockout, timeout, CSRF, password change, and session revocation;
- the last enabled administrator cannot be disabled or demoted;
- all dashboard and existing element/strategy routes require authentication;
- every mutation allows administrators and rejects operators unless explicitly
  permitted;
- operator run-now, acknowledge, and webhook-test permissions;
- full Profile IDs and secrets never appear in responses;
- pagination bounds;
- stale revision and idempotency;
- deletion blocked by dependencies;
- no force-publish or direct probe-gate route exists.

### JavaScript controller tests

- seven-tab navigation;
- status polling pauses when hidden;
- older revisions cannot overwrite newer state;
- background refresh preserves drafts;
- filters and pagination;
- role-based control state;
- status labels do not rely on color;
- confirmation-dialog outcome text;
- no `innerHTML` rendering path.

### Workflow tests

- create element, confirm semantic suggestion, validate, and publish;
- failed draft remains unpublished;
- existing legacy element migrates without strategy interruption;
- selector failure pauses only dependent strategies;
- alert acknowledgement leaves the gate unchanged;
- atomic recovery clears the probe reason but retains manual pause;
- historical rollback creates a new validated version;
- operator cannot change configuration;
- preflight blocks unsafe `enforce`.
- an operator cannot bypass policy through legacy element or strategy APIs.

## Acceptance criteria

- An administrator can add an arbitrary new semantic element without editing a
  production selector directly.
- The element cannot enter an Active Bundle before two profiles pass two fresh
  rounds and atomic publication succeeds.
- The overview remains usable with more than 100 elements.
- A failed alias visibly pauses only dependent strategies.
- Manual and automatic pause reasons remain distinguishable everywhere.
- Alert acknowledgement, recovery, and manual resume have distinct effects.
- All sensitive identifiers and secrets remain absent from UI responses.
- Operators can handle routine monitoring without gaining configuration or
  pause authority.
- Local login protects existing and new management routes, including legacy
  mutation paths.
- Existing element and strategy editing remains functional.
