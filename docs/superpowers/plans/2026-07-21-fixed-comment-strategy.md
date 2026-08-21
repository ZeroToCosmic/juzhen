# Fixed Comment Strategy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace automatic strategy ordered clicks with one fixed entry → input → submit comment workflow.

**Architecture:** `browser_strategy_runtime.py` owns canonical normalization, legacy migration, validation, and execution order. `gateway/app.py` uses that contract at API and UI boundaries; the dashboard exposes one selector per semantic role and removes the ordered-click builder.

**Tech Stack:** Python 3, Flask, browser JavaScript, pytest, Node `node:test`.

## Global Constraints

- Fixed execution order is exactly entry click → verified text input → submit click.
- `entry_element`, `input_element`, and `submit_element` are required, distinct aliases from saved webpage elements.
- New canonical strategies do not persist `click_elements` or `ordered_click_elements`.
- Legacy strategies are migrated only by the deterministic rules in the approved design; ambiguous strategies remain incomplete for manual correction.
- Manual action strategies, scrolling, copy selection, batching, AdsPower, and navigation behavior must not change.
- Use TDD and make only request-related changes.

---

### Task 1: Canonical model, migration, validation, and runtime order

**Files:**
- Modify: `browser_strategy_runtime.py:12-105, 240-285`
- Modify: `gateway/app.py:4269-4287, 4619-4626, 5868-5882`
- Modify: `tests/test_browser_strategy_runtime.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_settings_routes.py:838-860`

**Interfaces:**
- Produces: `normalize_auto_strategy(strategy) -> dict` with canonical `entry_element`, `input_element`, and `submit_element`, with legacy click-list keys removed.
- Produces: `validate_auto_strategy_elements(strategy, elements) -> dict`, returning the normalized strategy or raising one of the approved Chinese validation errors.
- Consumes in Task 2: canonical strategy fields and validation errors.

- [ ] **Step 1: Add failing normalization and validation tests**

Add focused tests to `tests/test_browser_strategy_runtime.py`:

```python
def test_auto_strategy_migrates_exact_legacy_three_element_flow():
    strategy = normalize_auto_strategy(
        {"click_elements": ["entry", "input", "submit"]}
    )

    assert strategy["entry_element"] == "entry"
    assert strategy["input_element"] == "input"
    assert strategy["submit_element"] == "submit"
    assert "click_elements" not in strategy
    assert "ordered_click_elements" not in strategy


def test_auto_strategy_migrates_entry_around_explicit_input_and_submit():
    strategy = normalize_auto_strategy(
        {
            "click_elements": ["input", "entry", "submit"],
            "input_element": "input",
            "submit_element": "submit",
        }
    )

    assert strategy["entry_element"] == "entry"
    assert strategy["input_element"] == "input"
    assert strategy["submit_element"] == "submit"
    assert "click_elements" not in strategy


def test_auto_strategy_does_not_guess_ambiguous_legacy_flow():
    strategy = normalize_auto_strategy({"click_elements": ["one", "two"]})

    assert strategy["entry_element"] == ""
    assert strategy["input_element"] == ""
    assert strategy["submit_element"] == ""


@pytest.mark.parametrize(
    ("strategy", "message"),
    [
        ({"entry_element": "", "input_element": "input", "submit_element": "submit"},
         "自动策略配置校验失败：必须配置入口、输入和提交元素"),
        ({"entry_element": "same", "input_element": "same", "submit_element": "submit"},
         "自动策略配置校验失败：入口、输入和提交元素不能重复"),
        ({"entry_element": "entry", "input_element": "input", "submit_element": "missing"},
         "自动策略引用了未配置元素：missing"),
    ],
)
def test_auto_strategy_validates_fixed_elements(strategy, message):
    with pytest.raises(ValueError, match=message):
        validate_auto_strategy_elements(
            strategy,
            {"entry": "//entry", "input": "//input", "submit": "//submit", "same": "//same"},
        )
```

