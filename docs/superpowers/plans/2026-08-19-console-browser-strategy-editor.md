# Console Browser Strategy Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Action Library links to the legacy Browser V2 strategy tab with a complete browser-strategy editor rendered as a dedicated Console child page.

**Architecture:** Add create and edit routes under `/console/actions/browser-strategies`, backed directly by the existing Execution V2 APIs and SQLite store. Extract the strategy draft, action, validation, serialization, and request rules into a DOM-free shared JavaScript core, then let the Console editor and legacy Browser V2 controller consume that core without changing backend behavior.

**Tech Stack:** Flask/Jinja, browser JavaScript using UMD/CommonJS-compatible modules, CSS, Node test runner, pytest, existing `/api/browser-v2` endpoints.

## Global Constraints

- Preserve `/browser-v2` and every existing Execution V2 API.
- Do not add a database, database migration, or Console proxy API.
- Preserve all five actions: move, scroll/video switch, click, input, and wait.
- Preserve readiness, click, and input element eligibility filtering.
- Preserve fixed-text and content-library input sources.
- An update must send `name`, `definition`, `enabled`, and `expected_revision` together.
- A 409 conflict must never overwrite a newer server revision.
- The Console editor must not initialize Profiles, execution jobs, picker sessions, history, or V2 local settings.
- Continue using authenticated same-origin requests and `management_fetch.js` CSRF behavior.
- Keep unrelated working-tree changes intact.

---

## File Structure

- Create `gateway/static/browser_strategy_editor_core.js`: DOM-free strategy draft, action, validation, serialization, and repository functions shared by both UIs.
- Create `gateway/templates/console_browser_strategy_editor.html`: dedicated Console create/edit workspace.
- Create `gateway/static/console_browser_strategy_editor.js`: Console-only rendering, form state, navigation, and user feedback.
- Create `gateway/static/console_browser_strategy_editor.css`: responsive editor layout using Console visual tokens.
- Create `tests-js/browser-strategy-editor-core.test.js`: shared-core unit tests.
- Create `tests-js/console-browser-strategy-editor.test.js`: Console controller unit tests.
- Modify `gateway/routes_console.py`: create/edit page routes.
- Modify `gateway/templates/console_actions.html`: new create link.
- Modify `gateway/static/console_actions.js`: per-strategy edit URLs.
- Modify `gateway/templates/browser_v2.html`: load the shared core before the legacy controller.
- Modify `gateway/static/browser_v2.js`: consume shared strategy rules instead of maintaining a second copy.
- Modify `tests/test_console_pages.py`: Flask page and shell assertions.
- Modify `tests-js/console-actions.test.js`: Action Library URL coverage.
- Modify `tests-js/browser-v2-ui.test.js`: shared-core compatibility coverage.

### Task 1: Extract the shared strategy editor core

**Files:**
- Create: `gateway/static/browser_strategy_editor_core.js`
- Create: `tests-js/browser-strategy-editor-core.test.js`
- Modify: `gateway/templates/browser_v2.html`
- Modify: `gateway/static/browser_v2.js`
- Modify: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: existing flat strategy records and `/api/browser-v2` JSON response envelope `{data: ...}`.
- Produces: `BrowserStrategyEditorCore` with `ACTIONS`, `StrategyRequestError`, `actionTemplate`, `normalizeStrategyDraft`, `createStrategyDraft`, `duplicateStrategyDraft`, `addAction`, `moveAction`, `removeAction`, `eligibleElements`, `serializeDefinition`, `buildCreatePayload`, `buildUpdatePayload`, and `createStrategyRepository`.

- [ ] **Step 1: Write the failing shared-core tests**

Add table-driven tests proving flat-record normalization, five action templates, unique IDs, ordering, filtering, content source handling, full update payloads, and status-specific request errors:

