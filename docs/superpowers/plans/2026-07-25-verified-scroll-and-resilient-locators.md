# Verified Video Scrolling and Resilient Locators Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make scroll counts represent verified TikTok video switches and replace brittle single-XPath aliases with persisted, scoped, ordered semantic locators.

**Architecture:** Introduce three focused modules: a pure element-schema/migration module, an async scoped-locator resolver, and a verified video-switch engine. Existing strategy actions continue to reference aliases and retain their persisted parameter shape; the gateway and UI expose canonical definitions, diagnostics, and video-switch terminology without changing unrelated strategy, recording, or launcher behavior.

**Tech Stack:** Python 3, Flask, Playwright async API over AdsPower CDP, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- Do not initialize or use Git; the workspace is intentionally not a Git repository.
- Use TDD for every production change: observe the focused RED before writing the minimal GREEN.
- Existing saved `total_count` values remain numerically unchanged; they acquire video-switch semantics without automatic conversion.
- Existing raw XPath text is preserved exactly as an advanced fallback during migration.
- Settings writes continue through the existing locked, atomic persistence path and backup mechanism.
- Click, keyboard input, and submit are never blindly retried after a possibly effective dispatch.
- A wheel event counts only when a different active video is observed and stable.
- The internal wheel delta remains `-120` upward and `+120` downward.
- Synthetic Playwright input does not claim to move the Windows hardware pointer.
- Do not start, stop, navigate, click, type in, or scroll a real AdsPower profile without explicit live-test authorization.
- Public diagnostics must not contain page HTML, cookies, credentials, generated comment content, captions, account names, or raw video identifiers.

---

### Task 1: Locator Schema Version 3 and Lossless Migration

**Files:**
- Create: `browser_element_schema.py`
- Modify: `browser_strategy_config.py:122-135`
- Modify: `browser_strategy_config.py:510-535`
- Modify: `gateway/app.py:5310-5375`
- Test: `tests/test_browser_element_schema.py`
- Test: `tests/test_browser_strategy_config.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Produces: `normalize_element_definitions(value: object) -> dict[str, dict]`.
- Produces: `migrate_element_definition(alias: str, value: object) -> dict`.
- Produces: `TIKTOK_COMMENT_TEMPLATE: dict[str, dict]`.
- Preserves: `browser_strategy_config.normalize_elements(...)` as a compatibility wrapper.

- [ ] **Step 1: Add failing schema and migration tests**

```python
from browser_element_schema import (
    TIKTOK_COMMENT_TEMPLATE,
    normalize_element_definitions,
)


def test_legacy_xpath_migrates_losslessly_and_idempotently():
    legacy = {"评论入口": "//article[@id='one-column-item-1']//button"}
    migrated = normalize_element_definitions(legacy)
    assert migrated["评论入口"]["scope"] == "page"
    assert migrated["评论入口"]["locators"][0]["type"] == "xpath"
    assert migrated["评论入口"]["locators"][0]["value"] == legacy["评论入口"]
    assert migrated["评论入口"]["locators"][0]["fallback"] is True
    assert normalize_element_definitions(migrated) == migrated


def test_tiktok_template_uses_scopes_and_semantic_primary_locators():
    assert TIKTOK_COMMENT_TEMPLATE["评论入口"]["scope"] == "active_video"
    assert TIKTOK_COMMENT_TEMPLATE["评论入口"]["locators"][0] == {
        "id": "tiktok-comment-entry-primary",
        "type": "attribute",
        "name": "data-e2e",
        "value": "comment-icon",
        "enabled": True,
    }
    assert TIKTOK_COMMENT_TEMPLATE["评论输入框"]["scope"] == "visible_comment_panel"
    assert TIKTOK_COMMENT_TEMPLATE["评论提交按钮"]["scope"] == "visible_comment_panel"
```

- [ ] **Step 2: Run the schema tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_element_schema.py -p no:cacheprovider -q
```

Expected: collection fails because `browser_element_schema` does not exist.

- [ ] **Step 3: Implement the pure canonical schema**

Create `browser_element_schema.py` with these exact public constants and functions:

```python
from __future__ import annotations

import copy
import hashlib
from typing import Any

ELEMENT_SCOPES = {"page", "active_video", "visible_comment_panel"}
LOCATOR_TYPES = {"attribute", "role", "css", "xpath"}


def _stable_locator_id(alias: str, selector: str) -> str:
    digest = hashlib.sha256(f"{alias}\0{selector}".encode("utf-8")).hexdigest()[:16]
    return f"locator-{digest}"


def migrate_element_definition(alias: str, value: object) -> dict:
    if isinstance(value, str):
        selector = value.strip()
        if not selector:
            raise ValueError(f"element selector must not be empty: {alias}")
        return {
            "scope": "page",
            "locators": [{
                "id": _stable_locator_id(alias, selector),
                "type": "xpath",
                "value": selector,
                "enabled": True,
                "fallback": True,
            }],
        }
    if not isinstance(value, dict):
        raise ValueError(f"element definition must be an object: {alias}")
    return copy.deepcopy(value)


def normalize_element_definitions(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        raise ValueError("elements must be a JSON object")
    normalized = {}
    for raw_alias, raw_definition in value.items():
        alias = str(raw_alias or "").strip()
        if not alias or alias in normalized:
            raise ValueError("element aliases must be non-empty and unique")
        definition = migrate_element_definition(alias, raw_definition)
        scope = str(definition.get("scope") or "").strip()
        if scope not in ELEMENT_SCOPES:
            raise ValueError(f"unsupported element scope: {scope}")
        raw_locators = definition.get("locators")
        if not isinstance(raw_locators, list) or not raw_locators:
            raise ValueError(f"element locators must be a non-empty list: {alias}")
        locators = [_normalize_locator(alias, item) for item in raw_locators]
        if len({item["id"] for item in locators}) != len(locators):
            raise ValueError(f"locator IDs must be unique: {alias}")
        if not any(item["enabled"] for item in locators):
            raise ValueError(f"element needs one enabled locator: {alias}")
        normalized[alias] = {"scope": scope, "locators": locators}
    return normalized
```

Implement `_normalize_locator(...)` with exact-key validation for:

```python
attribute = {"id", "type", "name", "value", "enabled", "fallback", "descendant"}
role = {"id", "type", "role", "name", "name_mode", "enabled", "fallback"}
css_or_xpath = {"id", "type", "value", "enabled", "fallback"}
```

Reject unknown keys, executable JavaScript, empty selectors, unsupported scopes/types, duplicate IDs, and a role `name_mode` outside `{"exact", "contains"}`.

- [ ] **Step 4: Define the TikTok template without user XPath**

Add the canonical template:

```python
TIKTOK_COMMENT_TEMPLATE = {
    "评论入口": {
        "scope": "active_video",
        "locators": [{
            "id": "tiktok-comment-entry-primary",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-icon",
            "enabled": True,
        }],
    },
    "评论输入框": {
        "scope": "visible_comment_panel",
        "locators": [{
            "id": "tiktok-comment-input-primary",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-input",
            "enabled": True,
            "descendant": {
                "type": "attribute",
                "name": "contenteditable",
                "value": "true",
                "role": "textbox",
            },
        }],
    },
    "评论提交按钮": {
        "scope": "visible_comment_panel",
        "locators": [
            {
                "id": "tiktok-comment-submit-primary",
                "type": "css",
                "value": "button[data-e2e=\"comment-post\"]",
                "enabled": True,
            },
            {
                "id": "tiktok-comment-submit-role",
                "type": "role",
                "role": "button",
                "name": "Post",
                "name_mode": "exact",
                "enabled": True,
                "fallback": True,
            },
        ],
    },
}
```

- [ ] **Step 5: Delegate existing normalization and advance schema version**

Make `browser_strategy_config.normalize_elements` call
`normalize_element_definitions`. Change successful canonical writes in
`gateway/app.py` from schema version `2` to `3`. Keep action aliases unchanged.

- [ ] **Step 6: Add route-level persistence and rename regression tests**

```python
def test_element_put_persists_locator_definition_and_renames_strategy_reference(client):
    definition = {
        "scope": "active_video",
        "locators": [{
            "id": "comment-primary",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-icon",
            "enabled": True,
        }],
    }
    response = client.put(
        "/api/browser/elements",
        json={"elements": {"新评论入口": definition}, "rename_from": "评论入口"},
    )
    assert response.status_code == 200
    assert response.get_json()["elements"]["新评论入口"] == definition
    assert client.get("/api/browser/elements").get_json()["elements"]["新评论入口"] == definition
    strategy = client.get("/api/browser/strategies").get_json()["strategies"][0]
    assert strategy["actions"][0]["params"]["element"] == "新评论入口"
```

- [ ] **Step 7: Run focused schema, route, and compatibility tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_element_schema.py tests/test_browser_strategy_config.py tests/test_app.py -k "element or migrat or strategy_reference" -p no:cacheprovider -q -W error
```

Expected: all selected tests pass; old string fixtures remain readable through canonical migration.

- [ ] **Step 8: Reviewer checkpoint**

Record RED/GREEN evidence and obtain an independent review of schema validation,
lossless migration, rename behavior, and persistence. Do not proceed with an
unfixed Critical or Important finding.

---

### Task 2: Scoped Locator Resolver and Read-Only Inspection

**Files:**
- Create: `browser_element_resolver.py`
- Test: `tests/test_browser_element_resolver.py`

**Interfaces:**
- Consumes: canonical definitions from `normalize_element_definitions`.
- Produces: `ResolvedElement(locator, alias, scope, candidate, diagnostics)`.
- Produces: `resolve_element(page, alias, definition) -> ResolvedElement`.
- Produces: `inspect_element(page, alias, definition) -> dict`.
- Produces: `LocatorResolutionError` with safe structured diagnostics.

- [ ] **Step 1: Write failing scope and ambiguity tests**

Use a deterministic fake Playwright page containing:

```html
<div id="column-list-container">
  <article id="one-column-item-4" data-e2e="recommend-list-item-container">
    <div data-e2e="comment-icon" role="button"></div>
  </article>
  <article id="one-column-item-5" data-e2e="recommend-list-item-container">
    <div data-e2e="comment-icon" role="button"></div>
  </article>