Import `validate_auto_strategy_elements` beside the existing runtime imports.

- [ ] **Step 2: Run normalization tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py -q -p no:cacheprovider
```

Expected: new tests FAIL because `entry_element` and `validate_auto_strategy_elements` do not exist and legacy keys remain.

- [ ] **Step 3: Implement canonical normalization and validation**

In `DEFAULT_AUTO_STRATEGY`, replace `"click_elements": []` with:

```python
    "entry_element": "",
```

In `normalize_auto_strategy`, read legacy values from the original `strategy`, not the defaults, and canonicalize:

```python
    legacy_clicks = strategy.get("click_elements", strategy.get("ordered_click_elements", []))
    if legacy_clicks is None:
        legacy_clicks = []
    if not isinstance(legacy_clicks, list):
        raise ValueError("点击元素必须是有序数组")
    legacy_clicks = [str(item).strip() for item in legacy_clicks if str(item).strip()]

    entry_alias = str(strategy.get("entry_element") or "").strip()
    input_alias = str(strategy.get("input_element") or "").strip()
    submit_alias = str(strategy.get("submit_element") or "").strip()
    if not entry_alias and input_alias and submit_alias:
        entry_candidates = list(dict.fromkeys(
            alias for alias in legacy_clicks if alias not in {input_alias, submit_alias}
        ))
        if len(entry_candidates) == 1:
            entry_alias = entry_candidates[0]
    elif not entry_alias and not input_alias and not submit_alias and len(legacy_clicks) == 3:
        entry_alias, input_alias, submit_alias = legacy_clicks

    result["entry_element"] = entry_alias
    result["input_element"] = input_alias
    result["submit_element"] = submit_alias
    result.pop("click_elements", None)
    result.pop("ordered_click_elements", None)
```

Add:

```python
def validate_auto_strategy_elements(
    strategy: dict[str, Any], elements: dict[str, str]
) -> dict[str, Any]:
    normalized = normalize_auto_strategy(strategy)
    aliases = [
        normalized["entry_element"],
        normalized["input_element"],
        normalized["submit_element"],
    ]
    if not all(aliases):
        raise ValueError("自动策略配置校验失败：必须配置入口、输入和提交元素")
    if len(set(aliases)) != 3:
        raise ValueError("自动策略配置校验失败：入口、输入和提交元素不能重复")
    missing = [alias for alias in aliases if alias not in elements]
    if missing:
        raise ValueError(f"自动策略引用了未配置元素：{', '.join(missing)}")
    return normalized
```

Add `validate_auto_strategy_elements` to the module's `__all__` export list.

- [ ] **Step 4: Add failing fixed-order runtime tests**

Update the primary runtime test fixture to use elements `entry`, `input`, and `submit`, and add assertions showing `page.clicked == ["entry", "input", "submit"]`. Add failure tests asserting an invisible entry prevents typing/submission and an input failure prevents submission. Use the existing `_FakePage` and visibility/failure controls; do not add mocks for runtime functions.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py -q -p no:cacheprovider
```

Expected: fixed-order assertions FAIL while `_interaction` still reads `click_elements`.

- [ ] **Step 5: Implement the fixed runtime sequence**

At the start of `_interaction`, require three aliases, then execute:

```python
    entry_alias = strategy.get("entry_element")
    input_alias = strategy.get("input_element")
    submit_alias = strategy.get("submit_element")
    _click(page, elements, entry_alias, clicked, stage="评论入口校验")
    comment_options = _comments(comments)
    if not comment_options:
        raise ValueError("文案选择校验失败：内容管理中没有可用文案")
    text = rng.choice(comment_options)
    _type_and_verify(page, elements, input_alias, text, rng, sleep_fn)
    _click(page, elements, submit_alias, clicked, stage="提交点击校验")
```

Validate at `run_auto_strategy` entry:

```python
    normalized = validate_auto_strategy_elements(strategy, elements)
```

