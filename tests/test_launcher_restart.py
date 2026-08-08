import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import launcher as launcher_module
from launcher import (
    CheckResult,
    CommentCampaignWorkerSupervisor,
    FlaskServiceSupervisor,
    LauncherApp,
    SelectorProbeWorkerSupervisor,
    StatisticsWorkerSupervisor,
    _requirement_names,
    ensure_admin,
    hidden_process_options,
    parse_listening_pids,
    service_failure_detail,
    show_startup_error,
    stop_port_listeners,
)


NETSTAT = """
TCP    127.0.0.1:5000    0.0.0.0:0    LISTENING    111
TCP    [::1]:5000        [::]:0       LISTENING    222
TCP    127.0.0.1:5001    0.0.0.0:0    LISTENING    333
TCP    127.0.0.1:5000    127.0.0.1:9  ESTABLISHED  444
"""


def test_parse_listening_pids_limits_scope_and_deduplicates():
    assert parse_listening_pids(NETSTAT + NETSTAT, 5000) == [111, 222]


def test_requirement_names_parses_the_real_requirements_file():
    assert _requirement_names()


def test_stop_port_listeners_kills_each_tree_and_waits_for_release():
    calls = []
    netstat_outputs = [NETSTAT, ""]

    def run(command, **_kwargs):
        calls.append(command)
        if command[0] == "netstat":
            return SimpleNamespace(
                returncode=0,
                stdout=netstat_outputs.pop(0),
                stderr="",
            )
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


def test_stop_port_listeners_ignores_current_process():
    outputs = [
        "TCP 127.0.0.1:5000 0.0.0.0:0 LISTENING 111\n",
        "",
    ]
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=outputs.pop(0), stderr="")

    assert stop_port_listeners(
        5000,
        command_runner=run,
        sleep=lambda _seconds: None,
        current_pid=111,
    ) == []
    assert not any(call[0] == "taskkill" for call in calls)


def test_stop_port_listeners_reports_taskkill_failure():
    def run(command, **_kwargs):
        if command[0] == "netstat":
            return SimpleNamespace(returncode=0, stdout=NETSTAT, stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="access denied")

    try:
        stop_port_listeners(
            5000,
            command_runner=run,
            sleep=lambda _seconds: None,
            current_pid=999,
        )
    except RuntimeError as exc:
        assert "access denied" in str(exc)
    else:
        raise AssertionError("taskkill failure must stop restart")


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


def test_hidden_process_options_are_windows_only():
    assert hidden_process_options("posix") == {}
    assert hidden_process_options("nt") == {
        "creationflags": launcher_module.subprocess.CREATE_NO_WINDOW
    }


@pytest.mark.parametrize(
    ("supervisor_type", "expected_tail"),
    [
        (FlaskServiceSupervisor, "app.py"),
        (StatisticsWorkerSupervisor, "serve"),
        (SelectorProbeWorkerSupervisor, "serve"),
    ],
)
def test_service_supervisors_hide_and_redirect_output(
    supervisor_type, expected_tail, tmp_path
):
    process = FakeProcess()
    calls = []
    log_path = tmp_path / f"{supervisor_type.__name__}.log"
    supervisor = supervisor_type(
        popen_factory=lambda command, **kwargs: calls.append((command, kwargs)) or process,
        log_path=log_path,
    )

    supervisor.start(environment={"APP_CONFIG_PATH": "x"})

    command, options = calls[0]
    assert expected_tail in command[-1]
    assert options["creationflags"] == launcher_module.subprocess.CREATE_NO_WINDOW
    assert options["stdout"] is options["stderr"]
    assert options["stdout"].name == str(log_path)

    supervisor.stop()

    assert options["stdout"].closed is True


@pytest.mark.parametrize(
    "supervisor_type",
    [
        FlaskServiceSupervisor,
        StatisticsWorkerSupervisor,
        SelectorProbeWorkerSupervisor,
    ],
)
def test_service_supervisor_closes_log_when_popen_fails(supervisor_type, tmp_path):
    log_path = tmp_path / "service.log"
    captured = {}

    def fail_start(_command, **options):
        captured.update(options)
        raise OSError("cannot start")

    supervisor = supervisor_type(popen_factory=fail_start, log_path=log_path)

    with pytest.raises(OSError, match="cannot start"):
        supervisor.start()

    assert captured["stdout"].closed is True


@pytest.mark.parametrize(
    "supervisor_type",
    [
        FlaskServiceSupervisor,
        StatisticsWorkerSupervisor,
        SelectorProbeWorkerSupervisor,
    ],
)
def test_service_supervisor_closes_log_without_a_live_process(supervisor_type, tmp_path):
    supervisor = supervisor_type(log_path=tmp_path / "service.log")
    supervisor._log_handle = supervisor._log_path.open("wb", buffering=0)

    supervisor.stop()

    assert supervisor._log_handle is None


