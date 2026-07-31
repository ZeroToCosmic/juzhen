import asyncio

import pytest

from browser_page_lifecycle import (
    PageLifecycle,
    is_closed_target_error,
    page_origin,
    prepare_target_page,
)


def run(coro):
    return asyncio.run(coro)


class FakePage:
    def __init__(self, url, *, closed=False, visible=True):
        self.url = url
        self._closed = closed
        self.visible = visible
        self.goto_calls = []
        self.close_calls = 0

    def is_closed(self):
        return self._closed

    async def evaluate(self, _expression):
        return "visible" if self.visible else "hidden"

    async def goto(self, url, **options):
        self.goto_calls.append((url, options))
        self.url = url

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def close(self):
        self._closed = True
        self.close_calls += 1


class FakeBrowser:
    def __init__(self, connected=True):
        self.connected = connected

    def is_connected(self):
        return self.connected


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.browser = FakeBrowser()

    async def new_page(self):
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


def test_resolve_prefers_visible_target_host_over_blank_and_other_host():
    blank = FakePage("about:blank")
    other = FakePage("https://example.com/", visible=True)
    hidden_target = FakePage("https://www.tiktok.com/a", visible=False)
    visible_target = FakePage("https://www.tiktok.com/b", visible=True)
    lifecycle = PageLifecycle(
        FakeContext([blank, other, hidden_target, visible_target]),
        "https://www.tiktok.com/",
    )

    assert run(lifecycle.resolve()) is visible_target


def test_resolve_replaces_closed_current_page_with_new_target_page():
    closed = FakePage("https://www.tiktok.com/old", closed=True)
    replacement = FakePage("https://www.tiktok.com/new")
    lifecycle = PageLifecycle(
        FakeContext([closed, replacement]),
        "https://www.tiktok.com/",
    )

    assert run(lifecycle.resolve(closed)) is replacement


