import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from gateway import auth_service
from gateway.auth_service import AuthError, AuthService, new_csrf_token
from gateway.auth_store import AuthStore
from gateway.management_db import open_management_db


PASSWORD = "valid password 123"
NOW = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def auth_context(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    store = AuthStore(connection)
    user = store.create_user(
        "ops",
        generate_password_hash(PASSWORD, method="scrypt"),
        "operator",
        must_change_password=False,
    )
    try:
        yield connection, AuthService(store), user
    finally:
        connection.close()


def test_five_failures_lock_for_fifteen_minutes(auth_context):
    _connection, service, user = auth_context

    for _index in range(5):
        with pytest.raises(AuthError) as caught:
            service.authenticate("ops", "wrong password", NOW)
        assert (caught.value.code, caught.value.status) == (
            "invalid_credentials",
            401,
        )

    locked = service.store.get_by_id(user.id)
    assert locked.failed_attempt_count == 5
    assert locked.locked_until == (
        NOW + timedelta(minutes=15)
    ).isoformat()


def test_inflight_failure_cannot_extend_an_active_lock(auth_context):
    connection, service, user = auth_context

    for _index in range(5):
        with pytest.raises(AuthError):
            service.authenticate("ops", "wrong password", NOW)

    stale_failure = service.store.record_login_failure_if_current(
        user.id,
        user.password_hash,
        user.session_version,
        NOW + timedelta(minutes=1),
    )

    locked = service.store.get_by_id(user.id)
    assert stale_failure is None
    assert locked.failed_attempt_count == 5
    assert locked.locked_until == (
        NOW + timedelta(minutes=15)
    ).isoformat()
    assert connection.execute(
        """
        SELECT COUNT(*) FROM management_audit_events
        WHERE event_type = 'login_failed'
        """
    ).fetchone()[0] == 5


def test_active_lock_uses_public_error_and_success_after_expiry_clears_it(
    auth_context,
):
    _connection, service, user = auth_context
    for _index in range(5):
        with pytest.raises(AuthError):
            service.authenticate("ops", "wrong", NOW)

    with pytest.raises(AuthError) as caught:
        service.authenticate(
            "ops",
            PASSWORD,
            NOW + timedelta(minutes=14, seconds=59),
        )
    assert caught.value.code == "invalid_credentials"
    assert service.store.get_by_id(user.id).failed_attempt_count == 5

    authenticated = service.authenticate(
        "ops",
        PASSWORD,
        NOW + timedelta(minutes=15),
    )
    assert authenticated.failed_attempt_count == 0
    assert authenticated.locked_until is None
    assert authenticated.last_login_at == (
        NOW + timedelta(minutes=15)
    ).isoformat()


def test_expired_lock_restarts_failure_count_before_locking_again(
    auth_context,
):
    _connection, service, user = auth_context
    for _index in range(5):
        with pytest.raises(AuthError):
            service.authenticate("ops", "wrong", NOW)

    after_expiry = NOW + timedelta(minutes=15)
    for expected_count in range(1, 5):
        with pytest.raises(AuthError):
            service.authenticate("ops", "wrong", after_expiry)
        current = service.store.get_by_id(user.id)
        assert current.failed_attempt_count == expected_count
        assert current.locked_until is None

    with pytest.raises(AuthError):
        service.authenticate("ops", "wrong", after_expiry)
    relocked = service.store.get_by_id(user.id)
    assert relocked.failed_attempt_count == 5
    assert relocked.locked_until == (
        after_expiry + timedelta(minutes=15)
    ).isoformat()


def test_unknown_and_disabled_users_run_dummy_scrypt_check(
    auth_context,
    monkeypatch,
):
    _connection, service, user = auth_context
    service.store.update_access(
        user.id,
        enabled=False,
        actor_user_id=user.id,
    )
    hashes = []
    original_check = auth_service.check_password_hash

    def recording_check(password_hash, password):
        hashes.append(password_hash)
        return original_check(password_hash, password)

    monkeypatch.setattr(
        auth_service,
        "check_password_hash",
        recording_check,
    )

    for username in ("missing", "ops"):
        with pytest.raises(AuthError) as caught:
            service.authenticate(username, PASSWORD, NOW)
        assert caught.value.code == "invalid_credentials"

    assert hashes == [auth_service.DUMMY_HASH, auth_service.DUMMY_HASH]
    assert auth_service.DUMMY_HASH.startswith("scrypt:")


def test_successful_login_clears_previous_failures(auth_context):
    _connection, service, user = auth_context
    for _index in range(2):
        with pytest.raises(AuthError):
            service.authenticate("ops", "wrong", NOW)

    authenticated = service.authenticate(" OPS ", PASSWORD, NOW)

    assert authenticated.id == user.id
    assert authenticated.failed_attempt_count == 0
    assert authenticated.last_login_at == NOW.isoformat()


@pytest.mark.parametrize("mutation", ("disable", "reset"))
def test_authenticate_cannot_succeed_after_concurrent_user_mutation(
    auth_context,
    tmp_path,
    monkeypatch,
    mutation,
):
    _connection, service, user = auth_context
    second_connection = open_management_db(tmp_path / "management.db")
    second_store = AuthStore(second_connection)
    original_check = auth_service.check_password_hash
    mutated = []

    def check_then_mutate(password_hash, password):
        result = original_check(password_hash, password)
        if result and not mutated:
            mutated.append(True)
            if mutation == "disable":
                second_store.update_access(
                    user.id,
                    enabled=False,
                    actor_user_id=user.id,
                )
            else:
                second_store.replace_password(
                    user.id,
                    generate_password_hash(
                        "concurrent reset password",
                        method="scrypt",
                    ),
                    must_change_password=True,
                    actor_user_id=user.id,
                    now=NOW,
                )
        return result

    monkeypatch.setattr(
        auth_service,
        "check_password_hash",
        check_then_mutate,
    )
    try:
        with pytest.raises(AuthError) as caught:
            service.authenticate("ops", PASSWORD, NOW)
        assert caught.value.code == "invalid_credentials"
    finally:
        second_connection.close()


def test_five_inflight_old_failures_do_not_lock_after_password_reset(
    auth_context,
    tmp_path,
    monkeypatch,
):
    connection, _service, user = auth_context
    all_checked = Barrier(6)
    release_checks = Barrier(6)
    original_check = auth_service.check_password_hash

    def pause_after_check(password_hash, password):
        result = original_check(password_hash, password)
        if password_hash == user.password_hash:
            all_checked.wait(timeout=10)
            release_checks.wait(timeout=10)
        return result

    monkeypatch.setattr(
        auth_service,
        "check_password_hash",
        pause_after_check,
    )

    def fail_login(_index):
        worker_connection = open_management_db(tmp_path / "management.db")
        try:
            worker_service = AuthService(AuthStore(worker_connection))
            try:
                worker_service.authenticate("ops", "wrong", NOW)
            except AuthError as error:
                return error.code
            return "unexpected_success"
        finally:
            worker_connection.close()

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(fail_login, index)
            for index in range(5)
        ]
        all_checked.wait(timeout=10)
        AuthStore(connection).replace_password(
            user.id,
            generate_password_hash(
                "administrator reset password",
                method="scrypt",
            ),
            must_change_password=True,
            actor_user_id=user.id,
            now=NOW,
        )
        release_checks.wait(timeout=10)
        outcomes = [future.result(timeout=10) for future in futures]

    current = AuthStore(connection).get_by_id(user.id)
    assert outcomes == ["invalid_credentials"] * 5
    assert current.failed_attempt_count == 0
    assert current.locked_until is None
    assert connection.execute(
        """
        SELECT COUNT(*) FROM management_audit_events
        WHERE event_type = 'login_failed'
        """
    ).fetchone()[0] == 0


