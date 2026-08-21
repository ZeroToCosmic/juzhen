# Console Settings Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将系统设置完整迁入 Console UI，保留旧配置字段、密钥保持、代理池、模型预设、备份恢复和 TikTok Cookie 行为。

**Architecture:** 新建可测试的设置控制器作为配置序列化和交互的唯一前端契约，新 Console 页面直接使用该控制器。旧根控制台在新页面验收后通过薄包装接入同一控制器；独立 `/settings` 页面保留为兼容回退，不创建第二份配置 Store。

**Tech Stack:** Flask、Jinja2、vanilla JavaScript UMD、CSS、pytest、Node test runner、现有 settings/tiktok-stats API。

## Global Constraints

- 配置继续由 `/api/settings` 和现有 Store 管理；不得直接读写 `config.json`。
- `_secrets_configured` 不得回传，任何密钥不得进入 DOM 文本、错误或测试快照。
- 密钥输入留空表示保持原值，不填充掩码字符串。
- 配置读取失败时禁止保存，避免空表单覆盖现有配置。
- 健康状态读取失败不阻止配置展示，但保存前必须确认配置源读取成功。
- 只提交发生修改的顶层分区；模型分区修改时提交完整 `models.items`。
- 不展示 Selector Probe 设置；后端 409 所有权保护继续生效。
- 账号级 Buffer Token、Profile 和代理分配不迁入系统设置。
- 不使用 iframe，不复制配置存储。

---

### Task 1: 建立可测试的配置序列化契约

**Files:**
- Create: `gateway/static/console_settings.js`
- Create: `tests-js/console-settings.test.js`

**Interfaces:**
- Produces: `serializeDirtySections(form, loadedSettings, dirtySections)`。
- Produces: `mergeModelDrafts(loadedItems, drafts)`。
- Produces: `secretConfigured(settings, path)`。
- Produces: `createConsoleSettingsController(options)`。

- [ ] **Step 1: 写类型、分区和密钥失败测试**

```javascript
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../gateway/static/console_settings.js");

function fakeForm(values) {
  return {
    elements: Object.entries(values).map(([name, value]) => ({name, value})),
  };
}

test("serialization sends only dirty top-level sections with typed values", () => {
  const form = fakeForm({
    "timeouts.buffer_publish_seconds": "45",
    "publish_queue.interval_seconds": "12",
    "publish_sampling.enabled": "false",
    "proxy.password": "",
  });
  const payload = ui.serializeDirtySections(form, {
    timeouts: {ip_check_seconds: 10, buffer_publish_seconds: 30},
    publish_queue: {interval_seconds: 8},
    publish_sampling: {enabled: true, interval_seconds: 300, min_age_hours: 24},
    _secrets_configured: {proxy: {password: true}},
  }, new Set(["timeouts", "publish_queue", "publish_sampling"]));

  assert.deepEqual(payload, {
    timeouts: {ip_check_seconds: 10, buffer_publish_seconds: 45},
    publish_queue: {interval_seconds: 12},
    publish_sampling: {enabled: false, interval_seconds: 300, min_age_hours: 24},
  });
  assert.equal("_secrets_configured" in payload, false);
  assert.equal("proxy" in payload, false);
});

test("blank secret never becomes a mask or explicit clear", () => {
  const payload = ui.serializeDirtySections(
    fakeForm({"r2.account_id": "acct", "r2.secret_access_key": ""}),
    {r2: {account_id: "old", secret_access_key: ""}, _secrets_configured: {r2: {secret_access_key: true}}},
    new Set(["r2"]),
  );

  assert.equal(payload.r2.account_id, "acct");
  assert.equal(payload.r2.secret_access_key, "");
  assert.doesNotMatch(JSON.stringify(payload), /\*\*\*/);
});

test("editing models preserves every model and sends boolean enabled values", () => {
  const items = ui.mergeModelDrafts(
    [{id: "a", api_key: "", enabled: true}, {id: "b", api_key: "", enabled: false}],
    [{id: "a", api_key: "", enabled: "false"}, {id: "b", api_key: "", enabled: "true"}],
  );

  assert.deepEqual(items.map((item) => [item.id, item.enabled]), [["a", false], ["b", true]]);
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node --test tests-js/console-settings.test.js
```

Expected: FAIL，因为模块不存在。

- [ ] **Step 3: 实现字段契约和安全序列化**

模块采用 UMD 导出，浏览器侧挂载 `window.ConsoleSettings`，Node 侧使用 `module.exports`。在模块中定义精确字段集合：

