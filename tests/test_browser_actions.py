import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import browser_actions
import browser_video_switch
from browser_actions import execute_action
from browser_element_resolver import LocatorResolutionError, ResolvedElement
from browser_page_lifecycle import PageLifecycle
from browser_strategy_config import DEFAULT_ACTION_PARAMS


def action(action_id, action_type, **params):
    values = dict(DEFAULT_ACTION_PARAMS[action_type])
    values.update(params)
    return {"id": action_id, "type": action_type, "params": values}


def run(coro):
    return asyncio.run(coro)


async def no_sleep(_seconds):
    return None


class FakeMouse:
    def __init__(self):
        self.wheel_calls = []

    async def wheel(self, x, y):
        self.wheel_calls.append((x, y))


class FakePage:
    def __init__(self, url="https://www.tiktok.com/replacement"):
        self.url = url
        self.mouse = FakeMouse()
        self.visible = True

    def is_closed(self):
        return False

    async def evaluate(self, _expression):
        return "visible" if self.visible else "hidden"


class VerifiedFeedMouse:
    def __init__(self, page):
        self.page = page
        self.move_calls = []
        self.wheel_calls = []

    async def move(self, x, y):
        self.move_calls.append((x, y))

    async def wheel(self, delta_x, delta_y):
        self.wheel_calls.append((delta_x, delta_y))
        self.page.video_index += 1 if delta_y > 0 else -1


class ClosingVerifiedFeedMouse(VerifiedFeedMouse):
    def __init__(self, page, *, close_on):
        super().__init__(page)
        self.close_on = close_on
        self.successes = 0

    @property
    def attempts(self):
        return len(self.wheel_calls)

    async def wheel(self, delta_x, delta_y):
        self.wheel_calls.append((delta_x, delta_y))
        if self.attempts == self.close_on:
            raise RuntimeError(
                "Mouse.wheel: Target page, context or browser has been closed"
            )
        self.successes += 1
        self.page.video_index += 1 if delta_y > 0 else -1


class VerifiedFeedPage(FakePage):
    def __init__(self, url="https://www.tiktok.com/feed"):
        super().__init__(url)
        self.video_index = 0
        self.mouse = VerifiedFeedMouse(self)

    async def evaluate(self, expression):
        if expression == "document.visibilityState":
            return "visible"
        assert "#column-list-container" in expression
        return {
            "identity": f"video:{self.video_index}",
            "container_x": 10,
            "container_y": 20,
            "container_width": 360,
            "container_height": 945,
            "scroll_top": self.video_index * 945,
        }


class TrackingRng:
    def __init__(self):
        self.randint_calls = []
        self.uniform_calls = []

    def randint(self, low, high):
        self.randint_calls.append((low, high))
        return high

    def uniform(self, low, high):
        self.uniform_calls.append((low, high))
        return low


class FakePageThatClosesOnSecondWheel(FakePage):
    class ClosingMouse:
        def __init__(self):
            self.attempts = 0
            self.wheel_calls = []

        async def wheel(self, x, y):
            self.attempts += 1
            if self.attempts == 2:
                raise RuntimeError(
                    "Mouse.wheel: Target page, context or browser has been closed"
                )
            self.wheel_calls.append((x, y))

    def __init__(self):
        super().__init__("https://www.tiktok.com:8443/original")
        self.mouse = self.ClosingMouse()


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class ActionMouse:
    def __init__(self, click_error=None):
        self.click_calls = []
        self.move_calls = []
        self.click_error = click_error

    async def move(self, x, y, **_kwargs):
        self.move_calls.append((x, y))

    async def click(self, x, y, **kwargs):
        self.click_calls.append((x, y, kwargs))
        if self.click_error is not None:
            raise self.click_error


class ActionKeyboard:
    def __init__(self, page):
        self.page = page

    async def type(self, character):
        self.page.focused.value += character


class ActionLocator:
    def __init__(self, page, *, value="", box=None, editable=True):
        self.page = page
        self.value = value
        self.box = box or {"x": 20, "y": 30, "width": 40, "height": 20}
        self.editable = editable
        self.focus_calls = 0
        self.scroll_calls = 0

    async def scroll_into_view_if_needed(self):
        self.scroll_calls += 1

    async def bounding_box(self):
        return self.box

    async def focus(self):
        self.focus_calls += 1
        self.page.focused = self

    async def input_value(self):
        return self.value

    async def evaluate(self, _expression):
        return self.editable


