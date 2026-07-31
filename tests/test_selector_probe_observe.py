from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from selector_probe import probe as probe_module
from selector_probe.contracts import default_tiktok_contracts
from selector_probe.probe import (
    ModelOutputFormatError,
    ProbeCleanupFailed,
    ProbeLeaseLost,
    run_healing_probe,
    run_observe_probe,
)
from selector_probe.model_client import ModelRequestError
from selector_probe.snapshot import SemanticNode, SemanticSnapshot
from selector_probe.state_runner import ProbeSafetyError


NOW = datetime(2026, 7, 27, 19, 0, tzinfo=UTC)


def test_observe_candidate_definition_uses_live_nested_comment_input():
    contracts = default_tiktok_contracts()
    input_contract = next(
        item
        for item in contracts.values()
        if item.accepted_roles == ("textbox",)
    )
    snapshot = SemanticSnapshot(
        nodes=(
            SemanticNode(
                1,
                None,
                "div",
                "generic",
                "",
                {},
                {"data-e2e": "comment-input"},
                None,
                True,
                True,
                False,
            ),
            SemanticNode(
                2,
                1,
                "div",
                "generic",
                "",
                {},
                {},
                None,
                True,
                True,
                False,
            ),
            SemanticNode(
                3,
                2,
                "div",
                "textbox",
                "",
                {},
                {"contenteditable": "true"},
                None,
                True,
                True,
                False,
            ),
        )
    )

    definition = probe_module._observe_candidate_definition(
        input_contract.alias,
        "comment_panel_open",
        snapshot,
        {"scope": input_contract.scope, "locators": []},
    )

    assert definition is not None
    assert definition["locators"][0]["value"] == "comment-input"
    assert definition["locators"][0]["descendant"]["role"] == "textbox"


def test_observer_uses_validated_entry_proposal_for_panel_transition(
    monkeypatch,
):
    proposal = {
        "scope": "active_video",
        "locators": [
            {
                "id": "probe-entry",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }
        ],
    }
    monkeypatch.setattr(
        probe_module,
        "_observe_candidate_definition",
        lambda alias, state, _snapshot, _historical: (
            proposal
            if alias == "comment-entry" and state == "feed_ready"
            else None
        ),
    )

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            if state == "comment_panel_open":
                assert kwargs["comment_entry_override"] == proposal
            return {"state": state, "ready": True}

    class Snapshot:
        def model_payload(self):
            return {"scope": "page", "nodes": []}

    async def scenario():
        records = await probe_module._default_observe_page(
            object(),
            config(),
            {"comment-entry": {"scope": "active_video"}},
            state_runner_factory=lambda _config: Runner(),
            snapshot_extractor=lambda _page: asyncio.sleep(
                0,
                result=Snapshot(),
            ),
            element_inspector=lambda _page, alias, _definition: (
                asyncio.sleep(
                    0,
                    result={
                        "status": "ok",
                        "alias": alias,
                        "scope": "active_video",
                    },
                )
            ),
            heartbeat=SimpleNamespace(require_owned=lambda renew=False: None),
            stop_event=None,
        )

        assert len(records) == 2

    asyncio.run(scenario())


class RetrySnapshot:
    def model_payload(self):
        return {
            "scope": "page",
            "nodes": [
                {
                    "role": "button",
                    "name": "comments",
                    "states": {},
                    "attributes": {"data-e2e": "comment-icon"},
                    "visible": True,
                    "in_viewport": True,
                    "actionable": True,
                }
            ],
        }


async def retry_observe(runner, calls, snapshots, inspected, progress=None):
    async def snapshot(_page):
        snapshots.append(len(calls))
        return RetrySnapshot()

    async def inspect(_page, alias, definition):
        inspected.append(alias)
        return {
            "status": "ok",
            "alias": alias,
            "scope": definition["scope"],
        }

    return await probe_module._default_observe_page(
        object(),
        config(),
        {
            "feed": {"scope": "page"},
            "panel": {"scope": "visible_comment_panel"},
        },
        state_runner_factory=lambda _config: runner,
        snapshot_extractor=snapshot,
        element_inspector=inspect,
        heartbeat=SimpleNamespace(require_owned=lambda renew=False: None),
        stop_event=None,
        progress_sink=progress.append if progress is not None else None,
    )


def test_comment_readiness_reloads_three_times_before_snapshot():
    calls, snapshots, inspected = [], [], []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            calls.append((state, kwargs.get("initial_action", "")))
            comments = sum(x[0] == "comment_panel_open" for x in calls)
            if state == "comment_panel_open" and comments < 3:
                raise ProbeSafetyError(
                    "comment_panel_readiness_timeout",
                    "open_comment_panel",
                )
            return {"state": state, "ready": True}

    async def scenario():
        records = await retry_observe(
            Runner(), calls, snapshots, inspected
        )
        assert len(records) == 2
        assert calls[:6] == [
            ("feed_ready", "navigate"),
            ("comment_panel_open", ""),
            ("feed_ready", "reload"),
            ("comment_panel_open", ""),
            ("feed_ready", "reload"),
            ("comment_panel_open", ""),
        ]
        assert len(snapshots) == 2

    asyncio.run(scenario())


def test_three_comment_readiness_failures_skip_comment_snapshot_and_dry_run():
    calls, snapshots, inspected = [], [], []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            calls.append((state, kwargs.get("initial_action", "")))
            if state == "comment_panel_open":
                raise ProbeSafetyError(
                    "comment_panel_snapshot_unstable",
                    "open_comment_panel",
                )
            return {"state": state, "ready": True}

    async def scenario():
        with pytest.raises(ProbeSafetyError) as caught:
            await retry_observe(Runner(), calls, snapshots, inspected)
        assert caught.value.code == "comment_panel_snapshot_unstable"
        assert snapshots == [1]
        assert "panel" not in inspected
        assert sum(x[0] == "comment_panel_open" for x in calls) == 3

    asyncio.run(scenario())