```javascript
const NUMBER_FIELDS = new Set([
  "timeouts.ip_check_seconds",
  "timeouts.buffer_publish_seconds",
  "publish_queue.interval_seconds",
  "publish_sampling.interval_seconds",
  "publish_sampling.min_age_hours",
]);
const BOOLEAN_FIELDS = new Set([
  "publish_sampling.enabled",
]);
const SECRET_FIELDS = new Set([
  "proxy.password",
  "proxy_pool.raw",
  "r2.account_token",
  "r2.access_key_id",
  "r2.secret_access_key",
  "adspower.api_key",
]);
const EDITABLE_TOP_LEVEL = new Set([
  "proxy", "proxy_pool", "services", "timeouts", "publish_queue",
  "publish_sampling", "browser", "adspower", "models", "r2",
]);
```

`serializeDirtySections` 必须从 `loadedSettings` 深拷贝脏分区，再用表单字段覆盖；从返回对象删除 `_secrets_configured`；模型 API Key 由后端按 ID 保持，前端不填掩码。

- [ ] **Step 4: 运行测试确认通过**

```powershell
node --test tests-js/console-settings.test.js
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add gateway/static/console_settings.js tests-js/console-settings.test.js
git commit -m "feat: add settings serialization contract"
```

### Task 2: 实现设置控制器的加载、保存和恢复状态机

**Files:**
- Modify: `gateway/static/console_settings.js`
- Modify: `tests-js/console-settings.test.js`

**Interfaces:**
- Produces: `controller.init()`, `reload()`, `save()`, `restoreLatest()`, `refreshProxyPool(options)`, `saveCookie(value)`, `validateCookie()`, `switchCategory(category)`, `destroy()`, `snapshot()`。
- Consumes: `options.requestJson(url, method, body)`、`options.confirm(message)`、`options.addBeforeUnload(handler)`。

- [ ] **Step 1: 写加载降级、恢复和 Cookie 失败测试**

```javascript
test("settings still load when health is unavailable but save remains guarded", async () => {
  const controller = createController({
    responses: {
      "/api/settings": {status: 200, data: {proxy: {host: "127.0.0.1"}}},
      "/api/settings/status": {status: 503, data: {error: "unavailable"}},
      "/api/model-presets": {status: 200, data: {providers: []}},
      "/api/proxy-pool/status?page=1&page_size=50&search=": {status: 200, data: {total: 0, assigned: 0, remaining: 0, items: []}},
      "/api/tiktok-stats/settings/cookie": {status: 200, data: {configured: true}},
    },
  });

  await controller.init();

  assert.equal(controller.snapshot().loaded, true);
  assert.equal(controller.snapshot().healthKnown, false);
  assert.equal(controller.snapshot().canSave, false);
});

test("restore requires confirmation and reloads every settings dependency", async () => {
  const calls = [];
  const controller = createController({confirm: () => true, calls});

  await controller.restoreLatest();

  assert.equal(calls[0].url, "/api/settings/restore-latest");
  assert.equal(calls[0].method, "POST");
  assert.ok(calls.some((call) => call.url === "/api/model-presets"));
  assert.ok(calls.some((call) => call.url.startsWith("/api/proxy-pool/status?")));
});

test("blank cookie is not submitted and validation posts an empty object", async () => {
  const calls = [];
  const controller = createController({calls});

  assert.equal(await controller.saveCookie(""), false);
  await controller.validateCookie();

  assert.deepEqual(calls.at(-1), {
    url: "/api/tiktok-stats/settings/cookie/validate",
    method: "POST",
    body: {},
  });
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node --test tests-js/console-settings.test.js
```

Expected: FAIL，因为控制器方法尚未实现。

- [ ] **Step 3: 实现控制器工厂和状态**

状态必须包含：

```javascript
const state = {
  loaded: false,
  healthKnown: false,
  canSave: false,
  saving: false,
  settings: {},
  health: {},
  presets: {},
  cookie: {configured: false, valid: null},
  proxyPool: {page: 1, pageCount: 1, total: 0, assigned: 0, remaining: 0, items: []},
  dirtySections: new Set(),
  category: "network",
  message: "",
  error: "",
};
```

`reload()` 使用独立请求结果，不让 health 失败阻止 `/api/settings` 展示。只有 settings 与 health 均成功时 `canSave = true`。`save()` 前再次读取 `/api/settings/status`；失败时保留所有脏状态。

- [ ] **Step 4: 实现离开保护和销毁**

注册的 handler 必须是：

```javascript
function beforeUnload(event) {
  if (!state.dirtySections.size) return undefined;
  event.preventDefault();
  event.returnValue = "";
  return "";
}
```

`destroy()` 移除同一个函数引用，不使用匿名函数。

- [ ] **Step 5: 运行前端及后端契约测试**

