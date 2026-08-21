# Comment Campaign Profile Allocation and AdsPower Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 AdsPower 健康误报，让缓存 Profile 可离线使用，并让 Comment Campaign 在规划时自动/手动选择窗口、执行前全量读取并冻结实际 TikTok 账号。

**Architecture:** AdsPower 配置和健康检查收敛到独立运行时模块；Profile GET 只读缓存，显式 sync 才访问 AdsPower。规划复用同一二分匹配器推荐窗口，账号身份则在 Worker 的 Campaign 级预检中分批读取，并用 SQLite revision CAS 和单调 `identity_generation` 一次性冻结；普通准备和提交都在任何输入/点击前验证代次。

**Tech Stack:** Python 3.13、Flask、Pydantic v2、SQLAlchemy 2、SQLite、RQ/Redis、Playwright、AdsPower Local API、原生 JavaScript、Node test runner、pytest。

## Global Constraints

- AdsPower 健康探针总墙钟上限为 4 秒，`max_retries=1`，只读取第一页一个 Profile。
- `GET /api/browser-v2/comment-profile-metadata` 必须 cache-only；五秒轮询不得调用 AdsPower。
- 显式 sync 失败返回 HTTP 200、已有缓存和 `meta.stale=true`，不得清空缓存。
- 规划只校验窗口状态、cooldown、标签和语言；不得使用 `expected_username`、`login_verified` 或地区。
- 自动选择必须复用正式二分匹配器，不能简单取前 N 个。
- 账号预检只打开最终 Assignment 使用的去重窗口，不打开未分配候选。
- 任何 Assignment 审批、评论输入或提交点击前，整个 Campaign 必须已有有效且一致的 `identity_generation`。
- generation 只单调递增；失效时不得重置为 `0` 或复用旧值。
- 重复、缺失或变化的账号必须暂停 Campaign、作废未消费审批并保持零提交点击。
- 已锁定 Campaign 不允许换窗；修正原窗口登录后可完整重预检，换窗必须新建 Campaign。
- 原始 AdsPower ID、Cookie、API Key、Authorization、CDP/WebSocket 地址不得进入 API、DOM、日志、证据或 RQ 参数。
- 自动测试只用 Fake AdsPower、Fake page、Fake Redis/Queue，并安装真实网络、真实 Profile 启动和真实 submit/click tripwire。
- 所有测试必须使用临时 Campaign DB；保留 `tests/conftest.py` 的正式 DB fail-fast 与只读指纹守卫。
- 当前托管环境的 `.git` 元数据只读；各任务保留建议 commit 命令，但本会话不得尝试修改 `.git`，只记录未提交状态。

---

## File Map

- Create `gateway/adspower_config.py`: Gateway 与 Worker 共用的动态 AdsPower 配置解析。
- Create `comment_campaign/adspower_health.py`: 4 秒 single-flight 健康探针和固定安全原因。
- Modify `adspower.py`: 将 HTTP/JSON/AdsPower 业务拒绝转换为不携带原始响应的依赖错误。
- Create `comment_campaign/identity.py`: TikTok 账号规范化和只读身份观察值。
- Modify `gateway/app.py`: 惰性注入共享 AdsPower runtime，不在大文件中新增业务算法。
- Modify `comment_campaign/worker.py`: 与 Gateway 使用同一配置解析和 controller 工厂。
- Modify `comment_campaign/models.py`: Campaign/Assignment `identity_generation`。
- Modify `comment_campaign/store.py`: SQLite 迁移、metadata 缺省、身份冻结/失效/代次 CAS。
- Modify `comment_campaign/allocation.py`: 窗口级资格、共享匹配器、推荐结果和安全失败详情。
- Modify `comment_campaign/schemas.py`: sync 空请求与推荐请求的严格模型。
- Modify `comment_campaign/errors.py`: 运行时身份稳定错误码和安全 `details`。
- Modify `comment_campaign/blueprint.py`: sync/preview 路由、422 details 脱敏投影。
- Modify `comment_campaign/service.py`: cache/sync、推荐、规划详情、预检调度。
- Modify `comment_campaign/locator.py`: 读取而非预期比对的 TikTok 身份合同。
- Modify `comment_campaign/profile_gateway.py`: 预检专用开窗入口，不放宽普通 prepare。
- Modify `comment_campaign/executor.py`: 全量预检、prepare/submit 代次门和身份变化处理。
- Modify `comment_campaign/queueing.py`, `comment_campaign/jobs.py`: prepare RQ 参数携带身份代次，旧 job 可无操作退出。
- Modify `gateway/static/comment_campaign.js`: 自动/手动选窗、缓存状态和安全提示。
- Modify `gateway/static/comment_campaign.css`: 仅补本模块选窗布局；保持浅色且作用域限定。
- Modify OpenAPI、错误码、模块和数据库文档。

---

### Task 1: Test isolation and real-execution tripwires

**Files:**
- Modify: `tests/conftest.py`
- Test: `tests/test_comment_campaign_security.py`

**Interfaces:**
- Produces: session-wide fail-fast before `CampaignStore` can create an engine for the production SQLite path.
- Produces: Comment Campaign-only tripwires for real HTTP, AdsPower browser start, Playwright/CDP connect, and submit click.

- [ ] **Step 1: Write failing isolation tests**

```python
def test_default_testing_app_cannot_construct_production_campaign_store(tmp_path):
    before = readonly_campaign_db_snapshot()
    app = create_app({"TESTING": True, "COMMENT_CAMPAIGN_SERVICE_FACTORY": None})
    response = app.test_client().get("/api/browser-v2/comment-templates")
    assert response.status_code == 500
    assert readonly_campaign_db_snapshot() == before


def test_comment_campaign_test_tripwires_reject_real_execution(external_bombs):
    with pytest.raises(AssertionError, match="real AdsPower start forbidden"):
        external_bombs.adspower.start_browser("raw-id")
    with pytest.raises(AssertionError, match="real submit click forbidden"):
        external_bombs.submit.click()
```

- [ ] **Step 2: Run the security test and confirm RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_comment_campaign_security.py -q -p no:cacheprovider
```

Expected: at least one test proves the current guard is opt-in or a real-execution boundary is unpatched.

- [ ] **Step 3: Make the production DB guard unconditional for tests**

At `pytest_sessionstart`, patch `CampaignStore.__init__` before the original constructor runs. Resolve and `normcase` every SQLite URL; if it equals `data/comment_campaign/comment_campaign.db`, raise a fixed `AssertionError` before engine creation. Keep the existing read-only SHA-256/size/mtime/template/revision snapshot as a second, session-finish check and restore the original constructor in `finally`.

- [ ] **Step 4: Add scoped external tripwires**

Use an autouse fixture that checks `request.node.path.name.startswith("test_comment_campaign_")`; do not inspect the full pytest node ID or test function name. For every matching test module, monkeypatch these exact boundaries to fixed bombs unless the test injected a Fake object:

```python
def forbidden_requests(*_args, **_kwargs):
    raise AssertionError("real HTTP forbidden in Comment Campaign tests")

def forbidden_start(*_args, **_kwargs):
    raise AssertionError("real AdsPower start forbidden in Comment Campaign tests")

async def forbidden_connect(*_args, **_kwargs):
    raise AssertionError("real Playwright/CDP connect forbidden in Comment Campaign tests")
```

Do not patch Flask's in-process test client. Test submit locators use a Fake handle whose `click()` is a bomb unless the individual approval test explicitly replaces it with a counting Fake.

Add a test named `test_health_probe_installs_external_bombs` inside `tests/test_comment_campaign_security.py`; its function name deliberately lacks the Comment Campaign prefix and must still see all four bombs, proving module-path scoping works.

- [ ] **Step 5: Run security and existing integration tests**

Expected: guard/tripwire tests pass, all pre-existing Comment Campaign integration tests still use tmp DB/Fakes, and the production DB read-only snapshot is unchanged.

- [ ] **Step 6: Checkpoint**

```bash
git add tests/conftest.py tests/test_comment_campaign_security.py
git commit -m "test: isolate Comment Campaign execution"
```

---

### Task 2: Shared AdsPower runtime settings and health probe

**Files:**
- Modify: `adspower.py:15-80`
- Create: `gateway/adspower_config.py`
- Create: `comment_campaign/adspower_health.py`
- Modify: `gateway/app.py:478-548`
- Modify: `comment_campaign/worker.py:33-91`
- Modify: `comment_campaign/service.py:487-527`
- Test: `tests/test_comment_campaign_service.py`
- Test: `tests/test_comment_campaign_integration.py`
- Test: `tests/test_comment_campaign_worker.py`

**Interfaces:**
- Produces: `AdsPowerConfig(base_url: str, api_key: str)`.
- Produces: `resolve_adspower_config(settings_loader, environ=None) -> AdsPowerConfig | None`.
- Produces: `AdsPowerDependencyError(reason)`，其中 `reason` 只能是批准的六种固定值。
- Produces: `AdsPowerHealthProbe(controller_factory, settings_provider, timeout_seconds=4.0).probe() -> dict[str, str]`.
- Consumed by: Gateway service factory and Worker runtime builder.

- [ ] **Step 1: Write failing settings and health tests**

```python
def test_persisted_adspower_values_win_and_env_only_fills_blanks():
    settings = resolve_adspower_config(
        lambda: {"adspower": {"base_url": "http://persisted:50325", "api_key": "persisted-key"}},
        {"ADSPOWER_BASE_URL": "http://env:50325", "ADSPOWER_API_KEY": "env-key"},
    )
    assert settings.base_url == "http://persisted:50325"
    assert settings.api_key == "persisted-key"