def test_poisoned_panel_sampler_is_not_reloaded_or_retried():
    calls, snapshots, inspected = [], [], []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            calls.append((state, kwargs.get("initial_action", "")))
            if state == "comment_panel_open":
                raise ProbeSafetyError(
                    "probe_panel_check_failed",
                    "verify_comment_panel",
                )
            return {"state": state, "ready": True}

    async def scenario():
        with pytest.raises(ProbeSafetyError) as caught:
            await retry_observe(Runner(), calls, snapshots, inspected)
        assert caught.value.code == "probe_panel_check_failed"
        assert calls == [
            ("feed_ready", "navigate"),
            ("comment_panel_open", ""),
        ]
        assert snapshots == [1]

    asyncio.run(scenario())


def test_comment_retry_continues_after_one_reload_timeout():
    calls, snapshots, inspected, progress = [], [], [], []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            action = kwargs.get("initial_action", "")
            calls.append((state, action))
            reloads = calls.count(("feed_ready", "reload"))
            comments = sum(x[0] == "comment_panel_open" for x in calls)
            if state == "comment_panel_open" and comments == 1:
                raise ProbeSafetyError(
                    "comment_panel_readiness_timeout",
                    "open_comment_panel",
                )
            if state == "feed_ready" and action == "reload" and reloads == 1:
                raise ProbeSafetyError("probe_navigation_timeout", "reload")
            return {"state": state, "ready": True}

    async def scenario():
        records = await retry_observe(
            Runner(), calls, snapshots, inspected, progress
        )
        assert len(records) == 2
        assert calls[:5] == [
            ("feed_ready", "navigate"),
            ("comment_panel_open", ""),
            ("feed_ready", "reload"),
            ("feed_ready", "reload"),
            ("comment_panel_open", ""),
        ]
        transition = [
            item
            for item in progress
            if item["name"] == "comment_panel_transition"
        ]
        assert transition[-1]["status"] == "passed"
        assert transition[-1]["attempt_count"] == 3

    asyncio.run(scenario())


class FixedClock:
    def now(self):
        return NOW


class FakeStore:
    def __init__(self, *, last_completed=None, events=None):
        self._last_completed = last_completed
        self.events = events
        self.started = []
        self.finished = []
        self.validations = []
        self.progress = []

    def last_completed_slot(self):
        return self._last_completed

    def start_run(self, **payload):
        self.started.append(payload)
        return 17

    def finish_run(self, run_id, **payload):
        if self.events is not None:
            self.events.append("finish")
        self.finished.append((run_id, payload))

    def record_validation(self, **payload):
        self.validations.append(payload)
        return len(self.validations)

    def update_run_progress(self, run_id, **payload):
        self.progress.append((run_id, payload))


class FakeLease:
    def __init__(
        self,
        *,
        acquired=True,
        renews=True,
        released=True,
        events=None,
    ):
        self.acquired = acquired
        self.renews = renews
        self.released = released
        self.events = events
        self.calls = []
        self.heartbeat_seconds = 30

    def acquire(self):
        self.calls.append("acquire")
        return self.acquired

    def renew(self):
        self.calls.append("renew")
        return self.renews

    def release(self):
        if self.events is not None:
            self.events.append("release")
        self.calls.append("release")
        return self.released


class LeaseFactory:
    def __init__(self, lease):
        self.lease = lease
        self.calls = []

    def __call__(
        self,
        client,
        key,
        owner_id,
        *,
        ttl_seconds,
        heartbeat_seconds,
    ):
        self.calls.append(
            {
                "client": client,
                "key": key,
                "owner_id": owner_id,
                "ttl_seconds": ttl_seconds,
                "heartbeat_seconds": heartbeat_seconds,
            }
        )
        return self.lease


class FakePage:
    def __init__(self, profile_mask):
        self.profile_mask = profile_mask


class FakeSessionManager:
    instances = []

    def __init__(
        self,
        _client,
        *,
        allowed_profile_ids,
        wait_for_cdp,
        stop_requested=None,
    ):
        self.allowed_profile_ids = tuple(allowed_profile_ids)
        self.wait_for_cdp = wait_for_cdp
        self.stop_requested = stop_requested
        self.pages = []
        self.closed = []
        self.stopped = []
        type(self).instances.append(self)

    def open_profiles(self, profile_ids):
        assert tuple(profile_ids) == self.allowed_profile_ids
        return [
            SimpleNamespace(
                profile_id=item,
                profile_mask=f"***{item[-4:]}",
                started_by_probe=True,
            )
            for item in profile_ids
        ]

    async def open_probe_page(self, _playwright, handle):
        page = FakePage(handle.profile_mask)
        page_handle = SimpleNamespace(
            profile=handle,
            page=page,
            created_by_probe=True,
        )
        self.pages.append(page_handle)
        return page_handle

    async def close_owned_pages(self, page_handles):
        self.closed.extend(page_handles)
        return [
            {
                "profile_mask": item.profile.profile_mask,
                "stage": "close_page",
                "ok": True,
                "code": "",
            }
            for item in page_handles
        ]

    def stop_owned_profiles(self, profile_handles):
        self.stopped.extend(profile_handles)
        return [
            {
                "profile_mask": item.profile_mask,
                "stage": "stop_profile",
                "ok": True,
                "code": "",
            }
            for item in profile_handles
        ]


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def config(*, enabled=True, observe_only=True):
    return SimpleNamespace(
        enabled=enabled,
        observe_only=observe_only,
        site="tiktok",
        environment="production",
        timezone="Asia/Shanghai",
        daily_time=datetime.strptime("03:00", "%H:%M").time(),
        target_url="https://www.tiktok.com/",
        test_profile_ids=("profile-a", "profile-b"),
    )


