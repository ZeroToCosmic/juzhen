import json
import sqlite3

from gateway.buffer_discovery import (
    import_buffer_accounts,
    parse_buffer_account_import_text,
)
from gateway.settings_store import save_settings
from init_db import init_db


def test_parse_buffer_account_import_text_accepts_csv_or_spreadsheet_rows():
    accounts = parse_buffer_account_import_text(
        """
        account_name,buffer_token,buffer_api
        Brand One,token-one,https://buffer.example/graphql
        Brand Two\ttoken-two\t
        """
    )

    assert accounts == [
        {
            "account_name": "Brand One",
            "buffer_token": "token-one",
            "buffer_api": "https://buffer.example/graphql",
        },
        {
            "account_name": "Brand Two",
            "buffer_token": "token-two",
            "buffer_api": "",
        },
    ]


def test_import_buffer_accounts_discovers_tiktok_channels_on_same_account_row(
    monkeypatch,
    tmp_path,
):
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

    def fake_discover(account):
        assert account["buffer_token"] == "token-one"
        return {
            "accountId": "imported-1",
            "account_name": account["account_name"],
            "organizations": [{"id": "org-1", "name": "Org One"}],
            "channels": [
                {
                    "id": "channel-tiktok",
                    "displayName": "Brand TikTok",
                    "service": "tiktok",
                    "descriptor": "@brand",
                    "organizationId": "org-1",
                    "organizationName": "Org One",
                }
            ],
            "tiktok_channels": [
                {
                    "id": "channel-tiktok",
                    "displayName": "Brand TikTok",
                    "service": "tiktok",
                    "descriptor": "@brand",
                    "organizationId": "org-1",
                    "organizationName": "Org One",
                }
            ],
            "buffer_profile_ids": ["channel-tiktok"],
        }

    result = import_buffer_accounts(
        db_path,
        accounts=[{"account_name": "Brand One", "buffer_token": "token-one"}],
        discover_account=fake_discover,
        now_fn=lambda: "2026-07-10T00:00:00+00:00",
    )

    assert result["imported"] == 1
    assert result["saved_accounts"] == 1
    assert result["results"][0]["buffer_profile_ids"] == ["channel-tiktok"]
    assert result["accounts"][0]["buffer_token"] == "toke...-one"
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT ads_power_user_id,
                   buffer_account_id,
                   account_name,
                   buffer_token,
                   proxy_session,
                   buffer_profile_ids,
                   last_channel_sync_error
            FROM accounts
            """
        ).fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row[0].startswith("buffer-account-")
    assert row[1].startswith("buffer-account-")
    assert row[2] == "Brand One"
    assert row[3] == "token-one"
    assert row[4] == "192.53.69.143:6781:nsucssou:3mjeb2p392yk"
    assert json.loads(row[5]) == ["channel-tiktok"]
    assert row[6] == ""
