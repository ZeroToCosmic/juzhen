# Equal Action Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Browser Strategy 与 Comment Campaign 作为完全同级的执行器适配到 Agent 通用框架，共用协议、注册、锁、permit、结果和证据清单。

**Architecture:** 两个适配器只依赖 `ExecutionContext`，分别把冻结 WorkOrder snapshot 翻译为现有内核调用。它们不互相导入、不互相启动；Agent 组合根在启动时并列注册。浏览器策略只负责一窗口一目标，Campaign 负责一视频完整评论树且每个评论节点是稳定 effect。

**Tech Stack:** Python 3、现有 execution_v2、现有 comment_campaign、Playwright/AdsPower、pytest。

## Global Constraints

- 两个适配器并行开发，写集分离；任何一个都不得成为另一个的前置动作。
- 适配器不得自行查询中控动作定义，不得随机选择文案、账号、窗口或目标。
- WorkOrder 的 `release_checksum`、`content_checksum`、executor kind 和本机能力不匹配时，在打开浏览器前 REJECTED。
- 所有平台写操作必须先取得对应 effect permit；只读导航/定位不申请 permit。
- 原始证据继续写各自本机证据目录，返回统一 manifest，不上传文件内容。
- 旧本机手动执行路径继续可用，但必须通过同一资源锁服务。

---

### Task 1: 先冻结两个适配器共享的符合性测试

**Files:**
- Create: `tests/remote_executor_contract.py`
- Create: `tests/test_action_adapter_contract.py`

- [ ] **Step 1: 写同级注册和契约测试**

建立参数化 `test_adapter_uses_generic_context`，输入 `(browser_executor_factory, "browser_strategy")` 和 `(campaign_executor_factory, "comment_campaign")`，断言两者只接收 `ExecutionContext`。同时实现 `test_both_adapters_register_without_order_dependency`、`test_action_specific_fields_exist_only_inside_snapshot`、`test_checksum_mismatch_rejects_before_browser_open`、`test_evidence_manifest_has_hash_device_and_local_reference_only`。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_action_adapter_contract.py -q
```

Expected: FAIL，Campaign Agent adapter 不存在，Browser adapter 仍使用旧 SubTask 协议。

- [ ] **Step 3: 提交测试契约**

```powershell
git add tests/remote_executor_contract.py tests/test_action_adapter_contract.py
git commit -m "test: freeze equal action adapter contract"
```

### Task 2A: 接入 BrowserStrategyExecutor

**Files:**
- Modify: `agent/execution_v2_executor.py`
- Create: `central/browser_strategy_compiler.py`
- Modify: `execution_v2/service.py`
- Modify: `execution_v2/executor.py`
- Modify: `execution_v2/store.py`
- Create: `tests/test_remote_browser_strategy_executor.py`
- Create: `tests/test_central_browser_strategy_compiler.py`
- Modify: `tests/test_agent_execution_v2.py`

- [ ] **Step 1: 写冻结快照、单窗口和取消测试**

必须实现以下具名测试：`test_browser_compiler_creates_one_order_per_window_and_target`（2 窗口 × 3 目标得到 6 个 WorkOrder）、`test_browser_adapter_executes_exact_frozen_snapshot`、`test_browser_adapter_rejects_more_than_one_window_or_target`、`test_browser_adapter_uses_preassigned_profile_and_window`、`test_cancel_before_effect_stops_at_checkpoint`、`test_uncertain_platform_write_returns_unverified`。冻结测试要在本机 Store 写入一个不同版本，断言执行器仍只使用 WorkOrder snapshot。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_browser_strategy_compiler.py tests/test_remote_browser_strategy_executor.py tests/test_agent_execution_v2.py -q
```

Expected: FAIL，现有适配器读取 `subtask.config_snapshot` 且返回旧 ExecutionOutcome。

- [ ] **Step 3: 改为通用 ExecutionContext**

```python
class BrowserStrategyExecutor:
    executor_kind = "browser_strategy"

    def execute(self, context: ExecutionContext) -> ExecutionOutcome:
        order = validate_browser_work_order(context.work_order)
        bound_definition = bind_parameters(
            order["definition"]["snapshot"],
            order["definition"]["parameter_schema"],
            order["runtime_params"],
        )
        account = require_reservation(order["resource_reservations"], "account")
        window = require_reservation(order["resource_reservations"], "window")
        return self._service.execute_frozen(
            snapshot=bound_definition,
            profile_id=account["account_id"],
            window_ref=window["window_ref"],
            hooks=context,
        )
```