async def start_playwright():
    return FakePlaywright()


async def passing_observer(page, _config, _elements):
    return [
        {
            "page_state": "feed_ready",
            "result": "passed",
            "failure_code": "",
            "evidence": {
                "snapshot_hash": page.profile_mask,
                "aliases": {"comment_entry": {"status": "ok"}},
            },
        }
    ]


def run_with_fakes(
    *,
    probe_config=None,
    store=None,
    lease=None,
    observer=passing_observer,
    elements=None,
    session_manager_factory=FakeSessionManager,
    stop_event=None,
    force=False,
):
    store = store or FakeStore()
    lease = lease or FakeLease()
    lease_factory = LeaseFactory(lease)
    result = run_observe_probe(
        config=probe_config or config(),
        store=store,
        redis_client=object(),
        adspower_client=object(),
        clock=FixedClock(),
        elements=elements or {"comment_entry": {"scope": "page"}},
        lease_factory=lease_factory,
        session_manager_factory=session_manager_factory,
        wait_for_cdp=lambda _url: True,
        playwright_starter=start_playwright,
        observe_page=observer,
        owner_id_factory=lambda: "owner-safe",
        stop_event=stop_event,
        force=force,
    )
    return result, store, lease, lease_factory


def test_disabled_probe_returns_without_schedule_or_lease():
    store = FakeStore()
    result, _, lease, lease_factory = run_with_fakes(
        probe_config=config(enabled=False),
        store=store,
    )

    assert result == {"status": "disabled", "observe_only": True}
    assert store.started == []
    assert lease.calls == []
    assert lease_factory.calls == []


def test_not_due_returns_before_claiming_lease():
    store = FakeStore(last_completed=NOW)
    result, _, lease, lease_factory = run_with_fakes(store=store)

    assert result == {"status": "not_due", "observe_only": True}
    assert store.started == []
    assert lease.calls == []
    assert lease_factory.calls == []


def test_force_observe_runs_even_when_daily_slot_is_completed():
    store = FakeStore(last_completed=NOW)

    result, _, lease, _lease_factory = run_with_fakes(
        store=store,
        force=True,
    )

    assert result["status"] == "completed"
    assert lease.calls[0] == "acquire"


def test_busy_site_environment_lease_does_not_start_durable_run():
    result, store, lease, lease_factory = run_with_fakes(
        lease=FakeLease(acquired=False)
    )

    assert result == {"status": "lease_busy", "observe_only": True}
    assert store.started == []
    assert lease.calls == ["acquire"]
    assert lease_factory.calls[0]["key"] == (
        "selector_registry:production:tiktok:lease"
    )
    assert lease_factory.calls[0]["ttl_seconds"] == 120
    assert lease_factory.calls[0]["heartbeat_seconds"] == 30


def test_completed_run_records_both_profiles_and_cleans_all_owned_resources():
    FakeSessionManager.instances.clear()
    result, store, lease, _ = run_with_fakes()
    session = FakeSessionManager.instances[-1]

    assert result == {
        "status": "completed",
        "observe_only": True,
        "run_id": 17,
        "profiles_observed": 2,
        "validations_recorded": 4,
        "lease_released": True,
    }
    assert store.started == [
        {
                "scheduled_for": NOW.isoformat(),
                "active_version_before": "",
                "attempt_token": "owner-safe",
                "management_request_id": "",
                "trigger": "scheduled",
            }
        ]
    assert len(store.validations) == 4
    assert {item["profile_mask"] for item in store.validations} == {
        "***le-a",
        "***le-b",
    }
    assert {item["round_number"] for item in store.validations} == {1, 2}
    assert all(
        item["attempt_token"] == "owner-safe"
        for item in store.validations
    )
    assert store.finished[0][1]["status"] == "completed"
    assert store.finished[0][1]["attempt_token"] == "owner-safe"
    assert store.finished[0][1]["details"]["observe_only"] is True
    assert len(session.closed) == 2
    assert len(session.pages) == 2
    assert len(session.stopped) == 2
    assert lease.calls == ["acquire", "renew", "release"]


def test_lease_loss_blocks_completed_and_persists_sanitized_cleanup():
    FakeSessionManager.instances.clear()
    store = FakeStore()
    with pytest.raises(ProbeLeaseLost):
        run_with_fakes(store=store, lease=FakeLease(renews=False))

    session = FakeSessionManager.instances[-1]
    assert len(session.closed) == 2
    assert len(session.stopped) == 2
    assert store.finished[0][1]["status"] == "probe_lease_lost"
    details = store.finished[0][1]["details"]
    assert details["failure_code"] == "probe_lease_lost"
    assert "profile-a" not in str(details)
    assert "profile-b" not in str(details)
    assert "ws://" not in str(details)
    assert all(
        set(item) == {"profile_mask", "stage", "ok", "code"}
        for item in details["cleanup"]
    )


