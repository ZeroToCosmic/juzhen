"""Durable management users and same-transaction audit events."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Mapping


SCHEMA = """
CREATE TABLE IF NOT EXISTS management_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('administrator', 'operator')),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    must_change_password INTEGER NOT NULL DEFAULT 1 CHECK (must_change_password IN (0, 1)),
    session_version INTEGER NOT NULL DEFAULT 1,
    failed_attempt_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    last_login_at TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS management_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES management_users(id),
    event_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_management_audit_created
ON management_audit_events(created_at DESC, id DESC);
"""

_SENSITIVE_MARKERS = (
    "authorization",
    "cookie",
    "credential",
    "csrf",
    "hash",
    "password",
    "secret",
    "token",
)
_SAFE_TEXT = re.compile(r"^[^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*$")


def open_management_db(path: Path) -> sqlite3.Connection:
    database_path = Path(path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        database_path,
        timeout=5,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.executescript(SCHEMA)
        return connection
    except BaseException:
        connection.close()
        raise


def record_management_audit(
    connection: sqlite3.Connection,
    *,
    actor_user_id,
    event_type,
    target_type,
    target_id,
    result,
    reason,
    details,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO management_audit_events (
            actor_user_id,
            event_type,
            target_type,
            target_id,
            result,
            reason,
            details_json,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _optional_user_id(actor_user_id),
            _text(event_type, "event_type"),
            _text(target_type, "target_type"),
            _optional_text(target_id, "target_id"),
            _text(result, "result"),
            _optional_text(reason, "reason"),
            _audit_json(details),
            datetime.now(UTC).isoformat(),
        ),
    )
    return int(cursor.lastrowid)


def _audit_json(value: object) -> str:
    if not isinstance(value, Mapping):
        raise ValueError("audit_details_invalid")
    sanitized = _sanitize_json(dict(value))
    try:
        return json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("audit_details_invalid") from error


def _sanitize_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("audit_details_invalid")
        return value
    if isinstance(value, str):
        if not _SAFE_TEXT.fullmatch(value):
            raise ValueError("audit_details_invalid")
        normalized = _normalized_marker(value)
        if any(marker in normalized for marker in _SENSITIVE_MARKERS):
            return "[redacted]"
        return value
    if isinstance(value, list):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, Mapping):
        sanitized = {}
        for key, item in value.items():
            if not isinstance(key, str) or not _SAFE_TEXT.fullmatch(key):
                raise ValueError("audit_details_invalid")
            normalized = _normalized_marker(key)
            if any(marker in normalized for marker in _SENSITIVE_MARKERS):
                continue
            sanitized[key] = _sanitize_json(item)
        return sanitized
    raise ValueError("audit_details_invalid")


def _normalized_marker(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or not _SAFE_TEXT.fullmatch(value)
    ):
        raise ValueError(f"{name}_invalid")
    return value


def _optional_text(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or len(value) > 256
        or not _SAFE_TEXT.fullmatch(value)
    ):
        raise ValueError(f"{name}_invalid")
    return value


def _optional_user_id(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("actor_user_id_invalid")
    return value


__all__ = [
    "open_management_db",
    "record_management_audit",
]