def test_health_probe_accepts_response_after_one_second_before_four_second_deadline(fake_clock):
    controller = FakeController(delay=1.2, rows=[{"id": "raw-never-returned"}])
    result = AdsPowerHealthProbe(lambda *_: controller, settings_provider, timeout_seconds=4).probe()
    assert result == {"status": "connected", "reason": "connected"}
    assert controller.calls == [(1, 1)]


@pytest.mark.parametrize("error,reason", [
    (requests.Timeout(), "timeout"),
    (requests.ConnectionError(OSError(10061, "refused")), "connection_refused"),
    (FakeHttpError(401), "authentication_failed"),
    (ValueError("SECRET"), "invalid_response"),
])
def test_health_probe_returns_only_fixed_safe_reason(error, reason):
    result = probe_raising(error).probe()
    assert result == {"status": "unavailable", "reason": reason}
    assert "SECRET" not in repr(result)


def test_adspower_business_auth_rejection_is_structured():
    controller = controller_responding_json({"code": -1, "msg": "API key invalid SECRET"})
    with pytest.raises(AdsPowerDependencyError) as caught:
        list_one_profile(controller)
    assert caught.value.reason == "authentication_failed"
    assert "SECRET" not in str(caught.value)


def test_settings_loader_failure_is_fixed_invalid_response():
    probe = AdsPowerHealthProbe(FakeController, lambda: (_ for _ in ()).throw(RuntimeError("SECRET")))
    assert probe.probe() == {"status": "unavailable", "reason": "invalid_response"}
    probe.close()
```

- [ ] **Step 2: Run tests and confirm RED**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_comment_campaign_service.py tests/test_comment_campaign_integration.py tests/test_comment_campaign_worker.py -q -p no:cacheprovider
```

Expected: failures because the new config/health modules and fixed health reasons do not exist; existing 1-second controllers are observed.

- [ ] **Step 3: Implement the focused runtime module**

```python
import os
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from threading import Lock

import requests


@dataclass(frozen=True, slots=True)
class AdsPowerConfig:
    base_url: str
    api_key: str


def resolve_adspower_config(settings_loader, environ=None):
    environment = os.environ if environ is None else environ
    loaded = settings_loader()
    source = loaded.get("adspower", {}) if isinstance(loaded, Mapping) else {}
    base_url = str(source.get("base_url") or environment.get("ADSPOWER_BASE_URL") or "").strip()
    api_key = str(source.get("api_key") or environment.get("ADSPOWER_API_KEY") or "").strip()
    return AdsPowerConfig(base_url, api_key) if base_url and api_key else None


def safe_health_reason(error):
    if isinstance(error, AdsPowerDependencyError):
        return error.reason
    current = error
    while current is not None:
        if isinstance(current, requests.Timeout):
            return "timeout"
        if is_explicit_connection_refused(current):
            return "connection_refused"
        if isinstance(current, requests.HTTPError) and getattr(current.response, "status_code", None) in {401, 403}:
            return "authentication_failed"
        current = current.__cause__
    return "invalid_response"


class AdsPowerHealthProbe:
    def __init__(self, controller_factory, settings_provider, timeout_seconds=4.0):
        self._controller_factory = controller_factory
        self._settings_provider = settings_provider
        self._timeout_seconds = float(timeout_seconds)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="adspower-health")
        self._lock = Lock()
        self._future = None

    def probe(self) -> dict[str, str]:
        with self._lock:
            if self._future is None or self._future.done():
                self._future = self._executor.submit(self._probe_once)
            future = self._future
        try:
            return dict(future.result(timeout=self._timeout_seconds))
        except FutureTimeoutError:
            return {"status": "unavailable", "reason": "timeout"}

    def _probe_once(self) -> dict[str, str]:
        try:
            config = self._settings_provider()
            if config is None:
                return {"status": "unavailable", "reason": "not_configured"}
            controller = self._controller_factory(
                base_url=config.base_url, api_key=config.api_key,
                timeout=self._timeout_seconds, max_retries=1, retry_delay=0,
            )
            rows = controller.list_profiles(page=1, page_size=1)
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                raise ValueError("invalid response shape")
            return {"status": "connected", "reason": "connected"}
        except Exception as error:
            return {"status": "unavailable", "reason": safe_health_reason(error)}

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
```

`AdsPowerDependencyError` stores only the fixed reason, never the API body/message. The AdsPower adapter converts HTTP 401/403 and a strict whitelist of AdsPower business authentication codes/messages to `authentication_failed`; explicit `ConnectionRefusedError`/WinError 10061 maps to `connection_refused`; other request/JSON/business failures map to `invalid_response`. Dynamic settings loading is inside the same `try`; a loader exception containing a secret maps to fixed `invalid_response` without leakage. No exception string may enter the return value. A timed-out Future remains the shared in-flight request; no caller starts a second probe until it finishes.

- [ ] **Step 4: Wire Gateway and Worker to the same resolver**

```python
def current_controller():
    runtime_settings = resolve_adspower_config(load_settings, os.environ)
    if runtime_settings is None:
        raise AdsPowerDependencyError("not_configured")
    return AdsPowerController(
        base_url=runtime_settings.base_url,
        api_key=runtime_settings.api_key,
    )


def profile_provider():
    return current_controller().list_all_profiles()


health_probe = AdsPowerHealthProbe(
    AdsPowerController,
    lambda: resolve_adspower_config(load_settings, os.environ),
    timeout_seconds=4.0,
)
```

Inject `health_probe.probe` and the dynamic `profile_provider` into the service. Missing configuration must not prevent service construction or cached history reads; it fails only sync/health/real execution. Do not cache a controller whose address/key survives a settings update. Use the same resolver once per `worker.build_runtime_service()` job runtime.

- [ ] **Step 5: Make health projection exact**

```python
adspower = probe()
if adspower.get("status") not in {"connected", "unavailable"}:
    adspower = {"status": "unavailable", "reason": "invalid_response"}
```

Keep SQLite/Redis/Worker probes independent. UI copy is handled in Task 9.

Register `health_probe.close` in the service/application closeables so its executor cannot leak across app recreation or tests.

- [ ] **Step 6: Run focused tests**

Expected: health reason matrix, single-flight concurrency, latest-settings refresh, Gateway/Worker precedence, and existing health tests pass; no controller method other than a one-row list call occurs.

- [ ] **Step 7: Checkpoint**

Suggested commit outside this read-only `.git` sandbox:

```bash
git add adspower.py gateway/adspower_config.py comment_campaign/adspower_health.py comment_campaign/service.py comment_campaign/worker.py gateway/app.py tests/test_adspower.py tests/test_comment_campaign_service.py tests/test_comment_campaign_integration.py tests/test_comment_campaign_worker.py
git commit -m "fix: stabilize AdsPower health checks"
```

---

### Task 3: Cache-only Profile reads, explicit sync, and safe metadata defaults

**Files:**
- Modify: `comment_campaign/store.py:82-107,302-404`
- Modify: `comment_campaign/schemas.py:138-160`
- Modify: `comment_campaign/service.py:233-269`
- Modify: `comment_campaign/blueprint.py:189-198`
- Test: `tests/test_comment_campaign_store.py`
- Test: `tests/test_comment_campaign_service.py`
- Test: `tests/test_comment_campaign_routes.py`
- Test: `tests/test_comment_campaign_security.py`

**Interfaces:**
- Produces: `CampaignStore.profile_cache_last_synced_at() -> str | None` from the maximum identity `updated_at`.
- Changes: `CampaignStore.sync_profile_identities()` creates missing metadata only for identities present in the current successful sync, in the same transaction.
- Produces: `CommentCampaignService.list_profile_metadata() -> {"data": list, "meta": dict}`.
- Produces: `CommentCampaignService.sync_profile_metadata() -> {"data": list, "meta": dict}`.
- Produces route: `POST /api/browser-v2/comment-profile-metadata/sync` with strict `{}`.

