import asyncio
from types import SimpleNamespace

import pytest

from browser_element_resolver import LocatorResolutionError
from selector_probe.state_runner import ProbeSafetyError, ProbeStateRunner


class FakeMouse:
    def __init__(self, calls):
        self.calls = calls

    async def wheel(self, delta_x, delta_y):
        self.calls.append(("wheel", delta_x, delta_y))


class FakeKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        self.page.calls.append(("press", key))
        if key == "Escape":
            self.page.panel_open = False


class FakePage:
    def __init__(self, *, redirected_url="https://www.tiktok.com/"):
        self.calls = []
        self.url = "about:blank"
        self.redirected_url = redirected_url
        self.panel_open = False
        self.mouse = FakeMouse(self.calls)
        self.keyboard = FakeKeyboard(self)
        self.goto_kwargs = {}

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", url))
        self.goto_kwargs = dict(kwargs)
        self.url = self.redirected_url

    async def reload(self, **kwargs):
        self.calls.append(("reload",))

    async def evaluate(self, *_args, **_kwargs):
        raise AssertionError("state runner must not use page.evaluate")


class FakeClickLocator:
    def __init__(self, page, *, opens=False, closes=False):
        self.page = page
        self.opens = opens
        self.closes = closes
        self.click_count = 0

    async def click(self):
        self.click_count += 1
        self.page.calls.append(("click",))
        if self.opens:
            self.page.panel_open = True
        if self.closes:
            self.page.panel_open = False


class FakeVisibilityNode:
    def __init__(self, visible):
        self.visible = visible

    async def is_visible(self):
        return self.visible

    async def wait_for(self, **_kwargs):
        if self.visible:
            raise TimeoutError


class FakeVisibilityLocator:
    def __init__(self, visible_states):
        self.nodes = [FakeVisibilityNode(item) for item in visible_states]

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]

    @property
    def first(self):
        return self.nodes[0]


class FakeReadinessPage(FakePage):
    def __init__(self, skeleton_states):
        super().__init__()
        self.skeleton_states = skeleton_states

    async def title(self):
        return "TikTok"

    async def evaluate(self, *_args, **_kwargs):
        return {
            "origin": "https://www.tiktok.com",
            "ready_state": "complete",
            "root_visible": True,
            "blocked_marker": "",
            "skeleton_count": len(self.skeleton_states),
            "feed_visible": True,
            "fingerprints": ["button|comments"],
        }

    def locator(self, selector):
        if selector == "html":
            return FakeVisibilityLocator([True])
        if selector.startswith('[data-e2e*="skeleton"'):
            return FakeVisibilityLocator(self.skeleton_states)
        return FakeVisibilityLocator([])


class StepClock:
    def __init__(self, step=0.01):
        self.value = 0.0
        self.step = step

    def __call__(self):
        current = self.value
        self.value += self.step
        return current


async def no_sleep(_seconds):
    return None


def readiness(*, origin="https://www.tiktok.com", blocked=False, skeleton_timed_out=False):
    async def check(_page):
        return {
            "ready": not blocked and not skeleton_timed_out,
            "origin": origin,
            "title_or_root": True,
            "blocked_marker": "captcha" if blocked else None,
            "skeleton_timed_out": skeleton_timed_out,
        }

    return check


async def panel_scope(page, scope):
    assert scope == "visible_comment_panel"
    if page.panel_open:
        return SimpleNamespace(), {"scope_target": scope}
    raise LocatorResolutionError(
        "element_scope_not_found",
        "",
        scope,
        {"scope_target": "missing"},
    )


def entry_definition():
    return {
        "scope": "active_video",
        "locators": [
            {
                "id": "comment-entry",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }
        ],
    }


def test_forbidden_action_fails_before_page_action():
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(target_url="https://www.tiktok.com/")
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.dispatch(
                page,
                {"type": "keyboard_input", "text": "blocked"},
                {},
            )
        assert caught.value.code == "probe_action_forbidden"
        assert caught.value.action == "keyboard_input"
        assert page.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action",
    [
        {"type": "evaluate", "script": "document.body.innerHTML = ''"},
        {"type": "submit"},
        {"type": "like"},
        {"type": "follow"},
        {"type": "publish"},
        {"type": "account_update"},
    ],
)
def test_mutating_or_unknown_actions_are_rejected_before_page_call(action):
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(target_url="https://www.tiktok.com/")
        with pytest.raises(ProbeSafetyError, match="probe_action_forbidden"):
            await runner.dispatch(page, action, {})
        assert page.calls == []

    asyncio.run(scenario())


def test_feed_ready_navigates_and_returns_complete_readiness_evidence():
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
        )
        result = await runner.ensure_state(page, "feed_ready", {})
        assert result == {
            "ready": True,
            "expected_origin": "https://www.tiktok.com",
            "origin": "https://www.tiktok.com",
            "origin_ok": True,
            "title_or_root": True,
            "blocked_marker": None,
            "skeleton_timed_out": False,
            "state": "feed_ready",
        }
        assert page.calls == [("goto", "https://www.tiktok.com/")]
        assert page.goto_kwargs == {
            "wait_until": "commit",
            "timeout": 30_000,
        }

    asyncio.run(scenario())


