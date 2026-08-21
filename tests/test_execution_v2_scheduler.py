import asyncio

from execution_v2.models import BrowserBinding, ProfileOutcome, Stage
from execution_v2.scheduler import BatchScheduler
from execution_v2.store import ExecutionStore


class FakeAdsPowerAdapter:
    def __init__(self, events, *, never_closes=(), start_raises_after_open=()):
        self.events = events
        self.never_closes = set(never_closes)
        self.start_raises_after_open = set(start_raises_after_open)
        self.active = set()
        self.started = []
        self.stopped = []
        self.active_checks = {}
        self.max_active = 0

    async def start(self, profile_id):
        self.events.append(("start", profile_id))
        self.started.append(profile_id)
        self.active.add(profile_id)
        self.max_active = max(self.max_active, len(self.active))
        if profile_id in self.start_raises_after_open:
            raise RuntimeError("started then failed")
        return f"ws://{profile_id}"

    async def stop(self, profile_id):
        self.events.append(("stop", profile_id))
        self.stopped.append(profile_id)
        if profile_id not in self.never_closes:
            self.active.discard(profile_id)

    async def is_active(self, profile_id):
        self.active_checks[profile_id] = self.active_checks.get(profile_id, 0) + 1
        is_active = profile_id in self.active
        if not is_active:
            self.events.append(("closed", profile_id))
        return is_active


class FakeSessionFactory:
    def __init__(self, events, *, fails=()):
        self.events = events
        self.fails = set(fails)

    async def connect(self, profile_id, ws_url):
        self.events.append(("connect", profile_id))
        if profile_id in self.fails:
            raise RuntimeError("planned connection failure")
        return BrowserBinding(profile_id, ws_url, object(), object(), object())


def successful_executor(events):
    async def execute(binding, _snapshot):
        events.append(("execute", binding.profile_id))
        return ProfileOutcome(
            binding.profile_id,
            True,
            Stage.EXECUTE_ACTION,
            action_results=({"type": "wait", "status": "succeeded"},),
        )

    return execute


def initialized_store(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    return store


def test_300_profiles_run_in_100_batches_of_three_and_close_before_next_batch(tmp_path):
    profiles = [f"profile-{index:03d}" for index in range(300)]
    events = []
    adapter = FakeAdsPowerAdapter(events)
    sessions = FakeSessionFactory(events)
    store = initialized_store(tmp_path)
    scheduler = BatchScheduler(store, adapter, sessions, successful_executor(events))

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {"revision": 1}, profiles, 3))

    assert result == {"job_id": "job-1", "status": "completed", "total_batches": 100}
    assert adapter.max_active == 3
    assert adapter.started == profiles
    assert adapter.stopped == profiles
    for boundary in range(3, 300, 3):
        assert events.index(("closed", profiles[boundary - 1])) < events.index(
            ("start", profiles[boundary])
        )
    assert len(store.list_profile_results("job-1")) == 300
    with store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM action_results").fetchone()[0] == 300


def test_one_profile_failure_does_not_cancel_its_siblings(tmp_path):
    async def execute(binding, _snapshot):
        if binding.profile_id == "p2":
            raise RuntimeError("planned failure")
        return ProfileOutcome(binding.profile_id, True, Stage.EXECUTE_ACTION)

    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([])
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), execute)

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3"], 3))
    rows = {row["profile_id"]: row for row in store.list_profile_results("job-1")}

    assert result["status"] == "completed"
    assert rows["p1"]["status"] == "succeeded"
    assert rows["p2"]["status"] == "failed"
    assert rows["p3"]["status"] == "succeeded"


def test_complete_batch_tiles_after_connections_and_before_actions(tmp_path):
    events = []
    store = initialized_store(tmp_path)

    async def tile(bindings):
        events.append(("tile", [item.profile_id for item in bindings]))

    scheduler = BatchScheduler(
        store,
        FakeAdsPowerAdapter(events),
        FakeSessionFactory(events),
        successful_executor(events),
        tile_batch=tile,
    )
    asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3"], 3))

    assert events.index(("connect", "p3")) < events.index(
        ("tile", ["p1", "p2", "p3"])
    )
    assert events.index(("tile", ["p1", "p2", "p3"])) < events.index(
        ("execute", "p1")
    )