def test_supervisors_expose_log_path_without_log_content_api(tmp_path):
    flask = FlaskServiceSupervisor(log_path=tmp_path / "flask.log")
    worker = StatisticsWorkerSupervisor(log_path=tmp_path / "worker.log")
    probe = SelectorProbeWorkerSupervisor(log_path=tmp_path / "probe.log")

    assert flask.log_path == tmp_path / "flask.log"
    assert worker.log_path == tmp_path / "worker.log"
    assert probe.log_path == tmp_path / "probe.log"
    assert not hasattr(flask, "error_summary")
    assert not hasattr(worker, "error_summary")
    assert not hasattr(probe, "error_summary")


def test_selector_probe_supervisor_uses_its_own_default_paths():
    statistics = StatisticsWorkerSupervisor()
    probe = SelectorProbeWorkerSupervisor()

    assert probe.log_path == (
        launcher_module.PROJECT_ROOT / "data" / "logs" / "selector-probe-worker.log"
    )
    assert probe._stop_file.parent == (
        launcher_module.PROJECT_ROOT / "data" / "selector-probe"
    )
    assert probe._stop_file != statistics._stop_file


@pytest.mark.parametrize(
    "supervisor_type",
    [
        FlaskServiceSupervisor,
        StatisticsWorkerSupervisor,
        SelectorProbeWorkerSupervisor,
    ],
)
def test_service_supervisor_recreates_log_for_a_new_run(supervisor_type, tmp_path):
    first = FakeProcess()
    second = FakeProcess()
    processes = iter([first, second])
    log_path = tmp_path / "service.log"
    supervisor = supervisor_type(
        popen_factory=lambda *_args, **_kwargs: next(processes),
        log_path=log_path,
    )

    supervisor.start()
    supervisor.stop()
    log_path.write_text("old output", encoding="utf-8")
    supervisor.start()

    assert log_path.read_bytes() == b""
    supervisor.stop()


@pytest.mark.parametrize(
    "supervisor_type",
    [
        FlaskServiceSupervisor,
        StatisticsWorkerSupervisor,
        SelectorProbeWorkerSupervisor,
    ],
)
def test_service_supervisor_closes_log_when_state_observes_natural_exit(
    supervisor_type, tmp_path
):
    process = FakeProcess()
    process.returncode = 7
    supervisor = supervisor_type(
        popen_factory=lambda *_args, **_kwargs: process,
        log_path=tmp_path / "service.log",
    )

    supervisor.start()
    state = supervisor.state()

    assert state["running"] is False
    assert supervisor._log_handle is None


class FinalWaitTimeoutProcess:
    pid = 8765

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        raise launcher_module.subprocess.TimeoutExpired("service", timeout)


@pytest.mark.parametrize(
    "supervisor_type",
    [
        FlaskServiceSupervisor,
        StatisticsWorkerSupervisor,
        SelectorProbeWorkerSupervisor,
    ],
)
def test_service_supervisor_closes_log_after_final_stop_timeout(
    supervisor_type, tmp_path
):
    process = FinalWaitTimeoutProcess()
    kwargs = {"log_path": tmp_path / "service.log"}
    if supervisor_type in (StatisticsWorkerSupervisor, SelectorProbeWorkerSupervisor):
        kwargs["stop_file"] = tmp_path / "worker.stop"
    supervisor = supervisor_type(
        popen_factory=lambda *_args, **_kwargs: process,
        **kwargs,
    )
    supervisor.start()

    with pytest.raises(launcher_module.subprocess.TimeoutExpired):
        supervisor.stop()

    assert supervisor._log_handle is None
    assert supervisor._process is process


class StopOperationErrorProcess:
    pid = 8766

    def __init__(self, failing_operation):
        self.failing_operation = failing_operation

    def poll(self):
        return None

    def terminate(self):
        if self.failing_operation == "terminate":
            raise OSError("terminate sentinel-secret")

    def kill(self):
        if self.failing_operation == "kill":
            raise OSError("kill sentinel-secret")

    def wait(self, timeout=None):
        raise launcher_module.subprocess.TimeoutExpired("service", timeout)


@pytest.mark.parametrize(
    "supervisor_type",
    [
        FlaskServiceSupervisor,
        StatisticsWorkerSupervisor,
        SelectorProbeWorkerSupervisor,
    ],
)
@pytest.mark.parametrize("failing_operation", ["terminate", "kill"])
def test_service_supervisor_closes_log_when_terminate_or_kill_raises(
    supervisor_type, failing_operation, tmp_path
):
    process = StopOperationErrorProcess(failing_operation)
    kwargs = {"log_path": tmp_path / "service.log"}
    if supervisor_type in (StatisticsWorkerSupervisor, SelectorProbeWorkerSupervisor):
        kwargs["stop_file"] = tmp_path / "worker.stop"
    supervisor = supervisor_type(
        popen_factory=lambda *_args, **_kwargs: process,
        **kwargs,
    )
    supervisor.start()

    with pytest.raises(OSError, match=failing_operation):
        supervisor.stop(timeout=0)

    assert supervisor._log_handle is None
    assert supervisor._process is process