- [ ] **Step 1: Write failing store tests for the existing 21-identity case**

```python
def test_sync_backfills_only_currently_discovered_missing_profile_metadata(store_with_identities):
    store_with_identities.insert_identity("raw-history-not-returned")
    store_with_identities.insert_identity("raw-existing")
    store_with_identities.upsert_profile_metadata(
        profile_ref="profile_ref_existing", enabled=False,
        login_verified=False, expected_username="", tags=["manual"],
        language="", region="", cooldown_until=None, health_status="unhealthy",
    )
    store_with_identities.sync_profile_identities([
        {"id": "raw-new", "name": "New", "status": "Active"},
        {"id": "raw-existing", "name": "Existing", "status": "Active"},
    ])
    rows = {row["profile_ref"]: row for row in store_with_identities.list_comment_profiles()}
    assert rows["profile_ref_new"]["enabled"] is True
    assert rows["profile_ref_new"]["health_status"] == "healthy"
    assert rows["profile_ref_existing"]["enabled"] is False
    assert rows["profile_ref_existing"]["tags"] == ["manual"]
    assert "profile_ref_history_not_returned" not in rows
```

Also assert initialize twice does not synthesize metadata and sync never overwrites disabled/quarantine/cooldown.

- [ ] **Step 2: Write failing service/route tests**

```python
def test_profile_get_is_cache_only_when_provider_would_explode(service):
    service._profile_provider = lambda: (_ for _ in ()).throw(AssertionError("network called"))
    payload = service.list_profile_metadata()
    assert payload["data"]
    assert payload["meta"]["stale"] is True


def test_explicit_sync_failure_returns_cached_rows_and_stale_meta(client, fake_provider):
    fake_provider.raise_error = AdsPowerDependencyError("connection_refused")
    response = client.post("/api/browser-v2/comment-profile-metadata/sync", json={})
    assert response.status_code == 200
    assert response.get_json()["meta"]["stale"] is True
    assert "secret-host" not in response.get_data(as_text=True)


def test_sync_store_failure_is_not_disguised_as_stale(service):
    service.store.sync_profile_identities = Mock(side_effect=RuntimeError("db SECRET"))
    with pytest.raises(RuntimeError, match="db SECRET"):
        service.sync_profile_metadata()
```

Add a not-configured sync test returning HTTP 200 with `safe_reason=not_configured`; strict-body tests for null/list/extra field; legacy operator/CSRF tests; local foreign Host/REMOTE_ADDR factory-count-zero tests; and a raw-ID redaction sentinel.

- [ ] **Step 3: Run tests and confirm RED**

Run the four files above; expect the current GET to call `_profile_provider` and the new route to be 404.

- [ ] **Step 4: Implement metadata backfill and atomic sync defaults**

Use these exact defaults only when no metadata row exists:

```python
DEFAULT_PROFILE_METADATA = {
    "expected_username": "",
    "enabled": True,
    "login_verified": False,
    "tags_json": "[]",
    "language": "",
    "region": "",
    "cooldown_until": "",
    "health_status": "healthy",
}
```

Do not backfill historical identities during `CampaignStore.initialize()`. In `sync_profile_identities`, for each identity returned by this successful sync, insert/update the safe identity projection and insert missing metadata within the same `session_factory.begin()` transaction. Do not create metadata for an identity absent from the current sync, and do not update an existing metadata row.

Change `_display_profile()` so an empty AdsPower name returns the fixed text `未命名 Profile`. Never derive visible text from the full `profile_ref` or any suffix. Add API and DOM assertions that an unnamed row contains neither `profile_ref_` nor its trailing characters; the UI may distinguish duplicate unnamed rows only by transient list position such as `未命名 Profile 1`.

- [ ] **Step 5: Implement cache/sync service methods and strict route**

```python
class EmptyRequest(_StrictInput):
    pass


def list_profile_metadata(self) -> dict:
    with self._profile_sync_lock:
        meta = dict(self._profile_sync_state)
    meta["last_synced_at"] = self.store.profile_cache_last_synced_at()
    return {"data": self.store.list_comment_profiles(), "meta": meta}


def sync_profile_metadata(self) -> dict:
    try:
        rows = self._validated_profile_provider_rows()
    except AdsPowerDependencyError as error:
        state = {"stale": True, "safe_reason": error.reason}
    else:
        self.store.sync_profile_identities(rows)
        state = {"stale": False, "safe_reason": None}
    with self._profile_sync_lock:
        self._profile_sync_state = state
    return self.list_profile_metadata()
```

Initialize `_profile_sync_state` to `{"stale": True, "safe_reason": None}` in the service. Keep stale/reason in-process under a lock; derive `last_synced_at` from the maximum persisted identity `updated_at`. Do not add a table or cache daemon. `_validated_profile_provider_rows` must rebuild only `{id,name,status}`.

Only `AdsPowerDependencyError` is converted to HTTP 200 stale. SQLAlchemy errors, Store failures, malformed internal values, and unknown programming exceptions propagate to the Blueprint's fixed 500 handler.

Blueprint route:

```python
@blueprint.post("/comment-profile-metadata/sync")
def sync_profile_metadata():
    _parse(EmptyRequest)
    return _profile_envelope(_call(service(), "sync_profile_metadata"))


def _profile_envelope(value):
    safe = _redact(value)
    return jsonify({"data": safe["data"], "meta": safe["meta"]}), 200
```

Use `_profile_envelope` for both GET and sync POST so the response is exactly `{data:[profiles], meta:{stale,last_synced_at,safe_reason}}`, never double wrapped.

- [ ] **Step 6: Run focused tests**

Expected: identities returned by a successful explicit sync receive enabled/healthy defaults without username configuration; historical identities absent from that sync remain without synthesized metadata; offline GET works; explicit dependency failure safely reports stale; Store/program failures remain fixed 500; all guards and redaction pass.

- [ ] **Step 7: Checkpoint**

```bash
git add comment_campaign/store.py comment_campaign/schemas.py comment_campaign/service.py comment_campaign/blueprint.py tests/test_comment_campaign_store.py tests/test_comment_campaign_service.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_security.py
git commit -m "feat: add offline Profile cache sync"
```

---

### Task 4: Window-only eligibility, authoritative recommendation, and allocation details

**Files:**
- Modify: `comment_campaign/allocation.py`
- Modify: `comment_campaign/errors.py`
- Modify: `comment_campaign/schemas.py`
- Modify: `comment_campaign/service.py:326-381,620-640`
- Modify: `comment_campaign/store.py:927-952`
- Modify: `comment_campaign/blueprint.py`
- Test: `tests/test_comment_campaign_allocation.py`
- Test: `tests/test_comment_campaign_service.py`
- Test: `tests/test_comment_campaign_routes.py`
- Test: `tests/test_comment_campaign_security.py`

**Interfaces:**
- Produces: `match_profiles(steps, profiles, *, eligibility_at, order_key) -> dict[str, str]`.
- Produces: `recommend_profiles(steps, profiles, *, eligibility_at) -> list[str]`.
- Changes: `AllocationError(reason, required_count=None, eligible_count=None, display_profiles=())` exposes a strict safe `details` mapping.
- Produces: `ProfileSelectionPreview(template_id, mode, template_revision=None)`.
- Produces route: `POST /api/browser-v2/comment-profile-selection/preview`.

- [ ] **Step 1: Write failing allocation tests**

```python
def test_window_is_eligible_without_username_or_historical_login_flag():
    profile = healthy_profile(expected_username="", login_verified=False)
    assert profile_matches(step(), profile, eligibility_at=NOW) is True


def test_region_is_not_a_step_matching_constraint():
    us_profile = healthy_profile(profile_ref="profile_ref_us", region="US")
    cn_profile = healthy_profile(profile_ref="profile_ref_cn", region="CN")
    unconstrained_step = step(language="")
    assert profile_matches(unconstrained_step, us_profile, eligibility_at=NOW) is True
    assert profile_matches(unconstrained_step, cn_profile, eligibility_at=NOW) is True


def test_recommendation_uses_augmenting_path_not_first_n():
    # flexible can use A/B; constrained can use A only. First-two greedy B/A order
    # must still return a complete two-profile matching.
    result = recommend_profiles([flexible, constrained], [profile_b, profile_a], eligibility_at=NOW)
    assert set(result) == {"profile_ref_a", "profile_ref_b"}
```

Add reason tests for insufficient count, disabled, unhealthy, cooldown, tags, language, unknown ref, and complete matching failure. Assert details contain only the approved keys.

