# Console Page Elements Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Browser V2 页面点选器和元素库迁入完整的 Console 页面，同时保持 Browser V2 策略编辑器和全部元素操作不退化。

**Architecture:** 先把元素状态与点选会话从 `browser_v2.js` 提取为以工作区根节点为边界的共享控制器，再让 Browser V2 和 `/console/page-elements` 装配同一控制器与同一 partial。Browser V2 通过 `onElementsChanged` 接收元素快照，继续为策略编辑器提供元素列表。

**Tech Stack:** Flask、Jinja2、vanilla JavaScript UMD、CSS、pytest、Node test runner、Browser V2 API。

## Global Constraints

- 必须保留启动点选、轮询、连续保存、完成、取消、重新点选、校验、改名、启停、删除和 revision 冲突保护。
- 页面点选器替代 Selector Probe；新页面不得出现 Selector Probe UI。
- 不展示当前不存在的中控同步状态或同步按钮。
- 元素详情只展示 Store 实际返回的数据；诊断字段缺失时显示 `—`。
- 删除必须确认；409 冲突不得覆盖服务端新版本。
- 截图链接只允许 `evidence/<32位十六进制>.png`，渲染为绝对 `/evidence/<filename>`。
- 不使用右侧详情抽屉；详情在记录下方全宽展开。

---

### Task 1: 提取页面元素控制器，保持 Browser V2 DOM 不变

**Files:**
- Create: `gateway/static/page_elements_controller.js`
- Create: `tests-js/page-elements-controller.test.js`
- Modify: `gateway/static/browser_v2.js`
- Modify: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Produces: `createPageElementsController(options)`。
- Produces: `controller.init()`, `refresh()`, `render()`, `setFilters(filters)`, `toggleDetails(elementId)`, `startPicker()`, `finishPicker(cancel)`, `savePickerElement(...)`, `beginRepick(...)`, `renameElement(...)`, `setElementEnabled(...)`, `validateElement(...)`, `deleteElement(...)`, `hasActivePicker()`, `getElements()`, `destroy()`。
- Produces callback: `onElementsChanged(elements)`。
- Consumes: `requestJson(url, method, body) -> Promise<{status, data}>`。

- [ ] **Step 1: 写控制器状态与 API 契约失败测试**

在新测试中使用现有 `fakeNode`/`fakeDocument` 风格建立最小 DOM，并覆盖：

```javascript
test("controller loads profiles and elements and publishes an isolated snapshot", async () => {
  const changed = [];
  const controller = ui.createPageElementsController({
    root: document,
    requestJson: async (url) => url.endsWith("/profiles")
      ? {status: 200, data: {data: [{profile_token: "p1", name: "One"}]}}
      : {status: 200, data: {data: [{id: "e1", name: "Like", revision: 1, status: "active"}]}},
    setTimeout: () => 1,
    clearTimeout: () => {},
    addBeforeUnload: () => {},
    removeBeforeUnload: () => {},
    confirm: () => true,
    onElementsChanged: (items) => changed.push(items),
  });

  await controller.init();

  assert.equal(controller.getElements()[0].id, "e1");
  assert.equal(changed.at(-1)[0].name, "Like");
  changed.at(-1)[0].name = "mutated";
  assert.equal(controller.getElements()[0].name, "Like");
});

test("stale revision is reported without reloading over the local operation", async () => {
  const messages = [];
  const controller = createController({
    responseForPut: {status: 409, data: {error: {message: "revision conflict"}}},
    onMessage: (message) => messages.push(message),
  });

  const saved = await controller.renameElement({id: "e1", revision: 2}, "New");

  assert.equal(saved, false);
  assert.match(messages.at(-1).text, /版本已变化/);
});
```

同时迁移现有 Browser V2 测试中的连续保存、重新点选和点选表单不被无关轮询重建用例。

- [ ] **Step 2: 运行测试确认失败**

```powershell
node --test tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js
```

Expected: FAIL，因为共享控制器不存在。

