# Central 业务模块与 Agent 执行器统一架构设计

**日期**：2026-08-12  
**状态**：待用户书面复核  
**范围**：Central、Web 业务模块、Windows Agent、Execution V2、Comment Campaign、账号与 AdsPower 窗口资源  
**原则**：本规格只定义架构和后续实施边界，不修改现有业务代码；现有单机创建、执行和调试能力继续保留。

## 1. 背景与目标

当前系统已有三条相对独立的能力链：

- Browser Execution V2：本机创建和运行线性浏览器动作策略；
- Comment Campaign：本机创建评论树、分配 Profile、准备、审批、提交和验证；
- Business Control System：Web 创建通用任务，Central 调度，Windows Agent 拉取并执行。

现有 BCS Web 创建任务时只提交 `task_type`、自由 `params` 和固定 `strategy_version`，没有选择、发布或冻结真实的 Execution V2 策略。Agent 的 Execution V2 适配器却要求 `config_snapshot` 包含完整 `strategy` 和 `elements`。因此 Web 创建的任务目前不能可靠调用既有策略；Comment Campaign 也仍停留在本机 SQLite/RQ 工作流，尚未接入 Central/Agent。

本设计目标是：

1. Web 用户从具体业务模块创建任务，不手动选择内部任务类型；
2. Central 将不同业务封装为独立模块，并使用统一任务、资源分配和调度协议；
3. Agent 根据明确的执行器协议调用 Execution V2、Comment Campaign 或账号部署执行器；
4. 评论树优先在单设备完成，但允许按评论节点 DAG 跨设备调度，跨设备依赖只认 Central 已验证父 Receipt；
5. 每台设备的 AdsPower 窗口作为可调度资源，账号和窗口采用可变的历史绑定关系；
6. 用户导入账号时只填写登录资料，设备、窗口和实际 TikTok 身份均由系统获取；
7. 本机创建功能继续用于单机开发和调试，不影响 Web 生产任务；
8. 定义、资源、审批、租约、凭据和外部副作用均可审计、可冻结且可安全恢复。

## 2. 已批准的关键决策

### 2.1 Central 是生产定义的权威来源

采用统一的可执行定义目录。Central 保存生产定义及不可变 revision；Web 生产任务只引用 Central 已发布版本。Agent 只执行任务携带的冻结快照，不在执行时读取某台设备的本地策略数据库。

本机 Execution V2 和 Comment Campaign 编辑器继续保留。只有用户显式执行“发布到 Central”，并通过 Central 再校验后，才会创建生产 revision。本机后续编辑不会改变已创建或运行中的生产任务。

### 2.2 业务模块决定任务类型

Web 不提供普通用户可见的通用任务类型选择器：

| Web 模块 | 用户操作 | Central 内部任务 | 执行器 |
|---|---|---|---|
| 新增账号 | 导入账号登录资料 | `account_deployment` | `account_deployment/v1` |
| 评论任务 | 创建评论任务 | `comment_campaign` | `comment_campaign/v1` |
| 养号任务 | 创建养号任务 | `nurture_strategy` | `execution_strategy/v1` |

内部 `task_kind` 和 `executor_kind` 仍然存在，用于模块注册、调度、能力协商和 Agent 路由，但不要求用户填写，也不从自由业务参数中推断。

### 2.3 评论树按节点 DAG 调度，单设备方案优先

一棵评论树不超过 50 条评论。Central 首先寻找风险和容量均满足要求的单设备完整方案；找不到完整方案、需要分散出口风险，或设备故障需要恢复未产生副作用的节点时，允许把不同评论节点分配到不同设备。

评论节点是最小副作用单元，Central 可以把同一时刻已满足依赖、且适合同一设备的多个节点组成批次。跨设备子节点只有在 Central 已持久化并验证父节点 Receipt 后才会变为可调度。任何已经输入、点击或处于结果不确定状态的节点均不得迁移或重放。

设备不是唯一风险边界。调度还必须使用窗口实际的 `egress_group`、代理网段和风险标签限制同一视频的集中度，不能仅凭 `device_id` 判断网络隔离。100 台满足能力的在线设备可以并行承载至少约 100 棵常规评论树；实际吞吐由账号过滤、出口风险、审批速度和树的依赖形状共同决定。

### 2.4 设备和账号选择

- Central 默认自动选择满足能力、容量和负载条件的设备；
- Web 可以人工指定设备作为覆盖；
- 评论任务自动模式优先寻找单设备完整方案；单设备不可行或不满足风险分散规则时，使用同一全局匹配器生成跨设备节点方案；
- 手动模式可先指定一个或多个允许设备，再从这些设备的脱敏账号中选择；
- 用户可以固定部分节点的账号或设备，Central 只为未固定节点补齐资源；
- 无完整全树匹配时进入 `WAITING_CAPACITY`，不得只运行可分配的半棵树；
- 锁定时冻结每个节点的设备、窗口、账号绑定、出口组和相关 revision；
- 未输入、未点击的节点可依据定义中的迁移策略生成新 allocation revision 和 fencing generation；已经产生副作用的节点保持原分配与 Receipt，不得静默重放。

### 2.5 审批位置和策略

生产任务审批只在 Central Web 工作台完成。本机审批只服务本机调试任务，不能操作 Central 任务。

Comment Campaign 定义可以允许以下审批策略：

