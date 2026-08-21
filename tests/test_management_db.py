import json
import math

import pytest

from gateway.management_db import (
    open_management_db,
    record_management_audit,
)


def test_open_management_db_applies_schema_and_required_pragmas(tmp_path):
    path = tmp_path / "nested" / "management.db"

    first = open_management_db(path)
    first.close()
    connection = open_management_db(path)

    tables = {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"management_users", "management_audit_events"} <= tables
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert isinstance(
        connection.execute("SELECT 1 AS value").fetchone()["value"],
        int,
    )
    connection.close()


def test_audit_details_are_strict_json_and_drop_sensitive_fields(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    connection.execute("BEGIN IMMEDIATE")
    event_id = record_management_audit(
        connection,
        actor_user_id=None,
        event_type="user_updated",
        target_type="management_user",
        target_id="7",
        result="success",
        reason="",
        details={
            "safe": {"role": "operator"},
            "password": "plain-secret",
            "nested": {
                "password_hash": "hash-secret",
                "csrfToken": "csrf-secret",
                "count": 2,
            },
        },
    )
    connection.commit()

    raw = connection.execute(
        "SELECT details_json FROM management_audit_events WHERE id = ?",
        (event_id,),
    ).fetchone()["details_json"]
    assert json.loads(raw) == {
        "nested": {"count": 2},
        "safe": {"role": "operator"},
    }
    for forbidden in (
        "plain-secret",
        "hash-secret",
        "csrf-secret",
        "password",
        "hash",
        "token",
    ):
        assert forbidden not in raw.casefold()

    connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(ValueError, match="audit_details_invalid"):
        record_management_audit(
            connection,
            actor_user_id=None,
            event_type="invalid",
            target_type="test",
            target_id="",
            result="failed",
            reason="invalid",
            details={"number": math.nan},
        )
    connection.rollback()
    connection.close()


def test_record_management_audit_does_not_commit_callers_transaction(tmp_path):
    connection = open_management_db(tmp_path / "management.db")
    connection.execute("BEGIN IMMEDIATE")
    record_management_audit(
        connection,
        actor_user_id=None,
        event_type="rolled_back",
        target_type="test",
        target_id="",
        result="success",
        reason="",
        details={},
    )
    connection.rollback()

    count = connection.execute(
        "SELECT COUNT(*) FROM management_audit_events"
    ).fetchone()[0]
    assert count == 0
    connection.close()
