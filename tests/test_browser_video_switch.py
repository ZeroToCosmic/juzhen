import asyncio
import hashlib

import pytest

import browser_video_switch
from browser_video_switch import (
    FeedState,
    VideoSwitchError,
    capture_feed_state,
    execute_verified_switches,
    wait_for_stable_changed_state,
)


def run(coro):
    return asyncio.run(coro)


class ManualClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    async def sleep(self, seconds):
        seconds = float(seconds)
        self.sleeps.append(seconds)
        self.now += seconds


class FixedRng:
    def __init__(self):
        self.uniform_calls = []

    def uniform(self, low, high):
        self.uniform_calls.append((low, high))
        return low


class FakeFeedMouse:
    def __init__(self, page):
        self.page = page
        self.move_calls = []
        self.wheel_calls = []

    async def move(self, x, y):
        self.move_calls.append((x, y))

    async def wheel(self, delta_x, delta_y):
        self.wheel_calls.append((delta_x, delta_y))
        self.page.apply_wheel(delta_y)


class FakeFeedPage:
    def __init__(
        self,
        *,
        pulses_per_switch=8,
        ignore_first=0,
        never_switch=False,
        container_height=945,
        transient_only=False,
        scroll_without_switch=False,
        identity_prefix="video",
    ):
        self.url = "https://www.tiktok.com/feed"
        self.pulses_per_switch = pulses_per_switch
        self.ignore_first = ignore_first
        self.never_switch = never_switch
        self.container_height = container_height
        self.transient_only = transient_only
        self.scroll_without_switch = scroll_without_switch
        self.identity_prefix = identity_prefix
        self.video_index = 0
        self.scroll_top = 0
        self.effective_pulses = 0
        self.transient_pending = False
        self.clock = ManualClock()
        self.mouse = FakeFeedMouse(self)

    def apply_wheel(self, delta_y):
        if self.scroll_without_switch:
            self.scroll_top += delta_y
            return
        if self.transient_only:
            self.transient_pending = True
            return
        if len(self.mouse.wheel_calls) <= self.ignore_first or self.never_switch:
            return
        self.effective_pulses += 1
        if self.effective_pulses == self.pulses_per_switch:
            self.video_index += 1 if delta_y > 0 else -1
            self.effective_pulses = 0

    async def evaluate(self, expression):
        assert "#column-list-container" in expression
        assert "article" in expression
        video_index = self.video_index
        if self.transient_pending:
            video_index += 99
            self.transient_pending = False
        return {
            "identity": f"{self.identity_prefix}:{video_index}",
            "container_x": 10,
            "container_y": 20,
            "container_width": 360,
            "container_height": self.container_height,
            "scroll_top": (
                self.scroll_top
                if self.scroll_without_switch
                else video_index * self.container_height
            ),
        }


DIAGNOSTIC_KEYS = {
    "pulse_index",
    "delta",
    "wheel_seen",
    "target_in_container",
    "default_prevented",
    "mutation_count",
    "poll_count",
    "different_identity_seen",
    "container_scroll_top_before",
    "container_scroll_top_after",
    "window_scroll_y_before",
    "window_scroll_y_after",
    "article_top_before",
    "article_top_after",
    "article_bottom_before",
    "article_bottom_after",
    "article_center_offset_before",
    "article_center_offset_after",
    "identity_source_before",
    "identity_source_after",
    "identity_hash_before",
    "identity_hash_after",
}


class DiagnosticFeedPage(FakeFeedPage):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.probe_active = False
        self.probe_install_calls = 0
        self.probe_cleanup_calls = 0
        self.raw_identities = []

    def _diagnostic_snapshot(self):
        raw_identity = f"raw-private-video-{self.video_index}"
        self.raw_identities.append(raw_identity)
        article_top = 20 - self.video_index * self.container_height
        return {
            "container_scroll_top": self.video_index * self.container_height,
            "window_scroll_y": 7,
            "article_top": article_top,
            "article_bottom": article_top + self.container_height,
            "article_center_offset": -self.video_index * self.container_height,
            "identity_source": "video_id",
            "identity": raw_identity,
        }

    async def evaluate(self, expression):
        if "__codex_verified_scroll_pulse_probe__" not in expression:
            return await super().evaluate(expression)
        if "new MutationObserver" in expression:
            assert "capture: true" in expression
            assert "queueMicrotask" in expression
            assert "event.defaultPrevented" in expression
            assert "childList: true, subtree: true" in expression
            assert not self.probe_active
            self.probe_active = True
            self.probe_install_calls += 1
            return self._diagnostic_snapshot()
        assert "observer.disconnect()" in expression
        assert "removeEventListener" in expression
        assert "delete window[key]" in expression
        assert self.probe_active
        self.probe_active = False
        self.probe_cleanup_calls += 1
        return {
            "wheel_seen": bool(self.mouse.wheel_calls),
            "target_in_container": True,
            "default_prevented": False,
            "mutation_count": self.video_index,
            "after": self._diagnostic_snapshot(),
        }


