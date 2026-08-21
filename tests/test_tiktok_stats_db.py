import sqlite3

import pytest

from tiktok_stats.db import MIGRATIONS, connect_stats_db, migrate_stats_db
from tiktok_stats.store import StatsStore


def _table_names(connection):
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _index_names(connection, table_name):
    return {row[1] for row in connection.execute(f"PRAGMA index_list({table_name})")}


def test_migration_creates_versioned_schema_and_connection_policy(tmp_path):
    db_path = tmp_path / "tiktok-stats.db"

    version = migrate_stats_db(db_path)
    connection = connect_stats_db(db_path)

    assert version == 2
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    assert _table_names(connection) >= {
        "schema_migrations",
        "tracked_accounts",
        "collection_runs",
        "account_snapshots",
        "posts_current",
        "daily_account_metrics",
        "worker_leases",
    }
    assert "idx_account_snapshots_account_captured_at" in _index_names(
        connection, "account_snapshots"
    )
    assert "idx_daily_account_metrics_business_date" in _index_names(
        connection, "daily_account_metrics"
    )
    assert "idx_daily_account_metrics_sort" in _index_names(
        connection, "daily_account_metrics"
    )
    snapshot_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(account_snapshots)")
    }
    assert snapshot_columns >= {
        "business_date",
        "covered_post_count",
        "posts_like_count",
        "posts_view_count",
        "posts_comment_count",
    }
    assert "deleted_detected_at" in {
        row[1] for row in connection.execute("PRAGMA table_info(posts_current)")
    }
    daily_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(daily_account_metrics)")
    }
    assert daily_columns >= {
        "posts_total",
        "likes_total",
        "views_total",
        "comments_total",
    }
    connection.close()


