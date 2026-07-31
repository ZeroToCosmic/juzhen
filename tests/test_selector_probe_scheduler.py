from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone

import pytest

from selector_probe.scheduler import (
    RELEASE_SCRIPT,
    RENEW_SCRIPT,
    RedisLease,
    due_daily_slot,
)


class FakeRedis:
    def __init__(self, *, return_bytes: bool = False):
        self.values: dict[str, str | bytes] = {}
        self.return_bytes = return_bytes
        self.set_calls: list[tuple[object, ...]] = []
        self.eval_calls: list[tuple[object, ...]] = []

    def set(self, key, value, nx=False, ex=None):
        self.set_calls.append((key, value, nx, ex))
        if nx and key in self.values:
            return False
        self.values[key] = value.encode() if self.return_bytes else value
        return True

    def eval(self, script, numkeys, *args):
        self.eval_calls.append((script, numkeys, *args))
        key, owner, *rest = args
        stored = self.values.get(key)
        comparable_owner = owner.encode() if isinstance(stored, bytes) else owner
        if stored != comparable_owner:
            return 0
        if script == RENEW_SCRIPT:
            assert rest == [120]
            return 1
        if script == RELEASE_SCRIPT:
            assert rest == []
            del self.values[key]
            return 1
        raise AssertionError("unexpected Lua script")


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (
            datetime(2026, 7, 27, 18, 59, 59, tzinfo=UTC),
            datetime(2026, 7, 26, 19, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 27, 19, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 19, 0, tzinfo=UTC),
        ),
        (
            datetime(2026, 7, 28, 2, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 19, 0, tzinfo=UTC),
        ),
    ],
)
def test_due_daily_slot_before_at_and_after_schedule(now, expected):
    assert due_daily_slot(now, None, "Asia/Shanghai", time(3, 0)) == expected


def test_due_daily_slot_returns_only_latest_missed_candidate():
    now = datetime(2026, 7, 31, 2, 0, tzinfo=UTC)
    old_completed = datetime(2026, 7, 20, 19, 0, tzinfo=UTC)

    assert due_daily_slot(
        now,
        old_completed,
        "Asia/Shanghai",
        time(3, 0),
    ) == datetime(2026, 7, 30, 19, 0, tzinfo=UTC)


def test_due_daily_slot_treats_equivalent_completed_instant_as_complete():
    now = datetime(2026, 7, 28, 2, 0, tzinfo=UTC)
    same_slot_in_shanghai = datetime(
        2026,
        7,
        28,
        3,
        0,
        tzinfo=timezone(timedelta(hours=8)),
    )

    assert (
        due_daily_slot(
            now,
            same_slot_in_shanghai,
            "Asia/Shanghai",
            time(3, 0),
        )
        is None
    )


@pytest.mark.parametrize(
    ("now", "last_completed", "message"),
    [
        (
            datetime(2026, 7, 28, 2, 0),
            None,
            "now_utc must be timezone-aware",
        ),
        (
            datetime(2026, 7, 28, 2, 0, tzinfo=UTC),
            datetime(2026, 7, 27, 19, 0),
            "last_completed_slot must be timezone-aware",
        ),
    ],
)
def test_due_daily_slot_rejects_naive_datetimes(now, last_completed, message):
    with pytest.raises(ValueError, match=message):
        due_daily_slot(
            now,
            last_completed,
            "Asia/Shanghai",
            time(3, 0),
        )


def test_due_daily_slot_reports_invalid_timezone_as_value_error():
    with pytest.raises(ValueError, match="invalid timezone"):
        due_daily_slot(
            datetime(2026, 7, 28, 2, 0, tzinfo=UTC),
            None,
            "Mars/Olympus_Mons",
            time(3, 0),
        )


def test_due_daily_slot_uses_first_occurrence_on_fall_back_day():
    now = datetime(2026, 11, 1, 6, 15, tzinfo=UTC)

    assert due_daily_slot(
        now,
        None,
        "America/New_York",
        time(1, 30),
    ) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)


def test_due_daily_slot_does_not_replay_second_fall_back_occurrence():
    first_occurrence = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)

    assert (
        due_daily_slot(
            datetime(2026, 11, 1, 7, 0, tzinfo=UTC),
            first_occurrence,
            "America/New_York",
            time(1, 30),
        )
        is None
    )