```js
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../gateway/static/browser_strategy_editor_core");

test("flat V2 records become nested editable drafts", () => {
  const draft = core.normalizeStrategyDraft({
    id: "strategy-1", name: "Feed", enabled: true, revision: 3,
    target_url: "https://www.tiktok.com/", ready_element_id: "ready-1",
    readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null,
    actions: [core.actionTemplate("wait", "wait-1")],
  });
  assert.equal(draft.definition.target_url, "https://www.tiktok.com/");
  assert.deepEqual(draft.definition.actions.map((item) => item.id), ["wait-1"]);
  assert.equal(Object.hasOwn(draft, "actions"), false);
});

test("updates are closed complete revision-checked payloads", () => {
  const draft = core.normalizeStrategyDraft({
    id: "strategy-1", name: "Feed", enabled: false, revision: 4,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "ready-1", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: []},
  });
  assert.deepEqual(Object.keys(core.buildUpdatePayload(draft)).sort(), ["definition", "enabled", "expected_revision", "name"]);
  assert.equal(core.buildUpdatePayload(draft).expected_revision, 4);
});

test("repository preserves revision conflicts as a distinct error", async () => {
  const repository = core.createStrategyRepository(async () => ({status: 409, data: {error: {message: "revision conflict"}}}));
  await assert.rejects(() => repository.update({id: "s1", revision: 1, name: "S", enabled: true, definition: {}}),
    (error) => error instanceof core.StrategyRequestError && error.code === "revision_conflict");
});
```

- [ ] **Step 2: Run the new test and verify the module is missing**

Run: `node --test tests-js/browser-strategy-editor-core.test.js`

Expected: FAIL with `Cannot find module '../gateway/static/browser_strategy_editor_core'`.

- [ ] **Step 3: Implement the UMD shared core**

Use one module in browsers and Node:

```js
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.BrowserStrategyEditorCore = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";
  const API_PREFIX = "/api/browser-v2";
  const ACTIONS = {
    move: {label: "移动"}, scroll: {label: "切换视频"}, click: {label: "点击元素"},
    input: {label: "键盘输入"}, wait: {label: "等待"},
  };
  class StrategyRequestError extends Error {
    constructor(code, message, status) { super(message); this.name = "StrategyRequestError"; this.code = code; this.status = status; }
  }
  function clone(value) { return value === undefined ? undefined : JSON.parse(JSON.stringify(value)); }
  function normalizeStrategyDraft(record) {
    const draft = clone(record || {});
    if (!draft.definition || typeof draft.definition !== "object") {
      draft.definition = {};
      ["target_url", "ready_element_id", "readiness_timeout_seconds", "run_mode", "loop_duration_minutes", "actions"].forEach((key) => {
        draft.definition[key] = draft[key]; delete draft[key];
      });
    }
    if (!Array.isArray(draft.definition.actions)) draft.definition.actions = [];
    return draft;
  }
  function requestCode(status) {
    if (status === 409) return "revision_conflict";
    if (status === 404) return "not_found";
    if (status === 422 || status === 400) return "validation_failed";
    return status === 0 ? "network_failed" : "request_failed";
  }
  function buildCreatePayload(draft) { return {id: draft.id, name: draft.name, definition: clone(draft.definition), enabled: draft.enabled !== false}; }
  function buildUpdatePayload(draft) { return {expected_revision: draft.revision, name: draft.name, definition: clone(draft.definition), enabled: draft.enabled !== false}; }
  function createStrategyRepository(requestJson) {
    async function call(url, method, body) {
      const result = await requestJson(url, method, body);
      if (![200, 201, 204].includes(result.status)) {
        const message = result.data?.error?.message || result.data?.error || "策略请求失败";
        throw new StrategyRequestError(requestCode(result.status), message, result.status);
      }
      return result.data && Object.hasOwn(result.data, "data") ? result.data.data : result.data;
    }
    return {
      loadDependencies: async () => Promise.all([call(`${API_PREFIX}/elements`, "GET"), call(`${API_PREFIX}/content-libraries`, "GET")]),
      load: (id) => call(`${API_PREFIX}/strategies/${encodeURIComponent(id)}`, "GET"),
      create: (draft) => call(`${API_PREFIX}/strategies`, "POST", buildCreatePayload(draft)),
      update: (draft) => call(`${API_PREFIX}/strategies/${encodeURIComponent(draft.id)}`, "PUT", buildUpdatePayload(draft)),
      remove: (draft) => call(`${API_PREFIX}/strategies/${encodeURIComponent(draft.id)}`, "DELETE", {expected_revision: draft.revision}),
    };
  }
  return {ACTIONS, StrategyRequestError, normalizeStrategyDraft, buildCreatePayload, buildUpdatePayload, createStrategyRepository};
});
```

