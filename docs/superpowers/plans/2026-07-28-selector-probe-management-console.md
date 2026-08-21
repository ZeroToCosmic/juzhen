# Selector Probe Management Console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the approved seven-tab selector-probe operations console with dynamic element management, evidence, runs, versions, strategy gates, alerts, settings, and administrator/operator controls.

**Architecture:** Extend the selector-probe Blueprint with sanitized paginated view models and revision-safe mutation endpoints. A focused vanilla JavaScript module owns probe state and rendering, while the existing browser-strategy controller remains the composition point and preserves current element/strategy editing.

**Tech Stack:** Python 3.11+, Flask 3.1.1, SQLite, Redis-backed probe services, vanilla JavaScript UMD modules, Node test runner, pytest, existing dashboard CSS.

## Global Constraints

- Complete the observe-only, healing-registry, strategy-isolation backend Tasks
  1-5, and local-management-auth plan first.
- Top-level tabs are exactly:
  `总览`, `元素`, `策略门禁`, `探针运行`, `版本`, `告警`, `设置`.
- Elements are dynamic and server-paginated; default page size 20, allowed 20,
  50, 100.
- No force-publish, force-activate, or direct probe-gate-clear UI or route.
- New/manual elements remain drafts until two profiles pass two fresh rounds
  and atomic publication succeeds.
- Operators may view, run-now, acknowledge alerts, and test Webhook only.
- Administrators own settings, element drafts, manual gates, users, and rollback
  validation.
- Full Profile IDs, secrets, CDP URLs, raw DOM/AX, raw prompts, and raw model
  output never enter responses or rendered state.
- Background polling never overwrites unsaved element or strategy drafts.
- Status is shown with text and icon, never color alone.
- Repository has no Git metadata. Do not initialize Git or add commit steps
  without user approval.

---

## File structure

Create:

- `selector_probe/catalog.py` — dynamic element catalog, filters, drafts, and
  migration lifecycle.
- `selector_probe/view_models.py` — sanitized UI projections and stable public
  error shapes.
- `gateway/static/selector_probe_ui.js` — state, requests, revision ordering,
  tab renderers, dialogs, and polling.
- `gateway/static/selector_probe.css` — responsive console components.
- `tests/test_selector_probe_catalog.py`
- `tests/test_selector_probe_view_models.py`
- `tests/conftest.py` — extend authenticated fixtures with probe stores and
  deterministic catalog/run/version/gate/alert records.
- `tests-js/selector-probe-console.test.js`
- `tests-js/selector-probe-elements.test.js`
- `tests-js/selector-probe-operations.test.js`
- `tests-js/selector-probe-settings.test.js`

Modify:

- `selector_probe/store.py` — element drafts, revisions, migrations, audit
  projections, and paginated queries.
- `selector_probe/blueprint.py` — catalog/detail/run/version/gate/alert/settings
  APIs with auth decorators.
- `selector_probe/probe.py` — element-scoped probe/validation request dispatch.
- `selector_probe/registry.py` — version diff and rollback-validation request.
- `selector_probe/gates.py` — paginated gate projection.
- `selector_probe/alerts.py` — paginated alert projection and screenshot lookup.
- `gateway/app.py:1907-2052` — seven-tab semantic shell.
- `gateway/app.py:6317-6339` — inject Blueprint services and public permissions.
- `gateway/static/browser_strategy_ui.js` — compose the probe controller and
  preserve existing drafts.
- `gateway/static/dashboard_shell.css` — import the focused probe stylesheet.
- `tests/test_selector_probe_routes.py`
- `tests/test_app.py`
- `tests/test_settings_routes.py`
- `tests-js/browser-strategy-ui.test.js`

## Task 1: Sanitized view models and paginated element catalog

**Files:**

- Create: `selector_probe/catalog.py`
- Create: `selector_probe/view_models.py`
- Modify: `selector_probe/store.py`
- Test: `tests/test_selector_probe_catalog.py`
- Test: `tests/test_selector_probe_view_models.py`

**Interfaces:**

- Produces: `PageResult(items, page, page_size, total, revision)`.
- Produces: `ElementRecord`.
- Produces: `ElementCatalog.list(query: ElementQuery) -> PageResult`.
- Produces: `ElementCatalog.get(element_id) -> ElementRecord | None`.
- Produces: `public_element_summary(record) -> dict`.
- Produces: `public_element_detail(record, evidence, dependencies) -> dict`.
- Produces: `public_error(code, *, message, details=None) -> dict`.

