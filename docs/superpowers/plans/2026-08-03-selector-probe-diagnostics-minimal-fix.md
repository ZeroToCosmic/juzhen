# Selector Probe Diagnostics Minimal Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让探针失败必有安全错误码和真实阶段证据；证据缺失时 UI 明确显示“失败阶段未记录”，不再误报“准备测试环境失败”。

**Architecture:** 保留现有 API、SQLite 表、Redis Key、探针算法和自愈流程。补齐 healing 结果错误码；把 Publish 模式已有 `ProbeSessionManager` 进度事件接入 `SelectorProbeStore.update_run_progress()`；调度线程记录安全分类和无敏感异常栈；前端只依据真实阶段证据定位失败。

**Tech Stack:** Python 3、Flask、SQLite、Redis、Playwright、AdsPower local API、Browser JavaScript、pytest、Node.js test runner

## Global Constraints

- 不新增或修改 HTTP API。
- 不修改 SQLite schema、Redis namespace、Redis Key 或发布 Lua。
- 不修改 Profile 数量规则、页面等待规则、选择器生成、LLM、自愈、发布、暂停/恢复逻辑。
- 公共响应、数据库、前端不得保存原始异常消息、Profile ID、CDP URL、API Key、Cookie 或 DOM 原文。
- run-now 调度异常由 Flask 进程写入 `data/logs/flask-service.log`；后台 Worker 仍写 `data/logs/selector-probe-worker.log`。两者只保存 `request_id`、安全错误码、异常类型、栈帧文件/行号，不保存异常消息。
- 失败码必须匹配 `^[a-z][a-z0-9_]{0,63}$`。
- 现有未提交 UI 修改属于工作区既有改动；只做局部补丁，不覆盖或重排无关代码。

---

## File Structure

- Modify `selector_probe/probe.py`: 为所有 infrastructure healing 分支补确定错误码。
- Modify `selector_probe/healing_runtime.py`: 接收进度回调，转发 Profile/CDP 事件，记录探针页面创建。
- Modify `selector_probe/worker.py`: Publish 模式持久化阶段事件并传入 `HealingRuntime`。
- Modify `selector_probe/blueprint.py`: 分类 run-now 调度异常并写安全栈日志。
- Modify `gateway/static/selector_probe_ui.js`: 无失败阶段证据时显示“失败阶段未记录”。
- Modify `tests/test_selector_probe_observe.py`: 覆盖 healing 错误码。
- Modify `tests/test_selector_probe_healing_runtime.py`: 覆盖 runtime 进度转发。
- Modify `tests/test_selector_probe_worker.py`: 覆盖 Publish 阶段持久化。
- Modify `tests/test_selector_probe_dispatcher.py`: 覆盖安全调度错误码和日志。
- Modify `tests-js/selector-probe-operations.test.js`: 覆盖 UI 不误判。

### Task 1: Make Every Healing Infrastructure Result Actionable

**Files:**
- Modify: `selector_probe/probe.py:1710-1940`
- Test: `tests/test_selector_probe_observe.py:1420-1760`

**Interfaces:**
- Consumes: runtime results shaped as `{status, failure_class, code?}`.
- Produces: every `status="infrastructure_unavailable"` result contains non-empty `failure_code`.

- [ ] **Step 1: Add failing tests for three unclassified paths**

Extend `tests/test_selector_probe_observe.py`:

```python
@pytest.mark.parametrize(
    ("failure_point", "expected_code"),
    [
        ("active", "validate_active_unavailable"),
        ("candidate", "candidate_validation_unavailable"),
        ("full", "full_validation_unavailable"),
    ],
)
def test_healing_infrastructure_result_always_has_specific_code(
    failure_point,
    expected_code,
):
    bundle = healing_bundle("diagnostic")
    runtime = HealingRuntime(
        active_result=(
            {"status": "unavailable", "failure_class": "infrastructure"}
            if failure_point == "active"
            else {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            }
        ),
        deterministic_bundle=bundle,
        candidate_results=[
            {"status": "unavailable", "failure_class": "infrastructure"}
            if failure_point == "candidate"
            else {"status": "passed"}
        ],
        full_result=(
            {"status": "unavailable", "failure_class": "infrastructure"}
            if failure_point == "full"
            else None
        ),
    )

    result = run_healing_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == expected_code
```

