import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import gateway.app as app_module
from gateway.app import create_app
from gateway.auth_blueprint import allow_roles, public_endpoint
from gateway.management_db import open_management_db


def test_dashboard_redirects_and_api_returns_401(client):
    dashboard = client.get("/")
    assert dashboard.status_code == 302
    assert dashboard.headers["Location"].endswith("/login")

    response = client.get("/api/browser/elements")
    assert response.status_code == 401
    assert response.get_json()["code"] == "authentication_required"


def test_login_requires_csrf(client):
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "valid password 123"},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "csrf_failed"


def test_login_page_uses_accessible_template_and_external_auth_controller(
    client,
):
    response = client.get("/login")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count("<main") == 1
    assert '<label for="username">' in html
    assert '<label for="password">' in html
    assert 'aria-live="polite"' in html
    assert 'name="csrf_token"' in html
    assert 'src="/static/auth.js"' in html
    assert 'href="/static/dashboard_shell.css"' in html
    assert "remember" not in html.lower()
    assert "<script>" not in html
    with client.session_transaction() as values:
        assert values["csrf_token"] in html


def test_login_page_contains_separate_hidden_password_change_view(client):
    html = client.get("/login").get_data(as_text=True)

    assert 'id="login-view"' in html
    assert 'id="password-change-view"' in html
    assert 'aria-live="polite"' in html
    assert 'autocomplete="new-password"' in html
    assert 'data-auth-view="password-change"' in html


def test_tiktok_stats_page_installs_authenticated_fetch(admin_client):
    response = admin_client.get("/tiktok-stats")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert (
        f'<meta name="csrf-token" content="{admin_client.csrf_token}">'
        in html
    )
    assert "/static/management_fetch.js" in html
    assert html.index("management_fetch.js") < html.index("tiktok_stats.js")
    run = admin_client.post(
        "/api/tiktok-stats/runs",
        json={"run_type": "incremental"},
    )
    assert run.status_code == 503
    assert run.get_json()["error"]["code"] == "dispatch_unavailable"


def test_login_clears_prelogin_state_rotates_csrf_and_returns_session(client):
    assert client.get("/login").status_code == 200
    with client.session_transaction() as values:
        first_csrf = values["csrf_token"]
        values["untrusted_prelogin_value"] = "discard-me"

    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "valid password 123"},
        headers={"X-CSRF-Token": first_csrf},
    )

    assert login.status_code == 200
    login_payload = login.get_json()
    with client.session_transaction() as values:
        assert "untrusted_prelogin_value" not in values
        assert values["csrf_token"] != first_csrf
        assert login_payload["csrf_token"] == values["csrf_token"]
        for key in (
            "user_id",
            "role",
            "session_version",
            "issued_at",
            "last_activity_at",
        ):
            assert key in values
    payload = client.get("/api/auth/session").get_json()
    assert payload["username"] == "admin"
    assert payload["role"] == "administrator"
    assert payload["must_change_password"] is False
    assert "users:manage" in payload["permissions"]


