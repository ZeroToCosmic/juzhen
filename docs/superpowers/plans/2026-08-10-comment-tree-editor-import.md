# Comment Tree Editor and Excel Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ID-heavy Comment Campaign template form with a dark, human-readable comment-tree editor and add safe two-stage Excel import.

**Architecture:** Keep `CommentTemplate` as the only persisted tree aggregate. Add a focused workbook parser and two import service/API operations, then move tree draft behavior into a small browser-side module consumed by the existing workbench. Preserve all current template, revision, Campaign snapshot, authentication, CSRF, local-direct, and executor contracts.

**Tech Stack:** Flask, Pydantic 2, SQLAlchemy, openpyxl, plain JavaScript, Node test runner, CSS, pytest.

## Global Constraints

- Do not add React, shadcn, pnpm, a frontend build step, or a second tree persistence model.
- UI colors: background `#09090B`, card `#18181B`, border `#27272A`; never use `#000000` or saturated primary colors.
- Use Inter, SF Pro, or system-ui; 1px subtle borders; 12px/16px radii; 8pt spacing; 300ms subtle hover transitions.
- Keep forms single-column; expose parent selection only when advanced branching is enabled.
- Never render editable template IDs, step IDs, parent IDs, content library IDs, or content item IDs.
- Preserve existing IDs while editing; generate `crypto.randomUUID()` only for new browser-created nodes.
- One imported tree may contain at most 100 nodes; one comment may contain at most 2200 characters.
- Excel parsing is `.xlsx` only, read-only, `data_only=True`; do not execute formulas, macros, or external links.
- Existing Campaigns keep their frozen template revision and snapshot.
- All API success payloads use `{ "data": ... }`; errors use the existing fixed `{ "error": ... }` envelope.
- Legacy mode keeps management authentication, administrator authorization, and CSRF. Local-direct keeps loopback and Host guards.

### Verification runtimes

```powershell
$PY = 'python'
$NODE = 'node'
```

---

## File Map

- Create `comment_campaign/template_import.py`: pure workbook parsing, normalization, tree validation, and conversion to `TemplateCreate` dictionaries.
- Modify `comment_campaign/schemas.py`: strict commit request shapes for normalized imported trees.
- Modify `comment_campaign/service.py`: preview and independently commit valid imported trees.
- Modify `comment_campaign/blueprint.py`: multipart preview and JSON commit routes plus fixed import error codes.
- Create `gateway/static/comment_tree_editor.js`: comment-tree draft state, validation, safe DOM rendering, and import preview rendering.
- Modify `gateway/static/comment_campaign.js`: integrate the editor, save/import callbacks, and Campaign template dropdown.
- Modify `gateway/templates/comment_campaign.html`: load the editor module before the workbench.
- Modify `gateway/static/comment_campaign.css`: approved dark visual system and responsive editor layout.
- Modify `comment_campaign/errors.py`: register the four stable import error codes used by `CampaignValidationError`.
- Modify `docs/architecture/api/openapi.yaml`: import routes and request/response schemas.
- Modify `docs/architecture/api/error-codes.md`: stable import error codes.
- Modify `docs/architecture/modules/comment-campaign.md`: user flow and hidden-ID boundary.
- Create `tests/test_comment_template_import.py`: parser and service tests.
- Modify `tests/test_comment_campaign_routes.py`: route contract, auth-safe envelope, and redaction tests.
- Modify `tests/test_comment_campaign_integration.py`: real Gateway route and frozen-template regression.
- Modify `tests-js/comment-campaign-ui.test.js`: tree state, DOM, import, Campaign dropdown, responsive-contract assertions.

---

### Task 1: Parse and validate Excel comment trees

**Files:**
- Create: `comment_campaign/template_import.py`
- Create: `tests/test_comment_template_import.py`

