"""Windows browser window discovery and tiling helpers."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class BrowserWindow:
    hwnd: int
    title: str
    process_id: int | None = None
    process_name: str = ""


def debug_port_from_ws_url(ws_url: str) -> int:
    parsed = urlsplit(str(ws_url or ""))
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError("AdsPower CDP endpoint must use a local ws URL")
    if not parsed.port:
        raise ValueError("AdsPower CDP endpoint has no debug port")
    return int(parsed.port)


def listening_pid_for_port(port: int, *, run_command=subprocess.run) -> int | None:
    if os.name != "nt":
        return None
    try:
        requested_port = int(port)
        completed = run_command(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        return None
    if getattr(completed, "returncode", 0) != 0:
        return None

    matches: set[int] = set()
    for line in str(getattr(completed, "stdout", "") or "").splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[-2].casefold() != "listening" or not fields[-1].isdigit():
            continue
        try:
            local_port = int(fields[1].rsplit(":", 1)[1])
            process_id = int(fields[-1])
        except (IndexError, ValueError):
            continue
        if local_port == requested_port and process_id > 0:
            matches.add(process_id)
    return next(iter(matches)) if len(matches) == 1 else None


def process_family(root_pid: int) -> set[int]:
    root = int(root_pid)
    if os.name != "nt":
        return {root}

    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snapshot = None
    invalid_handle = ctypes.c_void_p(-1).value
    kernel32 = None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESSENTRY32W),
        ]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.GetLastError.argtypes = []
        kernel32.GetLastError.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot is None or snapshot == invalid_handle:
            snapshot = None
            return {root}

        children: dict[int, set[int]] = {}
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        available = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while available:
            parent = int(entry.th32ParentProcessID)
            children.setdefault(parent, set()).add(int(entry.th32ProcessID))
            if kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                continue
            if kernel32.GetLastError() != 18:
                return {root}
            available = False
    except (AttributeError, OSError, TypeError, ValueError):
        return {root}
    finally:
        if snapshot is not None and kernel32 is not None:
            try:
                kernel32.CloseHandle(snapshot)
            except (AttributeError, OSError, TypeError, ValueError):
                pass

    family = {root}
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), set()):
            if child not in family:
                family.add(child)
                pending.append(child)
    return family


def _best_row_groups(count: int, width: int, height: int) -> list[int]:
    if count <= 0:
        return []
    ideal_columns = math.sqrt(count * width / height)
    ideal_rows = math.sqrt(count * height / width)
    best_groups: list[int] | None = None
    best_score: tuple[float, float, int, tuple[int, ...]] | None = None

    for row_count in range(1, count + 1):
        base, remainder = divmod(count, row_count)
        groups = [base + (1 if index < remainder else 0) for index in range(row_count)]
        groups = [group for group in groups if group > 0]
        columns = max(groups)
        empty_slots = row_count * columns - count
        score = (
            abs(math.log(columns / ideal_columns)) + abs(math.log(row_count / ideal_rows)),
            empty_slots,
            max(groups) - min(groups),
            tuple(groups),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_groups = groups

    return best_groups or []


def build_layout_cells(count: int, width: int, height: int) -> list[tuple[int, int, int, int]]:
    if count <= 0:
        raise ValueError("count must be positive")
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive")
    if count == 2:
        middle = round(width / 2)
        return [(0, 0, middle, height), (middle, 0, width, height)]
    if count == 4:
        middle_x = round(width / 2)
        middle_y = round(height / 2)
        return [
            (0, 0, middle_x, middle_y),
            (middle_x, 0, width, middle_y),
            (0, middle_y, middle_x, height),
            (middle_x, middle_y, width, height),
        ]

    groups = _best_row_groups(count, width, height)
    cells: list[tuple[int, int, int, int]] = []
    assigned = 0
    top = 0

    for group in groups:
        next_assigned = assigned + group
        bottom = round(height * next_assigned / count)
        left = 0
        for column in range(group):
            right = round(width * (column + 1) / group)
            cells.append((left, top, right, bottom))
            left = right
        top = bottom
        assigned = next_assigned

    return cells


def page_scale_for_count(count: int) -> float:
    if count <= 1:
        return 1.0
    if count <= 2:
        return 0.85
    if count <= 4:
        return 0.75
    if count <= 6:
        return 0.65
    return 0.55


def scale_browser_page(ws_url: str, scale: float) -> None:
    if not ws_url:
        raise ValueError("missing ws.puppeteer endpoint")
    if not 0.4 <= scale <= 1.0:
        raise ValueError("scale must stay within 0.4..1.0")
    from websocket import create_connection

    websocket = create_connection(ws_url, timeout=5, suppress_origin=True)
    message_id = 0

    def command(method: str, params: dict | None = None, session_id: str | None = None):
        nonlocal message_id
        message_id += 1
        payload = {"id": message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        websocket.send(json.dumps(payload))
        while True:
            response = json.loads(websocket.recv())
            if response.get("id") == message_id:
                if response.get("error"):
                    raise RuntimeError(response["error"].get("message", "CDP call failed"))
                return response.get("result", {})

    try:
        targets = command("Target.getTargets").get("targetInfos", [])
        pages = [target for target in targets if target.get("type") == "page"]
        if not pages:
            raise RuntimeError("no page target available for zoom")
        for target in pages:
            attached = command(
                "Target.attachToTarget",
                {"targetId": target["targetId"], "flatten": True},
            )
            session_id = attached.get("sessionId")
            if not session_id:
                continue
            command(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": f"document.documentElement.style.zoom = '{scale:.2f}';"},
                session_id,
            )
            command(
                "Runtime.evaluate",
                {"expression": f"document.documentElement.style.zoom = '{scale:.2f}'; void 0", "returnByValue": True},
                session_id,
            )
    finally:
        websocket.close()


def get_work_area() -> tuple[int, int, int, int]:
    if os.name != "nt":
        raise RuntimeError("window tiling is only supported on Windows")
    import ctypes

    class Rect(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    rect = Rect()
    if not ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
        raise RuntimeError("unable to read Windows work area")
    return rect.left, rect.top, rect.right, rect.bottom


def list_visible_windows() -> list[BrowserWindow]:
    if os.name != "nt":
        raise RuntimeError("window tiling is only supported on Windows")
    import win32gui

    try:
        import win32api
        import win32con
        import win32process
    except ImportError:  # pragma: no cover
        win32api = win32con = win32process = None

    def process_details(hwnd: int) -> tuple[int | None, str]:
        if win32process is None:
            return None, ""
        process_id = None
        handle = None
        try:
            _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
            if win32api is None or win32con is None:
                return process_id, ""
            access = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ
            handle = win32api.OpenProcess(access, False, process_id)
            return process_id, win32process.GetModuleFileNameEx(handle, 0)
        except Exception:
            return process_id, ""
        finally:
            if handle is not None and win32api is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:
                    pass

    windows: list[BrowserWindow] = []

    def collect(hwnd, _extra):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd).strip()
            if title:
                process_id, process_name = process_details(hwnd)
                windows.append(BrowserWindow(hwnd, title, process_id, process_name))

    win32gui.EnumWindows(collect, None)
    return windows


def _matches(window: BrowserWindow, hint: dict[str, str]) -> bool:
    title = window.title.casefold()
    for key in ("profile_id", "name"):
        token = hint.get(key, "").strip().casefold()
        if token and token in title:
            return True
    profile_no = hint.get("profile_no", "").strip().casefold()
    if profile_no:
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(profile_no)}(?![a-z0-9])", title))
    return False


def _is_browser_window(window: BrowserWindow) -> bool:
    title = window.title.casefold()
    process_name = (window.process_name or "").casefold()
    return any(
        marker in title or marker in process_name
        for marker in ("adspower", "sunbrowser", "chromium", "chrome")
    )


def _is_adspower_window(window: BrowserWindow) -> bool:
    return "adspower" in window.title.casefold() or "adspower" in (window.process_name or "").casefold()


def _precise_candidate(
    hint: dict[str, str],
    available: list[BrowserWindow],
    used: set[int],
) -> tuple[BrowserWindow | None, str, bool]:
    remaining = [item for item in available if item.hwnd not in used]
    try:
        port = debug_port_from_ws_url(hint.get("ws_puppeteer", ""))
        root_pid = listening_pid_for_port(port)
    except (OSError, TypeError, ValueError):
        root_pid = None
    if root_pid:
        family = process_family(root_pid)
        exact = [item for item in remaining if item.process_id in family]
        if len(exact) == 1:
            return exact[0], "", False
        if len(exact) > 1:
            return None, "window mapping ambiguous", True
    titled = [item for item in remaining if _matches(item, hint)]
    if len(titled) == 1:
        return titled[0], "", False
    if len(titled) > 1:
        return None, "window mapping ambiguous", True
    if len(remaining) == 1:
        return remaining[0], "", False
    return None, "window mapping ambiguous", len(remaining) > 1


def find_browser_windows(hints: list[dict[str, str]], timeout: float = 15.0) -> tuple[list[BrowserWindow], list[str]]:
    deadline = time.monotonic() + timeout
    while True:
        all_windows = list_visible_windows()
        available = [window for window in all_windows if _is_browser_window(window)]
        matched: list[BrowserWindow] = []
        missing: list[str] = []
        used: set[int] = set()
        terminal_ambiguity = False
        for hint in hints:
            window, error, ambiguous = _precise_candidate(hint, available, used)
            if window:
                matched.append(window)
                used.add(window.hwnd)
            else:
                label = hint.get("profile_id") or hint.get("name") or "unknown-window"
                missing.append(f"{label}: {error}")
                terminal_ambiguity = terminal_ambiguity or ambiguous
        if terminal_ambiguity or not missing or time.monotonic() >= deadline:
            return matched, missing
        time.sleep(0.5)


def _set_window_position_verified(
    win32gui,
    win32con,
    window: BrowserWindow,
    x: int,
    y: int,
    width: int,
    height: int,
) -> tuple[int, int, int, int]:
    for attempt in range(2):
        win32gui.SetWindowPos(
            window.hwnd,
            win32con.HWND_TOP,
            x,
            y,
            width,
            height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        get_rect = getattr(win32gui, "GetWindowRect", None)
        if not callable(get_rect):
            return x, y, x + width, y + height
        actual = tuple(get_rect(window.hwnd))
        if actual == (x, y, x + width, y + height):
            return actual
        if attempt == 0:
            time.sleep(0.05)
    raise RuntimeError(
        f"window rect verification failed for hwnd={window.hwnd}: expected {(x, y, x + width, y + height)}, got {actual}"
    )


def _activate_window(
    win32gui,
    window: BrowserWindow,
    *,
    user32=None,
    kernel32=None,
) -> dict[str, str]:
    result: dict[str, str] = {}
    bring_to_top = getattr(win32gui, "BringWindowToTop", None)
    if callable(bring_to_top):
        try:
            bring_to_top(window.hwnd)
            result["bring_to_top"] = "ok"
        except Exception as exc:
            result["bring_to_top"] = f"failed: {exc}"
    else:
        result["bring_to_top"] = "unsupported"

    set_foreground = getattr(win32gui, "SetForegroundWindow", None)
    if not callable(set_foreground):
        result["set_foreground"] = "unsupported"
        return result
    try:
        set_foreground(window.hwnd)
        result["set_foreground"] = "ok"
        return result
    except Exception as first_error:
        attached = False
        try:
            import ctypes

            user32 = user32 or ctypes.windll.user32
            kernel32 = kernel32 or ctypes.windll.kernel32
            foreground = win32gui.GetForegroundWindow()
            if not foreground:
                raise RuntimeError("no foreground window")
            foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
            current_thread = kernel32.GetCurrentThreadId()
            if current_thread != foreground_thread:
                attached = bool(
                    user32.AttachThreadInput(current_thread, foreground_thread, True)
                )
                if not attached:
                    raise RuntimeError("AttachThreadInput failed")
            if callable(bring_to_top):
                bring_to_top(window.hwnd)
            set_foreground(window.hwnd)
            result["set_foreground"] = "ok-after-retry"
        except Exception as retry_error:
            result["set_foreground"] = (
                f"failed: {first_error}; retry failed: {retry_error}"
            )
        finally:
            if attached:
                try:
                    detached = bool(
                        user32.AttachThreadInput(
                            current_thread, foreground_thread, False
                        )
                    )
                    if not detached:
                        raise RuntimeError("AttachThreadInput failed")
                except Exception as detach_error:
                    if result.get("set_foreground") == "ok-after-retry":
                        result["set_foreground"] = (
                            f"failed: {first_error}; detach failed: {detach_error}"
                        )
                    else:
                        result["set_foreground"] += (
                            f"; detach failed: {detach_error}"
                        )
    return result


def _rect_to_dict(rect: tuple[int, int, int, int]) -> dict[str, int]:
    return {"left": rect[0], "top": rect[1], "right": rect[2], "bottom": rect[3]}


def _rectangles_overlap(first: tuple[int, int, int, int], second: tuple[int, int, int, int]) -> bool:
    return not (
        first[2] <= second[0]
        or second[2] <= first[0]
        or first[3] <= second[1]
        or second[3] <= first[1]
    )


def tile_browser_windows(hints: list[dict[str, str]]) -> dict:
    if not 1 <= len(hints) <= 8:
        raise ValueError("can only tile 1..8 browser windows at a time")

    import win32con
    import win32gui

    windows, missing = find_browser_windows(hints)
    left, top, right, bottom = get_work_area()
    width = right - left
    height = bottom - top
    requested_count = len(hints)
    groups = _best_row_groups(len(windows), width, height)
    cells = build_layout_cells(len(windows), width, height) if windows else []
    if len(windows) == 2:
        columns, rows = 2, 1
    elif len(windows) == 4:
        columns, rows = 2, 2
    else:
        columns = max(groups, default=0)
        rows = len(groups)
    layout = []
    positioned = []

    for window, cell in zip(windows, cells):
        cell_left, cell_top, cell_right, cell_bottom = cell
        x = left + cell_left
        y = top + cell_top
        current_width = cell_right - cell_left
        current_height = cell_bottom - cell_top
        target_rect = (x, y, x + current_width, y + current_height)
        try:
            win32gui.ShowWindow(window.hwnd, win32con.SW_RESTORE)
            actual_rect = _set_window_position_verified(
                win32gui,
                win32con,
                window,
                x,
                y,
                current_width,
                current_height,
            )
            item = {
                "hwnd": window.hwnd,
                "title": window.title,
                "x": x,
                "y": y,
                "width": current_width,
                "height": current_height,
                "target_rect": _rect_to_dict(target_rect),
                "actual_rect": _rect_to_dict(actual_rect),
            }
            layout.append(item)
            positioned.append((window, item))
        except Exception as exc:
            missing.append(f"hwnd={window.hwnd} title={window.title}: move failed: {exc}")

    for window, item in positioned:
        item["z_order"] = _activate_window(win32gui, window)

    for item in layout:
        rect = item["actual_rect"]
        current_rect = (rect["left"], rect["top"], rect["right"], rect["bottom"])
        overlaps = []
        for other in layout:
            if other["hwnd"] == item["hwnd"]:
                continue
            other_rect = other["actual_rect"]
            if _rectangles_overlap(
                current_rect,
                (other_rect["left"], other_rect["top"], other_rect["right"], other_rect["bottom"]),
            ):
                overlaps.append(other["hwnd"])
        item["overlap_detected"] = bool(overlaps)
        item["overlaps_with"] = overlaps

    scale = page_scale_for_count(len(windows) or len(hints))
    scale_results = []
    for hint in hints:
        ws_url = str(hint.get("ws_puppeteer") or "")
        try:
            scale_browser_page(ws_url, scale)
            scale_results.append({"profile_id": hint.get("profile_id", ""), "scale": scale, "status": "scaled"})
        except Exception as exc:
            scale_results.append(
                {"profile_id": hint.get("profile_id", ""), "scale": scale, "status": "failed", "error": str(exc)}
            )

    return {
        "count": len(layout),
        "requested_count": requested_count,
        "matched_count": len(windows),
        "columns": columns,
        "rows": rows,
        "work_area": {"left": left, "top": top, "width": width, "height": height},
        "layout": layout,
        "missing": missing,
        "page_scale": scale,
        "scale_results": scale_results,
    }