- [ ] **Step 2: Run focused test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_observe.py -k "specific_code" -q
```

Expected: FAIL because current branches omit `failure_code`.

- [ ] **Step 3: Add exact fallback codes**

In `run_healing_probe()` change only infrastructure branches that currently return an empty code:

```python
if not active_passed and not _selector_failure(active_result):
    failure_code = str(active_result.get("code") or "")
    if failure_code not in _COMMENT_READINESS_CODES:
        return _healing_result(
            "infrastructure_unavailable",
            failure_code=(
                failure_code
                if _SAFE_CODE.fullmatch(failure_code)
                else "validate_active_unavailable"
            ),
        )
```

Inside `validate_fully()`:

```python
if not _healing_passed(validation):
    if _selector_failure(validation):
        return "selector", validation, None
    return "infrastructure", _healing_result(
        "infrastructure_unavailable",
        failure_code="candidate_validation_unavailable",
    ), None

if not _healing_passed(full_result):
    if _selector_failure(full_result):
        return "selector", full_result, None
    return "infrastructure", _healing_result(
        "infrastructure_unavailable",
        failure_code="full_validation_unavailable",
    ), None
```

Keep existing explicit codes such as `runtime_contract_invalid`, `validation_context_unavailable`, and `full_validation_invalid` unchanged.

- [ ] **Step 4: Run healing tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_observe.py -k "healing" -q
```