- `per_comment`：逐条人工审批；
- `batch`：全Campaign身份预检、文案、DAG、资源分配和不可变执行计划冻结后一次批准；子节点仍等待verified父Receipt，但不再逐条请求人工批准；
- `prepare_only`：只执行全Campaign预检和当前依赖可满足节点的只读准备，不输入、不点击；因父Receipt尚不存在而无法准备的子节点明确显示为依赖等待。

任务创建时冻结所选策略，Agent 无权改变或绕过。

### 2.6 账号、窗口和绑定是不同实体

- AdsPower 窗口是设备上的固定资源；
- 业务账号是用户导入的登录主体；
- 一个窗口当前可以运行一个账号，但账号失效、封禁或换号后可以释放并绑定其他账号；
- 绑定不是永久关系，必须版本化并保留历史；
- 实际 TikTok 身份由 Agent 在浏览器中读取，不由窗口名称或历史元数据推断。

### 2.7 登录和身份审核

导入批次支持 `automatic_first` 和 `manual_only`。自动登录遇到验证码、2FA、风控或页面歧义时进入 `WAITING_MANUAL_LOGIN`，不判定部署失败。

实际 TikTok 身份与导入预期不一致时进入 `IDENTITY_REVIEW_REQUIRED`。管理员可以接受实际身份并创建新的账号身份 revision，或拒绝并重新登录。确认前该账号绑定不能进入养号或评论任务候选池。

### 2.8 凭据方案

Central 使用应用层信封加密保存账号、密码和验证邮箱等登录资料。数据库只保存密文、加密数据密钥、密钥版本和凭据状态。Agent 在获得有效工作单元和设备租约后，通过一次性短期 grant 领取所需凭据。

明文凭据不得进入任务快照、队列、日志、Outbox、SSE/WebSocket、Attempt、Receipt 或公共错误。第一阶段即使用跨平台 AES-256-GCM 信封加密；主密钥通过环境变量、Docker Secret 或只读文件注入。业务密文不得依赖 Windows DPAPI，`KeyProvider` 接口允许以后迁移到 OpenBao/Vault，而无需重写业务数据模型。

## 3. 总体架构

```text
Web 业务模块
├── 账号管理
├── 评论任务
└── 养号任务
        │ 严格业务 API
        ▼
Central 业务模块层
├── AccountDeploymentModule
├── CommentCampaignModule
└── NurtureStrategyModule
        │ 标准 ResourceDemand / ExecutionPlan
        ▼
Central 通用任务与调度层
├── Definition Catalog
├── Task / WorkUnit
├── Resource Allocator
├── Scheduler / Lease / Fencing
├── Approval / Audit
└── Outbox / Inbox
        │ SSE 控制唤醒 + HTTP 数据传输/状态拉取
        ▼
Windows Agent
├── BrowserInventorySync
├── CredentialGrant Client
└── ExecutorRegistry
    ├── account_deployment/v1
    ├── execution_strategy/v1
    └── comment_campaign/v1
        │
        ▼
本机底层执行库
├── AdsPower Adapter
├── Playwright/CDP Session
├── Element Locator
├── Execution V2 Actions
├── TikTok Identity Reader
└── Comment Receipt Verifier
```

Central 调度器不理解评论树、点击步骤或登录页面。业务模块负责“做什么”和“需要什么资源”；调度器负责“在哪台设备执行”；Agent 执行器负责“如何操作本机浏览器”。

## 4. Central 业务模块协议

所有业务模块实现稳定接口：

```python
class BusinessTaskModule:
    task_kind: str

    def validate_request(self, request, context): ...
    def build_definition_snapshot(self, request, context): ...
    def calculate_resource_demand(self, snapshot, context): ...
    def compile_work_units(self, snapshot, allocation, context): ...
    def validate_agent_result(self, result, context): ...
    def handle_result(self, result, context): ...
```

注册关系：

```text
account_deployment → AccountDeploymentModule
comment_campaign   → CommentCampaignModule
nurture_strategy   → NurtureStrategyModule
```

业务模块输出标准资源需求：

```json
{
  "placement": "single_device_preferred_dag_fallback",
  "required_windows": 23,
  "required_capabilities": [
    "adspower",
    "playwright",
    "tiktok_identity_v1",
    "comment_campaign_v1"
  ],
  "account_filters": {
    "status": "ACTIVE",
    "tags_all": ["en"],
    "language": "en"
  },
  "allowed_device_ids": [],
  "egress_policy": {
    "max_nodes_per_group_per_video": 5,
    "distinct_groups_preferred": true
  }
}
```

调度器只读取标准需求，不解析业务请求中的自由字段。

## 5. 可执行定义和发布

### 5.1 定义类型

```text
ExecutableDefinition
├── execution_strategy
└── comment_campaign

SystemWorkflowDefinition
└── account_login
```

账号部署不要求用户选择策略，但仍由 Central 维护版本化的系统登录工作流。

### 5.2 数据结构

```text
executable_definitions
- id
- tenant_id
- kind
- name
- status                 # draft/published/disabled
- current_revision
- created_by
- created_at
- updated_at

executable_definition_revisions
- definition_id
- revision
- schema_version
- executor_kind
- definition_snapshot
- input_schema
- capability_requirements
- content_hash
- published_by
- published_at
```

约束：

