import asyncio
import math
import random

import browser_actions
import pytest
from actions_context import generate_comment
from browser_actions import validate_action_config
from browser_element_resolver import ResolvedElement, build_candidate_locator


class _ActionResolvedLocator:
    """Give action-focused test doubles the editable contract resolver guarantees."""

    def __init__(self, locator):
        self.locator = locator

    def __getattr__(self, name):
        return getattr(self.locator, name)

    async def evaluate(self, expression):
        evaluator = getattr(self.locator, "evaluate", None)
        if callable(evaluator):
            return await evaluator(expression)
        return True


@pytest.fixture(autouse=True)
def isolate_actions_from_canonical_resolver(monkeypatch):
    async def resolve(page, alias, definition):
        assert isinstance(definition, dict)
        assert definition["scope"] == "page"
        candidate = next(
            item for item in definition["locators"] if item["enabled"]
        )
        locator = build_candidate_locator(page, candidate)
        return ResolvedElement(
            locator=_ActionResolvedLocator(locator),
            alias=alias,
            scope=definition["scope"],
            candidate={"id": candidate["id"], "type": candidate["type"]},
            diagnostics={"candidates": []},
        )

    monkeypatch.setattr(browser_actions, "resolve_element", resolve)


def test_legacy_strategy_executor_is_not_exposed():
    assert not hasattr(browser_actions, "execute_strategy")


def test_get_viewport_prefers_live_inner_size_after_window_resize():
    from actions_dom import get_viewport

    class Page:
        viewport_size = {"width": 1280, "height": 720}

        async def evaluate(self, expression):
            assert "window.innerWidth" in expression
            return {"width": 640, "height": 900}

    assert asyncio.run(get_viewport(Page())) == (640.0, 900.0)


def test_generate_comment_prefers_keyword_rule():
    comment, is_ai = asyncio.run(
        generate_comment({"description": "A new iPhone camera test", "tags": ["tech"]})
    )

    assert "iPhone" in comment or "iPhone".lower() in comment.lower()
    assert is_ai is False


def test_generate_comment_falls_back_when_no_active_model(monkeypatch):
    monkeypatch.setattr("actions_context._active_model", lambda: None)

    comment, is_ai = asyncio.run(generate_comment({"description": "unknown product"}))

    assert comment == "Great video! 🔥"
    assert is_ai is True


def test_human_type_types_each_character(monkeypatch):
    from actions_dom import human_type

    typed = []

    class Keyboard:
        async def type(self, character):
            typed.append(character)

        async def down(self, character):
            self.character = character

        async def up(self, _character):
            typed.append(self.character)

    class Page:
        keyboard = Keyboard()

    async def no_sleep(_duration):
        return None

    monkeypatch.setattr("actions_dom.asyncio.sleep", no_sleep)
    asyncio.run(human_type(Page(), "abc"))

    assert typed == ["a", "b", "c"]


def test_human_type_uses_canonical_keyboard_pattern_without_content():
    from actions_dom import human_type

    typed = []
    waits = []

    class Keyboard:
        async def type(self, character):
            typed.append(character)

        async def down(self, character):
            self.character = character

        async def up(self, _character):
            typed.append(self.character)

    class Page:
        keyboard = Keyboard()

    async def record_wait(seconds):
        waits.append(seconds)

    patterns = {
        "keys": {
            "id": "keys",
            "type": "keyboard",
            "data": {
                "intervals_ms": [80, 120],
                "hold_ms": [20, 30],
                "sample_count": 2,
                "total_duration_ms": 250,
            },
        }
    }

    asyncio.run(human_type(
        Page(), "abc", timing={"source": "pattern", "id": "keys"}, patterns=patterns,
        rng=random.Random(1), sleep_fn=record_wait,
    ))

    assert typed == ["a", "b", "c"]
    assert len(waits) == 6
    assert all(0.018 <= value <= 0.033 for value in waits[1::2])
    assert waits[0] >= 0.05
    assert all(value >= 0 for value in waits)
    assert "text" not in patterns["keys"]["data"]


def test_human_type_replays_pattern_holds_with_matching_intervals():
    from actions_dom import human_type

    events = []
    waits = []

    class Keyboard:
        async def type(self, character):
            events.append(("type", character))

        async def down(self, character):
            events.append(("down", character))

        async def up(self, character):
            events.append(("up", character))

    class Page:
        keyboard = Keyboard()

    class NoJitter:
        @staticmethod
        def randint(minimum, _maximum):
            return minimum

        @staticmethod
        def uniform(_minimum, _maximum):
            return 1.0

    async def record_wait(seconds):
        waits.append(seconds)

    pattern = {
        "keys": {"type": "keyboard", "data": {
            "intervals_ms": [80, 120], "hold_ms": [20, 30],
        }}
    }

    asyncio.run(human_type(
        Page(), "ab", timing={"source": "pattern", "id": "keys"},
        patterns=pattern, rng=NoJitter(), sleep_fn=record_wait,
    ))

    assert events == [("down", "a"), ("up", "a"), ("down", "b"), ("up", "b")]
    assert waits == [0.08, 0.02, 0.1, 0.03]
    assert "text" not in pattern["keys"]["data"]


