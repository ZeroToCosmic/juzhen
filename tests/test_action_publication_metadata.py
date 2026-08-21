"""Shared publication semantics for the two peer action libraries."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from comment_campaign.store import CampaignStore
import comment_campaign.store as campaign_store_module
from comment_campaign.models import CommentActionIdentityRecord
from comment_campaign.errors import CampaignValidationError
from execution_v2.store import ExecutionStore
import execution_v2.store as execution_store_module
from remote_actions.contracts import MessageTooLargeError
from remote_actions.publication import (
    ActionIdentityError,
    PublicationActor,
    PublishGateError,
)
from remote_actions.parameters import ParameterBindingError, bind_parameters


ACTION_ID = re.compile(r"^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
DEVELOPER_ACTOR = PublicationActor("developer-1", "operator", True)
ADMIN_ACTOR = PublicationActor("admin-1", "administrator", True)


def _element_definition() -> dict[str, Any]:
    return {
        "url_pattern": "https://www.tiktok.com/",
        "frame_path": [],
        "locators": [{"type": "css", "value": "button", "priority": 1}],
        "diagnostic_metadata": {},
        "screenshot_path": "",
    }


def _strategy_definition(ready_element_id: str) -> dict[str, Any]:
    return {
        "target_url": "https://www.tiktok.com/",
        "ready_element_id": ready_element_id,
        "readiness_timeout_seconds": 5,
        "run_mode": "once",
        "loop_duration_minutes": None,
        "actions": [],
    }


def _template() -> dict[str, Any]:
    return {
        "name": "thread",
        "description": "",
        "supported_modes": ["threaded"],
        "language": "en",
        "tags": ["test"],
        "steps": [
            {
                "id": "root",
                "label": "root",
                "content_source": "fixed",
                "fixed_text": "hello",
                "content_library_id": "",
                "content_item_id": "",
                "parent_step_id": None,
                "required_profile_tags": [],
                "excluded_profile_tags": [],
                "language": "en",
            }
        ],
    }


def _campaign() -> dict[str, Any]:
    return {
        "name": "campaign",
        "mode": "threaded",
        "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": "template-1",
        "template_revision": 1,
        "profile_refs": ["profile_ref_a"],
        "batch_size": 1,
        "allocation_seed": "seed",
        "start_mode": "manual",
        "scheduled_at": "",
    }


@dataclass
class ActionStoreCase:
    store: Any
    local_id: str
    reopen: Any


@pytest.fixture(params=["browser_strategy", "comment_campaign"])
def action_store_case(request: pytest.FixtureRequest, tmp_path) -> ActionStoreCase:
    if request.param == "browser_strategy":
        path = tmp_path / "execution.db"
        store = ExecutionStore(path)
        store.initialize()
        ready = store.create_element(
            "ready",
            "ready",
            "readiness",
            "generic",
            _element_definition(),
        )
        store.create_strategy(
            "strategy-1",
            "strategy",
            _strategy_definition(ready["id"]),
            True,
        )
        return ActionStoreCase(store, "strategy-1", lambda: ExecutionStore(path))

    path = tmp_path / "campaign.db"
    url = f"sqlite:///{path}"
    store = CampaignStore(url)
    store.initialize()
    store.create_template(_template(), "template-1")
    store.create_campaign(
        _campaign(),
        "campaign-1",
        "12345678",
        "https://www.tiktok.com/@owner/video/12345678",
    )
    return ActionStoreCase(store, "campaign-1", lambda: CampaignStore(url))


def test_new_action_identity_is_persistent_and_separate_from_local_id(
    action_store_case: ActionStoreCase,
) -> None:
    first = action_store_case.store.get_action_publication_metadata(action_store_case.local_id)
    assert ACTION_ID.fullmatch(first["action_id"])
    assert first["action_id"] != action_store_case.local_id
    assert first["tombstoned_at"] is None

    reopened = action_store_case.reopen()
    reopened.initialize()
    second = reopened.get_action_publication_metadata(action_store_case.local_id)
    assert second["action_id"] == first["action_id"]


def test_publish_requires_latest_successful_debug_of_same_content(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    with pytest.raises(PublishGateError):
        store.prepare_release(
            metadata["action_id"],
            metadata["action_revision"],
            actor=DEVELOPER_ACTOR,
        )

    store.record_debug_run(
        metadata["action_id"],
        metadata["action_revision"],
        metadata["content_checksum"],
        "SUCCEEDED",
        "run_debug_1",
        "2026-08-20T10:00:00.000Z",
    )
    release = store.prepare_release(
        metadata["action_id"],
        metadata["action_revision"],
        actor=DEVELOPER_ACTOR,
    )
    assert release["content_checksum"] == metadata["content_checksum"]
    assert release["validation_status"] == "validated"
    assert release["release_checksum"].startswith("sha256:")


def test_admin_waiver_is_explicit_audited_and_syncable(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    release = store.prepare_release(
        metadata["action_id"],
        metadata["action_revision"],
        actor=ADMIN_ACTOR,
        waive_validation=True,
        reason="incident recovery",
    )
    assert release["validation_status"] == "waived"
    assert release["actor"] == "admin-1"
    assert release["waiver_reason"] == "incident recovery"
    assert isinstance(release["release_payload"], dict)
    assert release["release_payload"]["snapshot"]

    synced = store.mark_release_synced(
        metadata["action_id"],
        metadata["action_revision"],
        central_revision=metadata["action_revision"],
        synced_at="2026-08-20T10:01:00.000Z",
    )
    assert synced["central_revision"] == metadata["action_revision"]
    assert synced["synced_at"] == "2026-08-20T10:01:00.000Z"


def test_waiver_requires_actor_and_reason(action_store_case: ActionStoreCase) -> None:
    metadata = action_store_case.store.get_action_publication_metadata(
        action_store_case.local_id
    )
    with pytest.raises(PublishGateError):
        action_store_case.store.prepare_release(
            metadata["action_id"],
            metadata["action_revision"],
            actor="",
            waive_validation=True,
            reason="",
        )
    with pytest.raises(PublishGateError, match="administrator"):
        action_store_case.store.prepare_release(
            metadata["action_id"],
            metadata["action_revision"],
            actor=DEVELOPER_ACTOR,
            waive_validation=True,
            reason="not authorized",
        )
    with pytest.raises(PublishGateError, match="authenticated"):
        action_store_case.store.prepare_release(
            metadata["action_id"],
            metadata["action_revision"],
            actor=PublicationActor("admin-1", "administrator"),
            waive_validation=True,
            reason="not authenticated",
        )


def test_tombstoned_action_id_cannot_be_rebound(action_store_case: ActionStoreCase) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    store.tombstone_action_identity(metadata["action_id"], "2026-08-20T10:02:00.000Z")
    with pytest.raises(ActionIdentityError):
        store.bind_action_identity("copy-local-id", action_id=metadata["action_id"])


def test_identity_binding_and_database_reject_out_of_range_ulid(
    action_store_case: ActionStoreCase,
) -> None:
    invalid_action_id = "act_" + ("Z" * 26)
    with pytest.raises(ActionIdentityError, match="invalid action_id"):
        action_store_case.store.bind_action_identity(
            action_store_case.local_id,
            action_id=invalid_action_id,
        )
    with pytest.raises(ActionIdentityError, match="invalid action_id"):
        action_store_case.store.bind_action_identity(
            action_store_case.local_id,
            action_id="",
        )

    if isinstance(action_store_case.store, ExecutionStore):
        with pytest.raises(sqlite3.IntegrityError, match="invalid action_id"):
            with action_store_case.store.connect() as connection:
                connection.execute(
                    "INSERT INTO strategy_action_identities"
                    "(action_id, strategy_id, source_revision, content_checksum, "
                    "tombstoned_at, created_at) VALUES (?, NULL, 1, '', NULL, ?)",
                    (invalid_action_id, "2026-08-20T10:00:00.000Z"),
                )
        with pytest.raises(sqlite3.IntegrityError, match="invalid action_id"):
            with action_store_case.store.connect() as connection:
                connection.execute(
                    "INSERT INTO strategy_action_identities"
                    "(action_id, strategy_id, source_revision, content_checksum, "
                    "tombstoned_at, created_at) VALUES (NULL, NULL, 1, '', NULL, ?)",
                    ("2026-08-20T10:00:00.000Z",),
                )
        return

    with pytest.raises(IntegrityError):
        with action_store_case.store.engine.begin() as connection:
            connection.execute(
                CommentActionIdentityRecord.__table__.insert(),
                {
                    "action_id": invalid_action_id,
                    "campaign_id": None,
                    "source_revision": 1,
                    "content_checksum": "",
                    "tombstoned_at": None,
                    "created_at": "2026-08-20T10:00:00.000Z",
                },
            )
    with pytest.raises(IntegrityError):
        with action_store_case.store.engine.begin() as connection:
            connection.execute(
                CommentActionIdentityRecord.__table__.insert(),
                {
                    "action_id": None,
                    "campaign_id": None,
                    "source_revision": 1,
                    "content_checksum": "",
                    "tombstoned_at": None,
                    "created_at": "2026-08-20T10:00:00.000Z",
                },
            )


def test_browser_legacy_identity_trigger_is_upgraded_idempotently(tmp_path) -> None:
    path = tmp_path / "legacy-browser-identity.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE strategies (
              id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL,
              revision INTEGER NOT NULL, definition_json TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE strategy_action_identities (
              action_id TEXT PRIMARY KEY, strategy_id TEXT UNIQUE,
              source_revision INTEGER NOT NULL DEFAULT 1,
              content_checksum TEXT NOT NULL DEFAULT '', tombstoned_at TEXT,
              created_at TEXT NOT NULL
            );
            CREATE TRIGGER strategy_action_identity_id_insert
            BEFORE INSERT ON strategy_action_identities
            WHEN NOT (
              length(NEW.action_id) = 30
              AND substr(NEW.action_id, 1, 4) = 'act_'
              AND substr(NEW.action_id, 5, 1) GLOB '[0-7]'
              AND substr(NEW.action_id, 6) NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'
            )
            BEGIN SELECT RAISE(ABORT, 'invalid action_id'); END;
            """
        )

    store = ExecutionStore(path)
    store.initialize()
    store.initialize()
    with store.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert "strategy_action_identity_id_insert" in triggers
        assert "strategy_action_identity_id_insert_v2" in triggers

        for invalid_action_id in (None, "act_" + ("Z" * 26)):
            with pytest.raises(sqlite3.IntegrityError, match="invalid action_id"):
                connection.execute(
                    "INSERT INTO strategy_action_identities"
                    "(action_id, strategy_id, source_revision, content_checksum, "
                    "tombstoned_at, created_at) VALUES (?, NULL, 1, '', NULL, ?)",
                    (invalid_action_id, "2026-08-20T10:00:00.000Z"),
                )

        connection.execute(
            "INSERT INTO strategy_action_identities"
            "(action_id, strategy_id, source_revision, content_checksum, "
            "tombstoned_at, created_at) VALUES (?, NULL, 1, '', NULL, ?)",
            (
                "act_0000000000000000000000002A",
                "2026-08-20T10:00:00.000Z",
            ),
        )