def test_readiness_rejects_redirected_origin():
    async def scenario():
        page = FakePage(redirected_url="https://accounts.example.test/login")
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(origin="https://accounts.example.test"),
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(page, "feed_ready", {})
        assert caught.value.code == "probe_origin_mismatch"
        assert page.calls == [("goto", "https://www.tiktok.com/")]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("check", "code"),
    [
        (readiness(blocked=True), "probe_page_blocked"),
        (readiness(skeleton_timed_out=True), "probe_readiness_timeout"),
    ],
)
def test_readiness_reports_block_and_skeleton_timeout(check, code):
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=check,
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(page, "feed_ready", {})
        assert caught.value.code == code

    asyncio.run(scenario())


def test_readiness_waits_for_every_skeleton_not_only_first_match():
    async def scenario():
        page = FakeReadinessPage([False, True])
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_timeout_ms=1,
            readiness_poll_interval_seconds=0.001,
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(page, "feed_ready", {})
        assert caught.value.code == "page_readiness_timeout"

    asyncio.run(scenario())


def test_readiness_fails_closed_when_skeleton_scan_limit_is_exceeded():
    async def scenario():
        page = FakeReadinessPage([False, False, False])
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_timeout_ms=1,
            max_skeleton_nodes=2,
            readiness_poll_interval_seconds=0.001,
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(page, "feed_ready", {})
        assert caught.value.code == "page_readiness_timeout"

    asyncio.run(scenario())


def test_origin_change_is_rejected_before_scroll_or_click():
    async def resolver(*_args):
        raise AssertionError("resolver must not run on an unsafe origin")

    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
        )
        await runner.ensure_state(page, "feed_ready", {})
        page.calls.clear()
        page.url = "https://evil.example.test/"

        with pytest.raises(ProbeSafetyError, match="probe_origin_mismatch"):
            await runner.dispatch(
                page,
                {"type": "bounded_scroll", "steps": 1, "delta_y": 100},
                {},
            )
        assert page.calls == []

        with pytest.raises(ProbeSafetyError, match="probe_origin_mismatch"):
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {"评论入口": entry_definition()},
            )
        assert page.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("unprovable_url", ["", "about:blank", "data:text/html,blank"])
def test_unprovable_current_origin_never_falls_back_to_last_safe_origin(unprovable_url):
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
        )
        await runner.ensure_state(page, "feed_ready", {})
        page.calls.clear()
        page.url = unprovable_url

        with pytest.raises(ProbeSafetyError) as caught:
            await runner.dispatch(
                page,
                {"type": "bounded_scroll", "steps": 1, "delta_y": 100},
                {},
            )
        assert caught.value.code == "probe_origin_mismatch"
        assert page.calls == []

    asyncio.run(scenario())


def test_open_comment_panel_resolves_entry_clicks_once_and_verifies_state():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)

        async def resolver(_page, alias, definition):
            assert alias == "评论入口"
            assert definition == entry_definition()
            return SimpleNamespace(locator=locator, candidate={"id": "comment-entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
        )
        await runner.ensure_state(page, "feed_ready", {})
        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {"评论入口": entry_definition()},
        )
        assert result["state"] == "comment_panel_open"
        assert result["clicked"] is True
        assert result["alias"] == "评论入口"
        assert locator.click_count == 1

    asyncio.run(scenario())


def test_comment_panel_accepts_only_valid_run_local_entry_override():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)
        seen = []

        async def resolver(_page, alias, definition):
            seen.append((alias, definition))
            return SimpleNamespace(locator=locator, candidate={"id": "probe"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
        )
        await runner.ensure_state(page, "feed_ready", {})
        await runner.ensure_state(
            page,
            "comment_panel_open",
            {},
            comment_entry_override=entry_definition(),
        )

        assert seen == [(runner.comment_entry_alias, entry_definition())]
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "feed_ready",
                {},
                comment_entry_override=entry_definition(),
            )
        assert caught.value.code == "probe_override_forbidden"

    asyncio.run(scenario())


def test_open_polls_delayed_panel_without_repeating_click():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page)
        checks = 0

        async def resolver(_page, _alias, _definition):
            return SimpleNamespace(locator=locator, candidate={"id": "comment-entry"})

        async def delayed_panel(_page, scope):
            nonlocal checks
            assert scope == "visible_comment_panel"
            checks += 1
            if checks >= 3:
                return SimpleNamespace(), {"scope_target": scope}
            raise LocatorResolutionError(
                "element_scope_not_found",
                "",
                scope,
                {},
            )

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=delayed_panel,
            panel_timeout_seconds=0.2,
            poll_interval_seconds=0.01,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(),
        )
        await runner.ensure_state(page, "feed_ready", {})
        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {"评论入口": entry_definition()},
        )
        assert result["state"] == "comment_panel_open"
        assert locator.click_count == 1
        assert checks == 3

    asyncio.run(scenario())


