# PRD：业务控制系统（基于现有项目修订版）

**版本**：v2.0 | **状态**：草稿 | **前置文档**：原 v1.8 业务控制系统 PRD（技术选型与架构已按现有项目修订）

## 修订说明（相对 v1.8 的关键变更）

| # | 原 PRD | 修订后 | 原因 |
|---|---|---|---|
| 1 | 中控/Web 用 Go | **中控/Web 用 Python**（FastAPI + SQLAlchemy 2 + APScheduler），Agent 保持 Python | 现有系统全部 Python 资产（comment_campaign / execution_v2 / selector_probe / gateway），双栈成本不可接受 |
| 2 | 全新三件套（新 Web + 新中控 + 新 Agent） | **Web = 现有 Flask 管理后台演进；Agent = 现有执行器改造；中控 = 新 Python 服务（先与 Web 同进程，后按需独立）** | 最大化复用现有代码与测试 |
| 3 | PostgreSQL 为主库 | **中控数据用 PostgreSQL（Docker，多实例/回收器并发需要）；v1 单实例阶段允许 SQLite 起步；现有系统数据（accounts/execution_v2/comment_campaign/selector_probe）暂留 SQLite，经迁移章节逐步并库** | 现有 5 个 SQLite 库承载业务数据，一次性迁移风险高 |
| 4 | 消息骨干 NATS（JetStream） | **保留 NATS（nats-py）；过渡期允许 Redis/RQ 并存（现有 Comment Campaign 即 RQ），Outbox/Inbox 模式统一，逐步收敛到 NATS** | 现有 RQ/租约已有生产验证 |
| 5 | 中控部署于独立 Linux 服务器 | **v1：中控/Web 与现有系统同机部署（Windows 或 Linux Docker）；v2：按需拆独立 Linux 服务器** | 现有系统即 Windows 单机运行，先行收敛业务 |
| 6 | Agent 全新编写 | **Agent = 现有 Flask + execution_v2 + comment_campaign + selector_probe 改造**（见 4.2 复用映射） | 现有模块已实现 PRD 60% 概念 |
| 7 | 多租户全新设计 | **保留租户隔离目标；v1 以"单租户先跑通 + tenant_id 字段全链路预留 + 数据访问层强制过滤"落地，多租户激活列为 M4** | 现有系统无租户概念，全量改造需分期 |

---

## 1. 背景与目标

### 1.1 背景
现有系统：一台 Windows 主控机运行 Flask 模块化单体（管理后台 + Browser Execution V2 + Comment Campaign + Selector Probe + TikTok Stats），通过 AdsPower 浏览器分身执行 TikTok 业务动作，已具备单机版"任务下发、执行、结果汇总"的雏形（Campaign 批量、人工批准、回执验证、探针门禁）。

目标：在现有资产基础上，升级为**多机任务下发、执行监控、结果汇总**的业务控制系统，支持多租户隔离，中控可水平扩展。

### 1.2 目标
1. 客户在 Web 端一键创建任务，自动分配到多台电脑执行（现有 Campaign/策略执行器扩展为 Agent 内核）
2. 实时汇总展示所有电脑的执行进度与结果（新建看板）
3. 通过限流、重试策略、账户状态机管理，降低风控封禁风险（现有状态机扩展）
4. 支持多客户（租户）数据隔离（新增，v1 预留字段，M4 激活）
5. 中控与 Web 可演进部署（v1 同机 → v2 独立 Linux/Docker）

### 1.3 非目标（v1 不做）
与 v1.8 一致，另补充：
- 不重写现有 execution_v2 / comment_campaign / selector_probe 的业务逻辑（只做适配与扩展）
- 不将现有 5 个 SQLite 库一次性迁入 PostgreSQL（分期迁移，见第 14 章）
- 不在 v1 激活多租户（字段与过滤层先就位）

---

## 2. 术语定义

沿用 v1.8 术语，补充与现有系统的映射：

