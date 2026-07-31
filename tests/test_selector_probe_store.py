from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import json
import sqlite3
from threading import Barrier

import pytest

from selector_probe.registry import reconcile_registry
from selector_probe.store import (
    ElementRequestInProgressError,
    ManagementIdempotencyConflictError,
    SelectorProbeStore,
)


def test_store_migrates_legacy_management_runs_before_creating_indexes(
    tmp_path,
):
    database = tmp_path / "legacy-probe.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE probe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            active_version_before TEXT NOT NULL DEFAULT '',
            published_version_after TEXT NOT NULL DEFAULT '',
            failed_aliases_json TEXT NOT NULL DEFAULT '[]',
            details_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE management_run_requests (
            id TEXT PRIMARY KEY,
            actor_user_id INTEGER NOT NULL,
            actor_username TEXT NOT NULL,
            retry_of_run_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO management_run_requests (
            id, actor_user_id, actor_username, retry_of_run_id,
            status, created_at, updated_at
        ) VALUES (
            'legacy-request', 1, 'system', '', 'accepted',
            '2026-07-29T19:00:00+00:00',
            '2026-07-29T19:00:00+00:00'
        );
        """
    )
    connection.close()

    with SelectorProbeStore(database) as store:
        detail = store.management_run_detail("legacy-request")
        columns = {
            row["name"]
            for row in store.connection.execute(
                "PRAGMA table_info(management_run_requests)"
            )
        }

    assert detail is not None
    assert detail["status"] == "failed"
    assert detail["failure_code"] == "legacy_unlinked_request"
    assert {
        "trigger",
        "probe_run_id",
        "failure_code",
        "finished_at",
    }.issubset(columns)


def _version_bundle(selector_value="comment-icon"):
    elements = {
        "comment-entry": {
            "scope": "active_video",
            "locators": [
                {
                    "id": "comment-entry-data-e2e",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": selector_value,
                    "enabled": True,
                }
            ],
        }
    }
    payload = json.dumps(
        elements,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "bundle_hash": "sha256:" + hashlib.sha256(payload.encode()).hexdigest(),
        "elements": elements,
    }


def _version_evidence(bundle_value=None, *, profiles=2):
    current = bundle_value or _version_bundle()
    aliases = {
        alias: {
            "status": "ok",
            "candidate_id": next(
                locator["id"]
                for locator in definition["locators"]
                if locator["enabled"]
            ),
        }
        for alias, definition in current["elements"].items()
    }
    validations = []
    for round_number in (1, 2):
        for profile_number in range(profiles):
            marker = f"{profile_number}:{round_number}"
            digest = hashlib.sha256(marker.encode()).hexdigest()
            validations.append(
                {
                    "profile_mask": f"***p{profile_number:03d}",
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:" + digest,
                    "snapshot_hash": "sha256:"
                    + hashlib.sha256(f"snapshot:{marker}".encode()).hexdigest(),
                    "page_generation": "sha256:"
                    + hashlib.sha256(f"generation:{marker}".encode()).hexdigest(),
                    "aliases": copy.deepcopy(aliases),
                }
            )
    return {
        "status": "passed",
        "bundle_hash": current["bundle_hash"],
        "profiles_passed": profiles,
        "rounds_passed": 2,
        "validations": validations,
    }


def test_store_initializes_phase_one_schema_and_finishes_run(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        run_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="settings-v3",
        )
        validation_id = store.record_validation(
            run_id=run_id,
            profile_mask="***le-a",
            round_number=1,
            page_state="feed_ready",
            result="passed",
            failure_code="",
            evidence={"aliases": {"评论入口": {"status": "ok"}}},
        )
        store.finish_run(
            run_id,
            status="completed",
            details={"observe_only": True},
        )

        run = store.connection.execute(
            "SELECT * FROM probe_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        validation = store.connection.execute(
            "SELECT * FROM selector_validation_runs WHERE id = ?",
            (validation_id,),
        ).fetchone()

        assert run["status"] == "completed"
        assert run["finished_at"]
        assert run["details_json"] == '{"observe_only":true}'
        assert json.loads(validation["evidence_json"]) == {
            "aliases": {"评论入口": {"status": "ok"}}
        }


def test_contract_aliases_are_replaced_atomically(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.save_contracts(
            {
                "评论入口": {
                    "scope": "active_video",
                    "required_state": "feed_ready",
                },
                "评论输入框": {
                    "scope": "visible_comment_panel",
                    "required_state": "comment_panel_open",
                },
            }
        )
        store.save_contracts(
            {
                "评论入口": {
                    "scope": "active_video",
                    "required_state": "feed_ready",
                },
            }
        )

        rows = store.connection.execute(
            """
            SELECT alias, site, environment, enabled
            FROM element_probe_contracts
            ORDER BY alias
            """
        ).fetchall()

        assert [row["alias"] for row in rows] == ["评论入口"]
        assert dict(rows[0]) == {
            "alias": "评论入口",
            "site": "tiktok",
            "environment": "production",
            "enabled": 1,
        }


def test_contract_replacement_preserves_other_site_environment_scopes(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.save_contracts(
            {
                "production-old": {
                    "site": "tiktok",
                    "environment": "production",
                    "scope": "active_video",
                },
                "staging-only": {
                    "site": "tiktok",
                    "environment": "staging",
                    "scope": "active_video",
                },
            }
        )
        store.save_contracts(
            {
                "production-new": {
                    "site": "tiktok",
                    "environment": "production",
                    "scope": "active_video",
                }
            }
        )

        rows = store.connection.execute(
            """
            SELECT alias, site, environment
            FROM element_probe_contracts
            ORDER BY environment, alias
            """
        ).fetchall()

        assert [dict(row) for row in rows] == [
            {
                "alias": "production-new",
                "site": "tiktok",
                "environment": "production",
            },
            {
                "alias": "staging-only",
                "site": "tiktok",
                "environment": "staging",
            },
        ]


def test_contract_seed_preserves_db_managed_catalog_and_lists_enabled(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.seed_contracts(
            {
                "ui-added": {
                    "scope": "page",
                    "required_state": "feed_ready",
                }
            }
        )
        store.upsert_contract(
            "ui-added",
            {
                "scope": "active_video",
                "required_state": "feed_ready",
            },
            site="tiktok",
            environment="production",
            enabled=True,
        )
        store.seed_contracts(
            {
                "ui-added": {
                    "scope": "page",
                    "required_state": "comment_panel_open",
                },
                "default-new": {
                    "scope": "page",
                    "required_state": "feed_ready",
                },
            }
        )

        contracts = store.list_contracts(
            site="tiktok",
            environment="production",
        )

    assert contracts["ui-added"]["scope"] == "active_video"
    assert contracts["default-new"]["scope"] == "page"


def test_contract_alias_is_isolated_by_site_and_environment(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.upsert_contract(
            "shared-alias",
            {"scope": "page", "required_state": "feed_ready"},
            site="tiktok",
            environment="production",
        )
        store.upsert_contract(
            "shared-alias",
            {
                "scope": "active_video",
                "required_state": "comment_panel_open",
            },
            site="tiktok",
            environment="staging",
        )

        production = store.list_contracts(
            site="tiktok",
            environment="production",
        )
        staging = store.list_contracts(
            site="tiktok",
            environment="staging",
        )

    assert production["shared-alias"]["scope"] == "page"
    assert staging["shared-alias"]["scope"] == "active_video"


def test_selector_failure_is_a_terminal_daily_slot(tmp_path):
    slot = "2026-07-28T19:00:00+00:00"
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        run_id = store.start_run(
            scheduled_for=slot,
            active_version_before="sel-old",
            attempt_token="owner",
        )
        store.finish_run(
            run_id,
            status="selector_validation_failed",
            details={"failure_code": "multiple_match"},
            failed_aliases=("comment-entry",),
            attempt_token="owner",
        )

        assert store.last_terminal_slot() == datetime.fromisoformat(slot)


def test_duplicate_unfinished_slot_resets_attempt_without_duplication(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        first_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="settings-v3",
        )
        store.record_validation(
            run_id=first_id,
            profile_mask="***le-a",
            round_number=1,
            page_state="feed_ready",
            result="failed",
            failure_code="not_found",
            evidence={},
        )
        store.finish_run(
            first_id,
            status="probe_unavailable",
            details={"failure_code": "probe_unavailable"},
            published_version_after="discard-me",
            failed_aliases=("comment_entry",),
        )
        second_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="settings-v4",
        )

        rows = store.connection.execute(
            """
            SELECT id, status, finished_at, active_version_before,
                   published_version_after, failed_aliases_json, details_json
            FROM probe_runs
            """
        ).fetchall()
        validation_count = store.connection.execute(
            "SELECT COUNT(*) FROM selector_validation_runs"
        ).fetchone()[0]

        assert second_id == first_id
        assert [dict(row) for row in rows] == [
            {
                "id": first_id,
                "status": "running",
                "finished_at": None,
                "active_version_before": "settings-v4",
                "published_version_after": "",
                "failed_aliases_json": "[]",
                "details_json": "{}",
            }
        ]
        assert validation_count == 0


def test_equivalent_timezone_representations_share_one_scheduled_run(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        local_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="settings-v3",
        )
        utc_id = store.start_run(
            scheduled_for="2026-07-27T19:00:00Z",
            active_version_before="settings-v4",
        )

        rows = store.connection.execute(
            "SELECT id, scheduled_for FROM probe_runs"
        ).fetchall()

        assert utc_id == local_id
        assert [dict(row) for row in rows] == [
            {
                "id": local_id,
                "scheduled_for": "2026-07-27T19:00:00+00:00",
            }
        ]


def test_completed_slot_cannot_be_restarted(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        run_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="settings-v3",
        )
        store.finish_run(run_id, status="completed", details={})

        with pytest.raises(RuntimeError, match="already completed"):
            store.start_run(
                scheduled_for="2026-07-27T19:00:00Z",
                active_version_before="settings-v4",
            )

        row = store.connection.execute(
            "SELECT status, active_version_before FROM probe_runs"
        ).fetchone()
        assert dict(row) == {
            "status": "completed",
            "active_version_before": "settings-v3",
        }


def test_concurrent_same_slot_starts_share_one_transactional_attempt(tmp_path):
    path = tmp_path / "probe.db"
    SelectorProbeStore(path).close()

    def start(active_version):
        with SelectorProbeStore(path) as store:
            return store.start_run(
                scheduled_for="2026-07-28T03:00:00+08:00",
                active_version_before=active_version,
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        run_ids = list(
            executor.map(start, ("settings-v3", "settings-v4"))
        )

    with SelectorProbeStore(path) as store:
        rows = store.connection.execute(
            "SELECT id, status FROM probe_runs"
        ).fetchall()
    assert run_ids[0] == run_ids[1]
    assert [dict(row) for row in rows] == [
        {"id": run_ids[0], "status": "running"}
    ]


def test_new_attempt_fences_stale_validation_and_finish_writers(tmp_path):
    path = tmp_path / "probe.db"
    with (
        SelectorProbeStore(path) as old_store,
        SelectorProbeStore(path) as new_store,
    ):
        run_id = old_store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="",
            attempt_token="attempt-old",
        )
        assert (
            new_store.start_run(
                scheduled_for="2026-07-28T03:00:00+08:00",
                active_version_before="",
                attempt_token="attempt-new",
            )
            == run_id
        )

        with pytest.raises(RuntimeError, match="stale probe attempt"):
            old_store.record_validation(
                run_id=run_id,
                profile_mask="***le-a",
                round_number=1,
                page_state="feed_ready",
                result="passed",
                failure_code="",
                evidence={},
                attempt_token="attempt-old",
            )
        with pytest.raises(RuntimeError, match="stale probe attempt"):
            old_store.finish_run(
                run_id,
                status="probe_unavailable",
                details={},
                attempt_token="attempt-old",
            )

        new_store.record_validation(
            run_id=run_id,
            profile_mask="***le-a",
            round_number=1,
            page_state="feed_ready",
            result="passed",
            failure_code="",
            evidence={},
            attempt_token="attempt-new",
        )
        new_store.finish_run(
            run_id,
            status="completed",
            details={},
            attempt_token="attempt-new",
        )

        run = new_store.connection.execute(
            "SELECT status, attempt_token FROM probe_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        validation_count = new_store.connection.execute(
            "SELECT COUNT(*) FROM selector_validation_runs"
        ).fetchone()[0]
        assert dict(run) == {
            "status": "completed",
            "attempt_token": "attempt-new",
        }
        assert validation_count == 1


def test_existing_database_is_migrated_with_attempt_token(tmp_path):
    path = tmp_path / "legacy-probe.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE probe_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for TEXT NOT NULL UNIQUE,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            active_version_before TEXT NOT NULL DEFAULT '',
            published_version_after TEXT NOT NULL DEFAULT '',
            failed_aliases_json TEXT NOT NULL DEFAULT '[]',
            details_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    connection.commit()
    connection.close()

    with SelectorProbeStore(path) as store:
        columns = {
            row["name"]
            for row in store.connection.execute(
                "PRAGMA table_info(probe_runs)"
            ).fetchall()
        }
        assert "attempt_token" in columns


def test_existing_gate_and_effect_rows_are_migrated_with_scope(tmp_path):
    path = tmp_path / "legacy-scopes.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE strategy_gate_reasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id TEXT NOT NULL,
            source TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            selector_version_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            cleared_at TEXT,
            cleared_by TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX idx_open_gate_reason
        ON strategy_gate_reasons(
            strategy_id, source, reason_code, selector_version_id
        )
        WHERE cleared_at IS NULL;
        CREATE TABLE probe_effect_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            effect_key TEXT NOT NULL UNIQUE,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );
        """
    )
    connection.execute(
        """
        INSERT INTO strategy_gate_reasons (
            strategy_id, source, reason_code, aliases_json,
            selector_version_id, created_at
        ) VALUES (
            'comment-flow', 'probe', 'selector_validation_failed',
            '["comment-entry"]', 'sel-old', '2026-07-28T19:00:00Z'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO strategy_gate_reasons (
            strategy_id, source, reason_code, aliases_json,
            selector_version_id, created_at
        ) VALUES (
            'comment-flow', 'manual', 'operator_pause',
            '[]', '', '2026-07-28T19:00:00Z'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO probe_effect_outbox (
            effect_key, event_type, payload_json, status, created_at
        ) VALUES (?, 'selector_failure', ?, 'pending', ?)
        """,
        (
            "legacy-effect",
            json.dumps(
                {
                    "site": "tiktok",
                    "environment": "staging",
                    "aliases": ["comment-entry"],
                }
            ),
            "2026-07-28T19:00:00Z",
        ),
    )
    connection.commit()
    connection.close()

    with SelectorProbeStore(path) as store:
        reasons = store.connection.execute(
            """
            SELECT source, site, environment
            FROM strategy_gate_reasons
            ORDER BY source
            """
        ).fetchall()
        effect = store.connection.execute(
            "SELECT site, environment FROM probe_effect_outbox"
        ).fetchone()

    assert [tuple(row) for row in reasons] == [
        ("manual", "", ""),
        ("probe", "tiktok", "production"),
    ]
    assert tuple(effect) == ("tiktok", "staging")


