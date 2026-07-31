import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from gateway.auth_store import AuthStore, LastAdministratorError
from gateway.management_db import open_management_db


@pytest.fixture
def auth_store(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    try:
        yield connection, AuthStore(connection)
    finally:
        connection.close()


def test_usernames_are_case_insensitively_unique(auth_store):
    _connection, auth = auth_store
    auth.create_user("Admin", "hash-one", "administrator")

    with pytest.raises(ValueError, match="^username_exists$"):
        auth.create_user("admin", "hash-two", "operator")


@pytest.mark.parametrize(
    "username",
    (
        "Straße",
        "İ",
        " leading",
        "trailing ",
        "two words",
        "line\nbreak",
        "tab\tname",
        "_leading-symbol",
        "a" * 65,
    ),
)
def test_usernames_reject_non_ascii_whitespace_and_controls(
    auth_store,
    username,
):
    _connection, auth = auth_store

    with pytest.raises(ValueError, match="^username_invalid$"):
        auth.create_user(username, "hash", "operator")


def test_last_enabled_administrator_cannot_be_disabled_or_demoted(auth_store):
    _connection, auth = auth_store
    admin = auth.create_user("admin", "hash", "administrator")

    for patch in ({"enabled": False}, {"role": "operator"}):
        with pytest.raises(LastAdministratorError) as caught:
            auth.update_access(
                admin.id,
                actor_user_id=admin.id,
                **patch,
            )
        assert caught.value.code == "last_administrator"


def test_role_and_enabled_changes_increment_session_version(auth_store):
    _connection, auth = auth_store
    admin = auth.create_user("admin", "hash", "administrator")
    operator = auth.create_user("ops", "hash", "operator")

    promoted = auth.update_access(
        operator.id,
        role="administrator",
        actor_user_id=admin.id,
    )
    disabled = auth.update_access(
        operator.id,
        enabled=False,
        actor_user_id=admin.id,
    )

    assert promoted.session_version == operator.session_version + 1
    assert disabled.session_version == promoted.session_version + 1
    assert auth.get_by_username("OPS") == disabled
    assert auth.get_by_id(operator.id) == disabled


def test_password_replacement_revokes_sessions_without_auditing_hash(
    auth_store,
):
    connection, auth = auth_store
    admin = auth.create_user("admin", "old-hash", "administrator")

    updated = auth.replace_password(
        admin.id,
        "new-hash-secret",
        must_change_password=False,
        actor_user_id=admin.id,
    )

    assert updated.password_hash == "new-hash-secret"
    assert updated.session_version == admin.session_version + 1
    assert updated.must_change_password is False
    audit_json = "\n".join(
        row["details_json"]
        for row in connection.execute(
            "SELECT details_json FROM management_audit_events"
        )
    )
    assert "new-hash-secret" not in audit_json
    assert "password" not in audit_json.casefold()
    assert "hash" not in audit_json.casefold()


def test_login_failure_lock_and_success_reset_are_durable_and_audited(
    auth_store,
):
    connection, auth = auth_store
    user = auth.create_user("ops", "hash", "operator")
    now = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)

    current = user
    for _index in range(5):
        current = auth.record_login_failure(current.id, now)

    assert current.failed_attempt_count == 5
    assert current.locked_until == (now + timedelta(minutes=15)).isoformat()
    reset = auth.record_login_success(current.id, now + timedelta(minutes=16))
    assert reset.failed_attempt_count == 0
    assert reset.locked_until is None
    assert reset.last_login_at == (now + timedelta(minutes=16)).isoformat()
    events = [
        row["event_type"]
        for row in connection.execute(
            "SELECT event_type FROM management_audit_events ORDER BY id"
        )
    ]
    assert events.count("login_failed") == 5
    assert events[-1] == "login_succeeded"


def test_user_and_audit_insert_commit_or_rollback_together(auth_store):
    connection, auth = auth_store
    connection.execute(
        """
        CREATE TRIGGER reject_management_audit
        BEFORE INSERT ON management_audit_events
        BEGIN
            SELECT RAISE(ABORT, 'audit unavailable');
        END
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
        auth.create_user("admin", "hash", "administrator")

    assert connection.execute(
        "SELECT COUNT(*) FROM management_users"
    ).fetchone()[0] == 0


def test_audit_details_for_all_store_mutations_are_secret_free_json(
    auth_store,
):
    connection, auth = auth_store
    admin = auth.create_user("admin", "hash-secret-one", "administrator")
    operator = auth.create_user("ops", "hash-secret-two", "operator")
    auth.update_access(
        operator.id,
        role="administrator",
        actor_user_id=admin.id,
    )
    auth.replace_password(
        operator.id,
        "hash-secret-three",
        must_change_password=True,
        actor_user_id=admin.id,
    )

    raw_values = [
        row["details_json"]
        for row in connection.execute(
            "SELECT details_json FROM management_audit_events"
        )
    ]
    for raw in raw_values:
        assert isinstance(json.loads(raw), dict)
        for forbidden in (
            "hash-secret-one",
            "hash-secret-two",
            "hash-secret-three",
            "password",
            "hash",
            "secret",
        ):
            assert forbidden not in raw.casefold()


def test_revision_cas_allows_only_one_concurrent_access_update(tmp_path):
    database = tmp_path / "management.db"
    setup_connection = open_management_db(database)
    setup_store = AuthStore(setup_connection)
    admin = setup_store.create_user("admin", "hash", "administrator")
    target = setup_store.create_user("ops", "hash", "operator")
    setup_connection.close()

    def update(enabled):
        connection = open_management_db(database)
        try:
            return AuthStore(connection).update_access_if_revision(
                target.id,
                expected_revision=target.session_version,
                enabled=enabled,
                actor_user_id=admin.id,
            )
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(update, (False, True)))

    assert sum(outcome is not None for outcome in outcomes) == 1
    connection = open_management_db(database)
    try:
        current = AuthStore(connection).get_by_id(target.id)
        assert current.session_version == target.session_version + 1
    finally:
        connection.close()


def test_last_administrator_check_is_atomic_across_connections(tmp_path):
    database = tmp_path / "management.db"
    setup_connection = open_management_db(database)
    setup_store = AuthStore(setup_connection)
    first = setup_store.create_user("first", "hash", "administrator")
    second = setup_store.create_user("second", "hash", "administrator")
    setup_connection.close()

    def demote(user):
        connection = open_management_db(database)
        try:
            try:
                result = AuthStore(
                    connection
                ).update_access_if_revision(
                    user.id,
                    expected_revision=user.session_version,
                    role="operator",
                    actor_user_id=first.id,
                )
                return "updated", result
            except LastAdministratorError:
                return "last_administrator", None
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(demote, (first, second)))

    assert sorted(code for code, _result in outcomes) == [
        "last_administrator",
        "updated",
    ]
    connection = open_management_db(database)
    try:
        enabled_admins = connection.execute(
            """
            SELECT COUNT(*) FROM management_users
            WHERE role = 'administrator' AND enabled = 1
            """
        ).fetchone()[0]
        assert enabled_admins == 1
    finally:
        connection.close()