**Interfaces:**
- Produces: `preview_comment_tree_workbook(filename: str, content: bytes) -> dict[str, Any]`
- Produces: `import_tree_to_template(tree: Mapping[str, Any], *, id_factory: Callable[[], str] = lambda: str(uuid4())) -> dict[str, Any]`
- Output tree shape: `{name, nodes: [{node_no, parent_node_no, text, row, position}], errors, valid}`

- [ ] **Step 1: Write failing parser tests**

```python
def test_preview_groups_linear_and_branched_trees(xlsx_bytes):
    result = preview_comment_tree_workbook("trees.xlsx", xlsx_bytes([
        ["评论树名称", "节点序号", "回复节点序号", "评论文案"],
        ["A", "1", "", "root"],
        ["A", "2", "", "linear child"],
        ["A", "3", "1", "branch child"],
        ["B", "1", "", "other root"],
    ]))
    assert [tree["name"] for tree in result["trees"]] == ["A", "B"]
    assert result["trees"][0]["nodes"][1]["parent_node_no"] == "1"
    assert result["trees"][0]["nodes"][2]["parent_node_no"] == "1"
    assert all(tree["valid"] for tree in result["trees"])


@pytest.mark.parametrize("rows,code", [
    ([["A", "1", "", ""]], "comment_text_missing"),
    ([["A", "1", "", "root"], ["A", "1", "", "duplicate"]], "duplicate_node_no"),
    ([["A", "1", "2", "one"], ["A", "2", "1", "two"]], "cycle_detected"),
    ([["A", "1", "9", "orphan"]], "parent_not_found"),
])
def test_preview_reports_tree_scoped_errors(xlsx_bytes, rows, code):
    result = preview_comment_tree_workbook(
        "trees.xlsx",
        xlsx_bytes([["评论树名称", "节点序号", "回复节点序号", "评论文案"], *rows]),
    )
    assert result["trees"][0]["valid"] is False
    assert code in {error["code"] for error in result["trees"][0]["errors"]}
```

- [ ] **Step 2: Run tests and confirm failure**

Run:

```powershell
& $PY -m pytest tests/test_comment_template_import.py -q -p no:cacheprovider
```

Expected: collection fails because `comment_campaign.template_import` does not exist.

- [ ] **Step 3: Implement the parser and converter**

Implement these exact boundaries in `comment_campaign/template_import.py`:

```python
MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 5000
MAX_TREE_NODES = 100
MAX_COMMENT_LENGTH = 2200

HEADER_ALIASES = {
    "tree_name": {"评论树名称", "tree_name"},
    "node_no": {"节点序号", "node_no"},
    "parent_node_no": {"回复节点序号", "parent_node_no"},
    "text": {"评论文案", "comment_text", "text"},
}


def preview_comment_tree_workbook(filename: str, content: bytes) -> dict[str, Any]:
    if Path(filename or "").suffix.lower() != ".xlsx":
        raise CampaignValidationError("unsupported_import_type")
    if not content or len(content) > MAX_IMPORT_BYTES:
        raise CampaignValidationError("import_file_too_large" if content else "import_file_invalid")
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True, keep_links=False)
        rows = list(islice(workbook.active.iter_rows(values_only=True), MAX_IMPORT_ROWS + 2))
    except Exception as exc:
        raise CampaignValidationError("import_file_invalid") from exc
    if len(rows) > MAX_IMPORT_ROWS + 1:
        raise CampaignValidationError("import_file_too_large")
    headers = _map_headers(rows[0] if rows else ())
    grouped = _normalize_rows(rows[1:], headers)
    trees = [_validate_tree(name, nodes) for name, nodes in grouped.items()]
    return {"trees": trees, "summary": {
        "tree_count": len(trees),
        "valid_count": sum(1 for tree in trees if tree["valid"]),
        "rejected_count": sum(1 for tree in trees if not tree["valid"]),
    }}


def import_tree_to_template(tree: Mapping[str, Any], *, id_factory: Callable[[], str] = lambda: str(uuid4())) -> dict[str, Any]:
    checked = _validate_tree(str(tree.get("name") or ""), list(tree.get("nodes") or []))
    if not checked["valid"]:
        raise CampaignValidationError("template_invalid")
    id_by_no = {node["node_no"]: id_factory() for node in checked["nodes"]}
    steps = []
    for position, node in enumerate(checked["nodes"]):
        parent_no = node["parent_node_no"]
        steps.append({
            "id": id_by_no[node["node_no"]],
            "label": "楼主评论" if parent_no is None else f"回复 {position}",
            "content_source": "fixed",
            "fixed_text": node["text"],
            "content_library_id": "",
            "content_item_id": "",
            "parent_step_id": id_by_no[parent_no] if parent_no else None,
            "required_profile_tags": [],
            "excluded_profile_tags": [],
            "language": "",
        })
    return {"name": checked["name"], "description": "", "supported_modes": ["threaded"], "language": "", "tags": [], "steps": steps}
```

