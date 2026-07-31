from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3

from init_db import init_db


ACTIVE_RESULTS = {"success", "failed"}
BANNED_RESULTS = {"banned", "abnormal"}
VALID_RESULTS = ACTIVE_RESULTS | BANNED_RESULTS


def get_today():
    return date.today().isoformat()


def connect(db_path):
    init_db(db_path)
    connection = sqlite3.connect(Path(db_path))
    connection.row_factory = sqlite3.Row
    return connection


def get_next_account(db_path, today=None):
    today = today or get_today()
    with closing(connect(db_path)) as connection, connection:
        row = connection.execute(
            """
            SELECT ads_power_user_id
            FROM accounts
            WHERE status = 'active'
              AND (
                last_interact_date IS NULL
                OR last_interact_date = ''
                OR last_interact_date != ?
              )
            ORDER BY id ASC
            LIMIT 1
            """,
            (today,),
        ).fetchone()

    if row is None:
        return None

    return {"ads_power_user_id": row["ads_power_user_id"]}


def update_account(db_path, ads_power_user_id, result, today=None):
    if result not in VALID_RESULTS:
        return None

    today = today or get_today()
    status = "banned" if result in BANNED_RESULTS else "active"

    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE accounts
            SET last_interact_date = ?,
                status = ?
            WHERE ads_power_user_id = ?
            """,
            (today, status, ads_power_user_id),
        )

    if cursor.rowcount == 0:
        return None

    return {
        "ads_power_user_id": ads_power_user_id,
        "last_interact_date": today,
        "status": status,
    }


def get_assigned_proxy_sessions(db_path):
    with closing(connect(db_path)) as connection, connection:
        rows = connection.execute(
            """
            SELECT DISTINCT proxy_session
            FROM accounts
            WHERE proxy_session IS NOT NULL
              AND proxy_session != ''
            """
        ).fetchall()

    return [row["proxy_session"] for row in rows]


def list_buffer_accounts(db_path, account_id=None):
    with closing(connect(db_path)) as connection, connection:
        rows = connection.execute(
            """
            SELECT id,
                   ads_power_user_id,
                   buffer_account_id,
                   proxy_session,
                   status,
                   account_name,
                   buffer_token,
                   buffer_api,
                   buffer_channels,
                   buffer_profile_ids,
                   last_channel_sync_at,
                   last_channel_sync_error
            FROM accounts
            ORDER BY id ASC
            """
        ).fetchall()

    accounts = [_row_to_buffer_account(row) for row in rows]
    if not account_id:
        return accounts

    account_id = str(account_id)
    return [
        account
        for account in accounts
        if account["id"] == account_id
        or account["ads_power_user_id"] == account_id
        or account["buffer_account_id"] == account_id
    ]


def public_accounts(db_path):
    return [_public_account(account) for account in list_buffer_accounts(db_path)]


def account_summary(db_path):
    accounts = public_accounts(db_path)
    available = [account for account in accounts if account.get("buffer_profile_ids")]
    return {
        "count": len(accounts),
        "available_count": len(available),
        "accounts": accounts,
        "available_accounts": available,
    }


def get_buffer_account(db_path, account_id):
    matches = list_buffer_accounts(db_path, account_id)
    return matches[0] if matches else None


def public_account(db_path, account_id):
    account = get_buffer_account(db_path, account_id)
    return _public_account(account) if account else None


def save_buffer_account(db_path, account):
    account_name = (account.get("account_name") or "").strip()
    buffer_token = (account.get("buffer_token") or "").strip()
    buffer_api = (account.get("buffer_api") or "").strip()

    if not account_name:
        raise ValueError("account_name is required")

    with closing(connect(db_path)) as connection, connection:
        existing = None
        if account.get("id"):
            existing = connection.execute(
                "SELECT id, buffer_token FROM accounts WHERE id = ?",
                (account["id"],),
            ).fetchone()
        if existing is None:
            existing = connection.execute(
                "SELECT id, buffer_token FROM accounts WHERE account_name = ?",
                (account_name,),
            ).fetchone()

        if existing:
            row_id = existing["id"]
            if not buffer_token or "..." in buffer_token or buffer_token == "****":
                buffer_token = existing["buffer_token"] or ""
            if not buffer_token:
                raise ValueError("buffer_token is required")
            connection.execute(
                """
                UPDATE accounts
                SET account_name = ?,
                    buffer_token = ?,
                    buffer_api = ?
                WHERE id = ?
                """,
                (account_name, buffer_token, buffer_api, row_id),
            )
        else:
            if not buffer_token:
                raise ValueError("buffer_token is required")
            cursor = connection.execute(
                """
                INSERT INTO accounts (
                    ads_power_user_id,
                    buffer_account_id,
                    proxy_session,
                    status,
                    account_name,
                    buffer_token,
                    buffer_api,
                    buffer_channels,
                    buffer_profile_ids,
                    last_channel_sync_error
                )
                VALUES (?, ?, '', 'active', ?, ?, ?, '[]', '[]', '')
                """,
                (
                    "pending",
                    "pending",
                    account_name,
                    buffer_token,
                    buffer_api,
                ),
            )
            row_id = cursor.lastrowid
            stable_id = f"buffer-account-{row_id}"
            connection.execute(
                """
                UPDATE accounts
                SET ads_power_user_id = ?,
                    buffer_account_id = ?
                WHERE id = ?
                """,
                (stable_id, stable_id, row_id),
            )

    return public_accounts(db_path)[
        next(
            index
            for index, item in enumerate(list_buffer_accounts(db_path))
            if item["id"] == str(row_id)
        )
    ]


def assign_proxy_session(db_path, account_id, proxy_session):
    with closing(connect(db_path)) as connection, connection:
        cursor = connection.execute(
            """
            UPDATE accounts
            SET proxy_session = ?
            WHERE id = ?
               OR ads_power_user_id = ?
               OR buffer_account_id = ?
            """,
            (
                proxy_session or "",
                str(account_id),
                str(account_id),
                str(account_id),
            ),
        )

    if cursor.rowcount == 0:
        return None

    return public_account(db_path, account_id)


def update_channel_sync(
    db_path,
    account_id,
    *,
    channels=None,
    profile_ids=None,
    synced_at="",
    error="",
):
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """
            UPDATE accounts
            SET buffer_channels = ?,
                buffer_profile_ids = ?,
                last_channel_sync_at = ?,
                last_channel_sync_error = ?
            WHERE id = ?
            """,
            (
                json.dumps(channels or [], ensure_ascii=False),
                json.dumps(profile_ids or [], ensure_ascii=False),
                synced_at,
                error,
                account_id,
            ),
        )


def update_channel_sync_error(db_path, account_id, error):
    with closing(connect(db_path)) as connection, connection:
        connection.execute(
            """
            UPDATE accounts
            SET last_channel_sync_error = ?
            WHERE id = ?
            """,
            (error, account_id),
        )


def _row_to_buffer_account(row):
    return {
        "id": str(row["id"]),
        "ads_power_user_id": row["ads_power_user_id"],
        "buffer_account_id": row["buffer_account_id"],
        "proxy_session": row["proxy_session"],
        "status": row["status"],
        "account_name": row["account_name"] or row["buffer_account_id"],
        "buffer_token": row["buffer_token"] or "",
        "buffer_api": row["buffer_api"] or "",
        "buffer_channels": _load_json(row["buffer_channels"], []),
        "buffer_profile_ids": _load_json(row["buffer_profile_ids"], []),
        "last_channel_sync_at": row["last_channel_sync_at"] or "",
        "last_channel_sync_error": row["last_channel_sync_error"] or "",
    }


def _public_account(account):
    public = dict(account)
    public["buffer_token"] = _mask_token(account.get("buffer_token", ""))
    return public


def _mask_token(token):
    if not token:
        return ""
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


def _load_json(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default
