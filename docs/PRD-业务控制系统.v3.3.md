# PRD：业务控制系统 v3.3

**版本**：v3.3
**日期**：2026-08-17  
**状态**：设计草案，待产品方批准
**适用范围**：M5a、M5b、M5c、M6  
**目标规模**：500 台 Agent、100 个客户、500 个同时在线 Web 用户

---

## 0. 文档治理

### 0.1 权威性

本文件是 v3.2 的候选后继 PRD。产品方批准前，v3.2 仍是实施权威；批准后，本文件整体替代 v3.2，成为业务控制系统产品行为、系统边界、状态、安全、里程碑和验收要求的唯一权威 PRD。

本版本批准后，任何产品行为、状态、安全边界或验收门槛变更必须发布后续 PRD 版本，不得直接回写 v3.3 规避变更审查。

规范优先级：

1. 本文件：产品行为与非功能契约。
2. `docs/architecture/api/openapi.yaml` 与版本化事件 Schema：字段级接口契约；必须符合本文件。
3. 状态迁移代码与契约测试：本文件状态机的可执行实现；不得扩展未声明路径。
4. ADR：记录技术决策原因；不能覆盖本文件。
5. v3.2、v3.1、v2、旧实施蓝图、交接文档：历史参考；v3.3 批准后不具规范效力。

发现冲突时，开发必须暂停相关范围，先修订低优先级文档或代码。禁止通过口头约定绕过。

### 0.2 被替代文档

- `docs/PRD-业务控制系统.v3.2.md`：本文件获批准后被整体替代；批准前仍是实施权威，保留为历史基线且不回写修订。
- `docs/PRD-业务控制系统.v3.1.md`：被 v3.2 整体替代；保留为历史基线，不回写修订。
- `docs/PRD-业务控制系统.v2.md`：被本文件整体替代。
- `docs/superpowers/specs/2026-08-12-central-business-modules-and-agent-executors-design.md`：仅保留未与本文件冲突的实现参考。
- `docs/architecture/modules/business-control-system.md`：继续记录实现现状，不定义目标行为。
- `docs/architecture/adr/ADR-0010-browser-control-system-python-evolution.md`：继续记录演进原因，不覆盖本文件的发布边界。

### 0.3 需求与进度分离

本文件不使用“已实现/部分实现/未实现”标记。实现状态由交接文档、实施计划和测试报告维护，避免需求与进度同时变化导致失真。

### 0.4 术语迁移

外部业务概念统一使用 `customer_id`，表示客户数据归属范围。旧代码和旧表中的 `tenant_id` 是迁移期实现别名：

- 新 OpenAPI、事件和业务代码使用 `customer_id`。
- 持久层可以在 M5 内部映射旧列名。
- M6 外部发布前，公共响应、审计查询和权限代码不得再暴露或信任 `tenant_id` 输入。

---

## 1. 背景、目标与非目标

### 1.1 背景

现有系统在 Windows 主控机运行 Flask 管理后台、Browser Execution V2、Comment Campaign、Selector Probe、TikTok Stats、Redis/RQ 和本地数据模块，通过 AdsPower 浏览器窗口执行 TikTok 业务动作。

目标是升级为多机业务控制系统：外部客户只通过 Web 创建任务和查看结果；Central 与 Agent 始终由运营方掌控，Agent 运行在本地 Windows 执行机。

### 1.2 产品目标

1. 客户经 Web 创建任务，无需了解设备、窗口、Agent 或 AdsPower Profile。
2. Central 自动校验、拆分、调度、监控并聚合结果。
3. 500 台 Agent 下保持可测的延迟、吞吐、一致性与故障恢复能力。
4. 多客户账号、任务、结果严格隔离；运营方全局管理执行资源。
5. 外部副作用不确定时禁止自动重放，避免重复评论或重复动作。
6. 现有 execution_v2、comment_campaign、selector_probe 内核通过适配复用，不进行无关重写。
7. M5 完成受控内部试运行；M6 通过发布门后开放外部客户。

### 1.3 成功标准

- 一棵评论树端到端完成：创建、审批、单机分配、执行、回执验证、结果展示。
- 账号导入、窗口绑定、身份核验、失效隔离、延迟清洗闭环可恢复、可审计。
- 500 台 Agent 稳态运行 60 分钟，满足第 18 章性能与可靠性门槛。
- 跨客户读、写、枚举和事件订阅 100% 拒绝。
- 点击成功但回执丢失、Agent 崩溃、Redis 故障、消息重复和乱序均不产生重复外部动作或数据丢失。

### 1.4 非目标

- 不由系统创建 AdsPower Profile；Profile 是运营方预置资源。
- 不开发浏览器内 publish；发布继续使用既有 Buffer 路径。
- 评论树任何阶段不跨设备拆分或迁移。
- M5/M6 不借调、抢占或临时出租 Comment Campaign 的 `RESERVED_SPARE`；不引入 `CAPACITY_PARKED`。若未来接受恢复延迟换取更高利用率，必须另立 ADR 和新版 PRD。
- M5/M6 不引入 NATS；满足第 20.2 节升级条件后另立 ADR。
- M6 不包含企业 SSO；初始使用用户名、密码和 Flask Session。
- 不将 Central 或 Agent 管理端口直接暴露公网。
- 不重写各执行机现有 SQLite；按第 13 章同步必要数据。

---

## 2. 术语

| 术语 | 定义 |
|---|---|
| Customer | 外部客户的数据归属单元；通过 `customer_id` 隔离账号、任务和结果 |
| Flask BFF | 公网 Web 入口；负责页面、Session、CSRF、权限和对 Central 的服务端调用 |
| Central | FastAPI 业务控制服务；生产任务、策略、租约、状态和最终结果的权威 |
| Agent | Windows 执行进程；管理本机窗口并执行 Central 工作单元 |
| Device | 一台由运营方管理的执行电脑 |
| Profile | AdsPower 内预置浏览器窗口；系统同步、绑定、隔离和清洗，但不创建 |
| Work Unit | Central 下发给 Agent 的最小可租约执行单元 |
| Lease | 工作单元的限时所有权，包含 owner、recovery_epoch、generation 和 expires_at |
| Fencing | 使用 `(recovery_epoch, generation)` 拒绝旧 Agent、旧租约和晚到结果 |
| Attempt | 一次 Work Unit 执行尝试，持久化外部副作用阶段 |
| Receipt | Agent 对外部动作的原始证据；经 Central 验证后形成最终结果 |
| WAL | Agent 本地执行阶段日志，用于崩溃恢复和副作用判断 |
| Outbox/Inbox | 事务后发布与原始消费信封；永久幂等由 source checkpoint、领域唯一键/Fencing 和命令幂等记录承担 |
| ACK/checkpoint | Central 确认已持久化到指定来源 revision 的同步水位 |
| PUBLISHED_UNVERIFIED | 可能已产生外部副作用但结果不确定；只能验证，禁止重放 |
| INDETERMINATE | Attempt 的中性终态：现有安全验证手段已穷尽，无法证明外部副作用成功或失败；永久禁止重放 |
| CLOSED_UNVERIFIED | SubTask 的中性终态：其 Attempt 以 INDETERMINATE 封存，结果保持未知 |
| COMPLETED_WITH_UNRESOLVED | Task 的中性终态：至少一个 SubTask 为 CLOSED_UNVERIFIED；不得解释为成功、部分成功或失败 |
| durable_ingest_ack | Central 已把恢复证据完整持久化到恢复隔离存储；不表示业务状态已应用 |
| recovery_disposition | Central 对一条恢复证据作出的处理结论：APPLIED、DUPLICATE、QUARANTINED_DURABLE 或 RETRYABLE_REJECTED |
| AdsPowerGateway | 每台 Agent 唯一的 AdsPower Local API 进程级入口；统一排队、限流、超时、熔断和指标 |
| egress_group | 窗口实际共享出口或代理风险域 |

---

## 3. 角色、身份与权限

### 3.1 角色

| 角色 | 范围 | 主要权限 |
|---|---|---|
| `platform_admin` | 全平台 | 客户、用户、全局配置、设备、Profile、Agent、审计 |
| `operator` | 全平台运营 | 账号审核、任务运维、人工处理、资源监控；不能授予平台管理员 |
| `customer_admin` | 单客户 | 本客户用户、账号、任务、结果 |
| `customer_member` | 单客户 | 创建任务，查看本客户账号、任务和结果 |
| `customer_viewer` | 单客户 | 只读本客户账号、任务和结果 |

平台角色的 Principal 使用 `customer_id=null`；其跨客户查询必须显式携带服务端校验的目标客户范围。客户角色的 Principal 必须有且只能有一个 customer_id。角色与 customer_id 组合由数据库 `CHECK` 约束，应用层不得修补非法组合。

### 3.2 数据隔离

- `accounts`、`tasks`、`subtasks`、`attempts`、`results`、客户侧审计记录必须带 `customer_id`。
- Device、Profile、Agent 属运营方全局资源，不带客户所有权，客户不可见。
- 分配器可使用全局设备池，但候选账号必须属于任务的 `customer_id`。
- 所有客户数据查询默认从认证 Principal 注入 `customer_id`，禁止调用方自由指定范围。
- `platform_admin` 和 `operator` 跨客户操作必须显式选择目标客户，并写入操作者、理由和前后值。
- 资源不存在与资源属于其他客户，对客户统一返回 404，避免枚举。

### 3.3 Web 用户认证

M6 初始使用用户名、密码和 Flask Session：

1. 平台管理员创建 Customer 和首个 `customer_admin`。
2. 初始密码一次性使用，首次登录强制修改。
3. 客户管理员可创建本客户成员，但不能创建平台角色。
4. Flask 每次请求从用户库重新取得 `user_id`、`customer_id`、`role`、`session_version`。
5. 禁用用户、修改密码或撤销会话后，旧 Session 立即失效。
6. 写操作必须通过 CSRF 校验。
7. Session Cookie 必须设置 `HttpOnly`、`Secure`、`SameSite=Lax` 或更严格策略。
8. 同一用户在 15 分钟内连续失败 5 次后锁定 15 分钟；成功登录后失败计数归零，锁定和解锁均写安全审计。
9. Flask Session 必须保存当前 `web_session_epoch`；每次请求同时校验 epoch 和 session_version，任一不匹配即强制重新登录。

浏览器提供的 `customer_id`、`tenant_id`、角色 Header 或 Cookie 字段均不可信。

Central PostgreSQL 是 Customer/User/Role 和密码摘要的生产权威。Flask 负责登录交互，通过内部身份应用服务认证和读取 Principal；M6 不保留可独立修改身份的 `management.db` 生产副本。

### 3.4 内部服务身份

Flask 调用 Central 时使用独立服务凭证。Central 仅在服务凭证有效后接受 Flask 注入的 Principal：

```text
request_id
user_id
customer_id
role
session_version
web_session_epoch
issued_at
```

同机部署时 Central 绑定受限接口并校验轮换服务密钥；分机或容器部署时使用私网 TLS，并保留升级 mTLS 的能力。

Web 事件令牌采用非对称签名：私钥只在 Flask，Central 按 `kid` 使用受控公钥集验证；密钥轮换必须允许新旧公钥重叠至少一个事件令牌 TTL，并写安全审计。

---

## 4. 系统边界与拓扑

### 4.1 组件职责

| 组件 | 必须负责 | 禁止负责 |
|---|---|---|
| Flask BFF | 页面、静态资源、登录、Session、CSRF、角色、客户上下文、普通 Web API 编排 | 调度、租约、Agent 心跳、凭据存储、任务状态权威 |
| FastAPI Central | 生产业务状态、调度、租约、策略、事件、同步、凭据 grant、最终结果 | 渲染客户页面、直接执行浏览器动作 |
| Agent | 设备身份、窗口观察、WAL、Receipt、工作单元执行、库存同步、唯一 AdsPowerGateway | 决定客户权限、修改 Central 权威状态、跨设备协调、绕过 Gateway 直调 AdsPower Local API |
| Worker | 节点激活、清洗、验证、归档、分区维护和回收；使用独立后台数据库池 | 增删初始执行计划、旁路状态机、跳过 Fencing 或借用 Agent 关键数据库池 |

业务核心必须是普通 Python 模块，不依赖 Flask `request`、`session` 或 Blueprint：

```text
Flask Route / FastAPI Route
  -> Application Service
  -> Domain
  -> Repository / Adapter
```

### 4.2 M5 单实例拓扑

```text
Windows 主控机（launcher 监督）
├─ Flask BFF :5000（仅运营内部）
├─ FastAPI Central :8000（API、调度、回收、Outbox、WS、Agent SSE）
├─ PostgreSQL 16（Docker）
├─ Redis（RQ、可回放事件流、轻量唤醒）
└─ 既有 RQ/Celery Worker

Windows 执行机 x 500
└─ Agent -> Central HTTPS/SSE -> AdsPower
```

### 4.3 M6 外部发布拓扑

```text
外部浏览器
  -> HTTPS 反向代理
     -> Waitress/Gunicorn -> Flask BFF
     -> 受限事件路径 -> FastAPI Central WebSocket

Flask BFF -> 私网 TLS -> Central API 多实例
Agent -> 私网/VPN TLS -> Central Agent API/SSE
Central -> PostgreSQL + Redis
```

- Flask 内置开发服务器不得用于 M6。
- Central 和 Agent 管理端口不得直接暴露公网。
- 浏览器普通请求只访问 Flask。
- 实时看板连接前，由 Flask 签发短期、只读事件令牌；令牌至少绑定 `jti/user_id/customer_id/session_version/web_session_epoch/aud/iat/exp/scope`，反向代理只开放指定 WebSocket 路径。
- Central 在 WebSocket 握手时原子消费 `jti`，重放返回 401；连接期间每 60 秒复核用户状态、`session_version` 与 `web_session_epoch`，任一不一致立即关闭连接。

### 4.4 通信模型

| 调用 | 协议 | 权威性 |
|---|---|---|
| Browser -> Flask | HTTPS | 用户输入入口，不携带可信客户归属 |
| Flask -> Central | 私网 HTTPS | 已认证 Principal 下的业务命令和查询 |
| Browser -> Central Events | WSS + 短期事件令牌 | 只读事件通知；断档后 HTTP 补拉 |
| Central -> Agent | SSE | 轻量唤醒，不携带任务快照或凭据 |
| Agent -> Central | HTTPS | 心跳、拉取、续租、库存、Receipt、结果、grant 领取 |

### 4.5 数据库工作负载隔舱

Central 必须按工作负载建立四个独立 SQLAlchemy Engine/Pool；独立是硬边界，不是同一 Pool 上的逻辑标签：

| Pool | 允许流量 | 禁止行为 |
|---|---|---|
| `AGENT_CRITICAL` | Device Session、心跳、权威拉取、Lease 续租、Receipt、结果和 grant | API、Worker、WebSocket 不得借用 |
| `API_INTERACTIVE` | Flask BFF 发起的普通命令、查询和内部管理 API | 后台扫描、归档和长事务不得进入 |
| `WORKER_BACKGROUND` | 回收、依赖收敛、聚合、验证、归档和分区维护 | 不得借用 `AGENT_CRITICAL`；池满时延期批次，不得扩大连接 |
| `WEB_EVENT` | WebSocket 令牌消费、Principal 复核和必要的轻量快照索引 | 禁止全量 Task/SubTask 扫描；大快照必须读 BFF 缓存或预计算投影 |

所有实例的 `pool_size + max_overflow` 必须计入同一部署级总预算，并按下列唯一算法分配：

```text
B = min(db_application_connection_budget,
        floor((PostgreSQL max_connections - db_admin_reserved_connections) * 0.8))

raw[class]  = B * configured_percent[class] / 100
budget[class] = floor(raw[class])
remainder = B - sum(budget[class])
```

余数按 `raw-floor(raw)` 从大到小每类加 1；小数相同时固定优先级为 `AGENT_CRITICAL > API_INTERACTIVE > WORKER_BACKGROUND > WEB_EVENT`，直到 remainder 为 0。每类配置 `N` 个 process slot，必须覆盖正常副本、独立 Worker 和滚动升级最大 surge；slot_id 固定为 `0..N-1`。该类 slot `j` 的唯一连接上限为 `floor(budget[class]/N) + (j < budget[class] mod N ? 1 : 0)`。

配置服务按 revision 预建 `db_pool_process_slots`。进程创建业务 Engine 前，只能用一条短生命周期 slot-control 连接（计入 `db_admin_reserved_connections`）以 generation CAS 租用自己承载的每个 class 的一个 slot；成功后关闭 control 连接再按 slot 上限创建 Engine。同一实例每 class 最多一个、同一 slot 最多一个 owner。无 slot、重复 slot_id、租约失效或 `pool_size + max_overflow` 超过 slot 上限时实例不得 Ready，并在 expires_at 前 5 秒停止接受该 class 新请求。v3.3 的 `max_overflow` 固定为 0；slot-control、迁移和运维连接使用 `db_admin_reserved_connections`，不计入应用 slot。

每条业务物理连接必须设置可审计 `application_name=bcs:{class}:{config_revision}:{slot_id}:{generation}:{instance_id}`；新建物理连接时必须调用数据库 slot guard，确认对应 slot 为 ACTIVE、owner/generation 匹配且未过期，否则立即关闭。续租失败、owner/generation 改变或进入到期前 5 秒时，旧实例必须退出 Ready、停止 checkout、调用 Engine dispose 关闭全部 idle 连接，并让在用连接在 statement/transaction timeout 内结束后关闭。

过期 slot 接管采用物理 drain 协议：新 owner 先 CAS 写入更高 generation、`state=DRAINING`，使旧 generation 的新连接全部被 guard 拒绝；slot-control 角色按 application_name 等待最多 `db_pool_connection_drain_seconds`，然后终止仍存活的旧 generation backend，并确认 `pg_stat_activity` 中旧 generation 连接为 0；仅此后才能 CAS 为 ACTIVE 并创建新 Engine。旧进程仍存活但续租失败、暂停后恢复或网络恢复时，同样因 generation 不匹配无法重建连接。未确认旧连接归零时保持 DRAINING/fail-closed，不得让新旧 owner 物理连接重叠。

默认 B=120、四类比例 30/35/20/15、每类 N=3（已包含一个滚动升级 surge slot），得到 class budget 36/42/24/18，每 slot 上限依次为 12/14/8/6。修改 B、比例、slot 数或副本 surge 必须发布同一配置 revision，先 DRAIN 旧 slot，再激活新 revision；扩容不得临时创建计划外连接。进程启动还必须读取 PostgreSQL 实际 max_connections 复算，任何输入或总和不一致均 fail-fast。

Worker 每批最多处理 `worker_db_batch_size` 行，使用稳定排序、`FOR UPDATE SKIP LOCKED` 和短事务；禁止无边界 `.all()`、递归期间 N+1 查询、持有事务等待网络或 AdsPower。Pool 获取超时或数据库过载时，HTTP 请求返回 503 与 Retry-After，不得伪装为 500；Worker 记录延期并按退避重试。

---

## 5. 数据权威与领域模型

### 5.1 字段级权威矩阵

| 数据 | 权威 | 非权威副本 | 冲突规则 |
|---|---|---|---|
| Customer/User/Role | Central | Flask Session 缓存 | Central 覆盖；每请求校验 session_version |
| 账号归属、部署状态、业务状态 | Central | Agent 本地花名册 | Agent 只能上报观察，不能覆盖归属或状态 |
| Task/SubTask/Attempt | Central | Agent Work Unit/WAL | Fencing 标识不匹配的 Agent 更新拒绝 |
| 策略 revision/config_snapshot | Central 不可变内容制品 | Agent 按 customer/schema/checksum 隔离的加密缓存 | 已创建 Task 只引用冻结 snapshot；checksum 不一致时拒绝执行 |
| Lease/分配 | Central | Agent 当前租约 | Central CAS + `(recovery_epoch, generation)` 唯一权威 |
| Profile `observed_status` | Agent | Central 观察投影 | Agent source_revision 新于 Central 才接受 |
| Profile `allocation_status`、绑定和隔离 | Central | Agent 执行缓存 | Agent 观察不能解除 QUARANTINED 或改写绑定 |
| 原始 WAL/Receipt | Agent | Central 恢复隔离副本 | 普通纪元按 ACK 清理；旧纪元仅在 durable_ingest_ack、可清理 disposition、签名 purge checkpoint 和 age >=7 天同时满足时清理 |
| 最终结果、看板、审计 | Central | Web 缓存 | Central 权威；事件仅用于失效缓存 |