def test_statistics_worker_closes_log_when_stop_file_request_fails(tmp_path, monkeypatch):
    process = FakeProcess()
    supervisor = StatisticsWorkerSupervisor(
        popen_factory=lambda *_args, **_kwargs: process,
        log_path=tmp_path / "service.log",
        stop_file=tmp_path / "worker.stop",
    )
    supervisor.start()
    monkeypatch.setattr(
        supervisor,
        "_request_stop",
        lambda: (_ for _ in ()).throw(OSError("cannot write stop file")),
    )

    with pytest.raises(OSError, match="cannot write stop file"):
        supervisor.stop()

    assert supervisor._log_handle is None
    assert supervisor._process is process


def test_selector_probe_supervisor_starts_hidden_worker(tmp_path):
    process = FakeProcess()
    calls = []
    stop_file = tmp_path / "probe.stop"
    log_path = tmp_path / "probe.log"
    supervisor = SelectorProbeWorkerSupervisor(
        popen_factory=lambda args, **kwargs: calls.append(
            {"args": args, **kwargs}
        )
        or process,
        stop_file=stop_file,
        log_path=log_path,
    )

    supervisor.start(environment={"PATH": "test"})

    assert calls[0]["args"] == [
        launcher_module.sys.executable,
        "-m",
        "selector_probe.worker",
        "serve",
    ]
    assert calls[0]["cwd"] == launcher_module.PROJECT_ROOT
    assert calls[0]["env"] == {
        "PATH": "test",
        "SELECTOR_PROBE_STOP_FILE": str(stop_file),
    }
    assert calls[0]["creationflags"] == launcher_module.subprocess.CREATE_NO_WINDOW
    assert calls[0]["stdout"] is calls[0]["stderr"]

    supervisor.stop()
    assert calls[0]["stdout"].closed is True


def test_comment_campaign_worker_supervisor_starts_hidden_worker(tmp_path):
    process = FakeProcess()
    calls = []
    supervisor = CommentCampaignWorkerSupervisor(
        popen_factory=lambda command, **kwargs: calls.append((command, kwargs)) or process,
        log_path=tmp_path / "campaign-worker.log",
    )

    supervisor.start(environment={"COMMENT_CAMPAIGN_REDIS_URL": "redis://127.0.0.1/0"})

    assert calls[0][0][-3:] == ["-m", "comment_campaign.worker", "serve"]
    assert calls[0][1]["creationflags"] == launcher_module.subprocess.CREATE_NO_WINDOW
    assert calls[0][1]["env"] == {"COMMENT_CAMPAIGN_REDIS_URL": "redis://127.0.0.1/0"}
    supervisor.stop()


def test_restart_cleans_up_when_comment_campaign_worker_exits_early(monkeypatch):
    events = []
    statuses = []
    launcher = launcher_for_restart(events)
    launcher.comment_campaign_worker = RecordingSupervisor(events, "campaign")
    launcher.comment_campaign_worker.log_path = Path("data/logs/comment-campaign-worker.log")
    launcher.comment_campaign_worker.current_state = {"running": False, "pid": None, "returncode": 12}
    launcher.results = [CheckResult("environment", True, "passed")]
    launcher.check_completed = True
    launcher._set_status = statuses.append
    monkeypatch.setattr("launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}"))
    monkeypatch.setattr("launcher.webbrowser.open", lambda _url: events.append("browser"))
    monkeypatch.setattr("launcher.messagebox.showerror", lambda *_args: None)

    assert launcher._restart_services(run_checks=False) is False

    assert events == [
        "flask-stop", "worker-stop", "probe-stop", "campaign-stop", "port-stop:5000",
        "flask-start", "worker-start", "probe-start", "campaign-start", "health",
        "worker-state", "probe-state", "campaign-state",
        "flask-stop", "worker-stop", "probe-stop", "campaign-stop",
    ]
    assert "browser" not in events
    assert "campaign-worker.log" in statuses[0]


def test_selector_probe_supervisor_requests_cooperative_stop_before_terminate(tmp_path):
    stop_file = tmp_path / "probe.stop"

    class CooperativeProcess(FakeProcess):
        def wait(self, timeout=None):
            assert stop_file.exists()
            self.returncode = 0
            return self.returncode

    process = CooperativeProcess()
    supervisor = SelectorProbeWorkerSupervisor(
        popen_factory=lambda *_args, **_kwargs: process,
        stop_file=stop_file,
        log_path=tmp_path / "probe.log",
    )
    supervisor.start()

    supervisor.stop()

    assert process.terminate_calls == 0
    assert process.kill_calls == 0
    assert not stop_file.exists()
    assert supervisor.state() == {"running": False, "pid": None, "returncode": 0}


