import ctypes
import subprocess
import sys
import types

import pytest

import window_tiler


def test_debug_port_is_parsed_from_adspower_ws_url():
    assert window_tiler.debug_port_from_ws_url(
        "ws://127.0.0.1:55001/devtools/browser/abc"
    ) == 55001


@pytest.mark.parametrize(
    "ws_url",
    [
        "http://127.0.0.1:55001/devtools/browser/abc",
        "ws://192.168.1.20:55001/devtools/browser/abc",
        "ws://127.0.0.1/devtools/browser/abc",
    ],
)
def test_debug_port_rejects_nonlocal_or_incomplete_ws_urls(ws_url):
    with pytest.raises(ValueError):
        window_tiler.debug_port_from_ws_url(ws_url)


def test_listening_pid_for_port_returns_only_unique_listening_pid(monkeypatch):
    calls = []

    def fake_run(command, **options):
        calls.append((command, options))
        return types.SimpleNamespace(
            stdout=(
                "  TCP    127.0.0.1:55001    0.0.0.0:0       LISTENING       9002\n"
                "  TCP    127.0.0.1:55001    127.0.0.1:61234 ESTABLISHED     7777\n"
                "  TCP    127.0.0.1:55002    0.0.0.0:0       LISTENING       9001\n"
            )
        )

    monkeypatch.setattr(window_tiler, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    assert window_tiler.listening_pid_for_port(55001, run_command=fake_run) == 9002
    assert calls == [
        (
            ["netstat", "-ano", "-p", "tcp"],
            {
                "capture_output": True,
                "text": True,
                "check": False,
                "creationflags": 0x08000000,
            },
        )
    ]


def test_listening_pid_for_port_rejects_multiple_listener_pids(monkeypatch):
    def fake_run(_command, **_options):
        return types.SimpleNamespace(
            stdout=(
                "  TCP    127.0.0.1:55001    0.0.0.0:0       LISTENING       9001\n"
                "  TCP    [::1]:55001        [::]:0          LISTENING       9002\n"
            )
        )

    monkeypatch.setattr(window_tiler, "os", types.SimpleNamespace(name="nt"))

    assert window_tiler.listening_pid_for_port(55001, run_command=fake_run) is None


def test_listening_pid_for_port_rejects_partial_output_from_failed_netstat(monkeypatch):
    def fake_run(_command, **_options):
        return types.SimpleNamespace(
            returncode=1,
            stdout="  TCP    127.0.0.1:55001    0.0.0.0:0       LISTENING       9001\n",
        )

    monkeypatch.setattr(window_tiler, "os", types.SimpleNamespace(name="nt"))

    assert window_tiler.listening_pid_for_port(55001, run_command=fake_run) is None


def test_listening_pid_for_port_safely_degrades_off_windows(monkeypatch):
    monkeypatch.setattr(window_tiler, "os", types.SimpleNamespace(name="posix"))

    assert window_tiler.listening_pid_for_port(
        55001,
        run_command=lambda *_args, **_kwargs: pytest.fail("netstat must not run"),
    ) is None


def test_process_family_safely_degrades_off_windows(monkeypatch):
    monkeypatch.setattr(window_tiler, "os", types.SimpleNamespace(name="posix"))

    assert window_tiler.process_family(9001) == {9001}


def test_process_family_discards_partial_snapshot_after_enumeration_error(monkeypatch):
    closed_handles = []

    class FakeFunction:
        def __init__(self, implementation):
            self.implementation = implementation

        def __call__(self, *args):
            return self.implementation(*args)

    def first_process(_snapshot, entry_pointer):
        entry_pointer._obj.th32ProcessID = 9002
        entry_pointer._obj.th32ParentProcessID = 9001
        return True

    kernel32 = types.SimpleNamespace(
        CreateToolhelp32Snapshot=FakeFunction(lambda _flags, _pid: 123),
        Process32FirstW=FakeFunction(first_process),
        Process32NextW=FakeFunction(lambda _snapshot, _entry_pointer: False),
        GetLastError=FakeFunction(lambda: 5),
        CloseHandle=FakeFunction(lambda handle: closed_handles.append(handle) or True),
    )
    monkeypatch.setattr(window_tiler, "os", types.SimpleNamespace(name="nt"))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: kernel32, raising=False)

    assert window_tiler.process_family(9001) == {9001}
    assert closed_handles == [123]


