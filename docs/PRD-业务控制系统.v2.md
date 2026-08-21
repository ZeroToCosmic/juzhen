# PRD：业务控制系统

**版本**：v3.0 | **日期**：2026-08-12
**前置文档**：`docs/architecture/adr/ADR-0010-browser-control-system-python-evolution.md`（技术决策）、`docs/架构改进方案.md`（500 台规模强化）、`docs/superpowers/specs/2026-08-12-central-business-modules-and-agent-executors-design.md`（M5 实施蓝图）、`docs/architecture/modules/business-control-system.md`（实现现状交接）

---

## 1. 背景与目标

### 1.1 背景
现有系统：一台 Windows 主控机运行 Flask 模块化单体（管理后台 + Browser Execution V2 + Comment Campaign + Selector Probe + TikTok Stats），通过 AdsPower 浏览器分身执行 TikTok 业务动作，已具备单机版"任务下发、执行、结果汇总"的雏形（Campaign 批量、人工批准、回执验证、探针门禁）。

目标：升级为**多机任务下发、执行监控、结果汇总**的业务控制系统，支持外部 SaaS 多租户，中控可水平扩展。**规模目标：500 台执行机**。

### 1.2 目标
1. 客户在 Web 端一键创建任务，自动分配到多台电脑执行
2. 实时汇总展示所有电脑的执行进度与结果（看板）
3. 通过限流、重试策略、账户状态机管理，降低风控封禁风险
4. 多客户（租户）数据隔离（轻租户：客户只见自己的账号/任务/结果）
5. 中控与 Web 可演进部署（v1 同机双进程 → v2 独立 Linux/Docker + central 多实例）

### 1.3 非目标
- 不重写现有 execution_v2 / comment_campaign / selector_probe 的业务逻辑（只做适配与扩展）
- **不搬迁各电脑本地 SQLite**（本地为事实源，Agent 同步上报汇聚，§14）
- v1 不引入 NATS（Agent 数据面 = HTTP + SSE 控制唤醒；NATS 升级条件见附录 A #3）
- v1 不做 JWT 生产化（X-Tenant-ID 过渡，JWT 为 central 侧规划项）
- **不创建 AdsPower Profile**：窗口为预置资源池，系统只同步/绑定，不接收指纹参数

---

## 2. 术语定义

| 术语 | 定义 | 现状对应 |
|---|---|---|
| 租约 / Fencing | 分配有效期 + generation CAS（旧代请求一律 409 拒绝） | `central/leases.py`（已实现） |
| 容量水位（water_level） | 设备可用容量视图，分配器选水位最低设备 | `central/allocation.py`（已实现） |
| config_snapshot | 任务创建时冻结的策略/配置快照，执行不随线上变更 | `central/tasks.py`（已实现） |
| 窗口 WAL | Agent 本地执行阶段机，断电/崩溃后恢复处置 | `agent/wal.py`（库已实现，接线待做） |
| fake-agent | 负载模拟器：模拟 N 台设备的心跳/拉取/执行/回传 | 未实现（M5 首项） |
| 窗口资源 | AdsPower 客户端内预置的浏览器窗口，**系统不创建**；每日定时同步状态 = 可用资源池 | 浏览器接管模块（Agent 侧，M5） |
| 账号绑定 | 导入账号 → 分配给未使用窗口（非强绑定：窗口固定、账号可变） | 账号绑定状态机（§6） |
| 浏览器接管模块 | Agent 侧窗口管理：列表/状态同步/绑定/登录驱动/失效清洗 | 待新建（M5） |
| 失效账号识别 | 自动判定账号不可用（banned/登录失效错误码 + 连续失败确认），与风控熔断 SUSPENDED（可恢复）区分 | §5 F1、§6 |
| 窗口清洗 | 失效确认后释放窗口：**立即软隔离（QUARANTINED 停分配）→ 延迟 10s 后台低优先级队列清洗（停窗口→清 cookie/storage）→ 上报已清洗 → 恢复 AVAILABLE**（决策 #18） | §5 F26 |
| WAITING_WINDOW / WAITING_CAPACITY | 资源不足时的等待状态（不失败），资源事件触发唤醒 | §6、§12 |
| egress_group | 窗口实际共享出口/代理风险域，用于同视频节点集中度限制 | M5（实施规格 §6.1） |

其余沿用：Strategy / Handle / 回执 / DAG 依赖 / 人工批准 / PUBLISHED_UNVERIFIED / Outbox / PROBE / 部署子任务。

---

## 3. 角色、权限与租户

角色沿用现有 auth_store 的 administrator/operator 映射（管理员 / 客户·运营），另设只读观察者。

**认证归属**：认证与授权归 **central**（JWT 规划，RBAC 矩阵 `central/permissions.py` 已就绪未接线）；v1 过渡期以 `X-Tenant-ID` header 模拟租户；Flask 页面经 HTTP/WS 携带租户标识访问 central。

**租户形态：外部 SaaS 轻租户**——客户只经 Web 端创建任务/查看看板与结果；**中控、设备、窗口资源、执行全部由运营方统一持有**，不归属客户。

| 实体 | 归属 | 隔离 |
|---|---|---|
| 设备 / 窗口资源池（profiles） | **运营方全局共享**（不归属客户，全局调度） | 不按租户隔离；客户不可见 |
| 账号（登录信息/绑定） | 客户（导入方） | **按租户隔离** |
| 任务 / 子任务 / 结果 / 看板 | 客户 | **按租户隔离** |
| 配置（F6） | 运营方默认 + 租户覆盖 | scope=global/tenant 已支持 |
| 客户配额（账号数/并发，M6） | 按租户 | SaaS 计费基础 |

**权限边界**：客户角色仅"租户内任务创建/查看"；设备/窗口管理、全局看板、清洗任务等仅运营方管理员；客户看不到资源池与其他租户。

**多租户激活**：`tenant_id` 全链路字段 + 查询强制过滤（矩阵测试已过）即为隔离骨架；激活时调整：设备/窗口表改全局视图、分配器全局水位分配 + 账号租户校验、客户入口（同一 Web 租户过滤 vs 独立客户门户，M6）。

---

## 4. 整体架构

### 4.1 业务流程

```
Web 端（Flask /bcs 页面）按业务模块创建任务（模块内选策略版本/模板，不选任务类型）
  → 模块→task_type→strategy_kind 隐式映射（§4.4）
  → central（:8000）校验（策略注册表存在性/快照合法/DAG/配额/租户）
  → 拆分子任务 → 优先级出队 + Profile 排他锁 + 容量水位分配
  → Outbox 事务落库 → 中继（v1 LoggingPublisher；NATS 为 v2 接入点）
  → Agent 收 SSE 唤醒（work_available 等轻量事件）→ HTTP 拉取权威工作单元
  → 租约校验（generation CAS）→ 窗口调度器（并发上限 3）
  → 执行器分发（strategy_kind → ExecutionV2Executor / CampaignExecutor）
  → 策略契约执行（execution_v2 动作/定位/拟人化；comment_campaign 意图/回执/人工批准）
  → 结果/状态事件回传（HTTP submit_result，Inbox 去重 + CAS 落库）
  → WebSocket（快照+序号续传）推送到 Web 端
```