Complete this module by moving the existing tested action templates, range parsing, action serialization, element eligibility, add/move/remove behavior, and unique action-ID allocation from `browser_v2.js`. Preserve the exact serialized V2 action schema already covered by `browser-v2-ui.test.js`.

- [ ] **Step 4: Wire Browser V2 to the shared core**

Load the core first in `browser_v2.html`:

```html
<script defer src="{{ url_for('static', filename='browser_strategy_editor_core.js') }}"></script>
<script defer src="{{ url_for('static', filename='browser_v2.js') }}"></script>
```

Pass the dependency into the legacy UMD factory and replace its local strategy helpers:

```js
const strategyCore = typeof module === "object" && module.exports
  ? require("./browser_strategy_editor_core")
  : root.BrowserStrategyEditorCore;
const {ACTIONS, actionTemplate, normalizeStrategyDraft} = strategyCore;
```

Keep Browser V2 view switching, jobs, picker, history, and settings inside `browser_v2.js`; only strategy rules move to the core.

- [ ] **Step 5: Run focused compatibility tests**

Run: `node --test tests-js/browser-strategy-editor-core.test.js tests-js/browser-v2-ui.test.js`

Expected: all tests PASS, including the existing five-action, flat-record, save, and action-ID cases.

- [ ] **Step 6: Commit the shared boundary**

```powershell
git add gateway/static/browser_strategy_editor_core.js gateway/static/browser_v2.js gateway/templates/browser_v2.html tests-js/browser-strategy-editor-core.test.js tests-js/browser-v2-ui.test.js
git commit -m "refactor: share browser strategy editor core"
```

### Task 2: Add Console editor routes and shell

**Files:**
- Create: `gateway/templates/console_browser_strategy_editor.html`
- Modify: `gateway/routes_console.py`
- Modify: `tests/test_console_pages.py`

**Interfaces:**
- Consumes: `_render(template, active_nav, **context)` extended in this task.
- Produces: route context `{editor_mode: "new"|"edit", strategy_id: str|None}` rendered into `#console-browser-strategy-editor[data-mode][data-strategy-id]`.

- [ ] **Step 1: Write failing Flask route tests**

```python
@pytest.mark.parametrize(
    ("path", "mode", "strategy_id"),
    [
        ("/console/actions/browser-strategies/new", "new", ""),
        ("/console/actions/browser-strategies/strategy-1/edit", "edit", "strategy-1"),
        ("/console/actions/browser-strategies/%E7%AD%96%E7%95%A5%201/edit", "edit", "策略 1"),
    ],
)
def test_console_browser_strategy_editor_uses_console_shell(client, path, mode, strategy_id):
    html = client.get(path).get_data(as_text=True)
    assert 'id="console-browser-strategy-editor"' in html
    assert f'data-mode="{mode}"' in html
    assert f'data-strategy-id="{strategy_id}"' in html
    assert html.count('aria-current="page"') == 1
    assert ">动作库</a>" in html
    for legacy in ("V2 独立执行模块", "执行中心", "运行历史", "Profile"):
        assert legacy not in html
```

- [ ] **Step 2: Run the focused Flask test and verify 404**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py -q`

Expected: new editor route cases FAIL with HTTP 404 or missing editor marker.

- [ ] **Step 3: Add the routes and context-aware renderer**

```python
@bp.get("/actions/browser-strategies/new")
def new_browser_strategy():
    return _render(
        "console_browser_strategy_editor.html",
        "action-library",
        editor_mode="new",
        strategy_id=None,
    )

@bp.get("/actions/browser-strategies/<strategy_id>/edit")
def edit_browser_strategy(strategy_id: str):
    return _render(
        "console_browser_strategy_editor.html",
        "action-library",
        editor_mode="edit",
        strategy_id=strategy_id,
    )

def _render(template: str, active_nav: str, **context):
    return render_template(
        template,
        active_nav=active_nav,
        csrf_token=session["csrf_token"],
        **context,
    )
