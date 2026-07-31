# Selector Profile Bulk Save Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Save two or more manually entered or currently selected AdsPower test Profile IDs in one atomic selector-probe settings update.

**Architecture:** Keep raw staged IDs inside the existing UI controller closure. Reuse the existing `profile_changes.add` list accepted by the settings API; add no endpoint or persistence model.

**Tech Stack:** Vanilla JavaScript, Flask-rendered HTML, Node test runner

## Global Constraints

- No new API, database table, module split, or unrelated refactor.
- Saved Profile IDs remain masked in public UI state.
- Existing authorization, reason, confirmation, audit, revision, and atomic-write rules remain active.
- This workspace has no Git repository, so commit steps are omitted.

---

### Task 1: Bulk stage and atomically save test Profiles

**Files:**
- Modify: `gateway/app.py`
- Modify: `gateway/static/selector_probe_ui.js`
- Test: `tests-js/selector-probe-settings.test.js`

**Interfaces:**
- Consumes: checked `.adspower-select` elements and existing `PATCH /api/selector-probe/settings`.
- Produces: `profile_changes: {add: string[]}` with trimmed unique IDs.

- [ ] **Step 1: Add failing tests**

Test pure parsing and Origin normalization:

```javascript
assert.deepEqual(parseProfileIds(" first \nsecond\nfirst "), ["first", "second"]);
assert.equal(normalizeTargetOrigin("https://www.tiktok.com/"), "https://www.tiktok.com");
```

Test controller submission:

```javascript
ui.stageProfileAdds(["manual-a", "selected-b", "manual-a"]);
ui.confirmSettingsSave(candidate, "add dedicated profiles", {});
await ui.submitSettingsSave();
assert.deepEqual(patch.body.profile_changes.add, ["manual-a", "selected-b"]);
```

Test failed requests retain staged IDs for retry.

- [ ] **Step 2: Verify tests fail**

Run:

```powershell
node --test tests-js/selector-probe-settings.test.js
```

Expected: new helper/controller assertions fail because bulk staging is absent.

- [ ] **Step 3: Add minimal UI**

Replace the single input with:

```html
<textarea name="profileAdd" autocomplete="off"
  placeholder="每行一个 Profile ID"></textarea>
<button id="selector-settings-profile-stage" type="button">加入暂存列表</button>
<button id="selector-settings-profile-import" type="button">导入当前已选 Profiles</button>
<div id="selector-settings-profile-staged"></div>
```

- [ ] **Step 4: Implement bulk staging and submission**

Add helpers:

```javascript
function parseProfileIds(value) {
  return [...new Set(String(value || "").split(/\r?\n/)
    .map((item) => item.trim()).filter(Boolean))];
}

function normalizeTargetOrigin(value) {
  const parsed = new URL(String(value || "").trim());
  return parsed.origin;
}
```

Keep staged IDs in a closure array, merge manual and selected IDs, render only
masked suffixes, remove by index, and submit:

```javascript
if (pendingProfileAdds.length) {
  body.profile_changes = {add: pendingProfileAdds.slice()};
}
```

Clear staged IDs only after a successful `200` response or controller cleanup.
Supply selected IDs from:

```javascript
Array.from(document.querySelectorAll(".adspower-select:checked"))
  .map((node) => node.dataset.profileId);
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
node --test tests-js/selector-probe-settings.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_management_routes.py -q -p no:cacheprovider
```

Expected: both commands exit `0`.

- [ ] **Step 6: Run syntax and regression checks**

Run:

```powershell
node --check gateway/static/selector_probe_ui.js
.\.venv\Scripts\python.exe -m pytest tests/test_auth_routes.py tests/test_selector_probe_routes.py -q -p no:cacheprovider
```

Expected: all checks exit `0`.