class ActionPage:
    def __init__(self, *, click_error=None, panel_visible=True):
        self.url = "https://www.tiktok.com/@target"
        self.viewport_size = {"width": 320, "height": 240}
        self.mouse = ActionMouse(click_error)
        self.keyboard = ActionKeyboard(self)
        self.focused = None
        self.locator_calls = []
        self.panel_visible = panel_visible

    def locator(self, selector):
        self.locator_calls.append(selector)
        if selector == '[data-e2e="comment-input"]:visible':
            return SimpleCountLocator(1 if self.panel_visible else 0)
        raise AssertionError("element actions must use the resolved locator")


class SimpleCountLocator:
    def __init__(self, count):
        self._count = count

    async def count(self):
        return self._count


class StepClock:
    def __init__(self, step=1.0):
        self.value = -step
        self.step = step

    def __call__(self):
        self.value += self.step
        return self.value


class ManualClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    async def sleep(self, seconds):
        self.value += seconds


def canonical_definition(scope="active_video", candidate_id="entry-primary"):
    return {
        "scope": scope,
        "locators": [
            {
                "id": candidate_id,
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }
        ],
    }


def resolved_element(
    page,
    locator,
    *,
    alias="评论入口",
    scope="active_video",
    candidate_id="entry-primary",
    candidate_type="attribute",
):
    return ResolvedElement(
        locator=locator,
        alias=alias,
        scope=scope,
        candidate={"id": candidate_id, "type": candidate_type},
        diagnostics={"candidates": []},
    )


def test_click_resolves_alias_inside_active_video_once(monkeypatch):
    page = ActionPage()
    locator = ActionLocator(page)
    resolved = resolved_element(page, locator)
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)

    result = run(
        execute_action(
            page,
            action("click-entry", "click", element="评论入口"),
            {"评论入口": canonical_definition()},
            {},
            lambda _item: "",
            sleep_fn=no_sleep,
        )
    )

    resolver.assert_awaited_once()
    assert page.mouse.click_calls and len(page.mouse.click_calls) == 1
    assert page.locator_calls == ['[data-e2e="comment-input"]:visible']
    assert result["locator"] == {
        "scope": "active_video",
        "candidate_id": "entry-primary",
        "candidate_type": "attribute",
    }
    assert result["postcondition"] == "observed"


def test_fallback_does_not_repeat_a_dispatched_click(monkeypatch):
    page = ActionPage(click_error=RuntimeError("page replaced after dispatch"))
    locator = ActionLocator(page)
    resolved = resolved_element(page, locator, candidate_id="fallback-entry")
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)
    definition = canonical_definition(candidate_id="primary-entry")
    definition["locators"].append(
        {
            "id": "fallback-entry",
            "type": "role",
            "role": "button",
            "name": "comments",
            "name_mode": "contains",
            "enabled": True,
            "fallback": True,
        }
    )

    with pytest.raises(RuntimeError, match="page replaced after dispatch"):
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": definition},
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
            )
        )

    resolver.assert_awaited_once()
    assert len(page.mouse.click_calls) == 1


def test_keyboard_targets_actual_editable_descendant(monkeypatch):
    page = ActionPage()
    wrapper = ActionLocator(page)
    editable = ActionLocator(page)
    resolved = resolved_element(
        page,
        editable,
        alias="评论输入框",
        scope="visible_comment_panel",
        candidate_id="comment-input",
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)
    definition = canonical_definition(
        scope="visible_comment_panel",
        candidate_id="comment-input",
    )
    definition["locators"][0]["descendant"] = {
        "type": "attribute",
        "name": "contenteditable",
        "value": "true",
        "role": "textbox",
    }

    result = run(
        execute_action(
            page,
            action(
                "type-comment",
                "keyboard_input",
                element="评论输入框",
                content={"source": "fixed", "text": "hello", "brand_id": ""},
                typing={"source": "builtin", "interval_ms": [0, 0]},
            ),
            {"评论输入框": definition},
            {},
            lambda _item: "hello",
            sleep_fn=no_sleep,
        )
    )

    assert result["status"] == "ok"
    assert result["text"] == "hello"
    assert result["locator"]["candidate_id"] == "comment-input"
    assert editable.focus_calls == 1
    assert wrapper.focus_calls == 0
    assert page.locator_calls == []