</div>
<section data-comment-panel hidden></section>
<section data-comment-panel>
  <div data-e2e="comment-input">
    <div contenteditable="true" role="textbox"></div>
  </div>
  <button data-e2e="comment-post">Post</button>
</section>
```

Tests:

```python
async def test_active_video_scope_uses_center_article():
    result = await resolve_element(page, "评论入口", entry_definition)
    assert await result.locator.get_attribute("data-e2e") == "comment-icon"
    assert result.diagnostics["scope_target"] == "one-column-item-5"


async def test_page_scope_rejects_ambiguous_candidate():
    with pytest.raises(LocatorResolutionError) as caught:
        await resolve_element(page, "评论入口", page_scoped_entry)
    assert caught.value.code == "element_candidate_ambiguous"
    assert caught.value.diagnostics["candidates"][0]["raw_count"] == 2


async def test_inspection_does_not_focus_scroll_click_or_type():
    before = await page.evaluate("document.documentElement.outerHTML")
    result = await inspect_element(page, "评论输入框", input_definition)
    after = await page.evaluate("document.documentElement.outerHTML")
    assert result["status"] == "ok"
    assert before == after
```

- [ ] **Step 2: Run resolver tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_element_resolver.py -p no:cacheprovider -q
```

Expected: collection fails because the resolver module does not exist.

- [ ] **Step 3: Implement result and error contracts**

```python
from dataclasses import dataclass
from typing import Any


@dataclass
class ResolvedElement:
    locator: Any
    alias: str
    scope: str
    candidate: dict
    diagnostics: dict


class LocatorResolutionError(RuntimeError):
    def __init__(self, code: str, alias: str, scope: str, diagnostics: dict):
        self.code = code
        self.alias = alias
        self.scope = scope
        self.diagnostics = diagnostics
        super().__init__(f"{code}: {alias} ({scope})")
```

- [ ] **Step 4: Implement scope resolution**

Implement:

```python
async def resolve_scope(page, scope: str):
    if scope == "page":
        return page, {"scope_target": "page"}
    if scope == "active_video":
        return await _resolve_active_video_scope(page)
    if scope == "visible_comment_panel":
        return await _resolve_visible_comment_panel_scope(page)
    raise LocatorResolutionError("element_scope_not_found", "", scope, {})
```

`_resolve_active_video_scope` evaluates visible
`article[data-e2e="recommend-list-item-container"]` rectangles against the
center line of `#column-list-container`, requires one best intersection, and
returns an ID-scoped locator. `_resolve_visible_comment_panel_scope` starts from
exactly one visible `[data-e2e="comment-input"]`, walks to its nearest comment
`section`, and rejects hidden/exiting duplicates.

- [ ] **Step 5: Implement ordered candidate resolution**

Create locators by type:

```python
def build_candidate_locator(scope_locator, candidate):
    kind = candidate["type"]
    if kind == "css":
        return scope_locator.locator(candidate["value"])
    if kind == "xpath":
        return scope_locator.locator(f"xpath={candidate['value']}")
    if kind == "attribute":
        locator = scope_locator.locator(
            f'[{candidate["name"]}="{candidate["value"]}"]'
        )
        return apply_descendant(locator, candidate.get("descendant"))
    if kind == "role":
        return role_locator(scope_locator, candidate)
    raise ValueError(f"unsupported locator type: {kind}")
```

For each enabled candidate, calculate raw, visible, and actionable counts
without scrolling or focusing. Accept exactly one visible/actionable target.
Return safe diagnostics without HTML or text content.

- [ ] **Step 6: Verify fallback and re-render behavior**

Add tests where the semantic candidate is absent and XPath succeeds, then replace
the target DOM node and prove a second `resolve_element` returns the new node.
Add a mutation check proving `.first()` would fail the ambiguity test.

