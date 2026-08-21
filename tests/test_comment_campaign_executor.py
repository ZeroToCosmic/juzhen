import asyncio
import pytest

from comment_campaign.errors import CampaignValidationError
from comment_campaign.errors import DuplicateTikTokAccountError, RevisionConflictError
from comment_campaign.executor import BatchResult, CommentExecutor
from comment_campaign.identity import AccountObservation
from comment_campaign.profile_gateway import IdentityPreflightStale


def test_submit_never_clicks_without_current_unconsumed_approval():
    class Store:
        def get_campaign(self, _): return {"id": "campaign", "video_id": "12345678", "status": "running"}
        def get_assignment(self, _): return {"assignment_id": "a1", "campaign_id": "campaign", "revision": 2, "status": "awaiting_step_approval"}
        def get_approval(self, *_): return None

    executor = CommentExecutor(Store(), gateway=None, locator_resolver=None)
    with pytest.raises(CampaignValidationError, match="approval_revision_mismatch"):
        asyncio.run(executor.submit_assignment("campaign", "a1", 2))


def test_preflight_scans_six_windows_in_closed_batches_and_duplicate_invalidates_once(monkeypatch):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 3, "video_id": "123"}
        assignments = [{"assignment_id": f"a{n}", "profile_ref": f"p{n}", "status": "planned"} for n in range(6)]
        def __init__(self): self.freeze_calls = 0; self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [dict(row) for row in self.assignments]
        def get_approval(self, *_args): raise AssertionError("preflight must not read approvals")
        def eligible_assignment_ids(self, *_args): raise AssertionError("preflight must not prepare work")
        def freeze_campaign_identities(self, *_args):
            self.freeze_calls += 1
            raise DuplicateTikTokAccountError("same", "Same", ("a0", "a3"))
        def invalidate_campaign_identity(self, _id, _revision, _generation, *, error_code, affected_assignment_ids, failure_details=None):
            self.invalidated = (error_code, tuple(affected_assignment_ids), failure_details)
            self.campaign.update(status="paused", identity_generation=1, revision=2)
            return dict(self.campaign)

    class Gateway:
        def __init__(self): self.opened = []; self.closed = []; self.active = 0; self.maximum = 0
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, _refs, **_kw): return True
        async def open_identity_batch(self, refs, *_args):
            self.opened.append(tuple(refs)); self.active += len(refs); self.maximum = max(self.maximum, self.active)
            return [type("Binding", (), {"page": ref, "profile_id": ref})() for ref in refs]
        async def close_bindings(self, bindings):
            self.closed.append(tuple(binding.profile_id for binding in bindings)); self.active -= len(bindings)
            return {binding.profile_id: True for binding in bindings}
        async def click(self, *_args): raise AssertionError("preflight must not click")
        async def input(self, *_args): raise AssertionError("preflight must not input")
        async def approve(self, *_args): raise AssertionError("preflight must not approve")

    async def video(_page, _video): return {"video_id": "123"}
    async def identity(page, *_args, **_kwargs):
        key = "same" if page in {"p0", "p3"} else page
        return AccountObservation(key, key, f"https://www.tiktok.com/@{key}", "2026-08-11T00:00:00Z")
    monkeypatch.setattr(module, "verify_video", video)
    monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store, gateway = Store(), Gateway()
    executor = CommentExecutor(store, gateway, None, element_provider=lambda _id: element,
                               settings_provider=lambda _campaign: {"account_element_id": "account"})

    result = asyncio.run(executor.preflight_campaign_identities("campaign", 0))

    assert result.ready is False and result.stale is False
    assert gateway.opened == [("p0", "p1", "p2"), ("p3", "p4", "p5")]
    assert gateway.closed == gateway.opened and gateway.maximum == 3
    assert store.freeze_calls == 1
    assert store.invalidated == ("duplicate_tiktok_account", ("a0", "a3"), {"visible_username": "Same"})


def test_preflight_freeze_cas_loser_is_stale_without_retry(monkeypatch):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 1, "batch_size": 1, "video_id": "123"}
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def freeze_campaign_identities(self, *_args): self.campaign.update(revision=2, identity_generation=2); raise RevisionConflictError("campaign")
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, _refs, **_kw): return True
        async def open_identity_batch(self, refs, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): return {binding.profile_id: True for binding in bindings}
    async def video(*_args): return {"video_id": "123"}
    async def identity(*_args, **_kwargs): return AccountObservation("p", "p", None, "2026-08-11T00:00:00Z")
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    result = asyncio.run(CommentExecutor(Store(), Gateway(), None, element_provider=lambda _id: element,
                                         settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 1))
    assert result.stale is True and result.identity_generation == 2


