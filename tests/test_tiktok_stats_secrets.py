from __future__ import annotations

from datetime import datetime, timezone
import json
import multiprocessing
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tiktok_stats.secrets import CookieSecretStore, redact_secrets


class FakeProtector:
    """Deterministic, non-production protector for encrypted-store behavior tests."""

    def protect(self, value: bytes) -> bytes:
        return b"fake-protected:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        prefix = b"fake-protected:"
        if not value.startswith(prefix):
            raise ValueError("invalid fake ciphertext")
        return value[len(prefix) :][::-1]


def _cookie_value() -> str:
    return "session" + "id=" + "test-value-" + ("x" * 12)


def _paused_validation_process(path, payload_read, allow_write, result_queue):
    store = CookieSecretStore(path, protector=FakeProtector())
    store.load_cookie()
    original_read = store._read_payload

    def paused_read():
        payload = original_read()
        payload_read.set()
        if not allow_write.wait(10):
            raise TimeoutError("test did not release validation write")
        return payload

    store._read_payload = paused_read
    try:
        result_queue.put(
            store.mark_validation(
                True, "ok", datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)
            )
        )
    except BaseException as error:
        result_queue.put((type(error).__name__, str(error)))


def _save_cookie_process(path, cookie, save_done, result_queue):
    try:
        CookieSecretStore(path, protector=FakeProtector()).save_cookie(cookie)
        result_queue.put(True)
    except BaseException as error:
        result_queue.put((type(error).__name__, str(error)))
    finally:
        save_done.set()


def test_round_trip_persists_only_protected_bytes_and_public_metadata(tmp_path):
    path = tmp_path / "cookie-secret.json"
    cookie = _cookie_value()
    store = CookieSecretStore(path, protector=FakeProtector())

    saved = store.save_cookie(cookie)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert saved.configured is True
    assert store.load_cookie() == cookie
    assert payload["encrypted_cookie"].startswith("ZmFrZS1wcm90ZWN0ZWQ6")
    assert cookie not in path.read_text(encoding="utf-8")
    assert set(payload) == {"version", "encrypted_cookie", "validation"}
    assert set(store.public_status()) == {"configured", "state", "message", "checked_at"}
    assert cookie not in repr(store.public_status())


def test_missing_and_corrupt_secrets_fail_closed_without_returning_a_cookie(tmp_path):
    path = tmp_path / "cookie-secret.json"
    store = CookieSecretStore(path, protector=FakeProtector())

    assert store.load_cookie() == ""
    assert store.public_status()["state"] == "missing"

    path.write_text("not-json", encoding="utf-8")
    assert store.load_cookie() == ""
    assert store.public_status()["configured"] is False
    assert store.public_status()["state"] == "corrupt"


def test_public_status_marks_malformed_ciphertext_as_corrupt_without_loading_it(tmp_path):
    path = tmp_path / "cookie-secret.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "encrypted_cookie": "not base64!",
                "validation": {"valid": None, "message": None, "checked_at": None},
            }
        ),
        encoding="utf-8",
    )

    status = CookieSecretStore(path, protector=FakeProtector()).public_status()

    assert status["configured"] is False
    assert status["state"] == "corrupt"


def test_save_replaces_secret_atomically_and_attempts_private_permissions(tmp_path, monkeypatch):
    path = tmp_path / "cookie-secret.json"
    store = CookieSecretStore(path, protector=FakeProtector())
    first_cookie = _cookie_value()
    second_cookie = first_cookie + "-replacement"
    chmod_calls: list[tuple[object, int]] = []
    replace_calls: list[tuple[object, object]] = []
    real_chmod = os.chmod
    real_replace = os.replace

    def recording_chmod(target, mode):
        chmod_calls.append((target, mode))
        return real_chmod(target, mode)

    def recording_replace(source, target):
        replace_calls.append((source, target))
        return real_replace(source, target)

    monkeypatch.setattr("tiktok_stats.secrets.os.chmod", recording_chmod)
    monkeypatch.setattr("tiktok_stats.secrets.os.replace", recording_replace)

    store.save_cookie(first_cookie)
    store.save_cookie(second_cookie)

    assert store.load_cookie() == second_cookie
    assert len(replace_calls) == 2
    assert all(os.fspath(source) != os.fspath(target) for source, target in replace_calls)
    assert any(mode == 0o600 for _, mode in chmod_calls)


