from __future__ import annotations

import asyncio
import json
import threading
import time

from flask import Flask
import pytest

from selector_probe.blueprint import _profile_ref, create_selector_probe_blueprint
from selector_probe.picker import (
    _BROWSE_SCRIPT,
    PickerError,
    PickerService,
    run_browser_picker,
)
import selector_probe.picker as picker_module


def candidate(**changes):
    value = {
        "target_key": "main-button",
        "tag": "button",
        "input_type": "button",
        "text": "Open comments",
        "role": "button",
        "name": "Comments",
        "attributes": {
            "data-e2e": "comment-icon",
            "aria-label": "Comments",
            "onclick": "unsafe()",
        },
        "frame_key": "main",
        "shadow": False,
        "shadow_key": "document",
        "region": {"x": 0.8, "y": 0.4, "width": 0.1, "height": 0.1},
        "locators": [
            {
                "type": "css",
                "value": '[data-e2e="comment-icon"]',
                "match_count": 1,
            },
            {
                "type": "xpath",
                "value": "//*[@data-e2e='comment-icon']",
                "match_count": 1,
            },
        ],
        "visible": True,
        "enabled": True,
        "hit_target": True,
        "target_match": True,
    }
    value.update(changes)
    return value


def test_browse_script_observes_clicks_without_blocking_page_actions():
    assert "preventDefault" not in _BROWSE_SCRIPT
    assert "stopPropagation" not in _BROWSE_SCRIPT
    assert "stopImmediatePropagation" not in _BROWSE_SCRIPT
    assert 'addEventListener("click", click, true)' in _BROWSE_SCRIPT
    assert "contentDocument" in _BROWSE_SCRIPT
    assert "shadowRoot?.mode" in _BROWSE_SCRIPT
    assert "rightUnique - leftUnique" in _BROWSE_SCRIPT
    assert ".slice(0, 6)" in _BROWSE_SCRIPT
    assert _BROWSE_SCRIPT.index("if (!isVisible") < _BROWSE_SCRIPT.index(
        "rawCount += 1"
    )


def test_navigation_recovery_rejects_cross_origin_page():
    class Page:
        url = "https://evil.example/"

        def is_closed(self):
            return False

        async def evaluate(self, _script, _argument=None):
            return True

    with pytest.raises(PickerError, match="picker_cross_origin_navigation"):
        asyncio.run(
            picker_module._restore_browse_script(
                Page(),
                browse_arguments={},
                expected_origin="https://www.tiktok.com",
                stop_event=threading.Event(),
            )
        )


def test_navigation_recovery_has_its_own_90_second_bounded_timeout(monkeypatch):
    class Page:
        url = "https://www.tiktok.com/"

        def is_closed(self):
            return False

        async def evaluate(self, script, _argument=None):
            if script == picker_module._GENERIC_READY_SCRIPT:
                return True
            raise RuntimeError("document changed")

    monkeypatch.setattr(picker_module, "PAGE_READY_TIMEOUT_SECONDS", 0.01)
    with pytest.raises(PickerError, match="picker_page_readiness_timeout"):
        asyncio.run(
            picker_module._restore_browse_script(
                Page(),
                browse_arguments={},
                expected_origin="https://www.tiktok.com",
                stop_event=threading.Event(),
            )
        )


class MemoryRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, **_options):
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)


