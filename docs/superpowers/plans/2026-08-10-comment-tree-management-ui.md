# Comment Tree Management UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将评论树管理改为浅色、紧凑、面向业务语言的界面，并补齐启用、停用、软删除及历史 Campaign 安全兼容。

**Architecture:** `CommentTemplateRecord` 保存当前生命周期，revision 保存不可变快照；普通 API 隐藏软删除记录，内部仍可按 revision 读取历史。Service 在 Campaign 锁定前检查模板可用性，锁定后只使用冻结数据。前端把评论树列表与创建流程拆成互不覆盖的视图。

**Tech Stack:** Python 3.13、Flask、SQLAlchemy、Pydantic、SQLite、原生 JavaScript/HTML/CSS、Node test runner、pytest。

## Global Constraints

- 评论 Campaign 模块使用浅色主题；不得修改其他模块主题。
- UI 只显示“盖楼回复/独立评论”，不得显示 raw mode 或内部模板、步骤、父步骤、文案库、文案项 ID。
- 生命周期只允许 enabled→disabled、disabled→enabled、disabled→deleted。
- `deleted_at IS NOT NULL` 必须同时满足 `enabled=False`。
- 生命周期变更必须在同一事务完成 revision CAS、master 更新、revision snapshot 和 steps 写入。
- 只有 `locked_at` 非空 Campaign 获得祖父化资格；未锁定 Campaign 不得原地换树。
- 普通 API 对 deleted 返回 404；内部历史 revision 仍可读。
- 自动测试必须使用临时数据库，不得写正式 Campaign DB，不得连接真实 AdsPower/TikTok/Worker。

---

### Task 1: Template lifecycle persistence and SQLite migration

**Files:**
- Modify: `comment_campaign/models.py:13-24`
- Modify: `comment_campaign/store.py:70-180,1398-1422`
- Test: `tests/test_comment_campaign_store.py`

**Interfaces:**
- Produces: `CampaignStore.enable_template(template_id: str, expected_revision: int) -> dict`
- Produces: `CampaignStore.delete_template(template_id: str, expected_revision: int) -> dict`
- Produces: `CampaignStore.get_template_lifecycle(template_id: str) -> str | None`
- Preserves: current get/list hide deleted; explicit historical revision remains readable.

- [ ] **Step 1: Write failing lifecycle and migration tests**

Add a full transition test:

```python
def test_template_lifecycle_is_revision_guarded_and_deleted_is_hidden(store):
    created = store.create_template(_template(), "template-1")
    disabled = store.disable_template("template-1", created["revision"])
    enabled = store.enable_template("template-1", disabled["revision"])
    disabled = store.disable_template("template-1", enabled["revision"])
    deleted = store.delete_template("template-1", disabled["revision"])
    assert deleted["lifecycle_status"] == "deleted"
    assert store.list_templates() == []
    assert store.get_template("template-1") is None
    assert store.get_template("template-1", revision=1)["steps"][0]["id"] == "root"
```

Also test update/disable only while enabled; enable/delete only while disabled; deleted actions are 404; visible wrong revision is 409 before state; forced revision-write failure rolls back; old SQLite schema gains nullable `deleted_at`; repeated `initialize()` succeeds; old revision/step rows remain. Add a direct SQL test that bypasses Store: setting non-null `deleted_at` while `enabled=1` raises `IntegrityError`, while `enabled=0` plus `deleted_at` succeeds.

- [ ] **Step 2: Run the store tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_comment_campaign_store.py -q -p no:cacheprovider
```

Expected: missing field/method/lifecycle assertions fail.

- [ ] **Step 3: Add model field and idempotent migration**

Import `CheckConstraint`, add a named table constraint, and add the field:

```python
__table_args__ = (
    CheckConstraint(
        "deleted_at IS NULL OR enabled = 0",
        name="ck_comment_template_deleted_disabled",
    ),
)
deleted_at: Mapped[str | None] = mapped_column(String(40), nullable=True, default=None)
```

Use `PRAGMA table_info(comment_templates)` and run the following only when absent:

```sql
ALTER TABLE comment_templates
ADD COLUMN deleted_at VARCHAR(40)
CHECK (deleted_at IS NULL OR enabled = 0)
```

The named SQLAlchemy `CheckConstraint` covers new databases and non-SQLite metadata; the column-level SQLite CHECK covers existing SQLite databases migrated with `ALTER TABLE`.

- [ ] **Step 4: Implement lifecycle snapshots and CAS transitions**

Use exactly:

```python
def _lifecycle(enabled: bool, deleted_at: str | None) -> str:
    if deleted_at:
        return "deleted"
    return "enabled" if enabled else "disabled"