Expected: all selected tests pass; every infrastructure result has `failure_code`.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- selector_probe/probe.py tests/test_selector_probe_observe.py
git commit -m "fix(probe): preserve healing failure codes"
```

### Task 2: Persist Publish-Mode Profile and CDP Progress

**Files:**
- Modify: `selector_probe/healing_runtime.py:99-330`
- Modify: `selector_probe/worker.py:1093-1710`
- Test: `tests/test_selector_probe_healing_runtime.py`
- Test: `tests/test_selector_probe_worker.py`

**Interfaces:**
- Consumes: `progress_sink(event: Mapping[str, object])` and `SelectorProbeStore.update_run_progress(run_id, attempt_token, stages)`.
- Produces: bounded stage records named `profile_session`, `profile_start`, `cdp_endpoint`, `cdp_ready`, and `probe_page_open`.

- [ ] **Step 1: Add failing runtime forwarding test**

Add to `tests/test_selector_probe_healing_runtime.py`:

```python
def test_publish_runtime_forwards_profile_and_page_progress():
    events = []
    progress = []
    client = SimpleNamespace(events=events)
    store = FakeStore(events)
    registry = FakeRegistry(events)
    config = SimpleNamespace(
        target_url="https://www.tiktok.com/",
        test_profile_ids=("dedicated-a", "dedicated-b"),
        model_id="",
        site="tiktok",
        environment="production",
    )

    class ProgressSessionManager(FakeSessionManager):
        def __init__(self, client, *, progress_sink, **kwargs):
            super().__init__(client, **kwargs)
            self.progress_sink = progress_sink

        def open_profiles(self, profile_ids):
            handles = super().open_profiles(profile_ids)
            self.progress_sink({
                "name": "cdp_ready",
                "profile_mask": handles[0].profile_mask,
                "status": "passed",
                "attempt_count": 1,
            })
            return handles

    async def start_playwright():
        return FakePlaywright(events)

    runtime = HealingRuntime(
        config=config,
        settings={"selector_probe": {}},
        store=store,
        registry=registry,
        adspower_client=client,
        elements={},
        session_manager_factory=ProgressSessionManager,
        playwright_starter=start_playwright,
        progress_sink=progress.append,
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        pass

    assert any(item["name"] == "cdp_ready" for item in progress)
    page_events = [
        item for item in progress if item["name"] == "probe_page_open"
    ]
    assert len(page_events) == 2
    assert all(item["status"] == "passed" for item in page_events)
```

- [ ] **Step 2: Run runtime test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_healing_runtime.py -k "forwards_profile_and_page_progress" -q
```

Expected: FAIL because `HealingRuntime` has no `progress_sink` parameter.

- [ ] **Step 3: Add optional progress sink to HealingRuntime**

In `HealingRuntime.__init__()` add:

```python
progress_sink: Callable[[Mapping[str, object]], None] | None = None,
```

Validate and store:

```python
if progress_sink is not None and not callable(progress_sink):
    raise TypeError("progress_sink must be callable")
self.progress_sink = progress_sink
```

Add a private emitter that never breaks probe execution:

```python
def _record_progress(self, event: Mapping[str, object]) -> None:
    if self.progress_sink is None:
        return
    try:
        self.progress_sink(dict(event))
    except Exception:
        return
```

When constructing `ProbeSessionManager`, pass the callback only when its signature accepts `progress_sink` or `**kwargs`, matching the existing observe-mode compatibility pattern.

For every successful `open_probe_page()` call emit:

```python
self._record_progress({
    "name": "probe_page_open",
    "profile_mask": profile.profile_mask,
    "status": "passed",
    "attempt_count": 1,
})
```

If `open_probe_page()` raises, emit the same event with `status="failed"` and the safe exception `code`, then re-raise. Do not include exception text or CDP URL.

- [ ] **Step 4: Add failing Worker persistence test**

Add to `tests/test_selector_probe_worker.py` a Publish-mode `run_tick()` test using a runtime factory that captures `progress_sink`, emits:

```python
{"name": "profile_start", "profile_mask": "***0001", "status": "passed", "attempt_count": 1}
{"name": "cdp_ready", "profile_mask": "***0001", "status": "failed", "attempt_count": 3, "failure_code": "cdp_unavailable"}
```

Then return `{"status": "infrastructure_unavailable", "failure_code": "cdp_unavailable"}`. Assert stored `probe_runs.details_json` contains both sanitized stages and no raw Profile ID or CDP URL.

- [ ] **Step 5: Run Worker test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_worker.py -k "publish_progress" -q
```

Expected: FAIL because Publish mode does not pass or persist progress.

- [ ] **Step 6: Add bounded Publish progress recorder**

In the non-observe branch of `run_tick()`, create `stage_map` and a local `record_progress()` after `run_id` exists. Reuse `_sanitize_progress_event()` and the existing key shape:

```python
stage_map: dict[tuple[str, str, object], dict[str, object]] = {}

def record_progress(event: Mapping[str, object]) -> None:
    sanitized = _sanitize_progress_event(event)
    key = (
        str(sanitized["name"]),
        str(sanitized["profile_mask"]),
        sanitized.get("round"),
    )
    stage_map[key] = sanitized
    while len(stage_map) > 30:
        stage_map.pop(next(iter(stage_map)))
    if run_id is not None:
        store.update_run_progress(
            run_id,
            attempt_token=attempt_token,
            stages=list(stage_map.values()),
        )
```

Pass `progress_sink=record_progress` to `healing_runtime_factory(...)`.

Emit `profile_session` as `running` before opening runtime, `passed` immediately after `with runtime as opened_runtime` enters, and `failed` with the safe final code when runtime opening raises. `update_run_progress()` failure must not replace the original probe result; catch it, log only `selector_probe_progress_persist_failed`, and continue.

- [ ] **Step 7: Run Publish runtime and Worker tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_healing_runtime.py tests/test_selector_probe_worker.py -q
```

Expected: all tests pass; Publish run records real Profile/CDP/page stages.

- [ ] **Step 8: Commit Task 2**

```powershell
git add -- selector_probe/healing_runtime.py selector_probe/worker.py tests/test_selector_probe_healing_runtime.py tests/test_selector_probe_worker.py
git commit -m "feat(probe): persist publish runtime stages"
```

### Task 3: Preserve Safe Dispatcher Failure Categories and Stack Location

**Files:**
- Modify: `selector_probe/blueprint.py:1-70, 398-675`
- Test: `tests/test_selector_probe_dispatcher.py`

**Interfaces:**
- Consumes: an exception raised by `tick_runner(force=True, management_request_id=...)`.
- Produces: public safe code plus local diagnostic record containing request ID, exception type, and stack frames without exception message.

- [ ] **Step 1: Add failing dispatcher categorization test**

Extend `tests/test_selector_probe_dispatcher.py`:

```python
def test_dispatcher_classifies_untyped_exception_without_leaking_message():
    redis = SharedRedis()
    completed = threading.Event()
    terminal = []
    diagnostic = []

    def fail_tick(*, force):
        assert force is True
        raise RuntimeError("profile-secret api_key=do-not-log")

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=fail_tick,
        environment="production",
        site="tiktok",
        ttl_seconds=30,
        terminal_callback=lambda request_id, **payload: terminal.append(
            (request_id, payload)
        ),
        diagnostic_sink=diagnostic.append,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert completed.wait(0.5)
    assert terminal[0][1]["failure_code"] == "probe_dispatch_failed"
    rendered = repr(diagnostic)
    assert "RuntimeError" in rendered
    assert "request-a" in rendered
    assert "profile-secret" not in rendered
    assert "do-not-log" not in rendered
```

Import `_dispatch_failure_code` in the test module and add:

```python
def test_dispatch_failure_code_classifies_known_untyped_dependencies():
    class OperationalError(RuntimeError):
        pass

    assert (
        _dispatch_failure_code(OperationalError("private database path"))
        == "probe_store_unavailable"
    )
    assert (
        _dispatch_failure_code(ConnectionError("private redis URL"))
        == "probe_dependency_unavailable"
    )
    assert (
        _dispatch_failure_code(TimeoutError("private timeout target"))
        == "probe_dispatch_timeout"
    )
```

- [ ] **Step 2: Run dispatcher test and verify failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_dispatcher.py -k "classifies_untyped_exception" -q
```

Expected: FAIL because dispatcher currently collapses errors to `probe_unavailable` and has no diagnostic sink.

- [ ] **Step 3: Add safe classification and diagnostic payload**

Add `Callable` to the `collections.abc` import; add `logging` and `traceback` imports. Define:

```python
LOGGER = logging.getLogger("selector_probe.dispatcher")
```

Add helper:

```python
def _dispatch_failure_code(error: BaseException) -> str:
    explicit = _safe_code_text(
        getattr(error, "code", None),
        maximum=64,
    )
    if explicit:
        return explicit
    return {
        "OperationalError": "probe_store_unavailable",
        "ConnectionError": "probe_dependency_unavailable",
        "TimeoutError": "probe_dispatch_timeout",
    }.get(type(error).__name__, "probe_dispatch_failed")
```

Add optional constructor argument:

```python
diagnostic_sink: Callable[[Mapping[str, object]], None] | None = None,
```

Store it as `self.diagnostic_sink`. Add an emitter; injected sink receives a dict, default sink writes one structured local log entry:

```python
def _emit_dispatch_diagnostic(
    sink: Callable[[Mapping[str, object]], None] | None,
    *,
    request_id: str,
    failure_code: str,
    error: BaseException,
) -> None:
    payload = {
        "request_id": request_id,
        "failure_code": failure_code,
        "exception_type": type(error).__name__,
        "stack": [
            {
                "file": frame.filename,
                "line": frame.lineno,
                "function": frame.name,
            }
            for frame in traceback.extract_tb(error.__traceback__)[-12:]
        ],
    }
    try:
        if callable(sink):
            sink(payload)
        else:
            LOGGER.error(
                "selector_probe_run_now_failed diagnostic=%s",
                json.dumps(payload, ensure_ascii=True, separators=(",", ":")),
            )
    except Exception:
        return
```

Never include `str(error)`, `repr(error)`, local variables, arguments, Profile IDs, URLs, or credentials.

In the dispatcher catch block:

```python
failure_code = _dispatch_failure_code(error)
_emit_dispatch_diagnostic(
    self.diagnostic_sink,
    request_id=request_id,
    failure_code=failure_code,
    error=error,
)
```

Terminal callback continues storing only `failure_code`.

- [ ] **Step 4: Run dispatcher and management route tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_dispatcher.py tests/test_selector_probe_management_routes.py -q
```

Expected: all tests pass; sensitive exception message absent.

- [ ] **Step 5: Commit Task 3**

```powershell
git add -- selector_probe/blueprint.py tests/test_selector_probe_dispatcher.py
git commit -m "fix(probe): classify run dispatch failures"
```

### Task 4: Stop UI From Inventing a Failed Stage

**Files:**
- Modify: `gateway/static/selector_probe_ui.js:900-1140`
- Test: `tests-js/selector-probe-operations.test.js`

**Interfaces:**
- Consumes: sanitized failed run with zero failed stages.
- Produces: `currentStage.id === "unrecorded_failure"`, label `失败阶段未记录`, status `失败`.

- [ ] **Step 1: Add failing presentation test**

Add to `tests-js/selector-probe-operations.test.js`:

```javascript
test("failed run without stage evidence never blames environment preparation", () => {
  const presentation = buildRunPresentation({
    id: "request-no-stage",
    status: "dispatch_failed",
    failure: {status: "failed", failure_code: "probe_dispatch_failed"},
    stages: [],
  });

  assert.equal(presentation.statusLabel, "失败");
  assert.equal(presentation.currentStage.id, "unrecorded_failure");
  assert.equal(presentation.currentStage.title, "失败阶段未记录");
  assert.equal(presentation.currentStage.statusLabel, "失败");
  assert.doesNotMatch(presentation.result, /准备测试环境/);
  assert.match(presentation.failure.reason, /调度/);
});
```

- [ ] **Step 2: Run focused test and verify failure**

Run:

```powershell
node --test --test-name-pattern="without stage evidence" tests-js/selector-probe-operations.test.js
```

Expected: FAIL because current model selects first waiting stage.

- [ ] **Step 3: Add synthetic unrecorded-failure presentation only**

Before selecting `currentStage`, create:

```javascript
const unrecordedFailure = (
  overallStatus === "failed" && !failedStage
) ? {
  id: "unrecorded_failure",
  title: "失败阶段未记录",
  purpose: "后端未保存可确认的失败阶段，不将故障归因到任一业务步骤。",
  status: "failed",
  statusLabel: USER_STAGE_STATUS_LABELS.failed,
  result: "请查看安全错误码和 Worker 日志。",
} : null;

const currentStage = failedStage
  || unrecordedFailure
  || stages.find((stage) => stage.status === "running")
  || stages.find((stage) => stage.status === "waiting")
  || stages.at(-1);
```

Do not insert the synthetic item into the five-stage array. Five stage cards remain stable; only summary/current-step attribution changes.

Extend `FAILURE_REASON_LABELS`:

```javascript
probe_dispatch_failed: "探针调度器执行失败",
probe_store_unavailable: "探针运行记录存储暂时不可用",
probe_dependency_unavailable: "探针依赖服务暂时不可用",
probe_dispatch_timeout: "探针调度启动超时",
validate_active_unavailable: "无法读取或验证当前稳定选择器版本",
candidate_validation_unavailable: "候选选择器验证服务不可用",
full_validation_unavailable: "两个 Profile 两轮验证未完成",
```

- [ ] **Step 4: Run UI tests**

Run:

```powershell
node --test tests-js/selector-probe-operations.test.js
npm.cmd run test:node
```

Expected: operations tests and all Node tests pass; no raw `unknown` fields return to visible cards.

- [ ] **Step 5: Commit Task 4**

```powershell
git add -- gateway/static/selector_probe_ui.js tests-js/selector-probe-operations.test.js
git commit -m "fix(ui): avoid invented probe failure stage"
```

### Task 5: Full Regression and Diagnostic Acceptance

**Files:**
- Verify only; no production changes expected.

**Interfaces:**
- Consumes: all changes from Tasks 1-4.
- Produces: evidence that behavior, publication, security, and UI remain compatible.

- [ ] **Step 1: Run complete selector probe Python suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/test_selector_probe_*.py -q
```

Expected: all selector probe tests pass.

- [ ] **Step 2: Run complete Node suite**

Run:

```powershell
npm.cmd run test:node
```

Expected: all Node tests pass.

- [ ] **Step 3: Run source safety checks**

Run:

```powershell
rg -n "str\(error\)|repr\(error\)|exc_info=True" selector_probe/blueprint.py
rg -n "innerHTML" gateway/static/selector_probe_ui.js
git diff --check
git status --short
```

Expected:

- dispatcher diagnostic path contains no raw exception rendering;
- selector probe UI adds no `innerHTML` write;
- `git diff --check` succeeds;
- only planned files plus pre-existing user changes appear.

- [ ] **Step 4: Restart services once**

Use existing launcher restart flow. Do not start a second Flask or Worker instance. Confirm launcher reports Flask, statistics worker, and selector probe worker running.

- [ ] **Step 5: Run one manual probe and inspect read-only evidence**

From management UI click one run button once. Do not repeatedly dispatch while active. Confirm:

- failed request has non-empty `failure_code`;
- failed run with stage evidence points to actual Profile/CDP/page stage;
- failed request without stage evidence displays `失败阶段未记录`;
- run-now 调度异常时，`data/logs/flask-service.log` 包含 request ID、错误码、异常类型和栈帧；后台 Worker 异常仍检查 `data/logs/selector-probe-worker.log`；
- log contains no Profile ID, API Key, CDP URL, Cookie, or raw exception message.

- [ ] **Step 6: Commit any test-only acceptance correction**

Only if acceptance exposed a test fixture mismatch; production behavior changes require a new reviewed task.

```powershell
git add -- tests tests-js
git commit -m "test(probe): cover diagnostic acceptance"
```

## Rollback

- Code rollback: revert Tasks 4, 3, 2, 1 in reverse order.
- No data migration rollback required.
- Existing `stages` data remains valid JSON and is ignored by older code.
- New failure codes degrade safely to generic UI copy on older frontend versions.

## Success Criteria

- `infrastructure_unavailable` never leaves `failure_code` empty.
- `dispatch_failed` never exposes raw exception text.
- Publish mode records Profile start, CDP readiness, and probe page opening.
- UI 不会因缺少阶段证据而推断“准备测试环境失败”。
- One failed run can be assigned to one of: dispatcher, store/dependency, Profile start, CDP, page open, active validation, candidate validation, full validation.
- No API/schema/Redis key/automation behavior changes.
