# 远程动作 WSS 执行体系设计

## 文档状态

- 日期：2026-08-20
- 状态：已完成业务方案确认，等待书面设计审核
- 范围：Web → 中控 → 本机 Agent 的远程动作下发、执行与结果上报
- 首批动作：浏览器策略、评论 Campaign

## 1. 背景

现有系统已经具备本机动作设计和执行能力，也存在 `central/`、`agent/`、Task/SubTask、设备心跳、租约、结果上报、Agent 拉取和 Execution V2 适配骨架。但当前链路仍存在以下关键缺口：

- 正式链路以 Agent 拉取任务为主，不符合中控主动下发的业务要求。
- 动作参数、动作版本和远程调用 ID 缺少统一契约。
- 设备身份目前依赖可伪造的环境变量，生产身份方案尚未确定。
- Agent Inbox、Outbox、WAL、周期续租和真实执行器未形成可靠闭环。
- 评论 Campaign 尚未接入远程执行框架。
- 结果上报失败可能丢失，副作用不确定时可能存在重复执行风险。
- 动作库不能退化为发布入口，必须继续承担本机动作设计和调试。

本设计在现有 Central、Agent、Execution V2 和 Comment Campaign 基础上扩展，不重写动作业务内核。

## 2. 目标

1. Web 创建并审批任务后，由中控拆分并通过 WSS 主动下发给指定 Agent。
2. 每个可远程调用的动作具有全局唯一且永久不复用的 `action_id`。
3. 动作以不可变 revision 发布到中控，远程任务携带完整冻结快照和动态参数。
4. 浏览器策略和评论 Campaign 是同级动作，通过统一执行器注册表接入。
5. Agent 可靠接收、幂等执行、处理取消和断线，并可靠上报结果。
6. 防止点击发布等外部副作用被盲目重放。
7. 保留本机动作设计、调试、版本维护和本地证据能力。
8. 为后续新增同级动作提供稳定扩展点，不修改 WSS 核心协议。

## 3. 非目标

- 本轮不确定设备首次注册和长期凭据生成方式。
- 本轮不上传原始截图、录像或 DOM 快照到中控。
- Agent 不处理运行时审批；只接收 Web 已创建并审批的任务。
- 本轮不引入 NATS、RabbitMQ 或其他消息代理。
- 本轮不接入浏览器策略和评论 Campaign 之外的动作。
- 普通 Web 客户不选择具体设备；设备固定只对运营管理员开放。

## 4. 总体架构

正式链路如下：

```text
Web 创建并审批任务
  → 中控解析已发布动作版本
  → 中控冻结动作快照和本次动态参数
  → 中控拆分 WorkOrder
  → 中控选择设备并预占账号/窗口
  → 中控持久化 WorkOrder、租约和下发命令
  → 中控通过 WSS 主动下发完整 WorkOrder
  → Agent 持久化 Inbox 后返回 RECEIVED
  → Agent 校验动作、能力和本机资源后返回 ACCEPTED
  → Agent 通过 Executor Registry 选择动作执行器
  → Agent 上报关键进度
  → Agent 将最终结果写入本地 Outbox
  → Agent 通过 HTTPS 幂等上报最终结果和证据清单
```

### 4.1 通道职责

| 通道 | 职责 |
|---|---|
| WSS | 中控主动下发 WorkOrder、取消命令；Agent 返回 ACK、轻量关键进度和终态 event 引用 |
| HTTPS | Agent 可靠上报完整最终结果、错误和证据清单；执行外部副作用前申请持久化 effect permit |
| 本地存储 | Agent Inbox、Outbox、执行检查点、资源锁和原始证据 |

Agent 不定期轮询任务。原 `pull_subtasks()` 不再是正式业务入口，只允许保留为兼容或人工排障能力。WSS 断线重连时通过序号和本机活动任务进行一次对账，不等同于主动轮询任务。

## 5. 标识体系

| 标识 | 作用 |
|---|---|
| `action_id` | 全局唯一标识一个动作定义 |
| `action_revision` | 标识动作的不可变发布版本 |
| `task_id` | Web 创建的一次业务任务 |
| `work_order_id` | 中控拆分后的一个工作单元 |
| `command_id` | 中控的一条幂等下发命令；重发保持不变 |
| `run_id` | Agent 本机的一次执行记录 |
| `event_id` | Agent 的一条幂等结果或事件记录 |
| `evidence_id` | Agent 本机保存的一份证据记录 |

### 5.1 Action ID 规则

- 使用 UUIDv7 或 ULID 生成带 `act_` 前缀的全局唯一 ID。
- 新建动作生成新 ID。
- 编辑和发布新 revision 保持原 ID。
- 复制动作生成新 ID。
- 删除动作只写 tombstone，ID 永久不复用。
- 同一个动作同步到中控或其他 Agent 时保持原 ID。
- “另存为新动作”生成新 ID。
- 同一 `action_id + revision` 在所有位置必须具有相同 `release_checksum`；冲突时拒绝同步。

`executor_kind` 只负责选择执行器，不能代替 `action_id`。不同类型动作也必须使用全局不冲突的 `action_id`。

### 5.2 Checksum 规范

为保证“调试过的草稿内容”与“发布 revision”可以独立验证，定义两个 checksum：

