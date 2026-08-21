# Browser V2 Input Content Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a V2 keyboard-input action select a real input element and choose either fixed text or one randomly selected item from an existing brand copy library for each Profile execution.

**Architecture:** Keep the existing V2 strategy schema. Add one read-only V2 content-library endpoint backed by the existing gateway content store, and inject an async library resolver into `StrategyExecutor`. Extend the existing V2 editor without adding a database or returning copy bodies to the browser.

**Tech Stack:** Flask, asyncio, SQLite-backed existing content store, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- Input targets remain strict: `purpose=action` and `kind=input` only.
- The picker must save the real `<input>`, `<textarea>`, or `[contenteditable=true]` node as `kind=input`; click elements are never converted automatically.
- `GET /api/browser-v2/content-libraries` returns only `id`, `name`, and `copy_count`; it never returns copy bodies.
- A library item is selected independently for every Profile/action execution.
- Empty or missing libraries fail with the stable code `content_library_unavailable`.
- Execution results and logs never expose selected text; only existing action metadata may be persisted.
- No schema migration, new dependency, legacy strategy change, or unrelated refactor.
- `.git` metadata is read-only in this managed workspace. Run tests and record changed paths; do not claim a commit.

---

### Task 1: Closed V2 content-library API

**Files:**
- Modify: `execution_v2/service.py`
- Modify: `execution_v2/blueprint.py`
- Test: `tests/test_execution_v2_service.py`
- Test: `tests/test_execution_v2_routes.py`

**Interfaces:**
- Consumes: optional `content_library_provider: Callable[[], Any]` supplied by the gateway.
- Produces: `ExecutionV2Service.list_content_libraries() -> list[dict[str, str | int]]` and `GET /api/browser-v2/content-libraries`.

- [ ] **Step 1: Write failing service and route tests**

```python
def test_content_libraries_are_normalized_to_closed_public_shape(tmp_path):
    service, *_ = make_service(
        tmp_path,
        content_library_provider=lambda: [
            {"id": "ofs", "name": "OFS", "copy_count": 40, "body": "secret"},
            {"id": "", "name": "invalid", "copy_count": 1},
        ],
    )
    try:
        assert service.list_content_libraries() == [
            {"id": "ofs", "name": "OFS", "copy_count": 40}
        ]
    finally:
        service.close()
```

Add `FakeService.list_content_libraries`, add `GET /api/browser-v2/content-libraries` to the route matrix, and assert returned data contains no `body` field.

- [ ] **Step 2: Run tests and confirm missing-method failures**

Run:

```powershell
python -m pytest tests/test_execution_v2_service.py tests/test_execution_v2_routes.py -q
```

Expected: new tests fail because service method and route do not exist.

- [ ] **Step 3: Add minimal provider normalization**

Add constructor/factory parameter `content_library_provider=None`. Store `self._content_library_provider = content_library_provider or (lambda: [])`. Implement sync/async provider handling through the owned runtime, discard invalid IDs, clean names, coerce non-negative integer counts, and return exactly:

```python
{"id": library_id, "name": name or library_id, "copy_count": max(0, count)}
```

Add blueprint route:

```python
@blueprint.get("/content-libraries")
def list_content_libraries():
    return _data(_call(service(), "list_content_libraries"))
```

- [ ] **Step 4: Run focused tests**

Run the Task 1 pytest command. Expected: PASS.

---

### Task 2: Gateway library provider and random text resolver

**Files:**
- Modify: `gateway/app.py`
- Modify: `execution_v2/service.py`
- Test: `tests/test_app.py`
- Test: `tests/test_execution_v2_integration.py`

**Interfaces:**
- Consumes: `list_brands(data_dir)`, `list_copy_items(data_dir, brand_id)`, and `strategy_comment_texts(values)`.
- Produces: `build_execution_v2_content_library_provider(data_dir)` and `build_execution_v2_text_resolver(data_dir, *, rng=None)`; factory parameter `text_resolver` passed to `StrategyExecutor`.

- [ ] **Step 1: Write failing provider, resolver, and injection tests**

```python
def test_v2_text_resolver_picks_from_requested_library(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "list_copy_items", lambda _dir, brand: [
        {"body": "first"}, {"body": "second"}
    ] if brand == "ofs" else [])
    resolver = app_module.build_execution_v2_text_resolver(
        tmp_path, rng=type("Rng", (), {"choice": staticmethod(lambda values: values[-1])})()
    )
    assert asyncio.run(resolver({"content_library_id": "ofs"})) == "second"
```

Also test missing/empty library raises `RuntimeError("content_library_unavailable")`, provider returns counts without bodies, and `test_default_v2_service_receives_persisted_adspower_settings` captures both injected callables.

