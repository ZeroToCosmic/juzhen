"""Restart recovery is deliberately preparation-only and never reposts."""

from __future__ import annotations

import pytest

from comment_campaign.service import CommentCampaignService
from comment_campaign.store import CampaignStore


class PrepareOnlyQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, int]] = []

    def enqueue_prepare_generation(
        self, campaign_id: str, generation: int, identity_generation: int,
    ):
        self.calls.append(("prepare", campaign_id, generation, identity_generation))
        return {"id": f"campaign-prepare-{campaign_id}-g{generation}"}

    def enqueue_submit(self, *_args):  # pragma: no cover - tripwire
        raise AssertionError("reconciliation must never enqueue a submit")


def _runtime(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'recovery.db'}")
    store.initialize()
    profile_ref = store.sync_profile_identities([
        {"id": "raw-recovery-profile", "name": "Recovery profile", "status": "active"}
    ])[0]["profile_ref"]
    store.upsert_profile_metadata(
        profile_ref=profile_ref, expected_username="recovery_user", enabled=True,
        login_verified=True, tags=[], language="", region="", cooldown_until=None,
        health_status="healthy",
    )
    queue = PrepareOnlyQueue()
    service = CommentCampaignService(store, queue_coordinator=queue)
    service.create_template({
        "name": "recovery", "description": "", "supported_modes": ["independent"],
        "language": "", "tags": [], "steps": [{
            "id": "root", "label": "root", "content_source": "fixed", "fixed_text": "safe text",
            "content_library_id": "", "content_item_id": "", "parent_step_id": None,
            "required_profile_tags": [], "excluded_profile_tags": [], "language": "",
        }],
    }, "recovery-template")
    service.create_campaign({
        "name": "recovery", "mode": "independent", "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": "recovery-template", "profile_refs": [profile_ref],
    }, "recovery-campaign")
    planned = service.plan_campaign("recovery-campaign", seed="recovery-seed")
    locked = service.lock_plan("recovery-campaign", planned["campaign"]["revision"])
    campaign = service.approve_campaign("recovery-campaign", locked["revision"])["campaign"]
    campaign = store.transition_campaign_status("recovery-campaign", campaign["revision"], "running")
    assignment = planned["assignments"][0]
    store.freeze_campaign_identities("recovery-campaign", campaign["revision"], 0, ({
        "assignment_id": assignment["assignment_id"], "profile_ref": assignment["profile_ref"],
        "account_key": "recovery_user", "visible_username": "Recovery user",
        "canonical_href": "https://www.tiktok.com/@recovery_user",
        "observed_at": "2026-08-11T00:00:00Z",
        "target_video": {"video_id": "12345678", "canonical_url": "https://www.tiktok.com/@owner/video/12345678"},
        "element_binding": {"id": "account", "revision": 1, "definition_sha256": "a" * 64},
    },))
    queue.calls.clear()  # The initial durable g1 is not a reconciliation event.
    return service, queue, planned["assignments"][0]


def _awaiting_with_consumed_approval(service, assignment_id: str, *, consume: bool = True):
    assignment = service.store.get_assignment(assignment_id)
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        assignment = service.store.update_assignment_status(assignment_id, assignment["revision"], status)
    service.store.create_submit_approval("recovery-campaign", assignment_id, assignment["revision"], "opaque")
    if consume:
        service.store.consume_submit_approval("recovery-campaign", assignment_id, assignment["revision"])
    return assignment


def test_reconcile_consumed_approval_reprepares_with_new_revision_never_submit(tmp_path):
    service, queue, root = _runtime(tmp_path)
    before = _awaiting_with_consumed_approval(service, root["assignment_id"])

    result = service.reconcile_campaign("recovery-campaign")

    after = service.store.get_assignment(root["assignment_id"])
    assert after["status"] == "waiting_dependency"
    assert after["revision"] == before["revision"] + 1
    assert after["evidence"] == {}
    assert result["recovered"] == 1
    assert queue.calls == [("prepare", "recovery-campaign", 2, 1)]


@pytest.mark.parametrize("interrupted_status", ["submitting", "verifying_receipt"])
def test_reconcile_marks_interrupted_submit_unverified_and_never_replays_it(tmp_path, interrupted_status):
    service, queue, root = _runtime(tmp_path)
    awaiting = _awaiting_with_consumed_approval(service, root["assignment_id"], consume=False)
    submitting = service.store.begin_submitting("recovery-campaign", root["assignment_id"], awaiting["revision"], 1)
    if interrupted_status == "verifying_receipt":
        submitting = service.store.update_assignment_status(root["assignment_id"], submitting["revision"], interrupted_status)

    result = service.reconcile_campaign("recovery-campaign")

    after = service.store.get_assignment(root["assignment_id"])
    assert after["status"] == "published_unverified"
    assert after["revision"] == submitting["revision"] + 1
    assert result["recovered"] == 1
    assert queue.calls == []
    receipts = service.store.list_receipts("recovery-campaign")
    assert receipts[-1]["status"] == "published_unverified"
    assert service.store.list_attempts("recovery-campaign")[-1]["stage"] == "recovery"