Implement `_map_headers`, `_normalize_rows`, and `_validate_tree` as pure functions. `_validate_tree` must report row-aware errors, enforce exactly one root, detect missing parents and cycles over the complete graph, and default a blank parent to the previous node except for the first node.

- [ ] **Step 4: Run focused tests**

Run the command from Step 2.

Expected: all parser tests pass; no test opens AdsPower or a browser.

- [ ] **Step 5: Commit**

```powershell
git add comment_campaign/template_import.py tests/test_comment_template_import.py
git commit -m "feat: parse comment tree imports"
```

---

### Task 2: Add strict preview and commit API operations

**Files:**
- Modify: `comment_campaign/errors.py`
- Modify: `comment_campaign/schemas.py`
- Modify: `comment_campaign/service.py`
- Modify: `comment_campaign/blueprint.py`
- Modify: `tests/test_comment_template_import.py`
- Modify: `tests/test_comment_campaign_routes.py`

**Interfaces:**
- Consumes: `preview_comment_tree_workbook`, `import_tree_to_template`
- Produces: `CommentCampaignService.preview_template_import(filename: str, content: bytes) -> dict`
- Produces: `CommentCampaignService.import_templates(payload: TemplateImportCommit) -> dict`
- Produces routes: `POST /api/browser-v2/comment-template-imports/preview` and `POST /api/browser-v2/comment-template-imports`

- [ ] **Step 1: Write failing service and route tests**

```python
def test_import_service_commits_valid_trees_independently(service, xlsx_bytes):
    preview = service.preview_template_import("trees.xlsx", xlsx_bytes([
        ["评论树名称", "节点序号", "回复节点序号", "评论文案"],
        ["A", "1", "", "root"], ["A", "2", "1", "child"],
        ["B", "1", "9", "broken"],
    ]))
    result = service.import_templates({"trees": preview["trees"]})
    assert [item["name"] for item in result["created"]] == ["A"]
    assert result["rejected"][0]["name"] == "B"
    assert service.store.list_templates()[0]["steps"][0]["id"] != "1"


def test_preview_route_requires_xlsx_and_returns_envelope(client, xlsx_bytes):
    response = client.post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(xlsx_bytes([["评论树名称", "节点序号", "回复节点序号", "评论文案"], ["A", "1", "", "root"]])), "trees.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["trees"][0]["name"] == "A"
```

Also add cases for missing file, unknown JSON keys, files over 2 MiB, malformed workbooks, fixed Chinese error messages, and recursive redaction of injected forbidden keys.

- [ ] **Step 2: Run tests and confirm failure**

```powershell
& $PY -m pytest tests/test_comment_template_import.py tests/test_comment_campaign_routes.py -q -p no:cacheprovider
```

Expected: fails because the schemas, service methods, and routes are absent.

- [ ] **Step 3: Implement strict schemas and service methods**

Add to `comment_campaign/schemas.py`:

```python
class ImportedTreeNode(_StrictInput):
    node_no: str = Field(min_length=1, max_length=120)
    parent_node_no: str | None = Field(default=None, max_length=120)
    text: str = Field(min_length=1, max_length=2200)
    row: int = Field(ge=2, le=5001)
    position: int = Field(ge=0, le=99)


class ImportedTree(_StrictInput):
    name: str = Field(min_length=1, max_length=100)
    nodes: list[ImportedTreeNode] = Field(min_length=1, max_length=100)
    errors: list[dict] = Field(default_factory=list, max_length=100)
    valid: bool


class TemplateImportCommit(_StrictInput):
    trees: list[ImportedTree] = Field(min_length=1, max_length=100)
```

Add these values to `ERROR_CODES` in `comment_campaign/errors.py` so `CampaignValidationError` cannot collapse them to `invalid_state_transition`:

```python
"import_file_invalid",
"unsupported_import_type",
"import_file_too_large",
"import_tree_failed",
```

Add to `CommentCampaignService`:

```python
def preview_template_import(self, filename: str, content: bytes) -> dict[str, Any]:
    return preview_comment_tree_workbook(filename, content)


def import_templates(self, payload: TemplateImportCommit | dict[str, Any]) -> dict[str, Any]:
    request = payload if isinstance(payload, TemplateImportCommit) else TemplateImportCommit.model_validate(payload)
    created, rejected = [], []
    for tree in request.trees:
        raw = tree.model_dump()
        if not raw["valid"] or raw["errors"]:
            rejected.append({"name": raw["name"], "errors": raw["errors"] or [{"code": "template_invalid"}]})
            continue
        try:
            template = TemplateCreate.model_validate(import_tree_to_template(raw))
            created.append(self.create_template(template))
        except CampaignError as exc:
            rejected.append({"name": raw["name"], "errors": [{"code": exc.code}]})
    return {"created": created, "rejected": rejected}
```

Do not catch unknown exceptions in the service; the Blueprint must retain the existing fixed `internal_error` projection.

- [ ] **Step 4: Implement the routes and run tests**

Add the strict route bodies to `comment_campaign/blueprint.py`:

```python
@blueprint.post("/comment-template-imports/preview")
def preview_template_import():
    if set(request.files) != {"file"} or request.form:
        raise CampaignApiError("invalid_request")
    upload = request.files["file"]
    content = upload.stream.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise CampaignApiError("import_file_too_large", 413)
    return _data(_call(service(), "preview_template_import", upload.filename or "", content))


@blueprint.post("/comment-template-imports")
def import_templates():
    payload = _parse(TemplateImportCommit)
    return _data(_call(service(), "import_templates", payload), 201)
```

Add fixed `_MESSAGES` entries for `import_file_invalid`, `unsupported_import_type`, `import_file_too_large`, and `import_tree_failed`. Map file type/content validation to 422 and size overflow to 413.
Extend the route-test fake service with `preview_template_import(filename, content)` and `import_templates(payload)` methods that record delegation arguments and return safe fixed payloads.

Run the Step 2 command.

Expected: all focused backend tests pass.

- [ ] **Step 5: Commit**

```powershell
git add comment_campaign/errors.py comment_campaign/schemas.py comment_campaign/service.py comment_campaign/blueprint.py tests/test_comment_template_import.py tests/test_comment_campaign_routes.py
git commit -m "feat: add comment tree import API"
```

---

### Task 3: Extract testable comment-tree draft behavior

**Files:**
- Create: `gateway/static/comment_tree_editor.js`
- Modify: `gateway/templates/comment_campaign.html`
- Modify: `tests-js/comment-campaign-ui.test.js`

**Interfaces:**
- Produces global `CommentTreeEditor`
- Produces: `createDraft`, `addReply`, `removeNode`, `moveNode`, `setParent`, `validate`, `templatePayload`, `render`
- Consumes only injected DOM and callbacks; no polling and no direct global fetch.

- [ ] **Step 1: Write failing Node tests for the pure draft API**