1. `content_checksum`，算法标识 `action-content-jcs-sha256-v1`。输入且只输入 `executor_kind`、`definition_schema_version`、`parameter_schema`、`result_schema`、`snapshot`、`execution_defaults`。它不包含 action ID 和 revision，草稿调试门禁使用此值。
2. `release_checksum`，算法标识 `action-release-jcs-sha256-v1`。输入且只输入 `action_id`、`revision`、`content_checksum`。发布、同步和远程调用使用此值。

两个算法都按 RFC 8785 JSON Canonicalization Scheme 生成 UTF-8 字节，再计算 SHA-256，输出 `sha256:` 加 64 位小写十六进制。名称、说明、作者、创建/发布时间、同步状态、调试记录、运行时参数和资源预占都不参与哈希。

发布端、中控和 Agent 必须共享同一组 canonical JSON golden vectors。未知 checksum 算法、非规范化数字或无法规范化的数据必须拒绝，不允许降级到普通 JSON 序列化。

## 6. 动作存储、调试与发布

### 6.1 本机存储边界

已创建动作继续保存在各动作模块自己的数据库中：

```text
浏览器策略数据库
  └── action_id + 草稿 + revision + 策略数据

评论 Campaign 数据库
  └── action_id + 草稿 + revision + Campaign 配置

动作库聚合索引
  └── action_id + action_kind + 名称 + 状态 + 调试和发布摘要
```

动作库是统一 UI 和聚合查询层，不成为第二份动作业务事实源。

### 6.2 动作生命周期

```text
DRAFT → DEBUG_PASSED → PUBLISHED
   ↑          │
   └─修改后测试状态失效
```

- 动作草稿必须至少成功完成一次本机调试。
- 调试记录必须对应当前草稿 `content_checksum`。
- 草稿修改后必须重新调试。
- 调试成功不会自动发布。
- 发布生成不可修改的新 revision，并同步到中控。
- 正常发布必须通过当前 `content_checksum` 的本机调试门禁。发布时分配 revision，并由 `action_id + revision + content_checksum` 生成 `release_checksum`。管理员强制发布是显式例外：允许绕过调试门禁，但 revision 必须标记 `validation_status=waived`，填写原因并记录审计事件；Web 和中控必须明显展示该状态。强制发布不能伪造调试成功记录。
- 发布请求必须携带可信入口注入的操作者身份与角色；payload 中的 actor 必须与调用上下文一致，只有 `administrator` 可以提交 `waived`。中控在同一事务内保存 actor、原因和独立审计事件。JWT 尚未接入前，该入口只允许受控环境使用。
- Browser Strategy、Comment Campaign 本机发布和中控接收必须调用同一内容限制器：snapshot 不超过 512 KiB、整体发布内容预留至少 64 KiB WorkOrder 信封空间、JSON 最大深度 32；任何一端不得接受另一端无法装入 WorkOrder 的 revision。
- 定期同步校验 `release_checksum`、发现缺失并补传已发布 revision，不发布草稿。

### 6.3 本机调试

动作库中的“调试”生成 `Local WorkOrder`：

- 不经过 WSS。
- 不需要 Web 任务或中控租约。
- 使用与远程任务相同的参数 Schema、Executor Registry、执行器和结果格式。
- 结果进入本机调试记录和本地回执证据。
- 与远程任务使用同一套账号/窗口资源锁。
- 本机调试占用通过连接状态上报中控，中控不得分配对应资源。
- 已被远程任务预占的资源不能用于本机调试。
- 远程任务不能强制终止本机调试。

## 7. WorkOrder 契约

所有动作共用统一信封，动作差异存在于 `definition.snapshot`、`runtime_params` 和结果 Schema 中。

```json
{
  "protocol_version": "1.0",
  "command_id": "cmd_01...",
  "task_id": "task_01...",
  "work_order_id": "wo_01...",
  "device_id": "dev_01...",
  "lease_generation": 3,
  "executor_kind": "comment_campaign",
  "definition": {
    "action_id": "act_01...",
    "revision": 7,
    "content_checksum": "sha256:...",
    "release_checksum": "sha256:...",
    "definition_schema_version": "1.0",
    "parameter_schema": {},
    "result_schema": {},
    "snapshot": {},
    "execution_defaults": {}
  },
  "runtime_params": {},
  "resource_reservations": [],
  "effect_plan": [],
  "execution_policy": {}
}
```

### 7.1 必需字段语义

- `protocol_version`：Agent 不支持时明确返回协议拒绝。
- `command_id`：同一命令重复下发时不得重复执行。
- `lease_generation`：旧代次不得开始新的副作用步骤或上报权威成功结果。
- `definition`：携带复算 content/release checksum 所需的全部字段，不依赖 Agent 本机已有定义。
- `runtime_params`：中控已经确定并冻结的本次输入，不允许 Agent再次随机选择。
- `resource_reservations`：中控预占的具体账号和窗口。
- `effect_plan`：本工作单元可能产生的外部副作用节点；每个节点具有稳定 `effect_id`、依赖和结果 Schema。
- `execution_policy`：截止时间、重试边界、取消规则和证据要求。

### 7.2 参数安全

每个动作 revision 必须声明输入 Schema 和允许绑定位置。Agent 只能将运行参数绑定到预先声明的位置，例如 `target_url` 或 `input_text`，不能使用任意 JSON merge 覆盖动作结构。

发布端和执行端必须实现自定义 JSON Schema format `https-url`：只接受无 userinfo、具有有效 DNS/IPv4/括号 IPv6 host、端口为 1–65535 的 HTTPS URL。未知该 format 的执行器不得忽略并继续执行。

### 7.3 WSS 消息 Schema

