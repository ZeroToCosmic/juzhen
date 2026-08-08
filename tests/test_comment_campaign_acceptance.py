"""Large local simulation: production gateway lifecycle, no external calls."""

from __future__ import annotations

import asyncio

from comment_campaign.executor import CommentExecutor
from comment_campaign.profile_gateway import ProfileGateway


def test_300_profiles_run_in_batches_of_three_and_close_before_next():
    events: list[tuple[str, str]] = []

    class Store:
        def get_raw_profile_id(self, profile_ref): return f"raw-{profile_ref}"

    class AdsPower:
        active: set[str] = set()
        async def start(self, raw_id):
            assert len(self.active) < 3, "batch exceeded its three-window limit"
            self.active.add(raw_id); events.append(("start", raw_id)); return "fake-endpoint"
        async def stop(self, raw_id):
            events.append(("stop", raw_id)); self.active.discard(raw_id)
        async def is_active(self, raw_id): return raw_id in self.active

    class Sessions:
        async def connect(self, raw_id, _endpoint):
            return type("Binding", (), {"profile_id": raw_id})()

    async def run():
        adapter = AdsPower()
        gateway = ProfileGateway(Store(), adapter, Sessions())
        profiles = [f"profile-{index:03d}" for index in range(300)]
        batches = []
        for offset in range(0, len(profiles), 3):
            bindings = await gateway.open_many(profiles[offset:offset + 3])
            assert await gateway.close_bindings(bindings) == {binding.profile_id: True for binding in bindings}
            assert not adapter.active
            batches.append(tuple(binding.profile_id for binding in bindings))
        return batches

    batches = asyncio.run(run())
    assert len(batches) == 100
    assert all(len(batch) == 3 for batch in batches)
    assert len({profile for batch in batches for profile in batch}) == 300
    for boundary in range(6, len(events), 6):
        assert [kind for kind, _profile in events[boundary - 3:boundary]] == ["stop", "stop", "stop"]
        assert events[boundary][0] == "start"


def test_executor_runs_300_fake_assignments_in_100_closed_batches_without_submit():
    events: list[str] = []

    class Store:
        generation = 0
        campaign = {"id": "campaign", "status": "running", "revision": 1, "batch_size": 3}
        assignments = {
            f"a-{index:03d}": {"assignment_id": f"a-{index:03d}", "campaign_id": "campaign",
                              "profile_ref": f"p-{index:03d}", "position": index,
                              "parent_assignment_id": None}
            for index in range(300)
        }
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, assignment_id): return dict(self.assignments[assignment_id])
        def get_raw_profile_id(self, profile_ref): return "raw-" + profile_ref
        def next_prepare_generation(self, _id): self.generation += 1; return self.generation
        def mark_reconcile_prepare_generation(self, _id, _generation): return True

    class AdsPower:
        active: set[str] = set()
        async def start(self, raw_id):
            assert len(self.active) < 3
            self.active.add(raw_id); events.append("start:" + raw_id); return "fake"
        async def stop(self, raw_id): self.active.discard(raw_id); events.append("stop:" + raw_id)
        async def is_active(self, raw_id): return raw_id in self.active

    class Sessions:
        async def connect(self, raw_id, _endpoint): return type("Binding", (), {"profile_id": raw_id, "page": object()})()

    class Queue:
        calls = []
        def enqueue_prepare_generation(self, campaign_id, generation):
            self.calls.append((campaign_id, generation)); return {"id": f"g{generation}"}
        def enqueue_submit(self, *_args): raise AssertionError("simulation must not enqueue submit")

    class Executor(CommentExecutor):
        async def _prepare_one(self, _campaign, _assignment, _binding): return None

    async def run():
        store, adapter, queue = Store(), AdsPower(), Queue()
        gateway = ProfileGateway(store, adapter, Sessions())
        executor = Executor(store, gateway, locator_resolver=None, queue_coordinator=queue)
        for offset in range(0, 300, 3):
            ids = [f"a-{index:03d}" for index in range(offset, offset + 3)]
            result = await executor.prepare_batch("campaign", ids)
            assert result.close_confirmed is True
            assert not adapter.active
        return queue.calls

    generations = asyncio.run(run())
    assert generations == [("campaign", index) for index in range(1, 101)]
    for boundary in range(6, len(events), 6):
        assert all(item.startswith("stop:") for item in events[boundary - 3:boundary])


def test_executor_close_failure_pauses_and_never_enqueues_next_batch():
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "batch_size": 3}
        assignment = {"assignment_id": "a", "campaign_id": "campaign", "profile_ref": "p", "position": 1, "parent_assignment_id": None}
        metadata = {"profile_ref": "p", "expected_username": "u", "enabled": True, "login_verified": True, "tags": [], "language": "", "region": "", "cooldown_until": None, "health_status": "healthy"}
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, _id): return dict(self.assignment)
        def get_raw_profile_id(self, _ref): return "raw-p"
        def get_profile_metadata(self, _ref): return dict(self.metadata)
        def upsert_profile_metadata(self, **values): self.metadata = values
        def transition_campaign_status(self, _id, _revision, status, *, pause_reason): self.campaign.update(status=status, pause_reason=pause_reason)
        def next_prepare_generation(self, _id): raise AssertionError("close failure must stop next batch")

    class AdsPower:
        async def start(self, _raw_id): return "fake"
        async def stop(self, _raw_id): return None
        async def is_active(self, _raw_id): return True

    class Sessions:
        async def connect(self, raw_id, _endpoint): return type("Binding", (), {"profile_id": raw_id, "page": object()})()

    class Queue:
        def enqueue_prepare_generation(self, *_args): raise AssertionError("close failure must not enqueue")

    class Executor(CommentExecutor):
        async def _prepare_one(self, _campaign, _assignment, _binding): return None

    async def run():
        store = Store()
        executor = Executor(store, ProfileGateway(store, AdsPower(), Sessions()), None, queue_coordinator=Queue())
        result = await executor.prepare_batch("campaign", ["a"])
        return store, result

    store, result = asyncio.run(run())
    assert result.close_confirmed is False
    assert store.campaign["status"] == "paused"
    assert store.metadata["enabled"] is False