class DelayedCommitFeedPage(FakeFeedPage):
    def __init__(self):
        super().__init__(pulses_per_switch=1)
        self.pending_captures = 0

    def apply_wheel(self, delta_y):
        self.pending_captures = 2
        self.pending_delta = 1 if delta_y > 0 else -1

    async def evaluate(self, expression):
        if self.pending_captures:
            self.pending_captures -= 1
        elif hasattr(self, "pending_delta"):
            self.video_index += self.pending_delta
            del self.pending_delta
        return await super().evaluate(expression)


class SecondPulseCommitFeedPage(FakeFeedPage):
    def apply_wheel(self, delta_y):
        if len(self.mouse.wheel_calls) == 2:
            self.video_index += 1 if delta_y > 0 else -1


class SecondPulseTransientFeedPage(FakeFeedPage):
    def apply_wheel(self, _delta_y):
        if len(self.mouse.wheel_calls) == 2:
            self.transient_pending = True


def execute(
    page,
    monkeypatch,
    *,
    requested,
    direction="down",
    rng=None,
    interval_range=(0, 0),
    lifecycle=None,
    diagnostic=False,
):
    monkeypatch.setattr(browser_video_switch, "_monotonic", page.clock.monotonic)
    diagnostic_kwargs = {"diagnostic": True} if diagnostic else {}
    return run(
        execute_verified_switches(
            page,
            direction=direction,
            requested=requested,
            interval_range=interval_range,
            lifecycle=lifecycle,
            rng=rng or FixedRng(),
            sleep_fn=page.clock.sleep,
            **diagnostic_kwargs,
        )
    )


def test_one_completed_count_requires_observed_video_change(monkeypatch):
    page = FakeFeedPage(pulses_per_switch=8)

    result = execute(page, monkeypatch, requested=1)

    assert result["requested_switches"] == 1
    assert result["completed_switches"] == 1
    assert result["wheel_events"] == 8
    assert page.mouse.move_calls == [(190, 492.5)]


def test_switch_waits_for_async_identity_change_after_one_wheel(monkeypatch):
    page = DelayedCommitFeedPage()

    result = execute(page, monkeypatch, requested=1, direction="down")

    assert result["completed_switches"] == 1
    assert result["wheel_events"] == 1
    assert page.mouse.wheel_calls == [(0, 120)]


def test_each_pulse_observes_for_450ms_before_retrying_upward(monkeypatch):
    page = SecondPulseCommitFeedPage()

    result = execute(page, monkeypatch, requested=1, direction="up")

    assert result["completed_switches"] == 1
    assert result["wheel_events"] == 2
    assert page.mouse.wheel_calls == [(0, -120), (0, -120)]
    assert page.clock.now == pytest.approx(0.5)


def test_side_effect_guard_runs_before_mouse_move_and_every_wheel_retry(
    monkeypatch,
):
    page = SecondPulseCommitFeedPage()
    monkeypatch.setattr(
        browser_video_switch,
        "_monotonic",
        page.clock.monotonic,
    )
    guarded_states = []

    async def before_side_effect():
        guarded_states.append(
            (len(page.mouse.move_calls), len(page.mouse.wheel_calls))
        )

    result = run(
        execute_verified_switches(
            page,
            direction="down",
            requested=1,
            interval_range=(0, 0),
            lifecycle=None,
            rng=FixedRng(),
            sleep_fn=page.clock.sleep,
            before_side_effect=before_side_effect,
        )
    )

    assert result["wheel_events"] == 2
    assert guarded_states == [(0, 0), (1, 0), (1, 1)]


