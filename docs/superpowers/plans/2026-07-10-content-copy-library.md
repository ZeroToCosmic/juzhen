# Content Copy Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add multi-brand Excel/CSV/TSV copy import, safe brand-folder management, and a compact folder-grid UI without changing the publishing ID contract.

**Architecture:** A new `gateway/content_import.py` module converts uploaded files into one normalized row structure. `gateway/content_store.py` owns brand identity, grouping, deduplication, statistics, and rename behavior; `gateway/app.py` exposes thin HTTP routes and renders the folder-grid workflow. Existing publishing code continues to consume stable `brand_id` and `copy_id` values.

**Tech Stack:** Python 3, Flask 3.1.1, openpyxl 3.1.5, standard-library `csv`, pytest, HTML/CSS/vanilla JavaScript.

## Global Constraints

- Support `.xlsx`, `.csv`, and `.tsv` files.
- Required import fields are brand name and copy body; Tag is optional.
- Skip duplicates only when normalized body and ordered normalized tags match within the same brand.
- Brand folders support create, open, and rename; there is no delete operation.
- Brand rename must never change `brand_id` or move its directory.
- Publishing continues to use only existing `brand_id` and `copy_id` contracts.
- Upload size is limited to 10 MB.
- The UI is Chinese, compact, responsive, and uses the selected folder-grid layout.
- The current workspace is not a valid Git repository, so commit steps are replaced by test checkpoints.

---

## File Structure

- Create `gateway/content_import.py`: parse supported spreadsheet formats and normalize rows.
- Modify `gateway/content_store.py`: stable brand IDs, brand statistics, rename, deduplication, and batch persistence.
- Modify `gateway/app.py`: upload/rename routes, size handling, folder-grid markup, compact styles, and UI behavior.
- Modify `requirements.txt`: add `openpyxl==3.1.5`.
- Create `tests/test_content_import.py`: parser behavior for all formats and malformed files.
- Modify `tests/test_content_publish.py`: store and API integration tests.
- Modify `tests/test_console.py`: content-management UI contract tests.

### Task 1: Spreadsheet Parser

**Files:**
- Create: `gateway/content_import.py`
- Create: `tests/test_content_import.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `filename: str`, `stream: BinaryIO`.
- Produces: `parse_copy_import(filename, stream) -> {"total": int, "rows": list[dict], "errors": list[dict]}` where every valid row has `row`, `brand_name`, `body`, and `tags`.

- [ ] **Step 1: Add the parser dependency**

Append this exact requirement:

```text
openpyxl==3.1.5
```

- [ ] **Step 2: Write failing parser tests**

```python
from io import BytesIO

import pytest
from openpyxl import Workbook

from gateway.content_import import parse_copy_import


def xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("copy.csv", "品牌名,文案,tag\n春日茶饮,夏日新品,#新品 #饮品\n".encode("utf-8-sig")),
        ("copy.tsv", "brand\tbody\ttags\nCity Coffee\tNew menu\t#coffee\n".encode()),
    ],
)
def test_parse_copy_import_supports_delimited_files(filename, content):
    result = parse_copy_import(filename, BytesIO(content))
    assert result["total"] == 1
    assert result["errors"] == []
    assert result["rows"][0]["row"] == 2


def test_parse_copy_import_supports_xlsx_and_reports_invalid_rows():
    source = xlsx_bytes([
        ["品牌", "正文", "标签"],
        ["春日茶饮", "第一条", "#新品"],
        ["春日茶饮", "", "#空文案"],
    ])
    result = parse_copy_import("copy.xlsx", source)
    assert result["total"] == 2
    assert result["rows"] == [
        {"row": 2, "brand_name": "春日茶饮", "body": "第一条", "tags": "#新品"}
    ]
    assert result["errors"] == [{"row": 3, "error": "缺少文案"}]


@pytest.mark.parametrize("filename", ["copy.txt", "copy.xls"])
def test_parse_copy_import_rejects_unsupported_files(filename):
    with pytest.raises(ValueError, match="仅支持"):
        parse_copy_import(filename, BytesIO(b"data"))


