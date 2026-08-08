from contextlib import closing
from datetime import date, timedelta
import sqlite3

from gateway.app import create_app
from init_db import init_db


def today():
    return date.today().isoformat()


def yesterday():
    return (date.today() - timedelta(days=1)).isoformat()


def insert_account(
    db_path,
    ads_power_user_id,
    *,
    last_interact_date=None,
    status="active",
    account_name=None,
    buffer_token=None,
    buffer_api=None,
):
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                last_interact_date,
                status,
                account_name,
                buffer_token,
                buffer_api
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ads_power_user_id,
                f"buffer-{ads_power_user_id}",
                f"session-{ads_power_user_id}",
                last_interact_date,
                status,
                account_name,
                buffer_token,
                buffer_api,
            ),
        )


def make_client(db_path):
    app = create_app()
    app.config["ACCOUNTS_DB_PATH"] = db_path
    return app.test_client()


def test_next_account_returns_active_account_not_interacted_today(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(db_path, "ads-used-today", last_interact_date=today())
    insert_account(db_path, "ads-banned", status="banned")
    insert_account(db_path, "ads-ready", last_interact_date=yesterday())

    response = make_client(db_path).get("/api/account/next")

    assert response.status_code == 200
    assert response.get_json() == {"ads_power_user_id": "ads-ready"}


def test_next_account_returns_404_when_none_available(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(db_path, "ads-used-today", last_interact_date=today())
    insert_account(db_path, "ads-banned", status="banned")

    response = make_client(db_path).get("/api/account/next")

    assert response.status_code == 404
    assert response.get_json() == {"error": "no available account"}


def test_account_update_marks_success_as_interacted_today(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(db_path, "ads-ready", last_interact_date=None)

    response = make_client(db_path).post(
        "/api/account/update",
        json={"ads_power_user_id": "ads-ready", "result": "success"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ads_power_user_id": "ads-ready",
        "last_interact_date": today(),
        "status": "active",
    }


def test_account_update_marks_abnormal_as_banned(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(db_path, "ads-risky", last_interact_date=None)

    response = make_client(db_path).post(
        "/api/account/update",
        json={"ads_power_user_id": "ads-risky", "result": "abnormal"},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ads_power_user_id": "ads-risky",
        "last_interact_date": today(),
        "status": "banned",
    }


def test_account_update_rejects_invalid_payload(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)

    response = make_client(db_path).post(
        "/api/account/update",
        json={"ads_power_user_id": "ads-ready", "result": "paused"},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "ads_power_user_id and valid result are required"
    }


def test_discover_accounts_writes_buffer_channels_and_masks_token(monkeypatch, tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(
        db_path,
        "ads-ready",
        account_name="Brand Account",
        buffer_token="buffer-secret-token",
        buffer_api="https://buffer.example/graphql",
    )

    def fake_discover(account):
        assert account["buffer_token"] == "buffer-secret-token"
        return {
            "accountId": account["id"],
            "account_name": account["account_name"],
            "organizations": [{"id": "org-1", "name": "Org One"}],
            "channels": [{"id": "channel-tiktok", "service": "tiktok"}],
            "tiktok_channels": [{"id": "channel-tiktok", "service": "tiktok"}],
            "buffer_profile_ids": ["channel-tiktok"],
        }

    monkeypatch.setattr("gateway.routes_accounts.discover_accounts", lambda db_path, account_id=None: {
        "count": 1,
        "results": [
            {
                "status": "ok",
                **fake_discover(
                    {
                        "id": "1",
                        "account_name": "Brand Account",
                        "buffer_token": "buffer-secret-token",
                    }
                ),
            }
        ],
        "accounts": [
            {
                "id": "1",
                "account_name": "Brand Account",
                "buffer_token": "buff...oken",
                "buffer_profile_ids": ["channel-tiktok"],
                "last_channel_sync_error": "",
            }
        ],
    })

    response = make_client(db_path).post("/api/accounts/discover", json={})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"][0]["buffer_profile_ids"] == ["channel-tiktok"]
    assert payload["accounts"][0]["buffer_token"] == "buff...oken"
    assert "buffer-secret-token" not in response.get_data(as_text=True)


def test_import_buffer_accounts_endpoint_masks_token(monkeypatch, tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)

    monkeypatch.setattr(
        "gateway.routes_accounts.import_buffer_accounts",
        lambda db_path, accounts=None, raw_text="", discover_account=None: {
            "imported": 1,
            "saved_accounts": 1,
            "results": [{"status": "ok", "buffer_profile_ids": ["channel-tiktok"]}],
            "accounts": [
                {
                    "id": "1",
                    "account_name": "Brand TikTok",
                    "buffer_token": "toke...-one",
                    "buffer_profile_ids": ["channel-tiktok"],
                }
            ],
        },
    )

    response = make_client(db_path).post(
        "/api/accounts/import",
        json={
            "accounts": [
                {"account_name": "Brand One", "buffer_token": "token-one"}
            ]
        },
    )

    assert response.status_code == 200
    assert response.get_json()["saved_accounts"] == 1
    assert response.get_json()["accounts"][0]["buffer_token"] == "toke...-one"
    assert "token-one" not in response.get_data(as_text=True)


def test_import_buffer_accounts_endpoint_accepts_single_manual_account(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    captured = {}

    def fake_import(db_path, accounts=None, raw_text="", discover_account=None):
        captured["accounts"] = accounts
        captured["raw_text"] = raw_text
        return {
            "imported": 1,
            "saved_accounts": 1,
            "results": [{"status": "ok", "buffer_profile_ids": ["channel-tiktok"]}],
            "accounts": [{"buffer_token": "toke...-one"}],
        }

    monkeypatch.setattr("gateway.routes_accounts.import_buffer_accounts", fake_import)

    response = make_client(db_path).post(
        "/api/accounts/import",
        json={
            "account_name": "Brand One",
            "buffer_token": "token-one",
            "buffer_api": "https://graph.buffer.com/graphql",
        },
    )

    assert response.status_code == 200
    assert captured["raw_text"] == ""
    assert captured["accounts"] == [
        {
            "account_name": "Brand One",
            "buffer_token": "token-one",
            "buffer_api": "https://graph.buffer.com/graphql",
        }
    ]


def test_accounts_api_returns_masked_available_accounts(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                account_name,
                buffer_token,
                buffer_profile_ids,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "buffer-account-1",
                "buffer-account-1",
                "proxy-session",
                "Brand One",
                "token-one",
                '["channel-tiktok"]',
                "active",
            ),
        )

    response = make_client(db_path).get("/api/accounts")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["available_count"] == 1
    assert payload["accounts"][0]["buffer_token"] == "toke...-one"
    assert payload["accounts"][0]["buffer_profile_ids"] == ["channel-tiktok"]


def test_save_buffer_account_endpoint_stores_masked_account(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)

    response = make_client(db_path).post(
        "/api/accounts/save",
        json={
            "account_name": "Brand One",
            "buffer_token": "token-one",
            "buffer_api": "https://graph.buffer.com/graphql",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["account"]["account_name"] == "Brand One"
    assert payload["account"]["buffer_token"] == "toke...-one"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        row = connection.execute("SELECT account_name, buffer_token FROM accounts").fetchone()
    assert row == ("Brand One", "token-one")


def test_save_buffer_account_endpoint_updates_existing_without_requiring_token(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(
        db_path,
        "buffer-account-1",
        account_name="Old Brand",
        buffer_token="old-token",
        buffer_api="https://api.buffer.com",
    )

    response = make_client(db_path).post(
        "/api/accounts/save",
        json={
            "id": "1",
            "account_name": "New Brand",
            "buffer_token": "",
            "buffer_api": "https://graph.buffer.com/graphql",
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["account"]["account_name"] == "New Brand"
    assert payload["account"]["buffer_token"] == "old-...oken"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        row = connection.execute(
            "SELECT account_name, buffer_token, buffer_api FROM accounts WHERE id = 1"
        ).fetchone()
    assert row == ("New Brand", "old-token", "https://graph.buffer.com/graphql")


def test_assign_account_proxy_automatically_from_proxy_pool(monkeypatch, tmp_path):
    from gateway.settings_store import save_settings

    config_path = tmp_path / "config.json"
    db_path = tmp_path / "accounts.db"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "raw": "192.53.69.143:6781:nsucssou:3mjeb2p392yk"
            }
        },
        config_path,
    )
    init_db(db_path)
    insert_account(
        db_path,
        "buffer-account-1",
        account_name="Brand One",
        buffer_token="token-one",
    )

    response = make_client(db_path).post(
        "/api/accounts/proxy",
        json={"account_id": "buffer-account-1", "mode": "auto"},
    )

    assert response.status_code == 200
    assert response.get_json()["account"]["proxy_session"] == (
        "192.53.69.143:6781:nsucssou:3mjeb2p392yk"
    )


def test_auto_proxy_save_keeps_existing_pool_proxy(monkeypatch, tmp_path):
    from gateway.settings_store import save_settings

    config_path = tmp_path / "config.json"
    db_path = tmp_path / "accounts.db"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "raw": "\n".join(
                    [
                        "192.0.2.1:8001:user1:pass1",
                        "192.0.2.2:8002:user2:pass2",
                        "192.0.2.3:8003:user3:pass3",
                        "192.0.2.4:8004:user4:pass4",
                    ]
                )
            }
        },
        config_path,
    )
    init_db(db_path)
    insert_account(db_path, "account-a")
    insert_account(db_path, "account-b")
    client = make_client(db_path)

    first = client.post(
        "/api/accounts/proxy",
        json={"account_id": "account-a", "mode": "auto"},
    )
    client.post(
        "/api/accounts/proxy",
        json={"account_id": "account-b", "mode": "auto"},
    )
    repeated = client.post(
        "/api/accounts/proxy",
        json={"account_id": "account-a", "mode": "auto"},
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.get_json()["account"]["proxy_session"] == (
        first.get_json()["account"]["proxy_session"]
    )


def test_assign_account_proxy_manually(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    insert_account(
        db_path,
        "buffer-account-1",
        account_name="Brand One",
        buffer_token="token-one",
    )

    response = make_client(db_path).post(
        "/api/accounts/proxy",
        json={
            "account_id": "buffer-account-1",
            "mode": "manual",
            "proxy": "203.0.113.8:9000:user2:pass2",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["account"]["proxy_session"] == "203.0.113.8:9000:user2:pass2"