- [ ] **Step 1: Write failing pagination and redaction tests**

```python
import pytest

from selector_probe.catalog import ElementCatalog, ElementQuery
from selector_probe.view_models import ElementRecord, public_element_detail


def test_catalog_paginates_and_prioritizes_unhealthy_items(catalog_store):
    catalog = ElementCatalog(catalog_store)
    result = catalog.list(ElementQuery(page=1, page_size=20, status="all"))
    assert result.page == 1
    assert result.page_size == 20
    assert len(result.items) == 20
    priorities = [item.runtime_status for item in result.items[:3]]
    assert priorities == ["failed", "using_lkg", "draft"]


def test_detail_omits_raw_browser_and_model_data(element_record):
    payload = public_element_detail(
        element_record,
        evidence={
            "profile_id": "full-profile-secret",
            "profile_mask": "***3A7F",
            "raw_dom": "<html>secret</html>",
            "raw_ax": {"secret": True},
            "prompt": "private prompt",
            "model_output": "private output",
            "rounds": [],
        },
        dependencies=(),
    )
    text = str(payload)
    assert "***3A7F" in text
    for forbidden in (
        "full-profile-secret",
        "<html>",
        "private prompt",
        "private output",
    ):
        assert forbidden not in text
```

Extend `tests/conftest.py` with:

- `catalog_store`: `SelectorProbeStore(tmp_path / "management.db")` containing
  47 `managed_elements`; the first records are failed, using-LKG, a healthy
  record with `draft_status="draft"`, probe-unavailable, then healthy records;
