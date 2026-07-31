import atexit
import json
import math
import queue
import subprocess
import threading
import uuid
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parent
_WORKER_COMMAND = ["node", "browser/ghost-cursor-worker.js"]


class GhostCursorError(RuntimeError):
    pass


class _RequestFailure(Exception):
    pass


class GhostCursorBridge:
    def __init__(
        self,
        *,
        process_factory=subprocess.Popen,
        thread_factory=None,
        id_factory=None,
        response_timeout=5.0,
    ) -> None:
        self._process_factory = process_factory
        self._thread_factory = thread_factory or threading.Thread
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._response_timeout = self._normalize_response_timeout(response_timeout)
        self._lock = threading.RLock()
        self._process = None
        self._responses = None
        self._reader = None

    def generate_path(
        self,
        start: tuple[float, float],
        end: tuple[float, float],
        target: dict[str, float] | None = None,
    ) -> list[dict[str, float]]:
        with self._lock:
            try:
                normalized_start = self._normalize_point(start, "start")
                normalized_end = self._normalize_point(end, "end")
                normalized_target = self._normalize_target(target)
            except GhostCursorError:
                self._stop_worker()
                raise

            final_reason = "unknown worker failure"
            for attempt in range(2):
                try:
                    return self._generate_once(
                        normalized_start,
                        normalized_end,
                        normalized_target,
                    )
                except _RequestFailure as exc:
                    final_reason = str(exc)
                    self._stop_worker()
                    if attempt == 1:
                        raise GhostCursorError(
                            "Ghost Cursor path generation failed after retry: "
                            f"{final_reason}"
                        ) from None
            raise AssertionError("unreachable")

    def close(self) -> None:
        with self._lock:
            self._stop_worker()

    @staticmethod
    def _normalize_float(value, label):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GhostCursorError(f"{label} must be a finite number")
        try:
            normalized = float(value)
        except (TypeError, ValueError, OverflowError):
            raise GhostCursorError(f"{label} must be a finite number") from None
        if not math.isfinite(normalized):
            raise GhostCursorError(f"{label} must be a finite number")
        return normalized

    @classmethod
    def _normalize_point(cls, value, label):
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            raise GhostCursorError(f"{label} must contain two finite numbers")
        return (
            cls._normalize_float(value[0], f"{label}.x"),
            cls._normalize_float(value[1], f"{label}.y"),
        )

    @classmethod
    def _normalize_target(cls, target):
        if target is None:
            return None
        if not isinstance(target, dict):
            raise GhostCursorError("target must contain a finite positive box")
        try:
            x = cls._normalize_float(target["x"], "target.x")
            y = cls._normalize_float(target["y"], "target.y")
            width = cls._normalize_float(target["width"], "target.width")
            height = cls._normalize_float(target["height"], "target.height")
        except KeyError:
            raise GhostCursorError(
                "target must contain a finite positive box"
            ) from None
        if width <= 0 or height <= 0:
            raise GhostCursorError("target dimensions must be positive")
        return {"x": x, "y": y, "width": width, "height": height}

    @classmethod
    def _normalize_response_timeout(cls, response_timeout):
        normalized = cls._normalize_float(
            response_timeout, "response_timeout"
        )
        if normalized <= 0:
            raise GhostCursorError("response_timeout must be positive")
        return normalized

    def _generate_once(self, start, end, target):
        self._ensure_worker()
        request_id = str(self._id_factory())
        request = {
            "id": request_id,
            "start": {"x": start[0], "y": start[1]},
            "end": {"x": end[0], "y": end[1]},
        }
        if target is not None:
            request["target"] = {
                "x": target["x"],
                "y": target["y"],
                "width": target["width"],
                "height": target["height"],
            }

        try:
            payload = json.dumps(request, separators=(",", ":"))
            self._process.stdin.write(payload + "\n")
            self._process.stdin.flush()
        except Exception:
            raise _RequestFailure("worker request could not be written") from None

        try:
            line = self._responses.get(timeout=self._response_timeout)
        except queue.Empty:
            raise _RequestFailure("worker response timed out") from None

        if line is None:
            raise _RequestFailure("worker closed its output")

        try:
            response = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            raise _RequestFailure("worker returned malformed JSON") from None

        return self._validate_response(response, request_id)

    def _ensure_worker(self):
        if self._process is not None and self._process.poll() is None:
            return
        self._stop_worker()

        options = {
            "cwd": str(_PROJECT_ROOT),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "bufsize": 1,
        }
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            options["creationflags"] = subprocess.CREATE_NO_WINDOW

        try:
            process = self._process_factory(_WORKER_COMMAND, **options)
        except Exception:
            raise _RequestFailure("worker could not be started") from None

        if process.stdin is None or process.stdout is None:
            try:
                self._terminate_process(process)
            finally:
                self._cleanup_resources(process, None)
            raise _RequestFailure("worker pipes were unavailable")

        responses = queue.Queue()
        reader = None
        try:
            reader = self._thread_factory(
                target=self._read_stdout,
                args=(process.stdout, responses),
                name="ghost-cursor-reader",
                daemon=True,
            )
            reader.start()
        except Exception:
            try:
                self._terminate_process(process)
            finally:
                self._cleanup_resources(process, reader)
            raise _RequestFailure("worker reader could not be started") from None

        self._process = process
        self._responses = responses
        self._reader = reader

    @staticmethod
    def _read_stdout(stdout, responses):
        try:
            while True:
                line = stdout.readline()
                if line == "":
                    break
                responses.put(line)
        finally:
            responses.put(None)

    @staticmethod
    def _validate_response(response, request_id):
        if not isinstance(response, dict):
            raise _RequestFailure("worker response was not an object")
        if response.get("id") != request_id:
            raise _RequestFailure("worker response ID did not match")
        if "error" in response:
            raise _RequestFailure("worker rejected the path request")

        points = response.get("points")
        if not isinstance(points, list) or len(points) < 2:
            raise _RequestFailure("worker returned too few path points")

        normalized = []
        for point in points:
            if not isinstance(point, dict):
                raise _RequestFailure("worker returned an invalid path point")
            x = point.get("x")
            y = point.get("y")
            if (
                isinstance(x, bool)
                or isinstance(y, bool)
                or not isinstance(x, (int, float))
                or not isinstance(y, (int, float))
            ):
                raise _RequestFailure("worker returned an invalid path point")
            try:
                normalized_x = float(x)
                normalized_y = float(y)
            except (TypeError, ValueError, OverflowError):
                raise _RequestFailure("worker returned an invalid path point") from None
            if not math.isfinite(normalized_x) or not math.isfinite(normalized_y):
                raise _RequestFailure("worker returned an invalid path point")
            normalized.append({"x": normalized_x, "y": normalized_y})
        return normalized

    def _stop_worker(self):
        process = self._process
        reader = self._reader
        self._process = None
        self._responses = None
        self._reader = None
        if process is not None:
            try:
                self._terminate_process(process)
            finally:
                self._cleanup_resources(process, reader)

    @staticmethod
    def _terminate_process(process):
        try:
            returncode = process.poll()
        except Exception:
            returncode = None
        if returncode is not None:
            return

        try:
            process.terminate()
        except Exception:
            pass

        try:
            process.wait(timeout=2)
            return
        except Exception:
            pass

        try:
            process.kill()
        except Exception:
            pass

        try:
            process.wait(timeout=2)
        except Exception:
            pass

    @staticmethod
    def _cleanup_resources(process, reader):
        for pipe_name in ("stdin", "stdout"):
            try:
                pipe = getattr(process, pipe_name, None)
                close = getattr(pipe, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
        if reader is not None and reader is not threading.current_thread():
            try:
                reader.join(timeout=2)
            except Exception:
                pass


_default_bridge = GhostCursorBridge()
atexit.register(_default_bridge.close)


def generate_ghost_path(
    start: tuple[float, float],
    end: tuple[float, float],
    target: dict[str, float] | None = None,
) -> list[dict[str, float]]:
    return _default_bridge.generate_path(start, end, target)