```

Every new snapshot stores `enabled` and `lifecycle_status`. Old snapshots without lifecycle status map only from their `enabled` value. Enable/delete must follow the existing disable transaction style and re-materialize unchanged steps into the new revision. Never delete old rows.

- [ ] **Step 5: Filter deleted records and preserve internal history**

`list_templates()` adds `deleted_at IS NULL`; current `get_template(id)` returns `None` when deleted; `get_template(id, revision=N)` remains readable. Add `get_template_lifecycle()` for internal guards.

- [ ] **Step 6: Run store tests and verify GREEN**

Run Step 2. Expected: all pass.

- [ ] **Step 7: Commit**

```powershell
git add comment_campaign/models.py comment_campaign/store.py tests/test_comment_campaign_store.py
git commit -m "feat(comment): add template lifecycle state"
```

If `.git/index.lock` is denied, record the blocker and do not alter Git permissions or stage unrelated files.

---

### Task 2: Service, API, errors, and authorization

**Files:**
- Modify: `comment_campaign/errors.py`
- Modify: `comment_campaign/blueprint.py:39-70,126-155,405-435`
- Modify: `comment_campaign/service.py:175-230`
- Test: `tests/test_comment_campaign_service.py`
- Test: `tests/test_comment_campaign_routes.py`
- Test: `tests/test_comment_campaign_security.py`

**Interfaces:**
- Produces service methods `enable_template` and `delete_template`.
- Produces POST routes `/comment-templates/<id>/enable` and `/comment-templates/<id>/delete`.
- Reuses strict `ExpectedRevision` request model.

- [ ] **Step 1: Write failing service/route/security tests**

Add exact route cases:

```python
("POST", "/comment-templates/template-1/enable", {"expected_revision": 2}, 200, "enable_template")
("POST", "/comment-templates/template-1/delete", {"expected_revision": 3}, 200, "delete_template")
```

Test missing body, string revision and extra fields as 422. Test error priority 404→revision conflict→invalid state. Cover legacy anonymous, operator write, administrator CSRF, local foreign Host/REMOTE_ADDR before factory, temporary DB and recursive redaction.

- [ ] **Step 2: Run focused tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_comment_campaign_service.py tests\test_comment_campaign_routes.py tests\test_comment_campaign_security.py -q -p no:cacheprovider
```

- [ ] **Step 3: Register stable error and service wrappers**

Register `template_unavailable` as HTTP 409 with fixed message `所选评论树已停用或删除。`. Add service wrappers that directly call Task 1 store methods.

- [ ] **Step 4: Register strict routes**

Use this shape for both actions:

```python
@blueprint.post("/comment-templates/<template_id>/enable")
def enable_template(template_id: str):
    payload = _parse(ExpectedRevision)
    return _data(_call(service(), "enable_template", template_id, payload.expected_revision))
```

Delete uses the same shape and never physically deletes.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run Step 2. Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add comment_campaign/errors.py comment_campaign/blueprint.py comment_campaign/service.py tests/test_comment_campaign_service.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_security.py
git commit -m "feat(comment): expose template lifecycle actions"
```

---

### Task 3: Locked Campaign grandfathering

**Files:**
- Modify: `comment_campaign/service.py:320-390`
- Test: `tests/test_comment_campaign_service.py`
- Test: `tests/test_comment_campaign_integration.py`
- Test: `tests/test_comment_campaign_acceptance.py`

**Interfaces:**
- Consumes `get_template_lifecycle`.
- Produces `_require_unlocked_template_available(campaign: Mapping[str, Any]) -> None`.
- Preserves locked execution as snapshot-only.

- [ ] **Step 1: Write failing unlocked and locked tests**

For disabled and deleted templates, assert plan/reallocate/lock/approve on an unlocked Campaign raise code `template_unavailable`. For a threaded Campaign, freeze snapshot and Assignments, lock it, delete the template, then approve/queue using fake dependencies and assert resolved text, parent assignment, role, position and template snapshot remain byte-for-byte unchanged.

Install bombs for AdsPower, TikTok, real Redis and submit clicks.

- [ ] **Step 2: Run Campaign tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_comment_campaign_service.py tests\test_comment_campaign_integration.py tests\test_comment_campaign_acceptance.py -q -p no:cacheprovider
```

