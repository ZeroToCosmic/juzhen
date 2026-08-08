# Browser V2 Strategy Editor State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep strategy fields and action editors stable while picker or job polling runs.

**Architecture:** Treat `state.draft` as strategy editor source of truth by synchronizing every static strategy field. Limit active polling renders to Profile controls, job status, and picker status so strategy action DOM nodes remain intact.

**Tech Stack:** JavaScript, Node.js built-in test runner

## Global Constraints

- Production change limited to `gateway/static/browser_v2.js`.
- Add no HTML, API, database, backend validation, or dependency change.
- Preserve existing strategy request schema and validation.

---

### Task 1: Preserve Strategy Draft During Active Polling

**Files:**
- Modify: `gateway/static/browser_v2.js:154-166, 405-410`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Consumes: `state.draft`, `refreshActive()`, existing static strategy form controls.
- Produces: static field events update `state.draft`; active polling never calls full `render()`.

- [ ] **Step 1: Add failing draft synchronization test**

Add this fixture after `pickerDocument()`:

```javascript
function strategyDocument() {
  const fields = {
    "#v2-strategy-list": fakeNode(),
    "#v2-strategy-editor": fakeNode(),
    "#v2-strategy-empty": fakeNode(),
    "#v2-action-list": fakeNode("ol"),
    "#v2-strategy-name": fakeNode("input"),
    "#v2-strategy-target-url": fakeNode("input"),
    "#v2-strategy-ready-element": fakeNode("select"),
    "#v2-strategy-readiness-timeout": fakeNode("input"),
    "#v2-strategy-run-mode": fakeNode("select"),
    "#v2-strategy-minutes": fakeNode("input"),
    "#v2-strategy-enabled": fakeNode("input"),
    "#v2-strategy-minutes-wrap": fakeNode(),
  };
  fields["#v2-strategy-enabled"].checked = true;
  return {
    fields,
    createElement: (tag) => fakeNode(tag),
    querySelector: (selector) => fields[selector] || null,
    querySelectorAll: () => [],
  };
}
```

Add this test:

```javascript
test("strategy static fields synchronize into draft", () => {
  const document = strategyDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-new", localNew: true, name: "旧名称", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: []},
  };
  const fields = document.fields;
  fields["#v2-strategy-name"].value = "新名称"; fields["#v2-strategy-name"].listeners.input();
  fields["#v2-strategy-target-url"].value = "https://www.tiktok.com/foryou"; fields["#v2-strategy-target-url"].listeners.input();
  fields["#v2-strategy-ready-element"].value = "ready-1"; fields["#v2-strategy-ready-element"].listeners.change();
  fields["#v2-strategy-readiness-timeout"].value = "30"; fields["#v2-strategy-readiness-timeout"].listeners.input();
  fields["#v2-strategy-minutes"].value = "2-5"; fields["#v2-strategy-minutes"].listeners.input();
  fields["#v2-strategy-enabled"].checked = false; fields["#v2-strategy-enabled"].listeners.change();
  fields["#v2-strategy-run-mode"].value = "duration"; fields["#v2-strategy-run-mode"].listeners.change();

  assert.equal(ui.state.draft.name, "新名称");
  assert.equal(ui.state.draft.definition.target_url, "https://www.tiktok.com/foryou");
  assert.equal(ui.state.draft.definition.ready_element_id, "ready-1");
  assert.equal(ui.state.draft.definition.readiness_timeout_seconds, "30");
  assert.equal(ui.state.draft.definition.loop_duration_minutes, "2-5");
  assert.equal(ui.state.draft.definition.run_mode, "duration");
  assert.equal(ui.state.draft.enabled, false);
  assert.equal(fields["#v2-strategy-minutes-wrap"].hidden, false);
});
```

- [ ] **Step 2: Add failing polling isolation test**

Add this test:

```javascript
test("active picker polling does not rebuild strategy action editors", async () => {
  const document = strategyDocument();
  const scheduled = [];
  const ui = createBrowserV2UI({
    document,
    requestJson: async () => response(200, {data: {id: "picker-1", status: "waiting_for_selection"}}),
    setTimeout: (fn, delay) => { const timer = {fn, delay}; scheduled.push(timer); return timer; },
    clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-new", localNew: true, name: "策略", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "", readiness_timeout_seconds: 15, run_mode: "once", loop_duration_minutes: null, actions: [actionTemplate("wait", "wait-1")]},
  };
  ui.state.picker = {id: "picker-1", status: "waiting_for_selection"};
  ui.render();
  const actionCard = document.fields["#v2-action-list"].children[0];

  ui.syncPolling();
  await scheduled[0].fn();

  assert.equal(document.fields["#v2-action-list"].children[0], actionCard);
});
```

- [ ] **Step 3: Run tests and verify failure**

```powershell
node --test tests-js/browser-v2-ui.test.js
```

Expected: field test lacks `input`/`change` listeners; polling test reports a different action-card object.

- [ ] **Step 4: Synchronize static strategy controls**

Add event handlers in `wire()`:

```javascript
el("#v2-strategy-name")?.addEventListener("input", function () {
  if (state.draft) state.draft.name = el("#v2-strategy-name").value;
});
el("#v2-strategy-target-url")?.addEventListener("input", function () {
  if (state.draft) state.draft.definition.target_url = el("#v2-strategy-target-url").value;
});
el("#v2-strategy-ready-element")?.addEventListener("change", function () {
  if (state.draft) state.draft.definition.ready_element_id = el("#v2-strategy-ready-element").value;
});
el("#v2-strategy-readiness-timeout")?.addEventListener("input", function () {
  if (state.draft) state.draft.definition.readiness_timeout_seconds = el("#v2-strategy-readiness-timeout").value;
});
el("#v2-strategy-minutes")?.addEventListener("input", function () {
  if (state.draft) state.draft.definition.loop_duration_minutes = el("#v2-strategy-minutes").value;
});
el("#v2-strategy-enabled")?.addEventListener("change", function () {
  if (state.draft) state.draft.enabled = el("#v2-strategy-enabled").checked;
});
```

Change run-mode handler to update only draft and minutes visibility:

```javascript
el("#v2-strategy-run-mode")?.addEventListener("change", function () {
  if (!state.draft) return;
  state.draft.definition.run_mode = el("#v2-strategy-run-mode").value;
  const wrap = el("#v2-strategy-minutes-wrap");
  if (wrap) wrap.hidden = state.draft.definition.run_mode !== "duration";
});
```

- [ ] **Step 5: Limit active polling render scope**

Replace the final `render()` in `refreshActive()` with:

```javascript
renderProfiles();
renderJob();
renderPicker();
setMessage(state.error, state.status);
```

- [ ] **Step 6: Run focused and related tests**

```powershell
node --test tests-js/browser-v2-ui.test.js
node --test tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: all tests pass with zero failures.

- [ ] **Step 7: Commit when Git metadata is writable**

```powershell
git add -- gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js docs/superpowers/specs/2026-08-05-browser-v2-strategy-editor-state-design.md docs/superpowers/plans/2026-08-05-browser-v2-strategy-editor-state.md
git commit -m "fix(v2): preserve strategy editor state"
```

Current managed environment cannot create `.git/index.lock`; test verification remains available.