def test_selector_probe_supervisor_forces_cleanup_when_stop_file_write_fails(
    tmp_path, monkeypatch
):
    process = FakeProcess()
    supervisor = SelectorProbeWorkerSupervisor(
        popen_factory=lambda *_args, **_kwargs: process,
        stop_file=tmp_path / "probe.stop",
        log_path=tmp_path / "probe.log",
    )
    supervisor.start()
    monkeypatch.setattr(
        supervisor,
        "_request_stop",
        lambda: (_ for _ in ()).throw(OSError("cannot write stop file")),
    )

    with pytest.raises(OSError, match="cannot write stop file"):
        supervisor.stop()

    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert supervisor._process is None
    assert supervisor._log_handle is None
    assert supervisor.state() == {"running": False, "pid": None, "returncode": 0}


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
    assert supervisor.state() == {"running": False, "pid": None, "returncode": 0}


class RecordingSupervisor:
    def __init__(self, events, name, *, record=True):
        self.events = events
        self.name = name
        self.record = record
        self.process = SimpleNamespace(poll=lambda: None)
        self.current_state = {"running": True, "pid": 4321, "returncode": None}
        self.log_path = Path(f"data/logs/{name}-service.log")
        self.stop_error = None

    def stop(self):
        if self.record:
            self.events.append(f"{self.name}-stop")
        if self.stop_error is not None:
            raise self.stop_error

    def start(self, environment=None):
        if self.record:
            self.events.append(f"{self.name}-start")
        return self.process

    def state(self):
        if self.record:
            self.events.append(f"{self.name}-state")
        return self.current_state


def launcher_for_restart(events):
    launcher = LauncherApp.__new__(LauncherApp)
    launcher.flask_service = RecordingSupervisor(events, "flask")
    launcher.statistics_worker = RecordingSupervisor(events, "worker")
    launcher.selector_probe_worker = RecordingSupervisor(events, "probe")
    launcher.comment_campaign_worker = RecordingSupervisor(events, "campaign", record=False)
    launcher.root = SimpleNamespace(after=lambda _delay, callback: callback())
    launcher._render = lambda _results: None
    launcher._set_status = lambda _message: None
    launcher._service_environment = lambda: {}
    launcher._wait_for_flask = lambda _process: events.append("health") or True
    launcher.results = []
    launcher.check_completed = False
    launcher._closing = False
    launcher._cancel_event = threading.Event()
    launcher._lifecycle_lock = threading.RLock()
    return launcher


def test_restart_services_runs_in_required_order(monkeypatch):
    events = []
    launcher = launcher_for_restart(events)

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

    assert launcher._restart_services(run_checks=True) is True
    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "checks",
        "flask-start",
        "worker-start",
        "probe-start",
        "health",
        "worker-state",
        "probe-state",
        "browser",
    ]
    assert launcher.check_completed is True


def test_restart_services_stops_when_check_blocks(monkeypatch):
    events = []
    launcher = launcher_for_restart(events)
    monkeypatch.setattr(
        "launcher.stop_port_listeners",
        lambda port: events.append(f"port-stop:{port}"),
    )
    monkeypatch.setattr(
        "launcher.run_all_checks",
        lambda: events.append("checks")
        or [CheckResult("环境", False, "失败")],
    )

    assert launcher._restart_services(run_checks=True) is False
    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "checks",
    ]


@pytest.mark.parametrize(
    ("flask_error", "worker_error", "probe_error"),
    [
        (RuntimeError("flask cleanup failed"), None, None),
        (None, RuntimeError("worker cleanup failed"), None),
        (None, None, RuntimeError("probe cleanup failed")),
        (
            RuntimeError("flask cleanup failed"),
            RuntimeError("worker cleanup failed"),
            RuntimeError("probe cleanup failed"),
        ),
    ],
)
def test_restart_reports_and_does_not_start_when_cleanup_fails(
    monkeypatch, flask_error, worker_error, probe_error
):
    events = []
    statuses = []
    dialogs = []
    launcher = launcher_for_restart(events)
    launcher.flask_service.stop_error = flask_error
    launcher.statistics_worker.stop_error = worker_error
    launcher.selector_probe_worker.stop_error = probe_error
    launcher._set_status = statuses.append
    monkeypatch.setattr(
        "launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}")
    )
    monkeypatch.setattr("launcher.messagebox.showerror", lambda *args: dialogs.append(args))

    assert launcher._restart_services(run_checks=False) is False

    assert events == ["flask-stop", "worker-stop", "probe-stop"]
    assert statuses == [
        "\u81ea\u52a8\u542f\u52a8\u5931\u8d25\uff1b"
        f"Flask \u65e5\u5fd7\uff1a{Path('data/logs/flask-service.log')}\uff1b"
        f"\u7edf\u8ba1\u670d\u52a1\u65e5\u5fd7\uff1a{Path('data/logs/worker-service.log')}\uff1b"
        f"\u63a2\u9488\u670d\u52a1\u65e5\u5fd7\uff1a{Path('data/logs/probe-service.log')}\uff1b"
        f"\u8bc4\u8bba Campaign Worker \u65e5\u5fd7\uff1a{Path('data/logs/campaign-service.log')}"
    ]
    assert dialogs == [("\u670d\u52a1\u542f\u52a8\u5931\u8d25", statuses[0])]