def test_find_browser_windows_maps_each_profile_by_debug_port_pid(monkeypatch):
    windows = [
        window_tiler.BrowserWindow(101, "TikTok - SunBrowser", 9001, "sunbrowser.exe"),
        window_tiler.BrowserWindow(102, "TikTok - SunBrowser", 9002, "sunbrowser.exe"),
    ]
    monkeypatch.setattr(window_tiler, "list_visible_windows", lambda: windows)
    monkeypatch.setattr(
        window_tiler,
        "listening_pid_for_port",
        lambda port: {55001: 9002, 55002: 9001}[port],
    )
    monkeypatch.setattr(window_tiler, "process_family", lambda pid: {pid})

    matched, missing = window_tiler.find_browser_windows(
        [
            {"profile_id": "a", "ws_puppeteer": "ws://127.0.0.1:55001/devtools/browser/a"},
            {"profile_id": "b", "ws_puppeteer": "ws://127.0.0.1:55002/devtools/browser/b"},
        ],
        timeout=0,
    )

    assert [item.hwnd for item in matched] == [102, 101]
    assert missing == []


def test_find_browser_windows_rejects_ambiguous_process_family_candidates(monkeypatch):
    windows = [
        window_tiler.BrowserWindow(101, "TikTok - SunBrowser", 9001, "sunbrowser.exe"),
        window_tiler.BrowserWindow(102, "TikTok - SunBrowser", 9002, "sunbrowser.exe"),
    ]
    monkeypatch.setattr(window_tiler, "list_visible_windows", lambda: windows)
    monkeypatch.setattr(window_tiler, "listening_pid_for_port", lambda _port: 9000)
    monkeypatch.setattr(window_tiler, "process_family", lambda _pid: {9000, 9001, 9002})

    matched, missing = window_tiler.find_browser_windows(
        [{"profile_id": "a", "ws_puppeteer": "ws://127.0.0.1:55001/devtools/browser/a"}],
        timeout=0,
    )

    assert matched == []
    assert missing == ["a: window mapping ambiguous"]


def test_find_browser_windows_rejects_ambiguous_unmapped_candidates(monkeypatch):
    monkeypatch.setattr(
        window_tiler,
        "list_visible_windows",
        lambda: [
            window_tiler.BrowserWindow(101, "SunBrowser", 1, "sunbrowser.exe"),
            window_tiler.BrowserWindow(102, "SunBrowser", 2, "sunbrowser.exe"),
        ],
    )
    monkeypatch.setattr(window_tiler, "listening_pid_for_port", lambda _port: None)

    matched, missing = window_tiler.find_browser_windows(
        [{"profile_id": "a", "ws_puppeteer": "ws://127.0.0.1:55001/devtools/browser/a"}],
        timeout=0,
    )

    assert matched == []
    assert missing == ["a: window mapping ambiguous"]