### 4.2 运行拓扑（v1，同机双进程）

```
Windows 主控机（launcher.py 监督）
├─ central 服务（:8000：API + 调度 tick + 回收器 + Outbox 中继 + WS + SSE 控制面）
├─ Flask Web/API（:5000：管理后台 + /bcs 页面，JS 经 HTTP/WS 调 central）
├─ Redis（WS 事件流；不可用降级内存）
├─ PostgreSQL（Docker，开发/测试/生产一致）
└─ 现有 Worker（Comment Campaign RQ Worker / Selector Probe / TikTok Stats）

执行电脑（每台 = 一个 Agent）
└─ Agent
   ├─ CentralClient（SSE 控制唤醒 + HTTP 数据面：心跳/拉取/回传/续租/凭据 grant）
   ├─ ExecutionV2Executor（策略契约执行，stage 间续租）
   ├─ WindowWal（断电恢复）
   ├─ 浏览器接管模块（窗口同步/花名册上报/绑定/登录驱动/失效清洗）
   └─ AdsPower 客户端（窗口预置；本地 EXTERNAL_BUSY 标记兜底）
```

### 4.3 通信模型

- **Agent 数据面 = HTTP（权威）**：心跳、工作单元拉取、结果/增量上报、续租、凭据 grant
- **Agent 控制面 = SSE（单向轻量唤醒）**：每设备一条，事件仅 `work_available`/`approval_decided`/`task_paused`/`inventory_sync_requested`/`lease_revoked` 等聚合引用；Last-Event-ID 续传，序号缺口触发 HTTP 全量补拉；SSE 不可用降级低频 Pull（5s→60s 退避+抖动）；**禁止逐条审批 1s 固定轮询**
- **重连风暴防护（2026-08-12 定稿）**：
  - 客户端抖动：断开后 `Delay = Min(60, 2^retry × 2) + Random(0.5, 3.0)` 秒重连（指数退避 + 随机抖动）
  - 服务端保护：SSE 网关层连接速率限制（Ratelimit），超阈重连请求返回 **429** → Agent 降级低频 HTTP 轮询（60s 退避），防止 Uvicorn 线程池崩溃
  - SSE 端点实现为 **async**（不占线程池）；断开期间 HTTP Pull 兜底（数据不丢）
- **Web 端 = WebSocket 事件流**（快照+last_seq 续传，Redis 优先/Memory 降级）
- NATS 升级条件见附录 A #3

### 4.4 复用映射（Agent 内核改造范围）

| 现有模块 | 改造动作 | 需求 |
|---|---|---|
| `execution_v2/`（elements/strategies/scheduler/executor/locator/actions） | 策略契约执行内核（已接入 config_snapshot）；注册为**原子动作策略**（strategy_kind=v2） | F20/F18/F19 |
| `comment_campaign/`（domain/receipts/approvals/queueing/worker） | **策略化改造**：意图+回执验证内核接入租约/Fencing；**注册为复合业务策略（strategy_kind=campaign）** | F21/F13/F15 |
| `selector_probe/` | PROBE 门禁与人工确认 | F14 |
| `gateway/auth_*` | 角色/权限扩展 + 租户字段 | F7 |
| `gateway/settings_store.py` | 配置版本化（version/effective_at/灰度） | F6 |
| `adspower.py` / `execution_v2/adspower_adapter.py` | 串行网关复用 + **浏览器接管模块扩展**：窗口列表/状态同步、绑定/解绑、登录驱动、失效清洗；**不创建 Profile** | F19/F26 |
| `launcher.py` | 主控机进程编排（central/回收器）；**单机模式保留 = 单机开发调试工具** | 部署 |

### 4.5 策略注册表与调用链

**目标**：Web 端创建的任何任务都落到"一个策略（version + config_snapshot 冻结）+ 一个执行器"上执行；业务模块对任务系统只暴露策略契约。**用户不选择任务类型**——在哪个业务模块创建任务，就隐式代表执行哪种任务。

```
Web 端按业务模块创建任务（模块内只选策略版本/模板）
  → 模块→task_type→strategy_kind 隐式映射
  → central 校验（策略注册表 + 快照合法 + DAG/配额/租户）
  → Task/SubTask（task_type + strategy_version + config_snapshot + strategy_kind）
  → Agent 收 SSE 唤醒 → HTTP 拉取 → 执行器分发 → 执行 → 回执/结果
```

**Web 模块 → 任务映射**：

| Web 端模块（创建入口） | 隐含 task_type | 策略（strategy_kind） | 执行器 |
|---|---|---|---|
| 新增账号（导入账号） | deploy | 绑定语义：导入=绑定未使用窗口，无策略契约 | 浏览器接管模块（绑定/登录驱动，F26） |
| 评论任务模块 | comment | 复合业务策略（campaign：评论模板/盖楼依赖/意图回执/人工批准） | CampaignExecutor |
| 养号任务模块 | browse（可含 like/follow 参数） | 原子动作策略（v2 浏览器执行策略） | ExecutionV2Executor（已实现） |
| （预留）独立浏览/点赞/关注 | browse / like / follow | v2 策略 | ExecutionV2Executor |
| publish | 禁用 | 拒绝创建（浏览器内发布不做开发；发布沿用现有 Buffer 路径独立运行） | — |

**规则**：模块新增/拆分时更新上表；task_type 枚举与映射表是 central 校验的唯一权威（API 层保留显式 task_type 字段供映射写入，Web 用户不感知）。

**策略级别**：

| 级别 | 定义 | 来源 | 执行器 | 状态 |
|---|---|---|---|---|
| 原子动作策略（v2） | actions（move/scroll/click/input/wait）+ elements + readiness，随 snapshot 冻结下发 | `execution_v2`（已存在） | `ExecutionV2Executor`（已实现） | ✅ 链路已通（AdsPower 真实验收前置） |
| 复合业务策略（campaign） | 评论业务动作：浏览器动作 + 意图/回执验证 + 人工批准 + 盖楼依赖 | `comment_campaign`（待策略化） | `CampaignExecutor`（待开发） | ❌ M5 |

**统一策略注册表（F8，M5）**：`strategies` 表（central）+ 三源归集（execution_v2.strategies / comment_campaign 策略 / selector_probe.versions，经 Agent 引导同步 §14）；每条策略含 strategy_id、kind（v2/campaign）、version、checksum、schema、来源模块。**定义目录与发布（实施规格 §5）**：Central 为生产定义权威，revision 不可变；本机草稿须显式"发布到 Central"并经 Central 重新校验后才进生产；本机后续编辑不影响已冻结任务。

### 4.6 窗口资源模型