def test_preflight_unique_six_windows_freezes_once_and_never_opens_extra_candidates(monkeypatch):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0,
                    "batch_size": 3, "video_id": "123", "profile_refs": [f"candidate-{n}" for n in range(9)]}
        assignments = [{"assignment_id": f"a{n}", "profile_ref": f"p{n}", "status": "planned"} for n in range(6)]
        def __init__(self): self.frozen = []
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [dict(row) for row in self.assignments]
        def freeze_campaign_identities(self, _id, _revision, _generation, observations):
            self.frozen.append(observations); self.campaign.update(identity_generation=1, revision=2); return dict(self.campaign)
    class Gateway:
        def __init__(self): self.opened = []; self.closed = []
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, _refs, **_kw): return True
        async def open_identity_batch(self, refs, *_args):
            self.opened.append(tuple(refs)); return [type("B", (), {"page": ref, "profile_id": ref})() for ref in refs]
        async def close_bindings(self, bindings):
            self.closed.append(tuple(item.profile_id for item in bindings)); return {item.profile_id: True for item in bindings}
    async def video(*_args): return {"video_id": "123"}
    async def identity(page, *_args, **_kwargs): return AccountObservation(page, page, None, "2026-08-11T00:00:00Z")
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store, gateway = Store(), Gateway()
    result = asyncio.run(CommentExecutor(store, gateway, None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result.ready is True and result.identity_generation == 1
    assert gateway.opened == [("p0", "p1", "p2"), ("p3", "p4", "p5")] == gateway.closed
    assert len(store.frozen) == 1 and [row["profile_ref"] for row in store.frozen[0]] == [f"p{n}" for n in range(6)]


@pytest.mark.parametrize("code", ["redis_unavailable", "adspower_unavailable", "profile_start_failed", "cdp_connect_failed", "tiktok_login_required", "tiktok_identity_unavailable", "target_video_mismatch", "profile_close_failed"])
def test_preflight_closed_failure_matrix_invalidates_current_batch_only(monkeypatch, code):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 3, "video_id": "123"}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": f"a{n}", "profile_ref": f"p{n}", "status": "planned"} for n in range(6)]
        def freeze_campaign_identities(self, *_args): raise AssertionError("failure must not freeze")
        def invalidate_campaign_identity(self, _id, _revision, _generation, **kwargs): self.invalidated = kwargs; self.campaign.update(identity_generation=1); return dict(self.campaign)
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, _refs, **_kw): return True
        async def open_identity_batch(self, _refs, *_args): raise CampaignValidationError(code)
    monkeypatch.setattr(module, "verify_video", lambda *_args: None)
    monkeypatch.setattr(module, "read_tiktok_identity", lambda *_args, **_kwargs: None)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store = Store()
    result = asyncio.run(CommentExecutor(store, Gateway(), None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result.ready is False and result.stale is False
    assert store.invalidated["error_code"] == code
    assert store.invalidated["affected_assignment_ids"] == ["a0", "a1", "a2"]


def test_preflight_generation_changes_during_scan_is_stale_without_freeze(monkeypatch):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 1, "batch_size": 1, "video_id": "123"}
        def __init__(self): self.freeze_calls = 0; self.invalidate_calls = 0
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def freeze_campaign_identities(self, *_args): self.freeze_calls += 1
        def invalidate_campaign_identity(self, _id, revision, generation, **_kwargs):
            self.invalidate_calls += 1
            assert (revision, generation) == (1, 1)
            raise RevisionConflictError("campaign")
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, _refs, **_kw): return True
        async def open_identity_batch(self, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): return {item.profile_id: True for item in bindings}
    store = Store()
    entered, release = asyncio.Event(), asyncio.Event()
    async def video(*_args): return {"video_id": "123"}
    async def identity(*_args, **_kwargs):
        entered.set()
        await release.wait()
        raise CampaignValidationError("tiktok_identity_unavailable")
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    executor = CommentExecutor(store, Gateway(), None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"})
    async def scenario():
        pending = asyncio.create_task(executor.preflight_campaign_identities("campaign", 1))
        await entered.wait()
        store.campaign.update(identity_generation=2, revision=2, status="running")
        release.set()
        return await pending
    result = asyncio.run(scenario())
    assert result.stale is True and result.identity_generation == 2 and store.freeze_calls == 0
    assert store.invalidate_calls == 1 and store.campaign == {"id": "campaign", "status": "running", "revision": 2, "identity_generation": 2, "batch_size": 1, "video_id": "123"}


def test_preflight_campaign_lease_loser_is_stale_without_any_write():
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 4, "batch_size": 1}
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): raise AssertionError("lease loser must not scan")
    class Gateway:
        async def acquire_campaign_lease(self, _id): raise IdentityPreflightStale()
    result = asyncio.run(CommentExecutor(Store(), Gateway(), None).preflight_campaign_identities("campaign", 4))
    assert result == type(result)(stale=True, ready=False, identity_generation=4)


def test_preflight_campaign_lease_redis_failure_invalidates_current_generation():
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 3, "identity_generation": 5, "batch_size": 2}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "status": "planned"}, {"assignment_id": "b", "status": "planned"}, {"assignment_id": "c", "status": "planned"}]
        def invalidate_campaign_identity(self, _id, revision, generation, **kwargs):
            self.invalidated = (revision, generation, kwargs); self.campaign.update(identity_generation=6); return dict(self.campaign)
    class Gateway:
        async def acquire_campaign_lease(self, _id): raise CampaignValidationError("redis_unavailable")
    store = Store()
    result = asyncio.run(CommentExecutor(store, Gateway(), None).preflight_campaign_identities("campaign", 5))
    assert result == type(result)(stale=False, ready=False, identity_generation=6)
    assert store.invalidated == (3, 5, {"error_code": "redis_unavailable", "affected_assignment_ids": ["a", "b", "c"], "failure_details": None})