所有 WSS 消息共用信封：

```json
{
  "type": "WORK_ORDER_DELIVER",
  "protocol_version": "1.0",
  "message_id": "msg_01...",
  "device_id": "dev_01...",
  "session_id": "sess_01...",
  "server_sequence": 182,
  "sent_at": "2026-08-20T10:00:00Z",
  "payload": {}
}
```

上例是中控→Agent 信封。`server_sequence` 在该方向必需；Agent→中控信封字段相同但禁止出现 `server_sequence`。

V1 必须冻结以下消息类型和 payload：

| `type` | 方向 | 必需 payload |
|---|---|---|
| `WORK_ORDER_DELIVER` | 中控→Agent | 完整 WorkOrder |
| `COMMAND_ACK` | Agent→中控 | `command_id`、`work_order_id`、`ack_kind`、`run_id`、`persisted_at`；拒绝时附稳定 `rejection_code` |
| `WORK_ORDER_CANCEL` | 中控→Agent | 新的取消 `command_id`、目标 work order、目标 generation、原因码 |
| `PROGRESS_EVENT` | Agent→中控 | `event_id`、work order、generation、run、业务状态、stage、时间和安全摘要 |
| `TERMINAL_REFERENCE` | Agent→中控 | work order、generation、最终 status、已写 Outbox 的 `result_event_id` |
| `RECONCILE_REQUEST` | Agent→中控 | `last_server_sequence`、活动 work orders、待报 result event IDs |
| `RECONCILE_RESPONSE` | 中控→Agent | 缺失命令、有效租约、已确认 result event IDs、必须停止的旧 generation |

`COMMAND_ACK.ack_kind` 只允许 `RECEIVED`、`ACCEPTED`、`REJECTED` 和 `ALREADY_TERMINAL`。`ALREADY_TERMINAL` 只携带 `result_event_id`，完整 Outcome 始终通过 HTTPS 上报，禁止通过 WSS 返回完整最终结果。

`RECEIVED` 和 `ACCEPTED` 是 ACK 类型，同时分别触发中控业务状态进入 `RECEIVED` 和 `RUNNING` 前的 `VALIDATING`。`PROGRESS_EVENT` 是 `RUNNING`、`VERIFYING` 等观测状态的来源；ACK 本身不是独立 WorkOrder 终态。

`server_sequence` 只存在于中控→Agent 的持久化控制消息，由中控按 device 在跨 session 范围内单调递增，是重连补发的唯一顺序权威。Agent→中控消息禁止携带 `server_sequence`，使用 `message_id`、`event_id` 或 `command_id` 幂等；不维护第二条 client sequence。WorkOrder payload 内不重复保存 sequence。

### 7.4 Schema 规范约束

G0 必须产出版本化 Schema 文件：`wss-envelope-v1`、`work-order-v1`、`command-ack-v1`、`cancel-command-v1`、`progress-event-v1`、`terminal-reference-v1`、`reconcile-request-v1`、`reconcile-response-v1`、`effect-permit-v1` 和 `execution-outcome-v1`。

- 所有对象默认 `additionalProperties=false`；扩展字段只能进入显式 `extensions` 命名空间。
- 所有 ID 是 1–80 字符的 ASCII 前缀 ID；文本摘要最多 4 KiB。
- `server_sequence` 和 generation 是大于等于 1 的 64 位整数。
- 时间使用 UTC RFC 3339，精确到毫秒。
- 单条 WSS 消息最大 1 MiB；动作 snapshot 最大 512 KiB；runtime params 最大 256 KiB。
- 枚举未知值、超限深度、重复 JSON key、非有限数字和不符合 Schema 的消息必须 fail closed。
- 每个消息 Schema 必须明确 required、类型、长度、枚举和嵌套对象，不允许只依赖代码中的自由字典。

## 8. 命令下发与 ACK

1. 中控在数据库事务中保存 WorkOrder、资源预占、租约和命令。
2. 事务提交后，WSS 网关才发送 WorkOrder。
3. Agent 校验基础信封并持久化到本地 Inbox。
4. 持久化成功后返回 `RECEIVED`。
5. Agent 复算并校验动作 Schema、content/release checksum、执行器能力和本机资源。
6. 校验成功返回 `ACCEPTED` 并开始执行；失败返回稳定的 `REJECTED` 原因。

ACK 超时时，中控先向同一会话重发相同 `command_id`。Agent 对重复 `command_id` 返回已有 ACK；如果本机已经终态，则返回 `ALREADY_TERMINAL + result_event_id`，不返回完整 Outcome，也不创建新 run。

设备离线后不能立即把同一工作单元交给另一设备。中控必须等待或撤销旧租约、递增 `lease_generation`，再重新分配。

## 9. 状态机

### 9.1 中控与 Agent 主状态

```text
APPROVED
  → QUEUED
  → DISPATCHING
  → RECEIVED
  → VALIDATING
  → RUNNING
  → VERIFYING
  → SUCCEEDED
```

分支状态：

- `REJECTED`：协议、动作、能力或资源校验失败；中控可重新调度。
- `FAILED`：未处于不确定副作用状态的确定失败。
- `CANCELLED`：在安全检查点完成取消。
- `PARTIALLY_SUCCEEDED`：至少一个副作用已确认成功，但其余节点被确定失败、跳过或安全取消，且不存在不确定节点。
- `UNVERIFIED`：副作用可能已经发生，但无法确认最终结果；禁止自动重试。

