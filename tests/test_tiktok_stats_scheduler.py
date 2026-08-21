from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import subprocess
import threading
from zoneinfo import ZoneInfo

from launcher import StatisticsWorkerSupervisor
from tiktok_stats.collector import AccountCollectionResult
from tiktok_stats.scheduler import due_incremental_slots, full_calibration_due
from tiktok_stats.store import LeaseWriteGuardLost, StatsStore
from tiktok_stats.worker import LeaseHeartbeat, run_worker_once


SHANGHAI = ZoneInfo("Asia/Shanghai")


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


class SequenceRng:
    def __init__(self, values=(0.25, 0.75)):
        self.values = iter(values)

    def random(self):
        return next(self.values)


class FakeCollector:
    def __init__(self, *, clock=None):
        self.calls = []
        self.clock = clock

    def collect_incremental(self, account_id, run_id):
        self.calls.append(("incremental", account_id, run_id))
        if self.clock is not None:
            self.clock.value += timedelta(seconds=40)
        return AccountCollectionResult(account_id=account_id, status="completed")

    def collect_full(self, account_id, run_id, business_date):
        self.calls.append(("full", account_id, run_id, business_date))
        if self.clock is not None:
            self.clock.value += timedelta(seconds=40)
        return AccountCollectionResult(account_id=account_id, status="completed")


def _account(store: StatsStore, username: str) -> int:
    return int(store.upsert_account(username, username.casefold())["id"])


def test_due_incremental_slots_align_to_all_eight_shanghai_slots():
    now = datetime(2026, 7, 22, 15, 59, tzinfo=UTC)  # 23:59 in Shanghai

    slots = due_incremental_slots(now, None, SHANGHAI)

    assert [slot.astimezone(SHANGHAI).hour for slot in slots] == list(range(0, 24, 3))
    assert all(slot.minute == 0 and slot.second == 0 for slot in slots)
    assert slots == sorted(slots)


def test_due_incremental_slots_cross_midnight_and_exclude_last_slot():
    last = datetime(2026, 7, 22, 13, 0, tzinfo=UTC)  # 21:00 Shanghai
    now = datetime(2026, 7, 22, 16, 1, tzinfo=UTC)  # next local day 00:01

    assert due_incremental_slots(now, last, "Asia/Shanghai") == [
        datetime(2026, 7, 22, 16, 0, tzinfo=UTC)
    ]


def test_restart_catches_up_once_and_persists_slot_run(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "one")
    clock = MutableClock(datetime(2026, 7, 22, 4, 1, tzinfo=UTC))
    collector = FakeCollector()

    first = run_worker_once(
        store, collector, clock=clock, sleeper=lambda _: None,
        rng=SequenceRng((0.0,)), owner_id="worker-a", include_full=False,
    )
    second = run_worker_once(
        store, collector, clock=clock, sleeper=lambda _: None,
        rng=SequenceRng(()), owner_id="worker-b", include_full=False,
    )

    assert first.incremental_run_ids
    assert second.incremental_run_ids == ()
    rows = store.connection.execute(
        "SELECT scheduled_for, status FROM collection_runs WHERE run_type = 'incremental'"
    ).fetchall()
    assert len(rows) == len(first.incremental_run_ids)
    assert all(row["status"] == "completed" for row in rows)
    store.close()


def test_restart_catches_up_every_slot_after_last_persisted_run(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "one")
    first_slot = "2026-07-21T16:00:00.000Z"  # local midnight
    run_id = store.start_run("incremental", scheduled_for=first_slot)
    store.finish_run(run_id, "completed")
    clock = MutableClock(datetime(2026, 7, 22, 1, 1, tzinfo=UTC))  # local 09:01
    collector = FakeCollector()

    result = run_worker_once(
        store, collector, clock=clock, sleeper=lambda _: None,
        rng=SequenceRng((0.0, 0.0, 0.0)), owner_id="worker-a",
        include_full=False,
    )

    assert len(result.incremental_run_ids) == 3
    assert [row[0] for row in store.connection.execute(
        "SELECT scheduled_for FROM collection_runs ORDER BY scheduled_for"
    )] == [
        first_slot,
        "2026-07-21T19:00:00.000Z",
        "2026-07-21T22:00:00.000Z",
        "2026-07-22T01:00:00.000Z",
    ]
    store.close()