def test_reconcile_paused_campaign_does_not_enqueue_even_when_work_is_eligible(tmp_path):
    service, queue, _root = _runtime(tmp_path)
    campaign = service.store.get_campaign("recovery-campaign")
    service.store.transition_campaign_status("recovery-campaign", campaign["revision"], "paused", pause_reason="operator")

    result = service.reconcile_campaign("recovery-campaign")

    assert result["enqueued"] is False
    assert queue.calls == []


def test_reconcile_rolls_back_receipt_attempt_and_status_when_recovery_audit_fails(tmp_path, monkeypatch):
    service, _queue, root = _runtime(tmp_path)
    awaiting = _awaiting_with_consumed_approval(service, root["assignment_id"], consume=False)
    service.store.begin_submitting("recovery-campaign", root["assignment_id"], awaiting["revision"], 1)

    monkeypatch.setattr(service.store, "_append_recovery_attempt", lambda *_args: (_ for _ in ()).throw(RuntimeError("audit failed")))
    try:
        service.store.reconcile_campaign_state("recovery-campaign")
    except RuntimeError as exc:
        assert str(exc) == "audit failed"
    else:
        raise AssertionError("fault injection must roll back the recovery transaction")

    assert service.store.get_assignment(root["assignment_id"])["status"] == "submitting"
    assert service.store.list_receipts("recovery-campaign") == []
    assert service.store.list_attempts("recovery-campaign") == []


def test_threaded_recovery_pauses_only_descendants_and_rolls_back_pause_fault(tmp_path, monkeypatch):
    store = CampaignStore(f"sqlite:///{tmp_path / 'threaded.db'}"); store.initialize()
    refs = [row["profile_ref"] for row in store.sync_profile_identities([
        {"id": f"raw-{index}", "name": str(index), "status": "active"} for index in range(4)
    ])]
    for index, ref in enumerate(refs):
        store.upsert_profile_metadata(profile_ref=ref, expected_username=f"u{index}", enabled=True, login_verified=True, tags=[], language="", region="", cooldown_until=None, health_status="healthy")
    service = CommentCampaignService(store, queue_coordinator=PrepareOnlyQueue())
    service.create_template({"name":"tree","description":"","supported_modes":["threaded"],"language":"","tags":[],"steps":[
        {"id":"root","label":"root","content_source":"fixed","fixed_text":"r","content_library_id":"","content_item_id":"","parent_step_id":None,"required_profile_tags":[],"excluded_profile_tags":[],"language":""},
        {"id":"child","label":"child","content_source":"fixed","fixed_text":"c","content_library_id":"","content_item_id":"","parent_step_id":"root","required_profile_tags":[],"excluded_profile_tags":[],"language":""},
        {"id":"grand","label":"grand","content_source":"fixed","fixed_text":"g","content_library_id":"","content_item_id":"","parent_step_id":"child","required_profile_tags":[],"excluded_profile_tags":[],"language":""},
        {"id":"sibling","label":"sibling","content_source":"fixed","fixed_text":"s","content_library_id":"","content_item_id":"","parent_step_id":"root","required_profile_tags":[],"excluded_profile_tags":[],"language":""},
    ]}, "tree")
    service.create_campaign({"name":"tree","mode":"threaded","target_source":"manual_url","target_reference":"https://www.tiktok.com/@o/video/12345678","template_id":"tree","profile_refs":refs}, "tree-c")
    plan=service.plan_campaign("tree-c", seed="tree"); locked=service.lock_plan("tree-c",plan["campaign"]["revision"]); campaign=service.approve_campaign("tree-c",locked["revision"])["campaign"]; campaign=store.transition_campaign_status("tree-c",campaign["revision"],"running")
    planned_rows = store.list_assignments("tree-c")
    store.freeze_campaign_identities("tree-c", campaign["revision"], 0, tuple({
        "assignment_id": row["assignment_id"], "profile_ref": row["profile_ref"],
        "account_key": f"u{index}", "visible_username": f"user {index}",
        "canonical_href": f"https://www.tiktok.com/@u{index}",
        "observed_at": "2026-08-11T00:00:00Z",
        "target_video": {"video_id": "12345678", "canonical_url": "https://www.tiktok.com/@o/video/12345678"},
        "element_binding": {"id": "account", "revision": 1, "definition_sha256": "a" * 64},
    } for index, row in enumerate(planned_rows)))
    rows={row["step_id"]:row for row in store.list_assignments("tree-c")}; root=rows["root"]
    for state in ("opening_profile","locating_video","preparing_comment","awaiting_step_approval","submitting","verifying_receipt","published_verified"): root=store.update_assignment_status(root["assignment_id"],root["revision"],state)
    store.save_receipt(root["assignment_id"], {"status":"published_verified"})
    root_child=rows["child"]
    for state in ("opening_profile","locating_video","locating_parent","preparing_comment","awaiting_step_approval"): root_child=store.update_assignment_status(root_child["assignment_id"],root_child["revision"],state)
    store.create_submit_approval("tree-c",root_child["assignment_id"],root_child["revision"],"x"); store.begin_submitting("tree-c",root_child["assignment_id"],root_child["revision"], 1)
    service.reconcile_campaign("tree-c")
    states={row["step_id"]:row["status"] for row in store.list_assignments("tree-c")}
    assert states["root"] == "published_verified" and states["child"] == "published_unverified" and states["grand"] == "paused_dependency" and states["sibling"] == "waiting_dependency"
