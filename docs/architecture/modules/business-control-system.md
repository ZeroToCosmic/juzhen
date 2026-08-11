# 业务控制系统（BCS）交接文档

**版本**：1.0 | **日期**：2026-08-11 | **对象**：`central/`、`agent/`、`gateway/routes_bcs.py`、`gateway/templates/bcs.html`、`gateway/static/bcs.js`、`infra/`、`scripts/smoke_adspower.py`
**前置文档**：`docs/PRD-业务控制系统.v2.md`（需求）、`docs/architecture/adr/ADR-0010-browser-control-system-python-evolution.md`（决策）
**证据等级**：`源码确认`（全部）、`测试确认`（132 pytest + 9 node 测试）、`运行时未验证`（AdsPower 真实执行）

---

## 1. 系统概览

业务控制系统（Business Control System, BCS）在现有单机 Flask 系统之上新增的**多机任务调度与执行**子系统：Web 端创建任务 → 中控（central）拆解/分配/监控 → Agent（每台 Windows 电脑）拉取并真实执行浏览器动作 → 结果回传汇总。

```
浏览器 → Flask(5000) 管理后台 BCS 页面 → JS fetch/WS → central(8000) FastAPI
                                                        │
                 central: 设备/账号/任务/DAG/分配/租约/Outbox-Inbox/看板/DLQ/配置/WS
                                                        │ HTTP 拉取
                                                Agent(每台 Windows)
                                                     ├─ CentralClient（HTTP）
                                                     ├─ ExecutionV2Executor（真实执行，复用 execution_v2）
                                                     └─ WindowWal（断电恢复）
```

**运行模式**：中控 = 独立 FastAPI 进程（SQLite 起步，可切 PostgreSQL）；Agent = 进程内库（`agent/`）供打包为独立 worker；Web = 现有 Flask 新增页面。

---

## 2. 目录与文件地图

### 2.1 central/（中控服务，FastAPI + SQLAlchemy 2）

| 文件 | 功能 | 关键符号 |
|---|---|---|
| `central/app.py` | FastAPI 装配、CORS、事件总线初始化、WS 路由 | `app`、`event_store`、`ws_events` |
| `central/config.py` | 全部环境变量配置（见 §8） | `CENTRAL_DB_URL`、`LEASE_TIMEOUT_SECONDS` 等 |
| `central/models.py` | SQLAlchemy 模型（Base 派生） | Tenant/User/Device/DeviceSession/Account/ImportJob/DeployTask/Task/SubTask/DependencyEdge/Handle/TaskResult/ConfigSetting/ConfigVersion |
| `central/db.py` | 引擎与会话管理 | `get_engine`（SQLite 动态路径/PG URL）、`session_scope`（事务上下文）、`get_session`（FastAPI 依赖）、`init_db` |
| `central/security.py` | 租户上下文与权限守卫 | `require_tenant`（X-Tenant-ID header）、`require_permission` |
| `central/permissions.py` | 角色→权限矩阵 | `ROLE_PERMISSIONS`、`has_permission` |
| `central/events.py` | WS 事件总线 | `EventStore` 协议、`RedisEventStore`（XADD/XRANGE）、`MemoryEventStore` |
| `central/outbox.py` | 事务发件箱（先落库后发消息） | `OutboxMessage`、`add_outbox`、`claim_batch`、`mark_sent/mark_failed` |
| `central/inbox.py` | 消费幂等去重 | `InboxMessage`（msg_id+subject 联合主键）、`try_dedupe` |
| `central/relay.py` | Outbox 中继（v1 日志发布器） | `relay_pending`、`LoggingPublisher` |
| `central/allocation.py` | 容量分配器（水位最低） | `select_device` |
| `central/assignment.py` | 子任务调度器 | `dispatch_queued`（优先级出队+Profile 排他锁+在线设备） |
| `central/leases.py` | 租约管理 + 回收器（Fencing） | `renew_lease`（generation 校验）、`reclaim_stale`（超时回收→QUEUED/DLQ） |
| `central/dependencies.py` | Handle 门禁与依赖激活 | `submit_handle`、`activate_ready_dependents` |
| `central/tasks.py` | 任务创建（DAG 校验/快照冻结/定时解析） | `create_task`、`detect_cycle`（Kahn）、`parse_iso` |
| `central/scheduler.py` | 调度 tick + 子任务生命周期 API | `scheduler_tick`、`scheduled_tick`、`probe_tick`、`agent_pull_subtasks`、`submit_result`、`lease_renew`、`handle_submit` |
| `central/devices.py` | 设备管理 API | `list_devices`、`get_device_detail`、`update_device`、`report_offline` |
| `central/accounts.py` | 账号导入/部署 API | `import_accounts`、`import_job_status`、`list_accounts` |
| `central/account_states.py` | 账户业务状态机（PRD #12-18） | `ACCOUNT_TRANSITIONS`、`update_account_status` |
| `central/human_review.py` | DLQ 人工处理 | `list_dlq`、`requeue_dlq`、`terminate_dlq` |
| `central/dashboard.py` | 看板统计 | `dashboard_summary` |
| `central/settings.py` | 配置版本化 | `put_config`（scope=global/tenant）、`get_config`、`list_effective_configs` |
| `central/websocket.py` | WS 端点实现（快照/回放/背压） | `websocket_events`、`build_snapshot` |
| `central/migrate.py` | 数据迁移工具（SQLite→PG） | `migrate`（依赖排序/类型转换） |