def test_keyboard_rejects_non_editable_resolved_wrapper(monkeypatch):
    page = ActionPage()
    wrapper = ActionLocator(page, editable=False)
    resolved = resolved_element(
        page,
        wrapper,
        alias="评论输入框",
        scope="visible_comment_panel",
        candidate_id="comment-input-wrapper",
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action(
                    "type-comment",
                    "keyboard_input",
                    element="评论输入框",
                    content={"source": "fixed", "text": "hello", "brand_id": ""},
                    typing={"source": "builtin", "interval_ms": [0, 0]},
                ),
                {
                    "评论输入框": canonical_definition(
                        scope="visible_comment_panel",
                        candidate_id="comment-input-wrapper",
                    )
                },
                {},
                lambda _item: "hello",
                sleep_fn=no_sleep,
            )
        )

    assert caught.value.code == "element_not_actionable"
    assert wrapper.focus_calls == 0


def test_move_resolves_alias_and_preserves_trajectory_engine(monkeypatch):
    page = ActionPage()
    locator = ActionLocator(page)
    resolved = resolved_element(page, locator, alias="目标")
    resolver = AsyncMock(return_value=resolved)
    move_to = AsyncMock(return_value=(40, 40))
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)
    monkeypatch.setattr(browser_actions, "human_move_to", move_to)

    result = run(
        execute_action(
            page,
            action("move-target", "move", element="目标"),
            {"目标": canonical_definition()},
            {},
            lambda _item: "",
            sleep_fn=no_sleep,
        )
    )

    resolver.assert_awaited_once()
    move_to.assert_awaited_once()
    assert result["trajectory_source"] == "ghost-cursor"
    assert result["locator"]["candidate_id"] == "entry-primary"
    assert page.locator_calls == []


def test_action_boundary_migrates_legacy_selector_before_resolution(monkeypatch):
    page = ActionPage()
    locator = ActionLocator(page)
    resolved = resolved_element(page, locator, alias="目标")
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)
    monkeypatch.setattr(
        browser_actions,
        "human_move_to",
        AsyncMock(return_value=(40, 40)),
    )

    run(
        execute_action(
            page,
            action("move-target", "move", element="目标"),
            {"目标": "//article[@id='legacy-target']"},
            {},
            lambda _item: "",
            sleep_fn=no_sleep,
        )
    )

    definition = resolver.await_args.args[2]
    assert definition["scope"] == "page"
    assert definition["locators"][0]["type"] == "xpath"
    assert definition["locators"][0]["value"] == "//article[@id='legacy-target']"


def test_legacy_comment_entry_alias_observes_panel_after_one_click(monkeypatch):
    page = ActionPage(panel_visible=True)
    locator = ActionLocator(page)
    resolved = resolved_element(
        page,
        locator,
        alias="评论入口",
        scope="page",
        candidate_id="legacy-entry",
        candidate_type="xpath",
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)

    result = run(
        execute_action(
            page,
            action("click-entry", "click", element="评论入口"),
            {"评论入口": "//article[@id='legacy']//button"},
            {},
            lambda _item: "",
            sleep_fn=no_sleep,
        )
    )

    assert len(page.mouse.click_calls) == 1
    assert result["postcondition"] == "observed"
    assert result["locator"] == {
        "scope": "page",
        "candidate_id": "legacy-entry",
        "candidate_type": "xpath",
    }
    assert "//article" not in str(result["locator"])


def test_comment_entry_waits_for_delayed_locator_before_one_click(monkeypatch):
    page = ActionPage(panel_visible=True)
    locator = ActionLocator(page)
    resolved = resolved_element(page, locator)
    resolver = AsyncMock(
        side_effect=[
            LocatorResolutionError(
                "element_candidate_not_found", "评论入口", "active_video", {}
            ),
            LocatorResolutionError(
                "element_candidate_not_found", "评论入口", "active_video", {}
            ),
            resolved,
        ]
    )
    clock = ManualClock()
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)
    monkeypatch.setattr(
        browser_actions,
        "human_move_to",
        AsyncMock(return_value=(40, 40)),
    )

    result = run(
        execute_action(
            page,
            action("click-entry", "click", element="评论入口"),
            {"评论入口": canonical_definition()},
            {},
            lambda _item: "",
            sleep_fn=clock.sleep,
            monotonic_fn=clock.monotonic,
        )
    )

    assert resolver.await_count == 3
    assert len(page.mouse.click_calls) == 1
    assert result["postcondition"] == "observed"
    assert clock.value == pytest.approx(0.2, abs=1e-9)