def test_tile_failure_blocks_actions_and_still_closes_the_batch(tmp_path):
    events = []
    store = initialized_store(tmp_path)

    async def failed_tile(_bindings):
        events.append(("tile", "failed"))
        raise RuntimeError("private tiler detail")

    scheduler = BatchScheduler(
        store,
        FakeAdsPowerAdapter(events),
        FakeSessionFactory(events),
        successful_executor(events),
        tile_batch=failed_tile,
    )
    asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2"], 2))
    rows = store.list_profile_results("job-1")

    assert not [event for event in events if event[0] == "execute"]
    assert [event for event in events if event[0] == "stop"] == [
        ("stop", "p1"),
        ("stop", "p2"),
    ]
    assert {row["stage"] for row in rows} == {"window_tile"}
    assert {row["error_code"] for row in rows} == {"window_tile_failed"}
    assert all("private" not in row["error_summary"] for row in rows)


def test_start_or_connection_failure_does_not_cancel_its_siblings(tmp_path):
    async def execute(binding, _snapshot):
        return ProfileOutcome(binding.profile_id, True, Stage.EXECUTE_ACTION)

    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([], start_raises_after_open={"p1"})
    sessions = FakeSessionFactory([], fails={"p2"})
    scheduler = BatchScheduler(store, adapter, sessions, execute)

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3"], 3))
    rows = {row["profile_id"]: row for row in store.list_profile_results("job-1")}

    assert result["status"] == "completed"
    assert rows["p1"]["status"] == "failed"
    assert rows["p2"]["status"] == "failed"
    assert rows["p3"]["status"] == "succeeded"
    assert adapter.stopped == ["p1", "p2", "p3"]


def test_close_failure_blocks_later_batches(tmp_path):
    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([], never_closes={"p2"})
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), successful_executor([]))

    result = asyncio.run(
        scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3", "p4"], 3)
    )

    assert result["status"] == "cleanup_blocked"
    assert "p4" not in adapter.started
    assert adapter.active_checks["p2"] == 3
    assert store.get_job("job-1")["status"] == "cleanup_blocked"


def test_cancel_request_closes_current_batch_without_starting_the_next(tmp_path):
    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([])

    async def execute(binding, _snapshot):
        store.request_cancel("job-1")
        return ProfileOutcome(binding.profile_id, True, Stage.EXECUTE_ACTION)

    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), execute)
    result = asyncio.run(
        scheduler.run("job-1", "strategy-1", {}, ["p1", "p2", "p3", "p4"], 3)
    )

    assert result["status"] == "cancelled"
    assert adapter.started == ["p1", "p2", "p3"]
    assert adapter.stopped == ["p1", "p2", "p3"]


def test_start_that_opens_then_raises_is_still_stopped(tmp_path):
    store = initialized_store(tmp_path)
    adapter = FakeAdsPowerAdapter([], start_raises_after_open={"p1"})
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), successful_executor([]))

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1"], 3))

    assert result["status"] == "completed"
    assert adapter.stopped == ["p1"]
    row = store.list_profile_results("job-1")[0]
    assert row["status"] == "failed"
    assert row["close_confirmed"] is True


def test_unknown_active_state_is_not_confirmed_and_blocks_after_three_rounds(tmp_path):
    class UnknownStateAdapter(FakeAdsPowerAdapter):
        async def is_active(self, profile_id):
            self.active_checks[profile_id] = self.active_checks.get(profile_id, 0) + 1
            raise RuntimeError("unknown active state")

    store = initialized_store(tmp_path)
    adapter = UnknownStateAdapter([])
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), successful_executor([]))

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1"], 3))

    assert result["status"] == "cleanup_blocked"
    assert adapter.active_checks["p1"] == 3


def test_cleanup_persists_closing_stage_before_ads_power_stop(tmp_path):
    store = initialized_store(tmp_path)
    seen = []
    original = store.set_profile_status

    def record_status(job_id, profile_id, status, stage, **kwargs):
        seen.append((profile_id, str(status), str(stage)))
        return original(job_id, profile_id, status, stage, **kwargs)

    store.set_profile_status = record_status
    scheduler = BatchScheduler(store, FakeAdsPowerAdapter([]), FakeSessionFactory([]), successful_executor([]))

    asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1"], 3))

    assert ("p1", "closing", "adspower_stop") in seen


