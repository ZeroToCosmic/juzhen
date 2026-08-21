import json
import queue
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from ghost_cursor_bridge import (
    GhostCursorBridge,
    GhostCursorError,
    generate_ghost_path,
)


class FakeStdout:
    def __init__(self):
        self._lines = queue.Queue()
        self._closed = False

    def readline(self):
        line = self._lines.get()
        return "" if line is None else line

    def feed(self, value):
        self._lines.put(
            value if isinstance(value, str) else json.dumps(value, allow_nan=True)
        )

    def close(self):
        if not self._closed:
            self._closed = True
            self._lines.put(None)


class FakeStdin:
    def __init__(self, process, responder):
        self._process = process
        self._responder = responder

    def write(self, data):
        request = json.loads(data)
        self._process.requests.append(request)
        self._responder(request, self._process)
        return len(data)

    def flush(self):
        return None


class FakeProcess:
    def __init__(self, responder):
        self.stdout = FakeStdout()
        self.stdin = FakeStdin(self, responder)
        self.requests = []
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        self.stdout.close()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.close()

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode


class ProcessFactory:
    def __init__(self, responders):
        self._responders = iter(responders)
        self.processes = []
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        process = FakeProcess(next(self._responders))
        self.processes.append(process)
        return process


class TrackingPipe:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.close_calls = 0

    def close(self):
        self.close_calls += 1
        if self.raises:
            raise RuntimeError("pipe close failed")


class TrackingReader:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.join_calls = []

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        if self.raises:
            raise RuntimeError("reader join failed")


class CleanupProcess:
    def __init__(self, mode):
        resource_errors = mode == "resource_error"
        self.stdin = TrackingPipe(raises=resource_errors)
        self.stdout = TrackingPipe()
        self.mode = mode
        self.poll_calls = 0
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        self.poll_calls += 1
        if self.mode == "poll_error":
            raise RuntimeError("poll failed")
        return None

    def terminate(self):
        self.terminate_calls += 1
        if self.mode == "terminate_error":
            raise RuntimeError("terminate failed")

    def kill(self):
        self.kill_calls += 1
        if self.mode == "kill_error":
            raise RuntimeError("kill failed")

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.mode in {
            "bounded",
            "kill_error",
            "poll_error",
            "terminate_error",
            "wait_error",
        }:
            if len(self.wait_calls) == 1:
                if self.mode == "wait_error":
                    raise RuntimeError("wait failed")
                raise subprocess.TimeoutExpired("node", timeout)
            if self.mode == "wait_error":
                raise RuntimeError("wait failed")
        return -15


def successful_response(points):
    def respond(request, process):
        process.stdout.feed({"id": request["id"], "points": points})

    return respond


def test_public_path_methods_expose_the_documented_type_contract():
    expected = {
        "start": tuple[float, float],
        "end": tuple[float, float],
        "target": dict[str, float] | None,
        "return": list[dict[str, float]],
    }

    assert GhostCursorBridge.generate_path.__annotations__ == expected
    assert generate_ghost_path.__annotations__ == expected


def test_successful_calls_reuse_one_process_and_normalize_floats():
    factory = ProcessFactory(
        [successful_response([{"x": 1, "y": 2}, {"x": 3.5, "y": 4}])]
    )
    bridge = GhostCursorBridge(process_factory=factory, id_factory=iter(["a", "b"]).__next__)

    try:
        first = bridge.generate_path((1, 2), (3, 4))
        second = bridge.generate_path((5, 6), (7, 8))
    finally:
        bridge.close()

    assert first == [{"x": 1.0, "y": 2.0}, {"x": 3.5, "y": 4.0}]
    assert second == first
    assert len(factory.processes) == 1
    command, options = factory.calls[0]
    assert command == ["node", "browser/ghost-cursor-worker.js"]
    assert options["stdin"] is subprocess.PIPE
    assert options["stdout"] is subprocess.PIPE
    assert options["stderr"] is subprocess.DEVNULL
    assert options["text"] is True
    assert options["encoding"] == "utf-8"
    assert options["bufsize"] == 1