def test_find_browser_windows_keeps_ambiguous_result_fail_closed_across_timeout(monkeypatch):
    snapshots = [
        [
            window_tiler.BrowserWindow(101, "SunBrowser", 1, "sunbrowser.exe"),
            window_tiler.BrowserWindow(102, "SunBrowser", 2, "sunbrowser.exe"),
        ],
        [window_tiler.BrowserWindow(101, "SunBrowser", 1, "sunbrowser.exe")],
    ]
    monotonic_values = iter([0.0, 0.1])
    monkeypatch.setattr(window_tiler, "list_visible_windows", lambda: snapshots.pop(0))
    monkeypatch.setattr(window_tiler, "listening_pid_for_port", lambda _port: None)
    monkeypatch.setattr(window_tiler.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(window_tiler.time, "sleep", lambda _seconds: None)

    matched, missing = window_tiler.find_browser_windows(
        [{"profile_id": "a", "ws_puppeteer": "ws://127.0.0.1:55001/devtools/browser/a"}],
        timeout=1,
    )

    assert matched == []
    assert missing == ["a: window mapping ambiguous"]


def _install_win32_mocks(monkeypatch):
    calls = []
    win32con = types.SimpleNamespace(
        HWND_TOP=0,
        HWND_TOPMOST=-1,
        SWP_NOACTIVATE=0x0010,
        SWP_NOZORDER=0x0004,
        SWP_SHOWWINDOW=0x0040,
        SW_RESTORE=9,
    )
    rects = {}

    def set_window_pos(hwnd, insert_after, x, y, width, height, flags):
        rects[hwnd] = (x, y, x + width, y + height)
        calls.append(
            ("position", hwnd, x, y, width, height, flags, insert_after)
        )

    win32gui = types.SimpleNamespace(
        ShowWindow=lambda hwnd, command: calls.append(("restore", hwnd, command)),
        SetWindowPos=set_window_pos,
        GetWindowRect=lambda hwnd: rects[hwnd],
        BringWindowToTop=lambda hwnd: calls.append(("bring_to_top", hwnd)),
        SetForegroundWindow=lambda hwnd: calls.append(("set_foreground", hwnd)),
    )
    monkeypatch.setitem(sys.modules, "win32con", win32con)
    monkeypatch.setitem(sys.modules, "win32gui", win32gui)
    return calls


def _overlaps(a, b):
    return not (
        a[2] <= b[0]
        or b[2] <= a[0]
        or a[3] <= b[1]
        or b[3] <= a[1]
    )


def test_scale_browser_page_suppresses_websocket_origin_for_adspower(monkeypatch):
    calls = []

    def fake_create_connection(url, **options):
        calls.append((url, options))
        raise RuntimeError("stop after handshake options")

    monkeypatch.setitem(
        sys.modules,
        "websocket",
        types.SimpleNamespace(create_connection=fake_create_connection),
    )

    with pytest.raises(RuntimeError, match="stop after handshake options"):
        window_tiler.scale_browser_page("ws://127.0.0.1:50001/devtools/browser/test", 0.75)

    assert calls == [
        (
            "ws://127.0.0.1:50001/devtools/browser/test",
            {"timeout": 5, "suppress_origin": True},
        )
    ]


@pytest.mark.parametrize("count", range(1, 9))
def test_build_layout_cells_evenly_tiles_work_area_for_counts_one_to_eight(count):
    width = 1600
    height = 900

    cells = window_tiler.build_layout_cells(count, width, height)

    assert len(cells) == count
    assert all(len(cell) == 4 for cell in cells)
    assert all(0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height for x1, y1, x2, y2 in cells)

    areas = []
    for index, cell in enumerate(cells):
        x1, y1, x2, y2 = cell
        areas.append((x2 - x1) * (y2 - y1))
        for other in cells[index + 1 :]:
            assert not _overlaps(cell, other)

    assert sum(areas) == width * height
    assert max(areas) - min(areas) <= max(width, height)


def test_build_layout_cells_keeps_two_windows_as_left_right_halves_in_portrait():
    cells = window_tiler.build_layout_cells(2, 900, 1600)

    assert cells == [
        (0, 0, 450, 1600),
        (450, 0, 900, 1600),
    ]


def test_build_layout_cells_keeps_four_windows_as_quadrants_in_portrait():
    cells = window_tiler.build_layout_cells(4, 900, 1600)

    assert cells == [
        (0, 0, 450, 800),
        (450, 0, 900, 800),
        (0, 800, 450, 1600),
        (450, 800, 900, 1600),
    ]


@pytest.mark.parametrize(
    ("count", "work_area", "expected_rows", "expected_columns"),
    [
        (2, (0, 0, 1600, 900), 1, 2),
        (2, (0, 0, 900, 1600), 1, 2),
        (4, (0, 0, 1600, 900), 2, 2),
        (4, (0, 0, 900, 1600), 2, 2),
    ],
)
def test_tile_browser_windows_reports_fixed_special_layout_metadata(
    monkeypatch,
    count,
    work_area,
    expected_rows,
    expected_columns,
):
    _install_win32_mocks(monkeypatch)
    windows = [
        window_tiler.BrowserWindow(100 + index, f"profile-{index}")
        for index in range(1, count + 1)
    ]
    hints = [
        {
            "profile_id": f"profile-{index}",
            "ws_puppeteer": f"ws-{index}",
        }
        for index in range(1, count + 1)
    ]
    monkeypatch.setattr(
        window_tiler,
        "find_browser_windows",
        lambda _hints: (windows, []),
    )
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: work_area)
    monkeypatch.setattr(
        window_tiler,
        "scale_browser_page",
        lambda _ws_url, _scale: None,
    )

    result = window_tiler.tile_browser_windows(hints)

    assert result["rows"] == expected_rows
    assert result["columns"] == expected_columns


