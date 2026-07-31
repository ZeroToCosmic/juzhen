import asyncio
import threading

import pytest

from selector_probe.session import (
    ProbePageHandle,
    ProbeSessionManager,
    ProfileHandle,
)


class FakeAdsPower:
    def __init__(self):
        self.active = {
            "existing": {
                "status": "Active",
                "ws": {"puppeteer": "ws://existing"},
            }
        }
        self.started = []
        self.stopped = []
        self.active_calls = []

    def get_browser_active(self, profile_id):
        self.active_calls.append(profile_id)
        return self.active.get(profile_id, {"status": "Inactive"})

    def start_browser(self, profile_id):
        self.started.append(profile_id)
        return f"ws://{profile_id}"

    def stop_browser(self, profile_id):
        self.stopped.append(profile_id)
        return {"code": 0}


class FakePage:
    def __init__(self, name, *, close_error=None):
        self.name = name
        self.closed = False
        self.close_error = close_error

    async def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakeContext:
    def __init__(self, existing_pages=(), *, new_page_error=None):
        self.pages = list(existing_pages)
        self.new_page_error = new_page_error
        self.new_page_calls = 0

    async def new_page(self):
        self.new_page_calls += 1
        if self.new_page_error:
            raise self.new_page_error
        page = FakePage("probe")
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = list(contexts)


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.urls = []

    async def connect_over_cdp(self, ws_url):
        self.urls.append(ws_url)
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)


def make_manager(client=None, *, wait_for_cdp=lambda _url: True):
    return ProbeSessionManager(
        client or FakeAdsPower(),
        allowed_profile_ids=("existing", "profile-a", "profile-b", "profile-c"),
        wait_for_cdp=wait_for_cdp,
        sleep_fn=lambda _seconds: None,
    )


def test_stop_owned_profiles_never_stops_preexisting_browser():
    client = FakeAdsPower()
    manager = make_manager(client)

    handles = manager.open_profiles(("existing", "profile-a"))
    assert [item.started_by_probe for item in handles] == [False, True]

    results = manager.stop_owned_profiles(handles)

    assert client.stopped == ["profile-a"]
    assert results == [
        {
            "profile_mask": "***le-a",
            "stage": "stop_profile",
            "ok": True,
            "code": "",
        }
    ]


def test_open_profiles_reuses_case_insensitive_active_response_shape():
    client = FakeAdsPower()
    client.active["profile-a"] = {
        "status": "active",
        "ws": {"puppeteer": "ws://already-open"},
    }
    manager = make_manager(client)

    handles = manager.open_profiles(("existing", "profile-a"))

    assert [item.started_by_probe for item in handles] == [False, False]
    assert [item.ws_url for item in handles] == [
        "ws://existing",
        "ws://already-open",
    ]
    assert client.started == []


def test_active_profile_without_cdp_is_polled_not_started():
    class DelayedActive(FakeAdsPower):
        def __init__(self):
            super().__init__()
            self.reads = {}

        def get_browser_active(self, profile_id):
            self.active_calls.append(profile_id)
            count = self.reads.get(profile_id, 0) + 1
            self.reads[profile_id] = count
            if count < 3:
                return {"data": {"status": "Active", "ws": {}}}
            return {
                "data": {
                    "status": "Active",
                    "ws": {"puppeteer": f"ws://{profile_id}"},
                }
            }

    client = DelayedActive()
    events = []
    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("profile-a", "profile-b"),
        wait_for_cdp=lambda _url: True,
        progress_sink=events.append,
        sleep_fn=lambda _seconds: None,
    )

    handles = manager.open_profiles(("profile-a", "profile-b"))

    assert client.started == []
    assert len(handles) == 2
    assert any(
        item["name"] == "cdp_endpoint"
        and item["attempt_count"] == 3
        for item in events
    )


def test_unhealthy_preexisting_profile_is_never_stopped():
    class BrokenActive(FakeAdsPower):
        def get_browser_active(self, profile_id):
            return {"data": {"status": "Active", "ws": {}}}

    client = BrokenActive()
    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("profile-a", "profile-b"),
        wait_for_cdp=lambda _url: True,
        sleep_fn=lambda _seconds: None,
    )

    with pytest.raises(RuntimeError) as caught:
        manager.open_profiles(("profile-a", "profile-b"))

    assert caught.value.code == "preexisting_profile_unhealthy"
    assert client.started == []
    assert client.stopped == []