- [ ] **Step 7: Run the complete resolver suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_element_resolver.py -p no:cacheprovider -q -W error
```

Expected: all resolver tests pass without page mutation.

- [ ] **Step 8: Reviewer checkpoint**

Review geometry scoping, visibility/actionability checks, safe diagnostics,
fallback ordering, and the no-mutation guarantee.

---

### Task 3: Locator Inspection API and TikTok Template API

**Files:**
- Modify: `gateway/app.py:5115-5150`
- Modify: `gateway/app.py:5310-5375`
- Test: `tests/test_app.py`
- Test: `tests/test_browser_routes.py`

**Interfaces:**
- Consumes: `inspect_element` and `TIKTOK_COMMENT_TEMPLATE`.
- Produces: `POST /api/browser/elements/test`.
- Produces: `GET /api/browser/elements/templates/tiktok-comment`.

- [ ] **Step 1: Add failing read-only route tests**

```python
def test_element_test_route_inspects_each_profile_without_actions(client, monkeypatch):
    calls = []

    async def fake_inspect(page, alias, definition):
        calls.append((page.profile_id, alias))
        return {
            "alias": alias,
            "scope": definition["scope"],
            "status": "ok",
            "candidate_id": definition["locators"][0]["id"],
            "raw_count": 1,
            "visible_count": 1,
            "actionable_count": 1,
        }

    monkeypatch.setattr("gateway.app.inspect_element", fake_inspect)
    response = client.post(
        "/api/browser/elements/test",
        json={"windows": ["profile-1", "profile-2"], "elements": test_elements},
    )
    assert response.status_code == 200
    assert len(response.get_json()["results"]) == 2
    assert calls == [("profile-1", "评论入口"), ("profile-2", "评论入口")]


def test_tiktok_template_route_returns_a_copy(client):
    first = client.get("/api/browser/elements/templates/tiktok-comment").get_json()
    first["elements"]["评论入口"]["scope"] = "page"
    second = client.get("/api/browser/elements/templates/tiktok-comment").get_json()
    assert second["elements"]["评论入口"]["scope"] == "active_video"
```

- [ ] **Step 2: Run route tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_browser_routes.py -k "element_test or tiktok_template" -p no:cacheprovider -q
```

Expected: 404 because the routes do not exist.

- [ ] **Step 3: Implement template GET route**

```python
@app.get("/api/browser/elements/templates/tiktok-comment")
def get_tiktok_comment_element_template():
    return jsonify({"elements": copy.deepcopy(TIKTOK_COMMENT_TEMPLATE)})
```

- [ ] **Step 4: Replace the old XPath reader with canonical inspection**

Add `/api/browser/elements/test` that:

- normalizes the submitted draft definitions;
- acquires the selected sessions without navigating;
- connects once per profile;
- chooses the current active page;
- calls `inspect_element` for every alias;
- releases Playwright and the existing session lease in `finally`;
- returns per-profile, per-alias safe diagnostics;
- never calls click, wheel, focus, keyboard, or `scroll_into_view_if_needed`.

Keep `/api/browser/read-elements` as a compatibility route that delegates to the
new inspection path after legacy-string migration.

- [ ] **Step 5: Add sanitization and failure-isolation assertions**

One profile returns valid results; the other raises
`LocatorResolutionError`. Assert both appear independently and logs contain no
raw selector value, HTML, cookie, or CDP URL.

- [ ] **Step 6: Run gateway tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_browser_routes.py -k "element or locator or browser_log" -p no:cacheprovider -q -W error
```

Expected: all selected tests pass.

- [ ] **Step 7: Reviewer checkpoint**

Review read-only behavior, lease cleanup, safe public data, template copy
semantics, and legacy route compatibility.

---

### Task 4: Element Manager UI for Scopes and Ordered Locators

**Files:**
- Modify: `gateway/app.py:1900-2040`
- Modify: `gateway/static/browser_strategy_ui.js:20-180`
- Modify: `gateway/static/browser_strategy_ui.js:680-900`
- Test: `tests-js/browser-strategy-ui.test.js`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: canonical element definitions and the two new APIs from Task 3.
- Produces: locator-draft manipulation methods in `BrowserStrategyUI`.

- [ ] **Step 1: Add failing UI state tests**

```javascript
test("element draft supports scope and ordered locator candidates", () => {
  const ui = createUI();
  ui.openElementDialog({
    alias: "评论入口",
    definition: {
      scope: "active_video",
      locators: [
        {id: "primary", type: "attribute", name: "data-e2e", value: "comment-icon", enabled: true},
        {id: "fallback", type: "xpath", value: "//button", enabled: true, fallback: true},
      ],
    },
    originalAlias: "评论入口",
  });
  ui.moveElementLocator("fallback", -1);
  assert.deepEqual(ui.state.elementDialog.draft.definition.locators.map((item) => item.id), ["fallback", "primary"]);
});

test("template application mutates only the draft until explicit save", async () => {
  const ui = createUI({template: canonicalTemplate});
  await ui.applyTikTokCommentTemplate();
  assert.equal(ui.state.elements["评论入口"], undefined);
  assert.equal(ui.state.elementDialog.draft.definition.scope, "active_video");
});