- 已发布 revision 不可变；
- 编辑产生新 revision；
- 停用只阻止新任务，不改变已冻结任务；
- Central 发布接口必须重新校验内容，不信任本机 `valid` 标志；
- Central 保存自包含快照，不能只保存本地 strategy/template ID。

### 5.3 Execution V2 发布内容

- 目标 URL 规则；
- readiness；
- action 顺序和参数；
- action 引用的 element 定义；
- 内容来源约束；
- 动作幂等等级；
- 所需 Agent 能力和最低版本。

### 5.4 Comment Campaign 发布内容

- 评论树和父子关系；
- 文案及来源快照；
- 分配规则；
- 元素要求；
- 允许的审批策略；
- schema version 和内容 hash。

## 6. 设备与窗口库存

### 6.1 数据模型

```text
devices
- id
- tenant_id
- name
- status
- agent_version
- capability_snapshot
- current_session_id
- inventory_epoch
- last_heartbeat_at

device_sessions
- id
- device_id
- started_at
- last_heartbeat_at
- ended_at
- capability_snapshot

adspower_instances
- id
- device_id
- local_instance_ref
- status
- last_sync_at
- inventory_epoch

browser_windows
- id
- device_id
- adspower_instance_id
- window_ref
- display_name
- resource_status
- binding_status
- health_status
- egress_group
- risk_labels
- inventory_epoch
- first_seen_at
- last_seen_at
- missing_count
- revision

network_egress_groups
- id
- tenant_id
- display_name
- risk_status
- region
- provider_class
- revision
```

每台设备当前使用一个独立 AdsPower 账号/实例；独立实例表为以后更换或增加实例保留扩展点。

窗口状态：

```text
AVAILABLE → RESERVED → BOUND → BUSY
                  └────→ QUARANTINED
AVAILABLE/BOUND → MISSING → OFFLINE
```

Central 只保存 Agent 生成的稳定不透明 `window_ref`、脱敏信息和用于调度的出口风险组，不保存代理凭据、raw AdsPower Profile ID、Cookie 或 CDP/WebSocket 地址。`egress_group`表示实际共享出口或可被平台关联的代理风险域；若Agent不能可靠识别，则使用保守的设备级风险组，不能假设不同窗口天然隔离。

### 6.2 全量对账与实时增量库存

每台 Agent 按设备本地时区每日 03:00 执行一次全量对账，并使用稳定的 0–15 分钟抖动避免 100 台设备同时上报。Agent 启动、AdsPower 重连以及 Central 检测到增量序号断档时也执行全量对账。Web 可以对单台设备触发“立即同步”。账号花名册与浏览器接管页面必须调用同一个 `BrowserInventorySync`，不得维护两套同步实现。

```text
AdsPower Local API
→ Agent BrowserInventorySync
→ 规范化和脱敏
→ Central inventory epoch / event_seq CAS
→ Resource Event
→ 唤醒 WAITING_WINDOW / WAITING_CAPACITY
```

全量对账规则：

- `device_id + window_ref` 幂等合并；
- 同一设备只允许一个同步运行，重复请求共享或拒绝；
- 同步失败保留上次库存并标记 `STALE`；
- 本次未发现的旧窗口标记 `MISSING`，不立即删除；
- 连续多次缺失后转为 `OFFLINE`；
- 超过 26 小时未完成全量对账时标记全量盘点过期；这不是实时可用性的唯一依据；
- 已绑定窗口失踪时暂停相关任务，不自动迁移账号；
- 调度和绑定事务必须校验最新 `inventory_epoch`。

Agent 本地发生以下变化时，必须立即通过 HTTP 上报轻量 `inventory_delta_event`：

- 窗口启动、关闭、崩溃或关闭未确认；
- CAPTCHA、封禁、退出登录、人工解封或身份变化；
- 绑定创建、暂停、释放；
- AdsPower启动、CDP连接或Profile缓存故障；
- 本机调试占用或释放窗口。

每条增量包含 `device_session_id`、`inventory_epoch`、单调 `event_seq`、`window_ref`、`expected_revision`、新状态和固定原因码。Central 使用 Inbox 去重并执行 revision CAS；发现 `event_seq` 缺口时拒绝继续应用后续增量并请求全量对账。增量事件不得携带 Cookie、CDP地址、raw AdsPower ID或动态异常文本。

设备心跳、SSE控制连接或增量上报超过短时阈值失联后，Central 立即停止向该设备分配新资源，不等待 26 小时。调度提交前仍执行数据库资源CAS；Agent打开窗口前再执行本地最终状态校验。

Agent 上报接口：

```text
POST /api/central/agent/inventory/sync
POST /api/central/agent/inventory/deltas
```

控制面接口：

```text
POST /api/central/devices/{device_id}/inventory-sync
GET  /api/central/devices/{device_id}/windows
GET  /api/central/devices/{device_id}/inventory-status
```

Central 的“立即同步”只创建 Agent 命令，不直接访问远程设备的 AdsPower Local API。

## 7. 账号、凭据与窗口绑定

### 7.1 账号和凭据

```text
business_accounts
- id
- tenant_id
- imported_username
- expected_identity
- observed_identity
- identity_revision
- deployment_status
- business_status
- risk_status
- created_at
- updated_at

account_credentials
- account_id
- ciphertext
- encrypted_data_key
- key_version
- credential_revision
- status
- rotated_at
```