### 2.2 agent/（Agent 侧库）

| 文件 | 功能 | 关键符号 |
|---|---|---|
| `agent/config.py` | Agent 环境变量（见 §8） | `CENTRAL_BASE_URL`、`AGENT_DEVICE_ID` 等 |
| `agent/client.py` | central HTTP 客户端 | `CentralClient`：`heartbeat`/`pull_subtasks`/`renew_lease`/`submit_result`/`submit_handle`；异常 `CentralError` |
| `agent/protocol.py` | 执行器协议 | `Executor` 协议、`ExecutionOutcome`、`StubExecutor`（测试桩） |
| `agent/worker.py` | Agent 主循环 | `AgentWorker`：`start`（心跳线程）/`run_once`/`run_forever`/`_process` |
| `agent/execution_v2_executor.py` | 真实执行适配器 | `ExecutionV2Executor`：AdsPower start→CDP connect→StrategyExecutor.run→close→stop，错误分类（F22），stage 间租约续期 |
| `agent/wal.py` | 本地 WAL 窗口阶段机（断电恢复） | `WindowWal`：`set_stage`/`set_generation`/`recover`（NEW/STARTING→abandon、RUNNING→aborted、SUBMITTING/VERIFYING→unverified） |

### 2.3 Web 侧（Flask）

| 文件 | 功能 |
|---|---|
| `gateway/routes_bcs.py` | 3 个页面路由（`/bcs`、`/bcs/devices`、`/bcs/tasks`） |
| `gateway/templates/bcs.html` | BCS 单页（看板/设备/任务三面板），复用 `_dashboard_sidebar.html` |
| `gateway/static/bcs.js` | 前端逻辑：central API 调用 + WebSocket 实时刷新 + 轮询兜底 |
| `gateway/templates/_dashboard_sidebar.html` | 侧边栏新增"业务控制系统"入口 |

### 2.4 基础设施与脚本

| 文件 | 功能 |
|---|---|
| `infra/docker-compose.yml` | PG/Redis/NATS 单节点原型（v1） |
| `infra/README.md` | 使用与生产形态说明 |
| `scripts/smoke_adspower.py` | 真实 AdsPower 烟雾验收工具（link/strategy 两级） |
| `scripts/backup_all.py` / `.cmd` | 一键全量备份（此前交付） |

---

## 3. HTTP API 参考（central，全部需 `X-Tenant-ID` header）

> 统一响应：成功 `{"data": ...}` 语义（当前为直出对象）；错误 `{"detail": "..."}`（FastAPI 默认）。
> JWT 未实现——当前以 header 模拟租户上下文（见 §11 限制）。

### 3.1 运维

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | central 存活检查 `{"status":"ok"}` |
| WS | `/ws/events?tenant_id=X&last_seq=Y` | 实时事件通道（见 §4） |

