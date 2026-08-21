"""Execution V2 adapter tests (M4 increment 3): orchestration with mocks."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from execution_v2.models import ProfileOutcome

from agent.execution_v2_executor import ExecutionV2Executor


class FakeAdspower:
    def __init__(self):
        self.started = []
        self.stopped = []
        self.fail_start = False
        self.ws_url = "ws://fake-cdp"

    async def start(self, profile_id):
        if self.fail_start:
            raise RuntimeError("adspower down")
        self.started.append(profile_id)
        return self.ws_url

    async def stop(self, profile_id):
        self.stopped.append(profile_id)

    async def is_active(self, profile_id):
        return False


class FakeSessionFactory:
    def __init__(self):
        self.connected = []
        self.fail_connect = False
        self.binding = SimpleNamespace(
            resolver=object(),
            browser=SimpleNamespace(closed=False),
        )

        async def close():
            self.binding.browser.closed = True

        self.binding.browser.close = close

    async def connect(self, profile_id, ws_url):
        if self.fail_connect:
            raise RuntimeError("cdp refused")
        self.connected.append((profile_id, ws_url))
        return self.binding


class FakeStrategyExecutor:
    def __init__(self):
        self.calls = []
        self.outcome = None
        self.raise_error = None

    async def run(self, binding, snapshot):
        self.calls.append((binding, snapshot))
        if self.raise_error is not None:
            raise self.raise_error
        return self.outcome


def _subtask(profile_id="profile-1", with_snapshot=True):
    return {
        "subtask_id": "st-1",
        "profile_id": profile_id,
        "config_snapshot": (
            {
                "strategy": {
                    "target_url": "https://www.tiktok.com/",
                    "ready_element_id": "entry",
                    "readiness_timeout_seconds": 10,
                    "actions": [{"type": "scroll_down", "params": {}}],
                },
                "elements": {"entry": {"definition": {}}},
            }
            if with_snapshot
            else {}
        ),
    }


def test_success_path_maps_outcome():
    adspower = FakeAdspower()
    sessions = FakeSessionFactory()
    executor = FakeStrategyExecutor()
    executor.outcome = ProfileOutcome(
        "profile-1", True, "execute_action", action_results=({"ok": True},)
    )
    adapter = ExecutionV2Executor(adspower, sessions, strategy_executor=executor)
    outcome = adapter.execute(_subtask())
    assert outcome.status == "SUCCESS"
    assert outcome.result_data["action_count"] == 1
    assert adspower.started == ["profile-1"]
    assert adspower.stopped == ["profile-1"]
    assert sessions.connected == [("profile-1", "ws://fake-cdp")]
    assert sessions.binding.browser.closed is True


def test_missing_profile_id_fails_environment():
    adapter = ExecutionV2Executor(FakeAdspower(), FakeSessionFactory())
    outcome = adapter.execute(_subtask(profile_id=""))
    assert outcome.status == "FAILED"
    assert outcome.error_category == "environment"
    assert outcome.error_code == "missing_profile_id"


def test_missing_strategy_snapshot_fails_strategy():
    adapter = ExecutionV2Executor(FakeAdspower(), FakeSessionFactory())
    outcome = adapter.execute(_subtask(with_snapshot=False))
    assert outcome.status == "FAILED"
    assert outcome.error_category == "strategy"
    assert outcome.error_code == "missing_strategy_snapshot"


def test_adspower_start_failure_maps_environment():
    adspower = FakeAdspower()
    adspower.fail_start = True
    adapter = ExecutionV2Executor(adspower, FakeSessionFactory())
    outcome = adapter.execute(_subtask())
    assert outcome.status == "FAILED"
    assert outcome.error_category == "environment"
    assert outcome.error_code == "adspower_start_failed"
    assert adspower.stopped == []


def test_session_connect_failure_maps_environment():
    sessions = FakeSessionFactory()
    sessions.fail_connect = True
    adapter = ExecutionV2Executor(FakeAdspower(), sessions)
    outcome = adapter.execute(_subtask())
    assert outcome.status == "FAILED"
    assert outcome.error_category == "environment"
    assert outcome.error_code == "session_connect_failed"
    # profile still stopped in finally
    assert adapter._adspower.stopped == ["profile-1"]


def test_failed_outcome_maps_retryable():
    adspower = FakeAdspower()
    executor = FakeStrategyExecutor()
    executor.outcome = ProfileOutcome(
        "profile-1", False, "readiness", action_results=()
    )
    adapter = ExecutionV2Executor(adspower, FakeSessionFactory(), strategy_executor=executor)
    outcome = adapter.execute(_subtask())
    assert outcome.status == "FAILED"
    assert outcome.error_category == "retryable"
    assert outcome.error_code == "v2_execution_failed"


def test_lease_renewer_called_between_stages():
    adspower = FakeAdspower()
    renewals = []
    executor = FakeStrategyExecutor()
    executor.outcome = ProfileOutcome("profile-1", True, "execute_action", action_results=())
    adapter = ExecutionV2Executor(
        adspower,
        FakeSessionFactory(),
        strategy_executor=executor,
        lease_renewer=lambda: renewals.append("renew"),
    )
    # run with on_stage wired through executor: emulate by calling _on_stage
    asyncio.run(adapter._on_stage("profile-1", None, None))
    assert renewals == ["renew"]


def test_strategy_executor_default_constructed_when_missing():
    adspower = FakeAdspower()
    adapter = ExecutionV2Executor(adspower, FakeSessionFactory())
    assert adapter._strategy_executor is None
    outcome = adapter.execute(_subtask())
    # default StrategyExecutor requires real page bindings; without them it
    # fails on navigation, which is the expected behavior for a missing
    # injected executor in tests.
    assert outcome.status == "FAILED"
