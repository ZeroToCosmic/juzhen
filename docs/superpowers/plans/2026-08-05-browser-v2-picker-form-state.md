# Browser V2 Picker Form State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the picker element-name form editable during polling and lock the selected AdsPower Profile until the picker terminates.

**Architecture:** Store the picker Profile token only in V2 frontend state. Give each safe picker selection a stable key and reuse its existing candidate form DOM node across one-second status renders. Replace the form only when selection identity changes or a save succeeds.

**Tech Stack:** Browser JavaScript, Node.js `node:test`, existing Flask-served V2 UI.

## Global Constraints

- Modify only `gateway/static/browser_v2.js` and `tests-js/browser-v2-ui.test.js`.
- Add no API, backend, database, dependency, storage, polling-interval, picker-overlay, or V1 changes.
- Keep Profile handles opaque and frontend-memory-only.
- Preserve form DOM identity for the same selection.
- Keep the last Profile selected after a terminal picker state, but unlock it.

---

### Task 1: Preserve and lock picker Profile state

**Files:**
- Modify: `gateway/static/browser_v2.js:95-99,194-214,358,388-400`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: existing `state.picker`, `activePicker()`, `#v2-picker-profile`, and opaque `profile_token` values.
- Produces: `state.pickerProfileToken: string`, kept in memory for render restoration and Profile locking.

- [ ] **Step 1: Add a minimal fake picker DOM helper and failing Profile test**

Add this helper near `browserWindow()`:

```js
function fakeNode(tag = "div") {
  const item = {
    tagName: tag.toUpperCase(), children: [], listeners: {}, value: "", disabled: false,
    append(...children) { this.children.push(...children.filter((child) => child && typeof child === "object")); },
    replaceChildren(...children) { this.children = []; this.append(...children); },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    setAttribute() {}, removeAttribute() {},
  };
  Object.defineProperty(item, "childElementCount", {get() { return item.children.length; }});
  return item;
}

function pickerDocument() {
  const fields = {
    "#v2-profile-list": fakeNode(),
    "#v2-picker-profile": fakeNode("select"),
    "#v2-picker-state": fakeNode(),
    "#v2-picker-candidates": fakeNode(),
  };
  return {
    fields,
    createElement: (tag) => fakeNode(tag),
    querySelector: (selector) => fields[selector] || null,
    querySelectorAll: () => [],
  };
}
```

Add test:

```js
test("active picker keeps selected Profile locked when status omits profile_token", () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.profilesAvailable = true;
  ui.state.profiles = [{profile_token: "profile_token_a", display_id: "***001"}];
  ui.state.pickerProfileToken = "profile_token_a";
  ui.state.picker = {id: "picker-1", status: "waiting_for_selection"};

  ui.render();

  const select = document.fields["#v2-picker-profile"];
  assert.equal(select.children.find((option) => option.selected)?.value, "profile_token_a");
  assert.equal(select.disabled, true);

  ui.state.picker = {id: "picker-1", status: "completed"};
  ui.render();
  assert.equal(select.children.find((option) => option.selected)?.value, "profile_token_a");
  assert.equal(select.disabled, false);
});
```

- [ ] **Step 2: Run the failing Profile test**

Run:

```powershell
node --test --test-name-pattern="active picker keeps selected Profile" tests-js\browser-v2-ui.test.js
```

Expected: FAIL because `pickerProfileToken` is not used and the select is not locked.

- [ ] **Step 3: Implement local Profile state and lock**

Extend state:

```js
picker: null, pickerProfileToken: "", renderedPickerSelectionKey: "",
repickTarget: null, draft: null, submitting: false, error: "", status: "准备加载", timer: null,
```

In `renderProfiles()`, capture the state value before clearing, use it to mark the option, and lock the selector:

```js
const pickerSelect = el("#v2-picker-profile");
const selectedPickerProfile = state.pickerProfileToken || (pickerSelect && pickerSelect.value) || "";
if (selectedPickerProfile) state.pickerProfileToken = selectedPickerProfile;
clear(pickerSelect); const empty = node("option", "请选择"); empty.value = ""; pickerSelect.append(empty);
state.profiles.forEach(function (profile) {
  const option = node("option", profileName(profile));
  option.value = profileToken(profile);
  option.selected = option.value === state.pickerProfileToken;
  pickerSelect.append(option);
});
```

Change the disabled rule:

```js
if (pickerSelect) pickerSelect.disabled = unavailable || activePicker();
```

Capture selection in `startPicker()` before the request:

```js
state.pickerProfileToken = profile;
```

Wire manual changes:

```js
el("#v2-picker-profile")?.addEventListener("change", function () {
  state.pickerProfileToken = el("#v2-picker-profile").value;
});
```

- [ ] **Step 4: Run focused and full V2 frontend tests**

Run:

