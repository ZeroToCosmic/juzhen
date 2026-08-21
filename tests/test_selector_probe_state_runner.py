import asyncio
from types import SimpleNamespace

import pytest

from browser_element_resolver import LocatorResolutionError
from selector_probe import state_runner as state_runner_module
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
        self.shell_visible = False
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

    def locator(self, selector):
        visible = (
            self.shell_visible
            and selector
            == state_runner_module._COMMENT_PANEL_SHELL_SELECTOR
        )
        return FakeVisibilityLocator([visible])


class FakeClickLocator:
    def __init__(
        self,
        page,
        *,
        opens=False,
        closes=False,
        aria_expanded=None,
    ):
        self.page = page
        self.opens = opens
        self.closes = closes
        self.aria_expanded = aria_expanded
        self.click_count = 0

    async def get_attribute(self, name):
        return self.aria_expanded if name == "aria-expanded" else None

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


def panel_sample(**overrides):
    value = {
        "panel_visible": True,
        "input_visible": True,
        "textbox_visible": True,
        "submit_visible": True,
        "submit_disabled": True,
        "loading_marker": "",
        "aria_busy": False,
        "fingerprint_hash": "sha256:stable",
    }
    value.update(overrides)
    return value


def panel_sequence(*samples):
    calls = []

    async def check(_page):
        index = min(len(calls), len(samples) - 1)
        calls.append(index)
        return dict(samples[index])

    check.calls = calls
    return check


async def stable_panel(_page):
    return panel_sample()


class FakeScopedNode:
    def __init__(
        self,
        *,
        a11y="",
        attributes=None,
        disabled=False,
        editable=True,
        visible=True,
    ):
        self.a11y = a11y
        self.attributes = attributes or {}
        self.disabled = disabled
        self.editable = editable
        self.visible = visible
        self.aria_error = None
        self.aria_calls = 0
        self.editable_calls = 0

    async def is_visible(self):
        return self.visible

    async def aria_snapshot(self):
        self.aria_calls += 1
        if self.aria_error is not None:
            raise self.aria_error
        return self.a11y

    async def get_attribute(self, name):
        return self.attributes.get(name)

    async def is_disabled(self):
        return self.disabled

    async def is_editable(self):
        self.editable_calls += 1
        return self.editable


class FakeScopedLocator:
    def __init__(self, nodes=()):
        self.nodes = list(nodes)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return self.nodes[index]


class FakePanel:
    def __init__(self):
        self.visible_markers = set()
        self.marker_counts = {}
        self.aria_busy = False
        self.input = FakeScopedNode(
            attributes={"data-e2e": "comment-input"}
        )
        self.textbox = FakeScopedNode(
            a11y='- textbox "Add comment"',
            attributes={
                "aria-label": "Add comment",
                "contenteditable": "true",
            },
        )
        self.submit = FakeScopedNode(
            a11y='- button "Post" [disabled]',
            attributes={
                "aria-label": "Post",
                "data-e2e": "comment-post",
            },
            disabled=True,
        )
        self.comments = []

    def locator(self, selector):
        if selector == '[data-e2e="comment-input"]':
            return FakeScopedLocator([self.input])
        if selector == '[data-e2e="comment-post"]':
            return FakeScopedLocator([self.submit])
        if "textarea" in selector:
            return FakeScopedLocator([self.textbox])
        count = self.marker_counts.get(
            selector,
            int(selector in self.visible_markers),
        )
        visible = selector in self.visible_markers
        return FakeScopedLocator(
            FakeScopedNode(visible=visible) for _ in range(count)
        )

    async def get_attribute(self, name):
        return {
            "role": "region",
            "aria-busy": "true" if self.aria_busy else "false",
        }.get(name)


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
            panel_readiness_check=stable_panel,
            sleep_fn=no_sleep,
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
            panel_readiness_check=stable_panel,
            sleep_fn=no_sleep,
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

        async def delayed_panel(_page):
            nonlocal checks
            checks += 1
            return panel_sample(
                panel_visible=checks >= 3,
                fingerprint_hash=(
                    "sha256:stable" if checks >= 3 else ""
                ),
            )

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=delayed_panel,
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
        assert checks == 5

    asyncio.run(scenario())