```
每台电脑 AdsPower 客户端（窗口预置，系统不创建）
  └─ 浏览器接管模块：账号花名册同步 + 浏览器同步（两功能均可触发，每日定时全量对账 + 实时增量上报）
        └─ 上报 central：窗口列表（window_ref/状态/绑定账号/egress_group 等同步参数）
central：窗口资源池（profiles 表）→ 可用窗口 = 未绑定状态
Web 导入账号（仅登录信息：账号名/密码/验证邮箱）
  → central 校验 + 分配未绑定窗口（无窗口 → WAITING_WINDOW 等待）→ 绑定任务（deploy）
  → Agent 登录驱动（人工或自动；验证码/2FA → WAITING_MANUAL_LOGIN）
  → 身份核验（实际 TikTok 身份 vs 导入预期 → 不一致 IDENTITY_REVIEW_REQUIRED）
  → 窗口=账号（ACTIVE）→ 失效自动识别（banned/登录失效错误码 + 连续失败 3 次确认）
  → 立即软隔离：Agent 内存 + Central 状态库置 QUARANTINED（隔离态，立即停新分配，
     不立刻调 AdsPower 关窗/清目录）
  → 异步延迟清洗：清洗任务进后台低优先级延迟队列，等 10s（Chrome 进程退出 +
     文件锁释放）在 API 空闲时静默执行（停止窗口 → 清 cookie/storage）
  → 上报 window_cleaned → 窗口恢复 AVAILABLE、账号标 banned（保留审计）
  → 新账号绑定该窗口 → 自动登录 → 更新窗口信息
```

- **同步机制**：每日定时全量对账（本地时区 03:00 + 0-15min 抖动）；Agent 启动/AdsPower 重连/增量序号断档也全量对账；Web 可对单台触发"立即同步"；窗口状态变化实时上报 `inventory_delta_event`（幂等去重 + revision CAS，序号缺口拒绝并请求全量）
- **窗口状态**：AVAILABLE → RESERVED → BOUND → BUSY / QUARANTINED；MISSING → OFFLINE（连续缺失）；已绑定窗口失踪暂停相关任务，不自动迁移账号
- **绑定关系**：`accounts.bound_profile_id` 可变更（解绑置空）；窗口维度保留绑定历史；**用户 UI 直接打开的窗口标 EXTERNAL_BUSY 停止新分配**
- **失效≠熔断**：SUSPENDED（风控熔断）可人工恢复不清洗；失效（banned/登录拒绝/验证码不可恢复）才触发清洗（"自动清洗开关"可配置，默认开）
- **清洗解耦（2026-08-12 定稿）**：状态变更（QUARANTINED）与物理清理分离——立即隔离停分配，延迟 10s 后台低优先级队列清洗（避让 Windows 文件锁），API 空闲时执行；**清洗失败保持 QUARANTINED + 进清洗重试队列（不丢、不阻塞任务路径）**；清洗成功后上报 window_cleaned 恢复 AVAILABLE
- **凭据**：AES-256-GCM 信封加密存储（KeyProvider 可迁移 Vault），Agent 经一次性短期 grant 领取；明文凭据禁止进入快照/队列/日志/WS/Receipt
- **调试互斥（决策 B4 修订）**：**Agent 调试标签**为主方案——Web 设备页"进入调试"→ central 标记 device.status=DEBUG → 调度器停止向该设备分配新任务，已运行任务自然收敛（未产生副作用者可回收重派，已点击者等租约兜底）；调试完成"退出调试"恢复分配。**本地轻量兜底**：Agent 执行前检查窗口未被本机手动占用；用户直接打开窗口标 EXTERNAL_BUSY 停止新分配（无需跨进程锁）

### 4.7 节点账号动态替换（评论树容错，M5）

**场景**：评论树 N 条评论 = N 个账号参与（树内账号不重复）。个别账号异常不应卡死整棵树——**节点级换号（单机铁律：树不跨设备拆分）**：

```
创建时预检：树规模 + 换号余量（20%）≤ 目标机可分配账号数，不足拒绝开工
节点执行失败（账号相关原因）
  → 判定：账号暂不可用（SUSPENDED/冷却 → 等待恢复）或失效（account_failed → 走清洗释放窗口）
  → 副作用边界检查：仅【未输入、未点击、结果确定未产生副作用】的节点可换号；
     已输入/已点击或结果不确定 → 保持原分配与 Receipt，记 published_unverified，禁止自动重试/重放
  → 换号算法（仅检索当前 PC 可用账号池：ACTIVE + 绑窗 + 未参与该树 + 本租户）：
      ① 同机可用账号存在 → 单机节点换号（换 account_id + window_id，重新 QUEUED，
         allocation revision + fencing generation 单调递增）
      ② 同机可用账号 = 0 → 禁止跨机拆树（单机铁律）→ 节点置 WAITING_CAPACITY_REPLACEMENT
         （等单机资源唤醒：冷却结束/清洗回池/新账号绑定）
      ③ 15 分钟仍无同机账号释放 → 任务优雅转 DLQ，错误码 NO_LOCAL_REPLACEMENT_ACCOUNT
```

- 换号记录：audit_events + 看板"节点已换号（原→新，原因）"
- **防循环**：换号候选排除本任务已失败过的账号；每节点累计换号 ≤3 次（可配置，F6）
- **副作用铁律**：已点击后任何异常 → published_unverified 保留 Receipt，不自动重试、不换号、不迁移
- **与 A6 等待上限分工**：换号治"账号异常卡住"（无副作用节点），等待上限治"父评论超时"，两者独立兜底

### 4.8 父评论验证与等待上限（M5）

**验证判定**（复用 comment_campaign 验证内核）：父评论提交后进入**三阶段弹性验证窗口**（兼容 TikTok 暗审延迟，2026-08-12 定稿）：

```
0 ~ 10 分钟（高频检查）：每 2 分钟滚动检查页面是否出现可见 comment_id
10 ~ 60 分钟（低频抽查）：未出现不报废，降级每 15 分钟抽查一次
> 60 分钟（终态认定）：仍不可见 → UNVERIFIED（超时）
```

**等待上限与处置**：

| 事件 | 处理 |
|---|---|
| 验证窗口内出现可见 comment_id | VERIFIED，子任务激活盖楼 |
| 明确失败（错误码/平台拒绝） | 立即失败（真失败，不走观察期） |
| 60min 终态仍不可见 | 父记 UNVERIFIED（原因=超时）；子任务失败进 DLQ（原因"父评论超时未验证"）；**DLQ 提供"一键重新校验"按钮（人工触发一次 DOM 扫描）** |
| 人工处理 | 重校验 / 重试父任务 / 放弃整组 |

- 终态时长默认 60min，**F6 可配置**（运营按平台观察调 90/120min）
- **三层防御**：换号（账号异常，单机池）→ 弹性验证窗口（确认真发出）→ 终态兜底