@pytest.mark.parametrize(
    "active_response",
    [
        None,
        [],
        {},
        {"status": "unknown"},
        {"status": 1},
    ],
)
def test_malformed_active_response_never_triggers_a_start(active_response):
    client = FakeAdsPower()
    client.active["profile-a"] = active_response
    manager = make_manager(client)

    with pytest.raises(RuntimeError, match="profile_open_failed"):
        manager.open_profiles(("profile-a", "profile-b"))

    assert client.started == []
    assert client.stopped == []


@pytest.mark.parametrize(
    "profile_ids",
    [
        ("profile-a",),
        ("profile-a", "profile-a"),
    ],
)
def test_open_profiles_requires_two_unique_profiles_before_network(profile_ids):
    client = FakeAdsPower()
    manager = make_manager(client)

    with pytest.raises(ValueError, match="at least two unique"):
        manager.open_profiles(profile_ids)

    assert client.active_calls == []
    assert client.started == []


def test_unlisted_profile_is_rejected_before_adspower_call():
    client = FakeAdsPower()
    manager = make_manager(client)

    with pytest.raises(ValueError, match="not allowlisted"):
        manager.open_profiles(("profile-a", "production-profile"))

    assert client.active_calls == []
    assert client.started == []


@pytest.mark.parametrize(
    "profile_ids",
    [
        "profile-a",
        None,
        ("profile-a", 2),
        ("profile-a", ""),
    ],
)
def test_open_profiles_rejects_invalid_input_before_network(profile_ids):
    client = FakeAdsPower()
    manager = make_manager(client)

    with pytest.raises((TypeError, ValueError)):
        manager.open_profiles(profile_ids)

    assert client.active_calls == []


def test_later_open_failure_stops_all_probe_owned_profiles_including_current():
    class FailingAdsPower(FakeAdsPower):
        def start_browser(self, profile_id):
            self.started.append(profile_id)
            if profile_id == "profile-b":
                raise RuntimeError(
                    "raw profile-b and ws://secret must not escape"
                )
            return f"ws://{profile_id}"

    client = FailingAdsPower()
    manager = make_manager(client)

    with pytest.raises(RuntimeError) as caught:
        manager.open_profiles(("profile-a", "profile-b"))

    assert "profile-b" not in str(caught.value)
    assert "ws://secret" not in str(caught.value)
    assert client.stopped == ["profile-b", "profile-a"]
    assert caught.value.code == "profile_open_failed"
    assert caught.value.cleanup_results == [
        {
            "profile_mask": "***le-b",
            "stage": "stop_profile",
            "ok": True,
            "code": "",
        },
        {
            "profile_mask": "***le-a",
            "stage": "stop_profile",
            "ok": True,
            "code": "",
        },
    ]


def test_cdp_wait_failure_stops_current_and_prior_probe_owned_profiles():
    client = FakeAdsPower()
    calls = []

    def wait_for_cdp(url):
        calls.append(url)
        return not url.endswith("profile-b")

    manager = make_manager(client, wait_for_cdp=wait_for_cdp)

    with pytest.raises(RuntimeError, match="cdp_unavailable"):
        manager.open_profiles(("profile-a", "profile-b"))

    assert client.stopped == ["profile-a", "profile-b"]


def test_stop_after_first_profile_blocks_second_api_and_cleans_first():
    client = FakeAdsPower()
    stop_event = threading.Event()

    def wait_for_cdp(url):
        if url.endswith("profile-a"):
            stop_event.set()
        return True

    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("profile-a", "profile-b"),
        wait_for_cdp=wait_for_cdp,
        stop_requested=stop_event.is_set,
    )

    with pytest.raises(asyncio.CancelledError):
        manager.open_profiles(("profile-a", "profile-b"))

    assert client.active_calls == ["profile-a"]
    assert client.started == ["profile-a"]
    assert client.stopped == ["profile-a"]


def test_invalid_started_cdp_url_stops_the_profile_just_started():
    class InvalidUrlAdsPower(FakeAdsPower):
        def start_browser(self, profile_id):
            self.started.append(profile_id)
            return "not-a-cdp-url"

    client = InvalidUrlAdsPower()
    manager = make_manager(client)

    with pytest.raises(RuntimeError, match="profile_open_failed"):
        manager.open_profiles(("profile-a", "profile-b"))

    assert client.stopped == ["profile-a"]


