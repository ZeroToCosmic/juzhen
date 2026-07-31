import json

import pytest


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key).casefold()
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_normalize_mouse_recording_uses_only_viewport_ratios_and_timing():
    from browser_pattern_recorder import normalize_recording_sample

    normalized = normalize_recording_sample(
        {
            "type": "mouse",
            "viewport": {"width": 400, "height": 200},
            "started_at_ms": 100,
            "stopped_at_ms": 180,
            "samples": [
                {"x": 40, "y": 50, "at_ms": 110},
                {"x": 500, "y": -20, "at_ms": 145},
                {"x": 200, "y": 100, "at_ms": 180},
            ],
        }
    )

    assert normalized == {
        "points": [
            {"x_ratio": 0.1, "y_ratio": 0.25, "dt_ms": 10.0},
            {"x_ratio": 1.0, "y_ratio": 0.0, "dt_ms": 35.0},
            {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 35.0},
        ],
        "sample_count": 3,
        "total_duration_ms": 80.0,
    }
    serialized = json.dumps(normalized)
    assert '"x"' not in serialized
    assert '"y"' not in serialized
    assert "viewport" not in serialized


def test_plan_mouse_points_contract_is_supported():
    from browser_pattern_recorder import normalize_recording_sample

    sample = normalize_recording_sample(
        {
            "type": "mouse",
            "viewport": {"width": 1000, "height": 500},
            "points": [
                {"x": 100, "y": 50, "dt_ms": 0},
                {"x": 600, "y": 300, "dt_ms": 90},
            ],
        }
    )

    assert sample["points"] == [
        {"x_ratio": 0.1, "y_ratio": 0.1, "dt_ms": 0.0},
        {"x_ratio": 0.6, "y_ratio": 0.6, "dt_ms": 90.0},
    ]
    assert "x" not in sample["points"][0]


def test_plan_keyboard_events_contract_drops_keys_and_returns_timing_only():
    from browser_pattern_recorder import normalize_recording_sample

    sample = normalize_recording_sample(
        {
            "type": "keyboard",
            "events": [
                {"key": "s", "interval_ms": 80, "hold_ms": 20},
                {"key": "e", "interval_ms": 120, "hold_ms": 30},
            ],
        }
    )

    assert sample == {
        "intervals_ms": [80.0, 120.0],
        "hold_ms": [20.0, 30.0],
        "sample_count": 2,
    }


def test_normalize_keyboard_recording_returns_timing_only():
    from browser_pattern_recorder import normalize_recording_sample

    normalized = normalize_recording_sample(
        {
            "type": "keyboard",
            "started_at_ms": 100,
            "stopped_at_ms": 240,
            "samples": [
                {"sequence": 11, "down_at_ms": 120, "up_at_ms": 135},
                {"sequence": 12, "down_at_ms": 180, "up_at_ms": 205},
            ],
        }
    )

    assert normalized == {
        "intervals_ms": [20.0, 60.0],
        "hold_ms": [15.0, 25.0],
        "sample_count": 2,
        "total_duration_ms": 140.0,
    }
    assert not ({"key", "code", "text", "password", "value", "clipboard"} & set(_walk_keys(normalized)))


def test_keyboard_normalization_drops_all_content_bearing_fields():
    from browser_pattern_recorder import normalize_recording_sample

    normalized = normalize_recording_sample(
        {
            "type": "keyboard",
            "started_at_ms": 0,
            "stopped_at_ms": 50,
            "text": "outer secret",
            "samples": [
                {
                    "sequence": 1,
                    "down_at_ms": 5,
                    "up_at_ms": 10,
                    "key": "a",
                    "code": "KeyA",
                    "value": "secret",
                },
                {
                    "sequence": 2,
                    "down_at_ms": 25,
                    "up_at_ms": 35,
                    "password": "secret",
                    "clipboard": "secret",
                },
            ],
        }
    )

    assert normalized == {
        "intervals_ms": [5.0, 20.0],
        "hold_ms": [5.0, 10.0],
        "sample_count": 2,
        "total_duration_ms": 50.0,
    }
    assert not ({"key", "code", "text", "password", "value", "clipboard"} & set(_walk_keys(normalized)))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (
            {
                "type": "mouse",
                "viewport": {"width": 100, "height": 100},
                "started_at_ms": 0,
                "stopped_at_ms": 20,
                "samples": [{"x": 1, "y": 1, "at_ms": 10}],
            },
            "鼠标录制样本不足",
        ),
        (
            {
                "type": "keyboard",
                "started_at_ms": 0,
                "stopped_at_ms": 20,
                "samples": [{"sequence": 1, "down_at_ms": 5, "up_at_ms": 10}],
            },
            "键盘录制样本不足",
        ),
    ],
)
def test_normalization_rejects_insufficient_samples(raw, message):
    from browser_pattern_recorder import normalize_recording_sample

    with pytest.raises(ValueError, match=message):
        normalize_recording_sample(raw)


class FakeCdpClient:
    instances = []
    evaluate_values = []

    def __init__(self, ws_url):
        self.ws_url = ws_url
        self.commands = []
        self.closed = False
        self.__class__.instances.append(self)

    def page_session(self):
        return "session-1", [{"targetId": "target-1", "type": "page"}]

    def command(self, method, params=None, session_id=None):
        self.commands.append((method, params or {}, session_id))
        if method == "Runtime.evaluate":
            value = self.__class__.evaluate_values.pop(0)
            return {"result": {"value": value}}
        return {}

    def close(self):
        self.closed = True


