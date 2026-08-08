# Local Direct Dashboard Restoration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the original management dashboard at `/` in local direct mode while keeping Browser V2 at `/browser-v2` without login.

**Architecture:** Local direct mode remains protected by the loopback/Host guard and gains one request hook that initializes a CSRF token for legacy dashboard pages. The root route always renders the existing dashboard; V2 remains a separate template and receives a plain sidebar link.

**Tech Stack:** Python 3, Flask sessions and request hooks, Jinja templates, pytest, Node.js built-in test runner.

## Global Constraints

- Do not restore account/password login in local direct mode.
- Do not weaken `install_local_only_guard` or permit non-loopback access.
- Do not change existing dashboard modules, APIs, data, or V2 behavior.
- `/` renders the existing `CONTROL_PAGE_HTML`; `/browser-v2` renders `browser_v2.html`.
- Traditional management mode keeps its existing authentication, authorization, and CSRF behavior.

---

### Task 1: Restore the root dashboard in local direct mode

**Files:**
- Modify: `gateway/app.py:6684-6690,6769-6781`
- Test: `tests/test_execution_v2_integration.py:44-59`

**Interfaces:**
- Consumes: Flask `session`, existing `secrets.token_urlsafe`, `install_local_only_guard(app)`, and `CONTROL_PAGE_HTML`.
- Produces: a non-empty local session `csrf_token`; `/` returning the original dashboard; unchanged `/browser-v2` V2 page.

- [ ] **Step 1: Rewrite the direct-mode page test to reproduce the regression**

Replace the current assertion that expects V2 at both routes:

```python
def test_direct_mode_opens_dashboard_and_v2_without_login_or_management_state(
    tmp_path,
):
    app, created, state_dir = _direct_app(tmp_path)
    client = app.test_client()

    root = client.get("/", base_url="http://127.0.0.1:5000")
    page = client.get("/browser-v2", base_url="http://127.0.0.1:5000")

    root_html = root.get_data(as_text=True)
    v2_html = page.get_data(as_text=True)
    assert root.status_code == 200
    assert page.status_code == 200
    assert "dashboard-shell" in root_html
    assert "browser-v2-app" not in root_html
    assert "browser-v2-app" in v2_html
    assert client.get("/login", base_url="http://127.0.0.1:5000").status_code == 404
    with client.session_transaction() as local_session:
        assert isinstance(local_session.get("csrf_token"), str)
        assert local_session["csrf_token"]
    assert "management_auth_service_factory" not in app.extensions
    assert created == []
    assert not (state_dir / "management.db").exists()
    assert not (state_dir / "session.key").exists()
```

- [ ] **Step 2: Run the focused test and verify the old V2-only root fails**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_execution_v2_integration.py::test_direct_mode_opens_dashboard_and_v2_without_login_or_management_state -p no:cacheprovider
```

Expected: FAIL because `/` contains `browser-v2-app` and no `dashboard-shell`.

- [ ] **Step 3: Initialize the local CSRF session after the local-only guard**

Extend only the existing local direct branch:

```python
    if local_direct_mode:
        install_local_only_guard(app)

        @app.before_request
        def ensure_local_direct_csrf():
            csrf_token = session.get("csrf_token")
            if not isinstance(csrf_token, str) or not csrf_token:
                session["csrf_token"] = secrets.token_urlsafe(32)
    else:
```

Register the hook after `install_local_only_guard` so rejected foreign requests do not create a local session.

- [ ] **Step 4: Restore the dashboard route without changing V2**

Replace the local V2 branch in `dashboard_page()`:

```python
    @app.get("/")
    def dashboard_page():
        return render_template_string(
            CONTROL_PAGE_HTML,
            active_nav="settings",
            csrf_token=session["csrf_token"],
        )

    @app.get("/browser-v2")
    def browser_v2_page():
        return render_template("browser_v2.html")
```

- [ ] **Step 5: Run direct-mode and authentication regression tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_execution_v2_integration.py tests\test_auth_routes.py -p no:cacheprovider
```

Expected: all tests PASS; local direct mode has no login, while traditional mode still requires authentication.

- [ ] **Step 6: Commit the route restoration**

```powershell
git add -- gateway/app.py tests/test_execution_v2_integration.py
git commit -m "fix(ui): restore local dashboard root"
```

---

### Task 2: Add the independent V2 sidebar entry and run full regression

**Files:**
- Modify: `gateway/templates/_dashboard_sidebar.html`
- Test: `tests/test_execution_v2_integration.py`

**Interfaces:**
- Consumes: the existing `.dashboard-nav-link` styling and `/browser-v2` route.
- Produces: one ordinary anchor without `data-panel`, so dashboard panel switching ignores it.

- [ ] **Step 1: Add the failing sidebar-link assertion**

Add to the direct-mode page test after `root_html` is captured:

```python
    assert 'href="/browser-v2"' in root_html
    assert "浏览器执行策略 V2" in root_html
```

- [ ] **Step 2: Run the focused test and verify the link is missing**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_execution_v2_integration.py::test_direct_mode_opens_dashboard_and_v2_without_login_or_management_state -p no:cacheprovider
```

Expected: FAIL on the missing `/browser-v2` link.

- [ ] **Step 3: Add the plain V2 navigation link**

Append after the existing execution-strategy link in `_dashboard_sidebar.html`:

```html
    <a class="dashboard-nav-link" href="/browser-v2">浏览器执行策略 V2</a>
```

Do not add `data-panel` or replace the existing execution-strategy link.

- [ ] **Step 4: Run focused route and template tests**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_execution_v2_integration.py -p no:cacheprovider
node --test tests-js\browser-v2-ui.test.js tests-js\execution-v2-picker.test.js
```

Expected: all tests PASS.

- [ ] **Step 5: Run the full V2 and launcher regression**

```powershell
$files = Get-ChildItem tests -Filter 'test_execution_v2_*.py' | ForEach-Object { $_.FullName }
& .\.venv\Scripts\python.exe -m pytest -q @files tests\test_launcher_restart.py tests\test_auth_routes.py -p no:cacheprovider
```

Expected: all tests PASS, including local-only rejection and 300 Profile batching.

- [ ] **Step 6: Perform browser acceptance after restarting the launcher**

Verify these visible states:

```text
http://127.0.0.1:5000/            原管理后台，显示原有侧栏和模块
http://127.0.0.1:5000/browser-v2  V2 独立执行模块
侧栏“浏览器执行策略 V2”             打开 /browser-v2
/login                              404
外部 Host 或非回环访问               403
```

- [ ] **Step 7: Commit the navigation change**

```powershell
git add -- gateway/templates/_dashboard_sidebar.html tests/test_execution_v2_integration.py
git commit -m "feat(ui): link Browser V2 from dashboard"
```
