# Launcher Restart Services Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Opening the launcher stops every process listening on port `5000`, checks the environment, starts the current Flask app and statistics worker, waits for health, then opens the dashboard.

**Architecture:** Keep all behavior in `launcher.py`. Add two small pure/process helpers: Windows port cleanup and a Flask child-process supervisor. `LauncherApp` runs one background restart workflow and reuses current checks and `StatisticsWorkerSupervisor`.

**Tech Stack:** Python 3.13, Tkinter, Windows `netstat`/`taskkill`, Flask, pytest.

## Global Constraints

- Stop only PIDs reported as `LISTENING` on local port `5000`; user authorized terminating any owner of that port.
- Use `taskkill /PID <pid> /T /F`; never scan or stop unrelated ports or arbitrary Python processes.
- Before creating Tk, request Windows UAC elevation with `runas` when the launcher is not already administrator; the unelevated parent exits.
- No PID registry, Windows service, new package, or new persistent configuration.
- Run process cleanup and checks outside the Tk UI thread.
- Failed cleanup, checks, or health checks must not open the browser.
- Do not manage debug ports `53330` or `53332`.
- Current directory is not a Git repository; do not initialize or repair Git and do not create commits.

---

### Task 1: Port cleanup and Flask process ownership

**Files:**
- Modify: `launcher.py:20-100`
- Create: `tests/test_launcher_restart.py`

**Interfaces:**
- Produces: `parse_listening_pids(output: str, port: int) -> list[int]`
- Produces: `stop_port_listeners(port: int, *, command_runner=None, sleep=None, current_pid=None, poll_attempts=20) -> list[int]`
- Produces: `FlaskServiceSupervisor.start(environment=None)`, `.stop(timeout=10)`, `.state()`

- [ ] **Step 1: Write failing parsing and cleanup tests**

```python
from types import SimpleNamespace

from launcher import parse_listening_pids, stop_port_listeners


NETSTAT = """
TCP    127.0.0.1:5000    0.0.0.0:0    LISTENING    111
TCP    [::1]:5000        [::]:0       LISTENING    222
TCP    127.0.0.1:5001    0.0.0.0:0    LISTENING    333
TCP    127.0.0.1:5000    127.0.0.1:9  ESTABLISHED  444
"""


def test_parse_listening_pids_limits_scope_and_deduplicates():
    assert parse_listening_pids(NETSTAT + NETSTAT, 5000) == [111, 222]


def test_stop_port_listeners_kills_each_tree_and_waits_for_release():
    calls = []
    netstat_outputs = [NETSTAT, ""]

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "netstat":
            return SimpleNamespace(returncode=0, stdout=netstat_outputs.pop(0), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    stopped = stop_port_listeners(
        5000,
        command_runner=run,
        sleep=lambda _seconds: None,
        current_pid=999,
    )

    assert stopped == [111, 222]
    assert [call for call in calls if call[0] == "taskkill"] == [
        ["taskkill", "/PID", "111", "/T", "/F"],
        ["taskkill", "/PID", "222", "/T", "/F"],
    ]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py -p no:cacheprovider -q
```

Expected: collection fails because `parse_listening_pids` and `stop_port_listeners` do not exist.

- [ ] **Step 3: Implement minimal port helpers**

Add to `launcher.py`:

```python
def parse_listening_pids(output: str, port: int) -> list[int]:
    found = set()
    for line in output.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[-2].upper() != "LISTENING":
            continue
        local = fields[1]
        _, separator, port_text = local.rpartition(":")
        if not separator or port_text != str(port) or not fields[-1].isdigit():
            continue
        found.add(int(fields[-1]))
    return sorted(found)


def stop_port_listeners(
    port: int,
    *,
    command_runner=None,
    sleep=None,
    current_pid=None,
    poll_attempts: int = 20,
) -> list[int]:
    runner = command_runner or subprocess.run
    pause = sleep or time.sleep
    own_pid = os.getpid() if current_pid is None else current_pid

    def listening() -> list[int]:
        completed = runner(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "无法查询端口占用")
        return [pid for pid in parse_listening_pids(completed.stdout, port) if pid != own_pid]

    targets = listening()
    for pid in targets:
        completed = runner(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"无法停止 PID {pid}")

    for _ in range(poll_attempts):
        if not listening():
            return targets
        pause(0.1)
    raise RuntimeError(f"端口 {port} 未释放")
```

