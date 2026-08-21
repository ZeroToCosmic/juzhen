# Remote Action Console and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有统一 Console 中补齐动作调试/发布/同步、远程任务、资源占用和本地证据展示，并完成两个适配器联合运行与 500 Agent 参考规模验收。

**Architecture:** Console 只读取本机动作库和 Agent 远程运行数据库；发布动作通过 Agent→Central HTTPS API，任务页面不再调用旧 pull API。动作详情沿用已确认的独立子页面/全宽展开形式。规模测试使用无浏览器 Fake Executor，真实内核只参加功能验收。

**Tech Stack:** Flask、Jinja2、vanilla JavaScript UMD、CSS、Node test runner、pytest、FastAPI TestClient、WebSocket 模拟 Agent。

## Global Constraints

- 动作库继续支持新建、复制、编辑、版本、手动调试；远程能力不能取代本机设计能力。
- Browser Strategy 与 Comment Campaign 在筛选、状态、发布和同步信息上同级展示。
- “发布”只针对成功调试的精确 checksum；“强制发布”只对管理员展示，必须填写原因。
- 任务执行页只读展示已下发并由本机持久化的 WorkOrder，不在页面创建正式任务。
- 资源冲突必须在启动调试前可见；不提供强制抢占按钮。
- 回执详情只上传 Central 接收状态；原始证据链接只指向经过校验的本机路径。
- 页面不得恢复右侧小抽屉；详情使用独立子页面或表格下方全宽展开。
- Task 1–3 属于 G1，必须在 Equal Action Adapters 计划开始前完成；Task 4–6 属于 G3，依赖两个适配器完成。

---

### Task 1: 为 Console 提供本机动作发布和远程运行只读 API

**Files:**
- Create: `gateway/routes_remote_actions.py`
- Create: `gateway/action_library.py`
- Modify: `gateway/app.py`
- Modify: `gateway/routes_console.py`
- Create: `tests/test_console_remote_action_api.py`
- Modify: `tests/test_console_pages.py`

- [ ] **Step 1: 写 API 来源、权限和证据路径测试**

必须实现以下具名测试：`test_tasks_api_reads_local_remote_store_without_calling_pull`、`test_action_library_aggregates_without_copying_action_definition`、`test_debug_endpoint_uses_local_work_order_runner`、`test_action_release_rejects_unvalidated_checksum`、`test_admin_waiver_requires_reason_and_audit_actor`、`test_resource_api_exposes_owner_type_without_secret_payload`、`test_evidence_api_rejects_path_traversal`。第一个测试将 `CentralClient.pull_subtasks` monkeypatch 为抛错函数，API 仍必须返回本机记录；调试测试将两个业务 service 的直接执行方法替换为抛错函数，Local runner 仍被调用一次。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_console_remote_action_api.py tests/test_console_pages.py -q
```

Expected: FAIL，`/console/api/tasks` 仍调用 `CentralClient.pull_subtasks()`。

- [ ] **Step 3: 实现明确 API**

```text
GET  /console/api/remote/tasks
GET  /console/api/remote/tasks/{work_order_id}
GET  /console/api/remote/resources
GET  /console/api/actions/{kind}/{action_id}/releases
POST /console/api/actions/{kind}/{action_id}/debug-runs
POST /console/api/actions/{kind}/{action_id}/releases
POST /console/api/actions/sync
GET  /console/api/remote/outcomes/{run_id}
```

旧 `/console/api/tasks` 返回同一 service 的兼容投影，不再访问中控网络。`ActionLibraryService` 只聚合两个模块 Store 的 identity、草稿、debug 和 release 摘要，不复制动作正文。发布 service 先读取本机 immutable release，再调用 Central API；网络失败保留 `pending_sync`。调试端点必须构造 Local WorkOrder 并调用 `LocalWorkOrderRunner`，不得直接调用两个业务 service 的执行入口。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_console_remote_action_api.py tests/test_console_pages.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add gateway/routes_remote_actions.py gateway/action_library.py gateway/app.py gateway/routes_console.py tests/test_console_remote_action_api.py tests/test_console_pages.py
git commit -m "feat: expose local remote action console APIs"
```