def test_human_type_pattern_uses_the_same_selected_offset_for_holds():
    from actions_dom import human_type

    events = []
    waits = []

    class Keyboard:
        async def down(self, character):
            events.append(("down", character))

        async def up(self, character):
            events.append(("up", character))

    class Page:
        keyboard = Keyboard()

    class SelectSecondSample:
        @staticmethod
        def randint(_minimum, _maximum):
            return 1

        @staticmethod
        def uniform(_minimum, _maximum):
            return 1.0

    async def record_wait(seconds):
        waits.append(seconds)

    asyncio.run(human_type(
        Page(), "ab", timing={"source": "pattern", "id": "keys"},
        patterns={"keys": {"type": "keyboard", "data": {
            "intervals_ms": [10, 70, 100], "hold_ms": [5, 20, 30],
        }}},
        rng=SelectSecondSample(), sleep_fn=record_wait,
    ))

    assert events == [("down", "a"), ("up", "a"), ("down", "b"), ("up", "b")]
    assert waits == [0.07, 0.02, 0.08, 0.03]


def test_human_type_pattern_inserts_chinese_and_emoji_without_key_down_up():
    from actions_dom import human_type

    inserted = []
    waits = []

    class Keyboard:
        async def down(self, _character):
            raise AssertionError("Unicode text must not use keyboard.down")

        async def up(self, _character):
            raise AssertionError("Unicode text must not use keyboard.up")

        async def insert_text(self, text):
            inserted.append(text)

    class Page:
        keyboard = Keyboard()

    class NoJitter:
        @staticmethod
        def randint(minimum, _maximum):
            return minimum

        @staticmethod
        def uniform(_minimum, _maximum):
            return 1.0

    async def record_wait(seconds):
        waits.append(seconds)

    asyncio.run(human_type(
        Page(), "中😀", timing={"source": "pattern", "id": "keys"},
        patterns={"keys": {"type": "keyboard", "data": {
            "intervals_ms": [80, 120], "hold_ms": [20, 30],
        }}},
        rng=NoJitter(), sleep_fn=record_wait,
    ))

    assert inserted == ["中", "😀"]
    assert waits == [0.08, 0.02, 0.1, 0.03]


class _AsyncKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, character, **_kwargs):
        self.page.typed.append(character)

    async def down(self, character):
        self.character = character

    async def up(self, _character):
        self.page.typed.append(self.character)


class _AsyncLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector.removeprefix("xpath=")

    async def focus(self):
        self.page.focused.append(self.selector)

    async def input_value(self):
        return "".join(self.page.typed)


class _AsyncPage:
    def __init__(self):
        self.typed = []
        self.clicked = []
        self.focused = []
        self.keyboard = _AsyncKeyboard(self)

    def locator(self, selector):
        return _AsyncLocator(self, selector)


async def _fixed_text(action):
    return action["params"]["content"]["text"]


def test_keyboard_input_focuses_without_clicking():
    from browser_actions import execute_action

    page = _AsyncPage()
    result = asyncio.run(execute_action(
        page,
        {
            "id": "type",
            "type": "keyboard_input",
            "params": {
                "element": "input",
                "content": {"source": "fixed", "text": "hello", "brand_id": ""},
                "typing": {"source": "builtin", "interval_ms": [0, 0]},
            },
        },
        {"input": "//textarea"},
        {},
        _fixed_text,
    ))

    assert page.focused == ["//textarea"]
    assert page.clicked == []
    assert result["text"] == "hello"
    assert {
        key: result[key]
        for key in ("action_id", "type", "status", "element", "text")
    } == {
        "action_id": "type",
        "type": "keyboard_input",
        "status": "ok",
        "element": "input",
        "text": "hello",
    }
    assert result["locator"]["scope"] == "page"
    assert result["locator"]["candidate_type"] == "xpath"
    assert result["locator"]["candidate_id"]


def test_keyboard_input_rejects_missing_target_echo():
    from browser_actions import execute_action

    class Field:
        async def focus(self):
            return None

        async def input_value(self):
            return "different value"

    class Keyboard:
        async def type(self, _character):
            return None

    class Page:
        keyboard = Keyboard()

        @staticmethod
        def locator(_selector):
            return Field()

    with pytest.raises(RuntimeError, match="keyboard input was not reflected"):
        asyncio.run(execute_action(
            Page(),
            {"id": "type", "type": "keyboard_input", "params": {
                "element": "input",
                "content": {"source": "fixed", "text": "hello", "brand_id": ""},
                "typing": {"source": "builtin", "interval_ms": [0, 0]},
            }},
            {"input": "//textarea"}, {}, _fixed_text,
        ))