def test_request_contains_only_allowed_fields_and_serializes_target_box():
    factory = ProcessFactory(
        [successful_response([{"x": 10, "y": 20}, {"x": 30, "y": 40}])]
    )
    bridge = GhostCursorBridge(process_factory=factory, id_factory=lambda: "request-1")

    try:
        bridge.generate_path(
            (1.25, 2.5),
            (90, 100),
            {
                "x": 10,
                "y": 20,
                "width": 30,
                "height": 40,
                "Cookie": "must-not-leak",
                "proxy": "must-not-leak",
            },
        )
    finally:
        bridge.close()

    assert factory.processes[0].requests == [
        {
            "id": "request-1",
            "start": {"x": 1.25, "y": 2.5},
            "end": {"x": 90.0, "y": 100.0},
            "target": {"x": 10.0, "y": 20.0, "width": 30.0, "height": 40.0},
        }
    ]
    request = factory.processes[0].requests[0]
    assert all(
        isinstance(value, float)
        for field in ("start", "end", "target")
        for value in request[field].values()
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ((True, 0), (1, 2)),
        ((0, 0), ("sensitive-coordinate", 2)),
        ((0,), (1, 2)),
        ((0, 0), (1,)),
        ((float("nan"), 0), (1, 2)),
        ((0, 0), (float("inf"), 2)),
        ("01", (1, 2)),
        ((0, 0), {"x": 1, "y": 2}),
    ],
)
def test_direct_input_validation_rejects_malformed_points_without_starting_worker(
    start, end
):
    factory = ProcessFactory([])
    bridge = GhostCursorBridge(process_factory=factory)

    with pytest.raises(GhostCursorError) as exc_info:
        bridge.generate_path(start, end)

    assert factory.calls == []
    assert "sensitive-coordinate" not in str(exc_info.value)


@pytest.mark.parametrize(
    "target",
    [
        {"y": 2, "width": 3, "height": 4},
        {"x": True, "y": 2, "width": 3, "height": 4},
        {"x": "sensitive-target", "y": 2, "width": 3, "height": 4},
        {"x": float("nan"), "y": 2, "width": 3, "height": 4},
        {"x": 1, "y": float("inf"), "width": 3, "height": 4},
        {"x": 1, "y": 2, "width": 0, "height": 4},
        {"x": 1, "y": 2, "width": -1, "height": 4},
        {"x": 1, "y": 2, "width": float("inf"), "height": 4},
        {"x": 1, "y": 2, "width": True, "height": 4},
        {"x": 1, "y": 2, "width": 3},
        {"x": 1, "y": 2, "width": 3, "height": "sensitive-target"},
    ],
)
def test_direct_input_validation_rejects_malformed_target_without_starting_worker(
    target,
):
    factory = ProcessFactory([])
    bridge = GhostCursorBridge(process_factory=factory)

    with pytest.raises(GhostCursorError) as exc_info:
        bridge.generate_path((0, 0), (1, 2), target)

    assert factory.calls == []
    assert "sensitive-target" not in str(exc_info.value)


@pytest.mark.parametrize(
    "response_timeout",
    [0, -1, float("nan"), float("inf"), True, "5", None],
)
def test_response_timeout_must_be_a_positive_finite_float(response_timeout):
    factory = ProcessFactory([])

    with pytest.raises(GhostCursorError):
        GhostCursorBridge(
            process_factory=factory,
            response_timeout=response_timeout,
        )

    assert factory.calls == []


def test_invalid_direct_input_stops_an_already_running_worker():
    factory = ProcessFactory(
        [successful_response([{"x": 0, "y": 0}, {"x": 1, "y": 1}])]
    )
    bridge = GhostCursorBridge(process_factory=factory, id_factory=lambda: "valid")

    try:
        bridge.generate_path((0, 0), (1, 1))
        with pytest.raises(GhostCursorError):
            bridge.generate_path((True, 0), (1, 1))
    finally:
        bridge.close()

    assert len(factory.calls) == 1
    assert factory.processes[0].terminate_calls == 1


