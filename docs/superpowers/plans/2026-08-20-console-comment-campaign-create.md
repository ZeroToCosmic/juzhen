# Console Comment Campaign Create Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Action Library's “新建评论 Campaign” button open a native Console create form while leaving all existing Campaign maintenance and backend behavior unchanged.

**Architecture:** Add one Console route and one page-specific UMD/CommonJS controller. The controller reads only comment templates and cached Profile metadata, calls the existing selection-preview endpoint when needed, and posts the existing strict Campaign create payload. The legacy workbench, maintenance links, Campaign service, and worker remain untouched.

**Tech Stack:** Python 3, Flask/Jinja, browser JavaScript with CommonJS tests, CSS, pytest, Node's built-in test runner.

## Global Constraints

- The only new page route is `/console/actions/comment-campaigns/new`.
- Successful creation navigates to `/console/actions`.
- Campaign-row maintenance links remain `/comment-campaigns`.
- Do not modify `gateway/static/console_actions.js`, `gateway/static/comment_campaign.js`, `gateway/templates/comment_campaign.html`, or any file under `comment_campaign/`.
- Do not add Campaign detail, maintenance, planning, approval, Assignment, receipt, evidence, scheduling, Profile-sync, polling, or Central-sync behavior.
- Do not redirect, embed, restyle, or remove `/comment-campaigns`.
- Use only the existing strict create schema fields; do not add API fields.
- The page uses the existing same-origin CSRF-aware `management_fetch.js` loaded by `console_base.html`.
- Preserve all unrelated dirty-worktree changes.
- Do not commit or push unless Git write access is available and the user has authorized it.

---

## File Structure

- Modify `gateway/routes_console.py`: register the one native create route.
- Modify `gateway/templates/console_actions.html`: change only the top create anchor.
- Create `gateway/templates/console_comment_campaign_create.html`: own the create form's stable DOM contract.
- Create `gateway/static/console_comment_campaign_create.js`: own loading, preview, local draft, validation, rendering, and create submission.
- Create `gateway/static/console_comment_campaign_create.css`: own page-specific form, Profile table, status, and responsive rules.
- Modify `tests/test_console_pages.py`: verify route, shell, native entry, and absence of legacy workbench content.
- Create `tests-js/console-comment-campaign-create.test.js`: verify the page controller without a live browser.

No shared Campaign core is introduced. The new controller is intentionally page-specific so this fix does not refactor the legacy workbench.

### Task 1: Add the native route, page contract, and Action Library entry

**Files:**
- Modify: `gateway/routes_console.py:70-100`
- Modify: `gateway/templates/console_actions.html:7`
- Create: `gateway/templates/console_comment_campaign_create.html`
- Modify: `tests/test_console_pages.py:50-75`

**Interfaces:**
- Consumes: `console._render(template: str, active_nav: str, **context)` and the shared `console_base.html` shell.
- Produces: endpoint `console.new_comment_campaign` at `/console/actions/comment-campaigns/new` and stable DOM IDs consumed by Task 2.

- [ ] **Step 1: Write failing Flask route and link tests**

Add these tests beside the existing Action Library entry tests:

```python
def test_console_actions_new_comment_campaign_uses_native_create_route(client):
    html = client.get("/console/actions").get_data(as_text=True)

    assert 'href="/console/actions/comment-campaigns/new"' in html
    assert '>新建评论 Campaign</a>' in html


def test_console_comment_campaign_create_is_native_console_page(client):
    response = client.get("/console/actions/comment-campaigns/new")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="console-comment-campaign-create"' in html
    assert 'class="dashboard-sidebar"' in html
    assert 'name="csrf-token"' in html
    assert 'href="/console/actions"' in html
    assert "返回动作库" in html
    assert "创建 Campaign" in html
    assert html.count('aria-current="page"') == 1
    active_start = html.rfind("<a", 0, html.index(">动作库</a>") + len(">动作库</a>"))
    active_tag = html[active_start:html.index(">", active_start)]
    assert 'aria-current="page"' in active_tag
    for legacy_marker in (
        'id="comment-campaign-app"',
        'id="comment-campaign-list"',
        'id="comment-campaign-preview"',
        'id="comment-campaign-approvals"',
        'id="campaign-drawer"',
    ):
        assert legacy_marker not in html
```

- [ ] **Step 2: Run the focused Flask tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py::test_console_actions_new_comment_campaign_uses_native_create_route tests/test_console_pages.py::test_console_comment_campaign_create_is_native_console_page -q -p no:cacheprovider
```

Expected: the link assertion fails because it still uses `/comment-campaigns`, and the page test fails with HTTP 404.

- [ ] **Step 3: Add the single Console route**

Add this route beside the browser-strategy child routes:

```python
@bp.get("/actions/comment-campaigns/new")
def new_comment_campaign():
    return _render(
        "console_comment_campaign_create.html",
        "action-library",
    )
