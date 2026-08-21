"""Management authentication, password, session, and CSRF rules."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping
import secrets

from werkzeug.security import check_password_hash, generate_password_hash

from gateway.auth_store import (
    AuthStore,
    LastAdministratorError,
    ManagementUser,
)


MAX_FAILURES = 5
LOCK_MINUTES = 15
IDLE_MINUTES = 30
ABSOLUTE_HOURS = 8
MIN_PASSWORD_LENGTH = 12
ALLOWED_ROLES = frozenset({"administrator", "operator"})
DUMMY_HASH = generate_password_hash(
    "management-auth-dummy-password",
    method="scrypt",
)


class AuthError(RuntimeError):
    def __init__(self, code, status):
        super().__init__(code)
        self.code = code
        self.status = status


class AuthService:
    def __init__(self, store: AuthStore) -> None:
        self.store = store

    def authenticate(self, username, password, now) -> ManagementUser:
        now_value = _aware_now(now)
        password_value = password if isinstance(password, str) else ""
        normalized_username = (
            username.strip() if isinstance(username, str) else ""
        )
        user = _find_user(self.store, normalized_username)
        if user is None or not user.enabled:
            check_password_hash(DUMMY_HASH, password_value)
            raise _invalid_credentials()

        locked_until = _stored_timestamp(user.locked_until)
        password_matches = check_password_hash(
            user.password_hash,
            password_value,
        )
        if locked_until is not None and now_value < locked_until:
            raise _invalid_credentials()
        if not password_matches:
            self.store.record_login_failure_if_current(
                user.id,
                user.password_hash,
                user.session_version,
                now_value,
            )
            raise _invalid_credentials()
        authenticated = self.store.record_login_success_if_current(
            user.id,
            user.password_hash,
            user.session_version,
            now_value,
        )
        if authenticated is None:
            raise _invalid_credentials()
        return authenticated

    def change_password(
        self,
        user_id,
        current_password,
        new_password,
        now,
    ) -> ManagementUser:
        now_value = _aware_now(now)
        user = _find_user_by_id(self.store, user_id)
        if user is None or not user.enabled:
            raise AuthError("authentication_required", 401)
        current_value = (
            current_password if isinstance(current_password, str) else ""
        )
        if not check_password_hash(user.password_hash, current_value):
            raise _invalid_credentials()
        if (
            not isinstance(new_password, str)
            or len(new_password) < MIN_PASSWORD_LENGTH
        ):
            raise AuthError("password_too_short", 400)
        password_hash = generate_password_hash(
            new_password,
            method="scrypt",
        )
        changed = self.store.replace_password_if_current(
            user.id,
            user.password_hash,
            user.session_version,
            password_hash,
            must_change_password=False,
            actor_user_id=user.id,
            now=now_value,
        )
        if changed is None:
            raise AuthError("session_revoked", 401)
        return changed

    def create_temporary_user(
        self,
        username,
        role,
        actor_user_id,
        now,
    ) -> tuple[ManagementUser, str]:
        now_value = _aware_now(now)
        temporary_password = secrets.token_urlsafe(18)
        password_hash = generate_password_hash(
            temporary_password,
            method="scrypt",
        )
        try:
            user = self.store.create_user(
                username,
                password_hash,
                role,
                must_change_password=True,
                actor_user_id=actor_user_id,
                now=now_value,
            )
        except ValueError as error:
            raise _store_auth_error(error) from error
        return user, temporary_password

    def list_users(self) -> tuple[ManagementUser, ...]:
        return self.store.list_users()

    def update_user(
        self,
        user_id,
        *,
        expected_revision,
        role=None,
        enabled=None,
        actor_user_id,
    ) -> ManagementUser:
        try:
            updated = self.store.update_access_if_revision(
                user_id,
                expected_revision=expected_revision,
                role=role,
                enabled=enabled,
                actor_user_id=actor_user_id,
            )
        except LastAdministratorError as error:
            raise AuthError("last_administrator", 409) from error
        except ValueError as error:
            raise _store_auth_error(error) from error
        if updated is None:
            raise AuthError("stale_revision", 409)
        return updated

    def reset_temporary_password(
        self,
        user_id,
        actor_user_id,
        now,
    ) -> tuple[ManagementUser, str]:
        now_value = _aware_now(now)
        temporary_password = secrets.token_urlsafe(18)
        password_hash = generate_password_hash(
            temporary_password,
            method="scrypt",
        )
        try:
            user = self.store.replace_password(
                user_id,
                password_hash,
                must_change_password=True,
                actor_user_id=actor_user_id,
                now=now_value,
            )
        except ValueError as error:
            raise _store_auth_error(error) from error
        return user, temporary_password

    def revoke_sessions(
        self,
        user_id,
        actor_user_id,
        now,
    ) -> ManagementUser:
        now_value = _aware_now(now)
        try:
            return self.store.revoke_sessions(
                user_id,
                actor_user_id=actor_user_id,
                now=now_value,
            )
        except ValueError as error:
            raise _store_auth_error(error) from error

    def validate_session(self, payload, now) -> ManagementUser:
        now_value = _aware_now(now)
        values = _session_values(payload)
        issued_at = values["issued_at"]
        last_activity_at = values["last_activity_at"]
        if (
            issued_at > now_value
            or last_activity_at > now_value
            or last_activity_at < issued_at
        ):
            raise AuthError("authentication_required", 401)

        user = _find_user_by_id(self.store, values["user_id"])
        if user is None or not user.enabled:
            raise AuthError("authentication_required", 401)
        if user.session_version != values["session_version"]:
            raise AuthError("session_revoked", 401)
        if (
            now_value - last_activity_at
            >= timedelta(minutes=IDLE_MINUTES)
            or now_value - issued_at
            >= timedelta(hours=ABSOLUTE_HOURS)
        ):
            raise AuthError("session_expired", 401)
        return user


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _find_user(store: AuthStore, username: str) -> ManagementUser | None:
    if not username:
        return None
    try:
        return store.get_by_username(username)
    except ValueError:
        return None


def _find_user_by_id(store: AuthStore, user_id) -> ManagementUser | None:
    if isinstance(user_id, bool) or not isinstance(user_id, int):
        return None
    try:
        return store.get_by_id(user_id)
    except ValueError:
        return None


def _session_values(payload) -> dict:
    if not isinstance(payload, Mapping):
        raise AuthError("authentication_required", 401)
    user_id = payload.get("user_id")
    session_version = payload.get("session_version")
    if (
        isinstance(user_id, bool)
        or not isinstance(user_id, int)
        or user_id <= 0
        or isinstance(session_version, bool)
        or not isinstance(session_version, int)
        or session_version < 0
    ):
        raise AuthError("authentication_required", 401)
    return {
        "user_id": user_id,
        "session_version": session_version,
        "issued_at": _payload_timestamp(payload.get("issued_at")),
        "last_activity_at": _payload_timestamp(
            payload.get("last_activity_at")
        ),
    }


def _payload_timestamp(value) -> datetime:
    if not isinstance(value, str) or not value:
        raise AuthError("authentication_required", 401)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise AuthError("authentication_required", 401) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthError("authentication_required", 401)
    return parsed


def _stored_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise _invalid_credentials() from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _invalid_credentials()
    return parsed


def _aware_now(value) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("now_invalid")
    return value


def _invalid_credentials() -> AuthError:
    return AuthError("invalid_credentials", 401)


def _store_auth_error(error: ValueError) -> AuthError:
    code = str(error)
    if code == "username_exists":
        return AuthError("username_exists", 409)
    if code == "user_not_found":
        return AuthError("user_not_found", 404)
    return AuthError("invalid_request", 400)


__all__ = [
    "AuthError",
    "AuthService",
    "new_csrf_token",
]
