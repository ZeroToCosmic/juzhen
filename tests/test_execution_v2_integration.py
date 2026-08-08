from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time

import execution_v2.service as execution_v2_service_module
import gateway.app as app_module
from gateway.app import create_app
from launcher import LauncherApp


class FakeV2Service:
    def __init__(self) -> None:
        self.closed = 0

    def list_profiles(self):
        return [{"profile_token": "profile_1", "name": "Test"}]

    def close(self) -> None:
        self.closed += 1


def _direct_app(tmp_path):
    created: list[FakeV2Service] = []

    def factory():
        service = FakeV2Service()
        created.append(service)
        return service

    state_dir = tmp_path / "management-state"
    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "SERVER_PORT": 5000,
            "MANAGEMENT_STATE_DIR": state_dir,
            "MANAGEMENT_DB_PATH": state_dir / "management.db",
            "EXECUTION_V2_SERVICE_FACTORY": factory,
        }
    )
    return app, created, state_dir


def test_direct_mode_opens_dashboard_and_v2_without_login_or_management_state(
    tmp_path,
):
    app, created, state_dir = _direct_app(tmp_path)
    client = app.test_client()

    root = client.get("/", base_url="http://127.0.0.1:5000")
    page = client.get("/browser-v2", base_url="http://127.0.0.1:5000")

    root_html = root.get_data(as_text=True)
    v2_html = page.get_data(as_text=True)
    assert root.status_code == 200
    assert page.status_code == 200
    assert "dashboard-shell" in root_html
    assert "browser-v2-app" not in root_html
    assert "browser-v2-app" in v2_html
    assert 'class="dashboard-shell"' in v2_html
    assert 'class="dashboard-sidebar"' in v2_html
    assert 'class="dashboard-main"' in v2_html
    assert 'href="/?panel=strategies"' not in v2_html
    assert '<a class="dashboard-nav-link" href="/browser-v2">' in v2_html
    assert 'href="/browser-v2"' in root_html
    assert "浏览器执行策略 V2" in root_html
    assert client.get("/login", base_url="http://127.0.0.1:5000").status_code == 404
    with client.session_transaction(
        base_url="http://127.0.0.1:5000"
    ) as local_session:
        assert isinstance(local_session.get("csrf_token"), str)
        assert local_session["csrf_token"]
    assert "management_auth_service_factory" not in app.extensions
    assert created == []
    assert not (state_dir / "management.db").exists()
    assert not (state_dir / "session.key").exists()


def test_direct_mode_local_only_guard_rejects_foreign_remote_host_and_port(tmp_path):
    app, _created, _state_dir = _direct_app(tmp_path)
    client = app.test_client()

    assert client.get("/", base_url="http://example.test:5000").status_code == 403
    assert (
        client.get("/", base_url="http://127.0.0.1:5001").status_code
        == 403
    )
    remote = client.get(
        "/api/browser-v2/profiles",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.12"},
    )
    assert remote.status_code == 403
    assert remote.get_json() == {
        "error": {"code": "local_access_only", "message": "仅允许本机访问。"}
    }


def test_direct_mode_v2_service_is_lazy_cached_and_explicitly_closable(tmp_path):
    app, created, _state_dir = _direct_app(tmp_path)
    client = app.test_client()

    first = client.get("/api/browser-v2/profiles", base_url="http://localhost:5000")
    second = client.get("/api/browser-v2/profiles", base_url="http://localhost:5000")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(created) == 1
    app.extensions["execution_v2_close"]()
    assert created[0].closed == 1


def test_direct_mode_v2_factory_is_singleton_under_concurrent_first_access(tmp_path):
    calls: list[FakeV2Service] = []
    calls_lock = threading.Lock()
    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "EXECUTION_V2_SERVICE_FACTORY": lambda: _slow_service(calls, calls_lock),
        }
    )
    factory = app.extensions["execution_v2_service_factory"]
    barrier = threading.Barrier(8)

    def get_service():
        barrier.wait(timeout=3)
        return factory()

    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(lambda _unused: get_service(), range(8)))

    assert len(calls) == 1
    assert all(service is calls[0] for service in services)