def test_non_executable_local_changes_do_not_invalidate_debug_revision(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    before = store.get_action_publication_metadata(action_store_case.local_id)
    if isinstance(store, ExecutionStore):
        strategy = store.get_strategy_or_raise(action_store_case.local_id)
        definition = {
            key: strategy[key]
            for key in (
                "target_url",
                "ready_element_id",
                "readiness_timeout_seconds",
                "run_mode",
                "loop_duration_minutes",
                "actions",
            )
        }
        store.update_strategy(
            action_store_case.local_id,
            "renamed only",
            definition,
            True,
            expected_revision=strategy["revision"],
        )
    else:
        campaign = store.get_campaign(action_store_case.local_id)
        store.transition_campaign_status(
            action_store_case.local_id,
            campaign["revision"],
            "planned",
        )
    after = store.get_action_publication_metadata(action_store_case.local_id)
    assert after["content_checksum"] == before["content_checksum"]
    assert after["action_revision"] == before["action_revision"]


def test_browser_executable_change_increments_action_revision_and_invalidates_debug(
    tmp_path,
) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    store.initialize()
    ready = store.create_element(
        "ready", "ready", "readiness", "generic", _element_definition()
    )
    strategy = store.create_strategy(
        "strategy-1", "strategy", _strategy_definition(ready["id"]), True
    )
    before = store.get_action_publication_metadata(strategy["id"])
    store.record_debug_run(
        before["action_id"],
        before["action_revision"],
        before["content_checksum"],
        "SUCCEEDED",
        "run_debug_before_edit",
        "2026-08-20T10:00:00.000Z",
    )
    changed = _strategy_definition(ready["id"])
    changed["target_url"] = "https://www.tiktok.com/@changed"
    store.update_strategy(
        strategy["id"], "strategy", changed, True, expected_revision=1
    )
    after = store.get_action_publication_metadata(strategy["id"])
    assert after["action_revision"] == before["action_revision"] + 1
    assert after["content_checksum"] != before["content_checksum"]
    with pytest.raises(PublishGateError):
        store.prepare_release(
            after["action_id"], after["action_revision"], actor=DEVELOPER_ACTOR
        )


def test_release_content_is_database_immutable(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    release = store.prepare_release(
        metadata["action_id"],
        metadata["action_revision"],
        actor=ADMIN_ACTOR,
        waive_validation=True,
        reason="migration",
    )
    if isinstance(store, ExecutionStore):
        with pytest.raises(sqlite3.IntegrityError):
            with store.connect() as connection:
                connection.execute(
                    "UPDATE strategy_releases SET actor = 'tampered' "
                    "WHERE action_id = ? AND revision = ?",
                    (release["action_id"], release["revision"]),
                )
    else:
        with pytest.raises(IntegrityError):
            with store.engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE comment_action_releases SET actor = 'tampered' "
                        "WHERE action_id = :action_id AND revision = :revision"
                    ),
                    {
                        "action_id": release["action_id"],
                        "revision": release["revision"],
                    },
                )