def test_change_password_checks_current_length_and_uses_scrypt(auth_context):
    _connection, service, user = auth_context

    with pytest.raises(AuthError) as caught:
        service.change_password(user.id, "wrong", "new password 123", NOW)
    assert caught.value.code == "invalid_credentials"

    with pytest.raises(AuthError) as caught:
        service.change_password(user.id, PASSWORD, "too short", NOW)
    assert (caught.value.code, caught.value.status) == (
        "password_too_short",
        400,
    )

    changed = service.change_password(
        user.id,
        PASSWORD,
        "replacement password 456",
        NOW,
    )

    assert changed.password_hash.startswith("scrypt:")
    assert check_password_hash(
        changed.password_hash,
        "replacement password 456",
    )
    assert changed.session_version == user.session_version + 1
    assert changed.must_change_password is False
    assert changed.password_changed_at == NOW.isoformat()


def test_change_password_cannot_overwrite_concurrent_reset(
    auth_context,
    tmp_path,
    monkeypatch,
):
    _connection, service, user = auth_context
    second_connection = open_management_db(tmp_path / "management.db")
    second_store = AuthStore(second_connection)
    reset_password = "administrator reset password"
    reset_hash = generate_password_hash(reset_password, method="scrypt")
    original_check = auth_service.check_password_hash
    reset = []

    def check_then_reset(password_hash, password):
        result = original_check(password_hash, password)
        if result and not reset:
            reset.append(True)
            second_store.replace_password(
                user.id,
                reset_hash,
                must_change_password=True,
                actor_user_id=user.id,
                now=NOW,
            )
        return result

    monkeypatch.setattr(
        auth_service,
        "check_password_hash",
        check_then_reset,
    )
    try:
        with pytest.raises(AuthError) as caught:
            service.change_password(
                user.id,
                PASSWORD,
                "stale request replacement",
                NOW,
            )
        assert caught.value.code == "session_revoked"
        current = service.store.get_by_id(user.id)
        assert current.password_hash == reset_hash
        assert current.must_change_password is True
    finally:
        second_connection.close()