def test_default_v2_service_receives_persisted_adspower_settings(
    monkeypatch, tmp_path
):
    captured = {}

    class FakeController:
        def __init__(self, base_url=None, api_key=None):
            captured["controller"] = {"base_url": base_url, "api_key": api_key}

    service = FakeV2Service()

    def fake_default_service(**kwargs):
        captured["service"] = kwargs
        return service

    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {
            "adspower": {
                "base_url": "http://127.0.0.1:50325",
                "api_key": "persisted-key",
            }
        },
    )
    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(
        execution_v2_service_module,
        "create_default_execution_v2_service",
        fake_default_service,
    )

    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "EXECUTION_V2_DB_PATH": tmp_path / "v2.db",
            "EXECUTION_V2_EVIDENCE_DIR": tmp_path / "evidence",
        }
    )
    response = app.test_client().get(
        "/api/browser-v2/profiles", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert captured["controller"] == {
        "base_url": "http://127.0.0.1:50325",
        "api_key": "persisted-key",
    }
    assert captured["service"]["controller"].__class__ is FakeController
    assert callable(captured["service"]["content_library_provider"])
    assert callable(captured["service"]["text_resolver"])


def test_default_v2_service_uses_environment_when_persisted_values_are_blank(
    monkeypatch, tmp_path
):
    captured = {}

    class FakeController:
        def __init__(self, base_url=None, api_key=None):
            captured.update(base_url=base_url, api_key=api_key)

    monkeypatch.setattr(
        app_module,
        "load_settings",
        lambda: {"adspower": {"base_url": "", "api_key": ""}},
    )
    monkeypatch.setenv("ADSPOWER_BASE_URL", "http://127.0.0.1:50326")
    monkeypatch.setenv("ADSPOWER_API_KEY", "environment-key")
    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(
        execution_v2_service_module,
        "create_default_execution_v2_service",
        lambda **_kwargs: FakeV2Service(),
    )

    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "EXECUTION_V2_DB_PATH": tmp_path / "v2.db",
            "EXECUTION_V2_EVIDENCE_DIR": tmp_path / "evidence",
        }
    )
    response = app.test_client().get(
        "/api/browser-v2/profiles", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert captured == {
        "base_url": "http://127.0.0.1:50326",
        "api_key": "environment-key",
    }


def test_direct_mode_concurrent_v2_requests_share_one_service(tmp_path):
    calls: list[FakeV2Service] = []
    calls_lock = threading.Lock()
    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": True,
            "EXECUTION_V2_SERVICE_FACTORY": lambda: _slow_service(calls, calls_lock),
        }
    )
    barrier = threading.Barrier(8)

    def fetch_profiles():
        barrier.wait(timeout=3)
        response = app.test_client().get(
            "/api/browser-v2/profiles", base_url="http://127.0.0.1:5000"
        )
        return response.status_code, app.extensions["execution_v2_service_factory"]()

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(lambda _unused: fetch_profiles(), range(8)))

    assert len(calls) == 1
    assert all(status == 200 for status, _service in responses)
    assert all(service is calls[0] for _status, service in responses)


def test_evidence_route_serves_only_strict_v2_png_names(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    filename = f"{'a' * 32}.png"
    content = b"\x89PNG\r\n\x1a\nlocal-evidence"
    (evidence_dir / filename).write_bytes(content)
    app, _created, _state_dir = _direct_app(tmp_path)
    app.config["EXECUTION_V2_EVIDENCE_DIR"] = evidence_dir
    client = app.test_client()

    response = client.get(
        f"/evidence/{filename}", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == content
    for invalid in (
        "not-a-uuid.png",
        f"{'A' * 32}.png",
        f"{'b' * 32}.jpg",
        "%2e%2e%2fsecret.png",
    ):
        rejected = client.get(
            f"/evidence/{invalid}", base_url="http://127.0.0.1:5000"
        )
        assert rejected.status_code == 404


def test_evidence_route_remains_authenticated_in_legacy_mode(tmp_path):
    state_dir = tmp_path / "legacy-evidence-state"
    evidence_dir = tmp_path / "legacy-evidence"
    evidence_dir.mkdir()
    filename = f"{'c' * 32}.png"
    (evidence_dir / filename).write_bytes(b"\x89PNG\r\n\x1a\n")
    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": False,
            "MANAGEMENT_STATE_DIR": state_dir,
            "MANAGEMENT_DB_PATH": state_dir / "management.db",
            "EXECUTION_V2_EVIDENCE_DIR": evidence_dir,
        }
    )

    response = app.test_client().get(f"/evidence/{filename}")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def _slow_service(calls, calls_lock):
    time.sleep(0.03)
    service = FakeV2Service()
    with calls_lock:
        calls.append(service)
    return service


def test_legacy_mode_keeps_auth_and_does_not_create_v2_service(tmp_path):
    state_dir = tmp_path / "legacy-state"
    calls: list[object] = []
    app = create_app(
        {
            "TESTING": True,
            "LOCAL_DIRECT_MODE": False,
            "MANAGEMENT_STATE_DIR": state_dir,
            "MANAGEMENT_DB_PATH": state_dir / "management.db",
            "EXECUTION_V2_SERVICE_FACTORY": lambda: calls.append(object()),
        }
    )
    client = app.test_client()

    assert client.get("/").status_code == 302
    response = client.get("/api/browser-v2/profiles")
    assert response.status_code == 401
    assert response.get_json() == {"code": "authentication_required"}
    assert calls == []
    assert (state_dir / "session.key").exists()


def test_launcher_forces_local_direct_mode(monkeypatch):
    monkeypatch.setenv("LOCAL_DIRECT_MODE", "0")
    launcher = LauncherApp.__new__(LauncherApp)

    assert launcher._service_environment()["LOCAL_DIRECT_MODE"] == "1"