### Task 2: 扩展动作库的调试、版本、发布和同步状态

**Files:**
- Modify: `gateway/templates/console_actions.html`
- Modify: `gateway/static/console_actions.js`
- Modify: `gateway/static/console.css`
- Modify: `tests-js/console-actions.test.js`
- Modify: `tests/test_console_pages.py`

- [ ] **Step 1: 写两个动作同级和发布门禁 UI 测试**

```javascript
test("both action kinds expose the same maintenance operations", () => {
  for (const item of [strategy, campaign]) {
    const row = api.normalizeAction(item);
    assert.deepEqual(row.operations, ["edit", "copy", "debug", "versions", "publish"]);
  }
});

test("publish is disabled when latest debug checksum differs", () => {
  const row = api.normalizeAction({content_checksum: "new", latest_debug_checksum: "old"});
  assert.equal(row.canPublish, false);
});
test("pending and mismatched sync states are visible", () => {
  assert.equal(api.syncLabel("pending_sync"), "待同步");
  assert.equal(api.syncLabel("checksum_mismatch"), "版本不一致");
});
test("resource busy disables debug without offering preemption", () => {
  const actions = api.operationsFor({resource_status: "busy_remote"});
  assert.equal(actions.find((item) => item.id === "debug").disabled, true);
  assert.equal(actions.some((item) => item.id === "preempt"), false);
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node --test tests-js/console-actions.test.js
python -m pytest tests/test_console_pages.py -q
```

Expected: FAIL，现有动作列表只有“维护”。

- [ ] **Step 3: 实现高密度动作表和全宽详情**

列固定为：动作名称/ID、类型、草稿 revision、最近调试、发布 revision、同步状态、资源状态、操作。点击“版本”在当前表格行下方插入 colspan 全宽区；“编辑”进入现有独立子页面；“调试”先打开参数/资源确认区再启动。

- [ ] **Step 4: 运行测试确认通过**

```powershell
node --test tests-js/console-actions.test.js
python -m pytest tests/test_console_pages.py -q
```

Expected: PASS，动作库不再显示“中控同步接口未配置”的硬编码占位文案。

- [ ] **Step 5: 提交本任务**

```powershell
git add gateway/templates/console_actions.html gateway/static/console_actions.js gateway/static/console.css tests-js/console-actions.test.js tests/test_console_pages.py
git commit -m "feat: restore action debug publish workflow"
```

### Task 3: 完善任务、运行环境和回执证据页面

**Files:**
- Modify: `gateway/templates/console_tasks.html`
- Modify: `gateway/static/console_tasks.js`
- Modify: `gateway/templates/console_overview.html`
- Modify: `gateway/static/console_overview.js`
- Modify: `gateway/templates/console_receipts.html`
- Modify: `gateway/static/console_receipts.js`
- Create: `tests-js/console-tasks.test.js`
- Modify: `tests-js/console-overview.test.js`
- Modify: `tests-js/console-receipts.test.js`

- [ ] **Step 1: 写业务字段与状态映射测试**

```javascript
test("task row prioritizes action target account progress and updated time", () => {
  assert.deepEqual(api.columns, ["action", "source", "resources", "status", "progress", "received_at", "updated_at"]);
});
test("task detail shows key stages without mouse events", () => {
  assert.deepEqual(api.timeline([{stage: "RUNNING"}, {stage: "mouse_move"}]), [{stage: "RUNNING"}]);
});
test("runtime shows WSS outbox locks and drain restart", () => {
  assert.deepEqual(runtime.cards, ["wss", "reconcile", "outbox", "runs", "locks", "drain_restart"]);
});
test("receipt keeps only a local evidence manifest", () => {
  assert.equal(receipts.normalizeEvidence({local_ref: "evidence/a.png", sha256: "abc"}).local_ref, "evidence/a.png");
});
test("runtime anomalies never render a local approval action", () => {
  assert.equal(api.operationsForTask({status: "UNVERIFIED"}).includes("approve"), false);
});
```

- [ ] **Step 2: 运行测试确认失败**

```powershell
node --test tests-js/console-tasks.test.js tests-js/console-overview.test.js tests-js/console-receipts.test.js
```