def test_transient_second_pulse_times_out_without_a_completed_switch(
    monkeypatch,
):
    page = SecondPulseTransientFeedPage()
    monkeypatch.setattr(browser_video_switch, "SWITCH_TIMEOUT_SECONDS", 0.9)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1, direction="down")

    assert page.mouse.wheel_calls == [(0, 120), (0, 120)]
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 2


def test_unchanged_feed_times_out_at_eight_seconds_within_pulse_bound(
    monkeypatch,
):
    page = FakeFeedPage(never_switch=True, container_height=100_000)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1)

    assert caught.value.code == "video_switch_timeout"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events <= 24
    assert page.clock.now == pytest.approx(8.0)


def test_ignored_wheel_pulses_do_not_increment_switch_count(monkeypatch):
    page = FakeFeedPage(ignore_first=4, pulses_per_switch=8)

    result = execute(page, monkeypatch, requested=2)

    assert result["completed_switches"] == 2
    assert result["wheel_events"] == 20


def test_switch_limit_fails_with_partial_measurements(monkeypatch):
    page = FakeFeedPage(never_switch=True, container_height=945)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=3)

    assert caught.value.code == "video_switch_not_observed"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 12


@pytest.mark.parametrize(
    ("container_height", "expected_pulses", "expected_code"),
    [
        (1, 5, "video_switch_not_observed"),
        (945, 12, "video_switch_not_observed"),
        (100_000, 18, "video_switch_timeout"),
    ],
)
def test_switch_pulse_bound_is_exact(
    monkeypatch,
    container_height,
    expected_pulses,
    expected_code,
):
    page = FakeFeedPage(
        never_switch=True,
        container_height=container_height,
    )

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1)

    assert caught.value.wheel_events == expected_pulses
    assert caught.value.code == expected_code


def test_upward_switch_uses_negative_fixed_wheel_delta(monkeypatch):
    page = FakeFeedPage(pulses_per_switch=1)

    result = execute(page, monkeypatch, requested=1, direction="up")

    assert page.mouse.wheel_calls == [(0, -120)]
    assert result["distance"] == 120


def test_interval_is_sampled_and_slept_only_between_completed_switches(monkeypatch):
    page = FakeFeedPage(pulses_per_switch=1)
    rng = FixedRng()

    result = execute(
        page,
        monkeypatch,
        requested=3,
        rng=rng,
        interval_range=(1.25, 2.5),
    )

    assert result["completed_switches"] == 3
    assert rng.uniform_calls == [(1.25, 2.5), (1.25, 2.5)]
    assert page.clock.sleeps.count(1.25) == 2


def test_transient_changed_fingerprint_is_not_counted(monkeypatch):
    page = FakeFeedPage(transient_only=True)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1)

    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 12


def test_scroll_position_change_without_fingerprint_change_is_not_counted(monkeypatch):
    page = FakeFeedPage(scroll_without_switch=True)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1)

    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 12


def test_failure_exposes_only_safe_hash_not_raw_identity(monkeypatch):
    raw_identity = "video:987654321|caption=private|account=secret|selector=#feed"
    page = FakeFeedPage(never_switch=True, identity_prefix=raw_identity)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1)

    error = caught.value
    assert len(error.safe_fingerprint) == 12
    assert all(character in "0123456789abcdef" for character in error.safe_fingerprint)
    assert raw_identity not in str(error)
    assert raw_identity not in repr(error.__dict__)


def test_hanging_state_capture_is_cancelled_at_the_remaining_deadline():
    class HangingFeedPage(FakeFeedPage):
        async def evaluate(self, _expression):
            await asyncio.Event().wait()

    page = HangingFeedPage()
    before = FeedState(
        fingerprint="before",
        safe_fingerprint="6db7d803e74f",
        container_x=0,
        container_y=0,
        container_width=360,
        container_height=945,
        scroll_top=0,
    )

    result = run(
        asyncio.wait_for(
            wait_for_stable_changed_state(
                page,
                before,
                timeout=0.01,
                sleep_fn=page.clock.sleep,
            ),
            timeout=0.05,
        )
    )

    assert result is None