用户只导入账号名、登录账号、密码、验证邮箱、邮箱凭据和可选预期 TikTok 身份。用户不填写设备、窗口、AdsPower ID、浏览器版本或当前 TikTok 身份。

导入合法账号全部入库。当前有空闲窗口的立即进入部署流程；其余进入 `WAITING_WINDOW`。窗口释放、库存同步或新设备上线时自动唤醒。容量不足不会让整批导入失败。

### 7.2 动态绑定

```text
account_window_bindings
- id
- account_id
- window_id
- device_id
- status
- binding_revision
- observed_identity_revision
- bound_at
- verified_at
- released_at
- release_reason
```

绑定状态：

```text
RESERVED
→ LOGIN_PENDING
→ AUTO_LOGIN
→ WAITING_MANUAL_LOGIN
→ VERIFYING_IDENTITY
→ IDENTITY_REVIEW_REQUIRED
→ ACTIVE
→ SUSPENDED/RELEASING
→ RELEASED
```

约束：

- 一个窗口最多一个非终态绑定；
- 一个账号最多一个非终态绑定；
- `ACTIVE` 必须存在验证过的身份 revision；
- 释放不删除历史；
- `binding_revision` 参与任务锁定和 Agent CAS；
- 身份审核完成前不能进入业务任务候选池。

### 7.3 凭据 grant

```text
POST /api/central/agent/credential-grants
POST /api/central/agent/credential-grants/{grant_id}/redeem
```

领取必须校验设备 session、work unit、lease generation、账号窗口绑定、执行器类型、有效期和未使用状态。grant 一次使用、短时有效，只返回当前登录流程需要的字段，并写入审计事件。

## 8. 统一任务、工作单元与资源分配

### 8.1 任务模型

```text
tasks
- id
- tenant_id
- business_module
- definition_id
- definition_revision
- definition_snapshot
- business_input_snapshot
- approval_policy
- status
- priority
- schedule
- deadline
- created_by
- revision
- created_at

work_units
- id
- task_id
- executor_kind
- device_id
- device_session_id
- allocation_id
- lease_generation
- inventory_epoch
- status
- attempt
- deadline
- created_at
- updated_at
```

业务专属数据进入 `account_deployment_tasks`、`comment_campaign_tasks`、`nurture_tasks`，不继续扩大通用 `params` JSON。通用任务表只保存调度、冻结和审计所需的公共字段。

跨设备评论DAG使用专属节点执行记录：

```text
comment_node_executions
- id
- task_id
- node_id
- parent_node_id
- dependency_status
- allocation_id
- work_unit_id
- device_id
- account_id
- window_id
- egress_group
- allocation_revision
- lease_generation
- identity_generation
- status
- receipt_id
- revision
```

同一时刻满足依赖的多个节点可以被编译为同设备批次，但每个节点拥有独立副作用状态、Receipt和fencing字段。批次只是传输与开窗优化，不能把多个节点合并成不可区分的副作用事务。

### 8.2 分配模型

```text
resource_allocations
- id
- work_unit_id
- device_id
- inventory_epoch
- status
- revision

resource_allocation_items
- allocation_id
- comment_node_id
- window_id
- account_id
- binding_revision
- role_ref
- egress_group
- status

leases
- resource_type
- resource_id
- owner_work_unit_id
- generation
- expires_at
- heartbeat_at
```

调度事务必须原子校验：

- 设备在线且协议/能力兼容；
- 库存 epoch 最新；
- 窗口未被其他任务占用；
- 账号绑定为 `ACTIVE` 且 revision 未变；
- 评论任务所有节点均有满足账号属性、设备能力和出口风险约束的分配；
- 优先选择单设备完整方案；跨设备方案必须保持节点DAG、全Campaign身份唯一性和风险集中度限制；
- 子节点的父Receipt已经在Central持久化为verified，或该子节点仍保持依赖等待；
- 唯一约束和 CAS 允许本次写入。

重新规划使 fencing generation 单调增加。旧 Agent 即使恢复，也不能续租、写状态或提交结果。

### 8.3 WorkUnitEnvelope

```json
{
  "work_unit_id": "opaque",
  "task_id": "opaque",
  "task_kind": "comment_campaign",
  "executor_kind": "comment_campaign/v1",
  "device_id": "opaque",
  "lease_generation": 7,
  "inventory_epoch": 42,
  "definition_snapshot": {},
  "business_input": {},
  "resource_bindings": [],
  "approval_policy": "per_comment",
  "deadline": null
}
```

敏感凭据不进入该 envelope。

## 9. Agent 执行器协议

```text
ExecutorRegistry
├── account_deployment/v1 → AccountDeploymentExecutor
├── execution_strategy/v1 → ExecutionV2Executor
└── comment_campaign/v1   → CommentCampaignExecutor
```

Agent 收到工作单元后：

1. 校验设备、session、lease generation 和 inventory epoch；
2. 校验自身 Agent 版本、协议版本和能力；
3. 按 `executor_kind` 精确路由，不解析业务参数猜测执行器；
4. 执行冻结定义和已分配的本机资源；
5. 在阶段边界续租并上报固定事件；
6. 返回严格、脱敏的结果。

未知执行器或版本返回 `executor_not_supported`，不打开浏览器。