```javascript
test("threaded replies get hidden UUIDs and default to the previous node", () => {
  const ids = ["uuid-root", "uuid-child"];
  const draft = editor.createDraft(() => ids.shift(), "threaded");
  const next = editor.addReply(draft, () => ids.shift());
  assert.equal(next.nodes[1].id, "uuid-child");
  assert.equal(next.nodes[1].parentId, "uuid-root");
  assert.equal(JSON.stringify(next).includes("step_1"), false);
});


test("independent comments remain peers without parents", () => {
  const ids = ["uuid-a", "uuid-b"];
  const draft = editor.createDraft(() => ids.shift(), "independent");
  const next = editor.addReply(draft, () => ids.shift());
  assert.deepEqual(next.nodes.map((node) => node.parentId), [null, null]);
});


test("removing a parent requires an explicit descendant decision", () => {
  const draft = {
    name: "tree", advanced: false,
    nodes: [
      {id: "a", text: "root", parentId: null},
      {id: "b", text: "child", parentId: "a"},
    ],
  };
  assert.throws(() => editor.removeNode(draft, "a", {removeDescendants: false}), /node_has_descendants/);
  assert.equal(editor.removeNode(draft, "a", {removeDescendants: true}).nodes.length, 0);
});
```

Add assertions for one root, cycles, blank text, 100-node cap, 2200-character cap, and preserving existing IDs during edits.

- [ ] **Step 2: Run tests and confirm failure**

```powershell
& $NODE --test tests-js/comment-campaign-ui.test.js
```

Expected: fails because `comment_tree_editor.js` and the new global are absent.

- [ ] **Step 3: Implement the draft module**

Use the existing UMD-compatible project pattern:

```javascript
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CommentTreeEditor = factory();
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function createDraft(idFactory, mode) {
    const makeId = idFactory || (() => crypto.randomUUID());
    return {name: "", mode: mode || "threaded", source: "manual", advanced: false, nodes: [{id: makeId(), text: "", parentId: null}]};
  }
  function addReply(draft, idFactory) {
    const makeId = idFactory || (() => crypto.randomUUID());
    const nodes = draft.nodes.map((node) => ({...node}));
    const parent = nodes[nodes.length - 1] || null;
    nodes.push({id: makeId(), text: "", parentId: draft.mode === "threaded" && parent ? parent.id : null});
    return {...draft, nodes};
  }
  function validate(draft) {
    const errors = [];
    if (!String(draft.name || "").trim()) errors.push({code: "tree_name_missing"});
    if (!Array.isArray(draft.nodes) || draft.nodes.length < 1 || draft.nodes.length > 100) errors.push({code: "tree_size_invalid"});
    const ids = new Set((draft.nodes || []).map((node) => node.id));
    const roots = (draft.nodes || []).filter((node) => !node.parentId);
    if (draft.mode === "threaded" && roots.length !== 1) errors.push({code: "root_count_invalid"});
    if (draft.mode === "independent" && roots.length !== (draft.nodes || []).length) errors.push({code: "independent_parent_invalid"});
    (draft.nodes || []).forEach((node, index) => {
      const text = String(node.text || "").trim();
      if (!text) errors.push({code: "comment_text_missing", index});
      if (text.length > 2200) errors.push({code: "comment_text_too_long", index});
      if (node.parentId && !ids.has(node.parentId)) errors.push({code: "parent_not_found", index});
    });
    return errors.concat(cycleErrors(draft.nodes || []));
  }
  return {createDraft, addReply, removeNode, moveNode, setParent, validate, templatePayload, render};
});
```

Implement `removeNode` without reparenting descendants, `moveNode` without changing IDs or parents, `setParent` with cycle prevention, and `templatePayload` using the current strict `TemplateCreate` shape.
The renderer must expose a human-readable “独立评论 / 盖楼回复” mode control. Switching to independent mode clears every parent only after explicit confirmation; switching back to threaded defaults each non-root node to its preceding node.

- [ ] **Step 4: Load the module and rerun Node tests**

Add before `comment_campaign.js` in `gateway/templates/comment_campaign.html`:

```html
<script defer src="{{ url_for('static', filename='comment_tree_editor.js') }}"></script>
```

Run the Step 2 command.

