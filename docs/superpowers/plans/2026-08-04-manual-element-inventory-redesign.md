# Manual Element Inventory Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace semantic element matching with a manually curated inventory of visible interactive elements while preserving deterministic validation, two-Profile/two-round publication, element-scoped strategy protection, alerts, and recovery.

**Architecture:** Reuse the existing short-lived AdsPower picker and HTTP polling endpoints, but change the browser runner from click interception to continuous DOM inventory plus click-step recording. Persist manually named element definitions and deterministic CSS/XPath candidates, validate them with a new managed-element runtime, and retain existing Redis publication, alert, gate, lease, and outbox boundaries. Replace the seven-tab console with four task-oriented menus and keep technical evidence inside details.

**Tech Stack:** Python 3, Flask, SQLite, Redis Lua publication, Playwright/CDP, vanilla JavaScript, CSS, `pytest`, Node test runner.

## Global Constraints

- `Role` and `Name` are display-only metadata. They must not affect inventory inclusion, locator ranking, Dry-Run, validation, replay, fallback selection, publication, or recovery.
- Do not call an LLM and do not keep model, API Key, Prompt, semantic intent, accepted Role/Name, or semantic-contract settings.
- Scan visible interactive DOM targets only; return at most 500 deduplicated public records per scan.
- Generate deterministic CSS/XPath only; prohibit absolute XPath, text locators, Role locators, random-class paths, and coordinate clicks.
- Keep one collector Profile open until confirm, cancel, five-minute expiry, or fatal failure. Selection must not close it early.
- Page and recorded-step readiness timeout is exactly 90 seconds.
- Initial activation and automatic recovery require at least two independent test Profiles, two consistent rounds per Profile, and successful atomic publication.
- Refresh and retry failed validation at most three times.
- Daily automatic validation remains scheduled for 03:00 Asia/Shanghai.
- Only strategies that depend on a failed element may be automatically paused or resumed. Manual pause reasons are never automatically cleared.
- Reuse the existing picker start/status/confirm/cancel endpoint family and HTTP polling; do not add WebSocket or SSE transport.
- Preserve current Redis last-known-good, publication outbox, screenshot alert, Webhook, lease, revision, idempotency, and audit guarantees.
- The current worktree contains unrelated user changes. Stage and commit only files named by the active task.

## File Structure

### New files

- `selector_probe/inventory.py`: public inventory schema, sanitization, deduplication, deterministic locator allowlist, recorded-step normalization.
- `selector_probe/managed_runtime.py`: load saved definitions, replay steps, perform two-Profile/two-round deterministic validation, promote saved fallbacks, build publication result.
- `gateway/templates/_selector_probe_console.html`: four-menu selector management markup removed from the oversized `gateway/app.py` string.
- `gateway/static/selector_inventory_ui.js`: pure inventory, naming, filtering, and element-detail view helpers.
- `tests/test_selector_probe_inventory.py`: inventory and locator security tests.
- `tests/test_selector_probe_managed_runtime.py`: validation, fallback, retry, element isolation, and recovery tests.
- `tests-js/selector-inventory-ui.test.js`: four-menu and inventory rendering tests.

### Modified files

- `selector_probe/picker.py`: continuous inventory, browse-mode click recording, stable selection IDs, named confirmation.
- `selector_probe/store.py`: v2 managed-element schema, idempotent legacy migration, status and action-step persistence.
- `selector_probe/catalog.py`: manual definition create/edit/rebind/delete instead of semantic contracts.
- `selector_probe/view_models.py`: public manual-element and business-run projections.
- `selector_probe/blueprint.py`: existing picker and element routes accept and return new shapes.
- `selector_probe/validator.py`: deterministic CSS/XPath validation without semantic postconditions.
- `selector_probe/probe.py`: managed-element run stages, three-attempt retry, fallback promotion, element-level outcomes.
- `selector_probe/worker.py`: instantiate `ManagedElementRuntime` and stop loading semantic contracts.
- `selector_probe/alerts.py`: expose element name, failure path, screenshot, and affected strategies.
- `selector_probe/gates.py`: keep element-level aliases and manual pause protection.
- `selector_probe/config.py`: remove selector-probe model and semantic settings.
- `gateway/settings_store.py`: remove selector-probe model secrets from defaults and public settings.
- `gateway/app.py`: include the new console partial and remove old inline selector markup.
- `gateway/static/selector_probe_ui.js`: four-menu controller, polling, save/rebind/delete, runs and alert flow.
- `gateway/static/selector_probe.css`: sidebar hierarchy, panels, inventory table, details, responsive layout.
- Existing selector-probe Python and Node tests: update fixtures and assertions to the manual schema.

### Removed after replacement tests pass

- `selector_probe/contracts.py`
- `selector_probe/candidates.py`
- `selector_probe/discovery.py`
- `selector_probe/repair.py`
- `selector_probe/model_client.py`
- `selector_probe/healing_runtime.py`
- Tests dedicated only to removed semantic/LLM behavior.

The global `openai` JavaScript dependency is not removed because other project features may consume it; only selector-probe imports and settings are removed.

---

### Task 1: Build the safe interactive-element inventory

**Files:**
- Create: `selector_probe/inventory.py`
- Create: `tests/test_selector_probe_inventory.py`
- Modify: `selector_probe/picker.py`

**Interfaces:**
- Produces: `normalize_inventory(raw_items: object, *, selection_ids: Mapping[str, str] | None = None, limit: int = 500) -> list[dict[str, object]]`.
- Produces: `normalize_recorded_step(raw: object) -> dict[str, object]`.
- Produces: `public_inventory_item(value: Mapping[str, object]) -> dict[str, object]`.
- Inventory item keys: `selection_id`, `fingerprint`, `tag`, `input_type`, `text`, `role`, `name`, `attributes`, `frame_key`, `shadow`, `region`, `locators`, `locatable`, `match_counts`.
- Recorded-step keys: `sequence`, `locator`, `url_before`, `url_after`, `recorded_at`.

- [ ] **Step 1: Write failing inventory normalization tests**

