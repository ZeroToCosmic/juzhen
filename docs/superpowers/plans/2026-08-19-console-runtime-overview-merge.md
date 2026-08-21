# Console Runtime Overview Merge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将当前独立但只有四项状态的“运行环境”并入运行总览，并保留兼容重定向。

**Architecture:** `/console/overview` 继续作为唯一状态入口，前端以 `Promise.allSettled` 独立读取网关、Browser V2 Profile 和 TikTok 采集状态。`/console/runtime` 只保留为带锚点的兼容重定向，不新增排空或重启能力。

**Tech Stack:** Flask、Jinja2、vanilla JavaScript、CSS、pytest、Node test runner。

## Global Constraints

- 不展示当前后端不存在的排空、重启、停止接单或重启进度按钮。
- `/api/status` 只能表示接口可达或配置状态，不能表述为完整进程健康。
- 单个状态接口失败不得清空其他成功结果。
- 侧边栏移除“运行环境”，但 `/console/runtime` 必须继续可访问。
- 不修改浏览器执行、发布或采集业务链。

---

### Task 1: 锁定路由、导航和模板契约

**Files:**
- Modify: `tests/test_console_pages.py`
- Modify: `gateway/routes_console.py`
- Modify: `gateway/templates/_dashboard_sidebar.html`
- Modify: `gateway/templates/console_overview.html`

**Interfaces:**
- Produces: `GET /console/runtime -> 302 /console/overview#local-runtime`
- Produces: `#local-runtime` 作为兼容锚点和本机状态根节点。

- [ ] **Step 1: 写失败的 Flask 页面测试**

将运行环境从共享页面参数表中移除，并增加精确测试：

```python
def test_console_runtime_redirects_to_local_runtime_section(client):
    response = client.get("/console/runtime")

    assert response.status_code == 302
    assert response.headers["Location"].endswith(
        "/console/overview#local-runtime"
    )


def test_overview_owns_local_runtime_and_sidebar_has_no_runtime_item(client):
    html = client.get("/console/overview").get_data(as_text=True)

    assert 'id="local-runtime"' in html
    assert 'href="/console/runtime"' not in html
    assert ">运行环境</a>" not in html
```

同步更新 `test_sidebar_uses_approved_module_order`，标签顺序中删除 `运行环境`。

- [ ] **Step 2: 运行测试确认失败**

Run:

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py
```

Expected: FAIL，因为 `/console/runtime` 仍渲染独立模板，侧边栏仍有入口，且总览没有 `#local-runtime`。

- [ ] **Step 3: 实现路由和导航变更**

将路由改为：

```python
@bp.get("/runtime")
def runtime():
    return redirect(url_for("console.overview", _anchor="local-runtime"))
```

从 `_dashboard_sidebar.html` 删除 `active_nav == 'runtime'` 的链接。将总览状态区域标记为：

```html
<section id="local-runtime" class="console-section" aria-labelledby="local-runtime-title">
  <div class="console-section-head">
    <div>
      <h2 id="local-runtime-title">本机状态</h2>
      <p>本机接口、浏览器和采集进程的当前可用情况</p>
    </div>
    <span id="overview-runtime-updated" class="console-muted">尚未刷新</span>
  </div>
  <div class="console-stat-grid" aria-label="本机状态">
    <article class="console-stat"><span>本机网关</span><strong id="overview-runtime-gateway">—</strong><small id="overview-runtime-gateway-note">正在读取</small></article>
    <article class="console-stat"><span>浏览器 Profile</span><strong id="overview-runtime-profiles">—</strong><small id="overview-runtime-profiles-note">正在读取</small></article>
    <article class="console-stat"><span>数据采集服务</span><strong id="overview-runtime-scraper">—</strong><small id="overview-runtime-scraper-note">正在读取</small></article>
    <article class="console-stat"><span>本机采集调度</span><strong id="overview-runtime-worker">—</strong><small id="overview-runtime-worker-note">正在读取</small></article>
  </div>
</section>
```

- [ ] **Step 4: 运行 Flask 测试确认通过**

Run:

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add gateway/routes_console.py gateway/templates/_dashboard_sidebar.html gateway/templates/console_overview.html tests/test_console_pages.py
git commit -m "refactor: merge runtime into overview"
```

### Task 2: 将总览状态读取改成可测试控制器

**Files:**
- Create: `tests-js/console-overview.test.js`
- Modify: `gateway/static/console_overview.js`

**Interfaces:**
- Produces: `createOverviewUI(dependencies)`。
- Produces: `summarizeLocalRuntime(results, nowText)` 纯函数。
- Consumes: `dependencies.read(url)` 和 `dependencies.document`。

- [ ] **Step 1: 写状态归一化失败测试**

```javascript
"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const ui = require("../gateway/static/console_overview.js");