def test_activate_window_retries_with_temporary_thread_input_attachment():
    calls = []
    attempts = {"count": 0}

    def set_foreground(hwnd):
        attempts["count"] += 1
        calls.append(("foreground", hwnd, attempts["count"]))
        if attempts["count"] == 1:
            raise RuntimeError("foreground denied")

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda hwnd: calls.append(("top", hwnd)),
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda hwnd, _pid: {999: 11, 101: 22}[hwnd],
        AttachThreadInput=lambda source, target, attach: calls.append(
            ("attach", source, target, bool(attach))
        )
        or 1,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == "ok-after-retry"
    assert ("attach", 33, 11, True) in calls
    assert ("attach", 33, 11, False) in calls
    assert attempts["count"] == 2


def test_activate_window_handles_missing_foreground_window_without_raising():
    calls = []

    def fail_foreground(_hwnd):
        raise RuntimeError("foreground denied")

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda _hwnd: None,
        SetForegroundWindow=fail_foreground,
        GetForegroundWindow=lambda: 0,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda *_args: calls.append("thread-id") or 11,
        AttachThreadInput=lambda *_args: calls.append("attach") or 1,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == (
        "failed: foreground denied; retry failed: no foreground window"
    )
    assert calls == []


def test_activate_window_retries_without_attaching_same_thread():
    calls = []
    attempts = {"count": 0}

    def set_foreground(_hwnd):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("foreground denied")

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda _hwnd: None,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd, _pid: 33,
        AttachThreadInput=lambda *_args: calls.append("attach") or 1,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == "ok-after-retry"
    assert attempts["count"] == 2
    assert calls == []


def test_activate_window_records_thread_input_attachment_failure_without_retry():
    calls = []
    attempts = {"count": 0}

    def set_foreground(_hwnd):
        attempts["count"] += 1
        raise RuntimeError("foreground denied")

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda _hwnd: None,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd, _pid: 11,
        AttachThreadInput=lambda source, target, attach: calls.append(
            ("attach", source, target, bool(attach))
        )
        or 0,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == (
        "failed: foreground denied; retry failed: AttachThreadInput failed"
    )
    assert attempts["count"] == 1
    assert calls == [("attach", 33, 11, True)]


def test_activate_window_detaches_thread_input_when_retry_fails():
    calls = []
    attempts = {"count": 0}

    def set_foreground(_hwnd):
        attempts["count"] += 1
        raise RuntimeError(f"foreground denied {attempts['count']}")

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda _hwnd: None,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd, _pid: 11,
        AttachThreadInput=lambda source, target, attach: calls.append(
            ("attach", source, target, bool(attach))
        )
        or 1,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == (
        "failed: foreground denied 1; retry failed: foreground denied 2"
    )
    assert attempts["count"] == 2
    assert calls == [
        ("attach", 33, 11, True),
        ("attach", 33, 11, False),
    ]


def test_activate_window_records_detach_failure_without_raising():
    calls = []
    attempts = {"count": 0}

    def set_foreground(_hwnd):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("foreground denied")

    def attach_thread_input(source, target, attach):
        calls.append(("attach", source, target, bool(attach)))
        if not attach:
            raise RuntimeError("detach denied")
        return 1

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda _hwnd: None,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd, _pid: 11,
        AttachThreadInput=attach_thread_input,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == (
        "failed: foreground denied; detach failed: detach denied"
    )
    assert attempts["count"] == 2
    assert calls == [
        ("attach", 33, 11, True),
        ("attach", 33, 11, False),
    ]


def test_activate_window_records_false_detach_result_without_raising():
    calls = []
    attempts = {"count": 0}

    def set_foreground(_hwnd):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("foreground denied")

    def attach_thread_input(source, target, attach):
        calls.append(("attach", source, target, bool(attach)))
        return 1 if attach else 0

    win32gui = types.SimpleNamespace(
        BringWindowToTop=lambda _hwnd: None,
        SetForegroundWindow=set_foreground,
        GetForegroundWindow=lambda: 999,
    )
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd, _pid: 11,
        AttachThreadInput=attach_thread_input,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)

    result = window_tiler._activate_window(
        win32gui,
        window_tiler.BrowserWindow(101, "SunBrowser"),
        user32=user32,
        kernel32=kernel32,
    )

    assert result["set_foreground"] == (
        "failed: foreground denied; detach failed: AttachThreadInput failed"
    )
    assert attempts["count"] == 2
    assert calls == [
        ("attach", 33, 11, True),
        ("attach", 33, 11, False),
    ]