def test_observer_exception_finishes_failed_and_cleans_before_reraising():
    async def failing_observer(_page, _config, _elements):
        raise RuntimeError("ws://secret profile-a")

    FakeSessionManager.instances.clear()
    store = FakeStore()
    with pytest.raises(RuntimeError, match="secret"):
        run_with_fakes(store=store, observer=failing_observer)

    session = FakeSessionManager.instances[-1]
    assert len(session.closed) == 1
    assert len(session.stopped) == 2
    assert store.finished[0][1]["status"] == "probe_unavailable"
    assert "secret" not in str(store.finished[0])


def test_cancelled_observer_finishes_cancelled_and_always_cleans_up():
    async def cancelled_observer(_page, _config, _elements):
        raise asyncio.CancelledError()

    FakeSessionManager.instances.clear()
    store = FakeStore()
    with pytest.raises(asyncio.CancelledError):
        run_with_fakes(store=store, observer=cancelled_observer)

    session = FakeSessionManager.instances[-1]
    assert len(session.closed) == 1
    assert len(session.stopped) == 2
    assert store.finished[0][1]["status"] == "probe_cancelled"


def test_default_observer_groups_saved_elements_by_read_only_page_state():
    states = []
    inspected = []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            if state == "feed_ready":
                assert kwargs["initial_action"] == "navigate"
            if state == "comment_panel_open":
                assert kwargs["comment_entry_override"]["scope"] == (
                    "active_video"
                )
            states.append(state)
            return {"state": state, "ready": True}

    class Snapshot:
        def model_payload(self):
            return {
                "scope": "page",
                "nodes": [
                    {
                        "role": "button",
                        "name": "comments",
                        "states": {},
                        "attributes": {"data-e2e": "comment-icon"},
                        "visible": True,
                        "in_viewport": True,
                        "actionable": True,
                    }
                ],
            }

    async def snapshot(_page):
        return Snapshot()

    async def inspect(_page, alias, _definition):
        inspected.append(alias)
        return {"status": "ok", "alias": alias, "scope": "page"}

    elements = {
        "feed": {"scope": "page"},
        "panel": {"scope": "visible_comment_panel"},
    }
    result = run_observe_probe(
        config=config(),
        store=FakeStore(),
        redis_client=object(),
        adspower_client=object(),
        clock=FixedClock(),
        elements=elements,
        lease_factory=LeaseFactory(FakeLease()),
        session_manager_factory=FakeSessionManager,
        wait_for_cdp=lambda _url: True,
        playwright_starter=start_playwright,
        state_runner_factory=lambda _config: Runner(),
        snapshot_extractor=snapshot,
        element_inspector=inspect,
        owner_id_factory=lambda: "owner-safe",
    )

    assert result["status"] == "completed"
    assert states == [
        "feed_ready",
        "comment_panel_open",
        "comment_panel_closed",
        "feed_ready",
        "comment_panel_open",
        "comment_panel_closed",
        "feed_ready",
        "comment_panel_open",
        "comment_panel_closed",
        "feed_ready",
        "comment_panel_open",
        "comment_panel_closed",
    ]
    assert inspected == [
        "feed",
        "comment-entry",
        "panel",
        "feed",
        "comment-entry",
        "panel",
        "feed",
        "comment-entry",
        "panel",
        "feed",
        "comment-entry",
        "panel",
    ]


def test_observer_falls_back_to_profile_close_when_escape_cannot_close_panel():
    progress = []

    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **_kwargs):
            if state == "comment_panel_closed":
                raise ProbeSafetyError(
                    "probe_state_verification_failed",
                    "close_comment_panel",
                )
            return {"state": state, "ready": True}

    class Snapshot:
        def model_payload(self):
            return {
                "scope": "page",
                "nodes": [
                    {
                        "role": "button",
                        "name": "comments",
                        "states": {},
                        "attributes": {"data-e2e": "comment-icon"},
                        "visible": True,
                        "in_viewport": True,
                        "actionable": True,
                    }
                ],
            }

    async def scenario():
        records = await probe_module._default_observe_page(
            object(),
            config(),
            {"panel": {"scope": "visible_comment_panel"}},
            state_runner_factory=lambda _config: Runner(),
            snapshot_extractor=lambda _page: asyncio.sleep(
                0,
                result=Snapshot(),
            ),
            element_inspector=lambda _page, alias, _definition: (
                asyncio.sleep(
                    0,
                    result={
                        "status": "ok",
                        "alias": alias,
                        "scope": "active_video",
                    },
                )
            ),
            heartbeat=SimpleNamespace(require_owned=lambda renew=False: None),
            stop_event=None,
            progress_sink=progress.append,
        )

        assert len(records) == 2
        assert records[-1]["evidence"]["panel_cleanup"] == {
            "status": "fallback",
            "method": "profile_window_close",
        }
        assert progress[-1]["name"] == "comment_panel_cleanup"
        assert progress[-1]["status"] == "passed"

    asyncio.run(scenario())