def test_parse_copy_import_rejects_missing_required_headers():
    with pytest.raises(ValueError, match="缺少必要表头"):
        parse_copy_import("copy.csv", BytesIO("文案,tag\n正文,#tag\n".encode()))
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_import.py -q
```

Expected: collection fails because `gateway.content_import` does not exist.

- [ ] **Step 4: Implement the minimal parser**

Create `gateway/content_import.py` with these public functions and mappings:

```python
import csv
from io import TextIOWrapper
from pathlib import Path

from openpyxl import load_workbook


HEADER_ALIASES = {
    "brand_name": {"品牌名", "品牌", "brand_name", "brand"},
    "body": {"文案", "正文", "copy", "body", "content"},
    "tags": {"tag", "标签", "tags"},
}


def normalize_header(value):
    return str(value or "").strip().casefold()


def map_headers(values):
    mapped = {}
    for index, value in enumerate(values):
        normalized = normalize_header(value)
        for field, aliases in HEADER_ALIASES.items():
            if normalized in aliases and field not in mapped:
                mapped[field] = index
    if "brand_name" not in mapped or "body" not in mapped:
        raise ValueError("缺少必要表头：品牌名、文案")
    return mapped


def normalize_row(row_number, values, headers):
    def cell(field):
        index = headers.get(field)
        return str(values[index] or "").strip() if index is not None and index < len(values) else ""

    brand_name = cell("brand_name")
    body = cell("body")
    if not brand_name:
        return None, {"row": row_number, "error": "缺少品牌名"}
    if not body:
        return None, {"row": row_number, "error": "缺少文案"}
    return {
        "row": row_number,
        "brand_name": brand_name,
        "body": body,
        "tags": cell("tags"),
    }, None


def parse_rows(rows):
    rows = iter(rows)
    try:
        headers = map_headers(next(rows))
    except StopIteration as error:
        raise ValueError("导入文件为空") from error
    parsed = []
    errors = []
    total = 0
    for row_number, values in enumerate(rows, start=2):
        if not any(str(value or "").strip() for value in values):
            continue
        total += 1
        item, item_error = normalize_row(row_number, list(values), headers)
        if item_error:
            errors.append(item_error)
        else:
            parsed.append(item)
    return {"total": total, "rows": parsed, "errors": errors}


def parse_copy_import(filename, stream):
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".xlsx":
        try:
            workbook = load_workbook(stream, read_only=True, data_only=True)
            return parse_rows(workbook.active.iter_rows(values_only=True))
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("Excel 文件无法读取") from error
    if suffix in {".csv", ".tsv"}:
        try:
            wrapper = TextIOWrapper(stream, encoding="utf-8-sig", newline="")
            return parse_rows(csv.reader(wrapper, delimiter="," if suffix == ".csv" else "\t"))
        except UnicodeError as error:
            raise ValueError("表格文件必须使用 UTF-8 编码") from error
    raise ValueError("仅支持 .xlsx、.csv、.tsv 文件")
```

- [ ] **Step 5: Run parser tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_import.py -q
```

Expected: all parser tests pass with no warnings.

### Task 2: Brand Store and Deduplicated Import

**Files:**
- Modify: `gateway/content_store.py`
- Modify: `tests/test_content_publish.py`

**Interfaces:**
- Consumes: parser result from `parse_copy_import()`.
- Produces: `find_brand_by_name(data_dir, brand_name)`, `rename_brand(data_dir, brand_id, new_name)`, and `apply_copy_import(data_dir, parsed)`.
- Extends: `list_brands(data_dir)` items with `copy_count` and `updated_at`.

- [ ] **Step 1: Write failing store tests**

