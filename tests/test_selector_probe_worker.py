from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from selector_probe import worker
from selector_probe.healing_runtime import HealingRuntime
from selector_probe.store import SelectorProbeStore, _validated_bundle


def raw_settings(*, enabled=True, observe_only=True):
    return {
        "selector_probe": {
            "enabled": enabled,
            "site": "tiktok",
            "environment": "production",
            "timezone": "Asia/Shanghai",
            "daily_time": "03:00",
            "target_url": "https://www.tiktok.com/",
            "test_profile_ids": ["profile-a", "profile-b"],
            "model_id": "",
            "observe_only": observe_only,
            "webhook": {
                "enabled": False,
                "type": "generic",
                "url": "",
                "signing_secret": "",
            },
        },
        "browser": {
            "action_elements": {
                "comment_entry": "//button[@data-e2e='comment-icon']"
            }
        },
        "adspower": {
            "base_url": "http://local.adspower.net:50325",
            "api_key": "adspower-secret",
        },
    }


def _queued_element_request(path, request_id="worker-request"):
    with SelectorProbeStore(path) as store:
        store.create_managed_element_draft(
            element_id="worker-element",
            display_name="Worker element",
            contract={
                "intent": "inspect the worker element",
                "required_state": "feed_ready",
                "scope": "active_video",
                "accepted_roles": ["button"],
                "accepted_names": {
                    "mode": "exact",
                    "values": ["Worker"],
                },
                "preferred_attributes": ["data-e2e"],
                "postcondition": "",
                "probe_action": "inspect_only",
            },
            scope="active_video",
            actor_user_id=11,
            actor_username="admin",
        )
        store.reserve_element_request(
            element_id="worker-element",
            request_type="validate",
            request_id=request_id,
            expected_revision=1,
            actor_user_id=11,
            actor_username="admin",
        )


def _stage_element_publication(path, request_id):
    _queued_element_request(path, request_id=request_id)
    candidate, _ = _validated_bundle(
        {
            "elements": {
                "worker-element": {
                    "scope": "active_video",
                    "locators": [
                        {
                            "id": "staged-worker-selector",
                            "type": "attribute",
                            "name": "data-e2e",
                            "value": "worker",
                            "enabled": True,
                        }
                    ],
                }
            }
        }
    )
    validations = []
    for round_number in (1, 2):
        for profile_number in (1, 2):
            marker = f"{profile_number}:{round_number}"
            validations.append(
                {
                    "profile_mask": f"***P{profile_number:03d}",
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:"
                    + hashlib.sha256(f"reset:{marker}".encode()).hexdigest(),
                    "snapshot_hash": "sha256:"
                    + hashlib.sha256(
                        f"snapshot:{marker}".encode()
                    ).hexdigest(),
                    "page_generation": "sha256:"
                    + hashlib.sha256(
                        f"generation:{marker}".encode()
                    ).hexdigest(),
                    "aliases": {
                        "worker-element": {
                            "status": "ok",
                            "candidate_id": "staged-worker-selector",
                        }
                    },
                }
            )
    evidence = {
        "status": "passed",
        "bundle_hash": candidate["bundle_hash"],
        "profiles_passed": 2,
        "rounds_passed": 2,
        "validations": validations,
    }
    with SelectorProbeStore(path) as store:
        claim = store.claim_element_request(claim_token="stage-worker")
        version_id = store.store_validated_version(
            bundle=candidate,
            evidence=evidence,
            base_version_id="",
            model_id="test-model",
            prompt_version="test-prompt",
            element_request_id=claim["request_id"],
            element_request_claim_token=claim["claim_token"],
            element_request_generation=claim["claim_generation"],
            staged_result={
                "candidate": candidate,
                "validation_evidence": evidence,
                "repairs": [],
            },
        )
    return claim, version_id


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.closed = False
        self.started = []
        self.finished = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True

    def last_completed_slot(self):
        return None

    def start_run(self, **payload):
        self.started.append(payload)
        return 1

    def finish_run(self, run_id, **payload):
        self.finished.append((run_id, payload))


class RedisClient:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class RuntimeContext:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_tick_loads_fresh_settings_and_wires_saved_elements(tmp_path):
    settings = raw_settings()
    stores = []
    redis_client = RedisClient()
    captured = {}

    def store_factory(path):
        stores.append(Store(path))
        return stores[-1]

    def run_probe(**kwargs):
        captured.update(kwargs)
        return {"status": "completed"}

    result = worker.run_tick(
        settings_loader=lambda: settings,
        store_factory=store_factory,
        redis_factory=lambda _url: redis_client,
        adspower_factory=lambda **kwargs: ("adspower", kwargs),
        probe_runner=run_probe,
        db_path=tmp_path / "probe.db",
        redis_url="redis://test/9",
        clock=object(),
    )

    assert result == {"status": "completed"}
    assert captured["config"].test_profile_ids == ("profile-a", "profile-b")
    assert captured["elements"] == {
        "comment_entry": {
            "scope": "page",
            "locators": [
                {
                    "id": captured["elements"]["comment_entry"][
                        "locators"
                    ][0]["id"],
                    "type": "xpath",
                    "value": "//button[@data-e2e='comment-icon']",
                    "enabled": True,
                    "fallback": True,
                }
            ],
        }
    }
    assert captured["store"] is stores[0]
    assert captured["redis_client"] is redis_client
    assert captured["adspower_client"] == (
        "adspower",
        {
            "base_url": "http://local.adspower.net:50325",
            "api_key": "adspower-secret",
            "timeout": 5.0,
            "max_retries": 1,
        },
    )
    assert stores[0].closed is True
    assert redis_client.closed is True