def execute_with_short_deadline(
    page,
    monkeypatch,
    *,
    sleep_fn=None,
    rng=None,
    diagnostic=False,
):
    monkeypatch.setattr(
        browser_video_switch,
        "_monotonic",
        browser_video_switch.time.monotonic,
    )
    monkeypatch.setattr(browser_video_switch, "SWITCH_TIMEOUT_SECONDS", 0.01)
    diagnostic_kwargs = {"diagnostic": True} if diagnostic else {}
    return run(
        asyncio.wait_for(
            execute_verified_switches(
                page,
                direction="down",
                requested=1,
                interval_range=(0, 0),
                lifecycle=None,
                rng=rng or FixedRng(),
                sleep_fn=sleep_fn or page.clock.sleep,
                **diagnostic_kwargs,
            ),
            timeout=0.05,
        )
    )


def test_hanging_initial_capture_becomes_safe_timeout(monkeypatch):
    class HangingCapturePage(FakeFeedPage):
        async def evaluate(self, _expression):
            await asyncio.Event().wait()

    page = HangingCapturePage()

    with pytest.raises(VideoSwitchError) as caught:
        execute_with_short_deadline(page, monkeypatch)

    assert caught.value.code == "video_switch_timeout"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 0


def test_hanging_mouse_move_becomes_safe_timeout(monkeypatch):
    class HangingMoveMouse(FakeFeedMouse):
        async def move(self, _x, _y):
            await asyncio.Event().wait()

    page = FakeFeedPage()
    page.mouse = HangingMoveMouse(page)

    with pytest.raises(VideoSwitchError) as caught:
        execute_with_short_deadline(page, monkeypatch)

    assert caught.value.code == "video_switch_timeout"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 0


def test_hanging_wheel_is_not_counted_and_becomes_safe_timeout(monkeypatch):
    class HangingWheelMouse(FakeFeedMouse):
        async def wheel(self, delta_x, delta_y):
            self.wheel_calls.append((delta_x, delta_y))
            await asyncio.Event().wait()

    page = FakeFeedPage()
    page.mouse = HangingWheelMouse(page)

    with pytest.raises(VideoSwitchError) as caught:
        execute_with_short_deadline(page, monkeypatch)

    assert caught.value.code == "video_switch_timeout"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 0


def test_poll_timeout_counts_only_the_successfully_dispatched_wheel(monkeypatch):
    class HangingAfterWheelPage(FakeFeedPage):
        def __init__(self):
            super().__init__()
            self.evaluate_calls = 0

        async def evaluate(self, expression):
            self.evaluate_calls += 1
            if self.evaluate_calls > 1:
                await asyncio.Event().wait()
            return await super().evaluate(expression)

    page = HangingAfterWheelPage()

    with pytest.raises(VideoSwitchError) as caught:
        execute_with_short_deadline(page, monkeypatch)

    assert caught.value.code == "video_switch_timeout"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 1


def test_interval_error_is_sanitized_with_completed_partial_measurements(monkeypatch):
    secret = "interval failed; account=private caption=hidden selector=#feed"

    class SecretRng(FixedRng):
        def uniform(self, _low, _high):
            raise RuntimeError(secret)

    page = FakeFeedPage(pulses_per_switch=1)

    with pytest.raises(VideoSwitchError) as caught:
        execute(
            page,
            monkeypatch,
            requested=2,
            rng=SecretRng(),
        )

    error = caught.value
    assert error.code == "video_switch_interval_failed"
    assert error.completed_switches == 1
    assert error.wheel_events == 1
    assert len(error.switches) == 1
    assert secret not in str(error)
    assert secret not in repr(error.__dict__)


def test_hanging_interval_is_bounded_and_sanitized(monkeypatch):
    page = FakeFeedPage(pulses_per_switch=1)

    async def hanging_interval(seconds):
        if float(seconds) == 0:
            await asyncio.Event().wait()
        await page.clock.sleep(seconds)

    monkeypatch.setattr(
        browser_video_switch,
        "INTERVAL_GRACE_SECONDS",
        0.01,
        raising=False,
    )

    with pytest.raises(VideoSwitchError) as caught:
        run(
            asyncio.wait_for(
                execute_verified_switches(
                    page,
                    direction="down",
                    requested=2,
                    interval_range=(0, 0),
                    lifecycle=None,
                    rng=FixedRng(),
                    sleep_fn=hanging_interval,
                ),
                timeout=0.05,
            )
        )

    assert caught.value.code == "video_switch_interval_failed"
    assert caught.value.completed_switches == 1
    assert caught.value.wheel_events == 1
    assert len(caught.value.switches) == 1