Agent 心跳至少包含：

```json
{
  "agent_version": "x.y.z",
  "protocol_versions": [
    "account_deployment/v1",
    "execution_strategy/v1",
    "comment_campaign/v1"
  ],
  "capabilities": [
    "inventory_sync_v1",
    "credential_grant_v1",
    "tiktok_identity_v1",
    "comment_receipt_v1"
  ],
  "max_windows": 100,
  "inventory_epoch": 42
}
```

## 10. 三条业务调用链

### 10.1 账号导入与部署

```text
Web账号模块
→ POST /api/central/accounts/import
→ AccountDeploymentModule逐行验证和加密
→ 创建BusinessAccount
→ 分配空闲窗口，或WAITING_WINDOW
→ account_deployment/v1工作单元
→ Agent一次性领取凭据
→ 自动登录或WAITING_MANUAL_LOGIN
→ 读取实际TikTok身份
→ 一致：ACTIVE
→ 不一致：IDENTITY_REVIEW_REQUIRED
```

### 10.2 评论任务

```text
Web评论模块
→ POST /api/central/comment-tasks
→ 选择已发布Comment Campaign revision
→ 冻结评论树、业务输入和审批策略
→ 自动选择设备集合或人工限定允许设备
→ 单设备优先，必要时按节点DAG生成跨设备全树方案
→ 无全树方案：WAITING_CAPACITY
→ 锁定各节点的设备、窗口、账号绑定、出口组和generation
→ 各设备执行comment_campaign/v1预检工作单元
→ Central汇总全Campaign身份预检并原子冻结
→ Central Web审批
→ Central仅释放依赖已满足的节点批次
→ Agent执行节点，回传Receipt
→ verified父Receipt激活跨设备子节点
→ 完成或进入安全暂停/人工核验
```

跨设备预检使用一次Campaign级 `preflight_round`：Central按设备创建只读预检工作单元，Agent只能打开已分配窗口、验证目标视频并读取实际TikTok身份，禁止打开评论输入、输入文案或点击提交。各Agent把观察写入本轮暂存区；只有当全部节点观察齐全，Central才在一个事务中校验窗口/绑定revision、账号唯一性、出口策略和preflight generation，并一次性冻结全Campaign identity generation。任一Agent失败、观察重复、资源漂移或本轮过期都会废弃整轮暂存，零审批、零可执行节点；不得把部分预检结果提升为生产证据。

### 10.3 养号任务

```text
Web养号模块
→ POST /api/central/nurture-tasks
→ 选择已发布Execution V2 revision
→ 冻结strategy/actions/elements/readiness
→ 按账号当前绑定设备拆分WorkUnit
→ execution_strategy/v1
→ Agent运行Execution V2动作
→ Central汇总设备和账号结果
```

## 11. Web 页面和 API 边界

普通用户使用以下业务入口：

```text
账号管理
├── 导入账号
├── 部署队列
├── 等待人工登录
└── 身份确认

评论任务
├── Campaign定义
├── 新建评论任务
├── 审批工作台
└── 任务历史

养号任务
├── 养号策略
├── 新建养号任务
├── 执行进度
└── 任务历史

设备与窗口
├── 设备列表
├── 窗口库存
├── 立即同步
└── 隔离资源
```

业务 API 分开设计：

```text
POST /api/central/accounts/import
POST /api/central/comment-tasks
POST /api/central/nurture-tasks
```

Central 内部建议按领域拆分为：

```text
central/api/accounts.py
central/api/account_deployments.py
central/api/comment_definitions.py
central/api/comment_tasks.py
central/api/comment_approvals.py
central/api/nurture_definitions.py
central/api/nurture_tasks.py
central/api/devices.py
central/api/inventory.py
central/api/agent_work.py
central/api/credentials.py
```

业务 API 调用 Application Service；不直接写调度器表。调度器不直接解析业务请求。

## 12. 状态机

### 12.1 账号部署

```text
IMPORTED
→ WAITING_WINDOW
→ WINDOW_RESERVED
→ AUTO_LOGIN
→ WAITING_MANUAL_LOGIN
→ VERIFYING_IDENTITY
→ IDENTITY_REVIEW_REQUIRED
→ ACTIVE
```

异常和释放：

```text
AUTO_LOGIN → RETRYABLE/LOGIN_FAILED
ACTIVE → SUSPENDED/ACCOUNT_INVALID
ACCOUNT_INVALID → RELEASING → RELEASED
```

### 12.2 评论任务

```text
DRAFT
→ WAITING_CAPACITY
→ PLANNED
→ LOCKED
→ PREFLIGHT
→ WAITING_APPROVAL
→ RUNNING
→ COMPLETED
```

安全暂停：

```text
PAUSED_IDENTITY
PAUSED_DEVICE
PAUSED_CLOSE_FAILURE
PAUSED_UNVERIFIED
CANCELLED
FAILED
```

Agent 可以复用现有 Assignment、Receipt、Attempt 和 identity generation 机制；Central 是生产控制面权威。点击后不确定必须进入 `published_unverified`，不得自动重试。

### 12.3 养号任务

```text
DRAFT → QUEUED → ASSIGNED → RUNNING → COMPLETED
```

单工作单元：

```text
SUCCESS
RETRYABLE_FAILED
MANUAL_REVIEW
CANCELLED
DLQ
```