class StopEvent:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        self.stopped = True
        return True


def test_serve_checks_stop_signal_in_half_second_increments():
    event = StopEvent()
    loads = []

    def tick_runner(*, settings_loader, stop_event):
        assert stop_event is event
        loads.append(settings_loader())
        return {"status": "disabled"}

    status = worker.serve(
        settings_loader=lambda: {"generation": len(loads) + 1},
        tick_runner=tick_runner,
        stop_event=event,
        interval_seconds=30,
        check_seconds=0.5,
    )

    assert status == 0
    assert loads == [{"generation": 1}]
    assert event.waits == [0.5]


def test_stop_file_prevents_tick(tmp_path):
    stop_file = tmp_path / "stop"
    stop_file.write_text("", encoding="utf-8")
    calls = []

    result = worker.serve(
        settings_loader=lambda: raw_settings(),
        tick_runner=lambda **_kwargs: calls.append("tick"),
        stop_file=stop_file,
    )

    assert result == 0
    assert calls == []


def test_stop_file_interrupts_an_active_tick(tmp_path):
    stop_file = tmp_path / "stop"
    observed = []

    def tick_runner(*, settings_loader, stop_event):
        settings_loader()
        deadline = time.monotonic() + 1
        while not stop_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        observed.append(stop_event.is_set())

    timer = threading.Timer(
        0.02,
        lambda: stop_file.write_text("", encoding="utf-8"),
    )
    timer.start()
    started = time.monotonic()
    try:
        result = worker.serve(
            settings_loader=lambda: raw_settings(),
            tick_runner=tick_runner,
            stop_file=stop_file,
            check_seconds=0.05,
        )
    finally:
        timer.cancel()

    assert result == 0
    assert observed == [True]
    assert time.monotonic() - started < 0.5


def test_serve_treats_active_probe_cancellation_as_clean_stop():
    stop_event = threading.Event()
    logger = CapturingLogger()

    def tick_runner(*, settings_loader, stop_event):
        settings_loader()
        stop_event.set()
        raise asyncio.CancelledError()

    result = worker.serve(
        settings_loader=lambda: raw_settings(),
        tick_runner=tick_runner,
        stop_event=stop_event,
        logger=logger,
    )

    assert result == 0
    assert logger.messages == []


class CapturingLogger:
    def __init__(self):
        self.messages = []

    def error(self, message, *args):
        self.messages.append(message % args)


def test_serve_logs_only_safe_code_and_continues_until_stop():
    event = StopEvent()
    logger = CapturingLogger()

    def failing_tick(**_kwargs):
        raise RuntimeError(
            "profile-a ws://secret api-key=adspower-secret"
        )

    result = worker.serve(
        settings_loader=lambda: raw_settings(),
        tick_runner=failing_tick,
        stop_event=event,
        logger=logger,
    )

    assert result == 0
    assert logger.messages == [
        "selector_probe_tick_failed code=probe_unavailable"
    ]
    assert "profile-a" not in str(logger.messages)
    assert "ws://" not in str(logger.messages)
    assert "adspower-secret" not in str(logger.messages)


def test_main_dispatches_tick_and_serve_without_exposing_exceptions():
    calls = []
    logger = CapturingLogger()

    assert (
        worker.main(
            ["tick"],
            tick_runner=lambda: calls.append("tick") or {"status": "disabled"},
            serve_runner=lambda: calls.append("serve") or 0,
            logger=logger,
        )
        == 0
    )
    assert (
        worker.main(
            ["serve"],
            tick_runner=lambda: calls.append("unexpected"),
            serve_runner=lambda: calls.append("serve") or 0,
            logger=logger,
        )
        == 0
    )
    assert calls == ["tick", "serve"]


def test_main_tick_failure_returns_one_with_safe_log():
    logger = CapturingLogger()

    def fail():
        raise RuntimeError("profile-b wss://private")

    assert (
        worker.main(
            ["tick"],
            tick_runner=fail,
            serve_runner=lambda: 0,
            logger=logger,
        )
        == 1
    )
    assert logger.messages == [
        "selector_probe_tick_failed code=probe_unavailable"
    ]


def test_invalid_saved_elements_fail_closed_before_opening_dependencies(
    tmp_path,
):
    settings = raw_settings()
    settings["browser"]["action_elements"] = {
        "comment_entry": {
            "scope": "page",
            "locators": [],
        }
    }
    calls = []

    with pytest.raises(ValueError, match="non-empty"):
        worker.run_tick(
            settings_loader=lambda: settings,
            store_factory=lambda _path: calls.append("store"),
            redis_factory=lambda _url: calls.append("redis"),
            db_path=tmp_path / "probe.db",
        )

    assert calls == []


def test_tick_passes_stop_token_to_active_probe(tmp_path):
    stop_event = StopEvent()
    captured = {}

    worker.run_tick(
        settings_loader=lambda: raw_settings(),
        store_factory=Store,
        redis_factory=lambda _url: RedisClient(),
        adspower_factory=lambda **_kwargs: object(),
        probe_runner=lambda **kwargs: captured.update(kwargs)
        or {"status": "disabled"},
        db_path=tmp_path / "probe.db",
        stop_event=stop_event,
    )

    assert captured["stop_event"] is stop_event


