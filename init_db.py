from contextlib import closing
from pathlib import Path
import sqlite3


DEFAULT_DB_PATH = Path("accounts.db")


def init_db(db_path=DEFAULT_DB_PATH):
    db_path = Path(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ads_power_user_id TEXT NOT NULL,
                buffer_account_id TEXT NOT NULL,
                proxy_session TEXT NOT NULL,
                last_interact_date TEXT,
                status TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'banned')),
                account_name TEXT,
                buffer_token TEXT,
                buffer_api TEXT,
                buffer_channels TEXT,
                buffer_profile_ids TEXT,
                last_channel_sync_at TEXT,
                last_channel_sync_error TEXT
            )
            """
        )
        _ensure_columns(conn)

    return db_path


def _ensure_columns(conn):
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
    }
    migrations = {
        "account_name": "ALTER TABLE accounts ADD COLUMN account_name TEXT",
        "buffer_token": "ALTER TABLE accounts ADD COLUMN buffer_token TEXT",
        "buffer_api": "ALTER TABLE accounts ADD COLUMN buffer_api TEXT",
        "buffer_channels": "ALTER TABLE accounts ADD COLUMN buffer_channels TEXT",
        "buffer_profile_ids": "ALTER TABLE accounts ADD COLUMN buffer_profile_ids TEXT",
        "last_channel_sync_at": "ALTER TABLE accounts ADD COLUMN last_channel_sync_at TEXT",
        "last_channel_sync_error": "ALTER TABLE accounts ADD COLUMN last_channel_sync_error TEXT",
    }

    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)


if __name__ == "__main__":
    init_db()