```powershell
node --test tests-js/console-settings.test.js
python -m pytest -q -p no:cacheprovider tests/test_settings_routes.py tests/test_settings_store.py tests/test_proxy_pool.py tests/test_model_presets.py tests/test_tiktok_stats_routes.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交本任务**

```powershell
git add gateway/static/console_settings.js tests-js/console-settings.test.js
git commit -m "feat: add settings controller state machine"
```

### Task 3: 建立共享设置工作区和 Console 页面

**Files:**
- Create: `gateway/templates/_console_settings_workspace.html`
- Create: `gateway/templates/console_settings.html`
- Modify: `gateway/routes_console.py`
- Modify: `gateway/static/console.css`
- Modify: `tests/test_console_pages.py`
- Modify: `tests-js/console-settings.test.js`

**Interfaces:**
- Produces root: `[data-console-settings-workspace]`。
- Produces: `GET /console/settings -> 200`。
- Consumes: `window.ConsoleSettings.createConsoleSettingsController`。

- [ ] **Step 1: 写原生 Console 路由失败测试**

```python
def test_console_settings_renders_native_workspace(client):
    response = client.get("/console/settings")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="console-settings"' in html
    assert 'data-console-settings-workspace' in html
    assert "console_settings.js" in html
    assert 'aria-current="page"' in html
    assert "Selector Probe" not in html
    assert response.headers.get("Location") is None
```

- [ ] **Step 2: 运行 Flask 测试确认失败**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py
```

Expected: FAIL，因为 `/console/settings` 仍重定向到 `/?panel=settings`。

- [ ] **Step 3: 创建共享工作区 partial**

工作区必须包含六个分类按钮及同名 panel：

```html
<div class="console-settings-workspace" data-console-settings-workspace>
  <nav class="console-settings-tabs" aria-label="设置分类">
    <button type="button" data-settings-category="network" aria-current="page">代理与网络</button>
    <button type="button" data-settings-category="browser">浏览器与 AdsPower</button>
    <button type="button" data-settings-category="publishing">Buffer 与发布</button>
    <button type="button" data-settings-category="r2">R2 存储</button>
    <button type="button" data-settings-category="collection">数据采集</button>
    <button type="button" data-settings-category="models">模型服务</button>
  </nav>
  <form id="console-settings-form" novalidate>
    <section data-settings-panel="network"></section>
    <section data-settings-panel="browser" hidden></section>
    <section data-settings-panel="publishing" hidden></section>
    <section data-settings-panel="r2" hidden></section>
    <section data-settings-panel="collection" hidden></section>
    <section data-settings-panel="models" hidden></section>
  </form>
  <div class="console-settings-savebar">
    <span id="console-settings-dirty">未修改</span>
    <button id="console-settings-restore" type="button">恢复最近备份</button>
    <button id="console-settings-save" class="console-button primary" type="submit" form="console-settings-form">保存设置</button>
  </div>
</div>
```

各 panel 使用 `name="top_level.path"` 的表单字段。浏览器分类保留 `browser.cdp_url`、`browser.task_goal`、`browser.default_url`、`adspower.base_url`、`adspower.api_key` 和 `adspower.default_group_id`。模型区使用可重复的 `data-model-row`，每条包含 `id/provider/enabled/base_url/api_key/model/mode`；不得只渲染第一条模型。

- [ ] **Step 4: 实现 Console 路由和模板**

```python
@bp.get("/settings")
def settings():
    return _render("console_settings.html", "system-settings")
```

模板继承 `console_base.html`，包含 partial，并只加载 `console_settings.js`。脚本检测 `#console-settings` 后自动装配控制器。

- [ ] **Step 5: 增加统一表单样式**

`console.css` 增加：

```css
.console-settings-tabs { display: flex; gap: 6px; overflow-x: auto; margin-bottom: 14px; }
.console-settings-tabs button[aria-current="page"] { color: #fff; background: var(--console-primary); border-color: var(--console-primary); }
.console-settings-panel { padding: 16px; background: var(--console-surface); border: 1px solid var(--console-line); border-radius: 8px; }
.console-settings-savebar { position: sticky; bottom: 0; display: flex; justify-content: flex-end; align-items: center; gap: 10px; padding: 12px; background: var(--console-surface); border: 1px solid var(--console-line); }
```

- [ ] **Step 6: 运行路由和控制器测试**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py tests/test_settings_routes.py
node --test tests-js/console-settings.test.js
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交本任务**

```powershell
git add gateway/templates/_console_settings_workspace.html gateway/templates/console_settings.html gateway/routes_console.py gateway/static/console.css gateway/static/console_settings.js tests/test_console_pages.py tests-js/console-settings.test.js
git commit -m "feat: migrate settings to console"
```

### Task 4: 让旧根控制台复用设置控制器

**Files:**
- Modify: `gateway/page_templates.py`
- Modify: `gateway/static/console_settings.js`
- Modify: `tests/test_console.py`
- Modify: `tests-js/console-settings.test.js`
- Verify: `gateway/routes_settings.py`