def test_keyboard_input_rejects_an_unchanged_existing_value():
    from browser_actions import execute_action

    class Field:
        async def focus(self):
            return None

        async def input_value(self):
            return "hello"

    class Keyboard:
        async def type(self, _character):
            return None

    class Page:
        keyboard = Keyboard()

        @staticmethod
        def locator(_selector):
            return Field()

    with pytest.raises(RuntimeError, match="keyboard input was not reflected"):
        asyncio.run(execute_action(
            Page(),
            {"id": "type", "type": "keyboard_input", "params": {
                "element": "input",
                "content": {"source": "fixed", "text": "hello", "brand_id": ""},
                "typing": {"source": "builtin", "interval_ms": [0, 0]},
            }},
            {"input": "//textarea"}, {}, _fixed_text,
        ))


def test_keyboard_input_rejects_unrelated_change_without_an_extra_expected_copy():
    from browser_actions import execute_action

    class Field:
        def __init__(self):
            self.values = iter(["hello", "hello!"])

        async def focus(self):
            return None

        async def input_value(self):
            return next(self.values)

    class Page:
        keyboard = type("Keyboard", (), {"type": staticmethod(lambda _character: asyncio.sleep(0))})()
        field = Field()

        @classmethod
        def locator(cls, _selector):
            return cls.field

    with pytest.raises(RuntimeError, match="keyboard input was not reflected"):
        asyncio.run(execute_action(
            Page(),
            {"id": "type", "type": "keyboard_input", "params": {
                "element": "input",
                "content": {"source": "fixed", "text": "hello", "brand_id": ""},
                "typing": {"source": "builtin", "interval_ms": [0, 0]},
            }},
            {"input": "//textarea"}, {}, _fixed_text,
        ))


@pytest.mark.parametrize("kind", ["input", "textarea", "contenteditable"])
def test_keyboard_input_verifies_input_textarea_and_contenteditable(kind):
    from browser_actions import execute_action

    reads = []
    values = ["", "hello"]

    class Field:
        async def focus(self):
            return None

        async def input_value(self):
            reads.append("input_value")
            if kind == "contenteditable":
                raise RuntimeError("not an input")
            return values.pop(0)

        async def text_content(self):
            reads.append("text_content")
            return values.pop(0)

    class Keyboard:
        async def type(self, _character):
            return None

    class Page:
        keyboard = Keyboard()

        @staticmethod
        def locator(_selector):
            return Field()

    asyncio.run(execute_action(
        Page(),
        {"id": "type", "type": "keyboard_input", "params": {
            "element": kind,
            "content": {"source": "fixed", "text": "hello", "brand_id": ""},
            "typing": {"source": "builtin", "interval_ms": [0, 0]},
        }},
        {kind: f"//{kind}"}, {}, _fixed_text,
    ))

    assert reads == (
        ["input_value", "text_content", "input_value", "text_content"]
        if kind == "contenteditable"
        else ["input_value", "input_value"]
    )


def test_execute_action_accepts_task_one_normalized_pattern_lists():
    from browser_actions import execute_action

    page = _AsyncPage()

    async def no_wait(_seconds):
        return None

    result = asyncio.run(execute_action(
        page,
        {"id": "type", "type": "keyboard_input", "params": {
            "element": "input", "content": {"source": "fixed", "text": "ok", "brand_id": ""},
            "typing": {"source": "pattern", "id": "keys"},
        }},
        {"input": "//textarea"},
        [{"id": "keys", "type": "keyboard", "data": {
            "intervals_ms": [0], "hold_ms": [0], "sample_count": 1, "total_duration_ms": 0,
        }}],
        _fixed_text, rng=random.Random(1), sleep_fn=no_wait,
    ))

    assert result["text"] == "ok"
    assert page.typed == ["o", "k"]


class _Mouse:
    def __init__(self, page):
        self.page = page

    async def move(self, x, y, **_kwargs):
        self.page.moves.append((x, y))

    async def click(self, x, y, **kwargs):
        self.page.clicked.append((x, y, kwargs))

    async def wheel(self, x, y):
        self.page.wheels.append((x, y))
        self.page.video_index += 1 if y > 0 else -1


class _PointerLocator(_AsyncLocator):
    async def scroll_into_view_if_needed(self):
        return None

    async def bounding_box(self):
        return {"x": 20, "y": 10, "width": 40, "height": 20}


class _PointerPage(_AsyncPage):
    def __init__(self):
        super().__init__()
        self.viewport_size = {"width": 100, "height": 50}
        self.moves = []
        self.wheels = []
        self.video_index = 0
        self.mouse = _Mouse(self)
        self._human_pointer = (10, 10)

    def locator(self, selector):
        return _PointerLocator(self, selector)

    async def evaluate(self, expression):
        assert "#column-list-container" in expression
        return {
            "identity": f"video:{self.video_index}",
            "container_x": 0,
            "container_y": 0,
            "container_width": 100,
            "container_height": 50,
            "scroll_top": self.video_index * 50,
        }