def test_due_daily_slot_normalizes_spring_gap_to_first_valid_instant():
    now = datetime(2026, 3, 8, 7, 15, tzinfo=UTC)

    assert due_daily_slot(
        now,
        None,
        "America/New_York",
        time(2, 30),
    ) == datetime(2026, 3, 8, 7, 0, tzinfo=UTC)


def test_due_daily_slot_never_returns_future_spring_gap_candidate():
    now = datetime(2026, 3, 8, 6, 45, tzinfo=UTC)
    slot = due_daily_slot(
        now,
        None,
        "America/New_York",
        time(2, 30),
    )

    assert slot == datetime(2026, 3, 7, 7, 30, tzinfo=UTC)
    assert slot <= now


def test_redis_lease_acquire_uses_atomic_set_with_expiry():
    client = FakeRedis()
    lease = RedisLease(client, "probe:lease", "owner-a")

    assert lease.acquire() is True
    assert client.set_calls == [("probe:lease", "owner-a", True, 120)]
    assert RedisLease(client, "probe:lease", "owner-b").acquire() is False


@pytest.mark.parametrize("return_bytes", [False, True])
def test_redis_lease_renews_and_releases_its_own_value(return_bytes):
    client = FakeRedis(return_bytes=return_bytes)
    lease = RedisLease(client, "probe:lease", "owner-a")

    assert lease.acquire() is True
    assert lease.renew() is True
    assert client.eval_calls[-1] == (
        RENEW_SCRIPT,
        1,
        "probe:lease",
        "owner-a",
        120,
    )
    assert lease.release() is True
    assert client.eval_calls[-1] == (
        RELEASE_SCRIPT,
        1,
        "probe:lease",
        "owner-a",
    )
    assert "probe:lease" not in client.values


@pytest.mark.parametrize("operation", ["renew", "release"])
def test_redis_lease_non_owner_cannot_renew_or_release(operation):
    client = FakeRedis(return_bytes=True)
    owner = RedisLease(client, "probe:lease", "owner-a")
    intruder = RedisLease(client, "probe:lease", "owner-b")
    assert owner.acquire() is True

    assert getattr(intruder, operation)() is False
    assert client.values["probe:lease"] == b"owner-a"


@pytest.mark.parametrize(
    ("key", "owner_id", "message"),
    [
        ("", "owner-a", "key must be a non-empty string"),
        ("   ", "owner-a", "key must be a non-empty string"),
        ("probe:lease", "", "owner_id must be a non-empty string"),
        ("probe:lease", " \t", "owner_id must be a non-empty string"),
        (123, "owner-a", "key must be a non-empty string"),
        ("probe:lease", b"owner-a", "owner_id must be a non-empty string"),
    ],
)
def test_redis_lease_requires_nonempty_text_identity(key, owner_id, message):
    with pytest.raises(ValueError, match=message):
        RedisLease(FakeRedis(), key, owner_id)


@pytest.mark.parametrize(
    ("ttl", "heartbeat", "message"),
    [
        (0, 30, "ttl_seconds must be a positive integer"),
        (-1, 30, "ttl_seconds must be a positive integer"),
        (True, 30, "ttl_seconds must be a positive integer"),
        (120.0, 30, "ttl_seconds must be a positive integer"),
        (120, 0, "heartbeat_seconds must be a positive integer"),
        (120, -1, "heartbeat_seconds must be a positive integer"),
        (120, False, "heartbeat_seconds must be a positive integer"),
        (120, 30.0, "heartbeat_seconds must be a positive integer"),
        (120, 120, "heartbeat_seconds must be less than ttl_seconds"),
        (120, 121, "heartbeat_seconds must be less than ttl_seconds"),
    ],
)
def test_redis_lease_validates_timing_boundaries(ttl, heartbeat, message):
    with pytest.raises(ValueError, match=message):
        RedisLease(
            FakeRedis(),
            "probe:lease",
            "owner-a",
            ttl_seconds=ttl,
            heartbeat_seconds=heartbeat,
        )
