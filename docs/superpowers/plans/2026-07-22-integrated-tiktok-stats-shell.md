# 数据统计融入自动化主控台 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 TikTok 数据统计页面放入与自动化主控台完全一致、始终展开的侧栏外壳，并删除主控台旧统计面板。

**Architecture:** 单一 Jinja 片段生成侧栏；单一 CSS 文件定义固定外壳。主控台继续使用现有面板 DOM，只增加可测试的 URL 导航控制器；TikTok 统计继续使用现有控制器、API 和 URL 筛选状态。

**Tech Stack:** Flask/Jinja、原生 HTML/CSS/JavaScript、Python pytest、Node.js `node:test`

## Global Constraints

- 采用 A＋A1：共享主控台视觉外壳，左侧栏始终展开。
- 不修改 TikTok 数据 API、采集器、调度器、Cookie、安全存储、SQLite、90 天快照及永久日汇总。
- 不修改执行策略、滚动动作、`config.json` 保存行为。
- 不引入前端框架，不把主控台重写为 SPA。
- 统计列表、详情、趋势、导入和设置能力保持原行为。
- 暂不执行任何 Git 初始化、提交、分支或工作树操作。

---

## File Map

- Create `gateway/templates/_dashboard_sidebar.html`: 唯一侧栏文案、顺序、地址和当前项状态。
- Create `gateway/static/dashboard_shell.css`: 主控台与统计页共用的固定展开外壳。
- Create `gateway/static/dashboard_navigation.js`: 主控台面板与 `?panel=` URL 双向同步。
- Modify `gateway/app.py`: 引入共享外壳、接入导航控制器、删除旧统计面板和专用脚本。
- Modify `gateway/templates/tiktok_stats.html`: 使用共享外壳和侧栏。
- Modify `gateway/static/tiktok_stats.css`: 只保留统计内容样式，适配外壳内容区。
- Modify `tests/test_console.py`: 验证共享导航和旧统计 DOM 已删除。
- Modify `tests/test_tiktok_stats_routes.py`: 验证统计页共享外壳、当前项及资源。
- Create `tests-js/dashboard-navigation.test.js`: 验证初始化、点击和前进后退。
- Modify `tests-js/tiktok-stats-ui.test.js`: 验证统计模板仍保留完整内容和共享外壳。

### Task 1: 共享侧栏与主控台 URL 导航

**Files:**
- Create: `gateway/templates/_dashboard_sidebar.html`
- Create: `gateway/static/dashboard_shell.css`
- Create: `gateway/static/dashboard_navigation.js`
- Modify: `gateway/app.py`
- Test: `tests/test_console.py`
- Test: `tests-js/dashboard-navigation.test.js`

**Interfaces:**
- Consumes: 主控台面板 ID `panel-<name>`；侧栏上下文 `active_nav`。
- Produces: `DashboardNavigation.createDashboardNavigation({window, panels, links, title, fallback})`，返回带 `start()` 和 `showPanel()` 的控制器。

- [ ] **Step 1: 写 Python 失败测试**

验证 `/` 包含 `_dashboard_sidebar.html` 生成的九个链接，集中配置当前项拥有 `aria-current="page"`，普通模块地址为 `/?panel=<name>`，数据统计地址为 `/tiktok-stats`。

- [ ] **Step 2: 运行 Python 测试并确认预期失败**

Run: `python -m pytest tests/test_console.py -q`

Expected: FAIL，原因是现有侧栏使用按钮，且没有共享外壳资源。

- [ ] **Step 3: 写 Node 失败测试**

覆盖以下行为：合法 `panel` 初始化、非法值回退 `settings`、普通链接点击调用 `preventDefault` 并写入 `history.pushState`、`popstate` 恢复面板、统计链接不被控制器绑定。

- [ ] **Step 4: 运行 Node 测试并确认预期失败**

Run: `node --test tests-js/dashboard-navigation.test.js`

Expected: FAIL，原因是 `gateway/static/dashboard_navigation.js` 尚不存在。

- [ ] **Step 5: 实现共享侧栏、外壳和导航控制器**

侧栏固定九项顺序：集中配置、账号花名册、代理配置、内容管理、发布管理、发布结果、数据统计、浏览器接管、执行策略。普通链接带 `data-panel`；统计链接不带 `data-panel`。控制器只绑定当前文档中存在目标面板的链接。

- [ ] **Step 6: 主控台接入共享实现**

`CONTROL_PAGE_HTML` 引入共享 CSS、侧栏 partial 和导航脚本；根路由传 `active_nav="settings"`。删除旧 `navButtons` 与旧 `showPanel`，用新控制器启动当前 `?panel=` 面板。面板内部代码继续调用控制器暴露的 `showPanel` 包装。

- [ ] **Step 7: 运行定向测试并确认通过**

Run: `python -m pytest tests/test_console.py -q`

