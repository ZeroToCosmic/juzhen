"""Local, DPAPI-protected storage for TikTok collection credentials."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping, Protocol


_REDACTED = "[REDACTED]"
_SENSITIVE_FIELD = re.compile(
    r"(?:cookie|authorization|token|session|proxy(?:[_-]?(?:credential|password|username|auth))?)",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"\b(cookie|authorization|(?:access[_-]?)?token|session(?:[_-]?\w+)?|"
    r"proxy[_-]?(?:credential|password|username|auth))\s*([:=])\s*([^\s,;]+)",
    re.IGNORECASE,
)
_PATH_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


class SecretProtectionUnavailable(RuntimeError):
    """Raised when current-user Windows DPAPI cannot protect a secret."""


class Protector(Protocol):
    def protect(self, value: bytes) -> bytes: ...

    def unprotect(self, value: bytes) -> bytes: ...


@dataclass(frozen=True)
class CookieStatus:
    configured: bool
    state: str
    message: str | None = None
    checked_at: str | None = None

    def as_public_dict(self) -> dict[str, str | bool | None]:
        return {
            "configured": self.configured,
            "state": self.state,
            "message": self.message,
            "checked_at": self.checked_at,
        }


class CookieSecretStore:
    """Persist a Cookie only as current-user DPAPI ciphertext and safe metadata."""

    def __init__(self, path: str | os.PathLike[str], protector: Protector | None = None):
        self.path = Path(path)
        self._protector = protector
        self._last_status: CookieStatus | None = None
        self._loaded_version: str | None = None
        self._lock = _lock_for_path(self.path)
        self._lock_path = self.path.with_name(self.path.name + ".lock")

    def save_cookie(self, plaintext: str) -> CookieStatus:
        if not isinstance(plaintext, str) or not plaintext:
            raise ValueError("Cookie must be a non-empty string")

        with self._critical_section():
            encrypted = self._get_protector().protect(plaintext.encode("utf-8"))
            if not isinstance(encrypted, bytes) or not encrypted:
                raise ValueError("Secret protector returned invalid ciphertext")

            payload = {
                "version": 1,
                "encrypted_cookie": base64.b64encode(encrypted).decode("ascii"),
                "validation": {"valid": None, "message": None, "checked_at": None},
            }
            self._write_payload(payload)
            status = CookieStatus(configured=True, state="configured")
            self._last_status = status
            self._loaded_version = _secret_version(payload)
            return status

    def load_cookie(self) -> str:
        return self.load_cookie_with_version()[0]

    def load_cookie_with_version(self) -> tuple[str, str | None]:
        """Load plaintext and its non-secret ciphertext fingerprint as one operation."""
        with self._critical_section():
            payload = self._read_payload()
            if payload is None:
                self._loaded_version = None
                return "", None

            try:
                encrypted = base64.b64decode(payload["encrypted_cookie"], validate=True)
                plaintext = self._get_protector().unprotect(encrypted).decode("utf-8")
                if not plaintext:
                    raise ValueError("empty plaintext")
            except SecretProtectionUnavailable:
                self._last_status = CookieStatus(configured=True, state="unavailable")
                self._loaded_version = None
                return "", None
            except (KeyError, TypeError, ValueError, UnicodeDecodeError):
                self._last_status = CookieStatus(configured=False, state="corrupt")
                self._loaded_version = None
                return "", None
            except Exception:
                self._last_status = CookieStatus(configured=False, state="corrupt")
                self._loaded_version = None
                return "", None

            self._last_status = self._status_from_payload(payload)
            self._loaded_version = _secret_version(payload)
            return plaintext, self._loaded_version

    def public_status(self) -> dict[str, str | bool | None]:
        with self._critical_section():
            payload = self._read_payload()
            if payload is None:
                return self._last_status.as_public_dict()
            self._last_status = self._status_from_payload(payload)
            return self._last_status.as_public_dict()

    def mark_validation(
        self,
        valid: bool,
        message: str,
        checked_at: datetime,
        *,
        expected_version: str | None = None,
    ) -> bool:
        if not isinstance(valid, bool):
            raise ValueError("Validation result must be a boolean")
        if checked_at.tzinfo is None:
            raise ValueError("Validation timestamp must be timezone-aware")
        # Validation failures can contain upstream request details. Persist only a
        # fixed public result so even an unlabelled credential cannot escape.
        del message

        with self._critical_section():
            payload = self._read_payload()
            if payload is None:
                raise FileNotFoundError("No encrypted Cookie has been configured")
            target_version = expected_version or self._loaded_version
            if target_version is None or _secret_version(payload) != target_version:
                self._last_status = self._status_from_payload(payload)
                return False
            payload["validation"] = {
                "valid": valid,
                "message": _public_validation_message(valid),
                "checked_at": _utc_iso(checked_at),
            }
            self._write_payload(payload)
            self._last_status = self._status_from_payload(payload)
            return True

    @contextmanager
    def _critical_section(self) -> Iterator[None]:
        with self._lock:
            with _exclusive_file_lock(self._lock_path):
                yield

    def _get_protector(self) -> Protector:
        if self._protector is not None:
            return self._protector
        if os.name != "nt":
            raise SecretProtectionUnavailable("Windows DPAPI is required to protect Cookies")
        try:
            return _WindowsDpapiProtector()
        except (ImportError, OSError) as error:
            raise SecretProtectionUnavailable("Windows DPAPI is unavailable") from error

    def _read_payload(self) -> dict[str, Any] | None:
        if not self.path.exists():
            self._last_status = CookieStatus(configured=False, state="missing")
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Secret payload is not an object")
            if payload.get("version") != 1 or not isinstance(payload.get("encrypted_cookie"), str):
                raise ValueError("Secret payload has an invalid schema")
            if not base64.b64decode(payload["encrypted_cookie"], validate=True):
                raise ValueError("Secret payload has empty ciphertext")
            if not isinstance(payload.get("validation"), dict):
                raise ValueError("Secret payload has invalid validation metadata")
            return payload
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            self._last_status = CookieStatus(configured=False, state="corrupt")
            return None

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, prefix=f".{self.path.name}.", delete=False
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            _attempt_private_permissions(temp_path)
            os.replace(temp_path, self.path)
            _attempt_private_permissions(self.path)
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    @staticmethod
    def _status_from_payload(payload: Mapping[str, Any]) -> CookieStatus:
        validation = payload.get("validation", {})
        valid = validation.get("valid")
        if valid is True:
            state = "valid"
        elif valid is False:
            state = "invalid"
        else:
            state = "configured"
        checked_at = validation.get("checked_at")
        return CookieStatus(
            configured=True,
            state=state,
            message=_public_validation_message(valid) if isinstance(valid, bool) else None,
            checked_at=checked_at if isinstance(checked_at, str) else None,
        )


class _WindowsDpapiProtector:
    def __init__(self) -> None:
        import win32crypt

        self._win32crypt = win32crypt

    def protect(self, value: bytes) -> bytes:
        return self._win32crypt.CryptProtectData(
            value, None, None, None, None, self._win32crypt.CRYPTPROTECT_UI_FORBIDDEN
        )[1]

    def unprotect(self, value: bytes) -> bytes:
        return self._win32crypt.CryptUnprotectData(
            value, None, None, None, self._win32crypt.CRYPTPROTECT_UI_FORBIDDEN
        )[1]


def redact_secrets(value: Any) -> Any:
    """Return a recursively redacted copy suitable for logs and error samples."""
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _is_sensitive_field(key) else redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, set):
        return {redact_secrets(item) for item in value}
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(value: str) -> str:
    return _SENSITIVE_TEXT.sub(lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", value)


@contextmanager
def _exclusive_file_lock(path: Path, timeout_seconds: float = 10.0) -> Iterator[None]:
    """Hold one cross-platform, cross-process exclusive lock byte."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b", buffering=0) as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            os.fsync(handle.fileno())
        _attempt_private_permissions(path)
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _acquire_lock_byte(handle)
                break
            except OSError as error:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for credential file lock") from error
                time.sleep(0.01)
        try:
            yield
        finally:
            _release_lock_byte(handle)


def _acquire_lock_byte(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_lock_byte(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _lock_for_path(path: Path) -> threading.RLock:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(normalized)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[normalized] = lock
        return lock


def _secret_version(payload: Mapping[str, Any]) -> str:
    ciphertext = str(payload["encrypted_cookie"]).encode("ascii")
    return hashlib.sha256(ciphertext).hexdigest()


def _is_sensitive_field(key: object) -> bool:
    return isinstance(key, str) and bool(_SENSITIVE_FIELD.search(key))


def _attempt_private_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _public_validation_message(valid: bool) -> str:
    return "Cookie validation succeeded" if valid else "Cookie validation failed"


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
