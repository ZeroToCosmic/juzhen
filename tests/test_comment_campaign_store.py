from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
import pytest

from comment_campaign.errors import CampaignNotFoundError, CampaignValidationError, RevisionConflictError
from comment_campaign.store import CampaignStore


def _template(name="thread"):
    return {
        "name": name,
        "description": "",
        "supported_modes": ["threaded"],
        "language": "en",
        "tags": ["test"],
        "steps": [
            {"id": "root", "label": "root", "content_source": "fixed", "fixed_text": "hello", "content_library_id": "", "content_item_id": "", "parent_step_id": None, "required_profile_tags": [], "excluded_profile_tags": [], "language": "en"},
            {"id": "reply", "label": "reply", "content_source": "fixed", "fixed_text": "hi", "content_library_id": "", "content_item_id": "", "parent_step_id": "root", "required_profile_tags": [], "excluded_profile_tags": [], "language": "en"},
        ],
    }


def _campaign():
    return {
        "name": "campaign", "mode": "threaded", "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": "template-1", "template_revision": 1,
        "profile_refs": ["profile_ref_a", "profile_ref_b"], "batch_size": 3,
        "allocation_seed": "seed", "start_mode": "manual", "scheduled_at": "",
    }


def _profile_refs(store):
    rows = store.sync_profile_identities([
        {"id": "raw-profile-a", "name": "A", "status": "active"},
        {"id": "raw-profile-b", "name": "B", "status": "active"},
    ])
    return [row["profile_ref"] for row in rows]


@pytest.fixture
def store(tmp_path):
    result = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    result.initialize()
    return result


def test_profile_identity_mapping_is_random_persistent_and_redacted(store):
    first = store.sync_profile_identities([{"id": "raw-adspower-secret", "name": "Alice", "status": "active"}])
    second = store.sync_profile_identities([{"id": "raw-adspower-secret", "name": "Alice", "status": "active"}])
    assert first[0]["profile_ref"].startswith("profile_ref_")
    assert first == second
    assert "raw-adspower-secret" not in repr(first)
    assert store.get_raw_profile_id(first[0]["profile_ref"]) == "raw-adspower-secret"


def test_close_disposes_campaign_store_engine(store, monkeypatch):
    disposed = []
    monkeypatch.setattr(store.engine, "dispose", lambda: disposed.append(True))

    store.close()

    assert disposed == [True]


def test_profile_list_includes_unconfigured_safe_identity(store):
    store.sync_profile_identities(
        [{"id": "raw-adspower-secret", "name": "Alice", "status": "active"}]
    )

    rows = store.list_comment_profiles()

    assert rows == [{
        "profile_ref": rows[0]["profile_ref"],
        "display_profile": rows[0]["display_profile"],
        "name": "Alice",
        "status": "active",
        "configured": False,
        "expected_username": "",
        "enabled": False,
        "login_verified": False,
        "tags": [],
        "language": "",
        "region": "",
        "cooldown_until": None,
        "health_status": "unknown",
    }]
    assert "raw-adspower-secret" not in repr(rows)


def test_profile_metadata_is_identity_backed_and_never_has_role(store):
    with pytest.raises(CampaignNotFoundError):
        store.upsert_profile_metadata(profile_ref="profile_ref_a", expected_username="alice", enabled=True, login_verified=True, tags=["en"], language="en", region="US", cooldown_until=None, health_status="healthy")
    profile_ref = store.sync_profile_identities([{"id": "raw-a", "name": "Alice", "status": "active"}])[0]["profile_ref"]
    row = store.upsert_profile_metadata(profile_ref=profile_ref, expected_username="alice", enabled=True, login_verified=True, tags=["en"], language="en", region="US", cooldown_until=None, health_status="healthy")
    assert "role" not in row
    assert row["profile_ref"] == profile_ref


def test_template_revisions_are_immutable_and_revision_guarded(store):
    created = store.create_template(_template(), "template-1")
    changed = _template("changed")
    updated = store.update_template("template-1", created["revision"], changed)
    assert updated["revision"] == 2
    assert store.get_template("template-1", revision=1)["name"] == "thread"
    assert store.get_template("template-1", revision=2)["name"] == "changed"
    with pytest.raises(RevisionConflictError):
        store.update_template("template-1", 1, changed)


def test_step_parent_constraint_is_deferred_for_ui_ordered_templates(store):
    payload = _template()
    payload["steps"] = [payload["steps"][1], payload["steps"][0]]
    created = store.create_template(payload, "template-1")
    assert [step["id"] for step in created["steps"]] == ["reply", "root"]