def test_invalid_cdp_preserves_main_error_and_all_sanitized_cleanup_results():
    class InvalidUrlAndStopFailingAdsPower(FakeAdsPower):
        def start_browser(self, profile_id):
            self.started.append(profile_id)
            if profile_id == "profile-b":
                return "not-a-cdp-url"
            return f"ws://{profile_id}"

        def stop_browser(self, profile_id):
            self.stopped.append(profile_id)
            if profile_id == "profile-b":
                raise RuntimeError("profile-b ws://profile-b")
            return {"code": 0, "ws": f"ws://{profile_id}"}

    client = InvalidUrlAndStopFailingAdsPower()
    manager = make_manager(client)

    with pytest.raises(RuntimeError) as caught:
        manager.open_profiles(("profile-a", "profile-b"))

    error = caught.value
    assert error.code == "profile_open_failed"
    assert client.stopped == ["profile-b", "profile-a"]
    assert error.cleanup_results == [
        {
            "profile_mask": "***le-b",
            "stage": "stop_profile",
            "ok": False,
            "code": "profile_stop_failed",
        },
        {
            "profile_mask": "***le-a",
            "stage": "stop_profile",
            "ok": True,
            "code": "",
        },
    ]
    assert "profile-a" not in repr(error.cleanup_results)
    assert "profile-b" not in repr(error.cleanup_results)
    assert "ws://" not in repr(error.cleanup_results)


def test_probe_uses_new_tab_and_closes_only_that_tab():
    async def scenario():
        existing_page = FakePage("existing")
        context = FakeContext(existing_pages=[existing_page])
        playwright = FakePlaywright(FakeBrowser([context]))
        manager = make_manager()
        profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            False,
        )

        owned = await manager.open_probe_page(playwright, profile)
        results = await manager.close_owned_pages((owned,))

        assert context.new_page_calls == 1
        assert owned.page.closed is True
        assert existing_page.closed is False
        assert results[0]["ok"] is True

    asyncio.run(scenario())


def test_open_probe_page_rejects_missing_browser_context():
    async def scenario():
        manager = make_manager()
        profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            False,
        )
        playwright = FakePlaywright(FakeBrowser([]))

        with pytest.raises(RuntimeError, match="browser_context_unavailable"):
            await manager.open_probe_page(playwright, profile)

    asyncio.run(scenario())


def test_open_probe_page_sanitizes_async_connect_failure():
    async def scenario():
        class FailingChromium:
            async def connect_over_cdp(self, _ws_url):
                raise RuntimeError("profile-a ws://profile-a")

        class FailingPlaywright:
            chromium = FailingChromium()

        manager = make_manager()
        profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            False,
        )

        with pytest.raises(RuntimeError) as caught:
            await manager.open_probe_page(FailingPlaywright(), profile)

        assert "cdp_connect_failed" in str(caught.value)
        assert "profile-a" not in str(caught.value)
        assert "ws://profile-a" not in str(caught.value)

    asyncio.run(scenario())


def test_open_probe_page_sanitizes_async_new_page_failure():
    async def scenario():
        context = FakeContext(
            new_page_error=RuntimeError("profile-a ws://profile-a")
        )
        playwright = FakePlaywright(FakeBrowser([context]))
        manager = make_manager()
        profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            False,
        )

        with pytest.raises(RuntimeError) as caught:
            await manager.open_probe_page(playwright, profile)

        assert "probe_page_open_failed" in str(caught.value)
        assert "profile-a" not in str(caught.value)
        assert "ws://profile-a" not in str(caught.value)

    asyncio.run(scenario())


def test_open_probe_page_rejects_none_from_new_page_with_safe_error():
    async def scenario():
        class NonePageContext:
            async def new_page(self):
                return None

        playwright = FakePlaywright(FakeBrowser([NonePageContext()]))
        manager = make_manager()
        profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            False,
        )

        with pytest.raises(RuntimeError, match="probe_page_open_failed"):
            await manager.open_probe_page(playwright, profile)

    asyncio.run(scenario())