def test_force_is_forwarded_to_observe_only_probe(tmp_path):
    captured = {}

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=True),
        store_factory=Store,
        redis_factory=lambda _url: RedisClient(),
        adspower_factory=lambda **_kwargs: object(),
        probe_runner=lambda **kwargs: (
            captured.update(kwargs) or {"status": "completed"}
        ),
        db_path=tmp_path / "probe.db",
        force=True,
    )

    assert result == {"status": "completed"}
    assert captured["force"] is True


def test_tick_observe_only_never_reconciles_or_constructs_registry(tmp_path):
    events = []

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(),
        store_factory=Store,
        redis_factory=lambda _url: RedisClient(),
        registry_factory=lambda *_args, **_kwargs: events.append("registry"),
        reconcile_runner=lambda *_args: events.append("reconcile"),
        adspower_factory=lambda **_kwargs: object(),
        probe_runner=lambda **_kwargs: (
            events.append("observe") or {"status": "completed"}
        ),
        db_path=tmp_path / "probe.db",
    )

    assert result == {"status": "completed"}
    assert events == ["observe"]


@pytest.mark.parametrize(
    ("enabled", "observe_only", "error_code", "status"),
    [
        (True, True, "rollout_disabled", "rollout_disabled"),
        (False, False, "probe_disabled", "disabled"),
    ],
)
def test_tick_aborts_crash_staged_publication_without_redis(
    tmp_path,
    enabled,
    observe_only,
    error_code,
    status,
):
    path = tmp_path / f"{error_code}.db"
    claim, version_id = _stage_element_publication(
        path,
        f"{error_code}-request",
    )
    forbidden = []

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(
            enabled=enabled,
            observe_only=observe_only,
        ),
        store_factory=SelectorProbeStore,
        redis_factory=lambda _url: forbidden.append("redis"),
        registry_factory=lambda *_args, **_kwargs: forbidden.append(
            "registry"
        ),
        probe_runner=lambda **_kwargs: forbidden.append("probe"),
        db_path=path,
        evidence_root=tmp_path / "evidence",
    )

    assert result["status"] == status
    assert forbidden == []
    with SelectorProbeStore(path) as store:
        request = store.get_element_request(claim["request_id"])
        element = store.get_managed_element_row(claim["element_id"])
        outbox = store.connection.execute(
            "SELECT status FROM publication_outbox"
        ).fetchone()
        version = store.get_version(version_id)
        assert request["status"] == "failed"
        assert request["error_code"] == error_code
        assert element["draft_status"] == "draft"
        assert outbox["status"] == "cancelled"
        assert version["status"] == "cancelled"
        store.delete_managed_element(
            element_id=claim["element_id"],
            expected_revision=int(element["revision"]),
            actor_user_id=11,
            actor_username="admin",
        )


def test_observe_abort_waits_for_active_outbox_claim_then_expires(tmp_path):
    path = tmp_path / "rollout-race.db"
    claim, _version_id = _stage_element_publication(
        path,
        "rollout-race-request",
    )
    with SelectorProbeStore(path) as store:
        event = store.claim_outbox_event(
            claim_token="active-reconciler",
            lease_seconds=60,
        )
        assert event is not None
    zero_redis = lambda _url: pytest.fail("Redis must not be constructed")
    first_now = datetime.now(UTC)

    waiting = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=True),
        store_factory=SelectorProbeStore,
        redis_factory=zero_redis,
        db_path=path,
        clock=SimpleNamespace(now=lambda: first_now),
    )
    assert waiting["status"] == "rollout_disabled"
    assert waiting["aborted"] == 0
    assert waiting["inflight"] == 1
    with SelectorProbeStore(path) as store:
        assert store.get_element_request(claim["request_id"])["status"] == (
            "publishing"
        )

    reads = []

    class ReadOnlyRegistry:
        def get_active(self):
            reads.append("get_active")
            return None

        def publish(self, _event):
            pytest.fail("rollout resolution must not publish")

    expired = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=True),
        store_factory=SelectorProbeStore,
        redis_factory=lambda _url: RedisClient(),
        registry_factory=lambda *_args, **_kwargs: ReadOnlyRegistry(),
        db_path=path,
        clock=SimpleNamespace(
            now=lambda: first_now + timedelta(seconds=120)
        ),
    )
    assert expired["status"] == "rollout_disabled"
    assert expired["cancelled"] == 1
    assert expired["inflight"] == 0
    assert reads == ["get_active"]
    with SelectorProbeStore(path) as store:
        assert store.get_element_request(claim["request_id"])["status"] == (
            "failed"
        )