def test_tile_browser_windows_uses_requested_count_when_one_window_is_missing(monkeypatch):
    calls = _install_win32_mocks(monkeypatch)
    windows = [
        window_tiler.BrowserWindow(101, "profile-1"),
        window_tiler.BrowserWindow(102, "profile-2"),
        window_tiler.BrowserWindow(103, "profile-3"),
    ]
    hints = [{"profile_id": f"profile-{index}", "ws_puppeteer": f"ws-{index}"} for index in range(1, 5)]
    monkeypatch.setattr(window_tiler, "find_browser_windows", lambda _hints: (windows, ["profile-4"]))
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (0, 0, 1600, 1000))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    result = window_tiler.tile_browser_windows(hints)

    positions = [call for call in calls if call[0] == "position"]
    assert result["requested_count"] == 4
    assert result["matched_count"] == 3
    assert result["columns"] == 2
    assert result["rows"] == 2
    assert result["missing"] == ["profile-4"]
    assert [(item[2], item[3], item[4], item[5]) for item in positions] == [
        (0, 0, 800, 667),
        (800, 0, 800, 667),
        (0, 667, 1600, 333),
    ]


def test_tile_browser_windows_tiles_four_windows_into_four_equal_cells(monkeypatch):
    calls = _install_win32_mocks(monkeypatch)
    windows = [window_tiler.BrowserWindow(100 + index, f"profile-{index}") for index in range(1, 5)]
    hints = [{"profile_id": f"profile-{index}", "ws_puppeteer": f"ws-{index}"} for index in range(1, 5)]
    monkeypatch.setattr(window_tiler, "find_browser_windows", lambda _hints: (windows, []))
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (10, 20, 1610, 1020))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    result = window_tiler.tile_browser_windows(hints)

    positions = [call for call in calls if call[0] == "position"]
    assert result["count"] == 4
    assert [(item[2], item[3], item[4], item[5]) for item in positions] == [
        (10, 20, 800, 500),
        (810, 20, 800, 500),
        (10, 520, 800, 500),
        (810, 520, 800, 500),
    ]
    win32con = sys.modules["win32con"]
    for position_call in positions:
        flags = position_call[-2]
        insert_after = position_call[-1]
        assert flags == win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW
        assert insert_after == win32con.HWND_TOP
        assert (
            not hasattr(win32con, "HWND_TOPMOST")
            or insert_after != win32con.HWND_TOPMOST
        )
    activation_indexes = [
        index
        for index, call in enumerate(calls)
        if call[0] in {"bring_to_top", "set_foreground"}
    ]
    position_indexes = [
        index for index, call in enumerate(calls) if call[0] == "position"
    ]
    assert max(position_indexes) < min(activation_indexes)
    assert [call for call in calls if call[0] == "bring_to_top"] == [
        ("bring_to_top", 101),
        ("bring_to_top", 102),
        ("bring_to_top", 103),
        ("bring_to_top", 104),
    ]
    assert [call for call in calls if call[0] == "set_foreground"] == [
        ("set_foreground", 101),
        ("set_foreground", 102),
        ("set_foreground", 103),
        ("set_foreground", 104),
    ]


def test_tile_browser_windows_restores_foreground_and_reports_rects(monkeypatch):
    calls = _install_win32_mocks(monkeypatch)
    windows = [window_tiler.BrowserWindow(101, "profile-1")]
    hints = [{"profile_id": "profile-1", "ws_puppeteer": "ws-1"}]
    monkeypatch.setattr(window_tiler, "find_browser_windows", lambda _hints: (windows, []))
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    result = window_tiler.tile_browser_windows(hints)

    position_call = next(call for call in calls if call[0] == "position")
    assert position_call[-2] & sys.modules["win32con"].SWP_NOZORDER == 0
    assert ("restore", 101, sys.modules["win32con"].SW_RESTORE) in calls
    assert ("bring_to_top", 101) in calls
    assert ("set_foreground", 101) in calls
    assert calls.count(("bring_to_top", 101)) == 1
    assert calls.count(("set_foreground", 101)) == 1
    assert calls.index(position_call) < calls.index(("bring_to_top", 101))
    assert calls.index(("bring_to_top", 101)) < calls.index(
        ("set_foreground", 101)
    )
    assert result["layout"][0]["target_rect"] == {"left": 0, "top": 0, "right": 1200, "bottom": 800}
    assert result["layout"][0]["actual_rect"] == {"left": 0, "top": 0, "right": 1200, "bottom": 800}
    assert result["layout"][0]["z_order"]["bring_to_top"] == "ok"
    assert result["layout"][0]["z_order"]["set_foreground"] == "ok"
    assert result["layout"][0]["overlap_detected"] is False