适配器还必须实现 `validate/cancel/recover`；真实远程路径不得调用 `get_strategy(strategy_id)`。策略内任何映射到 `effect_plan` 的平台写动作都必须通过 `context.effects.for_node(effect_id, attempt)`：AUTHORIZED 阶段的确定性准备失败调用 `close_unused`；只有准备成功后才 `mark_submitting`，此后只能 `confirm` 或 `mark_uncertain`。只读动作不申请 permit。`central/browser_strategy_compiler.py` 注册同一个 `browser_strategy` kind，并冻结一窗口、一目标、一次执行所需的 `runtime_params` 和 reservation。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_central_browser_strategy_compiler.py tests/test_remote_browser_strategy_executor.py tests/test_agent_execution_v2.py tests/test_execution_v2_executor.py tests/test_execution_v2_service.py -q
```

Expected: PASS，完成 `A-02`。

- [ ] **Step 5: 提交本任务**

```powershell
git add agent/execution_v2_executor.py central/browser_strategy_compiler.py execution_v2/service.py execution_v2/executor.py execution_v2/store.py tests/test_central_browser_strategy_compiler.py tests/test_remote_browser_strategy_executor.py tests/test_agent_execution_v2.py
git commit -m "feat: adapt browser strategy to remote runtime"
```

### Task 2B: 并行接入 CommentCampaignExecutor

**Files:**
- Create: `agent/comment_campaign_executor.py`
- Create: `central/comment_campaign_compiler.py`
- Modify: `comment_campaign/service.py`
- Modify: `comment_campaign/executor.py`
- Modify: `comment_campaign/store.py`
- Create: `tests/test_remote_comment_campaign_executor.py`
- Create: `tests/test_central_comment_campaign_compiler.py`
- Modify: `tests/test_comment_campaign_executor.py`

- [ ] **Step 1: 写完整评论树、稳定 effect 和依赖测试**

必须实现以下具名测试：`test_campaign_compiler_creates_one_order_per_video_with_full_tree`（3 个视频得到 3 个 WorkOrder）、`test_campaign_compiler_resolves_text_before_persisting`、`test_campaign_adapter_keeps_one_video_and_full_tree_on_one_agent`、`test_campaign_uses_resolved_text_without_random_calls`、`test_each_node_uses_stable_effect_id_from_work_order`、`test_child_permit_waits_for_confirmed_parent`、`test_mixed_confirmed_and_failed_nodes_are_partially_succeeded`、`test_any_uncertain_node_makes_work_order_unverified`。随机性测试 monkeypatch 所有随机入口为抛错函数，执行仍必须成功。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_comment_campaign_compiler.py tests/test_remote_comment_campaign_executor.py tests/test_comment_campaign_executor.py -q
```

Expected: FAIL，Agent adapter 不存在，旧 service 仍包含本机审批/队列流程。

- [ ] **Step 3: 增加冻结执行入口，不删除本机设计能力**

```python
class CommentCampaignExecutor:
    executor_kind = "comment_campaign"

    def execute(self, context: ExecutionContext) -> ExecutionOutcome:
        order = context.work_order
        bound_definition = bind_parameters(
            order["definition"]["snapshot"],
            order["definition"]["parameter_schema"],
            order["runtime_params"],
        )
        tree = validate_campaign_snapshot(
            bound_definition,
            effect_plan=order["effect_plan"],
            reservations=order["resource_reservations"],
        )
        for node in topological_nodes(tree):
            require_confirmed_parent(node, context)
            effect = context.effects.for_node(node["effect_id"], node["attempt"])
            permit = effect.request_permit()
            try:
                prepared = self._executor.prepare_frozen_node(node, permit=permit, hooks=context)
            except PreSubmitFailure as error:
                effect.close_unused(error)
                continue
            effect.mark_submitting(permit)
            try:
                result = self._executor.submit_prepared_node(prepared, permit=permit, hooks=context)
                effect.confirm(result)
            except Exception as error:
                effect.mark_uncertain(error)
        return aggregate_campaign_outcome(tree)
```

适配器还必须实现 `validate/cancel/recover`。远程入口绕过 `approve_campaign/approve_submit`，因为 Web 审批发生在中控创建任务之前；本机手动 Campaign 原有调试流程继续存在。`central/comment_campaign_compiler.py` 注册 `comment_campaign` kind，在入库前解析最终文案、完整树和稳定 effect plan。