### 3.2 设备

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/central/devices/heartbeat` | 心跳+能力上报。请求：`{tenant_id, device_id, session_id, agent_version, capabilities, channel, max_accounts, used_accounts, inventory_epoch, running_windows, queue_depth}`；响应 `{device_id, status}`。Device 幂等 upsert + DeviceSession 留痕 |
| GET | `/api/central/devices` | 设备列表（租户过滤）；心跳超 90s 自动判 offline |
| GET | `/api/central/devices/{device_id}` | 设备详情 |
| PATCH | `/api/central/devices/{device_id}` | 更新 `{name, enabled, channel(dev/canary/stable), max_accounts}` |
| POST | `/api/central/devices/{device_id}/offline` | 显式离线 |

### 3.3 账号

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/central/accounts/import` | 批量导入+自动部署。请求 `{accounts:[{account_id,tiktok_identity,ads_power_params}], dry_run}`；同事务：ImportJob+Account+DeployTask+容量占用+Outbox `{tenant}/account.deploy`；失败原因 `duplicate_account`/`no_device_capacity` |
| GET | `/api/central/accounts/import/{job_id}` | 导入批次状态 |
| GET | `/api/central/accounts` | 账号列表（部署/业务状态、权威设备、revision） |
| GET | `/api/central/accounts/devices` | 可分配设备水位视图 `{water_level}` |
| POST | `/api/central/accounts/{account_id}/status` | 业务状态迁移（§6.2 表，非法 409） |

### 3.4 任务与调度

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/central/tasks` | 创建任务。请求 `{task_type(publish/browse/comment/like/follow/deploy), params, account_ids, strategy_version, priority, deadline(ISO), schedule:{run_at(ISO),missed_policy(immediate/skip)}, config_snapshot, dependencies:[{parent_account_id,child_account_id,required_handle_schema}]}`；校验：账号 ACTIVE、DAG 环、类型/优先级/时间格式；无调度→QUEUED，有→PENDING；同事务写 Task+SubTasks+DependencyEdges+Outbox `{tenant}/task.created` |
| POST | `/api/central/scheduler/tick` | 一轮：回收→依赖激活→分配。响应 `{reclaim:{reclaimed,dlq}, activation:{activated,failed}, dispatch:{assigned,skipped}}` |
| POST | `/api/central/scheduler/scheduled` | 定时触发：run_at 到期→QUEUED（事件 `task.started`）；错过>15min 默认 skip→MISSED（事件 `task.missed`）；deadline 到期→MISSED；CAS 幂等 |
| POST | `/api/central/scheduler/probe` | PROBE 门禁：MANUAL_VERIFIED 账户（冷却结束+无活动探针）→ 创建低频 browse 试探任务 |
| GET | `/api/central/agent/subtasks?device_id=X` | Agent 拉取本机 ASSIGNED/RUNNING 子任务（含 config_snapshot、lease_generation、lease_timeout_at） |
| GET | `/api/central/subtasks` | 子任务列表（状态/设备/generation/attempts/revision） |

### 3.5 子任务生命周期

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/central/subtasks/lease/renew` | 续租 `{subtask_id, device_id, generation}`；generation/owner 不匹配→409 `stale generation`（Fencing） |
| POST | `/api/central/subtasks/handle` | 提交 Handle `{subtask_id, verification_status(VERIFIED/UNVERIFIED), content, text_hash}`→子任务 SUCCESS |
| POST | `/api/central/subtasks/result` | 结果回传 `{subtask_id, device_id, generation, status(SUCCESS/FAILED), error_category, error_code, result_data, duration_ms, msg_id}`；流程：Inbox 去重（重复 409）→ generation CAS（旧代 409）→ 写 TaskResult + 状态迁移（SUCCESS→SUCCESS；retryable 且 attempts≤3→QUEUED(generation+1)；否则→DLQ）；熔断（连续失败≥3→账户 SUSPENDED）；PROBE 结果解析（成功→ACTIVE/失败→CAPTCHA）；响应含 `circuit_broken`/`probe_resolved` |

### 3.6 人工处理与看板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/central/dlq` | DLQ 列表（含错误分类/码） |
| POST | `/api/central/dlq/{id}/requeue` | 重派（→QUEUED，generation+1，attempts 重置 0） |
| POST | `/api/central/dlq/{id}/terminate` | 终止（→CANCELLED） |
| GET | `/api/central/dashboard/summary` | 看板：今日任务/成功率/窗口/排队/DLQ/在线设备 |

### 3.7 配置

