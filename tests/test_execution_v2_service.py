import asyncio
from pathlib import Path
import threading
import time

import pytest

from execution_v2.models import BrowserBinding, JobStatus, ProfileStatus, Stage
from execution_v2.service import (
    ExecutionConflictError,
    ExecutionV2Service,
    PickerSessionNotFoundError,
    ProfileTokenError,
    create_default_execution_v2_service,
)
from execution_v2.store import ExecutionStore
from execution_v2.wheel_calibration import WheelCalibrationError


def definition():
    return {
        "url_pattern": "https://www.tiktok.com/",
        "frame_path": [],
        "locators": [{"type": "css", "value": "button", "priority": 1}],
        "diagnostic_metadata": {},
        "screenshot_path": "",
    }


class FakeAdsPower:
    def __init__(self):
        self.active = set()
        self.started = []
        self.stopped = []

    async def start(self, profile_id):
        self.active.add(profile_id)
        self.started.append(profile_id)
        return f"ws://{profile_id}/secret"

    async def stop(self, profile_id):
        self.stopped.append(profile_id)
        self.active.discard(profile_id)

    async def is_active(self, profile_id):
        return profile_id in self.active


class FakePage:
    def __init__(self, *, goto_error=False):
        self.goto_error = goto_error
        self.urls = []

    async def goto(self, url):
        self.urls.append(url)
        if self.goto_error:
            raise RuntimeError("cannot navigate wss://secret")


class FakeSessions:
    def __init__(self, *, goto_error=False):
        self.page = FakePage(goto_error=goto_error)

    async def connect(self, profile_id, ws_url):
        return BrowserBinding(profile_id, ws_url, object(), object(), self.page)


class FakePickerSession:
    def __init__(self):
        self.selections = []
        self.closed = []
        self.pending = [
            {
                "tag": "button", "attributes": {}, "role": "button", "name": "Comment",
                "text_preview": "", "frame_path": [], "original_fingerprint": "x",
                "actionable_ancestor_fingerprint": "x", "bounding_box": {},
            }
        ]
        self.current = None

    async def next_selection(self):
        if self.pending:
            self.current = self.pending.pop(0)
            return self.current
        await asyncio.Event().wait()

    async def save_selection(self, name, purpose, kind):
        if self.current is None:
            raise RuntimeError("selection required")
        saved = {"name": name, "purpose": purpose, "kind": kind, "definition": definition()}
        self.selections.append(saved)
        self.current = None
        if not self.pending:
            self.pending.append({"tag": "button"})
        return saved

    async def finish(self):
        self.closed.append("finish")
        return tuple(self.selections)

    async def cancel(self):
        self.closed.append("cancel")


class FakePicker:
    def __init__(self):
        self.session = FakePickerSession()

    async def start(self, binding, target_url):
        self.binding = binding
        self.target_url = target_url
        return self.session


class FakeScheduler:
    def __init__(self, store, *, hold=False):
        self.store = store
        self.recovered = 0
        self.cancelled = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not hold:
            self.release.set()

    async def cleanup_after_restart(self):
        self.recovered += 1
        return []

    async def run_existing(self, job_id):
        self.started.set()
        await self.release.wait()
        self.store.set_job_status(job_id, JobStatus.COMPLETED)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)
        self.store.request_cancel(job_id)


