# Remote Action Contracts and Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 冻结 Central 与 Agent 共用的远程动作协议，并让浏览器策略和评论 Campaign 拥有全局唯一动作 ID、可校验 checksum 和不可变发布版本。

**Architecture:** JSON Schema 是跨进程线协议的唯一真相源；Python 契约层负责加载 Schema、枚举和参数绑定，JavaScript 只实现同一 RFC 8785 canonical JSON/checksum 向量。两个本机动作库继续各自持久化草稿和调试记录，中控新增不可变 ActionRevision；发布只接受最近一次成功调试的精确 `content_checksum`。

**Tech Stack:** Python 3、Pydantic 2、jsonschema、SQLAlchemy 2、SQLite/PostgreSQL、vanilla JavaScript、Node test runner、pytest。

## Global Constraints

- `action_id` 全局唯一且永不复用；它与现有策略/Campaign 本地主键分离，固定使用 `act_<26 位 Crockford Base32 ULID>`。
- `revision` 只属于一个 `action_id`，发布后不可修改；修改动作内容只能发布新 revision。
- `content_checksum` 不包含 `action_id/revision`；`release_checksum` 必须包含二者。
- canonical JSON 使用 RFC 8785 JCS；checksum 使用 SHA-256 小写十六进制。
- WorkOrder 只允许按 `parameter_schema` 中声明的 JSON Pointer 绑定动态参数，禁止通用 deep merge。
- 所有 Schema 顶层 `additionalProperties` 为 `false`，并设置字符串、数组和对象数量上限。
- 本阶段不建立生产设备凭据；不得以 tenant/device 查询参数作为生产认证。

---

### Task 1: 建立版本化线协议 Schema

**Files:**
- Create: `docs/architecture/messaging/schemas/remote-actions/wss-envelope-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/work-order-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/command-ack-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/cancel-command-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/progress-event-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/terminal-reference-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/reconcile-request-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/reconcile-response-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/effect-permit-v1.schema.json`
- Create: `docs/architecture/messaging/schemas/remote-actions/execution-outcome-v1.schema.json`
- Create: `tests/fixtures/remote_actions/protocol_vectors.json`
- Create: `tests/test_remote_action_schemas.py`
- Modify: `requirements.txt`

- [x] **Step 1: 写 golden vector 与拒绝向量测试**

```python
SCHEMAS = {
    "WORK_ORDER_DELIVER": "work-order-v1.schema.json",
    "COMMAND_ACK": "command-ack-v1.schema.json",
    "WORK_ORDER_CANCEL": "cancel-command-v1.schema.json",
    "PROGRESS_EVENT": "progress-event-v1.schema.json",
    "TERMINAL_REFERENCE": "terminal-reference-v1.schema.json",
    "RECONCILE_REQUEST": "reconcile-request-v1.schema.json",
    "RECONCILE_RESPONSE": "reconcile-response-v1.schema.json",
}

@pytest.mark.parametrize("vector", load_vectors())
def test_protocol_vector(vector):
    validator = validator_for(vector["schema"])
    if vector["valid"]:
        validator.validate(vector["payload"])
    else:
        with pytest.raises(ValidationError):
            validator.validate(vector["payload"])
```

每种 Schema 至少提供 1 个有效向量，以及字段缺失、类型错误、额外字段、超限 4 类无效向量。

- [x] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_remote_action_schemas.py -q
```

Expected: FAIL，Schema 目录和 vector 尚不存在。

- [x] **Step 3: 创建 Schema 并加入 `jsonschema>=4.23,<5.0`**

WSS 信封固定为：

```json
{
  "type": "WORK_ORDER_DELIVER",
  "protocol_version": "1.0",
  "message_id": "msg_2f2bcf5d9d664b75a7c5aa48e8caf601",
  "device_id": "dev_7f9823833838463b8bd121d0ff0ed9a8",
  "session_id": "sess_b1318dfe6c7d4b89ab0de92315ec6264",
  "server_sequence": 42,
  "sent_at": "2026-08-20T10:00:00.000Z",
  "payload": {}
}
```

Agent→Central 信封不得出现 `server_sequence`；Central→Agent 信封必须出现正整数 `server_sequence`。两向信封都必须出现 `type`、`protocol_version`、`message_id`、`device_id`、`session_id`、`sent_at` 和 `payload`。WorkOrder 必须包含设计文档 §7 的全部身份、动作快照、参数、资源和 generation 字段。

`resource_reservations` 是多个原子资源项，不是账号/窗口合并对象。账号项固定为 `{"resource_key":"account:<account_id>","resource_type":"account","account_id":"<account_id>"}`；窗口项固定为 `{"resource_key":"window:<device_id>:<window_ref>","resource_type":"window","device_id":"<device_id>","window_ref":"<window_ref>"}`。每个 item 禁止额外字段，WorkOrder Schema 要求 resource key 与结构化字段一致。

- [x] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_remote_action_schemas.py -q
```