| 方法 | 路径 | 说明 |
|---|---|---|
| PUT | `/api/central/configs/{key}` | 写配置 `{value, gray_ratio, scope(global/tenant)}`；tenant_id=""=全局；version+1 + 历史行同事务 |
| GET | `/api/central/configs/{key}` | 读生效值（租户覆盖全局） |
| GET | `/api/central/configs` | 生效视图（合并） |

---

## 4. WebSocket 协议（/ws/events）

**握手**：连接即推全量快照 `{"type":"snapshot","payload":{"subtask_counts":{...},"total_subtasks":N}}` → 按 `last_seq` 回放缺失事件 → 实时推送。
**事件帧**：`{"seq":"<stream-id>","type":"<event>","payload":{...}}`；心跳 `{"type":"ping"}`（30s）。
**事件类型**：`task.created` / `task.started` / `task.missed` / `subtask.result`（含 circuit_broken/probe_resolved）/ `account.status` / `account.circuit_broken` / `account.probe_scheduled`。
**背压**：发送队列上限 2000，超限断开（code 4401，客户端走快照重连）。
**存储**：`RedisEventStore`（XADD MAXLEN 1000）优先，Redis 不可用降级 `MemoryEventStore`（生产必须 Redis）。

---

## 5. Outbox 主题与消费

| subject | 生产者 | 载荷 |
|---|---|---|
| `{tenant}/task.created` | 任务创建 | task_id/task_type/subtask_count |
| `{tenant}/account.deploy` | 账号导入分配 | account_id/device_id/ads_power_params |
| `{tenant}/subtask.assigned` | 调度分配 | subtask_id/account_id/device_id/lease_generation/config_snapshot |

**中继**：`relay_pending(session, publisher)` 领取→发布→mark_sent；失败 mark_failed（退避重试）。v1 为 `LoggingPublisher`；NATS 发布器为后续接入点（§11）。

---

## 6. 状态机（源码实现位置）

| 实体 | 状态 | 实现 |
|---|---|---|
| Task | PENDING/QUEUED/MISSED/… | `scheduler.py scheduled_tick`（PENDING→QUEUED/MISSED），迁移 #22 |
| SubTask | QUEUED/ASSIGNED/RUNNING/SUCCESS/FAILED/DLQ/CANCELLED/WAITING_DEPENDENCY | `assignment.py dispatch_queued`（#3）、`leases.py reclaim_stale`（#4/#5）、`dependencies.py`（#1/#2）、`scheduler.py submit_result`（#6/#7）、`human_review.py`（重派/终止） |
| 账户（deploy） | IMPORTED/DEPLOYING/WAITING_LOGIN/ACTIVE/FAILED | `accounts.py import_accounts`（#25 起步；WAITING_LOGIN/ACTIVE 由 Agent 回执驱动，Agent 侧未全量实现） |
| 账户（business） | ACTIVE/CAPTCHA/MANUAL_VERIFIED/SUSPENDED/MANUAL_REVIEW | `account_states.py ACCOUNT_TRANSITIONS`（#12-18 唯一权威）+ 熔断（#16） + PROBE（#13/#14/#15） |
| 结果 | VERIFIED/UNVERIFIED/PUBLISHED_UNVERIFIED | PRD 语义；当前 `handle.verification_status`（VERIFIED/UNVERIFIED），PUBLISHED_UNVERIFIED 由 Agent 执行层上报（ExecutionV2Executor 映射预留） |

---

## 7. Agent 工作流（agent/）

```
AgentWorker.run_once()
  ├─ client.pull_subtasks()                    # HTTP 拉取本机 ASSIGNED/RUNNING
  ├─ for each subtask: _process(subtask)
  │    ├─ executor.execute(subtask)            # ExecutionV2Executor（真实）或 StubExecutor（测试）
  │    │    └─ 生命周期：adspower.start → PlaywrightSessionFactory.connect
  │    │        → StrategyExecutor.run（navigate/readiness/actions）→ browser.close → adspower.stop
  │    │    └─ stage 间调 lease_renewer（续租）
  │    ├─ SUCCESS：先 submit_result → 再 submit_handle（有 handle 时）
  │    └─ FAILED：submit_result（error_category/error_code）
  └─ 心跳线程（30s）：heartbeat（capabilities/容量/窗口数）
```