def test_initial_capture_preserves_external_cancellation(monkeypatch):
    class CancelledCapturePage(FakeFeedPage):
        async def evaluate(self, _expression):
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        execute(CancelledCapturePage(), monkeypatch, requested=1)


def test_interval_preserves_external_cancellation(monkeypatch):
    page = FakeFeedPage(pulses_per_switch=1)

    async def cancelled_interval(seconds):
        if float(seconds) == 0:
            raise asyncio.CancelledError()
        await page.clock.sleep(seconds)

    monkeypatch.setattr(browser_video_switch, "_monotonic", page.clock.monotonic)

    with pytest.raises(asyncio.CancelledError):
        run(
            execute_verified_switches(
                page,
                direction="down",
                requested=2,
                interval_range=(0, 0),
                lifecycle=None,
                rng=FixedRng(),
                sleep_fn=cancelled_interval,
            )
        )


@pytest.mark.parametrize(
    ("identity_fields", "expected"),
    [
        (
            {
                "video_id": "123456",
                "article_id": "article-fallback",
                "stable_attributes": {"data-e2e": "feed"},
                "visible_index": 7,
            },
            "video:123456",
        ),
        (
            {
                "video_id": "",
                "article_id": "article-primary",
                "stable_attributes": {"data-e2e": "feed"},
                "visible_index": 7,
            },
            "article:article-primary",
        ),
        (
            {
                "video_id": "",
                "article_id": "",
                "stable_attributes": {
                    "data-e2e": "feed",
                    "aria-label": "visible-card",
                },
                "visible_index": 7,
            },
            'fallback:{"aria-label":"visible-card","data-e2e":"feed"}:7',
        ),
    ],
)
def test_identity_source_priority_uses_evaluated_dom_contract(
    identity_fields,
    expected,
):
    class IdentityContractPage:
        async def evaluate(self, _expression):
            return {
                **identity_fields,
                "container_x": 10,
                "container_y": 20,
                "container_width": 360,
                "container_height": 945,
                "scroll_top": 0,
            }

    state = run(capture_feed_state(IdentityContractPage()))

    assert state.fingerprint == expected


class ClosingMouse(FakeFeedMouse):
    def __init__(self, page, close_on):
        super().__init__(page)
        self.close_on = close_on

    async def wheel(self, delta_x, delta_y):
        self.wheel_calls.append((delta_x, delta_y))
        if len(self.wheel_calls) == self.close_on:
            raise RuntimeError(
                "Mouse.wheel: Target page, context or browser has been closed"
            )
        self.page.apply_wheel(delta_y)


class FakeLifecycle:
    def __init__(self, replacement):
        self.replacement = replacement
        self.calls = []

    async def resolve_replacement(self, failed_page):
        self.calls.append(failed_page)
        return self.replacement


def test_interval_error_retains_recovery_and_completed_records(monkeypatch):
    class SecretRng(FixedRng):
        def uniform(self, _low, _high):
            raise RuntimeError("account=secret caption=hidden selector=#feed")

    first = FakeFeedPage(pulses_per_switch=1)
    first.mouse = ClosingMouse(first, close_on=1)
    replacement = FakeFeedPage(pulses_per_switch=1)
    replacement.clock = first.clock
    lifecycle = FakeLifecycle(replacement)

    with pytest.raises(VideoSwitchError) as caught:
        execute(
            first,
            monkeypatch,
            requested=2,
            lifecycle=lifecycle,
            rng=SecretRng(),
        )

    assert caught.value.code == "video_switch_interval_failed"
    assert caught.value.completed_switches == 1
    assert caught.value.wheel_events == 1
    assert len(caught.value.switches) == 1
    assert [
        event["outcome"] for event in caught.value.page_recoveries
    ] == ["recovered"]