def test_failed_replacement_keeps_prior_secret_usable_and_does_not_report_new_save(tmp_path, monkeypatch):
    path = tmp_path / "cookie-secret.json"
    store = CookieSecretStore(path, protector=FakeProtector())
    first_cookie = _cookie_value()
    store.save_cookie(first_cookie)

    def replacement_failure(source, target):
        raise OSError("replacement failed")

    monkeypatch.setattr("tiktok_stats.secrets.os.replace", replacement_failure)

    with pytest.raises(OSError, match="replacement failed"):
        store.save_cookie(first_cookie + "-new")

    assert store.load_cookie() == first_cookie
    assert store.public_status()["configured"] is True


def test_validation_metadata_and_recursive_redaction_never_expose_credentials(tmp_path):
    path = tmp_path / "cookie-secret.json"
    store = CookieSecretStore(path, protector=FakeProtector())
    cookie = _cookie_value()
    store.save_cookie(cookie)
    store.mark_validation(
        False,
        "Cookie: " + cookie + "; Authorization: bearer-value; token=sample-token",
        datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc),
    )

    status = store.public_status()
    redacted = redact_secrets(
        {
            "headers": {"Cookie": cookie, "Authorization": "bearer-value"},
            "nested": [{"access_token": "sample-token"}, {"proxy_password": "secret"}],
            "session_id": "session-value",
            "safe": "visible",
        }
    )

    assert status["checked_at"] == "2026-07-22T01:02:03Z"
    assert cookie not in repr(status)
    assert "bearer-value" not in repr(status)
    assert redacted == {
        "headers": {"Cookie": "[REDACTED]", "Authorization": "[REDACTED]"},
        "nested": [{"access_token": "[REDACTED]"}, {"proxy_password": "[REDACTED]"}],
        "session_id": "[REDACTED]",
        "safe": "visible",
    }


def test_validation_discards_a_bare_credential_message_from_disk_and_public_status(tmp_path):
    path = tmp_path / "cookie-secret.json"
    store = CookieSecretStore(path, protector=FakeProtector())
    store.save_cookie(_cookie_value())
    bare_credential = "unlabelled-credential-" + uuid4().hex

    store.mark_validation(
        False,
        bare_credential,
        datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc),
    )

    status = store.public_status()
    assert bare_credential not in path.read_text(encoding="utf-8")
    assert bare_credential not in repr(status)
    assert status == {
        "configured": True,
        "state": "invalid",
        "message": "Cookie validation failed",
        "checked_at": "2026-07-22T01:02:03Z",
    }


def test_public_status_discards_a_legacy_bare_validation_message(tmp_path):
    path = tmp_path / "cookie-secret.json"
    store = CookieSecretStore(path, protector=FakeProtector())
    store.save_cookie(_cookie_value())
    bare_credential = "legacy-credential-" + uuid4().hex
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["validation"] = {
        "valid": False,
        "message": bare_credential,
        "checked_at": "2026-07-22T01:02:03Z",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    status = CookieSecretStore(path, protector=FakeProtector()).public_status()

    assert bare_credential not in repr(status)
    assert status["state"] == "invalid"
    assert status["message"] == "Cookie validation failed"


def test_same_path_instances_serialize_validation_read_modify_write_and_cookie_save(tmp_path):
    path = tmp_path / "cookie-secret.json"
    validating_store = CookieSecretStore(path, protector=FakeProtector())
    replacing_store = CookieSecretStore(path.resolve(), protector=FakeProtector())
    old_cookie = _cookie_value() + "-old"
    new_cookie = _cookie_value() + "-new"
    validating_store.save_cookie(old_cookie)

    validation_read = threading.Event()
    allow_validation_write = threading.Event()
    replacement_done = threading.Event()
    original_read = validating_store._read_payload

    def paused_read():
        payload = original_read()
        validation_read.set()
        assert allow_validation_write.wait(5)
        return payload

    validating_store._read_payload = paused_read
    validation_thread = threading.Thread(
        target=lambda: validating_store.mark_validation(
            True, "ok", datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)
        )
    )
    replacement_thread = threading.Thread(
        target=lambda: (
            replacing_store.save_cookie(new_cookie), replacement_done.set()
        )
    )
    validation_thread.start()
    assert validation_read.wait(5)
    replacement_thread.start()
    try:
        assert not replacement_done.wait(0.2)
    finally:
        allow_validation_write.set()
        validation_thread.join(5)
        replacement_thread.join(5)

    assert not validation_thread.is_alive()
    assert not replacement_thread.is_alive()
    assert replacing_store.load_cookie() == new_cookie
    assert replacing_store.public_status()["state"] == "configured"