Expected: PASS，完成 `C-01` 与信封部分 `C-03`。

- [ ] **Step 5: 提交本任务**

```powershell
git add requirements.txt docs/architecture/messaging/schemas/remote-actions tests/fixtures/remote_actions/protocol_vectors.json tests/test_remote_action_schemas.py
git commit -m "feat: freeze remote action wire schemas"
```

### Task 2: 实现共享枚举、Schema 校验与参数绑定

**Files:**
- Create: `remote_actions/__init__.py`
- Create: `remote_actions/enums.py`
- Create: `remote_actions/contracts.py`
- Create: `remote_actions/parameters.py`
- Create: `tests/test_remote_action_contracts.py`

- [x] **Step 1: 写枚举穷举和参数越权测试**

```python
def test_bind_parameters_changes_only_declared_pointer():
    snapshot = {"target": {"url": "old"}, "execution": {"timeout": 30}}
    schema = {"bindings": {"target_url": {"pointer": "/target/url", "type": "string"}}}
    bound = bind_parameters(snapshot, schema, {"target_url": "https://tiktok.example/v/1"})
    assert bound == {"target": {"url": "https://tiktok.example/v/1"}, "execution": {"timeout": 30}}

@pytest.mark.parametrize("params", [
    {"unknown": 1},
    {"target_url": 7},
    {"target_url": {"$merge": {"execution": {"timeout": 0}}}},
])
def test_bind_parameters_rejects_undeclared_or_wrong_values(params):
    with pytest.raises(ParameterBindingError):
        bind_parameters(SNAPSHOT, PARAMETER_SCHEMA, params)
```

- [x] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_remote_action_contracts.py -q
```

Expected: FAIL，`remote_actions` 包不存在。

- [x] **Step 3: 实现严格契约入口**

```python
class MessageType(StrEnum):
    WORK_ORDER_DELIVER = "WORK_ORDER_DELIVER"
    COMMAND_ACK = "COMMAND_ACK"
    WORK_ORDER_CANCEL = "WORK_ORDER_CANCEL"
    PROGRESS_EVENT = "PROGRESS_EVENT"
    TERMINAL_REFERENCE = "TERMINAL_REFERENCE"
    RECONCILE_REQUEST = "RECONCILE_REQUEST"
    RECONCILE_RESPONSE = "RECONCILE_RESPONSE"

VALIDATORS: Mapping[MessageType, Draft202012Validator]

def validate_message(message_type: MessageType, payload: dict) -> None:
    VALIDATORS[message_type].validate(payload)

def bind_parameters(snapshot: dict, schema: dict, values: dict) -> dict:
    return ParameterBinder(schema).apply(snapshot, values)
```

同时定义 ACK、运行状态、终态、effect/permit、错误类别枚举，并用参数化测试保证每个值都有显式映射。

- [x] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_remote_action_contracts.py tests/test_remote_action_schemas.py -q
```

Expected: PASS，完成 `C-04`、`C-05`。

- [ ] **Step 5: 提交本任务**

```powershell
git add remote_actions tests/test_remote_action_contracts.py
git commit -m "feat: add strict remote action contracts"
```

### Task 3: 实现 Python/JavaScript 一致的 checksum

**Files:**
- Create: `remote_actions/checksums.py`
- Create: `remote_actions/identifiers.py`
- Create: `gateway/static/action_checksums.js`
- Create: `tests/fixtures/remote_actions/checksum_vectors.json`
- Create: `tests/test_remote_action_checksums.py`
- Create: `tests/test_remote_action_identifiers.py`
- Create: `tests-js/action-checksums.test.js`