- [ ] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_central_comment_campaign_compiler.py tests/test_remote_comment_campaign_executor.py tests/test_comment_campaign_executor.py tests/test_comment_campaign_service.py tests/test_comment_campaign_store.py -q
```

Expected: PASS，完成 `A-03`、`A-04`、`A-05`。

- [ ] **Step 5: 提交本任务**

```powershell
git add agent/comment_campaign_executor.py central/comment_campaign_compiler.py comment_campaign/service.py comment_campaign/executor.py comment_campaign/store.py tests/test_central_comment_campaign_compiler.py tests/test_remote_comment_campaign_executor.py tests/test_comment_campaign_executor.py
git commit -m "feat: adapt comment campaign to remote runtime"
```

### Task 3: 在组合根并列注册并统一本机调试锁

**Files:**
- Create: `agent/bootstrap.py`
- Create: `central/remote_bootstrap.py`
- Modify: `central/app.py`
- Modify: `gateway/app.py`
- Modify: `execution_v2/service.py`
- Modify: `comment_campaign/service.py`
- Create: `tests/test_agent_bootstrap.py`
- Create: `tests/test_central_remote_bootstrap.py`
- Create: `tests/test_local_debug_remote_locking.py`

- [ ] **Step 1: 写注册顺序无关和本机/远程互斥测试**

必须实现以下具名测试：`test_agent_bootstrap_registers_both_equal_kinds`、`test_central_bootstrap_registers_both_equal_compilers`、`test_reversing_registration_order_changes_nothing`、`test_remote_reservation_blocks_browser_local_debug`、`test_campaign_local_debug_lock_rejects_remote_work_order`、`test_no_force_preemption_endpoint_exists`。互斥测试同时断言失败方未打开 AdsPower 窗口。

- [ ] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_agent_bootstrap.py tests/test_central_remote_bootstrap.py tests/test_local_debug_remote_locking.py -q
```

Expected: FAIL，两个业务服务尚未共享本机锁服务。

- [ ] **Step 3: 实现唯一组合根**

```python
def build_remote_runtime(settings):
    locks = ResourceLockService(settings.remote_db_path)
    registry = ExecutorRegistry()
    registry.register(BrowserStrategyExecutor(settings.browser_service))
    registry.register(CommentCampaignExecutor(settings.campaign_service))
    return RemoteRuntime(
        registry=registry,
        locks=locks,
        store=settings.remote_store,
        client=settings.remote_client,
    )

def build_work_order_compilers(settings):
    registry = WorkOrderCompilerRegistry()
    registry.register("browser_strategy", BrowserStrategyCompiler(settings.inventory))
    registry.register("comment_campaign", CommentCampaignCompiler(settings.content_resolver))
    return registry
```

`central/app.py` 在启动时从 `central.remote_bootstrap` 获取唯一 compiler registry 并注入 Task service；不得保留只含 Fake compiler 的生产组合根。Gateway 的手动调试入口获取同一个 `ResourceLockService`，owner type 使用 `LOCAL_DEBUG`；远程运行 owner type 使用 `REMOTE_RUN`。

- [ ] **Step 4: 运行 G2 全部测试**

```powershell
python -m pytest tests/test_action_adapter_contract.py tests/test_remote_browser_strategy_executor.py tests/test_remote_comment_campaign_executor.py tests/test_agent_bootstrap.py tests/test_central_remote_bootstrap.py tests/test_local_debug_remote_locking.py -q
```

Expected: PASS，完成 `A-01`、`A-06` 和 `R-03`。

- [ ] **Step 5: 运行两个内核回归测试**

```powershell
python -m pytest tests/test_execution_v2_service.py tests/test_execution_v2_runtime.py tests/test_comment_campaign_service.py tests/test_comment_campaign_integration.py -q
```

Expected: PASS，本机创建、设计、调试能力无退化。

- [ ] **Step 6: 提交本任务**

```powershell
git add agent/bootstrap.py central/remote_bootstrap.py central/app.py gateway/app.py execution_v2/service.py comment_campaign/service.py tests/test_agent_bootstrap.py tests/test_central_remote_bootstrap.py tests/test_local_debug_remote_locking.py
git commit -m "feat: register equal action adapters"
```