def test_preflight_observation_failure_with_unconfirmed_close_invalidates_profile_close(monkeypatch):
    import comment_campaign.executor as module
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 1, "video_id": "123"}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def invalidate_campaign_identity(self, *_args, **kwargs): self.invalidated = kwargs; self.campaign.update(identity_generation=1); return dict(self.campaign)
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, *_args, **_kwargs): return True
        async def open_identity_batch(self, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): return {item.profile_id: False for item in bindings}
    async def video(*_args): return {"video_id": "123"}
    async def identity(*_args, **_kwargs): raise CampaignValidationError("tiktok_identity_unavailable")
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store = Store()
    result = asyncio.run(CommentExecutor(store, Gateway(), None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result.ready is False and result.stale is False and store.invalidated["error_code"] == "profile_close_failed"


def test_preflight_refresh_false_is_stale_without_invalidation(monkeypatch):
    import comment_campaign.executor as module
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 1, "video_id": "123"}
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def invalidate_campaign_identity(self, *_args, **_kwargs): raise AssertionError("ordinary refresh loss must not pause")
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return False
        async def refresh_leases(self, *_args, **_kwargs): return True
        async def open_identity_batch(self, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): return {item.profile_id: True for item in bindings}
    async def video(*_args): return {"video_id": "123"}
    async def identity(*_args, **_kwargs): return AccountObservation("p", "p", None, "2026-08-11T00:00:00Z")
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    result = asyncio.run(CommentExecutor(Store(), Gateway(), None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result == type(result)(stale=True, ready=False, identity_generation=0)


def test_identity_invalidation_outcome_stays_internal_and_projects_at_preflight_boundary():
    import comment_campaign.executor as module
    class Store:
        def invalidate_campaign_identity(self, *_args, **_kwargs): return {"identity_generation": 3}
    executor = CommentExecutor(Store(), None, None)
    outcome = executor._invalidate_preflight("campaign", 1, 2, "redis_unavailable", ["a"])
    assert isinstance(outcome, module._IdentityInvalidationOutcome)
    assert executor._preflight_from_invalidation(outcome) == module.PreflightResult(False, False, 3)


def test_prepare_batch_last_window_identity_drift_types_nothing(monkeypatch):
    import comment_campaign.executor as module
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 7, "identity_generation": 1,
                    "batch_size": 2, "video_id": "123", "canonical_url": "https://www.tiktok.com/@owner/video/123"}
        rows = {f"a{n}": {"assignment_id": f"a{n}", "campaign_id": "campaign", "profile_ref": f"p{n}",
                "position": n, "parent_assignment_id": None, "identity_generation": 1,
                "evidence": {"account_preflight": {"account_key": f"p{n}", "identity_generation": 1}}} for n in range(2)}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, assignment_id): return dict(self.rows[assignment_id])
        def invalidate_campaign_identity(self, _id, revision, generation, **kwargs):
            assert (revision, generation) == (7, 1)
            self.invalidated = kwargs; self.campaign.update(status="paused", identity_generation=2, revision=8); return dict(self.campaign)
    class Page:
        def __init__(self, key): self.key = key
        async def goto(self, *_args, **_kwargs): return None
    class Gateway:
        def __init__(self): self.closed = False
        async def open_many(self, refs, **_kwargs): return [type("B", (), {"profile_id": ref, "page": Page(ref)})() for ref in refs]
        async def close_bindings(self, bindings): self.closed = True; return {item.profile_id: True for item in bindings}
        async def release_campaign_lease(self, _id): return None
        async def refresh_leases(self, *_args, **_kwargs): return True
    async def video(*_args): return {"video_id": "123"}
    async def identity(page, *_args, **_kwargs):
        return AccountObservation("changed" if page.key == "p1" else "p0", page.key, None, "2026-08-11T00:00:00Z")
    async def bomb(*_args, **_kwargs): raise AssertionError("identity scan must finish before any input")
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    monkeypatch.setattr(module, "human_type", bomb); monkeypatch.setattr(module, "open_comment_panel", bomb)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store, gateway = Store(), Gateway()
    result = asyncio.run(CommentExecutor(store, gateway, None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).prepare_batch("campaign", ["a0", "a1"], 1))
    assert result.prepared == () and result.failed == ("a0", "a1") and gateway.closed is True
    assert store.invalidated["error_code"] == "tiktok_identity_changed" and store.invalidated["affected_assignment_ids"] == ["a1"]


def test_final_submit_generation_cas_loser_keeps_approval_and_never_clicks(monkeypatch):
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 4, "identity_generation": 1, "video_id": "123"}
        assignment = {"assignment_id": "a", "campaign_id": "campaign", "revision": 9,
                      "status": "awaiting_step_approval", "identity_generation": 1,
                      "profile_ref": "p", "expected_username": "account", "resolved_text": "hello",
                      "parent_assignment_id": None, "evidence": {"account_preflight": {"account_key": "account", "identity_generation": 1}}}
        def __init__(self): self.consumed = False
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, _id): return dict(self.assignment)
        def get_approval(self, *_args): return {"consumed_at": None}
        def verified_parent_receipt(self, *_args): return None
        def begin_submitting(self, *_args): raise RevisionConflictError("assignment")
        def invalidate_submit_approval(self, *_args): raise AssertionError("stale CAS must leave approval unconsumed")
    class Submit:
        clicks = 0
        async def click(self): self.clicks += 1
    class Gateway:
        async def open_one(self, *_args, **_kwargs): return type("B", (), {"page": object(), "profile_id": "p"})()
        async def close_bindings(self, bindings): return {item.profile_id: True for item in bindings}
        async def release_campaign_lease(self, _id): return None
        async def refresh_leases(self, *_args, **_kwargs): return True
    store, submit = Store(), Submit()
    executor = CommentExecutor(store, Gateway(), None)
    async def runtime(*_args): return "account"
    async def prepare(*_args, **_kwargs): return {"_submit": submit}
    async def candidates(*_args): return []
    monkeypatch.setattr(executor, "_runtime_identity_or_stop", runtime)
    monkeypatch.setattr(executor, "_prepare_page", prepare)
    monkeypatch.setattr(executor, "_preparation_evidence", lambda *_args: store.assignment["evidence"])
    monkeypatch.setattr(executor, "_candidates", candidates)
    result = asyncio.run(executor.submit_assignment("campaign", "a", 9))
    assert result == {"stale": True, "submitted": False, "identity_generation": 1}
    assert submit.clicks == 0 and store.consumed is False


