# Browser V2 Picker Click Capture Minimal Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make picker clicks capture the actionable node from the click event instead of a stale hover reference.

**Architecture:** Keep the existing page-local picker and strict backend Dry-Run unchanged. Re-resolve the actionable node from `click` event `composedPath()`, reject only explicitly detached nodes, then capture that exact node.

**Tech Stack:** JavaScript, Node.js built-in test runner

## Global Constraints

- Modify only `execution_v2/picker_overlay.js` and `tests-js/execution-v2-picker.test.js`.
- Add no API endpoint, database migration, dependency, backend validation change, or UI change.
- Preserve ordinary non-actionable element selection.

---

### Task 1: Capture Click-Time Actionable Node

**Files:**
- Modify: `execution_v2/picker_overlay.js:171-178`
- Test: `tests-js/execution-v2-picker.test.js`

**Interfaces:**
- Consumes: `resolveActionable(path: Element[]): Element | null`
- Produces: `click(event)` captures the current click-path node and updates `overlay.highlighted()` to that node.

- [ ] **Step 1: Write the failing regression test**

Add after the existing SVG ancestor test:

```javascript
test("picker click re-resolves actionable node instead of using stale hover", () => {
  const {overlay, emitted, listeners} = overlayHarness();
  const stale = target("DIV");
  const replacementButton = target("BUTTON");
  const svg = target("SVG", replacementButton);
  overlay.install();

  listeners.get("pointermove")(eventFor([stale]));
  listeners.get("click")(eventFor([svg, replacementButton]));

  assert.equal(overlay.highlighted(), replacementButton);
  assert.equal(emitted.length, 1);
  assert.equal(emitted[0].original_tag, "svg");
  assert.equal(emitted[0].actionable_tag, "button");
});
```

- [ ] **Step 2: Run test to verify failure**

Run:

```powershell
node --test tests-js/execution-v2-picker.test.js
```

Expected: new test fails because `overlay.highlighted()` remains `stale` and emitted actionable tag is `div`.

- [ ] **Step 3: Implement click-time resolution**

Replace the click handler body with:

```javascript
const click = (event) => {
  const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
  if (mode !== "select" || isPickerOwned(path)) return;
  const actionable = resolveActionable(path);
  if (!actionable || actionable.isConnected === false) return;
  active = actionable;
  event.preventDefault();
  event.stopPropagation();
  emit(capture(document, path[0] || actionable, actionable));
};
```

- [ ] **Step 4: Run focused and full picker tests**

Run:

```powershell
node --test tests-js/execution-v2-picker.test.js
node --test tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: all tests pass with zero failures.

- [ ] **Step 5: Commit when Git metadata is writable**

```powershell
git add -- execution_v2/picker_overlay.js tests-js/execution-v2-picker.test.js docs/superpowers/specs/2026-08-05-browser-v2-picker-click-capture-minimal-fix-design.md docs/superpowers/plans/2026-08-05-browser-v2-picker-click-capture-minimal-fix.md
git commit -m "fix(v2): capture picker click target"
```

Current managed environment cannot create `.git/index.lock`; code verification does not depend on this commit.