async def _move_to_visible_box(page, box):
    from actions_dom import element_viewport_target, human_move_to

    class Locator(_PointerLocator):
        async def bounding_box(self):
            return box

    page.locator = lambda selector: Locator(page, selector)
    viewport_size = (100.0, 50.0)
    point, target = await element_viewport_target(
        page, "//target", "clicked", viewport_size
    )
    final = await human_move_to(
        page,
        *point,
        duration_seconds=0,
        target_box=target,
        viewport_size=viewport_size,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    )
    return point, target, final


def test_builtin_ghost_cursor_receives_tracked_start_clipped_end_and_target_box(
    monkeypatch,
):
    from actions_dom import human_move_to

    page = _PointerPage()
    calls = []
    target_box = {"x": 20.0, "y": 10.0, "width": 40.0, "height": 20.0}

    def generate(start, end, target=None):
        calls.append((start, end, target))
        return [
            {"x": start[0], "y": start[1]},
            {"x": end[0], "y": end[1]},
        ]

    monkeypatch.setattr("actions_dom.generate_ghost_path", generate)

    asyncio.run(human_move_to(
        page,
        120,
        60,
        duration_seconds=0,
        target_box=target_box,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert calls == [(
        (10, 10),
        (
            math.nextafter(100.0, -math.inf),
            math.nextafter(50.0, -math.inf),
        ),
        target_box,
    )]


def test_builtin_ghost_cursor_emits_clipped_route_and_scales_total_duration(
    monkeypatch,
):
    from actions_dom import human_move_to

    page = _PointerPage()
    waits = []
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": 150, "y": -25},
            {"x": 75, "y": 35},
        ],
    )

    async def record_wait(seconds):
        waits.append(seconds)

    final = asyncio.run(human_move_to(
        page, 90, 40, duration_seconds=0.6, sleep_fn=record_wait
    ))

    assert page.moves == [
        (10.0, 10.0),
        (math.nextafter(100.0, -math.inf), 0.0),
        (90.0, 40.0),
    ]
    assert sum(waits) == pytest.approx(0.6)
    assert final == (90.0, 40.0)
    assert page._human_pointer == final


def test_builtin_ghost_cursor_forces_selected_element_endpoint(
    monkeypatch,
):
    from actions_dom import human_move_to

    page = _PointerPage()
    target_box = {"x": 20.0, "y": 10.0, "width": 40.0, "height": 20.0}
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": 500, "y": 500},
        ],
    )

    final = asyncio.run(human_move_to(
        page,
        40,
        20,
        duration_seconds=0,
        target_box=target_box,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert final == (40.0, 20.0)
    assert page.moves[-1] == final
    assert page._human_pointer == final


def test_quarter_pixel_visible_element_preserves_exact_box_and_center(
    monkeypatch,
):
    page = _PointerPage()
    box = {"x": 10.0, "y": 5.0, "width": 0.25, "height": 0.25}
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": end[0], "y": end[1]},
        ],
    )

    point, target, final = asyncio.run(_move_to_visible_box(page, box))

    assert target == box
    assert point == (10.125, 5.125)
    assert final == point
    assert box["x"] <= final[0] < box["x"] + box["width"] < 100.0
    assert box["y"] <= final[1] < box["y"] + box["height"] < 50.0


def test_half_pixel_right_edge_sliver_has_inside_center_and_final_point(
    monkeypatch,
):
    page = _PointerPage()
    box = {"x": 99.5, "y": 12.0, "width": 1.0, "height": 2.0}
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": end[0], "y": end[1]},
        ],
    )

    point, target, final = asyncio.run(_move_to_visible_box(page, box))

    assert target == {"x": 99.5, "y": 12.0, "width": 0.5, "height": 2.0}
    assert point == (99.75, 13.0)
    assert final == point
    assert box["x"] <= final[0] < 100.0 < box["x"] + box["width"]
    assert box["y"] <= final[1] < box["y"] + box["height"] < 50.0


def test_recorded_pattern_move_never_calls_ghost_cursor(monkeypatch):
    from actions_dom import human_move_to

    page = _PointerPage()
    waits = []

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("recorded patterns must not call Ghost Cursor")

    monkeypatch.setattr("actions_dom.generate_ghost_path", fail_if_called)
    pattern = {"data": {"points": [
        {"x_ratio": 0, "y_ratio": 0, "dt_ms": 10},
        {"x_ratio": 1, "y_ratio": 1, "dt_ms": 20},
    ]}}

    async def record_wait(seconds):
        waits.append(seconds)

    final = asyncio.run(human_move_to(
        page, 30, 20, pattern=pattern, sleep_fn=record_wait
    ))

    assert page.moves == [(10.0, 10.0), (30.0, 20.0)]
    assert waits == [0.01, 0.02]
    assert final == (30.0, 20.0)
    assert page._human_pointer == final