def test_prepare_batch_rechecks_running_and_identity_before_open_many():
    class Store:
        campaign = {"id": "campaign", "status": "running", "identity_generation": 2, "batch_size": 1}
        def __init__(self): self.campaign_reads = 0
        def get_campaign(self, _id):
            self.campaign_reads += 1
            value = dict(self.campaign)
            if self.campaign_reads > 1: value["status"] = "paused"
            return value
        def get_assignment(self, _id): return {"assignment_id": "a", "campaign_id": "campaign", "profile_ref": "p", "position": 0, "parent_assignment_id": None}
    class Gateway:
        async def open_many(self, *_args, **_kwargs): raise AssertionError("paused race must not open Profiles")
    result = asyncio.run(CommentExecutor(Store(), Gateway(), None).prepare_batch("campaign", ["a"], 2))
    assert result.prepared == () and result.failed == ("a",)


def test_prepare_open_many_close_failure_marks_batch_unconfirmed_and_invalidates_snapshot():
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 7, "identity_generation": 3, "batch_size": 2}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, assignment_id):
            return {"assignment_id": assignment_id, "campaign_id": "campaign", "profile_ref": assignment_id,
                    "position": 0, "parent_assignment_id": None}
        def invalidate_campaign_identity(self, _id, revision, generation, **kwargs):
            assert (revision, generation) == (7, 3)
            self.invalidated = kwargs
            self.campaign.update(status="paused", revision=8, identity_generation=4)
            return dict(self.campaign)
    class Gateway:
        async def open_many(self, *_args, **_kwargs): raise CampaignValidationError("profile_close_failed")
    store = Store()
    result = asyncio.run(CommentExecutor(store, Gateway(), None).prepare_batch("campaign", ["a", "b"], 3))
    assert result == BatchResult((), ("a", "b"), False)
    assert store.invalidated == {"error_code": "profile_close_failed", "affected_assignment_ids": ["a", "b"], "failure_details": None}