### 5.2 核心实体

| 实体 | 关键字段/约束 |
|---|---|
| `customers` | `customer_id`、name、status；客户 ID 不复用 |
| `users` | user_id、normalized_username、customer_id、role、status、session_version；normalized_username 全局唯一 |
| `devices` | device_id、status=`REGISTERING/ONLINE/OFFLINE/DEBUG/REVOKED/PERMANENT_FAILURE`、failure_reason、capabilities、agent_version、revision；运营方全局唯一 |
| `device_sessions` | device_session_id、device_id、recovery_epoch、expires_at、revoked_at；每设备仅一个当前 Session |
| `device_source_databases` | device_id、source_db_uuid、first_seen_at、last_seen_at、status；(device_id, source_db_uuid) 唯一，由已认证普通同步登记，恢复接口不得自行创建归属 |
| `profiles` | profile_id、device_id、window_ref、observed_status、allocation_status、binding_revision、egress_group；(device_id, window_ref) 唯一 |
| `accounts` | account_id、customer_id、bound_profile_id、deploy_status、business_status、credential_revision、revision |
| `config_snapshot_artifacts` | artifact_id、不可变 customer_id、schema_version、canonicalization_version、content_sha256、uncompressed_size_bytes、storage_ref、created_at；内容不可变，`(customer_id, canonicalization_version, schema_version, content_sha256)` 和 `(artifact_id, customer_id, content_sha256)` 唯一 |
| `config_snapshots` | snapshot_id、不可变 customer_id、strategy_id、strategy_revision、artifact_id、content_sha256、created_at；版本映射不可变，`(customer_id, strategy_id, strategy_revision)` 和 `(snapshot_id, customer_id, content_sha256)` 唯一，一个 artifact 可被多个 revision 映射引用 |
| `tasks` | task_id、customer_id、task_kind、strategy_revision、config_snapshot_id、config_snapshot_checksum、missed_policy、missed_window_seconds、cancel_requested、bound_device_id、tree_generation、status、unresolved_count、revision |
| `subtasks` | subtask_id、task_id、不可变 customer_id、account_id、device_id、profile_id、reassign_blocked、status、unresolved_reason、revision |
| `attempts` | attempt_id、subtask_id、不可变 customer_id、attempt_no、recovery_epoch、generation、stage、side_effect_started、error_category、status、unresolved_deadline_at、sealed_at |
| `leases` | lease_id 主键、subtask_id、lease_mode=`EXECUTE/VERIFY_ONLY`、owner_device_session_id、recovery_epoch、generation、target_attempt_id/target_execute_fencing/target_attempt_revision、expires_at、status；仅 ACTIVE subtask_id 部分唯一 |
| `receipts` | receipt_id、attempt_id、不可变 customer_id、source_revision、raw_hash、verification_status |
| `results` | result_id、task_id、subtask_id、不可变 customer_id、result_type、payload_ref、created_at |
| `comment_tree_reservations` | task_id 唯一、customer_id、bound_device_id、tree_generation、required_nodes、reserve_slots、active_reserved_slots、spare_target、spare_deficit、status、revision；reserve_slots 保留初始审计值，其余计数每次按锁定活跃行重算 |
| `comment_tree_accounts` | task_id、customer_id、account_id、bound_device_id、tree_generation、slot_kind、status、reserved_at、release_reason、released_at；status=`RESERVED_SPARE/IN_USE/RELEASED/INVALID`，(task_id, account_id) 唯一且历史行禁止同树复用 |
| `cleanup_jobs` | cleanup_id、profile_id、binding_revision、recovery_epoch、generation、status、next_attempt_at |
| `capacity_diagnostics` | request_id、customer_id、required_slots、global_base_eligible、global_eligible、max_single_device_eligible、prelock_max_single_device_eligible、reason_code、operator_details、created_at；创建失败审计，不创建 Task |
| `recovery_ingest_items` | ingest_id、device_id、source_db_uuid、source_recovery_epoch、source_revision、payload_checksum、envelope_fingerprint、durable_ingest_ack_at、disposition、archive_ref |
| `recovery_purge_checkpoints` | device_id、source_db_uuid、source_recovery_epoch、purge_through_revision、issued_at、key_id、signature；三元组唯一且 revision 单调 |
| `web_fallback_budget_slots` | config_revision、slot_id、slot_qps、owner_instance_id、generation、expires_at；(config_revision, slot_id) 唯一，当前 revision 的活跃 owner_instance_id 唯一 |
| `late_reconciliations` | reconciliation_id、attempt_id、idempotency_key、evidence_refs、checksum、resolution=`CONFIRMED_PUBLISHED/CONFIRMED_NOT_PUBLISHED`、note、created_at；只追加，不重开执行 |
| `outbox` | message_id、subject、aggregate、payload_ref、dispatched_at；与业务事务同库提交，历史按保留策略分区 |
| `inbox_raw_events` | inbox_id、source_device_id、source_db_uuid、source_recovery_epoch、source_revision、event_id、payload_checksum、envelope_fingerprint、payload_ref、received_at、safe_to_purge_at、status；`safe_to_purge_at=NULL` 留在不可按年龄删除的 default partition |
| `source_event_checkpoints` | source_device_id、source_db_uuid、source_recovery_epoch、applied_through_revision、updated_at；三元组唯一且水位只可 CAS 单调递增 |
| `source_event_ledger` | source_device_id、source_db_uuid、source_recovery_epoch、source_revision、event_id、payload_checksum、envelope_fingerprint、disposition、domain_result_ref、processed_at；按 `(source_device_id, source_db_uuid)` HASH 分 32 区，主键为来源三元组+revision，事件 ID 在来源纪元内唯一，不能随 raw Inbox 删除 |
| `command_idempotency_records` | scope、idempotency_key、request_fingerprint、domain_result_ref、created_at、expires_at；与领域写入同事务，冲突 fingerprint 返回 409 |
| `audit_events` | audit_id、actor、customer_id、resource、action、before/after、request_id、created_at；只追加、按 created_at 分区，主键 `(created_at, audit_id)` 包含分区键 |
| `db_pool_process_slots` | config_revision、pool_class、slot_id、owner_instance_id、generation、state=`DRAINING/ACTIVE`、expires_at；`(config_revision, pool_class, slot_id)` 唯一，每实例每 class 最多一个活跃 slot |
| `system_state` | 当前 recovery_epoch、当前 web_session_epoch、调度冻结状态、恢复阶段、active_web_fallback_config_revision、web_fallback_budget_state、fallback_transition_id/started_at/owner/expires_at；单行 CAS 更新 |

### 5.3 数据库原则

- Central 开发、测试、CI、生产全部使用 PostgreSQL 16；不支持 Central SQLite 模式。
- Agent 本地 SQLite 保留，用于现有业务数据、WAL 和待报缓冲。
- Central 需要 `SKIP LOCKED`、部分索引、JSONB、唯一约束和 CAS。
- 四个数据库 Pool 必须使用不同 Engine/Session Factory，遵守第 4.5 节部署级总预算；禁止运行时借池或自动扩大 `max_overflow`。连接池耗尽必须暴露为过载指标，HTTP 入口返回 503 + Retry-After。
- `db_pool_process_slots` 只按激活的 config_revision 发放；取得/续租使用数据库时间、generation CAS 和唯一约束。revision 切换或 owner 接管必须执行第 4.5 节 DRAINING、旧 generation backend 归零/终止、再 ACTIVE 的物理 drain 协议；不得只等待逻辑租约过期或让新旧预算重叠。
- 客户数据表索引前缀包含 `customer_id`；全局资源表不得伪造客户归属。
- 平台角色 `platform_admin/operator` 必须满足 `customer_id IS NULL`；客户角色必须满足 `customer_id IS NOT NULL`。数据库以 `CHECK` 约束角色和归属组合。
- Task、SubTask、Attempt、Receipt、Result 和评论树预留的 `customer_id` 创建后不可修改；使用复合外键或同事务约束保证其与父 Task、Account 一致。
- `comment_campaign` 的 `bound_device_id/tree_generation` 创建后不可修改；所有 SubTask、账号槽位和 Lease 分配必须匹配树级绑定，违反时事务回滚。
- `comment_campaign` 必须满足 bound_device_id 非空且 tree_generation=1；其他 task_kind 的两个字段必须为空。`comment_tree_accounts` 对 `status IN (RESERVED_SPARE, IN_USE)` 建立 account_id 部分唯一约束，禁止同一账号同时预留给多棵树。任何 `(task_id, account_id)` 历史行，包括已 RELEASED/INVALID 行，都使该账号永久不得再次进入同一树。
- `config_snapshot_artifacts` 先按版本化 canonicalization 生成 UTF-8 canonical bytes，再计算 SHA-256；`config_snapshots` 只把不可变 strategy revision 映射到 artifact。Task 保存 snapshot 映射引用和冗余 checksum，不得在 Task/SubTask 重复内联完整 JSON。同一 customer 下相同 canonicalization/schema/checksum 可复用 artifact，但不同 strategy/revision 必须各有自己的映射；M5/M6 禁止跨 customer 去重、探测或下载。
- `config_snapshots(artifact_id, customer_id, content_sha256)` 必须复合外键引用 `config_snapshot_artifacts(artifact_id, customer_id, content_sha256)`；`tasks(config_snapshot_id, customer_id, config_snapshot_checksum)` 必须复合外键引用 `config_snapshots(snapshot_id, customer_id, content_sha256)`。数据库必须拒绝跨 customer artifact、错误 checksum 和 Task/revision 映射错配，不能只依赖应用校验。
- Lease 历史行使用 `UNIQUE(subtask_id, recovery_epoch, generation)`；仅对 `status=ACTIVE` 建立 subtask_id 部分唯一约束，终态 Lease 永久保留且不阻止更高代 Lease。
- `lease_mode=VERIFY_ONLY` 时 target_attempt_id、target_execute_fencing、target_attempt_revision 必须非空；`lease_mode=EXECUTE` 时三者必须为空，使用数据库 `CHECK` 约束。
- `recovery_ingest_items` 对 `(device_id, source_db_uuid, source_recovery_epoch, source_revision)` 唯一；同键 envelope_fingerprint 不同必须冲突，不得插入第二行。disposition 使用数据库 CHECK 枚举；`RETRYABLE_REJECTED` 可在后续成功摄取时 CAS 转为三个可清理终值之一，`APPLIED/DUPLICATE/QUARANTINED_DURABLE` 写入后不可互转。
- `recovery_purge_checkpoints.purge_through_revision` 只能 CAS 单调递增，且不得超过相同来源已连续获得可清理 disposition 的最大 revision；签名 payload 必须覆盖全部主键字段、revision、issued_at 和 key_id。
- `inbox_raw_events` 父表按 nullable `safe_to_purge_at` RANGE 分区，明确不定义父表主键或全局唯一约束；default/日期叶分区只建立非权威本地索引 `(source_device_id, source_db_uuid, source_recovery_epoch, source_revision)` 和 received_at 索引。`inbox_id` 只是追踪 ID，不声称跨分区唯一。永久幂等由 ledger/checkpoint/领域键承担。
- `source_event_ledger` 的 HASH 分区键包含在主键和事件唯一键中：`PRIMARY KEY(source_device_id, source_db_uuid, source_recovery_epoch, source_revision)`、`UNIQUE(source_device_id, source_db_uuid, source_recovery_epoch, event_id)`。业务写入、ledger/disposition/result_ref 和 checkpoint 推进必须在同一事务提交。若目标日期分区已存在，可同事务设置 raw event 的 safe_to_purge_at；否则 raw 行保持 NULL，后续清理事务只有复验 ledger/checkpoint 后才可路由。
- `source_event_checkpoints` 与 `source_event_ledger` 不按时间清理；来源数据库仍登记期间不得删除，设备退役后只能连同最终水位、完整 ledger 归档和 source_db tombstone 一起封存，禁止 revision 从 0 重新使用。`command_idempotency_records` 至少保留 180 天；无法由永久领域唯一键重建原结果的记录 `expires_at` 必须为 NULL。
- `inbox_raw_events.safe_to_purge_at` 仅可在对应 checkpoint 或领域幂等记录已耐久提交后设置，至少为处理完成时间加 7 天；NULL 行位于 default partition，任何年龄都不得删除。日期分区仅包含已证明可清理的行，过期后可整分区 DROP；去重正确性不得依赖这些可删除分区上的唯一索引。
- Inbox 清理器必须至少提前 14 天创建 `safe_to_purge_at` 日期分区；只有目标分区已存在时才可设置非 NULL safe_to_purge_at 并路由记录。分区缺失时保留在 default、告警并 fail-closed，禁止临时逐行强删。
- `audit_events` 按月分区并只追加；热分区保留 90 天，至少 180 天在线可查询，超过 180 天仅在加密归档校验成功后 DETACH/DROP。分区主键/唯一约束必须包含分区键；禁止假设 PostgreSQL 能跨分区维护不含分区键的全局唯一索引。
- `web_fallback_budget_slots` 按 config_revision 预建固定数量槽位；实例只能用 `FOR UPDATE SKIP LOCKED` + generation CAS 取得或续租一个槽位。无有效槽位的实例不得接受 fallback 查询；同一 active config_revision 的 slot_qps 之和不得超过 `web_fallback_global_qps`。
- `late_reconciliations` 只允许追加；数据库权限和状态迁移服务均禁止借其更新 INDETERMINATE/CLOSED_UNVERIFIED/COMPLETED_WITH_UNRESOLVED。
- 生产迁移必须可前滚、可回滚或有明确不可逆备份门。

---

## 6. 任务、策略与创建入口

### 6.1 Web 模块映射

用户不选择内部任务类型。创建入口决定 `task_kind` 和执行器：

| Web 模块 | `task_kind` | 策略类型 | 执行器 |
|---|---|---|---|
| 新增账号 | `account_deployment` | 版本化系统登录工作流 | 浏览器接管模块 |
| 评论任务 | `comment_campaign` | `campaign` 复合策略 | CampaignExecutor |
| 养号任务 | `nurture_strategy` | `execution_v2` 原子策略 | ExecutionV2Executor |
| 独立浏览/点赞/关注 | `browse` / `like` / `follow` | `execution_v2` | ExecutionV2Executor |
| publish | 禁用 | 无 | API 校验拒绝 |

旧枚举 `deploy`、`comment` 是迁移别名；M6 公共 API 只使用本表规范值。

### 6.2 策略注册表

Central 是生产策略定义权威。每个可执行定义包含：

```text
strategy_id
kind
revision
semantic_version
checksum
snapshot_schema_version
canonicalization_version
input_schema
capability_requirements
idempotency_class
definition
published_at
disabled_at
```

- revision 发布后不可变。
- 本机草稿必须显式发布并经 Central 校验，才能创建生产任务。
- Task 创建时冻结 revision，并引用唯一不可变 `config_snapshot_id + config_snapshot_checksum`。
- Agent 只执行 checksum 对应的冻结快照，不读取本地策略数据库决定生产行为。
- 未知执行器、能力不足或 checksum 不符时，Agent 不打开浏览器并返回确定性错误。

#### 配置快照制品与 Agent 缓存

1. 发布服务按 `canonical_json_v1` 生成确定性 UTF-8 JSON：对象键排序、去除无意义空白、字符串和数字格式由版本化 Schema 固定；对未压缩 canonical bytes 计算 SHA-256，并取得/创建唯一 content artifact。
2. 发布时必须验证 Schema、未压缩大小、JSON 深度和秘密扫描。密码、Cookie、Token、CDP 地址、一次性 grant 及可换取凭据的材料一律拒绝进入快照。
3. 发布事务为 `(customer_id, strategy_id, strategy_revision)` 创建唯一 snapshot 映射并引用 artifact；映射和 artifact 发布后均不可变。相同内容的新 revision 可复用 artifact 但必须创建新映射；任何内容变化都产生新 checksum。M5/M6 禁止 delta/base-chain/patch 合并，防止 Agent 因缺基线、乱序或合并差异执行非冻结策略。
4. Work Unit Envelope 只携带 `snapshot_id`、`snapshot_checksum`、`snapshot_schema_version`、`snapshot_size_bytes` 和有界 `dynamic_inputs`，不得重复内联完整快照。完整制品不计入 Work Unit Envelope 大小。
5. Agent 缓存键为 `(customer_id, snapshot_schema_version, snapshot_checksum)`，本地加密并使用 LRU；同一键并发 miss 必须 single-flight。不同 customer 不得共享缓存命名空间或缓存文件。
6. Cache miss 时，Agent 只能凭当前有效 Device Session 和关联的 ACTIVE EXECUTE/VERIFY_ONLY Lease 下载该 Task 引用的制品；接口不得允许任意枚举 snapshot_id。响应使用 ETag、If-None-Match 和 gzip，Agent 对解压后的 canonical bytes 重新计算 SHA-256 后才可缓存。
7. checksum、schema、大小、压缩比或授权校验失败时，Agent 不打开浏览器、不领取凭据，返回确定性错误。暂时下载失败返回 retryable `CONFIG_SNAPSHOT_UNAVAILABLE` 并允许在 Lease 有效期内退避重试；checksum 不一致返回不可重试 `CONFIG_SNAPSHOT_CHECKSUM_MISMATCH` 并触发安全告警。
8. 冷缓存下载使用 Agent 级 0-5 秒全抖动、每 checksum single-flight 和 Central 有界下载并发。动态输入只承载该 Work Unit 的账号引用、节点参数和文案选择结果，不得改变执行步骤或绕过快照 Schema。

### 6.3 任务创建事务

1. Flask 根据 Session 构造 Principal。
2. Central 校验角色、customer_id、账号归属、策略 revision、参数 Schema、DAG、配额和截止时间。
3. Comment Campaign 在同一事务按冻结的账号过滤器选择一台 Device，写入不可变树级绑定，并按第 8.3 节原子预留容量。
4. Central 在同一事务完成 DAG 拆解，写 Task、全部初始 SubTask、依赖、不可变 config_snapshot 映射引用、评论树预留和 Outbox；完整 canonical 内容只在 `config_snapshot_artifacts` 保存一次，`config_snapshots` 仅保存 strategy revision 映射，Worker 不得增删初始执行计划。
5. API 在完整执行计划持久化后返回 202 与 task_id；仅节点激活、调度和执行异步进行。
6. 相同 `Idempotency-Key + customer_id` 重放返回原 task_id，不重复创建。

Comment Campaign 初始容量采用唯一的 fail-fast 语义，不创建等待中的僵尸 Task：

- `reserve_slots = ceil(节点数 × 1.2)`；账号过滤器至少包含 ACTIVE、健康、冷却、required/excluded tags、language、已绑定 Profile、Device ONLINE 和未被其他树活跃预留。
- 在候选账号行加锁并扣除并发预留后，若没有单台 Device 满足 reserve_slots，则整个创建事务回滚，不写 Task、SubTask、依赖、预留或业务 Outbox，返回 `409 INSUFFICIENT_SINGLE_DEVICE_CAPACITY`。
- 同一失败请求写独立 `capacity_diagnostics` 审计，字段为 `required_slots`、`global_base_eligible`、`global_eligible`、`max_single_device_eligible`、`prelock_max_single_device_eligible`、`eligible_by_device`、`excluded_by_tag`、`excluded_by_language`、`excluded_by_health`、`excluded_by_cooldown`、`excluded_by_binding`、`reserved_by_other_trees` 和 `reason_code`。`global_base_eligible` 已应用健康、冷却、绑定和既有预留但尚未应用 tags/language；`global_eligible` 再应用全部属性过滤。排除计数按 health、cooldown、tag、language、binding、reservation 的固定首个命中顺序统计，避免重复计数。
- `reason_code` 判定顺序固定且仅命中一项：锁前某台满足、加锁后因新并发预留不足为 `RESERVATION_CONTENTION`；否则过滤后全局足够但任一单机不足为 `SINGLE_DEVICE_FRAGMENTATION`；否则 global_base_eligible 足够而 global_eligible 不足为 `FILTER_FRAGMENTATION`；其余容量不足为 `GLOBAL_SHORTAGE`。
- 业务创建事务回滚后，必须先在独立短事务中耐久写入 capacity_diagnostics 才返回 409；诊断写入失败时仍不得创建 Task，返回 503 `CAPACITY_DIAGNOSTIC_UNAVAILABLE` 和 Retry-After，并记录高优先级告警。相同 request_id 重试复用已有诊断，不重复计数。
- 客户响应只含错误码、通用文案和 request_id；上述 Device、数量和排除细节仅 `platform_admin/operator` 可从内部诊断页查看。
- `WAITING_CAPACITY` 可用于其他明确声明等待策略的 task_kind，但 Comment Campaign 初始创建绝不进入该状态；运行期同机换号仍使用 `WAITING_CAPACITY_REPLACEMENT` 的 15 分钟有界等待。