def test_comment_entry_slow_success_after_readiness_deadline_never_clicks(
    monkeypatch,
):
    page = ActionPage(panel_visible=True)
    locator = ActionLocator(page)
    resolved = resolved_element(
        page,
        locator,
        alias="comment-entry",
        candidate_id="tiktok-comment-entry-primary",
    )
    clock = ManualClock()

    async def slow_success(_page, _alias, _definition):
        clock.value = 3.01
        return resolved

    monkeypatch.setattr(browser_actions, "resolve_element", slow_success)
    monkeypatch.setattr(
        browser_actions,
        "human_move_to",
        AsyncMock(return_value=(40, 40)),
    )

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="comment-entry"),
                {
                    "comment-entry": canonical_definition(
                        candidate_id="tiktok-comment-entry-primary"
                    )
                },
                {},
                lambda _item: "",
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )
        )

    assert caught.value.code == "element_candidate_not_found"
    assert caught.value.alias == "comment-entry"
    assert caught.value.scope == "active_video"
    assert caught.value.diagnostics == {}
    assert len(page.mouse.click_calls) == 0


def test_comment_entry_hanging_resolution_is_cancelled_without_click(
    monkeypatch,
):
    page = ActionPage(panel_visible=True)
    cancelled = []

    async def hanging_resolution(_page, _alias, _definition):
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    monkeypatch.setattr(browser_actions, "resolve_element", hanging_resolution)
    monkeypatch.setitem(
        browser_actions._resolve_comment_entry_when_ready.__kwdefaults__,
        "timeout_seconds",
        0.01,
    )

    async def guarded_action():
        return await asyncio.wait_for(
            execute_action(
                page,
                action("click-entry", "click", element="comment-entry"),
                {
                    "comment-entry": canonical_definition(
                        candidate_id="tiktok-comment-entry-primary"
                    )
                },
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
            ),
            timeout=0.05,
        )

    with pytest.raises(LocatorResolutionError) as caught:
        run(guarded_action())

    assert caught.value.code == "element_candidate_not_found"
    assert caught.value.alias == "comment-entry"
    assert caught.value.scope == "active_video"
    assert caught.value.diagnostics == {}
    assert cancelled == [True]
    assert len(page.mouse.click_calls) == 0


def test_comment_entry_does_not_retry_ambiguous_locator(monkeypatch):
    page = ActionPage(panel_visible=True)
    resolver = AsyncMock(
        side_effect=LocatorResolutionError(
            "element_candidate_ambiguous", "评论入口", "active_video", {}
        )
    )
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": canonical_definition()},
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
            )
        )

    assert caught.value.code == "element_candidate_ambiguous"
    assert resolver.await_count == 1
    assert len(page.mouse.click_calls) == 0


def test_comment_entry_locator_timeout_never_dispatches_click(monkeypatch):
    page = ActionPage(panel_visible=True)
    resolver = AsyncMock(
        side_effect=LocatorResolutionError(
            "element_candidate_not_found", "评论入口", "active_video", {}
        )
    )
    clock = ManualClock()
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": canonical_definition()},
                {},
                lambda _item: "",
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )
        )

    assert caught.value.code == "element_candidate_not_found"
    assert len(page.mouse.click_calls) == 0
    assert clock.value == pytest.approx(3.0, abs=1e-9)


def test_legacy_comment_entry_timeout_never_repeats_click(monkeypatch):
    page = ActionPage(panel_visible=False)
    locator = ActionLocator(page)
    resolved = resolved_element(
        page,
        locator,
        alias="评论入口",
        scope="page",
        candidate_id="legacy-entry",
        candidate_type="xpath",
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": "//article[@id='legacy']//button"},
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
                monotonic_fn=StepClock(),
            )
        )

    assert caught.value.code == "element_postcondition_not_observed"
    assert len(page.mouse.click_calls) == 1
    resolver.assert_awaited_once()


