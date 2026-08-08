# Browser V2 TikTok Wheel Calibration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 V2 页面点选器中采集三次一致的真实 TikTok 单格滚轮事件，原子发布校准版本，并让滚动动作每次只回放一个已校准事件组且验证只切换一个视频。

**Architecture:** 新建 `execution_v2/wheel_calibration.py`，集中负责事件采集、三样本归一化和无补发回放；V2 SQLite 保存版本及当前指针；`ExecutionV2Service` 复用现有 AdsPower、Playwright 和 Profile 租约管理校准会话。任务创建时把当前有效校准复制进策略快照，保证同一任务所有 Profile 使用同一版本。

**Tech Stack:** Python 3、Flask Blueprint、asyncio、Playwright、SQLite、原生 JavaScript、Node test runner、pytest。

## Global Constraints

- 校准必须使用一个独立测试 Profile，点选器、校准和任务执行互斥占用该 Profile。
- 连续采集 3 次真实单格滚动；只接受 `event.isTrusted === true`、`deltaMode === 0`。
- 每个事件组以 180ms 无新 wheel 事件作为结束边界。
- 三次事件数量和方向必须一致，总 `deltaY` 相对中位数偏差不得超过 20%。
- 每次校准和正式回放必须观察到恰好一次视频身份迁移且新身份稳定。
- 正式执行只回放一个校准事件组；失败不补发、不使用 burst。
- 新版本只有在 SQLite 事务提交成功后才替换旧版本；失败与取消保留旧版本。
- 单 Profile 失败不停止同批其他 Profile；窗口关闭和租约释放沿用 V2 现有规则。
- HTTP 不返回原始 Profile ID、WebSocket、Cookie 或页面内容。
- 当前环境 `.git` 元数据只读；各任务保留预期提交命令，但实现时不得执行，除非写权限恢复。

---

## File Structure

- Create `execution_v2/wheel_calibration.py`: wheel 事件采集、三样本验证、中位数发布数据、单事件组回放和视频迁移验证。
- Modify `execution_v2/store.py`: 校准版本表、当前指针表、读取与原子发布方法。
- Modify `execution_v2/service.py`: 校准会话生命周期、Profile 租约、任务预检与快照注入、关闭清理。
- Modify `execution_v2/blueprint.py`: 三个校准 HTTP 接口与公开错误映射。
- Modify `execution_v2/actions.py`: 滚动动作读取快照校准并调用单组回放。
- Modify `execution_v2/executor.py`: 把快照内的校准传给动作执行器。
- Modify `gateway/templates/browser_v2.html`: 页面点选器中的校准按钮和三次进度区。
- Modify `gateway/static/browser_v2.js`: 校准状态、启动、轮询、取消、与点选器互斥。
- Modify `gateway/static/browser_v2.css`: 三次进度格的最小样式。
- Create `tests/test_execution_v2_wheel_calibration.py`: 归一化、采集边界、回放和视频迁移单元测试。
- Modify `tests/test_execution_v2_store.py`: 原子发布和旧版本保留。
- Modify `tests/test_execution_v2_service.py`: 租约、生命周期、任务预检、清理。
- Modify `tests/test_execution_v2_routes.py`: 请求白名单、状态码和脱敏。
- Modify `tests/test_execution_v2_actions.py`: 只回放一次及错误透传。
- Modify `tests/test_execution_v2_executor.py`: 校准快照传递。
- Modify `tests-js/browser-v2-ui.test.js`: UI 互斥、轮询和三步状态。

---

### Task 1: 三样本归一化与原子版本存储

**Files:**
- Create: `execution_v2/wheel_calibration.py`
- Modify: `execution_v2/store.py:27-67,93-108`
- Create: `tests/test_execution_v2_wheel_calibration.py`
- Modify: `tests/test_execution_v2_store.py`

**Interfaces:**
- Produces: `WheelCalibrationError(code: str)`。
- Produces: `normalize_wheel_samples(samples: list[dict[str, Any]]) -> dict[str, Any]`。
- Produces: `ExecutionStore.publish_wheel_calibration(scope: str, direction: str, events: list[dict[str, Any]], sample_count: int) -> dict[str, Any]`。
- Produces: `ExecutionStore.get_wheel_calibration(scope: str = "tiktok_feed") -> dict[str, Any] | None`。

- [ ] **Step 1: 写归一化失败测试**