```

Do not add a Campaign ID or maintenance route.

- [ ] **Step 4: Add the stable Jinja form contract**

Create `gateway/templates/console_comment_campaign_create.html` with this structure:

```html
{% extends 'console_base.html' %}
{% block title %}新建评论 Campaign · 动作库{% endblock %}
{% block content %}
<div id="console-comment-campaign-create" class="console-page" data-state="loading">
  <a class="console-back-button" href="{{ url_for('console.actions') }}">← 返回动作库</a>
  <header class="console-page-head campaign-create-head">
    <div>
      <h1>新建评论 Campaign</h1>
      <p>创建一个使用现有评论树和本机 Profile 的调试 Campaign。</p>
    </div>
    <button id="campaign-create-submit" class="console-button primary" type="submit" form="campaign-create-form">创建 Campaign</button>
  </header>

  <form id="campaign-create-form" novalidate>
    <section class="console-section" aria-labelledby="campaign-basic-title">
      <div class="console-section-head"><div><h2 id="campaign-basic-title">基本信息</h2></div></div>
      <div class="console-form-grid campaign-create-grid">
        <label class="wide"><span>Campaign 名称</span><input id="campaign-name" class="console-control" name="name" maxlength="100" autocomplete="off" required></label>
        <label class="wide"><span>TikTok 视频链接</span><input id="campaign-target-reference" class="console-control" name="target_reference" type="url" autocomplete="off" required></label>
      </div>
    </section>

    <section class="console-section" aria-labelledby="campaign-config-title">
      <div class="console-section-head"><div><h2 id="campaign-config-title">评论配置</h2></div></div>
      <div class="console-form-grid campaign-create-grid">
        <label><span>评论模式</span><select id="campaign-mode" class="console-control" name="mode"><option value="independent">独立评论</option><option value="threaded">盖楼回复</option></select></label>
        <label><span>评论树</span><select id="campaign-template" class="console-control" name="template_id"><option value="">请选择评论树</option></select></label>
        <label><span>每批数量</span><input id="campaign-batch-size" class="console-control" name="batch_size" type="number" min="1" max="8" step="1" value="3"></label>
      </div>
    </section>

    <section class="console-section" aria-labelledby="campaign-profile-title">
      <div class="console-section-head"><div><h2 id="campaign-profile-title">Profile 分配</h2></div></div>
      <fieldset id="campaign-selection-mode" class="campaign-selection-mode">
        <legend>选择方式</legend>
        <label><input id="campaign-selection-automatic" name="selection_mode" type="radio" value="automatic" checked> 自动选择</label>
        <label><input id="campaign-selection-manual" name="selection_mode" type="radio" value="manual"> 手动选择</label>
      </fieldset>
      <div id="campaign-selection-summary" class="campaign-selection-summary" aria-live="polite">
        <span>需要 <strong id="campaign-required-count">—</strong></span>
        <span>可用 <strong id="campaign-eligible-count">—</strong></span>
        <span>已选 <strong id="campaign-selected-count">0</strong></span>
        <span>缺少 <strong id="campaign-shortage-count">—</strong></span>
      </div>
      <label id="campaign-profile-search-wrap" hidden><span>搜索 Profile</span><input id="campaign-profile-search" class="console-control search" type="search" autocomplete="off"></label>
      <div id="campaign-profile-table-wrap" class="console-table-wrap" hidden>
        <table class="console-dense-table"><thead><tr><th class="action">选择</th><th>Profile</th><th>语言/地区</th><th>状态</th></tr></thead><tbody id="campaign-profile-body"></tbody></table>
      </div>
      <p id="campaign-profile-empty" class="console-empty" hidden>暂无可选择的 Profile。</p>
    </section>
  </form>

  <p id="campaign-create-status" class="console-status" role="status" aria-live="polite"></p>