策略必须声明 `read_only`、`repeatable` 或 `non_repeatable`。只有前两类允许自动重试；非幂等动作结果不确定时进入人工审核。

## 13. 审批协议

```text
approvals
- id
- task_id
- work_unit_id
- subject_type
- subject_id
- policy
- evidence_revision
- lease_generation
- identity_generation
- status
- decided_by
- decided_at
```

审批操作必须绑定：

- approval ID；
- expected revision；
- lease generation；
- identity generation。

身份、资源或租约代次变化会使相关未消费审批失效。Agent 只能上传证据和读取审批结果，不能自行创建批准或绕过 Central。

## 14. 故障、迁移与恢复

### 14.1 资源不足

- 账号无窗口：`WAITING_WINDOW`；
- 评论树在允许设备集合中无法形成满足账号属性、DAG和出口风险约束的全树方案：`WAITING_CAPACITY`；
- 养号工作单元对应设备离线：保持等待。

这些状态不是失败。库存更新、窗口释放或设备上线产生资源事件并唤醒调度。

### 14.2 分阶段迁移

- 未锁定资源：可以自动重新选择设备；
- 已锁定但尚未打开窗口、输入或点击：可以依据任务迁移策略安全重新规划；人工固定的设备/账号约束必须由管理员显式解除；
- 已部署账号：不自动迁移，必须重新部署并验证身份；
- 评论节点尚未输入或点击时，可以提升 allocation revision 和 fencing generation 后迁移到另一设备；其父依赖仍只认 Central 已验证 Receipt；
- 评论节点已经输入、点击或处于结果不确定状态时不得迁移或重放；同一树中其他尚未产生副作用的节点仍可独立重新规划；
- 养号任务已开始：当前单元不迁移，只能依据动作幂等等级决定是否创建新重试单元；
- 重新规划必须先递增 fencing generation，使旧 Agent 结果失效。

### 14.3 关闭和外部副作用

- 窗口关闭未确认时隔离资源并暂停相关任务；
- 已发生评论点击后任何异常都必须保留 Receipt 语义；
- `published_unverified` 不允许自动提交重放；
- 已验证父 Receipt 是子评论继续执行的唯一依赖凭据。

## 15. 安全、审计与数据边界

### 15.1 跨平台信封加密

第一阶段即使用 Python `cryptography` 的 AES-256-GCM 实现跨平台信封加密：

```text
版本化主密钥（KEK）
→ 加密每条凭据的随机数据密钥（DEK）
→ DEK加密账号凭据正文
```

每条记录保存 `ciphertext`、`nonce`、`encrypted_data_key`、`key_version`、`algorithm_version` 和必要的认证元数据。AES-GCM authentication tag 可以按库约定附加在ciphertext中或独立保存，但格式必须由`algorithm_version`固定。关联数据至少绑定租户ID、账号ID和凭据revision，防止密文跨账号替换。

主密钥通过环境变量、Docker Secret或权限受限的只读文件注入，不写入数据库、代码仓库或镜像。`KeyProvider`提供`get_encrypting_key()`和`get_key(version)`，使Central以后接入OpenBao/Vault时无需改变业务表。密钥轮换优先只重新封装DEK，不批量解密和重写凭据正文。业务密文从第一天起不得依赖Windows DPAPI，因此可以直接迁移到Linux、Docker和PostgreSQL。

### 15.2 禁止公开的数据

- 登录密码和邮箱凭据；
- raw AdsPower Profile ID；
- Cookie、Authorization、API key；
- CDP/WebSocket地址；
- DOM原文和动态异常文本。

上述信息不得进入 API 公共响应、任务快照、消息、日志、Attempt、Receipt 或 WebSocket。

### 15.3 审计对象

- 凭据创建、领取、更新和轮换；
- 定义发布、停用；
- 自动和人工资源分配；
- 人工登录完成；
- 身份不一致确认；
- 审批、拒绝和未验证处置；
- 重新规划、租约失效和设备迁移。

使用 `audit_events`、`outbox_messages`、`inbox_messages`、`task_attempts`、`task_receipts` 和 `evidence_records`，并保持租户隔离。

## 16. 通信模型

第一阶段采用“SSE控制唤醒 + HTTP权威数据面”，避免高频空轮询，也不把任务快照或凭据放入推送通道。

### 16.1 Agent SSE 控制信道

每个在线设备维持一条经设备身份认证的 SSE 连接。Central 只发送轻量唤醒事件：

```text
work_available
approval_decided
task_paused
task_cancelled
inventory_sync_requested
lease_revoked
full_inventory_requested
```

事件只包含 `event_id`、`event_type`、`device_id` 和安全聚合引用；不包含定义快照、证据、审批令牌、凭据或业务明文。Agent 收到事件后再通过 HTTP 拉取权威状态。

SSE 使用单调事件序号、`Last-Event-ID`、有界保留和断线续传。序号缺口或保留窗口外重连时，Central 发送 `resync_required`，Agent执行一次HTTP全量补拉。SSE心跳注释用于发现断线，不代替设备业务心跳和租约续约。

SSE连接绑定当前 `device_session_id` 和短期设备令牌；旧session连接必须被关闭。每设备只允许一个当前控制流。服务端对同设备的重复 `work_available` 等幂等唤醒进行合并，并设置有界发送队列；慢消费者超过上限时断开连接，由Agent携带`Last-Event-ID`重连，不能无限占用Central内存。