Agent 不存在 `WAITING_APPROVAL`。任务必须在 Web 完成创建和审批后才允许下发。

### 9.2 取消规则

- 尚未执行：立即取消。
- 执行中且尚未产生外部副作用：运行到最近安全检查点后取消。
- 已经产生外部副作用：不能强杀；停止申请新的 effect permit，完成在途节点的只读验证，并按节点结果汇总为 `SUCCEEDED`、`PARTIALLY_SUCCEEDED` 或 `UNVERIFIED`。

### 9.3 多副作用节点

评论树和包含多个外部提交步骤的浏览器策略必须把每个副作用声明为独立 `effect_id`。本机 effect ledger 状态只允许：

```text
PENDING → PERMIT_REQUESTED → AUTHORIZED → SUBMITTING → CONFIRMED
                         │          │              └→ UNCERTAIN
                         │          └→ FAILED_PRE_EFFECT（关闭未使用 permit）
                         └→ PENDING / SKIPPED（permit 被拒绝）
PENDING → SKIPPED / FAILED_PRE_EFFECT
```

- `CONFIRMED` 节点永不重放。
- `UNCERTAIN` 节点只允许只读验证，禁止重新提交。
- `PENDING` 节点只有在租约有效且未取消时才能继续。
- 父子评论依赖以 `effect_id` 表达；父节点未 `CONFIRMED` 时，子节点不得申请 permit。

WorkOrder 终态按以下固定优先级汇总：

1. 任一节点 `UNCERTAIN` → `UNVERIFIED`。
2. 所有必需节点 `CONFIRMED` → `SUCCEEDED`。
3. 至少一个节点 `CONFIRMED`，其余节点均为确定终态但未全部成功 → `PARTIALLY_SUCCEEDED`。
4. 没有节点进入 `AUTHORIZED/SUBMITTING/CONFIRMED/UNCERTAIN` 且取消完成 → `CANCELLED`。
5. 没有已确认或不确定副作用，且出现不可恢复失败 → `FAILED`。

最终 Outcome 必须包含每个 `effect_id` 的状态和结果，不能只上报 WorkOrder 聚合状态。

### 9.4 Effect Permit 与 Lease Fencing

Agent 在每个外部副作用发生前，通过 HTTPS 使用当前 `work_order_id + lease_generation + effect_id` 申请一次性持久化 permit。中控在事务中确认：租约仍有效、节点未授权或完成、依赖已满足、任务未取消，然后返回 `permit_id`。

持久化顺序是强制协议：

1. Agent 在本地事务中写入 `PERMIT_REQUESTED + stable request_id`。
2. Agent 调用 permit API；中控创建或返回同一 permit。
3. Agent 把 `permit_id` 持久化为 `AUTHORIZED` 后，才允许准备外部调用。
4. Agent 在调用外部平台之前先持久化 `SUBMITTING`，再执行调用。
5. 调用完成后持久化 `CONFIRMED`；无法确定结果则持久化 `UNCERTAIN`。
6. 如果已授权但在写入 `SUBMITTING` 前确定失败，Agent 调用 close-unused API；中控将 permit 标记 `CLOSED_UNUSED`，本地节点进入 `FAILED_PRE_EFFECT`。

由于 `SUBMITTING` 必须先于外部调用落盘，重启后看到 `AUTHORIZED` 可以安全关闭未使用 permit；看到 `SUBMITTING` 则只能只读验证并进入 `CONFIRMED` 或 `UNCERTAIN`。

- 无法连接中控时不得开始新的外部副作用。
- 同一 `request_id` 重复申请返回同一 permit；同一 `effect_id` 同时最多有一个 `ISSUED` permit。
- permit `CLOSED_UNUSED` 后，原 Agent 可以在重试策略允许时使用新的 request ID 申请下一 `permit_attempt`；`CONFIRMED/UNCERTAIN` 后永远不得再签发。
- 中控 permit 状态只允许 `ISSUED`、`CLOSED_UNUSED`、`CONFIRMED` 和 `UNCERTAIN`。`ISSUED` 是未解决状态，不得递增 generation 或重新分配。
- 只有“从未签发 permit”或“所有已签发 permit 都是 `CLOSED_UNUSED`，且不存在 CONFIRMED/UNCERTAIN effect”时，才允许自动递增 generation 并重新分配。
- 一旦任一 effect `CONFIRMED` 或 `UNCERTAIN`，剩余节点只能由原 Agent 在原 generation 恢复，或按节点状态汇总为 `PARTIALLY_SUCCEEDED/UNVERIFIED`；不得把整个复合动作交给其他 Agent 重放。
- 中控接受旧 generation 的 Outcome 作为审计事件，但它不能覆盖当前 generation 的普通权威状态。旧 generation 携带有效 permit 的 `CONFIRMED/UNCERTAIN` effect 结果必须进入 effect ledger，并使工作单元保持 `RECONCILING`，直到聚合状态重新计算完成。
- 取消时，尚未进入 `SUBMITTING` 的已签发 permit 必须关闭为 `CLOSED_UNUSED`；已经进入 `SUBMITTING` 的节点必须完成只读验证并报告 `CONFIRMED` 或 `UNCERTAIN`。

generation 变更的原子时点是“旧租约关闭、所有旧 permit 可安全结算、generation 加一、新资源预占建立”这一中控数据库事务的提交点。

## 10. 中控任务拆分与资源调度

### 10.1 工作单元粒度

浏览器策略：

