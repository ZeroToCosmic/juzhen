import json
import sqlite3
import threading

from sqlalchemy import event, text
from comment_campaign.models import CommentProfileIdentityRecord
from sqlalchemy.exc import IntegrityError
import pytest

from comment_campaign.errors import (
    CampaignNotFoundError,
    CampaignValidationError,
    DuplicateTikTokAccountError,
    RevisionConflictError,
    StateTransitionError,
)
from comment_campaign.models import CommentTemplateRecord
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


def test_profile_list_sync_creates_safe_default_metadata(store):
    store.sync_profile_identities(
        [{"id": "raw-adspower-secret", "name": "Alice", "status": "active"}]
    )

    rows = store.list_comment_profiles()

    [row] = rows
    assert {key: row[key] for key in (
        "profile_ref", "display_profile", "name", "status", "configured",
        "expected_username", "enabled", "login_verified", "tags", "language",
        "region", "cooldown_until", "health_status",
    )} == {
        "profile_ref": row["profile_ref"], "display_profile": "Alice",
        "name": "Alice", "status": "active", "configured": True,
        "expected_username": "", "enabled": True, "login_verified": False,
        "tags": [], "language": "", "region": "", "cooldown_until": None,
        "health_status": "healthy",
    }
    assert "raw-adspower-secret" not in repr(rows)


def test_sync_backfills_only_currently_discovered_missing_profile_metadata(store):
    now = "2026-08-11T00:00:00+00:00"
    with store.session_factory.begin() as session:
        session.add(CommentProfileIdentityRecord(
            profile_ref="profile_ref_history", raw_adspower_id="raw-history-not-returned",
            display_profile="History", name="History", status="active",
            created_at=now, updated_at=now,
        ))
    existing = store.sync_profile_identities([
        {"id": "raw-existing", "name": "Existing", "status": "active"},
    ])[0]["profile_ref"]
    store.upsert_profile_metadata(
        profile_ref=existing, expected_username="", enabled=False,
        login_verified=False, tags=["manual"], language="", region="",
        cooldown_until="2026-08-12T00:00:00+00:00", health_status="unhealthy",
    )

    store.sync_profile_identities([
        {"id": "raw-new", "name": "New", "status": "active"},
        {"id": "raw-existing", "name": "Existing", "status": "active"},
    ])

    rows = {row["profile_ref"]: row for row in store.list_comment_profiles()}
    assert "profile_ref_history" not in rows
    assert rows[existing]["enabled"] is False
    assert rows[existing]["tags"] == ["manual"]
    assert rows[existing]["health_status"] == "unhealthy"
    new = next(row for row in rows.values() if row["name"] == "New")
    assert new["configured"] is True
    assert new["enabled"] is True
    assert new["login_verified"] is False
    assert new["expected_username"] == ""
    assert new["health_status"] == "healthy"


def test_initialize_never_backfills_historical_identity_metadata_and_cache_time_uses_identity(store):
    now = "2026-08-11T00:00:00+00:00"
    with store.session_factory.begin() as session:
        session.add(CommentProfileIdentityRecord(
            profile_ref="profile_ref_history", raw_adspower_id="raw-history-only",
            display_profile="History", name="History", status="active",
            created_at=now, updated_at=now,
        ))
    store.initialize()

    assert store.get_profile_metadata("profile_ref_history") is None
    assert store.profile_cache_last_synced_at() == now


def test_empty_profile_name_uses_fixed_display_without_profile_ref_suffix(store):
    [row] = store.sync_profile_identities([
        {"id": "raw-no-name", "name": "", "status": "active"},
    ])

    assert row["display_profile"] == "未命名 Profile"
    assert row["profile_ref"] not in row["display_profile"]


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