def test_default_observer_discovers_feed_and_comment_panel_controls():
    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, page, state, _elements, **kwargs):
            if state == "comment_panel_open":
                assert kwargs["comment_entry_override"]["locators"][0][
                    "value"
                ] == "comment-icon"
            page.state = state
            return {"state": state, "ready": True}

    class Snapshot:
        def __init__(self, nodes):
            self.nodes = nodes

        def model_payload(self):
            return {"scope": "page", "nodes": self.nodes}

    async def snapshot(page):
        if page.state == "feed_ready":
            nodes = [
                {
                    "role": "button",
                    "name": "comments",
                    "states": {},
                    "attributes": {"data-e2e": "comment-icon"},
                    "visible": True,
                    "in_viewport": True,
                    "actionable": True,
                }
            ]
        else:
            nodes = [
                {
                    "role": "textbox",
                    "name": "comment-input",
                    "states": {"editable": True},
                    "attributes": {
                        "data-e2e": "comment-input",
                        "contenteditable": "true",
                    },
                    "visible": True,
                    "in_viewport": True,
                    "actionable": True,
                },
                {
                    "role": "button",
                    "name": "publish",
                    "states": {},
                    "attributes": {"data-e2e": "comment-post"},
                    "visible": True,
                    "in_viewport": True,
                    "actionable": True,
                },
            ]
        return Snapshot(nodes)

    async def inspect_discovered(_page, _alias, _definition):
        return {"status": "ok"}

    store = FakeStore()
    result = run_observe_probe(
        config=config(),
        store=store,
        redis_client=object(),
        adspower_client=object(),
        clock=FixedClock(),
        elements={},
        lease_factory=LeaseFactory(FakeLease()),
        session_manager_factory=FakeSessionManager,
        wait_for_cdp=lambda _url: True,
        playwright_starter=start_playwright,
        state_runner_factory=lambda _config: Runner(),
        snapshot_extractor=snapshot,
        element_inspector=inspect_discovered,
        owner_id_factory=lambda: "owner-safe",
    )

    assert result["status"] == "completed"
    assert len(store.validations) == 8
    assert {item["round_number"] for item in store.validations} == {1, 2}
    panel_discoveries = [
        candidate
        for validation in store.validations
        if validation["page_state"] == "comment_panel_open"
        for candidate in validation["evidence"]["discoveries"]
    ]
    assert {item["role"] for item in panel_discoveries} == {
        "textbox",
        "button",
    }
    assert all(
        "backend_node_id" not in item for item in panel_discoveries
    )


def test_discovered_comment_entry_replaces_failed_saved_entry_for_run():
    class Runner:
        comment_entry_alias = "comment-entry"

        async def ensure_state(self, _page, state, _elements, **kwargs):
            if state == "comment_panel_open":
                assert kwargs["comment_entry_override"]["locators"][0][
                    "value"
                ] == "comment-icon"
            return {"state": state, "ready": True}

    class Snapshot:
        def model_payload(self):
            return {
                "scope": "page",
                "nodes": [
                    {
                        "role": "button",
                        "name": "comments",
                        "states": {},
                        "attributes": {"data-e2e": "comment-icon"},
                        "visible": True,
                        "in_viewport": True,
                        "actionable": True,
                    }
                ],
            }

    async def inspect(_page, alias, _definition):
        if _definition.get("locators"):
            return {"status": "ok", "alias": alias}
        return {
            "status": "not_found",
            "code": "zero_match",
            "alias": alias,
        }

    async def snapshot(_page):
        return Snapshot()

    store = FakeStore()
    result = run_observe_probe(
        config=config(),
        store=store,
        redis_client=object(),
        adspower_client=object(),
        clock=FixedClock(),
        elements={"comment-entry": {"scope": "page"}},
        lease_factory=LeaseFactory(FakeLease()),
        session_manager_factory=FakeSessionManager,
        wait_for_cdp=lambda _url: True,
        playwright_starter=start_playwright,
        state_runner_factory=lambda _config: Runner(),
        snapshot_extractor=snapshot,
        element_inspector=inspect,
        owner_id_factory=lambda: "owner-safe",
    )

    assert result["status"] == "completed"
    feed = [
        item
        for item in store.validations
        if item["page_state"] == "feed_ready"
    ]
    assert all(item["result"] == "passed" for item in feed)
    assert all(
        item["evidence"]["transition_selector_source"]
        == "discovery_fallback"
        for item in feed
    )


def test_completed_slot_is_latest_due_slot_not_an_old_backlog_slot():
    stale = NOW - timedelta(days=20)
    result, store, _, _ = run_with_fakes(
        store=FakeStore(last_completed=stale)
    )

    assert result["status"] == "completed"
    assert store.started[0]["scheduled_for"] == NOW.isoformat()


def test_terminal_run_is_finished_while_lease_is_still_owned():
    events = []
    store = FakeStore(events=events)
    lease = FakeLease(events=events)

    result, _, _, _ = run_with_fakes(store=store, lease=lease)

    assert result["lease_released"] is True
    assert events == ["finish", "release"]


def test_release_failure_is_best_effort_after_completed_terminal_state():
    store = FakeStore()
    result, _, lease, _ = run_with_fakes(
        store=store,
        lease=FakeLease(released=False),
    )

    assert result["status"] == "completed"
    assert result["lease_released"] is False
    assert store.finished[0][1]["status"] == "completed"
    assert lease.calls[-1] == "release"


def test_observe_run_persists_bounded_safe_stage_progress():
    _result, store, _lease, _lease_factory = run_with_fakes()

    assert store.progress
    assert all(run_id == 17 for run_id, _payload in store.progress)
    assert all(
        payload["attempt_token"] == "owner-safe"
        and len(payload["stages"]) <= 30
        for _run_id, payload in store.progress
    )
    final_stages = store.finished[-1][1]["details"]["stages"]
    assert {item["name"] for item in final_stages} >= {
        "profile_session",
        "cleanup",
        "lease_release",
    }
    assert "profile-a" not in repr(final_stages)


def test_stop_event_cancels_active_observation_and_then_cleans_up():
    cancelled = threading.Event()
    stop_event = threading.Event()

    async def waiting_observer(_page, _config, _elements):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.set()

    timer = threading.Timer(0.02, stop_event.set)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(asyncio.CancelledError):
            run_with_fakes(
                observer=waiting_observer,
                stop_event=stop_event,
            )
    finally:
        timer.cancel()

    assert time.monotonic() - started < 0.5
    assert cancelled.is_set()


