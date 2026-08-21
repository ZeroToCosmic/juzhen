from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


BUSY_TIMEOUT_MS = 5_000
LATEST_SCHEMA_VERSION = 2


def utc_now_iso() -> str:
    """Return a sortable UTC timestamp with an explicit UTC designator."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def connect_stats_db(path: str | Path) -> sqlite3.Connection:
    """Open a configured SQLite connection without applying migrations."""
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit a write transaction only after its complete block succeeds."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS tracked_accounts (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            username_key TEXT NOT NULL UNIQUE,
            source TEXT NOT NULL DEFAULT '',
            source_account_id TEXT,
            sec_uid TEXT,
            status TEXT NOT NULL DEFAULT 'enabled'
                CHECK (status IN ('enabled', 'disabled')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS collection_runs (
            id INTEGER PRIMARY KEY,
            run_type TEXT NOT NULL CHECK (run_type IN ('incremental', 'full', 'cleanup')),
            status TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'partial', 'failed')),
            started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            finished_at TEXT,
            scheduled_for TEXT,
            details_json TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tracked_accounts(id) ON DELETE RESTRICT,
            run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
            captured_at TEXT NOT NULL,
            snapshot_type TEXT NOT NULL DEFAULT 'incremental'
                CHECK (snapshot_type IN ('incremental', 'full')),
            coverage TEXT NOT NULL DEFAULT 'profile_recent'
                CHECK (coverage IN ('profile_recent', 'full')),
            follower_count INTEGER,
            following_count INTEGER,
            likes_count INTEGER,
            post_count INTEGER,
            UNIQUE (account_id, captured_at)
        );

        CREATE TABLE IF NOT EXISTS posts_current (
            video_id TEXT PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tracked_accounts(id) ON DELETE RESTRICT,
            created_at TEXT,
            description TEXT,
            view_count INTEGER,
            like_count INTEGER,
            comment_count INTEGER,
            share_count INTEGER,
            is_deleted INTEGER NOT NULL DEFAULT 0 CHECK (is_deleted IN (0, 1)),
            last_seen_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE TABLE IF NOT EXISTS daily_account_metrics (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tracked_accounts(id) ON DELETE RESTRICT,
            business_date TEXT NOT NULL,
            snapshot_id INTEGER REFERENCES account_snapshots(id) ON DELETE SET NULL,
            baseline_status TEXT NOT NULL DEFAULT 'unavailable'
                CHECK (baseline_status IN ('unavailable', 'available')),
            posts_delta INTEGER,
            likes_delta INTEGER,
            views_delta INTEGER,
            comments_delta INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (account_id, business_date)
        );

        CREATE TABLE IF NOT EXISTS worker_leases (
            lease_name TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            expires_at TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        );

        CREATE INDEX IF NOT EXISTS idx_tracked_accounts_status_key
            ON tracked_accounts(status, username_key);
        CREATE INDEX IF NOT EXISTS idx_collection_runs_type_started_at
            ON collection_runs(run_type, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_account_snapshots_account_captured_at
            ON account_snapshots(account_id, captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_account_snapshots_captured_at
            ON account_snapshots(captured_at DESC);
        CREATE INDEX IF NOT EXISTS idx_posts_current_account_created_at
            ON posts_current(account_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_daily_account_metrics_business_date
            ON daily_account_metrics(business_date DESC, account_id);
        CREATE INDEX IF NOT EXISTS idx_daily_account_metrics_sort
            ON daily_account_metrics(
                business_date DESC,
                posts_delta DESC,
                likes_delta DESC,
                views_delta DESC,
                comments_delta DESC
            );
        """,
    ),
    (
        2,
        """
        ALTER TABLE posts_current ADD COLUMN deleted_detected_at TEXT;

        ALTER TABLE daily_account_metrics RENAME TO daily_account_metrics_v1;
        ALTER TABLE account_snapshots RENAME TO account_snapshots_v1;

        CREATE TABLE account_snapshots (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tracked_accounts(id) ON DELETE RESTRICT,
            run_id INTEGER REFERENCES collection_runs(id) ON DELETE SET NULL,
            captured_at TEXT NOT NULL,
            business_date TEXT,
            snapshot_type TEXT NOT NULL DEFAULT 'incremental'
                CHECK (snapshot_type IN ('incremental', 'full')),
            coverage TEXT NOT NULL DEFAULT 'profile_recent'
                CHECK (coverage IN ('profile_recent', 'full')),
            follower_count INTEGER,
            following_count INTEGER,
            likes_count INTEGER,
            post_count INTEGER,
            covered_post_count INTEGER,
            posts_like_count INTEGER,
            posts_view_count INTEGER,
            posts_comment_count INTEGER,
            UNIQUE (account_id, captured_at, coverage)
        );

        INSERT INTO account_snapshots (
            id, account_id, run_id, captured_at, business_date, snapshot_type, coverage,
            follower_count, following_count, likes_count, post_count
        )
        SELECT
            id, account_id, run_id, captured_at,
            date(datetime(captured_at, '+8 hours')),
            snapshot_type, coverage, follower_count, following_count, likes_count, post_count
        FROM account_snapshots_v1;

        CREATE TABLE daily_account_metrics (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES tracked_accounts(id) ON DELETE RESTRICT,
            business_date TEXT NOT NULL,
            snapshot_id INTEGER REFERENCES account_snapshots(id) ON DELETE SET NULL,
            baseline_status TEXT NOT NULL DEFAULT 'incomplete'
                CHECK (baseline_status IN (
                    'ready', 'first_day', 'missing_previous', 'incomplete'
                )),
            posts_total INTEGER,
            likes_total INTEGER,
            views_total INTEGER,
            comments_total INTEGER,
            posts_delta INTEGER,
            likes_delta INTEGER,
            views_delta INTEGER,
            comments_delta INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
            UNIQUE (account_id, business_date)
        );

        INSERT INTO daily_account_metrics (
            id, account_id, business_date, snapshot_id, baseline_status,
            posts_delta, likes_delta, views_delta, comments_delta, created_at, updated_at
        )
        SELECT
            id, account_id, business_date, snapshot_id,
            CASE baseline_status
                WHEN 'available' THEN 'ready'
                ELSE 'missing_previous'
            END,
            posts_delta, likes_delta, views_delta, comments_delta, created_at, updated_at
        FROM daily_account_metrics_v1;

        DROP TABLE daily_account_metrics_v1;
        DROP TABLE account_snapshots_v1;

        CREATE INDEX idx_account_snapshots_account_captured_at
            ON account_snapshots(account_id, captured_at DESC);
        CREATE INDEX idx_account_snapshots_captured_at
            ON account_snapshots(captured_at DESC);

        CREATE INDEX idx_daily_account_metrics_business_date
            ON daily_account_metrics(business_date DESC, account_id);
        CREATE INDEX idx_daily_account_metrics_sort
            ON daily_account_metrics(
                business_date DESC,
                posts_delta DESC,
                likes_delta DESC,
                views_delta DESC,
                comments_delta DESC
            );
        """,
    ),
)


def migrate_stats_db(path: str | Path) -> int:
    """Apply each unapplied migration and return the current schema version."""
    connection = connect_stats_db(path)
    try:
        with transaction(connection):
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied_versions = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for version, sql in MIGRATIONS:
                if version in applied_versions:
                    continue
                for statement in sql.split(";"):
                    if statement.strip():
                        connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, utc_now_iso()),
                )
    finally:
        connection.close()
    return LATEST_SCHEMA_VERSION