def test_shell_fallback_detects_retained_panel_without_clicking():
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
            panel_readiness_check=stable_panel,
            sleep_fn=no_sleep,
        )
        await runner.ensure_state(page, "feed_ready", {})
        page.shell_visible = True

        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {"璇勮鍏ュ彛": entry_definition()},
        )

        assert result == {
            "state": "comment_panel_open",
            "clicked": False,
            "panel_visible": True,
            "stable_samples": 3,
            "required_samples": 3,
            "fingerprint_hash": "sha256:stable",
        }
        assert locator.click_count == 0

    asyncio.run(scenario())


def test_expanded_entry_prevents_click_when_only_skeleton_is_visible():
    class SkeletonPage(FakePage):
        def __init__(self):
            super().__init__()
            self.seen_selectors = []

        def locator(self, selector):
            self.seen_selectors.append(selector)
            return FakeVisibilityLocator(
                [selector == '[class*="skeleton" i]']
            )

    async def scenario():
        page = SkeletonPage()
        locator = FakeClickLocator(page, aria_expanded="true")
        samples = panel_sequence(
            panel_sample(
                loading_marker='[class*="skeleton" i]',
                fingerprint_hash="",
            ),
            panel_sample(),
            panel_sample(),
            panel_sample(),
        )

        async def resolver(*_args):
            return SimpleNamespace(locator=locator)

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            sleep_fn=no_sleep,
        )
        await runner.ensure_state(page, "feed_ready", {})
        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {runner.comment_entry_alias: entry_definition()},
        )

        assert result["state"] == "comment_panel_open"
        assert locator.click_count == 0
        assert (
            state_runner_module._COMMENT_PANEL_SHELL_SELECTOR
            in page.seen_selectors
        )
        assert await page.locator(
            '[class*="skeleton" i]'
        ).first.is_visible()

    asyncio.run(scenario())


def test_comment_panel_requires_three_identical_eligible_samples():
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)
        samples = panel_sequence(
            panel_sample(fingerprint_hash="sha256:a"),
            panel_sample(fingerprint_hash="sha256:b"),
            panel_sample(fingerprint_hash="sha256:b"),
            panel_sample(fingerprint_hash="sha256:b"),
        )

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=60,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        result = await runner.ensure_state(
            page,
            "comment_panel_open",
            {runner.comment_entry_alias: entry_definition()},
        )

        assert result["state"] == "comment_panel_open"
        assert result["stable_samples"] == 3
        assert result["fingerprint_hash"] == "sha256:b"
        assert len(samples.calls) == 4
        assert locator.click_count == 1

    asyncio.run(scenario())


def test_wait_accepts_stable_controls_while_comment_list_loads():
    async def scenario():
        samples = panel_sequence(
            panel_sample(loading_marker='[class*="skeleton" i]')
        )
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=20,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )

        result = await runner._wait_for_comment_panel_ready(object())

        assert result["stable_samples"] == 3
        assert result["loading_marker"] == '[class*="skeleton" i]'
        assert len(samples.calls) == 3

    asyncio.run(scenario())


def test_missing_controls_wait_until_deadline():
    async def scenario():
        samples = panel_sequence(
            panel_sample(
                submit_visible=False,
                fingerprint_hash="sha256:missing",
            )
        )
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=12,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )

        with pytest.raises(ProbeSafetyError) as caught:
            await runner._wait_for_comment_panel_ready(object())

        assert caught.value.code == "comment_panel_element_missing"
        assert len(samples.calls) == 4

    asyncio.run(scenario())


def test_delayed_controls_can_become_stable_before_deadline():
    async def scenario():
        missing = panel_sample(
            submit_visible=False,
            fingerprint_hash="sha256:missing",
        )
        ready = panel_sample(fingerprint_hash="sha256:ready")
        samples = panel_sequence(
            missing,
            missing,
            missing,
            ready,
            ready,
            ready,
        )
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=30,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )

        result = await runner._wait_for_comment_panel_ready(object())

        assert result["fingerprint_hash"] == "sha256:ready"
        assert result["stable_samples"] == 3
        assert len(samples.calls) == 6

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sequence", "timeout", "code"),
    [
        (
            (
                panel_sample(
                    aria_busy=True,
                    fingerprint_hash="",
                ),
            ),
            2,
            "comment_panel_readiness_timeout",
        ),
        (
            tuple(
                panel_sample(fingerprint_hash=f"sha256:{value}")
                for value in ("a", "b", "c")
            ),
            3,
            "comment_panel_snapshot_unstable",
        ),
        (
            (
                panel_sample(
                    submit_visible=False,
                    fingerprint_hash="sha256:missing",
                ),
            ),
            60,
            "comment_panel_element_missing",
        ),
    ],
)
def test_comment_panel_rejects_busy_unstable_or_missing_controls(
    sequence,
    timeout,
    code,
):
    async def scenario():
        page = FakePage()
        locator = FakeClickLocator(page, opens=True)
        samples = panel_sequence(*sequence)

        async def resolver(*_args):
            return SimpleNamespace(locator=locator, candidate={"id": "entry"})

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=readiness(),
            element_resolver=resolver,
            scope_resolver=panel_scope,
            panel_readiness_check=samples,
            comment_readiness_timeout_seconds=timeout,
            comment_readiness_poll_interval_seconds=2,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        await runner.ensure_state(page, "feed_ready", {})
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.ensure_state(
                page,
                "comment_panel_open",
                {runner.comment_entry_alias: entry_definition()},
            )

        assert caught.value.code == code
        assert runner.current_state == "feed_ready"
        assert locator.click_count == 1

    asyncio.run(scenario())