```

Render data through Jinja escaping, and place complete values in an `application/json` bootstrap node for the controller:

```html
<div id="console-browser-strategy-editor" class="console-page"></div>
<script id="console-browser-strategy-bootstrap" type="application/json">{{ {"mode": editor_mode, "strategy_id": strategy_id}|tojson }}</script>
```

- [ ] **Step 4: Run Flask tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py -q`

Expected: all Console page tests PASS; `/console/actions/browser-strategies//edit` remains 404.

- [ ] **Step 5: Commit the page contract**

```powershell
git add gateway/routes_console.py gateway/templates/console_browser_strategy_editor.html tests/test_console_pages.py
git commit -m "feat: add console strategy editor routes"
```

### Task 3: Implement the Console strategy controller

**Files:**
- Create: `gateway/static/console_browser_strategy_editor.js`
- Create: `tests-js/console-browser-strategy-editor.test.js`
- Modify: `gateway/templates/console_browser_strategy_editor.html`

**Interfaces:**
- Consumes: `BrowserStrategyEditorCore.createStrategyRepository(requestJson)` and JSON bootstrap `{mode, strategy_id}`.
- Produces: `createConsoleStrategyEditor({document, repository, history, location, confirm, idFactory})` with `init`, `save`, `copy`, `remove`, `addAction`, `moveAction`, and `removeAction` methods.

- [ ] **Step 1: Write failing controller tests**

Use a fake repository and fake DOM to prove request scope and navigation:

```js
test("edit initialization loads only dependencies and the selected strategy", async () => {
  const calls = [];
  const repository = {
    loadDependencies: async () => { calls.push("dependencies"); return [[], []]; },
    load: async (id) => { calls.push(["strategy", id]); return strategy("s 1", 2); },
  };
  const editor = ui.createConsoleStrategyEditor(harness({mode: "edit", strategyId: "s 1", repository}));
  await editor.init();
  assert.deepEqual(calls, ["dependencies", ["strategy", "s 1"]]);
});

test("first create replaces new URL with canonical edit URL", async () => {
  const replaced = [];
  const editor = ui.createConsoleStrategyEditor(harness({
    mode: "new",
    repository: {loadDependencies: async () => [[], []], create: async () => strategy("saved 1", 1)},
    history: {replaceState: (...args) => replaced.push(args)},
  }));
  await editor.init();
  await editor.save();
  assert.equal(replaced[0][2], "/console/actions/browser-strategies/saved%201/edit");
});

test("409 leaves draft and URL unchanged", async () => {
  const error = new core.StrategyRequestError("revision_conflict", "conflict", 409);
  const state = strategy("s1", 4);
  const editor = ui.createConsoleStrategyEditor(harness({mode: "edit", initialDraft: state, repository: {update: async () => { throw error; }}}));
  await editor.save();
  assert.equal(editor.state.draft.revision, 4);
  assert.equal(editor.state.errorCode, "revision_conflict");
});
```

Also test copy navigation, confirmed delete navigation, cancelled delete, repeated-submit blocking, all five action cards, and 404/422/network error copy.

- [ ] **Step 2: Run the test and verify the controller module is missing**

Run: `node --test tests-js/console-browser-strategy-editor.test.js`

Expected: FAIL with `Cannot find module '../gateway/static/console_browser_strategy_editor'`.

- [ ] **Step 3: Implement the controller UMD boundary and lifecycle**