```python
import pytest

from gateway.content_store import apply_copy_import, create_brand, list_brands, rename_brand


def test_apply_copy_import_groups_brands_and_skips_duplicates(tmp_path):
    data_dir = tmp_path / "content"
    create_brand(data_dir, "Brand One")
    first = apply_copy_import(data_dir, {
        "total": 3,
        "errors": [],
        "rows": [
            {"row": 2, "brand_name": "Brand One", "body": "正文一", "tags": "#a #b"},
            {"row": 3, "brand_name": "品牌二", "body": "正文二", "tags": "#c"},
            {"row": 4, "brand_name": "Brand One", "body": "正文一", "tags": "#a #b"},
        ],
    })
    assert first["created"] == 2
    assert first["duplicates"] == 1
    assert first["brands_created"] == 1
    assert len(list_brands(data_dir)) == 2
    assert next(item for item in list_brands(data_dir) if item["name"] == "品牌二")["id"].startswith("brand-")


def test_rename_brand_preserves_id_and_rejects_duplicate_name(tmp_path):
    data_dir = tmp_path / "content"
    brand = create_brand(data_dir, "Brand One")
    create_brand(data_dir, "Brand Two")
    renamed = rename_brand(data_dir, brand["id"], "Brand One New")
    assert renamed["id"] == brand["id"]
    assert renamed["name"] == "Brand One New"
    with pytest.raises(ValueError, match="已存在"):
        rename_brand(data_dir, brand["id"], "brand two")
```

- [ ] **Step 2: Run store tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_publish.py -q
```

Expected: import errors for `apply_copy_import` and `rename_brand`.

- [ ] **Step 3: Implement brand identity, statistics, rename, and batch import**

Add these behaviors to `gateway/content_store.py`:

```python
import hashlib


def normalize_brand_name(value):
    return str(value or "").strip()


def find_brand_by_name(data_dir, brand_name):
    target = normalize_brand_name(brand_name).casefold()
    return next((brand for brand in list_brands(data_dir) if brand.get("name", "").casefold() == target), None)


def brand_id_for_name(data_dir, brand_name):
    ascii_slug = slugify(brand_name)
    candidate = ascii_slug if ascii_slug != "brand" else ""
    existing_path = Path(data_dir) / "brands" / candidate if candidate else None
    if candidate and not existing_path.exists():
        return candidate
    digest = hashlib.sha256(brand_name.strip().casefold().encode("utf-8")).hexdigest()[:10]
    return f"brand-{digest}"


def rename_brand(data_dir, brand_id, new_name):
    new_name = normalize_brand_name(new_name)
    if not new_name:
        raise ValueError("品牌名称不能为空")
    path = Path(data_dir) / "brands" / brand_id / "brand.json"
    if not path.exists():
        return None
    duplicate = find_brand_by_name(data_dir, new_name)
    if duplicate and duplicate.get("id") != brand_id:
        raise ValueError("品牌名称已存在")
    brand = read_json(path, {"id": brand_id})
    brand.update({"id": brand_id, "name": new_name, "updated_at": now_iso()})
    write_json(path, brand)
    return brand


def copy_fingerprint(body, tags):
    return body.strip(), tuple(parse_tags(tags))


def apply_copy_import(data_dir, parsed):
    result = {
        "total": parsed["total"],
        "created": 0,
        "duplicates": 0,
        "failed": len(parsed["errors"]),
        "brands_created": 0,
        "brand_results": [],
        "errors": list(parsed["errors"]),
    }
    grouped = {}
    for row in parsed["rows"]:
        grouped.setdefault(row["brand_name"].casefold(), []).append(row)
    for rows in grouped.values():
        brand = find_brand_by_name(data_dir, rows[0]["brand_name"])
        if brand is None:
            brand = create_brand(data_dir, rows[0]["brand_name"])
            result["brands_created"] += 1
        path = Path(data_dir) / "brands" / brand["id"] / "copy.json"
        payload = read_json(path, {"items": []})
        items = payload.get("items", [])
        seen = {copy_fingerprint(item.get("body", ""), item.get("tags", [])) for item in items}
        brand_created = 0
        brand_duplicates = 0
        for row in rows:
            fingerprint = copy_fingerprint(row["body"], row["tags"])
            if fingerprint in seen:
                brand_duplicates += 1
                result["duplicates"] += 1
                continue
            seen.add(fingerprint)
            items.append({
                "id": f"copy-{len(items) + 1}",
                "body": row["body"],
                "tags": parse_tags(row["tags"]),
                "created_at": now_iso(),
            })
            brand_created += 1
            result["created"] += 1
        write_json(path, {"items": items})
        _touch_brand(data_dir, brand["id"])
        result["brand_results"].append({
            "brand_id": brand["id"],
            "brand_name": brand["name"],
            "created": brand_created,
            "duplicates": brand_duplicates,
        })
    return result