**WAL 集成**：真实执行前 `wal.set_stage(subtask_id, STAGE_RUNNING)`，提交阶段 `SUBMITTING/VERIFYING`，完成 `DONE`；进程重启后 `wal.recover()` 按 §2.2 规则处置（当前 worker 主循环已含 WAL 库，接线点：`ExecutionV2Executor` 调用前/后，见 §11 限制）。

---

## 8. 配置项（环境变量）

### central

| 变量 | 默认 | 说明 |
|---|---|---|
| `CENTRAL_DB_PATH` | `data/central/central.db` | SQLite 路径 |
| `CENTRAL_DB_URL` | 基于上面 | SQLAlchemy URL（设 PG URL 即切换数据库，见 §9） |
| `CENTRAL_REDIS_URL` | `redis://127.0.0.1:6379/0` | 事件总线 Redis |
| `CENTRAL_CORS_ORIGINS` | `http://127.0.0.1:5000,http://localhost:5000` | 允许的 Web 来源（逗号分隔） |
| `HEARTBEAT_ONLINE_SECONDS` | 90 | 心跳超时判离线 |
| `LEASE_TIMEOUT_SECONDS` | 300 | 租约无进展回收阈值 |
| `MAX_RETRY_ATTEMPTS` | 3 | 重试上限（超限→DLQ） |
| `MISSED_WINDOW_SECONDS` | 900 | 定时任务错过判定窗口 |
| `ACCOUNT_COOLDOWN_SECONDS` | 7200 | MANUAL_VERIFIED 后 PROBE 冷却 |

### agent

| 变量 | 默认 | 说明 |
|---|---|---|
| `CENTRAL_BASE_URL` | `http://127.0.0.1:8000` | central 地址 |
| `AGENT_TENANT_ID` / `AGENT_DEVICE_ID` / `AGENT_SESSION_ID` | tenant-default / 空 | 身份 |
| `AGENT_VERSION` | 0.1.0 | 心跳上报版本 |
| `AGENT_HEARTBEAT_INTERVAL` / `AGENT_RENEW_INTERVAL` | 30 / 60 | 心跳/续租间隔（秒） |
| `AGENT_REQUEST_TIMEOUT` | 15 | central 请求超时（秒） |
| `AGENT_MAX_CONCURRENT_WINDOWS` | 3 | 窗口并发上限（当前 worker 预留，未接入调度） |

---

## 9. 部署与启动

### SQLite 模式（开发/单实例）

```powershell
pip install -r requirements.txt
.venv\Scripts\python.exe -m uvicorn central.app:app --port 8000   # central
.venv\Scripts\python.exe app.py                                    # Flask（含 /bcs 页面）
```

### PostgreSQL 模式（生产）

```powershell
docker compose -f infra/docker-compose.yml up -d postgres
# 迁移：源 SQLite → 目标 PG（依赖排序 + JSON/Boolean 转换，外键安全）
python -c "from sqlalchemy import create_engine; from central.migrate import migrate; \
  migrate(create_engine('sqlite:///data/central/central.db'), \
          create_engine('postgresql+psycopg2://bcs:<pwd>@127.0.0.1:5432/bcs'))"
$env:CENTRAL_DB_URL = "postgresql+psycopg2://bcs:<pwd>@127.0.0.1:5432/bcs"
uvicorn central.app:app --port 8000
```

> 已在本机容器验证：16 表迁移 + 心跳/任务/分配/结果/看板全链路通过（`02a63b7` 提交）。
> 迁移重复执行到非空目标会因主键冲突失败（业务层幂等负责）。

### Agent 打包

`agent/` 为库；`AgentWorker(CentralClient, ExecutionV2Executor).run_forever()` 为入口；Windows 服务化（NSSM）+ PyInstaller 打包属后续（PRD F25 未实现）。

---

## 10. 测试体系