Expected: FAIL，当前任务页只有 4 列且回执聚合旧业务库。

- [ ] **Step 3: 实现页面信息结构**

任务列表列固定为：动作/目标、来源任务、账号与窗口、状态、关键进度、接收时间、更新时间。任务详情为独立列表视图内全宽页面，展示 WorkOrder 身份、冻结 revision/checksum、关键阶段、取消、错误和上报状态。

本机运行环境补充：WSS 连接/最近对账、待发 Outbox、当前远程运行、本机调试占用、锁冲突、排空后重启。回执与证据改读 remote outcome；历史 Browser/Campaign/Publishing 仍通过来源筛选兼容展示。

- [ ] **Step 4: 运行测试确认通过**

```powershell
node --test tests-js/console-tasks.test.js tests-js/console-overview.test.js tests-js/console-receipts.test.js
python -m pytest tests/test_console_pages.py tests/test_console_remote_action_api.py -q
```

Expected: PASS，不出现本机审批按钮或右侧详情抽屉。

- [ ] **Step 5: 提交本任务**

```powershell
git add gateway/templates/console_tasks.html gateway/static/console_tasks.js gateway/templates/console_overview.html gateway/static/console_overview.js gateway/templates/console_receipts.html gateway/static/console_receipts.js tests-js/console-tasks.test.js tests-js/console-overview.test.js tests-js/console-receipts.test.js
git commit -m "feat: show remote runs and local evidence"
```

### Task 4: 完成两个适配器端到端与故障联合验收

**Depends on:** Equal Action Adapters 全部任务，以及 Agent Remote Runtime Task 5 的 Fake Executor `F-01..F-07`、`R-01..R-05` 验收已通过。

**Files:**
- Create: `tests/test_remote_actions_e2e.py`
- Create: `tests/test_remote_actions_faults.py`
- Create: `tests/fixtures/remote_actions/browser_work_order.json`
- Create: `tests/fixtures/remote_actions/campaign_work_order.json`

- [ ] **Step 1: 实现 A-01 至 A-06 用例**

必须实现以下具名测试：`test_equal_adapters_run_concurrently_on_disjoint_resources`、`test_browser_batch_is_split_before_delivery`、`test_campaign_tree_stays_on_one_agent`、`test_replayed_frozen_order_has_identical_text_nodes_and_parameters`、`test_child_waits_for_confirmed_parent`、`test_results_locks_and_evidence_do_not_cross_contaminate`。并发测试必须用 barrier 证明两个适配器执行时间发生重叠，并按 run ID 比较结果、锁与 manifest。

- [ ] **Step 2: 用真实适配器重跑 F-01 至 F-07 与 R-01 至 R-05 故障矩阵**

G1 的权威通过条件仍是两个 Fake Executor；此处作为真实适配器回归，故障注入覆盖 Inbox commit、每个 effect 状态、Outcome commit、WSS ACK 前后、资源锁前后和重启恢复；每个 case 断言最终为确定终态或 `RECONCILING`，且 effect 提交计数不超过 1。

- [ ] **Step 3: 运行功能验收**

```powershell
python -m pytest tests/test_remote_actions_e2e.py tests/test_remote_actions_faults.py -q
```

Expected: PASS，`A-01..A-06`、`F-01..F-07`、`R-01..R-05` 全绿。

- [ ] **Step 4: 提交本任务**

```powershell
git add tests/test_remote_actions_e2e.py tests/test_remote_actions_faults.py tests/fixtures/remote_actions/browser_work_order.json tests/fixtures/remote_actions/campaign_work_order.json
git commit -m "test: verify remote action end to end behavior"
```

### Task 5: 完成 500 Agent 规模和背压验收

**Files:**
- Create: `tests/load/remote_agent_simulator.py`
- Create: `tests/load/test_remote_action_scale.py`
- Create: `docs/operations/remote-action-load-test.md`

- [ ] **Step 1: 实现无浏览器模拟 Agent**

模拟器必须使用真实 WSS 信封、sequence 对账、ACK、HTTPS Outcome 和 10% 重复 event ID；每个连接维持独立 device/session 状态，不导入业务适配器。