@pytest.mark.parametrize(
    ("enabled", "observe_only", "error_code"),
    [
        (True, True, "rollout_disabled"),
        (False, False, "probe_disabled"),
    ],
)
def test_rollout_read_only_resolution_acks_redis_success_after_db_crash(
    tmp_path,
    enabled,
    observe_only,
    error_code,
):
    path = tmp_path / f"redis-success-{error_code}.db"
    claim, version_id = _stage_element_publication(
        path,
        f"redis-success-{error_code}",
    )
    with SelectorProbeStore(path) as store:
        event = store.claim_outbox_event(
            claim_token="crashed-reconciler",
            lease_seconds=1,
        )
        version = store.get_version(version_id)
    calls = []

    class ReadOnlyRegistry:
        def get_active(self):
            calls.append("get_active")
            return {
                "version": version_id,
                "bundle_hash": version["bundle_hash"],
            }

        def publish(self, _event):
            pytest.fail("rollout resolution must not publish")

    future = datetime.now(UTC) + timedelta(seconds=5)
    result = worker.run_tick(
        settings_loader=lambda: raw_settings(
            enabled=enabled,
            observe_only=observe_only,
        ),
        store_factory=SelectorProbeStore,
        redis_factory=lambda _url: RedisClient(),
        registry_factory=lambda *_args, **_kwargs: ReadOnlyRegistry(),
        db_path=path,
        evidence_root=tmp_path / "evidence",
        clock=SimpleNamespace(now=lambda: future),
    )

    assert event is not None
    assert calls == ["get_active"]
    assert result["completed"] == 1
    with SelectorProbeStore(path) as store:
        request = store.get_element_request(claim["request_id"])
        outbox = store.connection.execute(
            "SELECT status FROM publication_outbox"
        ).fetchone()
        assert request["status"] == "completed"
        assert request["result"]["new_version"] == version_id
        assert store.get_version(version_id)["status"] == "published"
        assert outbox["status"] == "completed"


def test_tick_routes_due_non_observe_config_to_healing(tmp_path):
    events = []

    class Runtime:
        def __enter__(self):
            events.append("runtime:enter")
            return self

        def __exit__(self, *_args):
            events.append("runtime:close")

    class Lease:
        def acquire(self):
            events.append("lease:acquire")
            return True

        def renew(self):
            return True

        def release(self):
            events.append("lease:release")
            return True

    runtime = Runtime()
    clock = type(
        "Clock",
        (),
        {
            "now": lambda _self: datetime(
                2026,
                7,
                29,
                3,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        },
    )()

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=False),
        store_factory=Store,
        redis_factory=lambda _url: RedisClient(),
        registry_factory=lambda _redis, **_kwargs: (
            events.append("registry")
            or SimpleNamespace(get_active=lambda: None)
        ),
        reconcile_runner=lambda *_args: (
            events.append("reconcile") or {"acknowledged": 0}
        ),
        adspower_factory=lambda **_kwargs: object(),
        probe_runner=lambda **_kwargs: events.append("observe"),
        healing_runtime_factory=lambda **_kwargs: (
            events.append("runtime") or runtime
        ),
        lease_factory=lambda *_args, **_kwargs: Lease(),
        healing_runner=lambda selected: (
            events.append("healing")
            or {
                "status": "healthy",
                "runtime_matches": selected is runtime,
            }
        ),
        db_path=tmp_path / "probe.db",
        clock=clock,
    )

    assert result == {
        "status": "healthy",
        "runtime_matches": True,
    }
    assert events == [
        "lease:acquire",
        "registry",
        "reconcile",
        "runtime",
        "runtime:enter",
        "healing",
        "runtime:close",
        "lease:release",
    ]


def test_force_healing_skips_due_check_but_keeps_probe_lease(tmp_path):
    events = []

    class Runtime:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            events.append("runtime:close")

    class Lease:
        def acquire(self):
            events.append("lease:acquire")
            return True

        def renew(self):
            return True

        def release(self):
            events.append("lease:release")
            return True

    clock = type(
        "Clock",
        (),
        {
            "now": lambda _self: datetime(
                2026,
                7,
                29,
                1,
                0,
                tzinfo=ZoneInfo("Asia/Shanghai"),
            )
        },
    )()

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=False),
        store_factory=Store,
        redis_factory=lambda _url: RedisClient(),
        registry_factory=lambda *_args, **_kwargs: SimpleNamespace(
            get_active=lambda: None
        ),
        reconcile_runner=lambda *_args: events.append("reconcile"),
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: Runtime(),
        healing_runner=lambda _runtime: (
            events.append("healing") or {"status": "healthy"}
        ),
        lease_factory=lambda *_args, **_kwargs: Lease(),
        db_path=tmp_path / "probe.db",
        clock=clock,
        force=True,
    )

    assert result == {"status": "healthy"}
    assert events == [
        "lease:acquire",
        "reconcile",
        "healing",
        "runtime:close",
        "lease:release",
    ]


def test_selector_failure_is_not_retried_until_next_daily_slot(tmp_path):
    calls = []
    clock = SimpleNamespace(
        now=lambda: datetime(
            2026, 7, 29, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")
        )
    )

    def run_once():
        return worker.run_tick(
            settings_loader=lambda: raw_settings(observe_only=False),
            store_factory=SelectorProbeStore,
            redis_factory=lambda _url: RedisClient(),
            registry_factory=lambda *_args, **_kwargs: SimpleNamespace(
                get_active=lambda: None
            ),
            reconcile_runner=lambda *_args: {},
            adspower_factory=lambda **_kwargs: object(),
            healing_runtime_factory=lambda **_kwargs: RuntimeContext(),
            healing_runner=lambda _runtime: (
                calls.append("healing")
                or {
                    "status": "selector_validation_failed",
                    "proposed_pause_aliases": ["comment_entry"],
                    "failure_code": "multiple_match",
                    "match_count": 2,
                    "required_state": "feed_ready",
                }
            ),
            lease_factory=lambda *_args, **_kwargs: SimpleNamespace(
                acquire=lambda: True,
                renew=lambda: True,
                release=lambda: True,
            ),
            db_path=tmp_path / "probe.db",
            clock=clock,
        )

    first = run_once()
    second = run_once()

    assert first["status"] == "selector_validation_failed"
    assert second["status"] == "not_due"
    assert calls == ["healing"]