def test_restart_services_cleans_new_processes_when_health_fails(monkeypatch):
    events = []
    launcher = launcher_for_restart(events)
    launcher.results = [CheckResult("环境", True, "通过")]
    launcher.check_completed = True
    launcher._wait_for_flask = lambda _process: events.append("health") or False
    monkeypatch.setattr(
        "launcher.stop_port_listeners",
        lambda port: events.append(f"port-stop:{port}"),
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open",
        lambda _url: events.append("browser"),
    )
    monkeypatch.setattr("launcher.messagebox.showerror", lambda *_args: None)

    assert launcher._restart_services(run_checks=False) is False
    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "flask-start",
        "worker-start",
        "probe-start",
        "health",
        "flask-state",
        "flask-stop",
        "worker-stop",
        "probe-stop",
    ]


def test_service_failure_detail_allows_only_whitelisted_fields():
    detail = service_failure_detail(
        "Flask",
        {"running": False, "pid": None, "returncode": 2},
        "\u670d\u52a1\u542f\u52a8\u5931\u8d25\u6216\u5065\u5eb7\u68c0\u67e5\u8d85\u65f6",
        Path("data/logs/flask-service.log"),
    )

    assert detail == (
        "Flask\uff08\u9000\u51fa\u7801 2\uff09\uff1a"
        "\u670d\u52a1\u542f\u52a8\u5931\u8d25\u6216\u5065\u5eb7\u68c0\u67e5\u8d85\u65f6"
        f"\uff1b\u65e5\u5fd7\uff1a{Path('data/logs/flask-service.log')}"
    )
    assert service_failure_detail(
        "Worker",
        {"running": False, "pid": None, "returncode": None},
        "\u542f\u52a8\u540e\u7acb\u5373\u9000\u51fa",
        Path("data/logs/statistics-worker.log"),
    ) == f"Worker\uff1a\u542f\u52a8\u540e\u7acb\u5373\u9000\u51fa\uff1b\u65e5\u5fd7\uff1a{Path('data/logs/statistics-worker.log')}"


def test_restart_reports_flask_health_failure_without_opening_browser(monkeypatch):
    events = []
    statuses = []
    dialogs = []
    scheduled = []
    launcher = launcher_for_restart(events)
    launcher.root = SimpleNamespace(
        after=lambda delay, callback: scheduled.append(delay) or callback()
    )
    launcher.results = [CheckResult("environment", True, "passed")]
    launcher.check_completed = True
    launcher.flask_service.current_state = {
        "running": False,
        "pid": None,
        "returncode": 2,
    }
    launcher._wait_for_flask = lambda _process: events.append("health") or False
    launcher._set_status = statuses.append
    monkeypatch.setattr(
        "launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}")
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open", lambda _url: events.append("browser")
    )
    monkeypatch.setattr("launcher.messagebox.showerror", lambda *args: dialogs.append(args))

    assert launcher._restart_services(run_checks=False) is False

    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "flask-start",
        "worker-start",
        "probe-start",
        "health",
        "flask-state",
        "flask-stop",
        "worker-stop",
        "probe-stop",
    ]
    assert statuses == [
        "Flask\uff08\u9000\u51fa\u7801 2\uff09\uff1a"
        "\u670d\u52a1\u542f\u52a8\u5931\u8d25\u6216\u5065\u5eb7\u68c0\u67e5\u8d85\u65f6"
        f"\uff1b\u65e5\u5fd7\uff1a{Path('data/logs/flask-service.log')}"
    ]
    assert dialogs == [("\u670d\u52a1\u542f\u52a8\u5931\u8d25", statuses[0])]
    assert scheduled == [0]
    assert "browser" not in events


def test_restart_stops_services_when_statistics_worker_exits_early(monkeypatch):
    events = []
    statuses = []
    launcher = launcher_for_restart(events)
    launcher.results = [CheckResult("environment", True, "passed")]
    launcher.check_completed = True
    launcher.statistics_worker.current_state = {
        "running": False,
        "pid": None,
        "returncode": 9,
    }
    launcher._set_status = statuses.append
    monkeypatch.setattr(
        "launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}")
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open", lambda _url: events.append("browser")
    )
    monkeypatch.setattr("launcher.messagebox.showerror", lambda *_args: None)

    assert launcher._restart_services(run_checks=False) is False

    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "flask-start",
        "worker-start",
        "probe-start",
        "health",
        "worker-state",
        "flask-stop",
        "worker-stop",
        "probe-stop",
    ]
    assert statuses == [
        "\u7edf\u8ba1\u670d\u52a1\uff08\u9000\u51fa\u7801 9\uff09\uff1a\u542f\u52a8\u540e\u7acb\u5373\u9000\u51fa"
        f"\uff1b\u65e5\u5fd7\uff1a{Path('data/logs/worker-service.log')}"
    ]
    assert "browser" not in events


