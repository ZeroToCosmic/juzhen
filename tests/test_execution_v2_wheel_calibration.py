from __future__ import annotations

import asyncio

import pytest

from browser_video_switch import FeedState
from execution_v2.wheel_calibration import (
    dry_run_wheel_calibration,
    WheelCalibrationError,
    WheelCalibrationRunner,
    execute_calibrated_switches,
    normalize_wheel_samples,
    observe_single_transition,
)


class _Mouse:
    def __init__(self):
        self.moves = []
        self.wheels = []

    async def move(self, x, y):
        self.moves.append((x, y))

    async def wheel(self, x, y):
        self.wheels.append((x, y))


class _Page:
    def __init__(self):
        self.mouse = _Mouse()


class _Rng:
    def uniform(self, low, _high):
        return low


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def _sample(delta=100, *, transitions=1, delta_mode=0):
    return {
        "direction": "down",
        "identity_transitions": transitions,
        "events": [
            {"delta_x": 0, "delta_y": delta, "delta_mode": delta_mode, "delay_ms": 0}
        ],
    }


def _state(identity: str) -> FeedState:
    return FeedState(identity, f"safe-{identity}", 0, 0, 500, 800, 0)


async def _no_sleep(_seconds):
    return None


def test_three_consistent_samples_publish_median_event():
    assert normalize_wheel_samples([_sample(100), _sample(104), _sample(98)]) == {
        "direction": "down",
        "events": [
            {"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}
        ],
        "sample_count": 3,
    }


def test_different_event_counts_keep_real_group_nearest_median_total():
    samples = [
        {
            "direction": "down",
            "identity_transitions": 1,
            "events": [
                {"delta_x": 0, "delta_y": 45, "delta_mode": 0, "delay_ms": 0},
                {"delta_x": 1, "delta_y": 55, "delta_mode": 0, "delay_ms": 42},
            ],
        },
        _sample(104),
        {
            "direction": "down",
            "identity_transitions": 1,
            "events": [
                {"delta_x": 0, "delta_y": 30, "delta_mode": 0, "delay_ms": 0},
                {"delta_x": 0, "delta_y": 35, "delta_mode": 0, "delay_ms": 25},
                {"delta_x": 0, "delta_y": 33, "delta_mode": 0, "delay_ms": 28},
            ],
        },
    ]

    assert normalize_wheel_samples(samples) == {
        "direction": "down",
        "events": [
            {"delta_x": 0.0, "delta_y": 45.0, "delta_mode": 0, "delay_ms": 0.0},
            {"delta_x": 1.0, "delta_y": 55.0, "delta_mode": 0, "delay_ms": 42.0},
        ],
        "sample_count": 3,
    }


def test_dry_run_uses_one_event_per_candidate_and_stops_on_first_switch(monkeypatch):
    page = _Page()
    captures = iter([_state("a"), _state("a"), _state("a"), _state("a")])
    progress_updates = []

    async def capture(_page):
        return next(captures)

    async def observe(_page, before, **_kwargs):
        if page.mouse.wheels[-1][1] == 100.0:
            raise WheelCalibrationError("wheel_calibration_video_not_changed")
        return _state("b")

    async def progress(update):
        progress_updates.append(update)

    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    monkeypatch.setattr("execution_v2.wheel_calibration.observe_single_transition", observe)

    result = asyncio.run(
        dry_run_wheel_calibration(
            page,
            {"direction": "down", "events": _sample(100)["events"], "sample_count": 3},
            progress,
            asyncio.Event(),
            sleep_fn=_no_sleep,
        )
    )

    assert page.mouse.wheels == [(0.0, 100.0), (0.0, 150.0)]
    assert result["events"] == [
        {"delta_x": 0.0, "delta_y": 150.0, "delta_mode": 0, "delay_ms": 0.0}
    ]
    assert result["replay_validated"] is True
    assert progress_updates[-1]["candidate_results"][-1]["result"] == "passed"


def test_dry_run_all_single_events_miss_without_burst(monkeypatch):
    page = _Page()

    async def capture(_page):
        return _state("a")

    async def observe(_page, _before, **_kwargs):
        raise WheelCalibrationError("wheel_calibration_video_not_changed")

    async def progress(_update):
        return None

    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    monkeypatch.setattr("execution_v2.wheel_calibration.observe_single_transition", observe)

    with pytest.raises(WheelCalibrationError, match="wheel_calibration_replay_not_observed"):
        asyncio.run(
            dry_run_wheel_calibration(
                page,
                {"direction": "down", "events": _sample(100)["events"], "sample_count": 3},
                progress,
                asyncio.Event(),
                sleep_fn=_no_sleep,
            )
        )

    assert page.mouse.wheels == [
        (0.0, 100.0), (0.0, 150.0), (0.0, 200.0), (0.0, 300.0)
    ]


def test_dry_run_multiple_video_result_stops_without_larger_candidate(monkeypatch):
    page = _Page()

    async def capture(_page):
        return _state("a")

    async def observe(_page, _before, **_kwargs):
        raise WheelCalibrationError("wheel_calibration_multiple_videos")

    async def progress(_update):
        return None

    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    monkeypatch.setattr("execution_v2.wheel_calibration.observe_single_transition", observe)

    with pytest.raises(WheelCalibrationError, match="wheel_calibration_multiple_videos"):
        asyncio.run(
            dry_run_wheel_calibration(
                page,
                {"direction": "down", "events": _sample(100)["events"], "sample_count": 3},
                progress,
                asyncio.Event(),
                sleep_fn=_no_sleep,
            )
        )

    assert page.mouse.wheels == [(0.0, 100.0)]


def test_prepare_waits_for_slow_feed_before_injecting_recorder(monkeypatch):
    attempts = 0
    clock = _Clock()

    async def capture(_page):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("feed loading")
        return _state("ready")

    async def sleep(seconds):
        clock.value += seconds

    class Page:
        async def evaluate(self, script):
            assert "__codexV2WheelCalibration" in script
            return True

    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    asyncio.run(WheelCalibrationRunner(sleep_fn=sleep, clock=clock).prepare(Page()))
    assert attempts == 3
    assert clock.value == 1.0


@pytest.mark.parametrize(
    ("samples", "code"),
    [
        ([_sample(), _sample(), _sample(delta_mode=1)], "wheel_calibration_unsupported_delta_mode"),
        ([_sample(), _sample(), _sample(transitions=0)], "wheel_calibration_video_not_changed"),
        ([_sample(), _sample(), _sample(transitions=2)], "wheel_calibration_multiple_videos"),
        ([_sample(100), _sample(100), _sample(140)], "wheel_calibration_inconsistent"),
    ],
)
def test_invalid_samples_fail_closed(samples, code):
    with pytest.raises(WheelCalibrationError, match=code):
        normalize_wheel_samples(samples)


def test_observer_rejects_two_distinct_video_transitions(monkeypatch):
    states = iter([_state("b"), _state("c")])

    async def capture(_page):
        return next(states)

    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    done = asyncio.Event()
    done.set()
    with pytest.raises(WheelCalibrationError, match="wheel_calibration_multiple_videos"):
        asyncio.run(
            observe_single_transition(
                object(), _state("a"), side_effect_done=done, sleep_fn=_no_sleep
            )
        )


def test_calibrated_switch_dispatches_one_recorded_group(monkeypatch):
    states = iter([_state("a"), _state("b"), _state("b")])

    async def capture(_page):
        return next(states)

    monkeypatch.setattr("execution_v2.wheel_calibration.capture_feed_state", capture)
    page = _Page()
    result = asyncio.run(
        execute_calibrated_switches(
            page,
            {
                "revision": 4,
                "events": [
                    {"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}
                ],
            },
            direction="down",
            requested=1,
            interval_range=[0.2, 0.2],
            rng=_Rng(),
            sleep_fn=_no_sleep,
        )
    )

    assert page.mouse.wheels == [(0.0, 100.0)]
    assert result["completed_switches"] == 1
    assert result["calibration_revision"] == 4