```js
(function (root, factory) {
  "use strict";
  const core = typeof module === "object" && module.exports
    ? require("./browser_strategy_editor_core")
    : root.BrowserStrategyEditorCore;
  const api = factory(core);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root?.document?.querySelector("#console-browser-strategy-editor")) api.boot(root);
})(typeof globalThis !== "undefined" ? globalThis : this, function (core) {
  "use strict";
  function canonicalEditUrl(id) {
    return `/console/actions/browser-strategies/${encodeURIComponent(id)}/edit`;
  }
  function createConsoleStrategyEditor(deps) {
    const state = {mode: deps.mode, draft: deps.initialDraft || null, elements: [], contentLibraries: [], submitting: false, errorCode: "", message: ""};
    const handlers = {};
    function render() { renderEditor(deps.document, state, handlers); }
    async function init() {
      const [elements, libraries] = await deps.repository.loadDependencies();
      state.elements = Array.isArray(elements) ? elements : elements?.elements || [];
      state.contentLibraries = Array.isArray(libraries) ? libraries : libraries?.content_libraries || [];
      state.draft = state.mode === "edit" ? core.normalizeStrategyDraft(await deps.repository.load(deps.strategyId)) : core.createStrategyDraft(deps.idFactory());
      render();
    }
    async function save() {
      if (state.submitting) return false;
      state.submitting = true; render();
      try {
        const saved = state.mode === "new" ? await deps.repository.create(state.draft) : await deps.repository.update(state.draft);
        state.draft = core.normalizeStrategyDraft(saved);
        if (state.mode === "new") { state.mode = "edit"; deps.history.replaceState({}, "", canonicalEditUrl(saved.id)); }
        state.message = "策略已保存"; state.errorCode = ""; return true;
      } catch (error) {
        state.errorCode = error.code || "request_failed";
        state.message = state.errorCode === "revision_conflict" ? "数据已更新，请重新加载后再保存。" : error.message;
        return false;
      } finally { state.submitting = false; render(); }
    }
    async function copy() {
      if (state.submitting) return false;
      const copied = core.duplicateStrategyDraft(state.draft, deps.idFactory());
      const saved = await deps.repository.create(copied);
      deps.location.href = canonicalEditUrl(saved.id);
      return true;
    }
    async function remove() {
      if (state.mode === "new" || !deps.confirm("确认删除这个浏览器策略？")) return false;
      await deps.repository.remove(state.draft);
      deps.location.href = "/console/actions";
      return true;
    }
    function addAction(type) { core.addAction(state.draft, type); render(); }
    function moveAction(index, delta) { core.moveAction(state.draft, index, delta); render(); }
    function removeAction(index) { core.removeAction(state.draft, index); render(); }
    Object.assign(handlers, {save, copy, remove, addAction, moveAction, removeAction});
    return {state, init, save, copy, remove, addAction, moveAction, removeAction, render};
  }
  function boot(win) {
    const data = JSON.parse(win.document.querySelector("#console-browser-strategy-bootstrap").textContent);
    const repository = core.createStrategyRepository(browserRequestJson(win));
    const editor = createConsoleStrategyEditor({
      document: win.document, repository, history: win.history, location: win.location,
      confirm: win.confirm.bind(win), idFactory: () => `strategy_${Date.now()}`,
      mode: data.mode, strategyId: data.strategy_id,
    });
    editor.init();
    return editor;
  }
  return {canonicalEditUrl, createConsoleStrategyEditor, boot};
});
```

Implement `renderEditor(document, state, handlers)` in the same module so every template field is populated with `textContent`/`value`, action cards are built from `core.ACTIONS`, and handlers are bound exactly once. Implement `browserRequestJson(win)` with the same `{status, data}` contract used by `browser_v2.js`. On copy, create a fresh ID via `idFactory`, call repository create, then assign `location.href = canonicalEditUrl(saved.id)`. On successful confirmed delete, assign `location.href = "/console/actions"`.

- [ ] **Step 4: Load scripts in dependency order**

```html
{% block scripts %}
<script src="{{ url_for('static', filename='browser_strategy_editor_core.js') }}"></script>
<script src="{{ url_for('static', filename='console_browser_strategy_editor.js') }}"></script>
{% endblock %}
```

The browser `requestJson` adapter must use `fetch` with `credentials: "same-origin"`, JSON bodies only for mutating calls, and rely on `management_fetch.js` for CSRF headers.

- [ ] **Step 5: Run controller and shared-core tests**

Run: `node --test tests-js/browser-strategy-editor-core.test.js tests-js/console-browser-strategy-editor.test.js`

Expected: all tests PASS.

- [ ] **Step 6: Commit the behavior**

```powershell
git add gateway/static/console_browser_strategy_editor.js gateway/templates/console_browser_strategy_editor.html tests-js/console-browser-strategy-editor.test.js
git commit -m "feat: implement console strategy editor"
```

### Task 4: Build the unified Console layout