### 16.2 HTTP 数据面

```text
Agent heartbeat
Agent pull commands/work units after wake-up
Agent renew lease
Agent upload evidence/inventory delta
Agent submit events/results
Agent redeem one-time credential grant
```

证据、截图和其他大Payload只通过HTTP上传。SSE不可用时，Agent降级为带指数退避、随机抖动和服务器`Retry-After`提示的低频Pull，建议从5秒逐步退避到60秒；一旦收到工作或连接恢复，回到事件唤醒模式。禁止为逐条审批使用1秒级固定空轮询。SSE正常连接时不执行周期性任务/审批Pull，只在收到唤醒、启动补拉或状态对账时发起HTTP请求。

Outbox用于任务、审批、暂停、库存和租约事件的事务后发布；控制事件存入Redis Stream或等价的有界可重放事件存储。Inbox用于Agent增量、结果和回执幂等。Web运营端可以继续使用现有WebSocket事件流，Agent控制信道固定使用单向SSE。

## 17. 本机调试与 Central 生产隔离

| 属性 | 本机调试 | Central 生产 |
|---|---|---|
| authority | `local` | `central` |
| origin | `local_debug` | `central` |
| 状态库 | 本机模块库 | Central任务库 |
| 审批入口 | 本机页面 | Central Web工作台 |
| 定义来源 | 本机草稿 | Central已发布revision |
| 资源租约 | 本机租约 | Central fencing lease |
| 可否影响生产 | 否 | 是 |

两种模式只复用底层 AdsPower、Playwright、Locator、Action、Identity 和 Receipt 库，不共用任务状态、审批记录或资源所有权。本机页面不能批准、继续或修改 Central 任务。

### 17.1 设备级 LocalResourceArbiter

控制面隔离不足以避免本机调试与Central生产争用同一个AdsPower窗口。所有本机AdsPower调用路径必须先通过统一 `LocalResourceArbiter`：

```text
Central Agent / 本机Execution V2 / 本机Comment Campaign / 浏览器接管
                            ↓
                 LocalResourceArbiter
                            ↓
                  AdsPower Local API
```

Arbiter 使用跨进程OS排他锁，不得只使用单Python进程内互斥量。锁键是本机raw Profile ID的不可逆摘要；owner区分 `central:<work_unit_id>` 与 `local_debug:<job_id>`。锁从调用 `browser/start` 之前持有到窗口关闭已确认之后，覆盖启动、CDP、操作和关闭完整生命周期。

- 本机调试不能抢占Central生产锁；
- Central遇到本机调试锁时跳过或等待，不强杀调试进程；
- 进程异常退出后由OS释放排他锁；
- owner元数据只用于本机诊断，不能作为锁的唯一权威；
- 所有AdsPower Adapter必须接入Arbiter，禁止旁路调用；
- 用户直接在AdsPower UI打开的窗口标记为`EXTERNAL_BUSY`，由增量库存事件立即停止新分配。

## 18. 错误码和错误处理

稳定错误码至少包括：

```text
definition_not_published
definition_revision_unavailable
device_capability_unsatisfied
window_capacity_unsatisfied
inventory_stale
binding_revision_conflict
credential_grant_expired
manual_login_required
identity_review_required
lease_generation_stale
executor_not_supported
published_unverified
```

处理规则：

- 请求/权限/版本错误：4xx，不创建任务；
- 资源不足：进入等待状态；
- 可恢复执行错误：有界重试或重新排队；
- 外部副作用不确定：人工核验，禁止自动重试；
- 未知异常：固定 `internal_error`，不回显动态文本。

## 19. 观测与告警

主要指标：

```text
device_online_total
inventory_stale_total
window_available_total
account_waiting_window_total
task_waiting_capacity_total
lease_reclaim_total
manual_login_waiting_total
identity_review_total
published_unverified_total
credential_grant_failures_total
```

告警覆盖设备心跳/SSE失联、库存增量断档、全量盘点超过 26 小时、窗口不足、人工登录积压、身份不一致骤增、单一出口组集中度超限、关闭失败、未验证发布积压和凭据领取异常。

## 20. 测试与验收

### 20.1 单元测试

- 业务模块请求和定义校验；
- 定义发布、revision和快照冻结；
- 资源需求计算；
- 设备自动选择与人工指定；
- 同设备完整账号匹配；
- 窗口/账号唯一绑定；
- 信封加密和grant过期；
- 状态机、错误码和能力协商。

### 20.2 集成测试

- Web业务API到Task/WorkUnit；
- inventory epoch CAS；
- 多调度器并发分配；
- capability和协议版本匹配；
- Outbox/Inbox幂等；
- SSE事件序号、`Last-Event-ID`续传、序号缺口补拉和断线低频Pull降级；
- 审批完成后SSE即时唤醒，Agent再通过HTTP读取权威批准；
- inventory delta去重、乱序拒绝、revision CAS和缺口触发全量同步；
- lease generation fencing；
- 重新规划后旧Agent写入被拒绝；
- Central审批到Agent点击前门禁；
- 本机调试与生产控制面隔离；
- LocalResourceArbiter跨进程排他、进程退出释放和`EXTERNAL_BUSY`检测；
- AES-256-GCM认证失败、关联数据防替换、主密钥轮换和旧key解密。

