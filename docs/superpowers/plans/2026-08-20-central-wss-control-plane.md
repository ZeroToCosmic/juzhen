# Central WSS Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有中控的“SubTask + Agent HTTP 拉取”调度扩展为可持久恢复的 WorkOrder 主动 WSS 下发控制面，并提供资源预占、Effect Permit 和幂等 Outcome API。

**Architecture:** 现有 Task 保留为 Web 业务任务，新增 WorkOrder 作为唯一 Agent 执行单元；调度事务原子创建资源预占和待投递命令。WSS 网关只从持久化 DeliveryCommand 投递并维护每设备单调 `server_sequence`，内存队列只是加速器。最终 Outcome 和 Effect Permit 走 HTTPS，Central Inbox 以 event/request ID 幂等。

**Tech Stack:** FastAPI、SQLAlchemy 2、SQLite/PostgreSQL、Redis 可选通知、WebSocket、Pydantic 2、pytest。

## Global Constraints

- 正式任务不再依赖 `/api/central/agent/subtasks` 轮询；旧端点在迁移期只读保留并返回 `Deprecation` header。
- `server_sequence` 按 device 持久递增，跨 session 不归零，只存在于 Central→Agent 信封。
- 同一 device 同时只允许一个有效 session；新 session 完成认证后关闭旧连接并使旧连接失去 ACK/上报资格。
- WorkOrder 必须携带动作完整快照和两个 checksum，中控不得要求 Agent 再读取动作正文。
- Central 资源预占对全部 `resource_keys` 原子成功或原子失败。
- 每设备未 ACK 命令达到 100 条后停止继续分配；不得形成无界内存队列。
- 旧 generation 无 permit 的上报不改变权威状态；有有效 permit 的旧 generation 进入 `RECONCILING`。
- 设备身份未定前，`REMOTE_ACTION_AUTH_MODE=development` 才允许开发凭据；其他模式 fail closed。

---

### Task 1: 建立 WorkOrder、投递、资源和 effect 持久模型

**Files:**
- Create: `central/remote_models.py`
- Modify: `central/migrate.py`
- Create: `tests/test_central_remote_models.py`
- Modify: `tests/test_central_migrate.py`

- [ ] **Step 1: 写约束和迁移失败测试**

必须实现以下具名测试：`test_command_id_and_device_sequence_are_unique`、`test_work_order_identity_tuple_is_unique`、`test_one_active_reservation_per_resource`、`test_one_issued_permit_per_effect`、`test_remote_tables_migrate_with_json_intact`。每个唯一性测试都必须提交两条冲突记录并断言 `IntegrityError`；迁移测试必须逐字段比较源库和目标库 JSON。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_remote_models.py tests/test_central_migrate.py -q
```

Expected: FAIL，远程执行表不存在。

- [ ] **Step 3: 实现模型**

创建 `WorkOrderRecord`、`DeliveryCommandRecord`、`DeviceSequenceRecord`、`ResourceReservationRecord`、`EffectRecord`、`EffectPermitRecord`、`ProgressEventRecord`、`ExecutionOutcomeRecord`。核心唯一约束：

```python
UniqueConstraint("tenant_id", "work_order_id")
UniqueConstraint("device_id", "server_sequence")
UniqueConstraint("command_id")
UniqueConstraint("event_id")
UniqueConstraint("request_id")
UniqueConstraint("work_order_id", "effect_id", "attempt")
UniqueConstraint("active_permit_token")
```

`ResourceReservationRecord` 使用 `active_token`：ACTIVE 时值为 `resource_key`，释放后为 NULL，并对 `active_token` 建唯一索引，以兼容 SQLite/PostgreSQL。

`EffectPermitRecord` 同样使用 `active_permit_token`：状态为 ISSUED 时值为规范化的 `<work_order_id>:<effect_id>`，进入 `CLOSED_UNUSED/CONFIRMED/UNCERTAIN` 时原子置 NULL。这样同一 WorkOrder effect 的不同 request ID、不同 attempt 并发申请也只能有一个 ISSUED permit，而不同 WorkOrder 可安全复用节点级 effect ID。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_central_remote_models.py tests/test_central_migrate.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add central/remote_models.py central/migrate.py tests/test_central_remote_models.py tests/test_central_migrate.py
git commit -m "feat: add central remote execution ledger"
```