```python
import pytest

from execution_v2.wheel_calibration import WheelCalibrationError, normalize_wheel_samples


def _sample(delta=100, *, transitions=1, delta_mode=0):
    return {
        "direction": "down",
        "identity_transitions": transitions,
        "events": [
            {"delta_x": 0, "delta_y": delta, "delta_mode": delta_mode, "delay_ms": 0}
        ],
    }


def test_three_consistent_samples_publish_median_event():
    result = normalize_wheel_samples([_sample(100), _sample(104), _sample(98)])
    assert result == {
        "direction": "down",
        "events": [
            {"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}
        ],
        "sample_count": 3,
    }


@pytest.mark.parametrize(
    ("samples", "code"),
    [
        ([_sample(), _sample(), _sample(delta_mode=1)], "wheel_calibration_unsupported_delta_mode"),
        ([_sample(), _sample(), _sample(transitions=0)], "wheel_calibration_video_not_changed"),
        ([_sample(), _sample(), _sample(transitions=2)], "wheel_calibration_multiple_videos"),
        ([_sample(100), _sample(100), _sample(140)], "wheel_calibration_inconsistent"),
    ],
)
def test_invalid_samples_fail_closed(samples, code):
    with pytest.raises(WheelCalibrationError, match=code):
        normalize_wheel_samples(samples)
```

- [ ] **Step 2: 运行归一化测试并确认失败**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py -q -p no:cacheprovider
```

Expected: FAIL，模块 `execution_v2.wheel_calibration` 尚不存在。

- [ ] **Step 3: 实现严格归一化**

```python
class WheelCalibrationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def normalize_wheel_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(samples, list) or len(samples) != 3:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    directions = {sample.get("direction") for sample in samples}
    if directions != {"down"}:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    for sample in samples:
        transitions = sample.get("identity_transitions")
        if transitions == 0:
            raise WheelCalibrationError("wheel_calibration_video_not_changed")
        if transitions != 1:
            raise WheelCalibrationError("wheel_calibration_multiple_videos")
        events = sample.get("events")
        if not isinstance(events, list) or not events:
            raise WheelCalibrationError("wheel_calibration_inconsistent")
        if any(event.get("delta_mode") != 0 for event in events):
            raise WheelCalibrationError("wheel_calibration_unsupported_delta_mode")
    counts = {len(sample["events"]) for sample in samples}
    if len(counts) != 1:
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    totals = [sum(float(event["delta_y"]) for event in sample["events"]) for sample in samples]
    median_total = statistics.median(totals)
    if median_total <= 0 or any(abs(total - median_total) > abs(median_total) * 0.20 for total in totals):
        raise WheelCalibrationError("wheel_calibration_inconsistent")
    events = []
    for index in range(next(iter(counts))):
        events.append(
            {
                "delta_x": float(statistics.median(float(sample["events"][index]["delta_x"]) for sample in samples)),
                "delta_y": float(statistics.median(float(sample["events"][index]["delta_y"]) for sample in samples)),
                "delta_mode": 0,
                "delay_ms": float(statistics.median(float(sample["events"][index]["delay_ms"]) for sample in samples)),
            }
        )
    return {"direction": "down", "events": events, "sample_count": 3}
