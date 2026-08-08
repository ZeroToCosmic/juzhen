from __future__ import annotations

import sys
from types import ModuleType

import pytest

from comment_campaign import worker


class FakeStore:
    def __init__(self):
        self.recovery_calls = 0
        self.closed = False

    def recover_interrupted_submissions(self):
        self.recovery_calls += 1
        return 2

    def close(self):
        self.closed = True


class FakeRedis:
    def __init__(self):
        self.values = []
        self.current = {}

    def set(self, key, value, *, nx, ex):
        self.values.append((key, value, ex))
        if nx and key in self.current:
            return False
        self.current[key] = value
        return True

    def eval(self, script, _keys, key, owner, *_args):
        if self.current.get(key) != owner:
            return 0
        if "del" in script:
            del self.current[key]
        return 1


class FakeWorker:
    def __init__(self):
        self.calls = []

    def work(self, *, with_scheduler):
        self.calls.append(with_scheduler)


def test_windows_runtime_selects_spawn_worker(monkeypatch):
    rq_module = ModuleType("rq")

    class DefaultWorker:
        pass

    class SpawnWorker:
        pass

    rq_module.Worker = DefaultWorker
    rq_module.SpawnWorker = SpawnWorker
    monkeypatch.setitem(sys.modules, "rq", rq_module)
    monkeypatch.setattr(worker.os, "name", "nt")

    assert worker._rq_worker_class() is SpawnWorker


def test_worker_serve_recovers_then_heartbeats_and_uses_scheduler(monkeypatch):
    store = FakeStore()
    redis = FakeRedis()
    created = []

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            self.target = target
        def start(self):
            self.target()
        def join(self, timeout=None):
            return None

    worker_instance = FakeWorker()
    monkeypatch.setattr(worker.threading, "Thread", ImmediateThread)

    worker.serve(
        store_factory=lambda: store,
        redis_factory=lambda _url: redis,
        queue_factory=lambda _connection: "queue",
        worker_factory=lambda queue, connection: created.append((queue, connection)) or worker_instance,
        heartbeat_interval_seconds=0,
    )

    assert store.recovery_calls == 1
    assert store.closed is True
    assert created == [("queue", redis)]
    assert worker_instance.calls == [True]
    assert redis.values[0][0] == worker.WORKER_HEALTH_KEY
    assert redis.values[0][1].startswith(worker.WORKER_HEALTH_VALUE + ":")
    assert redis.values[0][2] == 30


def test_second_worker_never_recovers_while_health_lease_is_owned():
    store = FakeStore()
    redis = FakeRedis()
    redis.current[worker.WORKER_HEALTH_KEY] = "other-worker"
    started = []

    try:
        worker.serve(
            store_factory=lambda: store,
            redis_factory=lambda _url: redis,
            queue_factory=lambda _connection: "queue",
            worker_factory=lambda *_args: started.append(True),
            heartbeat_interval_seconds=0,
        )
    except RuntimeError as exc:
        assert "already held" in str(exc)
    else:
        raise AssertionError("a second worker must not touch the SQLite recovery path")
    assert store.recovery_calls == 0
    assert started == []
    assert store.closed is True


def test_importable_jobs_delegate_only_safe_identifiers(monkeypatch):
    calls = []
    from comment_campaign import jobs

    monkeypatch.setattr(jobs, "_service", lambda: calls.append("service") or object())
    monkeypatch.setattr(jobs, "_call", lambda service, method, *args: calls.append((method, args)) or {"ok": True})

    assert jobs.run_prepare_campaign("campaign-1") == {"ok": True}
    assert jobs.run_submit_assignment("campaign-1", "assignment-1", 3) == {"ok": True}
    assert jobs.run_reconcile_campaign("campaign-1") == {"ok": True}
    assert calls == [
        "service", ("prepare_campaign", ("campaign-1",)),
        "service", ("submit_assignment", ("campaign-1", "assignment-1", 3)),
        "service", ("reconcile_campaign", ("campaign-1",)),
    ]


def test_job_closes_runtime_service_when_task_operation_fails(monkeypatch):
    from comment_campaign import jobs

    class Service:
        closed = False
        def close(self):
            self.closed = True

    service = Service()
    monkeypatch.setattr(jobs, "_service", lambda: service)

    try:
        jobs.run_prepare_campaign("campaign-1")
    except RuntimeError as exc:
        assert "prepare_campaign" in str(exc)
    else:
        raise AssertionError("Task 6 operation must be unavailable until implemented")
    assert service.closed is True


def test_job_operation_and_async_close_use_the_same_event_loop(monkeypatch):
    import asyncio
    from comment_campaign import jobs

    loop_ids = []

    class Service:
        async def job_prepare_campaign(self, _campaign_id):
            loop_ids.append(id(asyncio.get_running_loop()))
            return {"ok": True}
        async def aclose(self):
            loop_ids.append(id(asyncio.get_running_loop()))

    monkeypatch.setattr(jobs, "_service", lambda: Service())
    assert jobs.run_prepare_campaign("campaign-1") == {"ok": True}
    assert len(loop_ids) == 2 and loop_ids[0] == loop_ids[1]