---

## 7. 账号与窗口生命周期

### 7.1 导入和绑定

客户只提交登录所需字段和可选预期 TikTok 身份，不提交设备、Profile ID、浏览器指纹或 CDP 地址。

```text
导入账号
  -> Central 校验 customer_id 与重复账号
  -> 选择运营方全局 AVAILABLE Profile
  -> 创建绑定 revision
  -> Agent 登录
  -> 核验实际身份
  -> ACTIVE
```

无空闲 Profile 时进入 `WAITING_WINDOW`，不判失败。资源事件触发重试，禁止高频轮询。

### 7.2 Profile 同步

- 每日当地时间 03:00 加 0-15 分钟随机抖动执行全量对账。
- Agent 启动、AdsPower 重连、事件序号断档时执行全量对账。
- Profile 状态变化实时上报 delta。
- Snapshot 和 delta 都携带 source_db_uuid、source_revision、checksum。
- Central 发现 revision 缺口时拒绝后续 delta 并请求全量快照。
- 所有需要 AdsPower Local API 的同步均经第 7.5 节 AdsPowerGateway；队列拥塞时上报 `observed_at` 和 `stale=true`，不得用旧库存覆盖新 revision。

### 7.3 调试互斥

- Web 将 Device 置为 `DEBUG` 后，Central 停止新分配。
- 已运行 Work Unit 按副作用规则自然收敛。
- Agent 执行前检查窗口是否被本机人工占用；占用则标记 `EXTERNAL_BUSY`。
- 退出 DEBUG 或人工关闭窗口后，状态经库存事件恢复；不得依赖跨进程强锁。

### 7.4 失效与清洗

账号永久失效与风控熔断使用独立计数：

- `invalid_identity_observation_count`：banned、login_expired、verification_unrecoverable 等永久问题。
- `risk_failure_count`：临时风险或连续业务失败；达到阈值进入 `SUSPENDED`。

失效确认后：

1. 立即将 Profile 置 `QUARANTINED`，停止新分配。
2. 创建携带 binding_revision、recovery_epoch 与 generation 的延迟清洗任务。
3. 默认等待 10 秒，等待 Chrome 退出和文件锁释放。
4. 经 AdsPowerGateway 低优先级队列执行停止窗口、清 Cookie/storage；Cleanup 最大并发固定为 1。
5. 成功后上报匹配代次的 `window_cleaned`，Profile 转 `AVAILABLE`。
6. 失败保持 `QUARANTINED`，按退避重试；超过上限进入人工处理。

清洗任务必须携带：

```text
cleanup_id
profile_id
account_id
binding_revision
credential_revision
recovery_epoch
generation
```

每个清洗步骤前 CAS 校验 Profile 仍是同一绑定代次、仍为 `QUARANTINED`、未绑定新账号。任一不符立即取消旧清洗。

### 7.5 AdsPower Local API 设备级隔舱

- 每台 Device 必须只有一个长生命周期 `AdsPowerGateway` 服务/actor；正常情况下由唯一 Agent 进程持有，并通过 Windows named mutex + 本机 IPC owner lease 阻止第二进程并发直连。第二 Agent 进程无法取得 owner lease 时只能上报冲突并退出执行数据面。Execution V2、Comment Campaign、库存同步、Profile 生命周期和 Cleanup 禁止自行创建 Controller/Adapter 或直接发送 HTTP。
- Gateway 默认 `max_concurrency=1`，设备级最小请求间隔 1 秒，使用有界优先队列；该限制覆盖所有模块和所有 Profile，而非单个类实例。
- 优先级固定为：P0 Lease 关键窗口 start/stop；P1 VERIFY_ONLY；P2 inventory delta；P3 full inventory snapshot；P4 Cleanup。持续有高优先级流量时，每完成 10 个 P0/P1 请求必须从已等待的 P2-P4 中放行 1 个，防止后台任务永久饥饿；不得为公平性突破并发和速率限制。
- Cleanup 同时最多一个调用。P0/P1 队列等待超时 10 秒、调用超时 30 秒；P2-P4 队列等待超时 60 秒、调用超时 5 秒。队列等待超时、调用超时、429/限流、本地服务不可达和熔断必须分类上报，禁止各业务模块叠加内部重试。
- 连续 5 次依赖失败后熔断 30 秒；半开仅放行一个探测。队列达到上限时 Agent 上报 `dependency_status=DEGRADED`、`dependency_capacity=0` 并停止接受新 Work Unit，已持久化 Cleanup 保持待处理。
- Agent 心跳、Device Session 续签和 Lease 续租不得获取 Gateway 锁，也不得等待 AdsPower 调用。AdsPower 超时只标记依赖 DEGRADED，不得把 Agent/Device 误判 OFFLINE。
- Gateway 输出设备级 queue_depth、active_calls、request_rate、queue_wait、call_duration、timeout、circuit_state 和按优先级统计；调用日志不得含凭据、Cookie 或 CDP 地址。

---

## 8. 调度、租约与单机评论树

### 8.1 通用分配顺序

1. 过滤任务状态、计划时间、截止时间和依赖。
2. 校验 customer_id、账号 ACTIVE、Profile BOUND、Device ONLINE、能力和 Agent 最低版本。
3. 应用账号 pacing、日限额、冷却、egress_group 和全局节流。
4. Comment Campaign 执行单机整树约束。
5. 从 TOP-N 低水位候选中使用 PostgreSQL `FOR UPDATE SKIP LOCKED` 认领。
6. 同一事务写分配、Lease recovery_epoch/generation、Outbox 和审计。
7. SSE 只发送 `work_available`；Agent 通过 HTTP 拉取权威 Work Unit。

### 8.2 租约

- 每个活跃 SubTask 只有一个 Lease。
- Fencing 标识为 `(recovery_epoch, generation)`；generation 仅在同一 recovery_epoch、同一 SubTask 内从 1 单调递增且不复用，跨 recovery_epoch 允许数值重复但 Fencing 标识绝不相同。
- Agent 续租必须匹配 subtask_id、device_session_id、owner、recovery_epoch 和 generation。
- Lease 默认 TTL 120 秒；Agent 每 30 秒续租。进入 `SUBMITTING` 前必须重新续租并确认剩余有效期至少 60 秒，否则在副作用前中止 Attempt。
- 旧 `(recovery_epoch, generation)` 的续租、Receipt、结果和清洗事件统一返回 409。
- Lease 超时后，Central 先撤销旧代，再决定安全重派、PUBLISHED_UNVERIFIED 或 DLQ。
- 已进入 `SUBMITTING` 的 Attempt 不因租约超时自动重派。

Lease 到期或撤销必须由一个 PostgreSQL 事务收敛。`BOUND_DEVICE_PERMANENT_FAILURE` 由第 9.8 节设备事务统一触发：Comment Campaign 分支按第 8.3 节整树处理，其他 task_kind 按第 9.8 节通用分支处理；两者均不进入下述 TTL 重试分支。下述分支只处理 TTL 到期、临时离线、暂停和非永久撤销：

1. `FOR UPDATE` 锁定 Lease、SubTask 和当前 Attempt，并再次校验 recovery_epoch 与 generation。
2. `VERIFY_ONLY` Lease 到期/撤销：Lease 转终态，SubTask 保持 `PUBLISHED_UNVERIFIED`；按退避规则决定后续只读验证。
3. 尚未创建 Attempt 的 `ASSIGNED` 节点：Lease 转终态，SubTask 转 `QUEUED`。
4. Attempt 处于 `PREPARED/INTERACTING/WAITING_APPROVAL` 且 `side_effect_started=false`：有剩余次数时，Lease 转终态、Attempt 转 `ABORTED`、SubTask 转 `QUEUED`；无剩余次数时，Lease 转终态、Attempt 转 `FAILED/error_category=retries_exhausted`、SubTask 转 `DLQ`。
5. Attempt 处于 `SUBMITTING/VERIFYING/PUBLISHED_UNVERIFIED`、`side_effect_started=true` 或阶段证据缺失：Lease 转终态，Attempt 与 SubTask 原子转 `PUBLISHED_UNVERIFIED`，设置 `reassign_blocked=true`，Task 转 `MANUAL_REVIEW`。
6. 同一事务写审计和 Outbox 后提交。仅 `reassign_blocked=false` 且 SubTask 已为 `QUEUED` 时，后续调度事务才可在当前 recovery_epoch 创建更高 generation 的 `EXECUTE` Lease；`VERIFY_ONLY` 仅按下述规则创建。

数据库部分唯一约束保证每个 SubTask 最多一个 `ACTIVE` Lease；任何回收分支都禁止在同一事务直接创建新 Lease。

`reassign_blocked=true` 只禁止新的 `EXECUTE` Lease。对 `PUBLISHED_UNVERIFIED` 可创建独立 `VERIFY_ONLY` Lease：

- 仅允许原 bound_device_id、account_id 和 profile_id，且设备在线、现有浏览器会话可只读访问。
- Work Unit 只包含验证指纹和只读步骤，使用独立验证执行器；禁止调用点击发布、评论、点赞、关注、登录或任何写动作。
- Work Unit 与结果必须同时携带当前 `verify_lease_id + verify_recovery_epoch + verify_generation`，以及 `target_attempt_id + target_execute_recovery_epoch + target_execute_generation + target_attempt_revision`。
- `VERIFY_ONLY` 不得领取 credential grant；Agent 和 Central 均必须在执行前校验 lease_mode。
- Central 处理验证结果时同时锁定 VERIFY_ONLY Lease 与目标 Attempt：前者必须仍 ACTIVE 且 Fencing 匹配，后者必须仍为 `PUBLISHED_UNVERIFIED` 且执行 Fencing/revision 匹配；然后 CAS 收敛原 Attempt/SubTask。任一不匹配或迟到结果返回 409。
- 验证成功或确认未发布时，按第 9.3 节收敛原 Attempt/SubTask；验证失败、租约到期或设备不可用时，SubTask 继续保持 `PUBLISHED_UNVERIFIED`，不得创建 `EXECUTE` Lease。
- 第 n 次自动验证失败后等待 `min(900, 60 × 2^n) + U(0, 10)` 秒，n 从 0 开始；最多自动验证 10 轮，耗尽后仅保留人工入口。
- 无法使用原设备只读验证时，只能由人工或无副作用的 Central 外部查询确认。
- 封存前取得确定证据时，platform_admin 必须使用 `manual_review.submit_deterministic_evidence`：证据必须是可校验的 external_object_id、只读查询原始响应或平台导出及 checksum；仅操作者陈述、不可复验截图或推测不构成确定证据。确认已发布收敛为 SUCCEEDED/SUCCESS，确认未发布收敛为 FAILED/FAILED；状态、审计和 Outbox 同事务提交。

为避免原设备永久损坏且平台无查询能力时无限阻塞，系统提供“按未知结果封存”，但不提供强制成功、强制失败或重新执行：

1. Attempt/SubTask 已为 `PUBLISHED_UNVERIFIED`，`reassign_blocked=true`，不存在 ACTIVE Lease。
2. bound Device 已由 `platform_admin` 置为 `PERMANENT_FAILURE` 并产生 `BOUND_DEVICE_PERMANENT_FAILURE` 事件，原设备 VERIFY_ONLY 永久不可用。
3. 策略声明不存在无副作用外部查询，或查询与自动验证已穷尽并留有证据；从首次同时满足第 2、3 条起已等待 `unresolved_seal_wait_seconds`，默认 24 小时。
4. `platform_admin` 在人工处理中心执行 `close_as_unresolved`，填写结构化 reason、证据引用和备注。客户角色不得执行；M5/M6 默认均为单个 platform_admin 批准，不隐含多人审批。
5. 单个事务把 Attempt 转 `INDETERMINATE`、SubTask 转 `CLOSED_UNVERIFIED`，递归取消其所有尚未执行的依赖后代并标记 `ANCESTOR_UNRESOLVED`，然后按第 9.1 节聚合 Task；全程保持 `reassign_blocked=true`，永久禁止新 `EXECUTE` Lease 和 credential grant。

`close_as_unresolved` 表示“证据不足，停止等待”，不表示动作成功或失败。界面、导出、Webhook 和统计必须显示 `UNKNOWN/未确认`，不得计入成功率或失败率。以后取得的新证据只追加 `late_reconciliations`，把业务解释标记为 `CONFIRMED_PUBLISHED` 或 `CONFIRMED_NOT_PUBLISHED`；不得修改已封存状态、重开 Task 或重新执行外部动作。

### 8.3 评论树铁律

- 一棵评论树最多 50 节点，整个生命周期由 Task 的不可变 `bound_device_id/tree_generation` 绑定一台 Device。
- 创建事务计算 `reserve_slots = ceil(节点数 × 1.2)`；仅当目标设备上属于同一 customer_id 的可分配账号数减去其他活跃树预留后仍不少于 reserve_slots，才可原子写入 `comment_tree_reservations`。
- 创建事务把节点账号写为 `IN_USE`，把 20% 替换余量写为 `RESERVED_SPARE`；活跃 account_id 跨树唯一，且 customer_id/device_id/tree_generation 必须与 Account、Task 和预留一致。
- 换号优先在同一事务把本树一个 `RESERVED_SPARE` 改为 `IN_USE`、原故障账号改为 `INVALID` 并更新节点账号。无备用行时，才可检索当前 Device 上未参与本树的新账号，由 account_id 部分唯一约束仲裁并直接插入 `IN_USE`。
- `RESERVED_SPARE` 是本树的硬替换容量，对其他评论树、普通任务、browse/养号和人工调试均不可见、不可借调、不可抢占；实现不得依赖“需要时快速收回”。
- SubTask 达到 `SUCCESS/FAILED/DLQ/CANCELLED/CLOSED_UNVERIFIED` 后，节点收敛事务必须确认不存在 ACTIVE Lease、VERIFY_ONLY 或未决 Receipt，再把该节点当前 `IN_USE` 行转 `RELEASED/release_reason=NODE_TERMINAL`。`(task_id, account_id)` 历史行继续阻止该账号重回本树，但账号可以参加其他任务。
- 每次节点终态、换号或取消后，在同一事务锁定 Task 预留，并按固定顺序收敛：先完成节点账号 RELEASED/INVALID/换号迁移；再计算 `remaining_protected_nodes=非终态 SubTask 数` 和 `spare_target=ceil(remaining_protected_nodes × comment_tree_spare_ratio_percent / 100)`；若备用超额，按 `(reserved_at, account_id)` 稳定顺序释放至目标；若换号后备用不足，尝试从当前 Device、同一 customer 的从未参与本树账号补充。不得释放任何 `IN_USE` 非终态节点，不得跨设备或借用其他任务账号补足。
- 所有行变更完成后、提交前必须从锁定行重新计算 `current_in_use`、`current_reserved_spare`、`active_reserved_slots=current_in_use+current_reserved_spare` 和 `spare_deficit=max(0, spare_target-current_reserved_spare)`，禁止按旧值增减计数。补充成功或节点终态降低 target 必须自动把 deficit 清零/降低；无候选时保留准确 deficit 并告警，但当前换号可继续。真正再次换号且无账号时才进入 `WAITING_CAPACITY_REPLACEMENT`。
- 非终态期间必须满足 `current_in_use <= remaining_protected_nodes`、`active_reserved_slots <= remaining_protected_nodes + spare_target`；违反不变量时事务回滚并触发数据完整性告警。
- `reserve_slots` 保留初始审计值。Task 终态聚合事务释放全部剩余活跃行，并令 active_reserved_slots/spare_target/spare_deficit 全部为 0。`INVALID` 行保留审计但不占活跃唯一约束。账号失效导致实际容量不足时只进入同机等待，不得修改 bound_device_id。
- 树内账号不重复；候选账号必须属于同一 customer_id。
- 新增备用账号仅检索当前 Device 的 ACTIVE、已绑窗且从未出现于本树 `comment_tree_accounts` 历史的账号；已预留换号只消费本树 `RESERVED_SPARE`。
- 同机无账号进入 `WAITING_CAPACITY_REPLACEMENT`。
- 默认等待 15 分钟；仍无资源转 DLQ，错误码 `NO_LOCAL_REPLACEMENT_ACCOUNT`。
- 每节点最多换号 3 次；排除本任务已失败账号。
- Device 永久故障，且整树所有 Attempt 均处于提交前阶段、完整连续 WAL 明确证明均未产生副作用：同一事务将活跃 Attempt 转 `FAILED/error_category=bound_device_permanent_failure`、对应 SubTask 转 `DLQ`、其余未执行节点转 `CANCELLED`，Task 聚合为 `FAILED` 并释放全部树预留。
- Device 永久故障的其他任何情况，包括任一 Attempt 已进入外部副作用、WAL 不完整或证据不确定：对应 Attempt/SubTask 转 `PUBLISHED_UNVERIFIED`，Task 转 `MANUAL_REVIEW` 并停止新分配；可验证时只按第 8.2 节验证，不可验证且达到期限时可按未知结果封存。预留仅在 Task 聚合进入终态时释放。
- 两种永久故障路径均不得修改 bound_device_id/tree_generation，禁止跨机续跑。

---

## 9. 状态机契约

所有迁移必须由唯一应用服务执行。迁移函数必须校验 `from status + revision/Fencing 标识 + guard`，并在同一事务写状态、审计和 Outbox。

### 9.1 Task

状态集：`PENDING`、`QUEUED`、`RUNNING`、`PAUSED`、`MANUAL_REVIEW`、`SUCCEEDED`、`PARTIAL_SUCCESS`、`FAILED`、`CANCELLED`、`MISSED`、`COMPLETED_WITH_UNRESOLVED`。

`missed_policy` 仅允许 `skip` 或 `run_immediately`。`missed_window_seconds` 在 Task 创建时冻结；无显式 `run_at` 的任务令 `run_at=created_at`。

| From | Event/Guard | To | 原子副作用 |
|---|---|---|---|
| PENDING | `now >= run_at` 且 `now <= run_at + missed_window_seconds` | QUEUED | 激活根 SubTask、Outbox |
| PENDING | 超出窗口且 `missed_policy=skip` | MISSED | 审计、终态事件 |
| PENDING | 超出窗口且 `missed_policy=run_immediately` | QUEUED | 写 `late_start=true`、激活根 SubTask、Outbox |
| QUEUED | 首个 SubTask 获得 Lease | RUNNING | revision+1、事件 |
| QUEUED/RUNNING | 管理员暂停 | PAUSED | 停止新分配、撤销安全租约 |
| PAUSED | 恢复后无存量活跃 Attempt | QUEUED | 重新激活可运行节点 |
| PAUSED | 恢复时仍有不可撤销活跃 Attempt | RUNNING | 只等待原 Attempt 收敛 |
| RUNNING | 无可运行节点且存在 PUBLISHED_UNVERIFIED/人工项 | MANUAL_REVIEW | 停止新分配、生成处理项 |
| MANUAL_REVIEW | 人工项解决且出现可运行节点 | RUNNING | 激活节点、审计 |
| PENDING/QUEUED/PAUSED/MANUAL_REVIEW | 全部非终态节点均满足安全取消 guard | CANCELLED | 取消节点、释放预留、审计、撤销安全租约 |
| RUNNING | 全部活跃 Attempt 均未进入 SUBMITTING 且满足安全取消 guard | CANCELLED | 取消节点、释放预留、审计、撤销安全租约 |
| RUNNING | 收到取消请求但存在 SUBMITTING/VERIFYING/PUBLISHED_UNVERIFIED | MANUAL_REVIEW | 停止新分配；保留并收敛不确定副作用 |
| RUNNING/MANUAL_REVIEW | 聚合真值表结果为 SUCCEEDED | SUCCEEDED | 汇总结果、释放预留、终态事件 |
| RUNNING/MANUAL_REVIEW | 聚合真值表结果为 PARTIAL_SUCCESS | PARTIAL_SUCCESS | 汇总结果、释放预留、终态事件 |
| RUNNING/MANUAL_REVIEW | 聚合真值表结果为 FAILED | FAILED | 汇总结果、释放预留、终态事件 |
| RUNNING/MANUAL_REVIEW | 聚合真值表结果为 CANCELLED | CANCELLED | 汇总结果、释放预留、终态事件 |
| RUNNING/MANUAL_REVIEW | 聚合真值表结果为 COMPLETED_WITH_UNRESOLVED | COMPLETED_WITH_UNRESOLVED | 汇总未知结果、释放预留、终态事件 |

