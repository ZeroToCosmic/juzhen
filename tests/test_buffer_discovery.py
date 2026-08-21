import json
import sqlite3

import pytest

from gateway.buffer_discovery import (
    buffer_graphql,
    discover_accounts,
    discover_channels_for_account,
    is_tiktok_channel,
)
from init_db import init_db


class FakeResponse:
    def __init__(self, *, ok=True, status_code=200, text="", reason="OK"):
        self.ok = ok
        self.status_code = status_code
        self._text = text
        self.reason = reason

    @property
    def text(self):
        return self._text


def test_buffer_graphql_posts_bearer_token_and_parses_payload():
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse(text=json.dumps({"data": {"ok": True}}))

    payload = buffer_graphql(
        "https://buffer.example/graphql",
        "token-123",
        "query Test { ok }",
        post=fake_post,
    )

    assert payload == {"data": {"ok": True}}
    assert calls[0][0] == "https://buffer.example/graphql"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer token-123"
    assert calls[0][1]["json"] == {"query": "query Test { ok }"}


def test_buffer_graphql_raises_api_errors():
    def fake_post(url, **kwargs):
        return FakeResponse(
            ok=False,
            status_code=401,
            text=json.dumps({"message": "Unauthorized"}),
            reason="Unauthorized",
        )

    with pytest.raises(RuntimeError, match="Unauthorized"):
        buffer_graphql("https://buffer.example/graphql", "bad-token", "query", post=fake_post)


def test_discover_channels_for_account_adds_context_to_unauthorized_errors(monkeypatch):
    monkeypatch.delenv("APP_CONFIG_PATH", raising=False)

    def fake_graphql(api_url, token, query):
        raise RuntimeError("Unauthorized")

    with pytest.raises(RuntimeError) as error:
        discover_channels_for_account(
            {
                "id": "1",
                "account_name": "Brand One",
                "buffer_token": "token-one",
                "buffer_api": "https://api.buffer.com",
            },
            graphql=fake_graphql,
        )

    message = str(error.value)
    assert "Brand One" in message
    assert "Unauthorized" in message
    assert "https://api.buffer.com" in message
    assert "toke...-one" in message
    assert "token-one" not in message


def test_buffer_graphql_raises_graphql_errors():
    def fake_post(url, **kwargs):
        return FakeResponse(text=json.dumps({"errors": [{"message": "Bad query"}]}))

    with pytest.raises(RuntimeError, match="Bad query"):
        buffer_graphql("https://buffer.example/graphql", "token", "query", post=fake_post)


def test_is_tiktok_channel_matches_service_or_descriptor():
    assert is_tiktok_channel({"service": "TIKTOK", "descriptor": ""}) is True
    assert is_tiktok_channel({"service": "short_video", "descriptor": "@brand TikTok"}) is True
    assert is_tiktok_channel({"service": "instagram", "descriptor": "@brand"}) is False


def test_discover_channels_for_account_returns_tiktok_profile_ids():
    queries = []

    def fake_graphql(api_url, token, query):
        queries.append(query)
        if "GetOrganizations" in query:
            return {
                "data": {
                    "account": {
                        "organizations": [
                            {"id": "org-1", "name": "Org One", "ownerEmail": "owner@example.com"}
                        ]
                    }
                }
            }
        return {
            "data": {
                "channels": [
                    {
                        "id": "channel-tiktok",
                        "name": "brand",
                        "displayName": "Brand TikTok",
                        "service": "tiktok",
                        "descriptor": "@brand",
                        "avatar": "https://example.com/avatar.png",
                        "externalLink": "https://tiktok.example/brand",
                        "isQueuePaused": False,
                        "isDisconnected": False,
                        "isLocked": False,
                        "organizationId": "org-1",
                    },
                    {
                        "id": "channel-ig",
                        "name": "brand-ig",
                        "service": "instagram",
                        "descriptor": "@brand",
                    },
                ]
            }
        }

    discovery = discover_channels_for_account(
        {
            "id": "1",
            "account_name": "Brand Account",
            "buffer_token": "token-123",
            "buffer_api": "https://buffer.example/graphql",
        },
        graphql=fake_graphql,
    )

    assert discovery["accountId"] == "1"
    assert discovery["account_name"] == "Brand Account"
    assert discovery["buffer_profile_ids"] == ["channel-tiktok"]
    assert len(discovery["channels"]) == 2
    assert discovery["tiktok_channels"][0]["organizationName"] == "Org One"
    assert "org-1" in queries[1]


def test_discover_accounts_writes_results_back_to_accounts_table(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                account_name,
                buffer_token,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("ads-1", "buffer-1", "session-1", "Brand Account", "token-123", "active"),
        )

    def fake_discover(account):
        return {
            "accountId": account["id"],
            "account_name": account["account_name"],
            "organizations": [],
            "channels": [{"id": "channel-tiktok", "service": "tiktok"}],
            "tiktok_channels": [{"id": "channel-tiktok", "service": "tiktok"}],
            "buffer_profile_ids": ["channel-tiktok"],
        }

    result = discover_accounts(db_path, discover_account=fake_discover)

    assert result["count"] == 1
    assert result["results"][0]["status"] == "ok"
    assert result["accounts"][0]["buffer_token"] == "toke...-123"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT buffer_channels,
                   buffer_profile_ids,
                   last_channel_sync_at,
                   last_channel_sync_error
            FROM accounts
            LIMIT 1
            """
        ).fetchone()

    assert json.loads(row[0]) == [{"id": "channel-tiktok", "service": "tiktok"}]
    assert json.loads(row[1]) == ["channel-tiktok"]
    assert row[2]
    assert row[3] == ""


def test_discover_accounts_keeps_existing_channels_when_sync_fails(tmp_path):
    db_path = tmp_path / "accounts.db"
    init_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                account_name,
                buffer_token,
                buffer_channels,
                buffer_profile_ids,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ads-1",
                "buffer-1",
                "session-1",
                "Brand Account",
                "token-123",
                json.dumps([{"id": "existing-channel"}]),
                json.dumps(["existing-channel"]),
                "active",
            ),
        )

    def failing_discover(account):
        raise RuntimeError("Buffer unavailable")

    result = discover_accounts(db_path, discover_account=failing_discover)

    assert result["results"][0]["status"] == "failed"
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT buffer_channels,
                   buffer_profile_ids,
                   last_channel_sync_error
            FROM accounts
            LIMIT 1
            """
        ).fetchone()

    assert json.loads(row[0]) == [{"id": "existing-channel"}]
    assert json.loads(row[1]) == ["existing-channel"]
    assert row[2] == "Buffer unavailable"
