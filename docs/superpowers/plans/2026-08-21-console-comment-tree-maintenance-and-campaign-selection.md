# Console Comment Tree Maintenance and Campaign Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore full comment-tree maintenance in the native Console UI and make Campaign creation select a mode that actually has enabled comment trees.

**Architecture:** Add one focused Console page controller that reuses the existing Comment Tree Editor and Comment Campaign APIs. Keep Campaign selection logic in its existing page controller, adding only initial-mode reconciliation, template-only refresh, and an explicit empty state. No backend schema or API changes are required.

**Tech Stack:** Flask/Jinja, browser JavaScript without a build step, Node `node:test`, existing Comment Campaign REST APIs, existing `comment_tree_editor.js`.

## Global Constraints

- Do not add or modify Comment Campaign backend APIs or database schemas.
- Keep `supported_modes` matching strict in both frontend and backend.
- Never auto-select a specific comment tree for a Campaign.
- Do not convert library-backed or multi-mode trees into editable fixed-text trees.
- Preserve editor and import drafts after validation, network, and revision-conflict failures.
- Keep `/comment-campaigns` available as a compatibility page.
- Render user content through `textContent`; do not use `innerHTML`.
- Use the existing same-origin authentication and CSRF request path.
- Do not include unrelated dirty-worktree changes in commits.

---

### Task 1: Reconcile the Campaign mode with available comment trees

**Files:**
- Modify: `gateway/static/console_comment_campaign_create.js`
- Modify: `gateway/templates/console_comment_campaign_create.html`
- Modify: `tests-js/console-comment-campaign-create.test.js`

**Interfaces:**
- Consumes: `GET /api/browser-v2/comment-templates`, existing `compatibleTemplate(template, mode)`.
- Produces: `controller.refreshTemplates(): Promise<boolean>`, `state.modeTouched: boolean`, and `model.templateEmpty: boolean`.

- [ ] **Step 1: Write failing controller tests for initial mode reconciliation**

Add tests proving that a threaded-only enabled template switches the untouched initial mode, that both-mode availability keeps `independent`, and that no template is auto-selected:

```js
test("initialization switches an untouched mode to the one with enabled trees", async () => {
  const records = [{id: "threaded", name: "盖楼", revision: 2, enabled: true, supported_modes: ["threaded"]}];
  const {controller, renders, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});

  await controller.init();

  assert.equal(controller.state.draft.mode, "threaded");
  assert.equal(controller.state.draft.template_id, "");
  assert.deepEqual(renders.at(-1).model.templates.map((item) => item.id), ["threaded"]);
  assert.equal(requests.some((item) => item.url.includes("selection/preview")), false);
});
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```powershell
node --test tests-js/console-comment-campaign-create.test.js
```

Expected: the new assertion reports `independent !== threaded`.

- [ ] **Step 3: Implement mode reconciliation and template-only refresh**

Add a deterministic helper and call it after successful template loading only while the operator has not touched the mode:

```js
const MODES = ["independent", "threaded"];

function firstAvailableMode(templates, preferred) {
  if ((templates || []).some((item) => compatibleTemplate(item, preferred))) return preferred;
  return MODES.find((mode) => (templates || []).some((item) => compatibleTemplate(item, mode))) || preferred;
}

async function refreshTemplates() {
  const result = await opts.requestJson(API + "/comment-templates", "GET");
  if (!result || result.status !== 200) throw new Error("评论树加载失败");
  state.templates = Array.isArray(envelopeData(result)) ? envelopeData(result) : [];
  if (!state.modeTouched) state.draft.mode = firstAvailableMode(state.templates, state.draft.mode);
  if (!compatibleTemplate(selectedTemplate(), state.draft.mode)) {
    state.draft.template_id = "";
    state.draft.template_revision = null;
    state.draft.profile_refs = [];
    invalidatePreview();
  }
  render();
  return true;
}
```

Set `state.modeTouched = true` only inside the user-driven `updateDraft("mode", value)` path. Expose `refreshTemplates` from the controller. Derive `model.templateEmpty` from the filtered template list.

- [ ] **Step 4: Add the maintenance, refresh, and empty-state controls**

In the comment configuration section add:

```html
<div class="console-section-actions">
  <a class="console-button" href="{{ url_for('console.comment_trees') }}" target="_blank" rel="noopener">管理评论树</a>
  <button id="campaign-template-refresh" class="console-button" type="button">刷新评论树</button>