When several failures coexist, use this deterministic reason priority: `unknown_profile_ref`, `insufficient_profiles`, `profile_disabled`, `profile_unhealthy`, `profile_in_cooldown`, `profile_tag_mismatch`, `profile_language_mismatch`, then `complete_matching_not_found`. Counts are computed from one immutable Profile snapshot and one injected `eligibility_at`.

- [ ] **Step 2: Run allocation/service tests and confirm RED**

Expected: username/login tests fail and preview schema/route are absent.

- [ ] **Step 3: Refactor one shared bounded matcher**

```python
def profile_matches(step, profile, *, eligibility_at=None):
    if profile.get("enabled") is not True or profile.get("health_status") != "healthy":
        return False
    # Preserve strict aware cooldown, tags, excluded tags and language checks.
    # Do not read expected_username, login_verified or region.


def match_profiles(steps, profiles, *, eligibility_at, order_key):
    # Move the existing Kuhn augmenting-path body here.
    # Validate <=100 steps, <=300 unique profile_ref values.
    # Return {step_id: profile_ref}; raise AllocationError with safe reason.
```

`allocate()` consumes `match_profiles` with its seeded order and sets `PlannedAssignment.expected_username=""`. `recommend_profiles()` consumes the same matcher with `(display_profile.casefold(), profile_ref)` order and returns the matched refs in display order.

- [ ] **Step 4: Remove username/login checks from lock-time profile validation**

In `CampaignStore._validate_locked_profiles`, retain metadata existence, enabled, health, cooldown, required/excluded tags, and language. Delete only the username/login/equality conditions. Add a regression proving disabled or quarantined still blocks lock.

- [ ] **Step 5: Add strict preview service and API**

```python
class ProfileSelectionPreview(_StrictInput):
    template_id: str = Field(min_length=1, max_length=120)
    mode: Literal["independent", "threaded"]
    template_revision: int | None = Field(default=None, ge=1)


def preview_profile_selection(self, payload: ProfileSelectionPreview) -> dict:
    template = self._enabled_template_revision(payload.template_id, payload.template_revision, payload.mode)
    profiles = self.store.list_comment_profiles()
    eligibility_at = datetime.now(timezone.utc)
    refs = recommend_profiles(template["steps"], profiles, eligibility_at=eligibility_at)
    by_ref = {row["profile_ref"]: row for row in profiles}
    return {
        "required_count": len(template["steps"]),
        "eligible_count": sum(
            any(profile_matches(step, profile, eligibility_at=eligibility_at) for step in template["steps"])
            for profile in profiles
        ),
        "profiles": [{"profile_ref": ref, "display_profile": by_ref[ref]["display_profile"]} for ref in refs],
    }
```

The route is strict POST and uses the existing auth/CSRF/local guard. It performs no writes.

- [ ] **Step 6: Project strict 422 allocation details**

```python
class AllocationError(CampaignError):
    def __init__(self, reason: str, **safe_counts):
        super().__init__("allocation_unsatisfied")
        self.details = allocation_details(reason, **safe_counts)


def _error(code: str, details: Mapping[str, Any] | None = None):
    body = {"code": safe_code, "message": _MESSAGES[safe_code]}
    if safe_code == "allocation_unsatisfied" and details:
        body["details"] = _redact(allow_allocation_details(details))
    return jsonify({"error": body})
```

Update `handle_error` to pass only `AllocationError.details`. Unknown exceptions still return fixed 500 without details.

- [ ] **Step 7: Run focused allocation/API/security tests**

Expected: preview returns exactly N safe refs, manual unknown refs get 422 with `reason=unknown_profile_ref`, Hall failures are distinct, no raw ID/ws/cookie sentinel survives.

- [ ] **Step 8: Checkpoint**

```bash
git add comment_campaign/allocation.py comment_campaign/errors.py comment_campaign/schemas.py comment_campaign/service.py comment_campaign/store.py comment_campaign/blueprint.py tests/test_comment_campaign_allocation.py tests/test_comment_campaign_service.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_security.py
git commit -m "feat: recommend eligible Campaign windows"
```

---

### Task 5: Persist monotonic Campaign account identity generations

**Files:**
- Modify: `comment_campaign/models.py:69-135`
- Modify: `comment_campaign/store.py:82-107,600-1020,1190-1240,1380-1460,1583-1588`
- Test: `tests/test_comment_campaign_store.py`
- Test: `tests/test_comment_campaign_recovery.py`

**Interfaces:**
- Adds: `CommentCampaignRecord.identity_generation: int` and `CommentAssignmentRecord.identity_generation: int`, both non-null default `0`.
- Produces: `account_preflight_required(campaign_id) -> bool`.
- Produces: `freeze_campaign_identities(campaign_id, expected_campaign_revision, expected_identity_generation, observations) -> dict`.
- Produces: `invalidate_campaign_identity(campaign_id, expected_campaign_revision, expected_identity_generation, *, error_code, affected_assignment_ids, failure_details=None) -> dict`.
- Produces internal `DuplicateTikTokAccountError(account_key, visible_username, assignment_ids)`; its raw fields are consumed only by the executor and never projected directly to API/logs.
- Changes: `begin_submitting(campaign_id, assignment_id, revision, expected_identity_generation)` atomically consumes approval and requires running Campaign plus three-way generation equality.

- [ ] **Step 1: Write failing migration and persistence tests**

```python
def test_old_sqlite_migration_adds_identity_generation_idempotently(old_database):
    store = CampaignStore(old_database)
    store.initialize(); store.initialize()
    assert column_default(old_database, "comment_campaigns", "identity_generation") == "0"
    assert column_default(old_database, "comment_assignments", "identity_generation") == "0"


def test_generation_zero_cannot_begin_approval_or_submit(planned_campaign):
    assert store.account_preflight_required(planned_campaign["id"]) is True
    with pytest.raises(CampaignValidationError) as caught:
        store.begin_submitting(campaign_id, assignment_id, revision, 0)
    assert caught.value.code == "tiktok_identity_unavailable"
```

- [ ] **Step 2: Write failing transaction tests**

Test all-or-nothing freeze, duplicate normalized handles, concurrent revision CAS (one winner), injected failure rollback, monotonic invalidation (1→2, never 0), all unconsumed approvals invalidated, every nonterminal Assignment revision/generation advanced, affected Assignments paused, every other nonterminal Assignment moved to `paused_dependency`, and published Receipt identity left unchanged. Duplicate tests assert the persisted/public `identity_failure` contains exactly two masked display names and one visible username with no account key/ref/raw ID. Add a resume→full preflight case proving every nonterminal root returns to `planned` and every nonterminal child returns to `waiting_dependency` before any prepare resumes.

- [ ] **Step 3: Run store/recovery tests and confirm RED**

Expected: missing columns/methods and current `begin_submitting` accepts generation-free rows.

- [ ] **Step 4: Add models and SQLite migration**

```python
identity_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
```

For existing SQLite, inspect both tables independently and run:

```sql
ALTER TABLE comment_campaigns ADD COLUMN identity_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE comment_assignments ADD COLUMN identity_generation INTEGER NOT NULL DEFAULT 0;
```

Never mutate `prepare_generation` or immutable plan snapshots for this purpose.

- [ ] **Step 5: Implement one-transaction freeze**

`observations` is a tuple of strict internal records:

```python
{
  "assignment_id": "assignment-1",
  "profile_ref": "profile_ref_a1b2c3",
  "account_key": "canonical.handle",
  "visible_username": "VisibleName",
  "canonical_href": "https://www.tiktok.com/@canonical.handle",
  "observed_at": "2026-08-11T00:00:00Z",
  "target_video": {"video_id": "7512345678901234567", "canonical_url": "https://www.tiktok.com/@creator/video/7512345678901234567"},
  "element_binding": {"id": "account-binding", "revision": 2, "definition_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
}
```

Within one transaction: lock/read Campaign and all nonterminal Assignments; verify exact Assignment/profile set; compare account keys with immutable published Receipt keys; reject zero/duplicate/missing; increment Campaign generation; write the same generation and `account_key` into both the compatibility `expected_username` field and `evidence.account_preflight.account_key`; persist the other Task 6 observation fields under `account_preflight`; increment Campaign/Assignment revisions. Reset every nonterminal root to `planned`, every nonterminal child to `waiting_dependency`, clear old execution errors and `evidence.identity_failure`, and leave published Assignments/Receipts byte-for-byte unchanged. This reset is the only route from an identity-invalidated Campaign back to preparable Assignment states.

- [ ] **Step 6: Implement atomic invalidation and submit CAS**