def test_infrastructure_failure_waits_for_15_minute_retry(tmp_path):
    calls = []
    current = [
        datetime(2026, 7, 29, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    ]
    clock = SimpleNamespace(now=lambda: current[0])

    def run_once():
        return worker.run_tick(
            settings_loader=lambda: raw_settings(observe_only=False),
            store_factory=SelectorProbeStore,
            redis_factory=lambda _url: RedisClient(),
            registry_factory=lambda *_args, **_kwargs: SimpleNamespace(
                get_active=lambda: None
            ),
            reconcile_runner=lambda *_args: {},
            adspower_factory=lambda **_kwargs: object(),
            healing_runtime_factory=lambda **_kwargs: RuntimeContext(),
            healing_runner=lambda _runtime: (
                calls.append("healing")
                or {"status": "infrastructure_unavailable"}
            ),
            lease_factory=lambda *_args, **_kwargs: SimpleNamespace(
                acquire=lambda: True,
                renew=lambda: True,
                release=lambda: True,
            ),
            db_path=tmp_path / "probe.db",
            clock=clock,
        )

    assert run_once()["status"] == "infrastructure_unavailable"
    current[0] = current[0].replace(minute=10)
    assert run_once()["status"] == "retry_wait"
    current[0] = current[0].replace(minute=15)
    assert run_once()["status"] == "infrastructure_unavailable"
    assert calls == ["healing", "healing"]


def test_healthy_two_by_two_evidence_is_persisted_for_audit(tmp_path):
    validations = [
        {
            "profile_mask": f"***p00{profile}",
            "round_number": round_number,
            "reset_evidence_hash": f"reset-{profile}-{round_number}",
            "snapshot_hash": f"snapshot-{profile}-{round_number}",
            "page_generation": f"generation-{profile}-{round_number}",
            "aliases": {"comment_entry": {"status": "ok"}},
        }
        for round_number in (1, 2)
        for profile in (0, 1)
    ]
    path = tmp_path / "probe.db"

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=False),
        store_factory=SelectorProbeStore,
        redis_factory=lambda _url: RedisClient(),
        registry_factory=lambda *_args, **_kwargs: SimpleNamespace(
            get_active=lambda: None
        ),
        reconcile_runner=lambda *_args: {},
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: RuntimeContext(),
        healing_runner=lambda _runtime: {
            "status": "healthy",
            "validation_evidence": {
                "status": "passed",
                "bundle_hash": "sha256:" + "a" * 64,
                "profiles_passed": 2,
                "rounds_passed": 2,
                "validations": validations,
            },
        },
        lease_factory=lambda *_args, **_kwargs: SimpleNamespace(
            acquire=lambda: True,
            renew=lambda: True,
            release=lambda: True,
        ),
        db_path=path,
        clock=SimpleNamespace(
            now=lambda: datetime(
                2026, 7, 29, 3, 0, tzinfo=ZoneInfo("Asia/Shanghai")
            )
        ),
    )
    with SelectorProbeStore(path) as store:
        rows = store.connection.execute(
            """
            SELECT profile_mask, round_number, evidence_json
            FROM selector_validation_runs
            ORDER BY profile_mask, round_number
            """
        ).fetchall()

    assert result["status"] == "healthy"
    assert len(rows) == 4
    assert {
        (row["profile_mask"], row["round_number"])
        for row in rows
    } == {
        ("***p000", 1),
        ("***p000", 2),
        ("***p001", 1),
        ("***p001", 2),
    }


def test_maintenance_failures_never_block_probe_run(tmp_path):
    settings = raw_settings()
    settings["selector_probe"]["webhook"] = {
        "enabled": True,
        "type": "generic",
        "url": "https://hooks.example.test/probe",
        "signing_secret": "s" * 32,
    }
    events = []

    class Alerts:
        def cleanup_screenshots(self, **_kwargs):
            events.append("cleanup")
            raise RuntimeError("cleanup failed")

    class Dispatcher:
        def deliver_due(self, _now):
            events.append("webhook")
            raise RuntimeError("delivery failed")

    result = worker.run_tick(
        settings_loader=lambda: settings,
        store_factory=Store,
        redis_factory=lambda _url: RedisClient(),
        adspower_factory=lambda **_kwargs: object(),
        probe_runner=lambda **_kwargs: (
            events.append("probe") or {"status": "completed"}
        ),
        alert_service_factory=lambda *_args, **_kwargs: Alerts(),
        webhook_dispatcher_factory=lambda **_kwargs: Dispatcher(),
        db_path=tmp_path / "probe.db",
    )

    assert result == {"status": "completed"}
    assert events == ["cleanup", "webhook", "probe"]