def test_campaign_assignment_cas_and_recovery_never_replays_submission(store):
    store.create_template(_template(), "template-1")
    store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    profile_a, profile_b = _profile_refs(store)
    rows = store.replace_assignments("campaign-1", [
        {"assignment_id": "assignment-root", "step_id": "root", "profile_ref": profile_a, "role": "owner", "resolved_text": "hello", "position": 1},
        {"assignment_id": "assignment-reply", "step_id": "reply", "profile_ref": profile_b, "role": "participant", "resolved_text": "hi", "parent_assignment_id": "assignment-root", "position": 2},
    ])
    root = rows[0]
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting"):
        root = store.update_assignment_status("assignment-root", root["revision"], status)
    reply = rows[1]
    for status in ("waiting_dependency", "opening_profile", "locating_video", "locating_parent", "preparing_comment", "awaiting_step_approval", "submitting", "verifying_receipt"):
        reply = store.update_assignment_status("assignment-reply", reply["revision"], status)
    assert store.recover_interrupted_submissions() == 2
    assert store.recover_interrupted_submissions() == 0
    recovered = {row["assignment_id"]: row for row in store.list_assignments("campaign-1")}
    assert {row["status"] for row in recovered.values()} == {"published_unverified"}
    with pytest.raises(RevisionConflictError):
        store.update_assignment_status("assignment-root", root["revision"], "published_unverified")


def test_terminal_failure_and_branch_pause_roll_back_together(store, monkeypatch):
    store.create_template(_template(), "template-1")
    store.create_campaign(
        _campaign(), "campaign-1", "12345678",
        "https://www.tiktok.com/@owner/video/12345678",
    )
    profile_a, profile_b = _profile_refs(store)
    rows = store.replace_assignments("campaign-1", [
        {"assignment_id": "assignment-root", "step_id": "root", "profile_ref": profile_a, "role": "owner", "resolved_text": "hello", "position": 1},
        {"assignment_id": "assignment-child", "step_id": "reply", "profile_ref": profile_b, "role": "participant", "resolved_text": "hi", "parent_assignment_id": "assignment-root", "position": 2},
    ])
    root = store.update_assignment_status("assignment-root", rows[0]["revision"], "opening_profile")

    def injected_failure(*_args, **_kwargs):
        raise RuntimeError("injected branch pause failure")

    monkeypatch.setattr(store, "_pause_descendants_in_session", injected_failure)
    with pytest.raises(RuntimeError, match="injected branch pause failure"):
        store.fail_assignment_and_pause_descendants(
            "assignment-root", root["revision"], "failed", "profile_start_failed"
        )

    assert store.get_assignment("assignment-root")["status"] == "opening_profile"
    assert store.get_assignment("assignment-child")["status"] == "planned"


def test_final_submit_cas_rejects_after_campaign_is_paused(store):
    store.create_template(_template(), "template-1")
    campaign = store.create_campaign(
        _campaign(), "campaign-1", "12345678",
        "https://www.tiktok.com/@owner/video/12345678",
    )
    profile_a, _profile_b = _profile_refs(store)
    assignment = store.replace_assignments("campaign-1", [{
        "assignment_id": "assignment-root", "step_id": "root",
        "profile_ref": profile_a, "role": "owner", "resolved_text": "hello",
    }])[0]
    for status in ("planned", "awaiting_campaign_approval", "queued", "running"):
        campaign = store.transition_campaign_status("campaign-1", campaign["revision"], status)
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        assignment = store.update_assignment_status("assignment-root", assignment["revision"], status)
    store.create_submit_approval("campaign-1", "assignment-root", assignment["revision"], "private")
    store.consume_submit_approval("campaign-1", "assignment-root", assignment["revision"])
    store.transition_campaign_status("campaign-1", campaign["revision"], "paused", pause_reason="operator")

    with pytest.raises(CampaignValidationError, match="approval_revision_mismatch"):
        store.begin_submitting("campaign-1", "assignment-root", assignment["revision"])
    assert store.get_assignment("assignment-root")["status"] == "awaiting_step_approval"


def test_reject_submit_pauses_only_the_assignment_branch_with_exact_revision(store):
    store.create_template(_template(), "template-1")
    store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    profile_a, profile_b = _profile_refs(store)
    rows = store.replace_assignments("campaign-1", [
        {"assignment_id": "root", "step_id": "root", "profile_ref": profile_a, "role": "owner", "resolved_text": "hello"},
        {"assignment_id": "child", "step_id": "reply", "profile_ref": profile_b, "role": "participant", "resolved_text": "hi", "parent_assignment_id": "root"},
    ])
    root = rows[0]
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        root = store.update_assignment_status("root", root["revision"], status)

    paused = store.reject_submit_and_pause_descendants("campaign-1", "root", root["revision"], "operator rejected")

    assert paused["status"] == "paused"
    saved = {row["assignment_id"]: row for row in store.list_assignments("campaign-1")}
    assert saved["root"]["error_code"] == "approval_rejected"
    assert saved["child"]["status"] == "paused_dependency"
    with pytest.raises(RevisionConflictError):
        store.reject_submit_and_pause_descendants("campaign-1", "root", root["revision"], "stale")


