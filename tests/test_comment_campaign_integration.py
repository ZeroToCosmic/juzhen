from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

import gateway.app as app_module
from gateway.app import create_app


class FakeCampaignService:
    def __init__(self):
        self.closed = 0

    def list_templates(self):
        return []

    def close(self):
        self.closed += 1


def test_campaign_service_is_lazy_cached_and_local_guarded(tmp_path):
    created = []

    def factory():
        service = FakeCampaignService()
        created.append(service)
        return service

    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": factory,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })
    client = app.test_client()

    first = client.get("/api/browser-v2/comment-templates", base_url="http://127.0.0.1:5000")
    second = client.get("/api/browser-v2/comment-templates", base_url="http://127.0.0.1:5000")
    remote = client.get(
        "/api/browser-v2/comment-templates",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )

    assert first.get_json() == {"data": []}
    assert second.status_code == 200
    assert remote.status_code == 403
    assert len(created) == 1
    app.extensions["comment_campaign_close"]()
    assert created[0].closed == 1


def test_remote_campaign_request_is_rejected_before_service_creation(tmp_path):
    created = []
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
            lambda: created.append(FakeCampaignService()) or created[-1]
        ),
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-templates",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )

    assert response.status_code == 403
    assert created == []


def test_campaign_factory_is_singleton_under_concurrent_first_access():
    created = []
    created_lock = threading.Lock()

    def factory():
        with created_lock:
            service = FakeCampaignService()
            created.append(service)
            return service

    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": factory,
    })
    service_factory = app.extensions["comment_campaign_service_factory"]
    barrier = threading.Barrier(8)

    def get_service():
        barrier.wait(timeout=3)
        return service_factory()

    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(lambda _value: get_service(), range(8)))

    assert len(created) == 1
    assert all(service is created[0] for service in services)


def test_campaign_blueprint_inherits_management_auth(monkeypatch, tmp_path):
    created = []
    monkeypatch.setattr(app_module, "load_or_create_session_key", lambda _path: "test")
    state_dir = tmp_path / "management-state"
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": False,
        "MANAGEMENT_STATE_DIR": state_dir,
        "MANAGEMENT_DB_PATH": state_dir / "management.db",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
        lambda: created.append(FakeCampaignService()) or created[-1]
        ),
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-templates"
    )

    assert response.status_code == 401
    assert created == []


def test_default_campaign_profile_provider_whitelists_and_exposes_first_profile(
    monkeypatch, tmp_path
):
    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def list_all_profiles(self):
            return [{
                "id": "raw-adspower-secret", "name": "Alice",
                "status": "active", "group_name": "private-group",
            }]

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(app_module, "load_settings", lambda: {"adspower": {}})
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-profile-metadata",
        base_url="http://127.0.0.1:5000",
    )

    assert response.status_code == 200
    rows = response.get_json()["data"]
    assert rows[0]["name"] == "Alice"
    assert rows[0]["configured"] is False
    assert rows[0]["profile_ref"].startswith("profile_ref_")
    assert "raw-adspower-secret" not in repr(rows)
    assert "group_name" not in rows[0]


def test_campaign_workbench_page_uses_local_management_shell_without_raw_profile_ids(tmp_path):
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })

    response = app.test_client().get(
        "/comment-campaigns", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'meta name="csrf-token"' in page
    assert page.index("management_fetch.js") < page.index("comment_campaign.js")
    assert 'href="/comment-campaigns"' in page
    assert "raw-adspower-secret" not in page
    assert "评论 Campaign" in page
    assert app.test_client().get(
        "/comment-campaigns", base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    ).status_code == 403
    assert app.test_client().get(
        "/comment-campaign-evidence/0123456789abcdef0123456789abcdef.png",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    ).status_code == 403


def test_campaign_settings_write_requires_admin_and_csrf(tmp_path, monkeypatch):
    from werkzeug.security import generate_password_hash
    from gateway.auth_store import AuthStore
    from gateway.management_db import open_management_db

    monkeypatch.setattr(app_module, "load_or_create_session_key", lambda _path: "test")
    state_dir = tmp_path / "management"
    database_path = state_dir / "management.db"
    connection = open_management_db(database_path)
    try:
        users = AuthStore(connection)
        password = generate_password_hash("valid password 123", method="scrypt")
        users.create_user("admin", password, "administrator", must_change_password=False)
        users.create_user("operator", password, "operator", must_change_password=False)
    finally:
        connection.close()
    app = create_app({"TESTING": True, "LOCAL_DIRECT_MODE": False, "MANAGEMENT_STATE_DIR": state_dir, "MANAGEMENT_DB_PATH": database_path})

    def login(username):
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as values:
            csrf = values["csrf_token"]
        response = client.post("/api/auth/login", json={"username": username, "password": "valid password 123"}, headers={"X-CSRF-Token": csrf})
        return client, response.get_json()["csrf_token"]

    admin, admin_csrf = login("admin")
    operator, operator_csrf = login("operator")
    payload = {
        "expected_revision": 1, "entry_element_id": "entry",
        "input_element_id": "input", "submit_element_id": "submit",
        "account_element_id": "account",
    }

    operator = operator.put("/api/browser-v2/comment-settings", json=payload, headers={"X-CSRF-Token": operator_csrf})
    missing_csrf = admin.put("/api/browser-v2/comment-settings", json=payload)
    assert admin.put("/api/browser-v2/comment-settings", json=payload, headers={"X-CSRF-Token": admin_csrf}).status_code != 403

    assert operator.status_code == 403
    assert missing_csrf.status_code == 403


def test_campaign_evidence_route_rejects_non_uuid_paths(tmp_path):
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
    })
    client = app.test_client()

    response = client.get(
        "/comment-campaign-evidence/../secret.txt",
        base_url="http://127.0.0.1:5000",
    )

    assert response.status_code == 404