- [ ] **Step 3: 创建 UMD 控制器和依赖校验**

文件入口使用：

```javascript
(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.PageElementsController = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (root) {
  "use strict";
  const API_PREFIX = "/api/browser-v2";
  const ACTIVE_PICKER = new Set(["selection_ready", "waiting_for_selection"]);

  function createPageElementsController(options) {
    const deps = options || {};
    const state = {
      profiles: [], elements: [], picker: null, repickTarget: null,
      filters: {query: "", kind: "", status: ""}, expandedId: "",
      latestValidation: new Map(), timer: null, initialized: false,
    };
    return {init, refresh, render, startPicker, finishPicker, savePickerElement,
      beginRepick, renameElement, setElementEnabled, validateElement,
      deleteElement, setFilters, toggleDetails, hasActivePicker, getElements, destroy};
  }

  return {createPageElementsController};
});
```

实现时将 `browser_v2.js` 中第 217–334 行的 Profile/元素/点选逻辑，以及第 440–441 行的点选 API 逻辑移动到新控制器。不要复制这些函数。

- [ ] **Step 4: 在 Browser V2 中装配控制器**

Browser V2 初始化时创建：

```javascript
const pageElements = deps.pageElementsFactory({
  root: doc(),
  requestJson: deps.requestJson,
  setTimeout: deps.setTimeout,
  clearTimeout: deps.clearTimeout,
  addBeforeUnload: deps.addUnload,
  removeBeforeUnload: deps.removeUnload,
  confirm: deps.confirm,
  onMessage: function (message) { setMessage(message.error, message.status); },
  onElementsChanged: function (elements) {
    state.elements = elements;
    renderStrategies();
  },
});
```

从 Browser V2 初始并行加载中删除 `/elements`，改为 `await pageElements.init()`；保留 `/profiles` 给执行中心使用。Browser V2 的 `destroy`/unload 必须调用 `pageElements.destroy()`。

- [ ] **Step 5: 运行聚焦测试**

```powershell
node --test tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js
python -m pytest -q -p no:cacheprovider tests/test_execution_v2_elements.py tests/test_gateway_app_contract.py
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交本任务**

```powershell
git add gateway/static/page_elements_controller.js gateway/static/browser_v2.js tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js
git commit -m "refactor: extract page elements controller"
```

### Task 2: 建立共享页面元素工作区和行内详情

**Files:**
- Create: `gateway/templates/_page_elements_workspace.html`
- Create: `gateway/static/page_elements.css`
- Modify: `gateway/templates/browser_v2.html`
- Modify: `gateway/static/page_elements_controller.js`
- Modify: `tests-js/page-elements-controller.test.js`
- Modify: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Produces root: `[data-page-elements-workspace]`。
- Produces stable IDs: `page-elements-picker-*`, `page-elements-filters`, `page-elements-body`, `page-elements-detail-row`, `page-elements-status`。

- [ ] **Step 1: 写筛选、展开和安全截图失败测试**

```javascript
test("filtering and expanding render one full-width detail row", async () => {
  const controller = createLoadedController([
    {id: "e1", name: "Like", kind: "click", status: "active", revision: 1,
      definition: {url_pattern: "https://www.tiktok.com/", frame_path: [], locators: [], diagnostic_metadata: {}, screenshot_path: "evidence/0123456789abcdef0123456789abcdef.png"}},
    {id: "e2", name: "Comment", kind: "input", status: "disabled", revision: 1},
  ]);

  controller.setFilters({query: "like", kind: "click", status: "active"});
  controller.toggleDetails("e1");
  controller.render();

  assert.equal(document.querySelectorAll("tr[data-element-row]").length, 1);
  assert.equal(document.querySelector("[data-element-detail]").colSpan, 8);
  assert.equal(document.querySelector("[data-element-evidence]").href, "/evidence/0123456789abcdef0123456789abcdef.png");
});