Invalidation first CAS-checks `status='running'`, the exact starting Campaign revision, and the exact starting identity generation in one transaction. It then increments Campaign generation, pauses Campaign, advances every nonterminal Assignment to the new generation/revision while leaving the old `account_preflight.identity_generation` unchanged, moves affected Assignments to `paused`, moves every other nonterminal Assignment to `paused_dependency`, clears executable page/input evidence for all nonterminal Assignments, marks all unconsumed approvals unusable, and records the fixed error code. A stale revision/generation raises `RevisionConflictError` and performs zero writes.

For `duplicate_tiktok_account` only, `failure_details` is rebuilt inside the Store from the two affected Assignment records plus the supplied visible username and persisted as `evidence.identity_failure` on those Assignments. Its public projection is exactly `{display_profiles:[masked name, masked name], visible_username}`; it contains no account key, profile ref, raw ID, URL or DOM data. Other failures persist only fixed codes. `begin_submitting` must require:

```python
campaign.status == "running"
campaign.identity_generation > 0
assignment.identity_generation == campaign.identity_generation
assignment.evidence["account_preflight"]["identity_generation"] == campaign.identity_generation
```

Put Campaign status/generation requirements into the Assignment CAS predicate: update the expected Assignment ID/revision/status/generation only where an `EXISTS` subquery finds the same Campaign with `status='running'`, the expected nonzero `identity_generation`, and matching ID. In the same transaction, locate the unique unconsumed approval for this Assignment revision and set `consumed_at`; then move the Assignment to `submitting`. Pre-read evidence only supplies the JSON equality gate. If any Campaign/Assignment/evidence/approval predicate fails, the entire transaction rolls back, approval remains unconsumed, and the stale job returns a stable no-click conflict.

- [ ] **Step 7: Run store and recovery tests**

Expected: all migration, rollback, concurrency, generation, approval invalidation, and uncertain-recovery regressions pass.

- [ ] **Step 8: Checkpoint**

```bash
git add comment_campaign/models.py comment_campaign/store.py tests/test_comment_campaign_store.py tests/test_comment_campaign_recovery.py
git commit -m "feat: persist Campaign account generations"
```

---

### Task 6: Strict TikTok identity observation contract

**Files:**
- Create: `comment_campaign/identity.py`
- Modify: `comment_campaign/locator.py:257-269`
- Modify: `comment_campaign/errors.py`
- Modify: `comment_campaign/blueprint.py:39-73`
- Test: `tests/test_comment_campaign_identity.py`
- Test: `tests/test_comment_campaign_locator.py`

**Interfaces:**
- Produces: `AccountObservation` immutable dataclass.
- Produces: `normalize_tiktok_account_key(value: str) -> str`.
- Produces: `read_tiktok_identity(page, account_definition, resolver=None) -> AccountObservation`.
- Preserves: `verify_logged_in_username(page, expected_username, definition, *, resolver=None)` as a compatibility wrapper for existing prepare/submit callers until Task 8.

- [ ] **Step 1: Write failing locator tests**

```python
@pytest.mark.asyncio
async def test_identity_prefers_canonical_href_and_keeps_display_separate():
    evidence = await read_tiktok_identity(page(text="Display Name", href="https://www.tiktok.com/@Canonical.Handle"), definition)
    assert evidence.account_key == "canonical.handle"
    assert evidence.visible_username == "Display Name"
    assert evidence.canonical_href == "https://www.tiktok.com/@Canonical.Handle"


@pytest.mark.parametrize("candidate", [not_logged_in, two_visible_nodes, href_text_conflict, bad_href])
def test_identity_is_fail_closed(candidate):
    with pytest.raises(CampaignValidationError) as caught:
        asyncio.run(read_tiktok_identity(candidate.page, definition))
    assert caught.value.code in {"tiktok_login_required", "tiktok_identity_unavailable"}
```

Normalize with NFKC, trim, remove one leading `@`, and casefold. If href and a handle-like visible text both exist, require equality. Do not accept an arbitrary display name as the canonical handle.

- [ ] **Step 2: Add pure normalization and URL tests**

Cover `@Name`, full-width Unicode normalization, mixed case, empty/control characters, handles over 24 characters, evil host suffix, userinfo, port, query/fragment policy and two visible candidates. No test may construct a real Playwright browser.

- [ ] **Step 3: Run identity/locator tests and confirm RED**

Expected: module is missing and current locator only verifies against a preconfigured expected username.

- [ ] **Step 4: Implement the immutable identity value**

```python
@dataclass(frozen=True, slots=True)
class AccountObservation:
    account_key: str
    visible_username: str
    canonical_href: str | None
    observed_at: str

    def as_dict(self) -> dict[str, str | None]:
        return {
            "account_key": self.account_key,
            "visible_username": self.visible_username,
            "canonical_href": self.canonical_href,
            "observed_at": self.observed_at,
        }


def normalize_tiktok_account_key(value: str) -> str:
    normalized = normalize("NFKC", str(value)).strip()
    if normalized.startswith("@"):
        normalized = normalized[1:]
    normalized = normalized.casefold()
    if not re.fullmatch(r"[a-z0-9._]{1,24}", normalized):
        raise CampaignValidationError("tiktok_identity_unavailable")
    return normalized
```

`read_tiktok_identity` resolves exactly one visible account element, extracts `{text,href}`, validates exact `tiktok.com`/`www.tiktok.com` `/@handle` links with no userinfo/port/fragment, and uses text only when no href exists. A handle-like text that conflicts with href is invalid. Map locator/DOM exceptions to a fixed identity code without exception text.

- [ ] **Step 5: Keep the compatibility verifier exact**

```python
async def verify_logged_in_username(page, expected_username, definition, *, resolver=None):
    evidence = await read_tiktok_identity(page, definition, resolver=resolver)
    if evidence.account_key != normalize_tiktok_account_key(expected_username):
        raise CampaignValidationError("tiktok_identity_changed")
    return evidence.as_dict()
```

Register `duplicate_tiktok_account`, `tiktok_login_required`, `tiktok_identity_unavailable`, and `tiktok_identity_changed` in `ERROR_CODES` and `_MESSAGES` with fixed Chinese copy.

- [ ] **Step 6: Run focused tests**

Expected: normalization, link-first, text fallback, ambiguity, explicit login state and compatibility wrapper pass; error messages contain no DOM/URL sentinel.

- [ ] **Step 7: Checkpoint**

```bash
git add comment_campaign/identity.py comment_campaign/locator.py comment_campaign/errors.py comment_campaign/blueprint.py tests/test_comment_campaign_identity.py tests/test_comment_campaign_locator.py
git commit -m "feat: define TikTok identity observations"
```

---

### Task 7: Campaign-wide identity preflight and generation-aware queue jobs

**Files:**
- Modify: `comment_campaign/profile_gateway.py:57-124,163-233`
- Modify: `comment_campaign/executor.py:60-123`
- Modify: `comment_campaign/service.py:76-116`
- Modify: `comment_campaign/queueing.py:120-185`
- Modify: `comment_campaign/jobs.py`
- Modify: `comment_campaign/worker.py:150-260`
- Test: `tests/test_comment_campaign_profile_gateway.py`
- Test: `tests/test_comment_campaign_executor.py`
- Test: `tests/test_comment_campaign_queueing.py`
- Test: `tests/test_comment_campaign_worker.py`
- Test: `tests/test_comment_campaign_acceptance.py`

**Interfaces:**
- Produces: `ProfileGateway.acquire_campaign_lease(campaign_id) -> None`, `refresh_campaign_lease(campaign_id)`, and `open_identity_batch(profile_refs, campaign_id, expected_identity_generation)`.
- Produces internal `IdentityPreflightStale`; Campaign status/generation drift and ordinary lease contention use this no-write/no-pause signal, while Redis dependency failure remains `CampaignValidationError("redis_unavailable")`.
- Produces: `CommentExecutor.preflight_campaign_identities(campaign_id, expected_identity_generation) -> PreflightResult`.
- Produces internal `IdentityInvalidationOutcome(stale, identity_generation)`; preflight, prepare and submit must project it into their own existing return types.
- Changes: `CommentExecutor.prepare_batch(campaign_id, assignment_ids, identity_generation) -> BatchResult`.
- Changes: `enqueue_prepare_generation(campaign_id, generation, identity_generation)` and `run_prepare_campaign(campaign_id, generation, identity_generation)`.
- Changes: `enqueue_at(campaign_id, when, prepare_generation, identity_generation)`; scheduled approval allocates the prepare generation before enqueue.
- Consumes: Task 5 freeze/invalidate Store methods and Task 6 identity observer.

- [ ] **Step 1: Write failing queue contract tests**