def test_expired_owner_run_is_resumed_without_duplicate_row(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "one")
    slot = "2026-07-21T22:00:00.000Z"  # local 06:00 slot
    stale_run_id = store.start_run("incremental", scheduled_for=slot)
    store.acquire_lease(
        f"incremental:{slot}", "dead-worker", "2026-07-21T22:00:30.000Z",
        now="2026-07-21T22:00:00.000Z",
    )
    clock = MutableClock(datetime(2026, 7, 21, 22, 31, tzinfo=UTC))

    result = run_worker_once(
        store, FakeCollector(), clock=clock, sleeper=lambda _: None,
        rng=SequenceRng((0.0,)), owner_id="replacement", include_full=False,
    )

    assert result.incremental_run_ids == (stale_run_id,)
    assert store.count_rows("collection_runs") == 1
    assert store.connection.execute(
        "SELECT status FROM collection_runs WHERE id = ?", (stale_run_id,)
    ).fetchone()[0] == "completed"
    store.close()


def test_full_calibration_due_is_false_only_for_complete_full_daily_row(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "daily")
    assert full_calibration_due(account_id, "2026-07-22", store) is True

    snapshot_id = store.insert_snapshot(
        account_id,
        captured_at="2026-07-22T15:00:00.000Z",
        business_date="2026-07-22",
        snapshot_type="full",
        coverage="full",
    )
    store.upsert_daily_metric(
        account_id,
        "2026-07-22",
        snapshot_id=snapshot_id,
        baseline_status="first_day",
        posts_total=1,
        likes_total=2,
        views_total=3,
        comments_total=4,
    )

    assert full_calibration_due(account_id, "2026-07-22", store) is False
    store.close()


def test_worker_full_calibration_is_idempotent_after_complete_daily_row(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "daily-worker")
    clock = MutableClock(datetime(2026, 7, 22, 2, 0, tzinfo=UTC))

    class CompletingCollector(FakeCollector):
        def collect_full(self, collected_account_id, run_id, business_date):
            result = super().collect_full(collected_account_id, run_id, business_date)
            snapshot_id = store.insert_snapshot(
                collected_account_id,
                captured_at="2026-07-22T02:00:00.000Z",
                business_date=business_date,
                run_id=run_id,
                snapshot_type="full",
                coverage="full",
            )
            store.upsert_daily_metric(
                collected_account_id, business_date, snapshot_id=snapshot_id,
                baseline_status="first_day", posts_total=1, likes_total=2,
                views_total=3, comments_total=4,
            )
            return result

    collector = CompletingCollector()
    first = run_worker_once(
        store, collector, clock=clock, sleeper=lambda _: None,
        rng=SequenceRng((0.0,)), owner_id="worker-a",
        include_incremental=False,
    )
    second = run_worker_once(
        store, collector, clock=clock, sleeper=lambda _: None,
        rng=SequenceRng(()), owner_id="worker-b",
        include_incremental=False,
    )

    assert first.full_run_id is not None
    assert second.full_run_id is None
    assert collector.calls == [("full", account_id, first.full_run_id, "2026-07-22")]
    store.close()


def test_account_jitter_uses_injected_rng_and_sleeper_without_real_sleep(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "one")
    _account(store, "two")
    sleeps = []
    clock = MutableClock(datetime(2026, 7, 22, 0, 1, tzinfo=UTC))
    collector = FakeCollector()

    result = run_worker_once(
        store,
        collector,
        clock=clock,
        sleeper=sleeps.append,
        rng=SequenceRng((0.25, 0.75)),
        owner_id="worker-a",
        account_jitter_seconds=20,
        include_full=False,
    )

    assert result.incremental_run_ids
    assert sleeps == [5.0, 15.0]
    assert [call[1] for call in collector.calls] == [1, 2]
    store.close()