def test_healthz_is_public_and_exact(client):
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_unsafe_legacy_route_defaults_to_administrator(operator_client):
    response = operator_client.put(
        "/api/browser/elements",
        json={"elements": {}},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "forbidden"


def test_operator_can_read_safe_legacy_route(operator_client):
    response = operator_client.get("/api/browser/elements")

    assert response.status_code == 200


def test_unsafe_route_rejects_missing_or_wrong_csrf(admin_client):
    for headers in ({}, {"X-CSRF-Token": "wrong"}):
        response = admin_client.client.put(
            "/api/browser/elements",
            json={"elements": {}},
            headers=headers,
        )
        assert response.status_code == 403
        assert response.get_json()["code"] == "csrf_failed"


def test_must_change_user_is_limited_to_session_logout_and_change(
    must_change_client,
):
    session_response = must_change_client.get("/api/auth/session")
    assert session_response.status_code == 200
    assert session_response.get_json()["must_change_password"] is True

    blocked = must_change_client.get("/api/browser/elements")
    assert blocked.status_code == 403
    assert blocked.get_json()["code"] == "password_change_required"

    changed = must_change_client.post(
        "/api/auth/change-password",
        json={
            "current_password": must_change_client.password,
            "new_password": "replacement password 456",
        },
    )
    assert changed.status_code == 200
    assert changed.get_json()["login_required"] is True
    with must_change_client.client.session_transaction() as values:
        assert "user_id" not in values


def test_protected_request_slides_last_activity(admin_client):
    with admin_client.client.session_transaction() as values:
        previous = datetime.now(timezone.utc) - timedelta(minutes=1)
        values["issued_at"] = (
            previous - timedelta(minutes=1)
        ).isoformat()
        values["last_activity_at"] = previous.isoformat()

    assert admin_client.get("/api/auth/session").status_code == 200

    with admin_client.client.session_transaction() as values:
        updated = datetime.fromisoformat(values["last_activity_at"])
    assert updated > previous


def test_logout_requires_csrf_and_clears_session(admin_client):
    missing = admin_client.client.post("/api/auth/logout")
    assert missing.status_code == 403

    response = admin_client.post("/api/auth/logout")
    assert response.status_code == 200
    with admin_client.client.session_transaction() as values:
        assert "user_id" not in values


def test_session_key_is_persistent_and_cookie_policy_is_configured(tmp_path):
    state_dir = tmp_path / "state"
    config = {
        "TESTING": True,
        "MANAGEMENT_STATE_DIR": state_dir,
        "MANAGEMENT_DB_PATH": state_dir / "management.db",
        "PUBLIC_ORIGIN_HTTPS": True,
    }

    first = create_app(config)
    second = create_app(config)

    assert first.config["SECRET_KEY"] == second.config["SECRET_KEY"]
    assert first.config["SESSION_COOKIE_HTTPONLY"] is True
    assert first.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert first.config["SESSION_COOKIE_SECURE"] is True
    assert (state_dir / "session.key").exists()


def test_request_scoped_management_connection_is_closed(
    tmp_path,
    monkeypatch,
):
    connections = []

    def tracking_open(path):
        connection = open_management_db(path)
        connections.append(connection)
        return connection

    monkeypatch.setattr(
        app_module,
        "open_management_db",
        tracking_open,
    )
    state_dir = tmp_path / "state"
    app = create_app(
        {
            "TESTING": True,
            "MANAGEMENT_STATE_DIR": state_dir,
            "MANAGEMENT_DB_PATH": state_dir / "management.db",
        }
    )

    response = app.test_client().get("/api/auth/session")

    assert response.status_code == 401
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connections[0].execute("SELECT 1")


def test_role_decorators_store_immutable_metadata():
    def view():
        return None

    assert public_endpoint(view).management_public is True
    decorated = allow_roles("operator", "administrator")(view)
    assert decorated.management_roles == frozenset(
        {"operator", "administrator"}
    )
    with pytest.raises(ValueError, match="invalid management roles"):
        allow_roles()
    with pytest.raises(ValueError, match="invalid management roles"):
        allow_roles("unknown")


def test_operator_cannot_list_or_create_users(operator_client):
    assert operator_client.get("/api/admin/users").status_code == 403
    assert operator_client.post(
        "/api/admin/users",
        json={"username": "new-ops", "role": "operator"},
    ).status_code == 403


def test_administrator_creates_one_time_password_without_projection_leaks(
    admin_client,
):
    response = admin_client.post(
        "/api/admin/users",
        json={"username": "new-ops", "role": "operator"},
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["user"]["must_change_password"] is True
    assert len(payload["temporary_password"]) >= 20
    assert "password_hash" not in payload["user"]
    listed_response = admin_client.get("/api/admin/users")
    assert listed_response.status_code == 200
    listed = listed_response.get_json()["users"]
    serialized = str(listed).casefold()
    assert "temporary_password" not in serialized
    assert "password_hash" not in serialized
    assert payload["temporary_password"] not in serialized


def test_user_creation_rejects_insecure_remote_origin_and_unknown_fields(
    admin_client,
):
    remote = admin_client.client
    remote.get("/login", base_url="http://example.com")
    with remote.session_transaction(
        base_url="http://example.com"
    ) as values:
        login_csrf = values["csrf_token"]
    assert remote.post(
        "/api/auth/login",
        base_url="http://example.com",
        json={
            "username": "admin",
            "password": "valid password 123",
        },
        headers={"X-CSRF-Token": login_csrf},
    ).status_code == 200
    remote_session = remote.get(
        "/api/auth/session",
        base_url="http://example.com",
    ).get_json()
    remote_csrf = remote_session["csrf_token"]

    insecure = remote.post(
        "/api/admin/users",
        base_url="http://example.com",
        json={"username": "remote-ops", "role": "operator"},
        headers={"X-CSRF-Token": remote_csrf},
    )
    assert insecure.status_code == 403
    assert insecure.get_json()["code"] == "secure_origin_required"

    secure = remote.post(
        "/api/admin/users",
        base_url="https://example.com",
        json={"username": "secure-ops", "role": "operator"},
        headers={"X-CSRF-Token": remote_csrf},
    )
    assert secure.status_code == 201

    unknown = admin_client.post(
        "/api/admin/users",
        json={
            "username": "bad-fields",
            "role": "operator",
            "enabled": True,
        },
    )
    assert unknown.status_code == 400
    assert unknown.get_json()["code"] == "invalid_request"


def test_user_creation_rejects_spoofed_loopback_host(admin_client):
    response = admin_client.post(
        "/api/admin/users",
        json={"username": "spoofed-host", "role": "operator"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.42"},
    )

    assert response.status_code == 403
    assert response.get_json()["code"] == "secure_origin_required"


def test_patch_requires_revision_and_rejects_stale_update(admin_client):
    created = admin_client.post(
        "/api/admin/users",
        json={"username": "patch-ops", "role": "operator"},
    ).get_json()["user"]

    missing = admin_client.patch(
        f"/api/admin/users/{created['id']}",
        json={"enabled": False},
    )
    assert missing.status_code == 400

    updated = admin_client.patch(
        f"/api/admin/users/{created['id']}",
        json={
            "expected_revision": created["revision"],
            "enabled": False,
        },
    )
    assert updated.status_code == 200
    assert updated.get_json()["user"]["enabled"] is False
    assert updated.get_json()["user"]["revision"] == (
        created["revision"] + 1
    )

    stale = admin_client.patch(
        f"/api/admin/users/{created['id']}",
        json={
            "expected_revision": created["revision"],
            "role": "administrator",
        },
    )
    assert stale.status_code == 409
    assert stale.get_json()["code"] == "stale_revision"


def test_reset_password_is_one_time_and_revoke_increments_revision(
    admin_client,
):
    created = admin_client.post(
        "/api/admin/users",
        json={"username": "reset-ops", "role": "operator"},
    ).get_json()["user"]

    reset = admin_client.post(
        f"/api/admin/users/{created['id']}/reset-password",
    )
    assert reset.status_code == 200
    reset_payload = reset.get_json()
    assert len(reset_payload["temporary_password"]) >= 20
    assert reset_payload["user"]["must_change_password"] is True
    assert reset_payload["user"]["revision"] == created["revision"] + 1

    revoked = admin_client.post(
        f"/api/admin/users/{created['id']}/revoke-sessions",
    )
    assert revoked.status_code == 200
    assert revoked.get_json()["user"]["revision"] == (
        reset_payload["user"]["revision"] + 1
    )
    listed = str(
        admin_client.get("/api/admin/users").get_json()
    )
    assert reset_payload["temporary_password"] not in listed


@pytest.mark.parametrize(
    "suffix",
    ("reset-password", "revoke-sessions"),
)
def test_reset_and_revoke_reject_unknown_fields(admin_client, suffix):
    created = admin_client.post(
        "/api/admin/users",
        json={"username": f"strict-{suffix}", "role": "operator"},
    ).get_json()["user"]

    response = admin_client.post(
        f"/api/admin/users/{created['id']}/{suffix}",
        json={"unexpected": True},
    )

    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_request"