Expected: all draft behavior tests pass.

- [ ] **Step 5: Commit**

```powershell
git add gateway/static/comment_tree_editor.js gateway/templates/comment_campaign.html tests-js/comment-campaign-ui.test.js
git commit -m "refactor: isolate comment tree editor state"
```

---

### Task 4: Integrate the editor, imports, and Campaign tree selection

**Files:**
- Modify: `gateway/static/comment_campaign.js`
- Modify: `gateway/static/comment_tree_editor.js`
- Modify: `tests-js/comment-campaign-ui.test.js`
- Modify: `tests/test_comment_campaign_integration.py`

**Interfaces:**
- Consumes: `CommentTreeEditor.render(options)`
- Consumes APIs from Task 2
- Replaces Campaign free-text `template_id` with a select whose option value remains the hidden ID.

- [ ] **Step 1: Write failing integration tests**

Add Node assertions that opening the tree drawer produces no label or editable control containing `模板 ID`, `步骤 ID`, `父步骤 ID`, `文案库 ID`, or `文案项 ID`. Add a Campaign test that selects a template by visible name but sends its ID:

```javascript
test("campaign template selector displays names and submits the hidden id", async () => {
  const ui = createHarness({templates: [{id: "internal-template", name: "新品讨论", revision: 3, steps: [{id: "hidden-step"}]}]});
  ui.openDrawer("create");
  const option = ui.document.querySelector('[data-field="template"] option');
  assert.equal(option.textContent.includes("新品讨论"), true);
  assert.equal(option.textContent.includes("internal-template"), false);
  option.parentElement.value = "internal-template";
  await ui.createCampaign();
  assert.equal(ui.requests.at(-1).body.template_id, "internal-template");
});
```

Add import tests for multipart preview, preview error rendering by row, committing only checked valid trees, and preserving the manual draft when polling refreshes templates.

- [ ] **Step 2: Run tests and confirm failure**

```powershell
& $NODE --test tests-js/comment-campaign-ui.test.js
& $PY -m pytest tests/test_comment_campaign_integration.py -q -p no:cacheprovider
```

Expected: UI assertions fail against the old ID-heavy drawer.

- [ ] **Step 3: Replace the old template drawer**

In `openDrawer("template")`, remove the inline ID/source fields and call:

```javascript
CommentTreeEditor.render({
  document: doc(),
  container: body,
  draft: state.draftTemplate || CommentTreeEditor.createDraft(),
  templates: state.templates,
  onDraftChange(next) { state.draftTemplate = next; },
  onSave: saveTemplate,
  onDisable: disableTemplate,
  async onPreviewImport(file) {
    const form = new FormData();
    form.append("file", file);
    const result = await request(API_PREFIX + "/comment-template-imports/preview", "POST", form, {isFormData: true});
    if (!ok(result, [200])) throw new Error(errorMessage(result, "导入预览失败"));
    state.draftTemplateImport = result.data;
    return result.data;
  },
  async onCommitImport(trees) {
    const result = await request(API_PREFIX + "/comment-template-imports", "POST", {trees});
    if (!ok(result, [201])) throw new Error(errorMessage(result, "导入评论树失败"));
    await loadSnapshot("templates", API_PREFIX + "/comment-templates", []);
    return result.data;
  },
});
```

Extend `request` with an explicit FormData path that does not set `Content-Type`; keep `credentials: "same-origin"` and existing CSRF behavior.

Change `saveTemplate` to call `CommentTreeEditor.validate` and `CommentTreeEditor.templatePayload`; map validation codes to row-aware Chinese messages before sending.

- [ ] **Step 4: Replace Campaign template ID input and rerun tests**

Build a native select:

```javascript
const templateSelect = node("select");
templateSelect.dataset.field = "template";
state.templates.filter((template) => template.enabled).forEach((template) => {
  const option = node("option", `${template.name} · ${(template.steps || []).length} 条评论 · 版本 ${template.revision}`);
  option.value = template.id;
  option.selected = option.value === (state.draftCampaign || {}).template_id;
  templateSelect.append(option);
});
templateSelect.addEventListener("change", () => {
  state.draftCampaign = {...(state.draftCampaign || {}), template_id: templateSelect.value};
});
```