def test_page_replacement_restarts_only_pending_switch(monkeypatch):
    first = FakeFeedPage(pulses_per_switch=1)
    first.mouse = ClosingMouse(first, close_on=2)
    replacement = FakeFeedPage(pulses_per_switch=1)
    replacement.clock = first.clock
    lifecycle = FakeLifecycle(replacement)

    result = execute(
        first,
        monkeypatch,
        requested=2,
        lifecycle=lifecycle,
    )

    assert result["completed_switches"] == 2
    assert result["wheel_events"] == 2
    assert len(result["switches"]) == 2
    assert lifecycle.calls == [first]
    assert result["_active_page"] is replacement
    assert [event["outcome"] for event in result["_page_recoveries"]] == [
        "recovered"
    ]


def test_closed_target_on_replacement_marks_the_single_retry_failed(monkeypatch):
    first = FakeFeedPage(pulses_per_switch=1)
    first.mouse = ClosingMouse(first, close_on=1)
    replacement = FakeFeedPage(pulses_per_switch=1)
    replacement.mouse = ClosingMouse(replacement, close_on=1)
    replacement.clock = first.clock
    lifecycle = FakeLifecycle(replacement)

    with pytest.raises(VideoSwitchError) as caught:
        execute(
            first,
            monkeypatch,
            requested=1,
            lifecycle=lifecycle,
        )

    assert caught.value.code == "video_switch_closed_target"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 0
    assert lifecycle.calls == [first]
    assert [
        event["outcome"] for event in caught.value.page_recoveries
    ] == ["retry_failed"]


def test_replacement_capture_failure_keeps_ordered_recovery_and_safe_error(
    monkeypatch,
):
    class FailingCapturePage(FakeFeedPage):
        async def evaluate(self, _expression):
            raise RuntimeError(
                "selector=#private account=secret caption=hidden video=998877"
            )

    first = FakeFeedPage(pulses_per_switch=1)
    first.mouse = ClosingMouse(first, close_on=1)
    replacement = FailingCapturePage()
    replacement.clock = first.clock
    lifecycle = FakeLifecycle(replacement)

    with pytest.raises(VideoSwitchError) as caught:
        execute(
            first,
            monkeypatch,
            requested=1,
            lifecycle=lifecycle,
        )

    assert caught.value.code == "video_switch_state_capture_failed"
    assert caught.value.completed_switches == 0
    assert caught.value.wheel_events == 0
    assert "private" not in str(caught.value)
    assert [
        event["outcome"] for event in caught.value.page_recoveries
    ] == ["retry_failed"]


def test_diagnostic_off_preserves_success_shape_and_installs_no_probe(monkeypatch):
    page = DiagnosticFeedPage(pulses_per_switch=1)

    result = execute(page, monkeypatch, requested=1)

    assert set(result) == {
        "count",
        "distance",
        "requested_switches",
        "completed_switches",
        "wheel_events",
        "switches",
    }
    assert "pulse_diagnostics" not in result
    assert page.probe_install_calls == 0
    assert page.probe_cleanup_calls == 0


def test_diagnostic_success_has_exact_safe_schema_and_twelve_char_hashes(
    monkeypatch,
):
    page = DiagnosticFeedPage(pulses_per_switch=1)

    result = execute(page, monkeypatch, requested=1, diagnostic=True)

    assert len(result["pulse_diagnostics"]) == 1
    record = result["pulse_diagnostics"][0]
    assert set(record) == DIAGNOSTIC_KEYS
    assert record["pulse_index"] == 1
    assert record["delta"] == 120
    assert record["wheel_seen"] is True
    assert record["target_in_container"] is True
    assert record["default_prevented"] is False
    assert record["mutation_count"] == 1
    assert record["poll_count"] == 2
    assert record["different_identity_seen"] is True
    assert record["identity_source_before"] == "video_id"
    assert record["identity_source_after"] == "video_id"
    assert record["identity_hash_before"] == hashlib.sha256(
        b"raw-private-video-0"
    ).hexdigest()[:12]
    assert record["identity_hash_after"] == hashlib.sha256(
        b"raw-private-video-1"
    ).hexdigest()[:12]
    assert all(
        len(record[key]) == 12
        and set(record[key]) <= set("0123456789abcdef")
        for key in ("identity_hash_before", "identity_hash_after")
    )
    assert all(raw not in repr(record) for raw in page.raw_identities)
    assert page.mouse.wheel_calls == [(0, 120)]
    assert page.probe_install_calls == 1
    assert page.probe_cleanup_calls == 1
    assert page.probe_active is False