def test_comment_postcondition_polling_crops_sleep_to_exact_deadline(monkeypatch):
    page = ActionPage()
    locator = ActionLocator(page)
    resolver = AsyncMock(
        return_value=resolved_element(page, locator, alias="评论入口")
    )
    clock = ManualClock()
    monkeypatch.setattr(browser_actions, "resolve_element", resolver)
    monkeypatch.setattr(
        browser_actions,
        "human_move_to",
        AsyncMock(return_value=(40, 40)),
    )
    monkeypatch.setattr(
        browser_actions,
        "_comment_panel_visible",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        browser_actions,
        "COMMENT_PANEL_TIMEOUT_SECONDS",
        0.25,
        raising=False,
    )

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": canonical_definition()},
                {},
                lambda _item: "",
                sleep_fn=clock.sleep,
                monotonic_fn=clock.monotonic,
            )
        )

    assert caught.value.code == "element_postcondition_not_observed"
    assert clock.value == pytest.approx(0.25, abs=1e-9)
    assert len(page.mouse.click_calls) == 1


def test_comment_postcondition_slow_query_is_cancelled_at_deadline(monkeypatch):
    page = ActionPage()
    locator = ActionLocator(page)
    resolver = AsyncMock(
        return_value=resolved_element(page, locator, alias="评论入口")
    )

    async def slow_panel_query(_page):
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr(browser_actions, "resolve_element", resolver)
    monkeypatch.setattr(
        browser_actions,
        "human_move_to",
        AsyncMock(return_value=(40, 40)),
    )
    monkeypatch.setattr(
        browser_actions,
        "_comment_panel_visible",
        slow_panel_query,
    )
    monkeypatch.setattr(
        browser_actions,
        "COMMENT_PANEL_TIMEOUT_SECONDS",
        0.01,
        raising=False,
    )

    started = time.monotonic()
    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": canonical_definition()},
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
            )
        )
    elapsed = time.monotonic() - started

    assert caught.value.code == "element_postcondition_not_observed"
    assert elapsed < 0.04
    assert len(page.mouse.click_calls) == 1


def test_comment_entry_postcondition_times_out_without_click_retry(monkeypatch):
    page = ActionPage(panel_visible=False)
    locator = ActionLocator(page)
    resolved = resolved_element(page, locator)
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)

    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                page,
                action("click-entry", "click", element="评论入口"),
                {"评论入口": canonical_definition()},
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
                monotonic_fn=StepClock(),
            )
        )

    assert caught.value.code == "element_postcondition_not_observed"
    assert len(page.mouse.click_calls) == 1
    resolver.assert_awaited_once()


def test_submit_click_dispatches_once_and_marks_unconfigured_postcondition(monkeypatch):
    page = ActionPage()
    locator = ActionLocator(page)
    resolved = resolved_element(
        page,
        locator,
        alias="评论提交按钮",
        scope="visible_comment_panel",
        candidate_id="comment-submit",
    )
    resolver = AsyncMock(return_value=resolved)
    monkeypatch.setattr(browser_actions, "resolve_element", resolver, raising=False)
    definition = {
        "scope": "visible_comment_panel",
        "locators": [
            {
                "id": "comment-submit",
                "type": "css",
                "value": 'button[data-e2e="comment-post"]',
                "enabled": True,
            }
        ],
    }

    result = run(
        execute_action(
            page,
            action("submit", "click", element="评论提交按钮"),
            {"评论提交按钮": definition},
            {},
            lambda _item: "",
            sleep_fn=no_sleep,
        )
    )

    assert len(page.mouse.click_calls) == 1
    resolver.assert_awaited_once()
    assert result["postcondition"] == "not_configured"
    assert result["locator"]["candidate_id"] == "comment-submit"


def test_missing_element_alias_raises_structured_resolution_error():
    with pytest.raises(LocatorResolutionError) as caught:
        run(
            execute_action(
                ActionPage(),
                action("missing", "click", element="missing"),
                {},
                {},
                lambda _item: "",
                sleep_fn=no_sleep,
            )
        )

    assert caught.value.code == "element_alias_missing"
    assert caught.value.alias == "missing"