def test_template_lifecycle_is_revision_guarded_and_deleted_is_hidden(store):
    created = store.create_template(_template(), "template-1")
    assert created["lifecycle_status"] == "enabled"
    disabled = store.disable_template("template-1", created["revision"])
    assert disabled["lifecycle_status"] == "disabled"
    enabled = store.enable_template("template-1", disabled["revision"])
    disabled = store.disable_template("template-1", enabled["revision"])
    deleted = store.delete_template("template-1", disabled["revision"])

    assert deleted["lifecycle_status"] == "deleted"
    assert deleted["revision"] == 5
    assert store.get_template_lifecycle("template-1") == "deleted"
    assert store.list_templates() == []
    assert store.get_template("template-1") is None
    assert store.get_template("template-1", revision=1)["steps"][0]["id"] == "root"
    assert store.get_template("template-1", revision=5)["lifecycle_status"] == "deleted"
    with store.engine.connect() as connection:
        for revision in range(1, 6):
            definitions = [
                json.loads(value)
                for value in connection.execute(text(
                    "SELECT definition_json FROM comment_steps "
                    "WHERE template_id='template-1' AND template_revision=:revision "
                    "ORDER BY position"
                ), {"revision": revision}).scalars()
            ]
            assert [step["id"] for step in definitions] == ["root", "reply"]
            assert [step["fixed_text"] for step in definitions] == ["hello", "hi"]


def test_template_lifecycle_error_priority_and_allowed_states(store):
    with pytest.raises(CampaignNotFoundError):
        store.enable_template("missing", 1)
    with pytest.raises(CampaignNotFoundError):
        store.delete_template("missing", 1)

    created = store.create_template(_template(), "template-1")
    with pytest.raises(StateTransitionError) as enabled_error:
        store.enable_template("template-1", created["revision"])
    assert enabled_error.value.code == "invalid_state_transition"
    assert (enabled_error.value.current, enabled_error.value.target) == (
        "enabled", "enabled"
    )
    with pytest.raises(StateTransitionError) as delete_error:
        store.delete_template("template-1", created["revision"])
    assert delete_error.value.code == "invalid_state_transition"
    assert (delete_error.value.current, delete_error.value.target) == (
        "enabled", "deleted"
    )

    disabled = store.disable_template("template-1", created["revision"])
    with pytest.raises(RevisionConflictError):
        store.disable_template("template-1", created["revision"])
    with pytest.raises(RevisionConflictError):
        store.enable_template("template-1", created["revision"])
    with pytest.raises(RevisionConflictError):
        store.update_template("template-1", created["revision"], _template("stale"))
    with pytest.raises(StateTransitionError) as disable_error:
        store.disable_template("template-1", disabled["revision"])
    assert disable_error.value.code == "invalid_state_transition"
    assert (disable_error.value.current, disable_error.value.target) == (
        "disabled", "disabled"
    )
    with pytest.raises(StateTransitionError) as update_error:
        store.update_template("template-1", disabled["revision"], _template("blocked"))
    assert update_error.value.code == "invalid_state_transition"
    assert (update_error.value.current, update_error.value.target) == (
        "disabled", "enabled"
    )

    deleted = store.delete_template("template-1", disabled["revision"])
    for operation in (
        lambda: store.disable_template("template-1", deleted["revision"]),
        lambda: store.enable_template("template-1", deleted["revision"]),
        lambda: store.delete_template("template-1", deleted["revision"]),
        lambda: store.update_template(
            "template-1", deleted["revision"], _template("deleted")
        ),
    ):
        with pytest.raises(CampaignNotFoundError):
            operation()


def test_template_lifecycle_revision_write_failure_rolls_back(store, monkeypatch):
    created = store.create_template(_template(), "template-1")
    disabled = store.disable_template("template-1", created["revision"])

    def fail_revision(*_args, **_kwargs):
        raise RuntimeError("injected revision failure")

    monkeypatch.setattr(store, "_add_template_revision", fail_revision)
    with pytest.raises(RuntimeError, match="injected revision failure"):
        store.enable_template("template-1", disabled["revision"])

    current = store.get_template("template-1")
    assert current["revision"] == disabled["revision"]
    assert current["lifecycle_status"] == "disabled"
    assert store.get_template("template-1", revision=disabled["revision"] + 1) is None


def test_old_template_snapshots_map_lifecycle_from_enabled(store):
    store.create_template(_template(), "template-1")
    with store.engine.begin() as connection:
        snapshot = json.loads(connection.execute(text(
            "SELECT snapshot_json FROM comment_template_revisions "
            "WHERE template_id='template-1' AND revision=1"
        )).scalar_one())
        snapshot.pop("lifecycle_status", None)
        snapshot["enabled"] = False
        connection.execute(text(
            "UPDATE comment_template_revisions SET snapshot_json=:snapshot "
            "WHERE template_id='template-1' AND revision=1"
        ), {"snapshot": json.dumps(snapshot)})

    historical = store.get_template("template-1", revision=1)
    assert historical["lifecycle_status"] == "disabled"