```

Update `create_brand()` to return an existing display-name match, use `brand_id_for_name()`, and set `updated_at`. Add `_touch_brand()` and call it from `add_copy_item()`. Update `list_brands()` so each item includes:

```python
brand["copy_count"] = len(list_copy_items(data_dir, brand["id"]))
brand["updated_at"] = brand.get("updated_at", "")
```

- [ ] **Step 4: Run store tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_publish.py -q
```

Expected: all content-store and existing publish tests pass.

### Task 3: Import and Rename HTTP APIs

**Files:**
- Modify: `gateway/app.py`
- Modify: `tests/test_content_publish.py`

**Interfaces:**
- Produces: `PATCH /api/content/brands/<brand_id>` and `POST /api/content/copy/import`.
- Extends: `GET /api/content/brands` response through enriched store data.

- [ ] **Step 1: Write failing API tests**

```python
from io import BytesIO

from openpyxl import Workbook


def api_xlsx_bytes(rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output


def test_copy_import_api_accepts_xlsx_and_returns_summary(monkeypatch, tmp_path):
    client, data_dir, _db_path = make_client(tmp_path, monkeypatch)
    source = api_xlsx_bytes([
        ["品牌名", "文案", "tag"],
        ["品牌一", "正文一", "#a"],
        ["品牌二", "正文二", "#b"],
    ])
    response = client.post(
        "/api/content/copy/import",
        data={"file": (source, "copy.xlsx")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    assert response.get_json()["created"] == 2
    assert len(response.get_json()["brands"]) == 2


def test_brand_rename_api_preserves_id(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    brand = client.post("/api/content/brands", json={"brand": "Brand One"}).get_json()["brand"]
    response = client.patch(f"/api/content/brands/{brand['id']}", json={"name": "Brand New"})
    assert response.status_code == 200
    assert response.get_json()["brand"] == {**response.get_json()["brand"], "id": brand["id"]}


def test_copy_import_api_rejects_missing_file(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    response = client.post("/api/content/copy/import")
    assert response.status_code == 400
    assert response.get_json()["error"] == "请选择导入文件"


def test_copy_import_api_rejects_oversized_file(monkeypatch, tmp_path):
    client, _data_dir, _db_path = make_client(tmp_path, monkeypatch)
    client.application.config["MAX_CONTENT_LENGTH"] = 32
    response = client.post(
        "/api/content/copy/import",
        data={"file": (BytesIO(b"x" * 128), "copy.csv")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert response.get_json()["error"] == "导入文件不能超过 10 MB"
```

- [ ] **Step 2: Run API tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_publish.py -q
```

Expected: 404/405 responses because the new routes do not exist.

- [ ] **Step 3: Add thin routes and upload-size handling**

Add imports for `RequestEntityTooLarge`, `parse_copy_import`, `apply_copy_import`, and `rename_brand`. In `create_app()` set:

```python
app.config.setdefault("MAX_CONTENT_LENGTH", 10 * 1024 * 1024)

@app.errorhandler(RequestEntityTooLarge)
def upload_too_large(_error):
    return jsonify({"error": "导入文件不能超过 10 MB"}), 413