def test_close_owned_pages_continues_after_async_close_failure_and_sanitizes():
    async def scenario():
        manager = make_manager()
        first_profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            True,
        )
        second_profile = ProfileHandle(
            "profile-b",
            "***le-b",
            "ws://profile-b",
            True,
        )
        first_page = FakePage(
            "first",
            close_error=RuntimeError("profile-a ws://profile-a"),
        )
        second_page = FakePage("second")

        results = await manager.close_owned_pages(
            (
                ProbePageHandle(first_profile, first_page),
                ProbePageHandle(second_profile, second_page),
            )
        )

        assert first_page.closed is True
        assert second_page.closed is True
        assert results == [
            {
                "profile_mask": "***le-a",
                "stage": "close_page",
                "ok": False,
                "code": "page_close_failed",
            },
            {
                "profile_mask": "***le-b",
                "stage": "close_page",
                "ok": True,
                "code": "",
            },
        ]
        assert "profile-a" not in repr(results)
        assert "ws://profile-a" not in repr(results)

    asyncio.run(scenario())


def test_close_owned_pages_delays_cancellation_until_all_pages_are_closed():
    async def scenario():
        manager = make_manager()
        first_profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            True,
        )
        second_profile = ProfileHandle(
            "profile-b",
            "***le-b",
            "ws://profile-b",
            True,
        )
        cancelled_page = FakePage(
            "cancelled",
            close_error=asyncio.CancelledError(),
        )
        remaining_page = FakePage("remaining")

        with pytest.raises(asyncio.CancelledError):
            await manager.close_owned_pages(
                (
                    ProbePageHandle(first_profile, cancelled_page),
                    ProbePageHandle(second_profile, remaining_page),
                )
            )

        assert cancelled_page.closed is True
        assert remaining_page.closed is True

    asyncio.run(scenario())


def test_close_owned_pages_does_not_catch_keyboard_interrupt():
    async def scenario():
        manager = make_manager()
        profile = ProfileHandle(
            "profile-a",
            "***le-a",
            "ws://profile-a",
            True,
        )
        interrupted_page = FakePage(
            "interrupted",
            close_error=KeyboardInterrupt(),
        )
        remaining_page = FakePage("remaining")

        with pytest.raises(KeyboardInterrupt):
            await manager.close_owned_pages(
                (
                    ProbePageHandle(profile, interrupted_page),
                    ProbePageHandle(profile, remaining_page),
                )
            )

        assert interrupted_page.closed is True
        assert remaining_page.closed is False

    asyncio.run(scenario())


def test_stop_owned_profiles_continues_after_failure_and_sanitizes():
    class StopFailingAdsPower(FakeAdsPower):
        def stop_browser(self, profile_id):
            self.stopped.append(profile_id)
            if profile_id == "profile-a":
                raise RuntimeError("profile-a ws://profile-a")
            return {"code": 0, "ws": f"ws://{profile_id}"}

    client = StopFailingAdsPower()
    manager = make_manager(client)
    handles = (
        ProfileHandle("profile-a", "***le-a", "ws://profile-a", True),
        ProfileHandle("existing", "***ting", "ws://existing", False),
        ProfileHandle("profile-b", "***le-b", "ws://profile-b", True),
    )

    results = manager.stop_owned_profiles(handles)

    assert client.stopped == ["profile-a", "profile-b"]
    assert results == [
        {
            "profile_mask": "***le-a",
            "stage": "stop_profile",
            "ok": False,
            "code": "profile_stop_failed",
        },
        {
            "profile_mask": "***le-b",
            "stage": "stop_profile",
            "ok": True,
            "code": "",
        },
    ]
    assert "profile-a" not in repr(results)
    assert "ws://profile-a" not in repr(results)


def test_stop_owned_profiles_does_not_catch_system_exit():
    class ExitingAdsPower(FakeAdsPower):
        def stop_browser(self, profile_id):
            self.stopped.append(profile_id)
            raise SystemExit(2)

    client = ExitingAdsPower()
    manager = make_manager(client)
    handles = (
        ProfileHandle("profile-a", "***le-a", "ws://profile-a", True),
        ProfileHandle("profile-b", "***le-b", "ws://profile-b", True),
    )

    with pytest.raises(SystemExit):
        manager.stop_owned_profiles(handles)

    assert client.stopped == ["profile-a"]


def test_cleanup_methods_reject_invalid_handle_collections():
    manager = make_manager()

    with pytest.raises(TypeError):
        manager.stop_owned_profiles("not-handles")

    async def scenario():
        with pytest.raises(TypeError):
            await manager.close_owned_pages((object(),))

    asyncio.run(scenario())