- 一个窗口。
- 一个目标。
- 一次冻结策略执行。
- 多目标、多账号和多窗口由中控拆成多个 WorkOrder。

评论 Campaign：

- 一个视频。
- 一套完整冻结评论树和最终文案。
- 一组中控预占的具体账号和窗口。
- 整棵评论树固定在同一 Agent 执行，不跨设备拆分。

### 10.2 文案决议

Web 可以配置模板或文案库，但中控拆分任务时必须确定每个视频和每个评论节点最终使用的文案，并冻结到 WorkOrder。Agent 不随机抽取文案。

### 10.3 资源分配

Agent 定期或在状态变化时同步账号和窗口库存。中控选择并预占具体资源，Agent 执行前进行本机二次校验。资源不可用时 Agent 返回 `REJECTED`，不得擅自换号。

资源使用统一唯一键：账号为 `account:<account_id>`，窗口为 `window:<device_id>:<window_ref>`。一个 WorkOrder 需要的所有资源按 `resource_key` 字典序排序，并且必须一次性全取或全部失败。

中控预占契约：

- `resource_reservation` 至少包含 `reservation_id`、`resource_key`、`work_order_id`、`lease_generation`、`status` 和时间戳。
- 数据库对每个 `resource_key` 只允许一条 ACTIVE 预占；多资源预占和 WorkOrder lease 在同一事务内完成。
- `REJECTED`、安全取消或确定终态时释放；已存在未解决 effect permit 时不得释放或转移。
- WSS 断线本身不释放预占。

Agent 本地锁契约：

- 本地数据库对每个 `resource_key` 只允许一条 ACTIVE 锁。
- 锁持有者为 `owner_kind=local_debug|remote_work`、`owner_id=run_id`，远程锁额外记录 reservation、work order 和 generation。
- Agent 按相同字典序在一个本地事务中获取全部锁；获取任意一个失败则回滚全部。
- Local WorkOrder 先获取本地锁再启动；Remote WorkOrder 必须同时验证中控 reservation 与本地锁。
- 本机调试在本地终态后释放锁。远程任务必须等到最终 Outcome 获得中控 HTTPS 确认或收到显式资源释放结论后才释放本地锁；断线和 Outbox 待报期间继续持有。如果 effect 状态未解决，则继续持有。
- Agent 重启时，本机调试锁对应进程不存在则标记调试中止并释放；远程锁保持，直到 WSS 对账确认继续、终止或进入人工恢复状态。

本机调试占用作为资源增量同步给中控并形成临时调度阻塞。如果中控预占与刚启动的本机调试发生同步竞态，Agent 的本地原子锁为最终执行门禁：Remote WorkOrder 必须 `REJECTED`，中控释放预占并重新调度，不能抢占调试。

调度按以下顺序处理：

1. 排除 WSS 离线、禁用或会话无效设备。
2. 校验协议、执行器和动作能力。
3. 校验账号、窗口归属和库存新鲜度。
4. 校验并发容量、本机调试占用和队列深度。
5. 校验设备健康状态。
6. 优先账号所在设备，再按负载和近期失败率排序。

普通 Web 客户不选择设备。运营管理员可以为调试或故障处理设置设备亲和性。

## 11. WSS 连接网关

- Agent 主动建立 `wss://.../agent/connect` 出站连接，中控通过该连接主动推送任务。
- 每台设备同一时间只允许一个有效 session。
- 新 session 认证完成后替换旧 session；旧连接失去下发资格。
- 每台设备维护跨 session 的独立单调递增 `server_sequence`。
- 每台设备使用有界发送队列；积压超过阈值后暂停继续分配。
- WSS 使用 443 端口和 TLS。
- 连接断开时，尚未发送的任务仍保存在中控数据库中。
- WSS 仅承担传输，不是任务事实源。

设备凭据的签发与恢复方式在独立身份设计中确定。当前协议通过 `DeviceCredentialProvider` 抽象身份读取、认证头生成和凭据轮换，生产模式不得使用无认证连接。

## 12. Agent 通用执行框架

通用框架包括：

- WSS 常驻客户端。
- 持久化 Inbox 和 Outbox。
- Executor Registry。
- WorkOrder 协调器。
- 统一账号/窗口资源锁。
- 执行阶段检查点和 WAL。
- 取消、断线和重启恢复。
- 关键进度上报。
- 本地证据与证据清单。

统一执行器契约：

```text
validate(order)       校验 Schema、能力、动作和资源
execute(context)      执行并写入安全检查点
cancel(checkpoint)    按副作用边界停止
recover(local_run)    重启后恢复、失败或标记不确定
```

通用框架不得包含浏览器策略或评论 Campaign 的业务字段判断。

## 13. 同级动作适配器

```text
Executor Registry
├── BrowserStrategyExecutor
└── CommentCampaignExecutor
```

两者等级相同、协议相同、资源锁相同、结果接口相同，不存在前后依赖或主次关系。通用框架完成后，两个适配器作为并行子项目接入。

### 13.1 BrowserStrategyExecutor

- 校验冻结策略、元素依赖、revision、content checksum 和 release checksum。
- 按输入 Schema 绑定目标 URL、输入文本等运行参数。
- 校验中控指定的账号和窗口。
- 复用现有 Execution V2 生命周期。
- 保存本机证据并输出统一 Outcome。

### 13.2 CommentCampaignExecutor

