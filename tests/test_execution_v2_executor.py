import asyncio

from browser_video_switch import VideoSwitchError
from execution_v2.executor import StrategyExecutor
from execution_v2.models import BrowserBinding, ProfileStatus, Stage
from execution_v2.readiness import PageReadinessError


class _Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


class _Page:
    def __init__(self):
        self.url = "https://www.tiktok.com/"
        self.navigations = []

    async def goto(self, url):
        self.navigations.append(url)
        self.url = url


class _Resolver:
    async def resolve(self, *_args, **_kwargs):
        raise AssertionError("readiness wait is injected in executor tests")


def _snapshot(run_mode="once", actions=None):
    return {
        "strategy": {
            "target_url": "https://www.tiktok.com/@example",
            "ready_element_id": "ready",
            "readiness_timeout_seconds": 20,
            "run_mode": run_mode,
            "loop_duration_minutes": None if run_mode == "once" else [1, 1],
            "actions": actions if actions is not None else [
                {"id": "one", "type": "wait", "duration_seconds": [0, 0]},
                {"id": "two", "type": "wait", "duration_seconds": [0, 0]},
            ],
        },
        "elements": [{"id": "ready", "definition": {"url_pattern": "https://www.tiktok.com/*"}}],
    }


def _binding():
    return BrowserBinding("profile-1", "ws://secret", object(), object(), _Page())


def test_executor_navigates_then_waits_then_runs_ordered_once_actions():
    calls = []

    async def ready(page, definition, resolver, **kwargs):
        calls.append(("ready", page.url, definition, kwargs["timeout_seconds"]))

    async def action(_page, action, *_args, **_kwargs):
        calls.append(("action", action["id"]))
        return {"action_id": action["id"], "action_type": action["type"], "status": "succeeded"}

    binding = _binding()
    outcome = asyncio.run(StrategyExecutor(_Resolver(), action_executor=action, readiness_waiter=ready).run(binding, _snapshot()))

    assert binding.page.navigations == ["https://www.tiktok.com/@example"]
    assert calls[0][0] == "ready"
    assert calls[1:] == [("action", "one"), ("action", "two")]
    assert outcome.succeeded is True
    assert outcome.stage is Stage.EXECUTE_ACTION
    assert outcome.action_results == (
        {"index": 0, "action_id": "one", "action_type": "wait", "status": "succeeded"},
        {"index": 1, "action_id": "two", "action_type": "wait", "status": "succeeded"},
    )


def test_default_executor_runs_real_wait_action_without_injected_rng():
    async def ready(*_args, **_kwargs):
        return None

    async def no_sleep(_seconds):
        return None

    outcome = asyncio.run(
        StrategyExecutor(
            _Resolver(), readiness_waiter=ready, sleep=no_sleep
        ).run(
            _binding(),
            _snapshot(actions=[
                {"id": "wait-1", "type": "wait", "duration_seconds": [0, 0]}
            ]),
        )
    )

    assert outcome.succeeded is True
    assert outcome.action_results == ({
        "index": 0,
        "action_id": "wait-1",
        "action_type": "wait",
        "status": "succeeded",
        "duration_seconds": 0.0,
    },)


def test_executor_preserves_explicitly_injected_rng():
    injected = object()
    seen = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(_page, item, *_args, **kwargs):
        seen.append(kwargs["rng"])
        return {
            "action_id": item["id"],
            "action_type": item["type"],
            "status": "succeeded",
        }

    outcome = asyncio.run(
        StrategyExecutor(
            _Resolver(),
            rng=injected,
            action_executor=action,
            readiness_waiter=ready,
        ).run(
            _binding(),
            _snapshot(actions=[
                {"id": "wait-1", "type": "wait", "duration_seconds": [0, 0]}
            ]),
        )
    )

    assert outcome.succeeded is True
    assert seen == [injected]


def test_executor_passes_one_frozen_wheel_calibration_to_actions():
    seen = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(_page, item, *_args, **kwargs):
        seen.append(kwargs["wheel_calibration"])
        return {
            "action_id": item["id"],
            "action_type": item["type"],
            "status": "succeeded",
        }

    snapshot = _snapshot()
    snapshot["wheel_calibration"] = {"revision": 7, "events": [{"delta_y": 100}]}
    outcome = asyncio.run(
        StrategyExecutor(
            _Resolver(), action_executor=action, readiness_waiter=ready
        ).run(_binding(), snapshot)
    )

    assert outcome.succeeded is True
    assert seen == [snapshot["wheel_calibration"], snapshot["wheel_calibration"]]