终态：`SUCCEEDED`、`PARTIAL_SUCCESS`、`FAILED`、`CANCELLED`、`MISSED`、`COMPLETED_WITH_UNRESOLVED`。

Task 聚合按以下优先级执行，第一条命中即停止：

| 优先级 | SubTask/Task 条件 | 聚合结果 |
|---:|---|---|
| 1 | 任一 SubTask 为 `PUBLISHED_UNVERIFIED`，或仍有未解决人工项 | `MANUAL_REVIEW` |
| 2 | 任一 SubTask 仍为非终态且不存在优先级 1 条件 | 保持当前非终态，不聚合 |
| 3 | 至少一个 SubTask 为 `CLOSED_UNVERIFIED`，且其余 SubTask 均为终态 | `COMPLETED_WITH_UNRESOLVED`；单列 unknown/success/failure/cancelled 计数 |
| 4 | `cancel_requested=true`，且全部 SubTask 已为 `SUCCESS/FAILED/DLQ/CANCELLED` | `CANCELLED`；结果保留已成功/失败计数 |
| 5 | 全部 SubTask 为 `SUCCESS` | `SUCCEEDED` |
| 6 | 至少一个 `SUCCESS`，且至少一个 `FAILED/DLQ/CANCELLED` | `PARTIAL_SUCCESS` |
| 7 | 无 `SUCCESS`，且至少一个 `FAILED/DLQ`，其余均为 `FAILED/DLQ/CANCELLED` | `FAILED` |
| 8 | 全部 SubTask 为 `CANCELLED` | `CANCELLED` |

`MISSED` 只由尚未开始执行的 `PENDING` Task 产生，不参与 SubTask 聚合。聚合与 Task 状态、结果、预留释放、审计、Outbox 必须同事务提交。

### 9.2 SubTask

状态集：`WAITING_DEPENDENCY`、`WAITING_CAPACITY`、`WAITING_CAPACITY_REPLACEMENT`、`QUEUED`、`ASSIGNED`、`RUNNING`、`WAITING_APPROVAL`、`VERIFYING`、`SUCCESS`、`FAILED`、`DLQ`、`CANCELLED`、`PUBLISHED_UNVERIFIED`、`CLOSED_UNVERIFIED`。

| From | Event/Guard | To |
|---|---|---|
| WAITING_DEPENDENCY | 所有父 Receipt VERIFIED | QUEUED |
| WAITING_DEPENDENCY | 任一父节点 `FAILED/DLQ` | CANCELLED；原因 `ANCESTOR_TERMINAL_FAILURE` |
| WAITING_DEPENDENCY | 任一父节点 `CANCELLED` | CANCELLED；原因 `ANCESTOR_CANCELLED` |
| WAITING_DEPENDENCY | 任一父节点 `PUBLISHED_UNVERIFIED` | 保持 WAITING_DEPENDENCY；标记 `PARENT_UNVERIFIED` |
| WAITING_DEPENDENCY | 任一父节点 `CLOSED_UNVERIFIED` | CANCELLED；原因 `ANCESTOR_UNRESOLVED` |
| WAITING_CAPACITY | 资源事件且重新校验成功 | QUEUED |
| WAITING_CAPACITY_REPLACEMENT | 同机候选账号出现 | QUEUED |
| WAITING_CAPACITY_REPLACEMENT | 等待达到 15 分钟 | DLQ |
| QUEUED | CAS 认领并创建 Lease | ASSIGNED |
| ASSIGNED | Agent 拉取并确认 Work Unit | RUNNING |
| ASSIGNED | Lease 在 Agent 确认前过期或撤销 | QUEUED |
| RUNNING | 策略要求人工批准 | WAITING_APPROVAL |
| WAITING_APPROVAL | 批准且 Lease 有效 | RUNNING |
| WAITING_APPROVAL | 拒绝或达到批准时限 | CANCELLED |
| WAITING_APPROVAL | Lease 在批准前过期/撤销且剩余 Attempt >0 | QUEUED |
| WAITING_APPROVAL | Lease 在批准前过期/撤销且重试已耗尽 | DLQ |
| RUNNING | Attempt 进入验证 | VERIFYING |
| VERIFYING | Receipt 验证成功 | SUCCESS |
| RUNNING | 提交前可重试失败且剩余 Attempt >0 | QUEUED |
| RUNNING/VERIFYING | 确定业务失败且不需人工处理 | FAILED |
| RUNNING/VERIFYING | 策略错误、重试耗尽或有界容量耗尽 | DLQ |
| RUNNING/WAITING_APPROVAL/VERIFYING | task_kind=comment_campaign，`BOUND_DEVICE_PERMANENT_FAILURE` 且完整 WAL 明确证明无副作用 | DLQ |
| RUNNING/WAITING_APPROVAL | task_kind!=comment_campaign，`BOUND_DEVICE_PERMANENT_FAILURE`、完整 WAL 无副作用且剩余 Attempt >0 | QUEUED |
| RUNNING/WAITING_APPROVAL | task_kind!=comment_campaign，`BOUND_DEVICE_PERMANENT_FAILURE`、完整 WAL 无副作用且重试耗尽 | DLQ |
| RUNNING/VERIFYING | 外部副作用结果不确定 | PUBLISHED_UNVERIFIED |
| PUBLISHED_UNVERIFIED | VERIFY_ONLY/外部查询/确定证据确认已发布 | SUCCESS |
| PUBLISHED_UNVERIFIED | `submit_deterministic_evidence` 确认未发布且属于确定业务失败 | FAILED |
| PUBLISHED_UNVERIFIED | 第 8.2 节全部 guard 满足并执行 `close_as_unresolved` | CLOSED_UNVERIFIED |
| WAITING_DEPENDENCY/WAITING_CAPACITY/WAITING_CAPACITY_REPLACEMENT/QUEUED/ASSIGNED | 未开始外部副作用且安全取消 | CANCELLED |
| RUNNING/WAITING_APPROVAL | Attempt 仍为 PREPARED/INTERACTING 且 `side_effect_started=false` | CANCELLED |

终态：`SUCCESS`、`FAILED`、`DLQ`、`CANCELLED`、`CLOSED_UNVERIFIED`。`VERIFYING` 和 `PUBLISHED_UNVERIFIED` 不允许直接取消；`CLOSED_UNVERIFIED` 不允许重开。

### 9.3 Attempt 与副作用

状态集：`PREPARED`、`INTERACTING`、`WAITING_APPROVAL`、`SUBMITTING`、`VERIFYING`、`SUCCEEDED`、`FAILED`、`ABORTED`、`CANCELLED`、`PUBLISHED_UNVERIFIED`、`INDETERMINATE`。

| From | Event/Guard | To | 自动重试 |
|---|---|---|---|
| PREPARED | 打开窗口并开始页面交互 | INTERACTING | 仅确认未提交且页面安全复位后允许 |
| INTERACTING | 需要批准 | WAITING_APPROVAL | 否；保持原分配 |
| WAITING_APPROVAL | 批准且 Lease 有效 | INTERACTING | 否 |
| WAITING_APPROVAL | 拒绝或达到批准时限 | CANCELLED | 不适用 |
| WAITING_APPROVAL | Lease 在批准前过期/撤销且剩余 Attempt >0 | ABORTED | 可创建新 Attempt |
| WAITING_APPROVAL | Lease 在批准前过期/撤销且重试已耗尽 | FAILED | 禁止；error_category=retries_exhausted |
| PREPARED/INTERACTING | Lease 到期/撤销、`side_effect_started=false` 且剩余 Attempt >0 | ABORTED | 可创建新 Attempt |
| PREPARED/INTERACTING | Lease 到期/撤销、`side_effect_started=false` 且重试已耗尽 | FAILED | 禁止；error_category=retries_exhausted |
| PREPARED/INTERACTING | 确定的提交前环境失败，且剩余 Attempt >0 | ABORTED | 可创建新 Attempt |
| PREPARED/INTERACTING | 提交前失败但重试已耗尽 | FAILED | 禁止 |
| PREPARED/INTERACTING | 策略/schema/checksum 错误 | FAILED | 禁止 |
| INTERACTING | 明确业务拒绝且确认未产生副作用 | FAILED | 按错误分类决定 |
| PREPARED/INTERACTING/WAITING_APPROVAL | task_kind=comment_campaign，`BOUND_DEVICE_PERMANENT_FAILURE` 且完整 WAL 明确证明无副作用 | FAILED | 禁止；error_category=bound_device_permanent_failure |
| PREPARED/INTERACTING/WAITING_APPROVAL | task_kind!=comment_campaign，`BOUND_DEVICE_PERMANENT_FAILURE`、完整 WAL 无副作用且剩余 Attempt >0 | ABORTED | 可在其他 Device 创建新 Attempt |
| PREPARED/INTERACTING/WAITING_APPROVAL | task_kind!=comment_campaign，`BOUND_DEVICE_PERMANENT_FAILURE`、完整 WAL 无副作用且重试耗尽 | FAILED | 禁止；error_category=retries_exhausted |
| INTERACTING | 持久化 `side_effect_started=true` 后开始点击 | SUBMITTING | 禁止 |
| SUBMITTING | 获得确定提交结果 | VERIFYING | 禁止 |
| SUBMITTING | 超时、断连、进程退出或响应不确定 | PUBLISHED_UNVERIFIED | 禁止 |
| VERIFYING | Receipt 验证成功 | SUCCEEDED | 不适用 |
| VERIFYING | 明确平台拒绝且确认无副作用 | FAILED | 按错误分类决定 |
| VERIFYING | 验证窗口超时 | PUBLISHED_UNVERIFIED | 禁止 |
| PREPARED/INTERACTING | 崩溃、WAL 证明未开始提交且剩余 Attempt >0 | ABORTED | 可创建新 Attempt |
| PREPARED/INTERACTING | 崩溃、WAL 证明未开始提交且重试已耗尽 | FAILED | 禁止；error_category=retries_exhausted |
| PUBLISHED_UNVERIFIED | VERIFY_ONLY/外部查询/确定证据确认已发布 | SUCCEEDED | 不适用 |
| PUBLISHED_UNVERIFIED | `submit_deterministic_evidence` 确认未发布 | FAILED | 不自动重试 |
| PUBLISHED_UNVERIFIED | 第 8.2 节全部 guard 满足并执行 `close_as_unresolved` | INDETERMINATE | 永久禁止 |
| PREPARED/INTERACTING/WAITING_APPROVAL | `side_effect_started=false` 且安全取消 | CANCELLED | 不适用 |

`max_attempts=3` 表示总尝试最多 3 次：首次执行加最多 2 次重试。节点换号次数与 Attempt 次数分别计数。

Attempt 终态：`SUCCEEDED`、`FAILED`、`ABORTED`、`CANCELLED`、`INDETERMINATE`。`PUBLISHED_UNVERIFIED` 是冻结的人工收敛态，不是终态；它只能经只读验证、确定证据或第 8.2 节中性封存转入终态，不得启动新副作用。

| Attempt 状态/分类 | SubTask 迁移 |
|---|---|
| `SUCCEEDED` | `VERIFYING -> SUCCESS`，写验证结果 |
| `ABORTED` 且仍有剩余次数 | `RUNNING/WAITING_APPROVAL -> QUEUED`，后续创建更高 attempt_no；原 Attempt 不复用 |
| `FAILED` 且 error_category 为确定业务失败 | `RUNNING/VERIFYING -> FAILED` |
| `FAILED` 且 error_category 为 strategy、retries_exhausted 或 bound_device_permanent_failure | `RUNNING/WAITING_APPROVAL/VERIFYING -> DLQ` |
| `CANCELLED` | 满足安全取消 guard 后，SubTask 转 `CANCELLED` |
| `PUBLISHED_UNVERIFIED` | SubTask 原子转 `PUBLISHED_UNVERIFIED`，设置 `reassign_blocked=true` |
| `INDETERMINATE` | SubTask 原子转 `CLOSED_UNVERIFIED`，保持 `reassign_blocked=true` |

不得产生“ABORTED 且无剩余次数”：最后一次提交前失败直接写 Attempt `FAILED/error_category=retries_exhausted`。Attempt 与 SubTask 迁移、审计、Outbox 必须同事务提交。

### 9.4 Account

部署状态：`IMPORTED`、`WAITING_WINDOW`、`BINDING`、`WAITING_LOGIN`、`VERIFYING_IDENTITY`、`IDENTITY_REVIEW_REQUIRED`、`ACTIVE`、`FAILED`、`UNBOUND`。

业务状态：`ACTIVE`、`CAPTCHA`、`MANUAL_VERIFIED`、`SUSPENDED`、`MANUAL_REVIEW`、`INVALID`。

部署状态迁移：

| From | Event/Guard | To |
|---|---|---|
| IMPORTED | 无可用 Profile | WAITING_WINDOW |
| IMPORTED/WAITING_WINDOW | CAS 预留 Profile | BINDING |
| BINDING | 需要自动或人工登录 | WAITING_LOGIN |
| WAITING_LOGIN | 登录完成并采集实际身份 | VERIFYING_IDENTITY |
| VERIFYING_IDENTITY | 实际身份匹配预期 | ACTIVE |
| VERIFYING_IDENTITY | 身份不匹配 | IDENTITY_REVIEW_REQUIRED |
| IDENTITY_REVIEW_REQUIRED | 人工确认正确绑定 | ACTIVE |
| IDENTITY_REVIEW_REQUIRED | 人工拒绝绑定 | FAILED |
| ACTIVE | 账号 INVALID 且匹配代次清洗成功 | UNBOUND |
| IMPORTED/WAITING_WINDOW/BINDING/WAITING_LOGIN/VERIFYING_IDENTITY/IDENTITY_REVIEW_REQUIRED | 不可恢复部署错误 | FAILED |

业务状态迁移：

| From | Event/Guard | To |
|---|---|---|
| ACTIVE | 出现 CAPTCHA | CAPTCHA |
| ACTIVE/MANUAL_VERIFIED | 风控失败达到阈值 | SUSPENDED |
| CAPTCHA/SUSPENDED | 进入人工处置 | MANUAL_REVIEW |
| ACTIVE/MANUAL_VERIFIED | 运营或异常检测要求人工处置 | MANUAL_REVIEW |
| MANUAL_REVIEW | 人工验证完成 | MANUAL_VERIFIED |
| MANUAL_VERIFIED | 冷却结束且只读 PROBE 通过 | ACTIVE |
| ACTIVE/CAPTCHA/SUSPENDED/MANUAL_REVIEW/MANUAL_VERIFIED | 永久失效证据确认 | INVALID |

- 身份不一致不得进入业务候选池。
- `SUSPENDED` 不触发清洗。
- `INVALID` 触发绑定 Profile 清洗，且为业务终态。
- 部署状态和业务状态是两个正交字段，禁止混入 Profile 状态。

### 9.5 Profile

Agent 权威观察状态 `observed_status`：`PRESENT_STOPPED`、`PRESENT_STARTED`、`MISSING`。Central 权威分配状态 `allocation_status`：`AVAILABLE`、`RESERVED`、`BOUND`、`BUSY`、`EXTERNAL_BUSY`、`QUARANTINED`、`OFFLINE`。以下迁移均指 `allocation_status`。

| From | Event/Guard | To |
|---|---|---|
| AVAILABLE | 绑定事务 CAS | RESERVED |
| RESERVED | Agent 登录和身份核验成功 | BOUND |
| RESERVED | 绑定失败且确认未登录 | AVAILABLE |
| BOUND | Work Unit 开始 | BUSY |
| BUSY | Work Unit 安全结束 | BOUND |
| BOUND/BUSY | 本地人工占用 | EXTERNAL_BUSY |
| EXTERNAL_BUSY | 人工占用结束且同步确认 | BOUND |
| RESERVED/BOUND/BUSY/EXTERNAL_BUSY | 永久失效确认 | QUARANTINED |
| QUARANTINED | 匹配代次清洗成功 | AVAILABLE |
| AVAILABLE/RESERVED/BOUND/BUSY/EXTERNAL_BUSY | observed_status 连续 MISSING 达阈值 | OFFLINE |
| OFFLINE | 对账确认无绑定、无预留且 Profile 可用 | AVAILABLE |
| OFFLINE | 对账确认存在未过期预留且 revision 匹配 | RESERVED |
| OFFLINE | 对账确认有效绑定且无活跃 Work Unit | BOUND |
| OFFLINE | 对账确认本地人工占用 | EXTERNAL_BUSY |
| OFFLINE | 对账发现归属、代次或身份冲突 | QUARANTINED |

`QUARANTINED` 不因 observed_status 恢复而自动解除。离线前的活跃 Lease 必须先过期或撤销；恢复对账不得直接把 Profile 置 `BUSY`，新 Work Unit 必须取得当前 recovery_epoch 下更高 generation 的 Lease。

### 9.6 Lease

状态集：`ACTIVE`、`EXPIRED`、`REVOKED`、`RELEASED`。

| From | Event/Guard | To |
|---|---|---|
| 无 | `QUEUED` SubTask CAS 认领成功，lease_mode=EXECUTE | ACTIVE |
| 无 | `PUBLISHED_UNVERIFIED` 只读验证认领成功，lease_mode=VERIFY_ONLY | ACTIVE |
| ACTIVE | lease_mode、owner、device_session_id、recovery_epoch、generation 匹配且未过期 | ACTIVE（续期） |
| ACTIVE | 到达 expires_at | EXPIRED |
| ACTIVE | 管理员暂停、设备吊销或安全重派 | REVOKED |
| ACTIVE | Work Unit 完成或安全取消 | RELEASED |

Lease 终态不可恢复；重派必须在当前 recovery_epoch 新建更高 generation 的 Lease。

### 9.7 Cleanup

状态集：`SCHEDULED`、`WAITING_DELAY`、`RUNNING`、`RETRY_WAIT`、`MANUAL_REVIEW`、`SUCCEEDED`、`CANCELLED`、`ABANDONED`。

| From | Event/Guard | To |
|---|---|---|
| SCHEDULED | 进入延迟队列 | WAITING_DELAY |
| WAITING_DELAY | 到达执行时间且 fencing 校验通过 | RUNNING |
| RUNNING | 停窗和清理成功，代次仍匹配 | SUCCEEDED |
| RUNNING | 可恢复失败且次数未耗尽 | RETRY_WAIT |
| RETRY_WAIT | 到达 next_attempt_at 且 fencing 通过 | RUNNING |
| SCHEDULED/WAITING_DELAY/RUNNING/RETRY_WAIT | binding_revision/recovery_epoch/generation 不匹配 | CANCELLED |
| RUNNING/RETRY_WAIT | 重试耗尽 | MANUAL_REVIEW |
| MANUAL_REVIEW | 人工排除文件锁/环境问题后请求重试，且 fencing 仍匹配 | WAITING_DELAY |
| MANUAL_REVIEW | Agent 只读复核确认清理已完成，且 fencing 仍匹配 | SUCCEEDED |
| MANUAL_REVIEW | binding_revision/recovery_epoch/generation 已变化 | CANCELLED |
| MANUAL_REVIEW | 运营明确废弃本次清洗并填写理由 | ABANDONED |

重试延迟：`min(3600, 30 * 2^retry_count) + Random(0, 10)` 秒。终态为 `SUCCEEDED`、`CANCELLED`、`ABANDONED`。进入 `MANUAL_REVIEW` 或 `ABANDONED` 后 Profile 保持 `QUARANTINED`；只有当前代次 Cleanup `SUCCEEDED` 才能解除。`CANCELLED` 仅表示旧任务作废，不改变新代次 Profile 状态。

### 9.8 Device

状态集：`REGISTERING`、`ONLINE`、`OFFLINE`、`DEBUG`、`REVOKED`、`PERMANENT_FAILURE`。

