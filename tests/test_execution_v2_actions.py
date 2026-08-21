import asyncio
import random

import pytest

import execution_v2.actions as actions_module
from browser_video_switch import FeedState, VideoSwitchError
from execution_v2.actions import ActionExecutionError, execute_action
from execution_v2.locator import ResolvedElement


class _Mouse:
    def __init__(self):
        self.moves = []
        self.wheels = []
        self.downs = []
        self.ups = []

    async def move(self, x, y):
        self.moves.append((x, y))

    async def wheel(self, x, y):
        self.wheels.append((x, y))

    async def down(self, *, button):
        self.downs.append(button)

    async def up(self, *, button):
        self.ups.append(button)


class _Keyboard:
    def __init__(self):
        self.presses = []

    async def type(self, _text):
        pass

    async def press(self, key):
        self.presses.append(key)


class _Handle:
    def __init__(self, value=""):
        self.focused = False
        self.value = value

    async def focus(self):
        self.focused = True

    async def evaluate(self, _script):
        return self.value


class _Page:
    def __init__(self):
        self.mouse = _Mouse()
        self.keyboard = _Keyboard()
        self.viewport_size = {"width": 200, "height": 100}
        self.evaluations = []

    async def evaluate(self, script):
        self.evaluations.append(script)


class _Resolver:
    def __init__(self, handles):
        self.handles = handles
        self.calls = []

    async def resolve(
        self,
        _page,
        definition,
        *,
        require_editable=False,
        require_in_viewport=False,
        allow_viewport_fallback=False,
    ):
        self.calls.append(
            (
                definition["id"],
                require_editable,
                require_in_viewport,
                allow_viewport_fallback,
            )
        )
        handle, box = self.handles[definition["id"]]
        return ResolvedElement(handle, "css", box, ())


def _elements():
    return {
        "button": {"id": "button", "definition": {"id": "button"}},
        "input": {"id": "input", "definition": {"id": "input"}},
    }


async def _no_sleep(_seconds):
    return None


async def _text_resolver(action):
    assert action["content_library_id"] == "library-1"
    return "library text"


