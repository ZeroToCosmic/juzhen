# Browser V2 Strategy Record Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert flat API strategy records into nested editor drafts before editing or continuing after save.

**Architecture:** Add one pure UI-boundary adapter that deep-copies a strategy record and moves six definition fields under `definition`. Use it only when opening an editor draft and when accepting a successful save response.

**Tech Stack:** JavaScript, Node.js built-in test runner

## Global Constraints

- Production change limited to `gateway/static/browser_v2.js`.
- Add no API, backend, database, HTML, dependency, or request-schema change.
- Keep `state.strategies` in API flat-record form.

---

### Task 1: Normalize API Records at Editor Boundaries

**Files:**
- Modify: `gateway/static/browser_v2.js:40-50, 336-345, 397-400`
- Test: `tests-js/browser-v2-ui.test.js`

**Interfaces:**
- Produces: `strategyDraft(record: object): object` with nested `definition.actions`.
- Consumes: flat API records and existing nested new-strategy drafts.

- [ ] **Step 1: Add failing edit-boundary test**

```javascript
test("flat API strategy opens as a nested editor draft", () => {
  const document = strategyDocument();
  const ui = createBrowserV2UI({
    document, requestJson: async () => response(200, {data: []}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.strategies = [{
    id: "strategy-1", name: "评论策略", enabled: true, revision: 1,
    target_url: "https://www.tiktok.com/", ready_element_id: "ready-1",
    readiness_timeout_seconds: 30, run_mode: "once", loop_duration_minutes: null,
    actions: [actionTemplate("wait", "wait-1")],
  }];

  ui.render();
  document.fields["#v2-strategy-list"].children[0].children[1].listeners.click();

  assert.equal(ui.state.draft.definition.target_url, "https://www.tiktok.com/");
  assert.deepEqual(ui.state.draft.definition.actions, [actionTemplate("wait", "wait-1")]);
  assert.equal(Object.hasOwn(ui.state.draft, "actions"), false);
});
```

- [ ] **Step 2: Add failing save-response test**

```javascript
test("successful strategy save keeps a nested editable draft", async () => {
  const document = strategyDocument();
  const fields = document.fields;
  fields["#v2-strategy-name"].value = "评论策略";
  fields["#v2-strategy-target-url"].value = "https://www.tiktok.com/";
  fields["#v2-strategy-ready-element"].value = "ready-1";
  fields["#v2-strategy-readiness-timeout"].value = "30";
  fields["#v2-strategy-run-mode"].value = "once";
  const flat = {
    id: "strategy-1", name: "评论策略", enabled: true, revision: 1,
    target_url: "https://www.tiktok.com/", ready_element_id: "ready-1",
    readiness_timeout_seconds: 30, run_mode: "once", loop_duration_minutes: null,
    actions: [actionTemplate("wait", "wait-1")],
  };
  const ui = createBrowserV2UI({
    document,
    requestJson: async (_url, method) => method === "POST"
      ? response(201, {data: flat}) : response(200, {data: [flat]}),
    setTimeout: () => 1, clearTimeout: () => {}, storage: {getItem: () => null, setItem: () => {}},
  });
  ui.state.draft = {
    id: "strategy-1", localNew: true, name: "评论策略", enabled: true,
    definition: {target_url: "https://www.tiktok.com/", ready_element_id: "ready-1", readiness_timeout_seconds: 30, run_mode: "once", loop_duration_minutes: null, actions: [actionTemplate("wait", "wait-1")]},
  };

  assert.equal(await ui.saveStrategy(), true);
  assert.deepEqual(ui.state.draft.definition.actions, [actionTemplate("wait", "wait-1")]);
  assert.equal(ui.state.draft.localNew, false);
});
```

- [ ] **Step 3: Run test and verify failure**

```powershell
node --test tests-js/browser-v2-ui.test.js
```

Expected: both new tests fail because flat records have no `definition`.

- [ ] **Step 4: Add normalization function**

```javascript
const STRATEGY_DEFINITION_FIELDS = [
  "target_url", "ready_element_id", "readiness_timeout_seconds",
  "run_mode", "loop_duration_minutes", "actions",
];

function strategyDraft(record) {
  const draft = clone(record || {});
  if (draft.definition && typeof draft.definition === "object") return draft;
  const definition = {};
  STRATEGY_DEFINITION_FIELDS.forEach(function (field) {
    definition[field] = draft[field];
    delete draft[field];
  });
  if (!Array.isArray(definition.actions)) definition.actions = [];
  draft.definition = definition;
  return draft;
}
```

- [ ] **Step 5: Apply adapter at both editor boundaries**

Change strategy-list edit handler:

```javascript
state.draft = strategyDraft(item);
```

Change successful save assignment:

```javascript
state.draft = strategyDraft(result.data || state.strategies.find(function (item) { return item.name === name; }) || state.draft);
```

- [ ] **Step 6: Run focused and related tests**

```powershell
node --test tests-js/browser-v2-ui.test.js
node --test tests-js/browser-v2-ui.test.js tests-js/execution-v2-picker.test.js
```

Expected: all tests pass with zero failures.

- [ ] **Step 7: Commit when Git metadata is writable**

```powershell
git add -- gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js docs/superpowers/specs/2026-08-05-browser-v2-strategy-record-normalization-design.md docs/superpowers/plans/2026-08-05-browser-v2-strategy-record-normalization.md
git commit -m "fix(v2): normalize strategy editor records"
```

Current managed environment cannot create `.git/index.lock`; test verification remains available.