test("unsafe screenshot path does not create a link", () => {
  assert.equal(ui.evidenceHref("../secret.png"), "");
  assert.equal(ui.evidenceHref("evidence/not-a-hash.png"), "");
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node --test tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js
```

Expected: FAIL，因为 neutral workspace、筛选与详情 API 尚不存在。

- [ ] **Step 3: 创建共享 partial**

partial 的核心结构必须是：

```html
<div class="page-elements-workspace" data-page-elements-workspace>
  <form id="page-elements-picker-form" class="page-elements-picker">
    <label>测试 Profile<select id="page-elements-picker-profile" required disabled></select></label>
    <label>目标网址<input id="page-elements-picker-url" type="url" value="https://www.tiktok.com/" required></label>
    <div class="page-elements-actions">
      <button id="page-elements-picker-start" type="submit">开始点选</button>
      <button id="page-elements-picker-finish" type="button" disabled>完成点选</button>
      <button id="page-elements-picker-cancel" type="button" disabled>取消点选</button>
    </div>
  </form>
  <p id="page-elements-picker-state" role="status">尚未开启点选器。</p>
  <div id="page-elements-picker-candidates"></div>
  <form id="page-elements-filters" class="page-elements-filters">
    <input name="query" placeholder="搜索名称、用途或页面">
    <select name="kind"><option value="">全部类型</option><option value="click">点击</option><option value="input">输入</option><option value="generic">通用</option></select>
    <select name="status"><option value="">全部状态</option><option value="active">已启用</option><option value="disabled">已停用</option></select>
    <select id="page-elements-validate-profile" aria-label="校验 Profile" disabled></select>
  </form>
  <div class="page-elements-table-wrap"><table><thead><tr><th>名称</th><th>用途</th><th>类型</th><th>所属页面</th><th>定位器</th><th>状态</th><th>版本</th><th>操作</th></tr></thead><tbody id="page-elements-body"></tbody></table></div>
  <p id="page-elements-empty" hidden>暂无符合条件的元素。</p>
  <p id="page-elements-status" role="status"></p>
</div>
```

- [ ] **Step 4: 用 partial 替换 Browser V2 元素区 DOM**

`browser_v2.html` 的元素 panel 保留外层 `data-panel="elements"`，内部改为：

```jinja2
{% include '_page_elements_workspace.html' %}
```

在 `browser_v2.css` 之后加载 `page_elements.css`，并将控制器选择器统一改为新 ID。

- [ ] **Step 5: 实现详情降级规则**

控制器读取：

```javascript
const definition = item.definition || {};
const metadata = definition.diagnostic_metadata || {};
const detail = {
  url: definition.url_pattern || "—",
  framePath: Array.isArray(definition.frame_path) && definition.frame_path.length ? definition.frame_path.join(" → ") : "顶层页面",
  tag: metadata.tag || "—",
  role: metadata.role || "—",
  name: metadata.name || metadata.accessible_name || "—",
  text: metadata.text_preview || "—",
  bounds: metadata.bounding_box ? JSON.stringify(metadata.bounding_box) : "—",
};
```

最近校验结果只保存在 `state.latestValidation`，刷新后不伪造历史结果。

- [ ] **Step 6: 运行测试并提交**

```powershell
node --test tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js
git add gateway/templates/_page_elements_workspace.html gateway/static/page_elements.css gateway/templates/browser_v2.html gateway/static/page_elements_controller.js tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js
git commit -m "feat: add shared page elements workspace"
```

Expected: 测试 PASS，Browser V2 元素标签仍可使用。

### Task 3: 建立 Console 页面元素入口

**Files:**
- Create: `gateway/templates/console_page_elements.html`
- Create: `gateway/static/console_page_elements.js`
- Modify: `gateway/routes_console.py`
- Modify: `gateway/static/console.css`
- Modify: `tests/test_console_pages.py`
- Modify: `tests-js/page-elements-controller.test.js`

**Interfaces:**
- Produces: `GET /console/page-elements -> 200`。
- Consumes: `window.PageElementsController.createPageElementsController`。

- [ ] **Step 1: 写 Console 路由与内容失败测试**

```python
def test_console_page_elements_renders_native_workspace(client):
    response = client.get("/console/page-elements")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="console-page-elements"' in html
    assert 'data-page-elements-workspace' in html
    assert "page_elements_controller.js" in html
    assert "console_page_elements.js" in html
    assert "/browser-v2?view=elements" not in response.headers.get("Location", "")
    assert "Selector Probe" not in html
    assert "同步到中控" not in html
```

- [ ] **Step 2: 运行 Flask 测试确认失败**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py
```

Expected: FAIL，因为路由仍重定向。

- [ ] **Step 3: 实现路由、模板和薄 adapter**

路由：

```python
@bp.get("/page-elements")
def page_elements():
    return _render("console_page_elements.html", "page-elements")
```

模板：

```jinja2
{% extends 'console_base.html' %}
{% block title %}页面元素 · Agent 自动化主控台{% endblock %}
{% block content %}
<div id="console-page-elements" class="console-page">
  <header class="console-page-head"><div><h1>页面元素</h1><p>点选、校验并维护本机页面元素</p></div></header>
  {% include '_page_elements_workspace.html' %}
</div>
{% endblock %}
{% block scripts %}
<script src="{{ url_for('static', filename='page_elements_controller.js') }}"></script>
<script src="{{ url_for('static', filename='console_page_elements.js') }}"></script>
{% endblock %}
```

adapter 只负责注入 `fetch`、计时器、`beforeunload`、确认框和根节点，不包含元素业务方法。

- [ ] **Step 4: 增加 Console 视觉适配**

`console.css` 只为共享工作区映射 Console token：

```css
.console-page .page-elements-workspace { display: grid; gap: 14px; }
.console-page .page-elements-picker,
.console-page .page-elements-filters { padding: 14px; background: var(--console-surface); border: 1px solid var(--console-line); border-radius: 8px; }
.console-page .page-elements-table-wrap { overflow-x: auto; border: 1px solid var(--console-line); border-radius: 8px; }
.console-page [data-element-detail] { padding: 14px; background: var(--console-soft); }
```

- [ ] **Step 5: 运行路由、UI 和 Picker 回归**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py
node --test tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: 全部 PASS。

- [ ] **Step 6: 提交本任务**

```powershell
git add gateway/templates/console_page_elements.html gateway/static/console_page_elements.js gateway/routes_console.py gateway/static/console.css tests/test_console_pages.py tests-js/page-elements-controller.test.js
git commit -m "feat: migrate page elements to console"
```

### Task 4: 页面元素完整回归

**Files:**
- Modify only if failures reveal migration regressions: files listed in Tasks 1–3.

**Interfaces:**
- Verifies Browser V2 strategy editor receives current element snapshots.

- [ ] **Step 1: 运行完整元素后端测试**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_execution_v2_elements.py tests/test_execution_v2_picker.py tests/test_execution_v2_routes.py tests/test_execution_v2_strategy.py
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行完整相关前端测试**

```powershell
node --test tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: 全部 PASS。

- [ ] **Step 3: 检查不存在旧选择器和伪功能**

```powershell
rg -n "v2-picker|v2-elements-list|同步到中控|Selector Probe" gateway/templates/console_page_elements.html gateway/templates/_page_elements_workspace.html gateway/static/console_page_elements.js
```

Expected: 无匹配。

- [ ] **Step 4: 提交仅由回归发现的修复**

如果前两步产生代码修复：

```powershell
git add gateway/static/page_elements_controller.js gateway/static/browser_v2.js gateway/templates/_page_elements_workspace.html gateway/templates/browser_v2.html gateway/templates/console_page_elements.html gateway/static/console_page_elements.js gateway/static/page_elements.css gateway/static/console.css tests-js/page-elements-controller.test.js tests-js/browser-v2-ui.test.js tests/test_console_pages.py
git commit -m "fix: preserve page element workflows"
```

若没有修复，不创建空提交。