**单机模式定位**：launcher 单机创建/执行路径原样保留，仅用于单台电脑开发与调试（本地直连、不走 central）；BCS 生产任务一律从 Web 端创建；调试期经设备调试标签（§4.6）与生产隔离，本地 EXTERNAL_BUSY 标记兜底。

---

## 5. 功能需求

F1-F26 标注：策略（复用/改造/新建）+ 状态（✅已实现 / 🔶部分 / ❌未实现）。

| 需求 | 策略 | 状态 | 说明 |
|---|---|---|---|
| F1 账号管理（含部署状态机） | 改造 | 🔶 | central 侧导入+容量占用+业务状态机（`accounts.py`/`account_states.py`）；绑定语义状态机（§6）+ WAITING_WINDOW + 身份审核（IDENTITY_REVIEW_REQUIRED）；失效自动识别与清洗；凭据信封加密；Agent 回执驱动登录未实现 |
| F2 设备管理 | 新建 | ✅ | 心跳 upsert/能力上报/在线判定（90s）/CRUD/离线/租户隔离（`devices.py`） |
| F3 任务管理 | 改造 | ✅ | Task/SubTask；定时/截止/错过策略（CAS 幂等）；DAG 环检测（Kahn）；task_type 由模块隐式映射；strategy_version+config_snapshot+strategy_kind |
| F4 看板 + F4a WS 续传 | 新建 | ✅ | 看板统计 + WS 快照/回放/背压；Redis 优先内存降级 |
| F5 人工处理中心 | 改造 | 🔶 | API 完整（DLQ 列表/重派/终止）；前端页面未做（M5） |
| F6 系统配置版本化 | 改造 | ✅ | scope=global/tenant、version+1、历史行、灰度（`settings.py`） |
| F7 权限租户 | 改造 | 🔶 | 租户字段+require_tenant 已实现；JWT/RBAC 未接线（M6） |
| F8 策略版本管理 | 改造 | ❌ | 统一策略注册表 + 定义目录/发布（§4.5，M5） |
| F8a Agent 升级发布 | 新建 | ❌ | `agent_releases` 模型已建；channel 灰度未做（M6） |
| F9-F10 任务校验与 DAG | 改造 | ✅ | 校验/快照冻结/依赖边；**依赖关系改节点级（M5）**：创建期按账号防环，执行期子节点依赖父节点回执 |
| F11 Profile+负载分配 | 新建 | ✅ | 容量水位最低 + Profile 排他锁 + 在线过滤 |
| F12 消息骨干 | 改造 | 🔶 | v1 = HTTP 数据面 + SSE 控制唤醒；Outbox/Inbox 已实现；NATS 为 v2 接入点 |
| F12a 多实例并发 | 新建 | ❌ | PG SKIP LOCKED TOP-N 认领未做（M5） |
| F13 重试+租约回收 | 改造 | ✅ | 租约续期（Fencing）+ 超时回收（QUEUED/DLQ）+ 重试上限 3 |
| F14 账户状态机 | 改造 | ✅ | 全量状态集 + 唯一权威迁移表 + 熔断（≥3 连败→SUSPENDED）+ PROBE 门禁（冷却 2h） |
| F15 结果聚合 / F15a DLQ | 改造/新建 | ✅ | 结果回传：Inbox 去重→CAS→状态迁移→重试/DLQ；熔断与 PROBE 解析 |
| F17-F19 窗口调度/AdsPower 网关 | 改造 | 🔶 | 执行适配器已实现（stage 间续租、错误分类）；窗口并发上限未接入调度；AdsPower 真实链路待烟雾验收 |
| F20 策略契约 | 复用 | ✅ | execution_v2 strategy + config_snapshot 冻结；campaign 复合契约 M5 补 |
| F21 动作幂等 | 复用 | ✅ | Inbox 去重（msg_id+subject）+ Handle 门禁；receipts 复用 comment_campaign |
| F22-F23 失败分类/熔断 | 改造 | ✅ | error_category：retryable/environment 可重试（≤3），其余→DLQ；熔断见 F14 |
| F24 心跳上报 | 改造 | ✅ | 心跳+capabilities+容量/窗口数/队列深度 |
| F25 Agent 灰度升级 | 新建 | ❌ | 未实现（M6） |
| F26 账号部署执行器 | 新建 | 🔶 | 改为"窗口同步+绑定+失效清洗执行器"（A8/A9）：浏览器接管模块；登录驱动（人工/自动）；**不创建 Profile、不接收指纹参数**；`ads_power_params` 字段废弃（兼容保留忽略） |

**任务类型与创建入口**：`task_type` 由 Web 端创建入口模块隐式映射（§4.5），用户不选择；导入账号只填登录信息；`publish` 类型保留枚举但校验拒绝创建（浏览器内发布不做开发）。

---

## 6. 状态机定义（唯一权威：统一迁移表）

- Task：PENDING/QUEUED/MISSED/…（`scheduler.py scheduled_tick`）
- SubTask：QUEUED/ASSIGNED/RUNNING/SUCCESS/FAILED/DLQ/CANCELLED/WAITING_DEPENDENCY
- 账户（deploy/bind）：IMPORTED → WAITING_WINDOW → BINDING → WAITING_LOGIN（人工/自动登录）→ VERIFYING_IDENTITY → IDENTITY_REVIEW_REQUIRED → ACTIVE / FAILED / UNBOUND（失效识别确认 → **QUARANTINED 软隔离 → 延迟清洗 → 回池**；无人工解绑）
- 失效识别规则：错误码 banned / login_expired / verification_unrecoverable（category=account_failed）且连续失败达 3 次（复用熔断计数，可配置）→ 失效 → **立即置 QUARANTINED（停分配）→ 10s 后低优先级队列清洗（停止窗口→清 cookie/storage）→ 上报已清洗 → AVAILABLE、账号标 banned 保留审计**；清洗失败保持 QUARANTINED + 重试队列；SUSPENDED 不触发清洗（可人工恢复）
- 账户（business）：ACTIVE/CAPTCHA/MANUAL_VERIFIED/SUSPENDED/MANUAL_REVIEW（`account_states.py` 唯一权威 + 熔断 + PROBE）
- 结果：VERIFIED/UNVERIFIED/PUBLISHED_UNVERIFIED（PUBLISHED_UNVERIFIED 由 Agent 执行层上报，真实回执判定在烟雾验收校准）
- 现有 comment_campaign Assignment / execution_v2 Job 状态机作为结果验证层/运行态挂接 SubTask 之下，状态迁移走唯一权威表，不旁路

---

## 7. 数据模型概要

已落地 16 表（见交接文档 §2.1），规模化补充：