def test_restart_stops_services_when_selector_probe_worker_exits_early(monkeypatch):
    events = []
    statuses = []
    launcher = launcher_for_restart(events)
    launcher.results = [CheckResult("environment", True, "passed")]
    launcher.check_completed = True
    launcher.selector_probe_worker.current_state = {
        "running": False,
        "pid": None,
        "returncode": 11,
    }
    launcher._set_status = statuses.append
    monkeypatch.setattr(
        "launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}")
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open", lambda _url: events.append("browser")
    )
    monkeypatch.setattr("launcher.messagebox.showerror", lambda *_args: None)

    assert launcher._restart_services(run_checks=False) is False

    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "flask-start",
        "worker-start",
        "probe-start",
        "health",
        "worker-state",
        "probe-state",
        "flask-stop",
        "worker-stop",
        "probe-stop",
    ]
    assert statuses == [
        "\u5143\u7d20\u63a2\u9488\u670d\u52a1\uff08\u9000\u51fa\u7801 11\uff09\uff1a"
        "\u542f\u52a8\u540e\u7acb\u5373\u9000\u51fa"
        f"\uff1b\u65e5\u5fd7\uff1a{Path('data/logs/probe-service.log')}"
    ]
    assert "browser" not in events


def test_service_failure_detail_never_uses_log_content(tmp_path, monkeypatch):
    log_path = tmp_path / "flask.log"
    log_path.write_text("password=secret\nCookie=session\nAuthorization=Bearer token")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("service-log contents must not be read")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)

    detail = service_failure_detail(
        "Flask",
        {"running": False, "pid": None, "returncode": 1},
        "\u670d\u52a1\u542f\u52a8\u5931\u8d25\u6216\u5065\u5eb7\u68c0\u67e5\u8d85\u65f6",
        log_path,
    )

    assert "password" not in detail
    assert "Cookie" not in detail
    assert "Authorization" not in detail
    assert calls == []


@pytest.mark.parametrize(
    ("flask_error", "worker_error", "probe_error"),
    [
        (RuntimeError("flask cleanup failed"), None, None),
        (None, RuntimeError("worker cleanup failed"), None),
        (None, None, RuntimeError("probe cleanup failed")),
        (
            RuntimeError("flask cleanup failed"),
            RuntimeError("worker cleanup failed"),
            RuntimeError("probe cleanup failed"),
        ),
    ],
)
def test_begin_restart_reports_exceptions_with_fixed_whitelist_message(
    monkeypatch, flask_error, worker_error, probe_error
):
    events = []
    reported = []
    launcher = launcher_for_restart(events)
    launcher.check_button = SimpleNamespace(configure=lambda **_kwargs: None)
    launcher.start_button = SimpleNamespace(configure=lambda **_kwargs: None)
    launcher._restart_thread = None
    launcher.flask_service.stop_error = flask_error
    launcher.statistics_worker.stop_error = worker_error
    launcher.selector_probe_worker.stop_error = probe_error
    launcher._restart_services = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("password=secret")
    )
    launcher._report_startup_failure = reported.append
    launcher._finish_restart = lambda: events.append("finish")

    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def is_alive(self):
            return False

        def start(self):
            self.target()

    monkeypatch.setattr("launcher.threading.Thread", InlineThread)

    launcher._begin_restart(run_checks=False)

    assert events == ["flask-stop", "worker-stop", "probe-stop", "finish"]
    assert reported == [
        "\u81ea\u52a8\u542f\u52a8\u5931\u8d25\uff1b"
        f"Flask \u65e5\u5fd7\uff1a{Path('data/logs/flask-service.log')}\uff1b"
        f"\u7edf\u8ba1\u670d\u52a1\u65e5\u5fd7\uff1a{Path('data/logs/worker-service.log')}\uff1b"
        f"\u63a2\u9488\u670d\u52a1\u65e5\u5fd7\uff1a{Path('data/logs/probe-service.log')}\uff1b"
        f"\u8bc4\u8bba Campaign Worker \u65e5\u5fd7\uff1a{Path('data/logs/campaign-service.log')}"
    ]


def test_launcher_close_stops_all_services_before_destroy():
    events = []
    launcher = launcher_for_restart(events)
    launcher.comment_campaign_worker = RecordingSupervisor(events, "campaign")
    launcher.root = SimpleNamespace(destroy=lambda: events.append("destroy"))

    launcher.close()

    assert events == [
        "flask-stop", "worker-stop", "probe-stop", "campaign-stop", "destroy"
    ]