def test_move_trajectory_source_reports_builtin_and_recorded_pattern(monkeypatch):
    from browser_actions import execute_action

    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": end[0], "y": end[1]},
        ],
    )

    async def no_wait(_seconds):
        return None

    builtin = asyncio.run(execute_action(
        _PointerPage(),
        {"id": "builtin", "type": "move", "params": {
            "target_mode": "viewport", "element": "", "delta_viewport": [0.1, 0.1],
            "trajectory": {"source": "builtin", "id": "bezier"},
            "duration_seconds": [0, 0],
        }},
        {}, {}, _fixed_text, sleep_fn=no_wait,
    ))
    recorded = asyncio.run(execute_action(
        _PointerPage(),
        {"id": "recorded", "type": "move", "params": {
            "target_mode": "viewport", "element": "", "delta_viewport": [0.1, 0.1],
            "trajectory": {"source": "pattern", "id": "curve"},
            "duration_seconds": [0, 0],
        }},
        {}, {"curve": {"data": {"points": [
            {"x_ratio": 0, "y_ratio": 0, "dt_ms": 0},
            {"x_ratio": 1, "y_ratio": 1, "dt_ms": 0},
        ]}}}, _fixed_text, sleep_fn=no_wait,
    ))

    assert builtin["trajectory_source"] == "ghost-cursor"
    assert recorded["trajectory_source"] == "recorded-pattern"