def test_heartbeat_loss_cancels_active_observation(monkeypatch):
    cancelled = threading.Event()

    async def waiting_observer(_page, _config, _elements):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.set()

    monkeypatch.setattr(
        probe_module,
        "LEASE_HEARTBEAT_SECONDS",
        0.01,
    )
    started = time.monotonic()
    with pytest.raises(ProbeLeaseLost):
        run_with_fakes(
            observer=waiting_observer,
            lease=FakeLease(renews=False),
        )

    assert time.monotonic() - started < 0.5
    assert cancelled.is_set()


def test_cleanup_failure_blocks_completed_status():
    class CleanupFailingSession(FakeSessionManager):
        async def close_owned_pages(self, page_handles):
            self.closed.extend(page_handles)
            return [
                {
                    "profile_mask": page_handles[0].profile.profile_mask,
                    "stage": "close_page",
                    "ok": False,
                    "code": "page_close_failed",
                }
            ]

    store = FakeStore()
    with pytest.raises(ProbeCleanupFailed):
        run_with_fakes(
            store=store,
            session_manager_factory=CleanupFailingSession,
        )

    assert store.finished[0][1]["status"] == "probe_cleanup_failed"


def test_cleanup_cancellation_is_delayed_until_other_cleanup_finishes():
    class CancellingCleanupSession(FakeSessionManager):
        async def close_owned_pages(self, page_handles):
            self.closed.extend(page_handles)
            raise asyncio.CancelledError()

    FakeSessionManager.instances.clear()
    store = FakeStore()
    with pytest.raises(asyncio.CancelledError):
        run_with_fakes(
            store=store,
            session_manager_factory=CancellingCleanupSession,
        )

    session = FakeSessionManager.instances[-1]
    assert len(session.closed) == 2
    assert len(session.stopped) == 2
    assert store.finished[0][1]["status"] == "probe_cancelled"


def test_heartbeat_stop_timeout_marks_lease_lost(monkeypatch):
    entered = threading.Event()
    unblock = threading.Event()

    class BlockingLease(FakeLease):
        def renew(self):
            entered.set()
            unblock.wait(1)
            return True

    monkeypatch.setattr(
        probe_module,
        "LEASE_HEARTBEAT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        probe_module,
        "HEARTBEAT_JOIN_TIMEOUT_SECONDS",
        0.01,
    )

    async def observer(_page, _config, _elements):
        assert entered.wait(0.2)
        return await passing_observer(_page, _config, _elements)

    try:
        with pytest.raises(ProbeLeaseLost):
            run_with_fakes(
                observer=observer,
                lease=BlockingLease(),
            )
    finally:
        unblock.set()


def test_default_cdp_wait_checks_stop_between_one_second_slices():
    stop_event = threading.Event()
    calls = []

    def wait_fn(_url, *, timeout, interval):
        calls.append((timeout, interval))
        stop_event.set()
        raise RuntimeError("not ready")

    heartbeat = probe_module._LeaseHeartbeat(FakeLease())
    with pytest.raises(asyncio.CancelledError):
        probe_module._wait_for_cdp_in_slices(
            "ws://profile-a",
            heartbeat=heartbeat,
            stop_event=stop_event,
            wait_fn=wait_fn,
        )

    assert len(calls) == 1
    assert calls[0][0] <= 1.0


def healing_bundle(version):
    elements = {
        "comment_entry": {
            "scope": "active_video",
            "locators": [
                {
                    "id": f"{version}-comment-entry",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-icon",
                    "enabled": True,
                }
            ],
        }
    }
    payload = json.dumps(
        elements,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "version": version,
        "bundle_hash": "sha256:"
        + hashlib.sha256(payload.encode()).hexdigest(),
        "elements": elements,
    }


def healing_evidence(bundle):
    validations = []
    alias_evidence = {
        "comment_entry": {
            "status": "ok",
            "candidate_id": bundle["elements"]["comment_entry"][
                "locators"
            ][0]["id"],
        }
    }
    for round_number in (1, 2):
        for profile_number in range(2):
            marker = f"{profile_number}:{round_number}"
            validations.append(
                {
                    "profile_mask": f"***p{profile_number:03d}",
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:"
                    + hashlib.sha256(f"reset:{marker}".encode()).hexdigest(),
                    "snapshot_hash": "sha256:"
                    + hashlib.sha256(
                        f"snapshot:{marker}".encode()
                    ).hexdigest(),
                    "page_generation": "sha256:"
                    + hashlib.sha256(
                        f"generation:{marker}".encode()
                    ).hexdigest(),
                    "aliases": copy.deepcopy(alias_evidence),
                }
            )
    return {
        "status": "passed",
        "bundle_hash": bundle["bundle_hash"],
        "profiles_passed": 2,
        "rounds_passed": 2,
        "validations": validations,
    }