```python
def test_prepare_job_carries_only_safe_ids_and_two_generations(fake_queue):
    coordinator.enqueue_prepare_generation("campaign-1", 7, 3)
    assert fake_queue.args == ("campaign-1", 7, 3)
    assert fake_queue.job_id == "campaign-prepare-campaign-1-g7"


def test_old_identity_generation_job_is_noop(runtime):
    runtime.campaign.identity_generation = 4
    result = run_prepare_campaign("campaign-1", 8, 3, runtime_factory=runtime.factory)
    assert result["stale"] is True
    assert runtime.adspower.start_calls == []
```

Add a concurrent lease-holder test: the loser receives a stale/no-op job result, performs zero Store writes/window opens/approvals/inputs/clicks, and does not cause RQ error/retry.

Update manual approval, scheduled approval, next-batch, resume, resolve-unverified, recovery and reconciliation enqueue calls to pass both values. `prepare_generation` remains the RQ delivery generation and is the only value included in the job ID; `identity_generation` is a safe job argument/CAS prerequisite and must not create a second RQ job for the same delivery generation. Remove the old single-generation `enqueue_prepare()` entry point or make it private and unreachable from production callers.

Add a parameterized matrix asserting exact args and the same `campaign-prepare-{campaign_id}-g{prepare_generation}` ID for every immediate/scheduled/recovery path. A scheduled path must first persist/allocate its prepare generation, then pass both integers to `enqueue_at`.

- [ ] **Step 2: Write failing cross-batch preflight tests**

Build six Fake Profile bindings with `batch_size=3`; windows 1 and 4 return the same account key. Assert:

```python
assert fake_page.input_count == 0
assert fake_submit.click_count == 0
assert store.list_approvals(campaign_id) == []
assert store.get_campaign(campaign_id)["status"] == "paused"
assert fake_adspower.max_concurrent_open == 3
assert batch_1.closed_at <= batch_2.opened_at
```

Add M>N coverage proving only final Assignment refs are opened, a unique-account success case, concurrent preflight CAS, unreadable/login-required/video-mismatch/CDP/open failures, heartbeat loss, and close-failure/no-next-batch. Every known failure asserts Campaign paused or stale no-op, zero approval/input/click, and no partial frozen observation. Add a deterministic barrier where failure is observed at revision/G1, another thread completes G2, then the old failure resumes; it must return stale and leave G2/status/evidence unchanged.

- [ ] **Step 3: Run queue/gateway/executor tests and confirm RED**

Expected: queue signatures lack identity generation and current prepare types before global uniqueness is known.

- [ ] **Step 4: Hold one Campaign lease across all identity batches**

Add a public acquire/refresh pair. `open_identity_batch` requires the current gateway already owns `campaign:{id}`, refreshes it, acquires only the batch's Profile leases, and reuses the exact start/connect/tile/close/quarantine logic. Before starting each batch and again after all pages connect, re-read the Campaign and require `status='running'` plus the expected identity generation. An outer heartbeat refreshes the Campaign lease and every still-open Profile lease until all batches close. Heartbeat loss or any unconfirmed close stops the scan and forbids the next batch. The Campaign lease is released only by the outermost `finally`; an unconfirmed Profile close keeps that Profile lease/quarantine. The preflight-specific gateway path reports a typed failure and may quarantine a Profile, but it must not independently transition Campaign status; `_invalidate_identity_or_stale` is the only Campaign/generation writer. Do not alter normal `open_many` status behavior.

- [ ] **Step 5: Implement in-memory full scan then one freeze transaction**

```python
@dataclass(frozen=True, slots=True)
class PreflightResult:
    stale: bool
    ready: bool
    identity_generation: int


async def preflight_campaign_identities(self, campaign_id, expected_identity_generation):
    campaign = self._campaign(campaign_id)
    if campaign["identity_generation"] != expected_identity_generation:
        return PreflightResult(
            stale=True, ready=False,
            identity_generation=campaign["identity_generation"],
        )
    start_revision = campaign["revision"]
    assignments = self.store.list_nonterminal_assignments(campaign_id)
    observations = []
    try:
        try:
            await self.gateway.acquire_campaign_lease(campaign_id)
        except IdentityPreflightStale:
            latest = self._campaign(campaign_id)
            return PreflightResult(True, False, latest["identity_generation"])
        except CampaignValidationError as error:
            if error.code != "redis_unavailable":
                raise
            outcome = self._invalidate_identity_or_stale(
                campaign_id, start_revision, expected_identity_generation,
                error_code=error.code,
                affected_assignment_ids=[row["assignment_id"] for row in assignments],
            )
            return PreflightResult(outcome.stale, False, outcome.identity_generation)
        for batch in chunks(assignments, int(campaign["batch_size"])):
            bindings = None
            failure = None
            try:
                bindings = await self.gateway.open_identity_batch(
                    [row["profile_ref"] for row in batch], campaign_id=campaign_id,
                    expected_identity_generation=expected_identity_generation,
                )
                observations.extend(await self._observe_identity_batch(campaign, batch, bindings))
            except IdentityPreflightStale:
                latest = self._campaign(campaign_id)
                return PreflightResult(True, False, latest["identity_generation"])
            except CampaignValidationError as error:
                if error.code not in PREFLIGHT_FAILURE_CODES:
                    raise
                failure = error.code
            finally:
                try:
                    closed = {} if bindings is None else await self.gateway.close_bindings(bindings)
                except CampaignValidationError as close_error:
                    if close_error.code not in PREFLIGHT_FAILURE_CODES:
                        raise
                    closed = {}
                    failure = close_error.code
            if bindings is not None and not all(closed.values()):
                failure = "profile_close_failed"
            if failure is not None:
                outcome = self._invalidate_identity_or_stale(
                    campaign_id, start_revision, expected_identity_generation,
                    error_code=failure,
                    affected_assignment_ids=[row["assignment_id"] for row in batch],
                )
                return PreflightResult(outcome.stale, False, outcome.identity_generation)
        try:
            frozen = self.store.freeze_campaign_identities(
                campaign_id, start_revision, expected_identity_generation, observations
            )
        except DuplicateTikTokAccountError as error:
            outcome = self._invalidate_identity_or_stale(
                campaign_id, start_revision, expected_identity_generation,
                error_code="duplicate_tiktok_account",
                affected_assignment_ids=list(error.assignment_ids),
                failure_details={"visible_username": error.visible_username},
            )
            return PreflightResult(outcome.stale, False, outcome.identity_generation)
        except RevisionConflictError:
            latest = self._campaign(campaign_id)
            return PreflightResult(True, False, latest["identity_generation"])
        return PreflightResult(
            stale=False, ready=True,
            identity_generation=frozen["identity_generation"],
        )
    finally:
        if self.gateway.owns_campaign_lease(campaign_id):
            await self.gateway.release_campaign_lease(campaign_id)
```

`_invalidate_identity_or_stale` always calls the generation-bound Store method with `start_revision` and `expected_identity_generation` and returns only `IdentityInvalidationOutcome`. If that Store CAS loses, it re-reads only to create `IdentityInvalidationOutcome(stale=True, latest_generation)` and never retries the write with a newer revision. The preflight boundary converts this to `PreflightResult`; it is never returned directly by `prepare_batch`, `submit_assignment`, an API route, or an RQ job. `PREFLIGHT_FAILURE_CODES` is the closed set `profile_start_failed`, `cdp_connect_failed`, `adspower_unavailable`, `redis_unavailable`, `target_video_mismatch`, `tiktok_login_required`, `tiktok_identity_unavailable`, and `profile_close_failed`. `_observe_identity_batch` may only `goto`, `verify_video`, read identity and capture safe evidence. It must not open the comment panel, focus input, call `human_type`, create approval or enqueue submit. Open/connect failures must expose only one of the closed codes plus affected safe Assignment IDs. Unknown Store/program exceptions propagate only after resources close. A duplicate error carries its two Assignment IDs and safe visible username internally; only the strict `identity_failure` projection from Task 5 becomes public.

- [ ] **Step 6: Gate the service job and continue only after freeze**

At job start, compare queue identity generation with Campaign. If stale, return no-op. Transition queued→running, run preflight when Store says it is required, and continue only when `PreflightResult.ready is True`. Then re-read Campaign and pass the returned generation to normal `prepare_batch`. No eligible Assignment query occurs before a valid freeze.

- [ ] **Step 7: Run focused preflight/queue tests**

Expected: unique scan freezes once and may prepare; cross-batch duplicate/unreadable/close failure has zero approval/input/click; stale jobs start no Profile; all RQ args contain only campaign ID and integers.

- [ ] **Step 8: Checkpoint**

```bash
git add comment_campaign/profile_gateway.py comment_campaign/executor.py comment_campaign/service.py comment_campaign/queueing.py comment_campaign/jobs.py comment_campaign/worker.py tests/test_comment_campaign_profile_gateway.py tests/test_comment_campaign_executor.py tests/test_comment_campaign_queueing.py tests/test_comment_campaign_worker.py tests/test_comment_campaign_acceptance.py
git commit -m "feat: preflight all Campaign accounts"
```