def test_scoped_panel_sample_ignores_old_panel_and_dynamic_comments():
    async def scenario():
        panel, old_panel = FakePanel(), FakePanel()

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        page = SimpleNamespace(old_panel=old_panel)

        panel.aria_busy = True
        busy = await runner._comment_panel_readiness_sample(page)
        assert busy["aria_busy"] is True
        assert busy["fingerprint_hash"] == ""
        assert panel.textbox.editable_calls == 0
        panel.aria_busy = False
        panel.visible_markers.add('[class*="spinner" i]')
        loading = await runner._comment_panel_readiness_sample(page)
        panel.visible_markers.clear()
        overflow_selector = '[data-e2e*="loading" i]'
        panel.marker_counts[overflow_selector] = 21
        overflow = await runner._comment_panel_readiness_sample(page)
        panel.marker_counts.clear()
        before = await runner._comment_panel_readiness_sample(page)
        panel.comments.append("new dynamic comment")
        old_panel.textbox.a11y = '- textbox "Changed old panel"'
        after = await runner._comment_panel_readiness_sample(page)

        assert loading["loading_marker"] == '[class*="spinner" i]'
        assert loading["input_visible"] is True
        assert loading["textbox_visible"] is True
        assert loading["submit_visible"] is True
        assert loading["fingerprint_hash"].startswith("sha256:")
        assert panel.textbox.aria_calls == 4
        assert panel.textbox.editable_calls == 4
        assert panel.submit.aria_calls == 4
        assert overflow["loading_marker"] == ""
        assert overflow["input_visible"] is True
        assert before["textbox_visible"] is True
        assert before["submit_visible"] is True
        assert before["submit_disabled"] is True
        assert after["fingerprint_hash"] == before["fingerprint_hash"]

    asyncio.run(scenario())


def test_comment_list_skeleton_does_not_hide_ready_controls():
    async def scenario():
        panel = FakePanel()
        panel.visible_markers.add('[class*="skeleton" i]')

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        sample = await runner._comment_panel_readiness_sample(object())

        assert sample["loading_marker"] == '[class*="skeleton" i]'
        assert sample["aria_busy"] is False
        assert sample["input_visible"] is True
        assert sample["textbox_visible"] is True
        assert sample["submit_visible"] is True
        assert sample["submit_disabled"] is True
        assert sample["fingerprint_hash"].startswith("sha256:")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "self_selector",
    [
        'textarea[data-e2e="comment-input"]',
        '[data-e2e="comment-input"][role="textbox"]',
    ],
)
def test_scoped_textbox_selector_supports_self_controls(self_selector):
    class VariantPanel(FakePanel):
        def locator(self, selector):
            if selector == state_runner_module._COMMENT_TEXTBOX_SELECTOR:
                return FakeScopedLocator(
                    [self.textbox] if self_selector in selector else []
                )
            return super().locator(selector)

    async def scenario():
        panel = VariantPanel()

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        sample = await runner._comment_panel_readiness_sample(object())

        assert sample["textbox_visible"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("self_selector", "blocked_attribute"),
    [
        ('textarea[data-e2e="comment-input"]', "readonly"),
        ('textarea[data-e2e="comment-input"]', "disabled"),
        (
            'input[data-e2e="comment-input"]:not([type="hidden"])',
            "readonly",
        ),
        (
            'input[data-e2e="comment-input"]:not([type="hidden"])',
            "disabled",
        ),
    ],
)
def test_readonly_or_disabled_native_textbox_is_not_ready(
    self_selector,
    blocked_attribute,
):
    class VariantPanel(FakePanel):
        def locator(self, selector):
            if selector == state_runner_module._COMMENT_TEXTBOX_SELECTOR:
                return FakeScopedLocator(
                    [self.textbox] if self_selector in selector else []
                )
            return super().locator(selector)

    async def scenario():
        panel = VariantPanel()
        panel.textbox.editable = False
        panel.textbox.attributes[blocked_attribute] = ""

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        sample = await runner._comment_panel_readiness_sample(object())

        assert sample["textbox_visible"] is False
        assert panel.textbox.editable_calls == 1

    asyncio.run(scenario())