### Task 2: 建立通用 WorkOrder 编译注册表与原子资源预占

**Files:**
- Create: `central/work_orders.py`
- Create: `central/remote_assignment.py`
- Modify: `central/tasks.py`
- Modify: `central/app.py`
- Create: `tests/test_central_work_orders.py`
- Modify: `tests/test_central_tasks.py`

- [ ] **Step 1: 用两个 Fake Compiler 写注册、冻结和资源原子性测试**

必须实现以下具名测试及结果：

- `test_compiler_registry_rejects_duplicate_executor_kind`：同 kind 第二次注册失败。
- `test_fake_compiler_persists_complete_frozen_work_order`：数据库记录包含 definition、runtime params、reservations、effect plan 和 policy，复算 checksum 一致。
- `test_unknown_executor_kind_creates_no_work_order`：返回稳定错误且事务无残留。
- `test_pinned_device_is_operator_only`：普通调用返回 403，operator 调试调用保留 pin。
- `test_reserving_two_resources_rolls_back_when_one_is_busy`：第二个 key 冲突后第一个 key 仍无新 owner。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_work_orders.py tests/test_central_tasks.py -q
```

Expected: FAIL，Task 仍只按 account 生成 SubTask，且没有 compiler registry。

- [ ] **Step 3: 实现明确的拆分器注册表**

```python
class WorkOrderCompilerRegistry:
    def register(self, executor_kind: str, compiler: WorkOrderCompiler) -> None:
        self._compilers.register_unique(executor_kind, compiler)

def create_and_reserve(session, task, release, inventory, registry) -> list[WorkOrderRecord]:
    orders = registry.get(release.executor_kind).compile(task, release, inventory)
    for order in orders:
        reserve_all(session, order.work_order_id, order.resource_keys)
        enqueue_delivery(session, order)
    return orders
```

G1 只注册 `fake.read` 与 `fake.effect` compiler；Browser Strategy 和 Comment Campaign compiler 在 G2 并行接入。普通 Web 请求忽略/拒绝 `device_id`，仅 operator 调试端点可设置 `pinned_device_id`。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_central_work_orders.py tests/test_central_tasks.py -q
```

Expected: PASS，两个 Fake Compiler 只通过 registry 分解；`central/work_orders.py` 不含真实动作业务字段。

- [ ] **Step 5: 提交本任务**

```powershell
git add central/work_orders.py central/remote_assignment.py central/tasks.py central/app.py tests/test_central_work_orders.py tests/test_central_tasks.py
git commit -m "feat: split tasks into reserved work orders"
```

### Task 3: 实现持久化 WSS 投递、ACK、进度和对账

**Files:**
- Create: `central/agent_gateway.py`
- Create: `central/delivery.py`
- Create: `central/remote_auth.py`
- Modify: `central/app.py`
- Modify: `central/config.py`
- Create: `tests/test_central_agent_gateway.py`
- Modify: `tests/test_central_websocket.py`

- [ ] **Step 1: 写 sequence、重放、ACK 和背压测试**

必须实现以下具名测试：`test_reconnect_replays_unacked_commands_after_last_sequence`、`test_received_ack_is_idempotent_and_marks_delivery`、`test_same_command_reconnect_never_allocates_new_sequence`、`test_new_authenticated_session_replaces_old_session`、`test_device_with_100_unacked_commands_is_not_scheduled`、`test_non_development_auth_without_provider_is_rejected`。分别断言重放集合、ACK 唯一记录、sequence 不变、旧 session 关闭且失去上报资格、背压设备不再获配和 WebSocket 关闭码 4403。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_agent_gateway.py tests/test_central_websocket.py -q
```

Expected: FAIL，现有 `/ws/events` 是租户仪表盘事件流，不是 Agent 双向网关。

- [ ] **Step 3: 新增独立 `/agent/connect`，不改造 `/ws/events`**

```python
@app.websocket("/agent/connect")
async def ws_agent(websocket: WebSocket) -> None:
    principal = await authenticate_agent(websocket)
    await serve_agent(websocket, principal)
