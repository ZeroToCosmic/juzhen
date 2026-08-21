import sqlite3

import pytest

from init_db import init_db
from gateway import account_store


def test_init_db_creates_accounts_table_with_expected_schema(tmp_path):
    db_path = tmp_path / "accounts.db"

    result = init_db(db_path)

    assert result == db_path
    conn = sqlite3.connect(db_path)
    columns = conn.execute("PRAGMA table_info(accounts)").fetchall()
    conn.close()

    assert [column[1] for column in columns] == [
        "id",
        "ads_power_user_id",
        "buffer_account_id",
        "proxy_session",
        "last_interact_date",
        "status",
        "account_name",
        "buffer_token",
        "buffer_api",
        "buffer_channels",
        "buffer_profile_ids",
        "last_channel_sync_at",
        "last_channel_sync_error",
    ]
    assert [column[2] for column in columns] == [
        "INTEGER",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
        "TEXT",
    ]


def test_init_db_status_accepts_only_active_or_banned(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO accounts (
            ads_power_user_id,
            buffer_account_id,
            proxy_session,
            status
        ) VALUES (?, ?, ?, ?)
        """,
        ("ads-1", "buffer-1", "session-1", "active"),
    )
    conn.execute(
        """
        INSERT INTO accounts (
            ads_power_user_id,
            buffer_account_id,
            proxy_session,
            status
        ) VALUES (?, ?, ?, ?)
        """,
        ("ads-2", "buffer-2", "session-2", "banned"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                status
            ) VALUES (?, ?, ?, ?)
            """,
            ("ads-3", "buffer-3", "session-3", "paused"),
        )

    conn.close()


def test_init_db_closes_its_sqlite_connection(monkeypatch, tmp_path):
    real_connect = sqlite3.connect
    closed = []

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def tracking_connect(*args, **kwargs):
        return real_connect(*args, factory=TrackingConnection, **kwargs)

    monkeypatch.setattr("init_db.sqlite3.connect", tracking_connect)

    init_db(tmp_path / "accounts.db")

    assert len(closed) == 1


def test_account_store_closes_connection_after_transaction(monkeypatch, tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    real_connect = sqlite3.connect
    closed = []

    class TrackingConnection(sqlite3.Connection):
        def close(self):
            closed.append(self)
            super().close()

    def tracking_connect(_db_path):
        connection = real_connect(db_path, factory=TrackingConnection)
        connection.row_factory = sqlite3.Row
        return connection

    monkeypatch.setattr(account_store, "connect", tracking_connect)

    assert account_store.get_next_account(db_path) is None
    assert len(closed) == 1