class Lease:
    acquired = False
    released = False

    def __init__(self, *_args, **_kwargs):
        type(self).acquired = False
        type(self).released = False

    def acquire(self):
        type(self).acquired = True
        return True

    def renew(self):
        return True

    def release(self):
        type(self).released = True
        return True


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_picker_service_updates_inventory_records_actions_and_stays_open_until_confirm():
    runner_stopped = threading.Event()

    def runner(
        *, ready_sink, inventory_sink, action_sink, stop_event, **_kwargs
    ):
        ready_sink()
        inventory_sink([candidate()], False)
        action_sink(
            {
                "sequence": 99,
                "locator": {
                    "type": "css",
                    "value": '[data-e2e="comment-icon"]',
                },
                "url_before": "https://www.tiktok.com/?secret=1",
                "url_after": "https://www.tiktok.com/",
                "recorded_at": "2026-08-04T03:00:00+00:00",
                "frame_key": "main",
                "shadow": False,
                "shadow_key": "document",
            }
        )
        stop_event.wait(2)
        runner_stopped.set()

    redis = MemoryRedis()
    service = PickerService(
        redis,
        lease_key="selector:production:tiktok:lease",
        key_prefix="selector:production:tiktok",
        runner=runner,
        lease_factory=Lease,
        active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="raw-profile-id",
        profile_mask="***file-id",
        page_state="comment_panel_open",
        actor_user_id=7,
        context={"settings": {}},
    )
    session_id = started["session_id"]
    assert wait_until(
        lambda: service.get(session_id, actor_user_id=7)["inventory_revision"]
        == 1
    )
    current = service.get(session_id, actor_user_id=7)
    assert current["mode"] == "browse"
    assert current["status"] == "ready"
    assert len(current["inventory"]) == 1
    assert current["recorded_steps"][0]["sequence"] == 1
    assert current["recorded_steps"][0]["url_before"] == "https://www.tiktok.com/"

    finished = service.confirm(
        session_id,
        actor_user_id=7,
        expected_revision=current["revision"],
        selections=[
            {
                "selection_id": current["inventory"][0]["selection_id"],
                "display_name": "评论入口",
            }
        ],
    )

    assert finished["status"] == "confirmed"
    assert finished["selections"][0]["display_name"] == "评论入口"
    assert finished["cleanup"] == "passed"
    assert runner_stopped.is_set()
    assert Lease.acquired is True
    assert Lease.released is True
    assert "raw-profile-id" not in json.dumps(finished)
    assert "raw-profile-id" not in json.dumps(redis.values)


def test_picker_inventory_revision_changes_only_when_fingerprint_sequence_changes():
    allow_second_scan = threading.Event()

    def runner(*, ready_sink, inventory_sink, stop_event, **_kwargs):
        ready_sink()
        inventory_sink([candidate(name="first display")], False)
        inventory_sink([candidate(name="changed display only")], False)
        allow_second_scan.wait(2)
        inventory_sink(
            [
                candidate(),
                candidate(
                    target_key="post-button",
                    attributes={"data-e2e": "comment-post"},
                    locators=[
                        {
                            "type": "css",
                            "value": '[data-e2e="comment-post"]',
                            "match_count": 1,
                        }
                    ],
                    region={"x": 0.7, "y": 0.7, "width": 0.1, "height": 0.1},
                ),
            ],
            True,
        )
        stop_event.wait(2)

    service = PickerService(
        MemoryRedis(),
        lease_key="lease",
        key_prefix="picker",
        runner=runner,
        lease_factory=Lease,
        active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="profile",
        profile_mask="***file",
        page_state="feed_ready",
        actor_user_id=7,
        context={},
    )
    session_id = started["session_id"]
    assert wait_until(
        lambda: service.get(session_id, actor_user_id=7)["inventory_revision"]
        == 1
    )
    first = service.get(session_id, actor_user_id=7)
    first_selection_id = first["inventory"][0]["selection_id"]
    allow_second_scan.set()
    assert wait_until(
        lambda: service.get(session_id, actor_user_id=7)["inventory_revision"]
        == 2
    )
    second = service.get(session_id, actor_user_id=7)
    assert second["inventory"][0]["selection_id"] == first_selection_id
    assert second["truncated"] is True
    service.cancel(
        session_id,
        actor_user_id=7,
        expected_revision=second["revision"],
    )


def test_picker_marks_inventory_truncated_after_more_than_500_unique_targets():
    def runner(*, ready_sink, inventory_sink, stop_event, **_kwargs):
        ready_sink()
        inventory_sink(
            [candidate(target_key=f"target-{index}") for index in range(501)],
            False,
        )
        stop_event.wait(2)

    service = PickerService(
        MemoryRedis(), lease_key="lease", key_prefix="picker",
        runner=runner, lease_factory=Lease, active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="profile", profile_mask="***file", page_state="feed_ready",
        actor_user_id=7, context={},
    )
    session_id = started["session_id"]
    assert wait_until(
        lambda: service.get(session_id, actor_user_id=7)["inventory_revision"] == 1
    )
    current = service.get(session_id, actor_user_id=7)
    assert len(current["inventory"]) == 500
    assert current["truncated"] is True
    service.cancel(
        session_id, actor_user_id=7, expected_revision=current["revision"]
    )