**Files:**
- Create: `gateway/static/console_browser_strategy_editor.css`
- Modify: `gateway/templates/console_browser_strategy_editor.html`
- Modify: `tests/test_console_pages.py`

**Interfaces:**
- Consumes: the IDs used by `console_browser_strategy_editor.js`.
- Produces: full-width Console workspace with no `.v2-*` classes and responsive breakpoints at 980px and 560px.

- [ ] **Step 1: Add failing template assertions**

```python
def test_console_browser_strategy_editor_has_operational_layout(client):
    html = client.get("/console/actions/browser-strategies/new").get_data(as_text=True)
    for marker in ('id="strategy-settings"', 'id="strategy-action-palette"', 'id="strategy-action-list"', "返回动作库", "保存策略"):
        assert marker in html
    assert "console_browser_strategy_editor.css" in html
    assert 'class="v2-' not in html
    assert "V2 独立执行模块" not in html
```

- [ ] **Step 2: Run the layout test and verify missing markers**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py::test_console_browser_strategy_editor_has_operational_layout -q`

Expected: FAIL because the editor sections and stylesheet are not yet present.

- [ ] **Step 3: Add the complete template structure**

Use `console-page-head`, `console-section`, `console-form-grid`, `console-control`, and `console-button` for shared visual behavior. Add only editor-specific classes for the action palette, ordered cards, field groups, metadata, and danger zone. The structure is:

```html
<a class="console-back-button" href="{{ url_for('console.actions') }}">← 返回动作库</a>
<header class="console-page-head">
  <div><h1 id="strategy-page-title">浏览器策略</h1><p id="strategy-meta">正在加载</p></div>
  <div class="console-actions"><span id="strategy-enabled-badge" class="console-badge"></span><button id="strategy-save" class="console-button primary" type="submit" form="strategy-form">保存策略</button></div>
</header>
<form id="strategy-form" novalidate>
  <section id="strategy-settings" class="console-section">...</section>
  <section class="console-section"><div id="strategy-action-palette">...</div><ol id="strategy-action-list"></ol></section>