- `element_record`: one immutable `ElementRecord` whose published locator and
  evidence use masked profile `***3A7F`.

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_catalog.py tests/test_selector_probe_view_models.py -q -p no:cacheprovider
```

Expected: imports fail.

- [ ] **Step 3: Add element revision schema**

```sql
CREATE TABLE IF NOT EXISTS managed_elements (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    management_source TEXT NOT NULL CHECK (
        management_source IN ('automatic', 'legacy_manual', 'disabled')
    ),
    published_status TEXT NOT NULL CHECK (
        published_status IN (
            'healthy', 'using_lkg', 'failed', 'probe_unavailable', 'disabled'
        )
    ),
    draft_status TEXT CHECK (
        draft_status IS NULL OR draft_status IN (
            'draft', 'queued', 'probing', 'validating'
        )
    ),
    active_version_id TEXT NOT NULL DEFAULT '',
    last_validated_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS element_drafts (
    element_id TEXT PRIMARY KEY REFERENCES managed_elements(id),
    contract_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL DEFAULT '[]',
    validation_json TEXT NOT NULL DEFAULT '{}',
    base_version_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1,
    created_by INTEGER NOT NULL REFERENCES management_users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_managed_elements_health
ON managed_elements(published_status, draft_status, last_validated_at);
```

- [ ] **Step 4: Implement exact query model**

```python
ALLOWED_PAGE_SIZES = {20, 50, 100}
ALLOWED_STATUSES = {
    "all",
    "healthy",
    "using_lkg",
    "draft",
    "failed",
    "probe_unavailable",
    "disabled",
}


@dataclass(frozen=True)
class ElementQuery:
    page: int = 1
    page_size: int = 20
    search: str = ""
    status: str = "all"
    source: str = "all"
    scope: str = "all"
    referenced: str = "all"


@dataclass(frozen=True)
class PageResult:
    items: tuple
    page: int
    page_size: int
    total: int
    revision: int


@dataclass(frozen=True)
class ElementRecord:
    id: str
    display_name: str
    management_source: str
    published_status: str
    draft_status: str | None
    scope: str
    primary_locator_type: str
    dependency_count: int
    last_validated_at: str | None
    revision: int

    @property
    def runtime_status(self) -> str:
        return self.draft_status or self.published_status
```

Reject invalid pages with `invalid_pagination`. Search uses escaped SQL `LIKE`
over display name and immutable ID plus dependency joins for strategy ID/name.
Use this overview priority:

```sql
CASE
  WHEN published_status = 'failed' THEN 1
  WHEN published_status = 'using_lkg' THEN 2
  WHEN draft_status IS NOT NULL THEN 3
  WHEN published_status = 'probe_unavailable' THEN 4
  ELSE 5
END
```

- [ ] **Step 5: Run catalog tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_catalog.py tests/test_selector_probe_view_models.py tests/test_selector_probe_store.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 2: Element APIs, drafts, validation requests, and migration

**Files:**

- Modify: `selector_probe/catalog.py`
- Modify: `selector_probe/blueprint.py`
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/store.py`
- Test: `tests/test_selector_probe_routes.py`
- Test: `tests/test_selector_probe_catalog.py`

**Interfaces:**

- Produces catalog routes from the UI specification.
- Produces:
  `ElementCatalog.create_draft(payload, actor_user_id) -> ElementRecord`.
- Produces:
  `ElementCatalog.update_draft(element_id, payload, expected_revision, actor_user_id) -> ElementRecord`.
- Produces:
  `ElementCatalog.delete(element_id, expected_revision, actor_user_id) -> None`.
- Produces:
  `ElementCatalog.create_legacy_migration(element_id, actor_user_id) -> ElementRecord`.
- Consumes: `@allow_roles` from local-management-auth.
- Test fixtures:
  - `created_element`: administrator-created draft with its current revision;
  - `dependent_element`: administrator-created element referenced by one
    persisted ready strategy.

- [ ] **Step 1: Write failing role, revision, and dependency tests**

```python
def test_operator_cannot_create_element(operator_client):
    response = operator_client.post(
        "/api/selector-probe/elements",
        json={
            "display_name": "分享入口",
            "intent": "open the active video share panel",
            "required_state": "feed_ready",
            "scope": "active_video",
            "probe_action": "open_read_only",
        },
    )
    assert response.status_code == 403


def test_stale_draft_revision_is_rejected(admin_client, created_element):
    response = admin_client.patch(
        f"/api/selector-probe/elements/{created_element['id']}/draft",
        json={
            "expected_revision": created_element["revision"] - 1,
            "contract": created_element["contract"],
        },
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "stale_revision"


def test_element_with_strategy_dependencies_cannot_be_deleted(
    admin_client,
    dependent_element,
):
    response = admin_client.delete(
        f"/api/selector-probe/elements/{dependent_element['id']}",
        json={"expected_revision": dependent_element["revision"]},
    )
    assert response.status_code == 409
    assert response.get_json()["code"] == "element_has_dependencies"
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py tests/test_selector_probe_catalog.py -k "element" -q -p no:cacheprovider
```

Expected: routes return 404 or ignore revisions.

- [ ] **Step 3: Implement exact route authorization**

Use `@allow_roles("administrator", "operator")` on read routes and
element-scoped `POST /probe`.

Use `@allow_roles("administrator")` on:

```text
POST   /api/selector-probe/elements
PATCH  /api/selector-probe/elements/<element_id>/draft
DELETE /api/selector-probe/elements/<element_id>
POST   /api/selector-probe/elements/<element_id>/validate
POST   /api/selector-probe/elements/<element_id>/migrate
```

`POST /probe` is read-only browser inspection and may be used by operators.
`POST /validate` can create a publishable draft and is administrator-only.

- [ ] **Step 4: Implement draft normalization**

Accepted create fields:

```python
CREATE_FIELDS = {
    "display_name",
    "intent",
    "required_state",
    "scope",
    "probe_action",
    "accepted_roles",
    "accepted_names",
    "name_mode",
    "preferred_attributes",
    "postcondition",
}
```

Generate immutable IDs:

```python
element_id = "element-" + secrets.token_hex(8)
```

Normalize the contract through `normalize_contracts`. Reject unknown fields,
unsafe actions, absolute XPath, JavaScript, coordinates, and invalid scope
before opening a browser.

Every mutation writes a `management_audit_events` row in the same transaction.

- [ ] **Step 5: Implement legacy migration**

For each current `action_elements` entry:

1. preserve its normalized current locators as historical candidates;
2. create `management_source="legacy_manual"`;
3. create an empty semantic-contract draft;
4. never modify a strategy dependency;
5. run observe-only until an administrator confirms the proposed contract;
6. change source to `automatic` only after atomic publication.

No migration route changes rollout mode.

- [ ] **Step 6: Run element API tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py tests/test_selector_probe_catalog.py tests/test_browser_element_schema.py -k "element or draft or migration" -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 3: Complete read APIs and dangerous-operation contracts

**Files:**

- Modify: `selector_probe/blueprint.py`
- Modify: `selector_probe/registry.py`
- Modify: `selector_probe/gates.py`
- Modify: `selector_probe/alerts.py`
- Modify: `selector_probe/store.py`
- Test: `tests/test_selector_probe_routes.py`

**Interfaces:**

- Produces paginated list/detail APIs for runs, versions, gates, alerts, audit,
  and settings.
- Produces:
  `request_rollback_validation(version_id, actor_user_id, reason, idempotency_key) -> dict`.
- Produces:
  `set_manual_gate(strategy_id, paused, actor_user_id, reason, expected_revision) -> dict`.
- Test fixtures:
  - `historical_version`: one superseded, fully published bundle ID;
  - `open_alert`: one open alert ID with a probe gate;
  - `mixed_gate`: one strategy ID with open probe and manual reasons at
    revision 2.

- [ ] **Step 1: Write failing operation-contract tests**

```python
def test_rollback_creates_new_draft_and_never_activates_source(
    admin_client,
    historical_version,
):
    before = admin_client.get("/api/selector-probe/active").get_json()["version"]
    response = admin_client.post(
        f"/api/selector-probe/versions/{historical_version}/rollback-validation",
        json={
            "reason": "restore known layout",
            "idempotency_key": "rollback-1",
        },
    )
    assert response.status_code == 202
    assert response.get_json()["draft_version"] != historical_version
    after = admin_client.get("/api/selector-probe/active").get_json()["version"]
    assert after == before


def test_acknowledge_alert_does_not_change_gate(operator_client, open_alert):
    before = operator_client.get("/api/selector-probe/gates").get_json()
    response = operator_client.post(
        f"/api/selector-probe/alerts/{open_alert}/acknowledge",
        json={"idempotency_key": "ack-1"},
    )
    assert response.status_code == 200
    after = operator_client.get("/api/selector-probe/gates").get_json()
    assert after == before


def test_manual_resume_leaves_probe_reason(admin_client, mixed_gate):
    response = admin_client.post(
        f"/api/selector-probe/strategies/{mixed_gate}/resume",
        json={
            "reason": "maintenance complete",
            "expected_revision": 2,
            "idempotency_key": "resume-1",
        },
    )
    payload = response.get_json()
    assert payload["effective_status"] == "paused"
    assert [item["source"] for item in payload["reasons"]] == ["probe"]
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py -k "rollback or acknowledge or manual_resume" -q -p no:cacheprovider
```

Expected: route or invariant failures.

- [ ] **Step 3: Implement exact list bounds**

All lists accept `page` and `page_size`. Runs, versions, gates, alerts, and audit
allow 20, 50, 100; default 20. Apply resource-specific filters from the spec.
Return:

```python
{
    "items": public_items,
    "page": page,
    "page_size": page_size,
    "total": total,
    "revision": store.current_revision(resource_name),
}
```

- [ ] **Step 4: Implement dangerous operations**

Administrator-only:

- manual pause/resume;
- rollback validation;
- settings mutation;
- element validation;
- alert manual resolve.

Administrator/operator:

- run-now;
- element probe;
- alert acknowledge;
- webhook test.

Require non-empty `reason` for manual gate, settings danger changes, and
rollback. Require `idempotency_key` for run, probe, validation, gate,
acknowledge, webhook test, and rollback. Cache the safe response for 24 hours
under `(actor_user_id, operation, idempotency_key)`.

Manual alert resolve returns `409 gate_still_active` while any underlying gate
is effective.

- [ ] **Step 5: Add safe screenshot route**

`GET /api/selector-probe/alerts/<int:alert_id>/screenshot`:

- requires authenticated administrator/operator;
- resolves the path from the alert record only;
- rejects missing/expired records with `404 screenshot_unavailable`;
- verifies the resolved absolute path is inside the configured screenshot
  directory;
- sends JPEG with `Cache-Control: private, no-store`;
- never accepts a filesystem path from the request.

- [ ] **Step 6: Run complete route tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py tests/test_selector_probe_registry.py tests/test_selector_probe_gates.py tests/test_selector_probe_alerts.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 4: Seven-tab shell and focused JavaScript controller

**Files:**

- Create: `gateway/static/selector_probe_ui.js`
- Create: `gateway/static/selector_probe.css`
- Modify: `gateway/app.py:1907-2052`
- Modify: `gateway/static/browser_strategy_ui.js`
- Modify: `gateway/static/dashboard_shell.css`
- Test: `tests-js/selector-probe-console.test.js`
- Test: `tests-js/browser-strategy-ui.test.js`

**Interfaces:**

- Produces: `createSelectorProbeUI(dependencies)`.
- Produces: `selectorProbeDependencies(root)`.
- Produces controller methods:
  `init`, `activateTab`, `refreshCurrent`, `destroy`, `snapshot`.
- Existing `createBrowserStrategyUI` composes one probe controller.

- [ ] **Step 1: Write failing tab and composition tests**

```javascript
const assert = require("node:assert/strict");
const test = require("node:test");

const {createSelectorProbeUI} = require("../gateway/static/selector_probe_ui");


function harness() {
  const renders = [];
  const ui = createSelectorProbeUI({
    requestJson: async (url) => ({
      status: 200,
      data: url.endsWith("/status")
        ? {status: "healthy", revision: 1}
        : {items: [], page: 1, page_size: 20, total: 0, revision: 1},
    }),
    render: (view, state) => renders.push([view, state.activeTab]),
    documentVisible: () => true,
    setInterval: () => 1,
    clearInterval() {},
    now: () => 1000,
  });
  return {ui, renders};
}


test("console exposes exactly seven tabs", () => {
  const {ui} = harness();
  assert.deepEqual(ui.tabs, [
    "overview", "elements", "gates", "runs", "versions", "alerts", "settings",
  ]);
});


test("activating a tab preserves unrelated browser strategy draft", async () => {
  const {ui} = harness();
  const browserDraft = {id: "strategy-1", dirty: true};
  await ui.activateTab("elements");
  assert.deepEqual(browserDraft, {id: "strategy-1", dirty: true});
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/selector-probe-console.test.js
```

Expected: module is missing.

- [ ] **Step 3: Implement state and revision ordering**

Initial state:

```javascript
const state = {
  activeTab: "overview",
  status: null,
  overview: null,
  elements: {items: [], page: 1, pageSize: 20, total: 0, revision: 0, filters: {}},
  gates: {items: [], page: 1, pageSize: 20, total: 0, revision: 0},
  runs: {items: [], page: 1, pageSize: 20, total: 0, revision: 0},
  versions: {items: [], page: 1, pageSize: 20, total: 0, revision: 0},
  alerts: {items: [], page: 1, pageSize: 20, total: 0, revision: 0},
  settings: null,
  session: null,
  selected: null,
  pending: new Map(),
};
```

`acceptRevision(resource, incoming)` rejects `incoming.revision` lower than the
stored revision. It never writes to the existing browser controller's
`elementDraft`, `strategyDraft`, or dirty flags.

- [ ] **Step 4: Add semantic shell**

Markup requirements:

- one `nav` with `role="tablist"`;
- seven buttons with `role="tab"` and matching `aria-controls`;
- one active `tabpanel`;
- page-level `aria-live="polite"` status;
- header health badge, refresh, run-now, unread-alert count;
- all text rendered with `textContent`.

Load `selector_probe_ui.js` before `browser_strategy_ui.js`; inject the probe
controller rather than adding probe logic directly to the existing module.

- [ ] **Step 5: Run shell tests**

```powershell
node --test tests-js/selector-probe-console.test.js tests-js/browser-strategy-ui.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -k "selector_probe or browser_strategy" -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 5: Overview and dynamic element directory

**Files:**

- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Test: `tests-js/selector-probe-elements.test.js`

**Interfaces:**

- Consumes: Tasks 1 and 4.
- Produces:
  `renderOverview(document, state)`.
- Produces:
  `renderElementDirectory(document, state)`.
- Produces:
  `serializeElementFilters(form) -> object`.

- [ ] **Step 1: Write failing priority, filter, and pagination tests**

```javascript
test("overview renders five unhealthy-priority elements", () => {
  const items = [
    {id: "healthy", published_status: "healthy"},
    {id: "draft", published_status: "healthy", draft_status: "draft"},
    {id: "lkg", published_status: "using_lkg"},
    {id: "failed", published_status: "failed"},
    {id: "unavailable", published_status: "probe_unavailable"},
    {id: "old", published_status: "healthy"},
  ];
  const selected = selectOverviewElements(items);
  assert.deepEqual(selected.map((item) => item.id), [
    "failed", "lkg", "draft", "unavailable", "old",
  ]);
});


test("element query uses bounded page size and encoded filters", () => {
  const query = buildElementQuery({
    page: 2,
    pageSize: 50,
    search: "评论 入口",
    status: "failed",
  });
  assert.equal(
    query,
    "?page=2&page_size=50&search=%E8%AF%84%E8%AE%BA%20%E5%85%A5%E5%8F%A3&status=failed",
  );
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/selector-probe-elements.test.js
```

Expected: functions are missing.

- [ ] **Step 3: Implement overview**

Render:

- health banner;
- current version;
- last validation and next 03:00 run;
- dynamic element counts;
- automatic/manual gate counts separately;
- alert/webhook state;
- five priority elements;
- recent sanitized events.

Every summary link activates the target tab with a matching filter. The overview
never assumes three elements.

- [ ] **Step 4: Implement directory**

Use server results directly; do not client-sort across pages. Render:

- counts;
- search;
- status/source/scope/dependency filters;
- columns from the spec;
- 20/50/100 selector;
- page buttons;
- administrator-only `新增元素`.

Search waits 300 milliseconds after the last keystroke. Abort the prior request
before issuing another. The API remains the source of truth.

- [ ] **Step 5: Run element UI tests**

```powershell
node --test tests-js/selector-probe-elements.test.js tests-js/selector-probe-console.test.js
```

Expected: all tests pass.

## Task 6: Element wizard, detail, evidence, and migration UI

**Files:**

- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Test: `tests-js/selector-probe-elements.test.js`
- Test: `tests/test_selector_probe_routes.py`

**Interfaces:**

- Produces:
  `openElementWizard()`.
- Produces:
  `serializeSemanticContract(form) -> dict`.
- Produces:
  `renderElementDetail(document, detail)`.
- Produces:
  `renderValidationMatrix(document, rounds)`.

- [ ] **Step 1: Write failing wizard safety tests**

```javascript
test("basic wizard serializes semantic fields and no selector code", () => {
  const payload = serializeSemanticContract({
    displayName: "分享入口",
    intent: "open current video share panel",
    requiredState: "feed_ready",
    scope: "active_video",
    probeAction: "open_read_only",
  });
  assert.deepEqual(Object.keys(payload).sort(), [
    "display_name", "intent", "probe_action", "required_state", "scope",
  ]);
  assert.equal("xpath" in payload, false);
  assert.equal("javascript" in payload, false);
});


test("validation matrix requires two profiles and two rounds", () => {
  const result = summarizeValidation([
    {profile_mask: "***3A7F", round: 1, status: "passed"},
    {profile_mask: "***3A7F", round: 2, status: "passed"},
    {profile_mask: "***91C2", round: 1, status: "passed"},
    {profile_mask: "***91C2", round: 2, status: "passed"},
  ]);
  assert.deepEqual(result, {profiles: 2, rounds: 2, publishable: true});
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/selector-probe-elements.test.js
```

Expected: wizard functions are missing.

- [ ] **Step 3: Implement three-step wizard**

Step 1 posts the semantic contract. Step 2 starts read-only probe and displays
Role, Name, stable attributes, deterministic candidates, LLM-used flag, and
safe rejected-method codes. Step 3 starts administrator-only validation and
polls its run detail.

Advanced selector editing uses the existing structured Locator editor and
schema. It never adds a raw script or absolute-XPath field.

- [ ] **Step 4: Implement detail tabs**

Detail tabs:

```javascript
["evidence", "candidates", "repairs", "history"]
```

Evidence shows ordered locators and the masked 2x2 matrix. Candidate comparison
shows active, deterministic, and repaired candidates. Repair history shows at
most three safe attempts. Version history links to the versions tab.

No component receives raw DOM, AX, prompt, or model response values.

- [ ] **Step 5: Implement legacy migration UI**

`legacy_manual` rows show `加入自动管理`. The dialog states:

- current Locator is retained;
- observe-only runs first;
- administrator confirms the proposed contract;
- strategy dependencies remain unchanged;
- enforcement does not turn on automatically.

- [ ] **Step 6: Run wizard and route tests**

```powershell
node --test tests-js/selector-probe-elements.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py -k "element or migration or evidence" -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 7: Gates, runs, versions, and alerts views

**Files:**

- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Test: `tests-js/selector-probe-operations.test.js`

**Interfaces:**

- Produces renderers:
  `renderGates`, `renderRuns`, `renderVersions`, `renderAlerts`.
- Produces dialogs:
  `confirmManualGate`, `confirmRollbackValidation`, `confirmAlertAction`.

- [ ] **Step 1: Write failing operation-semantics tests**

```javascript
test("manual resume copy states when probe reason remains", () => {
  const copy = manualResumeOutcome({
    reasons: [{source: "probe"}, {source: "manual"}],
  });
  assert.equal(copy.includes("仍将暂停"), true);
  assert.equal(copy.includes("probe"), true);
});


test("alert acknowledgement never advertises strategy recovery", () => {
  const action = alertActionModel({status: "open", gate_active: true});
  assert.equal(action.acknowledge.label, "确认告警");
  assert.equal(action.acknowledge.clears_gate, false);
  assert.equal(action.resolve.disabled, true);
});


test("historical version has rollback validation and no activate action", () => {
  const actions = versionActions({status: "superseded"});
  assert.deepEqual(actions.map((item) => item.id), ["rollback-validation"]);
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/selector-probe-operations.test.js
```

Expected: operation functions are missing.

- [ ] **Step 3: Implement strategy gates**

Show automatic, manual, healthy, and unmanaged totals. Each row lists all
reasons, aliases, version, affected actions, time, and actor. Manual
pause/resume requires administrator permission, non-empty reason, expected
revision, idempotency key, and exact outcome confirmation.

Never render a “continue partial run” action.

- [ ] **Step 4: Implement runs**

Show trigger, due slot, rollout mode, masked profiles, stage timings, per-element
outcomes, repairs, publish/reconcile, cleanup, and lease. `立即探测` opens the
new run ID. `probe_busy` links to the active run. Infrastructure failures show
15/30/60-minute next retry.

- [ ] **Step 5: Implement versions**

Show Active, LKG, validated/pending, superseded, failed, and conflict. Detail
shows diff, dependencies, evidence, outbox/Lua/reconcile stages. Historical
versions expose only rollback validation.

- [ ] **Step 6: Implement alerts**

Show lifecycle, occurrence count, aliases, strategies, LKG, retries, webhook,
and authenticated screenshot. Acknowledge is operator-capable and never clears
a gate. Resolve is disabled while the underlying gate is active.

- [ ] **Step 7: Run operations tests**

```powershell
node --test tests-js/selector-probe-operations.test.js tests-js/selector-probe-console.test.js
```

Expected: all tests pass.

## Task 8: Settings, preflight, Webhook, and account UI

**Files:**

- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Modify: `selector_probe/blueprint.py`
- Test: `tests-js/selector-probe-settings.test.js`
- Test: `tests/test_selector_probe_routes.py`
- Test: `tests/test_auth_routes.py`

**Interfaces:**

- Produces:
  `renderSettings`.
- Produces:
  `dangerousSettingsDiff(before, after) -> string[]`.
- Produces:
  `renderAccountManagement`.
- Consumes auth account APIs and probe settings APIs.

- [ ] **Step 1: Write failing permission and preflight tests**

```javascript
test("operator settings model is read only", () => {
  const model = settingsPermissions({
    role: "operator",
    permissions: ["probe:read", "probe:run", "alert:acknowledge", "webhook:test"],
  });
  assert.equal(model.canEdit, false);
  assert.equal(model.canTestWebhook, true);
});


test("enforce change requires preflight and reason", () => {
  const result = validateSettingsSave(
    {rollout_mode: "publish"},
    {rollout_mode: "enforce"},
    {reason: "", preflight: null},
  );
  assert.deepEqual(result.errors, ["reason_required", "preflight_required"]);
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/selector-probe-settings.test.js
```

Expected: settings functions are missing.

- [ ] **Step 3: Implement settings sections**

Sections:

- basic;
- masked AdsPower profiles;
- model;
- Redis;
- Webhook;
- permissions/accounts.

Secrets use set/unset indicators and write-only inputs. Blank retains the
current secret; explicit clear is a separate administrator-confirmed mutation.

Changing profiles, Origin, Redis, rollout mode, or enabled state requires reason
and second confirmation. `enforce` save requires successful profile, Redis
AOF/noeviction, model, and Webhook diagnostics.

- [ ] **Step 4: Implement account management**

Use `/api/admin/users`. Render username, role, state, last login, and actions.
Temporary passwords display once in a modal with a copy button; closing clears
the JavaScript value and DOM text. Never store it in controller state after
modal close.

Disable demote/disable controls for the last enabled administrator and still
handle server `409 last_administrator`.

- [ ] **Step 5: Implement synthetic Webhook test**

Operator/admin test payload contains:

```json
{
  "event": "selector_probe.webhook_test",
  "environment": "production",
  "site": "tiktok",
  "synthetic": true
}
```

It never includes a real alert, screenshot, selector, profile, or local path.

- [ ] **Step 6: Run settings and auth tests**

```powershell
node --test tests-js/selector-probe-settings.test.js tests-js/auth-ui.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py tests/test_auth_routes.py -k "settings or users or webhook" -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 9: Polling, accessibility, responsive behavior, and regressions

**Files:**

- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Modify: `tests-js/selector-probe-console.test.js`
- Modify: `tests/test_app.py`

**Interfaces:**

- Produces visibility-aware 15-second polling.
- Produces accessible seven-tab keyboard interaction.
- Produces final supported regression evidence.

- [ ] **Step 1: Write failing polling and stale-response tests**

```javascript
test("polling stops while document is hidden", async () => {
  let visible = false;
  let requests = 0;
  const ui = createSelectorProbeUI({
    requestJson: async () => { requests += 1; return {status: 200, data: {revision: 1}}; },
    render() {},
    documentVisible: () => visible,
    setInterval: (fn) => { fn(); return 1; },
    clearInterval() {},
    now: () => 1000,
  });
  ui.startPolling();
  assert.equal(requests, 0);
});


test("older revision cannot overwrite newer state", () => {
  const state = {revision: 5, items: [{id: "new"}]};
  applyRevision(state, {revision: 4, items: [{id: "old"}]});
  assert.deepEqual(state.items, [{id: "new"}]);
});
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
node --test tests-js/selector-probe-console.test.js
```

Expected: polling/revision functions fail.

- [ ] **Step 3: Implement polling and cancellation**

- Poll status, gate summary, and unread-alert count every 15 seconds.
- Skip while hidden.
- On visibility restore, refresh once immediately.
- Use one `AbortController` per resource; a new request aborts the prior one.
- `destroy` clears timers, aborts requests, and removes visibility listeners.
- Mutations update their object, then refresh its summary.

- [ ] **Step 4: Implement keyboard and responsive rules**

- Arrow keys move tabs; Home/End jump; Enter/Space activates.
- Focus remains on the triggering control after refresh.
- Dialog focus is trapped and restored.
- Progress uses polite live regions.
- Tables scroll below 900 px.
- Summary cards use five, two, then one column.
- Tab bar scrolls horizontally.
- Critical actions are visible buttons, not hover-only menus.

- [ ] **Step 5: Add forbidden-rendering regression**

```javascript
test("selector probe renderer does not use innerHTML", () => {
  const source = require("node:fs").readFileSync(
    require("node:path").join(__dirname, "../gateway/static/selector_probe_ui.js"),
    "utf8",
  );
  assert.equal(source.includes(".innerHTML"), false);
});
```

- [ ] **Step 6: Run all focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_catalog.py tests/test_selector_probe_view_models.py tests/test_selector_probe_routes.py tests/test_selector_probe_store.py tests/test_selector_probe_registry.py tests/test_selector_probe_gates.py tests/test_selector_probe_alerts.py tests/test_auth_routes.py tests/test_app.py -q -p no:cacheprovider -W error
node --test tests-js/selector-probe-console.test.js tests-js/selector-probe-elements.test.js tests-js/selector-probe-operations.test.js tests-js/selector-probe-settings.test.js tests-js/auth-ui.test.js tests-js/browser-strategy-ui.test.js
```

Expected: all focused tests pass.

- [ ] **Step 7: Run supported full suites**

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -W error
node --test tests-js/*.test.js
```

Expected: all supported Python and Node tests pass.

## Final acceptance

- [ ] Seven tabs render and are keyboard accessible.
- [ ] Overview stays dynamic with more than 100 elements.
- [ ] Administrators can add a semantic element without editing production
  Selector code.
- [ ] Drafts cannot publish before 2 profiles × 2 rounds and atomic registry
  success.
- [ ] Legacy elements migrate without dependency or rollout changes.
- [ ] Element detail exposes sanitized evidence and no raw browser/model data.
- [ ] Historical versions have rollback validation and no activate action.
- [ ] Alert acknowledgement does not clear gates.
- [ ] Probe recovery retains manual pause.
- [ ] Operators cannot reach configuration, element-draft, gate, account, or
  rollback mutations.
- [ ] Secrets and full Profile IDs never render.
- [ ] Polling preserves existing element and strategy drafts.
- [ ] Existing authenticated browser-element and strategy suites remain green.