def test_move_uses_existing_human_move_to_at_an_interior_element_point(monkeypatch):
    page = _Page()
    resolver = _Resolver({"button": (_Handle(), {"x": 20, "y": 10, "width": 80, "height": 40})})
    calls = []

    async def move(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr("execution_v2.actions.human_move_to", move)

    result = asyncio.run(execute_action(
        page,
        {"id": "move-1", "type": "move", "element_id": "button", "duration_seconds": [0.2, 0.2]},
        _elements(), resolver, _text_resolver, rng=random.Random(3), sleep=_no_sleep,
    ))

    assert resolver.calls == [("button", False, True, True)]
    assert len(calls) == 1
    assert 20 < calls[0][0][1] < 100
    assert 10 < calls[0][0][2] < 50
    assert calls[0][1]["target_box"] == {"x": 20.0, "y": 10.0, "width": 80.0, "height": 40.0}
    assert result == {"action_id": "move-1", "action_type": "move", "status": "succeeded", "duration_seconds": 0.2}


def _feed_state(identity):
    return FeedState(identity, f"safe-{identity}", 0, 0, 200, 100, 0)


def test_scroll_presses_arrow_down_once_per_verified_video_switch(monkeypatch):
    page = _Page()
    captured = iter([_feed_state("one"), _feed_state("two")])
    waits = []

    async def capture(page_arg):
        assert page_arg is page
        return next(captured)

    async def changed(page_arg, before, **kwargs):
        assert page_arg is page
        assert kwargs["timeout"] == 8.0
        assert kwargs["sleep_fn"] is sleep
        return _feed_state("two" if before.fingerprint == "one" else "three")

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(actions_module, "capture_feed_state", capture)
    monkeypatch.setattr(actions_module, "wait_for_stable_changed_state", changed)

    result = asyncio.run(execute_action(
        page,
        {"id": "scroll-1", "type": "scroll", "direction": "down", "distance_pixels": [400, 600], "count": [2, 2], "interval_seconds": [0.2, 0.5]},
        _elements(), _Resolver({}), _text_resolver, rng=random.Random(1), sleep=sleep,
        wheel_calibration={"revision": 4, "events": [{"delta_y": 100}]},
    ))

    assert page.keyboard.presses == ["ArrowDown", "ArrowDown"]
    assert len(page.evaluations) == 2
    assert page.mouse.wheels == []
    assert len(waits) == 1
    assert 0.2 <= waits[0] <= 0.5
    assert result["count"] == 2
    assert result["requested_switches"] == 2
    assert result["completed_switches"] == 2
    assert "distance_pixels" not in result
    assert "wheel_events" not in result
    assert "calibration_revision" not in result


def test_scroll_up_uses_arrow_up(monkeypatch):
    page = _Page()

    async def capture(_page):
        return _feed_state("one")

    async def changed(_page, _before, **_kwargs):
        return _feed_state("zero")

    monkeypatch.setattr(actions_module, "capture_feed_state", capture)
    monkeypatch.setattr(actions_module, "wait_for_stable_changed_state", changed)
    result = asyncio.run(execute_action(
        page,
        {"id": "scroll-1", "type": "scroll", "direction": "up", "distance_pixels": [400, 600], "count": [1, 1], "interval_seconds": [0.2, 0.5]},
        _elements(), _Resolver({}), _text_resolver, rng=random.Random(1), sleep=_no_sleep,
    ))

    assert page.keyboard.presses == ["ArrowUp"]
    assert result["completed_switches"] == 1


def test_scroll_stops_after_one_unobserved_arrow_key_switch(monkeypatch):
    async def capture(_page):
        return _feed_state("one")

    async def unchanged(*_args, **_kwargs):
        return None

    monkeypatch.setattr(actions_module, "capture_feed_state", capture)
    monkeypatch.setattr(actions_module, "wait_for_stable_changed_state", unchanged)
    page = _Page()
    with pytest.raises(VideoSwitchError) as caught:
        asyncio.run(execute_action(
            page,
            {"id": "scroll-1", "type": "scroll", "direction": "down", "distance_pixels": [400, 600], "count": [2, 2], "interval_seconds": [0.2, 0.5]},
            _elements(), _Resolver({}), _text_resolver, rng=random.Random(1), sleep=_no_sleep,
        ))

    assert caught.value.code == "video_switch_not_observed"
    assert caught.value.requested_switches == 2
    assert caught.value.completed_switches == 0
    assert page.keyboard.presses == ["ArrowDown"]


def test_click_moves_then_holds_each_requested_click_and_waits_after(monkeypatch):
    page = _Page()
    resolver = _Resolver({"button": (_Handle(), {"x": 20, "y": 10, "width": 80, "height": 40})})
    calls = []
    waits = []

    async def move(*args, **kwargs):
        calls.append((args, kwargs))

    async def sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("execution_v2.actions.human_move_to", move)
    result = asyncio.run(execute_action(
        page,
        {"id": "click-1", "type": "click", "element_id": "button", "button": "right", "click_count": 2, "hold_seconds": [0.1, 0.1], "after_seconds": [0.3, 0.3]},
        _elements(), resolver, _text_resolver, rng=random.Random(1), sleep=sleep,
    ))

    assert len(calls) == 1
    assert page.mouse.downs == ["right", "right"]
    assert page.mouse.ups == ["right", "right"]
    assert waits == [0.1, 0.1, 0.3]
    assert result["click_count"] == 2


def test_input_focuses_without_click_reuses_human_type_and_verifies_full_text(monkeypatch):
    page = _Page()
    handle = _Handle()
    resolver = _Resolver({"input": (handle, {"x": 20, "y": 10, "width": 80, "height": 40})})
    calls = []

    async def type_text(_page, text, **kwargs):
        calls.append((text, kwargs))
        handle.value += text

    monkeypatch.setattr("execution_v2.actions.human_type", type_text)
    result = asyncio.run(execute_action(
        page,
        {"id": "input-1", "type": "input", "element_id": "input", "content_source": "library", "fixed_text": "", "content_library_id": "library-1", "interval_ms": [20, 20]},
        _elements(), resolver, _text_resolver, rng=random.Random(1), sleep=_no_sleep,
    ))

    assert resolver.calls == [("input", True, True, True)]
    assert handle.focused is True
    assert calls[0][0] == "library text"
    assert calls[0][1]["timing"] == {"source": "builtin", "interval_ms": [20, 20]}
    assert calls[0][1]["sleep_fn"] is _no_sleep
    assert result == {"action_id": "input-1", "action_type": "input", "status": "succeeded", "content_source": "library", "text_length": 12}
    assert page.mouse.downs == []


def test_input_fails_closed_when_final_value_does_not_contain_full_text(monkeypatch):
    page = _Page()
    handle = _Handle("partial")
    resolver = _Resolver({"input": (handle, {"x": 20, "y": 10, "width": 80, "height": 40})})

    async def type_text(*_args, **_kwargs):
        return None

    monkeypatch.setattr("execution_v2.actions.human_type", type_text)
    with pytest.raises(ActionExecutionError, match="input_verification_failed"):
        asyncio.run(execute_action(
            page,
            {"id": "input-1", "type": "input", "element_id": "input", "content_source": "fixed", "fixed_text": "complete", "content_library_id": "", "interval_ms": [20, 20]},
            _elements(), resolver, _text_resolver, rng=random.Random(1), sleep=_no_sleep,
        ))


def test_input_accepts_equivalent_contenteditable_whitespace(monkeypatch):
    page = _Page()
    handle = _Handle("")
    resolver = _Resolver({"input": (handle, {"x": 20, "y": 10, "width": 80, "height": 40})})

    async def type_text(_page, _text, **_kwargs):
        handle.value = "first\u00a0  line second line"

    monkeypatch.setattr("execution_v2.actions.human_type", type_text)
    result = asyncio.run(execute_action(
        page,
        {
            "id": "input-1", "type": "input", "element_id": "input",
            "content_source": "fixed", "fixed_text": "first line\nsecond line",
            "content_library_id": "", "interval_ms": [20, 20],
        },
        _elements(), resolver, _text_resolver, rng=random.Random(1), sleep=_no_sleep,
    ))

    assert result["status"] == "succeeded"


def test_input_rejects_matching_text_that_did_not_change(monkeypatch):
    page = _Page()
    handle = _Handle("already present")
    resolver = _Resolver({"input": (handle, {"x": 20, "y": 10, "width": 80, "height": 40})})

    async def type_text(*_args, **_kwargs):
        return None

    monkeypatch.setattr("execution_v2.actions.human_type", type_text)
    with pytest.raises(ActionExecutionError, match="input_verification_failed"):
        asyncio.run(execute_action(
            page,
            {
                "id": "input-1", "type": "input", "element_id": "input",
                "content_source": "fixed", "fixed_text": "already present",
                "content_library_id": "", "interval_ms": [20, 20],
            },
            _elements(), resolver, _text_resolver, rng=random.Random(1), sleep=_no_sleep,
        ))


def test_wait_only_uses_injected_sleep():
    waits = []

    async def sleep(seconds):
        waits.append(seconds)

    result = asyncio.run(execute_action(
        _Page(), {"id": "wait-1", "type": "wait", "duration_seconds": [0.5, 0.5]},
        _elements(), _Resolver({}), _text_resolver, rng=random.Random(1), sleep=sleep,
    ))

    assert waits == [0.5]
    assert result == {"action_id": "wait-1", "action_type": "wait", "status": "succeeded", "duration_seconds": 0.5}