class HealingRuntime:
    def __init__(
        self,
        *,
        active_result=None,
        candidate_results=(),
        full_result=None,
        publication=None,
        deterministic_bundle=None,
        repeated_context=False,
    ):
        self.active_result = active_result or {
            "status": "failed",
            "failure_class": "selector",
            "failed_aliases": ["comment_entry"],
        }
        self.candidate_results = list(candidate_results)
        self.full_result = full_result
        self.publication = publication or {
            "published": True,
            "version": "sel-new",
            "reconciled": True,
        }
        self.deterministic_bundle = (
            deterministic_bundle or healing_bundle("deterministic")
        )
        self.repeated_context = repeated_context
        self.events = []
        self.context_generation = 0

    def validate_active(self):
        self.events.append("validate_active")
        return self.active_result

    def deterministic_candidates(self, *, candidate_fn=None):
        self.events.append("deterministic")
        if candidate_fn is not None:
            return candidate_fn(self.deterministic_bundle)
        return self.deterministic_bundle

    def fresh_validation_context(self):
        self.context_generation += 1
        generation = 1 if self.repeated_context else self.context_generation
        self.events.append(f"context:{generation}")
        snapshot = {
            "generation": generation,
            "nodes": [],
        }
        snapshot_hash = "sha256:" + hashlib.sha256(
            json.dumps(
                snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        page_generation = "sha256:" + hashlib.sha256(
            f"page:{generation}".encode()
        ).hexdigest()
        return {
            "active_bundle": {
                "version": "sel-old",
                "elements": {"comment_entry": {"locators": []}},
            },
            "snapshot": snapshot,
            "snapshot_hash": snapshot_hash,
            "page_generation": page_generation,
            "contracts": {
                "comment_entry": {"intent": "open comments"},
            },
        }

    def repair_candidate(self, **_kwargs):
        return None

    def validate_candidate(self, bundle):
        self.events.append(f"validate:{bundle['version']}")
        return self.candidate_results.pop(0)

    def full_validate(self, bundle):
        self.events.append(f"full:{bundle['version']}")
        return self.full_result or healing_evidence(bundle)

    def store_and_publish(self, bundle, full_evidence):
        self.events.append(f"publish:{bundle['version']}")
        assert full_evidence == (self.full_result or healing_evidence(bundle))
        return self.publication


def test_healing_healthy_active_skips_candidate_context_and_llm():
    calls = []
    runtime = HealingRuntime(
        active_result={"status": "passed", "version": "sel-current"}
    )

    result = run_healing_probe(
        runtime,
        candidate_fn=lambda **_kwargs: calls.append("candidate"),
        model_call=lambda *_args, **_kwargs: calls.append("llm"),
        repair_fn=lambda **_kwargs: calls.append("repair"),
    )

    assert result == {
        "status": "healthy",
        "published": False,
        "new_version": None,
        "proposed_pause_aliases": [],
    }
    assert runtime.events == ["validate_active"]
    assert calls == []


@pytest.mark.parametrize(
    "active_result",
    [
        {"status": "unavailable", "failure_class": "infrastructure"},
        {
            "status": "failed",
            "failure_class": "infrastructure",
            "failed_aliases": ["comment_entry"],
        },
    ],
)
def test_healing_infrastructure_failure_bypasses_candidate_and_llm(
    active_result,
):
    calls = []
    runtime = HealingRuntime(active_result=active_result)

    result = run_healing_probe(
        runtime,
        candidate_fn=lambda **_kwargs: calls.append("candidate"),
        model_call=lambda *_args, **_kwargs: calls.append("llm"),
        repair_fn=lambda **_kwargs: calls.append("repair"),
    )

    assert result["status"] == "infrastructure_unavailable"
    assert result["published"] is False
    assert result["proposed_pause_aliases"] == []
    assert runtime.events == ["validate_active"]
    assert calls == []


def test_healing_validates_deterministic_candidate_before_repair_and_publish():
    bundle = healing_bundle("deterministic-1")
    runtime = HealingRuntime(
        candidate_results=[{"status": "passed"}],
        deterministic_bundle=bundle,
    )
    calls = []

    result = run_healing_probe(
        runtime,
        repair_fn=lambda **_kwargs: calls.append(("repair", 1)),
    )

    assert result["status"] == "published"
    assert result["published"] is True
    assert result["new_version"] == "sel-new"
    assert result["proposed_pause_aliases"] == []
    assert calls == []
    assert runtime.events == [
        "validate_active",
        "deterministic",
        "validate:deterministic-1",
        "full:deterministic-1",
        "publish:deterministic-1",
    ]


def test_healing_uses_three_fresh_repair_contexts_and_accumulates_prohibitions():
    runtime = HealingRuntime(
        candidate_results=[
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
                "prohibited_methods": ["attribute:data-e2e"],
            },
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
        ],
        deterministic_bundle=healing_bundle("deterministic"),
    )
    repairs = []

    def repair_fn(*, attempt, prohibited_methods, context, **_kwargs):
        repairs.append(
            {
                "attempt": attempt,
                "prohibited": tuple(prohibited_methods),
                "generation": context["snapshot"]["generation"],
            }
        )
        bundle = healing_bundle(f"repair-{attempt}")
        bundle["elements"]["comment_entry"]["locators"][0] = {
            "id": f"repair-{attempt}-role",
            "type": "role",
            "role": "button",
            "name": f"comments-{attempt}",
            "name_mode": "exact",
            "enabled": True,
        }
        return bundle

    result = run_healing_probe(
        runtime,
        repair_fn=repair_fn,
    )

    assert [item["attempt"] for item in repairs] == [1, 2, 3]
    assert [item["generation"] for item in repairs] == [1, 2, 3]
    assert "attribute:data-e2e" in repairs[0]["prohibited"]
    assert set(repairs[0]["prohibited"]) < set(repairs[1]["prohibited"])
    assert set(repairs[1]["prohibited"]) < set(repairs[2]["prohibited"])
    assert result["status"] == "selector_validation_failed"
    assert result["published"] is False
    assert result["new_version"] is None
    assert result["proposed_pause_aliases"] == ["comment_entry"]
    assert not any(event.startswith("publish:") for event in runtime.events)


def test_healing_full_validation_failure_never_stores_or_publishes():
    bundle = healing_bundle("draft")
    runtime = HealingRuntime(
        candidate_results=[{"status": "passed"}],
        deterministic_bundle=bundle,
        full_result={
            "status": "failed",
            "failure_class": "selector",
            "failed_aliases": ["comment_entry"],
            "profiles_passed": 1,
            "rounds_passed": 2,
        },
    )

    result = run_healing_probe(runtime)

    assert result["status"] == "selector_validation_failed"
    assert result["published"] is False
    assert result["proposed_pause_aliases"] == ["comment_entry"]
    assert "full:draft" in runtime.events
    assert runtime.events[-1] == "context:3"


def test_each_repair_runs_full_validation_and_third_success_publishes():
    runtime = HealingRuntime(
        candidate_results=[
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
            {"status": "passed"},
            {"status": "passed"},
            {"status": "passed"},
        ],
        deterministic_bundle=healing_bundle("deterministic"),
    )
    full_results = [
        {
            "status": "failed",
            "failure_class": "selector",
            "failed_aliases": ["comment_entry"],
            "code": "zero_match",
            "match_count": 0,
        },
        {
            "status": "failed",
            "failure_class": "selector",
            "failed_aliases": ["comment_entry"],
            "code": "wrong_semantics",
            "match_count": 1,
        },
        None,
    ]

    def full_validate(bundle):
        runtime.events.append(f"full:{bundle['version']}")
        result = full_results.pop(0)
        return healing_evidence(bundle) if result is None else result

    runtime.full_validate = full_validate
    repairs = []

    def repair_fn(*, attempt, failure, **_kwargs):
        repairs.append((attempt, failure.get("code")))
        return healing_bundle(f"repair-{attempt}")

    result = run_healing_probe(runtime, repair_fn=repair_fn)

    assert result["status"] == "published"
    assert repairs == [
        (1, None),
        (2, "zero_match"),
        (3, "wrong_semantics"),
    ]
    assert [
        event for event in runtime.events if event.startswith("full:")
    ] == [
        "full:repair-1",
        "full:repair-2",
        "full:repair-3",
    ]
    assert runtime.events[-1] == "publish:repair-3"


def test_healing_rejects_reused_fresh_context_without_consuming_attempt():
    repairs = []
    runtime = HealingRuntime(
        deterministic_bundle=healing_bundle("deterministic"),
        repeated_context=True,
        candidate_results=[
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
        ],
    )

    result = run_healing_probe(
        runtime,
        repair_fn=lambda *, attempt, **_kwargs: (
            repairs.append(attempt) or healing_bundle(f"repair-{attempt}")
        ),
    )

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "validation_context_not_fresh"
    assert repairs == [1]
    assert not any(event.startswith("publish:") for event in runtime.events)


def test_healing_requires_complete_runtime_contract():
    class IncompleteRuntime:
        def validate_active(self):
            return {"status": "passed"}

    result = run_healing_probe(IncompleteRuntime())

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "runtime_contract_invalid"


def test_healing_rejects_forged_full_validation_evidence():
    runtime = HealingRuntime(
        deterministic_bundle=healing_bundle("draft"),
        candidate_results=[{"status": "passed"}],
        full_result={
            "status": "passed",
            "bundle_hash": "sha256:" + "0" * 64,
            "profiles_passed": 2,
            "rounds_passed": 2,
            "validations": [],
        },
    )

    result = run_healing_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "full_validation_invalid"
    assert runtime.events[-1] == "full:draft"


def test_healing_continues_after_one_model_parse_failure():
    runtime = HealingRuntime(
        candidate_results=[
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            },
            {"status": "passed"},
        ],
        deterministic_bundle=healing_bundle("deterministic"),
    )
    attempts = []

    def repair_fn(*, attempt, prohibited_methods, **_kwargs):
        attempts.append((attempt, tuple(prohibited_methods)))
        if attempt == 1:
            raise ModelOutputFormatError(
                "repair output must be one exact JSON object"
            )
        return healing_bundle(f"repair-{attempt}")

    result = run_healing_probe(runtime, repair_fn=repair_fn)

    assert result["status"] == "published"
    assert [item[0] for item in attempts] == [1, 2]
    assert "repair_parse:attempt-1" in attempts[1][1]