</div>
{% endblock %}
```

Do not add old workbench scripts, drawers, Campaign lists, planning controls, or polling markers.

- [ ] **Step 5: Change only the Action Library create anchor**

Replace the hard-coded create URL with:

```html
<a class="console-button" href="{{ url_for('console.new_comment_campaign') }}">新建评论 Campaign</a>
```

Do not edit `gateway/static/console_actions.js`; its existing Campaign maintenance URL remains `/comment-campaigns`.

- [ ] **Step 6: Run the focused Flask tests and verify GREEN**

Run the command from Step 2.

Expected: `2 passed`.

- [ ] **Step 7: Commit the route contract when Git writes are available**

```powershell
git add gateway/routes_console.py gateway/templates/console_actions.html gateway/templates/console_comment_campaign_create.html tests/test_console_pages.py
git commit -m "feat: add console campaign create route"
```

### Task 2: Implement dependency loading and Profile selection

**Files:**
- Create: `gateway/static/console_comment_campaign_create.js`
- Create: `tests-js/console-comment-campaign-create.test.js`
- Modify: `gateway/templates/console_comment_campaign_create.html`

**Interfaces:**
- Consumes: existing envelopes from `GET /api/browser-v2/comment-templates`, `GET /api/browser-v2/comment-profile-metadata`, and `POST /api/browser-v2/comment-profile-selection/preview`.
- Produces: module exports `createConsoleCommentCampaignCreate`, `validateDraft`, `buildCreatePayload`, and `boot`; controller methods `init`, `render`, `updateDraft`, `setProfileQuery`, `toggleProfile`, `refreshSelectionPreview`, and `submit`.

- [ ] **Step 1: Create the controller harness and failing initialization tests**

Create `tests-js/console-comment-campaign-create.test.js` with a request harness that records exact calls:

```js
"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const page = require("../gateway/static/console_comment_campaign_create");

function response(status, body) { return {status, data: body}; }

function harness(overrides = {}) {
  const requests = [];
  const renders = [];
  const assigned = [];
  const controller = page.createConsoleCommentCampaignCreate({
    requestJson: async (url, method = "GET", body) => {
      requests.push({url, method, body});
      const value = overrides.responses?.[`${method} ${url}`];
      return typeof value === "function" ? value(body) : value;
    },
    location: {assign: (url) => assigned.push(url)},
    render: (state, model) => renders.push({
      state: structuredClone(state),
      model: structuredClone(model),
    }),
  });
  return {controller, requests, renders, assigned};
}

const templates = [{id: "tree-1", name: "评论树", revision: 7, enabled: true, supported_modes: ["independent", "threaded"]}];
const profiles = [{profile_ref: "opaque-a", display_profile: "窗口 A", enabled: true, health_status: "healthy", language: "zh", region: "CN"}];

test("initialization reads only templates and cached Profiles without polling or sync", async () => {
  const {controller, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
  }});

  await controller.init();

  assert.deepEqual(requests, [
    {url: "/api/browser-v2/comment-templates", method: "GET", body: undefined},
    {url: "/api/browser-v2/comment-profile-metadata", method: "GET", body: undefined},
  ]);
  assert.deepEqual(controller.state.templates, templates);
  assert.deepEqual(controller.state.profiles, profiles);
  assert.equal(controller.state.initialized, true);
});
```

Add these concrete tests. The injected `render(state, model)` receives a safe view model whose `templates` list is already filtered for the selected mode:

```js
test("templates filter disabled and incompatible records", async () => {
  const records = [
    {id: "independent", name: "独立", revision: 1, enabled: true, supported_modes: ["independent"]},
    {id: "threaded", name: "盖楼", revision: 2, enabled: true, supported_modes: ["threaded"]},
    {id: "disabled", name: "停用", revision: 3, enabled: false, supported_modes: ["independent"]},
  ];
  const {controller, renders} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [], meta: {stale: false}}),
  }});

  await controller.init();

  assert.deepEqual(renders.at(-1).model.templates.map((item) => item.id), ["independent"]);
});

test("automatic preview applies opaque refs and exact template revision", async () => {
  const {controller, requests} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {
      required_count: 1, eligible_count: 1,
      profiles: [{profile_ref: "opaque-a", display_profile: "窗口 A"}],
    }}),
  }});
  await controller.init();

  await controller.updateDraft("template_id", "tree-1");

  assert.deepEqual(requests.at(-1), {
    url: "/api/browser-v2/comment-profile-selection/preview", method: "POST",
    body: {template_id: "tree-1", template_revision: 7, mode: "independent"},
  });
  assert.deepEqual(controller.state.draft.profile_refs, ["opaque-a"]);
  assert.equal(controller.state.preview.requiredCount, 1);
});

test("manual selection preserves the operator candidate pool", async () => {
  const {controller} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: profiles, meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {
      required_count: 1, eligible_count: 1,
      profiles: [{profile_ref: "opaque-a", display_profile: "窗口 A"}],
    }}),
  }});
  await controller.init();
  await controller.updateDraft("selection_mode", "manual");
  await controller.updateDraft("template_id", "tree-1");

  controller.toggleProfile("opaque-a", true);

  assert.deepEqual(controller.state.draft.profile_refs, ["opaque-a"]);
  assert.equal(controller.state.preview.requiredCount, 1);
});