def test_textbox_editable_state_changes_fingerprint():
    async def scenario():
        panel = FakePanel()

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        editable = await runner._comment_panel_readiness_sample(object())
        panel.textbox.editable = False
        readonly = await runner._comment_panel_readiness_sample(object())

        assert editable["textbox_visible"] is True
        assert readonly["textbox_visible"] is False
        assert readonly["fingerprint_hash"] != editable["fingerprint_hash"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("control", "a11y"),
    [
        ("textbox", ""),
        ("textbox", '- generic "Comment"'),
        ("submit", '- text "Post"'),
    ],
)
def test_invalid_scoped_a11y_role_never_passes_readiness(control, a11y):
    async def scenario():
        panel = FakePanel()
        getattr(panel, control).a11y = a11y

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
            sleep_fn=no_sleep,
            monotonic_fn=StepClock(step=1),
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner._wait_for_comment_panel_ready(object())

        assert caught.value.code == "comment_panel_element_missing"

    asyncio.run(scenario())


@pytest.mark.parametrize("parent_cancel", [False, True])
def test_wait_does_not_block_on_sampler_cleanup(parent_cancel):
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()

        async def delays_cleanup(_page):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return panel_sample()

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=delays_cleanup,
            comment_readiness_timeout_seconds=(
                60 if parent_cancel else 0.01
            ),
        )
        waiting = asyncio.create_task(
            runner._wait_for_comment_panel_ready(object())
        )
        await started.wait()
        if parent_cancel:
            waiting.cancel()
        started_at = asyncio.get_running_loop().time()
        expected = asyncio.CancelledError if parent_cancel else ProbeSafetyError
        with pytest.raises(expected) as caught:
            await waiting
        assert asyncio.get_running_loop().time() - started_at < 0.1
        if not parent_cancel:
            assert caught.value.code == "probe_panel_check_failed"
        with pytest.raises(ProbeSafetyError) as poisoned:
            await runner._wait_for_comment_panel_ready(object())
        assert poisoned.value.code == "probe_panel_check_failed"
        release.set()
        await asyncio.sleep(0)

    asyncio.run(scenario())


def test_sampler_cleanup_error_does_not_replace_timeout():
    async def scenario():
        contexts = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(
            lambda _loop, context: contexts.append(context)
        )

        async def fails_cleanup(_page):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("cleanup failed")

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=fails_cleanup,
            comment_readiness_timeout_seconds=0.01,
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner._wait_for_comment_panel_ready(object())
        await asyncio.sleep(0)

        assert caught.value.code == "probe_panel_check_failed"
        assert contexts == []

    asyncio.run(scenario())


def test_production_panel_sample_error_is_preserved():
    async def scenario():
        panel = FakePanel()
        panel.textbox.aria_error = RuntimeError("aria unavailable")

        async def scope(_page, _scope):
            return panel, {}

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            scope_resolver=scope,
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner._wait_for_comment_panel_ready(object())

        assert caught.value.code == "probe_panel_check_failed"
        assert caught.value.action == "verify_comment_panel"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        ProbeSafetyError(
            "probe_origin_mismatch",
            "verify_comment_panel",
        ),
    ],
)
def test_sampler_errors_preserve_safety_classification(error):
    async def scenario():
        async def fails(_page):
            raise error

        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            panel_readiness_check=fails,
        )
        with pytest.raises(ProbeSafetyError) as caught:
            await runner._wait_for_comment_panel_ready(object())

        if isinstance(error, ProbeSafetyError):
            assert caught.value is error
        else:
            assert caught.value.code == "probe_panel_check_failed"

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