def test_resolve_replacement_excludes_still_open_failed_page():
    replacement = FakePage("https://www.tiktok.com/replacement")
    failed = FakePage("https://www.tiktok.com:8443/failed")
    lifecycle = PageLifecycle(
        FakeContext([replacement, failed]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    assert failed.is_closed() is False
    assert run(lifecycle.resolve_replacement(failed)) is replacement


def test_resolve_can_return_blank_page_only_when_preparing_navigation():
    blank = FakePage("about:blank")
    lifecycle = PageLifecycle(
        FakeContext([blank]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="no active page"):
        run(lifecycle.resolve())
    assert run(lifecycle.resolve(allow_blank=True)) is blank


def test_action_boundary_replacement_records_successful_attempt():
    closed = FakePage("https://www.tiktok.com/old", closed=True)
    replacement = FakePage("https://www.tiktok.com/new")
    lifecycle = PageLifecycle(
        FakeContext([closed, replacement]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    page, result, events = run(
        lifecycle.execute(
            closed,
            {"id": "pause-boundary", "type": "pause"},
            lambda selected: selected.url,
        )
    )

    assert page is replacement
    assert result == "https://www.tiktok.com/new"
    assert len(events) == 1
    event = events[0]
    assert event["closure_type"] == "page_closed"
    assert event["closure_reason"] == "page closed"
    assert event["replacement_found"] is True
    assert event["retry"] == 0
    assert event["outcome"] == "recovered"


def test_action_boundary_replacement_failure_attaches_diagnostic():
    closed = FakePage("https://www.tiktok.com/old", closed=True)
    lifecycle = PageLifecycle(
        FakeContext([closed]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="no active page") as caught:
        run(
            lifecycle.execute(
                closed,
                {"id": "pause-boundary", "type": "pause"},
                lambda _selected: pytest.fail("callback must not run"),
            )
        )

    event = caught.value.page_recoveries[0]
    assert event["closure_type"] == "page_closed"
    assert event["closure_reason"] == "page closed"
    assert event["replacement_found"] is False
    assert event["retry"] == 0
    assert event["outcome"] == "replacement_not_found"


def test_action_boundary_recovery_is_attached_when_the_action_then_fails():
    closed = FakePage("https://www.tiktok.com/old", closed=True)
    replacement = FakePage("https://www.tiktok.com/new")
    lifecycle = PageLifecycle(
        FakeContext([closed, replacement]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    with pytest.raises(RuntimeError, match="action failed") as caught:
        run(
            lifecycle.execute(
                closed,
                {"id": "pause-boundary", "type": "pause"},
                lambda _selected: (_ for _ in ()).throw(RuntimeError("action failed")),
            )
        )

    assert caught.value.page_recoveries == [
        {
            "action_id": "pause-boundary",
            "action_type": "pause",
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "https://www.tiktok.com",
            "closure_type": "page_closed",
            "closure_reason": "page closed",
            "replacement_found": True,
            "retry": 0,
            "status": "recovered",
            "outcome": "recovered",
        }
    ]


def test_safe_action_rebinds_and_retries_exactly_once():
    first = FakePage("https://www.tiktok.com/old")
    replacement = FakePage("https://www.tiktok.com/new")
    context = FakeContext([first])
    lifecycle = PageLifecycle(context, "https://www.tiktok.com/")
    calls = []

    async def callback(page):
        calls.append(page)
        if page is first:
            first._closed = True
            context.pages.append(replacement)
            raise RuntimeError("Mouse.wheel: Target page, context or browser has been closed")
        return "ok"

    page, result, events = run(
        lifecycle.execute(first, {"id": "move-1", "type": "move"}, callback)
    )

    assert page is replacement
    assert result == "ok"
    assert calls == [first, replacement]
    assert events == [{
        "action_id": "move-1",
        "action_type": "move",
        "old_page_origin": "https://www.tiktok.com",
        "new_page_origin": "https://www.tiktok.com",
        "closure_type": "target_closed",
        "closure_reason": "target page, context or browser has been closed",
        "replacement_found": True,
        "retry": 1,
        "status": "recovered",
        "outcome": "recovered",
    }]


def test_boundary_rebind_and_move_retry_success_return_all_events_in_order():
    closed = FakePage("https://www.tiktok.com/closed", closed=True)
    first = FakePage("https://www.tiktok.com/first")
    second = FakePage("https://www.tiktok.com/second")
    context = FakeContext([closed, first])
    lifecycle = PageLifecycle(context, "https://www.tiktok.com/", timeout_seconds=0)
    calls = []

    async def callback(page):
        calls.append(page)
        if page is first:
            first._closed = True
            context.pages.append(second)
            raise RuntimeError(
                "Mouse.move: Target page, context or browser has been closed"
            )
        return "ok"

    page, result, events = run(
        lifecycle.execute(closed, {"id": "move-chain", "type": "move"}, callback)
    )

    assert page is second
    assert result == "ok"
    assert calls == [first, second]
    assert [(event["outcome"], event["retry"]) for event in events] == [
        ("recovered", 0),
        ("recovered", 1),
    ]


def test_boundary_rebind_and_move_retry_failure_attaches_all_events_in_order():
    closed = FakePage("https://www.tiktok.com/closed", closed=True)
    first = FakePage("https://www.tiktok.com/first")
    second = FakePage("https://www.tiktok.com/second")
    context = FakeContext([closed, first])
    lifecycle = PageLifecycle(context, "https://www.tiktok.com/", timeout_seconds=0)
    calls = []

    async def callback(page):
        calls.append(page)
        if page is first:
            first._closed = True
            context.pages.append(second)
            raise RuntimeError(
                "Mouse.move: Target page, context or browser has been closed"
            )
        raise RuntimeError("replacement move failed")

    with pytest.raises(RuntimeError, match="replacement move failed") as caught:
        run(
            lifecycle.execute(
                closed,
                {"id": "move-chain", "type": "move"},
                callback,
            )
        )

    assert calls == [first, second]
    assert [
        (event["outcome"], event["retry"])
        for event in caught.value.page_recoveries
    ] == [
        ("recovered", 0),
        ("retry_failed", 1),
    ]


def test_boundary_recovery_precedes_nested_scroll_recoveries_without_duplicate():
    closed = FakePage("https://www.tiktok.com/closed", closed=True)
    replacement = FakePage("https://www.tiktok.com/replacement")
    lifecycle = PageLifecycle(
        FakeContext([closed, replacement]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    async def callback(_page):
        error = RuntimeError(
            "Mouse.wheel: Target page, context or browser has been closed"
        )
        error.page_recoveries = [
            {"outcome": "recovered", "retry": 1},
            {"outcome": "not_retried", "retry": 0},
        ]
        raise error

    with pytest.raises(RuntimeError, match="Target page") as caught:
        run(
            lifecycle.execute(
                closed,
                {"id": "scroll-chain", "type": "scroll_down"},
                callback,
            )
        )

    assert [
        (event["outcome"], event["retry"])
        for event in caught.value.page_recoveries
    ] == [
        ("recovered", 0),
        ("recovered", 1),
        ("not_retried", 0),
    ]


def test_safe_retry_does_not_reuse_failed_page_when_it_is_still_open():
    failed_page = FakePage("https://www.tiktok.com/")
    lifecycle = PageLifecycle(
        FakeContext([failed_page]),
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )
    calls = []

    async def callback(page):
        calls.append(page)
        raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(RuntimeError, match="no active page") as caught:
        run(lifecycle.execute(failed_page, {"id": "move-2", "type": "move"}, callback))

    assert calls == [failed_page]
    assert caught.value.page_recoveries == [
        {
            "action_id": "move-2",
            "action_type": "move",
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "",
            "closure_type": "target_closed",
            "closure_reason": "target page, context or browser has been closed",
            "replacement_found": False,
            "retry": 0,
            "status": "failed",
            "outcome": "replacement_not_found",
        }
    ]


def test_safe_retry_failure_attaches_found_replacement_diagnostic():
    first = FakePage("https://www.tiktok.com/old")
    replacement = FakePage("https://www.tiktok.com/new")
    context = FakeContext([first])
    lifecycle = PageLifecycle(context, "https://www.tiktok.com/", timeout_seconds=0)

    async def callback(page):
        if page is first:
            context.pages.append(replacement)
            raise RuntimeError(
                "Mouse.move: Target page, context or browser has been closed"
            )
        raise RuntimeError("replacement action failed")

    with pytest.raises(RuntimeError, match="replacement action failed") as caught:
        run(lifecycle.execute(first, {"id": "move-3", "type": "move"}, callback))

    assert caught.value.page_recoveries == [
        {
            "action_id": "move-3",
            "action_type": "move",
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


@pytest.mark.parametrize("action_type", ["click", "keyboard_input"])
def test_side_effect_action_never_retries(action_type):
    page = FakePage("https://www.tiktok.com/")
    lifecycle = PageLifecycle(FakeContext([page]), "https://www.tiktok.com/")
    calls = []

    async def callback(current):
        calls.append(current)
        raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(RuntimeError, match="Target page") as caught:
        run(
            lifecycle.execute(
                page,
                {"id": "side-effect", "type": action_type},
                callback,
            )
        )

    assert calls == [page]
    assert caught.value.page_recoveries == [
        {
            "action_id": "side-effect",
            "action_type": action_type,
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "",
            "closure_type": "target_closed",
            "closure_reason": "target page, context or browser has been closed",
            "replacement_found": False,
            "retry": 0,
            "status": "failed",
            "outcome": "not_retried",
        }
    ]


def test_postcondition_observation_rebinds_without_redispatching_action():
    first = FakePage("https://www.tiktok.com/original")
    replacement = FakePage("https://www.tiktok.com/replacement")
    context = FakeContext([first])
    lifecycle = PageLifecycle(
        context,
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )
    observations = []

    async def observe(current):
        observations.append(current)
        if current is first:
            context.pages.append(replacement)
            raise RuntimeError(
                "Target page, context or browser has been closed"
            )
        return True

    current, observed, recoveries = run(
        lifecycle.observe(
            first,
            {"id": "click-entry", "type": "click"},
            observe,
            timeout_seconds=5,
        )
    )

    assert current is replacement
    assert observed is True
    assert observations == [first, replacement]
    assert recoveries == [
        {
            "action_id": "click-entry",
            "action_type": "click",
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "https://www.tiktok.com",
            "closure_type": "target_closed",
            "closure_reason": "target page, context or browser has been closed",
            "replacement_found": True,
            "retry": 0,
            "status": "recovered",
            "outcome": "recovered",
        }
    ]


def test_postcondition_error_after_rebind_keeps_ordered_recovery():
    first = FakePage("https://www.tiktok.com/original")
    replacement = FakePage("https://www.tiktok.com/replacement")
    context = FakeContext([first])
    lifecycle = PageLifecycle(
        context,
        "https://www.tiktok.com/",
        timeout_seconds=0,
    )

    async def observe(current):
        if current is first:
            context.pages.append(replacement)
            raise RuntimeError(
                "Target page, context or browser has been closed"
            )
        raise RuntimeError("observation failed")

    with pytest.raises(RuntimeError, match="observation failed") as caught:
        run(
            lifecycle.observe(
                first,
                {"id": "click-entry", "type": "click"},
                observe,
                timeout_seconds=5,
            )
        )

    assert [
        (event["outcome"], event["retry"])
        for event in caught.value.page_recoveries
    ] == [("recovered", 0)]


def test_postcondition_observation_caps_replacement_wait_at_five_seconds():
    class Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        async def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    missing = FakePage("https://www.tiktok.com/missing", closed=True)
    lifecycle = PageLifecycle(
        FakeContext([]),
        "https://www.tiktok.com/",
        timeout_seconds=10,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )
    observations = []

    current, observed, recoveries = run(
        lifecycle.observe(
            missing,
            {"id": "click-entry", "type": "click"},
            lambda page: observations.append(page),
            timeout_seconds=5,
        )
    )

    assert current is missing
    assert observed is False
    assert observations == []
    assert clock.now <= 5.0 + 1e-9
    assert recoveries[-1]["outcome"] == "replacement_not_found"


def test_postcondition_polling_crops_final_sleep_to_remaining_budget():
    class Clock:
        def __init__(self):
            self.now = 0.0

        def monotonic(self):
            return self.now

        async def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    page = FakePage("https://www.tiktok.com/current")
    lifecycle = PageLifecycle(
        FakeContext([page]),
        "https://www.tiktok.com/",
        timeout_seconds=10,
        sleep_fn=clock.sleep,
        monotonic_fn=clock.monotonic,
    )
    observations = []

    current, observed, recoveries = run(
        lifecycle.observe(
            page,
            {"id": "click-entry", "type": "click"},
            lambda current: observations.append(current) or False,
            timeout_seconds=0.25,
        )
    )

    assert current is page
    assert observed is False
    assert recoveries == []
    assert len(observations) == 3
    assert clock.now == pytest.approx(0.25, abs=1e-9)


def test_postcondition_slow_callback_is_cancelled_at_remaining_deadline():
    page = FakePage("https://www.tiktok.com/current")
    lifecycle = PageLifecycle(
        FakeContext([page]),
        "https://www.tiktok.com/",
        timeout_seconds=10,
    )

    async def slow_observation(_current):
        await asyncio.sleep(0.05)
        return True

    started = lifecycle.monotonic_fn()
    current, observed, recoveries = run(
        lifecycle.observe(
            page,
            {"id": "click-entry", "type": "click"},
            slow_observation,
            timeout_seconds=0.01,
        )
    )
    elapsed = lifecycle.monotonic_fn() - started

    assert current is page
    assert observed is False
    assert recoveries == []
    assert elapsed < 0.04


def test_postcondition_observation_preserves_external_cancellation():
    page = FakePage("https://www.tiktok.com/current")
    lifecycle = PageLifecycle(
        FakeContext([page]),
        "https://www.tiktok.com/",
    )

    async def cancelled(_current):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        run(
            lifecycle.observe(
                page,
                {"id": "click-entry", "type": "click"},
                cancelled,
                timeout_seconds=5,
            )
        )


def test_closed_target_classifier_does_not_retry_unrelated_errors():
    assert is_closed_target_error(
        RuntimeError("Locator.click: Target page, context or browser has been closed")
    )
    assert not is_closed_target_error(RuntimeError("element not found"))


def test_page_origin_returns_empty_for_invalid_port():
    assert page_origin(FakePage("https://www.tiktok.com:not-a-port/")) == ""