</div>
<p id="campaign-template-empty" class="console-empty" hidden>当前没有可用评论树，请先创建或启用评论树。</p>
```

Bind the refresh button to `controller.refreshTemplates()` and render `model.templateEmpty` without setting a validation error before submit.

- [ ] **Step 5: Run the Campaign page tests**

Run:

```powershell
node --test tests-js/console-comment-campaign-create.test.js
```

Expected: all tests pass, including mode reconciliation and refresh preserving a manually selected mode.

- [ ] **Step 6: Commit the isolated Campaign fix**

```powershell
git add -- gateway/static/console_comment_campaign_create.js gateway/templates/console_comment_campaign_create.html tests-js/console-comment-campaign-create.test.js
git commit -m "fix(console): select an available comment mode"
```

---

### Task 2: Add the native Console comment-tree page shell

**Files:**
- Modify: `gateway/routes_console.py`
- Modify: `gateway/templates/console_actions.html`
- Create: `gateway/templates/console_comment_trees.html`
- Create: `gateway/static/console_comment_trees.css`
- Modify: `tests/test_console_pages.py`

**Interfaces:**
- Consumes: `console_base.html`, Flask endpoint `console.comment_trees`.
- Produces: `GET /console/actions/comment-trees` and DOM root `#console-comment-trees`.

- [ ] **Step 1: Write failing Flask page tests**

Add exact route and shell assertions:

```python
def test_console_comment_trees_is_native_action_library_page(client):
    response = client.get("/console/actions/comment-trees")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="console-comment-trees"' in html
    assert 'class="dashboard-sidebar"' in html
    assert 'href="/console/actions"' in html
    assert "console_comment_trees.css" in html
    assert "comment_tree_editor.js" in html
    assert "console_comment_trees.js" in html
    assert 'id="campaign-drawer"' not in html


def test_action_library_links_to_comment_tree_management(client):
    html = client.get("/console/actions").get_data(as_text=True)
    assert 'href="/console/actions/comment-trees"' in html
    assert ">评论树管理</a>" in html
```

- [ ] **Step 2: Run the route tests and verify 404/assertion failure**

Run:

```powershell
pytest tests/test_console_pages.py -q
```

Expected: the new route test fails because the route does not exist.

- [ ] **Step 3: Add the route, page template, and Action Library entry**

Add the route beside the existing action routes:

```python
@bp.get("/actions/comment-trees")
def comment_trees():
    return _render("console_comment_trees.html", "action-library")
```

Create a Console template with a full-width list/editor/import workspace, stable IDs for tests, and script order:

```html
{% block scripts %}
<script defer src="{{ url_for('static', filename='comment_tree_editor.js') }}"></script>
<script defer src="{{ url_for('static', filename='console_comment_trees.js') }}"></script>
{% endblock %}
```

Add “评论树管理” to the Action Library header actions.

- [ ] **Step 4: Add focused responsive styles**

Style only `.comment-trees-*` selectors. Use the existing Console control, button, table, status, and section classes. At `max-width: 760px`, stack filters and editor/preview columns and allow action buttons to wrap without horizontal scrolling.

- [ ] **Step 5: Run the route tests**

Run:

```powershell
pytest tests/test_console_pages.py -q
```

Expected: all Console page tests pass.

- [ ] **Step 6: Commit the page shell**

```powershell
git add -- gateway/routes_console.py gateway/templates/console_actions.html gateway/templates/console_comment_trees.html gateway/static/console_comment_trees.css tests/test_console_pages.py
git commit -m "feat(console): add comment tree workspace"
```

---

### Task 3: Implement comment-tree listing and lifecycle maintenance

**Files:**
- Create: `gateway/static/console_comment_trees.js`
- Create: `tests-js/console-comment-trees.test.js`

**Interfaces:**
- Consumes: existing comment-template list/detail/lifecycle endpoints and `window.ManagementFetch.requestJson` if available.
- Produces: `createConsoleCommentTrees(options)`, `createListModel(state)`, `templateSummary(template)`, and `boot(win)`.

- [ ] **Step 1: Write failing tests for list normalization, filtering, and lifecycle calls**

Test enabled/disabled grouping, localized modes, hidden internal IDs, exact expected revisions, duplicate-submit blocking, and delete cancellation:

```js
test("list model localizes modes and keeps internal IDs out of visible fields", () => {
  const model = page.createListModel({templates: [
    {id: "secret-id", name: "春季盖楼", supported_modes: ["threaded"], enabled: true, revision: 3, updated_at: "2026-08-21T00:00:00Z"},
  ], filters: {query: "", mode: "all", status: "all"}});

  assert.equal(model.enabled[0].name, "春季盖楼");
  assert.equal(model.enabled[0].modeLabel, "盖楼回复");
  assert.equal(JSON.stringify(model.enabled[0]).includes("secret-id"), false);
});

test("disable sends the current revision and refreshes the list", async () => {
  // Assert POST /comment-templates/tree/disable with {expected_revision: 3}.
});
```