- [ ] **Step 2: 写 L-01 至 L-05 自动断言**

```python
assert unexpected_disconnects == 0
assert received_ack_p95_seconds <= 2.0
assert lost_commands == duplicate_runs == 0
assert reconciled_devices == 500
assert max_pending_per_device <= 100
assert unique_outcomes == expected_unique_outcomes
```

- [ ] **Step 3: 运行短版 CI 规模测试**

```powershell
python -m pytest tests/load/test_remote_action_scale.py -q -m "not soak"
```

Expected: PASS，50 Agent、每台 10 个 WorkOrder、随机重连和背压均通过。

- [ ] **Step 4: 在受控非生产环境运行完整验收**

```powershell
python -m pytest tests/load/test_remote_action_scale.py -q -m soak --agents=500 --duration-seconds=1800
```

Expected: PASS，达到设计文档 `L-01` 至 `L-05`；记录 CPU、RSS、P50/P95 ACK、重连完成时间和唯一 Outcome 数。

- [ ] **Step 5: 记录可复现运行方式和结果格式**

`docs/operations/remote-action-load-test.md` 必须写明环境变量、Central/Redis/PostgreSQL 启动方式、命令、阈值和结果 JSON 保存位置；不得把一次本机结果写成永久基线。

- [ ] **Step 6: 提交本任务**

```powershell
git add tests/load/remote_agent_simulator.py tests/load/test_remote_action_scale.py docs/operations/remote-action-load-test.md
git commit -m "test: add remote action scale acceptance"
```

### Task 6: 全量回归和生产门禁确认

**Files:**
- Modify: `README.md`
- Create: `docs/operations/remote-action-production-gates.md`

- [ ] **Step 1: 运行协议、Central、Agent、适配器和 Console 定向套件**

```powershell
python -m pytest tests/test_remote_action_schemas.py tests/test_remote_action_contracts.py tests/test_remote_action_checksums.py tests/test_remote_action_identifiers.py tests/test_central_actions.py tests/test_central_work_orders.py tests/test_central_agent_gateway.py tests/test_central_effect_permits.py tests/test_central_remote_results.py tests/test_agent_remote_store.py tests/test_agent_remote_session.py tests/test_agent_remote_runtime.py tests/test_agent_local_debug.py tests/test_action_adapter_contract.py tests/test_central_browser_strategy_compiler.py tests/test_remote_browser_strategy_executor.py tests/test_central_comment_campaign_compiler.py tests/test_remote_comment_campaign_executor.py tests/test_agent_bootstrap.py tests/test_central_remote_bootstrap.py tests/test_remote_actions_e2e.py tests/test_remote_actions_faults.py tests/test_console_remote_action_api.py -q
node --test tests-js/action-checksums.test.js tests-js/console-actions.test.js tests-js/console-tasks.test.js tests-js/console-overview.test.js tests-js/console-receipts.test.js
```

Expected: PASS。

- [ ] **Step 2: 运行现有关键回归**

```powershell
python -m pytest tests/test_agent_integration.py tests/test_agent_execution_v2.py tests/test_execution_v2_service.py tests/test_comment_campaign_service.py tests/test_comment_campaign_integration.py tests/test_console_pages.py -q
```

Expected: PASS，本机动作设计/调试和旧数据读取无退化。

- [ ] **Step 3: 验证生产 fail closed**

```powershell
$env:AGENT_REMOTE_AUTH_MODE='production'
Remove-Item Env:AGENT_CREDENTIAL_PROVIDER -ErrorAction SilentlyContinue
python -m pytest tests/test_agent_remote_session.py -q -k production_auth
```

Expected: PASS，测试断言 Agent 拒绝连接且不会降级为 development credential。

- [ ] **Step 4: 写生产门禁文档**

明确列出：设备注册/凭据设计尚未批准、WSS TLS 终止、凭据轮换、审计保留、数据库备份恢复、500 Agent soak 结果。设备身份方案未另行确认前，状态必须为 `production_remote_execution: blocked`。

- [ ] **Step 5: 提交本任务**

```powershell
git add README.md docs/operations/remote-action-production-gates.md
git commit -m "docs: define remote action production gates"
```