def test_builtin_click_passes_visible_box_and_reports_ghost_cursor(
    monkeypatch,
):
    from browser_actions import execute_action

    page = _PointerPage()
    targets = []

    def generate(start, end, target=None):
        targets.append(target)
        return [
            {"x": start[0], "y": start[1]},
            {"x": target["x"] + target["width"] + 10, "y": target["y"] - 10},
        ]

    monkeypatch.setattr("actions_dom.generate_ghost_path", generate)

    result = asyncio.run(execute_action(
        page,
        {"id": "click", "type": "click", "params": {
            "element": "target", "button": "left", "click_count": 1,
            "hold_seconds": [0, 0],
            "trajectory": {"source": "builtin", "id": "bezier"},
        }},
        {"target": "//target"}, {}, _fixed_text,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert targets == [{"x": 20.0, "y": 10.0, "width": 40.0, "height": 20.0}]
    assert 20 <= page._human_pointer[0] <= 60
    assert 10 <= page._human_pointer[1] <= 30
    assert result["trajectory_source"] == "ghost-cursor"


def test_zero_hold_click_uses_selected_element_final_coordinates(monkeypatch):
    from actions_dom import human_click

    page = _PointerPage()
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": 25, "y": 15},
        ],
    )

    final_x, final_y, hold = asyncio.run(human_click(
        page,
        "//target",
        hold_seconds=(0, 0),
        trajectory={"source": "builtin", "id": "bezier"},
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert hold == 0
    assert (final_x, final_y) == (40.0, 20.0)
    assert page._human_pointer == (final_x, final_y)
    assert page.clicked[-1][:2] == (final_x, final_y)


@pytest.mark.parametrize("click_count", [2, 3])
def test_nonzero_hold_multiclick_uses_playwright_click_semantics(
    monkeypatch, click_count
):
    from actions_dom import human_click

    class MouseWithDownUp(_Mouse):
        async def down(self, **kwargs):
            self.page.down_up.append(("down", kwargs))

        async def up(self, **kwargs):
            self.page.down_up.append(("up", kwargs))

    page = _PointerPage()
    page.down_up = []
    page.mouse = MouseWithDownUp(page)
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": 500, "y": 500},
        ],
    )

    result = asyncio.run(human_click(
        page,
        "//target",
        button="right",
        click_count=click_count,
        hold_seconds=(0.125, 0.125),
        trajectory={"source": "builtin", "id": "bezier"},
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert result == (40.0, 20.0, 0.125)
    assert page.clicked == [(
        40.0,
        20.0,
        {"button": "right", "click_count": click_count, "delay": 125},
    )]
    assert page.down_up == []


@pytest.mark.parametrize("action_type", ["move", "click"])
def test_element_action_reuses_one_live_viewport_snapshot(
    monkeypatch, action_type
):
    from browser_actions import execute_action

    class Page(_PointerPage):
        def __init__(self):
            super().__init__()
            self.viewport_size = None
            self.viewport_evaluations = 0

        async def evaluate(self, _expression):
            self.viewport_evaluations += 1
            return {"width": 100, "height": 50}

    page = Page()
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda start, end, target=None: [
            {"x": start[0], "y": start[1]},
            {"x": end[0], "y": end[1]},
        ],
    )
    params = (
        {
            "target_mode": "element",
            "element": "target",
            "delta_viewport": [0, 0],
            "trajectory": {"source": "builtin", "id": "bezier"},
            "duration_seconds": [0, 0],
        }
        if action_type == "move"
        else {
            "element": "target",
            "button": "left",
            "click_count": 1,
            "hold_seconds": [0, 0],
            "trajectory": {"source": "builtin", "id": "bezier"},
        }
    )

    asyncio.run(execute_action(
        page,
        {"id": action_type, "type": action_type, "params": params},
        {"target": "//target"},
        {},
        _fixed_text,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert page.viewport_evaluations == 1


def test_recorded_pattern_click_never_calls_ghost_cursor_and_reports_source(
    monkeypatch,
):
    from browser_actions import execute_action

    page = _PointerPage()

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("recorded patterns must not call Ghost Cursor")

    monkeypatch.setattr("actions_dom.generate_ghost_path", fail_if_called)
    result = asyncio.run(execute_action(
        page,
        {"id": "click", "type": "click", "params": {
            "element": "target", "button": "left", "click_count": 1,
            "hold_seconds": [0, 0],
            "trajectory": {"source": "pattern", "id": "curve"},
        }},
        {"target": "//target"}, {"curve": {"data": {"points": [
            {"x_ratio": 0, "y_ratio": 0, "dt_ms": 0},
            {"x_ratio": 1, "y_ratio": 1, "dt_ms": 0},
        ]}}}, _fixed_text,
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert page._human_pointer == (40.0, 20.0)
    assert result["trajectory_source"] == "recorded-pattern"


def test_ghost_cursor_error_propagates_without_curve_fallback(monkeypatch):
    from actions_dom import human_move_to
    from ghost_cursor_bridge import GhostCursorError

    page = _PointerPage()

    def fail(*_args, **_kwargs):
        raise GhostCursorError("worker failed")

    monkeypatch.setattr("actions_dom.generate_ghost_path", fail)

    with pytest.raises(GhostCursorError, match="worker failed"):
        asyncio.run(human_move_to(
            page,
            30,
            20,
            sleep_fn=lambda _seconds: asyncio.sleep(0),
        ))

    assert page.moves == []
    assert page._human_pointer == (10, 10)


def test_human_move_to_scales_pattern_from_current_pointer_and_clamps_target():
    from actions_dom import human_move_to

    page = _PointerPage()
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    pattern = {
        "type": "mouse",
        "data": {
            "points": [
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 10},
                {"x_ratio": 1.0, "y_ratio": 1.0, "dt_ms": 20},
            ],
            "sample_count": 2,
            "total_duration_ms": 30,
        },
    }

    asyncio.run(human_move_to(
        page, 120, 60, duration_seconds=0.2, pattern=pattern,
        rng=random.Random(3), sleep_fn=record_wait,
    ))

    assert page.moves[-1] == (
        math.nextafter(100.0, -math.inf),
        math.nextafter(50.0, -math.inf),
    )
    assert page.moves[0] == (10.0, 10.0)
    assert waits == [0.01, 0.02]


def test_pattern_move_maps_recorded_curve_to_relative_viewport_target():
    from browser_actions import execute_action

    page = _PointerPage()

    async def no_wait(_seconds):
        return None

    asyncio.run(execute_action(
        page,
        {"id": "move", "type": "move", "params": {
            "target_mode": "viewport", "element": "", "delta_viewport": [0.4, 0.4],
            "trajectory": {"source": "pattern", "id": "curve"}, "duration_seconds": [0, 0],
        }},
        {},
        {"curve": {"type": "mouse", "data": {"points": [
            {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 0},
            {"x_ratio": 0.3, "y_ratio": 0.8, "dt_ms": 0},
            {"x_ratio": 0.9, "y_ratio": 0.7, "dt_ms": 0},
        ]}}},
        _fixed_text, rng=random.Random(1), sleep_fn=no_wait,
    ))

    assert [tuple(round(value, 4) for value in point) for point in page.moves] == [
        (10.0, 10.0), (22.1348, 37.4157), (50.0, 30.0)
    ]


def test_pattern_move_to_element_maps_endpoints_inside_element():
    from browser_actions import execute_action

    page = _PointerPage()

    async def no_wait(_seconds):
        return None

    asyncio.run(execute_action(
        page,
        {"id": "move", "type": "move", "params": {
            "target_mode": "element", "element": "target", "delta_viewport": [1, 1],
            "trajectory": {"source": "pattern", "id": "curve"}, "duration_seconds": [0, 0],
        }},
        {"target": "//target"},
        {"curve": {"type": "mouse", "data": {"points": [
            {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 0},
            {"x_ratio": 0.3, "y_ratio": 0.8, "dt_ms": 0},
            {"x_ratio": 0.9, "y_ratio": 0.7, "dt_ms": 0},
        ]}}},
        _fixed_text, rng=random.Random(1), sleep_fn=no_wait,
    ))

    assert [tuple(round(value, 4) for value in point) for point in page.moves] == [
        (10.0, 10.0), (21.236, 27.9775), (40.0, 20.0)
    ]


@pytest.mark.parametrize(
    ("points", "expected_middle"),
    [
        (
            [
                {"x_ratio": 0.1, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.5, "y_ratio": 0.75, "dt_ms": 0},
                {"x_ratio": 0.9, "y_ratio": 0.5, "dt_ms": 0},
            ],
            (23.75, 32.5),
        ),
        (
            [
                {"x_ratio": 0.5, "y_ratio": 0.1, "dt_ms": 0},
                {"x_ratio": 0.25, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.5, "y_ratio": 0.9, "dt_ms": 0},
            ],
            (23.75, 32.5),
        ),
        (
            [
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.7, "y_ratio": 0.2, "dt_ms": 0},
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
            ],
            (70.0, 40.0),
        ),
    ],
)
def test_pattern_move_preserves_curve_for_horizontal_vertical_and_degenerate_baselines(
    points, expected_middle
):
    from actions_dom import human_move_to

    page = _PointerPage()

    async def no_wait(_seconds):
        return None

    asyncio.run(human_move_to(
        page, 50, 30, pattern={"type": "mouse", "data": {"points": points}},
        sleep_fn=no_wait,
    ))

    assert tuple(round(value, 4) for value in page.moves[0]) == (10.0, 10.0)
    assert tuple(round(value, 4) for value in page.moves[1]) == expected_middle
    assert tuple(round(value, 4) for value in page.moves[-1]) == (50.0, 30.0)
    assert all(0 <= value[0] < 100 and 0 <= value[1] < 50 for value in page.moves)


@pytest.mark.parametrize(
    ("points", "expected_middle"),
    [
        (
            [
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.7, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.7, "y_ratio": 0.7, "dt_ms": 0},
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
            ],
            (31.6667, 8.3333),
        ),
        (
            [
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.7, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.500001, "y_ratio": 0.5, "dt_ms": 0},
            ],
            (40.0, 25.0),
        ),
        (
            [
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.500001, "y_ratio": 0.499999, "dt_ms": 0},
                {"x_ratio": 0.500001, "y_ratio": 0.5, "dt_ms": 0},
            ],
            (20.0, 15.0),
        ),
    ],
)
def test_pattern_move_keeps_closed_and_minimal_baseline_curvature(points, expected_middle):
    from actions_dom import human_move_to

    page = _PointerPage()

    async def no_wait(_seconds):
        return None

    asyncio.run(human_move_to(
        page, 30, 20, pattern={"type": "mouse", "data": {"points": points}},
        sleep_fn=no_wait,
    ))

    assert tuple(round(value, 4) for value in page.moves[1]) == expected_middle
    assert tuple(round(value, 4) for value in page.moves[-1]) == (30.0, 20.0)
    assert all(0 <= x < 100 and 0 <= y < 50 for x, y in page.moves)