```

Add routes:

```python
@app.patch("/api/content/brands/<brand_id>")
def rename_content_brand_route(brand_id):
    payload = request.get_json(silent=True) or {}
    try:
        brand = rename_brand(app.config["CONTENT_DATA_DIR"], brand_id, payload.get("name", ""))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if brand is None:
        return jsonify({"error": "品牌不存在"}), 404
    return jsonify({"brand": brand, "brands": list_brands(app.config["CONTENT_DATA_DIR"])})


@app.post("/api/content/copy/import")
def import_content_copy_route():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "请选择导入文件"}), 400
    try:
        parsed = parse_copy_import(upload.filename, upload.stream)
        result = apply_copy_import(app.config["CONTENT_DATA_DIR"], parsed)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({**result, "brands": list_brands(app.config["CONTENT_DATA_DIR"])})
```

- [ ] **Step 4: Run API tests and verify GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_publish.py -q
```

Expected: all content and publish API tests pass.

### Task 4: Compact Folder-Grid Content UI

**Files:**
- Modify: `gateway/app.py`
- Modify: `tests/test_console.py`

**Interfaces:**
- Consumes: enriched brand objects and the new import/rename routes.
- Produces: brand overview state, brand detail state, import modal state, and compact responsive presentation.

- [ ] **Step 1: Write failing UI contract tests**

```python
def test_content_management_uses_brand_folder_grid_and_import_dialog():
    page = create_app().test_client().get("/").data.decode("utf-8")
    content_panel = page.split('<section class="panel" id="panel-content">', 1)[1].split(
        '<section class="panel" id="panel-publish">', 1
    )[0]
    assert 'id="content-brand-overview"' in content_panel
    assert 'id="content-brand-grid"' in content_panel
    assert 'id="content-brand-detail"' in content_panel
    assert 'id="content-import-dialog"' in content_panel
    assert 'id="content-brand-dialog"' in content_panel
    assert 'id="content-rename-dialog"' in content_panel
    assert 'accept=".xlsx,.csv,.tsv"' in content_panel
    assert "新建品牌" in content_panel
    assert "导入表格" in content_panel
    assert "删除品牌" not in content_panel
    assert "/api/content/copy/import" in page
    assert "method: \"PATCH\"" in page
```

- [ ] **Step 2: Run UI tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_console.py::test_content_management_uses_brand_folder_grid_and_import_dialog -q
```

Expected: assertions fail because the current content page uses selects and a flat form.

- [ ] **Step 3: Replace content-panel markup with overview, detail, and dialog**

Use this DOM contract inside `panel-content`:

```html
<div class="content-section content-video-section">...</div>
<div class="content-section" id="content-brand-overview">
  <div class="content-toolbar">
    <div><h3>品牌文案库</h3><span class="muted" id="content-brand-total">0 个品牌</span></div>
    <div class="compact-actions">
      <button id="content-create-brand" type="button">+ 新建品牌</button>
      <button class="primary" id="content-open-import" type="button">↑ 导入表格</button>
    </div>
  </div>
  <div class="brand-folder-grid" id="content-brand-grid"></div>
</div>
<div class="content-section is-hidden" id="content-brand-detail">
  <div class="content-toolbar">
    <button class="icon-button" id="content-back-brands" type="button" aria-label="返回品牌列表">←</button>
    <h3 id="content-current-brand-name"></h3>
    <button id="content-rename-brand" type="button">✎ 重命名</button>
  </div>
  <div class="copy-quick-form">
    <label>正文<textarea id="content-copy-body"></textarea></label>
    <label>Tag<input id="content-copy-tags" placeholder="#tag1 #tag2"></label>
    <button class="primary" id="content-add-copy" type="button">添加文案</button>
  </div>
  <div class="table-wrap"><table>...</table></div>
</div>
<dialog id="content-import-dialog">
  <form id="content-import-form">
    <div class="dialog-header"><h3>导入品牌文案</h3><button type="button" id="content-close-import" aria-label="关闭">×</button></div>
    <label>表格文件<input id="content-import-file" name="file" type="file" accept=".xlsx,.csv,.tsv" required></label>
    <div class="import-result is-hidden" id="content-import-result"></div>
    <div class="compact-actions"><button type="button" id="content-cancel-import">取消</button><button class="primary" type="submit">提交导入</button></div>
  </form>
