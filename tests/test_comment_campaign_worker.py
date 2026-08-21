from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import ModuleType

import pytest

from comment_campaign import worker
from comment_campaign.worker_identity import (
    build_worker_health_value,
    extract_legacy_worker_pid,
    parse_worker_health_value,
    project_fingerprint,
)


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


def test_worker_health_identity_round_trips_v2_contract(tmp_path):
    root = tmp_path / "project"
    value = build_worker_health_value(321, root, "a" * 32)

    identity = parse_worker_health_value(value.encode("utf-8"))

    assert value == f"worker:v2:321:{project_fingerprint(root)}:{'a' * 32}"
    assert identity is not None
    assert identity.pid == 321
    assert identity.project_fingerprint == project_fingerprint(root)
    assert identity.owner_nonce == "a" * 32


def test_project_fingerprint_normalizes_equivalent_roots(tmp_path):
    root = tmp_path / "project"
    assert project_fingerprint(root) == project_fingerprint(root / "child" / "..")


@pytest.mark.parametrize(
    "value",
    [
        "worker:321:legacy",
        "worker:v1:321:" + "a" * 64 + ":" + "b" * 32,
        "worker:v2:0:" + "a" * 64 + ":" + "b" * 32,
        "worker:v2:321:not-a-fingerprint:" + "b" * 32,
        "worker:v2:321:" + "a" * 64 + ":bad nonce",
        b"\xff",
    ],
)
def test_worker_health_identity_rejects_legacy_and_malformed_values(value):
    assert parse_worker_health_value(value) is None


def test_legacy_worker_pid_is_exposed_only_for_exact_manual_migration_shape():
    assert extract_legacy_worker_pid("worker:321:" + "a" * 32) == 321
    assert extract_legacy_worker_pid("worker:321:legacy") is None


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