- [ ] **Step 2: Run the new Node suite and verify module-not-found failure**

Run:

```powershell
node --test tests-js/console-comment-trees.test.js
```

Expected: failure because `console_comment_trees.js` does not exist.

- [ ] **Step 3: Implement the testable controller and safe list model**

Use dependency injection matching other Console controllers:

```js
function createConsoleCommentTrees(options) {
  const opts = options || {};
  const state = {
    view: "list", templates: [], draft: null, readonlyTemplate: null,
    importDraft: null, filters: {query: "", mode: "all", status: "all"},
    loading: false, submitting: false, error: "",
  };

  async function refresh() {
    if (state.loading) return false;
    state.loading = true;
    try {
      const result = await opts.requestJson("/api/browser-v2/comment-templates", "GET");
      if (!result || result.status !== 200) throw new Error("评论树加载失败");
      state.templates = Array.isArray(result.data && result.data.data) ? result.data.data : [];
      state.error = "";
      return true;
    } catch (error) {
      state.error = error && error.message ? error.message : "评论树加载失败";
      return false;
    } finally {
      state.loading = false;
      opts.render(state, createListModel(state));
    }
  }
  async function transition(template, action) {
    if (state.submitting || !["disable", "enable", "delete"].includes(action)) return false;
    state.submitting = true;
    try {
      const path = "/api/browser-v2/comment-templates/" + encodeURIComponent(template.id) + "/" + action;
      const result = await opts.requestJson(path, "POST", {expected_revision: template.revision});
      if (!result || result.status !== 200) return false;
      return refresh();
    } finally {
      state.submitting = false;
    }
  }
  return {state, refresh, transition, setFilter, openCreate, openImport, openEdit, closeWorkspace};
}
```

Build DOM nodes with `document.createElement` and `textContent`. Keep IDs only in event-handler closures and request URLs; do not include them in rendered row text, attributes, titles, or errors.

- [ ] **Step 4: Implement lifecycle error recovery**

Map `403`, `404`, `409`, `422`, and `5xx` to concise Chinese messages. On `404` and invalid lifecycle transitions, refresh the list. On revision conflict, keep any current draft and update only server-side list metadata. Never auto-retry writes.

- [ ] **Step 5: Run the list/lifecycle Node tests**

Run:

```powershell
node --test tests-js/console-comment-trees.test.js
```

Expected: all list, filter, lifecycle, and safe-rendering tests pass.

- [ ] **Step 6: Commit list and lifecycle behavior**

```powershell
git add -- gateway/static/console_comment_trees.js tests-js/console-comment-trees.test.js
git commit -m "feat(console): maintain comment tree lifecycle"
```

---

### Task 4: Integrate manual editing and Excel import

**Files:**
- Modify: `gateway/static/console_comment_trees.js`
- Modify: `gateway/templates/console_comment_trees.html`
- Modify: `tests-js/console-comment-trees.test.js`

**Interfaces:**
- Consumes: `CommentTreeEditor.createDraft`, `CommentTreeEditor.render`, `CommentTreeEditor.validate`, `CommentTreeEditor.templatePayload`; existing import preview and commit APIs.
- Produces: `saveDraft()`, `previewImport(file)`, and `commitImport(selectedTrees)` controller methods.

- [ ] **Step 1: Write failing manual-editing tests**

Cover create payloads, update revisions, read-only detection, and draft preservation:

```js
test("editing a fixed single-mode tree preserves IDs only inside the request payload", async () => {
  // GET detail, convert to draft, PUT with expected_revision, assert visible model omits IDs.
});

test("library-backed and multi-mode trees remain read only", async () => {
  // Assert no writable editor view and no PUT request.
});

test("revision conflict preserves the complete editor draft", async () => {
  // Return 409 and deepEqual the draft before and after save.
});
```

- [ ] **Step 2: Implement editor adapters and save flow**

Add exact conversion helpers:

