# Selector Unsafe Self-Healing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unsafe Role fallbacks, attribute `selector_unsafe` to one element, trigger existing three-attempt self-healing, and show the real Dry-Run failure in the UI.

**Architecture:** Candidate generation removes only Role locators whose accessible names fail the current contract. Validator remains fail-closed but attaches the current alias to safety failures. Healing runtime maps that failure into the existing `wrong_semantics` repair path; UI derives its message from recorded Dry-Run evidence.

**Tech Stack:** Python 3.13, pytest, Node.js built-in test runner, existing Flask selector-probe modules.

## Global Constraints

- No new API, database field, Redis key, dependency, or probe action.
- Do not weaken `_selector_safe` or persist raw selectors, DOM fragments, Profile IDs, or page content.
- Keep two-Profile/two-round validation, atomic publication, affected-strategy-only pause, and previous-stable-version behavior unchanged.
- Preserve all unrelated dirty-worktree changes. Stage only task-specific hunks; if `.git` remains read-only, report the commit blocker and do not alter repository metadata.

---

### Task 1: Remove contract-incompatible Role fallbacks

**Files:**
- Modify: `selector_probe/candidates.py:640-656`
- Test: `tests/test_selector_probe_candidates.py:83-102`

**Interfaces:**
- Consumes: `_name_matches(contract: ElementContract, actual: str) -> bool`
- Preserves: `generate_candidates(...) -> list[dict]`
- Produces: stable attribute/XPath candidates without an incompatible Role candidate

- [ ] **Step 1: Add the failing regression test**

Add beside the historical-anchor tests:

```python
def test_historical_anchor_does_not_emit_incompatible_role_fallback():
    contract = tuple(default_tiktok_contracts().values())[0]

    candidates = generate_candidates(
        contract,
        snapshot(
            node(
                name="37 comments",
                attributes={"data-e2e": "comment-icon"},
            )
        ),
        TIKTOK_COMMENT_TEMPLATE[contract.alias],
    )

    assert any(
        item["type"] == "attribute"
        and item["name"] == "data-e2e"
        and item["value"] == "comment-icon"
        for item in candidates
    )
    assert all(item["type"] != "role" for item in candidates)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_candidates.py::test_historical_anchor_does_not_emit_incompatible_role_fallback -q -p no:cacheprovider
```

Expected: FAIL because a Role locator named `37 comments` is present.

- [ ] **Step 3: Apply the one-condition generator fix**

In `generate_candidates`, change Role generation to:

```python
role_name = _node_name(semantic_node)
if role_name and _name_matches(contract, role_name):
    scored.append(
        _ScoredCandidate(
            ROLE_SCORE,
            {
                "type": "role",
                "role": semantic_node.role.casefold(),
                "name": role_name,
                "name_mode": (
                    contract.name_mode
                    if contract.name_mode in {"exact", "contains"}
                    else "exact"
                ),
                "enabled": True,
            },
        )
    )
```

Do not change attribute, parent constraint, CSS, or XPath generation.

- [ ] **Step 4: Run candidate tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_candidates.py -q -p no:cacheprovider
```

Expected: all candidate tests pass.

- [ ] **Step 5: Commit only Task 1 files when Git is writable**

```powershell
git add -- selector_probe/candidates.py tests/test_selector_probe_candidates.py
git diff --cached --check
git commit -m "fix(probe): drop unsafe role fallbacks"
```

---

### Task 2: Attribute and self-heal selector safety failures

**Files:**
- Modify: `selector_probe/validator.py:456-480`
- Modify: `selector_probe/healing_runtime.py:46-68,866-922`
- Test: `tests/test_selector_probe_validator.py:1070-1097`
- Test: `tests/test_selector_probe_healing_runtime.py:717-813`

**Interfaces:**
- Consumes: `ValidationRejected(code, alias=..., required_state=...)`
- Preserves: `_candidate_ids(...) -> dict[str, set[str]]`
- Produces: `_validation_failure(...)` result with `failure_class="selector"`, one failed alias, and repair-facing `code="wrong_semantics"`

- [ ] **Step 1: Extend validator regression with alias attribution**

In `test_unsafe_css_or_xpath_is_rejected_before_page_access`, add:

```python
assert caught.value.alias == ALIAS
```

- [ ] **Step 2: Add healing classification regression**

Add to `tests/test_selector_probe_healing_runtime.py`:

```python
def test_runtime_routes_selector_unsafe_to_affected_alias_repair():
    runtime = HealingRuntime.__new__(HealingRuntime)
    runtime.contracts = {
        "评论入口": SimpleNamespace(required_state="feed_ready")
    }

    failure = runtime._validation_failure(
        ValidationRejected(
            "selector_unsafe",
            alias="评论入口",
            required_state="feed_ready",
        )
    )

    assert failure == {
        "status": "failed",
        "failure_class": "selector",
        "failed_aliases": ["评论入口"],
        "code": "wrong_semantics",
        "match_count": 0,
        "required_state": "feed_ready",
    }