def test_local_value_error_is_infrastructure_not_selector_terminal():
    runtime = HealingRuntime(
        candidate_results=[
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
            }
        ],
        deterministic_bundle=healing_bundle("deterministic"),
    )

    result = run_healing_probe(
        runtime,
        repair_fn=lambda **_kwargs: (_ for _ in ()).throw(
            ValueError("no enabled model")
        ),
    )

    assert result["status"] == "infrastructure_unavailable"
    assert result["proposed_pause_aliases"] == []


@pytest.mark.parametrize(
    "code",
    ("model_timeout", "model_network_error", "model_http_error"),
)
def test_confirmed_selector_failure_survives_three_model_transport_failures(
    code,
):
    runtime = HealingRuntime(
        candidate_results=[
            {
                "status": "failed",
                "failure_class": "selector",
                "failed_aliases": ["comment_entry"],
                "code": "zero_match",
                "match_count": 0,
            }
        ],
        deterministic_bundle=healing_bundle("deterministic"),
    )
    attempts = []

    def repair_fn(*, attempt, **_kwargs):
        attempts.append(attempt)
        raise ModelRequestError(code, 503 if code == "model_http_error" else None)

    result = run_healing_probe(runtime, repair_fn=repair_fn)

    assert attempts == [1, 2, 3]
    assert result["status"] == "selector_validation_failed"
    assert result["proposed_pause_aliases"] == ["comment_entry"]
    assert result["failure_code"] == "zero_match"