def test_explicit_inactive_state_confirms_cleanup_even_when_stop_call_errors(tmp_path):
    class StopErrorButInactiveAdapter(FakeAdsPowerAdapter):
        async def stop(self, profile_id):
            self.events.append(("stop", profile_id))
            self.stopped.append(profile_id)
            self.active.discard(profile_id)
            raise RuntimeError("stop response was lost")

    store = initialized_store(tmp_path)
    adapter = StopErrorButInactiveAdapter([])
    scheduler = BatchScheduler(
        store, adapter, FakeSessionFactory([]), successful_executor([])
    )

    result = asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1"], 3))

    assert result["status"] == "completed"
    assert adapter.active_checks["p1"] == 1


def test_failure_summary_redacts_websocket_and_credentials_before_storage(tmp_path):
    class SensitiveFailureSessions(FakeSessionFactory):
        async def connect(self, _profile_id, _ws_url):
            raise RuntimeError(
                "connect wss://secret.example/devtools?token=hidden "
                "api_key=hidden-key password=hidden-password"
            )

    store = initialized_store(tmp_path)
    scheduler = BatchScheduler(
        store,
        FakeAdsPowerAdapter([]),
        SensitiveFailureSessions([]),
        successful_executor([]),
    )

    asyncio.run(scheduler.run("job-1", "strategy-1", {}, ["p1"], 3))
    summary = store.list_profile_results("job-1")[0]["error_summary"]

    assert "wss://" not in summary
    assert "hidden-key" not in summary
    assert "hidden-password" not in summary
    assert "[redacted-websocket]" in summary


def test_scheduler_rejects_empty_duplicate_and_unsupported_profile_inputs(tmp_path):
    store = initialized_store(tmp_path)
    scheduler = BatchScheduler(store, FakeAdsPowerAdapter([]), FakeSessionFactory([]), successful_executor([]))

    for profiles, batch_size in (([], 3), (["p1", "p1"], 3), (["p1"], 9)):
        try:
            asyncio.run(scheduler.run("job-1", "strategy-1", {}, profiles, batch_size))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid scheduler input must fail")


def test_existing_job_runs_once_when_two_workers_race(tmp_path):
    events = []
    store = initialized_store(tmp_path)
    store.prepare_job("job-1", "strategy-1", {}, ["p1"], 3)
    scheduler = BatchScheduler(
        store, FakeAdsPowerAdapter(events), FakeSessionFactory(events), successful_executor(events)
    )

    async def run_race():
        return await asyncio.gather(
            scheduler.run_existing("job-1"), scheduler.run_existing("job-1")
        )

    results = asyncio.run(run_race())
    assert [event for event in events if event == ("execute", "p1")] == [("execute", "p1")]
    assert {result["status"] for result in results} <= {"running", "completed"}
    assert store.get_job("job-1")["status"] == "completed"


def test_restart_cleanup_never_replays_jobs_and_marks_unconfirmed_cleanup(tmp_path):
    store = initialized_store(tmp_path)
    store.prepare_job("job-1", "strategy-1", {}, ["p1", "p2"], 3)
    adapter = FakeAdsPowerAdapter([], never_closes={"p2"})
    adapter.active.add("p2")
    scheduler = BatchScheduler(store, adapter, FakeSessionFactory([]), successful_executor([]))

    recovered = asyncio.run(scheduler.cleanup_after_restart())
    rows = {row["profile_id"]: row for row in store.list_profile_results("job-1")}

    assert recovered == [{"job_id": "job-1", "status": "cleanup_blocked"}]
    assert adapter.started == []
    assert adapter.stopped == ["p1", "p2", "p2", "p2"]
    assert rows["p1"]["error_code"] == "service_restarted"
    assert rows["p2"]["error_code"] == "cleanup_blocked"
    assert store.get_job("job-1")["status"] == "cleanup_blocked"
    assert asyncio.run(scheduler.cleanup_after_restart()) == []
