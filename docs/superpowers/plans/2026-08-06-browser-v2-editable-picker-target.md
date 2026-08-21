# Browser V2 Editable Picker Target Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Browser V2 picker normalize both inner-node and outer-wrapper clicks to one real editable TikTok comment input before saving `kind=input`.

**Architecture:** Keep the existing picker payload and element schema. Resolve a unique editable DOM target in the injected overlay, then preserve the existing server-side strict editable Dry-Run. Map only the fixed input-target failure to one safe public API error.

**Tech Stack:** Vanilla JavaScript DOM APIs, Playwright async API, Flask, Node built-in test runner, pytest.

## Global Constraints

- Support clicks inside an editable ancestor and clicks on a wrapper containing exactly one editable descendant.
- Treat `input`, `textarea`, and any `[contenteditable]` whose value is not `false` as real editable candidates.
- A semantic `role=textbox` is not sufficient without a real editable node.
- Never guess when a wrapper contains more than one editable descendant.
- Keep `StrictLocatorResolver.resolve(..., require_editable=True)` as the final write gate.
- Do not change the database, saved element schema, strategy schema, input executor, AdsPower integration, or existing records.
- Do not expose DOM, selectors, exception text, or internal diagnostics in HTTP errors.
- `.git` metadata is read-only in this workspace; run tests and report changed files without claiming a commit.

---

### Task 1: Normalize inner and wrapper clicks to a real editable target

**Files:**
- Modify: `execution_v2/picker_overlay.js`
- Test: `tests-js/execution-v2-picker.test.js`

**Interfaces:**
- Consumes: click `composedPath()` and existing `resolveActionable(path)` behavior.
- Produces: `resolveEditableTarget(path) -> Element | null`; `resolveActionable(path)` prefers this result and retains existing click fallback.

- [ ] **Step 1: Write failing DOM tests**

Add fake-element support for attributes, descendants, `matches`, and `querySelectorAll`. Add these assertions:

```javascript
test("picker selects plaintext-only editable ancestor for an inner click", () => {
  const editor = element("DIV", {contenteditable: "plaintext-only"});
  const span = element("SPAN", {}, [], editor);
  assert.equal(resolveEditableTarget([span, editor]), editor);
  assert.equal(resolveActionable([span, editor]), editor);
});

test("picker selects one editable descendant from an outer wrapper", () => {
  const editor = element("DIV", {contenteditable: ""});
  const wrapper = element("DIV", {role: "textbox"}, [editor]);
  assert.equal(resolveEditableTarget([wrapper]), editor);
});

test("picker never guesses between two editable descendants", () => {
  const wrapper = element("DIV", {}, [
    element("TEXTAREA"), element("DIV", {contenteditable: "true"}),
  ]);
  assert.equal(resolveEditableTarget([wrapper]), null);
});

test("contenteditable false is not editable", () => {
  const blocked = element("DIV", {contenteditable: "false"});
  assert.equal(resolveEditableTarget([blocked]), null);
});
```

Keep the existing SVG-to-button test unchanged.

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
node --test tests-js/execution-v2-picker.test.js
```

Expected: new tests fail because `resolveEditableTarget` is not exported and wrapper fallback does not exist.

- [ ] **Step 3: Implement bounded editable resolution**

Add these functions and keep ordinary action resolution as fallback:

```javascript
const EDITABLE_DESCENDANTS = "input, textarea, [contenteditable]";

