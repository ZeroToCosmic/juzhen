from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .db import connect_stats_db, migrate_stats_db, transaction, utc_now_iso


class LeaseWriteGuardLost(RuntimeError):
    """Raised before commit when a worker no longer owns its collection lease."""


class StatsStore:
    """Transaction-scoped writes for TikTok statistics data."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        migrate_stats_db(self.path)
        self.connection = connect_stats_db(self.path)
        self._lease_write_guard: tuple[str, str, Any] | None = None

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "StatsStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with transaction(self.connection) as connection:
            yield connection
            self._assert_lease_write_guard(connection)

    @contextmanager
    def lease_write_guard(self, lease_name: str, owner_id: str, clock) -> Iterator[None]:
        """Require the current, unexpired owner immediately before each commit."""
        previous = self._lease_write_guard
        self._lease_write_guard = (lease_name, owner_id, clock)
        try:
            yield
        finally:
            self._lease_write_guard = previous

    def _assert_lease_write_guard(self, connection: sqlite3.Connection) -> None:
        guard = self._lease_write_guard
        if guard is None:
            return
        lease_name, owner_id, clock = guard
        now = _guard_clock_iso(clock)
        owned = connection.execute(
            """
            SELECT 1 FROM worker_leases
            WHERE lease_name = ? AND owner_id = ? AND expires_at > ?
            LIMIT 1
            """,
            (lease_name, owner_id, now),
        ).fetchone()
        if owned is None:
            raise LeaseWriteGuardLost("worker lease ownership was lost before commit")

    def upsert_account(
        self,
        username: str,
        username_key: str,
        *,
        source: str = "",
        source_account_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        def write(active_connection: sqlite3.Connection) -> dict[str, Any]:
            now = utc_now_iso()
            active_connection.execute(
                """
                INSERT INTO tracked_accounts (
                    username, username_key, source, source_account_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username_key) DO UPDATE SET
                    username = excluded.username,
                    source = excluded.source,
                    source_account_id = excluded.source_account_id,
                    updated_at = excluded.updated_at
                """,
                (username, username_key, source, source_account_id, now, now),
            )
            row = active_connection.execute(
                "SELECT * FROM tracked_accounts WHERE username_key = ?", (username_key,)
            ).fetchone()
            return dict(row)

        return self._write(connection, write)

    def tracked_account(self, username_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tracked_accounts WHERE username_key = ?", (username_key,)
        ).fetchone()
        return dict(row) if row else None

    def account_by_id(self, account_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tracked_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return dict(row) if row else None

    def enabled_accounts(self, account_ids: Sequence[int] | None = None) -> list[dict[str, Any]]:
        if account_ids is None:
            return [
                dict(row)
                for row in self.connection.execute(
                    "SELECT * FROM tracked_accounts WHERE status = 'enabled' ORDER BY id"
                )
            ]
        requested_ids = [int(account_id) for account_id in account_ids]
        if not requested_ids:
            return []
        placeholders = ", ".join("?" for _ in requested_ids)
        found = {
            int(row["id"]): dict(row)
            for row in self.connection.execute(
                f"SELECT * FROM tracked_accounts WHERE status = 'enabled' AND id IN ({placeholders})",
                requested_ids,
            )
        }
        return [found[account_id] for account_id in requested_ids if account_id in found]

    def cache_sec_uid(
        self,
        account_id: int,
        sec_uid: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._write(
            connection,
            lambda active_connection: active_connection.execute(
                "UPDATE tracked_accounts SET sec_uid = ?, updated_at = ? WHERE id = ?",
                (sec_uid, utc_now_iso(), account_id),
            ),
        )

    def known_post_ids(self, account_id: int) -> set[str]:
        return {
            str(row[0])
            for row in self.connection.execute(
                "SELECT video_id FROM posts_current WHERE account_id = ?", (account_id,)
            )
        }

    def add_tracked_account(
        self,
        username: str,
        username_key: str,
        *,
        source: str = "",
        source_account_id: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        def write(active_connection: sqlite3.Connection) -> dict[str, Any]:
            now = utc_now_iso()
            active_connection.execute(
                """
                INSERT INTO tracked_accounts (
                    username, username_key, source, source_account_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (username, username_key, source, source_account_id, now, now),
            )
            row = active_connection.execute(
                "SELECT * FROM tracked_accounts WHERE username_key = ?", (username_key,)
            ).fetchone()
            return dict(row)

        return self._write(connection, write)

    def enable_account(self, account_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        self._set_account_status(account_id, "enabled", connection)

    def disable_account(self, account_id: int, *, connection: sqlite3.Connection | None = None) -> None:
        self._set_account_status(account_id, "disabled", connection)

    def update_account_identity(
        self,
        account_id: int,
        username: str,
        username_key: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        """Rename one tracked account without changing its source or enabled state."""

        def write(active_connection: sqlite3.Connection) -> dict[str, Any] | None:
            cursor = active_connection.execute(
                """
                UPDATE tracked_accounts
                SET username = ?, username_key = ?, updated_at = ?
                WHERE id = ?
                """,
                (username, username_key, utc_now_iso(), int(account_id)),
            )
            if cursor.rowcount != 1:
                return None
            row = active_connection.execute(
                "SELECT * FROM tracked_accounts WHERE id = ?", (int(account_id),)
            ).fetchone()
            return dict(row)

        return self._write(connection, write)

    def _set_account_status(
        self, account_id: int, status: str, connection: sqlite3.Connection | None
    ) -> None:
        self._write(
            connection,
            lambda active_connection: active_connection.execute(
                "UPDATE tracked_accounts SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now_iso(), account_id),
            ),
        )

    def start_run(
        self,
        run_type: str,
        *,
        scheduled_for: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        return self._write(
            connection,
            lambda active_connection: int(
                active_connection.execute(
                    "INSERT INTO collection_runs (run_type, started_at, scheduled_for) VALUES (?, ?, ?)",
                    (run_type, utc_now_iso(), scheduled_for),
                ).lastrowid
            ),
        )

    def finish_run(
        self,
        run_id: int,
        status: str,
        *,
        details_json: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self._write(
            connection,
            lambda active_connection: active_connection.execute(
                """
                UPDATE collection_runs
                SET status = ?, finished_at = ?, details_json = ?
                WHERE id = ?
                """,
                (status, utc_now_iso(), details_json, run_id),
            ),
        )

    def finish_run_if_lease_owner(
        self,
        run_id: int,
        status: str,
        *,
        lease_name: str,
        owner_id: str,
        now: str,
        details_json: str = "",
    ) -> bool:
        """Finish a run only while its unexpired lease still belongs to this worker."""
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE collection_runs
                SET status = ?, finished_at = ?, details_json = ?
                WHERE id = ?
                  AND EXISTS (
                      SELECT 1 FROM worker_leases
                      WHERE lease_name = ? AND owner_id = ? AND expires_at > ?
                  )
                """,
                (status, utc_now_iso(), details_json, run_id, lease_name, owner_id, now),
            )
            return cursor.rowcount == 1

    def last_scheduled_slot(self, run_type: str) -> datetime | None:
        row = self.connection.execute(
            """
            SELECT scheduled_for FROM collection_runs
            WHERE run_type = ? AND scheduled_for IS NOT NULL AND status <> 'running'
            ORDER BY scheduled_for DESC LIMIT 1
            """,
            (run_type,),
        ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(str(row["scheduled_for"]).replace("Z", "+00:00")).astimezone(UTC)

    def running_scheduled_slots(self, run_type: str, through: str) -> list[datetime]:
        return [
            datetime.fromisoformat(str(row["scheduled_for"]).replace("Z", "+00:00")).astimezone(UTC)
            for row in self.connection.execute(
                """
                SELECT scheduled_for FROM collection_runs
                WHERE run_type = ? AND status = 'running'
                  AND scheduled_for IS NOT NULL AND scheduled_for <= ?
                ORDER BY scheduled_for
                """,
                (run_type, through),
            )
        ]

    def claim_scheduled_run(self, run_type: str, scheduled_for: str) -> int | None:
        """Create one durable scheduled run, serialized across SQLite writers."""
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT id FROM collection_runs
                WHERE run_type = ? AND scheduled_for = ?
                LIMIT 1
                """,
                (run_type, scheduled_for),
            ).fetchone()
            if existing is not None:
                status = connection.execute(
                    "SELECT status FROM collection_runs WHERE id = ?", (int(existing["id"]),)
                ).fetchone()["status"]
                return int(existing["id"]) if status == "running" else None
            return self.start_run(
                run_type, scheduled_for=scheduled_for, connection=connection
            )

    def running_full_run_id(self, business_date: str) -> int | None:
        """Return a running daily full run, including a legacy UTC-slot marker."""
        row = self.connection.execute(
            """
            SELECT id FROM collection_runs
            WHERE run_type = 'full' AND status = 'running'
              AND (
                  scheduled_for = ?
                  OR (
                      length(scheduled_for) > 10
                      AND date(datetime(scheduled_for, '+8 hours')) = ?
                  )
              )
            ORDER BY id LIMIT 1
            """,
            (business_date, business_date),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def claim_full_run(self, business_date: str, *, allow_new: bool = True) -> int | None:
        """Claim one daily full run, including a running legacy UTC-slot marker."""
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, status FROM collection_runs
                WHERE run_type = 'full'
                  AND status = 'running'
                  AND (
                      scheduled_for = ?
                      OR (
                          length(scheduled_for) > 10
                          AND date(datetime(scheduled_for, '+8 hours')) = ?
                      )
                  )
                ORDER BY id
                LIMIT 1
                """,
                (business_date, business_date),
            ).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE collection_runs SET scheduled_for = ? WHERE id = ?",
                    (business_date, int(existing["id"])),
                )
                return int(existing["id"])
            if not allow_new:
                return None
            return self.start_run(
                "full", scheduled_for=business_date, connection=connection
            )

    def insert_snapshot(
        self,
        account_id: int,
        *,
        captured_at: str | None = None,
        business_date: str | None = None,
        run_id: int | None = None,
        snapshot_type: str = "incremental",
        coverage: str = "profile_recent",
        follower_count: int | None = None,
        following_count: int | None = None,
        likes_count: int | None = None,
        post_count: int | None = None,
        covered_post_count: int | None = None,
        posts_like_count: int | None = None,
        posts_view_count: int | None = None,
        posts_comment_count: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        return self._write(
            connection,
            lambda active_connection: int(
                active_connection.execute(
                    """
                    INSERT INTO account_snapshots (
                        account_id, run_id, captured_at, business_date, snapshot_type, coverage,
                        follower_count, following_count, likes_count, post_count,
                        covered_post_count, posts_like_count, posts_view_count,
                        posts_comment_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        account_id,
                        run_id,
                        captured_at or utc_now_iso(),
                        business_date,
                        snapshot_type,
                        coverage,
                        follower_count,
                        following_count,
                        likes_count,
                        post_count,
                        covered_post_count,
                        posts_like_count,
                        posts_view_count,
                        posts_comment_count,
                    ),
                ).lastrowid
            ),
        )

    def replace_full_posts(
        self,
        account_id: int,
        posts: Sequence[Mapping[str, Any]],
        *,
        observed_at: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def write(active_connection: sqlite3.Connection) -> None:
            active_connection.execute("DELETE FROM posts_current WHERE account_id = ?", (account_id,))
            for post in posts:
                active_connection.execute(
                    """
                    INSERT INTO posts_current (
                        video_id, account_id, created_at, description, view_count,
                        like_count, comment_count, share_count, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        post["video_id"],
                        account_id,
                        post.get("created_at"),
                        post.get("description"),
                        post.get("view_count"),
                        post.get("like_count"),
                        post.get("comment_count"),
                        post.get("share_count"),
                        observed_at or utc_now_iso(),
                    ),
                )

        self._write(connection, write)

    def upsert_recent_posts(
        self,
        account_id: int,
        posts: Sequence[Mapping[str, Any]],
        *,
        observed_at: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def write(active_connection: sqlite3.Connection) -> None:
            for post in posts:
                active_connection.execute(
                    """
                    INSERT INTO posts_current (
                        video_id, account_id, created_at, description, view_count,
                        like_count, comment_count, share_count, is_deleted, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                    ON CONFLICT(video_id) DO UPDATE SET
                        created_at = excluded.created_at,
                        description = excluded.description,
                        view_count = excluded.view_count,
                        like_count = excluded.like_count,
                        comment_count = excluded.comment_count,
                        share_count = excluded.share_count,
                        is_deleted = 0,
                        deleted_detected_at = NULL,
                        last_seen_at = excluded.last_seen_at
                    WHERE posts_current.account_id = excluded.account_id
                    """,
                    (
                        post["video_id"],
                        account_id,
                        post.get("created_at"),
                        post.get("description"),
                        post.get("view_count"),
                        post.get("like_count"),
                        post.get("comment_count"),
                        post.get("share_count"),
                        observed_at,
                    ),
                )

        self._write(connection, write)

    def replace_full_posts_with_deletions(
        self,
        account_id: int,
        posts: Sequence[Mapping[str, Any]],
        *,
        observed_at: str,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def write(active_connection: sqlite3.Connection) -> None:
            active_connection.execute(
                """
                UPDATE posts_current
                SET deleted_detected_at = CASE
                        WHEN is_deleted = 0 THEN ? ELSE deleted_detected_at
                    END,
                    is_deleted = 1
                WHERE account_id = ?
                """,
                (observed_at, account_id),
            )
            self.upsert_recent_posts(
                account_id, posts, observed_at=observed_at, connection=active_connection
            )

        self._write(connection, write)

    def upsert_daily_metric(
        self,
        account_id: int,
        business_date: str,
        *,
        snapshot_id: int | None = None,
        baseline_status: str = "incomplete",
        posts_total: int | None = None,
        likes_total: int | None = None,
        views_total: int | None = None,
        comments_total: int | None = None,
        posts_delta: int | None = None,
        likes_delta: int | None = None,
        views_delta: int | None = None,
        comments_delta: int | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        def write(active_connection: sqlite3.Connection) -> None:
            now = utc_now_iso()
            normalized_status = _baseline_status(baseline_status)
            active_connection.execute(
                """
                INSERT INTO daily_account_metrics (
                    account_id, business_date, snapshot_id, baseline_status,
                    posts_total, likes_total, views_total, comments_total,
                    posts_delta, likes_delta, views_delta, comments_delta, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, business_date) DO UPDATE SET
                    snapshot_id = excluded.snapshot_id,
                    baseline_status = excluded.baseline_status,
                    posts_total = excluded.posts_total,
                    likes_total = excluded.likes_total,
                    views_total = excluded.views_total,
                    comments_total = excluded.comments_total,
                    posts_delta = excluded.posts_delta,
                    likes_delta = excluded.likes_delta,
                    views_delta = excluded.views_delta,
                    comments_delta = excluded.comments_delta,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    business_date,
                    snapshot_id,
                    normalized_status,
                    posts_total,
                    likes_total,
                    views_total,
                    comments_total,
                    posts_delta,
                    likes_delta,
                    views_delta,
                    comments_delta,
                    now,
                    now,
                ),
            )

        self._write(connection, write)

    def record_full_calibration(
        self,
        account_id: int,
        *,
        captured_at: str,
        business_date: str,
        posts: Sequence[Mapping[str, Any]],
        daily_metrics: Mapping[str, int | str | None],
        run_id: int | None = None,
    ) -> int:
        """Atomically replace one account's posts, full snapshot, and daily summary."""
        with self.transaction() as connection:
            snapshot_id = self.insert_snapshot(
                account_id,
                captured_at=captured_at,
                business_date=business_date,
                run_id=run_id,
                snapshot_type="full",
                coverage="full",
                follower_count=_int_or_none(daily_metrics.get("follower_count")),
                following_count=_int_or_none(daily_metrics.get("following_count")),
                likes_count=_int_or_none(daily_metrics.get("likes_count")),
                post_count=_int_or_none(daily_metrics.get("post_count")),
                covered_post_count=_int_or_none(daily_metrics.get("covered_post_count")),
                posts_like_count=_int_or_none(daily_metrics.get("posts_like_count")),
                posts_view_count=_int_or_none(daily_metrics.get("posts_view_count")),
                posts_comment_count=_int_or_none(daily_metrics.get("posts_comment_count")),
                connection=connection,
            )
            self.replace_full_posts(account_id, posts, observed_at=captured_at, connection=connection)
            self.upsert_daily_metric(
                account_id,
                business_date,
                snapshot_id=snapshot_id,
                baseline_status=str(daily_metrics.get("baseline_status", "incomplete")),
                posts_total=_int_or_none(daily_metrics.get("posts_total")),
                likes_total=_int_or_none(daily_metrics.get("likes_total")),
                views_total=_int_or_none(daily_metrics.get("views_total")),
                comments_total=_int_or_none(daily_metrics.get("comments_total")),
                posts_delta=_int_or_none(daily_metrics.get("posts_delta")),
                likes_delta=_int_or_none(daily_metrics.get("likes_delta")),
                views_delta=_int_or_none(daily_metrics.get("views_delta")),
                comments_delta=_int_or_none(daily_metrics.get("comments_delta")),
                connection=connection,
            )
            return snapshot_id

    def record_incremental_collection(
        self,
        account_id: int,
        *,
        captured_at: str,
        business_date: str,
        profile: Mapping[str, int],
        posts: Sequence[Mapping[str, Any]],
        run_id: int | None = None,
    ) -> int:
        with self.transaction() as connection:
            snapshot_id = self.insert_snapshot(
                account_id,
                captured_at=captured_at,
                business_date=business_date,
                run_id=run_id,
                snapshot_type="incremental",
                coverage="profile_recent",
                follower_count=int(profile["follower_count"]),
                following_count=int(profile["following_count"]),
                likes_count=int(profile["likes_count"]),
                post_count=int(profile["post_count"]),
                covered_post_count=len(posts),
                posts_like_count=sum(int(post["like_count"]) for post in posts),
                posts_view_count=sum(int(post["view_count"]) for post in posts),
                posts_comment_count=sum(int(post["comment_count"]) for post in posts),
                connection=connection,
            )
            self.upsert_recent_posts(
                account_id, posts, observed_at=captured_at, connection=connection
            )
            return snapshot_id

    def record_complete_collection(
        self,
        account_id: int,
        *,
        captured_at: str,
        business_date: str,
        profile: Mapping[str, int],
        posts: Sequence[Mapping[str, Any]],
        run_id: int | None = None,
    ) -> tuple[int, str]:
        current_totals = {
            "posts": int(profile["post_count"]),
            "likes": int(profile["likes_count"]),
            "views": sum(int(post["view_count"]) for post in posts),
            "comments": sum(int(post["comment_count"]) for post in posts),
        }
        previous_date = (date.fromisoformat(business_date) - timedelta(days=1)).isoformat()
        with self.transaction() as connection:
            previous = connection.execute(
                """
                SELECT * FROM daily_account_metrics
                WHERE account_id = ? AND business_date = ?
                """,
                (account_id, previous_date),
            ).fetchone()
            previous_totals = None
            if previous is not None and previous["baseline_status"] != "incomplete":
                candidate = {
                    name: previous[f"{name}_total"]
                    for name in ("posts", "likes", "views", "comments")
                }
                if all(value is not None for value in candidate.values()):
                    previous_totals = {name: int(value) for name, value in candidate.items()}

            if previous_totals is not None:
                baseline_status = "ready"
                deltas = {
                    name: current_totals[name] - previous_totals[name]
                    for name in current_totals
                }
            else:
                has_history = connection.execute(
                    """
                    SELECT 1 FROM daily_account_metrics
                    WHERE account_id = ? AND business_date < ?
                    LIMIT 1
                    """,
                    (account_id, business_date),
                ).fetchone()
                baseline_status = "missing_previous" if has_history else "first_day"
                deltas = {name: None for name in current_totals}

            snapshot_id = self.insert_snapshot(
                account_id,
                captured_at=captured_at,
                business_date=business_date,
                run_id=run_id,
                snapshot_type="full",
                coverage="full",
                follower_count=int(profile["follower_count"]),
                following_count=int(profile["following_count"]),
                likes_count=current_totals["likes"],
                post_count=current_totals["posts"],
                covered_post_count=len(posts),
                posts_like_count=sum(int(post["like_count"]) for post in posts),
                posts_view_count=current_totals["views"],
                posts_comment_count=current_totals["comments"],
                connection=connection,
            )
            self.replace_full_posts_with_deletions(
                account_id, posts, observed_at=captured_at, connection=connection
            )
            self.upsert_daily_metric(
                account_id,
                business_date,
                snapshot_id=snapshot_id,
                baseline_status=baseline_status,
                posts_total=current_totals["posts"],
                likes_total=current_totals["likes"],
                views_total=current_totals["views"],
                comments_total=current_totals["comments"],
                posts_delta=deltas["posts"],
                likes_delta=deltas["likes"],
                views_delta=deltas["views"],
                comments_delta=deltas["comments"],
                connection=connection,
            )
            return snapshot_id, baseline_status

    def acquire_lease(
        self, lease_name: str, owner_id: str, expires_at: str, *, now: str | None = None
    ) -> bool:
        acquired_at = now or utc_now_iso()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO worker_leases (lease_name, owner_id, acquired_at, expires_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    acquired_at = excluded.acquired_at,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                WHERE worker_leases.owner_id = excluded.owner_id
                   OR worker_leases.expires_at <= excluded.acquired_at
                """,
                (lease_name, owner_id, acquired_at, expires_at, acquired_at),
            )
            return cursor.rowcount == 1

    def renew_lease(
        self, lease_name: str, owner_id: str, expires_at: str, *, now: str | None = None
    ) -> bool:
        renewed_at = now or utc_now_iso()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE worker_leases SET expires_at = ?, updated_at = ?
                WHERE lease_name = ? AND owner_id = ? AND expires_at > ?
                """,
                (expires_at, renewed_at, lease_name, owner_id, renewed_at),
            )
            return cursor.rowcount == 1

    def release_lease(self, lease_name: str, owner_id: str) -> bool:
        with self.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM worker_leases WHERE lease_name = ? AND owner_id = ?",
                (lease_name, owner_id),
            )
            return cursor.rowcount == 1

    def cleanup_snapshots(self, before_captured_at: str) -> int:
        with self.transaction() as connection:
            return connection.execute(
                """
                DELETE FROM account_snapshots
                WHERE captured_at < ?
                  AND NOT EXISTS (
                      SELECT 1 FROM daily_account_metrics
                      WHERE daily_account_metrics.snapshot_id = account_snapshots.id
                  )
                """,
                (before_captured_at,),
            ).rowcount

    def daily_metric(self, account_id: int, business_date: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM daily_account_metrics
            WHERE account_id = ? AND business_date = ?
            """,
            (account_id, business_date),
        ).fetchone()
        return dict(row) if row else None

    def count_rows(self, table_name: str) -> int:
        allowed_tables = {
            "tracked_accounts",
            "collection_runs",
            "account_snapshots",
            "posts_current",
            "daily_account_metrics",
            "worker_leases",
        }
        if table_name not in allowed_tables:
            raise ValueError(f"Unknown statistics table: {table_name}")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])

    def _write(self, connection, callback):
        if connection is not None:
            return callback(connection)
        with self.transaction() as active_connection:
            return callback(active_connection)


def _int_or_none(value: object) -> int | None:
    return None if value is None else int(value)


def _baseline_status(value: str) -> str:
    legacy = {"available": "ready", "unavailable": "incomplete"}
    normalized = legacy.get(value, value)
    if normalized not in {"ready", "first_day", "missing_previous", "incomplete"}:
        raise ValueError(f"Unknown baseline status: {value}")
    return normalized


def _guard_clock_iso(clock) -> str:
    value = clock() if callable(clock) else clock.now()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError("lease write guard clock must return datetime or ISO timestamp")
    if parsed.tzinfo is None:
        raise ValueError("lease write guard clock must be timezone-aware")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