def test_mismatched_id_restarts_once_and_retries_with_a_new_id():
    def mismatch(request, process):
        process.stdout.feed({"id": "someone-else", "points": [{}, {}]})

    factory = ProcessFactory(
        [mismatch, successful_response([{"x": 1, "y": 2}, {"x": 8, "y": 9}])]
    )
    bridge = GhostCursorBridge(
        process_factory=factory, id_factory=iter(["first", "second"]).__next__
    )

    try:
        result = bridge.generate_path((1, 2), (8, 9))
    finally:
        bridge.close()

    assert result[-1] == {"x": 8.0, "y": 9.0}
    assert len(factory.processes) == 2
    assert factory.processes[0].terminate_calls == 1
    assert factory.processes[0].requests[0]["id"] == "first"
    assert factory.processes[1].requests[0]["id"] == "second"


@pytest.mark.parametrize("response_kind", ["eof", "malformed"])
def test_two_failed_responses_raise_after_exactly_one_retry(response_kind):
    def fail(_request, process):
        if response_kind == "eof":
            process.stdout.close()
        else:
            process.stdout.feed("{not-json")

    factory = ProcessFactory([fail, fail])
    bridge = GhostCursorBridge(
        process_factory=factory, id_factory=iter(["first", "second"]).__next__
    )

    try:
        with pytest.raises(
            GhostCursorError, match="Ghost Cursor path generation failed after retry"
        ):
            bridge.generate_path((0, 0), (1, 1))
    finally:
        bridge.close()

    assert len(factory.processes) == 2
    assert [process.terminate_calls for process in factory.processes] == [1, 1]


def test_response_timeout_restarts_and_succeeds():
    def no_response(_request, _process):
        return None

    factory = ProcessFactory(
        [no_response, successful_response([{"x": 0, "y": 0}, {"x": 2, "y": 3}])]
    )
    bridge = GhostCursorBridge(
        process_factory=factory,
        id_factory=iter(["timed-out", "retry"]).__next__,
        response_timeout=0.02,
    )

    try:
        result = bridge.generate_path((0, 0), (2, 3))
    finally:
        bridge.close()

    assert result[-1] == {"x": 2.0, "y": 3.0}
    assert len(factory.processes) == 2
    assert factory.processes[0].terminate_calls == 1


def test_close_is_idempotent_and_terminates_a_running_worker():
    factory = ProcessFactory(
        [successful_response([{"x": 0, "y": 0}, {"x": 1, "y": 1}])]
    )
    bridge = GhostCursorBridge(process_factory=factory, id_factory=lambda: "only")
    bridge.generate_path((0, 0), (1, 1))

    bridge.close()
    bridge.close()

    process = factory.processes[0]
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == [2]


def test_reader_start_failure_cleans_each_process_and_uses_retry_contract(
    monkeypatch,
):
    bridge_holder = {}
    published_states = []
    readers = []
    processes = []

    class FailingReader:
        def __init__(self, **_kwargs):
            self.join_calls = []

        def start(self):
            bridge = bridge_holder["bridge"]
            published_states.append(
                (bridge._process, bridge._responses, bridge._reader)
            )
            raise RuntimeError("sensitive reader startup failure")

        def join(self, timeout=None):
            self.join_calls.append(timeout)

    def thread_factory(**kwargs):
        reader = FailingReader(**kwargs)
        readers.append(reader)
        return reader

    def process_factory(_command, **_kwargs):
        process = CleanupProcess("reader_start")
        processes.append(process)
        return process

    monkeypatch.setattr("ghost_cursor_bridge.threading.Thread", thread_factory)
    bridge = GhostCursorBridge(process_factory=process_factory)
    bridge_holder["bridge"] = bridge

    try:
        with pytest.raises(
            GhostCursorError,
            match="Ghost Cursor path generation failed after retry",
        ) as exc_info:
            bridge.generate_path((0, 0), (1, 1))
    finally:
        bridge.close()

    assert "sensitive reader startup failure" not in str(exc_info.value)
    assert len(processes) == 2
    assert len(readers) == 2
    assert published_states == [(None, None, None), (None, None, None)]
    assert bridge._process is None
    assert bridge._responses is None
    assert bridge._reader is None
    for process in processes:
        assert process.terminate_calls == 1
        assert process.kill_calls == 0
        assert process.wait_calls == [2]
        assert process.stdin.close_calls == 1
        assert process.stdout.close_calls == 1
    assert [reader.join_calls for reader in readers] == [[2], [2]]


