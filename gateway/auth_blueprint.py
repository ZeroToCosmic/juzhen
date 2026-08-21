"""Flask management authentication routes and fail-closed request guard."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
from ipaddress import ip_address
from urllib.parse import urlsplit

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from gateway.auth_service import AuthError, new_csrf_token


_ALLOWED_ROLES = frozenset({"administrator", "operator"})
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MUST_CHANGE_ENDPOINTS = frozenset(
    {
        "management_auth.session_info",
        "management_auth.logout",
        "management_auth.change_password",
    }
)

ROLE_PERMISSIONS = {
    "administrator": (
        "management:read",
        "management:write",
        "strategy:execute",
        "probe:run",
        "alert:acknowledge",
        "webhook:test",
        "users:manage",
    ),
    "operator": (
        "management:read",
        "probe:run",
        "alert:acknowledge",
        "webhook:test",
    ),
}

def public_endpoint(function):
    function.management_public = True
    return function


def allow_roles(*roles):
    allowed = frozenset(roles)
    if not allowed or not allowed <= _ALLOWED_ROLES:
        raise ValueError("invalid management roles")

    def decorate(function):
        function.management_roles = allowed
        return function

    return decorate


def create_auth_blueprint(auth_service_factory) -> Blueprint:
    blueprint = Blueprint("management_auth", __name__)

    @blueprint.get("/login")
    @public_endpoint
    def login_page():
        csrf_token = session.get("csrf_token")
        if not isinstance(csrf_token, str) or not csrf_token:
            csrf_token = new_csrf_token()
            session["csrf_token"] = csrf_token
        return render_template("login.html", csrf_token=csrf_token)

    @blueprint.get("/healthz")
    @public_endpoint
    def healthz():
        return jsonify({"status": "ok"})

    @blueprint.post("/api/auth/login")
    @public_endpoint
    def login():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = request.form
        username = payload.get("username", "")
        password = payload.get("password", "")
        user = auth_service_factory().authenticate(
            username if isinstance(username, str) else "",
            password if isinstance(password, str) else "",
            datetime.now(timezone.utc),
        )
        now = datetime.now(timezone.utc)
        session.clear()
        session.update(
            {
                "user_id": user.id,
                "role": user.role,
                "session_version": user.session_version,
                "issued_at": now.isoformat(),
                "last_activity_at": now.isoformat(),
                "csrf_token": new_csrf_token(),
            }
        )
        return jsonify(
            {
                "status": "authenticated",
                "must_change_password": user.must_change_password,
                "csrf_token": session["csrf_token"],
            }
        )

    @blueprint.get("/api/auth/session")
    @allow_roles("administrator", "operator")
    def session_info():
        user = g.management_user
        return jsonify(
            {
                "username": user.username,
                "role": user.role,
                "permissions": list(ROLE_PERMISSIONS[user.role]),
                "must_change_password": user.must_change_password,
                "csrf_token": session["csrf_token"],
            }
        )

    @blueprint.post("/api/auth/logout")
    @allow_roles("administrator", "operator")
    def logout():
        session.clear()
        return jsonify({"status": "logged_out"})

    @blueprint.post("/api/auth/change-password")
    @allow_roles("administrator", "operator")
    def change_password():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            payload = {}
        user = auth_service_factory().change_password(
            g.management_user.id,
            payload.get("current_password", ""),
            payload.get("new_password", ""),
            datetime.now(timezone.utc),
        )
        session.clear()
        return jsonify(
            {
                "status": "password_changed",
                "user_id": user.id,
                "login_required": True,
            }
        )

    @blueprint.get("/api/admin/users")
    @allow_roles("administrator")
    def list_users():
        users = auth_service_factory().list_users()
        return jsonify(
            {"users": [public_user(user) for user in users]}
        )

    @blueprint.post("/api/admin/users")
    @allow_roles("administrator")
    def create_user():
        if not _secure_user_creation_origin():
            raise AuthError("secure_origin_required", 403)
        payload = _strict_json_payload(
            allowed={"username", "role"},
            required={"username", "role"},
        )
        username = payload["username"]
        role = payload["role"]
        if not isinstance(username, str) or not isinstance(role, str):
            raise AuthError("invalid_request", 400)
        user, temporary_password = (
            auth_service_factory().create_temporary_user(
                username,
                role,
                g.management_user.id,
                datetime.now(timezone.utc),
            )
        )
        return (
            jsonify(
                {
                    "user": public_user(user),
                    "temporary_password": temporary_password,
                }
            ),
            201,
        )

    @blueprint.patch("/api/admin/users/<int:user_id>")
    @allow_roles("administrator")
    def update_user(user_id):
        payload = _strict_json_payload(
            allowed={"expected_revision", "role", "enabled"},
            required={"expected_revision"},
        )
        if not ({"role", "enabled"} & payload.keys()):
            raise AuthError("invalid_request", 400)
        expected_revision = payload["expected_revision"]
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision <= 0
            or (
                "role" in payload
                and not isinstance(payload["role"], str)
            )
            or (
                "enabled" in payload
                and not isinstance(payload["enabled"], bool)
            )
        ):
            raise AuthError("invalid_request", 400)
        user = auth_service_factory().update_user(
            user_id,
            expected_revision=expected_revision,
            role=payload.get("role"),
            enabled=payload.get("enabled"),
            actor_user_id=g.management_user.id,
        )
        return jsonify({"user": public_user(user)})

    @blueprint.post("/api/admin/users/<int:user_id>/reset-password")
    @allow_roles("administrator")
    def reset_password(user_id):
        _require_empty_json_payload()
        user, temporary_password = (
            auth_service_factory().reset_temporary_password(
                user_id,
                g.management_user.id,
                datetime.now(timezone.utc),
            )
        )
        return jsonify(
            {
                "user": public_user(user),
                "temporary_password": temporary_password,
            }
        )

    @blueprint.post("/api/admin/users/<int:user_id>/revoke-sessions")
    @allow_roles("administrator")
    def revoke_sessions(user_id):
        _require_empty_json_payload()
        user = auth_service_factory().revoke_sessions(
            user_id,
            g.management_user.id,
            datetime.now(timezone.utc),
        )
        return jsonify({"user": public_user(user)})

    return blueprint


def install_management_guard(app, auth_service_factory) -> None:
    @app.before_request
    def management_guard():
        endpoint = request.endpoint
        if endpoint == "static" or (
            request.method == "GET" and request.path == "/healthz"
        ):
            return None

        view = (
            current_app.view_functions.get(endpoint)
            if endpoint is not None
            else None
        )
        if request.method in _UNSAFE_METHODS:
            _validate_csrf()
        if view is None:
            return None
        if getattr(view, "management_public", False):
            return None

        user = auth_service_factory().validate_session(
            dict(session),
            datetime.now(timezone.utc),
        )
        g.management_user = user
        session["last_activity_at"] = datetime.now(
            timezone.utc
        ).isoformat()

        if (
            user.must_change_password
            and endpoint not in _MUST_CHANGE_ENDPOINTS
        ):
            raise AuthError("password_change_required", 403)

        allowed = getattr(view, "management_roles", None)
        if allowed is None:
            allowed = (
                frozenset({"administrator"})
                if request.method in _UNSAFE_METHODS
                else _ALLOWED_ROLES
            )
        if user.role not in allowed:
            raise AuthError("forbidden", 403)
        return None

    app.register_error_handler(AuthError, _auth_error_response)


def _validate_csrf() -> None:
    expected = session.get("csrf_token")
    supplied = request.headers.get("X-CSRF-Token")
    if supplied is None and request.form:
        supplied = request.form.get("csrf_token")
    if (
        not isinstance(expected, str)
        or not expected
        or not isinstance(supplied, str)
        or not hmac.compare_digest(expected, supplied)
    ):
        raise AuthError("csrf_failed", 403)


def _auth_error_response(error):
    if request.path.startswith("/api/") or request.is_json:
        return jsonify({"code": error.code}), error.status
    return redirect(url_for("management_auth.login_page"))


def public_user(user) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "enabled": user.enabled,
        "must_change_password": user.must_change_password,
        "locked_until": user.locked_until,
        "last_login_at": user.last_login_at,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "revision": user.session_version,
    }


def _strict_json_payload(*, allowed: set, required: set) -> dict:
    payload = request.get_json(silent=True)
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or not payload.keys() <= allowed
    ):
        raise AuthError("invalid_request", 400)
    return payload


def _secure_user_creation_origin() -> bool:
    hostname = urlsplit(request.host_url).hostname
    return bool(request.is_secure or (
        _is_loopback_host(hostname)
        and _is_loopback_address(request.remote_addr)
    ))


def _is_loopback_host(hostname) -> bool:
    if hostname == "localhost":
        return True
    return _is_loopback_address(hostname)


def _is_loopback_address(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return ip_address(value).is_loopback
    except ValueError:
        return False


def _require_empty_json_payload() -> None:
    if not request.get_data(cache=True):
        return
    if request.get_json(silent=True) != {}:
        raise AuthError("invalid_request", 400)


__all__ = [
    "allow_roles",
    "create_auth_blueprint",
    "install_management_guard",
    "public_user",
    "public_endpoint",
]