Remove `click_elements` iteration and missing-element construction. Keep the existing loop, scrolling, timing, and return schema intact.

- [ ] **Step 6: Add failing API canonicalization tests**

In `tests/test_app.py`, save webpage elements `entry`, `input`, and `submit`, then assert PUT `/api/browser/auto-strategies`:

- accepts the three canonical fields;
- returns and persists `entry_element`;
- omits `click_elements`;
- rejects missing, duplicate, and unknown aliases with the exact validation messages.

Update `tests/test_settings_routes.py` auto-strategy preservation fixture to include those three action elements and three strategy fields.

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_settings_routes.py -q -p no:cacheprovider
```

Expected: new validation tests FAIL because the save route normalizes without checking webpage elements.

- [ ] **Step 7: Use canonical validation at API and execution boundaries**

Replace `validate_auto_strategy_definition` internals with:

```python
    from browser_actions import validate_action_config
    from browser_strategy_runtime import validate_auto_strategy_elements

    normalized_elements, _actions = validate_action_config(elements, [])
    validate_auto_strategy_elements(strategy, normalized_elements)
    return normalized_elements
```

In `save_auto_strategies_route`, load and normalize current action elements once, then validate every submitted strategy:

```python
            from browser_actions import validate_action_config

            elements = load_settings().get("browser", {}).get("action_elements", {})
            elements, _actions = validate_action_config(elements, [])
            strategies = [
                validate_auto_strategy_elements(item, elements)
                for item in raw
            ]
```

Keep duplicate-ID validation and `merge_saved_settings` unchanged.

- [ ] **Step 8: Update existing automatic-strategy fixtures**

For every automatic runtime/API fixture found by:

```powershell
rg -n '"click_elements"' tests/test_browser_strategy_runtime.py tests/test_app.py tests/test_settings_routes.py
```

replace canonical usage with `"entry_element": "entry"`, add `"entry": "//button[@data-entry]"` to its webpage-element map, and preserve the fixture's existing input/submit aliases. Keep only the explicit legacy-migration tests using `click_elements`.

- [ ] **Step 9: Verify Task 1**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py tests/test_app.py tests/test_settings_routes.py -q -p no:cacheprovider
```

Expected: all selected tests PASS.

- [ ] **Step 10: Commit when Git metadata is available**

```powershell
git add browser_strategy_runtime.py gateway/app.py tests/test_browser_strategy_runtime.py tests/test_app.py tests/test_settings_routes.py
git commit -m "refactor: fix comment strategy sequence"
```

If the workspace still reports `fatal: not a git repository`, do not initialize or repair Git; record that the commit was skipped.

---

### Task 2: Replace the ordered-click UI with an entry selector

**Files:**
- Modify: `gateway/app.py:1215-1248, 2057-2102, 3438-3740, 3960-3990`
- Modify: `tests/test_console.py:660-690`
- Modify: `tests-js/browser-auto-element-options.test.js`

**Interfaces:**
- Consumes: Task 1 canonical fields `entry_element`, `input_element`, `submit_element`.
- Produces: dashboard form submission containing only those three role fields; `syncBrowserAutoElementOptions(aliasChanges = {})` updates their selectors without touching unrelated unsaved fields.

- [ ] **Step 1: Write failing dashboard structure tests**

Update `tests/test_console.py` to assert:

```python
    assert 'id="browser-auto-entry-element"' in page
    assert 'id="browser-auto-input-element"' in page
    assert 'id="browser-auto-submit-element"' in page
    assert 'id="browser-auto-click-element-picker"' not in page
    assert 'id="browser-auto-click-order-list"' not in page
    assert 'id="browser-auto-click-order"' not in page
    assert "有序点击元素" not in page
```

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_console.py -q -p no:cacheprovider
```

Expected: FAIL because the old ordered-click builder still exists and entry selector is absent.

- [ ] **Step 2: Replace Step 6 markup and remove unused styles**

Replace Step 6 content with:

```html
              <section class="browser-auto-step" data-auto-step="6">
                <div class="browser-auto-step-header">
                  <span class="browser-auto-step-index">6</span>
                  <div class="browser-auto-step-title">
                    <strong>评论入口</strong>
                    <span class="muted">选择打开评论输入区域的网页元素。</span>
                  </div>
                </div>
                <div class="browser-auto-step-fields">
                  <label>入口元素<select id="browser-auto-entry-element"></select></label>
                </div>
              </section>