```python
from selector_probe.inventory import normalize_inventory, normalize_recorded_step


def test_inventory_keeps_interactive_nodes_without_semantic_filtering():
    raw = [{
        "tag": "button",
        "text": "",
        "role": "button",
        "name": "Comments",
        "attributes": {"data-e2e": "comment-icon", "onclick": "secret()"},
        "region": {"x": 0.8, "y": 0.4, "width": 0.1, "height": 0.1},
        "locators": [
            {"type": "css", "value": "[data-e2e=\"comment-icon\"]", "match_count": 1},
            {"type": "xpath", "value": "//*[@data-e2e='comment-icon']", "match_count": 1},
        ],
    }]

    result = normalize_inventory(raw)

    assert len(result) == 1
    assert result[0]["role"] == "button"
    assert result[0]["name"] == "Comments"
    assert result[0]["locatable"] is True
    assert "onclick" not in result[0]["attributes"]


def test_inventory_rejects_semantic_and_unsafe_locators():
    raw = [{
        "tag": "div",
        "text": "Post",
        "role": "button",
        "name": "Post",
        "attributes": {"aria-label": "Post"},
        "region": {"x": 0.2, "y": 0.2, "width": 0.1, "height": 0.1},
        "locators": [
            {"type": "role", "value": "button:Post", "match_count": 1},
            {"type": "xpath", "value": "/html/body/div[3]", "match_count": 1},
            {"type": "css", "value": "button:nth-of-type(2)", "match_count": 1},
        ],
    }]

    result = normalize_inventory(raw)

    assert [item["type"] for item in result[0]["locators"]] == ["css"]
    assert result[0]["locatable"] is True


def test_inventory_deduplicates_nested_targets_and_caps_results():
    duplicates = [{
        "target_key": "same-target",
        "tag": "button",
        "text": str(index),
        "role": "button",
        "name": str(index),
        "attributes": {"data-testid": f"control-{index}"},
        "region": {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1},
        "locators": [{"type": "css", "value": f"[data-testid=\"control-{index}\"]", "match_count": 1}],
    } for index in range(600)]

    unique = [{**item, "target_key": f"target-{index}"} for index, item in enumerate(duplicates)]

    assert len(normalize_inventory(duplicates)) == 1
    assert len(normalize_inventory(unique)) == 500


def test_recorded_step_accepts_css_or_xpath_only():
    step = normalize_recorded_step({
        "sequence": 1,
        "locator": {"type": "css", "value": "[data-e2e=\"comment-icon\"]"},
        "url_before": "https://www.tiktok.com/",
        "url_after": "https://www.tiktok.com/",
        "recorded_at": "2026-08-04T03:00:00+00:00",
    })
    assert step["sequence"] == 1
    assert step["locator"]["type"] == "css"
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `python -m pytest tests/test_selector_probe_inventory.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'selector_probe.inventory'`.

- [ ] **Step 3: Implement the inventory schema and sanitizer**

Add these exact public constants and validators in `selector_probe/inventory.py`:

```python
MAX_INVENTORY_ITEMS = 500
MAX_RAW_ITEMS = 1000
MAX_LOCATORS = 6
ALLOWED_LOCATOR_TYPES = frozenset({"css", "xpath"})
ALLOWED_ATTRIBUTES = (
    "data-e2e", "data-testid", "id", "name", "placeholder",
    "aria-label", "contenteditable", "type", "tabindex",
)
FORBIDDEN_XPATH_PREFIXES = ("/html", "//html")