def test_resolve_unverified_requires_durable_receipt_and_never_replays_submit(store):
    store.create_template(_template(), "template-1")
    store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    profile_a, _profile_b = _profile_refs(store)
    assignment = store.replace_assignments("campaign-1", [{
        "assignment_id": "root", "step_id": "root", "profile_ref": profile_a,
        "role": "owner", "resolved_text": "hello",
    }])[0]
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting", "published_unverified"):
        assignment = store.update_assignment_status("root", assignment["revision"], status)
    with pytest.raises(CampaignValidationError, match="comment_receipt_unverified"):
        store.resolve_unverified_assignment("campaign-1", "root", assignment["revision"], "published", "operator checked")
    store.save_receipt("root", {"status": "published_unverified"})

    resolved = store.resolve_unverified_assignment("campaign-1", "root", assignment["revision"], "published", "operator checked")

    assert resolved["status"] == "published_verified"
    assert store.list_receipts("campaign-1")[0]["status"] == "published_verified"


def test_campaign_summary_counts_unverified_publish_as_abnormal(store):
    store.create_template(_template(), "template-1")
    store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    profile_a, _profile_b = _profile_refs(store)
    assignment = store.replace_assignments("campaign-1", [{
        "assignment_id": "root", "step_id": "root", "profile_ref": profile_a,
        "role": "owner", "resolved_text": "hello",
    }])[0]
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting", "published_unverified"):
        assignment = store.update_assignment_status("root", assignment["revision"], status)

    [summary] = store.list_campaigns(None, 10, 0)

    assert summary["abnormal_assignment_count"] == 1


def test_sqlite_foreign_keys_and_wal_are_enabled(store):
    with store.engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar_one().lower() == "wal"
        assert connection.execute(text("PRAGMA busy_timeout")).scalar_one() == 5000
        with pytest.raises(Exception):
            connection.execute(text("INSERT INTO comment_profile_metadata(profile_ref, expected_username, enabled, login_verified, tags_json, language, region, cooldown_until, health_status, created_at, updated_at) VALUES ('missing', '', 1, 1, '[]', '', '', '', 'healthy', 'now', 'now')"))


def test_template_update_and_disable_use_revision_compare_and_swap(store):
    created = store.create_template(_template(), "template-1")
    updated = store.update_template("template-1", created["revision"], _template("once"))
    with pytest.raises(RevisionConflictError):
        store.disable_template("template-1", created["revision"])
    disabled = store.disable_template("template-1", updated["revision"])
    assert disabled["enabled"] is False
    assert disabled["revision"] == 3
    assert store.get_template("template-1", revision=3)["steps"][1]["parent_step_id"] == "root"


def test_assignment_parent_cannot_cross_campaign(store):
    store.create_template(_template(), "template-1")
    store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    other = _campaign()
    other["name"] = "other"
    store.create_campaign(other, "campaign-2", "87654321", "https://www.tiktok.com/@owner/video/87654321")
    profile_a, profile_b = _profile_refs(store)
    store.replace_assignments("campaign-1", [{"assignment_id": "assignment-root", "step_id": "root", "profile_ref": profile_a, "role": "owner", "resolved_text": "hello"}])
    with pytest.raises(IntegrityError):
        store.replace_assignments("campaign-2", [{"assignment_id": "assignment-child", "step_id": "root", "profile_ref": profile_b, "role": "owner", "resolved_text": "hello", "parent_assignment_id": "assignment-root"}])


def test_identity_sync_preserves_other_profiles_when_one_is_already_known(store):
    first = store.sync_profile_identities([{"id": "raw-shared", "name": "A", "status": "active"}])
    rows = store.sync_profile_identities([
        {"id": "raw-shared", "name": "A", "status": "active"},
        {"id": "raw-new", "name": "B", "status": "active"},
    ])
    assert rows[0]["profile_ref"] == first[0]["profile_ref"]
    assert len(rows) == 2
    assert "raw-shared" not in repr(rows)
    assert "raw-new" not in repr(rows)
    with pytest.raises(ValueError):
        store.sync_profile_identities([{"id": "raw-extra", "name": "C", "status": "active", "extra": "no"}])


def test_campaign_status_cas_checks_stale_revision_before_transition(store):
    store.create_template(_template(), "template-1")
    campaign = store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    planned = store.transition_campaign_status("campaign-1", campaign["revision"], "planned")
    with pytest.raises(RevisionConflictError):
        store.transition_campaign_status("campaign-1", campaign["revision"], "planned")
    assert planned["status"] == "planned"


def test_assignment_rejects_profile_ref_without_identity(store):
    store.create_template(_template(), "template-1")
    store.create_campaign(_campaign(), "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
    with pytest.raises(IntegrityError):
        store.replace_assignments("campaign-1", [{"assignment_id": "assignment-root", "step_id": "root", "profile_ref": "missing-profile", "role": "owner", "resolved_text": "hello"}])


def test_campaign_requires_the_referenced_template_revision(store):
    store.create_template(_template(), "template-1")
    missing_revision = _campaign()
    missing_revision["template_revision"] = 99
    with pytest.raises(CampaignNotFoundError):
        store.create_campaign(missing_revision, "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678")