def test_picker_confirm_requires_current_ids_and_unique_normalized_names():
    def runner(*, ready_sink, inventory_sink, stop_event, **_kwargs):
        ready_sink()
        inventory_sink(
            [
                candidate(),
                candidate(
                    target_key="post-button",
                    attributes={"data-e2e": "comment-post"},
                    locators=[
                        {
                            "type": "css",
                            "value": '[data-e2e="comment-post"]',
                            "match_count": 1,
                        }
                    ],
                    region={"x": 0.7, "y": 0.7, "width": 0.1, "height": 0.1},
                ),
            ],
            False,
        )
        stop_event.wait(2)

    service = PickerService(
        MemoryRedis(), lease_key="lease", key_prefix="picker",
        runner=runner, lease_factory=Lease, active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="profile", profile_mask="***file", page_state="feed_ready",
        actor_user_id=7, context={},
    )
    session_id = started["session_id"]
    assert wait_until(
        lambda: service.get(session_id, actor_user_id=7)["inventory_revision"] == 1
    )
    current = service.get(session_id, actor_user_id=7)
    ids = [item["selection_id"] for item in current["inventory"]]
    with pytest.raises(PickerError, match="duplicate_element_name"):
        service.confirm(
            session_id,
            actor_user_id=7,
            expected_revision=current["revision"],
            selections=[
                {"selection_id": ids[0], "display_name": "评论按钮"},
                {"selection_id": ids[1], "display_name": "  评论按钮  "},
            ],
        )
    with pytest.raises(PickerError, match="invalid_picker_selection"):
        service.confirm(
            session_id,
            actor_user_id=7,
            expected_revision=current["revision"],
            selections=[
                {"selection_id": "selection-not-current", "display_name": "入口"}
            ],
        )
    service.cancel(
        session_id,
        actor_user_id=7,
        expected_revision=current["revision"],
    )


def test_picker_service_rejects_wrong_owner_and_stale_revision():
    def runner(*, ready_sink, stop_event, **_kwargs):
        ready_sink()
        stop_event.wait(2)

    service = PickerService(
        MemoryRedis(),
        lease_key="lease",
        key_prefix="picker",
        runner=runner,
        lease_factory=Lease,
        active_ttl_seconds=30,
    )
    started = service.start(
        profile_id="profile",
        profile_mask="***file",
        page_state="feed_ready",
        actor_user_id=3,
        context={},
    )
    assert wait_until(
        lambda: service.get(started["session_id"], actor_user_id=3)["status"]
        == "ready"
    )
    with pytest.raises(PickerError, match="picker_not_found"):
        service.get(started["session_id"], actor_user_id=4)
    with pytest.raises(PickerError, match="stale_picker_revision"):
        service.cancel(
            started["session_id"], actor_user_id=3, expected_revision=1
        )
    current = service.get(started["session_id"], actor_user_id=3)
    service.cancel(
        started["session_id"],
        actor_user_id=3,
        expected_revision=current["revision"],
    )


def test_picker_terminal_command_from_another_service_stops_owner_runner():
    redis = MemoryRedis()
    stopped = threading.Event()

    def runner(*, ready_sink, stop_event, **_kwargs):
        ready_sink()
        stop_event.wait(3)
        stopped.set()

    owner = PickerService(
        redis,
        lease_key="lease",
        key_prefix="picker",
        runner=runner,
        lease_factory=Lease,
        active_ttl_seconds=30,
    )
    peer = PickerService(
        redis,
        lease_key="lease",
        key_prefix="picker",
        runner=runner,
        lease_factory=Lease,
        active_ttl_seconds=30,
    )
    started = owner.start(
        profile_id="profile",
        profile_mask="***file",
        page_state="feed_ready",
        actor_user_id=9,
        context={},
    )
    assert wait_until(
        lambda: peer.get(started["session_id"], actor_user_id=9)["status"]
        == "ready"
    )
    current = peer.get(started["session_id"], actor_user_id=9)
    peer.cancel(
        started["session_id"],
        actor_user_id=9,
        expected_revision=current["revision"],
    )

    assert wait_until(stopped.is_set)
    assert wait_until(
        lambda: peer.get(started["session_id"], actor_user_id=9)["cleanup"]
        == "passed"
    )