| 术语 | 定义 | 现有系统对应 |
|---|---|---|
| Strategy（执行策略） | 版本化动作契约 | `execution_v2.strategies` + `selector_probe` 版本/闸门 |
| Handle / 回执 | 动作产出的结构化证据 | `comment_campaign.receipts` + `action_intents` 原型 |
| DAG 依赖 | 子任务多父依赖 | `comment_campaign` 盖楼依赖（父评论 Handle 门禁） |
| 人工批准 | 不可逆动作前的人工确认 | `comment_campaign.approvals`（revision 一次消费） |
| PUBLISHED_UNVERIFIED | 结果无法确认的终态 | `comment_campaign` Assignment 同名终态 |
| 租约 / Fencing | 分配有效期 + generation | RQ worker owner-token TTL、Redis lease 原型 |
| Outbox | 事务发件箱 | 现有 5 张 SQLite Outbox 表（publication/webhook/probe_effect/element_request/management_settings） |
| PROBE | 试探任务 | `selector_probe`（只读状态动作白名单） |
| 容量 / 部署子任务 | 账号部署到目标机 | 新建（AdsPower 集成复用 `adspower.py` / `execution_v2.adspower_adapter`） |

---

## 3. 角色、权限与租户

沿用 v1.8 角色表。现有 `gateway/auth_store.py` 的 administrator/operator 角色映射为"管理员 / 客户·运营"；新增只读观察者角色与权限点（`task:create` 等）沿用 v1.8。

租户隔离落地顺序：
1. v1：数据模型增加 `tenant_id` 字段（可空，默认单租户），NATS Subject 预留 `{tenant_id}/` 前缀
2. M4：数据访问层强制过滤 + Subject ACL 激活

---

## 4. 整体业务流程

```
Web 端（Flask 管理后台演进）创建任务（定时/优先级/配置快照冻结）
  → 中控（Python，先与 Web 同进程）校验（DAG/配额/租户）
  → 拆分子任务（dependency_edges）→ 优先级出队 + Profile+负载+容量感知分配
  → Outbox 事务落库 → 中继发布（NATS JetStream，过渡期 Redis/RQ）
  → Agent（现有执行器改造）接收（租约校验）→ 窗口调度器（滑动窗口+优先级队列）
  → 策略契约执行（复用 execution_v2 动作/定位/拟人化）→ 意图+回执（复用 comment_campaign receipts）
  → 结果/状态事件回传 → 中控 Inbox 去重 + CAS 落库
  → WebSocket（快照+序号续传）推送到 Web 端
```

### 4.1 运行拓扑（v1）

```
Windows 主控机（现有 launcher.py 演进为"中控+Web"宿主）
├─ Flask Web/API（管理后台演进：任务/设备/账号/看板）
├─ 中控服务（同进程或独立进程：调度/分配/回收器/Outbox 中继）
├─ Redis（现有：队列/租约/心跳；新增：WebSocket 序号缓冲）
├─ PostgreSQL（中控数据；v1 允许 SQLite 起步）
├─ NATS（JetStream；过渡期与 RQ 并存）
└─ 现有 Worker（Comment Campaign RQ Worker / Selector Probe / TikTok Stats）

执行电脑（每台 = 一个 Agent）
└─ Agent（现有执行器改造：Flask 可选 / 纯 worker 形态）
   ├─ Execution V2 内核（策略契约执行）
   ├─ Comment Campaign 内核（意图/回执/人工批准）
   ├─ Selector Probe（PROBE）
   └─ AdsPower 网关（现有 adspower.py + 升级阶梯）
```

### 4.2 复用映射（Agent 内核改造范围）

| 现有模块 | 改造动作 | 对应 PRD 需求 |
|---|---|---|
| `execution_v2/`（elements/strategies/scheduler/executor/locator/actions） | 策略契约执行内核；新增"策略拉取 + config_snapshot"接入 | F20/F18/F19 |
| `comment_campaign/`（domain 状态机/receipts/approvals/queueing/worker） | 意图+回执验证内核；接入租约/Fencing | F21/F13/F15 |
| `selector_probe/` | PROBE 门禁与人工确认 | F14 |
| `gateway/auth_*` | 角色/权限扩展 + 租户字段 | F7 |
| `gateway/settings_store.py` | 配置版本化（version/effective_at/灰度） | F6 |
| `adspower.py` / `execution_v2/adspower_adapter.py` | 串行网关 + 升级阶梯 + 部署执行器 | F19/F26 |
| `launcher.py` | 主控机进程编排（新增中控/回收器进程） | 部署 |
| 5 张 SQLite Outbox 表 | 模式抽象为通用 Outbox 库（迁 PG） | F12 |