---

### Task 8: Enforce identity generation before every input and submit

**Files:**
- Modify: `comment_campaign/executor.py:124-217,219-283`
- Modify: `comment_campaign/store.py:990-1020,1190-1236`
- Modify: `comment_campaign/service.py:95-163`
- Test: `tests/test_comment_campaign_executor.py`
- Test: `tests/test_comment_campaign_threaded.py`
- Test: `tests/test_comment_campaign_recovery.py`
- Test: `tests/test_comment_campaign_receipts.py`

**Interfaces:**
- Produces: `CampaignStore.begin_comment_input(campaign_id, assignment_id, expected_revision, identity_generation) -> dict`.
- Changes: `CampaignStore.create_submit_approval(campaign_id, assignment_id, revision, approval_token)` requires Campaign/Assignment/account-preflight generation equality.
- Changes: `begin_submitting` consumes `expected_identity_generation`.
- Consumes internal `IdentityInvalidationOutcome`; `prepare_batch` maps it to `BatchResult`, while `submit_assignment` maps it to its existing safe dict result.
- Changes: normal prepare evidence preserves `account_preflight` and adds page/parent/input evidence beneath separate keys.

- [ ] **Step 1: Write failing stale-job tests**

```python
@pytest.mark.asyncio
async def test_old_generation_prepare_job_never_types(runtime):
    await runtime.preflight(generation=1)
    campaign = runtime.store.get_campaign(campaign_id)
    runtime.store.invalidate_campaign_identity(
        campaign_id, campaign["revision"], campaign["identity_generation"],
        error_code="tiktok_identity_changed",
        affected_assignment_ids=[assignment_id],
    )
    await runtime.executor.prepare_batch(campaign_id, [assignment_id], identity_generation=1)
    assert runtime.page.input_count == 0


@pytest.mark.asyncio
async def test_identity_change_after_approval_invalidates_every_unconsumed_approval(runtime):
    await runtime.approve_two_assignments(generation=1)
    runtime.page.account_handle = "changed.account"
    await runtime.executor.submit_assignment(campaign_id, assignment_id, approved_revision)
    assert runtime.submit.click_count == 0
    assert runtime.store.list_unconsumed_approvals(campaign_id) == []
    assert runtime.store.get_campaign(campaign_id)["status"] == "paused"
```

Add a stale submit RQ job test, a normal same-generation test, and a parent Receipt test proving runtime handle—not Profile metadata history—is used. Also add: generation-zero approval rejected; stale approval remains unconsumed; stale submit creates zero Receipt and performs zero click; two already-approved Assignments where one identity changes invalidate both approvals, then explicit resume plus full preflight returns all nonterminal Assignments to preparable states. Add deterministic CAS barriers for (a) `begin_comment_input` losing after another generation starts and (b) approval gate passing before another thread invalidates generation ahead of final submit CAS; both must leave zero input/click/Receipt/uncertain and keep approval unconsumed.

- [ ] **Step 2: Run executor/threaded/recovery tests and confirm RED**

Expected: current `_prepare_page` types before any generation check and current `begin_submitting` only checks status/revision.

- [ ] **Step 3: Add the last input CAS**

Immediately before focus/clear/`human_type`, call:

```python
current = self.store.begin_comment_input(
    campaign["id"], assignment["assignment_id"],
    current["revision"], campaign["identity_generation"],
)
```

The Store update must include Campaign `running`, nonzero generation, Assignment generation, and `account_preflight.identity_generation` checks. Only the returned revision may be used to persist `awaiting_step_approval`; no other Store path may synthesize that revision/status pair.

- [ ] **Step 4: Revalidate the whole prepare batch before any input**

After all batch windows connect, run a read-only phase across every Assignment: verify target video and call `read_tiktok_identity`; no comment panel, focus, clear or `human_type` is allowed during this phase. A barrier must confirm all identities match their frozen `account_preflight.account_key` before any `_prepare_page` proceeds. If one item fails, call the generation-bound helper once, close the entire batch, and return `BatchResult((), tuple(row["assignment_id"] for row in assignments), all(closed.values()))`. This prevents one concurrent Assignment from typing while another discovers identity drift.

```python
preflight = dict(assignment["evidence"]["account_preflight"])
try:
    actual = await read_tiktok_identity(page, account_definition, resolver=self.resolver)
except CampaignValidationError as error:
    if error.code not in RUNTIME_IDENTITY_FAILURE_CODES:
        raise
    outcome = self._invalidate_identity_or_stale(
        campaign["id"], campaign_snapshot_revision, expected_identity_generation,
        error_code=error.code,
        affected_assignment_ids=[assignment["assignment_id"]],
    )
    raise IdentityGenerationStopped(outcome)
if actual.account_key != preflight["account_key"]:
    outcome = self._invalidate_identity_or_stale(
        campaign["id"], campaign_snapshot_revision, expected_identity_generation,
        error_code="tiktok_identity_changed",
        affected_assignment_ids=[assignment["assignment_id"]],
    )
    raise IdentityGenerationStopped(outcome)
evidence = {
    "account_preflight": preflight,
    "page_evidence": page_evidence,
    "screenshot_path": screenshot_path,
}
```

Do not let `_preparation_evidence` replace `account_preflight`.

`campaign_snapshot_revision` and `expected_identity_generation` are captured before opening the Profile and never refreshed as write authority. `RUNTIME_IDENTITY_FAILURE_CODES` covers `profile_start_failed`, `cdp_connect_failed`, `adspower_unavailable`, `redis_unavailable`, `target_video_mismatch`, `tiktok_login_required`, `tiktok_identity_unavailable`, `tiktok_identity_changed`, and `profile_close_failed`. Prepare and submit use the same generation-bound `_invalidate_identity_or_stale`; a CAS loser is stale/no-op and cannot pause a newer generation. `IdentityGenerationStopped` is internal control flow only: `prepare_batch` catches it and returns the `BatchResult` above; `submit_assignment` catches it and returns `{"stale": outcome.stale, "submitted": False, "identity_generation": outcome.identity_generation}`. Neither public boundary returns `IdentityInvalidationOutcome` itself.

- [ ] **Step 5: Gate approval creation, approval consumption, and submit**

`create_submit_approval()` atomically requires Campaign `running`, `identity_generation>0`, Assignment generation equal to Campaign generation, and `evidence.account_preflight.identity_generation` equal to both. A submit job may perform a read-only early stale check, but it must not call the old standalone `consume_submit_approval()`. `begin_submitting` is the only consuming path: in one transaction it repeats the Campaign/Assignment/evidence generation checks, requires the exact approval with `consumed_at=''`, marks that approval consumed, and moves the Assignment to `submitting`. A stale/revision conflict rolls back every write and must never enter `comment_submit_uncertain`, create a Receipt, consume approval, or click. Identity mismatch invokes the atomic invalidation method. Recovery may enqueue only prepare/preflight; it must never replay submit or make generation valid by itself.

- [ ] **Step 6: Run focused safety tests**

Expected: stale generation prepare input count zero; stale submit click zero; changed account pauses Campaign and invalidates approvals; normal threaded Receipt and parent lookup use canonical runtime handle.

- [ ] **Step 7: Checkpoint**

```bash
git add comment_campaign/executor.py comment_campaign/store.py comment_campaign/service.py tests/test_comment_campaign_executor.py tests/test_comment_campaign_threaded.py tests/test_comment_campaign_recovery.py tests/test_comment_campaign_receipts.py
git commit -m "fix: gate Campaign input by account generation"
```

---

### Task 9: Auto/manual Profile selection UI and cached-state messaging

**Files:**
- Modify: `gateway/static/comment_campaign.js:1-175,215-245,390-410,510-575,685-716`
- Modify: `gateway/static/comment_campaign.css`
- Test: `tests-js/comment-campaign-ui.test.js`

**Interfaces:**
- Consumes: profile envelope `{data,meta}` from Task 3.
- Consumes: selection preview `{required_count,eligible_count,profiles}` from Task 4.
- Produces: `draftCampaign.selection_mode` and `draftCampaign.profile_refs` without visible internal IDs.

- [ ] **Step 1: Write failing Node/fake-DOM tests**

