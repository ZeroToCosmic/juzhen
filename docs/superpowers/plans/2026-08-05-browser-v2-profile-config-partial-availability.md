# Browser V2 Profile Config and Partial Availability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make V2 use the persisted AdsPower connection settings and keep non-Profile management features usable when AdsPower is unavailable.

**Architecture:** Keep Gateway as the composition root: it reads the existing settings and injects a configured `AdsPowerController` into the independent V2 service. Treat the four UI bootstrap reads independently, represent Profile availability explicitly, and disable only controls that require a Profile.

**Tech Stack:** Python 3, Flask, pytest, browser JavaScript, Node.js built-in test runner, AdsPower Local API.

## Global Constraints

- Do not add an API route, configuration key, database table, worker, or dependency.
- Do not change the V2 executor, scheduler, Profile token masking, or legacy AdsPower routes.
- API keys, raw Profile IDs, cookies, and WebSocket endpoints must not enter HTTP responses or logs.
- When Profile loading fails, element management, strategy editing, history, and local settings remain usable.
- Profile-dependent actions remain disabled until a successful Profile list response.

---

### Task 1: Inject persisted AdsPower settings into the V2 service

**Files:**
- Modify: `gateway/app.py:6692-6710`
- Test: `tests/test_execution_v2_integration.py`

**Interfaces:**
- Consumes: `load_settings() -> dict`, existing `AdsPowerController(base_url: str | None, api_key: str | None)`, and `create_default_execution_v2_service(..., controller: object)`.
- Produces: the existing lazy `execution_v2_service_factory()` singleton, now backed by the same AdsPower address and key as the legacy controller.

- [ ] **Step 1: Write the failing persisted-config injection test**

Add module imports and a test that replaces only construction boundaries:

```python
import execution_v2.service as execution_v2_service_module
import gateway.app as app_module


def test_default_v2_service_receives_persisted_adspower_settings(monkeypatch, tmp_path):
    captured = {}

    class FakeController:
        def __init__(self, base_url=None, api_key=None):
            captured["controller"] = {"base_url": base_url, "api_key": api_key}

    service = FakeV2Service()

    def fake_default_service(**kwargs):
        captured["service"] = kwargs
        return service

    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {
            "adspower": {
                "base_url": "http://127.0.0.1:50325",
                "api_key": "persisted-key",
            }
        },
    )
    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(
        execution_v2_service_module,
        "create_default_execution_v2_service",
        fake_default_service,
    )

    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "EXECUTION_V2_DB_PATH": tmp_path / "v2.db",
            "EXECUTION_V2_EVIDENCE_DIR": tmp_path / "evidence",
        }
    )
    response = app.test_client().get(
        "/api/browser-v2/profiles", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert captured["controller"] == {
        "base_url": "http://127.0.0.1:50325",
        "api_key": "persisted-key",
    }
    assert captured["service"]["controller"].__class__ is FakeController
```

- [ ] **Step 2: Run the test and verify the missing injection**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_execution_v2_integration.py::test_default_v2_service_receives_persisted_adspower_settings -p no:cacheprovider
```

Expected: FAIL because the default V2 service is called without `controller`, or `FakeController` is never constructed.

- [ ] **Step 3: Add the minimal Gateway composition code**

Change only the default branch inside `execution_v2_service_factory()`:

```python
                    if configured_factory is None:
                        from execution_v2.service import create_default_execution_v2_service

                        adspower_settings = load_settings().get("adspower", {})
                        controller = AdsPowerController(
                            base_url=(
                                adspower_settings.get("base_url")
                                or os.getenv("ADSPOWER_BASE_URL")
                            ),
                            api_key=(
                                adspower_settings.get("api_key")
                                or os.getenv("ADSPOWER_API_KEY", "")
                            ),
                        )
                        service = create_default_execution_v2_service(
                            db_path=app.config["EXECUTION_V2_DB_PATH"],
                            evidence_dir=app.config["EXECUTION_V2_EVIDENCE_DIR"],
                            controller=controller,
                        )