@pytest.mark.parametrize(
    ("flask_error", "worker_error", "probe_error"),
    [
        (RuntimeError("flask sentinel-secret"), None, None),
        (None, RuntimeError("worker sentinel-secret"), None),
        (None, None, RuntimeError("probe sentinel-secret")),
        (
            RuntimeError("flask sentinel-secret"),
            RuntimeError("worker sentinel-secret"),
            RuntimeError("probe sentinel-secret"),
        ),
    ],
)
def test_launcher_close_attempts_all_stops_reports_fixed_error_and_always_destroys(
    monkeypatch, flask_error, worker_error, probe_error
):
    events = []
    dialogs = []
    launcher = launcher_for_restart(events)
    launcher.flask_service.stop_error = flask_error
    launcher.statistics_worker.stop_error = worker_error
    launcher.selector_probe_worker.stop_error = probe_error
    launcher.root = SimpleNamespace(destroy=lambda: events.append("destroy"))
    monkeypatch.setattr(
        "launcher.messagebox.showerror", lambda *args: dialogs.append(args)
    )

    launcher.close()

    assert events == ["flask-stop", "worker-stop", "probe-stop", "destroy"]
    assert dialogs == [
        (
            "启动器关闭失败",
            "部分后台服务未能停止，请在任务管理器中检查。",
        )
    ]
    assert "sentinel-secret" not in repr(dialogs)


def test_close_before_first_spawn_cancels_restart_without_ui_after_destroy(monkeypatch):
    events = []
    dialogs = []
    browser_calls = []
    environment_started = threading.Event()
    environment_release = threading.Event()
    launcher = launcher_for_restart(events)
    launcher.results = [CheckResult("environment", True, "passed")]
    launcher.check_completed = True
    launcher.root = SimpleNamespace(
        after=lambda _delay, callback: callback(),
        destroy=lambda: events.append("destroy"),
    )

    def blocked_environment():
        environment_started.set()
        assert environment_release.wait(2)
        return {}

    launcher._service_environment = blocked_environment
    monkeypatch.setattr(
        "launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}")
    )
    monkeypatch.setattr(
        "launcher.messagebox.showerror", lambda *args: dialogs.append(args)
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open", lambda url: browser_calls.append(url)
    )

    restart_thread = threading.Thread(
        target=launcher._restart_services,
        kwargs={"run_checks": False},
    )
    restart_thread.start()
    assert environment_started.wait(2)

    launcher.close()
    environment_release.set()
    restart_thread.join(2)

    assert not restart_thread.is_alive()
    assert events == [
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "port-stop:5000",
        "flask-stop",
        "worker-stop",
        "probe-stop",
        "destroy",
    ]
    assert dialogs == []
    assert browser_calls == []
    assert "flask-start" not in events
    assert "worker-start" not in events


def test_close_during_worker_spawn_cleans_child_and_suppresses_post_close_ui(
    monkeypatch,
):
    events = []
    dialogs = []
    browser_calls = []
    worker_spawn_entered = threading.Event()
    worker_spawn_release = threading.Event()
    launcher = launcher_for_restart(events)
    launcher.results = [CheckResult("environment", True, "passed")]
    launcher.check_completed = True
    launcher.root = SimpleNamespace(
        after=lambda _delay, callback: callback(),
        destroy=lambda: events.append("destroy"),
    )
    worker = launcher.statistics_worker
    worker.active = False

    def blocked_worker_start(environment=None):
        events.append("worker-start")
        worker_spawn_entered.set()
        assert worker_spawn_release.wait(2)
        worker.active = True
        return worker.process

    def worker_stop():
        events.append("worker-stop")
        worker.active = False

    worker.start = blocked_worker_start
    worker.stop = worker_stop
    monkeypatch.setattr(
        "launcher.stop_port_listeners", lambda port: events.append(f"port-stop:{port}")
    )
    monkeypatch.setattr(
        "launcher.messagebox.showerror", lambda *args: dialogs.append(args)
    )
    monkeypatch.setattr(
        "launcher.webbrowser.open", lambda url: browser_calls.append(url)
    )

    restart_thread = threading.Thread(
        target=launcher._restart_services,
        kwargs={"run_checks": False},
    )
    restart_thread.start()
    assert worker_spawn_entered.wait(2)

    close_thread = threading.Thread(target=launcher.close)
    close_thread.start()
    assert launcher._cancel_event.wait(2)
    worker_spawn_release.set()
    restart_thread.join(2)
    close_thread.join(2)

    assert not restart_thread.is_alive()
    assert not close_thread.is_alive()
    assert worker.active is False
    assert events[-1] == "destroy"
    assert dialogs == []
    assert browser_calls == []


def test_launcher_initialization_schedules_automatic_restart():
    launcher_path = Path(__file__).resolve().parents[1] / "launcher.py"
    content = launcher_path.read_text(encoding="utf-8")

    assert "self.root.after(100, self.restart)" in content


def test_ensure_admin_does_not_relaunch_an_administrator():
    def unexpected(*_args):
        raise AssertionError("administrator must not relaunch")

    assert ensure_admin(is_admin=lambda: True, shell_execute=unexpected) is True


def test_ensure_admin_relaunches_current_launcher_with_runas():
    calls = []

    def shell_execute(*args):
        calls.append(args)
        return 42

    assert ensure_admin(is_admin=lambda: False, shell_execute=shell_execute) is False
    assert len(calls) == 1
    _window, verb, executable, parameters, directory, show = calls[0]
    assert verb == "runas"
    assert executable == launcher_module.sys.executable
    assert str(Path(launcher_module.__file__).resolve()) in parameters
    assert directory == str(Path(launcher_module.__file__).resolve().parent)
    assert show == 1