def test_diagnostic_failure_carries_safe_partial_records(monkeypatch):
    page = DiagnosticFeedPage(never_switch=True, container_height=1)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=2, diagnostic=True)

    error = caught.value
    assert error.code == "video_switch_not_observed"
    assert len(error.pulse_diagnostics) == 5
    assert [record["pulse_index"] for record in error.pulse_diagnostics] == [
        1,
        2,
        3,
        4,
        5,
    ]
    assert all(set(record) == DIAGNOSTIC_KEYS for record in error.pulse_diagnostics)
    assert all(
        raw not in repr(error.__dict__) for raw in page.raw_identities
    )
    assert page.probe_install_calls == 5
    assert page.probe_cleanup_calls == 5
    assert page.probe_active is False


def test_diagnostic_probe_cleans_up_after_ordinary_wheel_error(monkeypatch):
    class FailingWheelMouse(FakeFeedMouse):
        async def wheel(self, delta_x, delta_y):
            self.wheel_calls.append((delta_x, delta_y))
            raise RuntimeError("private wheel failure")

    page = DiagnosticFeedPage()
    page.mouse = FailingWheelMouse(page)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1, diagnostic=True)

    assert caught.value.code == "video_switch_state_capture_failed"
    assert len(caught.value.pulse_diagnostics) == 1
    assert page.mouse.wheel_calls == [(0, 120)]
    assert page.probe_install_calls == 1
    assert page.probe_cleanup_calls == 1
    assert page.probe_active is False


def test_diagnostic_probe_cleans_up_after_wheel_timeout(monkeypatch):
    class HangingWheelMouse(FakeFeedMouse):
        async def wheel(self, delta_x, delta_y):
            self.wheel_calls.append((delta_x, delta_y))
            await asyncio.Event().wait()

    page = DiagnosticFeedPage()
    page.mouse = HangingWheelMouse(page)

    with pytest.raises(VideoSwitchError) as caught:
        execute_with_short_deadline(page, monkeypatch, diagnostic=True)

    assert caught.value.code == "video_switch_timeout"
    assert len(caught.value.pulse_diagnostics) == 1
    assert page.mouse.wheel_calls == [(0, 120)]
    assert page.probe_install_calls == 1
    assert page.probe_cleanup_calls == 1
    assert page.probe_active is False


def test_diagnostic_probe_cleans_up_and_preserves_cancellation(monkeypatch):
    class CancelledWheelMouse(FakeFeedMouse):
        async def wheel(self, delta_x, delta_y):
            self.wheel_calls.append((delta_x, delta_y))
            raise asyncio.CancelledError()

    page = DiagnosticFeedPage()
    page.mouse = CancelledWheelMouse(page)

    with pytest.raises(asyncio.CancelledError):
        execute(page, monkeypatch, requested=1, diagnostic=True)

    assert page.mouse.wheel_calls == [(0, 120)]
    assert page.probe_install_calls == 1
    assert page.probe_cleanup_calls == 1
    assert page.probe_active is False


def test_non_diagnostic_failure_shape_remains_compatible(monkeypatch):
    page = DiagnosticFeedPage(never_switch=True, container_height=1)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1)

    assert set(caught.value.__dict__) == {
        "code",
        "completed_switches",
        "wheel_events",
        "requested_switches",
        "safe_fingerprint",
        "switches",
    }
    assert page.probe_install_calls == 0
    assert page.probe_cleanup_calls == 0


def _exception_traceback_text(error):
    rendered = []
    pending = [error]
    seen = set()
    seen_tasks = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend((repr(current), repr(current.__dict__)))
        traceback = current.__traceback__
        while traceback is not None:
            frame_locals = traceback.tb_frame.f_locals
            rendered.append(repr(frame_locals))
            for value in frame_locals.values():
                if (
                    isinstance(value, asyncio.Task)
                    and id(value) not in seen_tasks
                    and value.done()
                ):
                    seen_tasks.add(id(value))
                    if value.cancelled():
                        continue
                    try:
                        rendered.append(repr(value.result()))
                    except BaseException as task_error:
                        pending.append(task_error)
            traceback = traceback.tb_next
        pending.extend((current.__cause__, current.__context__))
    return "\n".join(rendered)