test("manual Profile search exposes only safe visible fields", async () => {
  const unsafeProfiles = [{
    profile_ref: "opaque-secret", display_profile: "窗口 A", language: "zh", region: "CN",
    enabled: true, health_status: "healthy", expected_username: "must-not-render", raw_id: "raw-secret",
  }];
  const {controller, renders} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: templates}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: unsafeProfiles, meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": response(200, {data: {required_count: 1, eligible_count: 1, profiles: []}}),
  }});
  await controller.init();
  await controller.updateDraft("selection_mode", "manual");
  controller.setProfileQuery("窗口");

  const row = renders.at(-1).model.profileRows[0];
  assert.deepEqual({display: row.display, locale: row.locale, status: row.status}, {
    display: "窗口 A", locale: "zh / CN", status: "可用",
  });
  assert.equal(JSON.stringify([row.display, row.locale, row.status]).includes("opaque-secret"), false);
  assert.equal(JSON.stringify([row.display, row.locale, row.status]).includes("must-not-render"), false);
  assert.equal(JSON.stringify([row.display, row.locale, row.status]).includes("raw-secret"), false);
});

test("stale preview cannot overwrite a newer selection", async () => {
  let releaseOld;
  const oldPreview = new Promise((resolve) => { releaseOld = resolve; });
  const records = [
    {id: "old", name: "旧树", revision: 1, enabled: true, supported_modes: ["independent"]},
    {id: "new", name: "新树", revision: 2, enabled: true, supported_modes: ["independent"]},
  ];
  const {controller} = harness({responses: {
    "GET /api/browser-v2/comment-templates": response(200, {data: records}),
    "GET /api/browser-v2/comment-profile-metadata": response(200, {data: [
      {profile_ref: "old-ref", display_profile: "旧窗口"},
      {profile_ref: "new-ref", display_profile: "新窗口"},
    ], meta: {stale: false}}),
    "POST /api/browser-v2/comment-profile-selection/preview": (body) => body.template_id === "old"
      ? oldPreview
      : response(200, {data: {required_count: 1, eligible_count: 1, profiles: [{profile_ref: "new-ref", display_profile: "新窗口"}]}}),
  }});
  await controller.init();

  const stale = controller.updateDraft("template_id", "old");
  await controller.updateDraft("template_id", "new");
  releaseOld(response(200, {data: {required_count: 1, eligible_count: 1, profiles: [{profile_ref: "old-ref", display_profile: "旧窗口"}]}}));
  await stale;

  assert.deepEqual(controller.state.draft.profile_refs, ["new-ref"]);
  assert.equal(controller.state.preview.inputKey, "new:2:independent:automatic");
});
```

- [ ] **Step 2: Run the focused Node tests and verify RED**

Run:

```powershell
node --test --test-name-pattern="initialization|templates|manual|automatic|stale preview" tests-js/console-comment-campaign-create.test.js
```

Expected: FAIL because `gateway/static/console_comment_campaign_create.js` does not exist.

- [ ] **Step 3: Implement the page-specific controller boundary**

Create a UMD/CommonJS module with this public shape:

```js
(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ConsoleCommentCampaignCreate = api;
  if (root?.document) {
    if (root.document.readyState === "loading") {
      root.document.addEventListener("DOMContentLoaded", () => api.boot(root), {once: true});
    } else {
      api.boot(root);
    }
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";

  const API = "/api/browser-v2";
  const EMPTY_DRAFT = Object.freeze({
    name: "", mode: "independent", target_reference: "",
    template_id: "", template_revision: null,
    selection_mode: "automatic", profile_refs: [], batch_size: "3",
  });

  function createConsoleCommentCampaignCreate(options) {
    const opts = options || {};
    const state = {
      draft: {...EMPTY_DRAFT, profile_refs: []}, templates: [], profiles: [],
      profileMeta: {stale: false, last_synced_at: null, safe_reason: null},
      profileQuery: "",
      preview: {version: 0, inputKey: "", status: "idle", requiredCount: 0, eligibleCount: 0, error: ""},
      fieldErrors: {}, loading: false, submitting: false, initialized: false, error: "",
    };

    return {state, init, render, updateDraft, setProfileQuery, toggleProfile, refreshSelectionPreview, submit};
  }

  return {createConsoleCommentCampaignCreate, validateDraft, buildCreatePayload, boot};
});
```

Implement these exact internal rules:

```js
function compatibleTemplate(template, mode) {
  return Boolean(template && template.enabled !== false &&
    Array.isArray(template.supported_modes) && template.supported_modes.includes(mode));
}