def test_tile_browser_windows_keeps_layout_when_detach_returns_false(monkeypatch):
    calls = _install_win32_mocks(monkeypatch)
    win32gui = sys.modules["win32gui"]
    attempts = {"count": 0}

    def set_foreground(hwnd):
        attempts["count"] += 1
        calls.append(("set_foreground", hwnd))
        if attempts["count"] == 1:
            raise RuntimeError("foreground denied")

    win32gui.SetForegroundWindow = set_foreground
    win32gui.GetForegroundWindow = lambda: 999
    user32 = types.SimpleNamespace(
        GetWindowThreadProcessId=lambda _hwnd, _pid: 11,
        AttachThreadInput=lambda _source, _target, attach: 1 if attach else 0,
    )
    kernel32 = types.SimpleNamespace(GetCurrentThreadId=lambda: 33)
    activate_window = window_tiler._activate_window
    monkeypatch.setattr(
        window_tiler,
        "_activate_window",
        lambda api, window: activate_window(
            api,
            window,
            user32=user32,
            kernel32=kernel32,
        ),
    )
    windows = [window_tiler.BrowserWindow(101, "profile-1")]
    hints = [{"profile_id": "profile-1", "ws_puppeteer": "ws-1"}]
    monkeypatch.setattr(
        window_tiler, "find_browser_windows", lambda _hints: (windows, [])
    )
    monkeypatch.setattr(
        window_tiler, "get_work_area", lambda: (0, 0, 1200, 800)
    )
    monkeypatch.setattr(
        window_tiler, "scale_browser_page", lambda _ws_url, _scale: None
    )

    result = window_tiler.tile_browser_windows(hints)

    assert result["count"] == 1
    assert result["missing"] == []
    assert len(result["layout"]) == 1
    assert result["layout"][0]["actual_rect"] == {
        "left": 0,
        "top": 0,
        "right": 1200,
        "bottom": 800,
    }
    assert result["layout"][0]["z_order"]["set_foreground"] == (
        "failed: foreground denied; detach failed: AttachThreadInput failed"
    )


def test_tile_browser_windows_records_z_order_failures_without_losing_layout(monkeypatch):
    calls = _install_win32_mocks(monkeypatch)
    win32gui = sys.modules["win32gui"]

    def fail_top(hwnd):
        calls.append(("bring_to_top", hwnd))
        raise RuntimeError("top denied")

    def fail_foreground(hwnd):
        calls.append(("set_foreground", hwnd))
        raise RuntimeError("foreground denied")

    win32gui.BringWindowToTop = fail_top
    win32gui.SetForegroundWindow = fail_foreground

    windows = [window_tiler.BrowserWindow(101, "profile-1")]
    hints = [{"profile_id": "profile-1", "ws_puppeteer": "ws-1"}]
    monkeypatch.setattr(window_tiler, "find_browser_windows", lambda _hints: (windows, []))
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    result = window_tiler.tile_browser_windows(hints)

    assert result["count"] == 1
    assert result["missing"] == []
    assert result["layout"][0]["z_order"]["bring_to_top"].startswith("failed:")
    assert result["layout"][0]["z_order"]["set_foreground"].startswith("failed:")


def test_tile_browser_windows_reports_rect_verification_failures_with_window_context(monkeypatch):
    _install_win32_mocks(monkeypatch)
    windows = [window_tiler.BrowserWindow(101, "profile-1")]
    hints = [{"profile_id": "profile-1", "ws_puppeteer": "ws-1"}]
    monkeypatch.setattr(window_tiler, "find_browser_windows", lambda _hints: (windows, []))
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    def fail_verified(*_args, **_kwargs):
        raise RuntimeError("window rect verification failed for hwnd=101: expected (0, 0, 1200, 800), got (10, 10, 200, 200)")

    monkeypatch.setattr(window_tiler, "_set_window_position_verified", fail_verified)

    result = window_tiler.tile_browser_windows(hints)

    assert result["count"] == 0
    assert result["layout"] == []
    assert "hwnd=101" in result["missing"][0]
    assert "profile-1" in result["missing"][0]
    assert "window rect verification failed" in result["missing"][0]