def test_lease_is_exclusive_renews_and_can_only_be_taken_after_expiry(tmp_path):
    store_a = StatsStore(tmp_path / "stats.db")
    store_b = StatsStore(tmp_path / "stats.db")
    assert store_a.acquire_lease(
        "slot", "a", "2026-07-22T00:01:00.000Z", now="2026-07-22T00:00:00.000Z"
    )
    assert not store_b.acquire_lease(
        "slot", "b", "2026-07-22T00:01:30.000Z", now="2026-07-22T00:00:30.000Z"
    )
    assert store_a.renew_lease(
        "slot", "a", "2026-07-22T00:02:00.000Z", now="2026-07-22T00:00:45.000Z"
    )
    assert not store_b.acquire_lease(
        "slot", "b", "2026-07-22T00:02:30.000Z", now="2026-07-22T00:01:30.000Z"
    )
    assert store_b.acquire_lease(
        "slot", "b", "2026-07-22T00:03:30.000Z", now="2026-07-22T00:02:00.000Z"
    )
    store_a.close()
    store_b.close()


def test_worker_renews_lease_between_long_account_collections(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "one")
    _account(store, "two")
    clock = MutableClock(datetime(2026, 7, 22, 0, 1, tzinfo=UTC))
    collector = FakeCollector(clock=clock)

    result = run_worker_once(
        store,
        collector,
        clock=clock,
        sleeper=lambda _: None,
        rng=SequenceRng((0.0, 0.0)),
        owner_id="worker-a",
        lease_seconds=60,
        account_jitter_seconds=0,
        include_full=False,
    )

    assert result.incremental_run_ids
    lease = store.connection.execute(
        "SELECT * FROM worker_leases ORDER BY lease_name LIMIT 1"
    ).fetchone()
    assert lease is None  # successful work releases ownership
    assert clock.value == datetime(2026, 7, 22, 0, 2, 20, tzinfo=UTC)
    store.close()


def test_independent_heartbeat_prevents_takeover_during_one_long_account(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "slow")
    clock = MutableClock(datetime(2026, 7, 21, 22, 1, tzinfo=UTC))
    heartbeat_ref = {}
    renewed = __import__("threading").Event()

    def heartbeat_factory(**kwargs):
        heartbeat = LeaseHeartbeat(**kwargs, on_renew=renewed.set)
        heartbeat_ref["value"] = heartbeat
        return heartbeat

    class SlowCollector(FakeCollector):
        def collect_incremental(self, account_id, run_id):
            self.calls.append(("incremental", account_id, run_id))
            clock.value = datetime(2026, 7, 21, 22, 1, 40, tzinfo=UTC)
            heartbeat_ref["value"].pulse()
            assert renewed.wait(timeout=1)
            contender = StatsStore(tmp_path / "stats.db")
            try:
                assert not contender.acquire_lease(
                    "incremental:2026-07-21T22:00:00.000Z",
                    "replacement",
                    "2026-07-21T22:03:10.000Z",
                    now="2026-07-21T22:02:10.000Z",
                )
            finally:
                contender.close()
            return AccountCollectionResult(account_id=account_id, status="completed")

    result = run_worker_once(
        store,
        SlowCollector(),
        clock=clock,
        sleeper=lambda _: None,
        rng=SequenceRng((0.0,)),
        owner_id="worker-a",
        lease_seconds=60,
        include_full=False,
        heartbeat_factory=heartbeat_factory,
    )

    assert result.incremental_run_ids
    assert store.count_rows("worker_leases") == 0
    store.close()


