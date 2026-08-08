# Browser V2 Shared Sidebar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reuse the management-console sidebar on `/browser-v2` while hiding only the old execution-strategy navigation link.

**Architecture:** Keep `_dashboard_sidebar.html` as the single navigation source. Wrap Browser V2 in the existing `dashboard-shell`/`dashboard-main` layout, retain the current `#browser-v2-app` controller root, and use one page-scoped CSS override to avoid double outer padding.

**Tech Stack:** Flask/Jinja, HTML, CSS, vanilla JavaScript tests, pytest.

## Global Constraints

- Remove only the old `/?panel=strategies` link from the shared sidebar.
- Preserve the old execution-strategy page, routes, APIs, configuration, and data.
- Keep the `/browser-v2` sidebar link without `active` and without `aria-current="page"`.
- Reuse `gateway/templates/_dashboard_sidebar.html` and `gateway/static/dashboard_shell.css`; do not duplicate navigation markup.
- Do not modify Browser V2 APIs, JavaScript state, actions, or data models.
- The repository metadata is read-only in this workspace, so do not stage or commit files.

---

### Task 1: Lock the shared-navigation contract

**Files:**
- Modify: `tests-js/browser-v2-ui.test.js`
- Modify: `tests/test_execution_v2_integration.py`

**Interfaces:**
- Consumes: Jinja partial `gateway/templates/_dashboard_sidebar.html` and route `GET /browser-v2`.
- Produces: regression assertions for navigation visibility and rendered shell structure.

- [x] **Step 1: Add a failing source-level sidebar test**

Extend the existing Browser V2 template test with:

```js
const sidebar = fs.readFileSync(path.join(
  __dirname, "..", "gateway", "templates", "_dashboard_sidebar.html"
), "utf8");
assert.match(html, /dashboard_shell\.css/);
assert.match(html, /class="dashboard-shell"/);
assert.match(html, /_dashboard_sidebar\.html/);
assert.match(html, /class="dashboard-main"/);
assert.doesNotMatch(sidebar, /href="\/\?panel=strategies"/);
assert.match(sidebar, /class="dashboard-nav-link" href="\/browser-v2"/);
assert.doesNotMatch(
  sidebar.match(/<a[^>]+href="\/browser-v2"[^>]*>/)[0],
  /active|aria-current/
);
```

- [x] **Step 2: Add rendered-page assertions**

In `test_direct_mode_opens_dashboard_and_v2_without_login_or_management_state`, add:

```python
assert 'class="dashboard-shell"' in v2_html
assert 'class="dashboard-sidebar"' in v2_html
assert 'class="dashboard-main"' in v2_html
assert 'href="/?panel=strategies"' not in v2_html
assert '<a class="dashboard-nav-link" href="/browser-v2">' in v2_html
```

- [x] **Step 3: Run tests and verify they fail**

```powershell
& 'C:\Users\burn1ng\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --test tests-js\browser-v2-ui.test.js
& .\.venv\Scripts\python.exe -m pytest tests\test_execution_v2_integration.py::test_direct_mode_opens_dashboard_and_v2_without_login_or_management_state -q -p no:cacheprovider
```

Expected: both fail because Browser V2 does not yet include the shared shell and the old link remains.

---

### Task 2: Reuse the dashboard shell on Browser V2

**Files:**
- Modify: `gateway/templates/_dashboard_sidebar.html`
- Modify: `gateway/templates/browser_v2.html`
- Modify: `gateway/static/browser_v2.css`
- Test: `tests-js/browser-v2-ui.test.js`
- Test: `tests/test_execution_v2_integration.py`

**Interfaces:**
- Consumes: `.dashboard-shell`, `.dashboard-sidebar`, and `.dashboard-main` from `dashboard_shell.css`.
- Produces: a rendered Browser V2 page with the existing `#browser-v2-app` root inside the shared layout.

- [x] **Step 1: Hide the old sidebar entry**

Delete only this line from `_dashboard_sidebar.html`:

```html
<a class="dashboard-nav-link{% if active_nav == 'strategies' %} active{% endif %}" href="/?panel=strategies" data-panel="strategies"{% if active_nav == 'strategies' %} aria-current="page"{% endif %}>执行策略</a>
```

Leave the following V2 entry unchanged:

```html
<a class="dashboard-nav-link" href="/browser-v2">浏览器执行策略 V2</a>
```

- [x] **Step 2: Load the shared shell stylesheet**

Add this before `browser_v2.css` in `browser_v2.html`:

```html
<link rel="stylesheet" href="{{ url_for('static', filename='dashboard_shell.css') }}">
```

- [x] **Step 3: Wrap the V2 app in the shared layout**

Replace the page-level opening structure with:

```html
<body class="browser-v2-page">
  <div class="dashboard-shell">
    {% include '_dashboard_sidebar.html' %}
    <main class="dashboard-main">
      <div id="browser-v2-app" class="v2-shell">
```

Replace the final `</main>` before `</body>` with:

```html
      </div>
    </main>
  </div>
</body>
```

Do not rename or remove any IDs inside `#browser-v2-app`.

- [x] **Step 4: Prevent double content padding**

Add after the existing `body` rule in `browser_v2.css`:

```css
.browser-v2-page .dashboard-main { padding: 0; }
```

The existing `.v2-shell` supplies 28 pixels on desktop and 16 pixels below 760 pixels.

- [x] **Step 5: Run focused tests**

Run the Task 1 commands.

Expected: PASS.

---

### Task 3: Regression verification

**Files:**
- Verify only; modify Task 2 files only if a regression is found.

**Interfaces:**
- Consumes: final shared sidebar and Browser V2 template.
- Produces: evidence that V2 behavior and shared responsive navigation remain intact.

- [x] **Step 1: Run Browser V2 backend and UI tests**

```powershell
$v2Tests = @(Get-ChildItem tests -File | Where-Object { $_.Name -like 'test_execution_v2_*' } | ForEach-Object { $_.FullName })
& .\.venv\Scripts\python.exe -m pytest $v2Tests -q -p no:cacheprovider
& 'C:\Users\burn1ng\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: all selected tests pass.

- [x] **Step 2: Run shared-sidebar regressions**

```powershell
& 'C:\Users\burn1ng\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe' --test tests-js\tiktok-stats-ui.test.js tests-js\dashboard-navigation.test.js tests-js\selector-probe-console.test.js
```

Expected: all selected tests pass.

- [x] **Step 3: Run syntax and diff checks**

```powershell
& .\.venv\Scripts\python.exe -m py_compile gateway\app.py
& 'C:\Users\burn1ng\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe' diff --check
```

Expected: exit code 0. Do not stage or commit because `.git` metadata is read-only.