def test_sync_is_idempotent_but_rejects_conflicting_central_revision(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    release = store.prepare_release(
        metadata["action_id"],
        metadata["action_revision"],
        actor=ADMIN_ACTOR,
        waive_validation=True,
        reason="migration",
    )
    store.mark_release_synced(
        release["action_id"], release["revision"], 7, "2026-08-20T10:03:00.000Z"
    )
    store.mark_release_synced(
        release["action_id"], release["revision"], 7, "2026-08-20T10:03:01.000Z"
    )
    with pytest.raises(PublishGateError, match="conflict"):
        store.mark_release_synced(
            release["action_id"],
            release["revision"],
            8,
            "2026-08-20T10:03:02.000Z",
        )


def test_deleting_local_draft_preserves_tombstoned_global_identity(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    if isinstance(store, ExecutionStore):
        local = store.get_strategy_or_raise(action_store_case.local_id)
        store.delete_strategy(action_store_case.local_id, expected_revision=local["revision"])
        with store.connect() as connection:
            identity = connection.execute(
                "SELECT * FROM strategy_action_identities WHERE action_id = ?",
                (metadata["action_id"],),
            ).fetchone()
            assert identity["strategy_id"] is None
            assert identity["tombstoned_at"] is not None
    else:
        local = store.get_campaign(action_store_case.local_id)
        store.delete_campaign(action_store_case.local_id, local["revision"])
        with store.session_factory.begin() as session:
            identity = session.get(CommentActionIdentityRecord, metadata["action_id"])
            assert identity.campaign_id is None
            assert identity.tombstoned_at is not None


def test_debug_completion_uses_content_frozen_at_start(tmp_path) -> None:
    store = ExecutionStore(tmp_path / "execution.db")
    store.initialize()
    ready = store.create_element(
        "ready", "ready", "readiness", "generic", _element_definition()
    )
    strategy = store.create_strategy(
        "strategy-1", "strategy", _strategy_definition(ready["id"]), True
    )
    frozen = store.begin_debug_run(strategy["id"], "run_frozen")
    changed = _strategy_definition(ready["id"])
    changed["target_url"] = "https://www.tiktok.com/@changed"
    store.update_strategy(strategy["id"], "strategy", changed, True, expected_revision=1)
    completed = store.complete_debug_run(
        "run_frozen", "SUCCEEDED", "2026-08-20T10:05:00.000Z"
    )
    assert completed["content_checksum"] == frozen["content_checksum"]
    current = store.get_action_publication_metadata(strategy["id"])
    assert current["content_checksum"] != completed["content_checksum"]
    with pytest.raises(PublishGateError):
        store.prepare_release(
            current["action_id"], current["action_revision"], actor=DEVELOPER_ACTOR
        )


def test_non_draft_campaign_cannot_be_physically_deleted(tmp_path) -> None:
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    store.create_template(_template(), "template-1")
    campaign = store.create_campaign(
        _campaign(), "campaign-1", "12345678",
        "https://www.tiktok.com/@owner/video/12345678",
    )
    planned = store.transition_campaign_status(campaign["id"], 1, "planned")
    with pytest.raises(CampaignValidationError):
        store.delete_campaign(campaign["id"], planned["revision"])
    assert store.get_campaign(campaign["id"]) is not None


def test_release_runtime_parameters_reject_empty_or_incomplete_values(
    action_store_case: ActionStoreCase,
) -> None:
    store = action_store_case.store
    metadata = store.get_action_publication_metadata(action_store_case.local_id)
    release = store.prepare_release(
        metadata["action_id"], metadata["action_revision"], actor=ADMIN_ACTOR,
        waive_validation=True, reason="contract test",
    )["release_payload"]
    schema = release["parameter_schema"]
    snapshot = release["snapshot"]
    if isinstance(store, ExecutionStore):
        for invalid_url in (
            "", "https://", "https://bad host/path", "https://?x",
            "https://#x", "https://:443", "https://localhost:0",
            "https://localhost:99999", "https://[:::]", "https://[....]",
            "https://[1.2.3.4]",
        ):
            with pytest.raises(ParameterBindingError):
                bind_parameters(snapshot, schema, {"target_url": invalid_url})
        bind_parameters(snapshot, schema, {"target_url": "https://example.test"})
        bind_parameters(snapshot, schema, {"target_url": "https://localhost:8443?x=1"})
        bind_parameters(snapshot, schema, {"target_url": "https://[2001:db8::1]:443/x"})
        return

    bindings = {
        "entry_element_id": "element_entry",
        "input_element_id": "element_input",
        "submit_element_id": "element_submit",
        "account_element_id": "element_account",
    }
    with pytest.raises(ParameterBindingError):
        bind_parameters(
            snapshot,
            schema,
            {"target_url": "https://example.test", "node_texts": {}, "element_bindings": bindings},
        )
    with pytest.raises(ParameterBindingError):
        bind_parameters(
            snapshot,
            schema,
            {
                "target_url": "",
                "node_texts": {"root": "hello"},
                "element_bindings": bindings,
            },
        )
    for invalid_url in (
        "https://", "https://?x", "https://#x", "https://:443",
        "https://localhost:99999", "https://[:::]", "https://[....]",
        "https://[1.2.3.4]",
    ):
        with pytest.raises(ParameterBindingError):
            bind_parameters(
                snapshot,
                schema,
                {
                    "target_url": invalid_url,
                    "node_texts": {"root": "hello"},
                    "element_bindings": bindings,
                },
            )
    with pytest.raises(ParameterBindingError):
        bind_parameters(
            snapshot,
            schema,
            {
                "target_url": "https://example.test",
                "node_texts": {"root": "   "},
                "element_bindings": bindings,
            },
        )
    with pytest.raises(ParameterBindingError):
        bind_parameters(
            snapshot,
            schema,
            {
                "target_url": "https://example.test",
                "node_texts": {"root": "hello"},
                "element_bindings": {**bindings, "input_element_id": "   "},
            },
        )
    bound = bind_parameters(
        snapshot,
        schema,
        {
            "target_url": "https://example.test",
            "node_texts": {"root": "hello"},
            "element_bindings": bindings,
        },
    )
    assert bound["runtime"]["node_texts"] == {"root": "hello"}


def test_local_release_calls_shared_content_limiter(
    action_store_case: ActionStoreCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = (
        execution_store_module
        if isinstance(action_store_case.store, ExecutionStore)
        else campaign_store_module
    )

    def reject(_document):
        raise MessageTooLargeError("release content exceeds shared limit")

    monkeypatch.setattr(module, "validate_release_content", reject)
    metadata = action_store_case.store.get_action_publication_metadata(
        action_store_case.local_id
    )
    with pytest.raises(MessageTooLargeError, match="shared limit"):
        action_store_case.store.prepare_release(
            metadata["action_id"],
            metadata["action_revision"],
            actor=ADMIN_ACTOR,
            waive_validation=True,
            reason="contract test",
        )