def test_scroll_recovery_resumes_without_duplicating_wheel_count():
    first = VerifiedFeedPage("https://www.tiktok.com:8443/original")
    first.mouse = ClosingVerifiedFeedMouse(first, close_on=2)
    second = VerifiedFeedPage()
    lifecycle = PageLifecycle(
        FakeContext([second, first]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )
    item = action(
        "scroll",
        "scroll_down",
        total_count=[3, 3],
        burst_count=[1, 1],
        interval_seconds=[0, 0],
    )

    result = run(
        execute_action(
            first,
            item,
            {},
            {},
            lambda _item: "",
            page_lifecycle=lifecycle,
            sleep_fn=no_sleep,
        )
    )

    assert first.mouse.wheel_calls == [(0, 120), (0, 120)]
    assert second.mouse.wheel_calls == [(0, 120), (0, 120)]
    assert result["count"] == 3
    assert result["completed_switches"] == 3
    assert result["wheel_events"] == 3
    assert result["_active_page"] is second
    assert result["_page_recoveries"] == [
        {
            "action_id": "scroll",
            "action_type": "scroll_down",
            "old_page_origin": "https://www.tiktok.com:8443",
            "new_page_origin": "https://www.tiktok.com",
            "closure_type": "target_closed",
            "closure_reason": "target page, context or browser has been closed",
            "replacement_found": True,
            "retry": 1,
            "status": "recovered",
            "outcome": "recovered",
        }
    ]


def test_scroll_action_samples_total_range_once_and_returns_verified_measurements(
    monkeypatch,
):
    page = VerifiedFeedPage()
    rng = TrackingRng()
    monkeypatch.setattr(browser_video_switch, "_monotonic", time.monotonic)

    result = run(
        execute_action(
            page,
            action(
                "verified-scroll",
                "scroll_down",
                total_count=[2, 3],
                burst_count=[5, 9],
                interval_seconds=[0, 0],
            ),
            {},
            {},
            lambda _item: "",
            rng=rng,
            sleep_fn=no_sleep,
        )
    )

    assert rng.randint_calls == [(2, 3)]
    assert result["requested_switches"] == 3
    assert result["completed_switches"] == 3
    assert result["wheel_events"] == 3
    assert result["count"] == 3


def test_scroll_recovery_is_attempted_at_most_once():
    first = VerifiedFeedPage("https://www.tiktok.com/original")
    first.mouse = ClosingVerifiedFeedMouse(first, close_on=1)
    second = VerifiedFeedPage()
    second.mouse = ClosingVerifiedFeedMouse(second, close_on=1)
    lifecycle = PageLifecycle(
        FakeContext([second, first]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    with pytest.raises(
        browser_video_switch.VideoSwitchError,
        match="video_switch_closed_target",
    ) as caught:
        run(
            execute_action(
                first,
                action(
                    "scroll",
                    "scroll_down",
                    total_count=[1, 1],
                    burst_count=[1, 1],
                    interval_seconds=[0, 0],
                ),
                {},
                {},
                lambda _item: "",
                page_lifecycle=lifecycle,
                sleep_fn=no_sleep,
            )
        )

    assert first.mouse.attempts == 1
    assert second.mouse.attempts == 1
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 0
    assert caught.value.page_recoveries == [
        {
            "action_id": "scroll",
            "action_type": "scroll_down",
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "https://www.tiktok.com",
            "closure_type": "target_closed",
            "closure_reason": "target page, context or browser has been closed",
            "replacement_found": True,
            "retry": 1,
            "status": "failed",
            "outcome": "retry_failed",
        }
    ]


def test_scroll_recovery_then_later_wheel_failure_attaches_all_events_in_order():
    first = VerifiedFeedPage("https://www.tiktok.com/original")
    first.mouse = ClosingVerifiedFeedMouse(first, close_on=1)
    second = VerifiedFeedPage("https://www.tiktok.com/replacement")
    second.mouse = ClosingVerifiedFeedMouse(second, close_on=2)
    lifecycle = PageLifecycle(
        FakeContext([second, first]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    with pytest.raises(
        browser_video_switch.VideoSwitchError,
        match="video_switch_closed_target",
    ) as caught:
        run(
            execute_action(
                first,
                action(
                    "scroll-chain",
                    "scroll_down",
                    total_count=[2, 2],
                    burst_count=[1, 1],
                    interval_seconds=[0, 0],
                ),
                {},
                {},
                lambda _item: "",
                page_lifecycle=lifecycle,
                sleep_fn=no_sleep,
            )
        )

    assert first.mouse.attempts == 1
    assert second.mouse.attempts == 2
    assert second.mouse.successes == 1
    assert caught.value.completed_switches == 1
    assert caught.value.wheel_events == 1
    assert [
        (event["outcome"], event["retry"])
        for event in caught.value.page_recoveries
    ] == [
        ("recovered", 1),
        ("not_retried", 0),
    ]