Run: `node --test tests-js/dashboard-navigation.test.js`

Expected: PASS。

### Task 2: 统计页接入 A＋A1 共享外壳

**Files:**
- Modify: `gateway/templates/tiktok_stats.html`
- Modify: `gateway/static/tiktok_stats.css`
- Modify: `tests/test_tiktok_stats_routes.py`
- Modify: `tests-js/tiktok-stats-ui.test.js`

**Interfaces:**
- Consumes: `_dashboard_sidebar.html`、`dashboard_shell.css`、`active_nav="tiktok-stats"`。
- Produces: `#tiktok-stats-app` 位于 `.dashboard-main` 内；原统计 DOM ID 全部保持不变。

- [ ] **Step 1: 写失败测试**

验证 `/tiktok-stats` 包含共享九项侧栏、数据统计项拥有 `aria-current="page"`、页面加载 `dashboard_shell.css`，且不再包含独立 `site-header`。

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `python -m pytest tests/test_tiktok_stats_routes.py::test_page_and_account_routes -q`

Run: `node --test tests-js/tiktok-stats-ui.test.js`

Expected: FAIL，原因是统计页仍使用独立顶栏。

- [ ] **Step 3: 修改模板和内容 CSS**

模板结构改为 `.dashboard-shell > shared sidebar + main#tiktok-stats-app.dashboard-main.stats-page-shell`。共享 CSS 固定 268px 侧栏、高度 100vh、sticky、始终展开；页面最小宽度避免小屏折叠；统计表格继续只在 `.table-scroll` 内横向滚动。

- [ ] **Step 4: 运行定向测试并确认通过**

Run: `python -m pytest tests/test_tiktok_stats_routes.py::test_page_and_account_routes -q`

Run: `node --test tests-js/tiktok-stats-ui.test.js`

Expected: PASS。

### Task 3: 删除旧统计面板与遗留脚本

**Files:**
- Modify: `gateway/app.py`
- Modify: `tests/test_console.py`

**Interfaces:**
- Consumes: 新 `/tiktok-stats` 入口。
- Produces: 主控台不含 `panel-publish-stats`、`stats-filter-date`、`stats-sort`、`stats-refresh`、旧统计卡片或 `publish-stats-body`。

- [ ] **Step 1: 写失败测试**

把原“旧统计面板存在”断言改为上述旧 DOM ID、文案和 `refreshPublishStats` 前端函数全部不存在；保留发布结果面板和 `/tiktok-stats` 链接断言。

- [ ] **Step 2: 运行测试并确认预期失败**

Run: `python -m pytest tests/test_console.py -q`

Expected: FAIL，原因是旧面板及函数仍存在。

- [ ] **Step 3: 删除旧 DOM 与仅服务旧 DOM 的代码**

删除旧面板、`statsFilterQuery`、`refreshPublishStats`、旧刷新和排序监听。移除发布动作完成后的旧统计刷新链；发布结果、采样、回填和日志清理自身行为保留。

- [ ] **Step 4: 扫描引用并运行定向测试**

Run: `rg -n "panel-publish-stats|stats-filter-date|stats-sort|stats-refresh|stats-count|stats-success|stats-likes|stats-views|stats-comments|stats-engagement|publish-stats-body|refreshPublishStats" gateway/app.py`

Expected: 无输出。

Run: `python -m pytest tests/test_console.py -q`

Expected: PASS。

### Task 4: 完整回归与页面验收

**Files:**
- Verify only: `gateway/app.py`, `gateway/templates/*.html`, `gateway/static/*.js`, `gateway/static/*.css`

**Interfaces:**
- Consumes: Tasks 1–3 完成结果。
- Produces: 可运行、无控制台错误、导航连续的 A＋A1 页面。

- [ ] **Step 1: 运行语法检查**

Run: `python -m compileall gateway tests`

Run: `node --check gateway/static/dashboard_navigation.js`

Run: `node --check gateway/static/tiktok_stats.js`

Expected: 全部退出码 0。

- [ ] **Step 2: 运行完整测试**

Run: `python -m pytest -q`

Run: `npm run test:node`

Expected: 全部 PASS。

- [ ] **Step 3: 运行秘密扫描**

Run: `.\.venv\Scripts\python.exe scripts\scan_repository_secrets.py`

Expected: PASS，无 Cookie、令牌或密码明文。

- [ ] **Step 4: 本地浏览器验收**

检查 `/`、`/?panel=strategies`、`/tiktok-stats`：侧栏文案顺序一致且始终展开；当前项正确；主控台内部点击不整页刷新；数据统计正常路由；浏览器前进后退恢复面板和统计筛选；统计列表、详情、趋势、导入和设置可用；控制台无新增异常。

- [ ] **Step 5: 记录结果，不操作 Git**

报告修改文件、测试数量及页面验收结果。Git 初始化和提交留到用户后续明确要求。