| From | Event/Guard | To |
|---|---|---|
| 无 | platform_admin 创建设备登记 | REGISTERING |
| REGISTERING | 一次性注册成功并建立 Session | ONLINE |
| ONLINE | 心跳超过 device_offline_after_seconds | OFFLINE |
| OFFLINE | 当前 Session 的有效心跳恢复 | ONLINE |
| ONLINE/OFFLINE | 管理员进入调试，expected_revision 匹配 | DEBUG |
| DEBUG | 管理员退出调试且当前 Session 心跳有效/无效 | ONLINE/OFFLINE |
| REGISTERING/ONLINE/OFFLINE/DEBUG | 安全吊销或管理员停用 | REVOKED |
| REGISTERING/ONLINE/OFFLINE/DEBUG/REVOKED | `device.mark_permanent_failure`，expected_revision 匹配 | PERMANENT_FAILURE |

`device.mark_permanent_failure` 必须带 Idempotency-Key、expected_revision、failure_reason 和 evidence_refs，并在一个 PostgreSQL 事务中：

1. `FOR UPDATE` 锁定 Device；校验前态、revision 和操作者为 platform_admin。更新 status/failure_reason/revision，吊销当前 Device Session。
2. 锁定该 Device/Session 的全部 ACTIVE Lease 并转 `REVOKED`。Comment Campaign 按第 8.3 节以 Task 为单位整树收敛；同一 Task 只处理一次。
3. 非 Comment Campaign：尚无 Attempt 的 ASSIGNED SubTask 转 QUEUED；提交前且 `side_effect_started=false` 的 Attempt 转 ABORTED，仍有次数则 SubTask 转 QUEUED、否则转 FAILED/DLQ；已进入副作用或证据不足则 Attempt/SubTask 转 PUBLISHED_UNVERIFIED、`reassign_blocked=true`，Task 转 MANUAL_REVIEW。
4. 不在本事务创建任何新 Lease；提交 Device、Session、Lease、Attempt/SubTask/Task 迁移、不可变审计和 `BOUND_DEVICE_PERMANENT_FAILURE` Outbox 后，后续调度才可处理安全 QUEUED 节点。

相同 Idempotency-Key 重放返回首次结果，不重复迁移或发事件；不同 Key 对已经 PERMANENT_FAILURE 且 reason/evidence 相同的请求返回当前结果，对 reason/evidence 冲突或 expected_revision 过期的请求返回 409。PERMANENT_FAILURE 无出迁移；替换硬件必须使用新 device_id。

---

## 10. 父评论验证与依赖

父评论从提交后进入 60 分钟验证窗口：

- 0-10 分钟：每 2 分钟检查一次。
- 10-60 分钟：每 15 分钟检查一次。
- 60 分钟仍不可见：父节点转 `PUBLISHED_UNVERIFIED`，原因 `PARENT_VERIFICATION_TIMEOUT`。
- 明确平台拒绝：立即确定失败。
- 出现可见且身份匹配的 comment_id：Receipt `VERIFIED`，子节点转 `QUEUED`。

超时父节点的子节点保持 `WAITING_DEPENDENCY` 并标记 `PARENT_UNVERIFIED`，不直接销毁依赖：

- “重新校验”只执行只读验证，不重新提交父评论。
- 重新校验成功后激活子节点。
- “重试父任务”只在原 Attempt 明确未进入 `SUBMITTING` 时可用。
- “放弃整组”只能把所有尚未执行且无副作用的依赖子节点转 `CANCELLED` 并记录人工理由；不确定父节点继续保持 `PUBLISHED_UNVERIFIED`，除非另行满足第 8.2 节的 `close_as_unresolved` guard。

验证必须校验 comment_id、预期账号身份、目标视频和内容指纹，不能只依赖 DOM 文本相似。

依赖传播仅支持以下固定策略；v3.3 不开放客户自定义失败边策略：

| 父节点结果 | 尚未执行的直接/间接依赖节点 | Task 行为 |
|---|---|---|
| 全部父节点 Receipt `VERIFIED` | 满足其他 guard 后转 `QUEUED` | 继续执行 |
| 任一父节点 `FAILED` 或 `DLQ` | 递归转 `CANCELLED/ANCESTOR_TERMINAL_FAILURE` | 按第 9.1 节聚合 |
| 任一父节点 `CANCELLED` | 递归转 `CANCELLED/ANCESTOR_CANCELLED` | 按第 9.1 节聚合 |
| 任一父节点 `PUBLISHED_UNVERIFIED` | 保持 `WAITING_DEPENDENCY/PARENT_UNVERIFIED` | Task 转 `MANUAL_REVIEW` |
| 任一父节点 `CLOSED_UNVERIFIED` | 递归转 `CANCELLED/ANCESTOR_UNRESOLVED` | 按第 9.1 节聚合为 `COMPLETED_WITH_UNRESOLVED` |

一棵树最多 50 节点。父终态写入、依赖遍历、所有受影响节点迁移、Task 聚合、审计和 Outbox 必须在同一事务完成；事务失败全部回滚，禁止留下永久 `WAITING_DEPENDENCY`。

---

## 11. 错误分类、重试与人工处理

| error_category | 示例 | 自动重试规则 |
|---|---|---|
| `environment_pre_submit` | AdsPower 启动失败、CDP 连接失败 | 仅 Attempt 未进入 SUBMITTING；总尝试最多 3 |
| `strategy` | 策略缺失、checksum/schema 不匹配 | 不重试，DLQ |
| `account_temporary` | CAPTCHA、冷却、风险限制 | SUSPENDED/等待人工，不用任务重试确认失效 |
| `account_invalid` | banned、login_expired、verification_unrecoverable | 只读确认后 INVALID；不重试业务动作 |
| `external_rejected` | 平台明确拒绝且确认无副作用 | 确定失败；按策略决定 DLQ |
| `external_uncertain` | 点击后断连、响应丢失 | PUBLISHED_UNVERIFIED；只验证 |
| `capacity` | 无窗口、初始单机容量不足、无同机替换账号 | 无窗口可 WAITING；评论树初始不足 409 fail-fast；运行期替换达到 15 分钟后 DLQ |
| `unresolved_side_effect` | 原设备永久损坏且无安全查询手段 | 达到期限后只允许按未知结果封存；永久禁止重放 |
| `cancelled` | 用户取消、管理员终止 | 不重试 |

人工处理中心必须支持：

- 查看 DLQ、PUBLISHED_UNVERIFIED、IDENTITY_REVIEW_REQUIRED、MANUAL_REVIEW、清洗失败。
- 只读重新验证。
- 提交可复验的确定证据；证据不满足第 8.2 节 guard 时拒绝。
- 满足副作用 guard 时重派。
- 对满足安全 guard 的节点终止或放弃，并填写理由；`PUBLISHED_UNVERIFIED` 只提供只读验证、确定证据收敛和第 8.2 节 `close_as_unresolved`，不提供强制成功、强制失败或重放入口。
- 查看完整 Attempt、Receipt、recovery_epoch/generation、账号替换和审计链。

界面不得提供绕过状态 guard 的“强制成功”或“无条件重试”。

---

## 12. Agent 身份与凭据

### 12.1 设备注册

采用一次性设备注册密钥：

1. `platform_admin` 创建设备登记和一次性注册密钥。
2. Agent 通过 TLS 提交注册密钥和设备指纹摘要。
3. Central 原子消费注册密钥，签发 device_id、device_session_id 和默认有效 15 分钟的短期 Session Token。
4. Token 绑定 device_id、device_session_id、recovery_epoch、aud、iat、exp；剩余有效期不足 5 分钟时，Agent 使用当前有效 Session 续签。
5. 新 Session 成功建立后，旧 Session Token 和旧 SSE 连接失效。
6. 支持设备吊销、重新注册、密钥轮换和审计。

禁止仅凭 device_id、IP、主机名或 User-Agent 认证。

`PERMANENT_FAILURE` 仅由 `platform_admin` 按第 9.8 节设置，必须填写 failure_reason=`DESTROYED/HARDWARE_LOSS/DECOMMISSIONED_WITHOUT_RECOVERY` 和证据引用。该 device_id 不得重新注册、取得新 Session 或恢复 ONLINE；修复/替换硬件必须登记新 device_id。以后找回的离线证据：Attempt 仍为 PUBLISHED_UNVERIFIED 时走 `manual_review.submit_deterministic_evidence`；已经 INDETERMINATE 时只可追加 `late_reconciliation`。两条路径均不得恢复 VERIFY_ONLY/EXECUTE。

### 12.2 凭据信封加密

- 每条凭据使用随机 DEK 和 AES-256-GCM 加密。
- KEK 通过环境、Docker Secret 或权限受限文件注入，不存数据库、仓库或镜像。
- 关联数据绑定 customer_id、account_id、credential_revision、algorithm_version。
- 密钥轮换优先重封装 DEK。
- 明文凭据不得进入任务快照、Outbox、Inbox、日志、Receipt、截图元数据、WS 或 SSE。

### 12.3 一次性 grant

grant 必须绑定：

```text
grant_id
device_id
device_session_id
account_id
subtask_id
lease_generation
recovery_epoch
lease_mode=EXECUTE
credential_revision
aud
exp
consumed_at
```

- 默认 TTL 60 秒。
- 领取时再次校验设备 Session、Lease、账号/Profile 绑定和未消费状态。
- lease_mode 必须为 `EXECUTE`；`VERIFY_ONLY` 请求 grant 固定返回 403 并写安全审计。
- `consumed_at` 使用原子 CAS 写入；重复领取返回 409。
- Agent 只在内存中保留当前登录流程所需明文，流程结束或过期立即清除。

---

## 13. 同步、冲突与数据保留

### 13.1 事件信封

所有 Agent 同步事件至少包含：

```text
event_id
event_type
source_device_id
source_db_uuid
source_revision
aggregate_type
aggregate_id
occurred_at
payload_checksum
envelope_fingerprint
recovery_epoch
payload
```

`envelope_fingerprint` 使用 `event_envelope_v1` canonicalization，对 `event_id/event_type/source_device_id/source_db_uuid/source_revision/aggregate_type/aggregate_id/occurred_at/payload_checksum/recovery_epoch` 的完整规范化信封计算 SHA-256；不直接重复序列化 payload，而以 payload_checksum 绑定内容。Central 必须重算并校验，不能信任 Agent 提交值。同 revision 只要 fingerprint 不同即为协议冲突，即使 payload_checksum 相同也不得视为重复。

### 13.2 处理规则

- `inbox_raw_events` 只保存和追踪原始信封，不作为永久去重账本；叶分区本地索引只减少重复存储，业务正确性不得依赖 raw 行或其索引永久存在。
- source_revision 小于等于 `source_event_checkpoints.applied_through_revision`：必须读取不可清理 `source_event_ledger`。同 revision 且 envelope_fingerprint 相同，返回 ledger 保存的 disposition/domain_result_ref，不读取已删除 raw 分区且不重复应用；fingerprint 不同返回 409 `SOURCE_REVISION_ENVELOPE_CONFLICT`；ledger 缺行返回 503 `IDEMPOTENCY_LEDGER_INCOMPLETE` 并告警，禁止猜测成功或重新应用。
- source_revision 等于 checkpoint+1：Central 先重算 fingerprint，再在同一事务锁定来源 checkpoint，执行领域唯一键/Fencing 校验，写业务状态与审计，插入 ledger 的 payload_checksum/envelope_fingerprint/disposition/domain_result_ref 并推进 checkpoint。目标日期分区存在时同事务把对应 raw event 的 `safe_to_purge_at` 设置为不早于处理完成后 7 天；分区缺失时保持 NULL，由后续清理事务复验 ledger 后路由，不影响业务事务耐久完成。
- source_revision 大于 checkpoint+1：拒绝后续 delta，返回 `resync_required`。
- Receipt、Result、Lease、Attempt 和可重放命令即使不使用顺序 revision，也必须由其领域唯一键或 `command_idempotency_records` 保证幂等；重复请求返回原结果，fingerprint 冲突返回 409，禁止仅查询 Inbox 消息 ID 决定是否执行。
- 删除必须发送 tombstone；记录缺失不表示删除。
- customer_id、账号归属、Central 状态冲突进入隔离队列，禁止 last-write-wins。
- 普通同步 ACK/checkpoint 只表示允许进入保留期清理判断：当前 recovery_epoch 的已 ACK 数据仅在 age >=7 天后可清理；未 ACK 数据无论年龄均不得删除。
- PITR 后旧 recovery_epoch 数据不使用普通 ACK，必须走第 15.3 节恢复专用摄取和下述 disposition/purge checkpoint 协议。

旧 recovery_epoch 恢复证据采用“两阶段确认”：

1. Agent 以当前有效 Device Session 上传原始信封，保留 `source_device_id/source_db_uuid/source_recovery_epoch/source_revision/event_id/payload_checksum/envelope_fingerprint`。入口必须校验 source_device_id 等于当前 Session 绑定 device_id，且 source_db_uuid 属于该设备历史登记，并重算 fingerprint；不匹配返回 403 `RECOVERY_SOURCE_DEVICE_MISMATCH`，不写隔离库。唯一幂等键为 `(device_id, source_db_uuid, source_recovery_epoch, source_revision)`；同键同 fingerprint 返回原 durable ACK/disposition，同键不同 fingerprint 返回 `409 SOURCE_REVISION_ENVELOPE_CONFLICT`、不推进水位且 Agent 必须保留原记录。
2. Central 先把完整原始信封提交到恢复隔离库或加密对象存储；提交成功才返回 `durable_ingest_ack`。该 ACK 只证明 Central 已耐久持有证据，不表示权威业务状态已应用。
3. 对账服务异步写唯一 `recovery_disposition`：`APPLIED` 表示已幂等应用，`DUPLICATE` 表示权威结果已存在且未重复应用，`QUARANTINED_DURABLE` 表示证据已耐久隔离等待人工，`RETRYABLE_REJECTED` 表示 Central 尚未安全持有或处理完成、Agent 必须保留并重试。
4. 无法解析或业务校验失败的证据不得静默丢弃；Central 必须先按原字节加密归档、记录 checksum/archive_ref，才可写 `QUARANTINED_DURABLE`。checksum 不匹配、上传截断或存储提交失败只能写/返回 `RETRYABLE_REJECTED`。
5. Central 仅对连续、无缺口且 disposition 属于 `APPLIED/DUPLICATE/QUARANTINED_DURABLE` 的最大 source_revision 签发 `recovery_purge_checkpoint`。Checkpoint 绑定 `(device_id, source_db_uuid, source_recovery_epoch, purge_through_revision)`，单调递增、可审计并由恢复专用非对称密钥签名；Agent 通过当前 Device Session 获取由平台根密钥签名的验证公钥，未知/已吊销 key_id 一律拒绝。
6. Agent 只有在本地记录 age >=7 天、收到并验证签名 checkpoint、且记录 revision 不大于连续水位时才可删除旧纪元本地副本。任何 `RETRYABLE_REJECTED` 或水位缺口后的记录都不得删除。

### 13.3 保留与背压

| 数据 | 保留策略 |
|---|---|
| Agent 当前纪元事件/WAL/Receipt | ACK 与否均至少保留 7 天；普通未 ACK 数据超过 7 天仍不得删除，只能扩容或停领新任务 |
| Agent 旧纪元恢复证据 | 至少保留 7 天；仅在 durable_ingest_ack、可清理 disposition 和签名连续 purge checkpoint 同时满足后删除；Central 隔离/归档副本按审计策略保留 |
| Central Inbox 未证明安全的原始事件 | `safe_to_purge_at=NULL`，位于 default partition；无论年龄均不得删除 |
| Central Inbox 已处理原始事件 | checkpoint/领域幂等依据提交后才设置 safe_to_purge_at；至少再保留 7 天，之后按日期分区直接 DROP；删除不影响旧事件幂等 ACK |
| Central TaskResult/DLQ | 热数据 90 天，至少保留 180 天；之后归档 |
| Central Audit | 按月分区；热数据 90 天、至少 180 天在线可查询；加密归档 checksum 验证成功后才可 DETACH/DROP |
| Central 运行日志 | 30 天；安全审计按合规要求延长 |
| Web 事件流 | 可回放窗口至少 24 小时；容量按压测峰值事件率 × 24 小时 × 1.2 配置，低于 24 小时视为容量验收失败；超窗必须全量补拉 |

Agent 缓冲达到 80% 发告警；达到 90% 暂停全量库存等非关键工作并优先恢复摄取；达到 95% 停止领取新 Work Unit，只保留心跳、续租、Receipt 与恢复上传。保留 age 从 Agent 首次本地耐久写入时间计算，不信任 occurred_at；磁盘压力不得绕过 7 天、disposition 或 purge checkpoint 删除证据。

Inbox 分区清理器只能 DROP `safe_to_purge_at` 已过期的日期分区；default partition、存在 NULL/未来 safe_to_purge_at 的分区或归档校验失败的分区必须 fail-closed。Audit 分区维护与业务 Worker 使用相同 `WORKER_BACKGROUND` 隔舱和有界批次，不得锁住 Agent 热路径。对数千万行规模的索引风险按缓存命中、写放大、WAL、VACUUM/bloat 和分区维护时延评估，不使用“B-tree 查询线性增长”的错误假设。

---

## 14. API 与事件契约

### 14.1 权威契约

- HTTP 字段、路径、状态码、认证、分页和示例以版本化 OpenAPI 为唯一权威。
- SSE/WS/Outbox/Inbox 信封以版本化 JSON Schema 为唯一权威。
- M5a 开工门：库存 snapshot/delta、设备注册、Agent Session、工作拉取、续租、结果、grant、Web 事件接口全部有 Schema 和契约测试。
- 不兼容变更必须新增 major version；旧版本按 Agent 灰度窗口保留。

### 14.2 统一请求规则

- 写请求必须带 `request_id`；可重放创建命令必须带 `Idempotency-Key`。
- Agent 写请求必须带 event_id、device_session_id、recovery_epoch 和相关 generation/revision。
- VERIFY_ONLY 结果必须带第 8.2 节定义的 verify Lease Fencing 与目标 Attempt 执行 Fencing/revision，缺一返回 422，不匹配返回 409。
- 错误体至少包含 `code`、`message`、`request_id`、`retryable`、可选 `details`。

| 场景 | HTTP 状态 |
|---|---|
| 未认证/Token 过期 | 401 |
| 已认证但无权限 | 403 |
| 不存在或客户不可见 | 404 |
| revision/Fencing 标识/状态冲突 | 409 |
| 参数或 Schema 错误 | 422 |
| 速率限制 | 429 + Retry-After |
| 数据库 Pool/依赖暂时过载 | 503 + Retry-After；不得返回通用 500 |

### 14.3 事件可靠性

- Outbox 与业务事务同库提交，发布成功后标记 dispatched。
- Redis Stream 保存可回放控制事件和单调序号。
- 多实例模式禁止使用进程内 Memory 事件流作为可恢复降级。
- Redis 不可用时，Agent 降级 HTTP Pull；第 `n` 次连续失败等待 `min(60, 5 × 2^n) + U(0, 3)` 秒，`n` 从 0 开始。
- Web 事件连接失败后只能轮询 Flask BFF，不得由 Browser 直连 Central HTTP。包括首次降级请求在内，必须先等待 `15 + U(0, 5)` 秒；后续轮询沿用同一全抖动间隔，页面不可配置更短间隔。
- Web 事件令牌的 `jti` 原子消费使用 Redis；Redis 不可用时新 WebSocket 握手失败关闭并返回 503，页面只经 Flask 使用 HTTP 快照/轮询，禁止跳过防重放校验。
- Flask BFF 快照响应必须使用 ETag/If-None-Match；对相同 `(customer_id, principal_scope_hash, route, canonical_query)` 使用 3 秒 stale snapshot cache 和 single-flight，未变化返回 304。缓存和合并只减少重复读取，不改变 Central 权威或客户隔离。
- Central Web fallback 查询使用独立于 Redis 的 admission control。PostgreSQL 预建 5 个 budget slot；每个服务进程最多以 generation CAS 租用一个 slot，每 slot 本地 token bucket 为 `floor(50/5)=10 QPS`，因此全部有效 slot 总和始终 <=50 QPS。Slot TTL 90 秒、每 30 秒续租；进程在数据库 expires_at 前 5 秒即停止使用，失租或无 slot 只返回 429 和 `Retry-After=15+U(0,5)`。扩缩容只竞争固定 slot，不得给新实例另配 50 QPS；修改全局 QPS 或 slot 数必须同一配置 revision 校验 `floor(global_qps/slots) × slots <= global_qps`。
- fallback 预算配置禁止原地修改。切换 revision 时，配置服务先 CAS 将 budget_state 置 DRAINING，停止旧 revision 的获取/续租；所有实例在 expires_at 前 5 秒停用旧 bucket。以 PostgreSQL `now()` 确认旧 slot 全部过期后，单事务创建带新 config_revision/slot_qps 的 slot 并切换 active revision/state=ACTIVE。DRAINING 期间 fallback 返回 429；任何时刻只有 active revision 可放行请求，禁止新旧 token bucket 重叠。
- revision 切换是可接管状态机：协调 Worker 以 transition_id 和 30 秒 owner lease CAS 持有 DRAINING；崩溃后任一健康 Worker 可在 owner lease 到期后接管，并根据数据库时间幂等继续“等旧 slot 过期 -> 激活新 revision”。DRAINING 超过 120 秒触发严重告警但保持 fail-closed 429，不回滚或复活旧 bucket；平台管理员可重试同一 transition_id，禁止创建并行切换。
- Web fallback、普通 API、Agent 数据面、Web 事件握手和 Worker 分别使用独立 admission 与第 4.5 节数据库 Pool；至少 30% 应用数据库连接硬分配给 `AGENT_CRITICAL`。任一其他 Pool 不得排队或借用该容量，四类 Pool 跨全部实例的上限必须满足部署级总预算。
- Redis 恢复后 Browser 在 0-60 秒内全抖动重连；事件入口全局最多接受 25 次新握手/秒，超限返回 429，避免恢复瞬间再次冲击 Central。
- 收到 429 时，实际等待时间取 `max(Retry-After, 本地退避值)`；所有等待单位为秒。
- 事件断档或超过保留期时返回 `resync_required`。
- SSE/WS 事件只含 event_id、event_type、作用域和安全聚合引用。