```

`execution_v2/wheel_calibration.py` 必须导入 `statistics` 和 `Any`，并拒绝布尔值、非有限数、负 `delay_ms` 及 `delta_y <= 0`；这些分支各补一个参数化测试。

- [ ] **Step 4: 写存储失败测试**

```python
def test_wheel_calibration_publish_is_versioned_and_restart_safe(tmp_path):
    store = ExecutionStore(tmp_path / "v2.db")
    store.initialize()
    first = store.publish_wheel_calibration(
        "tiktok_feed", "down",
        [{"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}],
        3,
    )
    second = store.publish_wheel_calibration(
        "tiktok_feed", "down",
        [{"delta_x": 0.0, "delta_y": 104.0, "delta_mode": 0, "delay_ms": 0.0}],
        3,
    )
    reopened = ExecutionStore(tmp_path / "v2.db")
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert reopened.get_wheel_calibration()["events"][0]["delta_y"] == 104.0


def test_failed_pointer_swap_rolls_back_new_version_and_keeps_current(tmp_path):
    store = ExecutionStore(tmp_path / "v2.db")
    store.initialize()
    store.publish_wheel_calibration("tiktok_feed", "down", EVENTS, 3)
    with store.connect() as connection:
        connection.execute(
            "CREATE TRIGGER reject_calibration_swap BEFORE UPDATE ON wheel_calibration_current "
            "BEGIN SELECT RAISE(ABORT, 'swap rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="swap rejected"):
        store.publish_wheel_calibration("tiktok_feed", "down", NEW_EVENTS, 3)
    assert store.get_wheel_calibration()["revision"] == 1
```

测试文件定义：

```python
import sqlite3

EVENTS = [{"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}]
NEW_EVENTS = [{"delta_x": 0.0, "delta_y": 104.0, "delta_mode": 0, "delay_ms": 0.0}]
```

- [ ] **Step 5: 添加表和原子发布方法**

```sql
CREATE TABLE IF NOT EXISTS wheel_calibrations (
  scope TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
  direction TEXT NOT NULL, events_json TEXT NOT NULL, sample_count INTEGER NOT NULL,
  created_at TEXT NOT NULL, PRIMARY KEY(scope, revision)
);
CREATE TABLE IF NOT EXISTS wheel_calibration_current (
  scope TEXT PRIMARY KEY, revision INTEGER NOT NULL,
  FOREIGN KEY(scope, revision) REFERENCES wheel_calibrations(scope, revision)
);
```

```python
def publish_wheel_calibration(self, scope, direction, events, sample_count):
    now = utc_now_iso()
    with self.connect() as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM wheel_calibrations WHERE scope = ?",
            (scope,),
        ).fetchone()
        revision = int(row["revision"])
        connection.execute(
            "INSERT INTO wheel_calibrations(scope, revision, status, direction, events_json, sample_count, created_at) VALUES (?, ?, 'validated', ?, ?, ?, ?)",
            (scope, revision, direction, _json(events), sample_count, now),
        )
        connection.execute(
            "INSERT INTO wheel_calibration_current(scope, revision) VALUES (?, ?) ON CONFLICT(scope) DO UPDATE SET revision = excluded.revision",
            (scope, revision),
        )
    return self.get_wheel_calibration(scope)
```

`get_wheel_calibration()` 用一次 JOIN 返回 `scope/revision/status/direction/events/sample_count/created_at`，并通过 `json.loads(events_json)` 解码事件。

- [ ] **Step 6: 运行 Task 1 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py tests\test_execution_v2_store.py -q -p no:cacheprovider
```

Expected: PASS。

- [ ] **Step 7: 记录预期提交（当前环境不执行）**

```powershell
git add execution_v2/wheel_calibration.py execution_v2/store.py tests/test_execution_v2_wheel_calibration.py tests/test_execution_v2_store.py
git commit -m "feat: persist wheel calibration versions"
```

---

### Task 2: 真实 wheel 采集与单次迁移观察器

**Files:**
- Modify: `execution_v2/wheel_calibration.py`
- Modify: `tests/test_execution_v2_wheel_calibration.py`

**Interfaces:**
- Consumes: `normalize_wheel_samples()`。
- Produces: `async WheelCalibrationRunner.prepare(page: Any) -> None`。
- Produces: `async WheelCalibrationRunner.collect(page: Any, progress: Callable[[dict], Awaitable[None]], cancel_event: asyncio.Event) -> dict[str, Any]`。
- Produces: `observe_single_transition(page: Any, before: FeedState, *, timeout: float, sleep_fn: Callable) -> FeedState`。

- [ ] **Step 1: 写事件分组和迁移测试**

```python
def test_prepare_injects_trusted_pixel_wheel_recorder():
    script = wheel_recorder_script("rec-1")
    assert "event.isTrusted" in script
    assert "event.deltaMode" in script
    assert "180" in script
    assert "addEventListener('wheel'" in script


@pytest.mark.asyncio
async def test_observer_rejects_two_distinct_video_transitions(monkeypatch):
    states = iter([state("a"), state("b"), state("c"), state("c")])
    async def capture(_page):
        return next(states)
    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    with pytest.raises(WheelCalibrationError, match="wheel_calibration_multiple_videos"):
        await observe_single_transition(object(), state("a"), timeout=1, sleep_fn=no_sleep)
```

测试替身应使用异步 `capture()` 函数逐次返回 `FeedState`，直到连续两次同一新 fingerprint 才稳定；观察到第二个不同新 fingerprint 立即失败。

- [ ] **Step 2: 运行测试并确认失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py -q -p no:cacheprovider
```

Expected: FAIL，采集器和观察器尚未定义。

- [ ] **Step 3: 实现页面采集器**

注入脚本使用固定私有键 `__codexV2WheelCalibration`，捕获阶段监听 wheel：

```javascript
const onWheel = event => {
  if (!state.active || !event.isTrusted) return;
  const now = performance.now();
  state.events.push({
    delta_x: Number(event.deltaX),
    delta_y: Number(event.deltaY),
    delta_mode: Number(event.deltaMode),
    at_ms: now
  });
  clearTimeout(state.idleTimer);
  state.idleTimer = setTimeout(() => {
    const events = state.events.splice(0);
    state.gestures.push(events.map((item, index) => ({
      delta_x: item.delta_x,
      delta_y: item.delta_y,
      delta_mode: item.delta_mode,
      delay_ms: index === 0 ? 0 : item.at_ms - events[index - 1].at_ms
    })));
  }, 180);
};
document.addEventListener('wheel', onWheel, true);
```

提供 `prepare/drain/cleanup` 三个页面操作；`cleanup` 必须移除监听器、清除 timer 并删除私有状态。

- [ ] **Step 4: 实现连续身份观察**

```python
async def observe_single_transition(page, before, *, timeout=8.0, sleep_fn=asyncio.sleep):
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    stable = None
    while time.monotonic() < deadline:
        current = await capture_feed_state(page)
        if current.fingerprint != before.fingerprint:
            if current.fingerprint not in seen:
                seen.append(current.fingerprint)
            if len(seen) > 1:
                raise WheelCalibrationError("wheel_calibration_multiple_videos")
            if stable is not None and stable.fingerprint == current.fingerprint:
                return current
            stable = current
        else:
            stable = None
        await sleep_fn(0.05)
    raise WheelCalibrationError("wheel_calibration_video_not_changed")
```

`WheelCalibrationRunner.collect()` 对三次采样循环：记录 before、等待 drain 返回一个完整事件组、并行持续采样身份、验证后调用 progress 更新 `sample_index/status`；异常和取消都在 `finally` 执行 cleanup。

- [ ] **Step 5: 运行 Task 2 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py tests\test_browser_video_switch.py -q -p no:cacheprovider
```

Expected: PASS，且旧 `browser_video_switch` 测试无回归。

- [ ] **Step 6: 记录预期提交（当前环境不执行）**

```powershell
git add execution_v2/wheel_calibration.py tests/test_execution_v2_wheel_calibration.py
git commit -m "feat: capture trusted wheel calibration"
```

---

### Task 3: V2 校准会话、Profile 租约和清理

**Files:**
- Modify: `execution_v2/service.py:64-69,151-219,309-441,638-672`
- Modify: `tests/test_execution_v2_service.py`

**Interfaces:**
- Consumes: `WheelCalibrationRunner.collect()`、store 读写方法。
- Produces: `ExecutionV2Service.start_wheel_calibration(profile_token: str, target_url: str) -> dict`。
- Produces: `ExecutionV2Service.get_wheel_calibration() -> dict`。
- Produces: `ExecutionV2Service.cancel_wheel_calibration() -> dict`。

- [ ] **Step 1: 写生命周期和租约失败测试**

```python
def test_calibration_and_picker_cannot_share_profile(tmp_path):
    runner = FakeWheelRunner(hold=True)
    service = make_service(tmp_path, wheel_runner=runner)
    token = service.list_profiles()[0]["profile_token"]
    started = service.start_wheel_calibration(token, "https://www.tiktok.com/")
    assert started["status"] == "waiting_for_sample"
    with pytest.raises(ExecutionConflictError, match="profile_already_in_use"):
        service.start_picker(token, "https://www.tiktok.com/")
    cancelled = service.cancel_wheel_calibration()
    assert cancelled["status"] == "cancelled"
    assert service.adspower.stopped == ["profile-raw"]


def test_failed_recalibration_keeps_previous_version(tmp_path):
    service = make_service(tmp_path, wheel_runner=FakeWheelRunner(error="wheel_calibration_inconsistent"))
    service.store.publish_wheel_calibration("tiktok_feed", "down", EVENTS, 3)
    token = service.list_profiles()[0]["profile_token"]
    service.start_wheel_calibration(token, "https://www.tiktok.com/")
    wait_until_terminal(service)
    state = service.get_wheel_calibration()
    assert state["current"]["revision"] == 1
    assert state["active"]["error_code"] == "wheel_calibration_inconsistent"
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_service.py -q -p no:cacheprovider
```

Expected: FAIL，service 尚无校准方法和 runner 注入点。

- [ ] **Step 3: 增加会话记录与依赖注入**

```python
@dataclass(slots=True)
class _WheelCalibrationRecord:
    session_id: str
    raw_profile_id: str
    binding: BrowserBinding
    lease_owner: str
    cancel_event: asyncio.Event
    state: dict[str, Any]
    task: asyncio.Task[Any] | None = None
```

`ExecutionV2Service.__init__` 增加 `wheel_runner: Any | None = None`，默认创建 `WheelCalibrationRunner()`；增加 `self._wheel_calibration: _WheelCalibrationRecord | None = None`。

- [ ] **Step 4: 实现启动、后台采集和公开状态**

```python
def start_wheel_calibration(self, profile_token: str, target_url: str) -> dict[str, Any]:
    return self._run(self._start_wheel_calibration(profile_token, target_url), timeout=60)


async def _start_wheel_calibration(self, profile_token, target_url):
    active_statuses = {"preparing", "waiting_for_sample", "validating", "cancelling"}
    if self._wheel_calibration is not None and self._wheel_calibration.state["status"] in active_statuses:
        raise ExecutionConflictError("wheel_calibration_already_active")
    self._wheel_calibration = None
    raw_id = (await self._resolve_profiles([profile_token]))[0]
    session_id = self._new_id()
    owner = f"wheel-calibration:{session_id}"
    self._acquire_profiles([raw_id], owner)
    started = False
    try:
        ws_url = await self.adspower.start(raw_id)
        started = True
        binding = await self.sessions.connect(raw_id, ws_url)
        await binding.page.goto(target_url)
        await self._wheel_runner.prepare(binding.page)
        record = _WheelCalibrationRecord(
            session_id, raw_id, binding, owner, asyncio.Event(),
            {"session_id": session_id, "status": "waiting_for_sample", "sample_index": 0},
        )
        self._wheel_calibration = record
        record.task = asyncio.create_task(self._run_wheel_calibration(record))
        return self._public(record.state)
    except Exception:
        if started:
            await self._stop_and_confirm(raw_id)
        self._release_profiles([raw_id], owner)
        raise
```

后台方法收到归一化结果后调用 `publish_wheel_calibration`；`finally` 关闭 Profile、释放 owner 租约，但保留终态公开 state 供 GET 展示。下一次 start 可覆盖已经终止的活动记录。

- [ ] **Step 5: 实现取消和服务关闭清理**

`cancel_wheel_calibration()` 在 V2 runtime 内设置 `cancel_event` 并等待 task；`_close_all()` 在关闭 pickers 后取消校准。窗口关闭及租约释放只能由后台 `finally` 执行一次。

```python
async def _cancel_wheel_calibration(self):
    record = self._wheel_calibration
    if record is None or record.state["status"] not in {"preparing", "waiting_for_sample", "validating"}:
        return {"status": "idle"}
    record.cancel_event.set()
    if record.task is not None:
        await record.task
    return self._public(record.state)
```

- [ ] **Step 6: 运行 Task 3 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_service.py tests\test_execution_v2_wheel_calibration.py -q -p no:cacheprovider
```

Expected: PASS。

- [ ] **Step 7: 记录预期提交（当前环境不执行）**

```powershell
git add execution_v2/service.py tests/test_execution_v2_service.py
git commit -m "feat: manage wheel calibration sessions"
```

---

### Task 4: 三个 HTTP 接口和公开错误

**Files:**
- Modify: `execution_v2/blueprint.py:87-239`
- Modify: `tests/test_execution_v2_routes.py`

**Interfaces:**
- Consumes: Task 3 的三个 service 方法。
- Produces: `GET /api/browser-v2/wheel-calibration`。
- Produces: `POST /api/browser-v2/wheel-calibration/start`。
- Produces: `POST /api/browser-v2/wheel-calibration/cancel`。

- [ ] **Step 1: 写路由契约测试**

```python
def test_wheel_calibration_routes_use_closed_request_shapes(client, service):
    started = client.post(
        "/api/browser-v2/wheel-calibration/start",
        json={"profile_token": "public-token", "target_url": "https://www.tiktok.com/"},
    )
    assert started.status_code == 202
    assert client.get("/api/browser-v2/wheel-calibration").status_code == 200
    assert client.post("/api/browser-v2/wheel-calibration/cancel", json={}).status_code == 202
    rejected = client.post(
        "/api/browser-v2/wheel-calibration/start",
        json={"profile_token": "public-token", "target_url": "https://www.tiktok.com/", "raw_id": "secret"},
    )
    assert rejected.status_code == 400
```

- [ ] **Step 2: 运行路由测试并确认失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_routes.py -q -p no:cacheprovider
```

Expected: FAIL，三个路径返回 404。

- [ ] **Step 3: 实现路由**

```python
@blueprint.get("/wheel-calibration")
def get_wheel_calibration():
    return _data(_call(service(), "get_wheel_calibration"))


@blueprint.post("/wheel-calibration/start")
def start_wheel_calibration():
    payload = _json_object(
        allowed={"profile_token", "target_url"},
        required={"profile_token", "target_url"},
    )
    _non_empty_string(payload["profile_token"])
    _non_empty_string(payload["target_url"])
    return _data(_call(service(), "start_wheel_calibration", **payload), 202)


@blueprint.post("/wheel-calibration/cancel")
def cancel_wheel_calibration():
    _json_object(allowed=set(), required=set())
    return _data(_call(service(), "cancel_wheel_calibration"), 202)
```

在固定错误映射中加入六个校准错误；仍由 `_error_status()` 把 `WheelCalibrationError` 映射为 422，把租约冲突映射为 409，不返回异常原文。

- [ ] **Step 4: 运行 Task 4 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_routes.py tests\test_execution_v2_service.py -q -p no:cacheprovider
```

Expected: PASS。

- [ ] **Step 5: 记录预期提交（当前环境不执行）**

```powershell
git add execution_v2/blueprint.py tests/test_execution_v2_routes.py
git commit -m "feat: expose wheel calibration API"
```

---

### Task 5: 校准快照预检和单事件组回放

**Files:**
- Modify: `execution_v2/wheel_calibration.py`
- Modify: `execution_v2/service.py:443-470`
- Modify: `execution_v2/executor.py:24-118,175-181`
- Modify: `execution_v2/actions.py:16-65`
- Modify: `tests/test_execution_v2_wheel_calibration.py`
- Modify: `tests/test_execution_v2_actions.py`
- Modify: `tests/test_execution_v2_executor.py`
- Modify: `tests/test_execution_v2_service.py`

**Interfaces:**
- Produces: `execute_calibrated_switches(page, calibration, *, direction, requested, interval_range, rng, sleep_fn) -> dict`。
- Changes: `execute_action(..., wheel_calibration: dict[str, Any] | None = None)`。
- Changes: `StrategyExecutor.run()` 从 `strategy_snapshot["wheel_calibration"]` 读取固定版本。

- [ ] **Step 1: 写回放和预检失败测试**

```python
@pytest.mark.asyncio
async def test_calibrated_switch_dispatches_exactly_one_recorded_group(monkeypatch):
    page = FakePage(states=["a", "b", "b"])
    calibration = {
        "revision": 4,
        "direction": "down",
        "events": [
            {"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}
        ],
    }
    result = await execute_calibrated_switches(
        page, calibration, direction="down", requested=1,
        interval_range=[0.2, 0.2], rng=FixedRng(), sleep_fn=no_sleep,
    )
    assert page.mouse.wheels == [(0.0, 100.0)]
    assert result["completed_switches"] == 1
    assert result["calibration_revision"] == 4


def test_job_with_scroll_fails_before_profile_lease_without_calibration(tmp_path):
    service = make_service(tmp_path)
    strategy_id = create_scroll_strategy(service.store)
    token = service.list_profiles()[0]["profile_token"]
    with pytest.raises(WheelCalibrationError, match="wheel_calibration_missing"):
        service.start_job(strategy_id, [token], 1)
    assert service.adspower.started == []
```

- [ ] **Step 2: 运行测试并确认失败**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py tests\test_execution_v2_actions.py tests\test_execution_v2_executor.py tests\test_execution_v2_service.py -q -p no:cacheprovider
```

Expected: FAIL，动作仍调用旧固定脉冲 helper，任务快照没有校准。

- [ ] **Step 3: 实现一次事件组回放**

```python
async def execute_calibrated_switches(page, calibration, *, direction, requested, interval_range, rng, sleep_fn):
    events = calibration["events"]
    completed = 0
    wheel_events = 0
    records = []
    for index in range(requested):
        before = await capture_feed_state(page)
        await page.mouse.move(
            before.container_x + before.container_width / 2,
            before.container_y + before.container_height / 2,
        )
        for event in events:
            if event["delay_ms"] > 0:
                await sleep_fn(event["delay_ms"] / 1000.0)
            delta_y = event["delta_y"] if direction == "down" else -event["delta_y"]
            await page.mouse.wheel(event["delta_x"], delta_y)
            wheel_events += 1
        try:
            after = await observe_single_transition(page, before, timeout=8.0, sleep_fn=sleep_fn)
        except WheelCalibrationError as error:
            if error.code == "wheel_calibration_video_not_changed":
                raise WheelCalibrationError("calibrated_video_switch_not_observed") from error
            raise
        completed += 1
        records.append({"from": before.safe_fingerprint, "to": after.safe_fingerprint, "wheel_events": len(events)})
        if index + 1 < requested:
            await sleep_fn(rng.uniform(*interval_range))
    return {
        "requested_switches": requested,
        "completed_switches": completed,
        "wheel_events": wheel_events,
        "switches": records,
        "calibration_revision": calibration["revision"],
    }
```

该函数没有重试循环；任何观察失败立即抛错。

- [ ] **Step 4: 在任务创建时固定校准版本**

在 `_start_job()` 的 `build_execution_snapshot()` 后、取得 Profile 租约前执行：

```python
actions = snapshot.get("strategy", {}).get("actions", [])
if any(action.get("type") == "scroll" for action in actions):
    calibration = self.store.get_wheel_calibration("tiktok_feed")
    if calibration is None:
        raise WheelCalibrationError("wheel_calibration_missing")
    snapshot["wheel_calibration"] = calibration
```

这样一个任务内所有 Profile 使用同一 revision；重新校准只影响后续新任务。

- [ ] **Step 5: 把快照传入动作**

`execute_action()` 增加关键字参数 `wheel_calibration=None`；`_scroll()` 调用 `execute_calibrated_switches()` 并把 `calibration_revision`、`switches`、`wheel_events` 放入结果。`StrategyExecutor.run()` 每次执行 action 都传同一个 `strategy_snapshot.get("wheel_calibration")`。

```python
result = await self._action_executor(
    binding.page,
    action,
    elements,
    self._resolver,
    self._text_resolver,
    rng=self._rng,
    sleep=self._sleep,
    wheel_calibration=strategy_snapshot.get("wheel_calibration"),
)
```

- [ ] **Step 6: 运行 Task 5 测试**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py tests\test_execution_v2_actions.py tests\test_execution_v2_executor.py tests\test_execution_v2_service.py tests\test_execution_v2_scheduler.py -q -p no:cacheprovider
```

Expected: PASS；断言回放失败后 `page.mouse.wheels` 数量仍等于一个校准事件组长度。

- [ ] **Step 7: 记录预期提交（当前环境不执行）**

```powershell
git add execution_v2/wheel_calibration.py execution_v2/service.py execution_v2/executor.py execution_v2/actions.py tests/test_execution_v2_wheel_calibration.py tests/test_execution_v2_actions.py tests/test_execution_v2_executor.py tests/test_execution_v2_service.py
git commit -m "feat: replay calibrated video switches"
```

---

### Task 6: 页面点选器中的滚轮校准 UI

**Files:**
- Modify: `gateway/templates/browser_v2.html:34-45`
- Modify: `gateway/static/browser_v2.js:114-207,216-294,419-483`
- Modify: `gateway/static/browser_v2.css`
- Modify: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: Task 4 三个 HTTP 接口。
- Produces: `state.wheelCalibration = {current, active}`。
- Produces: `startWheelCalibration()`、`cancelCurrentPickerOperation()`、`renderWheelCalibration()`。

- [ ] **Step 1: 写 UI 失败测试**

```javascript
test("wheel calibration reuses picker profile and renders three samples", async () => {
  const ui = harness();
  ui.responses["GET /api/browser-v2/wheel-calibration"] = response(200, {
    current: null,
    active: {status: "waiting_for_sample", sample_index: 1, samples: ["passed", "waiting", "pending"]}
  });
  await ui.app.init();
  assert.equal(ui.app.state.wheelCalibration.active.sample_index, 1);
  assert.match(ui.document.querySelector("#v2-wheel-calibration-state").textContent, /第 2\/3 次/);
});


test("picker and wheel calibration buttons are mutually exclusive", async () => {
  const ui = harness();
  ui.app.state.wheelCalibration = {current: null, active: {status: "waiting_for_sample"}};
  ui.app.render();
  assert.equal(ui.document.querySelector("#v2-picker-start").disabled, true);
  assert.equal(ui.document.querySelector("#v2-wheel-calibration-start").disabled, true);
});
```

- [ ] **Step 2: 运行 UI 测试并确认失败**

```powershell
node --test --test-name-pattern="wheel calibration|mutually exclusive" tests-js\browser-v2-ui.test.js
```

Expected: FAIL，节点和状态尚不存在。

- [ ] **Step 3: 添加最小页面结构**

在点选器按钮区加入：

```html
<button id="v2-wheel-calibration-start" class="v2-button" type="button" disabled>滚轮校准</button>
```

把取消按钮文案改为“取消当前操作”，并在当前点选卡片下加入：

```html
<section id="v2-wheel-calibration" class="v2-calibration" aria-labelledby="v2-wheel-calibration-title">
  <h3 id="v2-wheel-calibration-title">滚轮校准</h3>
  <p id="v2-wheel-calibration-state" class="v2-muted">尚未校准</p>
  <ol id="v2-wheel-calibration-samples" class="v2-calibration-samples"></ol>
  <p id="v2-wheel-calibration-version" class="v2-muted"></p>
</section>
```

- [ ] **Step 4: 接入状态、轮询和互斥**

初始化并行 GET 校准状态；`syncPolling()` 在 active status 属于 `preparing/waiting_for_sample/validating/cancelling` 时继续轮询。启动请求严格复用 `state.pickerProfileToken` 和 `#v2-picker-url`：

```javascript
async function startWheelCalibration() {
  if (activePicker() || activeWheelCalibration() || state.submitting) return false;
  const profile = el("#v2-picker-profile").value;
  const targetUrl = el("#v2-picker-url").value.trim();
  if (!profile || !targetUrl) {
    setMessage("请选择测试 Profile 并填写网址");
    return false;
  }
  state.pickerProfileToken = profile;
  state.submitting = true;
  const result = await request(API_PREFIX + "/wheel-calibration/start", "POST", {
    profile_token: profile,
    target_url: targetUrl,
  });
  state.submitting = false;
  if (!success(result, [202])) {
    setMessage(errorMessage(result, "启动滚轮校准失败"));
    render();
    return false;
  }
  state.wheelCalibration = {current: state.wheelCalibration.current, active: result.data};
  setMessage("请将鼠标放在视频区域，向下滚动一格");
  render();
  syncPolling();
  return true;
}
```

“取消当前操作”优先取消 active picker，否则 POST calibration cancel。终态停止轮询；重新 render 不得清空 Profile 选择。

- [ ] **Step 5: 添加三格状态样式**

```css
.v2-calibration-samples {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  padding: 0;
  list-style: none;
}
.v2-calibration-sample {
  border: 1px solid var(--v2-border);
  border-radius: 10px;
  padding: 10px;
}
```

沿用现有成功、危险和 muted 色，不新增设计系统。

- [ ] **Step 6: 运行 Task 6 测试**

```powershell
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: PASS。

- [ ] **Step 7: 记录预期提交（当前环境不执行）**

```powershell
git add gateway/templates/browser_v2.html gateway/static/browser_v2.js gateway/static/browser_v2.css tests-js/browser-v2-ui.test.js
git commit -m "feat: add wheel calibration to picker"
```

---

### Task 7: 全量回归与真实 1+2 Profile 验收

**Files:**
- Modify only if a failing test identifies a defect in files already listed above.
- Record evidence under existing runtime location: `data/execution_v2/evidence/`.

**Interfaces:**
- Consumes: Tasks 1-6 全部接口。
- Produces: 可重复的自动测试结果和一次真实校准、两个 Profile 并行执行记录。

- [ ] **Step 1: 运行 Python 聚焦回归**

```powershell
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_wheel_calibration.py tests\test_execution_v2_store.py tests\test_execution_v2_routes.py tests\test_execution_v2_service.py tests\test_execution_v2_actions.py tests\test_execution_v2_executor.py tests\test_execution_v2_scheduler.py tests\test_browser_video_switch.py -q -p no:cacheprovider
```

Expected: PASS，无新增 warning。

- [ ] **Step 2: 运行 V2 前端回归**

```powershell
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: PASS。

- [ ] **Step 3: 运行语法和差异检查**

```powershell
& .\.venv\Scripts\python.exe -m py_compile execution_v2\wheel_calibration.py execution_v2\store.py execution_v2\service.py execution_v2\blueprint.py execution_v2\actions.py execution_v2\executor.py
git diff --check
```

Expected: 两条命令退出码 0；如果 `.git` 只读仍可执行只读 `git diff --check`。

- [ ] **Step 4: 真实单 Profile 校准**

启动当前项目，进入 `/browser-v2` → “元素库”，选择一个独立测试 Profile，目标网址保持 `https://www.tiktok.com/`，点击“滚轮校准”。按页面提示真实向下滚动一格三次，每次等待状态显示“通过”后再操作下一次。

Expected:

```text
第 1 次：通过
第 2 次：通过
第 3 次：通过
当前有效版本：revision 1（或递增版本）
```

浏览器窗口自动关闭，页面无原始 Profile ID 或 WebSocket。

- [ ] **Step 5: 真实两个 Profile 并行回放**

创建只包含“等待 → 向下滚动 1 次 → 等待”的 V2 策略，选择两个测试 Profile、批次设为 2 后运行。

Expected:

- 两个窗口各自只切换一个视频。
- 不出现连续快速滚轮和视频抖动。
- 历史记录显示同一 `calibration_revision`、`requested_switches=1`、`completed_switches=1`。
- 若其中一个 Profile 失败，另一个仍完成；失败窗口只出现 `calibrated_video_switch_not_observed` 或固定校准错误。
- 批次结束后两个窗口都关闭并确认释放。

- [ ] **Step 6: 验证失败不覆盖和失败不补发**

启动重新校准后在第二次采样故意不滚动，等待失败或取消；确认当前有效 revision 不变。随后用测试替身运行一个不会改变视频身份的页面，确认 wheel 调用数量严格等于一个校准事件组长度。

- [ ] **Step 7: 记录预期最终提交（当前环境不执行）**

```powershell
git add execution_v2 gateway/templates/browser_v2.html gateway/static/browser_v2.js gateway/static/browser_v2.css tests tests-js docs/superpowers/specs/2026-08-06-browser-v2-wheel-calibration-design.md docs/superpowers/plans/2026-08-06-browser-v2-wheel-calibration.md
git commit -m "feat: calibrate TikTok video wheel input"
```

---

### Task 8: 最小修复三次成功后的误判

**Files:**
- Modify: `execution_v2/wheel_calibration.py`
- Modify: `gateway/static/browser_v2.js`
- Test: `tests/test_execution_v2_wheel_calibration.py`
- Test: `tests-js/browser-v2-ui.test.js`

- [x] **Step 1: 增加失败测试**

覆盖三组事件数量不同、但均只切换一个视频且总位移在中位数 20% 内的样本；预期校准成功，并完整保留最接近中位数的真实事件组。覆盖失败状态中文提示、上一稳定版本提示和脱敏样本指标。

- [x] **Step 2: 最小修改归一化规则**

删除“事件数量必须完全一致”的限制；保留方向、单视频切换、`deltaMode=0`、非空事件和总位移 20% 一致性校验；从三组真实样本中选择总位移最接近中位数的一整组，不合成新波形。

- [x] **Step 3: 暴露脱敏诊断与改进 UI 文案**

运行态仅增加每次采样的 `event_count` 与 `total_delta`。把单次 `passed` 显示为“视频切换成功”；最终波形不一致时显示“三次均切换成功，但滚轮数据差异过大”，并明确继续使用上一稳定版本。

- [x] **Step 4: 聚焦回归**

```powershell
python -m pytest tests/test_execution_v2_wheel_calibration.py tests/test_execution_v2_service.py tests/test_execution_v2_routes.py -q -p no:cacheprovider
node --test tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: 全部 PASS；不新增接口、数据表或回放重试。
