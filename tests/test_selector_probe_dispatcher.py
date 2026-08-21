from __future__ import annotations

import threading

from selector_probe.blueprint import (
    RedisRunDispatcher,
    _dispatch_failure_code,
    default_registry_factory,
)


class SharedRedis:
    def __init__(self):
        self.values = {}
        self.set_calls = []
        self.eval_calls = []
        self.lock = threading.Lock()

    def set(self, key, value, *, nx, ex):
        with self.lock:
            self.set_calls.append((key, value, nx, ex))
            if nx and key in self.values:
                return False
            self.values[key] = value
            return True

    def eval(self, script, key_count, key, owner, *args):
        with self.lock:
            self.eval_calls.append((script, key_count, key, owner, *args))
            if self.values.get(key) != owner:
                return 0
            if "EXPIRE" in script:
                return 1
            self.values.pop(key, None)
            return 1

    def close(self):
        return None


def test_distributed_dispatcher_blocks_other_app_and_releases_owner_lock():
    redis = SharedRedis()
    started = threading.Event()
    unblock = threading.Event()
    completed = threading.Event()
    calls = []

    def tick_runner(*, force):
        calls.append(force)
        started.set()
        assert unblock.wait(1)

    first_dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=tick_runner,
        environment="production",
        site="tiktok",
        ttl_seconds=45,
    )
    second_dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=tick_runner,
        environment="production",
        site="tiktok",
        ttl_seconds=45,
    )

    first = first_dispatcher("request-a", completed.set)
    assert started.wait(0.5)
    second = second_dispatcher("request-b", lambda: None)

    assert first == {
        "status": "accepted",
        "completion_managed": True,
    }
    assert second == {"status": "busy"}
    assert calls == [True]
    assert redis.set_calls[0] == (
        "selector_registry:production:tiktok:run_now",
        "request-a",
        True,
        45,
    )

    unblock.set()
    assert completed.wait(0.5)
    assert redis.values == {}
    assert redis.eval_calls[-1][3] == "request-a"


def test_dispatcher_lock_recovers_after_redis_ttl_expiry():
    redis = SharedRedis()
    redis.values[
        "selector_registry:production:tiktok:run_now"
    ] = "crashed-owner"
    completed = threading.Event()
    calls = []
    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=lambda *, force: calls.append(force),
        environment="production",
        site="tiktok",
        ttl_seconds=30,
    )

    assert dispatcher("request-a", lambda: None) == {"status": "busy"}

    # Redis expires the stale owner after EX TTL.
    redis.values.clear()
    assert dispatcher("request-b", completed.set)["status"] == "accepted"
    assert completed.wait(0.5)
    assert calls == [True]
    assert all(call[3] == 30 for call in redis.set_calls)


def test_dispatcher_exception_still_releases_exact_owner():
    redis = SharedRedis()
    completed = threading.Event()

    def fail_tick(*, force):
        assert force is True
        raise RuntimeError("profile-a api_key=secret")

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=fail_tick,
        environment="production",
        site="tiktok",
        ttl_seconds=30,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert completed.wait(0.5)
    assert redis.values == {}
    assert redis.eval_calls[-1][3] == "request-a"


def test_dispatcher_classifies_untyped_exception_without_leaking_message():
    redis = SharedRedis()
    completed = threading.Event()
    terminal = []
    diagnostic = []

    def fail_tick(*, force):
        assert force is True
        raise RuntimeError("profile-secret api_key=do-not-log")

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=fail_tick,
        environment="production",
        site="tiktok",
        ttl_seconds=30,
        terminal_callback=lambda request_id, **payload: terminal.append(
            (request_id, payload)
        ),
        diagnostic_sink=diagnostic.append,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert completed.wait(0.5)
    assert terminal[0][1]["failure_code"] == "probe_dispatch_failed"
    rendered = repr(diagnostic)
    assert "RuntimeError" in rendered
    assert "request-a" in rendered
    assert "profile-secret" not in rendered
    assert "do-not-log" not in rendered


