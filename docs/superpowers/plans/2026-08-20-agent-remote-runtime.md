# Agent Remote Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在本机 Agent 中建立与动作类型无关的 WSS 接收、持久化执行、资源互斥、Effect Permit、结果补报和重启恢复框架。

**Architecture:** WSS 接收线程只做校验和 Inbox 提交；调度线程从本地 SQLite Inbox 创建 Run，经 ExecutorRegistry 找到执行器。所有 ACK/进度/终态引用先进入 Outbox，再发送；完整 Outcome 通过 HTTPS 幂等提交。通用运行时只认识协议、资源、effect 和 executor kind，不导入 `execution_v2` 或 `comment_campaign`。

**Tech Stack:** Python 3、websocket-client、requests、SQLite、threading/queue、pytest。

## Global Constraints

- `AgentWorker.run_forever()` 的旧 HTTP pull 流程不得作为正式远程路径；保留为兼容测试入口并标记 deprecated。
- 收到命令后必须先事务持久化 Inbox，再发送 `RECEIVED`；同 `command_id` 不得产生第二个 run。
- 远程 reservation 先于本机锁；Agent 本机以排序后的 `resource_keys` 原子加锁。
- Remote 占用阻止本机调试，本机调试占用导致 WorkOrder `REJECTED(resource_busy_local)`；不得强制抢占。
- WSS 断线后不启动新 WorkOrder；当前执行只到安全检查点。
- 原始截图、HTML、日志只保存在本机；Outcome 只含 evidence manifest、hash 和 device ID。
- 有未解决 permit/effect 时不得释放锁或把执行转移给其他 generation。

---

### Task 1: 建立 ExecutorRegistry 和与业务无关的执行协议

**Files:**
- Modify: `agent/protocol.py`
- Create: `agent/executor_registry.py`
- Create: `tests/test_agent_executor_registry.py`
- Modify: `tests/test_agent_integration.py`

- [ ] **Step 1: 写注册冲突、未知类型和通用依赖测试**

必须实现以下具名测试：`test_registry_rejects_duplicate_executor_kind`、`test_registry_rejects_unknown_executor_kind`、`test_fake_executors_share_same_context_and_outcome_contract`、`test_remote_runtime_modules_do_not_import_business_kernels`。最后一个测试读取通用运行时源码并断言没有 `execution_v2` 或 `comment_campaign` import。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_agent_executor_registry.py -q
```

Expected: FAIL，registry 不存在。

- [ ] **Step 3: 实现通用协议**

```python
@dataclass(frozen=True)
class ExecutionContext:
    work_order: dict
    report_progress: Callable[[str, dict], None]
    effects: EffectController
    checkpoint: Callable[[str], None]
    cancellation_requested: Callable[[], bool]

class Executor(Protocol):
    executor_kind: str

    def validate(self, order: dict) -> ValidatedOrder:
        raise NotImplementedError

    def execute(self, context: ExecutionContext) -> ExecutionOutcome:
        raise NotImplementedError

    def cancel(self, checkpoint: Checkpoint) -> CancelDecision:
        raise NotImplementedError

    def recover(self, local_run: LocalRun) -> RecoveryDecision:
        raise NotImplementedError
```

`EffectController.for_node(effect_id, attempt)` 返回 `EffectSession`，其方法固定为 `request_permit()`、`mark_submitting()`、`confirm(result)`、`mark_uncertain(error)`、`close_unused(error)`。这些方法负责本地事务和 HTTPS 调用，适配器不得直接调用 permit API 或自行写 effect 状态。

`ExecutionOutcome.status` 使用共享终态枚举，保留兼容属性映射给旧 StubExecutor 测试。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_agent_executor_registry.py tests/test_agent_integration.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交本任务**

```powershell
git add agent/protocol.py agent/executor_registry.py tests/test_agent_executor_registry.py tests/test_agent_integration.py
git commit -m "feat: add generic agent executor registry"
```

### Task 2: 建立事务 Inbox、Outbox、Run、Effect 和本机锁

**Files:**
- Create: `agent/remote_store.py`
- Create: `agent/resource_locks.py`
- Create: `agent/effect_ledger.py`
- Modify: `agent/config.py`
- Create: `tests/test_agent_remote_store.py`
- Create: `tests/test_agent_resource_locks.py`

- [ ] **Step 1: 写 100 次重放、原子锁和崩溃恢复测试**

必须实现以下具名测试：`test_100_concurrent_command_replays_create_one_inbox_and_run`、`test_multi_resource_lock_rolls_back_on_one_conflict`、`test_local_debug_and_remote_lock_are_mutually_exclusive`、`test_unresolved_effect_keeps_lock_after_restart`、`test_outbox_event_id_same_payload_is_idempotent`。并发测试必须使用 barrier 同时启动 100 个写线程，并直接断言 Inbox 和 Run 各 1 行。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_agent_remote_store.py tests/test_agent_resource_locks.py -q
```

Expected: FAIL，本地远程运行数据库不存在。