def test_disabled_tick_runs_cleanup_without_redis_or_probe_dependencies(
    monkeypatch,
    tmp_path,
):
    monkeypatch.delenv("SELECTOR_PROBE_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    captured = {}

    class Alerts:
        def cleanup_screenshots(self, **_kwargs):
            captured["cleanup"] = True

    def store_factory(path):
        captured["path"] = Path(path)
        return Store(path)

    worker.run_tick(
        settings_loader=lambda: raw_settings(enabled=False),
        store_factory=store_factory,
        redis_factory=lambda _url: captured.update(redis=True),
        adspower_factory=lambda **_kwargs: captured.update(adspower=True),
        probe_runner=lambda **_kwargs: captured.update(probe=True),
        alert_service_factory=lambda *_args, **_kwargs: Alerts(),
        db_path=tmp_path / "selector-probe.db",
    )

    assert captured == {
        "path": tmp_path / "selector-probe.db",
        "cleanup": True,
    }


def test_redis_client_has_short_connect_and_socket_timeouts(monkeypatch):
    import redis

    captured = {}

    def from_url(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return object()

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(from_url))

    assert worker._redis_client("redis://test/1") is not None
    assert captured == {
        "url": "redis://test/1",
        "socket_connect_timeout": 3.0,
        "socket_timeout": 5.0,
    }


def test_element_request_worker_completes_once_and_writes_terminal_audit(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path)
    executed = []
    clock = SimpleNamespace(
        now=lambda: datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
    )

    result = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda request: (
            executed.append(request)
            or {
                "status": "published",
                "published": True,
                "reconciled": True,
                "new_version": "sel-worker",
                "candidate": {
                    "elements": {
                        "worker-element": {
                            "scope": "active_video",
                            "locators": [
                                {
                                    "id": "worker-primary",
                                    "type": "attribute",
                                    "name": "data-e2e",
                                    "value": "worker",
                                    "enabled": True,
                                }
                            ],
                        }
                    }
                },
                "rounds": [
                    {
                        "profile_mask": "***A123",
                        "round_number": 1,
                        "status": "passed",
                        "raw_dom": "secret",
                    }
                ],
                "repairs": [
                    {
                        "attempt": 1,
                        "failure_code": "zero_match",
                        "new_method": "attribute",
                        "prompt": "secret",
                    }
                ],
                "raw_snapshot": {"secret": True},
            }
        ),
        db_path=path,
        clock=clock,
    )

    assert result == {
        "claimed": 1,
        "completed": 1,
        "failed": 0,
        "retried": 0,
    }
    assert executed[0]["element_id"] == "worker-element"
    assert executed[0]["expected_revision"] == 2
    with SelectorProbeStore(path) as store:
        request = store.get_element_request("worker-request")
        events = store.connection.execute(
            """
            SELECT event_type
            FROM selector_management_audit_events
            WHERE event_type = 'element_validate_completed'
            """
        ).fetchall()
        assert request["status"] == "completed"
        assert request["result"]["new_version"] == "sel-worker"
        assert "raw_snapshot" not in request["result"]
        assert "raw_dom" not in request["result"]["rounds"][0]
        element = store.get_managed_element_row("worker-element")
        draft = store.managed_element_draft_row("worker-element")
        assert element["published_status"] == "healthy"
        assert element["draft_status"] is None
        assert json.loads(draft["candidates_json"])[0]["value"] == "worker"
        assert len(events) == 1
    second = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda _request: pytest.fail("completed request reran"),
        db_path=path,
        clock=clock,
    )
    assert second["claimed"] == 0


