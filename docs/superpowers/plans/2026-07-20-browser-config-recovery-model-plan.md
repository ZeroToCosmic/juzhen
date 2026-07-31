# 浏览器接管、配置恢复与模型选择 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 移除无用总览，修复配置被局部保存清空的问题，恢复 R2，增加可扩展模型下拉，并让“执行选中策略”自动启动、平铺、导航和执行选中窗口。

**Architecture:** 配置层继续使用一个 JSON 文件，但明确区分完整保存和局部更新，并在覆盖前建立可恢复备份。浏览器生命周期抽到独立编排模块，`open-tile` 和 `execute-strategy` 复用同一会话启动逻辑；模型预设通过独立注册表和只读 API 暴露给 GUI，保留现有 `models.items[]` 数据结构。

**Tech Stack:** Python 3、Flask、requests、Playwright/CDP、AdsPower Local API、原生 JavaScript、pytest、Node test runner、Windows pywin32。

## Global Constraints

- 主 GUI 删除“总览”并默认进入“集中配置”。
- “执行选中策略”必须自动处理启动、CDP 等待、平铺、TikTok 导航和策略执行。
- 单个窗口运行失败是窗口级结果，HTTP 200 返回；只有请求、URL 或策略定义整体无效时返回 HTTP 400。
- 默认目标网址是 `https://www.tiktok.com/`，但以集中配置的 `browser.default_url` 为准。
- 最多同时处理 8 个窗口；单个 Profile 最多进行 3 次启动/连接尝试。
- GUI 和日志不得显示完整 ws.puppeteer、AdsPower API Key、R2 密钥或模型 API Key。
- 继续兼容 `models.default_model_id` 和 `models.items[]` 的现有字段。
- R2 Public Base URL 与 Prefix 没有可靠历史值，保持为空并标记为可选。
- R2 凭据恢复是 controller-only 安全步骤：从用户在本任务会话中已提供的值恢复，不把明文写入计划、任务简报、报告、日志或测试。
- 所有生产代码修改必须先有失败测试，并验证红灯原因后再实现。

---

### Task 1: 修复局部配置保存并增加版本备份

**Files:**
- Modify: `gateway/settings_store.py`
- Modify: `gateway/app.py`（`save_browser_action_config`、`save_auto_strategies_route`）
- Test: `tests/test_settings_store.py`
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Consumes: 现有 `load_settings(path=None)`、`save_settings(settings, path=None)`、`update_settings(updates, path=None)`。
- Produces: `list_config_backups(path=None) -> list[Path]`、`restore_latest_backup(path=None) -> dict`；局部接口只调用 `update_settings()`。

- [ ] **Step 1: 写失败测试，证明局部策略保存会清空其他配置**

在 `tests/test_settings_routes.py` 增加：

```python
def test_browser_action_and_auto_strategy_updates_preserve_other_settings(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings({
        "r2": {"bucket": "tiktokvideo", "access_key_id": "access-1"},
        "adspower": {"api_key": "ads-key"},
        "proxy": {"host": "proxy.example"},
    }, config_path)
    client = create_app().test_client()

    action_response = client.put(
        "/api/browser/action-config",
        json={"elements": {"submit": "//button"}, "strategies": []},
    )
    auto_response = client.put(
        "/api/browser/auto-strategies",
        json={"strategies": []},
    )

    saved = load_settings(config_path)
    assert action_response.status_code == 200
    assert auto_response.status_code == 200
    assert saved["r2"]["bucket"] == "tiktokvideo"
    assert saved["adspower"]["api_key"] == "ads-key"
    assert saved["proxy"]["host"] == "proxy.example"
```

