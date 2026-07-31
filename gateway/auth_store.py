"""Transactional management-user persistence and access invariants."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
import sqlite3

from gateway.management_db import record_management_audit


ALLOWED_ROLES = frozenset({"administrator", "operator"})
MAX_FAILURES = 5
LOCK_MINUTES = 15
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class LastAdministratorError(RuntimeError):
    code = "last_administrator"


@dataclass(frozen=True)
class ManagementUser:
    id: int
    username: str
    password_hash: str
    role: str
    enabled: bool
    must_change_password: bool
    session_version: int
    failed_attempt_count: int
    locked_until: str | None
    last_login_at: str | None
    password_changed_at: str
    created_at: str
    updated_at: str


class AuthStore:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_user(
        self,
        username,
        password_hash,
        role,
        must_change_password=True,
        *,
        actor_user_id=None,
        now=None,
    ) -> ManagementUser:
        username_value = _username(username)
        hash_value = _password_hash(password_hash)
        role_value = _role(role)
        must_change = _boolean(
            must_change_password,
            "must_change_password",
        )
        actor_id = (
            _user_id(actor_user_id)
            if actor_user_id is not None
            else None
        )
        now_value = _timestamp(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """
                INSERT INTO management_users (
                    username,
                    password_hash,
                    role,
                    must_change_password,
                    password_changed_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    username_value,
                    hash_value,
                    role_value,
                    int(must_change),
                    now_value,
                    now_value,
                    now_value,
                ),
            )
            user_id = int(cursor.lastrowid)
            record_management_audit(
                self.connection,
                actor_user_id=actor_id,
                event_type="user_created",
                target_type="management_user",
                target_id=str(user_id),
                result="success",
                reason="",
                details={
                    "role": role_value,
                    "must_change": must_change,
                },
            )
            user = self._required_user(user_id)
            self.connection.commit()
            return user
        except sqlite3.IntegrityError as error:
            self.connection.rollback()
            if _username_exists(self.connection, username_value):
                raise ValueError("username_exists") from None
            raise error
        except BaseException:
            self.connection.rollback()
            raise

    def get_by_id(self, user_id) -> ManagementUser | None:
        row = self.connection.execute(
            "SELECT * FROM management_users WHERE id = ?",
            (_user_id(user_id),),
        ).fetchone()
        return _user(row) if row is not None else None

    def get_by_username(self, username) -> ManagementUser | None:
        row = self.connection.execute(
            "SELECT * FROM management_users WHERE username = ? COLLATE NOCASE",
            (_username(username),),
        ).fetchone()
        return _user(row) if row is not None else None

    def list_users(self) -> tuple[ManagementUser, ...]:
        rows = self.connection.execute(
            "SELECT * FROM management_users ORDER BY id"
        ).fetchall()
        return tuple(_user(row) for row in rows)

    def update_access(
        self,
        user_id,
        *,
        role=None,
        enabled=None,
        actor_user_id,
    ) -> ManagementUser:
        target_id = _user_id(user_id)
        actor_id = _user_id(actor_user_id)
        role_value = _role(role) if role is not None else None
        enabled_value = (
            _boolean(enabled, "enabled") if enabled is not None else None
        )
        if role_value is None and enabled_value is None:
            raise ValueError("access_patch_empty")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._required_user(target_id)
            next_role = role_value if role_value is not None else current.role
            next_enabled = (
                enabled_value if enabled_value is not None else current.enabled
            )
            if (
                current.role == "administrator"
                and current.enabled
                and (
                    next_role != "administrator"
                    or next_enabled is False
                )
                and self._enabled_administrator_count() == 1
            ):
                raise LastAdministratorError("last_administrator")
            changed = (
                next_role != current.role
                or next_enabled != current.enabled
            )
            now = datetime.now(UTC).isoformat()
            if changed:
                self.connection.execute(
                    """
                    UPDATE management_users
                    SET role = ?,
                        enabled = ?,
                        session_version = session_version + 1,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (next_role, int(next_enabled), now, target_id),
                )
            record_management_audit(
                self.connection,
                actor_user_id=actor_id,
                event_type="user_access_updated",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={
                    "role": next_role,
                    "enabled": next_enabled,
                    "changed": changed,
                },
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def update_access_if_revision(
        self,
        user_id,
        *,
        expected_revision,
        role=None,
        enabled=None,
        actor_user_id,
    ) -> ManagementUser | None:
        target_id = _user_id(user_id)
        revision = _session_version(expected_revision)
        actor_id = _user_id(actor_user_id)
        role_value = _role(role) if role is not None else None
        enabled_value = (
            _boolean(enabled, "enabled") if enabled is not None else None
        )
        if role_value is None and enabled_value is None:
            raise ValueError("access_patch_empty")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._required_user(target_id)
            if current.session_version != revision:
                self.connection.rollback()
                return None
            next_role = role_value if role_value is not None else current.role
            next_enabled = (
                enabled_value if enabled_value is not None else current.enabled
            )
            if (
                current.role == "administrator"
                and current.enabled
                and (
                    next_role != "administrator"
                    or next_enabled is False
                )
                and self._enabled_administrator_count() == 1
            ):
                raise LastAdministratorError("last_administrator")
            changed = (
                next_role != current.role
                or next_enabled != current.enabled
            )
            now = datetime.now(UTC).isoformat()
            self.connection.execute(
                """
                UPDATE management_users
                SET role = ?,
                    enabled = ?,
                    session_version = session_version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (next_role, int(next_enabled), now, target_id),
            )
            record_management_audit(
                self.connection,
                actor_user_id=actor_id,
                event_type="user_access_updated",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={
                    "role": next_role,
                    "enabled": next_enabled,
                    "changed": changed,
                },
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def replace_password(
        self,
        user_id,
        password_hash,
        *,
        must_change_password,
        actor_user_id,
        now=None,
    ) -> ManagementUser:
        target_id = _user_id(user_id)
        actor_id = _user_id(actor_user_id)
        hash_value = _password_hash(password_hash)
        must_change = _boolean(
            must_change_password,
            "must_change_password",
        )
        now_value = _timestamp(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._required_user(target_id)
            self.connection.execute(
                """
                UPDATE management_users
                SET password_hash = ?,
                    must_change_password = ?,
                    session_version = session_version + 1,
                    failed_attempt_count = 0,
                    locked_until = NULL,
                    password_changed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    hash_value,
                    int(must_change),
                    now_value,
                    now_value,
                    target_id,
                ),
            )
            record_management_audit(
                self.connection,
                actor_user_id=actor_id,
                event_type="user_password_replaced",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={"must_change": must_change},
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def replace_password_if_current(
        self,
        user_id,
        expected_password_hash,
        expected_session_version,
        password_hash,
        *,
        must_change_password,
        actor_user_id,
        now,
    ) -> ManagementUser | None:
        target_id = _user_id(user_id)
        expected_hash = _password_hash(expected_password_hash)
        expected_version = _session_version(expected_session_version)
        hash_value = _password_hash(password_hash)
        must_change = _boolean(
            must_change_password,
            "must_change_password",
        )
        actor_id = _user_id(actor_user_id)
        now_value = _timestamp(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._required_user(target_id)
            if (
                not current.enabled
                or current.password_hash != expected_hash
                or current.session_version != expected_version
            ):
                self.connection.rollback()
                return None
            self.connection.execute(
                """
                UPDATE management_users
                SET password_hash = ?,
                    must_change_password = ?,
                    session_version = session_version + 1,
                    failed_attempt_count = 0,
                    locked_until = NULL,
                    password_changed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    hash_value,
                    int(must_change),
                    now_value,
                    now_value,
                    target_id,
                ),
            )
            record_management_audit(
                self.connection,
                actor_user_id=actor_id,
                event_type="user_password_replaced",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={"must_change": must_change},
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def record_login_failure(self, user_id, now) -> ManagementUser:
        target_id = _user_id(user_id)
        now_value = _aware_datetime(now, "now")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._required_user(target_id)
            lock_deadline = _stored_datetime(current.locked_until)
            lock_expired = (
                lock_deadline is not None
                and now_value >= lock_deadline
            )
            failed_count = (
                1
                if lock_expired
                else current.failed_attempt_count + 1
            )
            locked_until = (
                (now_value + timedelta(minutes=LOCK_MINUTES)).isoformat()
                if failed_count >= MAX_FAILURES
                else None if lock_expired else current.locked_until
            )
            self.connection.execute(
                """
                UPDATE management_users
                SET failed_attempt_count = ?,
                    locked_until = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    failed_count,
                    locked_until,
                    now_value.isoformat(),
                    target_id,
                ),
            )
            record_management_audit(
                self.connection,
                actor_user_id=target_id,
                event_type="login_failed",
                target_type="management_user",
                target_id=str(target_id),
                result="failed",
                reason="invalid_credentials",
                details={
                    "failed_attempt_count": failed_count,
                    "locked": locked_until is not None,
                },
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def record_login_failure_if_current(
        self,
        user_id,
        expected_password_hash,
        expected_session_version,
        now,
    ) -> ManagementUser | None:
        target_id = _user_id(user_id)
        expected_hash = _password_hash(expected_password_hash)
        expected_version = _session_version(expected_session_version)
        now_value = _aware_datetime(now, "now")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._required_user(target_id)
            if (
                not current.enabled
                or current.password_hash != expected_hash
                or current.session_version != expected_version
            ):
                self.connection.rollback()
                return None
            lock_deadline = _stored_datetime(current.locked_until)
            if (
                lock_deadline is not None
                and now_value < lock_deadline
            ):
                self.connection.rollback()
                return None
            lock_expired = (
                lock_deadline is not None
                and now_value >= lock_deadline
            )
            failed_count = (
                1
                if lock_expired
                else current.failed_attempt_count + 1
            )
            locked_until = (
                (now_value + timedelta(minutes=LOCK_MINUTES)).isoformat()
                if failed_count >= MAX_FAILURES
                else None if lock_expired else current.locked_until
            )
            self.connection.execute(
                """
                UPDATE management_users
                SET failed_attempt_count = ?,
                    locked_until = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    failed_count,
                    locked_until,
                    now_value.isoformat(),
                    target_id,
                ),
            )
            record_management_audit(
                self.connection,
                actor_user_id=target_id,
                event_type="login_failed",
                target_type="management_user",
                target_id=str(target_id),
                result="failed",
                reason="invalid_credentials",
                details={
                    "failed_attempt_count": failed_count,
                    "locked": locked_until is not None,
                },
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def record_login_success(self, user_id, now) -> ManagementUser:
        target_id = _user_id(user_id)
        now_value = _aware_datetime(now, "now")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._required_user(target_id)
            self.connection.execute(
                """
                UPDATE management_users
                SET failed_attempt_count = 0,
                    locked_until = NULL,
                    last_login_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_value.isoformat(), now_value.isoformat(), target_id),
            )
            record_management_audit(
                self.connection,
                actor_user_id=target_id,
                event_type="login_succeeded",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={},
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def record_login_success_if_current(
        self,
        user_id,
        expected_password_hash,
        expected_session_version,
        now,
    ) -> ManagementUser | None:
        target_id = _user_id(user_id)
        expected_hash = _password_hash(expected_password_hash)
        expected_version = _session_version(expected_session_version)
        now_value = _aware_datetime(now, "now")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self._required_user(target_id)
            lock_deadline = _stored_datetime(current.locked_until)
            if (
                not current.enabled
                or current.password_hash != expected_hash
                or current.session_version != expected_version
                or (
                    lock_deadline is not None
                    and now_value < lock_deadline
                )
            ):
                self.connection.rollback()
                return None
            self.connection.execute(
                """
                UPDATE management_users
                SET failed_attempt_count = 0,
                    locked_until = NULL,
                    last_login_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_value.isoformat(), now_value.isoformat(), target_id),
            )
            record_management_audit(
                self.connection,
                actor_user_id=target_id,
                event_type="login_succeeded",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={},
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def revoke_sessions(
        self,
        user_id,
        *,
        actor_user_id,
        now,
    ) -> ManagementUser:
        target_id = _user_id(user_id)
        actor_id = _user_id(actor_user_id)
        now_value = _timestamp(now)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self._required_user(target_id)
            self.connection.execute(
                """
                UPDATE management_users
                SET session_version = session_version + 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_value, target_id),
            )
            record_management_audit(
                self.connection,
                actor_user_id=actor_id,
                event_type="user_sessions_revoked",
                target_type="management_user",
                target_id=str(target_id),
                result="success",
                reason="",
                details={},
            )
            updated = self._required_user(target_id)
            self.connection.commit()
            return updated
        except BaseException:
            self.connection.rollback()
            raise

    def _required_user(self, user_id: int) -> ManagementUser:
        row = self.connection.execute(
            "SELECT * FROM management_users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            raise ValueError("user_not_found")
        return _user(row)

    def _enabled_administrator_count(self) -> int:
        return int(
            self.connection.execute(
                """
                SELECT COUNT(*)
                FROM management_users
                WHERE role = 'administrator' AND enabled = 1
                """
            ).fetchone()[0]
        )


def _user(row: sqlite3.Row) -> ManagementUser:
    return ManagementUser(
        id=int(row["id"]),
        username=str(row["username"]),
        password_hash=str(row["password_hash"]),
        role=str(row["role"]),
        enabled=bool(row["enabled"]),
        must_change_password=bool(row["must_change_password"]),
        session_version=int(row["session_version"]),
        failed_attempt_count=int(row["failed_attempt_count"]),
        locked_until=row["locked_until"],
        last_login_at=row["last_login_at"],
        password_changed_at=str(row["password_changed_at"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _username(value: object) -> str:
    if not isinstance(value, str) or not _USERNAME_PATTERN.fullmatch(value):
        raise ValueError("username_invalid")
    return value


def _password_hash(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("password_hash_invalid")
    return value


def _role(value: object) -> str:
    if value not in ALLOWED_ROLES:
        raise ValueError("role_invalid")
    return str(value)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name}_invalid")
    return value


def _user_id(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("user_id_invalid")
    return value


def _session_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("session_version_invalid")
    return value


def _aware_datetime(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name}_invalid")
    return value


def _timestamp(value: object) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    return _aware_datetime(value, "now").isoformat()


def _stored_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("stored_datetime_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored_datetime_invalid")
    return parsed


def _username_exists(connection: sqlite3.Connection, username: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM management_users
            WHERE username = ? COLLATE NOCASE
            LIMIT 1
            """,
            (username,),
        ).fetchone()
        is not None
    )


__all__ = [
    "AuthStore",
    "LastAdministratorError",
    "ManagementUser",
]