def test_preflight_heartbeat_loss_closes_current_profile_and_never_freezes(monkeypatch):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 1, "video_id": "123"}
        def __init__(self): self.freeze_calls = 0
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def freeze_campaign_identities(self, *_args): self.freeze_calls += 1
    class Gateway:
        def __init__(self): self.closed = False; self.refreshes = []
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, refs, **_kw): self.refreshes.append(tuple(refs)); return not refs
        async def open_identity_batch(self, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): self.closed = True; return {item.profile_id: True for item in bindings}
    class FastHeartbeat(module._LeaseHeartbeat):
        def __init__(self, refresh, **_kwargs): super().__init__(refresh, interval_seconds=0.001)
    async def video(*_args): await asyncio.sleep(0.02); return {"video_id": "123"}
    async def identity(*_args, **_kwargs): raise AssertionError("heartbeat loss must prohibit identity read")
    monkeypatch.setattr(module, "_LeaseHeartbeat", FastHeartbeat)
    monkeypatch.setattr(module, "verify_video", video); monkeypatch.setattr(module, "read_tiktok_identity", identity)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store, gateway = Store(), Gateway()
    result = asyncio.run(CommentExecutor(store, gateway, None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result.stale is True and gateway.closed is True and ("p",) in gateway.refreshes and store.freeze_calls == 0


def test_preflight_heartbeat_loss_with_unconfirmed_close_invalidates_profile_close(monkeypatch):
    import comment_campaign.executor as module
    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 1, "video_id": "123"}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def invalidate_campaign_identity(self, _id, revision, generation, **kwargs):
            assert (revision, generation) == (1, 0)
            self.invalidated = kwargs; self.campaign.update(identity_generation=1); return dict(self.campaign)
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, refs, **_kw): return not refs
        async def open_identity_batch(self, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): return {item.profile_id: False for item in bindings}
    class FastHeartbeat(module._LeaseHeartbeat):
        def __init__(self, refresh, **_kwargs): super().__init__(refresh, interval_seconds=0.001)
    async def video(*_args): await asyncio.sleep(0.02); return {"video_id": "123"}
    monkeypatch.setattr(module, "_LeaseHeartbeat", FastHeartbeat); monkeypatch.setattr(module, "verify_video", video)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store = Store()
    result = asyncio.run(CommentExecutor(store, Gateway(), None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result == type(result)(stale=False, ready=False, identity_generation=1)
    assert store.invalidated["error_code"] == "profile_close_failed" and store.invalidated["affected_assignment_ids"] == ["a"]


def test_preflight_heartbeat_redis_error_invalidates_generation(monkeypatch):
    import comment_campaign.executor as module

    class Store:
        campaign = {"id": "campaign", "status": "running", "revision": 1, "identity_generation": 0, "batch_size": 1, "video_id": "123"}
        def __init__(self): self.invalidated = None
        def get_campaign(self, _id): return dict(self.campaign)
        def list_assignments(self, _id): return [{"assignment_id": "a", "profile_ref": "p", "status": "planned"}]
        def invalidate_campaign_identity(self, _id, _revision, _generation, **kwargs): self.invalidated = kwargs; self.campaign.update(identity_generation=1); return dict(self.campaign)
    class Gateway:
        async def acquire_campaign_lease(self, _id): return None
        async def release_campaign_lease(self, _id): return None
        async def refresh_campaign_lease(self, _id): return True
        async def refresh_leases(self, refs, **_kw):
            if refs: raise CampaignValidationError("redis_unavailable")
            return True
        async def open_identity_batch(self, *_args): return [type("B", (), {"page": "p", "profile_id": "p"})()]
        async def close_bindings(self, bindings): return {item.profile_id: True for item in bindings}
    class FastHeartbeat(module._LeaseHeartbeat):
        def __init__(self, refresh, **_kwargs): super().__init__(refresh, interval_seconds=0.001)
    async def video(*_args): await asyncio.sleep(0.02); return {"video_id": "123"}
    monkeypatch.setattr(module, "_LeaseHeartbeat", FastHeartbeat); monkeypatch.setattr(module, "verify_video", video)
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    store = Store()
    result = asyncio.run(CommentExecutor(store, Gateway(), None, element_provider=lambda _id: element,
        settings_provider=lambda _c: {"account_element_id": "account"}).preflight_campaign_identities("campaign", 0))
    assert result.stale is False and result.ready is False
    assert store.invalidated["error_code"] == "redis_unavailable" and store.invalidated["affected_assignment_ids"] == ["a"]


def _real_submit_runtime(tmp_path, name, *, sibling=False):
    """A real temporary Store with only the browser boundary faked."""
    from comment_campaign.service import CommentCampaignService
    from comment_campaign.store import CampaignStore

    class Queue:
        def __init__(self): self.calls = []
        def enqueue_prepare_generation(self, *args): self.calls.append(args); return {"id": "prepare"}

    store = CampaignStore(f"sqlite:///{tmp_path / (name + '.db')}")
    store.initialize()
    profile_refs = [row["profile_ref"] for row in store.sync_profile_identities([
        {"id": f"raw-{name}-{index}", "name": f"{name}-{index}", "status": "active"}
        for index in range(2 if sibling else 1)
    ])]
    # This deliberately disagrees with the live handle below: metadata/history
    # must never supply a Receipt author key.
    for profile_ref in profile_refs:
        store.upsert_profile_metadata(profile_ref=profile_ref, expected_username="historical_handle",
                                      enabled=True, login_verified=True, tags=[], language="", region="",
                                      cooldown_until=None, health_status="healthy")
    queue = Queue()
    service = CommentCampaignService(store, queue_coordinator=queue)
    service.create_template({"name": name, "description": "", "supported_modes": ["independent"],
        "language": "", "tags": [], "steps": [{"id": f"root-{index}", "label": f"root-{index}",
        "content_source": "fixed", "fixed_text": f"hello-{index}", "content_library_id": "",
        "content_item_id": "", "parent_step_id": None, "required_profile_tags": [],
        "excluded_profile_tags": [], "language": ""} for index in range(2 if sibling else 1)]}, f"template-{name}")
    service.create_campaign({"name": name, "mode": "independent", "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": f"template-{name}", "profile_refs": profile_refs}, f"campaign-{name}")
    plan = service.plan_campaign(f"campaign-{name}", seed=name)
    locked = service.lock_plan(f"campaign-{name}", plan["campaign"]["revision"])
    campaign = service.approve_campaign(f"campaign-{name}", locked["revision"])["campaign"]
    campaign = store.transition_campaign_status(f"campaign-{name}", campaign["revision"], "running")
    frozen = store.freeze_campaign_identities(f"campaign-{name}", campaign["revision"], 0, tuple({
        "assignment_id": assignment["assignment_id"], "profile_ref": assignment["profile_ref"],
        "account_key": f"frozen_handle_{index}", "visible_username": "Frozen", "canonical_href": "",
        "observed_at": "2026-08-11T00:00:00Z",
        "target_video": {"video_id": "12345678", "canonical_url": "https://www.tiktok.com/@owner/video/12345678"},
        "element_binding": {"id": "account", "revision": 1, "definition_sha256": "a" * 64},
    } for index, assignment in enumerate(plan["assignments"])))
    awaiting = []
    for assignment in plan["assignments"]:
        assignment = store.get_assignment(assignment["assignment_id"])
        for state in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
            assignment = store.update_assignment_status(assignment["assignment_id"], assignment["revision"], state)
        store.create_submit_approval(f"campaign-{name}", assignment["assignment_id"], assignment["revision"], "opaque")
        awaiting.append(assignment)
    queue.calls.clear()
    return store, f"campaign-{name}", awaiting[0], frozen["identity_generation"], queue, awaiting


@pytest.mark.parametrize("click_raises,close_confirmed,status", [
    (False, True, "published_verified"), (True, True, "published_unverified"),
    (False, False, "published_verified"), (True, False, "published_unverified"),
])
def test_real_store_receipt_uses_same_generation_runtime_handle_not_metadata(tmp_path, monkeypatch, click_raises, close_confirmed, status):
    store, campaign_id, assignment, generation, queue, _ = _real_submit_runtime(tmp_path, f"receipt-{click_raises}-{close_confirmed}")

    class Submit:
        def __init__(self): self.clicks = 0
        async def click(self):
            self.clicks += 1
            if click_raises: raise RuntimeError("browser disconnected after click")
    class Page: pass
    class Gateway:
        async def open_one(self, *_args, **_kwargs): return type("Binding", (), {"profile_id": "p", "page": Page()})()
        async def close_bindings(self, bindings): return {item.profile_id: close_confirmed for item in bindings}
        async def release_campaign_lease(self, _id): return None
        async def refresh_leases(self, *_args, **_kwargs): return True

    submit = Submit()
    executor = CommentExecutor(store, Gateway(), None, queue_coordinator=queue)
    async def runtime(*_args): return "runtime_handle"
    async def prepared(*_args, **_kwargs): return {"_submit": submit}
    async def candidates(*_args): return []
    async def verified(*_args): return (True, {})
    monkeypatch.setattr(executor, "_runtime_identity_or_stop", runtime)
    monkeypatch.setattr(executor, "_prepare_page", prepared)
    monkeypatch.setattr(executor, "_preparation_evidence", lambda *_args: store.get_assignment(assignment["assignment_id"])["evidence"])
    monkeypatch.setattr(executor, "_candidates", candidates)
    monkeypatch.setattr(executor, "_verify_post_click", verified)
    monkeypatch.setattr(executor, "_screenshot", lambda *_args: asyncio.sleep(0, result="evidence/" + "a" * 32 + ".png"))

    result = asyncio.run(executor.submit_assignment(campaign_id, assignment["assignment_id"], assignment["revision"]))
    receipt = store.list_receipts(campaign_id)[-1]
    assert result["status"] == status and submit.clicks == 1
    assert receipt["expected_username"] == "runtime_handle"
    assert receipt["expected_username"] not in {"historical_handle", "frozen_handle"}
    if not close_confirmed:
        assert store.get_profile_metadata(assignment["profile_ref"])["health_status"] == "unhealthy"
        assert queue.calls == []


@pytest.mark.parametrize("click_raises,status", [(False, "published_verified"), (True, "published_unverified")])
def test_close_failure_after_durable_root_invalidates_active_sibling_without_replaying_result(tmp_path, monkeypatch, click_raises, status):
    store, campaign_id, root, generation, queue, awaiting = _real_submit_runtime(
        tmp_path, f"sibling-close-{click_raises}", sibling=True,
    )
    sibling = awaiting[1]
    class Submit:
        async def click(self):
            if click_raises: raise RuntimeError("post-click disconnect")
    class Gateway:
        async def open_one(self, *_args, **_kwargs): return type("B", (), {"profile_id": "root", "page": object()})()
        async def close_bindings(self, bindings): return {item.profile_id: False for item in bindings}
        async def release_campaign_lease(self, *_args): return None
        async def refresh_leases(self, *_args, **_kwargs): return True
    executor = CommentExecutor(store, Gateway(), None, queue_coordinator=queue)
    async def runtime(*_args): return "runtime-root"
    async def prepared(*_args, **_kwargs): return {"_submit": Submit()}
    async def candidates(*_args): return []
    async def verified(*_args): return (True, {})
    monkeypatch.setattr(executor, "_runtime_identity_or_stop", runtime)
    monkeypatch.setattr(executor, "_prepare_page", prepared)
    monkeypatch.setattr(executor, "_preparation_evidence", lambda *_args: store.get_assignment(root["assignment_id"])["evidence"])
    monkeypatch.setattr(executor, "_candidates", candidates)
    monkeypatch.setattr(executor, "_verify_post_click", verified)
    monkeypatch.setattr(executor, "_screenshot", lambda *_args: asyncio.sleep(0, result="evidence/" + "b" * 32 + ".png"))

    result = asyncio.run(executor.submit_assignment(campaign_id, root["assignment_id"], root["revision"]))
    campaign, changed_sibling = store.get_campaign(campaign_id), store.get_assignment(sibling["assignment_id"])
    assert result["status"] == status
    assert store.list_receipts(campaign_id)[0]["expected_username"] == "runtime-root"
    assert campaign["pause_reason"] == "profile_close_failed" and campaign["identity_generation"] == generation + 1
    assert changed_sibling["status"] == "paused" and changed_sibling["error_code"] == "profile_close_failed"
    assert store.get_approval(sibling["assignment_id"], sibling["revision"])["consumed_at"]
    assert queue.calls == []


def test_runtime_drift_invalidates_all_approvals_then_resume_refreezes_every_assignment(tmp_path, monkeypatch):
    """End-to-end: real Store transactions, fake browser/runtime only."""
    from comment_campaign.service import CommentCampaignService
    from comment_campaign.store import CampaignStore
    import comment_campaign.executor as module

    class Queue:
        def __init__(self): self.calls = []
        def enqueue_prepare_generation(self, *args): self.calls.append(args); return {"id": "prepare"}
    store, queue = CampaignStore(f"sqlite:///{tmp_path / 'drift.db'}"), Queue()
    store.initialize()
    refs = [row["profile_ref"] for row in store.sync_profile_identities([
        {"id": f"raw-{index}", "name": f"profile-{index}", "status": "active"} for index in range(2)
    ])]
    for index, ref in enumerate(refs):
        store.upsert_profile_metadata(profile_ref=ref, expected_username=f"historic-{index}", enabled=True,
                                      login_verified=True, tags=[], language="", region="", cooldown_until=None,
                                      health_status="healthy")
    service = CommentCampaignService(store, queue_coordinator=queue)
    steps = [{"id": f"root-{index}", "label": f"root-{index}", "content_source": "fixed",
              "fixed_text": f"text-{index}", "content_library_id": "", "content_item_id": "",
              "parent_step_id": None, "required_profile_tags": [], "excluded_profile_tags": [], "language": ""}
             for index in range(2)]
    service.create_template({"name": "drift", "description": "", "supported_modes": ["independent"],
                             "language": "", "tags": [], "steps": steps}, "drift-template")
    service.create_campaign({"name": "drift", "mode": "independent", "target_source": "manual_url",
                             "target_reference": "https://www.tiktok.com/@owner/video/12345678",
                             "template_id": "drift-template", "profile_refs": refs}, "drift-campaign")
    plan = service.plan_campaign("drift-campaign", seed="drift")
    locked = service.lock_plan("drift-campaign", plan["campaign"]["revision"])
    campaign = service.approve_campaign("drift-campaign", locked["revision"])["campaign"]
    campaign = store.transition_campaign_status("drift-campaign", campaign["revision"], "running")
    rows = store.list_assignments("drift-campaign")
    frozen = store.freeze_campaign_identities("drift-campaign", campaign["revision"], 0, tuple({
        "assignment_id": row["assignment_id"], "profile_ref": row["profile_ref"], "account_key": f"live-{index}",
        "visible_username": f"Live {index}", "canonical_href": "", "observed_at": "2026-08-11T00:00:00Z",
        "target_video": {"video_id": "12345678", "canonical_url": "https://www.tiktok.com/@owner/video/12345678"},
        "element_binding": {"id": "account", "revision": 1, "definition_sha256": "a" * 64},
    } for index, row in enumerate(rows)))
    awaiting = []
    for row in store.list_assignments("drift-campaign"):
        current = row
        for state in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
            current = store.update_assignment_status(current["assignment_id"], current["revision"], state)
        store.create_submit_approval("drift-campaign", current["assignment_id"], current["revision"], "opaque")
        awaiting.append(current)

    class Gateway:
        async def open_one(self, *_args, **_kwargs): return type("B", (), {"profile_id": "p", "page": object()})()
        async def close_bindings(self, bindings): return {item.profile_id: True for item in bindings}
        async def release_campaign_lease(self, *_args): return None
        async def refresh_leases(self, *_args, **_kwargs): return True
    executor = CommentExecutor(store, Gateway(), None)
    async def drift(*_args): raise CampaignValidationError("tiktok_identity_changed")
    monkeypatch.setattr(executor, "_runtime_identity_or_stop", drift)
    stopped = asyncio.run(executor.submit_assignment("drift-campaign", awaiting[0]["assignment_id"], awaiting[0]["revision"]))
    assert stopped["submitted"] is False
    assert store.list_receipts("drift-campaign") == []
    assert store.get_campaign("drift-campaign")["status"] == "paused"
    assert all(store.get_approval(row["assignment_id"], row["revision"])["consumed_at"]
               for row in awaiting)

    # Resume is only allowed to enqueue prepare.  Its async worker path runs a
    # full fake-browser preflight and makes both non-terminal assignments eligible
    # under the new frozen generation.
    resumed = service.resume_campaign("drift-campaign", store.get_campaign("drift-campaign")["revision"])
    class PreflightGateway:
        async def acquire_campaign_lease(self, *_args): return None
        async def release_campaign_lease(self, *_args): return None
        async def refresh_campaign_lease(self, *_args): return True
        async def refresh_leases(self, *_args, **_kwargs): return True
        async def open_identity_batch(self, batch_refs, *_args):
            return [type("B", (), {"profile_id": ref, "page": type("Page", (), {"ref": ref})()})() for ref in batch_refs]
        async def close_bindings(self, bindings): return {item.profile_id: True for item in bindings}
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    fresh = CommentExecutor(store, PreflightGateway(), None, element_provider=lambda _id: element,
                            settings_provider=lambda _campaign: {"account_element_id": "account"})
    async def video(*_args): return {"video_id": "12345678"}
    async def identity(page, *_args, **_kwargs):
        index = refs.index(page.ref)
        return AccountObservation(f"refrozen-{index}", f"Refrozen {index}", None, "2026-08-11T00:00:00Z")
    async def prepared(campaign_id, ids, generation): return module.BatchResult(tuple(ids), (), True)
    monkeypatch.setattr(module, "verify_video", video)
    monkeypatch.setattr(module, "read_tiktok_identity", identity)
    monkeypatch.setattr(fresh, "prepare_batch", prepared)
    service._executor = fresh
    result = asyncio.run(service.job_prepare_campaign("drift-campaign", resumed["campaign"]["prepare_generation"], resumed["campaign"]["identity_generation"]))
    current = store.get_campaign("drift-campaign")
    active = store.list_assignments("drift-campaign")
    assert result["prepared"] == tuple(row["assignment_id"] for row in active)
    assert current["identity_generation"] == frozen["identity_generation"] + 2
    assert {row["status"] for row in active} == {"planned"}
    assert {row["evidence"]["account_preflight"]["identity_generation"] for row in active} == {current["identity_generation"]}


def test_child_scope_uses_parent_receipt_runtime_handle_not_profile_history(monkeypatch):
    """The child locator is bound to the durable parent Receipt, not metadata."""
    import comment_campaign.executor as module

    class Handle:
        async def focus(self): return None
        async def evaluate(self, *_args): return None
    class Store:
        campaign = {"id": "campaign", "canonical_url": "https://www.tiktok.com/@owner/video/123", "video_id": "123"}
        assignment = {"assignment_id": "child", "campaign_id": "campaign", "revision": 4,
                      "status": "preparing_comment", "profile_ref": "profile", "expected_username": "historic-child",
                      "resolved_text": "reply", "parent_assignment_id": "parent"}
        def get_campaign(self, _id): return dict(self.campaign)
        def get_assignment(self, _id): return dict(self.assignment)
        def verified_parent_receipt(self, *_args):
            return {"receipt_id": "receipt-parent", "expected_username": "runtime-parent-handle"}
    class Page:
        async def goto(self, *_args, **_kwargs): return None
    element = {"status": "active", "kind": "click", "revision": 1, "definition": {"locators": []}}
    executor = CommentExecutor(Store(), object(), None, element_provider=lambda _id: element,
                               settings_provider=lambda _campaign: {"account_element_id": "account", "entry_element_id": "entry"})
    async def video(*_args): return {"video_id": "123"}
    async def account(*_args, **_kwargs): return {"account_key": "historic-child"}
    async def panel(*_args, **_kwargs): return None
    async def parent(*_args, **_kwargs): return {"parent": "node"}
    async def scope(_parent, account_key):
        assert account_key == "runtime-parent-handle"
        return {"input": Handle(), "submit": object(), "parent_scope": {"parent_platform_comment_id": "runtime-parent-id"},
                "parent_author": "runtime-parent-handle"}
    async def typed(*_args, **_kwargs): return None
    async def input_value(*_args, **_kwargs): return "reply"
    monkeypatch.setattr(module, "verify_video", video)
    monkeypatch.setattr(module, "verify_logged_in_username", account)
    monkeypatch.setattr(module, "open_comment_panel", panel)
    monkeypatch.setattr(module, "locate_parent_comment", parent)
    monkeypatch.setattr(module, "open_scoped_reply", scope)
    monkeypatch.setattr(module, "human_type", typed)
    monkeypatch.setattr(module, "_read_input_value", input_value)

    evidence = asyncio.run(executor._prepare_page(Store.campaign, Store.assignment, Page(), retries=1))
    assert evidence["parent"] == {"parent_receipt_id": "receipt-parent", "parent_platform_comment_id": "runtime-parent-id",
                                  "parent_author": "runtime-parent-handle"}