**Interfaces:**
- Produces compatibility wrappers: `loadSettings()` 和 `refreshProxyPoolStatus(options)`。
- Keeps: 独立 `/settings` 的 `SETTINGS_PAGE_HTML` 回退入口。

- [ ] **Step 1: 写旧入口兼容失败测试**

更新 `tests/test_console.py`，不再断言旧内联函数源码，而断言根控制台加载共享控制器和 partial：

```python
def test_legacy_settings_panel_uses_shared_controller(client):
    page = client.get("/?panel=settings").get_data(as_text=True)

    assert "console_settings.js" in page
    assert 'data-console-settings-workspace' in page
    assert "async function saveSettings" not in page
    assert "async function restoreLatestSettings" not in page
    assert client.get("/settings").status_code == 200
```

- [ ] **Step 2: 运行旧控制台测试确认失败**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console.py
```

Expected: FAIL，因为根控制台仍包含旧设置表单和内联函数。

- [ ] **Step 3: 在根控制台包含共享 partial 和脚本**

将 `CONTROL_PAGE_HTML` 的 `panel-settings` 内容替换为：

```jinja2
<section class="panel" id="panel-settings">
  {% include '_console_settings_workspace.html' %}
</section>
```

在内联主脚本之前加载：

```html
<script src="/static/console_settings.js"></script>
```

- [ ] **Step 4: 删除旧设置状态与函数，增加薄包装**

删除 `currentSettings/settingsLoaded`、设置字段集合、`loadSettingsHealth/loadSettings/loadModelPresets/restoreLatestSettings/preserveLoadedModelItems/saveSettings` 的旧实现。创建一次控制器实例：

```javascript
const sharedSettings = window.ConsoleSettings.createConsoleSettingsController({
  root: document.querySelector("#panel-settings"),
  requestJson,
  confirm: (message) => window.confirm(message),
  addBeforeUnload: (handler) => window.addEventListener("beforeunload", handler),
  removeBeforeUnload: (handler) => window.removeEventListener("beforeunload", handler),
});

function loadSettings() {
  return sharedSettings.reload();
}

function refreshProxyPoolStatus(options = {}) {
  return sharedSettings.refreshProxyPool(options);
}
```

旧账号和发布模块继续通过这两个包装调用共享控制器；不要把账号代理分配逻辑迁入控制器。

- [ ] **Step 5: 更新初始化和事件绑定**

删除旧 `settings-form`、模型预设和恢复按钮的重复事件绑定。启动顺序改为：

```javascript
sharedSettings.init()
  .then(refreshStatus)
  .then(refreshAccounts)
  .then(refreshContentVideos)
  .then(refreshBrands)
  .then(refreshPublishResults)
  .then(refreshBatchRuns)
  .then(refreshDailySchedules);
```

- [ ] **Step 6: 运行旧入口和新入口回归**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console.py tests/test_console_pages.py tests/test_settings_routes.py
node --test tests-js/console-settings.test.js tests-js/dashboard-navigation.test.js
```

Expected: 全部 PASS。

- [ ] **Step 7: 提交本任务**

```powershell
git add gateway/page_templates.py gateway/static/console_settings.js tests/test_console.py tests-js/console-settings.test.js
git commit -m "refactor: share settings controller with legacy console"
```

### Task 5: 系统设置完整回归和安全检查

**Files:**
- Modify only if failures reveal migration regressions: files listed in Tasks 1–4.

**Interfaces:**
- Verifies new and legacy pages use the same backend configuration semantics.

- [ ] **Step 1: 运行设置、代理、模型和采集凭据测试**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_settings_routes.py tests/test_settings_store.py tests/test_proxy_pool.py tests/test_model_presets.py tests/test_tiktok_stats_routes.py tests/test_console_pages.py tests/test_console.py
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行相关 Node 测试**

```powershell
node --test tests-js/console-settings.test.js tests-js/dashboard-navigation.test.js
```

Expected: 全部 PASS。

- [ ] **Step 3: 扫描密钥泄漏和失效 UI**

```powershell
rg -n "Selector Probe|同步到中控|排空后重启|\*\*\*\*" gateway/templates/console_settings.html gateway/templates/_console_settings_workspace.html gateway/static/console_settings.js
```

Expected: 无匹配。

- [ ] **Step 4: 运行全量回归**

```powershell
python -m pytest -q -p no:cacheprovider
npm run test:node
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交仅由回归发现的修复**

如果回归产生修复：

```powershell
git add gateway/static/console_settings.js gateway/templates/_console_settings_workspace.html gateway/templates/console_settings.html gateway/page_templates.py gateway/static/console.css tests-js/console-settings.test.js tests/test_console_pages.py tests/test_console.py
git commit -m "fix: preserve settings compatibility"
```

若没有修复，不创建空提交。