test("scroll labels describe verified video switches", () => {
  const fields = createUI().parameterFields("scroll_down");
  assert.deepEqual(fields.map((field) => field.label), [
    "最少切换视频数",
    "最多切换视频数",
    "最小切换间隔秒数",
    "最大切换间隔秒数",
  ]);
});
```

- [ ] **Step 2: Run Node tests and verify RED**

```powershell
node --test tests-js/browser-strategy-ui.test.js
```

Expected: missing locator draft methods and old scroll labels.

- [ ] **Step 3: Implement locator draft operations**

Add and export:

```javascript
function addElementLocator(candidate) { /* append deep copy with stable id */ }
function updateElementLocator(locatorId, patch) { /* replace only matching item */ }
function moveElementLocator(locatorId, offset) { /* bounded reorder */ }
function removeElementLocator(locatorId) { /* retain at least one candidate */ }
function setElementScope(scope) { /* page | active_video | visible_comment_panel */ }
async function testElementDraft(windows) { /* POST read-only draft */ }
async function applyTikTokCommentTemplate(alias) { /* GET and copy into draft */ }
```

Every operation updates only `state.elementDialog.draft`; `state.elements`
changes only after a successful PUT response.

- [ ] **Step 4: Replace the single XPath form**

Render:

- alias input;
- scope select;
- ordered candidate cards;
- type-specific fields;
- enabled checkbox;
- add/reorder/remove controls;
- advanced XPath label;
- test button and per-window result table;
- template button with confirmation.

Use text nodes for all API error and diagnostic strings.

- [ ] **Step 5: Update serialization and compatibility**

`saveElements` sends canonical definitions. `openElementDialog` accepts either a
canonical definition or a legacy string and normalizes the latter into a local
XPath fallback draft without altering server state.

- [ ] **Step 6: Update scroll labels without converting values**

Keep:

```javascript
total_count: [positiveInteger("total_count.0"), positiveInteger("total_count.1")]
```

Change only labels and validation copy from “滚轮次数” to “切换视频数”.
Retain existing numeric values and hidden `burst_count`.

- [ ] **Step 7: Run UI and HTML contract tests**

```powershell
node --test tests-js/browser-strategy-ui.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -k "browser_strategy_ui or element_dialog or scroll_label" -p no:cacheprovider -q -W error
```

Expected: all selected tests pass.

- [ ] **Step 8: Reviewer checkpoint**

Review draft isolation, explicit template confirmation, accessible controls,
safe rendering, canonical persistence, and unchanged numeric values.

---

### Task 5: State-Aware Element Actions and Postconditions

**Files:**
- Modify: `browser_actions.py:101-195`
- Modify: `browser_strategy_runtime.py:330-420`
- Modify: `browser_page_lifecycle.py`
- Test: `tests/test_browser_actions.py`
- Test: `tests/test_browser_strategy_runtime.py`
- Test: `tests/test_browser_page_lifecycle.py`

**Interfaces:**
- Consumes: `resolve_element(...)`.
- Produces: action results containing safe locator diagnostics.
- Preserves: click/input non-retry semantics and ordered page recovery.

- [ ] **Step 1: Add failing click/input resolution tests**

```python
async def test_click_resolves_alias_inside_active_video_once(monkeypatch):
    resolved = fake_resolved_element(alias="评论入口")
    monkeypatch.setattr(browser_actions, "resolve_element", AsyncMock(return_value=resolved))
    result = await execute_action(page, click_action, canonical_elements, {}, resolver)
    assert page.mouse.click_calls == 1
    assert result["locator"]["candidate_id"] == resolved.candidate["id"]


async def test_fallback_does_not_repeat_a_dispatched_click(monkeypatch):
    resolved.locator.bounding_box.return_value = target_box
    page.mouse.click.side_effect = RuntimeError("page replaced after dispatch")
    with pytest.raises(RuntimeError):
        await execute_action(page, click_action, canonical_elements, {}, resolver)
    assert page.mouse.click_calls == 1


async def test_keyboard_targets_actual_editable_descendant():
    result = await execute_action(page, input_action, canonical_elements, {}, text_resolver)
    assert result["status"] == "ok"
    assert editable.focus_calls == 1
    assert wrapper.focus_calls == 0
```

- [ ] **Step 2: Run action tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_actions.py -k "resolve or editable or fallback" -p no:cacheprovider -q
```

Expected: failures because actions still pass strings directly to
`page.locator`.

- [ ] **Step 3: Replace direct selector lookup**

For `move`, `click`, and `keyboard_input`, replace:

```python
selector = elements.get(element)
field = page.locator(selector)
```

with:

```python
definition = elements.get(element)
if definition is None:
    raise LocatorResolutionError("element_alias_missing", element, "", {})
resolved = await resolve_element(page, element, definition)
field = resolved.locator
```

Return only:

```python
"locator": {
    "scope": resolved.scope,
    "candidate_id": resolved.candidate["id"],
    "candidate_type": resolved.candidate["type"],
}
```

Do not return raw CSS/XPath values.

