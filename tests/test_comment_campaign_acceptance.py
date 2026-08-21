"""Large local simulation: production gateway lifecycle, no external calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from comment_campaign.executor import CommentExecutor
from comment_campaign.profile_gateway import ProfileGateway
from comment_campaign.service import CommentCampaignService
from comment_campaign.store import CampaignStore


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
        campaign = {
            "id": "campaign", "status": "running", "revision": 1,
            "batch_size": 3, "identity_generation": 0,
        }
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
        def enqueue_prepare_generation(self, campaign_id, generation, identity_generation):
            self.calls.append((campaign_id, generation, identity_generation)); return {"id": f"g{generation}"}
        def enqueue_submit(self, *_args): raise AssertionError("simulation must not enqueue submit")

    class Executor(CommentExecutor):
        async def _verify_prepare_batch_identities(self, *_args): return None
        async def _prepare_one(self, _campaign, _assignment, _binding): return None

    async def run():
        store, adapter, queue = Store(), AdsPower(), Queue()
        gateway = ProfileGateway(store, adapter, Sessions())
        executor = Executor(store, gateway, locator_resolver=None, queue_coordinator=queue)
        for offset in range(0, 300, 3):
            ids = [f"a-{index:03d}" for index in range(offset, offset + 3)]
            result = await executor.prepare_batch("campaign", ids, 0)
            assert result.close_confirmed is True
            assert not adapter.active
        return queue.calls

    generations = asyncio.run(run())
    assert generations == [("campaign", index, 0) for index in range(1, 101)]
    for boundary in range(6, len(events), 6):
        assert all(item.startswith("stop:") for item in events[boundary - 3:boundary])


def test_executor_close_failure_pauses_and_never_enqueues_next_batch():
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "batch_size": 3, "identity_generation": 0}
        assignment = {"assignment_id": "a", "campaign_id": "campaign", "profile_ref": "p", "position": 1, "parent_assignment_id": None}
        metadata = {"profile_ref": "p", "expected_username": "u", "enabled": True, "login_verified": True, "tags": [], "language": "", "region": "", "cooldown_until": None, "health_status": "healthy"}
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, _id): return dict(self.assignment)
        def get_raw_profile_id(self, _ref): return "raw-p"
        def get_profile_metadata(self, _ref): return dict(self.metadata)
        def upsert_profile_metadata(self, **values): self.metadata = values
        def transition_campaign_status(self, _id, _revision, status, *, pause_reason): self.campaign.update(status=status, pause_reason=pause_reason)
        def invalidate_campaign_identity(self, _id, _revision, generation, *, error_code, **_kwargs):
            self.campaign.update(status="paused", pause_reason=error_code, identity_generation=generation + 1)
            return dict(self.campaign)
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
        async def _verify_prepare_batch_identities(self, *_args): return None
        async def _prepare_one(self, _campaign, _assignment, _binding): return None

    async def run():
        store = Store()
        executor = Executor(store, ProfileGateway(store, AdsPower(), Sessions()), None, queue_coordinator=Queue())
        result = await executor.prepare_batch("campaign", ["a"], 0)
        return store, result

    store, result = asyncio.run(run())
    assert result.close_confirmed is False
    assert store.campaign["status"] == "paused"
    assert store.metadata["enabled"] is False


def test_locked_prepare_and_submit_never_consult_current_template_or_network():
    events = []

    class Store:
        campaign = {
            "id": "campaign", "status": "queued", "revision": 7,
            "batch_size": 3, "locked_at": "2026-08-10T00:00:00+00:00",
            "prepare_generation": 1, "identity_generation": 0,
            "template_id": "deleted-template", "template_revision": 1,
            "template_snapshot": {"steps": [{"id": "root"}]},
        }

        def get_campaign(self, _campaign_id):
            return dict(self.campaign)

        def transition_campaign_status(self, _campaign_id, _revision, status):
            self.campaign = {**self.campaign, "status": status, "revision": 8}
            return dict(self.campaign)

        def eligible_assignment_ids(self, _campaign_id):
            return ["assignment"]

        def get_template_lifecycle(self, _template_id):
            raise AssertionError("locked execution consulted current template")

        def close(self):
            return None

    class Executor:
        async def prepare_batch(self, campaign_id, assignment_ids, identity_generation):
            assert identity_generation == 0
            events.append(("prepare", campaign_id, tuple(assignment_ids)))
            return SimpleNamespace(
                prepared=tuple(assignment_ids), failed=(), close_confirmed=True
            )

        async def submit_assignment(self, campaign_id, assignment_id, revision):
            events.append(("submit", campaign_id, assignment_id, revision))
            return {"status": "fake", "assignment_id": assignment_id}

    service = CommentCampaignService(Store(), executor=Executor())

    prepared = service.prepare_campaign("campaign", 1, 0)
    submitted = service.submit_assignment("campaign", "assignment", 3)

    assert prepared["prepared"] == ("assignment",)
    assert submitted == {"status": "fake", "assignment_id": "assignment"}
    assert events == [
        ("prepare", "campaign", ("assignment",)),
        ("submit", "campaign", "assignment", 3),
    ]


def test_same_runtime_window_keeps_campaign_identity_generations_isolated(tmp_path):
    """A/B reuse one opaque AdsPower mapping without mutating A's evidence."""
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    service = CommentCampaignService(store)
    template = service.create_template({
        "name": "one step", "description": "", "supported_modes": ["independent"],
        "language": "", "tags": [], "steps": [{
            "id": "root", "label": "root", "content_source": "fixed",
            "fixed_text": "hello", "content_library_id": "", "content_item_id": "",
            "parent_step_id": None, "required_profile_tags": [],
            "excluded_profile_tags": [], "language": "",
        }],
    }, "template")
    profile_ref = store.sync_profile_identities([
        {"id": "fixed-runtime-window", "name": "Shared window", "status": "active"},
    ])[0]["profile_ref"]
    store.upsert_profile_metadata(
        profile_ref=profile_ref, expected_username="historical-name", enabled=True,
        login_verified=True, tags=[], language="", region="", cooldown_until=None,
        health_status="healthy",
    )

    def running_campaign(campaign_id):
        campaign = service.create_campaign({
            "name": campaign_id, "mode": "independent", "target_source": "manual_url",
            "target_reference": "https://www.tiktok.com/@owner/video/12345678",
            "template_id": template["id"], "template_revision": template["revision"],
            "profile_refs": [profile_ref],
        }, campaign_id)
        planned = service.plan_campaign(campaign_id, seed=campaign_id)["campaign"]
        locked = store.lock_campaign_plan(campaign_id, planned["revision"])
        queued = store.approve_campaign_for_queue(campaign_id, locked["revision"])
        return store.transition_campaign_status(campaign_id, queued["revision"], "running")

    def observation(assignment_id, handle):
        return ({
            "assignment_id": assignment_id, "profile_ref": profile_ref,
            "account_key": handle.casefold(), "visible_username": handle,
            "canonical_href": f"https://www.tiktok.com/@{handle}",
            "observed_at": "2026-08-11T00:00:00Z",
            "target_video": {"video_id": "12345678", "canonical_url": "https://www.tiktok.com/@owner/video/12345678"},
            "element_binding": {"id": "account", "revision": 1, "definition_sha256": "a" * 64},
        },)

    campaign_a = running_campaign("campaign-a")
    assignment_a = store.list_assignments("campaign-a")[0]
    frozen_a = store.freeze_campaign_identities(
        "campaign-a", campaign_a["revision"], 0, observation(assignment_a["assignment_id"], "first.handle")
    )
    evidence_a = store.get_assignment(assignment_a["assignment_id"])["evidence"]
    store.save_receipt(assignment_a["assignment_id"], {
        "receipt_id": "receipt-a", "status": "published_verified",
        "expected_username": "first.handle", "marker": "campaign-a",
    })
    receipts_a = repr(store.list_receipts("campaign-a"))

    campaign_b = running_campaign("campaign-b")
    assignment_b = store.list_assignments("campaign-b")[0]
    frozen_b = store.freeze_campaign_identities(
        "campaign-b", campaign_b["revision"], 0, observation(assignment_b["assignment_id"], "second.handle")
    )

    assert store.get_raw_profile_id(profile_ref) == "fixed-runtime-window"
    assert frozen_a["identity_generation"] == frozen_b["identity_generation"] == 1
    assert store.get_campaign("campaign-a")["identity_generation"] == frozen_a["identity_generation"] == 1
    assert store.get_profile_metadata(profile_ref)["expected_username"] == "historical-name"
    assert store.get_assignment(assignment_a["assignment_id"])["evidence"] == evidence_a
    assert repr(store.list_receipts("campaign-a")) == receipts_a
    assert evidence_a["account_preflight"]["visible_username"] == "first.handle"
    assert store.get_assignment(assignment_b["assignment_id"])["evidence"]["account_preflight"]["visible_username"] == "second.handle"