def test_relative_move_uses_live_viewport_when_page_viewport_size_is_none():
    from browser_actions import execute_action

    class Page(_PointerPage):
        def __init__(self):
            super().__init__()
            self.viewport_size = None

        async def evaluate(self, _expression):
            return {"width": 200, "height": 100}

    page = Page()

    async def no_wait(_seconds):
        return None

    asyncio.run(execute_action(
        page,
        {"id": "move", "type": "move", "params": {
            "target_mode": "viewport", "element": "", "delta_viewport": [0.5, 0.5],
            "trajectory": {"source": "builtin", "id": "bezier"}, "duration_seconds": [0, 0],
        }},
        {}, {}, _fixed_text, rng=random.Random(1), sleep_fn=no_wait,
    ))

    assert page._human_pointer == (110.0, 60.0)


@pytest.mark.parametrize("action_type", ["move", "click"])
def test_element_actions_scroll_then_use_a_visible_element_point(action_type):
    from browser_actions import execute_action

    class Locator:
        def __init__(self):
            self.scrolled = False

        async def scroll_into_view_if_needed(self):
            self.scrolled = True

        async def bounding_box(self):
            return {"x": 20, "y": 10, "width": 40, "height": 20} if self.scrolled else None

    class Page(_PointerPage):
        def __init__(self):
            super().__init__()
            self.field = Locator()

        def locator(self, _selector):
            return self.field

    page = Page()
    params = (
        {"target_mode": "element", "element": "target", "delta_viewport": [0, 0],
         "trajectory": {"source": "builtin", "id": "bezier"}, "duration_seconds": [0, 0]}
        if action_type == "move"
        else {"element": "target", "button": "left", "click_count": 1,
              "hold_seconds": [0, 0], "trajectory": {"source": "builtin", "id": "bezier"}}
    )

    asyncio.run(execute_action(
        page, {"id": action_type, "type": action_type, "params": params},
        {"target": "//target"}, {}, _fixed_text, rng=random.Random(1),
        sleep_fn=lambda _seconds: asyncio.sleep(0),
    ))

    assert page.field.scrolled is True
    assert 20 <= page._human_pointer[0] <= 60
    assert 10 <= page._human_pointer[1] <= 30


