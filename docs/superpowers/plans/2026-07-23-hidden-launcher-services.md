# Hidden Launcher Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the Tk launcher, Flask app, and statistics worker without persistent command windows while keeping safe startup-failure feedback in the launcher.

**Architecture:** Start the GUI through `pythonw.exe`; keep the elevated Tk process visible; use `CREATE_NO_WINDOW` for both child services. Each supervisor owns a current-run log, but launcher messages use a strict whitelist and never include log contents.

**Tech Stack:** Python 3.13, Tkinter, VBScript/Windows Script Host, Windows process flags, pytest.

## Global Constraints

- `start_console.vbs` runs `.venv\Scripts\pythonw.exe launcher.py` asynchronously with VBS window style `0`.
- `start_console.cmd` remains compatible, invokes the VBS entry, exits immediately, and contains no `pause`.
- `ShellExecuteW` keeps show mode `1` so the elevated Tk launcher remains visible.
- Windows child services use `CREATE_NO_WINDOW`; non-Windows processes receive no Windows creation flag.
- Flask and worker logs are recreated per run at `data/logs/flask-service.log` and `data/logs/statistics-worker.log`.
- Launcher status and dialogs may contain only service name, exit code, fixed generic reason, and log path. They must never contain service-log contents.
- Keep all existing service commands, environment, stop-file behavior, timeouts, force-kill behavior, port cleanup, restart order, persistence, APIs, strategies, and pages.
- No real-time log panel, Windows service installation, Git initialization, or commits.

---

### Task 1: No-console entry and pre-Tk startup errors

**Files:**
- Create: `start_console.vbs`
- Modify: `start_console.cmd`
- Modify: `launcher.py`
- Modify: `tests/test_console.py`
- Modify: `tests/test_launcher_restart.py`

**Interfaces:**
- Produces: `show_startup_error(message: str, *, native_box=None) -> None`
- Preserves: `ensure_admin()` uses the current interpreter and show mode `1`

- [x] Add tests proving the command delegates to VBS without `pause`, VBS references `pythonw.exe` and `launcher.py`, and `cscript.exe` parses a byte-preserving non-executing copy.
- [x] Observe RED while VBS is absent and syntactically invalid.
- [x] Add ASCII-compatible VBS with doubled-quote command construction and asynchronous hidden `Run`.
- [x] Add native pre-Tk error reporting around `ensure_admin()`.
- [x] Verify launcher/console tests pass and complete independent task review.

---

### Task 2: Hidden service processes and safe log ownership

**Files:**
- Modify: `launcher.py`
- Modify: `tests/test_launcher_restart.py`

**Interfaces:**
- Produces: `hidden_process_options(platform_name: str | None = None) -> dict[str, int]`
- Adds: `FlaskServiceSupervisor(..., log_path: Path | None = None)`
- Adds: `StatisticsWorkerSupervisor(..., log_path: Path | None = None)`
- Adds to both supervisors: `log_path` read-only property returning `Path`

- [ ] **Step 1: Write or update failing tests for hidden processes and logs**

Tests must prove for both supervisors:

```python
assert options["creationflags"] == launcher_module.subprocess.CREATE_NO_WINDOW
assert options["stdout"] is options["stderr"]
assert options["stdout"].name == str(log_path)
```

Also prove:

- `hidden_process_options("posix") == {}`
- each new run truncates the prior log
- `Popen` failure closes the handle
- normal stop, no-child stop, natural exit observed by `state()`, stop-file failure, terminate/kill failure, and final wait timeout close the handle
- failed shutdown retains `_process`; successful shutdown clears it

- [ ] **Step 2: Run focused tests and verify RED where behavior is missing**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py -k "hidden_process or service_supervisor or natural_exit or final_stop_timeout or stop_file_request_fails" -q -p no:cacheprovider
```

Expected before implementation: failures for missing hidden/log lifecycle behavior.

- [ ] **Step 3: Implement minimal hidden/log lifecycle support**

Use:

```python
SERVICE_LOG_DIR = PROJECT_ROOT / "data" / "logs"


def hidden_process_options(platform_name: str | None = None) -> dict[str, int]:
    current_platform = os.name if platform_name is None else platform_name
    if current_platform != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}
```

Each supervisor must:

1. Open its log with `"wb", buffering=0` immediately before `Popen`.
2. Pass the same handle as `stdout` and `stderr`.
3. Pass `**hidden_process_options()`.
4. Close the handle in `Popen` failure and every `stop()` exit.
5. Close the handle when `state()` observes a terminal return code.
6. Preserve `_process` if stopping raises; clear it only after confirmed shutdown.

Expose only the path:

```python
@property
def log_path(self) -> Path:
    return self._log_path