| 表 | 说明 |
|---|---|
| tenants / users / roles / permissions | 新建（users 迁移自 management.db） |
| devices / device_sessions | 已实现（心跳留痕） |
| **profiles（窗口资源池）** | M5 新建：Agent 同步窗口视图（window_ref/device_id/状态/绑定账号/egress_group/同步时间）；唯一约束 (device_id, window_ref)；全局归属运营方 |
| accounts / account_status_logs | 已实现；新增 `bound_profile_id`（可变更）+ 绑定历史；`ads_power_params` 废弃 |
| account_credentials | M5：ciphertext / encrypted_data_key / key_version / credential_revision（AES-256-GCM 信封加密） |
| tasks / subtasks / dependency_edges / handles | 已实现（config_snapshot 冻结）；dependency_edges 改节点级（M5） |
| task_results | 已实现（result_data JSON） |
| outbox / inbox | 已实现（事务发件箱 + 去重联合主键） |
| dlq_items / audit_events | 已实现 |
| configs / config_versions | 已实现（scope/version/gray_ratio） |
| strategies（统一注册表） | M5 新建：strategy_id/kind（v2/campaign）/version/checksum/schema/来源模块；+ executable_definition_revisions（定义目录，实施规格 §5） |
| agent_releases | 已建模型（F25 未实现） |
| leases（独立表） | M6 演进：500 规模抽离独立 leases 表（subtask_id 唯一 + generation + owner + expires_at），回收器只扫活跃行 |

### 7.1 索引策略（500 规模部署时执行）

| 表 | 索引 |
|---|---|
| subtasks | (tenant_id, status, priority, id) 部分索引 WHERE status IN (QUEUED, ASSIGNED, RUNNING) |
| subtasks | (tenant_id, profile_id) WHERE status 活跃 |
| tasks | (tenant_id, status) |
| devices | (tenant_id, status, last_heartbeat_at) |
| task_results | (tenant_id, created_at) |
| outbox | (status, next_attempt_at) |

### 7.2 数据保留与归档

| 数据 | 策略 |
|---|---|
| task_results / audit_events | 按月分区；日批归档 >90 天 |
| DLQ / attempts | 保留 180 天 |
| 看板 | 只读近 90 天 |
| Agent 本地 | 7 天 |
| 中控/API 日志 | 30 天（Loki 可选）；audit_events ≥180 天 |

**引导同步优先级**：账号 → 回执/结果 → 策略/模板 → 其余（§14.1 全量同步，幂等合并，非搬迁）。

---

## 8. 非功能需求

### 8.1 性能指标

| 指标 | 目标 |
|---|---|
| 心跳/拉取/结果热路径 API | P99 <100ms |
| 分配 tick（15 万排队） | <1s |
| 回收器时延 | <500ms |
| 心跳请求率（合并后） | 100/s → ~50/s |
| 空闲期拉取请求率 | <10/s（SSE 唤醒 + 自适应退避） |
| 写延迟 | <50ms |
| 看板刷新 | ≤1s |
| 空系统 tick 频率 | ≈0（队列深度感知） |
| 批准事件 → Agent 权威拉取 | p95 <1s（SSE 唤醒） |

### 8.2 容量与风控参数（运营方底层可配置项，F6 版本化，仅运营方管理员可调，客户不可调）

| 项 | 默认值 | 说明 |
|---|---|---|
| 每账号最小动作间隔 | ≥60s | 单账号两次动作最小间隔（风控红线） |
| 每账号日动作上限 | ≤200/天 | 超限当日跳过该账号 |
| 冷却 | 2h | MANUAL_VERIFIED 后不自动 PROBE |
| 每台窗口并发 | 3 | 每机同时执行窗口数上限 |
| 全局执行节流 | ≤50 动作/s | 全系统每秒新动作上限 |
| 熔断 | 3 次连续失败 | 连败→SUSPENDED 停派发，人工确认恢复 |
| 每机账号上限 | = 该机窗口数（同步动态得出） | 无空闲窗口 → WAITING_WINDOW 等待；绑定事务性防超绑 |

**调参 SOP**：F6 版本化发布（version+1、灰度）→ 观察指标（失败率/熔断率/DLQ 增长）→ 小步长调整 → 审计；不得绕过 F6 直接改库。

### 8.3 其余非功能

| 类别 | 要求 |
|---|---|
| 一致性 | 六铁律：Outbox 先落库、Inbox 去重、Fencing、唯一权威状态表、唯一约束兜底、CAS |
| 可靠性 | 心跳/租约保障（已验证）；NATS 按升级条件引入 |
| 安全 | JWT（central）+ RBAC + 租户隔离；日志脱敏（`redact_public_*`）；凭据信封加密 + 一次性 grant |
| 风控 | pacing/人工门禁/UNVERIFIED 不自动重提/Profile 排他锁/egress 集中度限制（全部已验证或 M5） |
| 兼容 | Agent Windows 10/11；AdsPower Local API v5+；central Windows/Linux 双平台 |

---

## 9. 日志与监控

- 日志：structlog（JSONL）；脱敏复用 `browser_legacy.record_browser_log` 模式
- 指标（M5 落地 Prometheus）：写延迟/API P99、tick 时延、回收数、分配吞吐、队列深度（QUEUED/DLQ）、心跳到达率、Agent 版本分布、DB 增长、连接池水位、window_available / account_waiting_window / task_waiting_capacity / manual_login_waiting / identity_review / published_unverified / credential_grant_failures

### 9.1 告警规则表（2026-08-12 确认采用）

> 阈值按运营实测校准：运行期发现误报/漏报时经 F6 配置调整并审计；接收方式分层——看板红点 + Webhook（运营日常，复用现有 webhook outbox）/ 运维告警（M6 Prometheus Alertmanager）。

| 告警 | 判定 | 阈值 | 接收 |
|---|---|---|---|
| 设备掉线 | 心跳/SSE 失联 | >90s 标离线；持续 >10min 告警 | 看板红点 + Webhook |
| 磁盘水位 | Agent 上报 disk_free_gb | <50GB 警告 / <20GB 停新任务 | 看板 + Webhook |
| DLQ 增长 | DLQ 计数增量 | 10min 内 +10 | 看板 + Webhook |
| 任务失败率 | SUCCESS/FAILED 比 | 单任务 >50% | Webhook |
| 写延迟 | central 指标 | P99 >500ms 持续 5min | 运维告警 |
| tick 时延 | 调度器指标 | >1s 持续 5min | 运维告警 |
| 库存过期 | 全量对账 | >26h 未完成 | 运维告警 |
| 人工登录积压 | manual_login 计数 | 持续增长 | 看板 + Webhook |
| 身份不一致骤增 | identity_review 计数 | 10min +5 | 运维告警 |
| AdsPower 慢响应 | Agent 上报 API 均值 | P95 >5s | 设备页标记 + 动态背压 |
| 重启风暴 | Agent 重启计数 | 30min ≥3 次 | 设备标 DEGRADED + 告警 |
| 单一出口集中度 | egress_group 节点计数 | 超限 | 运维告警 |

---

## 10. 迭代规划