def test_prepare_injects_shadow_dom_manual_controls_and_exclusions(monkeypatch):
    import browser_pattern_recorder as recorder

    FakeCdpClient.instances.clear()
    FakeCdpClient.evaluate_values = [
        {"recording_id": "rec-1", "type": "keyboard", "status": "ready", "sample_count": 0}
    ]
    monkeypatch.setattr(recorder, "CdpClient", FakeCdpClient)

    result = recorder.prepare_recording("ws://profile", "rec-1", "keyboard")

    assert result["status"] == "ready"
    command = FakeCdpClient.instances[0].commands[0]
    assert command[0] == "Runtime.evaluate"
    script = command[1]["expression"]
    assert "attachShadow" in script
    assert "开始录制" in script
    assert "结束录制" in script
    assert "pointermove" in script
    assert "Ctrl+Shift+F10" in script
    assert "composedPath" in script
    assert "removeEventListener" in script
    assert "host.remove()" in script
    for forbidden_assignment in (
        "key: event.key",
        "code: event.code",
        "text:",
        "password:",
        "value:",
        "clipboard:",
    ):
        assert forbidden_assignment not in script
    assert FakeCdpClient.instances[0].closed is True


def test_prepare_does_not_read_key_identity_outside_exact_stop_shortcut(monkeypatch):
    import browser_pattern_recorder as recorder

    FakeCdpClient.instances.clear()
    FakeCdpClient.evaluate_values = [
        {"recording_id": "rec-1", "type": "keyboard", "status": "ready", "sample_count": 0}
    ]
    monkeypatch.setattr(recorder, "CdpClient", FakeCdpClient)

    recorder.prepare_recording("ws://profile", "rec-1", "keyboard")

    script = FakeCdpClient.instances[0].commands[0][1]["expression"]
    shortcut_lines = [line for line in script.splitlines() if "const isStopShortcut" in line]
    assert len(shortcut_lines) == 1
    shortcut_line = shortcut_lines[0]
    assert "event.ctrlKey" in shortcut_line
    assert "event.shiftKey" in shortcut_line
    assert "!event.altKey" in shortcut_line
    assert "!event.metaKey" in shortcut_line
    ordinary_paths = script.replace(shortcut_line, "")
    assert "event.key" not in ordinary_paths
    assert "event.code" not in ordinary_paths


def test_prepare_only_replaces_the_same_recording_id(monkeypatch):
    import browser_pattern_recorder as recorder

    FakeCdpClient.instances.clear()
    FakeCdpClient.evaluate_values = [
        {"recording_id": "rec-2", "type": "mouse", "status": "ready", "sample_count": 0}
    ]
    monkeypatch.setattr(recorder, "CdpClient", FakeCdpClient)

    recorder.prepare_recording("ws://profile", "rec-2", "mouse")

    script = FakeCdpClient.instances[0].commands[0][1]["expression"]
    assert "const old = store[recordingId]" in script
    assert "delete store[recordingId]" in script
    assert "Object.values(store)" not in script
    assert "for (const key of Object.keys(store))" not in script


def test_read_and_finish_use_page_state_and_finish_cleans_it(monkeypatch):
    import browser_pattern_recorder as recorder

    FakeCdpClient.instances.clear()
    raw = {
        "recording_id": "rec-1",
        "type": "mouse",
        "status": "stopped",
        "viewport": {"width": 100, "height": 100},
        "started_at_ms": 0,
        "stopped_at_ms": 20,
        "samples": [
            {"x": 10, "y": 20, "at_ms": 5},
            {"x": 50, "y": 60, "at_ms": 20},
        ],
    }
    FakeCdpClient.evaluate_values = [
        {"recording_id": "rec-1", "type": "mouse", "status": "recording", "sample_count": 2},
        raw,
    ]
    monkeypatch.setattr(recorder, "CdpClient", FakeCdpClient)

    status = recorder.read_recording("ws://profile", "rec-1")
    finished = recorder.finish_recording("ws://profile", "rec-1")

    assert status["sample_count"] == 2
    assert finished["sample"] == {
        "points": [
            {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 5.0},
            {"x_ratio": 0.5, "y_ratio": 0.6, "dt_ms": 15.0},
        ],
        "sample_count": 2,
        "total_duration_ms": 20.0,
    }
    finish_script = FakeCdpClient.instances[1].commands[0][1]["expression"]
    assert "state.cleanup()" in finish_script
    assert "delete" in finish_script


def test_missing_or_replaced_page_state_is_context_invalid(monkeypatch):
    import browser_pattern_recorder as recorder

    FakeCdpClient.instances.clear()
    FakeCdpClient.evaluate_values = [None]
    monkeypatch.setattr(recorder, "CdpClient", FakeCdpClient)

    with pytest.raises(RuntimeError, match="录制上下文已失效"):
        recorder.read_recording("ws://profile", "missing")


def test_finished_keyboard_sample_satisfies_persisted_pattern_contract(monkeypatch):
    import browser_pattern_recorder as recorder
    from browser_strategy_config import normalize_patterns

    FakeCdpClient.instances.clear()
    FakeCdpClient.evaluate_values = [
        {
            "recording_id": "rec-1",
            "type": "keyboard",
            "status": "stopped",
            "started_at_ms": 100,
            "stopped_at_ms": 240,
            "samples": [
                {"sequence": 1, "down_at_ms": 120, "up_at_ms": 135},
                {"sequence": 2, "down_at_ms": 180, "up_at_ms": 205},
            ],
        }
    ]
    monkeypatch.setattr(recorder, "CdpClient", FakeCdpClient)

    finished = recorder.finish_recording("ws://profile", "rec-1")
    patterns = normalize_patterns(
        [
            {
                "id": "pattern-1",
                "name": "录制节奏",
                "type": "keyboard",
                "data": finished["sample"],
            }
        ]
    )

    assert patterns[0]["data"]["total_duration_ms"] == 140.0