```

Do not export the key to the process environment and do not store it on `app.config`.

- [ ] **Step 4: Add and run the environment-fallback test**

Add:

```python
def test_default_v2_service_uses_environment_when_persisted_values_are_blank(
    monkeypatch, tmp_path
):
    captured = {}

    class FakeController:
        def __init__(self, base_url=None, api_key=None):
            captured.update(base_url=base_url, api_key=api_key)

    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {"adspower": {"base_url": "", "api_key": ""}},
    )
    monkeypatch.setenv("ADSPOWER_BASE_URL", "http://127.0.0.1:50326")
    monkeypatch.setenv("ADSPOWER_API_KEY", "environment-key")
    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(
        execution_v2_service_module,
        "create_default_execution_v2_service",
        lambda **_kwargs: FakeV2Service(),
    )

    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "EXECUTION_V2_DB_PATH": tmp_path / "v2.db",
            "EXECUTION_V2_EVIDENCE_DIR": tmp_path / "evidence",
        }
    )
    assert (
        app.test_client()
        .get("/api/browser-v2/profiles", base_url="http://127.0.0.1:5000")
        .status_code
        == 200
    )
    assert captured == {
        "base_url": "http://127.0.0.1:50326",
        "api_key": "environment-key",
    }
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_execution_v2_integration.py -p no:cacheprovider
```

Expected: all integration tests PASS; the existing custom `EXECUTION_V2_SERVICE_FACTORY` tests still bypass default construction.

- [ ] **Step 5: Commit the backend change**

```powershell
git add -- gateway/app.py tests/test_execution_v2_integration.py
git commit -m "fix(v2): inject persisted AdsPower config"
```

---

### Task 2: Represent Profile availability and preserve partial UI use

**Files:**
- Modify: `gateway/static/browser_v2.js:93-197,250-278,320-338`
- Modify: `gateway/templates/browser_v2.html:33-50`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: existing `{status, data}` request results and `load(path, target, keys)` resource loader.
- Produces: `state.profilesAvailable: boolean`; `load()` result `{ok: boolean, error: string}`; deterministic initialization status and disabled Profile controls.

- [ ] **Step 1: Replace the incomplete HTTP-error test with exact partial-state tests**

Add assertions that Profile failure does not erase successful resources:

```javascript
test("Profile bootstrap failure leaves non-Profile views available", async () => {
  const {ui, requests} = harness({
    responses: {
      "GET /api/browser-v2/profiles": response(500, {error: {message: "请求处理失败。"}}),
      "GET /api/browser-v2/elements": response(200, {data: [{id: "element-1"}]}),
      "GET /api/browser-v2/strategies": response(200, {data: [{id: "strategy-1"}]}),
      "GET /api/browser-v2/history": response(200, {data: [{id: "job-1"}]}),
    },
  });

  await ui.init();

  assert.equal(ui.state.profilesAvailable, false);
  assert.equal(ui.state.status, "部分可用：AdsPower 未连接");
  assert.equal(
    ui.state.error,
    "无法读取 AdsPower Profile，请确认 AdsPower 已启动及 API Key 正确"
  );
  assert.deepEqual(ui.state.elements, [{id: "element-1"}]);
  assert.deepEqual(ui.state.strategies, [{id: "strategy-1"}]);
  assert.deepEqual(ui.state.history, [{id: "job-1"}]);
  assert.equal(ui.switchView("elements"), true);
  assert.equal(ui.switchView("strategies"), true);
  assert.equal(ui.switchView("history"), true);
  assert.equal(ui.switchView("settings"), true);
  assert.equal(await ui.startJob(), false);
  assert.equal(await ui.startPicker(), false);
  assert.equal(requests.some((item) => item.method === "POST"), false);
});

test("successful bootstrap reports ready and enables Profile actions", async () => {
  const {ui} = harness({
    responses: {
      "GET /api/browser-v2/profiles": response(200, {data: []}),
      "GET /api/browser-v2/elements": response(200, {data: []}),
      "GET /api/browser-v2/strategies": response(200, {data: []}),
      "GET /api/browser-v2/history": response(200, {data: []}),
    },
  });

  await ui.init();

  assert.equal(ui.state.profilesAvailable, true);
  assert.equal(ui.state.status, "就绪");
  assert.equal(ui.state.error, "");
});
```

- [ ] **Step 2: Run the Node tests and verify the new state is absent**

Run:

```powershell
node --test tests-js\browser-v2-ui.test.js
```

Expected: FAIL because `profilesAvailable` and deterministic partial status do not exist; `startJob()` reaches missing DOM controls.

- [ ] **Step 3: Return structured bootstrap results and set deterministic status**

Add `profilesAvailable` to state and make `load` optionally quiet:

```javascript
const state = {
  view: "center", profiles: [], profilesAvailable: false,
  elements: [], strategies: [], history: [], job: null,
  picker: null, repickTarget: null, draft: null, submitting: false,
  error: "", status: "准备加载", timer: null,
  batchSize: 3, initialized: false,
};