- [ ] **Step 3: 实现单 SQLite 数据库和事务边界**

数据库默认 `data/agent/remote_runtime.db`，包含 `remote_inbox`、`remote_runs`、`remote_outbox`、`remote_effects`、`resource_locks`、`sequence_state`。接收事务：

```python
def accept_command(command):
    with store.transaction(immediate=True) as tx:
        inbox = tx.insert_inbox_if_absent(command)
        run = tx.insert_run_if_absent(inbox)
        tx.enqueue_ack(command["command_id"], "RECEIVED")
    return run
```

锁 key 固定为 `account:<account_id>`、`window:<device_id>:<window_ref>`，按字符串排序后在一个 `BEGIN IMMEDIATE` 事务中获取。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_agent_remote_store.py tests/test_agent_resource_locks.py -q
```

Expected: PASS，覆盖 Agent 侧 `F-01`、`R-01` 至 `R-05`。

- [ ] **Step 5: 提交本任务**

```powershell
git add agent/remote_store.py agent/resource_locks.py agent/effect_ledger.py agent/config.py tests/test_agent_remote_store.py tests/test_agent_resource_locks.py
git commit -m "feat: persist agent remote runtime state"
```

### Task 3: 实现 WSS 会话、对账和持久 Outbox 发送

**Files:**
- Create: `agent/remote_client.py`
- Create: `agent/remote_session.py`
- Modify: `agent/client.py`
- Create: `tests/test_agent_remote_session.py`

- [ ] **Step 1: 写断线窗口与 sequence 对账测试**

必须实现以下具名测试：`test_received_ack_is_enqueued_only_after_inbox_commit`、`test_reconnect_sends_last_server_sequence_and_open_command_ids`、`test_sequence_gap_forces_reconcile_without_accepting_later_command`、`test_disconnect_stops_new_run_dispatch`、`test_outbox_retries_until_https_or_wss_ack`。每个测试使用可脚本化 Fake WebSocket，明确断言发送消息的完整顺序。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_agent_remote_session.py -q
```

Expected: FAIL，WSS 客户端不存在。

- [ ] **Step 3: 实现单连接状态机**

```python
class RemoteSession:
    def run_forever(self) -> None:
        self._connection_loop.run(self._connect_and_reconcile)

    def reconcile(self) -> None:
        self._send(self._store.build_reconcile_request())

    def receive(self, envelope: dict) -> None:
        self._dispatcher.dispatch(envelope)

    def flush_wss_outbox(self) -> None:
        self._outbox_sender.flush_channel("wss")

    def submit_http_outbox(self) -> None:
        self._outbox_sender.flush_channel("https")
```

连接 URL 来自 `CENTRAL_WSS_URL`；开发认证仅在 `AGENT_REMOTE_AUTH_MODE=development` 时发送 `X-Agent-Dev-Credential`。指数退避带抖动，上限 30 秒；任何断线都将 runtime gate 设为 closed。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_agent_remote_session.py -q
```

Expected: PASS，重连不依赖任务轮询。

- [ ] **Step 5: 提交本任务**

```powershell
git add agent/remote_client.py agent/remote_session.py agent/client.py tests/test_agent_remote_session.py
git commit -m "feat: connect agent through reconciled WSS"
```

### Task 4: 实现执行状态机、取消和 Effect Permit

**Files:**
- Create: `agent/remote_runtime.py`
- Create: `agent/local_debug.py`
- Modify: `agent/wal.py`
- Modify: `agent/worker.py`
- Create: `tests/test_agent_remote_runtime.py`
- Create: `tests/test_agent_local_debug.py`
- Modify: `tests/test_agent_wal.py`

- [ ] **Step 1: 写每个崩溃点与取消状态测试**

```python
@pytest.mark.parametrize("crash_point", [
    "before_inbox", "after_inbox_before_ack", "after_accepted",
    "permit_requested", "authorized", "submitting", "outcome_persisted",
])
def test_restart_reaches_terminal_or_reconciling(crash_point, crash_harness):
    recovered = crash_harness.crash_and_restart(crash_point)
    assert recovered.status in {"SUCCEEDED", "FAILED", "CANCELLED", "UNVERIFIED", "RECONCILING"}

@pytest.mark.parametrize("effect_state", [
    "PENDING", "PERMIT_REQUESTED", "AUTHORIZED", "SUBMITTING", "CONFIRMED", "UNCERTAIN",
])
def test_cancel_never_submits_effect_twice(effect_state, cancel_harness):
    result = cancel_harness.cancel_at(effect_state)
    assert result.platform_submit_count <= 1
```

另写 `test_local_debug_uses_same_registry_outcome_and_resource_locks`、`test_local_effect_controller_persists_before_platform_submit`、`test_local_debug_does_not_request_central_permit`。Local debug 可以不经过 WSS，但不能绕过 registry、effect ledger、Outcome/evidence 格式或资源锁。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_agent_remote_runtime.py tests/test_agent_local_debug.py tests/test_agent_wal.py -q
```