</dialog>
<dialog id="content-brand-dialog">
  <form id="content-brand-form">
    <div class="dialog-header"><h3>新建品牌</h3><button type="button" class="js-close-brand-dialog" aria-label="关闭">×</button></div>
    <label>品牌名称<input id="content-brand-name" autocomplete="off" required></label>
    <div class="compact-actions"><button type="button" class="js-close-brand-dialog">取消</button><button class="primary" type="submit">创建品牌</button></div>
  </form>
</dialog>
<dialog id="content-rename-dialog">
  <form id="content-rename-form">
    <div class="dialog-header"><h3>重命名品牌</h3><button type="button" class="js-close-rename-dialog" aria-label="关闭">×</button></div>
    <label>品牌名称<input id="content-rename-name" autocomplete="off" required></label>
    <div class="compact-actions"><button type="button" class="js-close-rename-dialog">取消</button><button class="primary" type="submit">保存名称</button></div>
  </form>
</dialog>
```

- [ ] **Step 4: Add compact, responsive CSS**

Add scoped styles using the existing palette:

```css
.content-section + .content-section { border-top: 1px solid #d7dee8; margin-top: 18px; padding-top: 18px; }
.content-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 10px; flex-wrap: wrap; }
.content-toolbar h3 { margin: 0; }
.compact-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.brand-folder-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; margin-top: 12px; }
.brand-folder { min-height: 104px; padding: 13px; text-align: left; display: grid; align-content: space-between; border-color: #d7dee8; background: #f9fbfd; }
.brand-folder-name { font-size: 15px; font-weight: 800; overflow-wrap: anywhere; }
.brand-folder-meta { color: #627084; font-size: 12px; }
.copy-quick-form { display: grid; grid-template-columns: minmax(0, 2fr) minmax(180px, 1fr) auto; gap: 10px; align-items: end; margin-top: 12px; }
.copy-quick-form textarea { min-height: 72px; }
.icon-button { width: 38px; padding: 0; justify-content: center; }
.is-hidden { display: none !important; }
dialog { width: min(560px, calc(100% - 32px)); border: 1px solid #d7dee8; border-radius: 8px; padding: 16px; }
dialog::backdrop { background: rgba(23, 32, 51, 0.42); }
.dialog-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.import-result { margin-top: 12px; padding: 10px; background: #f4f7fa; }
@media (max-width: 780px) {
  .brand-folder-grid { grid-template-columns: 1fr; }
  .copy-quick-form { grid-template-columns: 1fr; }
}
```

- [ ] **Step 5: Add overview/detail/import JavaScript**

Extend state and replace select-driven content functions with these contracts:

```javascript
const state = {accounts: [], videos: [], brands: [], copyItems: [], activeBrandId: ""};

function renderBrandFolders() {
  const grid = document.querySelector("#content-brand-grid");
  document.querySelector("#content-brand-total").textContent = `${state.brands.length} 个品牌`;
  grid.innerHTML = state.brands.length ? state.brands.map((brand) => `
    <button class="brand-folder js-open-brand" type="button" data-brand-id="${escapeHtml(brand.id)}">
      <span class="brand-folder-name">▰ ${escapeHtml(brand.name)}</span>
      <span class="brand-folder-meta">${brand.copy_count || 0} 条文案<br>${escapeHtml(brand.updated_at || "尚未更新")}</span>
    </button>
  `).join("") : '<div class="muted">暂无品牌</div>';
}

async function openBrandFolder(brandId) {
  state.activeBrandId = brandId;
  document.querySelector("#content-brand-overview").classList.add("is-hidden");
  document.querySelector("#content-brand-detail").classList.remove("is-hidden");
  const brand = state.brands.find((item) => item.id === brandId);
  document.querySelector("#content-current-brand-name").textContent = brand?.name || "品牌文案";
  await refreshCopyItems();
}

async function submitCopyImport(event) {
  event.preventDefault();
  const file = document.querySelector("#content-import-file").files[0];
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch("/api/content/copy/import", {method: "POST", body: formData});
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "导入失败");
  const result = document.querySelector("#content-import-result");
  const errorRows = (payload.errors || []).map((item) =>
    `<li>第 ${item.row} 行：${escapeHtml(item.error)}</li>`
  ).join("");
  result.classList.remove("is-hidden");
  result.innerHTML = `
    <div>新增 ${payload.created} 条，跳过重复 ${payload.duplicates} 条，失败 ${payload.failed} 条，新建品牌 ${payload.brands_created} 个</div>
    ${errorRows ? `<details><summary>查看失败明细</summary><ul>${errorRows}</ul></details>` : ""}
  `;
  await refreshBrands();
}

async function createContentBrand(event) {
  event.preventDefault();
  const brand = document.querySelector("#content-brand-name").value.trim();
  const result = await postJson("/api/content/brands", {brand});
  if (result.status !== 200) throw new Error(result.data.error || "创建失败");
  document.querySelector("#content-brand-dialog").close();
  await refreshBrands();
}

function openRenameDialog() {
  const brand = state.brands.find((item) => item.id === state.activeBrandId);
  document.querySelector("#content-rename-name").value = brand?.name || "";
  document.querySelector("#content-rename-dialog").showModal();
}

async function renameActiveBrand(event) {
  event.preventDefault();
  const name = document.querySelector("#content-rename-name").value.trim();
  const result = await fetch(`/api/content/brands/${encodeURIComponent(state.activeBrandId)}`, {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name}),
  });
  const payload = await result.json();
  if (!result.ok) throw new Error(payload.error || "重命名失败");
  document.querySelector("#content-rename-dialog").close();
  await refreshBrands();
  await openBrandFolder(state.activeBrandId);
}
```

Update `refreshCopyItems()` and `addContentCopy()` to use `state.activeBrandId`. Keep `publish-brand-id` synchronized from `state.brands`; publishing selectors remain unchanged. Add event delegation for `.js-open-brand`, back, the three dialogs, create, rename, and import submit. Dialog close buttons call their owning dialog's `.close()` method.

- [ ] **Step 6: Run UI and full Python tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_console.py tests\test_content_publish.py tests\test_content_import.py -p no:cacheprovider -q --basetemp=work\pytest-content-ui
```

Expected: all selected tests pass.

### Task 5: Regression and Responsive Verification

**Files:**
- Verify only; modify implementation files only if verification reveals a defect and first add a failing regression test.

**Interfaces:**
- Verifies all Python, Node, API, and browser-facing behavior.

- [ ] **Step 1: Run the complete automated suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q --basetemp=work\pytest-content-final
npm.cmd run test:node --cache .\.npm-cache
```

Expected: both commands exit 0 with zero failures.

- [ ] **Step 2: Start the local console**

```powershell
.\.venv\Scripts\python.exe app.py
```

Expected: Flask serves `http://127.0.0.1:5000/` and `/ping` returns `{"status":"ok"}`.

- [ ] **Step 3: Verify the content workflow in the in-app browser**

At desktop width, verify:

- Video statistics and controls remain visible.
- Brand folders form a compact responsive grid.
- New brand, open folder, rename, manual add, and import dialog are reachable.
- Import result statistics render without exposing uploaded row contents.
- No brand delete control exists.

At a mobile width near 390 px, verify:

- Folder tiles stack to one column.
- Form fields and buttons do not overlap or overflow.
- The import dialog fits within the viewport.

- [ ] **Step 4: Re-run complete suites after any browser-found fixes**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q --basetemp=work\pytest-content-final-2
npm.cmd run test:node --cache .\.npm-cache
```

Expected: both commands exit 0 with zero failures.