| 迭代 | 范围 | 进度 |
|---|---|---|
| M0 | 决策与契约：PRD、ADR-0010、Docker Compose 原型、状态机唯一权威表 | ✅ |
| M1 | 基础设施+租户预留：central 骨架、Outbox/Inbox、设备+心跳/capabilities、账号导入（F26 中央侧） | ✅ |
| M2 | 任务主线：Task/SubTask/DAG、分配器、租约/Fencing、Agent 内核适配、结果回传 | ✅ |
| M3 | 运维能力：看板+WS 续传、人工处理 API、租约回收器、配置版本化 | ✅（多实例 ❌、灰度 ❌） |
| M4 | 风控+租户：账户状态机全量、熔断、PROBE、WAL 库、混沌测试、租户矩阵 | 🔶（WAL 接线 ❌、租户激活 ❌、引导同步 ❌） |
| M5 | 规模化加固 + 策略统一 + 窗口资源模型（实施蓝图 + 决策 #17-21）：**测试基建 PG 化（fixture+CI 容器）**、fake-agent、PG+索引、TOP-N 分配器、队列感知 tick、SSE+HTTP 数据面（429 限速+退避重连）、前端完整性（人工处理中心/任务创建按模块入口）、策略注册表+定义目录+Campaign 策略化、浏览器接管模块（同步/增量对账/绑定/登录/**软隔离+延迟清洗**/调试标签+EXTERNAL_BUSY 兜底）、凭据加密+grant、身份审核、WAITING_WINDOW/CAPACITY/**WAITING_CAPACITY_REPLACEMENT+15min 有界等待**、节点替换（单机铁律+预检+防循环）、父评论三阶段弹性验证+一键重校验、审批策略 per_comment/batch/prepare_only、publish 校验拒绝 | ❌ 待启动 |
| M6 | 运维与扩展：保留/归档、指标/告警、central 多实例、租约独立表、容量配额、JWT+RBAC、Agent 灰度（F25）、SaaS 租户激活（资源全局化/客户入口/租户配额） | ❌ 待启动 |

**M5 首项 = fake-agent 模拟器**（全部规模压测的前提工具）。

---

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| HTTP→NATS v2 迁移语义分裂 | 投递层抽象预留（同一业务入口），滚动迁移，旧路径限期收敛 |
| 分配器全表扫描放大 | TOP-N 重构 + 部分索引 + 事务认领，S3 压测 <1s |
| 心跳风暴（500 台） | 心跳/拉取合并 + SSE 唤醒 + 自适应退避 |
| 中控不可达期间本地数据丢失 | 本地待报缓冲（持久化队列，复用 WAL 模式）+ 全量对账兜底（§14） |
| 单机换号池枯竭卡死整树 | 创建预检 + WAITING_CAPACITY_REPLACEMENT 有界等待 + NO_LOCAL_REPLACEMENT_ACCOUNT（§4.7） |
| Windows 清洗文件锁/API 阻塞 | 软隔离 QUARANTINED + 延迟 10s 低优先级清洗队列（§4.6） |
| 现有模块改造破坏已验证功能 | 符号契约测试 + 全量回归 + 每迭代可回滚 |
| 多租户后期补返工 | v1 全链路预留 tenant_id + 强制过滤（矩阵测试已过） |
| 中控迁移 Linux 困难 | central 纯 Python 无 Windows 依赖 |
| AdsPower 真实执行未验收 | 烟雾验收工具已就绪（`scripts/smoke_adspower.py`），M5 前人工验收 |
| 评论点击后状态不确定 | published_unverified 不自动重试；副作用铁律（§4.7） |
| TikTok 暗审延迟误杀父评论 | 三阶段弹性验证（2min/15min/60min）+ 一键重校验（§4.8） |

---

## 12. 验收标准

### 12.1 业务验收（均需全量既有测试保持绿）

0. 存量联调：Comment Campaign 盖楼任务在新任务模型下端到端跑通（创建→拆解→分配→执行→人工批准→回执→看板）；M5 后经策略化（strategy_kind=campaign）由 Web 端创建、CampaignExecutor 执行
20. Agent 复用：execution_v2/comment_campaign 改造后既有模块测试全绿
21. 渐进切换：v1 同机运行期现有管理后台页面继续可用（URL 兼容）

### 12.2 量化验收表（2026-08-12 确认）

**验收契约性质**（三类来源）：
- **正确性断言**（100% 保证类）：防回归，无数值计算，靠唯一约束/CAS/状态门禁
- **工程目标值**：压测要验证的假设——S2/S3 实测超出时必须在压测报告记录并显式调整目标，**不静默放行**
- **配置值**：运营可调（F6），来自已确认风控参数/等待上限

| # | 验收项 | 指标+阈值 | 来源 |
|---|---|---|---|
| 1 | 任务创建 | P99 ≤500ms（单任务 500 账号） | 工程目标 |
| 2 | 数据库 | **全面统一 PostgreSQL（2026-08-12 修订）**：废弃 central 的 SQLite 起步模式——开发/测试/生产全部使用 Docker PG（轻量容器 ~100MB），同一引擎保证 SKIP LOCKED/JSONB 语义一致，杜绝双库语法断层；既有 132 测试基建改造为 PG fixture（CI 起容器，M5 前置）；**各电脑本地 SQLite 不受影响**（仍为执行系统事实源，Agent 同步上报） | 2026-08-08（修订 08-12） |重 | 重复 msg_id 100% 拒绝 | 断言 |
| 5 | 看板刷新 | 事件→页面 ≤1s | 工程目标（业务 SLA） |
| 6 | 心跳规模 | 500 台模拟写延迟 <50ms、请求率 <50/s | 工程目标 |
| 7 | 回收器 | 超时回收 ≤30s，时延 <500ms | 工程目标 |
| 8 | 成功率展示 | 看板与 DB 统计一致 | 断言 |
| 9 | 租户隔离 | 越权读写 100% 拒绝 | 断言 |
| 10 | 熔断 | 连败 3→SUSPENDED，恢复需 MANUAL_REVIEW | 配置值（A5） |
| 11 | 窗口同步 | 对账后资源池与各机一致；重复同步幂等 | 断言 |
| 12 | 绑定不超卖 | 一窗一账号（唯一约束）；失效释放回池可重绑 | 断言 |
| 13 | 失效自动清洗 | 失效错误码+连败确认→自动清洗回池；SUSPENDED 不清洗 | 断言 |
| 14 | 节点动态替换 | 50 节点树 5 账号异常→同机池换号整树完成；树内不重复；同机无号→WAITING_CAPACITY_REPLACEMENT→15min→DLQ（NO_LOCAL_REPLACEMENT_ACCOUNT）；防循环排除已失败账号+≤3 次 | 断言 |
| 15 | 父评论三阶段验证 | 0-10min 每 2min / 10-60min 每 15min / >60min 终态 UNVERIFIED；明确失败立即失败；DLQ 一键重校验；终态时长 F6 可配 | 配置值（A6 修订） |
| 16 | 凭据安全 | 日志/快照/WS 无明文凭据；grant 一次有效且过期 | 断言 |
| 17 | 身份审核 | 身份不一致不进入业务候选池 | 断言 |
| 18 | SSE 唤醒 | 空闲 100 台无任务时空 Pull 为零；批准→Agent 拉取 p95 <1s；断开重连按公式退避+抖动；超阈 429 降级 60s Pull 不崩溃 | 工程目标 |
| 19 | 调试互斥（B4） | 设备置 DEBUG → 停止新分配、已运行任务收敛（无副作用可回收重派、已点击等租约兜底）；退出调试恢复；手动打开窗口 EXTERNAL_BUSY 不派新任务 | 断言 |