function previewKey(draft) {
  return [draft.template_id, draft.template_revision || "", draft.mode, draft.selection_mode].join(":");
}
```

- `init()` uses one `Promise.all` for the two GET requests and never calls sync, Campaign list, health, settings, or timers.
- `updateDraft(field, value)` is always async and returns `Promise<boolean>`.
- `updateDraft("mode", value)` and `updateDraft("template_id", value)` clear `profile_refs`, update `template_revision` from the selected template, invalidate the preview, render, and await a fresh preview when a compatible template exists.
- `updateDraft("selection_mode", value)` accepts only `automatic` or `manual`, clears `profile_refs`, invalidates the preview, and awaits a fresh preview.
- Both automatic and manual modes call preview because the template list does not reliably expose the required count.
- Automatic preview replaces `profile_refs` with the returned opaque references.
- Manual preview updates counts but never replaces manually selected references.
- A preview response applies only when both its numeric version and `previewKey(state.draft)` equal the values captured before the request.
- `setProfileQuery(value)` stores a trimmed, case-insensitive query and renders Profile rows filtered by `display_profile`, `language`, and `region` only.
- `toggleProfile(profileRef, checked)` accepts only a reference present in `state.profiles`, keeps references unique, and works only in manual mode.
- The injected render view model contains `templates`, `profileRows`, and selection counts. Each Profile row has `{key, display, locale, status, checked}`; `key` is used only by the checkbox event closure. Visible cells use only `display`, `locale`, and `status`.
- The rendered Profile table never writes `profile_ref`, `expected_username`, raw IDs, or credentials into visible text.

- [ ] **Step 4: Add the browser request adapter and script load**

The browser adapter must return `{status, data}` and rely on the already wrapped `window.fetch`:

```js
async function requestJson(win, url, method, body) {
  const verb = String(method || "GET").toUpperCase();
  const options = {method: verb, credentials: "same-origin"};
  if (body !== undefined && verb !== "GET" && verb !== "HEAD") {
    options.headers = {"Content-Type": "application/json"};
    options.body = JSON.stringify(body);
  }
  const response = await win.fetch(url, options);
  let data;
  try { data = await response.json(); }
  catch (_) { data = {error: {code: "invalid_response", message: "服务返回格式无效"}}; }
  return {status: response.status, data};
}
```

Add this block to the template:

```html
{% block scripts %}
<script defer src="{{ url_for('static', filename='console_comment_campaign_create.js') }}"></script>
{% endblock %}
```

- [ ] **Step 5: Run the focused Task 2 tests and verify GREEN**

Run the command from Step 2.

Expected: every selected test passes.

- [ ] **Step 6: Commit loading and selection when Git writes are available**

```powershell
git add gateway/static/console_comment_campaign_create.js gateway/templates/console_comment_campaign_create.html tests-js/console-comment-campaign-create.test.js
git commit -m "feat: load campaign create dependencies"
```

### Task 3: Add strict creation, errors, layout, and regression coverage

**Files:**
- Modify: `gateway/static/console_comment_campaign_create.js`
- Create: `gateway/static/console_comment_campaign_create.css`
- Modify: `gateway/templates/console_comment_campaign_create.html`
- Modify: `tests-js/console-comment-campaign-create.test.js`
- Modify: `tests/test_console_pages.py`

**Interfaces:**
- Consumes: the Task 2 state and preview lifecycle.
- Produces: `validateDraft(draft, context) -> Record<string, string>`, `buildCreatePayload(draft) -> CampaignCreate`, and `submit() -> Promise<boolean>`.

- [ ] **Step 1: Add failing validation, payload, submission, and draft-preservation tests**

Add the following Task 3 tests:

```js
test("validation rejects an invalid draft without sending a create request", async () => {
  const {controller, requests} = harness({responses: {}});
  controller.state.initialized = true;
  controller.state.draft = {...controller.state.draft, name: "", target_reference: "http://example.test/video/1", batch_size: "9"};

  assert.equal(await controller.submit(), false);
  assert.equal(requests.length, 0);
  assert.ok(controller.state.fieldErrors.name);
  assert.ok(controller.state.fieldErrors.target_reference);
  assert.ok(controller.state.fieldErrors.batch_size);
});

test("payload contains only strict Campaign create fields", () => {
  assert.deepEqual(page.buildCreatePayload({
    name: "  Summer thread  ", mode: "threaded",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1", template_revision: 7,
    profile_refs: ["opaque-a", "opaque-b"], batch_size: "3",
    selection_mode: "manual", ignored: "must-not-leak",
  }), {
    name: "Summer thread", mode: "threaded",
    target_source: "manual_url",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1", template_revision: 7,
    profile_refs: ["opaque-a", "opaque-b"], batch_size: 3,
    start_mode: "manual",
  });
});

