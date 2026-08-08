# Browser Execution V2 Elements Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use subagent-driven development task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let users capture an exact element from one AdsPower page, save multiple verified locators, resolve it strictly at runtime, and wait for a saved readiness element without semantic guessing.

**Architecture:** Extend only the isolated `execution_v2` package. Element definitions are immutable revisions in SQLite. A locator resolver accepts a small explicit locator schema and either returns one exact actionable node or a typed failure; it never calls `first()`, guesses by position, or heals. The picker injects one overlay into the bound Playwright page and reports the original composed-path node plus its nearest actionable ancestor.

**Tech Stack:** Python asyncio, Playwright public Locator/ElementHandle APIs, SQLite, browser JavaScript, pytest.

## Global Constraints

- No A11y-tree discovery, LLM, probe, Redis, semantic contract, positional matching, or automatic healing.
- Locators are ordered: stable `data-*`, ARIA/role-name, stable name/placeholder/id, unique CSS, constrained text, relative XPath.
- Each saved locator must match exactly one visible node; all passing locators must resolve to the same node.
- Input elements must be editable; click elements need a non-zero bounding box.
- A picker session can save multiple elements and closes the AdsPower window only when the user finishes or cancels the session.
- Readiness samples every 500 ms and requires three consecutive visible, stable measurements.
- Do not modify V1 or selector-probe files.

---

### Task 1: Element definitions, revisions, and CRUD

**Files:**
- Create: `execution_v2/elements.py`
- Modify: `execution_v2/store.py`
- Test: `tests/test_execution_v2_elements.py`
- Test: `tests/test_execution_v2_store.py`

**Interfaces:**
- `normalize_element_definition(value) -> dict`
- `ExecutionStore.create_element(...)`, `get_element(id)`, `list_elements()`, `rename_element(...)`, `repick_element(...)`, `set_element_status(...)`, `delete_element(...)`.

- [ ] Write tests first for exact keys, purpose/kind/status enums, HTTPS URL pattern, non-empty frame path/locator list, locator type schema, rename without revision change, repick with revision increment, persistence, and delete rejection when a strategy action references the element.

```python
definition = {
    "url_pattern": "https://www.tiktok.com/*",
    "frame_path": [],
    "locators": [
        {"type": "css", "value": "[data-e2e='comment-icon']", "priority": 10},
        {"type": "xpath", "value": "//button[@aria-label='Open comments']", "priority": 60},
    ],
    "diagnostic_metadata": {"tag": "button", "text": ""},
    "screenshot_path": "artifacts/picker/session-1/comment.png",
}
```

- [ ] Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_elements.py tests/test_execution_v2_store.py -q -p no:cacheprovider`

Expected first run: FAIL because CRUD/normalizer do not exist.

- [ ] Implement strict normalization and transactional CRUD. `repick_element()` inserts `(element_id, revision)` into `element_revisions` and updates `elements` in one transaction. `delete_element()` parses saved `strategy_actions.action_json` and raises `ElementInUseError` when any action references the ID.

- [ ] Re-run the same tests; expected PASS.

---

### Task 2: Strict locator resolution and readiness

**Files:**
- Create: `execution_v2/locator.py`
- Create: `execution_v2/readiness.py`
- Test: `tests/test_execution_v2_locator.py`
- Test: `tests/test_execution_v2_readiness.py`

**Interfaces:**
- `StrictLocatorResolver.resolve(page, definition, *, require_editable=False) -> ResolvedElement`
- `wait_until_ready(page, definition, resolver, *, timeout_seconds, sample_interval=0.5, required_stable_samples=3, sleep=asyncio.sleep)`.

- [ ] Write failing resolver tests for match count 0, match count >1, invisible, zero box, non-editable input, two locators pointing to the same handle, two locators conflicting, iframe traversal, and the explicit prohibition on `first()`.

```python
resolved = await resolver.resolve(page, definition)
assert resolved.locator_type == "css"
assert resolved.bounding_box == {"x": 10, "y": 20, "width": 100, "height": 40}
```

- [ ] Write failing readiness tests using a fake clock: unstable boxes reset the counter; exactly three identical visible samples pass; timeout raises `ReadinessTimeout`; no real sleep occurs.

- [ ] Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_locator.py tests/test_execution_v2_readiness.py -q -p no:cacheprovider`

Expected first run: FAIL because both modules do not exist.

- [ ] Implement locator creation only with public APIs: `frame.locator(css)`, `frame.locator('xpath=...')`, and `frame.get_by_role(role, name=..., exact=True)`. Call `count()` before `element_handle()`. Compare passing handles with `frame.evaluate('(pair) => pair[0] === pair[1]', [left, right])`. Never use `.first`, `nth`, coordinates, fuzzy text, or similarity.

- [ ] Implement readiness as a deadline loop around the resolver and exact `(x, y, width, height)` measurements. Resolver failures reset stability but are retained as the final diagnostic.

- [ ] Re-run focused tests; expected PASS.

---

### Task 3: Continuous picker session and locator generation

**Files:**
- Create: `execution_v2/picker.py`
- Create: `execution_v2/picker_overlay.js`
- Create: `execution_v2/THIRD_PARTY_NOTICES.md`
- Modify: `package.json`
- Modify: `package-lock.json`
- Test: `tests/test_execution_v2_picker.py`
- Test: `tests-js/execution-v2-picker.test.js`

**Interfaces:**
- `PickerService.start(binding, target_url) -> PickerSession`
- `PickerSession.next_selection()`, `save_selection(name, purpose, kind)`, `finish()`, `cancel()`.
- Browser event payload contains `tag`, safe attributes, role/name, text preview, frame path, original-node fingerprint, actionable-ancestor fingerprint, and bounding box; it never contains cookies, storage, passwords, or page HTML.

- [ ] Add the locked dependency `@cypress/unique-selector@2.2.0`. Preserve the project lockfile format.

- [ ] Write failing JS tests for one installed overlay, hover highlight, Escape cancellation, `event.composedPath()`, original SVG versus actionable button ancestor, and listener cleanup.

- [ ] Write failing Python tests for multiple saves in one session, no auto-close between saves, generated candidate priority, validation through `StrictLocatorResolver`, failure when no locator passes, finish/cancel cleanup, and sanitized selection payloads.

- [ ] Implement the smallest overlay and queue bridge using Playwright `expose_binding()` plus `add_init_script()`/`evaluate()`. Generate stable attribute, ARIA/role-name, stable id/name/placeholder, unique CSS, constrained text, and relative XPath candidates; save only candidates that resolve uniquely to the confirmed handle.

- [ ] Record MIT attribution for `trembacz/xpath-finder` interaction/relative-XPath reference and the installed Cypress package in `THIRD_PARTY_NOTICES.md`; do not copy unrelated source files.

- [ ] Run: `.\.venv\Scripts\python.exe -m pytest tests/test_execution_v2_picker.py tests/test_execution_v2_locator.py -q -p no:cacheprovider`

- [ ] Run: `node --test tests-js/execution-v2-picker.test.js`

Expected: both PASS.

---

## Phase 2 Completion Gate

- Element create/rename/repick/disable/delete and revision history persist across restart.
- Referenced elements cannot be deleted.
- Locator count 0, count >1, invisible, non-actionable, and conflicting candidates all fail explicitly.
- No `.first()`, positional guess, semantic search, or healing exists.
- Readiness passes only after three stable 500 ms samples.
- One picker session saves multiple named elements without closing the browser between selections.
- No existing dirty file is modified except package manifests required for the locked open-source selector dependency.