---

## 5. 功能需求

沿用 v1.8 全部 F1-F26，按现状标注实现策略（`复用` / `改造` / `新建`）：

| 需求 | 实现策略 | 说明 |
|---|---|---|
| F1 账号管理（含部署状态机） | 改造 | `accounts.db` 扩展：新增 IMPORTED/DEPLOYING/WAITING_LOGIN/FAILED 状态与容量字段 |
| F2 设备管理 | 新建 | 现有系统无设备概念；Agent 心跳上报复用现有 worker 心跳模式 |
| F3 任务管理 | 改造 | Campaign/执行任务统一为 Task/SubTask 模型；定时/截止/错过策略新建（APScheduler） |
| F4 看板 + F4a WebSocket 续传 | 新建 | Redis Stream + event_seq 模式 |
| F5 人工处理中心 | 改造 | 现有 Campaign 审批 / Probe 告警 / DLQ 概念扩展为统一中心 |
| F6 系统配置版本化 | 改造 | `settings_store.py` 增加 version/effective_at/灰度 |
| F7 权限租户 | 改造 | 现有 auth 扩展 |
| F8 策略版本管理 | 改造 | `execution_v2.strategies` + `selector_probe.versions` 统一为策略注册表 |
| F8a Agent 升级发布 | 新建 | Agent 包版本化（PyInstaller 已有基础）+ channel 灰度 |
| F9-F10 任务校验与 DAG | 改造 | Campaign 依赖机制泛化 + dependency_edges 表 |
| F11 Profile+负载分配 | 新建（算法）/改造（Profile 排他锁参照 Campaign profile_gateway） | 现有单机无跨机分配 |
| F12 消息骨干 | 改造 | NATS 引入；Outbox/Inbox 模式从现有 SQLite 抽象 |
| F12a 多实例并发 | 新建 | SKIP LOCKED / 咨询锁（PG）；SQLite 起步阶段用单实例锁 |
| F13 重试+租约回收 | 改造 | 现有 lease/心跳模式提升为 DB 驱动回收器 |
| F14 账户状态机 | 改造 | 现有 ACTIVE/BANNED 扩展为完整状态集 + PROBE 门禁（selector_probe 已有） |
| F15 结果聚合 / F15a DLQ Handler | 改造/新建 | 现有 Campaign receipt 流程泛化 |
| F17-F19 窗口调度/AdsPower 网关 | 改造 | execution_v2/scheduler + window_tiler + 新增滑动窗口队列与升级阶梯 |
| F20 策略契约 | 复用 | execution_v2 strategy 模型即契约；补 strategy_version/checksum 发布 |
| F21 动作幂等 | 复用 | comment_campaign receipts/approvals 即原型；补 action_intent 表 |
| F22-F23 失败分类/熔断 | 改造 | 现有错误分类扩展 |
| F24 心跳上报 | 改造 | 现有 worker 心跳 + capabilities |
| F25 Agent 灰度升级 | 新建 | |
| F26 账号部署执行器 | 新建 | 复用 adspower.py 创建 profile |

---

## 6. 状态机定义（唯一权威：统一迁移表）

沿用 v1.8 §6 全部状态与迁移表（任务/子任务/结果验证/账户/设备），对接现有系统：

- 现有 `comment_campaign` Assignment 状态机（planned/opening_profile/.../published_verified/published_unverified）作为**结果验证层**状态，挂接在 SubTask RUNNING 之下
- 现有 `execution_v2` Job 状态机（queued/running/completed/cancelled/cleanup_blocked）映射为 SubTask 运行态
- 现有账户 ACTIVE/BANNED 映射到新账户状态集（ACTIVE/SUSPENDED/...），迁移规则见第 14 章
- 状态码中文映射表（v1.8 §6.3）保留，前端 `status-map.ts` 改为现有前端 JS 字典（`gateway/static/` 内维护）

---

## 7. 数据模型概要

沿用 v1.8 表结构，调整如下：