def test_executor_stops_after_first_action_failure_without_replay_or_refresh():
    calls = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(_page, item, *_args, **_kwargs):
        calls.append(item["id"])
        if item["id"] == "two":
            raise RuntimeError("sensitive ws://do-not-return")
        return {"action_id": item["id"], "action_type": item["type"], "status": "succeeded"}

    binding = _binding()
    outcome = asyncio.run(StrategyExecutor(_Resolver(), action_executor=action, readiness_waiter=ready).run(binding, _snapshot(actions=[
        {"id": "one", "type": "wait", "duration_seconds": [0, 0]},
        {"id": "two", "type": "wait", "duration_seconds": [0, 0]},
        {"id": "three", "type": "wait", "duration_seconds": [0, 0]},
    ])))

    assert calls == ["one", "two"]
    assert binding.page.navigations == ["https://www.tiktok.com/@example"]
    assert outcome.succeeded is False
    assert outcome.stage is Stage.EXECUTE_ACTION
    assert outcome.error_code == "action_execution_failed"
    assert "secret" not in outcome.error_summary
    assert [item["status"] for item in outcome.action_results] == ["succeeded", "failed"]


def test_executor_preserves_verified_video_switch_error_and_stops_later_actions():
    calls = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(_page, item, *_args, **_kwargs):
        calls.append(item["id"])
        if item["id"] == "scroll":
            raise VideoSwitchError("video_switch_not_observed")
        return {"action_id": item["id"], "action_type": item["type"], "status": "succeeded"}

    outcome = asyncio.run(StrategyExecutor(
        _Resolver(), action_executor=action, readiness_waiter=ready
    ).run(_binding(), _snapshot(actions=[
        {"id": "before", "type": "wait", "duration_seconds": [0, 0]},
        {"id": "scroll", "type": "scroll", "direction": "down"},
        {"id": "after", "type": "wait", "duration_seconds": [0, 0]},
    ])))

    assert calls == ["before", "scroll"]
    assert outcome.error_code == "video_switch_not_observed"
    assert outcome.action_results[-1]["error_code"] == "video_switch_not_observed"


def test_executor_returns_navigate_and_readiness_failures_with_stable_stages():
    async def action(*_args, **_kwargs):
        raise AssertionError("actions must not execute")

    class FailingPage(_Page):
        async def goto(self, _url):
            raise RuntimeError("navigation broke")

    binding = BrowserBinding("profile-1", "ws://secret", object(), object(), FailingPage())
    outcome = asyncio.run(StrategyExecutor(_Resolver(), action_executor=action).run(binding, _snapshot()))
    assert (outcome.succeeded, outcome.stage, outcome.error_code) == (False, Stage.NAVIGATE, "navigation_failed")

    async def no_action(*_args, **_kwargs):
        raise AssertionError("actions must not execute")

    async def failed_ready(*_args, **_kwargs):
        raise PageReadinessError("page_url_mismatch")

    outcome = asyncio.run(StrategyExecutor(_Resolver(), action_executor=no_action, readiness_waiter=failed_ready).run(_binding(), _snapshot()))
    assert (outcome.succeeded, outcome.stage, outcome.error_code) == (False, Stage.READINESS, "page_url_mismatch")


def test_duration_samples_one_deadline_and_never_starts_a_round_after_it():
    clock = _Clock()
    calls = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(_page, item, *_args, **_kwargs):
        calls.append(item["id"])
        clock.value += 40
        return {"action_id": item["id"], "action_type": item["type"], "status": "succeeded"}

    outcome = asyncio.run(StrategyExecutor(
        _Resolver(), action_executor=action, readiness_waiter=ready, clock=clock
    ).run(_binding(), _snapshot("duration")))

    assert calls == ["one", "two"]
    assert outcome.succeeded is True
    assert len(outcome.action_results) == 2


def test_executor_reports_stages_and_preserves_original_failure_when_capture_breaks():
    stages = []

    async def ready(*_args, **_kwargs):
        return None

    async def action(*_args, **_kwargs):
        raise RuntimeError("action failed")

    async def on_stage(profile_id, status, stage):
        stages.append((profile_id, status, stage))
        if status is ProfileStatus.WAITING_READINESS:
            raise RuntimeError("observer unavailable")

    async def capture_failure(_binding, _outcome):
        return "evidence/job-1/profile-1/failure.png"

    outcome = asyncio.run(StrategyExecutor(
        _Resolver(), action_executor=action, readiness_waiter=ready,
        on_stage=on_stage, capture_failure=capture_failure,
    ).run(_binding(), _snapshot()))

    assert outcome.error_code == "action_execution_failed"
    assert [status for _, status, _ in stages] == [
        ProfileStatus.NAVIGATING,
        ProfileStatus.WAITING_READINESS,
        ProfileStatus.EXECUTING,
        ProfileStatus.CAPTURING_EVIDENCE,
    ]
    assert outcome.action_results[-1]["evidence_path"] == "evidence/job-1/profile-1/failure.png"
    assert outcome.action_results[-1]["stage"] == Stage.CAPTURE_EVIDENCE.value


def test_executor_drops_unsafe_or_failed_failure_capture_without_masking_error():
    async def failed_ready(*_args, **_kwargs):
        raise PageReadinessError("ready failed")

    async def unsafe_capture(*_args, **_kwargs):
        return "C:\\secrets\\failure.png"

    outcome = asyncio.run(StrategyExecutor(
        _Resolver(), readiness_waiter=failed_ready, capture_failure=unsafe_capture,
    ).run(_binding(), _snapshot()))

    assert outcome.error_code == "ready failed"
    assert outcome.action_results == ()