def test_open_detects_panel_retained_after_reload_without_toggling_it_closed():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page)

        async def resolver(_page, _alias, _definition):
            return SimpleNamespace(
                locator=locator,
                candidate={"id": "comment-entry"},
            )

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
        )
        await runner.ensure_state(page, "feed_ready", {})
        page.panel_open = True

        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {"璇勮鍏ュ彛": entry_definition()},
        )

        assert result == {
            "state": "comment_panel_open",
            "clicked": False,
            "panel_visible": True,
        }
        assert locator.click_count == 0

    asyncio.run(scenario())


def test_open_failure_never_retries_dispatched_click():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page)

        async def resolver(_page, _alias, _definition):
            return SimpleNamespace(locator=locator, candidate={"id": "comment-entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_timeout_seconds=0.02,
            poll_interval_seconds=0.01,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(),
        )
        await runner.ensure_state(page, "feed_ready", {})
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {"评论入口": entry_definition()},
            )
        assert caught.value.code == "probe_state_verification_failed"
        assert locator.click_count == 1
        assert runner.current_state == "feed_ready"

    asyncio.run(scenario())


def test_close_polls_delayed_absence_without_repeating_escape():
    async def scenario():
        page = FakePage()
        page.url = "https://www.tiktok.com/"
        checks = 0

        async def delayed_close(_page, scope):
            nonlocal checks
            assert scope == "visible_comment_panel"
            checks += 1
            if checks <= 2:
                return SimpleNamespace(), {"scope_target": scope}
            raise LocatorResolutionError(
                "element_scope_not_found",
                "",
                scope,
                {},
            )

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=delayed_close,
            panel_timeout_seconds=0.2,
            poll_interval_seconds=0.01,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(),
        )
        runner.current_state = "comment_panel_open"
        result = await runner.ensure_state(page, "comment_panel_closed", {})
        assert result["state"] == "comment_panel_closed"
        assert page.calls == [("press", "Escape")]
        assert checks == 3

    asyncio.run(scenario())


def test_close_comment_panel_uses_escape_once_and_verifies_absence():
    async def scenario():
        page = FakePage()
        page.url = "https://www.tiktok.com/"
        page.panel_open = True
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=panel_scope,
        )
        runner.current_state = "comment_panel_open"
        result = await runner.ensure_state(page, "comment_panel_closed", {})
        assert result == {
            "state": "comment_panel_closed",
            "closed_with": "escape",
            "panel_visible": False,
        }
        assert page.calls == [("press", "Escape")]

    asyncio.run(scenario())


def test_close_comment_panel_can_use_configured_locator():
    close_definition = {
        "scope": "visible_comment_panel",
        "locators": [
            {
                "id": "close",
                "type": "attribute",
                "name": "aria-label",
                "value": "Close",
                "enabled": True,
            }
        ],
    }

    async def scenario():
        page = FakePage()
        page.url = "https://www.tiktok.com/"
        page.panel_open = True
        locator = FakeClickLocator(page, closes=True)

        async def resolver(_page, alias, definition):
            assert alias == "评论面板关闭"
            assert definition == close_definition
            return SimpleNamespace(locator=locator, candidate={"id": "close"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            element_resolver=resolver,
            scope_resolver=panel_scope,
            comment_close_alias="评论面板关闭",
        )
        runner.current_state = "comment_panel_open"
        result = await runner.ensure_state(
            page,
            "comment_panel_closed",
            {"评论面板关闭": close_definition},
        )
        assert result["closed_with"] == "locator"
        assert locator.click_count == 1
        assert page.calls == [("click",)]

    asyncio.run(scenario())


def test_close_requires_open_state_before_page_action():
    async def scenario():
        page = FakePage()
        page.url = "https://www.tiktok.com/"
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=panel_scope,
        )
        runner.current_state = "feed_ready"
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(page, "comment_panel_closed", {})
        assert caught.value.code == "probe_transition_forbidden"
        assert page.calls == []

    asyncio.run(scenario())


def test_bounded_scroll_enforces_step_and_delta_limits_before_page_action():
    async def scenario():
        page = FakePage()
        page.url = "https://www.tiktok.com/"
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            max_scroll_steps=3,
            max_scroll_delta=500,
        )
        runner.current_state = "feed_ready"

        for action in (
            {"type": "bounded_scroll", "steps": 4, "delta_y": 100},
            {"type": "bounded_scroll", "steps": 1, "delta_y": 501},
            {"type": "bounded_scroll", "steps": True, "delta_y": 100},
        ):
            with pytest.raises(ProbeSafetyError) as caught:
                await runner.dispatch(page, action, {})
            assert caught.value.code == "probe_action_invalid"
            assert page.calls == []

        result = await runner.dispatch(
            page,
            {"type": "bounded_scroll", "steps": 3, "delta_y": -500},
            {},
        )
        assert result == {"state": "feed_ready", "steps": 3, "delta_y": -500}
        assert page.calls == [
            ("wheel", 0, -500),
            ("wheel", 0, -500),
            ("wheel", 0, -500),
        ]

    asyncio.run(scenario())


def test_unknown_state_is_rejected_before_page_action():
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(target_url="https://www.tiktok.com/")
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(page, "comment_form_filled", {})
        assert caught.value.code == "probe_state_unsupported"
        assert page.calls == []

    asyncio.run(scenario())