def test_tile_browser_windows_flags_actual_rect_overlap(monkeypatch):
    _install_win32_mocks(monkeypatch)
    windows = [
        window_tiler.BrowserWindow(101, "profile-1"),
        window_tiler.BrowserWindow(102, "profile-2"),
    ]
    hints = [{"profile_id": "profile-1", "ws_puppeteer": "ws-1"}, {"profile_id": "profile-2", "ws_puppeteer": "ws-2"}]
    monkeypatch.setattr(window_tiler, "find_browser_windows", lambda _hints: (windows, []))
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    overlap_rects = {
        101: (0, 0, 900, 800),
        102: (300, 0, 1200, 800),
    }

    def overlapping_rects(_win32gui, _win32con, window, x, y, width, height):
        return overlap_rects[window.hwnd]

    monkeypatch.setattr(window_tiler, "_set_window_position_verified", overlapping_rects)

    result = window_tiler.tile_browser_windows(hints)

    assert result["count"] == 2
    assert result["layout"][0]["overlap_detected"] is True
    assert result["layout"][0]["overlaps_with"] == [102]
    assert result["layout"][1]["overlap_detected"] is True
    assert result["layout"][1]["overlaps_with"] == [101]


def test_list_visible_windows_keeps_minimized_windows_for_restore(monkeypatch):
    monkeypatch.setattr(window_tiler.os, "name", "nt", raising=False)

    fake_win32gui = types.SimpleNamespace()
    fake_win32gui.IsWindowVisible = lambda hwnd: True
    fake_win32gui.IsIconic = lambda hwnd: hwnd == 101
    fake_win32gui.GetWindowText = lambda hwnd: {101: "AdsPower Browser minimized", 102: "AdsPower Browser open"}[hwnd]
    fake_win32gui.EnumWindows = lambda callback, extra: [callback(hwnd, extra) for hwnd in (101, 102)]

    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.delitem(sys.modules, "win32api", raising=False)
    monkeypatch.delitem(sys.modules, "win32con", raising=False)
    monkeypatch.delitem(sys.modules, "win32process", raising=False)

    windows = window_tiler.list_visible_windows()

    assert [window.hwnd for window in windows] == [101, 102]


def test_minimized_window_can_be_found_and_restored_during_tiling(monkeypatch):
    calls = _install_win32_mocks(monkeypatch)
    minimized_window = window_tiler.BrowserWindow(101, "AdsPower Browser minimized")
    monkeypatch.setattr(window_tiler, "list_visible_windows", lambda: [minimized_window])
    monkeypatch.setattr(window_tiler, "get_work_area", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(window_tiler, "scale_browser_page", lambda _ws_url, _scale: None)

    result = window_tiler.tile_browser_windows([{"profile_id": "missing", "ws_puppeteer": "ws-1"}])

    assert [window.hwnd for window in window_tiler.find_browser_windows([{"profile_id": "missing"}], timeout=0)[0]] == [101]
    assert ("restore", 101, sys.modules["win32con"].SW_RESTORE) in calls
    assert result["count"] == 1


def test_find_browser_windows_rejects_ambiguous_adspower_windows(monkeypatch):
    available = [
        window_tiler.BrowserWindow(201, "stock_2012_20260717174100.xls - WPS Office"),
        window_tiler.BrowserWindow(202, "C:\\Windows\\system32\\cmd.exe"),
        window_tiler.BrowserWindow(203, "AdsPower Browser | 8.6.3 | 2.8.6.9"),
        window_tiler.BrowserWindow(204, "AdsPower Browser | 8.6.3 | 2.8.6.9"),
    ]
    monkeypatch.setattr(window_tiler, "list_visible_windows", lambda: available)

    matched, missing = window_tiler.find_browser_windows(
        [
            {"profile_id": "profile-1", "profile_no": "2"},
            {"profile_id": "profile-2", "profile_no": "3"},
        ],
        timeout=0,
    )

    assert matched == []
    assert missing == [
        "profile-1: window mapping ambiguous",
        "profile-2: window mapping ambiguous",
    ]