test("submit sends one request and navigation occurs only after 201", async () => {
  const {controller, requests, assigned} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": response(201, {data: {id: "c1"}}),
  }});
  Object.assign(controller.state, {
    initialized: true,
    templates,
    preview: {version: 1, inputKey: "tree-1:7:independent:manual", status: "ready", requiredCount: 1, eligibleCount: 1, error: ""},
  });
  controller.state.draft = {
    name: "Campaign", mode: "independent",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1", template_revision: 7,
    selection_mode: "manual", profile_refs: ["opaque-a"], batch_size: "3",
  };

  const first = controller.submit();
  const second = controller.submit();
  assert.equal(await second, false);
  assert.equal(await first, true);
  assert.equal(requests.filter((item) => item.method === "POST").length, 1);
  assert.deepEqual(assigned, ["/console/actions"]);
});
```

Add exact pure-validation cases using one valid baseline:

```js
function validDraft() {
  return {
    name: "Campaign", mode: "independent",
    target_reference: "https://www.tiktok.com/@creator/video/12345678",
    template_id: "tree-1", template_revision: 7,
    selection_mode: "manual", profile_refs: ["opaque-a"], batch_size: "3",
  };
}

function validContext() {
  return {
    templates,
    profiles,
    preview: {status: "ready", inputKey: "tree-1:7:independent:manual", requiredCount: 1, eligibleCount: 1},
  };
}

for (const [label, mutate, field] of [
  ["blank name", (draft) => { draft.name = " "; }, "name"],
  ["long name", (draft) => { draft.name = "x".repeat(101); }, "name"],
  ["non HTTPS URL", (draft) => { draft.target_reference = "http://www.tiktok.com/@a/video/12345678"; }, "target_reference"],
  ["non TikTok URL", (draft) => { draft.target_reference = "https://example.test/@a/video/12345678"; }, "target_reference"],
  ["non video URL", (draft) => { draft.target_reference = "https://www.tiktok.com/@creator"; }, "target_reference"],
  ["batch decimal", (draft) => { draft.batch_size = "2.5"; }, "batch_size"],
  ["batch too small", (draft) => { draft.batch_size = "0"; }, "batch_size"],
  ["batch too large", (draft) => { draft.batch_size = "9"; }, "batch_size"],
  ["duplicate Profile", (draft) => { draft.profile_refs = ["opaque-a", "opaque-a"]; }, "profile_refs"],
  ["unknown Profile", (draft) => { draft.profile_refs = ["missing"]; }, "profile_refs"],
]) {
  test(`validation rejects ${label}`, () => {
    const draft = validDraft();
    mutate(draft);
    const errors = page.validateDraft(draft, validContext());
    assert.ok(errors[field]);
  });
}

for (const [label, mutate] of [
  ["missing template", (context) => { context.templates = []; }],
  ["disabled template", (context) => { context.templates[0] = {...context.templates[0], enabled: false}; }],
  ["revision mismatch", (context) => { context.templates[0] = {...context.templates[0], revision: 8}; }],
  ["mode mismatch", (context) => { context.templates[0] = {...context.templates[0], supported_modes: ["threaded"]}; }],
  ["preview loading", (context) => { context.preview.status = "loading"; }],
  ["preview error", (context) => { context.preview.status = "error"; }],
  ["preview key mismatch", (context) => { context.preview.inputKey = "old"; }],
  ["Profile shortage", (context) => { context.preview.requiredCount = 2; }],
]) {
  test(`validation rejects ${label}`, () => {
    const context = validContext();
    mutate(context);
    const errors = page.validateDraft(validDraft(), context);
    assert.ok(errors.template_id || errors.profile_refs || errors.preview);
  });
}
```

Add exact draft-preservation cases for API and transport failures:

```js
for (const [label, result] of [
  ["403 error", response(403, {error: {code: "forbidden", message: "无权操作"}})],
  ["422 error", response(422, {error: {code: "allocation_unsatisfied", message: "候选 Profile 不足"}})],
  ["503 error", response(503, {error: {code: "runtime_unavailable", message: "服务不可用"}})],
  ["invalid response body", response(503, {})],
]) {
  test(`draft survives ${label}`, async () => {
    const {controller, assigned} = harness({responses: {"POST /api/browser-v2/comment-campaigns": result}});
    controller.state.initialized = true;
    controller.state.templates = templates;
    controller.state.profiles = profiles;
    controller.state.preview = validContext().preview;
    controller.state.draft = validDraft();
    const before = structuredClone(controller.state.draft);

    assert.equal(await controller.submit(), false);
    assert.deepEqual(controller.state.draft, before);
    assert.deepEqual(assigned, []);
  });
}