def _locator(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    locator_type = str(raw.get("type") or "").strip().casefold()
    value = str(raw.get("value") or "").strip()
    match_count = raw.get("match_count")
    if locator_type not in ALLOWED_LOCATOR_TYPES or not value:
        return None
    if locator_type == "xpath" and value.casefold().startswith(FORBIDDEN_XPATH_PREFIXES):
        return None
    if "javascript:" in value.casefold() or len(value) > 500:
        return None
    if isinstance(match_count, bool) or not isinstance(match_count, int) or match_count < 0:
        return None
    return {"type": locator_type, "value": value, "match_count": match_count}
```

`normalize_inventory` must sanitize strings, normalize regions to `0..1`, compute a SHA-256 fingerprint from tag plus stable attributes plus rounded center, keep supplied stable selection IDs by fingerprint, deduplicate `target_key`, sort locatable items first, and return at most `limit` items. It must never read `role` or `name` while deciding whether an item is locatable.

Treat an ID as dynamic when it contains a UUID, a hexadecimal run of at least eight characters, or a decimal run of at least six characters. Dynamic IDs may remain display metadata but must not enter a locator.

- [ ] **Step 4: Add the browser-side inventory collector**

In `selector_probe/picker.py`, replace the semantic `_SAFE_ROLES` gate with one DOM collector that starts from this selector set and adds pointer/listener targets discovered from event paths:

```javascript
const baseSelector = [
  "a", "button", "input", "textarea", "select", "option", "summary",
  "[contenteditable='true']", "[tabindex]", "[data-e2e]", "[data-testid]",
  "[onclick]"
].join(",");
```

For each node, generate candidates in this order: unique test attribute, stable unique ID, unique attribute combination, stable parent plus short child path, and at most three `nth-of-type` segments. Call `document.querySelectorAll` or an XPath snapshot to attach `match_count`. Keep `role` and accessible name only in the returned display fields.

Walk the main document, every same-origin iframe, and every open Shadow DOM recursively. Return a bounded `frame_key` and `shadow` flag. A cross-origin iframe that Playwright/CDP cannot inspect must produce one bounded diagnostic entry and must not fail the rest of the page inventory.

- [ ] **Step 5: Run focused tests**

Run: `python -m pytest tests/test_selector_probe_inventory.py tests/test_selector_probe_picker.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit Task 1**

```bash
git add selector_probe/inventory.py selector_probe/picker.py tests/test_selector_probe_inventory.py tests/test_selector_probe_picker.py
git commit -m "feat(probe): add safe element inventory"
```

### Task 2: Convert the picker into a browse-and-scan collector

**Files:**
- Modify: `selector_probe/picker.py`
- Modify: `selector_probe/blueprint.py`
- Modify: `tests/test_selector_probe_picker.py`
- Modify: `tests/test_selector_probe_routes.py`

**Interfaces:**
- `PickerService.start(*, profile_id: str, profile_mask: str, page_state: str, actor_user_id: int, context: Mapping[str, object]) -> dict[str, object]` keeps the existing inputs.
- `PickerService.get(session_id, *, actor_user_id) -> dict[str, object]` adds `inventory`, `inventory_revision`, `recorded_steps`, `truncated`, and `last_scanned_at`.
- `PickerService.confirm(session_id: str, *, actor_user_id: int, expected_revision: int, selections: Sequence[Mapping[str, str]]) -> dict[str, object]` accepts `selection_id` plus `display_name` and returns named candidates.
- Browser runner sinks: `inventory_sink(raw_items: object, truncated: bool)`, `action_sink(raw_step: object)`, and `ready_sink()`.

- [ ] **Step 1: Write failing collector lifecycle tests**

```python
def test_picker_updates_inventory_without_blocking_page_clicks():
    actions = []

    def runner(*, ready_sink, inventory_sink, action_sink, stop_event, **_kwargs):
        ready_sink()
        inventory_sink([candidate()], False)
        action_sink({
            "sequence": 1,
            "locator": {"type": "css", "value": "[data-e2e=\"comment-icon\"]"},
            "url_before": "https://www.tiktok.com/",
            "url_after": "https://www.tiktok.com/",
            "recorded_at": "2026-08-04T03:00:00+00:00",
        })
        actions.append("recorded")
        stop_event.wait(2)

    service = PickerService(
        MemoryRedis(), lease_key="lease", key_prefix="picker",
        runner=runner, lease_factory=Lease, active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="profile", profile_mask="***file",
        page_state="feed_ready", actor_user_id=7, context={},
    )
    assert wait_until(lambda: service.get(
        started["session_id"], actor_user_id=7
    )["inventory_revision"] == 1)
    current = service.get(started["session_id"], actor_user_id=7)
    assert len(current["inventory"]) == 1
    assert len(current["recorded_steps"]) == 1
    assert actions == ["recorded"]


def test_picker_confirm_requires_unique_custom_names():
    def runner(*, ready_sink, inventory_sink, stop_event, **_kwargs):
        ready_sink()
        inventory_sink([
            candidate(),
            candidate(attributes={"data-e2e": "comment-post"}),
        ], False)
        stop_event.wait(2)

    service = PickerService(
        MemoryRedis(), lease_key="lease", key_prefix="picker",
        runner=runner, lease_factory=Lease, active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="profile", profile_mask="***file",
        page_state="feed_ready", actor_user_id=7, context={},
    )
    assert wait_until(lambda: service.get(
        started["session_id"], actor_user_id=7
    )["inventory_revision"] == 1)
    current = service.get(started["session_id"], actor_user_id=7)
    with pytest.raises(PickerError, match="duplicate_element_name"):
        service.confirm(
            started["session_id"],
            actor_user_id=7,
            expected_revision=current["revision"],
            selections=[
                {"selection_id": current["inventory"][0]["selection_id"], "display_name": "评论按钮"},
                {"selection_id": current["inventory"][1]["selection_id"], "display_name": " 评论按钮 "},
            ],
        )
```

- [ ] **Step 2: Run the lifecycle tests and verify failure**

Run: `python -m pytest tests/test_selector_probe_picker.py -q`

Expected: failures show missing `inventory_sink`, `inventory_revision`, and named selection support.

- [ ] **Step 3: Replace click interception with browse-mode recording**

The injected click listener must observe in capture phase but must not call `preventDefault`, `stopPropagation`, or `stopImmediatePropagation`. It records the first allowed deterministic locator for the actionable ancestor, then lets TikTok handle the click normally. The runner rescans every second and only increments `inventory_revision` when the normalized fingerprint list changes.

Use these session defaults:

```python
{
    "status": "starting",
    "mode": "browse",
    "inventory": [],
    "inventory_revision": 0,
    "recorded_steps": [],
    "truncated": False,
    "last_scanned_at": "",
    "selection_count": 0,
}
```

- [ ] **Step 4: Update existing HTTP shapes without adding routes**

Keep:

```text
POST /api/selector-probe/picker/start
GET  /api/selector-probe/picker/<session_id>
POST /api/selector-probe/picker/<session_id>/confirm
POST /api/selector-probe/picker/<session_id>/cancel
```

Change confirm JSON to:

```json
{
  "expected_revision": 8,
  "selections": [
    {"selection_id": "selection-1", "display_name": "评论入口"}
  ]
}
```

Reject unknown keys, names outside `1..120` characters, duplicate normalized names, stale revisions, and IDs outside the current inventory.

- [ ] **Step 5: Run picker and route tests**

Run: `python -m pytest tests/test_selector_probe_picker.py tests/test_selector_probe_routes.py -q`

Expected: all tests pass; public payloads contain no raw Profile ID, CDP URL, DOM HTML, or form values.

- [ ] **Step 6: Commit Task 2**

```bash
git add selector_probe/picker.py selector_probe/blueprint.py tests/test_selector_probe_picker.py tests/test_selector_probe_routes.py
git commit -m "feat(probe): record manual page states"
```

### Task 3: Replace semantic catalog persistence with managed element definitions

**Files:**
- Modify: `selector_probe/store.py`
- Modify: `selector_probe/catalog.py`
- Modify: `selector_probe/view_models.py`
- Modify: `tests/test_selector_probe_store.py`
- Modify: `tests/test_selector_probe_catalog.py`

**Interfaces:**
- New element status set: `pending_rebind`, `draft`, `validating`, `healthy`, `degraded`, `invalid`, `disabled`.
- Manual definition keys: `page_key`, `target_origin`, `url_pattern`, `operation_steps`, `fingerprint`, `locators`.
- `ElementCatalog.create_draft(payload, actor_user_id, actor_username) -> ElementRecord` accepts a manual definition.
- `ElementCatalog.update_name(element_id: str, display_name: object, expected_revision: int, actor_user_id: int, actor_username: str) -> ElementRecord` changes only `display_name`.
- `ElementCatalog.rebind(element_id: str, definition: object, expected_revision: int, actor_user_id: int, actor_username: str) -> ElementRecord` replaces the draft definition and returns status `draft`.
- Test helper `legacy_store_with_contract_and_dependency(tmp_path: Path) -> SelectorProbeStore` creates the pre-v2 `element_probe_contracts`, `managed_elements`, `element_drafts`, and `strategy_dependencies` rows, closes that connection, and then constructs `SelectorProbeStore` so the production migration runs.

- [ ] **Step 1: Write failing schema and migration tests**

```python
def test_store_migrates_legacy_elements_to_pending_rebind_idempotently(tmp_path):
    store = legacy_store_with_contract_and_dependency(tmp_path)
    store.migrate_manual_elements()
    store.migrate_manual_elements()

    row = store.get_managed_element_row("评论入口")
    assert row["display_name"] == "评论入口"
    assert row["status"] == "pending_rebind"
    assert store.managed_element_dependency_rows("评论入口")[0]["strategy_id"] == "strategy-1"
    assert store.manual_element_definition("评论入口") is None


def test_catalog_creates_manual_draft_without_contract_fields(store):
    catalog = ElementCatalog(store, element_id_factory=lambda: "element-1")
    record = catalog.create_draft({
        "display_name": "评论输入框",
        "page_key": "comment-panel",
        "target_origin": "https://www.tiktok.com",
        "url_pattern": "https://www.tiktok.com/*",
        "operation_steps": [],
        "fingerprint": {"tag": "div", "attributes": {"data-e2e": "comment-input"}},
        "locators": [{"type": "css", "value": "[data-e2e=\"comment-input\"]"}],
    }, actor_user_id=7, actor_username="admin")
    assert record.status == "draft"
    assert not hasattr(record, "scope")
```

- [ ] **Step 2: Run schema tests and verify failure**

Run: `python -m pytest tests/test_selector_probe_store.py tests/test_selector_probe_catalog.py -q`

Expected: failures show the missing v2 columns, migration, and manual payload normalizer.

- [ ] **Step 3: Add an idempotent SQLite v2 migration**

Add or rebuild `managed_elements` and `element_drafts` inside one transaction. They must contain these v2 columns. Legacy compatibility columns may remain through Tasks 3–7 while old request/publication readers still exist; Task 8 removes those readers and rebuilds the exact final schema atomically:

```sql
CREATE TABLE managed_elements (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'pending_rebind','draft','validating','healthy','degraded','invalid','disabled'
    )),
    page_key TEXT NOT NULL DEFAULT '',
    target_origin TEXT NOT NULL DEFAULT '',
    url_pattern TEXT NOT NULL DEFAULT '',
    active_version_id TEXT NOT NULL DEFAULT '',
    last_known_good_version_id TEXT NOT NULL DEFAULT '',
    primary_locator_type TEXT NOT NULL DEFAULT '',
    last_validated_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE element_drafts (
    element_id TEXT PRIMARY KEY REFERENCES managed_elements(id) ON DELETE CASCADE,
    definition_json TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '{}',
    base_version_id TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_by INTEGER NOT NULL CHECK (created_by > 0),
    created_by_username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Copy legacy IDs, names, versions, dependencies, timestamps, and revisions. Set every legacy record to `pending_rebind`; do not copy semantic contract JSON into `definition_json`. Keep `element_probe_contracts` as a read-compatibility table during Tasks 3–7; Task 8 removes its remaining callers and drops it in the same migration so intermediate application versions remain startable.

- [ ] **Step 4: Replace catalog payload normalization**

Use one strict required field set:

```python
CREATE_FIELDS = frozenset({
    "display_name", "page_key", "target_origin", "url_pattern",
    "operation_steps", "fingerprint", "locators",
})
```

Validate origin as HTTPS TikTok origin from settings, bound URL patterns, at most 20 recorded steps, at most six CSS/XPath locators, and JSON size limits. Keep delete dependency protection and revision checks.

- [ ] **Step 5: Run schema and catalog tests**

Run: `python -m pytest tests/test_selector_probe_store.py tests/test_selector_probe_catalog.py -q`

Expected: all focused tests pass, including two consecutive migration calls.

- [ ] **Step 6: Commit Task 3**

```bash
git add selector_probe/store.py selector_probe/catalog.py selector_probe/view_models.py tests/test_selector_probe_store.py tests/test_selector_probe_catalog.py
git commit -m "refactor(probe): store manual element definitions"
```

### Task 4: Expose manual element APIs and public projections

**Files:**
- Modify: `selector_probe/blueprint.py`
- Modify: `selector_probe/view_models.py`
- Modify: `tests/test_selector_probe_routes.py`
- Modify: `tests/test_selector_probe_management_security.py`

**Interfaces:**
- Reuse `GET/POST /api/selector-probe/elements`.
- Reuse `GET/PATCH/DELETE /api/selector-probe/elements/<element_id>`.
- Add rebind as `PATCH` with `operation: "rebind"`; rename uses `operation: "rename"`.
- Element summary returns `id`, `display_name`, `status`, `page_key`, `primary_locator_type`, `dependency_count`, `last_validated_at`, `revision`.
- Element detail additionally returns sanitized `definition`, `dependencies`, `validation`, `history`, `alerts`, and `strategy_controls`.

- [ ] **Step 1: Write failing API projection tests**

```python
def test_element_create_rejects_semantic_contract_fields(admin_client):
    response = admin_client.post("/api/selector-probe/elements", json={
        "display_name": "评论入口",
        "accepted_roles": ["button"],
        "accepted_names": ["Comments"],
    })
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_element_payload"


def test_element_detail_exposes_manual_definition_only(admin_client, manual_element):
    response = admin_client.get(f"/api/selector-probe/elements/{manual_element.id}")
    payload = response.get_json()
    assert payload["display_name"] == "评论入口"
    assert payload["definition"]["locators"][0]["type"] == "css"
    assert "contract" not in payload
    assert "repairs" not in payload
    assert "prompt_version" not in str(payload)
```

- [ ] **Step 2: Run route tests and verify old contract output failure**

Run: `python -m pytest tests/test_selector_probe_routes.py tests/test_selector_probe_management_security.py -q`

Expected: tests fail because routes still require `contract` and view models still expose semantic comparison/repair fields.

- [ ] **Step 3: Replace route request shapes and view models**

Map picker confirmation items directly into the existing element create route. Keep admin authorization, idempotency key, revision, audit, and public error conventions. Remove semantic query filters `source` and `scope`; retain `search`, `status`, and `referenced`.

Implement operation dispatch exactly as:

```python
operation = payload.get("operation")
if operation == "rename":
    record = catalog.update_name(
        element_id, payload.get("display_name"), expected_revision,
        actor_user_id, actor_username,
    )
elif operation == "rebind":
    record = catalog.rebind(
        element_id, payload.get("definition"), expected_revision,
        actor_user_id, actor_username,
    )
else:
    return jsonify(public_error(
        "invalid_element_operation", message="不支持的元素操作"
    )), 400
```

- [ ] **Step 4: Run route and security tests**

Run: `python -m pytest tests/test_selector_probe_routes.py tests/test_selector_probe_management_security.py -q`

Expected: all focused tests pass; secrets, raw DOM, semantic fields, model fields, and Profile IDs remain absent.

- [ ] **Step 5: Commit Task 4**

```bash
git add selector_probe/blueprint.py selector_probe/view_models.py tests/test_selector_probe_routes.py tests/test_selector_probe_management_security.py
git commit -m "feat(probe): expose manual element catalog"
```

### Task 5: Implement deterministic validation and fallback promotion

**Files:**
- Create: `selector_probe/managed_runtime.py`
- Create: `tests/test_selector_probe_managed_runtime.py`
- Modify: `selector_probe/validator.py`
- Modify: `tests/test_selector_probe_validator.py`
- Test: `tests/test_selector_probe_registry.py`

**Interfaces:**
- `validate_locator(page: object, locator: Mapping[str, object]) -> Awaitable[dict[str, object]]`.
- `validate_element(page: object, definition: Mapping[str, object]) -> Awaitable[dict[str, object]]`.
- `ManagedElementRuntime.load_candidate() -> dict[str, object]`.
- `ManagedElementRuntime.validate_candidate(candidate: object) -> dict[str, object]`.
- `ManagedElementRuntime.promote_saved_fallbacks(candidate: object, validation: Mapping[str, object]) -> dict[str, object]`.
- `ManagedElementRuntime.prepare_publication(candidate: object, validation: Mapping[str, object]) -> dict[str, object]`.
- Test helper `visible_node() -> dict[str, object]` returns a visible, enabled, center-hit node projection.
- Test class `FakePage(css_matches: Mapping[str, Sequence[object]], xpath_matches: Mapping[str, Sequence[object]] | None = None)` implements the exact count, visibility, enabled, and hit-test calls consumed by `validate_locator`.
- Test helper `runtime_with_bundle(*, primary: str, fallback: str) -> ManagedElementRuntime` constructs an in-memory store/runtime with one element and two saved CSS locators.

- [ ] **Step 1: Write failing deterministic validator tests**

```python
@pytest.mark.asyncio
async def test_validator_ignores_role_and_name_metadata():
    page = FakePage(css_matches={"[data-e2e=\"comment-icon\"]": [visible_node()]})
    definition = {
        "display": {"role": "link", "name": "Changed Name"},
        "locators": [{"type": "css", "value": "[data-e2e=\"comment-icon\"]"}],
    }
    result = await validate_element(page, definition)
    assert result["status"] == "passed"
    assert result["selected_locator"]["type"] == "css"


@pytest.mark.asyncio
async def test_validator_reports_zero_and_ambiguous_matches():
    zero = await validate_locator(FakePage(css_matches={}), {
        "type": "css", "value": "[data-e2e=\"missing\"]",
    })
    many = await validate_locator(FakePage(css_matches={"button": [visible_node(), visible_node()]}), {
        "type": "css", "value": "button",
    })
    assert zero["failure_code"] == "selector_zero_match"
    assert many["failure_code"] == "selector_ambiguous"


def test_runtime_promotes_only_a_saved_fallback():
    runtime = runtime_with_bundle(primary=".old", fallback="[data-e2e=\"new\"]")
    promoted = runtime.promote_saved_fallbacks(runtime.load_candidate(), {
        "elements": {"element-1": {"selected_locator_index": 1, "status": "passed"}}
    })
    assert promoted["elements"]["element-1"]["locators"][0]["value"] == "[data-e2e=\"new\"]"
```

- [ ] **Step 2: Run validator tests and verify semantic behavior failure**

Run: `python -m pytest tests/test_selector_probe_managed_runtime.py tests/test_selector_probe_validator.py -q`

Expected: missing runtime and semantic validator assertions fail.

- [ ] **Step 3: Replace semantic validation with exact query validation**

`validate_locator` must use `query_selector_all` for CSS and an XPath locator count for XPath. A pass requires count `1`, visible `True`, enabled `True`, and center hit target equal to or contained by the matched node. It must return only:

```python
{
    "status": "passed",
    "failure_code": "",
    "match_count": 1,
    "visible": True,
    "enabled": True,
    "hit_target": True,
}
```

or the same bounded shape with status `failed` and one of `selector_zero_match`, `selector_ambiguous`, `selector_hidden`, `selector_disabled`, `selector_hit_test_failed`, `selector_query_invalid`.

- [ ] **Step 4: Implement the managed runtime and canonical bundle**

The runtime loads only `healthy`, `degraded`, or validating drafts. It replays stored click steps, validates every locator in order, marks the first passing saved locator, and never generates a new path.

Publish this canonical v2 bundle while retaining the registry hash and Lua transaction:

```json
{
  "version": "selector-version-id",
  "bundle_hash": "sha256:canonical-elements-hash",
  "elements": {
    "element-1": {
      "scope": "page",
      "locators": [
        {"id": "locator-id", "type": "css", "value": "[data-e2e=\"comment-icon\"]", "enabled": true}
      ]
    }
  }
}
```

`ManagedElementRuntime.prepare_publication` writes the existing registry field `scope` as `page`. Recorded operation steps remain in SQLite and are probe preparation data; they are not published to strategy consumers. Full-page uniqueness and hit testing provide the safety boundary. This keeps `browser_element_schema.py`, `browser_strategy_config.py`, and `browser_actions.py` compatible. New managed bundles must contain CSS/XPath locators only; registry read compatibility for pre-v2 active versions remains until migration marks those elements `pending_rebind`.

Step replay must scroll each saved click target into view, click its saved locator, and wait up to 90 seconds for DOM/candidate stability and the next saved target group. A missing step returns `recorded_step_unavailable` and fails only elements downstream of that step.

- [ ] **Step 5: Run runtime, validator, and registry tests**

Run: `python -m pytest tests/test_selector_probe_managed_runtime.py tests/test_selector_probe_validator.py tests/test_selector_probe_registry.py -q`

Expected: all focused tests pass.

- [ ] **Step 6: Commit Task 5**

```bash
git add selector_probe/managed_runtime.py selector_probe/validator.py tests/test_selector_probe_managed_runtime.py tests/test_selector_probe_validator.py
git commit -m "feat(probe): validate saved selectors only"
```

### Task 6: Wire daily validation, retries, strategy isolation, alerts, and recovery

**Files:**
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/worker.py`
- Modify: `selector_probe/alerts.py`
- Modify: `selector_probe/gates.py`
- Modify: `tests/test_selector_probe_worker.py`
- Modify: `tests/test_selector_probe_policy.py`
- Modify: `tests/test_selector_probe_alerts.py`
- Modify: `tests/test_selector_probe_gates.py`

**Interfaces:**
- `run_managed_probe(runtime: ManagedElementRuntime) -> dict[str, object]`.
- Business stage names: `prepare_environment`, `open_and_replay`, `validate_elements`, `protect_or_recover`, `alert_and_cleanup`.
- Element outcome keys: `element_id`, `status`, `failure_code`, `attempt_count`, `selected_locator_index`, `profile_results`.
- Test harness `run_policy_with_results(results: Mapping[str, str], dependencies: Mapping[str, Sequence[str]]) -> dict[str, object]` uses the real gate service with an in-memory store and no-op registry.
- Test harness `recover_with_matrix(matrix: Mapping[str, Sequence[str]], *, publish_result: str, manual_gate: bool) -> dict[str, object]` uses the real recovery function and a fake atomic publisher.
- Test harness `run_with_validation_failures(failures: Sequence[str]) -> dict[str, object]` records runtime refresh, alert, and attempt calls.

- [ ] **Step 1: Write failing policy tests**

```python
def test_one_invalid_element_pauses_only_dependent_strategies():
    outcome = run_policy_with_results({
        "comment-entry": "passed",
        "comment-input": "selector_zero_match",
    }, dependencies={
        "comment-entry": ["strategy-like"],
        "comment-input": ["strategy-comment"],
    })
    assert outcome["paused_strategy_ids"] == ["strategy-comment"]
    assert "strategy-like" not in outcome["paused_strategy_ids"]


def test_recovery_needs_two_profiles_two_rounds_and_atomic_publish():
    outcome = recover_with_matrix({
        "***one": ["passed", "passed"],
        "***two": ["passed", "passed"],
    }, publish_result="published", manual_gate=True)
    assert outcome["automatic_gate_cleared"] is True
    assert outcome["manual_gate_cleared"] is False


def test_validation_retries_exactly_three_times_before_alert():
    outcome = run_with_validation_failures([
        "selector_zero_match", "selector_zero_match", "selector_zero_match",
    ])
    assert outcome["attempt_count"] == 3
    assert outcome["alert_created"] is True
```

- [ ] **Step 2: Run worker and policy tests and verify failure**

Run: `python -m pytest tests/test_selector_probe_worker.py tests/test_selector_probe_policy.py tests/test_selector_probe_alerts.py tests/test_selector_probe_gates.py -q`

Expected: failures show the worker still constructs `HealingRuntime` and groups failures around semantic aliases.

- [ ] **Step 3: Wire `ManagedElementRuntime` into the worker**

Replace `_healing_runtime_factory` with:

```python
def _managed_runtime_factory(**kwargs) -> object:
    return ManagedElementRuntime(**kwargs)
```

Remove `default_tiktok_contracts` and model settings from worker construction. Keep the existing lease heartbeat, run-request association, two distinct CDP/Profile guards, publication reconciliation, outboxes, and cleanup ownership.

- [ ] **Step 4: Implement element-level retry and business stages**

For each failed element, refresh its owned page and retry the stored operation chain plus selectors until three attempts are exhausted. Do not rerun already passed unrelated elements. Store technical stages under `diagnostics`; store the five business stages under `stages` for the default UI.

- [ ] **Step 5: Update alerts and gate reasons**

Alert details must include custom element name, exact failure code, saved locator summaries, screenshot path, affected strategy IDs, attempt count, and next action `重新绑定元素`. Gate aliases remain element IDs. Recovery clears only matching automatic reasons after atomic publication succeeds.

- [ ] **Step 6: Run policy tests**

Run: `python -m pytest tests/test_selector_probe_worker.py tests/test_selector_probe_policy.py tests/test_selector_probe_alerts.py tests/test_selector_probe_gates.py -q`

Expected: all focused tests pass.

- [ ] **Step 7: Commit Task 6**

```bash
git add selector_probe/probe.py selector_probe/worker.py selector_probe/alerts.py selector_probe/gates.py tests/test_selector_probe_worker.py tests/test_selector_probe_policy.py tests/test_selector_probe_alerts.py tests/test_selector_probe_gates.py
git commit -m "feat(probe): isolate failed element strategies"
```

### Task 7: Build the four-menu management UI

**Files:**
- Create: `gateway/templates/_selector_probe_console.html`
- Create: `gateway/static/selector_inventory_ui.js`
- Create: `tests-js/selector-inventory-ui.test.js`
- Modify: `gateway/app.py`
- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `gateway/static/selector_probe.css`
- Modify: `tests-js/selector-probe-console.test.js`
- Modify: `tests-js/selector-probe-elements.test.js`
- Modify: `tests-js/selector-probe-operations.test.js`

**Interfaces:**
- Top-level menu IDs: `collect`, `managed`, `operations`, `settings`.
- Pure helper exports: `sanitizeInventory`, `filterInventory`, `serializeNamedSelections`, `elementStatusText`, `businessRunSteps`, `renderInventory`, `renderManagedElements`.
- Controller methods: `openCollector`, `pollCollector`, `confirmCollector`, `renameElement`, `rebindElement`, `deleteElement`, `runNow`, `acknowledgeAlert`.

- [ ] **Step 1: Write failing four-menu and inventory tests**

```javascript
const test = require("node:test");
const assert = require("node:assert/strict");
const {
  sanitizeInventory,
  serializeNamedSelections,
  businessRunSteps,
} = require("../gateway/static/selector_inventory_ui");

test("inventory keeps Role and Name as display metadata only", () => {
  const result = sanitizeInventory([{
    selection_id: "selection-1",
    tag: "button",
    role: "button",
    name: "Comments",
    locators: [{type: "css", value: "[data-e2e=\"comment-icon\"]", match_count: 1}],
    locatable: true,
  }]);
  assert.equal(result[0].role, "button");
  assert.equal(result[0].name, "Comments");
  assert.equal(result[0].locators[0].type, "css");
});

test("named selection payload uses IDs and custom names only", () => {
  assert.deepEqual(serializeNamedSelections([
    {selectionId: "selection-1", displayName: " 评论入口 "},
  ]), [{selection_id: "selection-1", display_name: "评论入口"}]);
});

test("run view always exposes five understandable steps", () => {
  assert.deepEqual(businessRunSteps({stages: []}).map((item) => item.id), [
    "prepare_environment", "open_and_replay", "validate_elements",
    "protect_or_recover", "alert_and_cleanup",
  ]);
});
```

- [ ] **Step 2: Run Node tests and verify missing module failure**

Run: `node --test tests-js/selector-inventory-ui.test.js tests-js/selector-probe-console.test.js tests-js/selector-probe-elements.test.js tests-js/selector-probe-operations.test.js`

Expected: missing `selector_inventory_ui.js` and seven-tab assertions fail.

- [ ] **Step 3: Extract selector console markup from `gateway/app.py`**

Replace the old inline selector console block with:

```jinja2
{% include '_selector_probe_console.html' %}
```

The partial must contain one left hierarchy with exactly four top-level buttons and four matching panels. Use the approved Chinese labels: `采集元素`, `已选元素`, `运行与告警`, `系统设置`.

- [ ] **Step 4: Implement the collector and managed-element views**

Collector panel requirements:

```text
Header: collector state and masked Profile
Progress: 打开页面 / 手动操作 / 扫描当前页面 / 命名并保存
Toolbar: 类型、位置、仅可定位、搜索、刷新列表
List: checkbox, tag, text, Role/Name, attributes, location, CSS/XPath, match state
Editor: custom name, recorded steps, locator list, Dry-Run, save
```

Managed panel cards must show custom name, status, page key, primary locator, dependency count, last validation, and buttons for rename, rebind, delete, screenshot, and detail. Dependency-protected delete must show affected strategies and stay disabled until references are removed.

- [ ] **Step 5: Implement operations and settings views**

The default run view must translate internal states into the five business steps. Put Profile matrices, CDP binding, rounds, retries, publication transaction, reconciliation, cleanup, and lease under a collapsed `诊断详情` element.

Merge alert list into the same panel. Keep `查看现场截图`, `确认告警`, and `重新绑定` actions. Settings retains Profiles, 03:00 schedule, target URL, 90-second readiness, Webhook, Redis, and permissions.

- [ ] **Step 6: Apply clear responsive visual hierarchy**

Use CSS sections `.selector-console-nav`, `.selector-console-panel`, `.selector-inventory-grid`, `.selector-element-card`, `.selector-run-step`, `.selector-diagnostic-details`. At widths below 900px collapse the left navigation into a horizontal scroll row and stack inventory list over editor. Preserve focus outlines, `aria-selected`, live status regions, and keyboard tab order.

- [ ] **Step 7: Run focused Node tests**

Run: `node --test tests-js/selector-inventory-ui.test.js tests-js/selector-probe-console.test.js tests-js/selector-probe-elements.test.js tests-js/selector-probe-operations.test.js`

Expected: all focused Node tests pass.

- [ ] **Step 8: Commit Task 7**

```bash
git add gateway/templates/_selector_probe_console.html gateway/static/selector_inventory_ui.js gateway/app.py gateway/static/selector_probe_ui.js gateway/static/selector_probe.css tests-js/selector-inventory-ui.test.js tests-js/selector-probe-console.test.js tests-js/selector-probe-elements.test.js tests-js/selector-probe-operations.test.js
git commit -m "feat(ui): simplify element management console"
```

### Task 8: Remove semantic and LLM selector-probe code paths

**Files:**
- Modify: `selector_probe/config.py`
- Modify: `gateway/settings_store.py`
- Modify: `gateway/static/selector_probe_ui.js`
- Modify: `tests-js/selector-probe-settings.test.js`
- Modify: selector-probe config, route, worker, and settings tests
- Delete: `selector_probe/contracts.py`
- Delete: `selector_probe/candidates.py`
- Delete: `selector_probe/discovery.py`
- Delete: `selector_probe/repair.py`
- Delete: `selector_probe/model_client.py`
- Delete: `selector_probe/healing_runtime.py`
- Delete: semantic-only tests after equivalent manual-runtime coverage exists

**Interfaces:**
- Selector-probe settings keys after removal: `enabled`, `rollout_mode`, `schedule_time`, `timezone`, `target_origin`, `test_profile_ids`, `dedicated_test_profile_ids`, `page_timeout_seconds`, `redis`, `webhook`.
- `page_timeout_seconds` must normalize to `90` for new defaults.

- [ ] **Step 1: Write failing settings-removal tests**

```javascript
test("selector settings do not expose model or semantic controls", () => {
  const settings = sanitizeSettings({
    model: {id: "old-model", api_key_set: true},
    schedule_time: "03:00",
    page_timeout_seconds: 90,
  });
  assert.equal(settings.schedule_time, "03:00");
  assert.equal(settings.page_timeout_seconds, 90);
  assert.equal(Object.hasOwn(settings, "model"), false);
});
```

```python
def test_probe_config_drops_model_and_contract_settings():
    result = normalize_probe_config({
        "schedule_time": "03:00",
        "page_timeout_seconds": 90,
        "model": {"id": "old-model"},
        "contracts": {"comment": {"accepted_roles": ["button"]}},
    })
    assert result["schedule_time"] == "03:00"
    assert result["page_timeout_seconds"] == 90
    assert "model" not in result
    assert "contracts" not in result
```

- [ ] **Step 2: Run settings tests and verify old fields remain**

Run: `python -m pytest tests/test_selector_probe_config.py tests/test_selector_probe_settings.py -q`

Run: `node --test tests-js/selector-probe-settings.test.js`

Expected: old model/contract fields still appear and tests fail.

- [ ] **Step 3: Remove selector-probe model and semantic settings**

Delete model secret mutation, model preflight, Prompt display, semantic contract defaults, and related public status fields. Keep Redis and Webhook secret handling unchanged. Existing stored obsolete keys may be ignored on read and removed on the next successful settings save.

- [ ] **Step 4: Prove removed modules have no live imports**

Run:

```powershell
rg -n "selector_probe\.(contracts|candidates|discovery|repair|model_client|healing_runtime)|accepted_roles|accepted_names|prompt_version" selector_probe gateway tests tests-js
```

Expected: no runtime/UI import or field reference. Historical design and plan documents may still contain the terms and are excluded from this check.

- [ ] **Step 5: Delete replaced modules and semantic-only tests**

Delete only after Tasks 1–7 tests pass and Task 4 migration retains old names/dependencies as `pending_rebind`. Do not delete alert, gate, outbox, version, registry, lease, screenshot, audit, or A11y snapshot modules. Keep A11y extraction only for display metadata and test that its output never enters locator validation.

- [ ] **Step 6: Run settings and import tests**

Run: `python -m pytest tests/test_selector_probe_config.py tests/test_selector_probe_settings.py tests/test_selector_probe_worker.py tests/test_selector_probe_routes.py -q`

Run: `node --test tests-js/selector-probe-settings.test.js tests-js/selector-probe-console.test.js`

Expected: all focused tests pass with no semantic/LLM module import.

- [ ] **Step 7: Commit Task 8**

```bash
git add selector_probe gateway/settings_store.py gateway/static/selector_probe_ui.js tests tests-js/selector-probe-settings.test.js
git commit -m "refactor(probe): remove semantic matching"
```

### Task 9: Migration rehearsal, end-to-end acceptance, and full regression

**Files:**
- Modify: tests and documentation only if acceptance exposes a real defect
- Test: full Python and Node suites

**Interfaces:**
- No new interface. This task verifies the completed design against a copied production-shaped SQLite database and two dedicated AdsPower test Profiles.

- [ ] **Step 1: Rehearse migration on a database copy**

Copy `data/selector-probe.db` to a temporary test path outside the live DB, start `SelectorProbeStore` against the copy twice, and assert:

```python
assert all(item["status"] == "pending_rebind" for item in migrated_legacy_items)
assert migrated_names == original_names
assert migrated_dependencies == original_dependencies
assert no_duplicate_element_ids
assert no_duplicate_gate_reasons
```

Do not mutate the live database during this rehearsal.

- [ ] **Step 2: Run the collector acceptance test**

Using one dedicated Profile, open TikTok, allow manual clicks, scan the homepage and opened comment panel, and confirm:

```text
At least the 50 candidates seen in run 28 can be displayed when present.
Candidates are not discarded because they fail old comment aliases.
Every displayed locator has a match count.
Role/Name edits in fixture data do not change locator choice.
Collector window remains open until explicit confirm/cancel.
```

- [ ] **Step 3: Run publication and failure-isolation acceptance**

Select and name at least one homepage element and one post-click element. Confirm current-window Dry-Run creates a draft only. Run two independent Profiles for two rounds and confirm atomic Redis activation. Then invalidate one saved selector in a controlled fixture and confirm three retries, screenshot alert, and pause of only its dependent strategy.

- [ ] **Step 4: Run recovery acceptance**

Restore or rebind the failed element. Confirm two Profiles times two rounds and atomic publication are required before automatic recovery. Add a manual pause reason and confirm it remains after automatic recovery.

- [ ] **Step 5: Run the full automated suites**

Run: `python -m pytest -q`

Expected: all Python tests pass; only explicitly documented environment-dependent tests may skip.

Run: `npm run test:node`

Expected: all Node tests pass.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Verify the final UI manually**

At desktop and narrow widths confirm four top-level menus, visible module boundaries, keyboard navigation, meaningful Chinese status text, working add/edit/rebind/delete flows, collapsed diagnostics, Profile removal, run-now behavior, screenshot viewing, Webhook status, and manual pause/restore.

- [ ] **Step 7: Commit acceptance fixes or record a no-change result**

If acceptance required code fixes, inspect `git status --short`, then stage only the exact fix files and their regression tests. Never use `git add .` or `git add -A`.

Commit staged acceptance fixes with `git commit -m "fix(probe): close manual inventory gaps"`.

If no fixes were required, do not create an empty commit; record full command results in the task handoff.

## Final Completion Checklist

- [ ] Current code contains no selector-probe semantic matching or LLM path.
- [ ] Old elements retain names and dependencies as `pending_rebind`.
- [ ] Collector lists visible interactive elements and supports named selection.
- [ ] Saved paths are deterministic CSS/XPath and pass current-window Dry-Run.
- [ ] Two Profiles and two rounds gate activation and recovery.
- [ ] Three failures create screenshot alert and pause only dependent strategies.
- [ ] Manual pause reasons survive automatic recovery.
- [ ] Redis atomic publication and last-known-good behavior remain intact.
- [ ] Four-menu UI and collapsed diagnostics match the approved mockups.
- [ ] Full Python and Node suites pass.