Expected: FAIL，现有 WAL 只记录窗口 stage，未连接远程 run/effect。

- [ ] **Step 3: 实现状态转换**

```text
RECEIVED → VALIDATING → ACCEPTED → RUNNING → REPORTING → TERMINAL
                                  ↘ REJECTED
RUNNING + disconnect → CHECKPOINT_BLOCKED
RUNNING + cancel before effect → CANCELLED
SUBMITTING + unknown result → UNVERIFIED
```

effect 转换必须按 `PENDING → PERMIT_REQUESTED → AUTHORIZED → SUBMITTING → CONFIRMED|UNCERTAIN` 每一步先落盘。未使用的 AUTHORIZED permit 通过 HTTPS close-unused 后才可回到可重试状态。

`EffectSession.request_permit()` 必须先在同一事务持久化稳定 `request_id` 和 `PERMIT_REQUESTED`，再发起 HTTPS；响应 `permit_id` 必须先持久化为 `AUTHORIZED`。`mark_submitting()` 必须在外部平台调用之前同步提交 SQLite 事务。重启看到 AUTHORIZED 时只允许 close-unused；看到 SUBMITTING 时只允许只读验证并转为 CONFIRMED 或 UNCERTAIN。

`LocalWorkOrderRunner` 构造同一 WorkOrder/ExecutionContext 并调用同一 ExecutorRegistry。它注入 `LocalEffectController`：在本机 ledger 中签发 `local_permit_id`，保持 AUTHORIZED/SUBMITTING 顺序，但绝不调用 Central permit API；远程运行注入 `RemoteEffectController`。适配器只依赖共同的 `EffectController` 接口。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_agent_remote_runtime.py tests/test_agent_local_debug.py tests/test_agent_wal.py -q
```

Expected: PASS，覆盖 `F-02`、`F-06`、`F-07`。

- [ ] **Step 5: 提交本任务**

```powershell
git add agent/remote_runtime.py agent/local_debug.py agent/wal.py agent/worker.py tests/test_agent_remote_runtime.py tests/test_agent_local_debug.py tests/test_agent_wal.py
git commit -m "feat: recover remote runs without duplicate effects"
```

### Task 5: 用两个 Fake Executor 完成 G1 验收

**Files:**
- Create: `tests/test_remote_framework_acceptance.py`
- Create: `tests/test_remote_framework_faults.py`
- Modify: `tests/test_agent_integration.py`

**Depends on:** Central WSS Control Plane Task 1–4，以及 Remote Action Console and Acceptance Task 1–3。未完成这些依赖不得宣称 G1 通过。

- [ ] **Step 1: 添加两个没有业务字段的 Fake Executor**

```python
class FakeReadExecutor:
    executor_kind = "fake.read"
    def validate(self, order):
        return ValidatedOrder(order)
    def execute(self, context):
        return ExecutionOutcome(status="SUCCEEDED", result_data={"read": True})
    def cancel(self, checkpoint):
        return CancelDecision.safe_stop(checkpoint)
    def recover(self, local_run):
        return RecoveryDecision.resume(local_run)

class FakeEffectExecutor:
    executor_kind = "fake.effect"
    def validate(self, order):
        return ValidatedOrder(order)
    def execute(self, context):
        effect = context.effects.for_node("effect-1", 1)
        permit = effect.request_permit()
        effect.mark_submitting(permit)
        effect.confirm({"fake": True})
        return confirmed_outcome(permit)
    def cancel(self, checkpoint):
        return CancelDecision.safe_stop(checkpoint)
    def recover(self, local_run):
        return RecoveryDecision.reconcile(local_run)
```

- [ ] **Step 2: 用 Fake Executor 运行跨 Central/Agent 的全部 G1 故障与资源测试**

```powershell
python -m pytest tests/test_central_work_orders.py tests/test_central_agent_gateway.py tests/test_central_effect_permits.py tests/test_central_remote_results.py tests/test_agent_executor_registry.py tests/test_agent_remote_store.py tests/test_agent_resource_locks.py tests/test_agent_remote_session.py tests/test_agent_remote_runtime.py tests/test_agent_local_debug.py tests/test_remote_framework_acceptance.py tests/test_remote_framework_faults.py tests/test_console_remote_action_api.py -q
```

Expected: PASS，两个 Fake Executor 完成 `F-01..F-07`、`R-01..R-05`；通用模块源码中 `rg "execution_v2|comment_campaign" agent/remote_*.py agent/executor_registry.py central/remote_*.py central/work_orders.py` 无匹配。

- [ ] **Step 3: 运行现有 Agent 回归测试**

```powershell
python -m pytest tests/test_agent_integration.py tests/test_agent_execution_v2.py tests/test_agent_wal.py -q
```

Expected: PASS，旧兼容入口仍可测试，但正式启动配置使用 RemoteSession。

- [ ] **Step 4: 提交本任务**

```powershell
git add tests/test_remote_framework_acceptance.py tests/test_remote_framework_faults.py tests/test_agent_integration.py
git commit -m "test: verify generic remote agent framework"
```