| 表 | 来源/说明 |
|---|---|
| tenants / users / roles / permissions | 新建；users 迁移自 `management.db.management_users` |
| devices / device_profiles | 新建 |
| accounts | 迁移自 `accounts.db`，新增部署状态/容量字段 |
| tasks / subtasks / dependency_edges / handles | 新建；subtasks 对齐 `comment_campaign` Assignment + `execution_v2` execution_jobs 语义 |
| attempts / action_intents / receipts | receipts 迁移自 comment_campaign；intents 新建 |
| leases | 新建；v1 可沿用 Redis lease（现有模式），M3 落 PG 表 |
| outbox / inbox | 新建 PG 版；v1 可复用现有 SQLite outbox 模式 |
| dlq_items / audit_events / account_status_logs | 新建；audit 迁移自 management.db |
| strategies / configs / agent_releases | 新建；strategies 迁移自 execution_v2 + selector_probe 版本 |
| task_results / device_sessions | 新建 |

**数据迁移优先级**：accounts → tasks/subtasks → receipts → strategies → 其余。每张表迁移带 batch_id 幂等 + 回滚（见第 14 章）。

---

## 8. 非功能需求

沿用 v1.8，技术栈调整：

| 类别 | 要求（修订） |
|---|---|
| 性能 | 单任务 500+ 账号；中控水平扩展（M3 后）；看板延迟 ≤ 1s |
| 一致性 | 六铁律不变；Outbox/Inbox 模式沿用现有实现并抽象通用库 |
| 可靠性 | 现有 RQ/租约先保障 v1；NATS JetStream 在 M2 引入后逐步收敛 |
| 安全 | JWT（现有 Flask session 可过渡）+ RBAC + 租户预留；日志脱敏沿用现有 `redact_public_*` 模式（`browser_legacy.py`） |
| 风控 | 全部沿用 v1.8（现有系统已验证：pacing/人工门禁/UNVERIFIED 不自动重提/Profile 排他锁） |
| 兼容 | Agent 支持 Windows 10/11；AdsPower Local API v5+；中控可 Windows/Linux（Python 跨平台） |

---

## 9. 日志与监控

沿用 v1.8，调整：
- Python `structlog`（替代 Go slog）；现有日志体系（logs/ + JSONL）演进，`browser_legacy.record_browser_log` 脱敏模式复用
- 监控：Prometheus + Grafana；现有 /healthz、/ping 扩展为带租约/队列深度的指标
- 保留周期：Agent 本地 7 天；中控/API 30 天（Loki，可选）；audit_events ≥ 180 天

---

## 10. 迭代规划（修订）

| 迭代 | 范围 |
|---|---|
| M0 | 决策与契约：本 PRD 定稿、ADR（Python 技术栈/演进方式）、仓库规划、状态机唯一权威表、Docker Compose 原型（PG/Redis/NATS） |
| M1 | 基础设施 + 租户预留：中控骨架（FastAPI + SQLAlchemy）、NATS/Outbox/Inbox 通用库（复用现有模式）、RBAC 扩展、设备模型 + Agent 心跳/capabilities、部署执行器（F26） |
| M2 | 任务主线：Task/SubTask/DAG（复用 Campaign 依赖机制泛化）、分配器、租约/Fencing、Agent 内核适配（execution_v2 + comment_campaign 接入租约与策略拉取）、结果回传 |
| M3 | 运维能力：看板 + WebSocket 续传、人工处理中心、租约回收器（DB 驱动）、多实例并发控制、配置版本化、Agent 灰度升级 |
| M4 | 风控加固 + 多租户激活：账户状态机全量、熔断、幂等验证补全、WAL 窗口阶段机、混沌压测、租户隔离激活、存量 SQLite→PG 迁移收尾 |

每个迭代以 PRD 第 12 章验收标准（19 条）切片验收，优先完成与现有系统可联调的端到端场景（如：现有 Campaign 盖楼任务在新任务模型下端到端跑通）。

---

## 11. 风险与对策

沿用 v1.8 风险表，新增：

| 风险 | 对策 |
|---|---|
| 现有模块改造破坏已验证功能 | 符号契约测试 + 全量回归（现有 pytest/node:test 体系）+ 每迭代独立可回滚 |
| SQLite→PG 双写期不一致 | 分期迁移 + batch_id 幂等 + 切换前快照 + 并行观察期 |
| RQ→NATS 双骨干并存期消息语义分裂 | 统一 Outbox/Inbox 模式，协议适配层（v1.8 F12 已有设计） |
| 多租户后期补造成返工 | v1 即全链路预留 tenant_id + Subject 前缀，数据访问层过滤在 M4 前必须就位 |
| 中控单机起步后迁移 Linux 困难 | 中控纯 Python 无 Windows 依赖（与 Agent 的 pywin32 边界隔离） |
| 现有 23 个浏览器测试基线失败等遗留债 | 在 Agent 内核改造迭代中一并处理（已记录） |