```

- [ ] **Step 3: Run both tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_validator.py::test_unsafe_css_or_xpath_is_rejected_before_page_access tests/test_selector_probe_healing_runtime.py::test_runtime_routes_selector_unsafe_to_affected_alias_repair -q -p no:cacheprovider
```

Expected: validator alias is empty; healing classifies the error as infrastructure.

- [ ] **Step 4: Preserve alias in fail-closed validation**

In `_candidate_ids`, wrap only `_selector_safe`:

```python
try:
    _selector_safe(candidate, contracts[alias])
except ValidationRejected as error:
    raise ValidationRejected(error.code, alias=alias) from None
```

Do not include candidate values in the exception.

- [ ] **Step 5: Map `selector_unsafe` into existing repair semantics**

Add `selector_unsafe` to `_SELECTOR_FAILURE_CODES`.

Update repair-code selection:

```python
repair_code = (
    error.code
    if error.code in {
        "zero_match",
        "multiple_match",
        "postcondition_failed",
    }
    else (
        "wrong_semantics"
        if error.code == "selector_unsafe"
        or error.code.startswith("semantic_")
        else "zero_match"
    )
)
```

This uses the existing `repair.FAILURE_CODES` value `wrong_semantics`; do not add a new repair failure code.

- [ ] **Step 6: Run backend probe regression suites**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_validator.py tests/test_selector_probe_healing_runtime.py tests/test_selector_probe_observe.py -q -p no:cacheprovider
```

Expected: all tests pass; no infrastructure classification for `selector_unsafe`.

- [ ] **Step 7: Commit only Task 2 hunks when Git is writable**

`selector_probe/healing_runtime.py` already contains unrelated worktree changes. Stage only this task's hunks, then inspect the staged diff:

```powershell
git add -- selector_probe/validator.py tests/test_selector_probe_validator.py tests/test_selector_probe_healing_runtime.py
git add -p -- selector_probe/healing_runtime.py
git diff --cached --check
git diff --cached -- selector_probe/healing_runtime.py
git commit -m "fix(probe): self-heal unsafe selectors"
```

---

### Task 3: Show Dry-Run safety failure instead of no elements

**Files:**
- Modify: `gateway/static/selector_probe_ui.js:927-1052`
- Test: `tests-js/selector-probe-operations.test.js:329-378`

**Interfaces:**
- Consumes: sanitized stage evidence `{name, status, failure_code, profile_mask}`
- Preserves: `buildRunPresentation(raw) -> presentation`
- Produces: element-stage result and overall failure reason describing `selector_unsafe`

- [ ] **Step 1: Add the failing UI regression**

Add to `tests-js/selector-probe-operations.test.js`:

```javascript
test("unsafe Dry-Run is not presented as no discovered element", () => {
  const presentation = buildRunPresentation({
    id: "run-unsafe",
    status: "selector_validation_failed",
    failed_aliases: ["评论入口"],
    failure_code: "selector_unsafe",
    stages: [
      {name: "candidate_filter", status: "passed", profile_mask: "***3A7F"},
      {
        name: "element_dry_run",
        status: "failed",
        failure_code: "selector_unsafe",
        profile_mask: "***3A7F",
      },
    ],
  });

  assert.match(presentation.stages[2].result, /未通过安全规则/);
  assert.doesNotMatch(presentation.stages[2].result, /尚未发现可用元素/);
  assert.match(presentation.failure.reason, /未通过安全规则/);
});
```

- [ ] **Step 2: Run the UI test and verify RED**

Run:

```powershell
node --test tests-js/selector-probe-operations.test.js
```

Expected: new assertion fails because element-stage result says `尚未发现可用元素` and the failure label is generic.

- [ ] **Step 3: Add the bounded failure label**

Add to `FAILURE_REASON_LABELS`:

```javascript
selector_unsafe: "已发现候选路径，但路径未通过安全规则",
```

- [ ] **Step 4: Derive the element-stage result from Dry-Run evidence**

Keep raw stage evidence before converting it to lifecycle values:

```javascript
const elementStageEvidence = stageSignals(run, [
  "a11y_snapshot", "candidate_filter", "element_dry_run",
  "comment_panel_transition", "comment_panel_cleanup", "validate",
  "full_validation",
]);
const elementSignals = elementStageEvidence
  .map((stage) => operationLifecycle(stage))
  .concat(/* existing element lifecycle mapping */);