def test_cross_worker_terminal_compare_and_set_allows_only_one_cancel():
    redis = MemoryRedis()

    def runner(*, ready_sink, stop_event, **_kwargs):
        ready_sink()
        stop_event.wait(3)

    owner = PickerService(
        redis, lease_key="lease", key_prefix="picker", runner=runner,
        lease_factory=Lease, active_ttl_seconds=30,
    )
    peers = [
        PickerService(
            redis, lease_key="lease", key_prefix="picker", runner=runner,
            lease_factory=Lease, active_ttl_seconds=30,
        )
        for _ in range(2)
    ]
    started = owner.start(
        profile_id="profile", profile_mask="***file", page_state="feed_ready",
        actor_user_id=9, context={},
    )
    session_id = started["session_id"]
    assert wait_until(
        lambda: owner.get(session_id, actor_user_id=9)["status"] == "ready"
    )
    current = owner.get(session_id, actor_user_id=9)
    stored_active = owner.repository.load(session_id)
    same_revision = dict(stored_active)
    same_revision["last_scanned_at"] = "overwrite"
    same_revision["_storage_token"] = "replacement-token"
    assert owner.repository.compare_and_save(
        same_revision,
        30,
        expected_revision=stored_active["revision"],
        expected_token=stored_active["_storage_token"],
    ) is False
    jumped_revision = dict(stored_active)
    jumped_revision["revision"] = stored_active["revision"] + 2
    jumped_revision["_storage_token"] = "jump-token"
    assert owner.repository.compare_and_save(
        jumped_revision,
        30,
        expected_revision=stored_active["revision"],
        expected_token=stored_active["_storage_token"],
    ) is False
    barrier = threading.Barrier(2)
    for peer in peers:
        original = peer._owned

        def synchronized_owned(selected_id, actor, _original=original):
            value = _original(selected_id, actor)
            barrier.wait(2)
            return value

        peer._owned = synchronized_owned

    results = []

    def cancel(peer):
        try:
            results.append(
                peer.cancel(
                    session_id,
                    actor_user_id=9,
                    expected_revision=current["revision"],
                )["status"]
            )
        except PickerError as error:
            results.append(error.code)

    threads = [threading.Thread(target=cancel, args=(peer,)) for peer in peers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert results.count("cancelled") == 1
    assert len(results) == 2
    assert any(code in {"picker_not_active", "stale_picker_revision"} for code in results)
    persisted = owner.repository.load(session_id)
    assert persisted["status"] == "cancelled"
    tampered_terminal = dict(persisted)
    tampered_terminal["revision"] = persisted["revision"] + 1
    tampered_terminal["_storage_token"] = "tampered-token"
    tampered_terminal["inventory"] = [candidate(target_key="tampered")]
    assert owner.repository.compare_and_save(
        tampered_terminal,
        30,
        expected_revision=persisted["revision"],
        expected_token=persisted["_storage_token"],
        terminal_cleanup=True,
    ) is False
    owner._update_inventory(session_id, [candidate(target_key="late")], False)
    assert owner.repository.load(session_id)["status"] == "cancelled"


class FakePickerService:
    def __init__(self):
        self.calls = []

    def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"session_id": "picker-1", "status": "starting", "revision": 1}

    def get(self, session_id, **kwargs):
        self.calls.append(("get", session_id, kwargs))
        return {"session_id": session_id, "status": "ready", "revision": 2}

    def confirm(self, session_id, **kwargs):
        self.calls.append(("confirm", session_id, kwargs))
        return {"session_id": session_id, "status": "confirmed", "revision": 3}

    def cancel(self, session_id, **kwargs):
        self.calls.append(("cancel", session_id, kwargs))
        return {"session_id": session_id, "status": "cancelled", "revision": 3}


def test_picker_routes_resolve_profile_ref_and_delegate_without_exposing_id():
    fake = FakePickerService()
    settings = {
        "selector_probe": {
            "test_profile_ids": ["profile-secret"],
            "dedicated_test_profile_ids": ["profile-secret"],
        }
    }
    app = Flask(__name__)
    app.secret_key = "test-secret"
    app.register_blueprint(
        create_selector_probe_blueprint(
            picker_service_factory=lambda: fake,
            settings_provider=lambda: settings,
        )
    )
    with app.app_context():
        ref = _profile_ref("profile-secret")
    client = app.test_client()

    started = client.post(
        "/api/selector-probe/picker/start",
        json={"profile_ref": ref, "page_state": "feed_ready"},
    )
    status = client.get("/api/selector-probe/picker/picker-1")
    confirmed = client.post(
        "/api/selector-probe/picker/picker-1/confirm",
        json={
            "expected_revision": 2,
            "selections": [
                {"selection_id": "selection-1", "display_name": "评论入口"}
            ],
        },
    )
    cancelled = client.post(
        "/api/selector-probe/picker/picker-1/cancel",
        json={"expected_revision": 2},
    )

    assert started.status_code == 202
    assert status.status_code == 200
    assert confirmed.status_code == 200
    assert cancelled.status_code == 200
    assert fake.calls[0][1]["profile_id"] == "profile-secret"
    assert "profile-secret" not in started.get_data(as_text=True)