- 将远程 WorkOrder 导入本机 `Remote Campaign Run`，保留 task、work order 和 lease 关联。
- 使用一个视频、完整评论树、每节点最终文案和具体账号窗口。
- 复用现有 Campaign 父子依赖、意图、定位、提交和 Receipt 验证内核。
- 不创建本机人工审批。
- 点击发布前允许安全失败；点击后无法确认只能进入 `UNVERIFIED`。
- 汇总每个评论节点的结构化结果和本机证据清单。

## 14. 最终结果与错误分类

最终 `ExecutionOutcome` 通过 HTTPS 幂等上报，至少包含：

- `event_id`
- `work_order_id`
- `lease_generation`
- `status`
- `side_effect_state`
- 每个 `effect_id` 的状态、permit ID 和节点级结果
- `result_data`
- 稳定的 `error.category`、`error.code`、`error.stage`
- 开始、结束、耗时和尝试次数
- `evidence_manifest`
- device、session 和 executor version

### 14.1 错误分类

错误分类描述拒绝原因或节点失败原因，不直接覆盖 WorkOrder 终态。只要 WorkOrder 已包含 effect 节点，最终状态始终先按 §9.3 聚合；下表中的状态只适用于尚无已确认/不确定 effect 的简单情况。

| 分类 | 处置 |
|---|---|
| `VALIDATION` | REJECTED，不计执行尝试 |
| `RESOURCE` | 释放预占、刷新库存、重新调度 |
| `TRANSIENT` | 从未签发 effect permit 时允许新 generation 重试；已有 permit 时只允许原 Agent/原 generation 恢复未完成节点 |
| `BUSINESS` | 当前节点 `FAILED_PRE_EFFECT`；无已确认 effect 时聚合为 FAILED，否则按 §9.3 聚合 |
| `CANCELLED` | 未执行节点 `SKIPPED`；无已开始 effect 时聚合为 CANCELLED，否则按 §9.3 聚合 |
| `SIDE_EFFECT_UNCERTAIN` | 当前节点 `UNCERTAIN`，WorkOrder 聚合为 UNVERIFIED，禁止自动重试 |

`VALIDATION` 和接收阶段发现的 `RESOURCE` 错误只通过 `COMMAND_ACK(REJECTED)` 返回，不能生成 `FAILED` Outcome；WorkOrder 已进入执行后发生的资源类节点失败统一归入稳定的 `TRANSIENT` 或 `BUSINESS` 节点错误，再按 §9.3 聚合。

禁止根据错误文案推断重试策略。

### 14.2 结果幂等

Agent 在执行结束时先把 Outcome 与稳定 `event_id` 写入本地 Outbox，再发送 HTTPS 请求。中控对重复 `event_id` 返回与首次相同的成功确认，而不是把重复补报作为错误。只有内容与首次不一致时才返回幂等冲突。

HTTPS 接收规则：

- 当前 generation 的 Outcome 可以推动 WorkOrder 权威状态。
- 旧 generation 的普通结果只记录审计，不覆盖当前状态。
- 旧 generation 中携带有效 permit 的节点级 `CONFIRMED/UNCERTAIN` 必须进入 effect ledger，并触发 `RECONCILING` 聚合；中控不能丢弃可能已经发生的外部副作用。
- 中控确认完整 Outcome 后返回 `result_event_id` 和当前聚合状态；Agent 收到确认后删除对应 Outbox 项。

### 14.3 Effect Permit API

`POST /api/central/work-orders/{work_order_id}/effects/{effect_id}/permit` 请求至少包含 `device_id`、`session_id`、`lease_generation`、`run_id` 和稳定 `request_id`。成功响应包含稳定 `permit_id`、`permit_attempt`、generation 和签发时间。

相同 request ID 重复提交返回同一 permit。同一 effect 同时只允许一个 ISSUED permit；CLOSED_UNUSED 后的新 request ID 可以按策略获得下一 attempt。租约失效、依赖未满足、任务已取消或 effect 已进入 CONFIRMED/UNCERTAIN 时返回稳定拒绝码，不得隐式创建新 generation。

`POST /api/central/work-orders/{work_order_id}/effects/{effect_id}/close-unused` 使用 permit ID 和同一 generation 幂等关闭尚未进入外部提交的许可；已 CONFIRMED/UNCERTAIN 或不属于当前持有者的 permit 必须拒绝。

## 15. 证据策略

原始截图、录像和 DOM 快照只保存在 Agent 本机，不上传中控。Agent 上报轻量证据清单：

```json
{
  "evidence_id": "ev_01...",
  "type": "screenshot",
  "sha256": "...",
  "captured_at": "...",
  "available_on_device": true
}
```

中控和 Web 只能看到证据存在、类型、哈希和所在设备，当前版本不能远程打开证据。未来按需调取必须复用 `evidence_id`，不能修改现有 Outcome 契约。

## 16. 断线与重启恢复

### 16.1 WSS 断线

- Agent 不开始新的工作单元。
- 当前工作单元运行到最近安全检查点。
- 不再申请新的 effect permit；`PENDING/PERMIT_REQUESTED` 节点暂停，`AUTHORIZED` 节点关闭未使用 permit，`SUBMITTING` 节点完成只读验证并进入 `CONFIRMED` 或 `UNCERTAIN`。
- 最终结果写入本地 Outbox。

重连时 Agent 上送：

- device 和 session
- `last_server_sequence`
- 活动 work order 列表
- 待上报 event ID 列表

中控返回：

- 缺失命令补发
- 当前有效租约
- 已确认结果
- 必须停止的旧 generation

### 16.2 Agent 重启