### 20.3 规模验收

1. 100台Fake设备、每台100个窗口，同时创建100棵50节点评论树；存在单设备完整方案时优先单设备分配；
2. 构造账号属性碎片使任一单设备均不足，但多设备联合存在完整方案，任务必须按节点DAG成功规划而不是永久`WAITING_CAPACITY`；
3. 同一父节点的不同分支可以跨设备并行；子节点在父Receipt verified前绝不派发；
4. 链式树在第25节点设备离线后，已发布节点和Receipt保持不变，未输入/未点击节点提升generation后可安全迁移；
5. 风险策略限制同一`egress_group`对同一视频的节点数量，不能用不同device_id绕过；
6. 同一窗口并发分配只有一个事务成功；
7. 每日全量同步重复执行幂等；实时delta能立即使崩溃/封禁窗口停止新分配；
8. 增量序号缺口触发全量对账，同步漏窗口不删除历史绑定；
9. 登录身份不一致不能进入业务候选池；
10. `per_comment`批准经SSE唤醒后只产生一次HTTP权威拉取，不产生持续高频空查询；
11. 评论审批后身份变化使相关未执行节点暂停且零点击；
12. 点击后断线只产生`published_unverified`，不自动重试；
13. 设备租约失效后旧Agent结果被generation拒绝；
14. 本机调试持有窗口锁时生产任务不能启动同一Profile，反向亦然；
15. 本机调试任务不能读取、审批或改变Central任务；
16. 同一批凭据密文可在Windows与Linux测试进程中通过相同KeyProvider解密，无DPAPI依赖。
17. 100台Agent保持SSE在线且无任务时，任务/审批HTTP空Pull为零；批准事件到Agent发起权威HTTP读取的p95低于1秒（不含人工操作和大Payload上传）。
18. 慢SSE客户端触发有界断开，重连后通过`Last-Event-ID`或`resync_required`恢复，不能造成事件丢失或无界内存增长。

### 20.4 安全Tripwire

自动测试必须阻断真实 AdsPower、CDP/Playwright、TikTok、评论点击和正式数据库访问；同时递归扫描日志、快照、队列、API、WebSocket、Attempt 和 Receipt，确保无明文凭据或内部连接信息。

## 21. 分阶段实施顺序

### 阶段一：统一协议和生产定义目录

- 建立定义和revision目录；
- 建立业务模块注册表和Agent执行器注册表；
- 建立Agent SSE控制唤醒和HTTP权威数据面；
- Execution V2策略显式发布Central；
- 保留本机执行路径。

### 阶段二：设备窗口库存

- Agent统一BrowserInventorySync和inventory delta；
- 每日全量同步、启动/断档全量同步和Web立即同步；
- Central窗口资源池、epoch和健康状态；
- 建立LocalResourceArbiter并使所有AdsPower Adapter接入；
- 账号花名册与浏览器接管复用同一同步服务。

### 阶段三：账号导入与部署

- 跨平台AES-256-GCM信封加密、KeyProvider和一次性凭据grant；
- `WAITING_WINDOW`；
- 动态账号窗口绑定；
- 自动优先/人工登录；
- 身份不一致审核。

### 阶段四：Web养号任务

- `NurtureStrategyModule`；
- 按账号当前绑定设备拆分工作单元；
- Agent Execution V2适配；
- 幂等等级和安全重试。

### 阶段五：Web评论Campaign

- Comment Campaign定义发布；
- 单设备优先、节点DAG跨设备的全树分配；
- Agent Campaign执行器；
- Central审批工作台；
- Receipt、identity generation和恢复协议。

### 阶段六：规模与生产化

- PostgreSQL；
- 多Central实例并发调度；
- 完整Outbox relay；
- 指标、告警、备份和灾难恢复；
- 100设备/100评论树压力验收。

每个阶段完成后，现有单机创建和调试功能仍然可用，不要求一次性迁移。

## 22. 明确不做的事项

- 不把 Comment Campaign 强行编译成线性 Execution V2 actions；
- 不让调度器从自由 JSON 参数猜测任务模块；
- 不让生产任务引用设备本地策略 ID；
- 不把已经输入、点击或结果不确定的评论节点迁移到其他设备；
- 不允许本机页面审批或控制 Central 生产任务；
- 不自动迁移已登录账号到其他窗口；
- 不在任务消息中传递明文登录凭据；
- 不在第一阶段增加实时远程浏览器画面；
- 不在本设计阶段修改业务代码。

## 23. 成功标准

完成本设计后应达到：

1. 用户只在对应Web业务模块创建账号部署、评论或养号任务；
2. Central业务模块能把请求编译为冻结、可审计的工作单元；
3. 通用调度器无需理解具体动作即可选择设备和资源；
4. Agent能按`executor_kind`可靠调用不同执行器；
5. 评论树优先单设备运行；必要时按节点DAG跨设备执行，并严格通过Central verified父Receipt衔接；
6. 账号导入不要求设备或窗口参数，资源不足时自动等待；
7. 窗口库存每日全量对账、事件驱动增量更新并支持Web显式触发；
8. 账号和窗口可安全解绑、换号并保留历史；
9. 本机开发调试和Central生产控制互不影响；
10. 凭据、租约、审批和外部副作用满足安全边界。