- [ ] **Step 3: Add one availability guard**

```python
def _require_unlocked_template_available(self, campaign: Mapping[str, Any]) -> None:
    if campaign.get("locked_at"):
        return
    if self.store.get_template_lifecycle(str(campaign["template_id"])) != "enabled":
        raise CampaignValidationError("template_unavailable")
```

Call before plan/reallocate, lock-plan and approve. Do not add a Campaign template-update API/UI and do not add current-template reads to queued/running executor paths.

- [ ] **Step 4: Run Campaign tests and verify GREEN**

Run Step 2. Expected: all pass and bombs remain untouched.

- [ ] **Step 5: Commit**

```powershell
git add comment_campaign/service.py tests/test_comment_campaign_service.py tests/test_comment_campaign_integration.py tests/test_comment_campaign_acceptance.py
git commit -m "fix(comment): guard unlocked template use"
```

---

### Task 4: Compact light UI and direct creation flows

**Files:**
- Modify: `gateway/static/comment_campaign.js:232-351,552-628,696`
- Modify: `gateway/static/comment_campaign.css`
- Modify: `gateway/templates/comment_campaign.html`
- Test: `tests-js/comment-campaign-ui.test.js`

**Interfaces:**
- Consumes lifecycle endpoints and existing disable/update/import endpoints.
- Produces `templateView` values: `list`, `choose`, `manual`, `excel`, `readonly`.
- Preserves manual/import/Campaign drafts, management fetch and polling isolation.

- [ ] **Step 1: Write failing DOM/state tests**

Assert: active list plus closed native `details` disabled list; inline action groups; Chinese mode/status; no raw mode/internal IDs in visible/accessibility text; confirm-cancel sends zero requests; enable/delete exact URL/body; 409 keeps both drafts and does not retry; new-tree view contains only manual/Excel choices; manual begins with a root comment; Excel directly shows file/preview; polling preserves focused nodes; library/multi-mode remains readonly; no `innerHTML`/localStorage.

- [ ] **Step 2: Run Node tests and confirm RED**

```powershell
node --test tests-js/comment-campaign-ui.test.js
```

- [ ] **Step 3: Separate list and creation state**

Initialize `templateView="list"`. “新建评论树” opens `choose`; buttons open `manual` or `excel`. Reuse `CommentTreeEditor.render` unchanged. Back returns to list without clearing unrelated drafts.

Use one mapping:

```javascript
function templateModeLabel(template) {
  return (template.supported_modes || []).includes("threaded") ? "盖楼回复" : "独立评论";
}
```

- [ ] **Step 4: Implement compact lifecycle lists and actions**

Render enabled and disabled arrays separately. Disabled records live in closed `details.template-disabled`. Add `enableTemplate` and `deleteTemplate`; delete must call `confirm("删除后该评论树将从普通界面消失且不能恢复，是否继续？")` before requesting. Lifecycle 409 refreshes once and never automatically repeats a write.

- [ ] **Step 5: Replace only Campaign dark styles with scoped light styles**

Remove global `:root` color/background. Scope under `.comment-campaign-page` using background `#f6f7fb`, card `#ffffff`, border `#e2e8f0`, text `#172033`, muted `#64748b`, hover `#f1f5f9`, accent `#0f766e`. Add flex `.template-row/.template-actions`; actions stay inline and wrap only on narrow screens. Preserve focus-visible and textual state. At 820/480/360 widths no horizontal overflow.

- [ ] **Step 6: Run UI checks and verify GREEN**

```powershell
node --test tests-js/comment-campaign-ui.test.js
node --check gateway/static/comment_campaign.js
node --check gateway/static/comment_tree_editor.js
```

- [ ] **Step 7: Commit**

```powershell
git add gateway/static/comment_campaign.js gateway/static/comment_campaign.css gateway/templates/comment_campaign.html tests-js/comment-campaign-ui.test.js
git commit -m "feat(comment): simplify tree management UI"
```

---

### Task 5: Contracts, pollution tripwire, and final regression

**Files:**
- Modify: `docs/architecture/api/openapi.yaml`
- Modify: `docs/architecture/api/error-codes.md`
- Modify: `docs/architecture/modules/comment-campaign.md`
- Modify: `docs/architecture/data/database-schema.md`
- Test: `tests/test_comment_campaign_integration.py`
- Modify: `tests/conftest.py` only if any fixture still points at the production Campaign DB.