def test_element_request_worker_rechecks_revision_before_execution(tmp_path):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="stale-request")
    with SelectorProbeStore(path) as store:
        draft = store.managed_element_draft_row("worker-element")
        contract = json.loads(draft["contract_json"])
        with store.connection:
            store.connection.execute(
                """
                UPDATE managed_elements
                SET revision = 3
                WHERE id = 'worker-element'
                """
            )
            store.connection.execute(
                """
                UPDATE element_drafts
                SET contract_json = ?
                WHERE element_id = 'worker-element'
                """,
                (
                    json.dumps(
                        {**contract, "intent": "changed intent"},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )

    result = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda _request: pytest.fail("stale request executed"),
        db_path=path,
        clock=SimpleNamespace(
            now=lambda: datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
        ),
    )

    assert result["failed"] == 1
    with SelectorProbeStore(path) as store:
        request = store.get_element_request("stale-request")
        assert request["status"] == "failed"
        assert request["error_code"] == "stale_revision"


def test_element_request_completion_fences_admin_draft_update_during_run(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="midflight-stale-request")
    changed_intent = "administrator changed this while validation ran"

    def executor(_request):
        with SelectorProbeStore(path) as admin_store:
            draft = admin_store.managed_element_draft_row("worker-element")
            contract = json.loads(draft["contract_json"])
            with admin_store.connection:
                admin_store.connection.execute(
                    """
                    UPDATE managed_elements
                    SET revision = 3, draft_status = 'draft'
                    WHERE id = 'worker-element'
                    """
                )
                admin_store.connection.execute(
                    """
                    UPDATE element_drafts
                    SET contract_json = ?
                    WHERE element_id = 'worker-element'
                    """,
                    (
                        json.dumps(
                            {**contract, "intent": changed_intent},
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
        return {
            "status": "published",
            "published": True,
            "reconciled": True,
            "new_version": "sel-must-not-activate",
            "candidate": {
                "elements": {
                    "worker-element": {
                        "scope": "active_video",
                        "locators": [
                            {
                                "id": "stale-candidate",
                                "type": "attribute",
                                "name": "data-e2e",
                                "value": "stale",
                                "enabled": True,
                            }
                        ],
                    }
                }
            },
        }

    result = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=executor,
        db_path=path,
        clock=SimpleNamespace(
            now=lambda: datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
        ),
    )

    assert result == {
        "claimed": 1,
        "completed": 0,
        "failed": 1,
        "retried": 0,
    }
    with SelectorProbeStore(path) as store:
        request = store.get_element_request("midflight-stale-request")
        element = store.get_managed_element_row("worker-element")
        draft = store.managed_element_draft_row("worker-element")
        audits = store.connection.execute(
            """
            SELECT event_type, details_json
            FROM selector_management_audit_events
            WHERE target_id = 'worker-element'
            ORDER BY id
            """
        ).fetchall()

        assert request["status"] == "failed"
        assert request["error_code"] == "stale_revision"
        assert element["revision"] == 3
        assert element["draft_status"] == "draft"
        assert element["published_status"] == "probe_unavailable"
        assert element["active_version_id"] == ""
        assert json.loads(draft["contract_json"])["intent"] == changed_intent
        assert json.loads(draft["candidates_json"]) == []
        assert [row["event_type"] for row in audits].count(
            "element_validate_completed"
        ) == 0
        assert [row["event_type"] for row in audits].count(
            "element_validate_failed"
        ) == 1


def test_element_request_worker_does_not_publish_healthy_only_result(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="healthy-only-request")
    result = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda _request: {"status": "healthy"},
        db_path=path,
        clock=SimpleNamespace(
            now=lambda: datetime(2099, 7, 29, 4, 0, tzinfo=UTC)
        ),
    )

    assert result["failed"] == 1
    with SelectorProbeStore(path) as store:
        request = store.get_element_request("healthy-only-request")
        element = store.get_managed_element_row("worker-element")
        assert request["status"] == "failed"
        assert request["error_code"] == "draft_not_published"
        assert element["published_status"] == "probe_unavailable"
        assert element["draft_status"] == "draft"


def test_observe_only_validate_is_terminal_without_runtime_or_redis(tmp_path):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="observe-only-validation")
    forbidden_calls = []

    def forbidden(name):
        def call(*_args, **_kwargs):
            forbidden_calls.append(name)
            raise AssertionError(f"{name} must not be called")

        return call

    result = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda request: worker.run_element_request_runtime(
            request,
            settings_loader=lambda: raw_settings(observe_only=True),
            store_factory=SelectorProbeStore,
            redis_factory=forbidden("redis"),
            registry_factory=forbidden("registry"),
            adspower_factory=forbidden("adspower"),
            healing_runtime_factory=forbidden("healing"),
            db_path=path,
        ),
        db_path=path,
        owner_id_factory=lambda: "observe-only-worker",
    )

    assert result == {
        "claimed": 1,
        "completed": 0,
        "failed": 1,
        "retried": 0,
    }
    assert forbidden_calls == []
    with SelectorProbeStore(path) as store:
        request = store.get_element_request("observe-only-validation")
        element = store.get_managed_element_row(request["element_id"])
        assert request["status"] == "failed"
        assert request["error_code"] == "observe_only_validation_disabled"
        assert element["draft_status"] == "draft"
        assert store.connection.execute(
            "SELECT COUNT(*) FROM selector_versions"
        ).fetchone()[0] == 0
        assert store.connection.execute(
            "SELECT COUNT(*) FROM publication_outbox"
        ).fetchone()[0] == 0


def test_element_request_worker_retries_infrastructure_failure(tmp_path):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="retry-request")
    now = datetime(2099, 7, 29, 4, 0, tzinfo=UTC)

    first = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda _request: (_ for _item in ()).throw(
            RuntimeError("temporary failure")
        ),
        db_path=path,
        clock=SimpleNamespace(now=lambda: now),
    )
    with SelectorProbeStore(path) as store:
        pending = store.get_element_request("retry-request")

    assert first["retried"] == 1
    assert pending["status"] == "pending"
    assert datetime.fromisoformat(pending["next_attempt_at"]) == (
        now + timedelta(seconds=15)
    )


def test_element_request_heartbeat_prevents_reclaim_past_initial_lease(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="long-request")
    started = threading.Event()
    release = threading.Event()
    first_result = {}

    def slow_executor(_request):
        started.set()
        assert release.wait(5)
        return {
            "status": "published",
            "published": True,
            "reconciled": True,
            "new_version": "sel-long",
        }

    def run_first():
        first_result.update(
            worker.consume_element_requests(
                store_factory=SelectorProbeStore,
                executor=slow_executor,
                db_path=path,
                max_requests=1,
                claim_lease_seconds=1,
                heartbeat_seconds=0.2,
            )
        )

    thread = threading.Thread(target=run_first)
    thread.start()
    assert started.wait(2)
    time.sleep(1.2)
    second = worker.consume_element_requests(
        store_factory=SelectorProbeStore,
        executor=lambda _request: pytest.fail("request was reclaimed"),
        db_path=path,
        max_requests=1,
        claim_lease_seconds=1,
        heartbeat_seconds=0.2,
    )
    release.set()
    thread.join(5)

    assert second["claimed"] == 0
    assert first_result["completed"] == 1