```js
function editableTemplate(detail) {
  return Array.isArray(detail.supported_modes) && detail.supported_modes.length === 1 &&
    (detail.steps || []).every((step) => step.content_source === "fixed");
}

function draftFromTemplate(detail) {
  return {
    name: detail.name || "", description: detail.description || "",
    language: detail.language || "", tags: Array.isArray(detail.tags) ? detail.tags.slice() : [],
    mode: detail.supported_modes[0], source: "manual", advanced: false,
    editingTemplateId: detail.id, expectedRevision: detail.revision,
    nodes: detail.steps.map((step) => ({
      id: step.id, label: step.label || "", text: step.fixed_text || "",
      parentId: step.parent_step_id || null,
      requiredProfileTags: Array.isArray(step.required_profile_tags) ? step.required_profile_tags.slice() : [],
      excludedProfileTags: Array.isArray(step.excluded_profile_tags) ? step.excluded_profile_tags.slice() : [],
      language: step.language || "",
    })),
  };
}
```

Render the existing editor into the editor host and pass confirmation callbacks for mode changes and descendant deletion. POST for create and PUT with `expected_revision` for edit.

- [ ] **Step 3: Write failing Excel preview and commit tests**

Verify multipart handling, selection, sanitized commit payloads, and partial failure recovery:

```js
test("preview upload lets fetch set the multipart content type", async () => {
  // Assert the request body is FormData and no Content-Type header is supplied.
});

test("partial import removes created trees and retains rejected trees", async () => {
  // Return created/rejected arrays and assert only failed previews remain.
});
```

- [ ] **Step 4: Implement two-stage Excel import**

Submit preview as `FormData` to `/comment-template-imports/preview`. Build the commit body from selected valid trees only:

```js
function importCommitPayload(trees) {
  return {trees: trees.filter((tree) => tree.valid && tree.selected).map((tree) => ({
    name: String(tree.name || "").trim(),
    nodes: (tree.nodes || []).map((node) => ({
      node_no: String(node.node_no || ""),
      parent_node_no: node.parent_node_no == null ? null : String(node.parent_node_no),
      text: String(node.text || ""),
    })),
  }))};
}
```

After partial success, refresh the list, remove created trees from the import draft, and retain rejected errors for correction or a new file selection.

- [ ] **Step 5: Run all comment-tree UI tests**

Run:

```powershell
node --test tests-js/console-comment-trees.test.js tests-js/comment-campaign-ui.test.js
```

Expected: all tests pass; the old compatibility workbench remains unchanged.

- [ ] **Step 6: Commit editor and import behavior**

```powershell
git add -- gateway/static/console_comment_trees.js gateway/templates/console_comment_trees.html tests-js/console-comment-trees.test.js
git commit -m "feat(console): edit and import comment trees"
```

---

### Task 5: Run integration regression and final architecture review

**Files:**
- Modify only files required by failures directly caused by Tasks 1–4.
- Test: `tests-js/console-comment-campaign-create.test.js`
- Test: `tests-js/console-comment-trees.test.js`
- Test: `tests-js/comment-campaign-ui.test.js`
- Test: `tests/test_console_pages.py`
- Test: `tests/test_comment_campaign_routes.py`
- Test: `tests/test_comment_campaign_service.py`

**Interfaces:**
- Consumes: all production changes from Tasks 1–4.
- Produces: verified native maintenance and Campaign selection behavior.

- [ ] **Step 1: Run focused JavaScript regression**

```powershell
node --test tests-js/console-comment-campaign-create.test.js tests-js/console-comment-trees.test.js tests-js/comment-campaign-ui.test.js tests-js/console-actions.test.js
```

Expected: all tests pass.

- [ ] **Step 2: Run focused Python regression**

```powershell
pytest tests/test_console_pages.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_service.py -q
```

Expected: all tests pass.

- [ ] **Step 3: Run whitespace and diff-scope checks**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only the planned files plus pre-existing user changes are present.

- [ ] **Step 4: Perform read-only Sol architecture and code review**

Delegate the final diff to `gpt-5.6-sol` with `reasoning_effort: high`. Require file-and-line evidence for every blocking issue. If blockers are found, fix only those caused by this feature, rerun focused tests, and send the fix back to Sol for a second read-only review.

- [ ] **Step 5: Commit final review fixes if any**

```powershell
git add -- gateway/routes_console.py gateway/templates/console_actions.html gateway/templates/console_comment_trees.html gateway/templates/console_comment_campaign_create.html gateway/static/console_comment_trees.css gateway/static/console_comment_trees.js gateway/static/console_comment_campaign_create.js tests/test_console_pages.py tests-js/console-comment-trees.test.js tests-js/console-comment-campaign-create.test.js
git commit -m "fix(console): harden comment tree maintenance"
```

- [ ] **Step 6: Report completion**

Report the implemented behavior, exact tests and counts, any skipped environment-dependent tests, and whether commits were blocked by the current `.git` permissions.