| 文件 | 覆盖 |
|---|---|
| `tests/test_central_skeleton.py` | 心跳/租户字段/在线判定（6） |
| `tests/test_central_devices.py` | 设备 CRUD + 租户隔离（8） |
| `tests/test_central_outbox_inbox.py` | 事务性/去重/中继/退避（7） |
| `tests/test_central_accounts.py` | 导入/容量/水位/dry-run（8） |
| `tests/test_central_tasks.py` | DAG/环检测/快照冻结/租户（8） |
| `tests/test_central_scheduler.py` | 分配/续租/回收/Handle 门禁/排他锁（10） |
| `tests/test_central_dlq_dashboard.py` | DLQ 操作/看板（7） |
| `tests/test_central_configs.py` | 配置版本化/覆盖/历史（6） |
| `tests/test_central_websocket.py` | WS 快照/回放/隔离（6） |
| `tests/test_central_scheduled.py` | 定时/MISSED/幂等（9） |
| `tests/test_central_account_states.py` | 账户状态机/熔断（7） |
| `tests/test_central_probe.py` | PROBE 门禁链路（6） |
| `tests/test_agent_integration.py` | Agent 端到端（8） |
| `tests/test_agent_execution_v2.py` | 执行适配器编排（8） |
| `tests/test_agent_wal.py` | WAL 恢复规则（7） |
| `tests/test_central_tenant_matrix.py` | 多租户激活矩阵（3） |
| `tests/test_chaos.py` | 租约撕裂/重放/双执行/恢复（8） |
| `tests/test_central_migrate.py` | 迁移工具（4） |
| `tests/test_bcs_pages.py` | BCS 页面渲染（5） |
| `tests-js/bcs-ui.test.js` | 前端 fetch/WS/渲染（6） |

运行：`pytest tests/test_central_*.py tests/test_agent_*.py tests/test_chaos.py tests/test_bcs_pages.py`；`node --test tests-js/bcs-ui.test.js`。

---

## 11. 已知限制与待办（诚实清单）

| 项 | 状态 | 说明/接入点 |
|---|---|---|
| **NATS 消息骨干** | ❌ 未实现 | Agent 用 **HTTP 拉取**（pull-based）替代推送订阅；Outbox 中继 `relay_pending` 的 Publisher 是 NATS 接入点（替换 `LoggingPublisher`）；事件总线 `RedisEventStore` 已可用 |
| **JWT 认证** | ❌ 未实现 | 当前 `X-Tenant-ID` header 模拟租户（`central/security.py require_tenant` 是替换点）；`require_permission` 已就绪未接线 |
| **前端完整性** | 🔶 基础版 | 只有看板/设备/任务 3 页；人工处理中心、MISSED 列表、迁移工单页面未做（API 已齐：`/api/central/dlq` 等） |
| **AdsPower 真实执行** | ⚠️ 待人工验收 | `scripts/smoke_adspower.py`（link/strategy 两级）+ 手册 `docs/superpowers/plans/2026-08-11-agent-adspower-smoke-acceptance.md`；需真实环境执行 |
| **WAL 接线** | 🔶 库已就绪 | `agent/wal.py` 已实现+测试；`ExecutionV2Executor` 调用前后接线（set_stage）待接入 worker 主循环 |
| **混沌真实注入** | 🔶 协议级 | `test_chaos.py` 用 API 模拟；真实 NATS 断开/断电注入待做 |
| **Agent 灰度升级（F25）** | ❌ 未实现 | `agent_releases` 模型已建，升级流程未做 |
| **PUBLISHED_UNVERIFIED 上报** | 🔶 部分 | 语义在 Handle/结果层预留；Agent 真实执行的回执判定需在烟雾验收中校准 |
| **多租户激活** | 🔶 就绪未全量 | 数据层过滤 + 矩阵测试已过；Subject ACL / JWT 激活后即为完整 |

---

## 12. 开发规范（新增功能的固定动作）

1. **新 API**：`central/` 内建 `router` → `central/app.py` 注册 → 测试（租户隔离断言必须）→ 补本文档 API 表
2. **新表**：`central/models.py` 或对应模块（outbox/inbox 在各自模块）→ 迁移工具依赖排序表补条目
3. **新状态**：对应模块迁移函数 + CAS（revision/generation）→ 状态机文档同步
4. **新事件**：`event_store.publish` + WS 事件表补条目；前端 `bcs.js refreshByEvent` 接线
5. **租户铁律**：所有查询必须带 `tenant_id` 过滤（数据访问层强制）
6. **一致性铁律**（PRD 15.1）：先落库后发消息（Outbox）、消费 Inbox 去重、Fencing 校验、状态迁移走唯一权威表