def test_diagnostic_error_tracebacks_do_not_retain_raw_identities(monkeypatch):
    class FailingWheelMouse(FakeFeedMouse):
        async def wheel(self, delta_x, delta_y):
            self.wheel_calls.append((delta_x, delta_y))
            raise RuntimeError("wheel failed")

    page = DiagnosticFeedPage()
    page.mouse = FailingWheelMouse(page)

    with pytest.raises(VideoSwitchError) as caught:
        execute(page, monkeypatch, requested=1, diagnostic=True)

    error = caught.value
    assert len(error.pulse_diagnostics) == 1
    assert error.pulse_diagnostics[0]["identity_hash_before"] == hashlib.sha256(
        b"raw-private-video-0"
    ).hexdigest()[:12]
    retained = _exception_traceback_text(error)
    assert all(raw not in retained for raw in page.raw_identities)


def test_real_task_cancellation_waits_for_diagnostic_cleanup(monkeypatch):
    class BlockingCleanupPage(DiagnosticFeedPage):
        def __init__(self):
            super().__init__(pulses_per_switch=1)
            self.cleanup_started = asyncio.Event()
            self.allow_cleanup = asyncio.Event()
            self.cleanup_finished = asyncio.Event()

        async def evaluate(self, expression):
            is_cleanup = (
                "__codex_verified_scroll_pulse_probe__" in expression
                and "new MutationObserver" not in expression
            )
            if not is_cleanup:
                return await super().evaluate(expression)
            self.cleanup_started.set()
            await self.allow_cleanup.wait()
            result = await super().evaluate(expression)
            self.cleanup_finished.set()
            return result

    async def scenario():
        page = BlockingCleanupPage()
        monkeypatch.setattr(
            browser_video_switch,
            "_monotonic",
            page.clock.monotonic,
        )
        task = asyncio.create_task(
            execute_verified_switches(
                page,
                direction="down",
                requested=1,
                interval_range=(0, 0),
                lifecycle=None,
                rng=FixedRng(),
                sleep_fn=page.clock.sleep,
                diagnostic=True,
            )
        )
        await asyncio.wait_for(page.cleanup_started.wait(), timeout=0.05)
        task.cancel()
        await asyncio.sleep(0)
        page.allow_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(page.cleanup_finished.wait(), timeout=0.05)
        assert page.probe_cleanup_calls == 1
        assert page.probe_active is False

    run(scenario())


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_completed_cleanup_cancellation_does_not_retain_raw_task_result(
    monkeypatch,
    cleanup_fails,
):
    class CompletedCleanupRacePage(DiagnosticFeedPage):
        def __init__(self):
            super().__init__(pulses_per_switch=1)
            self.caller_task = None

        async def evaluate(self, expression):
            is_cleanup = (
                "__codex_verified_scroll_pulse_probe__" in expression
                and "new MutationObserver" not in expression
            )
            if not is_cleanup:
                return await super().evaluate(expression)
            result = await super().evaluate(expression)
            asyncio.get_running_loop().call_soon(self.caller_task.cancel)
            if cleanup_fails:
                raise RuntimeError(
                    f"cleanup failed: {self.raw_identities[-1]}"
                )
            return result

    async def scenario():
        page = CompletedCleanupRacePage()
        monkeypatch.setattr(
            browser_video_switch,
            "_monotonic",
            page.clock.monotonic,
        )
        pulse_diagnostics = []

        async def operation(evidence):
            await page.mouse.wheel(0, 120)
            evidence["poll_count"] = 1
            evidence["different_identity_seen"] = True

        task = asyncio.create_task(
            browser_video_switch._run_diagnostic_pulse(
                page,
                deadline=page.clock.monotonic() + 8,
                pulse_index=1,
                delta=120,
                pulse_diagnostics=pulse_diagnostics,
                operation=operation,
            )
        )
        page.caller_task = task
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        return page, pulse_diagnostics, caught.value

    page, pulse_diagnostics, error = run(scenario())

    retained = _exception_traceback_text(error)
    assert all(raw not in retained for raw in page.raw_identities)
    assert len(pulse_diagnostics) == 1
    assert pulse_diagnostics[0]["identity_hash_before"] == hashlib.sha256(
        b"raw-private-video-0"
    ).hexdigest()[:12]
    assert page.mouse.wheel_calls == [(0, 120)]
    assert page.probe_cleanup_calls == 1
    assert page.probe_active is False