```javascript
test("poll uses cache-only GET and explicit sync runs only on initial load or button", async () => {
  await ui.poll(); await ui.poll();
  assert.equal(calls.filter(c => c.path.endsWith("/comment-profile-metadata/sync")).length, 0);
  await ui.syncProfiles();
  assert.equal(calls.filter(c => c.path.endsWith("/sync")).length, 1);
});

test("initialization triggers exactly one explicit sync", async () => {
  await ui.start();
  assert.equal(calls.filter(c => c.path.endsWith("/comment-profile-metadata/sync")).length, 1);
});

test("automatic selection submits hidden recommended refs", async () => {
  preview.respond({required_count: 3, eligible_count: 21, profiles: safeProfiles.slice(0, 3)});
  await ui.createCampaign();
  assert.deepEqual(lastPost.body.profile_refs, ["profile_ref_a", "profile_ref_b", "profile_ref_c"]);
  assert.equal(document.body.textContent.includes("profile_ref_"), false);
});
```

Also cover manual M<N allowing draft creation but disabling plan/lock, M>N candidate pool, template/mode change refresh, stale cache banner, fixed health reasons, duplicate-account pause rendering exactly the two `identity_failure.display_profiles` values plus `visible_username`, draft preservation across poll/409, XSS via `textContent`, keyboard checkboxes, and 360px no overflow. Assert this panel contains no account key, profile ref, raw ID or URL.

- [ ] **Step 2: Run Node test and confirm RED**

Run:

```powershell
node --test tests-js/comment-campaign-ui.test.js
```

Expected: current drawer still exposes a comma-separated Profile ref input and cannot parse profile `meta`.

- [ ] **Step 3: Separate cached Profile state from draft selection**

```javascript
const state = {
  profiles: [],
  profileMeta: {stale: false, last_synced_at: null, safe_reason: null},
  draftCampaign: {selection_mode: "automatic", profile_refs: []},
  // preserve all existing campaign/template/settings state
};
```

Modify `loadSnapshot` or add `loadProfilesCache` so the profile envelope is decoded without changing other resources. Initial `start()` performs cache GET, renders immediately, then one explicit sync; recurring poll performs only GET.

- [ ] **Step 4: Replace the free-text Profile ref input**

Render native radio/select controls for `自动选择` and `手动选择`. Automatic mode calls preview after template+mode are selected. Manual mode renders safe Profile cards/checkboxes with display name and window-level reason. Do not render `profile_ref`, expected username, login_verified, raw status or any internal ID as text/title/aria/data attributes.

Show:

```text
需要 3 个 · 已选择 3 个 · 当前可用 21 个
```

Allow Campaign draft creation when manual selection has at least one Profile. When M<N, disable plan/lock and show the exact shortage; the backend remains authoritative and returns 422 if called directly. Allow M>N as a candidate pool.

- [ ] **Step 5: Simplify Profile metadata editor**

Remove editable “预期账号/登录已验证” controls from the normal UI. Keep enabled, health/quarantine, tags, language, region display, and cooldown. When calling the compatibility POST, preserve legacy username/login values from server state without presenting them as planning requirements.

- [ ] **Step 6: Add safe health/cache messages**

Map only the fixed reasons from Task 2. Display `当前展示缓存数据，实际执行前需要 AdsPower 恢复` when stale. Status must include text, not color alone.

- [ ] **Step 7: Run Node tests and syntax checks**

```powershell
node --test tests-js/comment-campaign-ui.test.js
node --check gateway/static/comment_campaign.js
```

Expected: all tests pass, visible DOM contains no internal Profile refs, and existing Campaign/template/settings actions remain green.

- [ ] **Step 8: Checkpoint**

```bash
git add gateway/static/comment_campaign.js gateway/static/comment_campaign.css tests-js/comment-campaign-ui.test.js
git commit -m "feat: simplify Campaign Profile selection"
```

---

### Task 10: OpenAPI, security, regression, and controlled handoff

**Files:**
- Modify: `docs/architecture/api/openapi.yaml`
- Modify: `docs/architecture/api/error-codes.md`
- Modify: `docs/architecture/modules/comment-campaign.md`
- Modify: `docs/architecture/data/database-schema.md`
- Test: `tests/test_comment_campaign_security.py`
- Test: `tests/test_comment_campaign_integration.py`
- Test: `tests/test_comment_campaign_acceptance.py`

**Interfaces:**
- Documents the two new POST routes, profile cache envelope, strict allocation details, safe duplicate `identity_failure` projection, four identity errors, and two generation columns.

- [ ] **Step 1: Write failing OpenAPI contract assertions**

```python
def test_openapi_documents_profile_sync_and_selection_preview(openapi):
    sync = openapi["paths"]["/api/browser-v2/comment-profile-metadata/sync"]["post"]
    preview = openapi["paths"]["/api/browser-v2/comment-profile-selection/preview"]["post"]
    assert sync["requestBody"]["required"] is True
    assert preview["requestBody"]["content"]["application/json"]["schema"]["additionalProperties"] is False
    assert "422" in preview["responses"]
```

Assert the cache GET response has `data` array plus `meta`; allocation details enum matches the code; `identity_generation` is never writable by clients; duplicate failure details allow only two masked display names and one visible username.

- [ ] **Step 2: Update documentation exactly**

Document:

- health means only Local API list reachability;
- 4-second total deadline and fixed reasons;
- cache-only GET and explicit sync 200-stale behavior;
- automatic/manual candidate pool and backend matching;
- planning ignores historical username/login;
- Campaign-wide account preflight and generation lifecycle;
- prepare RQ job IDs use only `prepare_generation`, while every job argument also carries `identity_generation` for stale-job CAS;
- locked Campaign cannot swap windows;
- SQLite two-column idempotent migration;
- new stable errors and fixed Chinese messages.

- [ ] **Step 3: Run the security tripwire suite**

Inject raw AdsPower ID, cookie, Authorization, API key, ws/wss and exception-path sentinels into successful/error profile, preview, health, preflight, Attempt and Receipt projections. Assert zero matches in API JSON, rendered HTML, RQ args and persisted public evidence.

Install constructor/network/click bombs and run all new health/cache/selection tests. A test must explicitly prove no real AdsPower controller or TikTok submit is reached.

Add acceptance coverage where the same fixed AdsPower window participates in Campaign A and later Campaign B with different observed TikTok handles; each Campaign keeps its own frozen generation/evidence and neither overwrites the other's history.

- [ ] **Step 4: Run focused backend suites with formal DB guard**

```powershell
.venv\Scripts\python.exe -m pytest tests/test_comment_campaign_store.py tests/test_comment_campaign_service.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_security.py tests/test_comment_campaign_integration.py tests/test_comment_campaign_allocation.py -q -p no:cacheprovider
.venv\Scripts\python.exe -m pytest tests/test_comment_campaign_profile_gateway.py tests/test_comment_campaign_locator.py tests/test_comment_campaign_executor.py tests/test_comment_campaign_threaded.py tests/test_comment_campaign_receipts.py tests/test_comment_campaign_recovery.py tests/test_comment_campaign_acceptance.py -q -p no:cacheprovider
```

Expected: zero failures; any production DB hash/size/mtime/count change fails the session.

- [ ] **Step 5: Run frontend and syntax checks**

```powershell
node --test tests-js/comment-campaign-ui.test.js
node --check gateway/static/comment_campaign.js
.venv\Scripts\python.exe -m py_compile adspower.py gateway\adspower_config.py comment_campaign\adspower_health.py comment_campaign\identity.py comment_campaign\allocation.py comment_campaign\blueprint.py comment_campaign\executor.py comment_campaign\jobs.py comment_campaign\locator.py comment_campaign\models.py comment_campaign\profile_gateway.py comment_campaign\queueing.py comment_campaign\schemas.py comment_campaign\service.py comment_campaign\store.py comment_campaign\worker.py gateway\app.py
git diff --check
```

Expected: Comment Campaign Node suite fully green; Python compilation and diff check exit 0. Record unrelated existing failures separately; do not silently exclude a new regression.

- [ ] **Step 6: Run repository-wide regression gates**

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
npm run test:node
```

Expected: no new failures relative to the recorded baseline. The unconditional test DB guard remains active for the entire Python session. Any failure is classified and reported; no test is silently excluded.

- [ ] **Step 7: Sol final review gate**

Sol must review the final diff read-only for health deadline/config parity, cache isolation, matching correctness, migration safety, identity-generation races, cross-batch duplicates, submit fail-closed behavior, redaction and production DB isolation. Any P0 requires Luna fix and Sol re-review.

- [ ] **Step 8: Controlled handoff**

Report that automatic tests did not open real AdsPower/TikTok or publish comments. Real controlled acceptance requires explicit user authorization and should begin with three non-production Profiles, no submit approval, and visible verification of account preflight results.

Suggested final commit outside the read-only `.git` sandbox:

```bash
git add docs/architecture/api/openapi.yaml docs/architecture/api/error-codes.md docs/architecture/modules/comment-campaign.md docs/architecture/data/database-schema.md tests
git commit -m "docs: specify Campaign Profile allocation"
```
