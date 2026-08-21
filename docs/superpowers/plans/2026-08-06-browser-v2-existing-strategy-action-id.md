# Browser V2 Existing Strategy Action ID Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent newly added actions from reusing IDs already present in an existing Browser V2 strategy.

**Architecture:** Allocate a new action ID at insertion time by scanning the current draft's action IDs and advancing the existing page-local sequence until an unused `action_N` value is found. Preserve all saved action IDs and keep backend uniqueness validation unchanged.

**Tech Stack:** Browser JavaScript, Node.js built-in test runner, Python pytest.

## Global Constraints

- Modify only `gateway/static/browser_v2.js` and `tests-js/browser-v2-ui.test.js`.
- Preserve existing action IDs, order, types, and parameters.
- Do not change API routes, payload schema, database, executor, backend validator, or UI layout.
- `.git` metadata is read-only; do not stage or commit files.

---

### Task 1: Allocate an unused action ID when editing a saved strategy

**Files:**
- Modify: `gateway/static/browser_v2.js:399`
- Test: `tests-js/browser-v2-ui.test.js:117-127`

**Interfaces:**
- Consumes: `state.draft.definition.actions`, where every action may contain a string `id`.
- Produces: `nextActionId(actions)` returning the first unused page-local `action_N` identifier.

- [ ] **Step 1: Write the failing regression test**

Add after the existing action composition test:

```javascript
test("adding actions to an existing strategy skips IDs already in use", () => {
  const {ui} = harness();
  ui.state.draft = {
    id: "strategy-1",
    name: "Existing strategy",
    definition: {
      actions: [actionTemplate("wait", "action_1"), actionTemplate("wait", "action_2")],
    },
  };

  assert.equal(ui.addAction("scroll"), true);
  assert.equal(ui.addAction("wait"), true);

  const ids = ui.state.draft.definition.actions.map((item) => item.id);
  assert.deepEqual(ids, ["action_1", "action_2", "action_3", "action_4"]);
  assert.equal(new Set(ids).size, ids.length);
});
```

- [ ] **Step 2: Run the focused test and verify the bug**

```powershell
node --test --test-name-pattern="adding actions to an existing strategy skips IDs already in use" tests-js\browser-v2-ui.test.js
```

Expected: FAIL because current code produces duplicate IDs beginning with `action_1`.

- [ ] **Step 3: Add the minimal action-ID allocator**

Add immediately before `addAction()` and replace `addAction()`:

```javascript
function nextActionId(actions) {
  const used = new Set((actions || []).map(function (item) { return item && item.id; }).filter(Boolean));
  let candidate;
  do { candidate = "action_" + (++actionSequence); } while (used.has(candidate));
  return candidate;
}
function addAction(type) {
  if (!state.draft) return false;
  const actions = state.draft.definition.actions;
  actions.push(actionTemplate(type, nextActionId(actions)));
  renderStrategies();
  return true;
}
```

- [ ] **Step 4: Run focused and complete JavaScript tests**

```powershell
node --test --test-name-pattern="adding actions to an existing strategy skips IDs already in use" tests-js\browser-v2-ui.test.js
npm run test:node
```

Expected: focused test passes; complete Node test suite passes.

- [ ] **Step 5: Run full Browser V2 regression and static checks**

```powershell
$v2Tests = Get-ChildItem -LiteralPath tests -Filter 'test_execution_v2_*.py' | ForEach-Object { $_.FullName }
& .\.venv\Scripts\python.exe -m pytest @v2Tests -q -p no:cacheprovider
node --check gateway\static\browser_v2.js
git diff --check -- gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js
```

Expected: all V2 tests pass; syntax and diff checks exit 0.

- [ ] **Step 6: Record workspace state**

```powershell
git status --short -- gateway/static/browser_v2.js tests-js/browser-v2-ui.test.js
```

Expected: both changed paths are reported. Do not run `git add` or `git commit` because `.git` metadata is read-only.