def test_execute_action_dispatches_click_scroll_and_pause_with_measurements():
    from browser_actions import execute_action

    page = _PointerPage()
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    async def unused_text(_action):
        raise AssertionError("text resolver should not run")

    click = asyncio.run(execute_action(
        page,
        {"id": "click", "type": "click", "params": {
            "element": "button", "button": "right", "click_count": 2,
            "hold_seconds": [0, 0], "trajectory": {"source": "builtin", "id": "bezier"},
        }},
        {"button": "//button"}, {}, unused_text, rng=random.Random(4), sleep_fn=record_wait,
    ))
    scroll = asyncio.run(execute_action(
        page,
        {"id": "scroll", "type": "scroll_down", "params": {
            "distance": 300, "total_count": [3, 3], "burst_count": [2, 2],
            "interval_seconds": [0.1, 0.1],
        }},
        {}, {}, unused_text, rng=random.Random(4), sleep_fn=record_wait,
    ))
    pause = asyncio.run(execute_action(
        page,
        {"id": "pause", "type": "pause", "params": {"duration_seconds": [0.2, 0.2]}},
        {}, {}, unused_text, rng=random.Random(4), sleep_fn=record_wait,
    ))

    assert click["click_count"] == 2
    assert page.clicked[-1][2]["button"] == "right"
    assert {
        key: scroll[key]
        for key in (
            "action_id",
            "type",
            "status",
            "element",
            "distance",
            "count",
            "requested_switches",
            "completed_switches",
            "wheel_events",
        )
    } == {
        "action_id": "scroll",
        "type": "scroll_down",
        "status": "ok",
        "element": "",
        "distance": 120,
        "count": 3,
        "requested_switches": 3,
        "completed_switches": 3,
        "wheel_events": 3,
    }
    assert page.wheels == [(0, 120), (0, 120), (0, 120)]
    assert pause == {
        "action_id": "pause", "type": "pause", "status": "ok", "element": "", "duration_seconds": 0.2,
    }
    assert waits[-1] == 0.2


@pytest.mark.parametrize(
    ("action_type", "signed_distance"),
    [("scroll_down", 120), ("scroll_up", -120)],
)
def test_scroll_executes_exact_count_with_fixed_internal_delta(action_type, signed_distance):
    from browser_actions import execute_action

    class FixedRng:
        def randint(self, minimum, maximum):
            assert minimum == maximum
            return minimum

        def uniform(self, minimum, maximum):
            assert (minimum, maximum) == (0.25, 0.25)
            return minimum

    page = _PointerPage()
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    result = asyncio.run(execute_action(
        page,
        {"id": "wheel", "type": action_type, "params": {
            "distance": 275,
            "total_count": [4, 4],
            "burst_count": [9, 9],
            "interval_seconds": [0.25, 0.25],
        }},
        {}, {}, _fixed_text, rng=FixedRng(), sleep_fn=record_wait,
    ))

    assert page.wheels == [(0, signed_distance)] * 4
    assert waits.count(0.25) == 3
    assert result["count"] == 4
    assert result["completed_switches"] == 4
    assert result["wheel_events"] == 4


def test_move_dispatch_scales_viewport_delta_from_the_current_pointer():
    from browser_actions import execute_action

    page = _PointerPage()
    del page._human_pointer

    async def no_wait(_seconds):
        return None

    result = asyncio.run(execute_action(
        page,
        {"id": "move", "type": "move", "params": {
            "target_mode": "viewport", "element": "", "delta_viewport": [0.1, -0.1],
            "trajectory": {"source": "builtin", "id": "bezier"}, "duration_seconds": [0, 0],
        }},
        {}, {}, _fixed_text, rng=random.Random(5), sleep_fn=no_wait,
    ))

    assert result == {
        "action_id": "move", "type": "move", "status": "ok", "element": "",
        "duration_seconds": 0.0, "trajectory_source": "ghost-cursor",
    }
    assert page.moves[-1] == (60, 20)


def test_action_config_supports_move_click_and_generated_input():
    elements, strategies = validate_action_config(
        {"A": "//button[@id='move']", "B": "//button[@id='click']", "C": "//textarea"},
        [
            {"type": "move", "element": "A"},
            {"type": "click", "element": "B"},
            {"type": "input", "element": "C", "content_source": "generated_comment"},
        ],
    )

    assert list(elements) == ["A", "B", "C"]
    assert [item["type"] for item in strategies] == ["move", "click", "input"]


def test_action_config_supports_scroll_and_pause_without_xpath_elements():
    _elements, strategies = validate_action_config(
        {},
        [
            {"type": "scroll_up", "duration": 1.5, "distance": 800},
            {"type": "scroll_down", "duration": 2},
            {"type": "pause", "duration": 3},
        ],
    )

    assert [item["type"] for item in strategies] == ["scroll_up", "scroll_down", "pause"]
    assert strategies[0]["duration"] == 1.5
    assert strategies[0]["distance"] == 120
    assert strategies[1]["distance"] == 120