- [ ] **Step 4: Add RED tests for Flask ownership**

```python
from launcher import FlaskServiceSupervisor


class FakeProcess:
    pid = 4321

    def __init__(self):
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9


def test_flask_supervisor_owns_one_process_and_stops_it():
    process = FakeProcess()
    calls = []
    supervisor = FlaskServiceSupervisor(
        popen_factory=lambda command, **kwargs: calls.append((command, kwargs)) or process
    )

    assert supervisor.start(environment={"APP_CONFIG_PATH": "x"}) is process
    assert supervisor.start(environment={"APP_CONFIG_PATH": "x"}) is process
    assert len(calls) == 1
    assert calls[0][0][-1].endswith("app.py")

    supervisor.stop()

    assert process.terminate_calls == 1
    assert supervisor.state()["running"] is False
```

- [ ] **Step 5: Run Flask ownership test and verify RED**

Run the Task 1 command again. Expected: import failure for `FlaskServiceSupervisor`.

- [ ] **Step 6: Implement `FlaskServiceSupervisor` beside the worker supervisor**

Use the existing worker supervisor pattern. Command must be `[sys.executable, str(PROJECT_ROOT / "app.py")]`, `cwd=PROJECT_ROOT`, and passed environment. `start()` returns the existing live child. `stop()` calls `terminate()`, waits, then `kill()` only after timeout. `state()` returns `running`, `pid`, and `returncode` like `StatisticsWorkerSupervisor.state()`.

- [ ] **Step 7: Run Task 1 tests**

Run the Task 1 command. Expected: all tests pass.

---

### Task 1A: UAC elevation required by forced cleanup

**Files:**
- Modify: `launcher.py`
- Modify: `tests/test_launcher_restart.py`

**Interfaces:**
- Produces: `ensure_admin(*, is_admin=None, shell_execute=None) -> bool`
- Changes: `main()` returns before `Tk()` when an elevated child was launched

- [ ] Write tests proving an administrator continues without relaunch, a normal process calls `runas` with current Python and `launcher.py`, ShellExecute errors raise, and `main()` never creates Tk in the unelevated parent.
- [ ] Run focused tests and verify failure because `ensure_admin` does not exist.
- [ ] Implement with `ctypes.windll.shell32.IsUserAnAdmin` and `ShellExecuteW`; treat return values `<= 32` as errors.
- [ ] Run focused tests and verify pass.

---

### Task 2: Automatic launcher restart workflow

**Files:**
- Modify: `launcher.py:486-725`
- Modify: `tests/test_launcher_restart.py`
- Test: `tests/test_console.py`
- Test: `tests/test_tiktok_stats_scheduler.py`

**Interfaces:**
- Consumes: `stop_port_listeners(5000)` and `FlaskServiceSupervisor`
- Produces: `LauncherApp.restart()` and `LauncherApp.close()` lifecycle

- [ ] **Step 1: Write failing orchestration tests**

Add this test scaffold, using the intended `_restart_services(run_checks: bool)` internal method:

```python
from launcher import CheckResult, LauncherApp


class RecordingSupervisor:
    def __init__(self, events, name):
        self.events = events
        self.name = name
        self.process = SimpleNamespace(poll=lambda: None)

    def stop(self):
        self.events.append(f"{self.name}-stop")

    def start(self, environment=None):
        self.events.append(f"{self.name}-start")
        return self.process


def test_restart_services_runs_in_required_order(monkeypatch):
    events = []
    launcher = LauncherApp.__new__(LauncherApp)
    launcher.flask_service = RecordingSupervisor(events, "flask")
    launcher.statistics_worker = RecordingSupervisor(events, "worker")
    launcher.root = SimpleNamespace(after=lambda _delay, callback: callback())
    launcher._render = lambda _results: None
    launcher._set_status = lambda _message: None
    launcher._service_environment = lambda: {}
    launcher._wait_for_flask = lambda _process: events.append("health") or True

    monkeypatch.setattr(
        "launcher.stop_port_listeners",
        lambda port: events.append(f"port-stop:{port}"),
    )
    monkeypatch.setattr(
        "launcher.run_all_checks",
        lambda: events.append("checks") or [CheckResult("环境", True, "通过")],
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open",
        lambda _url: events.append("browser"),
    )

    launcher._restart_services(run_checks=True)

assert events == [
    "flask-stop",
    "worker-stop",
    "port-stop:5000",
    "checks",
    "flask-start",
    "worker-start",
    "health",
    "browser",
]


def test_restart_services_stops_when_check_blocks(monkeypatch):
    events = []
    launcher = LauncherApp.__new__(LauncherApp)
    launcher.flask_service = RecordingSupervisor(events, "flask")
    launcher.statistics_worker = RecordingSupervisor(events, "worker")
    launcher.root = SimpleNamespace(after=lambda _delay, callback: callback())
    launcher._render = lambda _results: None
    launcher._set_status = lambda _message: None
    monkeypatch.setattr("launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}"))
    monkeypatch.setattr(
        "launcher.run_all_checks",
        lambda: events.append("checks") or [CheckResult("环境", False, "失败")],
    )

    launcher._restart_services(run_checks=True)

    assert events == ["flask-stop", "worker-stop", "port-stop:5000", "checks"]
```

Add one matching health-failure test where `_wait_for_flask` returns `False`; assert both newly started supervisors receive a second `stop` and `webbrowser.open` is not called. Add a close test with recording supervisors and root; expected order is `flask-stop`, `worker-stop`, `destroy`.

- [ ] **Step 2: Run orchestration tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py -p no:cacheprovider -q
```

Expected: fail because automatic restart workflow is absent.

- [ ] **Step 3: Implement automatic restart**

Make these surgical changes:

```python
self.flask_service = FlaskServiceSupervisor()
self.root.after(100, self.restart)
```

`restart()` disables check/start buttons and launches a daemon thread targeting `_restart_services(run_checks=True)`. Background order:

1. `self.flask_service.stop()`
2. `self.statistics_worker.stop()`
3. `stop_port_listeners(5000)`
4. `self.results = run_all_checks()` and schedule `_render`
5. Abort with status if any blocking result failed
6. Build current environment exactly as existing `start()` does
7. Start Flask, then worker
8. Poll `APP_URL` with `urllib.request.urlopen(..., timeout=1)` for at most 15 seconds; abort if Flask exits
9. On success schedule `webbrowser.open(APP_URL)`
10. Re-enable controls in `finally`

Make manual `start()` use the same stop/start/health path after validating `check_completed`. Do not duplicate launch logic.

- [ ] **Step 4: Update close behavior**

```python
def close(self) -> None:
    self.flask_service.stop()
    self.statistics_worker.stop()
    self.root.destroy()
```

- [ ] **Step 5: Run focused launcher tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_launcher_restart.py tests\test_console.py tests\test_tiktok_stats_scheduler.py -p no:cacheprovider -q
```

Expected: all pass.

- [ ] **Step 6: Run full regressions and syntax checks**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -p no:cacheprovider -q
npm.cmd run test:node
.\.venv\Scripts\python.exe -m compileall -q launcher.py gateway tiktok_stats tests
```

Expected: zero failures and exit code `0` for all commands.

- [ ] **Step 7: Real local smoke test**

Before running, resolve exact PIDs listening on `5000`. Launch `launcher.py`; verify old PIDs exit, one current Flask listener appears, `/tiktok-stats` returns `200` and contains `TikTok 每日数据`, and opening a second launcher replaces the first Flask PID. Close both launchers; verify port `5000` has no listener. Do not operate AdsPower or real TikTok accounts.

- [ ] **Step 8: Record checkpoint**

Run `git status --short`. Expected current environment result: exit `128`, `fatal: not a git repository`. Do not initialize Git; record test evidence in the final response.