</form>
<section id="strategy-danger-zone" class="console-section">...</section>
<p id="strategy-status" class="console-status" role="status" aria-live="polite"></p>
```

- [ ] **Step 4: Add responsive editor CSS**

```css
.strategy-settings-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.strategy-field { display: grid; gap: 5px; color: var(--console-muted); font-size: 11px; }
.strategy-action-palette { display: flex; flex-wrap: wrap; gap: 8px; }
.strategy-action-list { display: grid; gap: 10px; margin: 12px 0 0; padding: 0; list-style: none; }
.strategy-action-card { padding: 13px; background: var(--console-soft); border: 1px solid var(--console-line); border-radius: 8px; }
.strategy-action-card > header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.strategy-action-fields { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.strategy-danger-zone { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
@media (max-width: 980px) { .strategy-settings-grid, .strategy-action-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .strategy-settings-grid, .strategy-action-fields { grid-template-columns: 1fr; } .strategy-action-card > header, .strategy-danger-zone { align-items: stretch; flex-direction: column; } }
```

- [ ] **Step 5: Run Flask and controller tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py -q`

Run: `node --test tests-js/console-browser-strategy-editor.test.js`

Expected: both commands PASS.

- [ ] **Step 6: Commit the unified layout**

```powershell
git add gateway/templates/console_browser_strategy_editor.html gateway/static/console_browser_strategy_editor.css tests/test_console_pages.py
git commit -m "style: unify console strategy editor"
```

### Task 5: Move Action Library create and maintenance links

**Files:**
- Modify: `gateway/templates/console_actions.html`
- Modify: `gateway/static/console_actions.js`
- Modify: `tests-js/console-actions.test.js`
- Modify: `tests/test_console_pages.py`

**Interfaces:**
- Consumes: strategy IDs from `/api/browser-v2/strategies`.
- Produces: `strategyEditorUrl(strategyId)` returning an encoded Console edit URL or an empty string for an empty ID.

- [ ] **Step 1: Write failing link tests**

```js
test("browser strategies use their dedicated Console edit URLs", () => {
  assert.equal(ui.strategyEditorUrl("策略 1"), "/console/actions/browser-strategies/%E7%AD%96%E7%95%A5%201/edit");
  assert.equal(ui.strategyEditorUrl(""), "");
  assert.equal(ui.normalizeStrategy({id: "s1", name: "Feed"}).href, "/console/actions/browser-strategies/s1/edit");
  assert.equal(ui.normalizeCampaign({id: "c1"}).href, "/comment-campaigns");
});
```

Add a Flask assertion that the static “新建浏览器策略” anchor points to `/console/actions/browser-strategies/new` and that `/browser-v2?view=strategies` is absent from the Action Library HTML.

- [ ] **Step 2: Run link tests and verify old URLs fail**

Run: `node --test tests-js/console-actions.test.js`

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py -q`

Expected: link assertions FAIL because the old Browser V2 URL is still rendered.

- [ ] **Step 3: Implement the URL helper and links**

```js
function strategyEditorUrl(strategyId) {
  const id = String(strategyId || "");
  return id ? `/console/actions/browser-strategies/${encodeURIComponent(id)}/edit` : "";
}
```

Use `strategyEditorUrl(item.id)` in `normalizeStrategy`. When a normalized strategy has no `href`, render its maintenance label as disabled text rather than an anchor.

Change the template anchor to:

```html
<a class="console-button primary" href="{{ url_for('console.new_browser_strategy') }}">新建浏览器策略</a>
```

- [ ] **Step 4: Run Action Library tests**

Run: `node --test tests-js/console-actions.test.js`

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py -q`

Expected: all tests PASS and no Action Library link contains `/browser-v2`.

- [ ] **Step 5: Commit the entry-point migration**

```powershell
git add gateway/templates/console_actions.html gateway/static/console_actions.js tests-js/console-actions.test.js tests/test_console_pages.py
git commit -m "fix: open strategies in console editor"
```

### Task 6: Regression and browser verification

**Files:**
- Modify only files from Tasks 1–5 if verification reveals an in-scope defect.

**Interfaces:**
- Consumes: the completed Console editor and unchanged Execution V2 API.
- Produces: automated and visual evidence that the new entry path works and the compatibility page still functions.

- [ ] **Step 1: Run focused JavaScript regression tests**

Run: `node --test tests-js/browser-strategy-editor-core.test.js tests-js/console-browser-strategy-editor.test.js tests-js/console-actions.test.js tests-js/browser-v2-ui.test.js`

Expected: all focused JavaScript tests PASS.

- [ ] **Step 2: Run focused Python regression tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py tests/test_execution_v2_routes.py -q`

Expected: all focused Python tests PASS.

- [ ] **Step 3: Run the complete Node suite**

Run: `npm run test:node`

Expected: every `tests-js/*.test.js` test PASS.

- [ ] **Step 4: Run the complete Python suite**

Run: `.\.venv\Scripts\python.exe -m pytest -q`

Expected: the suite exits with code 0. If a pre-existing unrelated failure remains, record its exact test name and prove every focused test above still passes.

- [ ] **Step 5: Verify the create/edit workflow in the browser**

Start the existing local application, then verify:

1. `/console/actions` opens the new Console create page.
2. Creating a strategy containing move, video switch, click, input, and wait actions changes `/new` to its encoded canonical edit URL.
3. Refresh restores all fields and action order.
4. A normal update increments the displayed revision.
5. A stale revision produces the conflict message and leaves the local draft intact.
6. Save as copy opens the copy's canonical edit URL.
7. Confirmed delete returns to `/console/actions`.
8. Desktop and narrow widths remain full-width workspaces with no right drawer.
9. `/browser-v2?view=strategies` still opens and can save an existing strategy.

- [ ] **Step 6: Request final Sol architecture and code review**

Delegate a read-only review with `model: gpt-5.6-sol` and `reasoning_effort: high`. Require findings to be tied to exact files and behavior. If a blocking issue is found, fix only that issue, rerun the focused suites, and repeat the Sol review.

- [ ] **Step 7: Commit verification fixes, if any**

```powershell
git add gateway tests tests-js docs/superpowers/specs/2026-08-19-console-browser-strategy-editor-design.md docs/superpowers/plans/2026-08-19-console-browser-strategy-editor.md
git commit -m "test: verify console strategy editor"
```