- [ ] **Step 4: Add state-aware comment-entry postcondition**

After one comment-entry click, wait up to 5 seconds for either:

```css
[data-e2e="comment-input"]:visible
```

or a successfully resolved `visible_comment_panel` scope. If the page is
replaced, use the existing lifecycle rebind and continue observation without
dispatching the click again. Raise `element_postcondition_not_observed` if no
panel appears.

- [ ] **Step 5: Preserve input reflection and add submit non-duplication**

Keep reflected-text verification. For submit, dispatch exactly once and record
`postcondition="not_configured"` until a stable site signal is defined; never
use fallback resolution after dispatch.

- [ ] **Step 6: Propagate locator errors through staged runtime**

Attach safe locator diagnostics to `BlockExecutionError` and
`StrategyRuntimeError`. Preserve ordered page recovery and the real
`execute_actions` stage.

- [ ] **Step 7: Run action/lifecycle/runtime regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_actions.py tests/test_browser_page_lifecycle.py tests/test_browser_strategy_runtime.py -p no:cacheprovider -q -W error
```

Expected: all tests pass; click/input invocation-count assertions remain one.

- [ ] **Step 8: Reviewer checkpoint**

Review stage-aware validation, postcondition observation, absence of blind
retries, safe diagnostics, and recovery ordering.

---

### Task 6: Verified Video-Switch Engine

**Files:**
- Create: `browser_video_switch.py`
- Modify: `browser_actions.py:196-275`
- Test: `tests/test_browser_video_switch.py`
- Test: `tests/test_browser_actions.py`

**Interfaces:**
- Produces: `execute_verified_switches(page, *, direction, requested, interval_range, lifecycle, rng, sleep_fn) -> dict`.
- Produces: `VideoSwitchError` with partial safe measurements.
- Consumes: existing page lifecycle replacement resolver.

- [ ] **Step 1: Add failing exact-switch tests**

```python
async def test_one_completed_count_requires_observed_video_change():
    page = FakeFeedPage(pulses_per_switch=8)
    result = await execute_verified_switches(
        page,
        direction="down",
        requested=1,
        interval_range=[0, 0],
        lifecycle=None,
        rng=FixedRng(),
        sleep_fn=no_sleep,
    )
    assert result["requested_switches"] == 1
    assert result["completed_switches"] == 1
    assert result["wheel_events"] == 8


async def test_ignored_wheel_pulses_do_not_increment_switch_count():
    page = FakeFeedPage(ignore_first=4, pulses_per_switch=8)
    result = await execute_verified_switches(..., requested=2)
    assert result["completed_switches"] == 2
    assert result["wheel_events"] == 20


async def test_switch_limit_fails_with_partial_measurements():
    page = FakeFeedPage(never_switch=True, container_height=945)
    with pytest.raises(VideoSwitchError) as caught:
        await execute_verified_switches(..., requested=3)
    assert caught.value.code == "video_switch_not_observed"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 12
```

- [ ] **Step 2: Run engine tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_video_switch.py -p no:cacheprovider -q
```

Expected: collection fails because `browser_video_switch` does not exist.

- [ ] **Step 3: Implement safe feed-state capture**

```python
@dataclass(frozen=True)
class FeedState:
    fingerprint: str
    safe_fingerprint: str
    container_x: float
    container_y: float
    container_width: float
    container_height: float
    scroll_top: float
```

`capture_feed_state(page)` evaluates `#column-list-container`, finds the feed
article crossing its center, and extracts identity in this order:

1. `/video/<digits>` ID from a descendant link;
2. article `id`;
3. deterministic stable attributes plus visible feed index.

Hash the internal identity with SHA-256 and expose only the first 12 hex
characters as `safe_fingerprint`.

- [ ] **Step 4: Implement one verified switch**

```python
async def switch_once(page, direction, *, sleep_fn):
    before = await capture_feed_state(page)
    await page.mouse.move(
        before.container_x + before.container_width / 2,
        before.container_y + before.container_height / 2,
    )
    max_pulses = min(24, max(4, math.ceil(before.container_height / 120) + 4))
    for pulse in range(1, max_pulses + 1):
        await page.mouse.wheel(0, 120 if direction == "down" else -120)
        after = await wait_for_stable_changed_state(page, before, timeout=5.0)
        if after is not None:
            return before, after, pulse
    raise VideoSwitchError(
        "video_switch_not_observed",
        completed_switches=0,
        wheel_events=max_pulses,
    )
```

`wait_for_stable_changed_state` requires a different fingerprint at the center
and two consecutive polls with scroll-position difference at most 2 CSS pixels.

- [ ] **Step 5: Implement exact N loop and intervals**

Sample `requested` once in `browser_actions.execute_action`, then call the
engine. Increment only after `switch_once` returns. Apply
`rng.uniform(*interval_range)` only between completed switches, never after the
last.

Result:

```python
{
    "count": completed,
    "distance": 120,
    "requested_switches": requested,
    "completed_switches": completed,
    "wheel_events": wheel_events,
    "switches": safe_switch_records,
}
```

- [ ] **Step 6: Integrate one-time page recovery**

If wheel or state capture reports a closed target:

- append the existing ordered recovery event;
- resolve one replacement page;
- restart only the pending switch;
- do not increment completed count;
- do not repeat previously verified switches.

Attach partial switch measurements and recovery events to any final error.

- [ ] **Step 7: Add upward, interval, stability, and mutation tests**

Prove:

- upward uses `-120`;
- requested range sampled once;
- interval occurs `N - 1` times;
- a transient changed fingerprint that is not stable is not counted;
- removing fingerprint verification makes a test fail;
- page replacement does not double-count.

- [ ] **Step 8: Run engine and action tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_video_switch.py tests/test_browser_actions.py tests/test_browser_page_lifecycle.py -p no:cacheprovider -q -W error
```

Expected: all selected tests pass with exact switch and wheel-event totals.

- [ ] **Step 9: Reviewer checkpoint**

Review scroll-container targeting, active-card identity, exact count semantics,
pulse bounds, stability polling, interval placement, page recovery, and safe
fingerprints.

---

### Task 7: Gateway Results, Failure Isolation, and Combined Strategy Flow

**Files:**
- Modify: `browser_strategy_runtime.py`
- Modify: `gateway/app.py:4200-4280`
- Modify: `gateway/app.py:5148-5260`
- Test: `tests/test_browser_strategy_runtime.py`
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: safe locator and video-switch measurements.
- Produces: per-profile public action results and staged errors.
- Preserves: per-profile execution reservation and ordinary/batch close policy.

- [ ] **Step 1: Add failing combined-flow test**

```python
def test_combined_strategy_reports_verified_switches_and_scoped_locator(client, monkeypatch):
    runner_result = {
        "actions": [
            {
                "action_id": "scroll-1",
                "type": "scroll_down",
                "status": "ok",
                "requested_switches": 3,
                "completed_switches": 3,
                "wheel_events": 23,
                "switches": [{"from": "a1b2", "to": "c3d4", "wheel_events": 8}],
            },
            {
                "action_id": "click-1",
                "type": "click",
                "status": "ok",
                "element": "评论入口",
                "locator": {
                    "scope": "active_video",
                    "candidate_id": "tiktok-comment-entry-primary",
                    "candidate_type": "attribute",
                },
            },
        ],
    }
    monkeypatch.setattr("gateway.app.run_prepared_block_strategy_on_cdp", lambda **_: runner_result)
    response = client.post("/api/browser/execute-strategy", json=request_payload)
    assert response.status_code == 200
    actions = response.get_json()["results"][0]["actions"]
    assert actions[0]["completed_switches"] == 3
    assert actions[0]["wheel_events"] == 23
    assert "selector" not in str(actions[1]).casefold()
```

- [ ] **Step 2: Run gateway/runtime tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_browser_strategy_runtime.py -k "verified_switch or scoped_locator" -p no:cacheprovider -q
```

Expected: result fields are not yet retained end-to-end.

- [ ] **Step 3: Preserve safe action measurements**

Ensure `run_block_strategy` and `run_prepared_block_strategy_on_cdp` retain:

- switch requested/completed/wheel totals;
- masked switch records;
- locator scope/candidate ID/type;
- ordered page recovery;
- partial measurements on staged failure.

Do not retain raw selector text or full fingerprints.

- [ ] **Step 4: Verify per-profile isolation and reservations**

Add two-profile tests where:

- profile 1 completes three switches and comments;
- profile 2 fails `video_switch_not_observed`;
- profile 1 remains `ok`;
- same-profile normal/batch overlap remains `execution_busy`;
- different profiles may run concurrently;
- reservations release after locator and switch errors.

- [ ] **Step 5: Verify public sanitization and logs**

Inspect response and JSONL logs. Assert absence of:

```text
xpath=
css=
outerHTML
contenteditable text
comment content
/video/<raw id>
cookie
authorization
devtools/browser
```

Assert presence of safe action ID, alias, scope, counts, failure code, and retry
number.