- [ ] **Step 2: Run tests and confirm missing-builder failures**

Run:

```powershell
python -m pytest tests/test_app.py tests/test_execution_v2_integration.py -q
```

Expected: new tests fail because builders and factory parameters are absent.

- [ ] **Step 3: Implement existing-store adapters and executor injection**

Implement an async provider using `asyncio.to_thread` to list brands and derive `copy_count`. Implement async resolver using `asyncio.to_thread(list_copy_items, data_dir, library_id)`, normalize through `strategy_comment_texts`, and call `rng.choice(texts)`. Reject blank IDs and empty results with the exact runtime error.

Extend `create_default_execution_v2_service(..., text_resolver=None)` and `ExecutionV2Service.__init__(..., text_resolver=None)`. When creating `StrategyExecutor`, pass:

```python
StrategyExecutor(
    self._resolver,
    text_resolver=text_resolver,
    on_stage=self._persist_executor_stage,
    capture_failure=self._capture_failure,
)
```

In `execution_v2_service_factory`, inject both gateway builders with the configured content directory.

- [ ] **Step 4: Run focused tests**

Run the Task 2 pytest command. Expected: PASS and no selected copy text in returned API payloads.

---

### Task 3: Keyboard-input editor controls and empty guidance

**Files:**
- Modify: `gateway/static/browser_v2.js`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: `GET /api/browser-v2/content-libraries` and existing input action fields `content_source`, `fixed_text`, `content_library_id`.
- Produces: source-specific form controls and closed-schema input action serialization.

- [ ] **Step 1: Write failing UI tests**

Add tests proving:

```javascript
assert.equal(ui.state.contentLibraries[0].id, "ofs");
assert.deepEqual(received.definition.actions[0], {
  id: "input-1", type: "input", element_id: "comment-input",
  content_source: "library", fixed_text: "", content_library_id: "ofs",
  interval_ms: [40, 120],
});
```

Also assert a zero-count library option is disabled and an empty eligible-input list renders guidance containing `<input>`, `<textarea>`, `[contenteditable=true]`, and `kind=input`.

- [ ] **Step 2: Run Node tests and confirm failures**

Run:

```powershell
node --test tests-js/browser-v2-ui.test.js
```

Expected: failures because libraries are not loaded, source controls are absent, and serialization forces fixed text.

- [ ] **Step 3: Implement minimal UI state, controls, and serialization**

Add `contentLibraries: []` to state and load `/content-libraries` during `init`. In input action cards:

- keep `elementField(["input"])`;
- show exact guidance when no eligible input element exists;
- render `content_source` selector with `fixed` and `library`;
- show fixed textarea only for `fixed`;
- show library selector only for `library`, with `name (copy_count 条)` labels and disabled zero-count options;
- clear the inactive field when source changes;
- preserve values in `state.draft` without a page-wide refresh.

Serialize selected source instead of hardcoding fixed:

```javascript
{
  id: action.id,
  type: "input",
  element_id: action.element_id,
  content_source: action.content_source,
  fixed_text: action.content_source === "fixed" ? (action.fixed_text || "") : "",
  content_library_id: action.content_source === "library" ? (action.content_library_id || "") : "",
  interval_ms: parseRange(action.interval_ms, "输入间隔范围", true),
}
```

- [ ] **Step 4: Run focused UI tests**

Run the Task 3 Node command. Expected: PASS.

---

### Task 4: Regression verification

**Files:**
- Verify only; no additional production files.

**Interfaces:**
- Consumes: all Task 1-3 outputs.
- Produces: evidence that picker, strategies, V2 routes, service, and gateway integration still work.

- [ ] **Step 1: Run V2 Python suite**

```powershell
python -m pytest tests/test_execution_v2_routes.py tests/test_execution_v2_service.py tests/test_execution_v2_integration.py tests/test_execution_v2_executor.py tests/test_execution_v2_actions.py tests/test_app.py -q
```

Expected: PASS.

- [ ] **Step 2: Run V2 JavaScript suite**

```powershell
node --test tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: PASS.

- [ ] **Step 3: Inspect diff scope**

```powershell
git diff -- execution_v2/service.py execution_v2/blueprint.py gateway/app.py gateway/static/browser_v2.js tests/test_execution_v2_service.py tests/test_execution_v2_routes.py tests/test_execution_v2_integration.py tests/test_app.py tests-js/browser-v2-ui.test.js
```

Expected: only approved API, gateway adapters, UI, tests, and this plan/spec documentation changed. Do not stage or commit because `.git` is read-only.