**Interfaces:**
- Documents lifecycle routes, error, deleted field, snapshot compatibility and locked grandfathering.
- Verifies formal DB bytes/counts are unchanged by tests.

- [ ] **Step 1: Add an executable production-database pollution tripwire**

Before app/service construction, assert `COMMENT_CAMPAIGN_DB_URL` contains `tmp_path` and `comment_campaign_service` is absent from extensions. In `tests/conftest.py`, add opt-in hooks using this exact mechanism (with the file's existing imports merged rather than duplicated):

```python
def _production_campaign_db_snapshot():
    path = Path(__file__).resolve().parents[1] / "data/comment_campaign/comment_campaign.db"
    if not path.exists():
        return None
    stat = path.stat()
    connection = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
    try:
        counts = (
            connection.execute("SELECT COUNT(*) FROM comment_templates").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM comment_template_revisions").fetchone()[0],
        )
    finally:
        connection.close()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), stat.st_mtime_ns, stat.st_size, counts)


def pytest_sessionstart(session):
    if os.getenv("COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD") == "1":
        session.config._comment_campaign_db_baseline = _production_campaign_db_snapshot()


def pytest_sessionfinish(session, exitstatus):
    baseline = getattr(session.config, "_comment_campaign_db_baseline", None)
    if baseline is not None and baseline != _production_campaign_db_snapshot():
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_sep("=", "production Comment Campaign DB changed")
```

The hooks are enabled only by `COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD=1`. They never open the production DB writable and never delete the existing five `A` rows.

Run the focused backend suite with:

```powershell
$env:COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD='1'
.\.venv\Scripts\python.exe -m pytest tests\test_comment_campaign_store.py tests\test_comment_campaign_service.py tests\test_comment_campaign_routes.py tests\test_comment_campaign_integration.py tests\test_comment_campaign_security.py tests\test_comment_campaign_acceptance.py -q -p no:cacheprovider
Remove-Item Env:\COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD
```

- [ ] **Step 2: Update OpenAPI and error table**

Document both strict ExpectedRevision POST routes with 200/404/409, no extra properties, and `template_unavailable` 409 with fixed message and non-retry guidance.

- [ ] **Step 3: Update module and database docs**

Document transition matrix, nullable `deleted_at`, snapshot `lifecycle_status` compatibility, public hiding/internal history, `locked_at` grandfathering, no in-place Campaign tree replacement, and explicit deletion requirement for the five disabled `A` rows.

- [ ] **Step 4: Run backend regression with the pollution guard enabled**

```powershell
$env:COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD='1'
.\.venv\Scripts\python.exe -m pytest tests\test_comment_campaign_store.py tests\test_comment_campaign_service.py tests\test_comment_campaign_routes.py tests\test_comment_campaign_integration.py tests\test_comment_campaign_security.py tests\test_comment_campaign_acceptance.py -q -p no:cacheprovider
Remove-Item Env:\COMMENT_CAMPAIGN_PRODUCTION_DB_GUARD
```

- [ ] **Step 5: Run UI/execution safety regression**

```powershell
node --test tests-js/comment-campaign-ui.test.js
.\.venv\Scripts\python.exe -m pytest tests\test_comment_campaign_allocation.py tests\test_comment_campaign_threaded.py tests\test_comment_campaign_executor.py tests\test_comment_campaign_recovery.py -q -p no:cacheprovider
```

- [ ] **Step 6: Run static checks**

```powershell
.\.venv\Scripts\python.exe -m py_compile comment_campaign\models.py comment_campaign\store.py comment_campaign\service.py comment_campaign\blueprint.py
node --check gateway/static/comment_campaign.js
git diff --check
git status --short
```

Expected: syntax/tests pass; unrelated dirty files remain untouched.

- [ ] **Step 7: Final Sol review**

Sol checks lifecycle priority, transaction atomicity, frozen Campaign compatibility, API hiding, UI ID boundaries, test isolation and recorded regression results. Terra fixes blockers and Sol re-reviews.

- [ ] **Step 8: Commit docs and final tests**

```powershell
git add docs/architecture/api/openapi.yaml docs/architecture/api/error-codes.md docs/architecture/modules/comment-campaign.md docs/architecture/data/database-schema.md tests/test_comment_campaign_integration.py tests/conftest.py
git commit -m "docs(comment): document template lifecycle"
```

If Git metadata remains read-only, report the exact blocker and leave verified changes unstaged.