Do not include template or step IDs in any visible text.

Run the Step 2 commands.

Expected: Node UI and Gateway integration tests pass.

- [ ] **Step 5: Commit**

```powershell
git add gateway/static/comment_campaign.js gateway/static/comment_tree_editor.js tests-js/comment-campaign-ui.test.js tests/test_comment_campaign_integration.py
git commit -m "feat: add comment tree workbench"
```

---

### Task 5: Apply the approved dark visual system and responsive layout

**Files:**
- Modify: `gateway/static/comment_campaign.css`
- Modify: `gateway/static/comment_tree_editor.js`
- Modify: `tests-js/comment-campaign-ui.test.js`

**Interfaces:**
- Consumes semantic classes emitted by `CommentTreeEditor.render`.
- Produces responsive `.comment-tree-*` layout with no horizontal overflow at 736px or 360px.

- [ ] **Step 1: Write failing visual-contract tests**

```javascript
test("comment tree editor uses approved semantic surfaces", () => {
  const text = fs.readFileSync("gateway/static/comment_campaign.css", "utf8");
  assert.match(text, /#09090B/i);
  assert.match(text, /#18181B/i);
  assert.match(text, /#27272A/i);
  assert.doesNotMatch(text, /#000000/i);
  assert.match(text, /@media \(max-width: 820px\)/);
  assert.match(text, /transition:[^;]*300ms/);
});
```

Add DOM assertions for one-column form controls, advanced branch controls hidden by default, native keyboard-accessible buttons, and preview hierarchy labels.

- [ ] **Step 2: Run Node tests and confirm failure**

```powershell
& $NODE --test tests-js/comment-campaign-ui.test.js
```

Expected: fails because the current stylesheet is light and the approved classes are absent.

- [ ] **Step 3: Implement the dark workbench styles**

Replace the page-level palette and add the editor component rules using these exact base values:

```css
:root {
  color: #F4F4F5;
  background: #09090B;
  font-family: Inter, "SF Pro Text", system-ui, -apple-system, "Segoe UI", sans-serif;
}
.comment-campaign-page { margin: 0; color: #F4F4F5; background: #09090B; }
.campaign-card,
.campaign-plan,
.campaign-approval-panel,
.comment-tree-editor,
.comment-tree-preview {
  background: #18181B;
  border: 1px solid #27272A;
  border-radius: 16px;
  box-shadow: none;
}
.comment-tree-layout {
  display: grid;
  grid-template-columns: minmax(0, 1.65fr) minmax(260px, .85fr);
  gap: 24px;
}
.comment-tree-node { transition: border-color 300ms, transform 300ms; }
.comment-tree-node:hover { border-color: #3F3F46; transform: translateY(-1px); }
@media (max-width: 820px) {
  .comment-tree-layout { grid-template-columns: 1fr; }
}
```

Retain clear `:focus-visible` outlines. Use muted grays for secondary copy and low-saturation green/red only for status and errors.

- [ ] **Step 4: Run visual-contract and full Node tests**

Run the Step 2 command.

Expected: all Node tests pass, Chinese text contains no `�`, and no forbidden ID label is rendered.

- [ ] **Step 5: Commit**

```powershell
git add gateway/static/comment_campaign.css gateway/static/comment_tree_editor.js tests-js/comment-campaign-ui.test.js
git commit -m "style: refine comment tree editor"
```

---

### Task 6: Document contracts and run regression acceptance

**Files:**
- Modify: `docs/architecture/api/openapi.yaml`
- Modify: `docs/architecture/api/error-codes.md`
- Modify: `docs/architecture/modules/comment-campaign.md`
- Modify: `tests/test_comment_campaign_integration.py`
- Modify: `tests/test_comment_campaign_security.py`

