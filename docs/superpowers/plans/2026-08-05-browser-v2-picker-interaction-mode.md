# Browser V2 Picker Interaction Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add safe element-selection and pass-through page-interaction modes to the AdsPower picker overlay.

**Architecture:** Keep mode entirely inside the existing page-local overlay. A picker-owned floating toolbar and `F2` switch between default `select` mode and pass-through `interact` mode; existing backend session and event payloads remain unchanged.

**Tech Stack:** Browser JavaScript, DOM event capture, Node.js `node:test`.

## Global Constraints

- Modify only `execution_v2/picker_overlay.js` and `tests-js/execution-v2-picker.test.js`.
- Default mode is `select`.
- `interact` mode passes ordinary page click, input, keyboard, focus, and scroll behavior through unchanged.
- Add no API, backend, database, storage, dependency, selector-generation, AdsPower adapter, or management-page changes.
- Toolbar state is page-memory-only and resets to `select` after navigation or reinstall.
- `Escape` keeps existing cancel behavior.

---

### Task 1: Add page-local select/interact modes

**Files:**
- Modify: `execution_v2/picker_overlay.js:8-10,103-166`
- Test: `tests-js/execution-v2-picker.test.js:1-48`

**Interfaces:**
- Consumes: existing `createPickerOverlay({document, emit})`, capture-phase `pointermove`, `click`, and `keydown` listeners.
- Produces: `overlay.mode() -> "select" | "interact"`; toolbar buttons and `F2` call internal `setMode(mode)`.

- [ ] **Step 1: Replace the old overlay test harness with tracked fake DOM nodes**

Add helpers before overlay tests:

```js
function fakeElement(tagName = "DIV") {
  const attributes = new Map();
  const listeners = new Map();
  return {
    nodeType: 1, tagName, style: {}, children: [], attributes, listeners, removed: false,
    append(...children) { this.children.push(...children); },
    appendChild(child) { this.children.push(child); return child; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
    getAttribute(name) { return attributes.get(name) || null; },
    addEventListener(name, handler) { listeners.set(name, handler); },
    remove() { this.removed = true; },
  };
}

function overlayHarness() {
  const emitted = [];
  const listeners = new Map();
  const appended = [];
  const document = {
    body: {appendChild: (node) => { appended.push(node); return node; }},
    createElement: (tag) => fakeElement(tag.toUpperCase()),
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: (name) => listeners.delete(name),
    querySelectorAll: () => [],
  };
  const overlay = createPickerOverlay({document, emit: (event) => emitted.push(event)});
  return {document, overlay, emitted, listeners, appended};
}

function eventFor(path, key = "") {
  const calls = {prevented: 0, stopped: 0};
  return {
    key, calls, composedPath: () => path,
    preventDefault: () => { calls.prevented += 1; },
    stopPropagation: () => { calls.stopped += 1; },
  };
}
```

- [ ] **Step 2: Write failing mode, toolbar-exclusion, lifecycle, and shortcut tests**

```js
test("picker defaults to select then interaction mode passes page clicks through", () => {
  const {overlay, emitted, listeners, appended} = overlayHarness();
  const button = target("BUTTON");
  overlay.install();
  assert.equal(overlay.mode(), "select");
  assert.equal(appended.length, 2);

  listeners.get("pointermove")(eventFor([button]));
  const selected = eventFor([button]);
  listeners.get("click")(selected);
  assert.equal(selected.calls.prevented, 1);
  assert.equal(selected.calls.stopped, 1);
  assert.equal(emitted.length, 1);

  const toolbar = appended.find((node) => node.getAttribute("data-execution-v2-picker-ui") === "toolbar");
  const interact = toolbar.children.find((node) => node.getAttribute("data-picker-mode") === "interact");
  const toolbarCapture = eventFor([interact, toolbar]);
  listeners.get("click")(toolbarCapture);
  interact.listeners.get("click")(toolbarCapture);
  assert.equal(overlay.mode(), "interact");
  assert.equal(emitted.length, 1);

  const passed = eventFor([button]);
  listeners.get("click")(passed);
  assert.deepEqual(passed.calls, {prevented: 0, stopped: 0});
  assert.equal(emitted.length, 1);
});

test("F2 toggles picker mode and Escape still cancels", () => {
  const {overlay, emitted, listeners} = overlayHarness();
  overlay.install();
  const first = eventFor([], "F2");
  listeners.get("keydown")(first);
  assert.equal(overlay.mode(), "interact");
  assert.deepEqual(first.calls, {prevented: 1, stopped: 1});
  listeners.get("keydown")(eventFor([], "F2"));
  assert.equal(overlay.mode(), "select");
  listeners.get("keydown")(eventFor([], "Escape"));
  assert.equal(emitted.at(-1).type, "cancel");
  assert.equal(listeners.size, 0);
});

test("uninstall removes picker toolbar marker and resets mode", () => {
  const {overlay, listeners, appended} = overlayHarness();
  overlay.install();
  listeners.get("keydown")(eventFor([], "F2"));
  overlay.uninstall();
  assert.equal(appended.every((node) => node.removed), true);
  assert.equal(listeners.size, 0);
  assert.equal(overlay.mode(), "select");
});
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```powershell
node --test --test-name-pattern="interaction mode|F2 toggles|uninstall removes picker" tests-js\execution-v2-picker.test.js
```

Expected: FAIL because the overlay has no `mode()`, toolbar, or pass-through state.

- [ ] **Step 4: Implement picker-owned UI detection and mode state**

Add constants and helpers:

```js
const PICKER_UI_ATTRIBUTE = "data-execution-v2-picker-ui";