function isRealEditable(node) {
  if (!node || node.nodeType !== 1) return false;
  const tag = String(node.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea") return true;
  if (typeof node.getAttribute !== "function") return false;
  const value = node.getAttribute("contenteditable");
  return value !== null && String(value).toLowerCase() !== "false";
}

function uniqueEditableDescendant(node) {
  if (!node || typeof node.querySelectorAll !== "function") return null;
  const matches = Array.from(node.querySelectorAll(EDITABLE_DESCENDANTS)).filter(isRealEditable);
  return matches.length === 1 ? matches[0] : null;
}

function resolveEditableTarget(path) {
  const elements = (path || []).filter((node) => node && node.nodeType === 1);
  for (const node of elements) if (isRealEditable(node)) return node;
  for (const node of elements) {
    const tag = String(node.tagName || "").toLowerCase();
    if (tag === "body" || tag === "html") break;
    const descendant = uniqueEditableDescendant(node);
    if (descendant) return descendant;
  }
  return null;
}

function resolveActionable(path) {
  const editable = resolveEditableTarget(path);
  if (editable) return editable;
  // Retain the existing clickable/actionable scan and first-element fallback.
}
```

Add `contenteditable` to the overlay safe-attribute list and preserve its empty-string value. Capture/highlight/selector generation must use the normalized editable target; `original_tag` and original fingerprint continue to use the clicked node.

Export `resolveEditableTarget` with existing test exports.

- [ ] **Step 4: Run focused JavaScript tests**

Run the Task 1 command. Expected: all picker tests pass, including existing button and interaction-mode behavior.

---

### Task 2: Preserve editable locator data and return one safe error

**Files:**
- Modify: `execution_v2/picker.py`
- Modify: `execution_v2/blueprint.py`
- Test: `tests/test_execution_v2_picker.py`
- Test: `tests/test_execution_v2_routes.py`

**Interfaces:**
- Consumes: normalized overlay payload and existing `PickerError` mapping.
- Produces: stable internal error `picker_input_target_not_editable` and public API code `input_target_not_editable`.

- [ ] **Step 1: Write failing Python tests**

Add a retry test proving the selection remains available after input Dry-Run failure:

```python
def test_input_picker_failure_is_specific_and_selection_can_retry():
    page = FakePage()
    resolver = Resolver(error=RuntimeError("not editable"))

    async def scenario():
        session = await PickerService(resolver=resolver).start(
            binding(page), "https://www.tiktok.com/"
        )
        await capture(page, payload())
        await session.next_selection()
        with pytest.raises(PickerError, match="picker_input_target_not_editable"):
            await session.save_selection("评论输入", "action", "input")
        resolver.error = None
        saved = await session.save_selection("评论输入", "action", "input")
        assert saved["kind"] == "input"

    run(scenario())
```

Add a route test:

```python
def test_input_picker_error_has_safe_specific_message(client, service):
    service.error = PickerError("picker_input_target_not_editable")
    response = client.post(
        "/api/browser-v2/picker/pick/save",
        json={"name": "评论输入", "purpose": "action", "kind": "input"},
    )
    assert response.status_code == 422
    assert response.get_json() == {"error": {
        "code": "input_target_not_editable",
        "message": "未能定位唯一可编辑输入框，请点选输入文字区域后重试。",
    }}
```

- [ ] **Step 2: Run tests and verify generic-error failures**

Run:

```powershell
& .\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_picker.py tests/test_execution_v2_routes.py -q -p no:cacheprovider
```

Expected: retry test receives `picker_locator_invalid`; route test receives `validation_failed`.

- [ ] **Step 3: Preserve `contenteditable` and add fixed error mapping**

In `picker.py`, add `contenteditable` to `_SAFE_ATTRIBUTES`. Update sanitization so an explicitly present empty `contenteditable` value is retained. Generate an exact CSS candidate for an explicitly present `contenteditable` attribute:

```python
if "contenteditable" in attrs:
    add("css", _css_attr("contenteditable", str(attrs["contenteditable"])), 21)
```

Keep count/visibility/area/disabled/editability checks in `StrictLocatorResolver`; a non-unique attribute candidate is naturally rejected while unique CSS fallback can still pass.

Change only input failure wrapping:

```python
except Exception as error:
    code = "picker_input_target_not_editable" if kind == "input" else "picker_locator_invalid"
    raise PickerError(code) from error
```

In `blueprint.py`, add:

```python
"input_target_not_editable": "未能定位唯一可编辑输入框，请点选输入文字区域后重试。",
```

Before generic `PickerError` mapping, return `(422, "input_target_not_editable")` only when `str(error) == "picker_input_target_not_editable"`. All other picker errors remain `validation_failed`.

- [ ] **Step 4: Run focused Python tests**

Run the Task 2 command. Expected: all tests pass; error response contains no selector, DOM, raw exception, Profile ID, or websocket URL.

---

### Task 3: Regression and scope verification

**Files:**
- Verify: `execution_v2/picker_overlay.js`
- Verify: `execution_v2/picker.py`
- Verify: `execution_v2/blueprint.py`
- Verify: related tests

**Interfaces:**
- Consumes: Task 1 and Task 2 outputs.
- Produces: acceptance evidence for A/B input targets and unchanged click/input execution behavior.

- [ ] **Step 1: Run full picker and V2 input tests**

```powershell
node --test tests-js/execution-v2-picker.test.js tests-js/browser-v2-ui.test.js
& .\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_picker.py tests/test_execution_v2_routes.py tests/test_execution_v2_service.py tests/test_execution_v2_locator.py tests/test_execution_v2_actions.py -q -p no:cacheprovider
```

Expected: all tests pass.

- [ ] **Step 2: Run syntax and diff checks**

```powershell
& .\.venv\Scripts\python.exe -m py_compile execution_v2\picker.py execution_v2\blueprint.py
git diff --check -- execution_v2/picker_overlay.js execution_v2/picker.py execution_v2/blueprint.py tests-js/execution-v2-picker.test.js tests/test_execution_v2_picker.py tests/test_execution_v2_routes.py
```

Expected: both commands exit 0. Line-ending warnings are allowed; whitespace errors are not.

- [ ] **Step 3: Record Git limitation**

Do not run destructive Git commands. `.git/index.lock` creation is denied in this managed workspace, so report modified paths and test evidence without claiming staging or commit success.