- `RECEIVED/VALIDATING`：重新校验，租约有效时继续。
- `PENDING` effect：从安全检查点恢复；租约无效时不再申请 permit。
- `PERMIT_REQUESTED` effect：使用原 request ID 重发 permit 请求，按幂等响应恢复。
- `AUTHORIZED` effect：由于 `SUBMITTING` 尚未落盘，确认未发生外部调用，幂等 close-unused 后按重试策略处理。
- `CONFIRMED` effect：直接复用节点结果，永不重新提交。
- `SUBMITTING` effect：先执行只读验证；无法确认则记为 `UNCERTAIN`，禁止重新提交。
- 已写 Outbox 的结果继续使用原 `event_id` 补报。

## 17. Console 模块职责

| 模块 | 职责 |
|---|---|
| 动作库 | 动作设计、编辑、本机调试、调试历史、版本历史、发布与同步状态 |
| 任务执行 | 展示远程任务和本机调试运行记录，不创建动作 |
| 账号与窗口 | 展示空闲、本机调试占用、远程预占、执行中等资源状态 |
| 回执与证据 | 本机执行结果、原始证据、证据清单和中控上报状态 |
| 本机运行环境 | WSS 连接、中控地址、协议版本、Outbox 积压和执行器能力 |
| 系统设置 | 中控地址、WSS 重连参数、本地证据保留等配置 |

不新增 Agent 审批模块。本机只展示任务状态，不做 Web 审批。

动作库每条动作提供：编辑、本机调试、调试历史、复制、版本记录、发布到中控、同步状态和停用。

## 18. 数据持久化边界

中控新增或演进的逻辑实体：

- 动作发布注册表和不可变 revisions
- WorkOrders
- 资源预占和 leases
- effect permits 和 effect ledger
- command deliveries 和设备 server sequence
- WorkEvents 和 ExecutionOutcomes
- 证据清单

Agent 新增或接线的逻辑实体：

- command Inbox
- local/remote work runs
- 本地 Outbox
- 资源锁
- 本机 effect ledger
- 执行检查点/WAL
- 本地 evidence 和 manifest
- 动作发布同步状态

动作业务数据继续保存在各模块数据库中。

## 19. 安全边界

- 生产通信必须使用 WSS/HTTPS 和 TLS。
- WorkOrder、日志、结果和证据清单禁止包含 cookie、浏览器连接地址、长期令牌和明文密钥。
- 动作 snapshot 和 runtime parameters 必须进行大小、深度、字段和 Schema 限制。
- Agent 必须校验目标 `device_id`、协议版本、content/release checksum 和 lease generation。
- 中控必须校验最终结果的设备、工作单元、lease generation 和 event ID。
- 设备身份方案确认前，只允许开发环境受控联调，不得生产启用。

## 20. 实施结构

### 20.1 Gate G0：协议冻结

先完成并冻结：

- 所有 WSS 消息和 HTTPS 请求/响应 JSON Schema
- WorkOrder、Outcome、Effect Permit 和资源锁契约
- action content/release checksum golden vectors
- 状态与 ACK 映射
- 错误码和终态聚合规则

G0 的通过条件是 §21.1 全部通过，Central 和 Agent 可使用生成或共享的契约类型独立开发。

### 20.2 Gate G1：通用框架完成

实现：

- 标识和协议 Schema
- 动作发布注册表
- WorkOrder 编译和持久化
- WSS 网关和 ACK
- Agent Inbox/Outbox/WAL
- Executor Registry
- 资源锁和调度接线
- 结果、取消、断线和恢复
- Console 通用状态展示

G1 的通过条件是：使用两个 Fake Executor 完成 §21.2、§21.3 的全部幂等、故障和资源并发测试；通用框架源代码不导入浏览器策略或评论 Campaign 模块，也不包含它们的业务字段。

### 20.3 Gate G2：同级适配器并行接入

G1 通过后并行开发：

- BrowserStrategyExecutor 远程适配
- CommentCampaignExecutor 远程适配

两个适配器使用相同的通用契约和验收标准，不允许为了单个适配器修改信封幂等、租约或资源锁语义。

### 20.4 Gate G3：联合验收

两个适配器同时注册，在同一 Agent 上并发运行不同资源的 WorkOrder，完成 §21.4 和 §21.5。G3 通过且设备身份设计另行通过后，才允许进入生产启用评审。

## 21. 测试与验收

### 21.1 契约测试

- `C-01`：每种 WSS/HTTPS Schema 至少包含一个有效 golden vector 和字段缺失、类型错误、额外字段、超限四类无效向量；Central 与 Agent 对每个向量结论完全一致。
- `C-02`：Python 和 JavaScript 对至少 50 个包含 Unicode、数字、嵌套对象和数组的 canonical JSON 向量生成完全相同的 `action-content-jcs-sha256-v1` 和 `action-release-jcs-sha256-v1` checksum。
- `C-03`：Browser Strategy、Comment Campaign 和两个 Fake Executor 使用同一 WSS 信封，信封中不存在动作专属顶层字段。
- `C-04`：未声明参数、错误类型、越权绑定路径和任意 JSON merge 全部被拒绝；合法参数只改变声明位置。
- `C-05`：ACK、业务状态、错误码和 WorkOrder 终态聚合使用枚举穷举测试，不存在未处理值。

### 21.2 幂等与故障注入