def test_campaign_evidence_rejects_symlink_and_serves_only_no_store_png(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    name = "0123456789abcdef0123456789abcdef.png"
    (evidence_dir / name).write_bytes(b"png")
    app = create_app({
        "TESTING": True, "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": evidence_dir,
    })
    client = app.test_client()

    served = client.get(f"/comment-campaign-evidence/{name}", base_url="http://127.0.0.1:5000")

    assert served.status_code == 200
    assert "no-store" in served.headers["Cache-Control"]
    link = evidence_dir / "fedcba9876543210fedcba9876543210.png"
    try:
        link.symlink_to(evidence_dir / name)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")
    assert client.get(f"/comment-campaign-evidence/{link.name}", base_url="http://127.0.0.1:5000").status_code == 404


def test_campaign_settings_are_a_strict_read_only_comment_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "load_settings", lambda: {
        "adspower": {},
        "comment_campaign": {"element_bindings": {
            "entry_element_id": "entry-1", "input_element_id": "input-1",
            "submit_element_id": "submit-1", "account_element_id": "account-1",
            "legacy_selector": "must-not-leak",
        }},
    })
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-settings", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert response.get_json() == {"data": {
        "element_bindings": {
            "entry_element_id": "entry-1", "input_element_id": "input-1",
            "submit_element_id": "submit-1", "account_element_id": "account-1",
        },
        "configured": True,
        "revision": 1,
        "can_write": True,
    }}


def test_campaign_settings_put_is_active_kind_checked_and_revision_guarded(tmp_path, monkeypatch):
    from execution_v2.store import ExecutionStore

    definition = {
        "url_pattern": "https://www.tiktok.com/*", "frame_path": [],
        "locators": [{"type": "css", "value": ".comment", "priority": 1}],
        "diagnostic_metadata": {}, "screenshot_path": "",
    }
    elements = ExecutionStore(tmp_path / "execution.db")
    elements.initialize()
    for identifier, kind in (("entry", "click"), ("input", "input"), ("submit", "click"), ("account", "click")):
        elements.create_element(identifier, identifier, "action", kind, definition)
    persisted = {"adspower": {}, "comment_campaign": {"revision": 3, "element_bindings": {
        "entry_element_id": "old-entry", "input_element_id": "old-input",
        "submit_element_id": "old-submit", "account_element_id": "old-account",
    }}, "unrelated": {"preserved": True}}
    monkeypatch.setattr(app_module, "load_settings", lambda: persisted)
    monkeypatch.setattr(app_module, "mutate_settings", lambda mutator: mutator(persisted) or persisted)
    app = create_app({
        "TESTING": True, "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "EXECUTION_V2_DB_PATH": tmp_path / "execution.db",
    })
    client = app.test_client()
    body = {"expected_revision": 3, "entry_element_id": "entry", "input_element_id": "input", "submit_element_id": "submit", "account_element_id": "account"}

    saved = client.put("/api/browser-v2/comment-settings", json=body, base_url="http://127.0.0.1:5000")
    stale = client.put("/api/browser-v2/comment-settings", json=body, base_url="http://127.0.0.1:5000")
    extra = client.put("/api/browser-v2/comment-settings", json={**body, "expected_revision": 4, "extra": "blocked"}, base_url="http://127.0.0.1:5000")
    blank = client.put("/api/browser-v2/comment-settings", json={**body, "expected_revision": 4, "input_element_id": ""}, base_url="http://127.0.0.1:5000")

    assert saved.status_code == 200
    assert saved.get_json()["data"]["revision"] == 4
    assert stale.status_code == 409
    assert extra.status_code == blank.status_code == 422
    assert persisted["comment_campaign"] == {"revision": 4, "element_bindings": {
        "entry_element_id": "entry", "input_element_id": "input",
        "submit_element_id": "submit", "account_element_id": "account",
    }}
    assert persisted["unrelated"] == {"preserved": True}
