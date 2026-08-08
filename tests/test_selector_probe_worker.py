from __future__ import annotations

from datetime import UTC, datetime
import inspect
import json
import subprocess
import sys

import pytest

from selector_probe import worker


def raw_settings(*, observe_only: bool) -> dict:
    return {
        "selector_probe": {
            "enabled": True,
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
        "adspower": {
            "base_url": "http://local.adspower.net:50325",
            "api_key": "test-key",
        },
    }


class FakeStore:
    def __init__(self, *, has_candidate=True):
        self.finished = []
        self.has_candidate = has_candidate

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def start_run(self, **_payload):
        return 7

    def finish_run(self, run_id, **payload):
        self.finished.append((run_id, payload))

    def list_managed_element_rows(self, **_kwargs):
        rows = (
            ({"id": "saved-element", "display_name": "Saved", "status": "healthy", "revision": 1},)
            if self.has_candidate
            else ()
        )
        return rows, len(rows), 1

    def manual_element_definition(self, _element_id):
        return {"locators": [{"type": "css", "value": "#saved"}]}


class FakeRedis:
    def close(self):
        pass


class FakeRegistry:
    def get_active(self):
        return None


class FakeLease:
    def acquire(self):
        return True

    def renew(self):
        return True

    def release(self):
        pass


class RuntimeContext:
    def __init__(self):
        self.enter_count = 0

    def __enter__(self):
        self.enter_count += 1
        return self

    def __exit__(self, *_args):
        return False


class FakeAlertService:
    def cleanup_screenshots(self, **_kwargs):
        pass


class FixedClock:
    def now(self):
        return datetime(2026, 8, 4, 3, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("observe_only", "expected_publish", "status"),
    [(True, False, "healthy"), (False, True, "published")],
)
def test_tick_uses_only_managed_runner_and_honors_read_only_mode(
    tmp_path,
    observe_only,
    expected_publish,
    status,
):
    store = FakeStore()
    calls = []
    reconciliations = []

    def managed_runner(runtime, *, publish, candidate):
        calls.append((runtime, publish, candidate))
        return {
            "status": status,
            "published": publish,
            "new_version": None,
            "proposed_pause_aliases": [],
        }

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=observe_only),
        store_factory=lambda _path: store,
        redis_factory=lambda _url: FakeRedis(),
        registry_factory=lambda *_args, **_kwargs: FakeRegistry(),
        reconcile_runner=lambda *_args: reconciliations.append(True) or {},
        adspower_factory=lambda **_kwargs: object(),
        managed_runner=managed_runner,
        managed_runtime_factory=lambda **_kwargs: RuntimeContext(),
        lease_factory=lambda *_args, **_kwargs: FakeLease(),
        gate_service_factory=lambda *_args, **_kwargs: object(),
        alert_service_factory=lambda *_args, **_kwargs: FakeAlertService(),
        owner_id_factory=lambda: "attempt-one",
        db_path=tmp_path / "probe.db",
        evidence_root=tmp_path,
        redis_url="redis://test/0",
        clock=FixedClock(),
        force=True,
    )

    assert calls == [(calls[0][0], expected_publish, calls[0][2])]
    assert calls[0][2]["elements"]
    assert result["observe_only"] is observe_only
    assert result["status"] == status
    assert len(reconciliations) == (0 if observe_only else 1)
    assert store.finished[0][1]["details"]["observe_only"] is observe_only
    assert store.finished[0][1]["effect"] is None


def test_empty_candidate_does_not_open_managed_runtime_or_raise_infrastructure_alert(
    tmp_path,
):
    store = FakeStore(has_candidate=False)
    runtime = RuntimeContext()
    calls = []
    adspower_calls = []
    runtime_factory_calls = []

    def managed_runner(selected_runtime, *, publish, candidate):
        calls.append((selected_runtime, publish, candidate))
        return {
            "status": "awaiting_element_selection",
            "failure_code": "awaiting_element_selection",
            "published": False,
            "new_version": None,
            "proposed_pause_aliases": [],
        }

    result = worker.run_tick(
        settings_loader=lambda: raw_settings(observe_only=False),
        store_factory=lambda _path: store,
        redis_factory=lambda _url: FakeRedis(),
        registry_factory=lambda *_args, **_kwargs: FakeRegistry(),
        reconcile_runner=lambda *_args: {},
        adspower_factory=lambda **kwargs: adspower_calls.append(kwargs) or object(),
        managed_runner=managed_runner,
        managed_runtime_factory=lambda **kwargs: (
            runtime_factory_calls.append(kwargs) or runtime
        ),
        lease_factory=lambda *_args, **_kwargs: FakeLease(),
        gate_service_factory=lambda *_args, **_kwargs: object(),
        alert_service_factory=lambda *_args, **_kwargs: FakeAlertService(),
        owner_id_factory=lambda: "attempt-empty",
        db_path=tmp_path / "probe.db",
        evidence_root=tmp_path,
        redis_url="redis://test/0",
        clock=FixedClock(),
        force=True,
    )

    assert result["status"] == "awaiting_element_selection"
    assert runtime.enter_count == 0
    assert adspower_calls == []
    assert runtime_factory_calls == []
    assert len(calls) == 1
    assert calls[0][1:] == (True, {"elements": {}})
    assert store.finished[0][1]["status"] == "awaiting_element_selection"
    assert store.finished[0][1]["effect"] is None
    assert store.finished[0][1]["policy"] is None


def test_worker_signature_has_no_legacy_runner_injection():
    parameters = inspect.signature(worker.run_tick).parameters
    for name in (
        "probe_runner",
        "healing_runner",
        "healing_runtime_factory",
    ):
        assert name not in parameters


def test_removed_element_request_waker_fails_explicitly_without_thread():
    with pytest.raises(RuntimeError, match="workflow is removed"):
        worker.wake_element_request_worker("request-one")


def test_worker_source_has_no_old_pipeline_imports_or_consumers():
    source = inspect.getsource(worker)
    for forbidden in (
        "selector_probe.healing_runtime",
        "default_tiktok_contracts",
        "run_observe_probe",
        "consume_element_requests",
        "run_element_request_runtime",
    ):
        assert forbidden not in source


def test_production_import_does_not_load_old_pipeline_modules():
    script = """
import json
import sys
import selector_probe
import selector_probe.worker
forbidden = (
    'selector_probe.contracts',
    'selector_probe.candidates',
    'selector_probe.discovery',
    'selector_probe.repair',
    'selector_probe.model_client',
    'selector_probe.healing_runtime',
)
print(json.dumps([name for name in forbidden if name in sys.modules]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []


def test_package_exports_only_config_and_managed_runtime():
    import selector_probe

    assert set(selector_probe.__all__) == {
        "ProbeConfig",
        "WebhookConfig",
        "ManagedElementRuntime",
        "ManagedProbeRuntime",
        "normalize_probe_config",
        "run_managed_probe",
    }