def test_template_deleted_constraint_is_named_and_enforced_by_sqlite(store):
    names = {constraint.name for constraint in CommentTemplateRecord.__table__.constraints}
    assert "ck_comment_template_deleted_disabled" in names
    store.create_template(_template(), "template-1")

    with pytest.raises(IntegrityError):
        with store.engine.begin() as connection:
            connection.execute(text(
                "UPDATE comment_templates SET deleted_at='now' "
                "WHERE id='template-1' AND enabled=1"
            ))
    with store.engine.begin() as connection:
        connection.execute(text(
            "UPDATE comment_templates SET enabled=0, deleted_at='now' "
            "WHERE id='template-1'"
        ))


def test_old_sqlite_schema_gets_idempotent_deleted_at_migration(tmp_path):
    database_path = tmp_path / "old.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript("""
            CREATE TABLE comment_templates (
                id VARCHAR(120) PRIMARY KEY, name VARCHAR(100) NOT NULL,
                description TEXT NOT NULL, supported_modes_json TEXT NOT NULL,
                language VARCHAR(32) NOT NULL, tags_json TEXT NOT NULL,
                enabled BOOLEAN NOT NULL, revision INTEGER NOT NULL,
                created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL
            );
            CREATE TABLE comment_template_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_id VARCHAR(120) NOT NULL, revision INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
                UNIQUE(template_id, revision)
            );
            CREATE TABLE comment_steps (
                template_id VARCHAR(120) NOT NULL,
                template_revision INTEGER NOT NULL,
                step_id VARCHAR(120) NOT NULL,
                parent_step_id VARCHAR(120), position INTEGER NOT NULL,
                definition_json TEXT NOT NULL,
                PRIMARY KEY(template_id, template_revision, step_id)
            );
            INSERT INTO comment_templates VALUES
                ('old','Old','', '["threaded"]','en','[]',0,1,'now','now');
            INSERT INTO comment_template_revisions(template_id,revision,snapshot_json,created_at)
                VALUES ('old',1,'{"id":"old","revision":1,"name":"Old","description":"","supported_modes":["threaded"],"language":"en","tags":[],"enabled":false,"steps":[{"id":"root"}]}','now');
            INSERT INTO comment_steps VALUES
                ('old',1,'root',NULL,1,'{"id":"root"}');
        """)
        connection.commit()
    finally:
        connection.close()

    migrated = CampaignStore(f"sqlite:///{database_path}")
    migrated.initialize()
    migrated.initialize()
    with migrated.engine.connect() as sql:
        columns = {row[1] for row in sql.exec_driver_sql(
            "PRAGMA table_info(comment_templates)"
        )}
        assert "deleted_at" in columns
        assert sql.execute(text(
            "SELECT COUNT(*) FROM comment_template_revisions WHERE template_id='old'"
        )).scalar_one() == 1
        assert sql.execute(text(
            "SELECT COUNT(*) FROM comment_steps WHERE template_id='old'"
        )).scalar_one() == 1
    assert migrated.get_template_lifecycle("old") == "disabled"
    assert migrated.get_template("old", revision=1)["lifecycle_status"] == "disabled"
    with pytest.raises(IntegrityError):
        with migrated.engine.begin() as sql:
            sql.execute(text(
                "UPDATE comment_templates SET enabled=1, deleted_at='now' WHERE id='old'"
            ))
    migrated.close()


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
    with pytest.raises(CampaignValidationError, match="approval_revision_mismatch"):
        store.create_submit_approval("campaign-1", "assignment-root", assignment["revision"], "private")
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


def _running_campaign_with_assignments(store):
    store.create_template(_template(), "template-1")
    campaign = store.create_campaign(
        _campaign(), "campaign-1", "12345678",
        "https://www.tiktok.com/@owner/video/12345678",
    )
    profile_a, profile_b = _profile_refs(store)
    assignments = store.replace_assignments("campaign-1", [
        {"assignment_id": "root", "step_id": "root", "profile_ref": profile_a,
         "display_profile": "Alpha", "role": "owner", "resolved_text": "root"},
        {"assignment_id": "child", "step_id": "reply", "profile_ref": profile_b,
         "display_profile": "Beta", "role": "participant", "resolved_text": "child",
         "parent_assignment_id": "root"},
    ])
    for status in ("planned", "awaiting_campaign_approval", "queued", "running"):
        campaign = store.transition_campaign_status("campaign-1", campaign["revision"], status)
    return campaign, assignments


def _identity_observations(assignments):
    return tuple({
        "assignment_id": assignment["assignment_id"],
        "profile_ref": assignment["profile_ref"],
        "account_key": f"canonical.{assignment['assignment_id']}",
        "visible_username": f"Visible {assignment['assignment_id']}",
        "canonical_href": f"https://www.tiktok.com/@canonical.{assignment['assignment_id']}",
        "observed_at": "2026-08-11T00:00:00Z",
        "target_video": {"video_id": "12345678", "canonical_url": "https://www.tiktok.com/@owner/video/12345678"},
        "element_binding": {"id": "account-binding", "revision": 2, "definition_sha256": "a" * 64},
    } for assignment in assignments)


def test_old_sqlite_identity_generation_migration_is_idempotent_and_keeps_prepare_generation(tmp_path):
    database_path = tmp_path / "old-identity.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript("""
            CREATE TABLE comment_campaigns (id TEXT PRIMARY KEY, prepare_generation INTEGER NOT NULL DEFAULT 7);
            CREATE TABLE comment_assignments (id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL);
        """)
        connection.commit()
    finally:
        connection.close()

    migrated = CampaignStore(f"sqlite:///{database_path}")
    migrated.initialize(); migrated.initialize()
    with migrated.engine.connect() as sql:
        campaign_columns = {row[1]: row for row in sql.exec_driver_sql("PRAGMA table_info(comment_campaigns)")}
        assignment_columns = {row[1]: row for row in sql.exec_driver_sql("PRAGMA table_info(comment_assignments)")}
    assert campaign_columns["identity_generation"][4] == "0"
    assert assignment_columns["identity_generation"][4] == "0"
    assert campaign_columns["prepare_generation"][4] == "7"


def test_freeze_campaign_identities_requires_the_full_assignment_profile_set_and_is_atomic(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    before = store.get_campaign("campaign-1")

    with pytest.raises(CampaignValidationError, match="tiktok_identity_unavailable"):
        store.freeze_campaign_identities(
            "campaign-1", campaign["revision"], 0, _identity_observations(assignments[:1])
        )

    assert store.get_campaign("campaign-1") == before
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    assert frozen["identity_generation"] == 1
    saved = {row["assignment_id"]: row for row in store.list_assignments("campaign-1")}
    assert saved["root"]["status"] == "planned"
    assert saved["child"]["status"] == "waiting_dependency"
    assert all(row["identity_generation"] == 1 for row in saved.values())
    assert saved["root"]["expected_username"] == "canonical.root"
    assert saved["root"]["evidence"]["account_preflight"]["identity_generation"] == 1


def test_freeze_campaign_identities_accepts_text_fallback_without_a_canonical_href(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    observations = list(_identity_observations(assignments))
    observations[0] = {**observations[0], "canonical_href": None}

    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, tuple(observations)
    )

    assert frozen["identity_generation"] == 1
    assert store.get_assignment("root")["evidence"]["account_preflight"]["canonical_href"] is None


def test_duplicate_identity_invalidation_is_sanitized_and_stale_cas_has_zero_writes(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    for assignment_id in ("root", "child"):
        assignment = store.get_assignment(assignment_id)
        for status in (("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval")
                       if assignment_id == "root" else ("opening_profile", "locating_video", "locating_parent", "preparing_comment", "awaiting_step_approval")):
            assignment = store.update_assignment_status(assignment_id, assignment["revision"], status)
        store.create_submit_approval("campaign-1", assignment_id, assignment["revision"], "opaque")

    with pytest.raises(DuplicateTikTokAccountError) as caught:
        store.freeze_campaign_identities(
            "campaign-1", frozen["revision"], 1,
            tuple({**row, "account_key": "same.handle", "visible_username": "Visible User"}
                  for row in _identity_observations(assignments)),
        )
    outcome = store.invalidate_campaign_identity(
        "campaign-1", frozen["revision"], 1, error_code="duplicate_tiktok_account",
        affected_assignment_ids=caught.value.assignment_ids,
        failure_details={"visible_username": caught.value.visible_username},
    )
    assert outcome["identity_generation"] == 2
    assert store.get_campaign("campaign-1")["status"] == "paused"
    assert store.account_preflight_required("campaign-1") is True
    assert all(row["consumed_at"] is not None for row in store.list_approvals("campaign-1"))
    failure = store.get_assignment("root")["evidence"]["identity_failure"]
    assert set(failure) == {"display_profiles", "visible_username"}
    assert "profile_ref" not in repr(failure) and "same.handle" not in repr(failure)
    assert store.get_assignment("root")["evidence"]["account_preflight"]["identity_generation"] == 1
    snapshot = store.get_campaign("campaign-1")
    with pytest.raises(RevisionConflictError):
        store.invalidate_campaign_identity(
            "campaign-1", frozen["revision"], 1, error_code="profile_start_failed",
            affected_assignment_ids=["root"],
        )
    assert store.get_campaign("campaign-1") == snapshot


def test_begin_submitting_requires_nonzero_three_way_generation_and_consumes_approval_atomically(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        root = store.update_assignment_status("root", root["revision"], status)
    store.create_submit_approval("campaign-1", "root", root["revision"], "opaque")

    submitting = store.begin_submitting("campaign-1", "root", root["revision"], frozen["identity_generation"])

    assert submitting["status"] == "submitting"
    assert store.get_approval("root", root["revision"])["consumed_at"] is not None


def test_begin_comment_input_requires_current_identity_generation_and_reserves_revision(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment"):
        root = store.update_assignment_status("root", root["revision"], status)
    reserved = store.begin_comment_input(
        "campaign-1", "root", root["revision"], frozen["identity_generation"]
    )
    assert reserved["status"] == "preparing_comment" and reserved["revision"] == root["revision"] + 1
    with pytest.raises(RevisionConflictError):
        store.begin_comment_input("campaign-1", "root", reserved["revision"], frozen["identity_generation"] + 1)
    assert store.get_assignment("root")["revision"] == reserved["revision"]


def test_begin_comment_input_awaiting_approval_uses_generation_cas_without_revision_churn(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities("campaign-1", campaign["revision"], 0, _identity_observations(assignments))
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        root = store.update_assignment_status("root", root["revision"], status)
    checked = store.begin_comment_input("campaign-1", "root", root["revision"], frozen["identity_generation"])
    assert checked["revision"] == root["revision"] and checked["status"] == "awaiting_step_approval"
    invalidated = store.invalidate_campaign_identity("campaign-1", frozen["revision"], frozen["identity_generation"], error_code="tiktok_identity_changed", affected_assignment_ids=["root"])
    with pytest.raises(RevisionConflictError):
        store.begin_comment_input("campaign-1", "root", checked["revision"], frozen["identity_generation"])
    assert store.get_campaign("campaign-1")["identity_generation"] == invalidated["identity_generation"]


def test_begin_submitting_stale_generation_leaves_approval_unconsumed(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        root = store.update_assignment_status("root", root["revision"], status)
    store.create_submit_approval("campaign-1", "root", root["revision"], "opaque")

    with pytest.raises(RevisionConflictError):
        store.begin_submitting("campaign-1", "root", root["revision"], frozen["identity_generation"] + 1)

    assert store.get_assignment("root")["status"] == "awaiting_step_approval"
    assert store.get_approval("root", root["revision"])["consumed_at"] is None


def test_freeze_rolls_back_campaign_generation_when_assignment_write_fails(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    before_campaign = store.get_campaign("campaign-1")
    before_assignments = store.list_assignments("campaign-1")

    def fail_assignment_update(_connection, _cursor, statement, *_args):
        if statement.startswith("UPDATE comment_assignments"):
            raise RuntimeError("injected assignment write failure")

    event.listen(store.engine, "before_cursor_execute", fail_assignment_update)
    try:
        with pytest.raises(RuntimeError, match="injected assignment write failure"):
            store.freeze_campaign_identities(
                "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_assignment_update)

    assert store.get_campaign("campaign-1") == before_campaign
    assert store.list_assignments("campaign-1") == before_assignments


def test_freeze_cas_loser_from_the_same_snapshot_performs_zero_writes(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    winner = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    winner_assignments = store.list_assignments("campaign-1")

    with pytest.raises(RevisionConflictError):
        store.freeze_campaign_identities(
            "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
        )

    assert store.get_campaign("campaign-1") == winner
    assert store.list_assignments("campaign-1") == winner_assignments


def test_invalidation_rolls_back_and_never_persists_an_untrusted_error_code(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    before_campaign = store.get_campaign("campaign-1")
    before_assignments = store.list_assignments("campaign-1")

    def fail_assignment_update(_connection, _cursor, statement, *_args):
        if statement.startswith("UPDATE comment_assignments"):
            raise RuntimeError("injected invalidation failure")

    event.listen(store.engine, "before_cursor_execute", fail_assignment_update)
    try:
        with pytest.raises(RuntimeError, match="injected invalidation failure"):
            store.invalidate_campaign_identity(
                "campaign-1", frozen["revision"], 1,
                error_code="secret-sentinel", affected_assignment_ids=["root"],
            )
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_assignment_update)
    assert store.get_campaign("campaign-1") == before_campaign
    assert store.list_assignments("campaign-1") == before_assignments

    invalidated = store.invalidate_campaign_identity(
        "campaign-1", frozen["revision"], 1,
        error_code="secret-sentinel", affected_assignment_ids=["root"],
    )
    assert invalidated["pause_reason"] == "tiktok_identity_unavailable"
    assert "secret-sentinel" not in repr(store.get_campaign("campaign-1"))
    assert "secret-sentinel" not in repr(store.list_assignments("campaign-1"))


def test_refreeze_keeps_published_assignment_and_receipt_identity_byte_for_byte(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting", "verifying_receipt", "published_verified"):
        root = store.update_assignment_status("root", root["revision"], status)
    store.save_receipt("root", {"status": "published_verified", "expected_username": "canonical.root"})
    before_assignment = store.get_assignment("root")
    before_receipt = store.list_receipts("campaign-1")

    refrozen = store.freeze_campaign_identities(
        "campaign-1", frozen["revision"], 1, _identity_observations(assignments[1:])
    )

    assert refrozen["identity_generation"] == 2
    assert store.get_assignment("root") == before_assignment
    assert store.list_receipts("campaign-1") == before_receipt


def test_refreeze_rejects_a_published_receipt_without_an_identity_key(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting", "verifying_receipt", "published_verified"):
        root = store.update_assignment_status("root", root["revision"], status)
    store.save_receipt("root", {"status": "published_verified", "expected_username": "   "})

    with pytest.raises(CampaignValidationError, match="tiktok_identity_unavailable"):
        store.freeze_campaign_identities(
            "campaign-1", frozen["revision"], 1, _identity_observations(assignments[1:])
        )


def test_invalidate_then_resume_and_refreeze_advances_generation_and_resets_tree(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    invalidated = store.invalidate_campaign_identity(
        "campaign-1", frozen["revision"], 1,
        error_code="tiktok_identity_changed", affected_assignment_ids=["root"],
    )
    queued = store.transition_campaign_status("campaign-1", invalidated["revision"], "queued")
    running = store.transition_campaign_status("campaign-1", queued["revision"], "running")

    refrozen = store.freeze_campaign_identities(
        "campaign-1", running["revision"], 2, _identity_observations(assignments)
    )

    states = {row["assignment_id"]: row for row in store.list_assignments("campaign-1")}
    assert refrozen["identity_generation"] == 3
    assert states["root"]["status"] == "planned"
    assert states["child"]["status"] == "waiting_dependency"
    assert {row["identity_generation"] for row in states.values()} == {3}
    assert store.account_preflight_required("campaign-1") is False


def test_refreeze_fails_closed_for_duplicate_published_receipts_with_distinct_active_account(store):
    template = _template()
    template["steps"].append({
        "id": "other", "label": "other", "content_source": "fixed", "fixed_text": "other",
        "content_library_id": "", "content_item_id": "", "parent_step_id": "root",
        "required_profile_tags": [], "excluded_profile_tags": [], "language": "en",
    })
    store.create_template(template, "template-1")
    profile_a, profile_b = _profile_refs(store)
    profile_c = store.sync_profile_identities([
        {"id": "raw-profile-c", "name": "C", "status": "active"}
    ])[0]["profile_ref"]
    campaign_data = _campaign()
    campaign_data["profile_refs"] = [profile_a, profile_b, profile_c]
    campaign = store.create_campaign(
        campaign_data, "campaign-1", "12345678", "https://www.tiktok.com/@owner/video/12345678"
    )
    assignments = store.replace_assignments("campaign-1", [
        {"assignment_id": "root", "step_id": "root", "profile_ref": profile_a, "role": "owner", "resolved_text": "root"},
        {"assignment_id": "reply", "step_id": "reply", "profile_ref": profile_b, "role": "participant", "resolved_text": "reply", "parent_assignment_id": "root"},
        {"assignment_id": "other", "step_id": "other", "profile_ref": profile_c, "role": "participant", "resolved_text": "other", "parent_assignment_id": "root"},
    ])
    for status in ("planned", "awaiting_campaign_approval", "queued", "running"):
        campaign = store.transition_campaign_status("campaign-1", campaign["revision"], status)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    for assignment_id, states in {
        "root": ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval", "submitting", "verifying_receipt", "published_verified"),
        "reply": ("opening_profile", "locating_video", "locating_parent", "preparing_comment", "awaiting_step_approval", "submitting", "verifying_receipt", "published_verified"),
    }.items():
        assignment = store.get_assignment(assignment_id)
        for status in states:
            assignment = store.update_assignment_status(assignment_id, assignment["revision"], status)
        store.save_receipt(assignment_id, {"status": "published_verified", "expected_username": "published.same"})
    before_campaign = store.get_campaign("campaign-1")
    before_assignments = store.list_assignments("campaign-1")

    with pytest.raises(CampaignValidationError, match="tiktok_identity_unavailable"):
        store.freeze_campaign_identities(
            "campaign-1", frozen["revision"], 1,
            _identity_observations([assignments[2]]),
        )

    assert store.get_campaign("campaign-1") == before_campaign
    assert store.list_assignments("campaign-1") == before_assignments


def test_file_sqlite_concurrent_freeze_has_one_winner_and_one_revision_conflict(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    barrier = threading.Barrier(2)
    results, errors, waiting_threads = [], [], set()

    def pause_before_first_campaign_update(_connection, _cursor, statement, *_args):
        if statement.startswith("UPDATE comment_campaigns"):
            thread_id = threading.get_ident()
            if thread_id not in waiting_threads:
                waiting_threads.add(thread_id)
                barrier.wait(timeout=10)

    def freeze_from_same_snapshot():
        try:
            results.append(store.freeze_campaign_identities(
                "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
            ))
        except Exception as exc:  # assertions below keep the race result exact
            errors.append(exc)

    event.listen(store.engine, "before_cursor_execute", pause_before_first_campaign_update)
    try:
        threads = [threading.Thread(target=freeze_from_same_snapshot) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert not any(thread.is_alive() for thread in threads)
    finally:
        event.remove(store.engine, "before_cursor_execute", pause_before_first_campaign_update)

    assert len(results) == 1
    assert len(errors) == 1 and isinstance(errors[0], RevisionConflictError)
    assert results[0]["identity_generation"] == 1
    assert store.get_campaign("campaign-1")["identity_generation"] == 1
    assert {row["identity_generation"] for row in store.list_assignments("campaign-1")} == {1}


def test_begin_submitting_assignment_write_failure_rolls_back_consumed_approval(store):
    campaign, assignments = _running_campaign_with_assignments(store)
    frozen = store.freeze_campaign_identities(
        "campaign-1", campaign["revision"], 0, _identity_observations(assignments)
    )
    root = store.get_assignment("root")
    for status in ("opening_profile", "locating_video", "preparing_comment", "awaiting_step_approval"):
        root = store.update_assignment_status("root", root["revision"], status)
    store.create_submit_approval("campaign-1", "root", root["revision"], "opaque")

    def fail_assignment_update(_connection, _cursor, statement, *_args):
        if statement.startswith("UPDATE comment_assignments"):
            raise RuntimeError("injected final submit CAS failure")

    event.listen(store.engine, "before_cursor_execute", fail_assignment_update)
    try:
        with pytest.raises(RuntimeError, match="injected final submit CAS failure"):
            store.begin_submitting("campaign-1", "root", root["revision"], frozen["identity_generation"])
    finally:
        event.remove(store.engine, "before_cursor_execute", fail_assignment_update)

    current = store.get_assignment("root")
    assert current["status"] == "awaiting_step_approval"
    assert current["revision"] == root["revision"]
    assert store.get_approval("root", root["revision"])["consumed_at"] is None