def test_last_completed_slot_ignores_unfinished_and_returns_latest_instant(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        earlier = store.start_run(
            scheduled_for="2026-07-27T19:00:00Z",
            active_version_before="",
        )
        store.finish_run(earlier, status="completed", details={})
        later = store.start_run(
            scheduled_for="2026-07-29T03:00:00+08:00",
            active_version_before="",
        )
        store.finish_run(later, status="completed", details={})
        unfinished = store.start_run(
            scheduled_for="2026-07-30T03:00:00+08:00",
            active_version_before="",
        )
        failed = store.start_run(
            scheduled_for="2026-07-31T03:00:00+08:00",
            active_version_before="",
        )
        store.finish_run(failed, status="failed", details={})

        assert store.last_completed_slot() == datetime(
            2026, 7, 28, 19, 0, tzinfo=UTC
        )
        assert store.connection.execute(
            "SELECT status FROM probe_runs WHERE id = ?", (unfinished,)
        ).fetchone()["status"] == "running"


def test_json_columns_are_serialized_deterministically(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        run_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="",
        )
        store.record_validation(
            run_id=run_id,
            profile_mask="***le-a",
            round_number=2,
            page_state="feed_ready",
            result="failed",
            failure_code="not_found",
            evidence={"z": "中文", "a": [2, 1]},
        )
        store.finish_run(
            run_id,
            status="failed",
            failed_aliases=("输入框", "评论入口"),
            details={"z": 2, "a": 1},
        )
        store.save_contracts(
            {
                "评论入口": {
                    "z": "中文",
                    "a": 1,
                }
            }
        )

        run = store.connection.execute(
            "SELECT failed_aliases_json, details_json FROM probe_runs"
        ).fetchone()
        evidence = store.connection.execute(
            "SELECT evidence_json FROM selector_validation_runs"
        ).fetchone()["evidence_json"]
        contract = store.connection.execute(
            "SELECT contract_json FROM element_probe_contracts"
        ).fetchone()["contract_json"]

        assert run["failed_aliases_json"] == (
            '["\\u8f93\\u5165\\u6846","\\u8bc4\\u8bba\\u5165\\u53e3"]'
        )
        assert run["details_json"] == '{"a":1,"z":2}'
        assert evidence == '{"a":[2,1],"z":"\\u4e2d\\u6587"}'
        assert contract == '{"a":1,"z":"\\u4e2d\\u6587"}'


@pytest.mark.parametrize(
    "invalid_contracts",
    [
        [],
        {"": {"scope": "active_video"}},
        {" padded ": {"scope": "active_video"}},
        {"评论入口": "not-an-object"},
        {"评论入口": {"enabled": 1}},
        {"评论入口": {"value": object()}},
    ],
)
def test_invalid_contract_input_does_not_destroy_existing_rows(
    tmp_path, invalid_contracts
):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.save_contracts({"保留元素": {"scope": "active_video"}})

        with pytest.raises(ValueError):
            store.save_contracts(invalid_contracts)

        row = store.connection.execute(
            "SELECT alias, contract_json FROM element_probe_contracts"
        ).fetchone()
        assert dict(row) == {
            "alias": "保留元素",
            "contract_json": '{"scope":"active_video"}',
        }


def test_record_validation_rejects_non_json_evidence_without_writing(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        run_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="",
        )

        with pytest.raises(ValueError, match="JSON-safe"):
            store.record_validation(
                run_id=run_id,
                profile_mask="***le-a",
                round_number=1,
                page_state="feed_ready",
                result="passed",
                failure_code="",
                evidence={"bad": object()},
            )

        count = store.connection.execute(
            "SELECT COUNT(*) FROM selector_validation_runs"
        ).fetchone()[0]
        assert count == 0


def test_context_manager_closes_connection(tmp_path):
    store = SelectorProbeStore(tmp_path / "probe.db")
    connection = store.connection

    with store:
        connection.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1").fetchone()


def test_validated_version_and_outbox_share_one_immediate_transaction(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        current = _version_bundle()
        version_id = store.store_validated_version(
            bundle=current,
            evidence=_version_evidence(current),
            base_version_id="old",
            model_id="gpt-main",
            prompt_version="selector-repair-v1",
        )
        version = store.connection.execute(
            "SELECT status, bundle_hash FROM selector_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        outbox = store.connection.execute(
            "SELECT status, aggregate_id FROM publication_outbox"
        ).fetchone()

        assert version["status"] == "validated"
        assert version["bundle_hash"] == current["bundle_hash"]
        assert outbox["status"] == "pending"
        assert outbox["aggregate_id"] == version_id


def test_outbox_insert_failure_rolls_back_version(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.connection.execute(
            """
            CREATE TRIGGER reject_publication_outbox
            BEFORE INSERT ON publication_outbox
            BEGIN
                SELECT RAISE(ABORT, 'outbox disabled');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="outbox disabled"):
            store.store_validated_version(
                bundle=_version_bundle(),
                evidence=_version_evidence(),
                base_version_id="",
                model_id="",
                prompt_version="",
            )

        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM selector_versions"
            ).fetchone()[0]
            == 0
        )


def test_same_second_identical_version_is_idempotent_and_changed_content_gets_suffix(
    tmp_path,
):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        first = store.store_validated_version(
            bundle=_version_bundle(),
            evidence=_version_evidence(),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        identical = store.store_validated_version(
            bundle=_version_bundle(),
            evidence=_version_evidence(),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        changed = store.store_validated_version(
            bundle=_version_bundle(),
            evidence=_version_evidence(),
            base_version_id="different",
            model_id="other",
            prompt_version="other",
        )

        rows = store.connection.execute(
            "SELECT id, base_version_id FROM selector_versions"
            " ORDER BY created_at, id"
        ).fetchall()
        outbox_count = store.connection.execute(
            "SELECT COUNT(*) FROM publication_outbox"
        ).fetchone()[0]
        assert identical == first
        assert changed.startswith(first + "-")
        assert len(rows) == 2
        assert {row["base_version_id"] for row in rows} == {"", "different"}
        assert outbox_count == 2


@pytest.mark.parametrize(
    "evidence",
    [
        {"profiles_passed": 1, "rounds_passed": 2},
        {"profiles_passed": 2, "rounds_passed": 1},
        {"profiles_passed": True, "rounds_passed": 2},
        {"profiles_passed": 2, "rounds_passed": 2, "blob": "x" * 70_000},
    ],
)
def test_store_rejects_unproven_or_oversized_validation_evidence(
    tmp_path,
    evidence,
):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        with pytest.raises(ValueError):
            store.store_validated_version(
                bundle=_version_bundle(),
                evidence=evidence,
                base_version_id="",
                model_id="",
                prompt_version="",
            )
        assert (
            store.connection.execute(
                "SELECT COUNT(*) FROM selector_versions"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(status="failed"),
        lambda item: item["validations"].pop(),
        lambda item: item["validations"][0].update(
            reset_evidence_hash="not-a-hash"
        ),
        lambda item: item["validations"][0]["aliases"]["comment-entry"].update(
            candidate_id="unknown"
        ),
        lambda item: item["validations"][-1]["aliases"]["comment-entry"].update(
            candidate_id="different"
        ),
        lambda item: item["validations"][-1].update(round_number=1),
    ],
)
def test_store_rejects_forged_task4_validation_evidence(tmp_path, mutate):
    current = _version_bundle()
    proof = _version_evidence(current)
    mutate(proof)
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        with pytest.raises(ValueError, match="evidence"):
            store.store_validated_version(
                bundle=current,
                evidence=proof,
                base_version_id="",
                model_id="",
                prompt_version="",
            )


def test_store_accepts_strict_evidence_from_three_profiles(tmp_path):
    current = _version_bundle()
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        version_id = store.store_validated_version(
            bundle=current,
            evidence=_version_evidence(current, profiles=3),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        assert store.get_version(version_id)["status"] == "validated"


def test_store_recomputes_canonical_hash_and_rejects_noncanonical_bundle(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        bad_hash = _version_bundle()
        bad_hash["bundle_hash"] = "sha256:" + "0" * 64
        with pytest.raises(ValueError, match="bundle_hash"):
            store.store_validated_version(
                bundle=bad_hash,
                evidence=_version_evidence(),
                base_version_id="",
                model_id="",
                prompt_version="",
            )

        injected = _version_bundle()
        injected["version"] = "../malicious"
        with pytest.raises(ValueError, match="bundle"):
            store.store_validated_version(
                bundle=injected,
                evidence=_version_evidence(),
                base_version_id="",
                model_id="",
                prompt_version="",
            )


def test_two_connections_claim_outbox_with_lease_fencing(tmp_path):
    path = tmp_path / "probe.db"
    with SelectorProbeStore(path) as store:
        current = _version_bundle()
        store.store_validated_version(
            bundle=current,
            evidence=_version_evidence(),
            base_version_id="",
            model_id="",
            prompt_version="",
        )

    barrier = Barrier(2)

    def claim(token):
        with SelectorProbeStore(path) as store:
            barrier.wait()
            return store.claim_outbox_event(claim_token=token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, ("worker-a", "worker-b")))

    events = [item for item in claimed if item is not None]
    assert len(events) == 1
    assert events[0]["attempt_count"] == 1


def test_expired_claim_is_reclaimed_and_old_worker_cannot_ack(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.store_validated_version(
            bundle=_version_bundle(),
            evidence=_version_evidence(),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        old = store.claim_outbox_event(
            claim_token="worker-old",
            lease_seconds=0,
        )
        new = store.claim_outbox_event(claim_token="worker-new")

        assert new["outbox_id"] == old["outbox_id"]
        assert new["attempt_count"] == 2
        assert not store.ack_outbox_event(
            old["outbox_id"],
            old["claim_token"],
            old["claim_generation"],
            outcome="published",
        )
        assert store.ack_outbox_event(
            new["outbox_id"],
            new["claim_token"],
            new["claim_generation"],
            outcome="published",
        )
        assert store.get_version(new["version"])["status"] == "published"


def test_failed_outbox_attempt_is_requeued_with_backoff(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.store_validated_version(
            bundle=_version_bundle(),
            evidence=_version_evidence(),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        event = store.next_outbox_event(claim_token="worker")
        assert store.fail_outbox_event(
            event["outbox_id"],
            event["claim_token"],
            event["claim_generation"],
            failure="redis_unavailable",
            retry=True,
        )
        row = store.connection.execute(
            """
            SELECT status, attempt_count, next_attempt_at, created_at,
                   lease_until, last_error
            FROM publication_outbox
            """
        ).fetchone()

        assert row["status"] == "pending"
        assert row["attempt_count"] == 1
        assert row["next_attempt_at"] > row["created_at"]
        assert row["lease_until"] is None
        assert row["last_error"] == "redis_unavailable"


def test_same_token_aba_is_fenced_by_claim_generation(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.store_validated_version(
            bundle=_version_bundle(),
            evidence=_version_evidence(),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        old = store.claim_outbox_event(
            claim_token="same-token",
            lease_seconds=0,
        )
        new = store.claim_outbox_event(claim_token="same-token")

        assert new["claim_generation"] == old["claim_generation"] + 1
        assert not store.ack_outbox_event(
            old["outbox_id"],
            old["claim_token"],
            old["claim_generation"],
            outcome="published",
        )
        assert store.ack_outbox_event(
            new["outbox_id"],
            new["claim_token"],
            new["claim_generation"],
            outcome="published",
        )


def test_outbox_claim_obeys_strict_oldest_event_single_flight_barrier(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.store_validated_version(
            bundle=_version_bundle("comment-old"),
            evidence=_version_evidence(_version_bundle("comment-old")),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        second_id = store.store_validated_version(
            bundle=_version_bundle("comment-new"),
            evidence=_version_evidence(_version_bundle("comment-new")),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        first = store.claim_outbox_event(claim_token="worker")
        assert store.claim_outbox_event(claim_token="other") is None
        assert store.fail_outbox_event(
            first["outbox_id"],
            first["claim_token"],
            first["claim_generation"],
            failure="redis_unavailable",
            retry=True,
        )

        assert store.claim_outbox_event(claim_token="other") is None
        second = store.connection.execute(
            "SELECT status FROM publication_outbox WHERE aggregate_id = ?",
            (second_id,),
        ).fetchone()
        assert second["status"] == "pending"


def test_existing_outbox_database_is_migrated_with_claim_columns_and_index(
    tmp_path,
):
    path = tmp_path / "legacy-outbox.db"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE publication_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            aggregate_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            completed_at TEXT
        )
        """
    )
    connection.commit()
    connection.close()

    with SelectorProbeStore(path) as store:
        columns = {
            row["name"]
            for row in store.connection.execute(
                "PRAGMA table_info(publication_outbox)"
            )
        }
        indexes = {
            row["name"]
            for row in store.connection.execute(
                "PRAGMA index_list(publication_outbox)"
            )
        }
        assert {
            "claim_token",
            "claim_generation",
            "lease_until",
            "last_error",
        } <= columns
        assert "idx_publication_outbox_due" in indexes


def test_dependency_rows_are_replaced_in_one_rollback_safe_transaction(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.replace_strategy_dependencies(
            [
                ("old-alias", "old-flow", "old-action", "click"),
            ]
        )
        store.connection.execute(
            """
            CREATE TRIGGER reject_new_dependency
            BEFORE INSERT ON strategy_dependencies
            WHEN NEW.alias = 'new-alias'
            BEGIN
                SELECT RAISE(ABORT, 'dependency rejected');
            END
            """
        )
        with pytest.raises(sqlite3.IntegrityError, match="dependency rejected"):
            store.replace_strategy_dependencies(
                [
                    ("new-alias", "new-flow", "new-action", "click"),
                ]
            )

        rows = store.connection.execute(
            """
            SELECT alias, strategy_id, action_id, action_type
            FROM strategy_dependencies
            """
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("old-alias", "old-flow", "old-action", "click")
        ]


def test_catalog_schema_uses_external_actor_snapshot_without_cross_database_fk(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        store.upsert_managed_element_projection(
            element_id="element-one",
            display_name="Element one",
            management_source="automatic",
            published_status="healthy",
            draft_status=None,
            active_version_id="sel-one",
            scope="active_video",
            primary_locator_type="attribute",
            last_validated_at="2026-07-29T03:00:00+00:00",
            actor_user_id=91,
            actor_username="admin-one",
        )

        foreign_keys = store.connection.execute(
            "PRAGMA foreign_key_list(element_drafts)"
        ).fetchall()
        audit = store.connection.execute(
            """
            SELECT actor_user_id, actor_username, target_id
            FROM selector_management_audit_events
            """
        ).fetchone()

        assert {row["table"] for row in foreign_keys} == {"managed_elements"}
        assert tuple(audit) == (91, "admin-one", "element-one")
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_dependency_schema_accepts_legacy_and_named_rows(tmp_path):
    path = tmp_path / "selector-probe.db"
    with SelectorProbeStore(path) as store:
        store.replace_strategy_dependencies(
            [
                ("legacy", "legacy-flow", "legacy-action", "click"),
                ("named", "named-flow", "named-action", "click", "Named flow"),
            ]
        )
        rows = store.dependency_rows_for_aliases(["legacy", "named"])

        assert [tuple(row) for row in rows] == [
            ("legacy", "legacy-flow", "legacy-action", "click", ""),
            ("named", "named-flow", "named-action", "click", "Named flow"),
        ]

    with SelectorProbeStore(path) as reopened:
        columns = {
            row["name"]
            for row in reopened.connection.execute(
                "PRAGMA table_info(strategy_dependencies)"
            )
        }
        assert "strategy_name" in columns


def test_dependency_snapshot_bumps_catalog_revision_only_when_content_changes(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    first = [
        ("comment-input", "comment-flow", "type", "fill", "Comment flow"),
        ("comment-submit", "comment-flow", "submit", "click", "Comment flow"),
    ]
    with SelectorProbeStore(path) as store:
        initial_revision = store.catalog_revision()

        store.replace_strategy_dependencies(first)
        first_revision = store.catalog_revision()
        store.replace_strategy_dependencies(list(reversed(first)))
        idempotent_revision = store.catalog_revision()

        renamed = [(*first[0][:4], "Renamed flow"), first[1]]
        store.replace_strategy_dependencies(renamed)
        renamed_revision = store.catalog_revision()

        changed_type = [
            (first[0][0], first[0][1], first[0][2], "type", "Renamed flow"),
            first[1],
        ]
        store.replace_strategy_dependencies(changed_type)
        changed_type_revision = store.catalog_revision()

        store.replace_strategy_dependencies(changed_type[:1])
        removed_revision = store.catalog_revision()

        assert first_revision == initial_revision + 1
        assert idempotent_revision == first_revision
        assert renamed_revision == idempotent_revision + 1
        assert changed_type_revision == renamed_revision + 1
        assert removed_revision == changed_type_revision + 1


def test_catalog_migration_preserves_partial_plan_rows_and_legacy_dependencies(tmp_path):
    path = tmp_path / "selector-probe.db"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE strategy_dependencies (
                alias TEXT NOT NULL,
                strategy_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                PRIMARY KEY(alias, strategy_id, action_id)
            );
            INSERT INTO strategy_dependencies
            VALUES ('legacy-element', 'legacy-flow', 'click', 'click');
            CREATE TABLE managed_elements (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                management_source TEXT NOT NULL,
                published_status TEXT NOT NULL,
                draft_status TEXT,
                active_version_id TEXT NOT NULL DEFAULT '',
                last_validated_at TEXT,
                revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO managed_elements VALUES (
                'legacy-element', 'Legacy element', 'legacy_manual',
                'probe_unavailable', 'draft', '', NULL, 1,
                '2026-07-29T00:00:00+00:00',
                '2026-07-29T00:00:00+00:00'
            );
            CREATE TABLE element_drafts (
                element_id TEXT PRIMARY KEY REFERENCES managed_elements(id),
                contract_json TEXT NOT NULL,
                candidates_json TEXT NOT NULL DEFAULT '[]',
                validation_json TEXT NOT NULL DEFAULT '{}',
                base_version_id TEXT NOT NULL DEFAULT '',
                revision INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL REFERENCES management_users(id),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO element_drafts VALUES (
                'legacy-element', '{}', '[]', '{}', '', 1, 17,
                '2026-07-29T00:00:00+00:00',
                '2026-07-29T00:00:00+00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()

    with SelectorProbeStore(path) as store:
        dependency = store.dependency_rows_for_aliases(["legacy-element"])[0]
        draft = store.connection.execute(
            "SELECT * FROM element_drafts WHERE element_id = 'legacy-element'"
        ).fetchone()

        assert tuple(dependency) == (
            "legacy-element",
            "legacy-flow",
            "click",
            "click",
            "",
        )
        assert draft["created_by"] == 17
        assert draft["created_by_username"] == "unknown"
        assert store.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def _reserve_element_request(store, request_id="request-one"):
    store.create_managed_element_draft(
        element_id="element-request",
        display_name="Request element",
        contract={
            "intent": "inspect the request element",
            "required_state": "feed_ready",
            "scope": "active_video",
            "accepted_roles": ["button"],
            "accepted_names": {
                "mode": "exact",
                "values": ["Request"],
            },
            "preferred_attributes": ["data-e2e"],
            "postcondition": "",
            "probe_action": "inspect_only",
        },
        scope="active_video",
        actor_user_id=9,
        actor_username="admin",
    )
    return store.reserve_element_request(
        element_id="element-request",
        request_type="validate",
        request_id=request_id,
        expected_revision=1,
        actor_user_id=9,
        actor_username="admin",
    )


def test_element_request_reservation_and_audit_are_atomic(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        store.create_managed_element_draft(
            element_id="element-request",
            display_name="Request element",
            contract={
                "intent": "inspect the request element",
                "required_state": "feed_ready",
                "scope": "active_video",
                "accepted_roles": ["button"],
                "accepted_names": {
                    "mode": "exact",
                    "values": ["Request"],
                },
                "preferred_attributes": ["data-e2e"],
                "postcondition": "",
                "probe_action": "inspect_only",
            },
            scope="active_video",
            actor_user_id=9,
            actor_username="admin",
        )
        store.connection.execute(
            """
            CREATE TRIGGER reject_element_request_audit
            BEFORE INSERT ON selector_management_audit_events
            WHEN NEW.event_type = 'element_validation_requested'
            BEGIN
                SELECT RAISE(ABORT, 'audit unavailable');
            END
            """
        )
        store.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
            store.reserve_element_request(
                element_id="element-request",
                request_type="validate",
                request_id="request-atomic",
                expected_revision=1,
                actor_user_id=9,
                actor_username="admin",
            )

        assert store.get_element_request("request-atomic") is None


def test_element_request_claim_is_single_owner_and_repeated_claim_is_empty(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store)

    def claim(token):
        with SelectorProbeStore(path) as worker_store:
            return worker_store.claim_element_request(
                claim_token=token,
                now=datetime(2099, 7, 29, 4, 0, tzinfo=UTC),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        claimed = list(executor.map(claim, ("worker-a", "worker-b")))

    assert sum(item is not None for item in claimed) == 1
    assert claim("worker-c") is None


def test_element_request_read_guard_does_not_open_write_transaction_or_renew(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    now = datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store)
        claim = store.claim_element_request(
            claim_token="read-guard-worker",
            now=now,
            lease_seconds=120,
        )
        before = store.get_element_request(claim["request_id"])

        assert store.connection.in_transaction is False
        assert store.guard_element_request_claim(
            claim["request_id"],
            claim["claim_token"],
            claim["claim_generation"],
            now=now + timedelta(seconds=1),
            renew=False,
            lease_seconds=300,
        )
        assert store.connection.in_transaction is False
        after = store.get_element_request(claim["request_id"])

        assert after["lease_until"] == before["lease_until"]
        assert after["updated_at"] == before["updated_at"]


def test_expired_element_request_claim_recovers_and_fences_old_worker(tmp_path):
    path = tmp_path / "selector-probe.db"
    started = datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store)
        old = store.claim_element_request(
            claim_token="worker-old",
            now=started,
            lease_seconds=30,
        )
        recovered = store.claim_element_request(
            claim_token="worker-new",
            now=started + timedelta(seconds=31),
            lease_seconds=30,
        )

        assert recovered["claim_generation"] == old["claim_generation"] + 1
        assert (
            store.complete_element_request(
                old["request_id"],
                old["claim_token"],
                old["claim_generation"],
                result={"status": "completed"},
                now=started + timedelta(seconds=32),
            )
            is False
        )
        assert store.complete_element_request(
            recovered["request_id"],
            recovered["claim_token"],
            recovered["claim_generation"],
            result={
                "status": "published",
                "published": True,
                "reconciled": True,
                "new_version": "sel-recovered",
            },
            now=started + timedelta(seconds=32),
        )
        request = store.get_element_request("request-one")
        terminal_audits = store.connection.execute(
            """
            SELECT event_type
            FROM selector_management_audit_events
            WHERE event_type = 'element_validate_completed'
            """
        ).fetchall()

        assert request["status"] == "completed"
        assert request["attempt_count"] == 2
        assert len(terminal_audits) == 1


def test_element_request_store_rejects_non_published_validation_result(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    now = datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store)
        claim = store.claim_element_request(
            claim_token="worker",
            now=now,
        )

        with pytest.raises(ValueError, match="completion contract"):
            store.complete_element_request(
                claim["request_id"],
                claim["claim_token"],
                claim["claim_generation"],
                result={"status": "healthy"},
                now=now,
            )

        request = store.get_element_request("request-one")
        element = store.get_managed_element_row("element-request")
        assert request["status"] == "processing"
        assert element["published_status"] == "probe_unavailable"
        assert element["draft_status"] == "validating"


class _CrashRecoveryRegistry:
    def __init__(self):
        self.keys = type(
            "Keys",
            (),
            {"site": "tiktok", "environment": "production"},
        )()
        self.active = None
        self.publish_calls = 0

    def get_active(self):
        return self.active

    def publish(self, event):
        self.publish_calls += 1
        bundle = event["bundle"]
        self.active = {
            "version": event["version"],
            "bundle_hash": bundle["bundle_hash"],
        }
        return "published"


def test_element_publication_recovers_after_stage_crash_without_reexecution(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    bundle = _version_bundle("request-selector")
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store, request_id="crash-window-request")
        claim = store.claim_element_request(claim_token="worker-one")
        version_id = store.store_validated_version(
            bundle=bundle,
            evidence=_version_evidence(bundle),
            base_version_id="",
            model_id="test-model",
            prompt_version="test-prompt",
            element_request_id=claim["request_id"],
            element_request_claim_token=claim["claim_token"],
            element_request_generation=claim["claim_generation"],
            staged_result={
                "candidate": bundle,
                "validation_evidence": _version_evidence(bundle),
                "repairs": [],
            },
        )
        assert store.get_element_request(claim["request_id"])["status"] == (
            "publishing"
        )

    executor_calls = []
    registry = _CrashRecoveryRegistry()
    with SelectorProbeStore(path) as restarted:
        assert (
            restarted.claim_element_request(
                claim_token="worker-two",
                now=datetime.now(UTC) + timedelta(days=1),
            )
            is None
        )
        executor_calls.clear()
        result = reconcile_registry(restarted, registry)
        assert result == {"acknowledged": 1, "version": version_id}
        request = restarted.get_element_request(claim["request_id"])
        assert request["status"] == "completed"
        assert request["result"]["new_version"] == version_id
        assert restarted.connection.execute(
            "SELECT COUNT(*) FROM selector_versions"
        ).fetchone()[0] == 1
        assert restarted.connection.execute(
            "SELECT COUNT(*) FROM publication_outbox"
        ).fetchone()[0] == 1
        assert restarted.connection.execute(
            """
            SELECT COUNT(*)
            FROM selector_management_audit_events
            WHERE event_type = 'element_validate_completed'
            """
        ).fetchone()[0] == 1
        reconcile_registry(restarted, registry)
        assert restarted.connection.execute(
            """
            SELECT COUNT(*)
            FROM selector_management_audit_events
            WHERE event_type = 'element_validate_completed'
            """
        ).fetchone()[0] == 1
    assert executor_calls == []
    assert registry.publish_calls == 1


def test_stale_element_generation_cannot_stage_or_publish(tmp_path):
    path = tmp_path / "selector-probe.db"
    bundle = _version_bundle("stale-selector")
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store, request_id="stale-stage-request")
        now = datetime.now(UTC) + timedelta(seconds=5)
        old = store.claim_element_request(
            claim_token="old-worker",
            now=now,
            lease_seconds=1,
        )
        current = store.claim_element_request(
            claim_token="current-worker",
            now=now + timedelta(seconds=5),
            lease_seconds=120,
        )
        assert current["claim_generation"] > old["claim_generation"]
        with pytest.raises(
            RuntimeError,
            match="stale element request",
        ):
            store.store_validated_version(
                bundle=bundle,
                evidence=_version_evidence(bundle),
                base_version_id="",
                model_id="test-model",
                prompt_version="test-prompt",
                element_request_id=old["request_id"],
                element_request_claim_token=old["claim_token"],
                element_request_generation=old["claim_generation"],
                staged_result={"candidate": bundle},
            )
        assert store.connection.execute(
            "SELECT COUNT(*) FROM selector_versions"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM publication_outbox"
        ).fetchone()[0] == 0


@pytest.mark.parametrize("terminal_action", ["conflict", "cancel", "reject"])
def test_linked_publication_terminal_failure_unlocks_draft_once(
    tmp_path,
    terminal_action,
):
    path = tmp_path / f"{terminal_action}.db"
    bundle = _version_bundle(f"{terminal_action}-selector")
    with SelectorProbeStore(path) as store:
        _reserve_element_request(
            store,
            request_id=f"{terminal_action}-request",
        )
        claim = store.claim_element_request(claim_token="publisher")
        version_id = store.store_validated_version(
            bundle=bundle,
            evidence=_version_evidence(bundle),
            base_version_id="",
            model_id="test-model",
            prompt_version="test-prompt",
            element_request_id=claim["request_id"],
            element_request_claim_token=claim["claim_token"],
            element_request_generation=claim["claim_generation"],
            staged_result={"candidate": bundle},
        )
        event = store.claim_outbox_event(claim_token="outbox-worker")
        if terminal_action == "conflict":
            assert store.ack_outbox_event(
                event["outbox_id"],
                event["claim_token"],
                event["claim_generation"],
                outcome="conflict",
            )
        elif terminal_action == "cancel":
            assert store.cancel_outbox_event(
                event["outbox_id"],
                event["claim_token"],
                event["claim_generation"],
            )
        else:
            assert store.fail_outbox_event(
                event["outbox_id"],
                event["claim_token"],
                event["claim_generation"],
                failure="hash_mismatch",
                retry=False,
            )

        request = store.get_element_request(claim["request_id"])
        element = store.get_managed_element_row(claim["element_id"])
        assert request["status"] == "failed"
        assert request["staged_version_id"] == version_id
        assert element["draft_status"] == "draft"
        assert store.connection.execute(
            """
            SELECT COUNT(*)
            FROM selector_management_audit_events
            WHERE event_type = 'element_validate_failed'
            """
        ).fetchone()[0] == 1


def test_linked_publication_retry_keeps_publishing_and_mutation_lock(tmp_path):
    path = tmp_path / "retry-linked.db"
    bundle = _version_bundle("retry-linked-selector")
    with SelectorProbeStore(path) as store:
        _reserve_element_request(store, request_id="linked-retry-request")
        claim = store.claim_element_request(claim_token="publisher")
        store.store_validated_version(
            bundle=bundle,
            evidence=_version_evidence(bundle),
            base_version_id="",
            model_id="test-model",
            prompt_version="test-prompt",
            element_request_id=claim["request_id"],
            element_request_claim_token=claim["claim_token"],
            element_request_generation=claim["claim_generation"],
            staged_result={"candidate": bundle},
        )
        event = store.claim_outbox_event(claim_token="outbox-worker")
        assert store.fail_outbox_event(
            event["outbox_id"],
            event["claim_token"],
            event["claim_generation"],
            failure="redis_unavailable",
            retry=True,
        )
        request = store.get_element_request(claim["request_id"])
        element = store.get_managed_element_row(claim["element_id"])
        assert request["status"] == "publishing"
        assert element["draft_status"] == "validating"
        with pytest.raises(
            ElementRequestInProgressError,
            match="element-request",
            ):
                store.delete_managed_element(
                    element_id=claim["element_id"],
                expected_revision=int(element["revision"]),
                actor_user_id=9,
                actor_username="admin",
            )


def _reserve_management(
    store,
    *,
    key,
    payload_hash,
    pending_response,
    now,
):
    return store.reserve_management_operation(
        actor_user_id=7,
        operation="lease-test",
        idempotency_key=key,
        payload_hash=payload_hash,
        request_payload={"private": "never-persist"},
        pending_response=pending_response,
        pending_status_code=409,
        now=now,
    )


def test_management_pending_lease_replays_then_refreshes_after_five_minutes(
    tmp_path,
):
    path = tmp_path / "management-lease.db"
    started = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
    digest = "sha256:" + "a" * 64
    with SelectorProbeStore(path) as store:
        first = _reserve_management(
            store,
            key="lease-key",
            payload_hash=digest,
            pending_response={"code": "first-pending"},
            now=started,
        )
        replay = _reserve_management(
            store,
            key="lease-key",
            payload_hash=digest,
            pending_response={"code": "must-not-replace"},
            now=started + timedelta(minutes=4, seconds=59),
        )
        before = store.connection.execute(
            """
            SELECT created_at, expires_at
            FROM management_idempotency_cache
            WHERE idempotency_key = 'lease-key'
            """
        ).fetchone()
        takeover_at = started + timedelta(minutes=5)
        takeover = _reserve_management(
            store,
            key="lease-key",
            payload_hash=digest,
            pending_response={"code": "recovered-pending"},
            now=takeover_at,
        )
        after = store.connection.execute(
            """
            SELECT response_json, status_code, state, request_json,
                   created_at, expires_at
            FROM management_idempotency_cache
            WHERE idempotency_key = 'lease-key'
            """
        ).fetchone()

    assert first["reserved"] is True
    assert replay == {
        "reserved": False,
        "state": "pending",
        "response": {"code": "first-pending"},
        "status_code": 409,
    }
    assert takeover == {
        "reserved": True,
        "state": "pending",
        "response": {"code": "recovered-pending"},
        "status_code": 409,
    }
    assert before["created_at"] == started.isoformat()
    assert after["created_at"] == takeover_at.isoformat()
    assert after["expires_at"] == (
        takeover_at + timedelta(hours=24)
    ).isoformat()
    assert json.loads(after["response_json"]) == {
        "code": "recovered-pending"
    }
    assert after["status_code"] == 409
    assert after["state"] == "pending"
    assert after["request_json"] == "{}"


def test_management_pending_lease_rejects_conflicting_payload_takeover(
    tmp_path,
):
    path = tmp_path / "management-lease-conflict.db"
    started = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
    with SelectorProbeStore(path) as store:
        _reserve_management(
            store,
            key="conflict-key",
            payload_hash="sha256:" + "a" * 64,
            pending_response={"code": "pending"},
            now=started,
        )

        with pytest.raises(ManagementIdempotencyConflictError):
            _reserve_management(
                store,
                key="conflict-key",
                payload_hash="sha256:" + "b" * 64,
                pending_response={"code": "different"},
                now=started + timedelta(minutes=6),
            )

        row = store.connection.execute(
            """
            SELECT payload_hash, response_json, created_at
            FROM management_idempotency_cache
            WHERE idempotency_key = 'conflict-key'
            """
        ).fetchone()
    assert row["payload_hash"] == "sha256:" + "a" * 64
    assert json.loads(row["response_json"]) == {"code": "pending"}
    assert row["created_at"] == started.isoformat()


@pytest.mark.parametrize("failed", (False, True))
def test_management_terminal_operation_cannot_be_taken_over(
    tmp_path,
    failed,
):
    path = tmp_path / f"management-terminal-{failed}.db"
    started = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
    digest = "sha256:" + "c" * 64
    key = f"terminal-{failed}"
    with SelectorProbeStore(path) as store:
        _reserve_management(
            store,
            key=key,
            payload_hash=digest,
            pending_response={"code": "pending"},
            now=started,
        )
        terminal_response = {
            "code": "failed" if failed else "completed"
        }
        store.complete_management_operation(
            actor_user_id=7,
            operation="lease-test",
            idempotency_key=key,
            payload_hash=digest,
            response=terminal_response,
            status_code=503 if failed else 200,
            failed=failed,
        )

        replay = _reserve_management(
            store,
            key=key,
            payload_hash=digest,
            pending_response={"code": "must-not-take-over"},
            now=started + timedelta(minutes=6),
        )

    assert replay == {
        "reserved": False,
        "state": "failed" if failed else "completed",
        "response": terminal_response,
        "status_code": 503 if failed else 200,
    }


def test_management_pending_lease_takeover_is_atomic(tmp_path):
    path = tmp_path / "management-lease-atomic.db"
    started = datetime(2026, 7, 29, 3, 0, tzinfo=UTC)
    digest = "sha256:" + "d" * 64
    with SelectorProbeStore(path) as store:
        _reserve_management(
            store,
            key="atomic-key",
            payload_hash=digest,
            pending_response={"code": "initial"},
            now=started,
        )
    barrier = Barrier(2)

    def reserve(index):
        with SelectorProbeStore(path) as store:
            barrier.wait()
            return _reserve_management(
                store,
                key="atomic-key",
                payload_hash=digest,
                pending_response={"owner": index},
                now=started + timedelta(minutes=6),
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, (1, 2)))

    assert sorted(result["reserved"] for result in results) == [
        False,
        True,
    ]
    winner = next(result for result in results if result["reserved"])
    replay = next(result for result in results if not result["reserved"])
    assert replay["response"] == winner["response"]


def test_management_request_and_probe_run_are_one_logical_row(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        request = store.create_management_run_request(
            "manual-request-1",
            actor_user_id=7,
            actor_username="operator",
        )
        run_id = store.start_run(
            scheduled_for="2026-07-30T03:00:00+08:00",
            active_version_before="v1",
            management_request_id="manual-request-1",
            trigger="manual",
        )
        store.finish_run(
            run_id,
            status="completed",
            details={"stages": [{"name": "cleanup", "status": "passed"}]},
        )

        rows, total, _revision = store.list_management_rows(
            "runs",
            page=1,
            page_size=20,
        )
        detail = store.management_run_detail("manual-request-1")

        assert request["status"] == "queued"
        assert total == 1
        assert rows[0]["id"] == "manual-request-1"
        assert rows[0]["probe_run_id"] == run_id
        assert rows[0]["status"] == "completed"
        assert detail["probe_run_id"] == run_id
        assert detail["details"]["stages"][0]["name"] == "cleanup"


def test_management_run_request_deduplicates_active_run(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        first = store.create_management_run_request(
            "manual-request-1",
            actor_user_id=7,
            actor_username="operator",
        )
        second = store.create_management_run_request(
            "manual-request-2",
            actor_user_id=8,
            actor_username="other",
        )

        assert first["deduplicated"] is False
        assert second["deduplicated"] is True
        assert second["id"] == first["id"]
        assert store.connection.execute(
            "SELECT COUNT(*) FROM management_run_requests"
        ).fetchone()[0] == 1