def test_element_runtime_rejects_reclaimed_sqlite_generation_before_open(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="reclaimed-runtime-request")
    now = datetime.now(UTC) + timedelta(seconds=5)
    with SelectorProbeStore(path) as store:
        old = store.claim_element_request(
            claim_token="old-generation",
            now=now,
            lease_seconds=1,
        )
        replacement = store.claim_element_request(
            claim_token="new-generation",
            now=now + timedelta(seconds=5),
            lease_seconds=120,
        )
    runtime_opened = []

    class Lease:
        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            return True

    with pytest.raises(worker.ElementRequestClaimLost):
        worker.run_element_request_runtime(
            old,
            settings_loader=lambda: raw_settings(observe_only=False),
            store_factory=SelectorProbeStore,
            redis_factory=lambda _url: RedisClient(),
            registry_factory=lambda *_args, **_kwargs: object(),
            adspower_factory=lambda **_kwargs: object(),
            healing_runtime_factory=lambda **_kwargs: runtime_opened.append(
                True
            ),
            lease_factory=lambda *_args, **_kwargs: Lease(),
            db_path=path,
        )

    assert replacement["claim_generation"] == (
        old["claim_generation"] + 1
    )
    assert runtime_opened == []
    with SelectorProbeStore(path) as store:
        assert store.connection.execute(
            "SELECT COUNT(*) FROM selector_versions"
        ).fetchone()[0] == 0


def test_element_runtime_stops_before_reconcile_when_rollout_turns_observe_only(
    tmp_path,
):
    path = tmp_path / "selector-probe.db"
    _queued_element_request(path, request_id="pre-reconcile-claim-loss")
    with SelectorProbeStore(path) as store:
        request = store.claim_element_request(
            claim_token="old-generation",
            lease_seconds=120,
        )
    element_id = request["element_id"]
    candidate, _bundle_hash = _validated_bundle(
        {
            "elements": {
                element_id: {
                    "scope": "active_video",
                    "locators": [
                        {
                            "id": "guarded-candidate",
                            "type": "attribute",
                            "name": "data-e2e",
                            "value": "guarded",
                            "enabled": True,
                        }
                    ],
                }
            }
        }
    )
    validations = []
    for round_number in (1, 2):
        for profile_number in (1, 2):
            marker = f"{profile_number}:{round_number}"
            validations.append(
                {
                    "profile_mask": f"***P{profile_number:03d}",
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:"
                    + hashlib.sha256(
                        f"reset:{marker}".encode()
                    ).hexdigest(),
                    "snapshot_hash": "sha256:"
                    + hashlib.sha256(
                        f"snapshot:{marker}".encode()
                    ).hexdigest(),
                    "page_generation": "sha256:"
                    + hashlib.sha256(
                        f"generation:{marker}".encode()
                    ).hexdigest(),
                    "aliases": {
                        element_id: {
                            "status": "ok",
                            "candidate_id": "guarded-candidate",
                        }
                    },
                }
            )
    full_evidence = {
        "status": "passed",
        "bundle_hash": candidate["bundle_hash"],
        "profiles_passed": 2,
        "rounds_passed": 2,
        "validations": validations,
    }
    reconciled = []
    rollout = {"observe_only": False}

    class Lease:
        def acquire(self):
            return True

        def renew(self):
            return True

        def release(self):
            return True

    class PublishingRuntime:
        store_and_publish = HealingRuntime.store_and_publish
        prepare_publication = HealingRuntime.prepare_publication
        _require_owned = HealingRuntime._require_owned
        _stopped = HealingRuntime._stopped
        model_call = None

        def __init__(self, **kwargs):
            self.config = kwargs["config"]
            self.store = kwargs["store"]
            self.registry = kwargs["registry"]
            self.lease_guard = kwargs["lease_guard"]
            self.stop_event = None
            self._lease_error = None
            self._active_bundle = None
            self._model_config = None
            self.probe_run_id = None
            self.attempt_token = ""
            self.element_request_id = kwargs["element_request_id"]
            self.element_request_claim_token = kwargs[
                "element_request_claim_token"
            ]
            self.element_request_generation = kwargs[
                "element_request_generation"
            ]
            self._staged_element_result = None
            self.reconciler = lambda *_args: reconciled.append(True)
            original_store = self.store.store_validated_version

            def store_then_disable_rollout(**payload):
                version = original_store(**payload)
                rollout["observe_only"] = True
                return version

            self.store.store_validated_version = store_then_disable_rollout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def validate_active(self):
            return {"status": "healthy"}

        def deterministic_candidates(self, **_kwargs):
            return candidate

        def fresh_validation_context(self, **_kwargs):
            raise AssertionError("repair must not run")

        def validate_candidate(self, _candidate):
            return {"status": "passed"}

        def repair_candidate(self, **_kwargs):
            raise AssertionError("repair must not run")

        def full_validate(self, _candidate):
            return full_evidence

    with pytest.raises(worker.ElementRequestRolloutDisabled):
        worker.run_element_request_runtime(
            request,
            settings_loader=lambda: raw_settings(
                observe_only=rollout["observe_only"]
            ),
            store_factory=SelectorProbeStore,
            redis_factory=lambda _url: RedisClient(),
            registry_factory=lambda *_args, **_kwargs: object(),
            adspower_factory=lambda **_kwargs: object(),
            healing_runtime_factory=PublishingRuntime,
            lease_factory=lambda *_args, **_kwargs: Lease(),
            db_path=path,
        )

    assert reconciled == []
    with SelectorProbeStore(path) as store:
        version = store.connection.execute(
            "SELECT status FROM selector_versions"
        ).fetchone()
        outbox = store.connection.execute(
            "SELECT status FROM publication_outbox"
        ).fetchone()
        request_row = store.get_element_request(request["request_id"])
        assert version["status"] == "validated"
        assert outbox["status"] == "pending"
        assert request_row["status"] == "publishing"