- [ ] **Step 2: 运行测试并确认红灯来自局部接口调用 `save_settings(partial)`**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_settings_routes.py::test_browser_action_and_auto_strategy_updates_preserve_other_settings -p no:cacheprovider
```

Expected: FAIL，R2、AdsPower 或 proxy 被默认值覆盖。

- [ ] **Step 3: 把两个局部路由改为深度合并更新**

在 `gateway/app.py` 中使用现有别名 `merge_saved_settings`：

```python
settings = merge_saved_settings(
    {"browser": {"action_elements": elements, "action_strategies": strategies}}
)
```

```python
settings = merge_saved_settings({"browser": {"auto_strategies": strategies}})
```

- [ ] **Step 4: 写失败测试，定义备份保留和恢复行为**

在 `tests/test_settings_store.py` 增加：

```python
def test_save_creates_bounded_backups_and_restore_latest(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings({"browser": {"task_goal": "first"}}, config_path)
    for index in range(7):
        update_settings({"browser": {"task_goal": f"version-{index}"}}, config_path)

    backups = list_config_backups(config_path)
    assert len(backups) == 5
    restored = restore_latest_backup(config_path)
    assert restored["browser"]["task_goal"] == "version-5"
```

- [ ] **Step 5: 运行备份测试确认函数尚不存在或行为不满足**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_settings_store.py::test_save_creates_bounded_backups_and_restore_latest -p no:cacheprovider
```

Expected: FAIL，缺少 `list_config_backups`/`restore_latest_backup`。

- [ ] **Step 6: 实现覆盖前备份、保留五份和最近备份恢复**

在 `gateway/settings_store.py` 增加：

```python
CONFIG_BACKUP_LIMIT = 5

def list_config_backups(path=None) -> list[Path]:
    config_path = get_config_path(path)
    return sorted(
        config_path.parent.glob(f"{config_path.name}.backup.*"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )

def _backup_current_config(config_path: Path) -> Path | None:
    if not config_path.exists():
        return None
    stamp = time.strftime("%Y%m%d%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.backup.{stamp}.{time.time_ns()}")
    shutil.copy2(config_path, backup)
    for stale in list_config_backups(config_path)[CONFIG_BACKUP_LIMIT:]:
        stale.unlink(missing_ok=True)
    return backup

def restore_latest_backup(path=None) -> dict:
    config_path = get_config_path(path)
    backups = list_config_backups(config_path)
    if not backups:
        raise FileNotFoundError("没有可恢复的配置备份")
    _backup_current_config(config_path)
    shutil.copy2(backups[0], config_path)
    return _load_settings(config_path)
```

在 `_save_settings()` 的原子替换之前调用 `_backup_current_config(config_path)`。

- [ ] **Step 7: 扩展空值保护并运行配置测试**

将 `public_base_url`、`prefix` 加入 `_PRESERVE_BLANK_KEYS`，然后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_settings_store.py tests\test_settings_routes.py -p no:cacheprovider
```

Expected: PASS。

### Task 2: 增加配置健康状态、恢复 API 和 R2 安全恢复

**Files:**
- Modify: `gateway/settings_store.py`
- Modify: `gateway/app.py`
- Modify: `config.json`（controller-only，禁止在任务报告中记录密钥）
- Test: `tests/test_settings_store.py`
- Test: `tests/test_settings_routes.py`
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: Task 1 的 `list_config_backups()`、`restore_latest_backup()`、`update_settings()`。
- Produces: `get_config_health(path=None) -> dict`、`GET /api/settings/status`、`POST /api/settings/restore-latest`。

- [ ] **Step 1: 写失败测试，定义损坏配置的健康状态和恢复接口**

```python
def test_invalid_config_reports_health_and_can_restore_latest(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings({"r2": {"bucket": "tiktokvideo"}}, config_path)
    update_settings({"browser": {"task_goal": "saved"}}, config_path)
    config_path.write_text('{"broken":', encoding="utf-8")
    client = create_app().test_client()

    status = client.get("/api/settings/status").get_json()
    restored = client.post("/api/settings/restore-latest").get_json()

    assert status["ok"] is False
    assert status["backup_available"] is True
    assert restored["settings"]["r2"]["bucket"] == "tiktokvideo"
```

- [ ] **Step 2: 运行测试确认路由缺失**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_settings_routes.py::test_invalid_config_reports_health_and_can_restore_latest -p no:cacheprovider
```

Expected: FAIL，status/restore 路由不存在。

- [ ] **Step 3: 实现健康状态并避免损坏配置被空默认覆盖**

在 `settings_store.py` 维护每个配置路径的最近读取错误：

```python
_CONFIG_LOAD_ERRORS: dict[str, str] = {}

def get_config_health(path=None) -> dict:
    config_path = get_config_path(path)
    error = _CONFIG_LOAD_ERRORS.get(str(config_path), "")
    backups = list_config_backups(config_path)
    return {
        "ok": not bool(error),
        "error": error,
        "backup_available": bool(backups),
        "latest_backup": backups[0].name if backups else "",
    }
```

JSON 解析失败时记录错误；成功读取或恢复后清除错误。GUI 在健康状态失败时禁止保存，防止默认空值覆盖损坏文件。

- [ ] **Step 4: 增加状态/恢复 API 与 GUI 按钮**

在 `gateway/app.py` 增加：

```python
@app.get("/api/settings/status")
def settings_status_route():
    return jsonify(get_config_health())

@app.post("/api/settings/restore-latest")
def restore_settings_route():
    try:
        return jsonify({"settings": restore_latest_backup(), "status": get_config_health()})
    except FileNotFoundError as error:
        return jsonify({"error": str(error)}), 404
```

集中配置区增加 `id="settings-restore-latest"` 按钮和 `id="settings-health-status"` 状态文字。页面加载先查询 `/api/settings/status`；状态失败时显示恢复按钮并阻止保存。

- [ ] **Step 5: controller-only 恢复已知 R2 配置**

主控制器先确认 `config.json` 已生成备份，再使用 `update_settings({"r2": ...})` 写入用户此前提供的 Account ID、Account Token、Access Key ID、Secret Access Key、Bucket `tiktokvideo` 和 S3 Endpoint。`public_base_url` 与 `prefix` 写为空。不得把凭据传给子代理、测试、计划、报告或日志。

- [ ] **Step 6: 验证恢复后字段存在但不输出内容**

使用只输出布尔值和长度的检查，确认六个已知字段非空；不得打印值。重载后再次检查。

- [ ] **Step 7: 运行配置与页面测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_settings_store.py tests\test_settings_routes.py tests\test_console.py -p no:cacheprovider
```

Expected: PASS。

### Task 3: 建立模型预设注册表和两级下拉

**Files:**
- Create: `gateway/model_presets.py`
- Modify: `gateway/app.py`
- Test: `tests/test_model_presets.py`
- Test: `tests/test_console.py`
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Produces: `public_model_presets() -> dict[str, dict]`、`GET /api/model-presets`。
- Preserves: `models.items[].provider/base_url/model/mode/api_key/enabled`。

- [ ] **Step 1: 写失败测试定义注册表内容**

```python
def test_public_model_presets_include_grok_deepseek_and_custom():
    presets = public_model_presets()
    assert set(presets) == {"grok", "deepseek", "custom"}
    assert presets["grok"]["default_mode"] == "responses"
    assert presets["deepseek"]["default_mode"] == "chat"
    assert [item["id"] for item in presets["deepseek"]["models"]] == [
        "deepseek-chat", "deepseek-reasoner"
    ]
    assert "api_key" not in str(presets)
```

- [ ] **Step 2: 运行测试确认模块缺失**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_model_presets.py -p no:cacheprovider
```

Expected: FAIL with import error。

- [ ] **Step 3: 创建无密钥模型注册表**

`gateway/model_presets.py`：

```python
MODEL_PRESETS = {
    "grok": {
        "label": "Grok",
        "default_base_url": "https://api.x.ai/v1",
        "default_mode": "responses",
        "models": [{"id": "grok-4.5", "label": "Grok 4.5"}],
    },
    "deepseek": {
        "label": "DeepSeek",
        "default_base_url": "https://api.deepseek.com/v1",
        "default_mode": "chat",
        "models": [
            {"id": "deepseek-chat", "label": "DeepSeek Chat"},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner"},
        ],
    },
    "custom": {
        "label": "自定义",
        "default_base_url": "",
        "default_mode": "chat",
        "models": [],
    },
}

def public_model_presets():
    return MODEL_PRESETS
```

- [ ] **Step 4: 增加只读预设 API 和两级下拉 HTML**

增加 `/api/model-presets`。将 `models.items.0.model` 的可见文本输入替换为：

```html
<select id="model-provider-select" name="models.items.0.provider"></select>
<select id="model-preset-select"></select>
<input id="model-custom-name" name="models.items.0.model" autocomplete="off">
```

保留 `base_url`、`api_key`、`mode` 和 `enabled` 字段。自定义输入只在 Custom 或“自定义模型”选项下显示。

- [ ] **Step 5: 实现联动且不覆盖 API Key**

```javascript
async function loadModelPresets() {
  const response = await fetch("/api/model-presets");
  modelPresets = await response.json();
  renderModelProviderOptions();
  syncModelPresetFields({preserveApiKey: true});
}
```

`syncModelPresetFields()` 只更新 `base_url`、`mode` 和模型名称，永不修改 `models.items.0.api_key`。

- [ ] **Step 6: 增加页面/API 测试并运行**

测试断言供应商下拉、模型下拉、自定义输入和 API；再运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_model_presets.py tests\test_console.py tests\test_settings_routes.py -p no:cacheprovider
```

Expected: PASS。

### Task 4: 抽取 AdsPower 会话启动编排

**Files:**
- Modify: `adspower.py`
- Create: `gateway/browser_orchestrator.py`
- Test: `tests/test_adspower.py`
- Create: `tests/test_browser_orchestrator.py`

**Interfaces:**
- Produces: `AdsPowerController.get_browser_active(profile_id) -> dict`。
- Produces: `ensure_profile_session(profile, current_ws, controller, wait_for_cdp, retries=3, sleep_fn=time.sleep) -> dict`。
- Result internal shape: `profile_id/profile_no/name/status/stage/attempts/ws_url/error`；公开响应前移除 `ws_url`。

- [ ] **Step 1: 写失败测试定义已有会话复用和三次启动**

```python
def test_ensure_profile_session_reuses_healthy_ws():
    result = ensure_profile_session(
        {"profile_id": "p1"}, "ws://ready", FakeController(),
        wait_for_cdp=lambda ws, timeout: True,
    )
    assert result["status"] == "ready"
    assert result["stage"] == "session_check"
    assert result["ws_url"] == "ws://ready"

def test_ensure_profile_session_retries_three_times_and_reports_stage():
    controller = FailingController()
    result = ensure_profile_session(
        {"profile_id": "p1"}, "", controller,
        wait_for_cdp=lambda ws, timeout: (_ for _ in ()).throw(TimeoutError("not ready")),
        sleep_fn=lambda _seconds: None,
    )
    assert result["status"] == "failed"
    assert result["stage"] == "wait_for_cdp"
    assert result["attempts"] == 3
```

- [ ] **Step 2: 运行测试确认模块/方法缺失**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_adspower.py tests\test_browser_orchestrator.py -p no:cacheprovider
```

Expected: FAIL。

- [ ] **Step 3: 增加 AdsPower 活跃状态查询**

在 `adspower.py`：

```python
def get_browser_active(self, profile_id: str) -> dict[str, Any]:
    profile_id = str(profile_id or "").strip()
    if not profile_id:
        raise ValueError("profile_id 不能为空")
    return self._request_with_retry("/api/v1/browser/active", profile_id)
```

- [ ] **Step 4: 实现独立会话编排器**

`ensure_profile_session()` 先检查已有 ws；启动失败或 CDP 超时后调用 `get_browser_active()` 收集状态，并在下一次尝试前停止残留 Profile。最后一次失败返回结构化结果，不抛出窗口级异常。内部保留 ws 供后续执行，但错误字符串不得拼接完整 ws。

- [ ] **Step 5: 覆盖空 ws、失效会话、启动成功和状态查询失败**

新增测试后运行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_adspower.py tests\test_browser_orchestrator.py -p no:cacheprovider
```

Expected: PASS。

### Task 5: 让执行策略自动启动、平铺、导航并隔离失败

**Files:**
- Modify: `gateway/app.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: Task 4 的 `ensure_profile_session()`。
- Preserves: `prepare_browser_page(ws_url, target_url)` 和现有自动/手动策略运行器。
- Produces: execute-strategy HTTP 200 窗口级结果及统一 `task_id`。

- [ ] **Step 1: 写失败测试，未打开窗口点击执行策略会自动启动**

```python
def test_execute_strategy_auto_starts_selected_profiles(monkeypatch, tmp_path):
    # 保存有效策略与 TikTok 默认地址；ACTIVE_BROWSER_SESSIONS 保持为空。
    # mock ensure_profile_session 返回 ready ws，mock prepare/run 记录调用。
    response = client.post("/api/browser/execute-strategy", json={
        "strategy_id": "auto:auto-demo",
        "windows": [{"profile_id": "p1", "profile_no": "1", "name": "buffer1"}],
    })
    data = response.get_json()
    assert response.status_code == 200
    assert data["results"][0]["status"] == "ok"
    assert data["results"][0]["target_url"] == "https://www.tiktok.com/"
```

- [ ] **Step 2: 写失败测试，单窗口失败不阻塞其他窗口**

```python
def test_execute_strategy_keeps_running_when_one_profile_fails_to_start():
    response = client.post("/api/browser/execute-strategy", json={
        "strategy_id": "auto:auto-demo",
        "windows": [{"profile_id": "bad"}, {"profile_id": "good"}],
    })
    data = response.get_json()
    assert response.status_code == 200
    assert [(item["profile_id"], item["status"]) for item in data["results"]] == [
        ("bad", "failed"), ("good", "ok")
    ]
```

- [ ] **Step 3: 运行两个测试确认当前返回 400**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_app.py -k "auto_starts_selected_profiles or keeps_running_when_one_profile_fails" -p no:cacheprovider
```

Expected: FAIL，当前路由要求预先存在 ACTIVE_BROWSER_SESSIONS。

- [ ] **Step 4: 在路由中统一建立会话并登记成功项**

新增内部辅助函数：

```python
def ensure_selected_browser_sessions(selected, controller):
    # 对每个 Profile 调用 ensure_profile_session；成功项写入
    # ACTIVE_BROWSER_SESSIONS，返回 ready 与 failed 两组结构化结果。
```

自动/手动策略在校验策略存在后调用此函数。成功会话执行 `tile_browser_windows()`，平铺失败记录为阶段结果但不阻止后续页面准备。

- [ ] **Step 5: 执行成功会话的页面准备和策略，合并原顺序结果**

每个窗口结果都包含：

```python
{
    "profile_id": profile_id,
    "status": "ok" or "failed",
    "stage": "execute_strategy" or failure_stage,
    "attempts": attempts,
    "target_url": target_url,
}
```

响应增加 `task_id`。写日志前使用清理函数移除 `ws_url`。

- [ ] **Step 6: 让 `/api/browser/open-tile` 复用同一启动逻辑**

移除重复的两次尝试 `open_one()`，改为 Task 4 编排器的三次尝试。保持现有 `results/layout/navigation` 兼容字段。

- [ ] **Step 7: 更新前端直接显示窗口级阶段原因**

`executeBrowserStrategy()` 对 HTTP 400 显示 `result.data.error`；HTTP 200 时逐个显示 `profile_id + stage + error`。不展示 ws 地址。

- [ ] **Step 8: 运行浏览器路由回归测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_app.py tests\test_browser_cdp.py tests\test_window_tiler.py -p no:cacheprovider
```

Expected: PASS。

### Task 6: 移除总览并默认进入集中配置

**Files:**
- Modify: `gateway/app.py`
- Test: `tests/test_console.py`
- Test: `tests/test_settings_routes.py`

**Interfaces:**
- Preserves: `/api/status`、账号获取 API 及其他页面对它们的调用。
- Produces: 初始 active panel 为 `settings`。

- [ ] **Step 1: 写失败测试定义页面结构**

```python
def test_dashboard_removes_overview_and_opens_settings_by_default():
    page = create_app().test_client().get("/").get_data(as_text=True)
    assert 'data-panel="overview"' not in page
    assert 'id="panel-overview"' not in page
    assert 'id="overview-next"' not in page
    assert 'id="overview-status"' not in page
    assert '<button class="active" data-panel="settings">集中配置</button>' in page
    assert '<section class="panel active" id="panel-settings">' in page
```

- [ ] **Step 2: 运行测试确认总览仍存在**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_console.py::test_dashboard_removes_overview_and_opens_settings_by_default -p no:cacheprovider
```

Expected: FAIL。

- [ ] **Step 3: 删除总览 HTML 和专属 JS**

删除总览导航、`panel-overview`、`overview-next`、`overview-status` 绑定。把集中配置按钮和面板设为 active，把初始标题改为“集中配置”。删除前先用 `rg` 确认专属函数是否被其他页面调用；保留共享 API 与共享刷新函数。

- [ ] **Step 4: 运行页面回归测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_console.py tests\test_settings_routes.py -p no:cacheprovider
```

Expected: PASS。

### Task 7: 集成验证与安全交付

**Files:**
- Test: `tests/`
- Test: `tests-js/`
- Verify: `config.json`（只检查字段存在性和长度）
- Verify: `logs/browser_operations.jsonl`

**Interfaces:**
- Consumes: Task 1–6 全部结果。
- Produces: 可复现测试证据和真实 AdsPower 手工验收说明。

- [ ] **Step 1: 运行 Python 全量测试**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests -p no:cacheprovider
```

Expected: 全部 PASS，0 failed。

- [ ] **Step 2: 运行 Node 测试**

```powershell
node --test "tests-js/*.test.js"
```

Expected: 0 failed。

- [ ] **Step 3: 运行语法检查**

```powershell
.\.venv\Scripts\python.exe -m py_compile gateway\app.py gateway\settings_store.py gateway\model_presets.py gateway\browser_orchestrator.py adspower.py browser_cdp.py browser_strategy_runtime.py window_tiler.py
```

Expected: exit 0；记录但不扩大处理现有模板字符串 SyntaxWarning。

- [ ] **Step 4: 安全检查配置和日志**

确认 `config.json` 的已知 R2 字段为非空，但命令只输出布尔值与长度。使用 `rg` 检查新日志和 HTML 不包含 `secret_access_key` 的实际值、完整 ws 或 API Key。

- [ ] **Step 5: 真实环境手工验收**

1. 启动 AdsPower 并确认 Local API 可用。
2. 选择 2–4 个 Profile，不先点击“打开窗口”。
3. 点击“执行选中策略”。
4. 确认成功窗口被启动、平铺、关闭旧 Tab、进入 TikTok 并执行策略。
5. 人为选择一个无法启动的 Profile，确认其他窗口继续执行且 GUI 显示该 Profile 的失败阶段。
6. 重启 GUI，确认 R2 和模型选择仍能回填。

- [ ] **Step 6: 凭据轮换提示**

交付说明必须提示：R2 API Token 和 S3 访问密钥曾经出现在会话明文中，建议在 Cloudflare 创建新凭据、更新 GUI 后撤销旧凭据。

## Completion Criteria

- [ ] 主 GUI 不再显示总览，默认进入集中配置。
- [ ] 局部保存动作/策略不会清空 R2、AdsPower、代理或模型配置。
- [ ] 配置覆盖前自动备份，GUI 可从最近备份恢复。
- [ ] 已知 R2 字段恢复且重启后仍存在；未知公开域名和前缀保持空。
- [ ] 模型界面可选择 Grok、DeepSeek 和 Custom，并通过注册表扩展。
- [ ] 执行策略自动启动未打开窗口；单窗口失败不再引发整体 400。
- [ ] 成功窗口进入 TikTok 后才执行策略，blank 页面不会执行。
- [ ] Python、Node 和语法检查通过，真实 AdsPower 验收结果被记录。