def test_dispatch_failure_code_classifies_known_untyped_dependencies():
    class OperationalError(RuntimeError):
        pass

    assert (
        _dispatch_failure_code(OperationalError("private database path"))
        == "probe_store_unavailable"
    )
    assert (
        _dispatch_failure_code(ConnectionError("private redis URL"))
        == "probe_dependency_unavailable"
    )
    assert (
        _dispatch_failure_code(TimeoutError("private timeout target"))
        == "probe_dispatch_timeout"
    )


def test_dispatcher_heartbeats_owner_until_long_run_completes():
    redis = SharedRedis()
    completed = threading.Event()
    unblock = threading.Event()

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=lambda *, force: unblock.wait(0.2),
        environment="production",
        site="tiktok",
        ttl_seconds=30,
        heartbeat_seconds=0.02,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert threading.Event().wait(0.07) is False
    renewals = [
        call for call in redis.eval_calls if "EXPIRE" in call[0]
    ]
    assert renewals
    assert redis.values[
        "selector_registry:production:tiktok:run_now"
    ] == "request-a"

    unblock.set()
    assert completed.wait(0.5)
    assert redis.values == {}


def test_default_registry_ignores_retired_environment_and_site_settings(monkeypatch):
    import gateway.settings_store
    import redis

    client = object()
    monkeypatch.setattr(
        gateway.settings_store,
        "load_settings",
        lambda: {
            "selector_probe": {
                "enabled": False,
                "site": "tiktok_stage",
                "environment": "staging",
                "timezone": "Asia/Shanghai",
                    "schedule_time": "03:00",
                    "target_origin": "https://www.tiktok.com",
                "test_profile_ids": [],
                "model_id": "",
                "observe_only": False,
                "webhook": {
                    "enabled": False,
                    "type": "generic",
                    "url": "",
                    "signing_secret": "",
                },
            }
        },
    )
    monkeypatch.setattr(
        redis.Redis,
        "from_url",
        lambda *_args, **_kwargs: client,
    )

    registry = default_registry_factory()

    assert registry.redis is client
    assert registry.keys.prefix == "selector_registry:production:tiktok"


def test_failed_heartbeat_cancels_run_before_lock_can_expire():
    class LostRedis(SharedRedis):
        def eval(self, script, key_count, key, owner, *args):
            if "EXPIRE" in script:
                self.values.pop(key, None)
                return 0
            return super().eval(script, key_count, key, owner, *args)

    redis = LostRedis()
    completed = threading.Event()
    cancelled = threading.Event()

    def tick_runner(*, force, stop_event):
        assert force is True
        assert stop_event.wait(0.3)
        cancelled.set()

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=tick_runner,
        environment="production",
        site="tiktok",
        ttl_seconds=30,
        heartbeat_seconds=0.02,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert cancelled.wait(0.5)
    assert completed.wait(0.5)


def test_redis_close_failure_still_releases_local_completion():
    class CloseFailureRedis(SharedRedis):
        def close(self):
            raise RuntimeError("close failed")

    redis = CloseFailureRedis()
    completed = threading.Event()
    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=lambda *, force: None,
        environment="production",
        site="tiktok",
        ttl_seconds=30,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert completed.wait(0.5)


def test_dispatcher_links_request_and_reports_terminal_result():
    redis = SharedRedis()
    completed = threading.Event()
    terminal = []
    calls = []

    def tick_runner(*, force, management_request_id):
        calls.append((force, management_request_id))
        return {"status": "completed", "run_id": 41}

    dispatcher = RedisRunDispatcher(
        redis_factory=lambda: redis,
        tick_runner=tick_runner,
        terminal_callback=lambda request_id, **payload: terminal.append(
            (request_id, payload)
        ),
        environment="production",
        site="tiktok",
        ttl_seconds=30,
    )

    assert dispatcher("request-a", completed.set)["status"] == "accepted"
    assert completed.wait(0.5)
    assert calls == [(True, "request-a")]
    assert terminal == [
        (
            "request-a",
            {
                "result": {"status": "completed", "run_id": 41},
                "failure_code": "",
            },
        )
    ]