def test_full_run_resumes_same_business_date_across_three_hour_slot(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "full-stale")
    stale_run_id = store.start_run(
        "full", scheduled_for="2026-07-22T01:00:00.000Z"
    )  # legacy slot marker, local 09:00
    store.acquire_lease(
        "full:2026-07-22",
        "dead-worker",
        "2026-07-22T01:00:00.000Z",
        now="2026-07-22T00:00:00.000Z",
    )
    clock = MutableClock(datetime(2026, 7, 22, 5, 1, tzinfo=UTC))

    result = run_worker_once(
        store,
        FakeCollector(),
        clock=clock,
        sleeper=lambda _: None,
        rng=SequenceRng((0.0,)),
        owner_id="replacement",
        include_incremental=False,
    )

    assert result.full_run_id == stale_run_id
    assert store.count_rows("collection_runs") == 1
    row = store.connection.execute(
        "SELECT status, scheduled_for FROM collection_runs WHERE id = ?", (stale_run_id,)
    ).fetchone()
    assert (row["status"], row["scheduled_for"]) == ("completed", "2026-07-22")
    store.close()


def test_worker_that_lost_lease_cannot_overwrite_replacement_result(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    _account(store, "lease-loss")
    clock = MutableClock(datetime(2026, 7, 21, 22, 1, tzinfo=UTC))

    class LostHeartbeat:
        def __init__(self, **_):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def raise_if_failed(self):
            raise RuntimeError("lost")

    class ReplacementCollector(FakeCollector):
        def collect_incremental(self, account_id, run_id):
            clock.value = datetime(2026, 7, 21, 22, 2, 1, tzinfo=UTC)
            replacement = StatsStore(tmp_path / "stats.db")
            try:
                assert replacement.acquire_lease(
                    "incremental:2026-07-21T22:00:00.000Z",
                    "replacement",
                    "2026-07-21T22:04:00.000Z",
                    now="2026-07-21T22:02:01.000Z",
                )
                replacement.finish_run(run_id, "completed")
            finally:
                replacement.close()
            return AccountCollectionResult(account_id=account_id, status="completed")

    try:
        run_worker_once(
            store,
            ReplacementCollector(),
            clock=clock,
            sleeper=lambda _: None,
            rng=SequenceRng((0.0,)),
            owner_id="old-worker",
            lease_seconds=60,
            include_full=False,
            heartbeat_factory=LostHeartbeat,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("lost worker must stop with an error")

    assert store.connection.execute(
        "SELECT status FROM collection_runs"
    ).fetchone()[0] == "completed"
    store.close()


def test_lost_owner_business_transaction_rolls_back_after_replacement_commit(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "business-lease-loss")
    clock = MutableClock(datetime(2026, 7, 21, 22, 1, tzinfo=UTC))

    class SilentHeartbeat:
        def __init__(self, **_):
            pass

        def start(self):
            return self

        def stop(self):
            pass

        def raise_if_failed(self):
            pass

    class ReplacedDuringFullCollection(FakeCollector):
        def collect_full(self, collected_account_id, run_id, business_date):
            clock.value = datetime(2026, 7, 21, 22, 2, 1, tzinfo=UTC)
            replacement = StatsStore(tmp_path / "stats.db")
            try:
                assert replacement.acquire_lease(
                    "full:2026-07-22",
                    "replacement",
                    "2026-07-21T22:05:00.000Z",
                    now="2026-07-21T22:02:01.000Z",
                )
                replacement.record_complete_collection(
                    collected_account_id,
                    captured_at="2026-07-21T22:02:02.000Z",
                    business_date=business_date,
                    profile={
                        "follower_count": 200,
                        "following_count": 200,
                        "likes_count": 200,
                        "post_count": 1,
                    },
                    posts=[{
                        "video_id": "post",
                        "created_at": 1,
                        "description": "replacement",
                        "view_count": 200,
                        "like_count": 200,
                        "comment_count": 200,
                        "share_count": 0,
                    }],
                    run_id=run_id,
                )
                replacement.finish_run(run_id, "completed")
            finally:
                replacement.close()

            clock.value = datetime(2026, 7, 21, 22, 2, 3, tzinfo=UTC)
            store.record_complete_collection(
                collected_account_id,
                captured_at="2026-07-21T22:02:03.000Z",
                business_date=business_date,
                profile={
                    "follower_count": 100,
                    "following_count": 100,
                    "likes_count": 100,
                    "post_count": 1,
                },
                posts=[{
                    "video_id": "post",
                    "created_at": 1,
                    "description": "stale",
                    "view_count": 100,
                    "like_count": 100,
                    "comment_count": 100,
                    "share_count": 0,
                }],
                run_id=run_id,
            )
            raise AssertionError("lost owner business write must not commit")

    with __import__("pytest").raises(LeaseWriteGuardLost):
        run_worker_once(
            store,
            ReplacedDuringFullCollection(),
            clock=clock,
            sleeper=lambda _: None,
            rng=SequenceRng((0.0,)),
            owner_id="old-worker",
            lease_seconds=60,
            include_incremental=False,
            heartbeat_factory=SilentHeartbeat,
        )

    assert store.count_rows("account_snapshots") == 1
    snapshot = store.connection.execute("SELECT * FROM account_snapshots").fetchone()
    post = store.connection.execute("SELECT * FROM posts_current").fetchone()
    daily = store.daily_metric(account_id, "2026-07-22")
    assert snapshot["posts_view_count"] == 200
    assert (post["view_count"], post["description"]) == (200, "replacement")
    assert (daily["views_total"], daily["likes_total"]) == (200, 200)
    assert store.connection.execute("SELECT status FROM collection_runs").fetchone()[0] == "completed"
    store.close()


def test_failed_full_run_retries_same_business_date_and_second_run_succeeds(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "retry-full")
    clock = MutableClock(datetime(2026, 7, 22, 3, 1, tzinfo=UTC))

    class FailingCollector(FakeCollector):
        def collect_full(self, collected_account_id, run_id, business_date):
            return AccountCollectionResult(
                account_id=collected_account_id,
                status="failed",
                error_code="upstream_unavailable",
            )

    class CompletingCollector(FakeCollector):
        def collect_full(self, collected_account_id, run_id, business_date):
            store.record_complete_collection(
                collected_account_id,
                captured_at="2026-07-22T03:01:00.000Z",
                business_date=business_date,
                profile={
                    "follower_count": 1,
                    "following_count": 1,
                    "likes_count": 2,
                    "post_count": 1,
                },
                posts=[{
                    "video_id": "retry-post",
                    "created_at": 1,
                    "description": "ok",
                    "view_count": 3,
                    "like_count": 2,
                    "comment_count": 4,
                    "share_count": 0,
                }],
                run_id=run_id,
            )
            return AccountCollectionResult(account_id=collected_account_id, status="completed")

    first = run_worker_once(
        store, FailingCollector(), clock=clock, sleeper=lambda _: None,
        rng=SequenceRng((0.0,)), owner_id="worker-a", include_incremental=False,
    )
    second = run_worker_once(
        store, CompletingCollector(), clock=clock, sleeper=lambda _: None,
        rng=SequenceRng((0.0,)), owner_id="worker-b", include_incremental=False,
    )

    assert first.full_run_id is not None
    assert second.full_run_id is not None and second.full_run_id != first.full_run_id
    assert [row[0] for row in store.connection.execute(
        "SELECT status FROM collection_runs ORDER BY id"
    )] == ["failed", "completed"]
    assert full_calibration_due(account_id, "2026-07-22", store) is False
    store.close()


def test_terminal_failed_or_partial_full_rows_do_not_block_same_day_retry(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    for day, terminal_status in (("2026-07-22", "failed"), ("2026-07-23", "partial")):
        first_run = store.claim_full_run(day)
        store.finish_run(first_run, terminal_status)

        retry_run = store.claim_full_run(day)

        assert retry_run is not None and retry_run != first_run
    store.close()


def test_completed_daily_data_still_resumes_and_finishes_expired_running_full(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "crash-after-daily")
    stale_run_id = store.start_run(
        "full", scheduled_for="2026-07-22T01:00:00.000Z"
    )  # legacy UTC-slot marker
    store.record_complete_collection(
        account_id,
        captured_at="2026-07-22T01:05:00.000Z",
        business_date="2026-07-22",
        profile={
            "follower_count": 1,
            "following_count": 1,
            "likes_count": 2,
            "post_count": 1,
        },
        posts=[{
            "video_id": "already-complete",
            "created_at": 1,
            "description": "complete",
            "view_count": 3,
            "like_count": 2,
            "comment_count": 4,
            "share_count": 0,
        }],
        run_id=stale_run_id,
    )
    store.acquire_lease(
        "full:2026-07-22", "dead-worker", "2026-07-22T02:00:00.000Z",
        now="2026-07-22T01:00:00.000Z",
    )
    collector = FakeCollector()
    clock = MutableClock(datetime(2026, 7, 22, 5, 1, tzinfo=UTC))

    result = run_worker_once(
        store, collector, clock=clock, sleeper=lambda _: None,
        rng=SequenceRng(()), owner_id="replacement", include_incremental=False,
    )

    assert result.full_run_id == stale_run_id
    assert collector.calls == []
    row = store.connection.execute(
        "SELECT status, scheduled_for FROM collection_runs WHERE id = ?", (stale_run_id,)
    ).fetchone()
    assert (row["status"], row["scheduled_for"]) == ("completed", "2026-07-22")
    assert store.count_rows("worker_leases") == 0
    assert store.count_rows("account_snapshots") == 1
    store.close()


def test_completed_daily_running_full_with_live_lease_is_not_stolen(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "live-after-daily")
    run_id = store.start_run("full", scheduled_for="2026-07-22")
    snapshot_id = store.insert_snapshot(
        account_id,
        captured_at="2026-07-22T01:05:00.000Z",
        business_date="2026-07-22",
        run_id=run_id,
        snapshot_type="full",
        coverage="full",
    )
    store.upsert_daily_metric(
        account_id, "2026-07-22", snapshot_id=snapshot_id,
        baseline_status="first_day", posts_total=1, likes_total=2,
        views_total=3, comments_total=4,
    )
    store.acquire_lease(
        "full:2026-07-22", "live-worker", "2026-07-22T06:00:00.000Z",
        now="2026-07-22T01:00:00.000Z",
    )

    result = run_worker_once(
        store, FakeCollector(),
        clock=MutableClock(datetime(2026, 7, 22, 5, 1, tzinfo=UTC)),
        sleeper=lambda _: None, rng=SequenceRng(()),
        owner_id="replacement", include_incremental=False,
    )

    assert result.full_run_id is None
    assert result.skipped_leases == ("full:2026-07-22",)
    assert store.connection.execute(
        "SELECT status FROM collection_runs WHERE id = ?", (run_id,)
    ).fetchone()[0] == "running"
    assert store.connection.execute(
        "SELECT owner_id FROM worker_leases WHERE lease_name = 'full:2026-07-22'"
    ).fetchone()[0] == "live-worker"
    store.close()


def test_cleanup_deletes_only_unreferenced_snapshots_older_than_90_days(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account_id = _account(store, "keeper")
    run_id = store.start_run("cleanup")
    referenced = store.insert_snapshot(
        account_id,
        captured_at="2026-01-01T00:00:00.000Z",
        business_date="2026-01-01",
        run_id=run_id,
        snapshot_type="full",
        coverage="full",
    )
    store.upsert_daily_metric(
        account_id, "2026-01-01", snapshot_id=referenced,
        baseline_status="first_day", posts_total=1, likes_total=1,
        views_total=1, comments_total=1,
    )
    store.insert_snapshot(
        account_id,
        captured_at="2026-01-02T00:00:00.000Z",
        business_date="2026-01-02",
        run_id=run_id,
    )
    store.insert_snapshot(
        account_id,
        captured_at="2026-07-21T00:00:00.000Z",
        business_date="2026-07-21",
        run_id=run_id,
    )

    deleted = store.cleanup_snapshots("2026-04-23T00:00:00.000Z")

    assert deleted == 1
    assert store.count_rows("account_snapshots") == 2
    assert store.count_rows("daily_account_metrics") == 1
    assert store.count_rows("tracked_accounts") == 1
    assert store.count_rows("collection_runs") == 1
    store.close()


@dataclass
class FakeProcess:
    pid: int = 4321
    returncode: int | None = None
    terminate_calls: int = 0
    wait_calls: int = 0
    cooperative: bool = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.returncode is None and not self.cooperative:
            raise subprocess.TimeoutExpired("worker", timeout)
        if self.cooperative:
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode = -9


def test_launcher_supervisor_starts_one_worker_reports_and_cleans_up():
    process = FakeProcess()
    calls = []

    def popen(command, **kwargs):
        calls.append((command, kwargs))
        return process

    supervisor = StatisticsWorkerSupervisor(popen_factory=popen)

    assert supervisor.start() is process
    assert supervisor.start() is process
    assert supervisor.state() == {"running": True, "pid": 4321, "returncode": None}
    assert len(calls) == 1
    assert calls[0][0][-3:] == ["-m", "tiktok_stats.worker", "serve"]

    supervisor.stop()

    assert process.terminate_calls == 1
    assert process.wait_calls == 2
    assert supervisor.state() == {"running": False, "pid": None, "returncode": 0}


def test_launcher_requests_cooperative_worker_stop_before_terminate(tmp_path):
    stop_file = tmp_path / "worker.stop"
    process = FakeProcess(cooperative=True)

    class CooperativeProcess(FakeProcess):
        def wait(self, timeout=None):
            assert stop_file.exists()
            return super().wait(timeout)

    process = CooperativeProcess(cooperative=True)
    supervisor = StatisticsWorkerSupervisor(
        popen_factory=lambda *_args, **_kwargs: process,
        stop_file=stop_file,
    )
    supervisor.start()

    supervisor.stop()

    assert process.terminate_calls == 0
    assert process.wait_calls == 1
    assert not stop_file.exists()


def test_launcher_concurrent_start_creates_only_one_worker_process():
    entered = threading.Event()
    release = threading.Event()
    calls = []
    process = FakeProcess()

    def blocking_popen(*args, **kwargs):
        calls.append((args, kwargs))
        entered.set()
        assert release.wait(timeout=1)
        return process

    supervisor = StatisticsWorkerSupervisor(popen_factory=blocking_popen)
    first = threading.Thread(target=supervisor.start)
    second = threading.Thread(target=supervisor.start)
    first.start()
    assert entered.wait(timeout=1)
    second.start()
    release.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert len(calls) == 1
    supervisor.stop()


def test_worker_serve_observes_stop_file_and_closes_runtime(monkeypatch, tmp_path):
    import tiktok_stats.worker as worker_module

    stop_file = tmp_path / "worker.stop"

    class Closeable:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    store = Closeable()
    client = Closeable()
    clock = MutableClock(datetime(2026, 7, 22, 0, 0, tzinfo=UTC))
    collector = object()
    monkeypatch.setenv("TIKTOK_STATS_STOP_FILE", str(stop_file))
    monkeypatch.setattr(
        worker_module,
        "_build_runtime",
        lambda: (store, client, collector, clock, object(), object()),
    )

    def one_tick(*_args, **_kwargs):
        stop_file.write_text("stop\n", encoding="utf-8")
        return worker_module.WorkerTickResult()

    monkeypatch.setattr(worker_module, "run_worker_once", one_tick)
    monkeypatch.setattr(
        worker_module,
        "run_cleanup",
        lambda *_args, **_kwargs: worker_module.WorkerTickResult(),
    )
    monkeypatch.setattr(
        worker_module.time,
        "sleep",
        lambda _: (_ for _ in ()).throw(AssertionError("serve should stop before sleeping")),
    )

    assert worker_module.main(["serve"]) == 0
    assert store.closed is True
    assert client.closed is True