def test_two_concurrent_password_changes_allow_only_one_commit(
    auth_context,
    tmp_path,
    monkeypatch,
):
    _connection, _service, user = auth_context
    barrier = Barrier(2)
    original_check = auth_service.check_password_hash

    def synchronized_check(password_hash, password):
        result = original_check(password_hash, password)
        if password_hash == user.password_hash:
            barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(
        auth_service,
        "check_password_hash",
        synchronized_check,
    )

    def change(new_password):
        connection = open_management_db(tmp_path / "management.db")
        try:
            service = AuthService(AuthStore(connection))
            try:
                changed = service.change_password(
                    user.id,
                    PASSWORD,
                    new_password,
                    NOW,
                )
                return "success", changed.password_hash
            except AuthError as error:
                return error.code, None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                change,
                ("first replacement password", "second replacement password"),
            )
        )

    assert sorted(code for code, _hash in outcomes) == [
        "session_revoked",
        "success",
    ]
    committed_hash = next(
        password_hash
        for code, password_hash in outcomes
        if code == "success"
    )
    assert service_password_hash(tmp_path, user.id) == committed_hash


def test_temporary_password_is_returned_once_and_never_audited(
    auth_context,
):
    connection, service, actor = auth_context

    user, temporary_password = service.create_temporary_user(
        "new-operator",
        "operator",
        actor.id,
        NOW,
    )

    assert user.must_change_password is True
    assert len(temporary_password) >= 20
    assert check_password_hash(user.password_hash, temporary_password)
    rows = connection.execute(
        """
        SELECT actor_user_id, details_json
        FROM management_audit_events
        WHERE target_id = ?
        """,
        (str(user.id),),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["actor_user_id"] == actor.id
    audit = rows[0]["details_json"]
    assert json.loads(audit)["must_change"] is True
    assert temporary_password not in audit
    assert user.password_hash not in audit
    assert "password" not in audit.casefold()
    assert user.created_at == NOW.isoformat()


def test_valid_session_returns_enabled_user_including_must_change_state(
    auth_context,
):
    _connection, service, user = auth_context
    temporary = service.store.replace_password(
        user.id,
        generate_password_hash("temporary password", method="scrypt"),
        must_change_password=True,
        actor_user_id=user.id,
        now=NOW,
    )
    payload = _session_payload(
        temporary,
        issued=NOW - timedelta(hours=1),
        activity=NOW - timedelta(minutes=1),
    )

    validated = service.validate_session(payload, NOW)

    assert validated.id == user.id
    assert validated.must_change_password is True


def test_session_missing_disabled_and_version_mismatch_use_stable_codes(
    auth_context,
):
    _connection, service, user = auth_context
    base = _session_payload(user)

    for payload, code in (
        ({}, "authentication_required"),
        ({**base, "user_id": user.id + 100}, "authentication_required"),
        (
            {**base, "session_version": user.session_version - 1},
            "session_revoked",
        ),
    ):
        with pytest.raises(AuthError) as caught:
            service.validate_session(payload, NOW)
        assert (caught.value.code, caught.value.status) == (code, 401)

    service.store.update_access(
        user.id,
        enabled=False,
        actor_user_id=user.id,
    )
    with pytest.raises(AuthError) as caught:
        service.validate_session(base, NOW)
    assert caught.value.code == "authentication_required"


@pytest.mark.parametrize(
    ("issued", "activity"),
    (
        (NOW - timedelta(hours=8), NOW - timedelta(minutes=1)),
        (NOW - timedelta(hours=1), NOW - timedelta(minutes=30)),
    ),
)
def test_session_expires_at_idle_or_absolute_boundary(
    auth_context,
    issued,
    activity,
):
    _connection, service, user = auth_context

    with pytest.raises(AuthError) as caught:
        service.validate_session(
            _session_payload(user, issued=issued, activity=activity),
            NOW,
        )

    assert (caught.value.code, caught.value.status) == (
        "session_expired",
        401,
    )


@pytest.mark.parametrize(
    ("issued", "activity"),
    (
        ("2026-07-28T02:00:00", "2026-07-28T02:59:00+00:00"),
        ("2026-07-28T02:00:00+00:00", "2026-07-28T02:59:00"),
        ("not-a-date", "2026-07-28T02:59:00+00:00"),
        (
            "2026-07-28T03:00:01+00:00",
            "2026-07-28T03:00:01+00:00",
        ),
        (
            "2026-07-28T02:00:00+00:00",
            "2026-07-28T03:00:01+00:00",
        ),
        (
            "2026-07-28T02:30:00+00:00",
            "2026-07-28T02:29:59+00:00",
        ),
    ),
)
def test_session_rejects_bad_timezone_future_and_reversed_timestamps(
    auth_context,
    issued,
    activity,
):
    _connection, service, user = auth_context
    payload = {
        "user_id": user.id,
        "session_version": user.session_version,
        "issued_at": issued,
        "last_activity_at": activity,
    }

    with pytest.raises(AuthError) as caught:
        service.validate_session(payload, NOW)

    assert caught.value.code == "authentication_required"


def test_service_rejects_naive_now(auth_context):
    _connection, service, _user = auth_context
    naive = NOW.replace(tzinfo=None)

    with pytest.raises(ValueError, match="^now_invalid$"):
        service.authenticate("ops", PASSWORD, naive)


def test_csrf_tokens_are_urlsafe_unique_and_have_256_bits_of_entropy():
    tokens = {new_csrf_token() for _index in range(32)}

    assert len(tokens) == 32
    assert all(len(token) >= 43 for token in tokens)
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", token) for token in tokens)


def _session_payload(user, *, issued=None, activity=None):
    return {
        "user_id": user.id,
        "session_version": user.session_version,
        "issued_at": (
            issued or NOW - timedelta(hours=1)
        ).isoformat(),
        "last_activity_at": (
            activity or NOW - timedelta(minutes=1)
        ).isoformat(),
    }


def service_password_hash(tmp_path, user_id):
    connection = open_management_db(tmp_path / "management.db")
    try:
        return AuthStore(connection).get_by_id(user_id).password_hash
    finally:
        connection.close()