def test_ensure_admin_reports_failed_uac_launch():
    with pytest.raises(launcher_module.AdminElevationError, match="UAC"):
        ensure_admin(
            is_admin=lambda: False,
            shell_execute=lambda *_args: 5,
        )


def test_ensure_admin_maps_shell_launch_exception_to_elevation_error():
    def fail_launch(*_args):
        raise OSError("password=sentinel-secret")

    with pytest.raises(launcher_module.AdminElevationError) as caught:
        ensure_admin(
            is_admin=lambda: False,
            shell_execute=fail_launch,
        )

    assert "sentinel-secret" not in str(caught.value)


def test_show_startup_error_uses_native_error_dialog():
    calls = []

    show_startup_error(
        "cannot elevate",
        native_box=lambda *args: calls.append(args) or 1,
    )

    assert calls == [(None, "cannot elevate", "启动器启动失败", 0x10)]


def test_main_shows_fixed_pre_tk_error_when_uac_launch_fails(monkeypatch):
    errors = []
    monkeypatch.setattr("launcher.load_project_environment", lambda: None)
    monkeypatch.setattr(
        "launcher.ensure_admin",
        lambda: (_ for _ in ()).throw(
            launcher_module.AdminElevationError("UAC sentinel-secret")
        ),
    )
    monkeypatch.setattr("launcher.show_startup_error", errors.append)
    monkeypatch.setattr(
        "launcher.Tk",
        lambda: (_ for _ in ()).throw(AssertionError("Tk must not be created")),
    )

    launcher_module.main()

    assert errors == [launcher_module.UAC_STARTUP_ERROR]
    assert "sentinel-secret" not in errors[0]


def test_main_shows_fixed_generic_pre_tk_error_without_exception_content(monkeypatch):
    errors = []
    monkeypatch.setattr("launcher.load_project_environment", lambda: None)
    monkeypatch.setattr(
        "launcher.ensure_admin",
        lambda: (_ for _ in ()).throw(RuntimeError("password=sentinel-secret")),
    )
    monkeypatch.setattr("launcher.show_startup_error", errors.append)
    monkeypatch.setattr(
        "launcher.Tk",
        lambda: (_ for _ in ()).throw(AssertionError("Tk must not be created")),
    )

    launcher_module.main()

    assert errors == [launcher_module.GENERIC_STARTUP_ERROR]
    assert "sentinel-secret" not in errors[0]


def test_main_shows_fixed_generic_error_when_tk_creation_fails(monkeypatch):
    errors = []
    monkeypatch.setattr("launcher.load_project_environment", lambda: None)
    monkeypatch.setattr("launcher.ensure_admin", lambda: True)
    monkeypatch.setattr("launcher.show_startup_error", errors.append)
    monkeypatch.setattr(
        "launcher.Tk",
        lambda: (_ for _ in ()).throw(
            RuntimeError("internal-path password=sentinel-secret")
        ),
    )

    launcher_module.main()

    assert errors == [launcher_module.GENERIC_STARTUP_ERROR]
    assert "sentinel-secret" not in errors[0]


def test_main_shows_fixed_native_error_when_dotenv_bootstrap_fails(monkeypatch):
    errors = []
    monkeypatch.setattr(
        "launcher.load_project_environment",
        lambda: (_ for _ in ()).throw(
            ModuleNotFoundError("dotenv password=sentinel-secret")
        ),
    )
    monkeypatch.setattr(
        "launcher.ensure_admin",
        lambda: (_ for _ in ()).throw(
            AssertionError("admin check must not run after bootstrap failure")
        ),
    )
    monkeypatch.setattr("launcher.show_startup_error", errors.append)
    monkeypatch.setattr(
        "launcher.Tk",
        lambda: (_ for _ in ()).throw(AssertionError("Tk must not be created")),
    )

    launcher_module.main()

    assert errors == [launcher_module.ENVIRONMENT_STARTUP_ERROR]
    assert "sentinel-secret" not in errors[0]


def test_launcher_module_import_does_not_load_dotenv_before_main_guard():
    launcher_path = Path(launcher_module.__file__)
    source = launcher_path.read_text(encoding="utf-8")
    main_prefix = source[: source.index("def load_project_environment")]

    assert "from dotenv import" not in main_prefix
    assert "load_dotenv(" not in main_prefix


def test_main_exits_before_tk_when_elevated_child_was_launched(monkeypatch):
    monkeypatch.setattr("launcher.load_project_environment", lambda: None)
    monkeypatch.setattr("launcher.ensure_admin", lambda: False)
    monkeypatch.setattr(
        "launcher.Tk",
        lambda: (_ for _ in ()).throw(AssertionError("Tk must not be created")),
    )

    launcher_module.main()