---

## 12. 验收标准

沿用 v1.8 全部 19 条验收标准，补充与现有系统衔接的验收：

0. **存量联调**：现有 Comment Campaign 盖楼任务在新任务模型下端到端跑通（创建→拆解→分配→执行→人工批准→回执→看板展示），全量既有测试保持绿
20. **Agent 复用**：现有 execution_v2/comment_campaign 模块改造后，全部既有模块测试通过（符号契约 + 回归）
21. **渐进切换**：v1 中控与 Web 同机运行期，现有管理后台页面可继续使用（URL 兼容）

---

## 13. 部署与运维（修订）

### 13.1 v1 拓扑（同机起步）

```
Windows 主控机
├─ launcher.py 演进：Flask Web/API + 中控 + Worker（现有 4 进程 + 新增回收器/中继）
├─ PostgreSQL（Docker 可选；SQLite 起步可）
├─ Redis（现有）
└─ NATS（可选起步；RQ 过渡）
执行电脑：Agent（现有执行器打包，PyInstaller）

v2 演进：中控+Web 迁独立 Linux/Docker（Python 跨平台，直接复用代码）
```

### 13.2 端口与网络、高可用、Agent 安装升级

沿用 v1.8 原则；Agent 升级沿用现有 PyInstaller 打包基础 + NSSM 服务化（新增）。

---

## 14. 存量系统迁移方案（修订）

四阶段与 v1.8 一致（盘点/策略迁移/数据迁移/切换回滚），调整：

### 14.1 盘点与映射
- 现有 accounts.db / management.db / execution_v2.db / comment_campaign.db / selector-probe.db 全量盘点（已有一键备份脚本 `scripts/backup_all.py` 作前置保障）
- 现有 AdsPower Profile 即存量资产，直接映射为"已部署"（跳过创建），置 WAITING_LOGIN 或按现状初始化

### 14.2 策略迁移
- `execution_v2.strategies` + `selector_probe` 版本直接转化为策略契约（字段对齐，无需转译）；canary 验证沿用现有流程

### 14.3 数据迁移
- 按第 7 章优先级逐表迁移，batch_id 幂等；历史 Campaign 结果映射 task_results（已确认项标 VERIFIED）

### 14.4 切换与回滚
- v1 中控与现有系统**同进程共运行**（不是并行两套系统），页面 URL 兼容 → 观察 2-4 周 → 关闭旧数据写入路径 → SQLite 只读归档
- 回滚：切换前快照（备份脚本）+ 全量测试绿

---

## 15. 一致性、安全与执行契约

沿用 v1.8 §15 全部内容（六铁律、故障处理矩阵、Agent 窗口阶段机、压测断言、安全契约），补充：
- 六铁律第 6 条"唯一约束兜底"：现有系统已实践（Profile 排他锁、approval 一次消费、receipts 唯一性），新系统必须保持同等强度
- 现有 `comment_campaign` 的一致性设计（revision CAS、租约、不确定提交不重试）作为新系统实现的**参考实现**，直接对齐测试

---

## 附录 A：待办决策清单（已确认，2026-08-08）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 中控进程形态：同进程（Flask 内嵌）vs 独立进程 | **独立进程**（launcher 监督，回收器/中继独立启停） |
| 2 | 数据库起步：SQLite vs PG | **SQLite 起步**（单实例）；引入回收器/多实例时切 PG |
| 3 | NATS 引入时点：M1 直接引入 vs RQ 过渡 | **RQ 过渡，M2 引入 NATS** |
| 4 | 多租户激活时点 | **v1 全链路预留字段/前缀，M4 激活** |
| 5 | 新 Web 前端技术 | **沿用 Jinja + 原生 JS**（现有体系） |

对应架构决策见 `docs/architecture/adr/ADR-0010-browser-control-system-python-evolution.md`。
