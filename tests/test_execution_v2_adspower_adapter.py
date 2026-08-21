import asyncio

import pytest

from execution_v2.adspower_adapter import AdsPowerStateError, RateLimitedAdsPowerAdapter


def test_adspower_calls_are_serialized_one_second_apart():
    events = []
    now = [0.0]

    class Controller:
        def start_browser(self, profile_id):
            events.append(("start", profile_id, now[0]))
            return f"ws://{profile_id}"

        def stop_browser(self, profile_id):
            events.append(("stop", profile_id, now[0]))
            return {"status": "stopped", "ws": "secret"}

        def get_browser_active(self, profile_id):
            events.append(("active", profile_id, now[0]))
            return {"status": "Inactive", "ws": "secret"}

    async def sleep(seconds):
        now[0] += seconds

    adapter = RateLimitedAdsPowerAdapter(
        Controller(), clock=lambda: now[0], sleep=sleep
    )

    assert asyncio.run(adapter.start("p1")) == "ws://p1"
    assert asyncio.run(adapter.stop("p1")) is None
    assert asyncio.run(adapter.is_active("p1")) is False
    assert events == [
        ("start", "p1", 0.0),
        ("stop", "p1", 1.0),
        ("active", "p1", 2.0),
    ]


def test_is_active_accepts_only_documented_active_status():
    class Controller:
        def get_browser_active(self, _profile_id):
            return {"status": "Active", "ws": "ws://secret"}

    adapter = RateLimitedAdsPowerAdapter(Controller())

    assert asyncio.run(adapter.is_active("p1")) is True


def test_is_active_rejects_unrecognized_payloads():
    class Controller:
        def get_browser_active(self, _profile_id):
            return {"running": True, "ws": "ws://secret"}

    adapter = RateLimitedAdsPowerAdapter(Controller())

    with pytest.raises(AdsPowerStateError, match="no status"):
        asyncio.run(adapter.is_active("p1"))