test("local runtime keeps fulfilled states when one dependency fails", () => {
  const result = ui.summarizeLocalRuntime([
    {status: "fulfilled", value: {ok: true}},
    {status: "fulfilled", value: {data: [{profile_token: "p1"}, {profile_token: "p2"}]}},
    {status: "rejected", reason: new Error("offline")},
  ], "2026-08-19 15:30");

  assert.equal(result.gateway.value, "可达");
  assert.equal(result.profiles.value, "2 个");
  assert.equal(result.scraper.value, "不可用");
  assert.equal(result.worker.value, "不可用");
  assert.equal(result.failed, 1);
  assert.equal(result.updatedAt, "2026-08-19 15:30");
});

test("scraper and worker are rendered independently", () => {
  const result = ui.summarizeLocalRuntime([
    {status: "fulfilled", value: {}},
    {status: "fulfilled", value: {data: []}},
    {status: "fulfilled", value: {scraper: {running: true}, worker: {running: false}}},
  ], "now");

  assert.equal(result.scraper.value, "运行中");
  assert.equal(result.worker.value, "未运行");
});
```

- [ ] **Step 2: 运行 Node 测试确认失败**

Run:

```powershell
node --test tests-js/console-overview.test.js
```

Expected: FAIL，因为当前文件没有 CommonJS 导出或 `summarizeLocalRuntime`。

- [ ] **Step 3: 实现 UMD 控制器和状态归一化**

将脚本改成与其他 Console 模块一致的 UMD 结构，并提供：

```javascript
function summarizeLocalRuntime(results, updatedAt) {
  const [gateway, profiles, collection] = results;
  const profilePayload = profiles.status === "fulfilled"
    ? (profiles.value.data || profiles.value)
    : [];
  const profileItems = Array.isArray(profilePayload)
    ? profilePayload
    : (profilePayload.profiles || []);
  const collectionValue = collection.status === "fulfilled" ? collection.value : {};
  return {
    gateway: {
      value: gateway.status === "fulfilled" ? "可达" : "不可用",
      note: gateway.status === "fulfilled" ? "本机接口响应正常" : "无法读取接口状态",
    },
    profiles: {
      value: profiles.status === "fulfilled" ? `${profileItems.length} 个` : "不可用",
      note: profiles.status === "fulfilled" ? "AdsPower Profile" : "无法读取 Profile",
    },
    scraper: {
      value: collection.status === "fulfilled" ? (collectionValue.scraper?.running ? "运行中" : "未运行") : "不可用",
      note: collection.status === "fulfilled" ? "TikTok 采集器" : "无法读取采集服务",
    },
    worker: {
      value: collection.status === "fulfilled" ? (collectionValue.worker?.running ? "运行中" : "未运行") : "不可用",
      note: collection.status === "fulfilled" ? "本机采集调度" : "无法读取采集调度",
    },
    failed: results.filter((item) => item.status === "rejected").length,
    updatedAt,
  };
}
```

`refresh()` 必须调用：

```javascript
const runtimeResults = await Promise.allSettled([
  deps.read("/api/status"),
  deps.read("/api/browser-v2/profiles"),
  deps.read("/api/tiktok-stats/status"),
]);
```

导出：

```javascript
return {createOverviewUI, summarizeLocalRuntime};
```

- [ ] **Step 4: 运行 Node 和 Flask 测试**

```powershell
node --test tests-js/console-overview.test.js
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add gateway/static/console_overview.js tests-js/console-overview.test.js
git commit -m "feat: show local runtime on overview"
```

### Task 3: 删除失去引用的独立运行环境资源

**Files:**
- Delete: `gateway/templates/console_runtime.html`
- Delete: `gateway/static/console_runtime.js`

**Interfaces:**
- Consumes: Task 1 的兼容重定向。

- [ ] **Step 1: 确认没有模板或静态资源引用**

```powershell
rg -n "console_runtime|console-runtime" gateway tests tests-js
```

Expected: 只命中待删除的两个文件；路由不再渲染模板。

- [ ] **Step 2: 删除两个失去引用的文件**

使用 `apply_patch` 删除：

```text
gateway/templates/console_runtime.html
gateway/static/console_runtime.js
```

- [ ] **Step 3: 运行聚焦回归**

```powershell
python -m pytest -q -p no:cacheprovider tests/test_console_pages.py tests/test_tiktok_stats_routes.py
node --test tests-js/console-overview.test.js
```

Expected: 全部 PASS。

- [ ] **Step 4: 提交本任务**

```powershell
git add gateway/templates/console_runtime.html gateway/static/console_runtime.js
git commit -m "chore: remove standalone runtime page"
```