### 14.4 v3.3 发布阻塞接口

以下逻辑接口必须在各自对应里程碑的 OpenAPI 中确定实际路径、字段 Schema 和契约测试；不得留给实现自行解释：

| 逻辑接口 | 认证/权限 | 必填输入 | 成功/失败契约 |
|---|---|---|---|
| Agent Work Unit pull | 当前 Device Session | capabilities、拉取游标/上限 | Work Unit Envelope 只返回 snapshot 元数据和 dynamic_inputs，完整 Envelope <=16 KiB；不得内联完整 config_snapshot；池过载返回 503 + Retry-After 且不改变 Lease/Attempt |
| `config_snapshot.get` | 当前 Device Session + 与 Task 关联的 ACTIVE Lease | snapshot_id、snapshot_checksum、可选 If-None-Match | 仅返回该 Lease 引用的不可变制品；命中返回 200 + ETag + gzip，缓存有效返回 304；越权 404，过大/压缩比异常/校验失败拒绝且无凭据下发 |
| Comment Campaign create | Customer Principal + Idempotency-Key | 现有创建 Schema、账号过滤器 | 202 返回 task_id；初始单机容量不足且诊断已持久化时返回 409 `INSUFFICIENT_SINGLE_DEVICE_CAPACITY`；诊断持久化失败返回 503 `CAPACITY_DIAGNOSTIC_UNAVAILABLE` + Retry-After；公开 details 仅含 request_id |
| `capacity_diagnostics.get` | `platform_admin/operator` | request_id | 返回第 6.3 节完整内部诊断；Customer 一律 404 |
| `device.mark_permanent_failure` | `platform_admin` + Idempotency-Key | device_id、expected_revision、failure_reason、evidence_refs | 按第 9.8 节原子迁移并返回新 revision/受影响 Task 摘要；stale revision 或冲突证据返回 409 |
| `manual_review.submit_deterministic_evidence` | `platform_admin` + Idempotency-Key | attempt_id、expected_revision、resolution=`CONFIRMED_PUBLISHED/CONFIRMED_NOT_PUBLISHED`、evidence_type、evidence_refs、checksum、note | 仅允许 PUBLISHED_UNVERIFIED；验证证据类型/checksum 后按第 8.2/9.3 节收敛，guard 不满足返回 409 |
| `manual_review.close_as_unresolved` | `platform_admin` | subtask_id、expected_revision、reason_code、evidence_refs、note | guard 满足返回封存后的 Task/SubTask revision；未到期限或仍有安全验证手段返回 409 `UNRESOLVED_SEAL_GUARD_FAILED` |
| `late_reconciliation.append` | `platform_admin` + Idempotency-Key | attempt_id、resolution、evidence_type、evidence_refs、checksum、note | 仅允许 INDETERMINATE Attempt；服务端复验 evidence_type/checksum 后追加并返回 reconciliation_id，不修改任何状态/统计，不可复验或 guard 不满足返回 409 |
| `recovery_ingest.batch` | 当前 Device Session | source_device_id、source_db_uuid、source_recovery_epoch、起止 revision、逐条 envelope_fingerprint、最多 500 条且解压后 <=10 MiB 的原始信封 | source_device_id/数据库归属先与 Session 校验，Central 逐条重算 fingerprint；每条返回 durable_ack、disposition 或 retryable error；整批部分成功必须逐条可重放，不得用批级 200 暗示全部成功 |
| `recovery_purge_checkpoint.get` | 当前 Device Session | source_db_uuid、source_recovery_epoch、after_revision | 返回单调 purge_through_revision、签名、签名 key_id、issued_at；无新水位返回 204 |
| Web fallback snapshot | Flask BFF Session + CSRF 规则 | view、canonical query、可选 If-None-Match | 200 + ETag、未变化 304、超预算 429 + Retry-After；响应不得暴露内部资源 |

OpenAPI 错误码必须至少增加：`INSUFFICIENT_SINGLE_DEVICE_CAPACITY`、`CAPACITY_DIAGNOSTIC_UNAVAILABLE`、`DEVICE_PERMANENT_FAILURE_CONFLICT`、`DETERMINISTIC_EVIDENCE_GUARD_FAILED`、`UNRESOLVED_SEAL_GUARD_FAILED`、`LATE_RECONCILIATION_GUARD_FAILED`、`SOURCE_REVISION_ENVELOPE_CONFLICT`、`IDEMPOTENCY_LEDGER_INCOMPLETE`、`RECOVERY_SOURCE_DEVICE_MISMATCH`、`RECOVERY_INGEST_RETRYABLE_REJECTED`、`WEB_FALLBACK_RATE_LIMITED`、`DB_POOL_OVERLOADED`、`CONFIG_SNAPSHOT_UNAVAILABLE`、`CONFIG_SNAPSHOT_TOO_LARGE`、`CONFIG_SNAPSHOT_CHECKSUM_MISMATCH`、`CONFIG_SNAPSHOT_SCHEMA_UNSUPPORTED` 和 `CONFIG_SNAPSHOT_ACCESS_DENIED`。所有内部 details 继续受第 3.2 节隔离规则约束。

---

## 15. 非功能要求

### 15.1 容量

- 500 台 Agent 同时在线。
- 100 个 Customer。
- 500 个同时在线 Web Session，最多 500 条实时看板连接。
- 单棵评论树最多 50 节点。
- 单次普通账号批任务最多 500 账号。
- 15 万 SubTask 排队。
- 单个未压缩 config_snapshot <=512 KiB、JSON 深度 <=32；Work Unit Envelope（不含独立制品）<=16 KiB。
- Agent config snapshot 加密缓存默认上限 512 MiB；淘汰只影响后续重新下载，不改变 Task 冻结语义。

### 15.2 性能

| 指标 | 门槛 |
|---|---|
| Central 普通热路径 API | P99 <100ms |
| 任务创建接受响应 | P99 <=500ms；完整初始计划同步持久化，节点激活与调度异步 |
| 15 万排队调度 tick | <1s |
| Agent 心跳写入 | P99 <50ms |
| 事件提交到看板 | P95 <=1s |
| 批准后 Agent 发起权威拉取 | P95 <1s |
| 超时租约发现并处置 | <=30s |
| 回收器单轮数据库处理 | <500ms |
| 热缓存 Work Unit 拉取 | P99 <100ms；不序列化完整 config_snapshot |
| 冷缓存 config_snapshot 下载 | 单 Agent/单 checksum 仅一次并发下载；下载完成后 checksum 100% 校验 |

统计必须说明硬件、数据库连接池、数据量、预热、样本数和持续时间。上传大文件、SSE 长连接本身和第三方 TikTok/AdsPower 延迟不计入普通 API P99，但必须单独报告。

数据库性能测试必须分别报告四类 Pool 的 `checked_out/limit`、acquire wait、timeout、transaction duration、idle-in-transaction、statement/lock timeout 和跨实例总连接数。Worker 单事务最多处理配置批量，事务 P99 必须小于 500ms；超限批次中止并缩小下一批，不得长期占用连接。

### 15.3 可靠性

- 500 Agent 稳态压测至少 60 分钟。
- PG、Redis、Central、Agent 重启后不出现重复分配和状态回退。
- Redis 故障期间数据面仍可通过 HTTP 权威拉取恢复。
- Agent 断网期间未 ACK 数据持久化；重连后按 revision 补报。
- Central 多实例下使用 PG 唯一约束、CAS 和 SKIP LOCKED 保证不重复认领。
- 四类工作负载必须使用第 4.5 节独立 admission/Engine/Pool；API、Worker 或 Web 事件流量耗尽自身预算时不得排队占用 Agent 保留容量。Worker 饱和只能延后后台批次，不能使 Agent 心跳、续租、Receipt 或结果提交超时。

PostgreSQL PITR 恢复必须执行恢复纪元协议：

1. 恢复实例首先保持 `scheduling_frozen=true`，生成新的不可预测 `recovery_epoch` 和 `web_session_epoch`，并轮换 Flask Session 签名材料；禁止沿用备份中的旧值。
2. 普通业务接口拒绝旧 recovery_epoch 的 Device Session、Lease、事件、Receipt 和结果；Central 将备份中残留的 ACTIVE Lease 批量置 `REVOKED`。旧 web_session_epoch 的 Flask Session 和事件连接全部失效，所有 Web 用户重新登录。
3. Agent 取得新 recovery_epoch 的 Session 后，只能通过恢复专用摄取接口上传本地保留的旧 epoch 事件/WAL/Receipt。普通业务接口继续拒绝旧 epoch；专用接口按第 13.2 节返回 durable_ingest_ack，证据保留原 source epoch/revision，只进入隔离队列，不直接修改权威状态。
4. 对账应用服务校验原 Task、账号、设备、内容指纹和副作用阶段后写 recovery_disposition。恢复点时处于 `SUBMITTING/VERIFYING` 或证据不足的 Attempt 一律转 `PUBLISHED_UNVERIFIED` 并设置 `reassign_blocked=true`，禁止自动补提；每条证据重复处理不得重复应用。
5. Central 为每个 `(device_id, source_db_uuid, source_recovery_epoch)` 维护连续 disposition 水位并签发 purge checkpoint；旧纪元普通拒绝不等于可删除，Agent 只能按第 13.2 节水位清理。
6. 仅当所有在线 Agent checkpoint 连续、离线 Agent 已显式隔离、差异报告归零或进入人工队列后，平台管理员才可 CAS 解除调度冻结。

恢复过程、纪元变化、旧租约撤销、差异清单和解除冻结批准必须写不可变审计。

恢复目标分开计时：从恢复命令开始，`control_plane_readonly_rto <=30 分钟`，终点为新 Web 登录成功且关键只读查询可用；`recovery_ingest_rto <=30 分钟`，终点为新 Agent Session 和恢复专用摄取接口可用；`scheduling_unfreeze_target <=60 分钟`，终点为在线 Agent 对账完成、离线 Agent 已隔离且调度解冻。RPO 目标为不超过 5 分钟。

### 15.4 安全

- 外部入口强制 HTTPS。
- Flask 生产使用 Waitress/Gunicorn 等生产 WSGI Server，前置反向代理。
- Central/Agent 管理端口不直接公网暴露。
- Web、内部服务、Agent 使用分离的身份与密钥。
- 日志、快照、队列、事件、API、WS、SSE 无明文凭据。
- Agent 的 customer 私有 config snapshot 缓存必须加密、按 customer/schema/checksum 隔离；登出、设备吊销或缓存损坏不得导致跨 customer 读取或跳过 checksum 校验。
- 所有跨客户测试覆盖 HTTP 查询、写入、批量导出、事件订阅和错误信息。

### 15.5 兼容性

- Windows 10/11 Agent。
- Python 3.12。
- Flask 3.1.x。
- FastAPI Central + ASGI Server。
- PostgreSQL 16。
- Node >=20 用于前端测试。
- AdsPower Local API v5+。

---

## 16. 配置项

所有配置经版本化配置服务发布，记录 scope、version、effective_at、操作者和审计。客户不可修改底层风控参数。

| 配置键 | 默认值 | 说明 |
|---|---:|---|
| `account_min_action_interval_seconds` | 60 | 单账号最小动作间隔 |
| `account_daily_action_limit` | 200 | 单账号每日动作上限 |
| `manual_verified_cooldown_seconds` | 7200 | 人工验证后冷却 |
| `device_window_concurrency` | 3 | 单机并行窗口上限；单 Profile 并发始终为 1 |
| `global_action_rate_per_second` | 50 | 全局新动作节流 |
| `risk_failure_threshold` | 3 | 风控熔断阈值 |
| `invalid_identity_observation_threshold` | 3 | 永久失效只读确认阈值 |
| `max_attempts` | 3 | 总执行尝试次数，含首次 |
| `task_missed_window_seconds` | 300 | 未单独指定时的计划任务容许迟到窗口 |
| `lease_ttl_seconds` | 120 | Work Unit Lease 有效期 |
| `lease_renew_interval_seconds` | 30 | Agent 正常续租间隔 |
| `lease_min_before_submit_seconds` | 60 | 进入外部副作用前所需最小剩余租期 |
| `verify_only_max_rounds` | 10 | 不确定副作用自动只读验证轮数上限 |
| `verify_only_base_delay_seconds` | 60 | VERIFY_ONLY 首轮失败后的基础退避 |
| `verify_only_max_delay_seconds` | 900 | VERIFY_ONLY 最大退避 |
| `unresolved_seal_wait_seconds` | 86400 | 安全验证手段穷尽后允许按未知结果封存前的等待时间；不得低于 1 小时 |
| `node_replacement_limit` | 3 | 单节点同机换号上限 |
| `comment_tree_spare_ratio_percent` | 20 | 初始及动态缩减后的硬备用比例；v3.3 不允许调低或借调 |
| `replacement_wait_seconds` | 900 | 同机替换容量等待上限 |
| `comment_parent_verification_timeout_minutes` | 60 | 父评论验证终态时间 |
| `cleanup_delay_seconds` | 10 | QUARANTINED 后清洗延迟 |
| `cleanup_max_attempts` | 10 | 单个清洗任务总尝试上限 |
| `adspower_api_max_concurrency` | 1 | 每台 Agent 所有模块共享的 Local API 最大并发；v3.3 不允许调高 |
| `adspower_api_min_interval_ms` | 1000 | 设备级 Local API 调用最小间隔 |
| `adspower_api_queue_capacity` | 100 | AdsPowerGateway 有界队列上限 |
| `adspower_api_critical_queue_timeout_seconds` | 10 | P0/P1 最大排队等待时间 |
| `adspower_api_background_queue_timeout_seconds` | 60 | P2-P4 最大排队等待时间 |
| `adspower_api_background_timeout_seconds` | 5 | P2-P4 后台调用超时 |
| `adspower_api_critical_timeout_seconds` | 30 | P0/P1 关键调用超时 |
| `adspower_api_high_priority_burst` | 10 | 连续高优先级请求后放行一个已等待后台请求 |
| `adspower_api_circuit_failure_threshold` | 5 | 连续依赖失败熔断阈值 |
| `adspower_api_circuit_open_seconds` | 30 | AdsPowerGateway 熔断打开时间 |
| `credential_grant_ttl_seconds` | 60 | 一次性 grant 有效期 |
| `web_event_token_ttl_seconds` | 60 | WebSocket 握手事件令牌有效期 |
| `agent_session_token_ttl_seconds` | 900 | Agent Session Token 有效期 |
| `agent_session_renew_before_seconds` | 300 | Agent 提前续签阈值 |
| `agent_heartbeat_interval_seconds` | 30 | Agent 心跳间隔 |
| `login_failure_window_seconds` | 900 | Web 登录失败统计窗口 |
| `login_failure_threshold` | 5 | 窗口内连续失败锁定阈值 |
| `login_lock_seconds` | 900 | Web 账号锁定时长 |
| `approval_timeout_seconds` | 3600 | 人工批准最长等待时间 |
| `config_snapshot_max_uncompressed_bytes` | 524288 | 发布和下载允许的未压缩 canonical bytes 上限 |
| `config_snapshot_max_json_depth` | 32 | 快照最大 JSON 深度 |
| `config_snapshot_max_compression_ratio` | 20 | 解压后/压缩前最大比例，超限拒绝 |
| `config_snapshot_cache_max_bytes` | 536870912 | 单 Agent 加密 LRU 缓存上限 |
| `config_snapshot_download_jitter_seconds` | 5 | 冷缓存下载全抖动上限 |
| `work_unit_envelope_max_bytes` | 16384 | 不含独立 snapshot 制品的 Work Unit Envelope 上限 |
| `event_replay_retention_hours` | 24 | Web 事件最短可回放时长 |
| `web_event_principal_recheck_seconds` | 60 | 活跃 WebSocket Principal 复核间隔 |
| `event_replay_capacity_multiplier` | 1.2 | 相对峰值 24 小时事件量的容量系数 |
| `web_fallback_poll_interval_seconds` | 15 | Web 事件故障后的最小 BFF 轮询间隔 |
| `web_fallback_poll_jitter_seconds` | 5 | Web fallback 每轮全抖动上限 |
| `web_fallback_cache_ttl_seconds` | 3 | BFF stale snapshot cache 与 single-flight 合并窗口 |
| `web_fallback_global_qps` | 50 | 所有有效 Central fallback budget slot 的总上限 |
| `web_fallback_budget_slots` | 5 | PostgreSQL 预建固定 slot 数；每服务进程最多租用一个 |
| `web_fallback_budget_lease_seconds` | 90 | fallback slot 租期 |
| `web_fallback_budget_renew_seconds` | 30 | fallback slot 续租周期；到期前 5 秒停止使用 |
| `web_fallback_transition_lease_seconds` | 30 | DRAINING 协调者 owner lease；超时允许健康 Worker 接管 |
| `web_fallback_budget_drain_timeout_seconds` | 120 | DRAINING 严重告警阈值；超时仍保持 fail-closed |
| `web_event_reconnect_global_qps` | 25 | Redis 恢复后 WebSocket 新握手总预算 |
| `db_application_connection_budget` | 120 | 所有 Central/Worker/Web 事件实例合计应用连接硬上限；仍受第 4.5 节 PostgreSQL 公式约束 |
| `db_admin_reserved_connections` | 20 | 不分配给应用 Pool 的 slot-control、运维、迁移、复制和故障处理保留连接 |
| `agent_db_pool_reserved_percent` | 30 | `AGENT_CRITICAL` 硬分配比例；其他 Pool 不得借用 |
| `api_db_pool_percent` | 35 | `API_INTERACTIVE` 硬分配比例 |
| `worker_db_pool_percent` | 20 | `WORKER_BACKGROUND` 硬分配比例 |
| `web_event_db_pool_percent` | 15 | `WEB_EVENT` 硬分配比例；四类比例之和必须等于 100 |
| `agent_db_pool_process_slots` | 3 | AGENT_CRITICAL 最大并发进程 slot，包含滚动升级 surge |
| `api_db_pool_process_slots` | 3 | API_INTERACTIVE 最大并发进程 slot，包含滚动升级 surge |
| `worker_db_pool_process_slots` | 3 | WORKER_BACKGROUND 最大并发进程 slot，包含独立 Worker/surge |
| `web_event_db_pool_process_slots` | 3 | WEB_EVENT 最大并发进程 slot，包含滚动升级 surge |
| `db_pool_slot_lease_seconds` | 90 | process slot 租期；实例在到期前 5 秒停止接收新流量 |
| `db_pool_slot_renew_seconds` | 30 | process slot 续租周期 |
| `db_pool_connection_drain_seconds` | 10 | slot 接管等待旧 generation 连接自然归零的上限；随后终止残留 backend |
| `db_pool_max_overflow` | 0 | 四类 Pool 固定 overflow；v3.3 禁止调高 |
| `db_pool_acquire_timeout_ms` | 500 | 连接获取上限；HTTP 超时映射 503 + Retry-After |
| `db_lock_timeout_ms` | 500 | 数据库锁等待上限 |
| `agent_db_statement_timeout_ms` | 1000 | Agent 关键 SQL statement timeout |
| `api_db_statement_timeout_ms` | 5000 | 交互 API SQL statement timeout |
| `worker_db_statement_timeout_ms` | 10000 | Worker 单语句上限；仍必须有界批处理 |
| `worker_db_batch_size` | 100 | Worker 单事务最大处理行数 |
| `worker_db_transaction_max_ms` | 500 | Worker 事务时长告警与主动中止门槛 |
| `device_offline_after_seconds` | 90 | 心跳离线判定 |
| `profile_missing_observation_threshold` | 3 | 连续 MISSING 后转 OFFLINE 的观察次数；观察随 30 秒心跳上报 |
| `inventory_full_sync_hour_local` | 3 | 每日全量同步当地小时 |
| `agent_buffer_warn_percent` | 80 | Agent 本地缓冲告警水位 |
| `agent_buffer_recovery_priority_percent` | 90 | 优先恢复摄取并暂停非关键工作的水位 |
| `agent_buffer_stop_work_percent` | 95 | 停止领取新 Work Unit 的水位 |
| `inbox_processed_raw_retention_days` | 7 | 仅适用于 safe_to_purge_at 已设置的 raw event 日期分区 |
| `inbox_partition_precreate_days` | 14 | safe_to_purge_at 日期分区提前创建窗口；缺失时 fail-closed |
| `source_event_ledger_hash_partitions` | 32 | 持久 revision/checksum/result ledger 的固定 HASH 分区数；修改需迁移和重验 |
| `command_idempotency_retention_days` | 180 | 可重放命令记录最短期限；不可由领域永久唯一键重建的记录 expires_at 必须为 NULL |
| `audit_hot_retention_days` | 90 | 主库审计热分区保留期 |
| `audit_online_retention_days` | 180 | 审计在线可查询最短期限；归档成功后才可删除 |
| `audit_partition_interval` | month | audit_events 声明式分区粒度 |

