"""Redis leases and the small, idempotent RQ boundary for Campaign work."""

from __future__ import annotations

from datetime import datetime, timezone
from secrets import token_urlsafe
import time
from typing import Any


PREFIX = "browser_v2:comment_campaign:"
QUEUE_NAME = "browser_v2_comment_campaign"
RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
REFRESH_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""


class RedisUnavailableError(RuntimeError):
    """Redis could not answer; this is distinct from a lost lease."""


def prefixed_key(key: str) -> str:
    if not isinstance(key, str) or not key or key.startswith(PREFIX):
        raise ValueError("lease key must be a non-empty, unprefixed suffix")
    return PREFIX + key


class RedisLease:
    """A TTL lease that cannot alter a lease acquired by another owner."""

    def __init__(self, redis: Any, key: str, owner: str, *, ttl_seconds: int) -> None:
        if not isinstance(owner, str) or not owner:
            raise ValueError("lease owner must be non-empty")
        if not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        self.redis = redis
        self.key = prefixed_key(key)
        self.owner = owner
        self.ttl_seconds = ttl_seconds

    def acquire(self) -> bool:
        try:
            return bool(self.redis.set(self.key, self.owner, nx=True, ex=self.ttl_seconds))
        except Exception as exc:
            raise RedisUnavailableError("Comment Campaign Redis is unavailable") from exc

    def refresh(self, ttl_seconds: int | None = None) -> bool:
        ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValueError("lease ttl_seconds must be positive")
        try:
            return bool(self.redis.eval(REFRESH_SCRIPT, 1, self.key, self.owner, ttl))
        except Exception as exc:
            raise RedisUnavailableError("Comment Campaign Redis is unavailable") from exc

    def release(self) -> bool:
        try:
            return bool(self.redis.eval(RELEASE_SCRIPT, 1, self.key, self.owner))
        except Exception as exc:
            raise RedisUnavailableError("Comment Campaign Redis is unavailable") from exc


class QueueCoordinator:
    """Create only one durable RQ job per Campaign operation and revision."""

    def __init__(
        self,
        queue: Any | None = None,
        *,
        redis: Any | None = None,
        redis_url: str | None = None,
        sleep=time.sleep,
        contention_attempts: int = 3,
    ) -> None:
        self._queue = queue
        self._redis = redis
        self._redis_url = redis_url
        self._sleep = sleep
        if not isinstance(contention_attempts, int) or contention_attempts <= 0:
            raise ValueError("contention_attempts must be positive")
        self._contention_attempts = contention_attempts

    @classmethod
    def from_url(cls, redis_url: str) -> "QueueCoordinator":
        return cls(redis_url=redis_url)

    @property
    def queue(self) -> Any:
        if self._queue is None:
            try:
                import redis
                from rq import Queue
            except ImportError as exc:
                raise RuntimeError("Comment Campaign queue requires redis and rq") from exc
            connection = redis.Redis.from_url(
                self._redis_url or "redis://127.0.0.1:6379/0",
                socket_connect_timeout=1.0, socket_timeout=1.0,
            )
            self._redis = connection
            self._queue = Queue(QUEUE_NAME, connection=connection)
        return self._queue

    @property
    def redis(self) -> Any:
        if self._redis is None:
            self._redis = getattr(self.queue, "connection", None)
        if self._redis is None:
            raise RuntimeError("Comment Campaign queue requires a Redis connection")
        return self._redis

    def enqueue_prepare_generation(
        self, campaign_id: str, prepare_generation: int, identity_generation: int,
    ):
        if type(prepare_generation) is not int or prepare_generation < 1:
            raise ValueError("prepare generation must be positive")
        if type(identity_generation) is not int or identity_generation < 0:
            raise ValueError("identity generation must be non-negative")
        return self._enqueue_once(
            f"campaign-prepare-{campaign_id}-g{prepare_generation}",
            "enqueue",
            "comment_campaign.jobs.run_prepare_campaign",
            campaign_id,
            prepare_generation,
            identity_generation,
            job_timeout=600,
            result_ttl=86400,
        )

    def enqueue_submit(self, campaign_id: str, assignment_id: str, revision: int):
        if type(revision) is not int or revision <= 0:
            raise ValueError("submit revision must be a positive integer")
        return self._enqueue_once(
            f"campaign-submit-{assignment_id}-r{revision}",
            "enqueue",
            "comment_campaign.jobs.run_submit_assignment",
            campaign_id,
            assignment_id,
            revision,
            job_timeout=300,
            result_ttl=86400,
        )

    def enqueue_reconcile_campaign(self, campaign_id: str, generation: int):
        """Worker-start recovery is a separate, idempotent, never-submit job."""
        if type(generation) is not int or generation < 1:
            raise ValueError("reconcile generation must be positive")
        return self._enqueue_once(
            f"campaign-reconcile-{campaign_id}-g{generation}",
            "enqueue",
            "comment_campaign.jobs.run_reconcile_campaign",
            campaign_id,
            job_timeout=300,
            result_ttl=86400,
        )

    def enqueue_at(
        self, campaign_id: str, when: datetime, prepare_generation: int,
        identity_generation: int,
    ):
        if not isinstance(when, datetime) or when.tzinfo is None:
            raise ValueError("scheduled Campaign time must be timezone-aware")
        if type(prepare_generation) is not int or prepare_generation < 1:
            raise ValueError("prepare generation must be positive")
        if type(identity_generation) is not int or identity_generation < 0:
            raise ValueError("identity generation must be non-negative")
        return self._enqueue_once(
            f"campaign-prepare-{campaign_id}-g{prepare_generation}",
            "enqueue_at",
            when.astimezone(timezone.utc),
            "comment_campaign.jobs.run_prepare_campaign",
            campaign_id,
            prepare_generation,
            identity_generation,
            job_timeout=600,
            result_ttl=86400,
        )

    def _enqueue_once(self, job_id: str, operation: str, *args: Any, **options: Any):
        # Fixed IDs initiate a Campaign once.  Task 6 must use a generation or
        # revision suffix for later batches instead of replaying this kickoff ID.
        existing = self._fetch_existing(job_id)
        if existing is not None:
            return existing
        gate = RedisLease(self.redis, f"job:{job_id}", token_urlsafe(24), ttl_seconds=30)
        if not gate.acquire():
            return self._wait_for_existing(job_id)
        try:
            existing = self._fetch_existing(job_id)
            if existing is not None:
                return existing
            job = getattr(self.queue, operation)(*args, job_id=job_id, **options)
            # The job is already durable.  A failed gate extension must not
            # pretend it was not enqueued; fixed-ID retries fetch it instead.
            try:
                gate.refresh(int(options["result_ttl"]))
            except RedisUnavailableError:
                pass
            return job
        except Exception:
            try:
                gate.release()
            except RedisUnavailableError:
                pass
            raise

    def _wait_for_existing(self, job_id: str):
        for _ in range(self._contention_attempts):
            existing = self._fetch_existing(job_id)
            if existing is not None:
                return existing
            self._sleep(0.05)
        raise RuntimeError("Comment Campaign queue job is being created; retry shortly")

    def _fetch_existing(self, job_id: str):
        fetch = getattr(self.queue, "fetch_job", None)
        if callable(fetch):
            return fetch(job_id)
        return None