test("draft survives network error", async () => {
  const {controller, assigned} = harness({responses: {
    "POST /api/browser-v2/comment-campaigns": () => { throw new Error("offline"); },
  }});
  controller.state.initialized = true;
  controller.state.templates = templates;
  controller.state.profiles = profiles;
  controller.state.preview = validContext().preview;
  controller.state.draft = validDraft();
  const before = structuredClone(controller.state.draft);

  assert.equal(await controller.submit(), false);
  assert.deepEqual(controller.state.draft, before);
  assert.deepEqual(assigned, []);
});
```

- [ ] **Step 2: Run the focused Task 3 Node tests and verify RED**

Run:

```powershell
node --test --test-name-pattern="validation|payload|submit|navigation|draft|error" tests-js/console-comment-campaign-create.test.js
```

Expected: FAIL because validation and submission are not complete.

- [ ] **Step 3: Implement exact validation and payload construction**

Use these rules:

```js
function isDirectTikTokVideoUrl(value) {
  try {
    const parsed = new URL(String(value || ""));
    return parsed.protocol === "https:" &&
      ["tiktok.com", "www.tiktok.com"].includes(parsed.hostname.toLowerCase()) &&
      /^\/@[^/]+\/video\/\d{8,30}\/?$/.test(parsed.pathname);
  } catch (_) {
    return false;
  }
}

function buildCreatePayload(draft) {
  return {
    name: String(draft.name || "").trim(),
    mode: draft.mode,
    target_source: "manual_url",
    target_reference: String(draft.target_reference || "").trim(),
    template_id: String(draft.template_id || ""),
    template_revision: Number(draft.template_revision),
    profile_refs: Array.isArray(draft.profile_refs) ? draft.profile_refs.slice() : [],
    batch_size: Number(draft.batch_size),
    start_mode: "manual",
  };
}
```

`validateDraft(draft, context)` must return a plain field-error object and enforce:

- trimmed name length 1 through 100;
- `isDirectTikTokVideoUrl`;
- mode is `independent` or `threaded`;
- template exists, is enabled, includes the mode, and has the exact selected revision;
- batch size is an integer from 1 through 8;
- each Profile reference is a unique string of length 1 through 80 and exists in loaded Profile metadata;
- preview status is `ready` and preview input key matches the current draft;
- selected count is at least `preview.requiredCount`.

- [ ] **Step 4: Implement submission and error mapping**

`submit()` follows this sequence exactly:

1. Return `false` immediately when `state.submitting` is true.
2. Validate without mutating the draft.
3. Render field errors and return `false` without a POST when invalid.
4. Set `state.submitting = true`, clear the page error, and render.
5. POST `buildCreatePayload(state.draft)` to `/api/browser-v2/comment-campaigns`.
6. On status `201`, call `location.assign("/console/actions")` and return `true`.
7. On other statuses, map `error.message`, then `error.code`, then the status-specific fallback.
8. On a thrown error, display `请求失败，请重试。`.
9. In `finally`, set `state.submitting = false` and render without changing `state.draft`.

Use fallbacks:

```js
const ERROR_FALLBACK = {
  403: "当前会话无权创建 Campaign。",
  422: "Campaign 配置无效，请检查表单。",
  503: "Campaign 服务暂不可用，请稍后重试。",
};
```

- [ ] **Step 5: Complete DOM binding and safe rendering**

Bind once to the stable IDs from Task 1:

- form submit -> `submit()`;
- name, target, mode, template, and batch inputs -> `updateDraft`;
- selection radios -> `updateDraft("selection_mode", value)`;
- Profile search -> `state.profileQuery` then render;
- Profile checkboxes -> `toggleProfile`.

Rendering requirements:

- update controls with values, loading, and submitting state;
- replace template options using only compatible enabled templates;
- display preview counts and shortage;
- show the manual table only in manual mode;
- build visible Profile text only from `display_profile`, `language`, `region`, `enabled`, and `health_status`;
- assign opaque `profile_ref` only to event closures or non-visible in-memory state, not `textContent`, labels, titles, dataset fields, or URLs;
- use `textContent` and DOM properties, never `innerHTML`.

- [ ] **Step 6: Add the page-specific CSS and stylesheet block**

Create `gateway/static/console_comment_campaign_create.css`:

```css
.campaign-create-head { align-items: flex-start; }
.campaign-create-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
.campaign-create-grid .wide { grid-column: span 2; }
.campaign-selection-mode { display: flex; flex-wrap: wrap; gap: 12px; margin: 0 0 14px; padding: 0; border: 0; }
.campaign-selection-mode legend { width: 100%; margin-bottom: 6px; color: var(--console-muted); font-size: 12px; }
.campaign-selection-mode label { display: inline-flex; align-items: center; gap: 6px; }
.campaign-selection-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-bottom: 14px; }
.campaign-selection-summary span { padding: 10px 12px; border: 1px solid var(--console-line); border-radius: 8px; background: var(--console-soft); color: var(--console-muted); }
.campaign-selection-summary strong { display: block; margin-top: 3px; color: var(--console-text); font-size: 18px; }
#campaign-profile-search-wrap { display: grid; gap: 5px; max-width: 360px; margin-bottom: 10px; }
#campaign-profile-body td:first-child { width: 56px; text-align: center; }
#console-comment-campaign-create[data-state="loading"] #campaign-create-form { opacity: .6; pointer-events: none; }
#campaign-create-status.error { color: var(--console-danger, #b42318); }
@media (max-width: 900px) {
  .campaign-create-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .campaign-selection-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 560px) {
  .campaign-create-grid, .campaign-selection-summary { grid-template-columns: 1fr; }
  .campaign-create-grid .wide { grid-column: auto; }
}
```

Add to the template:

```html
{% block styles %}
<link rel="stylesheet" href="{{ url_for('static', filename='console_comment_campaign_create.css') }}">
{% endblock %}
```

- [ ] **Step 7: Extend the Flask layout test**

Add assertions to `test_console_comment_campaign_create_is_native_console_page`:

```python
assert "console_comment_campaign_create.css" in html
assert "console_comment_campaign_create.js" in html
for marker in (
    'id="campaign-create-form"',
    'id="campaign-name"',
    'id="campaign-target-reference"',
    'id="campaign-mode"',
    'id="campaign-template"',
    'id="campaign-batch-size"',
    'id="campaign-profile-body"',
    'id="campaign-create-status"',
):
    assert marker in html