```

握手后 Agent 首条消息必须为 `RECONCILE_REQUEST`；服务端返回 `RECONCILE_RESPONSE`，再按已持久化 sequence 重发未确认命令。每条 `COMMAND_ACK` 先经 Schema 校验，再按 `command_id + ack_kind` 幂等落库。`PROGRESS_EVENT` 按 `event_id` 幂等落库并只接受关键业务 stage；`TERMINAL_REFERENCE` 只记录 Agent 已将 Outcome 写入本机 Outbox，不能代替 HTTPS Outcome。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_central_agent_gateway.py tests/test_central_websocket.py -q
```

Expected: PASS，旧仪表盘事件流测试仍通过。

- [ ] **Step 5: 提交本任务**

```powershell
git add central/agent_gateway.py central/delivery.py central/remote_auth.py central/app.py central/config.py tests/test_central_agent_gateway.py tests/test_central_websocket.py
git commit -m "feat: push work orders over durable WSS"
```

### Task 4: 实现进度、Effect Permit 和 Outcome HTTPS API

**Files:**
- Create: `central/remote_results.py`
- Create: `central/effect_permits.py`
- Modify: `central/app.py`
- Create: `tests/test_central_effect_permits.py`
- Create: `tests/test_central_remote_results.py`

- [ ] **Step 1: 写并发幂等和 generation fencing 测试**

必须实现以下具名测试：`test_100_concurrent_same_request_ids_create_one_permit`、`test_100_concurrent_distinct_request_ids_leave_one_issued_permit`、`test_closed_unused_allows_next_attempt_but_uncertain_does_not`、`test_duplicate_event_same_payload_returns_same_ack`、`test_duplicate_event_different_payload_returns_409`、`test_stale_generation_without_permit_cannot_change_terminal_state`、`test_stale_generation_with_permit_enters_reconciling`。两个并发测试结束都必须直接查询数据库确认只有一个 ISSUED permit；不同 request ID 的失败响应使用稳定 `effect_permit_already_issued` 错误码。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_effect_permits.py tests/test_central_remote_results.py -q
```

Expected: FAIL，API 不存在。

- [ ] **Step 3: 实现事务 API**

路由固定为：

```text
POST /api/central/work-orders/{work_order_id}/effects/{effect_id}/permit
POST /api/central/work-orders/{work_order_id}/effects/{effect_id}/close-unused
POST /api/central/work-orders/{work_order_id}/outcomes
```

Outcome 聚合优先级固定为 `UNVERIFIED > SUCCEEDED > PARTIALLY_SUCCEEDED > CANCELLED > FAILED`，但只有符合设计 §9.3 条件时才可产生相应状态。成功接收 Outcome 后 HTTPS 响应返回稳定 `result_event_id` 和当前聚合状态；Agent 确认后删除本机 Outbox 项。

- [ ] **Step 4: 运行本计划全部测试**

```powershell
python -m pytest tests/test_central_remote_models.py tests/test_central_work_orders.py tests/test_central_agent_gateway.py tests/test_central_effect_permits.py tests/test_central_remote_results.py tests/test_central_websocket.py -q
```

Expected: PASS，覆盖 `F-03`、`F-04`、`F-05` 和 Central 侧 `R-01/R-02`。

- [ ] **Step 5: 提交本任务**

```powershell
git add central/remote_results.py central/effect_permits.py central/app.py tests/test_central_effect_permits.py tests/test_central_remote_results.py
git commit -m "feat: accept fenced remote outcomes"
```
