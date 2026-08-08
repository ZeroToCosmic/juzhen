import asyncio
import pytest

from comment_campaign.errors import CampaignValidationError
from comment_campaign.profile_gateway import ProfileGateway


def test_partial_profile_start_closes_every_started_raw_profile():
    class Store:
        def get_raw_profile_id(self, ref):
            return {"one": "raw-one", "two": "raw-two"}[ref]

    class AdsPower:
        stopped = []
        async def start(self, raw_id):
            if raw_id == "raw-two":
                raise RuntimeError("start failed")
            return "ws://one"
        async def stop(self, raw_id):
            self.stopped.append(raw_id)
        async def is_active(self, raw_id):
            return False

    class Sessions:
        async def connect(self, raw_id, endpoint):
            return type("Binding", (), {"profile_id": raw_id, "ws_url": endpoint})()

    adapter = AdsPower()
    gateway = ProfileGateway(Store(), adapter, Sessions(), profile_discovery=lambda: [
        {"id": "raw-one", "name": "one", "status": "active"},
        {"id": "raw-two", "name": "two", "status": "active"},
    ])
    with pytest.raises(CampaignValidationError, match="profile_start_failed"):
        asyncio.run(gateway.open_many(["one", "two"]))
    assert adapter.stopped == ["raw-one", "raw-two"]


def test_unconfirmed_close_keeps_profile_lease_for_manual_cleanup():
    released = []

    class Lease:
        async def release(self): released.append(True)

    class AdsPower:
        async def stop(self, _): pass
        async def is_active(self, _): return True

    gateway = ProfileGateway(object(), AdsPower(), object(), lease_factory=lambda _: Lease())
    gateway._raw_to_ref["raw-one"] = "one"
    gateway._leases["profile:one"] = Lease()
    assert asyncio.run(gateway.close_many(["raw-one"])) == {"raw-one": False}
    assert released == []


def test_quarantined_profile_cannot_reopen_after_redis_lease_expires():
    class Store:
        def get_profile_metadata(self, _ref):
            return {"enabled": False, "health_status": "unhealthy"}
        def get_raw_profile_id(self, _ref):
            raise AssertionError("quarantined identities must stop before raw ID lookup")

    gateway = ProfileGateway(Store(), object(), object())
    with pytest.raises(CampaignValidationError, match="profile_start_failed"):
        gateway.resolve("one")


def test_batch_close_heartbeat_ignores_only_profiles_already_confirmed_closed():
    class Lease:
        refreshes = 0
        async def refresh(self):
            self.refreshes += 1
            return True
        async def release(self): return True

    class AdsPower:
        async def stop(self, _raw_id): return None
        async def is_active(self, raw_id):
            if raw_id == "raw-two":
                await asyncio.sleep(0.03)
            return False

    async def scenario():
        gateway = ProfileGateway(object(), AdsPower(), object(), lease_factory=lambda _key: Lease())
        first, second = Lease(), Lease()
        gateway._raw_to_ref.update({"raw-one": "one", "raw-two": "two"})
        gateway._leases.update({"profile:one": first, "profile:two": second})
        closing = asyncio.create_task(gateway.close_many(["raw-one", "raw-two"]))
        await asyncio.sleep(0.01)
        assert await gateway.refresh_leases(["one", "two"]) is True
        assert second.refreshes == 1
        assert await closing == {"raw-one": True, "raw-two": True}

    asyncio.run(scenario())


def test_open_heartbeat_loss_closes_started_profile_and_fails_closed():
    class Store:
        def get_raw_profile_id(self, _ref): return "raw-one"

    class Lease:
        async def acquire(self): return True
        async def refresh(self): return False
        async def release(self): return True

    class AdsPower:
        stopped = []
        async def start(self, raw_id):
            await asyncio.sleep(0.03)
            return "ws://one"
        async def stop(self, raw_id): self.stopped.append(raw_id)
        async def is_active(self, _raw_id): return False

    class Sessions:
        async def connect(self, raw_id, endpoint):
            return type("Binding", (), {"profile_id": raw_id, "ws_url": endpoint})()

    adapter = AdsPower()
    gateway = ProfileGateway(
        Store(), adapter, Sessions(), lease_factory=lambda _key: Lease(),
        lease_heartbeat_seconds=0.01,
    )
    with pytest.raises(CampaignValidationError, match="redis_unavailable"):
        asyncio.run(gateway.open_many(["one"], campaign_id="campaign"))
    assert adapter.stopped == ["raw-one"]


def test_partial_open_unconfirmed_close_quarantines_profile_and_pauses_campaign():
    class Store:
        metadata = {"profile_ref": "one", "expected_username": "a", "enabled": True, "login_verified": True, "tags": [], "language": "", "region": "", "cooldown_until": None, "health_status": "healthy"}
        campaign = {"id": "campaign", "revision": 1, "status": "running"}
        def get_raw_profile_id(self, ref): return {"one": "raw-one", "two": "raw-two"}[ref]
        def get_profile_metadata(self, _ref): return dict(self.metadata)
        def upsert_profile_metadata(self, **values): self.metadata = values
        def get_campaign(self, _id): return dict(self.campaign)
        def transition_campaign_status(self, _id, _revision, status, *, pause_reason):
            self.campaign.update(status=status, pause_reason=pause_reason)

    class Lease:
        async def acquire(self): return True
        async def refresh(self): return True
        async def release(self): return True

    class AdsPower:
        async def start(self, raw_id):
            if raw_id == "raw-two": raise RuntimeError("boom")
            return "ws://one"
        async def stop(self, _raw_id): return None
        async def is_active(self, _raw_id): return True

    class Sessions:
        async def connect(self, raw_id, endpoint):
            return type("Binding", (), {"profile_id": raw_id, "ws_url": endpoint})()

    store = Store()
    gateway = ProfileGateway(store, AdsPower(), Sessions(), lease_factory=lambda _key: Lease())
    with pytest.raises(CampaignValidationError, match="profile_start_failed"):
        asyncio.run(gateway.open_many(["one", "two"], campaign_id="campaign"))
    assert store.metadata["enabled"] is False
    assert store.metadata["health_status"] == "unhealthy"
    assert store.campaign["status"] == "paused"
    assert gateway._raw_to_ref == {"raw-one": "one", "raw-two": "two"}