- [x] **Step 1: 写 50 组跨语言固定向量测试**

```python
def test_checksum_vectors_match_python():
    for vector in load_vectors():
        assert content_checksum(vector["content_input"]) == vector["content_checksum"]
        assert release_checksum(**vector["release_input"]) == vector["release_checksum"]
```

```javascript
test("checksum vectors match JavaScript", async () => {
  for (const vector of vectors) {
    assert.equal(await api.contentChecksum(vector.content_input), vector.content_checksum);
    assert.equal(await api.releaseChecksum(vector.release_input), vector.release_checksum);
  }
});
```

- [x] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_remote_action_checksums.py tests/test_remote_action_identifiers.py -q
node --test tests-js/action-checksums.test.js
```

Expected: FAIL，checksum、ULID 实现和向量尚不存在。

- [x] **Step 3: 实现算法版本和固定输入投影**

```python
CONTENT_ALGORITHM = "action-content-jcs-sha256-v1"
RELEASE_ALGORITHM = "action-release-jcs-sha256-v1"

def content_projection(release: dict) -> dict:
    return {key: release[key] for key in (
        "executor_kind", "definition_schema_version", "parameter_schema",
        "result_schema", "snapshot", "execution_defaults",
    )}
```

向量必须覆盖 Unicode、整数/小数、嵌套对象、数组和不同键顺序；禁止 NaN/Infinity。

`new_action_id()` 使用毫秒时间戳和加密随机数组成 ULID，输出必须匹配 `^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$`；首字符限制保证 26 位编码不超过 ULID 的 128 位范围。测试固定时间和随机字节，验证同毫秒调用的单调性、复制生成新 ID、编辑/发布保持 ID。

- [x] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_remote_action_checksums.py tests/test_remote_action_identifiers.py -q
node --test tests-js/action-checksums.test.js
```

Expected: PASS，完成 `C-02`。

- [ ] **Step 5: 提交本任务**

```powershell
git add remote_actions/checksums.py remote_actions/identifiers.py gateway/static/action_checksums.js tests/fixtures/remote_actions/checksum_vectors.json tests/test_remote_action_checksums.py tests/test_remote_action_identifiers.py tests-js/action-checksums.test.js
git commit -m "feat: add canonical action checksums"
```

### Task 4: 增加本机调试门禁与动作发布元数据

**Files:**
- Modify: `execution_v2/store.py`
- Modify: `execution_v2/service.py`
- Modify: `comment_campaign/models.py`
- Modify: `comment_campaign/database.py`
- Modify: `comment_campaign/store.py`
- Modify: `comment_campaign/service.py`
- Create: `tests/test_action_publication_metadata.py`
- Modify: `tests/test_execution_v2_store.py`
- Modify: `tests/test_comment_campaign_store.py`

- [x] **Step 1: 写兼容迁移和发布门禁测试**

```python
def test_publish_requires_latest_successful_debug_of_same_content(action_service):
    action = action_service.create(
        name="Open target",
        definition={"actions": [{"type": "open", "url": "https://example.test"}]},
    )
    with pytest.raises(PublishGateError):
        action_service.prepare_release(action["id"], action["revision"])
    action_service.record_debug(action["id"], action["revision"], status="SUCCEEDED")
    release = action_service.prepare_release(action["id"], action["revision"])
    assert release["content_checksum"] == action_service.get(action["id"])["content_checksum"]

def test_admin_waiver_is_explicit_and_audited(action_service):
    release = action_service.prepare_release(
        action_id="act_1234",
        action_revision=1,
        waive_validation=True,
        actor="admin-1",
        reason="incident",
    )
    assert release["validation_status"] == "waived"
```