def test_runtime_build_uses_one_settings_snapshot_for_controller_and_bindings(monkeypatch):
    import adspower
    import comment_campaign.executor as executor_module
    import comment_campaign.profile_gateway as profile_gateway_module
    import comment_campaign.queueing as queueing_module
    import comment_campaign.service as service_module
    import execution_v2.adspower_adapter as adapter_module
    import execution_v2.locator as locator_module
    import execution_v2.service as execution_service_module
    import execution_v2.store as store_module
    import execution_v2.tiling as tiling_module
    import gateway.settings_store as settings_module

    calls = []
    persisted = {
        "adspower": {"base_url": "http://persisted", "api_key": "persisted-key"},
        "comment_campaign": {"element_bindings": {"entry_element_id": "entry"}},
    }
    monkeypatch.setattr(settings_module, "load_settings", lambda: calls.append(1) or persisted)
    monkeypatch.delenv("ADSPOWER_BASE_URL", raising=False)
    monkeypatch.delenv("ADSPOWER_API_KEY", raising=False)

    class Controller:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class QueueCoordinator:
        @classmethod
        def from_url(cls, _url):
            return type("Coordinator", (), {"redis": object()})()

    class CampaignService:
        def __init__(self):
            self.store = object()
            self._runtime_closeables = []

    class ExecutionStore:
        def __init__(self, _path):
            pass

        def initialize(self):
            pass

        def get_element(self, _identifier):
            return None

    monkeypatch.setattr(adspower, "AdsPowerController", Controller)
    monkeypatch.setattr(queueing_module, "QueueCoordinator", QueueCoordinator)
    monkeypatch.setattr(
        service_module, "create_default_comment_campaign_service", lambda **_kwargs: CampaignService()
    )
    monkeypatch.setattr(store_module, "ExecutionStore", ExecutionStore)
    monkeypatch.setattr(adapter_module, "RateLimitedAdsPowerAdapter", lambda value: value)
    monkeypatch.setattr(locator_module, "StrictLocatorResolver", lambda: object())
    monkeypatch.setattr(execution_service_module, "_OwnedPlaywrightSessions", lambda value: value)
    monkeypatch.setattr(tiling_module, "tile_browser_bindings", object())
    monkeypatch.setattr(profile_gateway_module, "ProfileGateway", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(executor_module, "CommentExecutor", lambda *_args, **_kwargs: object())

    service = worker.build_runtime_service()

    assert calls == [1]
    assert service._runtime_closeables


def test_runtime_build_propagates_an_unknown_settings_loader_failure(monkeypatch):
    import comment_campaign.queueing as queueing_module
    import gateway.settings_store as settings_module

    class QueueCoordinator:
        @classmethod
        def from_url(cls, _url):
            return object()

    monkeypatch.setattr(queueing_module, "QueueCoordinator", QueueCoordinator)
    monkeypatch.setattr(
        settings_module,
        "load_settings",
        lambda: (_ for _ in ()).throw(RuntimeError("settings failure")),
    )

    with pytest.raises(RuntimeError, match="settings failure"):
        worker.build_runtime_service()


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
    identity = parse_worker_health_value(redis.values[0][1])
    assert identity is not None
    assert identity.pid == worker.os.getpid()
    assert identity.project_fingerprint == project_fingerprint(worker.PROJECT_ROOT)
    assert redis.values[0][2] == 30


def test_worker_cli_rejects_a_foreign_project_fingerprint(monkeypatch):
    called = []
    monkeypatch.setattr(worker, "serve", lambda **kwargs: called.append(kwargs))

    with pytest.raises(RuntimeError, match="project fingerprint mismatch"):
        worker.main([
            "serve",
            "--project-fingerprint", "0" * 64,
            "--owner-nonce", "a" * 32,
        ])

    assert called == []


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

    assert jobs.run_prepare_campaign("campaign-1", 7, 3) == {"ok": True}
    assert jobs.run_submit_assignment("campaign-1", "assignment-1", 3) == {"ok": True}
    assert jobs.run_reconcile_campaign("campaign-1") == {"ok": True}
    assert calls == [
        "service", ("prepare_campaign", ("campaign-1", 7, 3)),
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
        jobs.run_prepare_campaign("campaign-1", 7, 3)
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
        async def job_prepare_campaign(self, _campaign_id, _prepare_generation, _identity_generation):
            loop_ids.append(id(asyncio.get_running_loop()))
            return {"ok": True}
        async def aclose(self):
            loop_ids.append(id(asyncio.get_running_loop()))

    monkeypatch.setattr(jobs, "_service", lambda: Service())
    assert jobs.run_prepare_campaign("campaign-1", 7, 3) == {"ok": True}
    assert len(loop_ids) == 2 and loop_ids[0] == loop_ids[1]


def test_old_identity_generation_job_is_noop_before_any_store_or_executor_action():
    from comment_campaign.service import CommentCampaignService

    class Store:
        def get_campaign(self, _campaign_id):
            return {
                "id": "campaign-1", "status": "queued", "revision": 8,
                "prepare_generation": 7, "identity_generation": 4, "batch_size": 1,
            }

        def transition_campaign_status(self, *_args):  # pragma: no cover - tripwire
            raise AssertionError("stale job must not write Campaign state")

        def eligible_assignment_ids(self, *_args):  # pragma: no cover - tripwire
            raise AssertionError("stale job must not inspect eligible assignments")

    class Executor:
        async def prepare_batch(self, *_args):  # pragma: no cover - tripwire
            raise AssertionError("stale job must not open a Profile")

    result = __import__("asyncio").run(
        CommentCampaignService(Store(), executor=Executor()).job_prepare_campaign(
            "campaign-1", 7, 3
        )
    )

    assert result == {
        "stale": True, "prepared": (), "failed": (), "close_confirmed": True,
        "identity_generation": 4,
    }


def test_prepare_job_does_not_query_eligible_work_before_preflight_is_ready():
    from comment_campaign.service import CommentCampaignService

    class Store:
        def get_campaign(self, _campaign_id):
            return {
                "id": "campaign-1", "status": "queued", "revision": 8,
                "prepare_generation": 7, "identity_generation": 3, "batch_size": 1,
            }

        def account_preflight_required(self, _campaign_id):
            return True

        def transition_campaign_status(self, _campaign_id, _revision, status):
            assert status == "running"
            return {
                "id": "campaign-1", "status": "running", "revision": 9,
                "prepare_generation": 7, "identity_generation": 3, "batch_size": 1,
            }

        def eligible_assignment_ids(self, *_args):  # pragma: no cover - tripwire
            raise AssertionError("preflight must run before querying eligible assignments")

    class Executor:
        async def preflight_campaign_identities(self, _campaign_id, _generation):
            return type("Result", (), {"stale": False, "ready": False, "identity_generation": 3})()

    result = __import__("asyncio").run(
        CommentCampaignService(Store(), executor=Executor()).job_prepare_campaign(
            "campaign-1", 7, 3
        )
    )

    assert result["stale"] is False
    assert result["ready"] is False
    assert result["identity_generation"] == 3


def test_prepare_job_revalidates_generation_before_eligible_after_ready_preflight():
    from comment_campaign.service import CommentCampaignService

    class Store:
        reads = 0
        def get_campaign(self, _campaign_id):
            self.reads += 1
            return {
                "id": "campaign-1", "status": "running", "revision": 8,
                "prepare_generation": 7, "identity_generation": 3 if self.reads == 1 else 4,
                "batch_size": 1,
            }
        def account_preflight_required(self, _campaign_id): return True
        def eligible_assignment_ids(self, *_args): raise AssertionError("generation change must stop before eligible query")
    class Executor:
        async def preflight_campaign_identities(self, _campaign_id, _generation):
            return type("Result", (), {"stale": False, "ready": True, "identity_generation": 3})()
        async def prepare_batch(self, *_args): raise AssertionError("generation change must not open Profiles")

    result = __import__("asyncio").run(
        CommentCampaignService(Store(), executor=Executor()).job_prepare_campaign("campaign-1", 7, 3)
    )
    assert result == {"stale": True, "prepared": (), "failed": (), "close_confirmed": True, "identity_generation": 4}


def test_worker_requeues_preflight_required_campaign_without_eligible_assignments():
    class Store(FakeStore):
        def recover_interrupted_campaigns(self):
            return {}

        def list_campaigns(self, _status, _limit, offset):
            return [{
                "id": "campaign", "status": "running", "prepare_generation": 2,
                "identity_generation": 5,
            }] if offset == 0 else []

        def get_campaign(self, _campaign_id):
            return {
                "id": "campaign", "status": "running", "prepare_generation": 2,
                "identity_generation": 5,
            }

        def account_preflight_required(self, _campaign_id):
            return True

        def eligible_assignment_ids(self, _campaign_id):  # pragma: no cover - tripwire
            raise AssertionError("preflight-required Campaign must not need eligible rows")

        def pending_reconcile_prepare_generation(self, _campaign_id):
            return 2

        def mark_reconcile_prepare_generation(self, *_args):
            return True

    class Queue:
        def __init__(self):
            self.jobs = []

        def fetch_job(self, _job_id):
            return None

        def enqueue(self, *args, **kwargs):
            self.jobs.append((args, kwargs))
            return {"id": kwargs["job_id"]}

    store, redis, queue = Store(), FakeRedis(), Queue()
    worker.serve(
        store_factory=lambda: store, redis_factory=lambda _url: redis,
        queue_factory=lambda _connection: queue,
        worker_factory=lambda _queue, _connection: FakeWorker(),
        heartbeat_interval_seconds=0,
    )

    assert queue.jobs[-1][0][1:] == ("campaign", 2, 5)
    assert queue.jobs[-1][1]["job_id"] == "campaign-prepare-campaign-g2"


@pytest.mark.parametrize(
    ("pending_generation", "prepare_generation", "expected_generation"),
    [(7, 7, 7), (None, 7, 8)],
)
def test_worker_keeps_future_scheduled_prepare_jobs_scheduled(
    pending_generation, prepare_generation, expected_generation,
):
    scheduled_at = datetime(2026, 8, 12, 9, tzinfo=timezone.utc)

    class Store(FakeStore):
        def recover_interrupted_campaigns(self):
            return {}

        def list_campaigns(self, _status, _limit, offset):
            return [{
                "id": "campaign", "status": "queued",
                "prepare_generation": prepare_generation, "identity_generation": 5,
                "start_mode": "scheduled", "scheduled_at": scheduled_at.isoformat(),
            }] if offset == 0 else []

        def get_campaign(self, _campaign_id):
            return self.list_campaigns(None, 1, 0)[0]

        def account_preflight_required(self, _campaign_id):
            return True

        def eligible_assignment_ids(self, _campaign_id):  # pragma: no cover - tripwire
            raise AssertionError("preflight requirement is sufficient to enqueue")

        def pending_reconcile_prepare_generation(self, _campaign_id):
            return pending_generation

        def next_prepare_generation(self, _campaign_id):
            assert pending_generation is None
            return 8

        def mark_reconcile_prepare_generation(self, *_args):
            return True

    class Queue:
        def __init__(self):
            self.calls = []

        def fetch_job(self, _job_id):
            return None

        def enqueue(self, *_args, **_kwargs):  # pragma: no cover - tripwire
            raise AssertionError("future scheduled Campaign must not enqueue immediately")

        def enqueue_at(self, when, function, *args, **kwargs):
            self.calls.append((when, function, args, kwargs))
            return {"id": kwargs["job_id"]}

    queue = Queue()
    worker.serve(
        store_factory=Store, redis_factory=lambda _url: FakeRedis(),
        queue_factory=lambda _connection: queue,
        worker_factory=lambda _queue, _connection: FakeWorker(),
        heartbeat_interval_seconds=0,
        now=lambda: datetime(2026, 8, 11, 9, tzinfo=timezone.utc),
    )

    assert queue.calls == [(
        scheduled_at, "comment_campaign.jobs.run_prepare_campaign",
        ("campaign", expected_generation, 5),
        {"job_id": f"campaign-prepare-campaign-g{expected_generation}", "job_timeout": 600, "result_ttl": 86400},
    )]


def test_worker_rebuilds_missing_acknowledged_prepare_job_as_next_generation():
    class Store(FakeStore):
        def __init__(self):
            super().__init__(); self.marked = []; self.generation = 1; self.pages = 0
        def recover_interrupted_campaigns(self): return {}
        def list_campaigns(self, _status, _limit, offset):
            self.pages += 1
            return [{"id": "campaign", "status": "running", "prepare_generation": 1, "identity_generation": 3}] if offset == 0 else []
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
                {"id": "campaign", "status": "running", "prepare_generation": 2, "identity_generation": 3}
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
                {"id": "campaign", "status": "running", "prepare_generation": 1, "identity_generation": 3}
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