```

- [ ] **Step 4: Remove log-content parsing**

Delete `read_service_error`, all secret/redaction regular expressions used only by it, both `.error_summary()` methods, and their parsing/security tests.

Add a source-level regression:

```python
def test_supervisors_expose_log_path_without_log_content_api(tmp_path):
    flask = FlaskServiceSupervisor(log_path=tmp_path / "flask.log")
    worker = StatisticsWorkerSupervisor(log_path=tmp_path / "worker.log")

    assert flask.log_path == tmp_path / "flask.log"
    assert worker.log_path == tmp_path / "worker.log"
    assert not hasattr(flask, "error_summary")
    assert not hasattr(worker, "error_summary")
```

- [ ] **Step 5: Run Task 2 verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py -q -p no:cacheprovider
```

Expected: zero failures. Independent review must return spec PASS and quality APPROVED before Task 3.

---

### Task 3: Whitelisted startup-failure messages

**Files:**
- Modify: `launcher.py`
- Modify: `tests/test_launcher_restart.py`

**Interfaces:**
- Produces: `service_failure_detail(name: str, state: dict, reason: str, log_path: Path) -> str`
- Produces: `LauncherApp._report_startup_failure(detail: str) -> None`
- Consumes: each supervisor's `.state()` and `.log_path`

- [ ] **Step 1: Write failing formatter and orchestration tests**

Formatter contract:

```python
detail = service_failure_detail(
    "Flask",
    {"running": False, "pid": None, "returncode": 2},
    "服务启动失败或健康检查超时",
    Path("data/logs/flask-service.log"),
)
assert detail == (
    "Flask（退出码 2）：服务启动失败或健康检查超时；"
    "日志：data/logs/flask-service.log"
)
```

Tests must also prove:

- no exit-code suffix when return code is `None`
- Flask health failure stops both services, updates status, and schedules an error dialog
- worker early exit after Flask health succeeds stops both services and prevents browser opening
- a log containing sentinel secrets (`password`, `Cookie`, `Authorization`) never contributes any sentinel text to status or dialog
- `_begin_restart()` exceptions use `_report_startup_failure`

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py -k "failure_detail or reports_flask or statistics_worker_exits or never_uses_log_content" -q -p no:cacheprovider
```

Expected: failures because the formatter and orchestration checks do not exist.

- [ ] **Step 3: Implement whitelist-only failure formatting**

```python
def service_failure_detail(
    name: str,
    state: dict[str, int | bool | None],
    reason: str,
    log_path: Path,
) -> str:
    returncode = state.get("returncode")
    suffix = f"（退出码 {returncode}）" if returncode is not None else ""
    return f"{name}{suffix}：{reason}；日志：{log_path}"
```

This function must not accept log text.

- [ ] **Step 4: Add the UI reporter**

```python
def _report_startup_failure(self, detail: str) -> None:
    self._set_status(detail)
    self.root.after(
        0,
        lambda message=detail: messagebox.showerror("服务启动失败", message),
    )
```

- [ ] **Step 5: Integrate startup checks**

- On Flask health failure, capture Flask state, stop both services, then report with fixed reason `"服务启动失败或健康检查超时"` and `flask_service.log_path`.
- After Flask is healthy, require `statistics_worker.state()["running"] is True`; otherwise stop both and report fixed reason `"启动后立即退出"` and `statistics_worker.log_path`.
- Open the browser only after both checks pass.
- In `_begin_restart()` exception handling, stop services and use only the fixed whitelist message:

```python
self._report_startup_failure(
    "自动启动失败；"
    f"Flask 日志：{self.flask_service.log_path}；"
    f"统计服务日志：{self.statistics_worker.log_path}"
)
```

Do not put raw exception text into the UI.

- [ ] **Step 6: Run focused and full verification**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py tests\test_console.py tests\test_tiktok_stats_scheduler.py tests\test_tiktok_stats_restart_persistence.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
npm.cmd run test:node
.\.venv\Scripts\python.exe -m py_compile launcher.py
```

Expected: zero failures and exit code `0` for all commands.

- [ ] **Step 7: Manual acceptance handoff**

Do not automatically open `start_console.vbs`, because the launcher intentionally stops the current port-5000 owner and may request UAC. Ask the user to double-click `start_console.vbs` and confirm that only the launcher and UAC consent appear, with no persistent command window.