class FakeWheelRunner:
    def __init__(self, *, hold=False, error=None):
        self.hold = hold
        self.error = error
        self.prepared = 0

    async def prepare(self, _page):
        self.prepared += 1

    async def collect(self, _page, progress, cancel_event):
        await progress(
            {
                "status": "waiting_for_sample",
                "sample_index": 0,
                "samples": ["waiting", "pending", "pending"],
            }
        )
        while self.hold and not cancel_event.is_set():
            await asyncio.sleep(0.001)
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if self.error:
            raise WheelCalibrationError(self.error)
        return {
            "direction": "down",
            "events": [
                {"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}
            ],
            "sample_count": 3,
            "replay_validated": True,
        }


def make_service(
    tmp_path,
    *,
    scheduler=None,
    sessions=None,
    picker=None,
    resolver=None,
    content_library_provider=None,
    wheel_runner=None,
):
    store = ExecutionStore(tmp_path / "v2.db")
    ads = FakeAdsPower()
    scheduler = scheduler or FakeScheduler(store)
    service = ExecutionV2Service(
        store=store,
        adspower=ads,
        sessions=sessions or FakeSessions(),
        scheduler=scheduler,
        picker=picker or FakePicker(),
        resolver=resolver,
        profile_provider=lambda: [
            {"id": "raw-profile-1234", "name": "safe profile", "status": "inactive"},
            {"id": "another-profile-1234", "name": "same suffix", "status": "inactive"},
        ],
        content_library_provider=content_library_provider,
        id_factory=iter(["picker-1", "element-1", "element-2", "job-1", "job-2"]).__next__,
        evidence_dir=tmp_path / "evidence",
        wheel_runner=wheel_runner,
    )
    return service, store, ads, scheduler


def _wait_for_calibration(service, terminal="completed"):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = service.get_wheel_calibration()
        if state["active"] and state["active"]["status"] == terminal:
            return state
        time.sleep(0.01)
    raise AssertionError(f"calibration did not reach {terminal}")


def test_calibration_and_picker_cannot_share_profile(tmp_path):
    runner = FakeWheelRunner(hold=True)
    service, _store, ads, _scheduler = make_service(
        tmp_path, wheel_runner=runner
    )
    try:
        token = service.list_profiles()[0]["profile_token"]
        started = service.start_wheel_calibration(
            token, "https://www.tiktok.com/"
        )
        assert started["status"] == "waiting_for_sample"
        with pytest.raises(ExecutionConflictError, match="profile_already_in_use"):
            service.start_picker(token, "https://www.tiktok.com/")
        cancelled = service.cancel_wheel_calibration()
        assert cancelled["status"] == "cancelled"
        assert ads.stopped == ["raw-profile-1234"]
    finally:
        service.close()


def test_calibration_rejects_non_https_url_before_profile_start(tmp_path):
    service, _store, ads, _scheduler = make_service(
        tmp_path, wheel_runner=FakeWheelRunner()
    )
    try:
        token = service.list_profiles()[0]["profile_token"]
        with pytest.raises(ValueError, match="target_url_invalid"):
            service.start_wheel_calibration(token, "http://www.tiktok.com/")
        assert ads.started == []
    finally:
        service.close()


def test_successful_calibration_publishes_and_closes_profile(tmp_path):
    service, _store, ads, _scheduler = make_service(
        tmp_path, wheel_runner=FakeWheelRunner()
    )
    try:
        token = service.list_profiles()[0]["profile_token"]
        service.start_wheel_calibration(token, "https://www.tiktok.com/")
        state = _wait_for_calibration(service)
        assert state["current"]["revision"] == 1
        assert state["current"]["event_count"] == 1
        assert ads.stopped == ["raw-profile-1234"]
    finally:
        service.close()


def test_failed_recalibration_keeps_previous_version(tmp_path):
    service, store, _ads, _scheduler = make_service(
        tmp_path,
        wheel_runner=FakeWheelRunner(error="wheel_calibration_inconsistent"),
    )
    store.publish_wheel_calibration(
        "tiktok_feed",
        "down",
        [{"delta_x": 0.0, "delta_y": 100.0, "delta_mode": 0, "delay_ms": 0.0}],
        3,
        replay_validated=True,
    )
    try:
        token = service.list_profiles()[0]["profile_token"]
        service.start_wheel_calibration(token, "https://www.tiktok.com/")
        state = _wait_for_calibration(service, "failed")
        assert state["current"]["revision"] == 1
        assert state["active"]["error_code"] == "wheel_calibration_inconsistent"
    finally:
        service.close()


def test_content_libraries_are_normalized_to_closed_public_shape(tmp_path):
    service, _store, _ads, _scheduler = make_service(
        tmp_path,
        content_library_provider=lambda: [
            {"id": "ofs", "name": "OFS", "copy_count": 40, "body": "must-not-leak"},
            {"id": "empty", "name": "", "copy_count": -3},
            {"id": "", "name": "invalid", "copy_count": 1},
        ],
    )
    try:
        assert service.list_content_libraries() == [
            {"id": "ofs", "name": "OFS", "copy_count": 40},
            {"id": "empty", "name": "empty", "copy_count": 0},
        ]
    finally:
        service.close()


def test_profiles_use_opaque_tokens_and_never_expose_raw_or_ambiguous_suffix(tmp_path):
    service, _store, _ads, scheduler = make_service(tmp_path)
    try:
        profiles = service.list_profiles()
        assert scheduler.recovered == 1
        assert {item["display_id"] for item in profiles} == {"***1234"}
        assert len({item["profile_token"] for item in profiles}) == 2
        assert all("raw-profile" not in str(item) for item in profiles)
        with pytest.raises(ProfileTokenError):
            service.start_picker("***1234", "https://www.tiktok.com/")
    finally:
        service.close()


def test_picker_stays_open_for_multiple_saves_then_confirmed_finish(tmp_path):
    picker = FakePicker()
    service, store, ads, _scheduler = make_service(tmp_path, picker=picker)
    try:
        token = service.list_profiles()[0]["profile_token"]
        session_id = service.start_picker(token, "https://www.tiktok.com/")["session_id"]
        assert service.get_picker(session_id)["status"] == "selection_ready"
        first = service.save_picker_selection(session_id, "comment entry", "action", "click")
        assert first["id"] == "element-1"
        assert service.get_picker(session_id)["status"] == "selection_ready"
        second = service.save_picker_selection(session_id, "comment send", "action", "click")
        assert second["id"] == "element-2"
        assert len(store.list_elements()) == 2
        closed = service.finish_picker(session_id)
        assert closed["cleanup"]["close_confirmed"] is True
        assert ads.stopped == ["raw-profile-1234"]
        with pytest.raises(PickerSessionNotFoundError):
            service.get_picker(session_id)
    finally:
        service.close()


def test_picker_repick_updates_existing_element_without_creating_another(tmp_path):
    picker = FakePicker()
    service, store, _ads, _scheduler = make_service(tmp_path, picker=picker)
    try:
        original = service.create_element(
            "comment entry", "action", "click", definition(), id="element-existing"
        )
        token = service.list_profiles()[0]["profile_token"]
        session_id = service.start_picker(token, "https://www.tiktok.com/")["session_id"]
        service.get_picker(session_id)
        updated = service.save_picker_selection(
            session_id,
            "comment entry",
            "action",
            "click",
            element_id=original["id"],
            expected_revision=original["revision"],
        )
        assert updated["id"] == original["id"]
        assert updated["revision"] == original["revision"] + 1
        assert len(store.list_elements()) == 1
        with pytest.raises(ValueError, match="fields_must_match"):
            service.save_picker_selection(
                session_id, "renamed", "action", "click", element_id=original["id"], expected_revision=2
            )
    finally:
        service.close()


def test_picker_start_failure_still_closes_adspower_profile(tmp_path):
    service, _store, ads, _scheduler = make_service(tmp_path, sessions=FakeSessions(goto_error=True))
    try:
        token = service.list_profiles()[0]["profile_token"]
        with pytest.raises(RuntimeError):
            service.start_picker(token, "https://www.tiktok.com/")
        assert ads.stopped == ["raw-profile-1234"]
        service.sessions = FakeSessions()
        assert service.start_picker(token, "https://www.tiktok.com/")["status"] == "waiting_for_selection"
    finally:
        service.close()


def _create_ready_strategy(store, *, element_id="ready", strategy_id="strategy"):
    ready = store.create_element(element_id, "ready", "readiness", "generic", definition())
    return store.create_strategy(
        strategy_id, "one", {
            "target_url": "https://www.tiktok.com/", "ready_element_id": ready["id"],
            "readiness_timeout_seconds": 5, "run_mode": "once", "loop_duration_minutes": None,
            "actions": [],
        }, True,
    )


def test_scroll_job_starts_without_wheel_calibration(tmp_path):
    service, store, ads, _scheduler = make_service(tmp_path)
    ready = store.create_element("ready-scroll", "ready", "readiness", "generic", definition())
    strategy = store.create_strategy(
        "scroll-strategy",
        "scroll",
        {
            "target_url": "https://www.tiktok.com/",
            "ready_element_id": ready["id"],
            "readiness_timeout_seconds": 5,
            "run_mode": "once",
            "loop_duration_minutes": None,
            "actions": [
                {
                    "id": "scroll-1",
                    "type": "scroll",
                    "direction": "down",
                    "distance_pixels": [400, 600],
                    "count": [1, 1],
                    "interval_seconds": [0.2, 0.5],
                }
            ],
        },
        True,
    )
    try:
        token = service.list_profiles()[0]["profile_token"]
        started = service.start_job(strategy["id"], [token], 1)
        assert started["status"] == "queued"
        snapshot = store.get_job(started["job_id"])["strategy_snapshot"]
        assert "wheel_calibration" not in snapshot
    finally:
        service.close()


def test_service_denies_second_active_job_and_releases_lease_after_completion(tmp_path):
    store = ExecutionStore(tmp_path / "v2.db")
    scheduler = FakeScheduler(store, hold=True)
    service, store, _ads, scheduler = make_service(tmp_path, scheduler=scheduler)
    try:
        strategy = _create_ready_strategy(store)
        token = service.list_profiles()[0]["profile_token"]
        first = service.start_job(strategy["id"], [token])
        with pytest.raises(ExecutionConflictError, match="already_active"):
            service.start_job(strategy["id"], [token])

        async def release():
            scheduler.release.set()

        service._run(release())
        service._jobs[first["job_id"]].result(timeout=2)
        second = service.start_job(strategy["id"], [token])
        assert second["status"] == "queued"
    finally:
        service.close()


def test_picker_and_job_cannot_lease_the_same_profile(tmp_path):
    service, store, _ads, _scheduler = make_service(tmp_path)
    try:
        strategy = _create_ready_strategy(store)
        token = service.list_profiles()[0]["profile_token"]
        picker = service.start_picker(token, "https://www.tiktok.com/")
        with pytest.raises(ExecutionConflictError, match="profile_already_in_use"):
            service.start_job(strategy["id"], [token])
        service.cancel_picker(picker["session_id"])
        assert service.start_job(strategy["id"], [token])["status"] == "queued"
    finally:
        service.close()


def test_job_is_visible_before_background_worker_and_results_are_redacted(tmp_path):
    store = ExecutionStore(tmp_path / "v2.db")
    scheduler = FakeScheduler(store, hold=True)
    service, store, _ads, scheduler = make_service(tmp_path, scheduler=scheduler)
    try:
        ready = store.create_element("ready", "ready", "readiness", "generic", definition())
        strategy = store.create_strategy(
            "strategy", "one", {
                "target_url": "https://www.tiktok.com/", "ready_element_id": ready["id"],
                "readiness_timeout_seconds": 5, "run_mode": "once", "loop_duration_minutes": None,
                "actions": [],
            }, True,
        )
        token = service.list_profiles()[0]["profile_token"]
        job_id = service.start_job(strategy["id"], [token], 3)["job_id"]
        visible = service.get_job(job_id)
        assert visible["status"] in {"queued", "running"}
        assert "raw-profile" not in str(visible)
        assert visible["profiles"][0]["display_id"] == "***1234"
        assert "profile_id" not in visible["profiles"][0]
        assert visible["summary"] == {
            "total": 1, "remaining": 1, "current_batch": 1, "total_batches": 1,
            "succeeded": 0, "failed": 0,
        }
        service._run(scheduler.release.set() or asyncio.sleep(0))
        service._jobs[job_id].result(timeout=2)
        results = service.get_results(job_id)
        assert "raw-profile" not in str(results)
        assert "profile_id" not in results["profiles"][0]
    finally:
        service.close()


def test_service_close_is_idempotent_and_cancels_open_picker(tmp_path):
    service, _store, ads, _scheduler = make_service(tmp_path)
    token = service.list_profiles()[0]["profile_token"]
    service.start_picker(token, "https://www.tiktok.com/")
    service.close()
    service.close()
    assert ads.stopped == ["raw-profile-1234"]


def test_stage_callback_uses_job_context_not_profile_lookup(tmp_path):
    service, store, _ads, _scheduler = make_service(tmp_path)
    try:
        service.list_profiles()  # Let startup cleanup finish before creating fixtures.
        raw = "raw-profile-1234"
        store.prepare_job("job-a", "strategy", {}, [raw], 3)
        store.prepare_job("job-b", "strategy", {}, [raw], 3)

        async def persist_stage():
            token = service._running_job_id.set("job-a")
            try:
                await service._persist_executor_stage(raw, ProfileStatus.NAVIGATING, Stage.NAVIGATE)
            finally:
                service._running_job_id.reset(token)

        service._run(persist_stage())
        first = store.list_profile_results("job-a")[0]
        second = store.list_profile_results("job-b")[0]
        assert (first["status"], first["stage"]) == ("navigating", "navigate")
        assert (second["status"], second["stage"]) == ("queued", "")
    finally:
        service.close()


class DryRunResolver:
    def __init__(self, *, error=None):
        self.calls = []
        self.error = error

    async def resolve(self, page, value, *, require_editable=False):
        self.calls.append((page, value, require_editable))
        if self.error:
            raise self.error
        return type("Resolved", (), {"diagnostics": ({"code": "valid"},)})()


def test_validate_element_dry_runs_browser_and_always_confirms_close(tmp_path):
    resolver = DryRunResolver()
    service, _store, ads, _scheduler = make_service(tmp_path, resolver=resolver)
    try:
        element = service.create_element("input", "action", "input", definition(), id="element-input")
        token = service.list_profiles()[0]["profile_token"]
        result = service.validate_element(element["id"], token)
        assert result == {
            "valid": True,
            "diagnostics": [{"code": "valid"}],
            "cleanup": {"close_confirmed": True, "attempts": 1},
        }
        assert resolver.calls[0][2] is True
        assert ads.started == ["raw-profile-1234"]
        assert ads.stopped == ["raw-profile-1234"]
    finally:
        service.close()


def test_blueprint_service_facade_methods_accept_documented_keywords(tmp_path):
    resolver = DryRunResolver()
    service, _store, _ads, _scheduler = make_service(tmp_path, resolver=resolver)
    try:
        element = service.create_element(
            id="element-1", name="ready", purpose="readiness", kind="generic", definition=definition(), status="active"
        )
        renamed = service.update_element(element["id"], expected_revision=1, name="ready 2")
        assert renamed["name"] == "ready 2"
        with pytest.raises(ValueError):
            service.update_element(element["id"], expected_revision=1, name="x", status="disabled")
        strategy_definition = {
            "target_url": "https://www.tiktok.com/", "ready_element_id": element["id"],
            "readiness_timeout_seconds": 5, "run_mode": "once", "loop_duration_minutes": None, "actions": [],
        }
        strategy = service.create_strategy(
            id="strategy-1", name="strategy", enabled=True,
            definition=strategy_definition,
        )
        assert service.update_strategy(
            strategy["id"], expected_revision=1, name="strategy 2", enabled=True,
            definition=strategy_definition,
        )["name"] == "strategy 2"
        assert service.history(limit=10, offset=0) == service.list_history(limit=10, offset=0)
        token = service.list_profiles()[0]["profile_token"]
        assert service.start_picker(profile_token=token, target_url="https://www.tiktok.com/")["status"] == "waiting_for_selection"
        service.cancel_picker("picker-1")
        assert service.validate_element(element["id"], profile_token=token)["valid"] is True
        assert service.cancel_job.__name__ == "cancel_job"
    finally:
        service.close()


def test_public_action_results_flatten_safe_failure_evidence_without_nested_secrets(tmp_path):
    service, store, _ads, _scheduler = make_service(tmp_path)
    try:
        service.list_profiles()
        raw = "raw-profile-1234"
        store.prepare_job("job-evidence", "strategy", {}, [raw], 3)
        store.append_action_result(
            "job-evidence", raw, 0, "capture_evidence", "succeeded", Stage.CAPTURE_EVIDENCE,
            {"evidence_path": "evidence/failure.png", "fixed_text": "do-not-leak", "cookie": "secret"},
        )
        store.append_action_result(
            "job-evidence", raw, 1, "click", "failed", Stage.EXECUTE_ACTION,
            {"error_code": "locator_not_found", "fixed_text": "also-secret", "duration_seconds": 1.2},
        )
        store.append_action_result(
            "job-evidence", raw, 2, "scroll", "succeeded", Stage.EXECUTE_ACTION,
            {
                "requested_switches": 2,
                "completed_switches": 2,
                "wheel_events": 5,
                "switches": [{"from": "private-a", "to": "private-b"}],
            },
        )
        records = [
            service.get_job("job-evidence")["actions"],
            service.history(limit=10, offset=0)[0]["actions"],
            service.get_results("job-evidence")["actions"],
        ]
        for actions in records:
            evidence, failed, scroll = actions
            assert evidence["evidence_path"] == "evidence/failure.png"
            assert failed["error_code"] == "locator_not_found"
            assert failed["duration_seconds"] == 1.2
            assert scroll["requested_switches"] == 2
            assert scroll["completed_switches"] == 2
            assert scroll["wheel_events"] == 5
            assert "switches" not in scroll
            assert "result" not in evidence and "result" not in failed
            assert "do-not-leak" not in str(actions)
            assert "secret" not in str(actions)
            assert "raw-profile" not in str(actions)
            assert "private-a" not in str(actions)
            assert "private-b" not in str(actions)
    finally:
        service.close()


def test_default_factory_uses_controller_profile_provider_without_loading_playwright(tmp_path):
    class Controller:
        def __init__(self):
            self.thread_name = ""

        def list_profiles(self):
            self.thread_name = threading.current_thread().name
            return [{"id": "raw-profile-1234", "name": "local"}]

    controller = Controller()
    service = create_default_execution_v2_service(
        db_path=tmp_path / "v2.db", evidence_dir=tmp_path / "evidence", controller=controller
    )
    try:
        profiles = service.list_profiles()
        assert profiles[0]["display_id"] == "***1234"
        assert "raw-profile" not in str(profiles)
        assert controller.thread_name.startswith("asyncio_")
    finally:
        service.close()