**Interfaces:**
- Documents the two new import endpoints and confirms existing template/Campaign contracts remain unchanged.

- [ ] **Step 1: Add failing regression and security tests**

Add tests proving:

```python
def test_import_does_not_change_frozen_campaign_template(service):
    original = service.create_template(existing_tree_payload, "stable-template")
    campaign = service.create_campaign(campaign_payload(template_id="stable-template", template_revision=original["revision"]))
    service.import_templates(valid_import_payload)
    detail = service.get_campaign(campaign["id"])
    assert detail["campaign"]["template_id"] == "stable-template"
    assert detail["campaign"]["template_revision"] == original["revision"]


def test_import_api_redacts_forbidden_nested_values(client):
    response = client.post("/api/browser-v2/comment-template-imports", json=valid_import_payload)
    serialized = json.dumps(response.get_json())
    assert "raw_adspower_id" not in serialized
    assert "ws://" not in serialized.casefold()
    assert "cookie" not in serialized.casefold()
```

Also assert legacy unauthenticated requests are rejected, operator writes are forbidden, administrator writes require CSRF, and local-direct foreign Host/REMOTE_ADDR requests are rejected before service construction.

- [ ] **Step 2: Run the focused regression suite**

```powershell
& $PY -m pytest tests/test_comment_template_import.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_integration.py tests/test_comment_campaign_security.py tests/test_comment_campaign_service.py tests/test_comment_campaign_store.py -q -p no:cacheprovider
```

Expected before documentation/final fixes: new regression tests expose any contract or security gap.

- [ ] **Step 3: Update OpenAPI, errors, and module guide**

Document:

- multipart preview request with required `file`.
- normalized preview tree and row-error response.
- strict JSON commit request and `created`/`rejected` result.
- 200 preview, 201 commit, 413 size error, 422 invalid type/content.
- `import_file_invalid`, `unsupported_import_type`, `import_file_too_large`, `import_tree_failed`.
- CommentTemplate-as-tree decision and hidden-ID UI boundary.
- template revision freezing and no-database-migration statement.

Do not document a new persistence entity or a public raw-ID field.

- [ ] **Step 4: Run all feature acceptance commands**

```powershell
& $NODE --test tests-js/comment-campaign-ui.test.js
& $PY -m pytest tests/test_comment_template_import.py tests/test_comment_campaign_routes.py tests/test_comment_campaign_integration.py tests/test_comment_campaign_security.py tests/test_comment_campaign_service.py tests/test_comment_campaign_store.py -q -p no:cacheprovider
& $PY -m pytest tests/test_comment_campaign_allocation.py tests/test_comment_campaign_threaded.py tests/test_comment_campaign_executor.py tests/test_comment_campaign_recovery.py -q -p no:cacheprovider
git diff --check
```

Expected: every listed test passes; `git diff --check` prints no output. No command may open AdsPower or submit to TikTok.

- [ ] **Step 5: Commit**

```powershell
git add docs/architecture/api/openapi.yaml docs/architecture/api/error-codes.md docs/architecture/modules/comment-campaign.md tests/test_comment_campaign_integration.py tests/test_comment_campaign_security.py
git commit -m "docs: define comment tree import contract"
```

---

## Final Manual Acceptance

- [ ] Open the Comment Campaign workbench in the local browser.
- [ ] Confirm the full page and drawer use the approved dark palette.
- [ ] Create a three-node linear tree without seeing or entering an ID.
- [ ] Enable advanced branching and make node 3 reply to the root.
- [ ] Verify the right-side preview changes immediately.
- [ ] Import an `.xlsx` containing one valid and one invalid tree.
- [ ] Confirm the preview identifies the invalid tree and row without writing data.
- [ ] Commit the valid tree and confirm it appears by name in the tree list.
- [ ] Create a Campaign by selecting the tree name from the dropdown.
- [ ] Confirm the request still contains the hidden `template_id` and the UI never displays it.
- [ ] Confirm an existing Campaign still shows its original frozen template revision after editing the tree.
