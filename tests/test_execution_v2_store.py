import sqlite3

import pytest

from execution_v2.models import JobStatus, ProfileStatus, Stage
from execution_v2.store import ExecutionStore


def test_store_enables_wal_foreign_keys_and_survives_restart(tmp_path):
    path = tmp_path / "execution_v2.db"
    store = ExecutionStore(path)
    store.initialize()
    store.create_job("job-1", "strategy-1", {"revision": 4}, ["p1", "p2"], 3)
    store.set_profile_status(
        "job-1", "p1", ProfileStatus.STARTING, Stage.ADSPOWER_START
    )

    reopened = ExecutionStore(path)
    reopened.initialize()
    assert reopened.get_job("job-1")["status"] == JobStatus.QUEUED.value
    assert (
        reopened.list_profile_results("job-1")[0]["stage"]
        == Stage.ADSPOWER_START.value
    )

    with reopened.connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_create_job_rolls_back_when_a_profile_insert_fails(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()

    with pytest.raises(sqlite3.IntegrityError):
        store.create_job("job-1", "strategy-1", {"revision": 1}, ["same", "same"], 3)

    assert store.get_job("job-1") is None


def test_request_cancel_persists_across_store_instances(tmp_path):
    path = tmp_path / "execution_v2.db"
    store = ExecutionStore(path)
    store.initialize()
    store.create_job("job-1", "strategy-1", {"revision": 1}, ["p1"], 3)

    assert store.is_cancel_requested("job-1") is False
    store.request_cancel("job-1")

    reopened = ExecutionStore(path)
    reopened.initialize()
    assert reopened.is_cancel_requested("job-1") is True


def test_initialize_creates_element_revision_and_action_tables(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()

    with store.connect() as connection:
        names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "elements",
        "element_revisions",
        "strategy_actions",
        "wheel_calibrations",
        "wheel_calibration_current",
    } <= names


def test_wheel_calibration_publish_is_versioned_and_restart_safe(tmp_path):
    path = tmp_path / "execution_v2.db"
    store = ExecutionStore(path)
    store.initialize()
    first = store.publish_wheel_calibration(
        "tiktok_feed",
        "down",
        [{"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}],
        3,
        replay_validated=True,
    )
    second = store.publish_wheel_calibration(
        "tiktok_feed",
        "down",
        [{"delta_x": 0.0, "delta_y": 104.0, "delta_mode": 0, "delay_ms": 0.0}],
        3,
        replay_validated=True,
    )

    reopened = ExecutionStore(path)
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert reopened.get_wheel_calibration()["events"][0]["delta_y"] == 104.0


def test_wheel_calibration_migration_hides_legacy_current_version(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE wheel_calibrations (
              scope TEXT NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL,
              direction TEXT NOT NULL, events_json TEXT NOT NULL, sample_count INTEGER NOT NULL,
              created_at TEXT NOT NULL, PRIMARY KEY(scope, revision)
            );
            CREATE TABLE wheel_calibration_current (
              scope TEXT PRIMARY KEY, revision INTEGER NOT NULL
            );
            INSERT INTO wheel_calibrations VALUES
              ('tiktok_feed', 1, 'validated', 'down', '[{"delta_y":100}]', 3, 'old');
            INSERT INTO wheel_calibration_current VALUES ('tiktok_feed', 1);
            """
        )

    store = ExecutionStore(path)
    store.initialize()

    with store.connect() as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(wheel_calibrations)")}
    assert "replay_validated" in columns
    assert store.get_wheel_calibration() is None


def test_prepared_jobs_and_action_results_list_with_decoded_stable_records(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    store.prepare_job("job-a", "strategy-1", {"revision": 1}, ["p2", "p1"], 3)
    store.create_job("job-b", "strategy-2", {"revision": 2}, ["p3"], 3)
    store.append_action_result(
        "job-a", "p1", 1, "wait", "succeeded", Stage.EXECUTE_ACTION, {"ok": True}
    )
    store.append_action_result(
        "job-a", "p2", 0, "click", "succeeded", Stage.EXECUTE_ACTION, {"ok": False}
    )

    assert [job["id"] for job in store.list_jobs()] == ["job-b", "job-a"]
    assert store.list_jobs(limit=1, offset=1)[0]["strategy_snapshot"] == {"revision": 1}
    results = store.list_action_results("job-a")
    assert [(row["profile_id"], row["result"]) for row in results] == [
        ("p2", {"ok": False}),
        ("p1", {"ok": True}),
    ]
    assert store.list_action_results("job-a", profile_id="p1")[0]["profile_id"] == "p1"