配置修改必须小步灰度并观察失败率、熔断率、DLQ、PUBLISHED_UNVERIFIED、CLOSED_UNVERIFIED 和资源水位。不得直接改库绕过版本化服务。标注“v3.3 不允许调高/调低”的安全边界只能通过新版 PRD 与重新验收修改。

---

## 17. 里程碑与发布门

### 17.1 M5a：基础设施与契约

范围：

- Central 全环境 PostgreSQL 16。
- FastAPI Central 服务边界和版本化 OpenAPI。
- fake-agent、Outbox/Inbox、同步 revision/ACK。
- `inbox_raw_events` 与持久 checkpoint/领域幂等分离，安全分区清理不改变重放结果。
- Redis Stream 控制事件、Agent SSE + HTTP 数据面。
- 四类数据库 Pool、部署级总连接预算、Worker 有界批处理和过载 503。
- 单实例调度、TOP-N/索引准备、500 Agent 基线压测。

退出门：

- 关键 API/事件 Schema 完整，无字段级未决项。
- PG fixture 和 CI 全绿。
- 500 fake-agent 稳态 60 分钟通过。
- 重复、乱序、断档和 Redis 故障测试通过。
- 第 18 章 F-30 通过；删除 raw Inbox 后同 revision 重放仍由持久 ledger/checkpoint 返回原结果或确定冲突。
- 第 18 章 P-09、R-10 通过；API/Worker/Web 事件饱和不能挤占 Agent Pool。

### 17.2 M5b：资源、身份与凭据

范围：

- 设备注册密钥、短期 Session Token、吊销和轮换。
- Profile snapshot/delta、账号绑定、登录、身份审核。
- 凭据信封加密和一次性 grant。
- QUARANTINED、清洗 fencing、WAITING_WINDOW/CAPACITY、DEBUG/EXTERNAL_BUSY。
- 设备级唯一 AdsPowerGateway、优先队列、限流、熔断和 Agent 控制面隔舱。

退出门：

- 旧清洗任务不能清除新绑定账号。
- grant 过期、重放、错设备、错 recovery_epoch/generation 全部拒绝。
- 身份不一致账号不能进入业务候选池。
- 第 18 章 P-07、P-08 通过；不存在绕过 AdsPowerGateway 的直接 Local API 调用。
- M5 仍仅运营方内部访问。

### 17.3 M5c：Campaign 闭环

范围：

- 策略注册表、不可变内容寻址 config snapshot、Agent 加密缓存、CampaignExecutor、Comment Campaign 策略化。
- 单机评论树、同机换号、Attempt 副作用阶段机。
- 节点验证终态及时释放账号并按剩余节点缩减硬备用；禁止借调或抢占 RESERVED_SPARE。
- 父评论 60 分钟验证、只读重新校验。
- 人工处理中心、任务创建入口、完整看板。
- 不确定副作用的中性封存，以及初始单机过滤容量 fail-fast 诊断。
- 真实 AdsPower/TikTok 人工烟雾验收。

退出门：

- 50 节点树与 5 个账号异常场景完成。
- 点击成功但回执丢失不重复提交。
- Agent 在 SUBMITTING 崩溃后进入 PUBLISHED_UNVERIFIED。
- 第 18 章 F-23、F-24、F-26、F-27 通过；设备永久损坏时可在不伪造结果、不重放的前提下闭合 Task。
- 第 18 章 F-22、F-28、F-29、P-10、S-13 通过；缓存缺失/损坏不改变冻结策略，安全释放不造成树内复用、错误 deficit 或备用借调。
- 存量 comment_campaign/execution_v2 测试保持通过。

### 17.4 M6：外部客户发布

范围：

- Flask BFF 生产 WSGI 部署、HTTPS、反向代理。
- Customer、五类角色、Session/CSRF、完整客户隔离。
- Central 多实例、独立 Lease 表、配额、归档、监控、告警。
- Audit 声明式分区、Inbox 安全清理分区和持久幂等水位；分区删除后旧消息重放仍不重复应用。
- Redis 故障时 Web fallback 限流/缓存/隔舱，以及恢复证据 durable ACK/disposition/purge checkpoint。
- Agent 灰度升级和版本兼容。
- 100 Customer、500 Web Session、500 Agent 联合压测。

发布硬门：

- 第 18 章 M6 验收全部通过。
- Central/Agent 端口无公网暴露。
- 跨客户攻击测试 100% 拒绝。
- 备份恢复、密钥轮换、会话撤销、设备吊销演练通过。
- 第 18 章 R-08、R-09、P-11、R-11 通过。
- 有回滚方案、值班手册和已签字压测报告。

任一硬门失败，不得通过降低环境真实性或静默修改阈值放行。

### 17.5 v3.3 发布阻塞修订

RB-01~RB-05 继承 v3.2，RB-06~RB-09 是 v3.3 新增强制发布阻塞项。评论树安全释放扩展 RB-02 的容量语义，不允许以“browse 无副作用”为由借调硬备用：

| 阻塞 ID | 修订 | 最早退出门 | 必须通过的验收 |
|---|---|---|---|
| RB-01 | 永久设备故障下按未知结果封存 | M5c | F-23、F-26、F-27 |
| RB-02 | 单机评论树过滤容量 fail-fast、诊断、硬备用不可借调与安全递减释放 | M5c | F-22、F-24、F-29 |
| RB-03 | AdsPower Local API 设备级隔舱 | M5b | P-07、P-08 |
| RB-04 | Redis 故障 Web fallback 防雪崩 | M6 | R-08 |
| RB-05 | 旧 recovery_epoch durable ACK 与安全清理 | M6 | R-09 |
| RB-06 | API/Agent/Worker/Web 事件数据库 Pool 隔舱、全局连接预算和过载保护 | M5a | P-09、R-10 |
| RB-07 | 不可变内容寻址 config snapshot 与 Agent 校验缓存 | M5c | F-28、P-10、S-13 |
| RB-08 | raw Inbox 与持久 source ledger/checkpoint/领域幂等分离 | M5a | F-10、F-30 |
| RB-09 | Inbox/Audit 生产分区、规模化保留、归档与故障恢复 | M6 | P-11、R-11 |

M6 继承全部未关闭的 M5a/M5b/M5c 阻塞项。任何 RB 未有完整自动化证据和人工签字，不得发布对应里程碑，也不得以“低概率”“先监控”或调整测试负载豁免。

---

## 18. 验收矩阵

### 18.1 统一证据格式

每项验收记录：

```text
ID | 需求 | 里程碑 | 前置条件 | 数据集 | 环境/硬件 | 负载模型
| 故障注入 | 持续时间 | 测量点 | 阈值 | 通过条件 | 证据 | 责任人
```

正确性断言必须 100% 通过。工程指标失败即阻塞对应发布门，除非走正式变更审批并同步修订 PRD、OpenAPI、测试和容量报告。

### 18.2 功能与一致性

| ID | 里程碑 | 验收场景 | 通过条件 |
|---|---|---|---|
| F-01 | M5b | 账号导入、无窗口等待、资源到达后绑定 | 不超绑；重复导入幂等；最终 ACTIVE |
| F-02 | M5b | 实际 TikTok 身份与预期不符 | 进入 IDENTITY_REVIEW_REQUIRED；不进入候选池 |
| F-03 | M5b | 旧清洗任务在 Profile 已重绑后执行 | CAS 取消旧清洗；新账号数据不受影响 |
| F-04 | M5c | 50 节点评论树创建和执行 | 整树同一 device_id；树内账号不重复 |
| F-05 | M5c | 5 个节点账号异常且同机有余量 | 同机换号后完成；失败账号不再入选 |
| F-06 | M5c | 同机无替换账号 | `WAITING_CAPACITY_REPLACEMENT` 保持 15 分钟后转 DLQ，错误码 `NO_LOCAL_REPLACEMENT_ACCOUNT` |
| F-07 | M5c | 父评论验证 | 2/15/60 分钟节奏正确；成功后仅激活依赖子节点 |
| F-08 | M5c | 点击成功但结果回传丢失 | PUBLISHED_UNVERIFIED；无第二次提交 |
| F-09 | M5c | Agent 在 SUBMITTING 阶段崩溃 | WAL 恢复为不确定；禁止自动重派 |
| F-10 | M5a | 重复 Inbox event_id/source_revision | source checkpoint/领域唯一键使状态与副作用只应用一次，不依赖 raw Inbox 行永久存在 |
| F-11 | M5a | delta 乱序和 revision 缺口 | 拒绝乱序并要求全量同步 |
| F-12 | M5a | Lease 过期后旧结果晚到 | 409 stale fencing；新 Fencing 标识的状态不被覆盖 |
| F-13 | M5c | 看板结果汇总 | 页面与 Central DB 统计一致 |
| F-14 | M5c | 父节点分别进入 FAILED、DLQ、CANCELLED、PUBLISHED_UNVERIFIED，并将后者按 guard 封存为 CLOSED_UNVERIFIED | 依赖节点按第 10 章迁移；PUBLISHED_UNVERIFIED 期间保持阻塞，封存事务递归取消后代且最终无永久 WAITING_DEPENDENCY |
| F-15 | M5a | Lease 分别在 PREPARED、SUBMITTING、VERIFYING 到期 | 提交前安全重排；提交后原子转 PUBLISHED_UNVERIFIED 且不能创建新 Lease |
| F-16 | M5b | Cleanup 自动重试耗尽后人工重试、复核成功、废弃 | 状态分别闭合；非成功路径 Profile 保持 QUARANTINED |
| F-17 | M5c | 树执行中账号失效、容量不足、旧分配晚到 | bound_device_id/tree_generation 不变；旧代拒绝；不跨设备 |
| F-18 | M5c | 两种 missed_policy 与全部 SubTask 终态组合 | 迁移及聚合结果逐项符合第 9.1 节真值表 |
| F-19 | M5c | Comment Campaign 的绑定 Device 分别在副作用前、SUBMITTING 后永久故障 | 前者 Attempt=FAILED、活跃 SubTask=DLQ、Task=FAILED 并释放预留，不回 QUEUED；后者 MANUAL_REVIEW 且不重派；两者均不改 bound_device_id |
| F-20 | M5a/M6 | ACK、durable_ingest_ack、disposition 和 purge checkpoint 的不同组合 | 当前纪元第 1 天保留且满 7 天后仅普通 ACK 可清理；旧纪元缺任一安全条件均不清理，条件齐全才按连续水位清理 |
| F-21 | M5c | PUBLISHED_UNVERIFIED 自动重新验证，并注入旧 VERIFY_ONLY 结果晚到 | 仅创建 VERIFY_ONLY Lease；grant 与所有写动作拒绝；双重 Fencing/revision 不匹配返回 409；失败后仍不创建 EXECUTE Lease |
| F-22 | M5c | 两棵树竞争账号、连续换号并覆盖“同机可补充备用/无可补充账号”；先制造 spare_deficit，再令足够节点终态降低 target，最后令 Task 终态 | 活跃 account_id 不重复；可补充时恢复 spare_target，无候选时准确记录 deficit 且不跨机/借调；target 降低后按公式自动清零/降低 deficit；历史 RELEASED/INVALID 账号不重回原树；Task 终态后活跃行全部 RELEASED 且三个计数均为 0 |
| F-23 | M5c | Attempt 在 SUBMITTING 后设备毁损，禁用外部查询并用测试时钟越过 24 小时 | 仅 platform_admin 可 `close_as_unresolved`；操作后 5 分钟内 Attempt=INDETERMINATE、SubTask=CLOSED_UNVERIFIED、Task=COMPLETED_WITH_UNRESOLVED，依赖后代取消、预留释放、无新 EXECUTE Lease；客户结果只显示 UNKNOWN |
| F-24 | M5c | required_slots=60 的四组数据：基础总量 40；基础 80 但属性过滤后 40；过滤后全局 80 且两机各 40；锁前单机 60、并发预留后 59 | reason_code 依次且唯一为 GLOBAL_SHORTAGE、FILTER_FRAGMENTATION、SINGLE_DEVICE_FRAGMENTATION、RESERVATION_CONTENTION；全部返回 409 且业务行/Outbox 为 0，首命中排除计数可复算，内部查询字段完整；客户错误体始终不含设备/数量。另注入诊断写失败并断言 503、Task=0 和告警产生 |
| F-25 | M5c | CLOSED_UNVERIFIED 后补入确认已发布与确认未发布两类迟到证据 | 只追加 late_reconciliations；Task/Attempt 状态不回退，不创建 Lease，不修改原终态统计 |
| F-26 | M5c | 从 ONLINE/OFFLINE/DEBUG 设备执行 mark_permanent_failure，并覆盖 ASSIGNED、提交前、SUBMITTING Lease；重放相同 Key、stale revision 和冲突证据 | Device/Session/Lease/Task 原子按第 9.8 节收敛，Outbox/审计各一次；非评论任务安全节点可重排，Comment Campaign 严格按第 8.3 节不跨机，不确定节点只进 PUBLISHED_UNVERIFIED；相同 Key 幂等，stale/冲突为 409，PERMANENT_FAILURE 无出迁移 |
| F-27 | M5c | PERMANENT_FAILURE 后分别在封存前、封存后找回确定已发布/未发布证据 | 封存前仅 submit_deterministic_evidence 可把 PUBLISHED_UNVERIFIED 收敛为 SUCCEEDED/FAILED；封存后仅 late_reconciliation 追加解释且状态/统计不变；不可复验证据两条路径均拒绝，VERIFY_ONLY/EXECUTE 始终为 0 |
| F-28 | M5c | 发布 500 KiB 快照并覆盖首次 cache miss、重复 Work Unit、缓存字节篡改、未知 schema、超过 512 KiB 和携带 delta/base-chain 字段 | 完整制品仅首次下载；重复 Work Unit 只含 <=16 KiB Envelope；篡改/未知 schema/超限/delta 全部在打开浏览器和领取 grant 前拒绝；缓存 miss 后执行内容与 Central canonical bytes checksum 一致 |
| F-29 | M5c | 50 节点树初始 10 个 RESERVED_SPARE；依次令 10 个节点验证终态、1 个节点保持 PUBLISHED_UNVERIFIED，并并发创建普通/browse/另一棵树任务 | 10 个终态节点 IN_USE 释放且不重回原树；备用从 10 原子缩至 8，active_reserved_slots=48、spare_deficit=0；未终态节点不释放；其他任务只能使用已 RELEASED 账号，任何借用 active RESERVED_SPARE 的请求均拒绝；无跨设备或重复账号 |
| F-30 | M5a/M6 | 顺序事件、Receipt 和幂等命令处理完成并推进 ledger/checkpoint/领域唯一键后，删除 30 天前 raw Inbox 分区，再分别重放同 envelope_fingerprint、同 revision/同 payload 但修改 event_type 或 aggregate_id、以及同 Idempotency-Key 异 request fingerprint 的消息；同时保留 safe_to_purge_at=NULL 记录并注入 ledger 缺行 | 同 fingerprint 返回 ledger 保存的原 ACK/结果且业务应用次数仍为 1；完整信封/request fingerprint 不同返回 409；ledger 缺行返回 503 `IDEMPOTENCY_LEDGER_INCOMPLETE` 且不应用；NULL 记录未删除；正确性不查询已删除 raw 分区 |

### 18.3 安全与隔离

| ID | 里程碑 | 验收场景 | 通过条件 |
|---|---|---|---|
| S-01 | M6 | 客户 A 枚举客户 B 账号/任务/结果 | HTTP、批量导出和事件订阅全部拒绝 |
| S-02 | M6 | 浏览器伪造 customer_id/role | 忽略输入；使用服务端 Principal |
| S-03 | M6 | Session 撤销、密码修改、用户禁用 | 旧 Session 下一请求失效；活跃 WebSocket 在 60 秒内关闭 |
| S-04 | M6 | CSRF 攻击写接口 | 403；无业务写入 |
| S-05 | M5b | 注册密钥重复使用 | 第二次原子拒绝 |
| S-06 | M5b | 设备 Token 错设备、过期、已吊销 | 401/403；无数据泄漏 |
| S-07 | M5b | grant 过期、重放、错 Lease、错绑定 | 全部拒绝；审计完整 |
| S-08 | M5b | 密文替换和 AES-GCM tag 篡改 | 解密失败；不返回部分明文 |
| S-09 | M5b/M6 | 扫描日志、快照、队列、API、WS/SSE | 无明文密码、Cookie、Token、CDP 地址 |
| S-10 | M6 | 事件令牌过期、重放、错客户或尝试写操作 | 握手/请求拒绝；无跨客户事件和业务写入 |
| S-11 | M6 | 客户访问资源端点、任务详情、导出、事件和错误体 | 不可枚举或泄漏 device_id、profile_id、agent_id、window_ref、CDP 地址及内部容量 |
| S-12 | M6 | 构造平台角色带 customer_id、客户角色无/错 customer_id | 数据库 CHECK/事务拒绝；无非法 User 或 Session |
| S-13 | M5c/M6 | 用 Customer A 的 snapshot_id 配合 Customer B Lease 下载并直接向数据库注入跨 customer artifact/snapshot/Task 组合；扫描 Agent 缓存、注入秘密字段、压缩炸弹和 checksum 篡改 | API 跨客户返回 404 且无存在性泄漏；三组错误数据库写入均由复合 FK 拒绝；缓存按 customer 加密隔离；秘密在发布阶段拒绝；压缩比/大小/checksum 异常在浏览器和 grant 前拒绝并告警 |

### 18.4 性能与恢复

