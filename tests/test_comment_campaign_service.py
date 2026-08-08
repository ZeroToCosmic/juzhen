import pytest

from comment_campaign.errors import CampaignError, RevisionConflictError
from comment_campaign.service import CommentCampaignService
from comment_campaign.store import CampaignStore


class FakeCampaignQueue:
    def __init__(self, *, fail_submit=False):
        self.fail_submit = fail_submit
        self.calls = []

    def enqueue_prepare(self, campaign_id):
        self.calls.append(("prepare", campaign_id))
        return {"id": f"campaign-prepare-{campaign_id}"}

    def enqueue_at(self, campaign_id, when):
        self.calls.append(("scheduled", campaign_id, when))
        return {"id": f"campaign-prepare-{campaign_id}"}

    def enqueue_submit(self, campaign_id, assignment_id, revision):
        self.calls.append(("submit", campaign_id, assignment_id, revision))
        if self.fail_submit:
            raise RuntimeError("queue unavailable")
        return {"id": f"campaign-submit-{assignment_id}-r{revision}"}



def _template():
    return {"name": "thread", "description": "", "supported_modes": ["threaded"], "language": "en", "tags": [], "steps": [
        {"id": "root", "label": "root", "content_source": "fixed", "fixed_text": "one", "content_library_id": "", "content_item_id": "", "parent_step_id": None, "required_profile_tags": ["root"], "excluded_profile_tags": [], "language": "en"},
        {"id": "reply", "label": "reply", "content_source": "library", "fixed_text": "", "content_library_id": "copy", "content_item_id": "", "parent_step_id": "root", "required_profile_tags": [], "excluded_profile_tags": [], "language": "en"},
    ]}