### 12.3 安全验收

自动测试：未认证 401、越权 403、租户越权拒绝、日志无明文凭据、AES-GCM 认证失败/防替换/密钥轮换；人工渗透：token 过期/篡改、租户 header 伪造（M6 JWT 后）。

### 12.4 兼容性验收

版本矩阵：AdsPower Local API v5+、Windows 10/11、Python 3.12、Node ≥20；烟雾验收：`scripts/smoke_adspower.py` link/strategy 两级 + 真实环境人工执行。

---

## 13. 部署与运维

### 13.1 v1 拓扑（同机双进程）

```
Windows 主控机（launcher.py 演进）
├─ central :8000（API + tick + 回收器 + 中继 + WS + SSE 控制面）
├─ Flask :5000（管理后台 + /bcs 三面板：看板/设备/任务）
├─ PostgreSQL（Docker，开发/测试/生产一致）
├─ Redis（WS 事件流；现有 RQ 保留）
执行电脑：Agent（PyInstaller + NSSM 服务化，F25 前手动/脚本升级）

v2 演进：central 多实例（无状态 API × N + 独立调度/回收实例 + PG SKIP LOCKED）+ Web 迁独立 Linux/Docker
```

### 13.2 端口与网络

| 服务 | 端口 | 说明 |
|---|---|---|
| central API/WS/SSE | 8000 | 内网；Agent 与 Flask 均访问；CORS 白名单 :5000 |
| Flask Web | 5000 | 浏览器入口 |
| Agent→central | 8000 | 主动出站，Agent 无需外网端口 |

### 13.3 高可用与 Agent 升级

沿用既有原则；Agent 升级 = PyInstaller 打包 + channel 灰度（F25，M6）+ NSSM 服务化。

---

## 14. 存量数据引导同步（bootstrap，非搬迁）

**原则（2026-08-12 确认）**：本地 SQLite 保持原样继续运行（本地为事实源），**不搬迁、不关闭、不重写**；central 通过 Agent 同步建立汇聚基线——本质与库存同步同构（全量对账 + 增量）。

### 14.1 首次全量同步（Agent 上线时）

| 数据 | 来源 | 目标 | 幂等键 |
|---|---|---|---|
| 账号名单/状态 | 本机 accounts.db（花名册） | central.accounts | account_id |
| 历史回执/结果 | 本机 comment_campaign / execution_v2 | task_results / handles | 批次 batch_id |
| 窗口清单 | AdsPower（经浏览器接管模块） | profiles 资源池 | device_id + window_ref |
| 策略/模板 | 本机 execution_v2 / comment_campaign / selector_probe | strategies 注册表（M5 后） | strategy_version / 批次 batch_id |

规则：全量同步 = 幂等合并（重复执行不重复导入）、断点续传（事件序号 + Inbox 去重）、失败重试不丢数据（本地待报缓冲，M5 前置）；已登录账号的窗口直接推断绑定关系，不重建不重写。

### 14.2 central 自身库切换（已废弃 SQLite，保留说明）

central 直接使用 PostgreSQL（Docker 开发镜像，决策 A #2 修订）；`central/migrate.py`（SQLite→PG 迁移工具）仅保留用于历史遗留库的一次性归集，开发/测试/生产不再使用 SQLite。

---

## 15. 一致性、安全与执行契约

- **六铁律**：Outbox 先落库后发消息、Inbox 去重、Fencing 校验、状态迁移走唯一权威表、唯一约束兜底、CAS
- **Agent 窗口阶段机 = WAL 库**（`agent/wal.py`：NEW/STARTING→abandon、RUNNING→aborted、SUBMITTING/VERIFYING→unverified、DONE 无操作）；接线到 ExecutionV2Executor/worker 主循环为 M4 收尾项
- 故障处理矩阵以 chaos 协议级测试为准（test_chaos.py 8 项），真实断开/断电注入待 M5 fake-agent 环境扩展
- 已点击后结果不确定 → published_unverified，不自动重试；已验证父 Receipt 是子评论继续执行的唯一依赖凭据
- 安全 Tripwire（M5）：自动测试阻断真实 AdsPower/TikTok 访问，递归扫描日志/快照/队列/API/WS 确保无明文凭据

---