| ID | 里程碑 | 负载/故障 | 通过条件 |
|---|---|---|---|
| P-01 | M5a | 500 Agent 按 30 秒心跳，持续 60 分钟 | 心跳 P99 <50ms、5xx <0.1%；按 event_id/checksum 对账零丢失 |
| P-02 | M5a | 15 万 QUEUED SubTask，连续 100 个调度 tick | 每个 tick <1s；无重复认领，队列计数完全一致 |
| P-03 | M5a | 100 RPS 列表/详情读取 + 20 RPS 创建/续租，持续 15 分钟 | Central 热路径 P99 <100ms、5xx <0.1%，数据库连接池峰值 <80% |
| P-04 | M5c | 500 账号批任务与 50 节点评论树各创建 100 次 | 接受响应 P99 <=500ms、5xx=0；持久化计划完整率 100% |
| P-05 | M6 | 500 WebSocket 连接、全局 100 事件/秒，持续 30 分钟 | 事件到页面 P95 <=1s、丢失率=0；单连接待发队列 <=1000，超限返回 resync_required |
| P-06 | M5a | 1000 次批准唤醒，500 Agent 在线 | 批准到 Agent 权威 HTTP 拉取 P95 <1s、P99 <2s；重复执行=0 |
| P-07 | M5b | 单台 Agent 同时注入 10 个 Cleanup、3 个窗口启动和 1 个全量同步，持续 15 分钟 | AdsPower Local API 实测并发始终 <=1；Agent 误离线=0、Lease 续租超时=0、关键请求 queue_wait P95 <=5s；Cleanup 最终均获得执行机会 |
| P-08 | M5b | 同一 Device 启动两个 Agent 进程，检测所有 50325 出站调用；随后填满 100 槽队列、注入 P0/P2 排队超时、连续 5 次依赖失败并恢复 | 仅一进程取得 owner，另一进程 API 调用=0 且退出执行数据面；Gateway 外直连=0、相邻调用间隔 >=1s；第 101 项拒绝并停止新 Work Unit但 Device 不 OFFLINE；P0 在 10s、P2 在 60s 产生不同 queue_timeout 分类；熔断保持 30s、半开并发探测=1、恢复后正常关闭 |
| P-09 | M5a/M6 | 使用默认 B=120、比例 30/35/20/15、每类 3 个 slot，运行 500 Agent 心跳/拉取、100 RPS 查询、20 RPS 创建/续租、500 Web 事件握手及回收/依赖/聚合 Worker 60 分钟，并覆盖 B=64 的余数分配 | 默认 class/slot 上限精确为 36/42/24/18 和 12/14/8/6；B=64 按第 4.5 节唯一算法复算且无歧义；报告 config revision、PG max、比例、slot/surge、期望与实测上限；跨实例连接不超 B、PG 总使用 <80%、借池=0；Agent timeout=0/心跳 P99 <50ms；Worker 每批 <=100、事务 P99 <500ms；过载通用 500=0 |
| P-10 | M5c | 1500 个 Work Unit 复用 10 个 100-500 KiB snapshot，先冷缓存再热缓存运行 30 分钟 | 热缓存命中率 >=95%；每 Agent/checksum 完整下载并发最多 1；热路径出口字节较每次内联基线下降 >=90%；Work Unit 拉取 P99 <100ms；Central 热路径不重复序列化完整 snapshot |
| P-11 | M6 | 3000 万条可清理 Inbox raw event、3000 万条持久 source ledger、100 万条不可清理 default 行和 3000 万条 Audit，持续 100 RPS submit_result 并执行旧分区 DETACH/DROP | submit_result P99 <100ms；ledger 幂等判定 P99 <10ms；分区维护不阻塞热写超过 1s；default/ledger 行零误删；Audit 180 天查询结果完整，归档 checksum 可复验 |
| R-01 | M5a | Redis 中断 10 分钟后恢复，期间保持 P-01 负载 | Agent 全程可 Pull；恢复后 5 分钟内 checkpoint 追平，event_id/checksum 对账零丢失 |
| R-02 | M5a | 随机 50 Agent 断网 15 分钟并各重启一次、注入 ACK 丢失 | 重连后 5 分钟内补报完成；未 ACK 数据零丢失、重复应用=0 |
| R-03 | M6 | 相同数据集和 15 分钟负载下，单实例与双实例并发认领对比 | 无重复分配；双实例有效吞吐 >=单实例基线 90%，P99 不劣化超过 20% |
| R-04 | M6 | PostgreSQL PITR 备份恢复演练 | RPO <=5 分钟；从恢复命令到新 Web 登录、关键只读查询和恢复摄取接口可用均 <=30 分钟；恢复点前审计链完整可查询 |
| R-05 | M6 | 500 Session、100 个 RUNNING Task 下 Flask/Central 各滚动升级一轮 | Session 非预期失效率=0；30 分钟内 Task/Attempt 数量与 event_id/checksum 对账一致，重复副作用=0 |
| R-06 | M6 | 500 Agent 同时断开并重连 | 95% 在 120 秒内恢复心跳和权威拉取；无未 ACK 数据丢失、无重复副作用 |
| R-07 | M6 | 在 100 个 RUNNING Task 中执行 PostgreSQL PITR | 新 recovery_epoch/web_session_epoch 前保持调度冻结；旧 Web/Device Session 与 Lease 100% 拒绝；60 分钟内完成在线对账或离线隔离后解冻，重复副作用=0 |
| R-08 | M6 | 500 Web Session 下 Redis 不可用 10 分钟，期间保持 P-01 Agent 负载、切换 fallback budget revision 并在 DRAINING 杀死协调 Worker，随后恢复 Redis | 30 秒 owner lease 后健康 Worker 接管且 120 秒内完成切换；全程 fallback 总 QPS <=50、新旧 slot/bucket 重叠=0，DRAINING 仅受控 429；DB 池 <80%，Agent 心跳/续租/Receipt P95 劣化 <=10% 且超时=0；恢复握手 <=25/s |
| R-09 | M6 | Agent 旧 recovery_epoch 未 ACK 证据占磁盘 96%，执行 PITR、重复/跨设备上传并用测试时钟越过 7 天 | 每条合法证据取得 durable ACK 与唯一 disposition，连续签名 checkpoint 的 issued_at/key_id/signature 可复验；可清理后本地占用降至 <80%，原始证据仍可在 Central 查询，重复业务应用=0，RETRYABLE_REJECTED/缺口数据删除=0；跨设备 source_device_id/source_db_uuid 100% 返回 403 且不入库 |
| R-10 | M5a/M6 | 分别耗尽 API、Worker 和 Web Event Pool，让 Worker 在批处理中崩溃；另保持旧进程存活但阻断 slot 续租，待新 owner 接管后恢复旧进程网络；全过程保持 P-01 Agent 负载 | 旧实例退出 Ready并 dispose；新 generation 先 DRAINING，旧 application_name backend 在 10 秒内自然退出或被终止且确认归零后才 ACTIVE；恢复的旧进程无法重建连接；新旧物理连接不重叠且总连接不超 B；非 Agent HTTP 只出现受控 503、Agent timeout=0/P95 劣化 <=10%；Worker 无重复迁移或连接泄漏 |
| R-11 | M6 | 在“raw Inbox 已写/业务未提交”“业务+ledger+checkpoint 同事务提交”“safe_to_purge_at 路由分区”“Audit 归档后 DETACH”四个边界逐一杀进程并重放 30 天前事件 | 未提交边界安全重试；已提交边界从 ledger 返回幂等结果；同 revision 异 envelope_fingerprint 始终 409；可清理 raw 分区删除后重复业务应用=0；unsafe/default/ledger 行零误删；Audit 仅在归档 checksum 成功后删除且查询链完整 |

### 18.5 兼容与真实环境

| ID | 里程碑 | 验收 | 通过条件 |
|---|---|---|---|
| C-01 | M5c | Windows 10/11、AdsPower Local API v5+ | link/strategy 两级烟雾通过 |
| C-02 | M5c | 现有 execution_v2/comment_campaign 回归 | 全量既有测试通过 |
| C-03 | M6 | Agent N/N-1 版本兼容 | 灰度期间协议兼容；不支持版本明确拒绝 |
| C-04 | M6 | Flask 旧管理 URL | 兼容矩阵内 URL 可用或有明确重定向 |

---

## 19. 监控与告警

### 19.1 指标

- Central API P50/P95/P99、错误率；四类 Pool 分别统计 config_revision、class/slot budget、slot_id/owner/generation/lease、checked_out/limit、acquire wait/timeout、transaction duration、statement/lock timeout、idle-in-transaction 和跨实例总连接数。
- 调度 tick、Lease 回收、认领冲突、队列深度。
- Agent 在线率、Session Token 失败、SSE 重连、版本分布。
- Profile AVAILABLE/BOUND/BUSY/QUARANTINED/EXTERNAL_BUSY。
- WAITING_WINDOW、WAITING_CAPACITY_REPLACEMENT、DLQ、PUBLISHED_UNVERIFIED、CLOSED_UNVERIFIED、COMPLETED_WITH_UNRESOLVED。
- grant 创建、领取、过期、重放拒绝。
- 同步 checkpoint 落后、revision 缺口、durable_ingest_ack/disposition、purge checkpoint 滞后、Agent 缓冲水位。
- AdsPowerGateway 按优先级 queue_depth/queue_wait、实际并发、请求间隔、timeout、限流和 circuit_state。
- Web fallback QPS、ETag 304 率、BFF cache hit、single-flight 合并率、429、budget state/config_revision/活跃 slot、Agent 保留池水位和 WebSocket 恢复握手率。
- Comment Campaign 创建容量拒绝率、按 reason_code 分布、capacity_diagnostics 写入失败和内部诊断查询错误率。
- Comment Campaign 的 IN_USE/RESERVED_SPARE/RELEASED 数、active_reserved_slots、spare_target/spare_deficit、预留年龄、节点终态释放延迟、借调拒绝次数和账号利用率。
- config snapshot 的发布大小/压缩比、Agent cache hit/miss/eviction、每 checksum 下载次数、single-flight 合并率、下载出口字节、序列化 CPU、checksum/schema/越权拒绝。
- Inbox default partition 行数/年龄、safe_to_purge 路由延迟、日期分区大小与 DROP 滞后、source ledger 分区大小/缺行、checkpoint 推进、envelope fingerprint/领域唯一冲突、旧事件重放拒绝；Audit 分区大小、归档/校验/DETACH 状态。
- 每 Customer 配额、任务成功率、失败率和结果延迟。

### 19.2 告警

| 告警 | 条件 | 接收 |
|---|---|---|
| 设备离线 | 90 秒无心跳；持续 10 分钟升级 | 看板 + Webhook |
| Agent 缓冲高水位 | >=80%；>=95% 严重 | 看板 + 运维告警 |
| DLQ 增长 | 10 分钟增加 >=10 | 看板 + Webhook |
| PUBLISHED_UNVERIFIED 增长 | 10 分钟增加 >=5 | 运营告警 |
| 可封存未知项超时 | 达到 unresolved deadline 后 1 小时仍未处理 | 平台管理员告警 |
| 容量诊断不可用 | capacity_diagnostics 写入失败 >=1，或 409 无对应诊断 | 高优先级运维告警 |
| 清洗积压 | QUARANTINED/RETRY_WAIT 持续增长 | 运维告警 |
| AdsPowerGateway 拥塞 | P0/P1 queue_wait P95 >5s 持续 5 分钟，或队列 >=80% | 运维告警；停止新 Work Unit |
| 同步过期 | 26 小时无成功全量对账 | 运维告警 |
| 恢复水位停滞 | 旧纪元 disposition/purge checkpoint 15 分钟无推进且缓冲 >=90% | 严重运维告警 |
| Web fallback 超预算 | Central fallback >50 QPS、新旧 config_revision slot 同时放行，或 Agent 保留池被 Web 使用 | 发布阻塞级告警 |
| Web fallback 切换停滞 | budget_state=DRAINING 持续 >120 秒或 transition owner 反复失租 | 严重运维告警；保持 fail-closed |
| 数据库 Pool 隔舱失效 | 任一 Pool 借用其他预算、重复/失租 slot 仍接流量、新旧 revision 重叠、跨实例总连接超 B、或 Agent acquire timeout >=1 | 发布阻塞级告警；相关实例退出 Ready，停止非关键 Worker/新 Web 握手 |
| Worker 长事务 | transaction P99 >500ms 或单批 >worker_db_batch_size 持续 5 分钟 | 运维告警；缩小批次并暂停对应 Worker |
| Snapshot 完整性异常 | checksum/schema/压缩比失败 >=1，或同 Agent/checksum 并发下载 >1 | 安全告警；阻止对应 Work Unit 执行 |
| 评论树预留泄漏/借调 | 节点终态后 60 秒仍 IN_USE、spare_deficit >0 持续 5 分钟、或 active 备用被其他任务使用 >=1 | 发布阻塞级告警；借调时冻结该树新执行，单纯 deficit 保持同机策略并告警 |
| Inbox 清理停滞 | safe_to_purge 已过期分区 24 小时未 DROP，或 default partition 最老记录 >7 天持续增长 | 运维告警；不得绕过 checkpoint 强删 |
| 幂等 Ledger 不完整 | revision <= checkpoint 但 source_event_ledger 缺行 >=1 | 发布阻塞级数据完整性告警；对应来源 fail-closed 503，禁止重放应用 |
| Audit 归档失败 | 满 180 天分区未归档、checksum 失败或归档前发生 DETACH | 安全/合规告警；禁止删除分区 |
| Central API | P99 >500ms 持续 5 分钟 | 运维告警 |
| 调度 tick | >1s 持续 5 分钟 | 运维告警 |
| grant 异常 | 重放/错设备骤增 | 安全告警 |
| 跨客户拒绝骤增 | 10 分钟超过基线阈值 | 安全告警 |

阈值修改必须版本化并审计；不得用调高阈值替代根因修复。

---

## 20. 演进条件与风险

### 20.1 主要风险

| 风险 | 对策 |
|---|---|
| Flask BFF 继续承载业务状态 | 依赖方向测试；状态只在 Central 应用服务写入 |
| 单机评论树属性过滤后容量不足或分布碎片 | 创建事务 fail-fast、确定性内部诊断、客户侧隐藏设备信息；运行期替换才有界等待 |
| 外部副作用重复 | WAL、Attempt 阶段、side_effect_started、禁止提交后重放 |
| 原设备毁损导致不确定副作用永久阻塞 | 24 小时 guard + platform_admin 中性封存；UNKNOWN 终态、永久禁止重放、迟到证据只追加 |
| 延迟清洗误伤新绑定 | binding_revision + recovery_epoch + generation fencing |
| AdsPower Local API 被并发清洗拖死 | 每设备唯一 Gateway、并发 1、优先队列、公平性、超时/熔断；心跳与续租独立 |
| 本地/Central 双向覆盖 | 字段级权威、单调 revision、冲突隔离、ACK |
| Redis 故障触发 Web 轮询雪崩 | 15-20 秒抖动轮询、ETag、3 秒缓存、single-flight、Central 总预算 50 QPS、Web/Agent 隔舱 |
| API、Worker 与 Web 事件竞争耗尽数据库连接 | 四类独立 Engine/Pool、跨实例总预算、Agent 30% 硬保留、Worker 有界批次/短事务、过载 503 |
| 完整 config_snapshot 重复下发拖垮出口和序列化 | 不可变内容寻址制品、<=16 KiB Envelope、Agent 加密 LRU/single-flight、ETag/gzip/checksum；禁止 delta merge |
| 评论树长等待造成账号高预留低利用 | 硬备用不可借调；节点验证终态释放 IN_USE，并按剩余非终态节点原子缩减备用；M5/M6 不做抢占/parking |
| Inbox/Audit 膨胀拖慢热写或固定 7 天删除破坏去重 | raw Inbox/Audit 声明式分区；raw 与 checkpoint/领域唯一键分离；仅 safe_to_purge 分区可删，default fail-closed |
| PITR 后旧纪元未 ACK 数据无法清理 | durable ACK + disposition + 签名连续 purge checkpoint；满 7 天后安全清理，Central 保留证据 |
| 500 Agent 重连风暴 | 指数退避、抖动、429 + Retry-After |
| 客户越权 | 服务端 Principal、默认过滤、404 防枚举、矩阵测试 |
| M5 过大 | 拆分 M5a/M5b/M5c，每阶段独立退出门 |

### 20.2 NATS 升级触发

满足任一条件时另立 ADR 评估 NATS：

- Agent 超过 2,000 台。
- HTTP/SSE 在真实压测下无法稳定满足批准到拉取 P95 <1s。
- 多个独立消费者需要长期订阅同一实时事件流。

升级必须保持 HTTP 权威数据面、Outbox/Inbox 和滚动兼容；不得同时维护两套业务语义。

### 20.3 无阻塞开放问题

本版本无阻塞产品或架构问题。SSO、NATS、跨设备评论树、评论树容量 parking/抢占借调均属于明确非目标或后续独立决策，不得在 M5/M6 实现中自行启用。

---

## 21. 被替代决策映射

| 旧结论 | v3.3 结论 |
|---|---|
| 外部 SaaS 使用 X-Tenant-ID 过渡 | 外部身份由 Flask Session 派生 customer_id；不信任浏览器 Header |
| 设备/Profile 按租户隔离 | 设备/Profile/Agent 是运营方全局资源；客户不可见 |
| 评论树优先单机、必要时跨机 | 评论树严格单机，任何阶段不跨机 |
| Central 开发可用 SQLite | Central 所有环境只用 PostgreSQL 16 |
| 父评论默认等待 30 分钟 | 默认 60 分钟，2/15 分钟分段验证 |
| generic retryable 可重试 3 次 | 总 Attempt 最多 3 次；SUBMITTING 后禁止重放 |
| Inbox 去重等同动作幂等 | raw Inbox 只保存消费信封；顺序事件由 source checkpoint、命令由幂等记录、外部动作由领域唯一键/Fencing 与 Attempt/WAL 共同保护 |
| 清洗延迟执行即可 | 清洗必须绑定 binding_revision + recovery_epoch + generation 并逐步 CAS |
| Redis 不可用降级进程内 Memory 事件流 | 多实例禁止 Memory 可恢复降级；改为 HTTP 补拉/快照 |
| Flask 与 Central 都可承载控制职责 | Flask 仅 Web/BFF；FastAPI Central 是控制与生产状态权威 |
| M5 同时完成全部规模化能力 | M5 拆为 M5a/M5b/M5c；M6 才开放外部客户 |
| PUBLISHED_UNVERIFIED 永远只能等验证，不提供终态 | 安全验证永久不可用时可按 UNKNOWN 中性封存；不得强制成功/失败或重放 |
| Comment Campaign 容量不足可进入 WAITING_CAPACITY | 初始单机过滤容量不足直接 409，输出内部确定性诊断且不创建 Task |
| 清洗延迟和低优先级足以保护 AdsPower | 每设备唯一 AdsPowerGateway 统一并发 1、限速、优先级、公平、熔断和隔舱 |
| Redis 故障后由浏览器指数退避轮询即可 | BFF 最小 15 秒抖动、ETag/cache/single-flight、Central 50 QPS 总预算和 Agent 保留容量 |
| 旧纪元数据因普通接口拒绝而永久未 ACK | 恢复专用 durable ACK、disposition 和签名连续 purge checkpoint 允许安全释放本地空间 |
| 为 Web fallback 保留 Agent 连接比例即可 | Agent/API/Worker/Web Event 使用四个独立 Engine/Pool；全部实例受同一数据库总预算约束，禁止借池 |
| Work Unit 每次携带完整 config_snapshot，或以 delta 合并节流 | Task 引用不可变内容寻址制品；Work Unit 只携带 checksum 元数据，Agent 校验缓存；M5/M6 禁止 delta/base-chain |
| RESERVED_SPARE 可借给 browse，需要时快速收回 | browse 也会改变账号状态且不可安全抢占；硬备用不可借调，改为节点终态释放和按剩余节点缩减 |
| Inbox 按 created_at 保留 7 天并靠消息唯一索引去重 | raw Inbox 可条件分区清理；永久幂等依赖 source checkpoint、领域唯一键/Fencing 和命令幂等记录，删除 raw 分区不改变结果 |

---

## 22. 变更记录

| 版本 | 日期 | 变更 |
|---|---|---|
| v3.3 | 2026-08-17 | 新增四类数据库 Pool 隔舱与全局预算、不可变内容寻址 config snapshot、评论树安全递减释放、Inbox/Audit 分区与持久幂等分离；新增 RB-06~RB-09 及对应接口、配置、监控和验收 |
| v3.2 | 2026-08-17 | 将五项隐患设为发布阻塞：不确定副作用中性封存、单机过滤容量诊断、AdsPower 设备级隔舱、Redis fallback 防雪崩、恢复纪元证据安全清理；补齐状态、接口、配置、监控和验收 |
| v3.1 | 2026-08-14 | 统一文档权威；采用 Flask BFF + FastAPI Central；tenant 语义改 customer；评论树严格单机；补齐数据权威、副作用阶段、清洗 fencing、Agent 身份、发布门和验收矩阵 |
