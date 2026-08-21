# Browser Auto Element Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make automatic strategy input and submit fields immediately select from the current saved webpage-element aliases.

**Architecture:** Keep the existing inline dashboard implementation in `gateway/app.py`. Add one focused option-synchronization function that rebuilds only the three element selectors, preserves valid form choices, migrates renamed aliases, and clears deleted aliases without rerendering unrelated strategy fields.

**Tech Stack:** Python 3, Flask-rendered HTML, browser JavaScript, pytest.

## Global Constraints

- Input and submit values must come only from `browserActionConfig.elements` aliases.
- Do not change backend endpoints, saved configuration fields, XPath editing, or browser execution behavior.
- Element synchronization must not overwrite unsaved automatic-strategy form fields.
- Use test-first development and make only request-related changes.

---

### Task 1: Synchronize automatic-strategy element selectors

**Files:**
- Modify: `gateway/app.py:3415-3682`
- Create: `tests-js/browser-auto-element-options.test.js`

**Interfaces:**
- Consumes: `browserActionConfig.elements: Record<string, string>` and the current values of `#browser-auto-input-element`, `#browser-auto-submit-element`, and `#browser-auto-click-element-picker`.
- Produces: `syncBrowserAutoElementOptions(aliasChanges = {})`, where `aliasChanges` maps an old alias to its renamed alias; the function returns nothing and updates only those three selectors.

- [ ] **Step 1: Write failing behavioral tests**

Create `tests-js/browser-auto-element-options.test.js`. Read `gateway/app.py`, extract the complete `syncBrowserAutoElementOptions` function by locating its declaration and balancing braces, then evaluate it in a Node `vm` context containing:

- `browserActionConfig.elements`
- a fake `document.querySelector` returning three fake select controls
- `escapeHtml` as an identity function for controlled test aliases
- fake selects whose `innerHTML` setter parses option values and applies native-select first-option behavior

Add three `node:test` cases:

1. Starting with `input` and `submit`, preserve a selected `input`, add `extra`, synchronize, and assert all three aliases are available while the selected value stays `input`.
2. Starting with selected `input`, replace it in the element map with `comment_input`, call the function with `{input: "comment_input"}`, and assert selection becomes `comment_input`.
3. Starting with selected `submit`, delete it from the element map, synchronize, and assert the selection becomes empty while an unrelated form field retains its value.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```powershell
node --test tests-js/browser-auto-element-options.test.js
```

Expected: all three tests FAIL because `syncBrowserAutoElementOptions` does not exist.

- [ ] **Step 3: Add the focused synchronization function**

In `gateway/app.py`, immediately before `refreshBrowserAutoElementOptions`, add:

```javascript
    function syncBrowserAutoElementOptions(aliasChanges = {}) {
      const aliases = Object.keys(browserActionConfig.elements || {});
      const options = aliases.map((alias) =>
        `<option value="${escapeHtml(alias)}">${escapeHtml(alias)}</option>`).join("");
      [
        {id: "browser-auto-input-element", placeholder: "请选择输入元素"},
        {id: "browser-auto-submit-element", placeholder: "请选择提交元素"},
        {id: "browser-auto-click-element-picker", placeholder: "选择元素后加入顺序"},
      ].forEach(({id, placeholder}) => {
        const select = document.querySelector(`#${id}`);
        if (!select) return;
        const previousValue = aliasChanges[select.value] || select.value;
        select.innerHTML = `<option value="">${placeholder}</option>${options}`;
        if (aliases.includes(previousValue)) select.value = previousValue;
      });
    }
```

Replace the selector-building block at the start of `refreshBrowserAutoElementOptions` with:

```javascript
      syncBrowserAutoElementOptions();
```

Keep brand-option rebuilding and `renderBrowserAutoStrategyOptions()` in the existing full refresh function.

- [ ] **Step 4: Synchronize every webpage-element mutation without full form rerender**

At the start of `saveBrowserElementFromDialog`, preserve the edited alias:

```javascript
      const previousAlias = editingBrowserElementAlias;
```

After updating `browserActionConfig.elements`, replace `renderBrowserAutoStrategyOptions()` with:

```javascript
      const aliasChanges = previousAlias && previousAlias !== alias
        ? {[previousAlias]: alias}
        : {};
      syncBrowserAutoElementOptions(aliasChanges);
```

After `browserActionConfig.elements[alias.trim()] = xpath.trim();` in `addBrowserElement`, add:

```javascript
      syncBrowserAutoElementOptions();
```

Inside the webpage-element delete handler, before `renderBrowserActionEditor()`, keep automatic strategies valid and refresh selectors:

```javascript
        browserAutoStrategies.forEach((strategy) => {
          strategy.click_elements = (strategy.click_elements || []).filter((item) => item !== alias);
          if (strategy.input_element === alias) strategy.input_element = "";
          if (strategy.submit_element === alias) strategy.submit_element = "";
        });
        syncBrowserAutoElementOptions();
```

After `browserActionConfig` is replaced following a successful `saveBrowserActionConfig`, add:

```javascript
          syncBrowserAutoElementOptions();
```

- [ ] **Step 5: Run the focused test and verify GREEN**

Run:

```powershell
node --test tests-js/browser-auto-element-options.test.js
```

Expected: 3 tests PASS.

- [ ] **Step 6: Run related regression tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_console.py -q
```

Expected: all tests PASS with no new warnings or errors.

- [ ] **Step 7: Run complete Python and Node test suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
npm run test:node
```

Expected: both commands exit with code 0.

- [ ] **Step 8: Commit the focused change when Git metadata is available**

```powershell
git add gateway/app.py tests-js/browser-auto-element-options.test.js docs/superpowers/specs/2026-07-21-browser-auto-element-selection-design.md docs/superpowers/plans/2026-07-21-browser-auto-element-selection.md
git commit -m "fix: sync strategy element selectors"
```

Expected: one commit containing only this feature. If this workspace still reports `fatal: not a git repository`, do not initialize or alter Git metadata; report that the commit was skipped.