@pytest.fixture
def service(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    resolver = lambda library_id: [{"content_item_id": "item-1", "text": "two"}]
    result = CommentCampaignService(store, publish_result_resolver=lambda _: "https://www.tiktok.com/@owner/video/12345678", content_resolver=resolver)
    result.create_template(_template(), "template")
    refs = [row["profile_ref"] for row in store.sync_profile_identities([{"id": "raw-a", "name": "A", "status": "active"}, {"id": "raw-b", "name": "B", "status": "active"}])]
    store.upsert_profile_metadata(profile_ref=refs[0], expected_username="alice", enabled=True, login_verified=True, tags=["root"], language="en", region="", cooldown_until=None, health_status="healthy")
    store.upsert_profile_metadata(profile_ref=refs[1], expected_username="bob", enabled=True, login_verified=True, tags=[], language="en", region="", cooldown_until=None, health_status="healthy")
    result.create_campaign({"name": "campaign", "mode": "threaded", "target_source": "publish_result", "target_reference": "published-id", "template_id": "template", "profile_refs": refs}, "campaign")
    return result


def test_plan_freezes_resolved_library_item_and_lock_is_cas_atomic(service):
    calls = []
    service._content_resolver = lambda library_id: calls.append(library_id) or [{"content_item_id": "item-1", "text": "two"}]
    planned = service.plan_campaign("campaign", seed="seed")
    assert calls == ["copy"]
    assert planned["campaign"]["status"] == "planned"
    assert planned["campaign"]["content_snapshot"]["steps"] == [{"step_id": "root", "content_source": "fixed", "content_library_id": "", "content_item_id": "", "resolved_text": "one"}, {"step_id": "reply", "content_source": "library", "content_library_id": "copy", "content_item_id": "item-1", "resolved_text": "two"}]
    locked = service.lock_plan("campaign", planned["campaign"]["revision"])
    assert locked["status"] == "awaiting_campaign_approval"
    assert locked["locked_at"]
    assert all(row["locked_at"] for row in service.store.list_assignments("campaign"))
    with pytest.raises(RevisionConflictError):
        service.reallocate_campaign("campaign", expected_revision=locked["revision"])


def test_comment_settings_use_one_strict_revisioned_persistent_callback(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    persisted = {"revision": 3, "element_bindings": {
        "entry_element_id": "entry-old", "input_element_id": "input-old",
        "submit_element_id": "submit-old", "account_element_id": "account-old",
    }}
    updates = []

    def provider():
        return dict(persisted)

    def updater(expected, bindings):
        if expected != persisted["revision"]:
            raise RevisionConflictError("comment-settings")
        persisted["revision"] += 1
        persisted["element_bindings"] = dict(bindings)
        updates.append((expected, dict(bindings)))
        return dict(persisted)

    service = CommentCampaignService(
        store, settings_provider=provider, settings_updater=updater
    )
    payload = {
        "expected_revision": 3, "entry_element_id": "entry-new",
        "input_element_id": "input-new", "submit_element_id": "submit-new",
        "account_element_id": "account-new",
    }

    assert service.get_comment_settings()["can_write"] is True
    saved = service.update_comment_settings(payload)

    assert updates == [(3, {
        "entry_element_id": "entry-new", "input_element_id": "input-new",
        "submit_element_id": "submit-new", "account_element_id": "account-new",
    })]
    assert saved["revision"] == 4
    with pytest.raises(RevisionConflictError):
        service.update_comment_settings(payload)


def test_not_published_resolution_is_audited_and_enqueues_a_fresh_prepare(service):
    planned = service.plan_campaign("campaign", seed="resolution")
    root = next(row for row in planned["assignments"] if row["step_id"] == "root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting", "published_unverified"):
        root = service.store.update_assignment_status(root["assignment_id"], root["revision"], status)
    service.store.save_receipt(root["assignment_id"], {"status": "published_unverified"})
    class GenerationQueue(FakeCampaignQueue):
        def enqueue_prepare_generation(self, campaign_id, generation):
            self.calls.append(("prepare_generation", campaign_id, generation))
            return {"id": f"campaign-prepare-{campaign_id}-g{generation}"}

    queue = GenerationQueue()
    service._queue_coordinator = queue

    result = service.resolve_unverified("campaign", root["assignment_id"], root["revision"], "not_published", "checked manually")

    assert result["assignment"]["status"] == "waiting_dependency"
    assert queue.calls == [("prepare_generation", "campaign", 1)]
    attempts = service.store.list_attempts("campaign")
    assert attempts[-1]["stage"] == "operator_resolution"


def test_profiles_can_switch_roles_across_campaigns_without_history(service):
    first = service.plan_campaign("campaign", seed="seed-a")
    refs = first["campaign"]["profile_refs"]
    service.store.upsert_profile_metadata(profile_ref=refs[1], expected_username="bob", enabled=True, login_verified=True, tags=["second-root"], language="en", region="", cooldown_until=None, health_status="healthy")
    second_template = _template()
    second_template["name"] = "switched"
    second_template["steps"][0]["required_profile_tags"] = ["second-root"]
    service.create_template(second_template, "second-template")
    service.create_campaign({"name": "second", "mode": "threaded", "target_source": "manual_url", "target_reference": "https://www.tiktok.com/@owner/video/87654321", "template_id": "second-template", "profile_refs": refs}, "second-campaign")
    second = service.plan_campaign("second-campaign", seed="seed-b")
    first_roles = {row["profile_ref"]: row["role"] for row in first["assignments"]}
    second_roles = {row["profile_ref"]: row["role"] for row in second["assignments"]}
    assert first_roles[refs[0]] == "owner" and second_roles[refs[0]] == "participant"
    assert first_roles[refs[1]] == "participant" and second_roles[refs[1]] == "owner"


def test_planning_failure_and_stale_lock_leave_persisted_plan_unchanged(service):
    service._content_resolver = lambda _library_id: [{"content_item_id": "one", "text": "one"}]
    with pytest.raises(CampaignError, match="allocation_unsatisfied"):
        service.plan_campaign("campaign", seed="broken")
    assert service.store.list_assignments("campaign") == []
    assert service.store.get_campaign("campaign")["status"] == "draft"

    service._content_resolver = lambda _library_id: [{"content_item_id": "two", "text": "two"}]
    planned = service.plan_campaign("campaign", seed="first")
    service.reallocate_campaign("campaign", seed="second", expected_revision=planned["campaign"]["revision"])
    before = service.store.list_assignments("campaign")
    with pytest.raises(RevisionConflictError):
        service.lock_plan("campaign", planned["campaign"]["revision"])
    assert service.store.get_campaign("campaign")["status"] == "planned"
    assert service.store.list_assignments("campaign") == before


def test_lock_rechecks_profile_and_rolls_back_every_assignment(service):
    planned = service.plan_campaign("campaign", seed="seed")
    root = next(row for row in planned["assignments"] if row["role"] == "owner")
    metadata = service.store.get_profile_metadata(root["profile_ref"])
    service.store.upsert_profile_metadata(**{**metadata, "health_status": "unhealthy"})
    before = service.store.list_assignments("campaign")
    with pytest.raises(CampaignError, match="allocation_unsatisfied"):
        service.lock_plan("campaign", planned["campaign"]["revision"])
    assert service.store.get_campaign("campaign")["status"] == "planned"
    assert service.store.list_assignments("campaign") == before


def test_lock_rejects_incomplete_assignment_set_without_writing_lock(service):
    from sqlalchemy import delete
    from comment_campaign.models import CommentAssignmentRecord

    planned = service.plan_campaign("campaign", seed="seed")
    with service.store.session_factory.begin() as session:
        session.execute(delete(CommentAssignmentRecord).where(CommentAssignmentRecord.id == planned["assignments"][0]["assignment_id"]))
    with pytest.raises(CampaignError, match="allocation_unsatisfied"):
        service.lock_plan("campaign", planned["campaign"]["revision"])
    campaign = service.store.get_campaign("campaign")
    assert campaign["status"] == "planned"
    assert campaign["locked_at"] is None
    assert all(not row["locked_at"] for row in service.store.list_assignments("campaign"))


@pytest.mark.parametrize("tamper", ["role", "parent", "position", "content"])
def test_lock_rejects_tampered_assignment_structure_and_content(service, tamper):
    import json

    from sqlalchemy import update
    from comment_campaign.models import CommentAssignmentRecord, CommentCampaignRecord

    planned = service.plan_campaign("campaign", seed="seed")
    assignments = {row["step_id"]: row for row in planned["assignments"]}
    with service.store.session_factory.begin() as session:
        if tamper == "role":
            session.execute(update(CommentAssignmentRecord).where(CommentAssignmentRecord.id == assignments["root"]["assignment_id"]).values(role="participant"))
        elif tamper == "parent":
            session.execute(update(CommentAssignmentRecord).where(CommentAssignmentRecord.id == assignments["reply"]["assignment_id"]).values(parent_assignment_id=None))
        elif tamper == "position":
            session.execute(update(CommentAssignmentRecord).where(CommentAssignmentRecord.id == assignments["root"]["assignment_id"]).values(position=2))
        else:
            campaign = service.store.get_campaign("campaign")
            content = campaign["content_snapshot"]
            content["steps"][1]["resolved_text"] = "tampered"
            session.execute(update(CommentCampaignRecord).where(CommentCampaignRecord.id == "campaign").values(content_snapshot_json=json.dumps(content, sort_keys=True, separators=(",", ":"))))
    with pytest.raises(CampaignError, match="allocation_unsatisfied"):
        service.lock_plan("campaign", planned["campaign"]["revision"])
    campaign = service.store.get_campaign("campaign")
    assert campaign["status"] == "planned"
    assert campaign["locked_at"] is None
    assert all(not row["locked_at"] for row in service.store.list_assignments("campaign"))


def test_profile_listing_includes_unconfigured_safe_identities(service):
    rows = service.list_profile_metadata()

    assert len(rows) == 2
    assert all(row["profile_ref"].startswith("profile_ref_") for row in rows)
    assert all(row["configured"] is True for row in rows)
    assert all("raw_profile_id" not in row for row in rows)


def test_create_campaign_validates_the_selected_template_revision(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'selected-revision.db'}")
    store.initialize()
    campaign_service = CommentCampaignService(store)
    independent = _template()
    independent["supported_modes"] = ["independent"]
    independent["steps"] = [independent["steps"][0]]
    independent["steps"][0]["required_profile_tags"] = []
    campaign_service.create_template(independent, "versioned-template")
    threaded = _template()
    campaign_service.update_template(
        "versioned-template", {**threaded, "expected_revision": 1}
    )
    profile_ref = store.sync_profile_identities(
        [{"id": "raw-versioned", "name": "Versioned", "status": "active"}]
    )[0]["profile_ref"]

    with pytest.raises(CampaignError, match="template_invalid"):
        campaign_service.create_campaign(
            {
                "name": "wrong revision",
                "mode": "threaded",
                "target_source": "manual_url",
                "target_reference": "https://www.tiktok.com/@owner/video/12345678",
                "template_id": "versioned-template",
                "template_revision": 1,
                "profile_refs": [profile_ref],
            }
        )


def test_assignment_override_is_cas_eligible_and_invalidates_campaign_lock(service):
    extra_refs = [row["profile_ref"] for row in service.store.sync_profile_identities(
        [
            {"id": "raw-third", "name": "Third", "status": "active"},
            {"id": "raw-fourth", "name": "Fourth", "status": "active"},
        ]
    )]
    for index, profile_ref in enumerate(extra_refs, start=3):
        service.store.upsert_profile_metadata(
            profile_ref=profile_ref, expected_username=f"user-{index}", enabled=True,
            login_verified=True, tags=["root"], language="en", region="",
            cooldown_until=None, health_status="healthy",
        )
    original = service.store.get_campaign("campaign")
    campaign = service.create_campaign(
        {
            "name": "override", "mode": "threaded", "target_source": "manual_url",
            "target_reference": "https://www.tiktok.com/@owner/video/23456789",
            "template_id": "template", "profile_refs": [*original["profile_refs"], *extra_refs],
        },
        "override-campaign",
    )
    planned = service.plan_campaign("override-campaign", seed="override")
    root = next(row for row in planned["assignments"] if row["step_id"] == "root")
    used = {row["profile_ref"] for row in planned["assignments"]}
    replacement = next(
        ref for ref in [original["profile_refs"][0], *extra_refs] if ref not in used
    )

    changed = service.override_assignment(
        "override-campaign", root["assignment_id"],
        {"expected_revision": root["revision"], "profile_ref": replacement},
    )

    assert changed["assignment"]["profile_ref"] == replacement
    assert changed["campaign"]["revision"] == campaign["revision"] + 2
    with pytest.raises(RevisionConflictError):
        service.override_assignment(
            "override-campaign", root["assignment_id"],
            {"expected_revision": root["revision"], "profile_ref": replacement},
        )
    with pytest.raises(CampaignError, match="allocation_unsatisfied"):
        service.override_assignment(
            "override-campaign", changed["assignment"]["assignment_id"],
            {"expected_revision": changed["assignment"]["revision"], "profile_ref": "missing"},
        )
    locked = service.lock_plan("override-campaign", changed["campaign"]["revision"])
    with pytest.raises(RevisionConflictError):
        service.override_assignment(
            "override-campaign", changed["assignment"]["assignment_id"],
            {"expected_revision": changed["assignment"]["revision"], "profile_ref": replacement},
        )
    assert locked["status"] == "awaiting_campaign_approval"


def test_campaign_approval_persists_queued_state_before_enqueue(service):
    queue = FakeCampaignQueue()
    service._queue_coordinator = queue
    planned = service.plan_campaign("campaign", seed="seed")
    locked = service.lock_plan("campaign", planned["campaign"]["revision"])

    approved = service.approve_campaign("campaign", locked["revision"])

    assert approved["campaign"]["status"] == "queued"
    assert queue.calls == [("prepare", "campaign")]


def test_submit_approval_is_durable_before_enqueue_and_does_not_expose_token(service):
    queue = FakeCampaignQueue()
    service._queue_coordinator = queue
    planned = service.plan_campaign("campaign", seed="seed")
    assignment_id = planned["assignments"][0]["assignment_id"]
    assignment = service.store.get_assignment(assignment_id)
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        assignment = service.store.update_assignment_status(
            assignment_id, assignment["revision"], status
        )
    campaign = service.store.get_campaign("campaign")
    for status in ("awaiting_campaign_approval", "queued", "running"):
        campaign = service.store.transition_campaign_status(
            "campaign", campaign["revision"], status
        )

    result = service.approve_submit("campaign", assignment_id, assignment["revision"])

    assert result["approval"]["assignment_id"] == assignment_id
    assert result["approval"]["revision"] == assignment["revision"]
    assert "token" not in repr(result)
    assert queue.calls == [("submit", "campaign", assignment_id, assignment["revision"])]
    persisted = service.store.list_approvals("campaign")
    assert persisted[0]["assignment_id"] == assignment_id
    assert "token" not in repr(persisted)


def test_submit_enqueue_failure_leaves_durable_unconsumed_approval_for_safe_retry(service):
    queue = FakeCampaignQueue(fail_submit=True)
    service._queue_coordinator = queue
    planned = service.plan_campaign("campaign", seed="seed")
    assignment_id = planned["assignments"][0]["assignment_id"]
    assignment = service.store.get_assignment(assignment_id)
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        assignment = service.store.update_assignment_status(assignment_id, assignment["revision"], status)
    campaign = service.store.get_campaign("campaign")
    for status in ("awaiting_campaign_approval", "queued", "running"):
        campaign = service.store.transition_campaign_status(
            "campaign", campaign["revision"], status
        )

    with pytest.raises(CampaignError, match="worker_unavailable"):
        service.approve_submit("campaign", assignment_id, assignment["revision"])
    approval = service.store.get_approval(assignment_id, assignment["revision"])
    assert approval is not None
    assert approval["consumed_at"] is None
    assert service.store.get_assignment(assignment_id)["status"] == "awaiting_step_approval"

    retry_queue = FakeCampaignQueue()
    service._queue_coordinator = retry_queue
    service.approve_submit("campaign", assignment_id, assignment["revision"])
    assert retry_queue.calls == [("submit", "campaign", assignment_id, assignment["revision"])]


def test_health_probes_each_dependency_independently_without_profile_discovery(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'health.db'}")
    store.initialize()

    class Redis:
        def ping(self): return True
        def get(self, _key): return b"worker:123"
        def ttl(self, _key): return 12

    class Queue:
        redis = Redis()

    service = CommentCampaignService(
        store, queue_coordinator=Queue(),
        profile_provider=lambda: (_ for _ in ()).throw(AssertionError("health must not list all profiles")),
        adspower_probe=lambda: [],
    )
    assert {name: value["status"] for name, value in service.health().items()} == {
        "sqlite": "connected", "redis": "connected", "worker": "connected", "adspower": "connected",
    }

    unavailable = CommentCampaignService(store, queue_coordinator=Queue(), adspower_probe=lambda: (_ for _ in ()).throw(RuntimeError()))
    projection = unavailable.health()
    assert projection["sqlite"]["status"] == projection["redis"]["status"] == "connected"
    assert projection["adspower"]["status"] == "unavailable"


@pytest.mark.parametrize(
    ("owner", "ttl"),
    [(None, 12), (b"invalid-owner", 12), (b"worker:123", 0), (b"worker:123", -1)],
)
def test_health_never_marks_an_invalid_or_expired_worker_heartbeat_connected(
    tmp_path, owner, ttl
):
    store = CampaignStore(f"sqlite:///{tmp_path / 'worker-health.db'}")
    store.initialize()

    class Redis:
        def ping(self):
            return True

        def get(self, _key):
            return owner

        def ttl(self, _key):
            return ttl

    service = CommentCampaignService(
        store,
        queue_coordinator=type("Queue", (), {"redis": Redis()})(),
        adspower_probe=lambda: [],
    )
    projection = service.health()

    assert projection["redis"]["status"] == "connected"
    assert projection["worker"]["status"] == "unavailable"


def test_health_error_messages_never_echo_dependency_secrets(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'secret-health.db'}")
    store.initialize()

    class Redis:
        def ping(self):
            raise RuntimeError("redis://user:secret@host cookie Authorization")

    service = CommentCampaignService(
        store,
        queue_coordinator=type("Queue", (), {"redis": Redis()})(),
        adspower_probe=lambda: (_ for _ in ()).throw(
            RuntimeError("api_key=secret wss://private")
        ),
    )
    rendered = repr(service.health()).casefold()

    for forbidden in ("redis://", "api_key", "wss://", "authorization", "cookie"):
        assert forbidden not in rendered