def test_terminate_timeout_kills_then_waits_with_a_bound_and_cleans_resources():
    bridge = GhostCursorBridge()
    process = CleanupProcess("bounded")
    reader = TrackingReader()
    bridge._process = process
    bridge._reader = reader

    bridge.close()

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [2, 2]
    assert process.stdin.close_calls == 1
    assert process.stdout.close_calls == 1
    assert reader.join_calls == [2]


@pytest.mark.parametrize(
    "mode",
    ["poll_error", "terminate_error", "wait_error"],
)
def test_teardown_stage_failure_still_kills_and_performs_bounded_final_wait(mode):
    bridge = GhostCursorBridge()
    process = CleanupProcess(mode)
    reader = TrackingReader()
    bridge._process = process
    bridge._reader = reader

    bridge.close()

    assert process.poll_calls == 1
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == [2, 2]
    assert process.stdin.close_calls == 1
    assert process.stdout.close_calls == 1
    assert reader.join_calls == [2]


@pytest.mark.parametrize(
    "mode",
    ["terminate_error", "kill_error", "wait_error", "resource_error"],
)
def test_cleanup_exceptions_do_not_escape_or_break_idempotent_close(mode):
    bridge = GhostCursorBridge()
    process = CleanupProcess(mode)
    reader = TrackingReader(raises=mode == "resource_error")
    bridge._process = process
    bridge._reader = reader

    bridge.close()
    bridge.close()

    assert bridge._process is None
    assert bridge._reader is None
    assert process.stdin.close_calls == 1
    assert process.stdout.close_calls == 1
    assert reader.join_calls == [2]
    assert all(timeout == 2 for timeout in process.wait_calls)


def test_concurrent_calls_cannot_cross_response_ids_or_paths():
    timers = []

    def delayed_response(request, process):
        delay = 0.04 if request["end"]["x"] == 101 else 0.001
        timer = threading.Timer(
            delay,
            process.stdout.feed,
            args=(
                {
                    "id": request["id"],
                    "points": [request["start"], request["end"]],
                },
            ),
        )
        timers.append(timer)
        timer.start()

    factory = ProcessFactory([delayed_response])
    ids = iter(["one", "two"])
    bridge = GhostCursorBridge(
        process_factory=factory, id_factory=ids.__next__, response_timeout=0.2
    )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(bridge.generate_path, (1, 1), (101, 101)),
                executor.submit(bridge.generate_path, (2, 2), (202, 202)),
            ]
            results = [future.result() for future in futures]
    finally:
        for timer in timers:
            timer.join()
        bridge.close()

    assert results == [
        [{"x": 1.0, "y": 1.0}, {"x": 101.0, "y": 101.0}],
        [{"x": 2.0, "y": 2.0}, {"x": 202.0, "y": 202.0}],
    ]
    assert len(factory.processes) == 1


@pytest.mark.parametrize(
    "points",
    [
        [{"x": 0, "y": 0}],
        [{"x": 0, "y": 0}, {"x": float("nan"), "y": 1}],
        [{"x": 0, "y": 0}, {"x": "1", "y": 1}],
        [{"x": 0, "y": 0}, {"x": True, "y": 1}],
    ],
)
def test_invalid_or_non_finite_points_are_rejected(points):
    factory = ProcessFactory([successful_response(points), successful_response(points)])
    bridge = GhostCursorBridge(
        process_factory=factory, id_factory=iter(["first", "second"]).__next__
    )

    try:
        with pytest.raises(
            GhostCursorError, match="Ghost Cursor path generation failed after retry"
        ):
            bridge.generate_path((0, 0), (1, 1))
    finally:
        bridge.close()

    assert len(factory.processes) == 2


def test_oversized_integer_points_retry_once_then_raise_bridge_error():
    oversized_points = [
        {"x": 0, "y": 0},
        {"x": 10**400, "y": 1},
    ]
    factory = ProcessFactory(
        [
            successful_response(oversized_points),
            successful_response(oversized_points),
        ]
    )
    bridge = GhostCursorBridge(
        process_factory=factory, id_factory=iter(["first", "second"]).__next__
    )

    try:
        with pytest.raises(
            GhostCursorError, match="Ghost Cursor path generation failed after retry"
        ):
            bridge.generate_path((0, 0), (1, 1))
    finally:
        bridge.close()

    assert len(factory.processes) == 2
    assert [process.terminate_calls for process in factory.processes] == [1, 1]