def test_same_path_processes_serialize_validation_read_modify_write_and_cookie_save(tmp_path):
    path = tmp_path / "cookie-secret.json"
    old_cookie = _cookie_value() + "-process-old"
    new_cookie = _cookie_value() + "-process-new"
    CookieSecretStore(path, protector=FakeProtector()).save_cookie(old_cookie)
    context = multiprocessing.get_context("spawn")
    payload_read = context.Event()
    allow_validation_write = context.Event()
    save_done = context.Event()
    validation_result = context.Queue()
    save_result = context.Queue()
    validation = context.Process(
        target=_paused_validation_process,
        args=(path, payload_read, allow_validation_write, validation_result),
    )
    replacement = context.Process(
        target=_save_cookie_process,
        args=(path, new_cookie, save_done, save_result),
    )

    validation.start()
    assert payload_read.wait(10)
    replacement.start()
    replacement_was_blocked = not save_done.wait(0.5)
    allow_validation_write.set()
    validation.join(10)
    replacement.join(10)
    if validation.is_alive():
        validation.terminate()
        validation.join(5)
    if replacement.is_alive():
        replacement.terminate()
        replacement.join(5)

    assert replacement_was_blocked
    assert validation.exitcode == 0
    assert replacement.exitcode == 0
    assert validation_result.get(timeout=2) is True
    assert save_result.get(timeout=2) is True
    current = CookieSecretStore(path, protector=FakeProtector())
    assert current.load_cookie() == new_cookie
    assert current.public_status()["state"] == "configured"
    lock_path = path.with_name(path.name + ".lock")
    assert lock_path.exists()
    assert old_cookie not in lock_path.read_text(encoding="utf-8", errors="ignore")
    assert new_cookie not in lock_path.read_text(encoding="utf-8", errors="ignore")


def test_public_status_reloads_disk_when_another_instance_replaces_cookie(tmp_path):
    path = tmp_path / "cookie-secret.json"
    first = CookieSecretStore(path, protector=FakeProtector())
    second = CookieSecretStore(path.resolve(), protector=FakeProtector())
    first.save_cookie(_cookie_value() + "-first")
    first.mark_validation(
        True, "ok", datetime(2026, 7, 22, 1, 2, 3, tzinfo=timezone.utc)
    )
    assert first.public_status()["state"] == "valid"

    second.save_cookie(_cookie_value() + "-second")

    assert first.public_status()["state"] == "configured"


def test_runtime_cookie_and_lock_files_are_ignored():
    ignore_text = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert "data/stats/tiktok_cookie.json*" in ignore_text
    assert "data/tiktok_stats/cookie-secret.json*" in ignore_text


def test_windows_dpapi_adapter_uses_only_the_binary_ciphertext(monkeypatch):
    calls = []
    fake_module = SimpleNamespace(
        CRYPTPROTECT_UI_FORBIDDEN=1,
        CryptProtectData=lambda value, *_args: ("description", b"ciphertext"),
        CryptUnprotectData=lambda value, *_args: ("description", b"plaintext"),
    )
    monkeypatch.setitem(sys.modules, "win32crypt", fake_module)

    from tiktok_stats.secrets import _WindowsDpapiProtector

    protector = _WindowsDpapiProtector()
    calls.append(protector.protect(b"input"))
    calls.append(protector.unprotect(b"ciphertext"))

    assert calls == [b"ciphertext", b"plaintext"]