@pytest.mark.parametrize("preexisting", [False, True])
def test_browser_runner_closes_owned_page_and_only_stops_started_profile(
    monkeypatch, preexisting
):
    calls = []
    stop = threading.Event()
    inventories = []

    class Controller:
        def get_browser_active(self, profile_id):
            calls.append(("active", profile_id))
            if preexisting:
                return {"status": "active", "ws": {"puppeteer": "ws://existing"}}
            return {"status": "inactive"}

        def start_browser(self, profile_id):
            calls.append(("start", profile_id))
            return "ws://started"

        def stop_browser(self, profile_id):
            calls.append(("stop", profile_id))

    class Page:
        closed = False
        scan_calls = 0
        url = "about:blank"

        async def goto(self, url, **options):
            self.url = url
            calls.append(("goto", url, options))

        async def expose_binding(self, name, _callback):
            calls.append(("binding", name.startswith("__selectorPicker_")))

        async def evaluate(self, script, _argument=None):
            if script == picker_module._GENERIC_READY_SCRIPT:
                return True
            if script == "window.__selectorProbeBrowse?.scan?.()":
                self.scan_calls += 1
                if self.scan_calls == 1:
                    raise RuntimeError("Execution context was destroyed")
                return {"items": [candidate()], "truncated": False}
            calls.append(("evaluate", script == _BROWSE_SCRIPT, "teardown" in script))

        def is_closed(self):
            return self.closed

        async def close(self):
            self.closed = True
            calls.append(("page_close", True))

    page = Page()

    class Context:
        async def new_page(self):
            return page

    class Chromium:
        async def connect_over_cdp(self, ws_url):
            calls.append(("connect", ws_url))
            return type("Browser", (), {"contexts": [Context()]})()

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            calls.append(("playwright_stop", True))

    class Starter:
        async def start(self):
            return Playwright()

    controller = Controller()
    monkeypatch.setattr(picker_module, "AdsPowerController", lambda **_kwargs: controller)
    monkeypatch.setattr(picker_module, "wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: Starter()
    )

    run_browser_picker(
        profile_id="profile-1",
        page_state="comment_panel_open",
        context={
            "settings": {
                "selector_probe": {"target_url": "https://www.tiktok.com/"},
                "adspower": {},
            }
        },
        ready_sink=lambda: calls.append(("ready", True)),
        inventory_sink=lambda items, truncated: (
            inventories.append((items, truncated)), stop.set()
        ),
        action_sink=lambda _value: None,
        stop_event=stop,
    )

    assert inventories == [([candidate()], False)]
    assert sum(call[:2] == ("evaluate", True) for call in calls) == 2
    assert calls.count(("ready", True)) == 1
    assert any(call[:2] == ("goto", "https://www.tiktok.com/") for call in calls)
    assert not any(call[0] == "state" for call in calls)
    assert ("page_close", True) in calls
    assert ("playwright_stop", True) in calls
    assert (("stop", "profile-1") in calls) is (not preexisting)


def test_browser_runner_stops_profile_after_partial_start_failure(monkeypatch):
    calls = []

    class Controller:
        def get_browser_active(self, _profile_id):
            return {"status": "inactive"}

        def start_browser(self, profile_id):
            calls.append(("start", profile_id))
            raise RuntimeError("endpoint missing")

        def stop_browser(self, profile_id):
            calls.append(("stop", profile_id))

    monkeypatch.setattr(
        picker_module, "AdsPowerController", lambda **_kwargs: Controller()
    )

    with pytest.raises(PickerError, match="picker_profile_open_failed"):
        run_browser_picker(
            profile_id="profile-1",
            page_state="feed_ready",
            context={
                "settings": {
                    "selector_probe": {
                        "target_url": "https://www.tiktok.com/"
                    },
                    "adspower": {},
                }
            },
            ready_sink=lambda: None,
            inventory_sink=lambda _items, _truncated: None,
            action_sink=lambda _value: None,
            stop_event=threading.Event(),
        )

    assert calls == [("start", "profile-1"), ("stop", "profile-1")]