def test_constraints_enforce_normalized_accounts_snapshot_and_daily_metric_uniqueness(
    tmp_path,
):
    db_path = tmp_path / "tiktok-stats.db"
    migrate_stats_db(db_path)
    connection = connect_stats_db(db_path)

    connection.execute(
        "INSERT INTO tracked_accounts (username, username_key) VALUES (?, ?)",
        ("Example", "example"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO tracked_accounts (username, username_key) VALUES (?, ?)",
            ("EXAMPLE", "example"),
        )

    account_id = connection.execute(
        "SELECT id FROM tracked_accounts WHERE username_key = ?", ("example",)
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO account_snapshots (account_id, captured_at) VALUES (?, ?)",
        (account_id, "2026-07-22T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO account_snapshots (
            account_id, captured_at, snapshot_type, coverage
        ) VALUES (?, ?, 'full', 'full')
        """,
        (account_id, "2026-07-22T00:00:00Z"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO account_snapshots (account_id, captured_at) VALUES (?, ?)",
            (account_id, "2026-07-22T00:00:00Z"),
        )

    connection.execute(
        "INSERT INTO daily_account_metrics (account_id, business_date) VALUES (?, ?)",
        (account_id, "2026-07-22"),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO daily_account_metrics (account_id, business_date) VALUES (?, ?)",
            (account_id, "2026-07-22"),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO account_snapshots (account_id, captured_at) VALUES (?, ?)",
            (999999, "2026-07-22T01:00:00Z"),
        )
    connection.close()


def test_status_defaults_and_migration_are_idempotent(tmp_path):
    db_path = tmp_path / "tiktok-stats.db"

    assert migrate_stats_db(db_path) == 2
    assert migrate_stats_db(db_path) == 2
    connection = connect_stats_db(db_path)
    connection.execute(
        "INSERT INTO tracked_accounts (username, username_key) VALUES (?, ?)",
        ("example", "example"),
    )
    connection.execute("INSERT INTO collection_runs (run_type) VALUES (?)", ("incremental",))

    assert connection.execute(
        "SELECT status FROM tracked_accounts"
    ).fetchone()[0] == "enabled"
    assert connection.execute(
        "SELECT status FROM collection_runs"
    ).fetchone()[0] == "running"
    assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 2
    connection.close()


def test_v2_migration_preserves_legacy_daily_rows_and_maps_baseline_statuses(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = connect_stats_db(db_path)
    connection.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for statement in MIGRATIONS[0][1].split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, '2026-07-20T00:00:00Z')"
    )
    connection.execute(
        "INSERT INTO tracked_accounts (id, username, username_key) VALUES (1, 'A', 'a')"
    )
    connection.execute(
        """
        INSERT INTO account_snapshots (
            id, account_id, captured_at, snapshot_type, coverage
        ) VALUES (4, 1, '2026-07-20T16:00:00Z', 'full', 'full')
        """
    )
    connection.execute(
        """
        INSERT INTO daily_account_metrics (
            id, account_id, business_date, snapshot_id, baseline_status, views_delta,
            created_at, updated_at
        ) VALUES (7, 1, '2026-07-20', 4, 'available', -4,
                  '2026-07-20T00:00:00Z', '2026-07-20T01:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO daily_account_metrics (
            id, account_id, business_date, baseline_status, created_at, updated_at
        ) VALUES (8, 1, '2026-07-21', 'unavailable',
                  '2026-07-21T00:00:00Z', '2026-07-21T01:00:00Z')
        """
    )
    connection.close()

    assert migrate_stats_db(db_path) == 2

    connection = connect_stats_db(db_path)
    migrated = list(
        connection.execute(
            "SELECT * FROM daily_account_metrics ORDER BY business_date"
        )
    )
    assert [(row["id"], row["baseline_status"]) for row in migrated] == [
        (7, "ready"),
        (8, "missing_previous"),
    ]
    assert migrated[0]["views_delta"] == -4
    assert migrated[0]["created_at"] == "2026-07-20T00:00:00Z"
    assert migrated[0]["updated_at"] == "2026-07-20T01:00:00Z"
    assert migrated[0]["views_total"] is None
    assert migrated[0]["snapshot_id"] == 4
    migrated_snapshot = connection.execute(
        "SELECT * FROM account_snapshots WHERE id = 4"
    ).fetchone()
    assert migrated_snapshot["business_date"] == "2026-07-21"
    assert "idx_daily_account_metrics_sort" in _index_names(
        connection, "daily_account_metrics"
    )
    assert list(connection.execute("PRAGMA foreign_key_check")) == []
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO daily_account_metrics (account_id, business_date, baseline_status) VALUES (1, '2026-07-22', 'available')"
        )
    connection.close()


def test_store_rolls_back_failed_account_write_and_persists_committed_resources(tmp_path):
    db_path = tmp_path / "tiktok-stats.db"
    store = StatsStore(db_path)
    account = store.upsert_account("Example", "example")

    with pytest.raises(RuntimeError):
        with store.transaction() as connection:
            store.insert_snapshot(
                account["id"],
                captured_at="2026-07-22T00:00:00Z",
                connection=connection,
            )
            store.upsert_daily_metric(
                account["id"],
                "2026-07-22",
                posts_delta=None,
                likes_delta=None,
                views_delta=None,
                comments_delta=None,
                connection=connection,
            )
            raise RuntimeError("force rollback")

    assert store.count_rows("account_snapshots") == 0
    assert store.count_rows("daily_account_metrics") == 0

    with store.transaction() as connection:
        store.insert_snapshot(
            account["id"], captured_at="2026-07-22T03:00:00Z", connection=connection
        )
        store.upsert_daily_metric(
            account["id"],
            "2026-07-22",
            posts_delta=None,
            likes_delta=3,
            views_delta=9,
            comments_delta=1,
            connection=connection,
        )
    store.close()

    reopened = StatsStore(db_path)
    assert reopened.count_rows("tracked_accounts") == 1
    assert reopened.count_rows("account_snapshots") == 1
    metric = reopened.daily_metric(account["id"], "2026-07-22")
    assert metric["posts_delta"] is None
    assert metric["views_delta"] == 9
    reopened.close()


def test_store_manages_account_status_and_run_lifecycle(tmp_path):
    store = StatsStore(tmp_path / "tiktok-stats.db")
    account = store.upsert_account("Example", "example")

    store.disable_account(account["id"])
    assert store.connection.execute(
        "SELECT status FROM tracked_accounts WHERE id = ?", (account["id"],)
    ).fetchone()[0] == "disabled"
    store.enable_account(account["id"])
    run_id = store.start_run("incremental", scheduled_for="2026-07-22T00:00:00Z")
    store.finish_run(run_id, "completed", details_json='{"accounts": 1}')

    run = store.connection.execute(
        "SELECT status, scheduled_for, finished_at, details_json FROM collection_runs WHERE id = ?",
        (run_id,),
    ).fetchone()
    assert run["status"] == "completed"
    assert run["scheduled_for"] == "2026-07-22T00:00:00Z"
    assert run["finished_at"].endswith("Z")
    assert run["details_json"] == '{"accounts": 1}'
    store.close()


def test_full_calibration_replaces_current_posts_and_daily_metric_in_one_transaction(tmp_path):
    store = StatsStore(tmp_path / "tiktok-stats.db")
    account = store.upsert_account("Example", "example")
    first_snapshot = store.record_full_calibration(
        account["id"],
        captured_at="2026-07-22T00:00:00Z",
        business_date="2026-07-22",
        posts=[{"video_id": "old", "view_count": 1}],
        daily_metrics={"views_delta": None},
    )
    second_snapshot = store.record_full_calibration(
        account["id"],
        captured_at="2026-07-22T03:00:00Z",
        business_date="2026-07-22",
        posts=[{"video_id": "new", "view_count": 9}],
        daily_metrics={"baseline_status": "available", "views_delta": -2},
    )

    assert second_snapshot != first_snapshot
    assert [row[0] for row in store.connection.execute("SELECT video_id FROM posts_current")] == [
        "new"
    ]
    metric = store.daily_metric(account["id"], "2026-07-22")
    assert metric["snapshot_id"] == second_snapshot
    assert metric["views_delta"] == -2

    with pytest.raises(KeyError):
        store.record_full_calibration(
            account["id"],
            captured_at="2026-07-22T06:00:00Z",
            business_date="2026-07-23",
            posts=[{}],
            daily_metrics={"views_delta": 7},
        )
    assert store.count_rows("account_snapshots") == 2
    assert store.count_rows("daily_account_metrics") == 1
    assert [row[0] for row in store.connection.execute("SELECT video_id FROM posts_current")] == [
        "new"
    ]
    store.close()


def test_worker_leases_allow_owner_renewal_expiry_takeover_and_release(tmp_path):
    store = StatsStore(tmp_path / "tiktok-stats.db")

    assert store.acquire_lease(
        "incremental", "worker-a", "2026-07-22T01:00:00Z", now="2026-07-22T00:00:00Z"
    )
    assert not store.acquire_lease(
        "incremental", "worker-b", "2026-07-22T01:00:00Z", now="2026-07-22T00:30:00Z"
    )
    assert store.renew_lease(
        "incremental", "worker-a", "2026-07-22T02:00:00Z", now="2026-07-22T00:45:00Z"
    )
    assert store.acquire_lease(
        "incremental", "worker-b", "2026-07-22T04:00:00Z", now="2026-07-22T03:00:00Z"
    )
    assert not store.release_lease("incremental", "worker-a")
    assert store.release_lease("incremental", "worker-b")
    assert store.count_rows("worker_leases") == 0
    store.close()


def test_worker_lease_renewal_refuses_the_expiry_boundary_and_allows_takeover(
    tmp_path, monkeypatch
):
    store = StatsStore(tmp_path / "tiktok-stats.db")
    expiry = "2026-07-22T01:00:00Z"
    store.acquire_lease("incremental", "worker-a", expiry, now="2026-07-22T00:00:00Z")
    monkeypatch.setattr("tiktok_stats.store.utc_now_iso", lambda: expiry)

    assert not store.renew_lease("incremental", "worker-a", "2026-07-22T02:00:00Z")
    assert store.acquire_lease(
        "incremental", "worker-b", "2026-07-22T03:00:00Z", now=expiry
    )
    assert store.connection.execute(
        "SELECT owner_id FROM worker_leases WHERE lease_name = ?", ("incremental",)
    ).fetchone()[0] == "worker-b"
    store.close()


def test_snapshot_cleanup_preserves_daily_metrics_current_posts_accounts_and_runs(tmp_path):
    store = StatsStore(tmp_path / "tiktok-stats.db")
    account = store.upsert_account("Example", "example")
    run_id = store.start_run("full")
    snapshot_id = store.record_full_calibration(
        account["id"],
        captured_at="2026-04-01T00:00:00Z",
        business_date="2026-04-01",
        posts=[{"video_id": "post"}],
        daily_metrics={"views_delta": None},
        run_id=run_id,
    )

    assert store.cleanup_snapshots("2026-04-23T00:00:00Z") == 0
    assert store.count_rows("account_snapshots") == 1
    assert store.count_rows("daily_account_metrics") == 1
    assert store.count_rows("posts_current") == 1
    assert store.count_rows("tracked_accounts") == 1
    assert store.count_rows("collection_runs") == 1
    metric = store.daily_metric(account["id"], "2026-04-01")
    assert snapshot_id > 0
    assert metric["snapshot_id"] == snapshot_id
    store.close()