- [x] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_action_publication_metadata.py tests/test_execution_v2_store.py tests/test_comment_campaign_store.py -q
```

Expected: FAIL，本机库没有 publication/debug 元数据。

- [x] **Step 3: 为两个独立动作库加入相同语义的表**

Browser V2 SQLite 增加 `strategy_action_identities`、`strategy_debug_runs`、`strategy_releases`；Campaign SQLAlchemy 增加 `CommentActionIdentityRecord`、`CommentActionDebugRunRecord`、`CommentActionReleaseRecord`。identity 表分别以现有 `strategy_id/campaign_id` 为唯一外键，并保存独立 `action_id`、`tombstoned_at`。两边均提供：

```python
record_debug_run(action_id, action_revision, content_checksum, status, run_id, finished_at)
prepare_release(action_id, action_revision, actor, waive_validation=False, reason="")
mark_release_synced(action_id, revision, central_revision, synced_at)
```

迁移时调用 `new_action_id()` 为每条已有记录生成并持久化一次 ULID；以后读取不得重新生成。新建策略/Campaign 在同一创建事务内写 identity。不得改写已有本地主键或执行记录外键，两个动作库也不得通过本地 ID 推导 action ID。

删除动作时先写 identity tombstone，再删除或归档业务草稿；复制和“另存为”必须生成新 action ID。测试必须证明 tombstoned action ID 不能再次绑定到新本地记录。

- [x] **Step 4: 运行测试确认通过**

```powershell
python -m pytest tests/test_action_publication_metadata.py tests/test_execution_v2_store.py tests/test_comment_campaign_store.py -q
```

Expected: PASS，旧数据库可打开，未发布草稿仍可本机调试。

- [ ] **Step 5: 提交本任务**

```powershell
git add execution_v2 comment_campaign tests/test_action_publication_metadata.py tests/test_execution_v2_store.py tests/test_comment_campaign_store.py
git commit -m "feat: gate immutable local action releases"
```

### Task 5: 建立中控不可变动作版本与同步 API

**Files:**
- Create: `central/action_models.py`
- Create: `central/actions.py`
- Modify: `central/app.py`
- Modify: `central/migrate.py`
- Create: `tests/test_central_actions.py`
- Modify: `tests/test_central_migrate.py`

- [x] **Step 1: 写幂等发布、冲突和不可变测试**

必须实现以下具名测试和断言：

- `test_same_release_is_idempotent_and_different_payload_conflicts`：同 checksum 两次 PUT 均返回 200 和相同记录 ID；同 action/revision 的不同 checksum 返回 409。
- `test_published_revision_cannot_be_updated_or_deleted`：发布记录没有 PATCH/DELETE 成功路径。
- `test_action_ids_are_unique_across_executor_kinds`：相同 action ID 不能分别登记为两个 executor kind。
- `test_sync_inventory_returns_missing_and_checksum_mismatch`：比较结果精确区分 `missing_remote`、`missing_local`、`checksum_mismatch`、`in_sync`。

- [x] **Step 2: 运行测试确认失败**

```powershell
python -m pytest tests/test_central_actions.py tests/test_central_migrate.py -q
```

Expected: FAIL，路由和表不存在。

- [x] **Step 3: 实现表与 API**

```python
class ActionDefinition(Base):
    __tablename__ = "action_definitions"
    action_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(64), nullable=False)

class ActionRevision(Base):
    __tablename__ = "action_revisions"
    __table_args__ = (UniqueConstraint("action_id", "revision"),)
    content_checksum: Mapped[str] = mapped_column(String(71), nullable=False)
    release_checksum: Mapped[str] = mapped_column(String(71), nullable=False, unique=True)
```

两个 checksum 列都必须校验 `^sha256:[0-9a-f]{64}$`，不得截断算法前缀。

路由固定为 `PUT /api/central/actions/{action_id}/revisions/{revision}`、`POST /api/central/actions/sync/compare`、`GET /api/central/actions/{action_id}/revisions/{revision}`。PUT 的同 checksum 重试返回原记录，不同 payload 返回 409。

- [x] **Step 4: 运行本计划全部测试**

```powershell
python -m pytest tests/test_remote_action_schemas.py tests/test_remote_action_contracts.py tests/test_remote_action_checksums.py tests/test_remote_action_identifiers.py tests/test_action_publication_metadata.py tests/test_central_actions.py tests/test_central_migrate.py -q
node --test tests-js/action-checksums.test.js
```

Expected: PASS，`C-01` 至 `C-05` 全部通过。

- [ ] **Step 5: 提交本任务**

```powershell
git add central/action_models.py central/actions.py central/app.py central/migrate.py tests/test_central_actions.py tests/test_central_migrate.py
git commit -m "feat: store immutable central action revisions"
```