## 附录 A：已确认决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | 中控进程形态 | **独立进程**（central :8000，launcher 监督） |
| 2 | 数据库起步 | **规模分级**：SQLite = ≤50 设备/开发；500 规模直接 PG（pool 20/40） |
| 3 | 消息骨干 | **v1 = HTTP 数据面 + SSE 控制唤醒**；NATS 为 v2 升级项（触发：任务下发延迟 SLA <1s / 设备 >2,000 / 实时事件流订阅方需求）；滚动迁移，投递层抽象预留 |
| 4 | 多租户激活时点 | v1 全链路预留字段/过滤（已完成），激活随 M6 |
| 5 | 新 Web 前端技术 | 沿用 Jinja + 原生 JS + node:test |
| 6 | 部署关系与页面托管 | central 独立纯 API；页面托管 Flask /bcs；认证归 central（JWT 规划，v1 X-Tenant-ID） |
| 7 | 任务类型与 Campaign 策略化 | publish 类型 v1 拒绝创建（浏览器内发布不做开发，Buffer 路径独立运行）；Campaign 统一为复合业务策略（strategy_kind=campaign）注册进统一策略注册表；单机模式保留 = 开发调试工具；**任务类型由创建入口模块隐式映射，用户不选择** |
| 8 | AdsPower 调用模型 | 窗口 = 预置资源池，系统不创建 Profile；每日定时同步 + 实时增量上报；导入 = 绑定未使用窗口（非强绑定）；用户只导入登录信息，参数系统自取；`ads_power_params` 字段废弃；**无空闲窗口 → WAITING_WINDOW 等待不拒绝** |
| 9 | 解绑方式 | **无人工解绑，全自动**：失效自动识别（account_failed 错误码 + 连续失败阈值=3，复用熔断计数可配置）→ 自动清洗（停窗口→清 cookie/storage）→ 窗口回池、账号标 banned 保留审计 → 新账号重绑；SUSPENDED 不清洗；"自动清洗开关"默认开 |
| 10 | 多租户形态 | **外部 SaaS 轻租户**：客户只 Web 端创建任务/看结果；设备/窗口 = 运营方全局共享池；账号/任务/结果按租户隔离；客户无资源池可见性；配额按租户（M6 计费基础）；激活时设备/窗口表改全局视图、分配器全局水位+账号租户校验、客户入口待定 |
| 11 | 风控参数 | **运营方底层可配置项**（F6 版本化，仅运营方可调，客户不可调）：间隔 60s / 日上限 200 / 冷却 2h / 窗口并发 3 / 全局节流 50/s / 熔断 3 次；调参走 F6+审计 |
| 12 | 评论树账号异常处理 | **节点账号动态替换**：节点失败（账号相关）→ 选未参与该树的新账号（含窗口）改派重试；替换上限 3 次（可配置）；超限/池不足→DLQ 标注原因；**依赖关系改节点级**；**副作用边界：仅未输入/未点击节点可换号/迁移，已点击→published_unverified 不自动重试** |
| 13 | 父评论验证与等待上限 | 判定 = 可见 comment_id + 发布后验证窗口内确认（复用 comment_campaign 验证内核）；等待上限默认 30min（F6 可配）；超时父 UNVERIFIED（原因=超时）、子失败进 DLQ |
| 14 | 本机调试与生产互斥（B4 修订） | **Agent 调试标签为主**：Web 设备页"进入调试"→ device.status=DEBUG → 停止新分配、已运行任务自然收敛（未产生副作用可回收重派，已点击等租约兜底）；"退出调试"恢复。**本地轻量兜底**：执行前检查窗口未被手动占用；用户直接打开窗口标 EXTERNAL_BUSY 停分配；无需跨进程锁 | 2026-08-12（B4 修订） |
| 15 | 量化验收阈值（C 确认） | **§12.2 19 项全部采用（2026-08-12）**：三类来源——正确性断言（防回归）/ 工程目标值（S2/S3 压测验证，实测超出必须报告调整，不静默放行）/ 配置值（F6 可调，来自 A5/A6） | 2026-08-12（C） |
| 16 | 告警阈值与接收（D4 确认） | **§9.1 12 条规则全部采用（2026-08-12）**：接收分层 = 看板红点+Webhook（复用现有 webhook outbox）/ 运维告警（M6 Prometheus Alertmanager）；运行期误报/漏报经 F6 调整并审计 | 2026-08-12（D4） |
| 17 | 节点替换单机铁律（2026-08-12 定稿） | **树不跨设备拆分**：换号仅检索当前 PC 账号池（ACTIVE+绑窗+未参与该树+本租户）；同机无号 → WAITING_CAPACITY_REPLACEMENT（等单机资源唤醒）；**15min 无释放 → 优雅转 DLQ（错误码 NO_LOCAL_REPLACEMENT_ACCOUNT）**；创建时预检（树规模+换号余量 20% ≤ 单机可分配数）；防循环：排除已失败账号 + 每节点累计换号 ≤3 次 | 2026-08-12 |
| 18 | 清洗解耦（2026-08-12 定稿） | **立即软隔离 + 异步延迟清洗**：失效确认 → 立即置 QUARANTINED（停分配，不立刻调 AdsPower）；清洗进后台低优先级延迟队列，等 10s（Chrome 退出+文件锁释放）在 API 空闲时执行；成功后上报 window_cleaned 恢复 AVAILABLE；**失败保持 QUARANTINED + 清洗重试队列（不丢不阻塞）** | 2026-08-12 |
| 19 | 数据库统一 PG（2026-08-12 定稿） | **废弃 central 的 SQLite**：开发/测试/生产全 Docker PG（~100MB），SKIP LOCKED/JSONB 语义一致；132 测试基建改 PG fixture（CI 起容器，M5 前置）；各电脑本地 SQLite 不受影响 | 2026-08-12 |
| 20 | SSE 重连防护（2026-08-12 定稿） | 客户端：`Delay = Min(60, 2^retry × 2) + Random(0.5, 3.0)` 秒；服务端：SSE 网关 Ratelimit，超阈返回 429 → Agent 降级 60s 低频 HTTP Pull；SSE 端点 async 不占线程池 | 2026-08-12 |
| 21 | 父评论弹性验证（2026-08-12 定稿） | **三阶段**：0-10min 每 2min 高频检查 → 10-60min 每 15min 低频抽查 → >60min 终态 UNVERIFIED；明确失败立即失败；DLQ 提供"一键重新校验"；终态时长 F6 可配置（默认 60min） | 2026-08-12 |

## 附录 B：错误码表

### B.1 错误分类（Agent 上报 → central 处置）

| error_category | 可重试 | 处置 |
|---|---|---|
| retryable | 是 | attempts ≤3 → QUEUED（generation+1）；超限 → DLQ |
| environment | 是 | 同上 |
| strategy | 否 | → DLQ |
| **account_failed（banned / login_expired / verification_unrecoverable）** | 否 | → 失效识别（连续失败确认）→ 自动清洗释放窗口，不重试 |
| （其余） | 否 | → DLQ |

### B.2 Agent 阶段映射（`agent/execution_v2_executor.py`）

| 阶段 | 分类 |
|---|---|
| adspower start 失败 / CDP 连接失败 | environment |
| 策略缺失/校验失败 | strategy |
| 其他运行失败 | retryable |

### B.3 central HTTP 状态码

| 场景 | 状态码 |
|---|---|
| 租约 generation/owner 不匹配 | 409 stale generation |
| Inbox 重复 msg_id | 409 duplicate message |
| 非法状态迁移 / 依赖未满足 Handle | 409 |
| 导入失败（duplicate_account / no_device_capacity） | 业务错误体（detail 含原因） |
| 资源不存在 / 未知租户 | 404 |
| 参数校验失败 | 422 |
| 未认证（JWT 落地后） | 401 |

## 附录 C：接口契约

central 全端点（30 个，含请求/响应字段与租户铁律）见交接文档 §3-§5：设备（心跳/列表/详情/PATCH/离线）、账号（导入/批次状态/列表/水位/状态迁移）、任务与调度（创建/tick/scheduled/probe/拉取/列表）、子任务生命周期（续租/Handle/结果）、人工处理与看板（DLQ 三件套/汇总）、配置（PUT/GET/列表）、运维（healthz、WS）。字段级契约以交接文档为当前权威，随新 API 同步更新（M5 新增：库存同步/增量、凭据 grant、SSE 控制面）。

## 附录 D：性能与压测基线

| 阶段 | 规模 | 关键断言 |
|---|---|---|
| S1 | 单机冒烟 | 现有 132 pytest + 9 node 全绿；真实进程 healthz + 心跳链路 |
| S2 | 50 设备 | 全程 PG；心跳请求率 ≤50/s；写延迟 <50ms |
| S3 | 500 设备 | API P99 <100ms；15 万排队 tick <1s；回收 <500ms；空闲拉取 <10/s |
| S4 | 双实例 | 吞吐线性、无重复分配（SKIP LOCKED + 唯一约束） |
