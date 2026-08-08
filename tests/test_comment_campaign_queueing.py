from __future__ import annotations

from datetime import datetime, timezone

import pytest

from comment_campaign.queueing import PREFIX, QueueCoordinator, RedisLease, RedisUnavailableError


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.calls: list[tuple] = []

    def set(self, key, value, *, nx, ex):
        self.calls.append(("set", key, value, nx, ex))
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def get(self, key):
        value = self.values.get(key)
        return value.encode() if value is not None else None

    def eval(self, script, numkeys, key, *args):
        self.calls.append(("eval", script, numkeys, key, *args))
        owner = args[0]
        if self.values.get(key) != owner:
            return 0
        if "del" in script:
            del self.values[key]
        return 1


class FakeQueue:
    def __init__(self):
        self.jobs = {}
        self.calls = []

    def fetch_job(self, job_id):
        return self.jobs.get(job_id)

    def enqueue(self, function, *args, **kwargs):
        self.calls.append(("enqueue", function, args, kwargs))
        job = {"id": kwargs["job_id"], "function": function, "args": args}
        self.jobs[job["id"]] = job
        return job

    def enqueue_at(self, when, function, *args, **kwargs):
        self.calls.append(("enqueue_at", when, function, args, kwargs))
        job = {"id": kwargs["job_id"], "function": function, "args": args}
        self.jobs[job["id"]] = job
        return job


def test_lease_prefixes_key_and_uses_set_nx_ex():
    redis = FakeRedis()
    lease = RedisLease(redis, "profile:profile-ref", "owner-a", ttl_seconds=30)

    assert lease.acquire() is True
    assert redis.calls == [("set", PREFIX + "profile:profile-ref", "owner-a", True, 30)]


def test_release_uses_owner_compare_and_delete():
    redis = FakeRedis()
    first = RedisLease(redis, "profile:a", "owner-a", ttl_seconds=30)
    second = RedisLease(redis, "profile:a", "owner-b", ttl_seconds=30)

    assert first.acquire() is True
    assert second.release() is False
    assert redis.get(PREFIX + "profile:a") == b"owner-a"


def test_refresh_uses_owner_compare_and_expire():
    redis = FakeRedis()
    lease = RedisLease(redis, "campaign:c1", "owner-a", ttl_seconds=30)
    assert lease.acquire() is True

    assert lease.refresh() is True
    assert "expire" in redis.calls[-1][1]
    assert redis.calls[-1][-1] == 30


def test_lost_redis_owner_cannot_refresh_or_release():
    redis = FakeRedis()
    lease = RedisLease(redis, "campaign:c1", "owner-a", ttl_seconds=30)
    assert lease.acquire() is True
    redis.values[PREFIX + "campaign:c1"] = "owner-b"

    assert lease.refresh() is False
    assert lease.release() is False
    assert redis.get(PREFIX + "campaign:c1") == b"owner-b"


def test_redis_outage_is_not_reported_as_lease_contention():
    class UnavailableRedis:
        def set(self, *_args, **_kwargs):
            raise OSError("connection refused")

    with pytest.raises(RedisUnavailableError):
        RedisLease(UnavailableRedis(), "campaign:c1", "owner", ttl_seconds=30).acquire()


def test_enqueue_prepare_is_idempotent_and_uses_safe_job_arguments():
    queue = FakeQueue()
    coordinator = QueueCoordinator(queue, redis=FakeRedis())

    first = coordinator.enqueue_prepare("campaign-1")
    second = coordinator.enqueue_prepare("campaign-1")

    assert second is first
    assert len(queue.calls) == 1
    assert queue.calls[0] == (
        "enqueue", "comment_campaign.jobs.run_prepare_campaign", ("campaign-1",),
        {"job_id": "campaign-prepare-campaign-1", "job_timeout": 600, "result_ttl": 86400},
    )


def test_enqueue_submit_is_idempotent_per_assignment_revision():
    queue = FakeQueue()
    coordinator = QueueCoordinator(queue, redis=FakeRedis())

    first = coordinator.enqueue_submit("campaign-1", "assignment-1", 4)
    second = coordinator.enqueue_submit("campaign-1", "assignment-1", 4)

    assert second is first
    assert len(queue.calls) == 1
    assert queue.calls[0][2] == ("campaign-1", "assignment-1", 4)
    assert queue.calls[0][3]["job_id"] == "campaign-submit-assignment-1-r4"


def test_enqueue_at_is_idempotent_and_passes_utc_datetime():
    queue = FakeQueue()
    coordinator = QueueCoordinator(queue, redis=FakeRedis())
    when = datetime(2026, 8, 7, 8, tzinfo=timezone.utc)

    first = coordinator.enqueue_at("campaign-1", when)
    second = coordinator.enqueue_at("campaign-1", when)

    assert second is first
    assert len(queue.calls) == 1
    assert queue.calls[0][0] == "enqueue_at"
    assert queue.calls[0][1] == when
    assert queue.calls[0][3] == ("campaign-1",)


def test_lease_rejects_unprefixed_empty_and_nonpositive_ttl():
    redis = FakeRedis()
    with pytest.raises(ValueError):
        RedisLease(redis, "", "owner", ttl_seconds=30)
    with pytest.raises(ValueError):
        RedisLease(redis, "profile:a", "", ttl_seconds=30)
    with pytest.raises(ValueError):
        RedisLease(redis, "profile:a", "owner", ttl_seconds=0)


def test_concurrent_queue_gate_never_calls_enqueue_twice_before_job_is_visible():
    queue = FakeQueue()
    redis = FakeRedis()
    owner = QueueCoordinator(queue, redis=redis)
    contender = QueueCoordinator(queue, redis=redis, sleep=lambda _seconds: None)
    gate = RedisLease(redis, "job:campaign-prepare-campaign-1", "other", ttl_seconds=30)
    assert gate.acquire() is True

    with pytest.raises(RuntimeError, match="retry shortly"):
        contender.enqueue_prepare("campaign-1")
    assert queue.calls == []


def test_enqueue_failure_releases_only_its_own_gate():
    class FailingQueue(FakeQueue):
        def enqueue(self, *args, **kwargs):
            raise OSError("queue down")

    redis = FakeRedis()
    with pytest.raises(OSError, match="queue down"):
        QueueCoordinator(FailingQueue(), redis=redis).enqueue_prepare("campaign-1")
    assert PREFIX + "job:campaign-prepare-campaign-1" not in redis.values