def test_worker_rebuilds_missing_acknowledged_prepare_job_as_next_generation():
    class Store(FakeStore):
        def __init__(self):
            super().__init__(); self.marked = []; self.generation = 1; self.pages = 0
        def recover_interrupted_campaigns(self): return {}
        def list_campaigns(self, _status, _limit, offset):
            self.pages += 1
            return [{"id": "campaign", "status": "running", "prepare_generation": 1}] if offset == 0 else []
        def eligible_assignment_ids(self, _campaign_id): return ["a"]
        def pending_reconcile_prepare_generation(self, _campaign_id): return None
        def next_prepare_generation(self, _campaign_id): self.generation += 1; return self.generation
        def mark_reconcile_prepare_generation(self, campaign_id, generation): self.marked.append((campaign_id, generation)); return True

    class Queue:
        def __init__(self): self.jobs = []
        def fetch_job(self, _job_id): return None
        def enqueue(self, *args, **kwargs): self.jobs.append((args, kwargs)); return {"id": kwargs["job_id"]}

    store, redis, queue = Store(), FakeRedis(), Queue()
    worker.serve(
        store_factory=lambda: store, redis_factory=lambda _url: redis,
        queue_factory=lambda _connection: queue,
        worker_factory=lambda _queue, _connection: FakeWorker(), heartbeat_interval_seconds=0,
    )
    assert store.marked == [("campaign", 2)]
    assert queue.jobs[-1][1]["job_id"] == "campaign-prepare-campaign-g2"


def test_worker_retries_the_same_pending_generation_after_enqueue_failure():
    class Store(FakeStore):
        def __init__(self):
            super().__init__()
            self.marked = []

        def recover_interrupted_campaigns(self):
            return {}

        def list_campaigns(self, _status, _limit, offset):
            return [
                {"id": "campaign", "status": "running", "prepare_generation": 2}
            ] if offset == 0 else []

        def eligible_assignment_ids(self, _campaign_id):
            return ["assignment"]

        def pending_reconcile_prepare_generation(self, _campaign_id):
            return None if self.marked else 2

        def next_prepare_generation(self, _campaign_id):  # pragma: no cover - tripwire
            raise AssertionError("a pending generation must be retried, not advanced")

        def mark_reconcile_prepare_generation(self, campaign_id, generation):
            self.marked.append((campaign_id, generation))
            return True

    class Queue:
        def __init__(self):
            self.fail_once = True
            self.jobs = []

        def fetch_job(self, _job_id):
            return None

        def enqueue(self, *args, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("redis enqueue failed")
            self.jobs.append((args, kwargs))
            return {"id": kwargs["job_id"]}

    store, redis, queue = Store(), FakeRedis(), Queue()
    options = dict(
        store_factory=lambda: store,
        redis_factory=lambda _url: redis,
        queue_factory=lambda _connection: queue,
        worker_factory=lambda _queue, _connection: FakeWorker(),
        heartbeat_interval_seconds=0,
    )
    with pytest.raises(RuntimeError, match="redis enqueue failed"):
        worker.serve(**options)
    worker.serve(**options)

    assert store.marked == [("campaign", 2)]
    assert [item[1]["job_id"] for item in queue.jobs] == [
        "campaign-prepare-campaign-g2"
    ]


@pytest.mark.parametrize(
    ("status", "should_rebuild"),
    [("finished", True), ("failed", True), ("queued", False), ("started", False)],
)
def test_worker_rebuilds_only_terminal_acknowledged_jobs(status, should_rebuild):
    class Store(FakeStore):
        def __init__(self):
            super().__init__()
            self.generation = 1
            self.marked = []

        def recover_interrupted_campaigns(self):
            return {}

        def list_campaigns(self, _status, _limit, offset):
            return [
                {"id": "campaign", "status": "running", "prepare_generation": 1}
            ] if offset == 0 else []

        def eligible_assignment_ids(self, _campaign_id):
            return ["assignment"]

        def pending_reconcile_prepare_generation(self, _campaign_id):
            return None

        def next_prepare_generation(self, _campaign_id):
            self.generation += 1
            return self.generation

        def mark_reconcile_prepare_generation(self, campaign_id, generation):
            self.marked.append((campaign_id, generation))
            return True

    class ExistingJob:
        def get_status(self, *, refresh):
            assert refresh is True
            return status

    class Queue:
        def __init__(self):
            self.jobs = []

        def fetch_job(self, job_id):
            if job_id == "campaign-prepare-campaign-g1":
                return ExistingJob()
            return None

        def enqueue(self, *args, **kwargs):
            self.jobs.append((args, kwargs))
            return {"id": kwargs["job_id"]}

    store, redis, queue = Store(), FakeRedis(), Queue()
    worker.serve(
        store_factory=lambda: store,
        redis_factory=lambda _url: redis,
        queue_factory=lambda _connection: queue,
        worker_factory=lambda _queue, _connection: FakeWorker(),
        heartbeat_interval_seconds=0,
    )

    assert bool(queue.jobs) is should_rebuild
    assert store.marked == ([('campaign', 2)] if should_rebuild else [])
