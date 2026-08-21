# Launcher Console Overview Startup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a successful Windows launcher startup open the new Console overview instead of the legacy root dashboard.

**Architecture:** Keep the existing launcher lifecycle unchanged and replace its single startup URL constant. The same URL remains the target for Flask readiness checks and the browser open, so startup verifies the page it will display.

**Tech Stack:** Python 3, Tkinter launcher, pytest.

## Global Constraints

- Use the exact URL `http://127.0.0.1:5000/console/overview` without a trailing slash.
- Preserve the legacy root route `/` and its existing UI.
- Do not redirect `/`.
- Do not change Console routes, settings routes, service startup order, retry behavior, or process lifecycle.
- Do not add startup-page configuration.
- Keep unrelated working-tree changes intact.

---

## File Structure

- Modify `launcher.py`: change only the `APP_URL` constant used by readiness checks and `webbrowser.open`.
- Modify `tests/test_launcher_restart.py`: prove a successful restart passes the exact Console overview URL to the browser.

### Task 1: Open the Console overview after successful startup

**Files:**
- Modify: `launcher.py:38`
- Modify: `tests/test_launcher_restart.py:794-880`

**Interfaces:**
- Consumes: `LauncherApp._restart_services(run_checks=False)` and the existing immediate `root.after` test fixture.
- Produces: one call to `webbrowser.open("http://127.0.0.1:5000/console/overview")` after all services become healthy.

- [ ] **Step 1: Write the failing launcher test**

Add this test beside the existing successful restart tests:

```python
def test_restart_opens_console_overview_after_success(monkeypatch):
    events = []
    opened_urls = []
    launcher = launcher_for_restart(events)
    monkeypatch.setattr(
        "launcher.stop_port_listeners",
        lambda port: events.append(f"port-stop:{port}"),
    )
    monkeypatch.setattr("launcher.webbrowser.open", opened_urls.append)

    assert launcher._restart_services(run_checks=False) is True
    assert opened_urls == ["http://127.0.0.1:5000/console/overview"]
```

- [ ] **Step 2: Run the test and verify the old URL fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_launcher_restart.py::test_restart_opens_console_overview_after_success -q
```

Expected: FAIL because the captured value is still `http://127.0.0.1:5000/`.

- [ ] **Step 3: Change the single launcher constant**

Replace the existing declaration with:

```python
APP_URL = "http://127.0.0.1:5000/console/overview"
```

Do not change `_wait_for_flask`, `_restart_services`, route definitions, or any supervisor.

- [ ] **Step 4: Run the focused test**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_launcher_restart.py::test_restart_opens_console_overview_after_success -q
```

Expected: PASS.

- [ ] **Step 5: Run launcher and Console regressions**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_launcher_restart.py tests/test_console_pages.py -q
```

Expected: all tests PASS. A pytest cache warning caused by the existing `.pytest_cache` permission may still be printed without failing the command.

- [ ] **Step 6: Perform a source-level scope check**

Run:

```powershell
git diff -- launcher.py tests/test_launcher_restart.py
```

Expected: one URL constant change and one regression test only; no root-route or startup-lifecycle edits.

- [ ] **Step 7: Commit when Git write access is available**

```powershell
git add launcher.py tests/test_launcher_restart.py docs/superpowers/specs/2026-08-20-launcher-console-overview-startup-design.md docs/superpowers/plans/2026-08-20-launcher-console-overview-startup.md
git commit -m "fix: open console overview after startup"
```