- `F-01`：同一 `command_id` 连续和并发各重放 100 次，只产生一个 Inbox 项和一个 run。
- `F-02`：分别在接收前、Inbox 提交后/ACK 前、ACCEPTED 后、每个 effect 状态和 Outcome 写入后注入断线或进程退出；恢复后每个已持久化 WorkOrder 必须进入确定终态或明确 `RECONCILING`，不能消失。
- `F-03`：同一 `event_id` 重放 100 次，中控只保存一份内容并每次返回相同确认；相同 ID 不同内容必须 409 冲突。
- `F-04`：同一 `request_id` 并发申请 100 次只生成一个 permit；同一 effect 同时最多一个 ISSUED permit；CLOSED_UNUSED 后才能生成下一 attempt；`CONFIRMED/UNCERTAIN` 节点不能再签发 permit。
- `F-05`：旧 generation 无 permit 的结果不能改变当前权威状态；旧 generation 有效 permit 的节点结果必须进入 effect ledger 和 `RECONCILING`。
- `F-06`：网络恢复后，本地 Outbox 中所有事件在 60 秒内获得确认；测试结束 Outbox 无无主项。
- `F-07`：取消在每个 effect 状态注入，聚合状态严格符合 §9.3，且任何已签发 permit 不被第二次提交。

### 21.3 资源测试

- `R-01`：100 个并发请求争用同一 `resource_key`，中控和 Agent 各自都只能有一个 ACTIVE 持有者。
- `R-02`：多资源获取在任意一个资源冲突时全部回滚，不残留部分锁。
- `R-03`：本机调试先持锁时 Remote WorkOrder 必须 REJECTED；远程预占先到时本机调试必须被 UI 阻止。
- `R-04`：在获取锁、执行中和写 Outcome 后分别模拟崩溃；启动恢复后每个锁都有活动 owner、恢复状态或明确释放记录，不存在无主 ACTIVE 锁。
- `R-05`：存在未解决 effect permit 时，中控预占和本地锁都不得释放或转移。

### 21.4 动作测试

- `A-01`：浏览器策略和评论 Campaign 使用同级注册与调度，并通过同一套通用契约测试。
- `A-02`：浏览器策略一个窗口、一个目标、一次执行；批量输入在中控生成对应数量 WorkOrder。
- `A-03`：Campaign 一个视频、完整评论树、同一 Agent；每个评论节点对应稳定 effect ID。
- `A-04`：重复运行相同冻结 WorkOrder 时，文案、节点和参数完全一致，Agent 无随机选择调用。
- `A-05`：父节点未 CONFIRMED 时子节点 permit 申请被拒绝；Receipt 验证后才能推进依赖。
- `A-06`：两个适配器同时注册并使用不同资源并发运行，互不覆盖状态、锁、结果或证据清单。

### 21.5 规模测试

规模验收使用受控非生产参考环境：中控 4 vCPU/8 GB RAM、独立 PostgreSQL、同区域网络、Fake Executor、不传原始证据。

- `L-01`：500 个模拟 Agent 同时保持 WSS 连接 30 分钟，非主动断开率为 0，中控进程无崩溃和无界内存增长。
- `L-02`：向 500 个 Agent 各下发 10 个 WorkOrder；在无故障条件下，WSS 发送到 `RECEIVED` 的 P95 不超过 2 秒，命令丢失和重复 run 均为 0。
- `L-03`：500 个 Agent 在 60 秒窗口内随机重连，全部完成 `server_sequence` 对账；对账后命令集合与中控持久化集合完全一致。
- `L-04`：单设备待发送消息达到 100 条时停止继续分配，内存队列不超过配置上限；ACK 恢复后自动解除背压。
- `L-05`：10,000 个 Outcome 含 10% 重复 event ID 并发上报，最终唯一结果数、幂等确认和预期完全一致。

## 22. 完成标准

1. `C-01` 至 `C-05` 通过：协议、checksum、参数绑定和状态映射已经冻结。
2. `F-01` 至 `F-07` 通过：重复命令、断线、重启、结果补报、旧 generation 和多副作用都具有确定结果。
3. `R-01` 至 `R-05` 通过：本机调试和远程任务不会同时占用同一资源。
4. `A-01` 至 `A-06` 通过：浏览器策略与评论 Campaign 以同级执行器完成端到端执行。
5. `L-01` 至 `L-05` 通过：达到 500 台设备参考规模目标且背压有界。
6. Web 已批准任务通过 WSS 主动下发，所有正式验收日志中 Agent 主动任务轮询调用数为 0。
7. 所有已发布动作具有全局唯一 `action_id`、不可变 revision 和可跨语言复算的 content/release checksum。
8. 动作库保留本机设计、调试、发布和同步能力；当前 content checksum 未通过调试时普通发布被拒绝，强制发布具有完整审计和 waived 标识。
9. 原始证据只保存在本机，中控保存的每条 evidence manifest 均可关联 device、work order 和 SHA-256。
10. 状态枚举和 UI 中不存在 Agent 运行时审批状态。
11. 上述 G0-G3 和端到端验收均在受控非生产环境执行；设备身份方案未确认时，生产远程执行配置保持 fail closed。

## 23. 延后决策与生产门禁

设备唯一身份是本方案唯一延后的架构决策。后续必须单独确认：

- 设备注册入口和操作者流程
- 全局 device ID 的签发方式
- 本机凭据存储和轮换
- 设备克隆、重装和硬件更换处理
- 凭据吊销和恢复

在该设计获批并实现前，WSS 网关只能使用开发环境凭据提供器进行受控联调，生产配置必须 fail closed。