async function load(path, target, keys, quiet) {
  const result = await request(path, "GET");
  if (!success(result, [200])) {
    const message = errorMessage(result, "加载失败");
    if (!quiet) setMessage(message);
    return {ok: false, error: message};
  }
  state[target] = listOf(result.data, keys);
  return {ok: true, error: ""};
}
```

Replace the unconditional `setMessage(state.error, "就绪")` in `init()`:

```javascript
const results = await Promise.all([
  load(API_PREFIX + "/profiles", "profiles", ["profiles"], true),
  load(API_PREFIX + "/elements", "elements", ["elements"], true),
  load(API_PREFIX + "/strategies", "strategies", ["strategies"], true),
  load(API_PREFIX + "/history", "history", ["history", "jobs"], true),
]);
state.initialized = true;
state.profilesAvailable = results[0].ok;
if (!results[0].ok) {
  setMessage(
    "无法读取 AdsPower Profile，请确认 AdsPower 已启动及 API Key 正确",
    "部分可用：AdsPower 未连接"
  );
} else if (results.some(function (result) { return !result.ok; })) {
  const errors = results
    .filter(function (result) { return !result.ok; })
    .map(function (result) { return result.error; });
  setMessage(errors.join("；"), "部分可用");
} else {
  setMessage("", "就绪");
}
render();
```

Keep non-bootstrap callers compatible: they may ignore the returned object, while non-quiet failures still update the page error.

- [ ] **Step 4: Disable and guard only Profile-dependent operations**

Set safe initial HTML state:

```html
<button id="v2-run-start" class="v2-button primary" type="submit" disabled>开始执行</button>
<select id="v2-picker-profile" required disabled></select>
<button id="v2-picker-start" class="v2-button primary" type="submit" disabled>开始点选器</button>
<select id="v2-element-validate-profile" aria-label="校验 Profile" disabled></select>
```

At the end of `renderProfiles()`, synchronize controls:

```javascript
const unavailable = !state.profilesAvailable;
const runStart = el("#v2-run-start");
const pickerStart = el("#v2-picker-start");
if (runStart) runStart.disabled = unavailable || state.submitting || activeJob();
if (pickerStart) pickerStart.disabled = unavailable || state.submitting || activePicker();
if (pickerSelect) pickerSelect.disabled = unavailable;
if (validationSelect) validationSelect.disabled = unavailable;
```

Give dynamically-created validation buttons a stable class and disabled state:

```javascript
const validate = button("校验", "v2-button v2-element-validate", function () {
  validateElement(item);
});
validate.disabled = !state.profilesAvailable;
```

Place guards before any DOM field access:

```javascript
if (!state.profilesAvailable) {
  setMessage("AdsPower 未连接，暂时无法执行 Profile 操作");
  return false;
}
```

Add that guard at the start of `startJob()`, `startPicker()`, and `validateElement()`.

- [ ] **Step 5: Run focused frontend tests**

Run:

```powershell
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: all V2 Node tests PASS; error text remains visible and no failed bootstrap reports “就绪”.

- [ ] **Step 6: Commit the frontend change**

```powershell
git add -- gateway/static/browser_v2.js gateway/templates/browser_v2.html tests-js/browser-v2-ui.test.js
git commit -m "fix(v2): show AdsPower partial availability"
```

---

### Task 3: Run regression and local acceptance

**Files:**
- Verify only; no product file changes expected.

**Interfaces:**
- Consumes: the configured V2 service and the deterministic UI state from Tasks 1 and 2.
- Produces: evidence that the fix works with both unavailable and available AdsPower states.

- [ ] **Step 1: Run all V2 Python tests without writing pytest cache**

```powershell
$files = Get-ChildItem tests -Filter 'test_execution_v2_*.py' | ForEach-Object { $_.FullName }
& .\.venv\Scripts\python.exe -m pytest -q @files tests\test_launcher_restart.py -p no:cacheprovider
```

Expected: all tests PASS, including the 300 Profile / 100 batches test.

- [ ] **Step 2: Run all V2 Node tests**

```powershell
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: all tests PASS.

- [ ] **Step 3: Verify unavailable AdsPower behavior**

With AdsPower stopped, restart the launcher and open the V2 page. Verify:

```text
顶部：部分可用：AdsPower 未连接
错误：无法读取 AdsPower Profile，请确认 AdsPower 已启动及 API Key 正确
开始执行：禁用
开始点选器：禁用
元素校验：禁用
元素、策略、历史、设置：可以打开
```

- [ ] **Step 4: Verify recovered AdsPower behavior**

Start AdsPower, confirm the saved API Key is valid, then reload the V2 page. Verify:

```text
GET /api/browser-v2/profiles = 200
顶部：就绪
页面无错误提示
Profile 只显示脱敏标识
开始执行和开始点选器恢复可用
```

- [ ] **Step 5: Run the real acceptance gate**

Use read-only Profile listing first. Only after it succeeds, proceed with the separately approved 6 ordinary + 2 logged-in Profile acceptance. Do not claim V2 accepted if listing, point selection, execution, or close confirmation fails.