- [ ] **Step 6: Run full gateway/runtime suite**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_strategy_runtime.py tests/test_browser_routes.py tests/test_app.py -p no:cacheprovider -q -W error
```

Expected: all tests pass with no warning filters.

- [ ] **Step 7: Reviewer checkpoint**

Review cross-module contracts, partial-result propagation, public redaction,
failure isolation, reservation release, and close-policy compatibility.

---

### Task 8: Canonical Persistence, Restart, and UI Integration Verification

**Files:**
- Modify: `tests/test_settings_store.py`
- Modify: `tests/test_app.py`
- Modify: `tests-js/browser-strategy-ui.test.js`
- Create: `docs/superpowers/reports/2026-07-25-verified-scroll-and-locators-verification.md`

**Interfaces:**
- Consumes: completed schema, UI, resolver, actions, and gateway.
- Produces: automated acceptance evidence.

- [ ] **Step 1: Add persistence round-trip test**

```python
def test_locator_order_scope_and_scroll_range_survive_reload(tmp_path, monkeypatch):
    save_settings({
        "browser": {
            "strategy_schema_version": 3,
            "action_elements": canonical_elements,
            "block_strategies": [{
                "id": "comment-flow",
                "name": "comment flow",
                "run_mode": "once",
                "batch_size": 2,
                "actions": [scroll_action(total_count=[30, 50])],
            }],
        },
    })
    reloaded = load_settings()["browser"]
    assert reloaded["action_elements"] == canonical_elements
    assert reloaded["block_strategies"][0]["actions"][0]["params"]["total_count"] == [30, 50]
```

- [ ] **Step 2: Run persistence test and verify its initial state**

Run the exact test before any compatibility fix. If it fails, record the
settings key/order/value that changed. If it already passes, add a restart
subprocess probe that imports `load_settings` in a fresh process and asserts the
same canonical JSON.

- [ ] **Step 3: Add full UI round-trip test**

The Node test must:

- load canonical elements;
- edit scope and reorder candidates;
- save and consume canonical server response;
- refresh UI state;
- retain locator order and scope;
- retain `[30, 50]` without conversion;
- keep strategy action aliases unchanged.

- [ ] **Step 4: Run focused persistence and UI tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_settings_store.py tests/test_browser_strategy_config.py tests/test_app.py -k "persist or reload or restart or migrate" -p no:cacheprovider -q -W error
node --test tests-js/browser-strategy-ui.test.js
```

Expected: all selected tests pass.

- [ ] **Step 5: Run the supported full Python and Node suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
npm.cmd run test:node
.\.venv\Scripts\python.exe -m py_compile browser_element_schema.py browser_element_resolver.py browser_video_switch.py browser_actions.py browser_strategy_config.py browser_strategy_runtime.py browser_page_lifecycle.py gateway\app.py
```

Expected: zero failures and compilation exit code 0. If root discovery remains
blocked by the pre-existing inaccessible generated directory, do not delete it;
record the boundary and use the supported `tests` root shown above.

- [ ] **Step 6: Inspect the dashboard locally**

Use the already running local application or normal hidden launcher only when
safe. Confirm:

- locator editor fields render;
- no placeholder controls;
- template confirmation works;
- per-window inspection table is readable;
- scroll labels say video switches;
- save/refresh restores canonical definitions.

Do not execute real page actions during this UI inspection.

- [ ] **Step 7: Write the verification report**

Record exact commands, pass counts, schema migration evidence, persistence
round-trip, UI inspection, any external boundary, and `Commits: none` in:

`docs/superpowers/reports/2026-07-25-verified-scroll-and-locators-verification.md`

- [ ] **Step 8: Final independent code review**

Review the complete reconstructed filesystem diff against both approved specs.
Fix every Critical and Important finding, rerun focused/full verification, and
obtain a final Ready verdict before reporting completion.

---

### Task 9: Explicitly Authorized Live AdsPower Acceptance

**Files:**
- Modify: `docs/superpowers/reports/2026-07-25-verified-scroll-and-locators-verification.md`

**Interfaces:**
- Consumes: two explicitly authorized disposable/test profiles.
- Produces: live acceptance evidence only; no production-code changes.

- [ ] **Step 1: Check authorization boundary**

Proceed only when the user explicitly identifies two disposable/test profile
IDs. Otherwise mark this task `Pending` and perform no live action.

- [ ] **Step 2: Test locator drafts read-only**

Apply the TikTok template to a draft and run the read-only locator inspection in
both profiles. Confirm active-video comment entry resolves independently.

- [ ] **Step 3: Verify exactly three downward switches**

Set `[3, 3]`, run only the downward switch action, and record:

- three distinct masked active-video transitions;
- `completed_switches == 3`;
- actual `wheel_events`;
- no false count on ignored pulses.

- [ ] **Step 4: Verify exactly three upward switches**

From the non-initial feed position, set `[3, 3]` upward and record the same
evidence.

- [ ] **Step 5: Verify comment flow**

Run:

```text
verified video switches
→ scoped comment entry
→ scoped editable input
→ keyboard input
→ scoped submit
```

Confirm both profiles resolve their own current DOM and neither dispatches a
duplicate click or submit.

- [ ] **Step 6: Verify persistence**

Refresh the dashboard and restart the application through the normal launcher.
Confirm locator definitions, ordering, scopes, template choices, strategy
actions, and switch range remain saved.

- [ ] **Step 7: Update report honestly**

Mask profile IDs except their final four characters. Record requested/completed
switches, wheel-event totals, candidate IDs/types, recovery events, and any
limitation. Never claim live success for an action that was not observed.