```

Remove CSS rules used only by `.browser-auto-click-builder`, `.browser-auto-click-order-list`, and `.browser-auto-click-row`.

- [ ] **Step 3: Write failing selector synchronization behavior tests**

Update `tests-js/browser-auto-element-options.test.js` fake DOM and assertions so the only synchronized IDs are:

```javascript
[
  "browser-auto-entry-element",
  "browser-auto-input-element",
  "browser-auto-submit-element",
]
```

Test add/preserve, rename mapping, delete clearing, unrelated-field preservation, and prototype-key aliases for all three role selectors. Remove ordered-click hidden-field tests because that UI no longer exists.

Run:

```powershell
node --test tests-js/browser-auto-element-options.test.js
```

Expected: FAIL because production JavaScript still synchronizes the ordered-click picker instead of entry.

- [ ] **Step 4: Replace ordered-click JavaScript with canonical role fields**

Make these exact changes in `gateway/app.py`:

- Delete `readBrowserAutoClickOrder`, `setBrowserAutoClickOrder`, `renderBrowserAutoClickOrderList`, `addBrowserAutoClickElement`, `moveBrowserAutoClickElement`, and `removeBrowserAutoClickElement`.
- Delete click-builder event listeners.
- In `renderBrowserAutoStrategyOptions`, set `browser-auto-entry-element` from `strategy.entry_element` and retain input/submit assignments.
- In `autoStrategyFromForm`, return `entry_element`, `input_element`, and `submit_element`; remove `click_elements`.
- In `addBrowserAutoStrategy`, initialize all three fields to `""`.
- In element rename/delete paths, update/clear `strategy.entry_element`, `strategy.input_element`, and `strategy.submit_element` only.
- In `syncBrowserAutoElementOptions`, use these descriptors:

```javascript
      [
        {id: "browser-auto-entry-element", placeholder: "请选择入口元素"},
        {id: "browser-auto-input-element", placeholder: "请选择输入元素"},
        {id: "browser-auto-submit-element", placeholder: "请选择提交元素"},
      ]
```

- [ ] **Step 5: Add client-side save validation**

Before mutating `browserAutoStrategies` in `saveBrowserAutoStrategies`, validate the selected form object:

```javascript
      const aliases = [selected.entry_element, selected.input_element, selected.submit_element];
      if (!aliases.every(Boolean)) {
        status.textContent = "必须配置入口、输入和提交元素";
        return;
      }
      if (new Set(aliases).size !== 3) {
        status.textContent = "入口、输入和提交元素不能重复";
        return;
      }
```

Keep server validation authoritative; this check only gives immediate feedback.

- [ ] **Step 6: Verify Task 2**

Run:

```powershell
node --test tests-js/browser-auto-element-options.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_console.py tests/test_app.py -q -p no:cacheprovider
```

Expected: all selected tests PASS.

- [ ] **Step 7: Run complete regression suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
npm.cmd run test:node
```

Expected: both commands exit with code 0 and no failures.

- [ ] **Step 8: Commit when Git metadata is available**

```powershell
git add gateway/app.py tests/test_console.py tests-js/browser-auto-element-options.test.js docs/superpowers/specs/2026-07-21-fixed-comment-strategy-design.md docs/superpowers/plans/2026-07-21-fixed-comment-strategy.md
git commit -m "feat: simplify comment strategy form"
```

If Git metadata remains unavailable, do not initialize or alter it; record that the commit was skipped.