```powershell
node --test --test-name-pattern="active picker keeps selected Profile" tests-js\browser-v2-ui.test.js
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: focused test PASS; existing V2 tests PASS.

- [ ] **Step 5: Commit Task 1**

```powershell
git add gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js
git commit -m "fix(v2): retain picker profile selection"
```

### Task 2: Preserve candidate form DOM during polling

**Files:**
- Modify: `gateway/static/browser_v2.js:40-41,238-265`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: safe picker selection fields `actionable_ancestor_fingerprint`, `original_fingerprint`, `unique_css`, and `relative_xpath`.
- Produces: `pickerSelectionKey(selection) -> string`; `state.renderedPickerSelectionKey` controls form reuse.

- [ ] **Step 1: Add failing DOM-identity and save-failure tests**

```js
test("polling the same picker selection preserves name input node and value", () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.profilesAvailable = true;
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };

  ui.render();
  const form = document.fields["#v2-picker-candidates"].children[0];
  const name = form.children[0];
  name.value = "评论入口";
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };
  ui.render();

  assert.equal(document.fields["#v2-picker-candidates"].children[0], form);
  assert.equal(form.children[0], name);
  assert.equal(name.value, "评论入口");

  ui.state.picker.selection = {tag: "button", actionable_ancestor_fingerprint: "button-2"};
  ui.render();
  assert.notEqual(document.fields["#v2-picker-candidates"].children[0], form);
  assert.equal(document.fields["#v2-picker-candidates"].children[0].children[0].value, "");
});

test("picker save failure keeps candidate form for retry", async () => {
  const document = pickerDocument();
  const ui = createBrowserV2UI({
    document,
    requestJson: async () => response(422, {error: {message: "保存失败"}}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.picker = {
    id: "picker-1", status: "selection_ready",
    selection: {tag: "button", actionable_ancestor_fingerprint: "button-1"},
  };
  ui.render();
  const form = document.fields["#v2-picker-candidates"].children[0];
  form.children[0].value = "评论入口";

  assert.equal(await ui.savePickerElement("评论入口", "action", "click"), false);
  ui.render();
  assert.equal(document.fields["#v2-picker-candidates"].children[0], form);
  assert.equal(form.children[0].value, "评论入口");
});
```

- [ ] **Step 2: Run tests and verify both fail**

Run:

```powershell
node --test --test-name-pattern="polling the same picker selection|picker save failure" tests-js\browser-v2-ui.test.js
```

Expected: FAIL because `renderPicker()` replaces the form on every render.

- [ ] **Step 3: Implement stable selection key and form reuse**

Add helper near `identifier()`:

```js
function pickerSelectionKey(selection) {
  if (!selection) return "";
  const fields = ["actionable_ancestor_fingerprint", "original_fingerprint", "unique_css", "relative_xpath"];
  for (const field of fields) {
    if (selection[field]) return field + ":" + String(selection[field]);
  }
  return JSON.stringify([selection.tag || "", selection.role || "", selection.name || "", selection.text_preview || ""]);
}
```

Change the beginning of `renderPicker()`:

```js
const stateNode = el("#v2-picker-state"), candidates = el("#v2-picker-candidates");
const active = activePicker();
["#v2-picker-finish", "#v2-picker-cancel"].forEach(function (selector) {
  const control = el(selector); if (control) control.disabled = !active;
});
if (!state.picker) {
  clear(candidates); state.renderedPickerSelectionKey = "";
  if (stateNode) stateNode.textContent = "尚未开启点选器。";
  return;
}
if (stateNode) stateNode.textContent = state.picker.error || (active ? "浏览器已打开，请在页面点选元素。" : stageLabel(state.picker.status));
const selection = state.picker.selection;
if (!selection) {
  clear(candidates); state.renderedPickerSelectionKey = ""; return;
}
const selectionKey = pickerSelectionKey(selection);
if (selectionKey === state.renderedPickerSelectionKey && candidates && candidates.childElementCount) return;
clear(candidates);
state.renderedPickerSelectionKey = selectionKey;
```

After successful `savePickerElement()`, before loading elements:

```js
state.picker = {...state.picker, status: "waiting_for_selection", selection: null};
state.renderedPickerSelectionKey = "";
```

- [ ] **Step 4: Run focused tests, V2 frontend suite, and diff checks**

Run:

```powershell
node --test --test-name-pattern="picker|Profile" tests-js\browser-v2-ui.test.js
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
git diff --check -- gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js
```

Expected: all tests PASS; `git diff --check` produces no errors.

- [ ] **Step 5: Browser acceptance**

Run latest app on an isolated local port. Start one picker with a test Profile, select one TikTok element, type and edit its name for longer than two polling intervals, and verify:

```text
Profile remains selected and disabled while picker active.
Name input remains focused and editable for at least 3 seconds.
Save succeeds with typed name.
Finish/cancel unlocks Profile selector and keeps last selected value.
```

- [ ] **Step 6: Commit Task 2**

```powershell
git add gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js
git commit -m "fix(v2): preserve picker form during polling"
```