const failedElementStage = elementStageEvidence.find(
  (stage) => operationLifecycle(stage) === "failed",
);
```

Set the element-stage result in this order:

```javascript
result: run.repairs.length
  ? `已触发自愈 ${run.repairs.length} 次`
  : failedElementStage?.failure_code
    ? FAILURE_REASON_LABELS[failedElementStage.failure_code]
      || "候选路径未通过 Dry-Run"
    : run.elements.length
      ? `未触发自愈；已记录 ${run.elements.length} 个元素结果`
      : "未触发自愈；尚未发现可用元素",
```

Do not render raw selector values or stage summaries.

- [ ] **Step 5: Run UI regression suites**

Run:

```powershell
node --test tests-js/selector-probe-operations.test.js
npm.cmd run test:node
```

Expected: focused operations tests and all Node tests pass.

- [ ] **Step 6: Commit only Task 3 hunks when Git is writable**

Both files already contain unrelated worktree changes. Stage only new hunks and inspect them:

```powershell
git add -p -- gateway/static/selector_probe_ui.js tests-js/selector-probe-operations.test.js
git diff --cached --check
git diff --cached -- gateway/static/selector_probe_ui.js tests-js/selector-probe-operations.test.js
git commit -m "fix(ui): explain unsafe selector Dry-Run"
```

---

### Task 4: Final focused verification

**Files:**
- Verify only; no planned source changes

**Interfaces:**
- Consumes: completed Tasks 1-3
- Produces: evidence that generation, validation, self-healing, and UI presentation agree

- [ ] **Step 1: Run complete selector-probe backend tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_candidates.py tests/test_selector_probe_validator.py tests/test_selector_probe_healing_runtime.py tests/test_selector_probe_observe.py tests/test_selector_probe_worker.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 2: Run complete frontend tests**

```powershell
npm.cmd run test:node
```

Expected: all Node tests pass.

- [ ] **Step 3: Verify source and diff integrity**

```powershell
.\.venv\Scripts\python.exe -m py_compile selector_probe/candidates.py selector_probe/validator.py selector_probe/healing_runtime.py
node --check gateway/static/selector_probe_ui.js
git diff --check
```

Expected: all commands exit `0`; no whitespace error.

- [ ] **Step 4: Perform one real probe after restarting the current launcher**

Expected evidence:

- `candidate_filter: passed`
- no contract-incompatible Role fallback
- if a candidate remains unsafe: failed alias is present and repair attempts are recorded
- UI never reports `尚未发现可用元素` when candidate filtering passed
- browser pages close only after success or terminal failure

Do not publish or resume affected strategies unless the existing two-Profile/two-round and atomic-publication gates pass.