```

- [ ] **Step 8: Run focused Node and Flask tests**

Run:

```powershell
node --test tests-js/console-comment-campaign-create.test.js
```

Expected: all create-page controller tests pass.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py -q -p no:cacheprovider
```

Expected: all Console page tests pass.

- [ ] **Step 9: Run focused regressions**

Run:

```powershell
node --test tests-js/console-comment-campaign-create.test.js tests-js/comment-campaign-ui.test.js tests-js/console-actions.test.js tests-js/console-browser-strategy-editor.test.js
```

Expected: all selected Node tests pass, including the existing assertion that Campaign maintenance remains `/comment-campaigns`.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_console_pages.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_integration.py -q -p no:cacheprovider
```

Expected: all selected Python tests pass.

- [ ] **Step 10: Verify source scope**

Run:

```powershell
git diff -- gateway/routes_console.py gateway/templates/console_actions.html gateway/templates/console_comment_campaign_create.html gateway/static/console_comment_campaign_create.js gateway/static/console_comment_campaign_create.css tests/test_console_pages.py tests-js/console-comment-campaign-create.test.js
```

Expected: only the native create route, create link, new create page assets, and their tests appear. Confirm separately that these files have no diff from this task:

```powershell
git diff -- gateway/static/console_actions.js gateway/static/comment_campaign.js gateway/templates/comment_campaign.html comment_campaign
```

Expected: no task-authored changes in the protected legacy/backend files. If the working tree already contained changes there, compare against the pre-task hashes or recorded baseline rather than reverting them.

- [ ] **Step 11: Perform browser verification**

Using the existing local application:

1. Open `/console/actions` and click “新建评论 Campaign”.
2. Confirm the URL is `/console/actions/comment-campaigns/new` and the Console shell remains visible.
3. Confirm no Campaign list, approval panel, drawer, or old workbench marker appears.
4. Verify automatic selection updates counts and selects the previewed Profiles.
5. Verify manual mode shows a searchable Profile table and preserves selections after a failed submit.
6. Create a valid Campaign and confirm navigation to `/console/actions`.
7. Confirm an existing Campaign row still opens `/comment-campaigns` for maintenance.
8. Check the form at desktop and narrow widths.

- [ ] **Step 12: Request final Sol review**

Delegate a read-only final review with `model: gpt-5.6-sol` and `reasoning_effort: high`. Require exact file/line findings for spec compliance, security, stale-preview handling, strict payload construction, draft preservation, and scope containment. If a blocking issue is found, fix only that issue, rerun Steps 8 and 9, and repeat the Sol review.

- [ ] **Step 13: Commit final implementation when Git writes are available**

```powershell
git add gateway/routes_console.py gateway/templates/console_actions.html gateway/templates/console_comment_campaign_create.html gateway/static/console_comment_campaign_create.js gateway/static/console_comment_campaign_create.css tests/test_console_pages.py tests-js/console-comment-campaign-create.test.js docs/superpowers/specs/2026-08-20-console-comment-campaign-create-design.md docs/superpowers/plans/2026-08-20-console-comment-campaign-create.md
git commit -m "feat: add console campaign create page"
```