function isPickerOwned(path) {
  return (path || []).some((node) => node && node.nodeType === 1
    && typeof node.getAttribute === "function" && node.getAttribute(PICKER_UI_ATTRIBUTE));
}
```

Inside `createPickerOverlay()` add:

```js
let toolbar = null;
let modeButtons = [];
let modeStatus = null;
let mode = "select";

function clearHighlight() {
  active = null;
  if (marker) marker.style.display = "none";
}

function setMode(next) {
  if (next !== "select" && next !== "interact") return false;
  mode = next;
  if (mode === "interact") clearHighlight();
  for (const button of modeButtons) {
    button.setAttribute("aria-pressed", String(button.getAttribute("data-picker-mode") === mode));
  }
  if (modeStatus) modeStatus.textContent = mode === "select" ? "当前：选择元素" : "当前：操作页面，可能触发真实行为";
  return true;
}
```

Use a small `createToolbar()` closure that creates a fixed toolbar, two buttons with `data-picker-mode="select|interact"`, and one status node. Mark every toolbar node with `PICKER_UI_ATTRIBUTE`, attach button click handlers that prevent/stop their toolbar event, and call `setMode()`.

```js
function pickerNode(tag, ownedValue) {
  const item = document.createElement(tag);
  item.setAttribute(PICKER_UI_ATTRIBUTE, ownedValue);
  return item;
}

function createToolbar() {
  const root = pickerNode("div", "toolbar");
  Object.assign(root.style, {
    position: "fixed", top: "12px", right: "12px", zIndex: "2147483647",
    display: "flex", gap: "6px", alignItems: "center", padding: "8px",
    background: "#111827", color: "#fff", borderRadius: "8px", font: "13px sans-serif",
  });
  const select = pickerNode("button", "control");
  select.type = "button"; select.setAttribute("data-picker-mode", "select"); select.textContent = "选择元素";
  const interact = pickerNode("button", "control");
  interact.type = "button"; interact.setAttribute("data-picker-mode", "interact"); interact.textContent = "操作页面";
  const status = pickerNode("span", "status"); status.setAttribute("data-picker-status", "true");
  for (const [button, value] of [[select, "select"], [interact, "interact"]]) {
    button.addEventListener("click", (event) => {
      event.preventDefault(); event.stopPropagation(); setMode(value);
    });
  }
  modeButtons = [select, interact]; modeStatus = status;
  root.append(select, interact, status); return root;
}
```

- [ ] **Step 5: Gate pointer/click behavior and update lifecycle**

At the start of `move`:

```js
const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
if (mode !== "select" || isPickerOwned(path)) { clearHighlight(); return; }
```

At the start of `click`, resolve its path before checking `active`:

```js
const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
if (mode !== "select" || isPickerOwned(path) || !active) return;
```

Update `keydown`:

```js
if (event.key === "F2") {
  event.preventDefault(); event.stopPropagation();
  setMode(mode === "select" ? "interact" : "select"); return;
}
if (event.key !== "Escape") return;
```

In `install()`, keep `mode = "select"`, create/append marker, then attempt toolbar creation without weakening selection when it fails:

```js
mode = "select";
try { toolbar = createToolbar(); document.body.appendChild(toolbar); }
catch (_) { toolbar = null; modeButtons = []; modeStatus = null; }
setMode("select");
```

In `uninstall()`:

```js
if (toolbar && typeof toolbar.remove === "function") toolbar.remove();
toolbar = null; modeButtons = []; modeStatus = null; mode = "select"; clearHighlight(); marker = null;
```

Expose read-only mode:

```js
mode() { return mode; },
```

- [ ] **Step 6: Run focused, V2, and full Node tests**

Run:

```powershell
node --test --test-name-pattern="interaction mode|F2 toggles|uninstall removes picker|overlay installs" tests-js\execution-v2-picker.test.js
node --test tests-js\execution-v2-picker.test.js tests-js\browser-v2-ui.test.js
& npm.cmd run test:node -- --test-reporter=spec
git diff --check -- execution_v2/picker_overlay.js tests-js/execution-v2-picker.test.js
```

Expected: focused tests PASS; V2 tests PASS; full Node suite PASS; diff check has no errors.

- [ ] **Step 7: Manual AdsPower acceptance**

Start one picker and verify this exact sequence:

```text
Start: 选择元素 active; clicking a target records but does not activate it.
Switch 操作页面: comment/search control opens normally; typing and scrolling work.
Switch 选择元素: nested comment/search element highlights and records.
Toolbar clicks never appear as candidates.
F2 toggles both modes; Escape cancels and removes toolbar/highlight.
```

- [ ] **Step 8: Commit**

```powershell
git add execution_v2/picker_overlay.js tests-js/execution-v2-picker.test.js
git commit -m "feat(v2): add picker interaction mode"
```
