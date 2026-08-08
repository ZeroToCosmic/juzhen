from contextlib import closing

import requests

from gateway.app import create_app
from gateway.buffer_client import extract_tiktok_url_from_buffer_payload


class FakeGraphQLResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def successful_graphql_response(post_id="buffer-123"):
    return FakeGraphQLResponse(
        {
            "data": {
                "createPost": {
                    "post": {
                        "id": post_id,
                        "text": "hello",
                        "dueAt": None,
                        "assets": [],
                    }
                }
            }
        }
    )


def test_publish_buffer_forwards_payload_through_account_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    captured = {}

    def fake_generate_proxy_url(account_id):
        captured["account_id"] = account_id
        return "http://proxy-url"

    def fake_post(url, json, headers, proxies, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        captured["proxies"] = proxies
        captured["timeout"] = timeout
        return successful_graphql_response()

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.buffer_client.requests.post", fake_post)

    client = create_app().test_client()
    response = client.post(
        "/publish/buffer",
        json={
            "account_id": "account-123",
            "access_token": "token-abc",
            "payload": {"text": "hello", "profile_ids": ["profile-1"]},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert response.get_json()["update_id"] == "buffer-123"
    assert captured["account_id"] == "account-123"
    assert captured["url"] == "https://api.buffer.com"
    assert "createPost" in captured["json"]["query"]
    assert captured["json"]["variables"]["input"] == {
        "text": "hello",
        "channelId": "profile-1",
        "schedulingType": "automatic",
        "mode": "addToQueue",
        "assets": [],
    }
    assert captured["headers"] == {
        "Authorization": "Bearer token-abc",
        "Content-Type": "application/json",
    }
    assert captured["proxies"] == {
        "http": "http://proxy-url",
        "https": "http://proxy-url",
    }
    assert captured["timeout"] == 30


def test_publish_buffer_uses_configured_service_url_and_timeout(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "services": {"buffer_graphql_url": "https://example.com/graphql"},
          "timeouts": {"buffer_publish_seconds": 45}
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    captured = {}

    def fake_generate_proxy_url(account_id):
        return "http://proxy-url"

    def fake_post(url, json, headers, proxies, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["input"] = json["variables"]["input"]
        return successful_graphql_response()

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.buffer_client.requests.post", fake_post)

    client = create_app().test_client()
    response = client.post(
        "/publish/buffer",
        json={
            "account_id": "account-123",
            "access_token": "token-abc",
            "payload": {
                "text": "hello",
                "profile_ids": ["profile-1"],
                "media": {"link": "https://cdn.example.com/video.mp4"},
                "scheduled_at": "2026-07-11T09:00:00+08:00",
            },
        },
    )

    assert response.status_code == 200
    assert captured == {
        "url": "https://example.com/graphql",
        "timeout": 45,
        "input": {
            "text": "hello",
            "channelId": "profile-1",
            "schedulingType": "automatic",
            "mode": "customScheduled",
            "dueAt": "2026-07-11T01:00:00.000Z",
            "assets": [
                {"video": {"url": "https://cdn.example.com/video.mp4"}}
            ],
        },
    }


def test_publish_buffer_requires_account_id_access_token_and_payload():
    client = create_app().test_client()

    response = client.post("/publish/buffer", json={"account_id": "account-123"})

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "account_id, access_token, and payload are required"
    }


def test_publish_buffer_uses_stored_token_and_profile_ids(monkeypatch, tmp_path):
    from init_db import init_db
    import sqlite3

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
                "stored-token",
                '["profile-a", "profile-b"]',
                "active",
            ),
        )
    captured = {}

    def fake_generate_proxy_url(account_id):
        return "http://proxy-url"

    def fake_post(url, json, headers, proxies, timeout):
        captured.setdefault("inputs", []).append(json["variables"]["input"])
        captured["headers"] = headers
        channel_id = json["variables"]["input"]["channelId"]
        return successful_graphql_response(f"post-{channel_id}")

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.buffer_client.requests.post", fake_post)

    app = create_app()
    app.config["ACCOUNTS_DB_PATH"] = db_path
    response = app.test_client().post(
        "/publish/buffer",
        json={
            "account_id": "buffer-account-1",
            "payload": {"text": "hello", "profile_ids": ["ignored"]},
        },
    )

    assert response.status_code == 200
    assert captured["headers"] == {
        "Authorization": "Bearer stored-token",
        "Content-Type": "application/json",
    }
    assert [item["channelId"] for item in captured["inputs"]] == [
        "profile-a",
        "profile-b",
    ]


def test_publish_buffer_returns_typed_graphql_error(monkeypatch):
    def fake_generate_proxy_url(account_id):
        return "http://proxy-url"

    def fake_post(url, json, headers, proxies, timeout):
        return FakeGraphQLResponse(
            {"data": {"createPost": {"message": "TikTok channel is disconnected"}}}
        )

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.buffer_client.requests.post", fake_post)

    response = create_app().test_client().post(
        "/publish/buffer",
        json={
            "account_id": "account-123",
            "access_token": "token-abc",
            "payload": {"text": "hello", "profile_ids": ["profile-1"]},
        },
    )

    assert response.status_code == 502
    assert response.get_json() == {"error": "TikTok channel is disconnected"}


def test_publish_buffer_returns_request_exception(monkeypatch):
    def fake_generate_proxy_url(account_id):
        return "http://proxy-url"

    def fake_post(url, json, headers, proxies, timeout):
        raise requests.exceptions.ReadTimeout("buffer request timed out")

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.buffer_client.requests.post", fake_post)

    client = create_app().test_client()
    response = client.post(
        "/publish/buffer",
        json={
            "account_id": "account-123",
            "access_token": "token-abc",
            "payload": {"text": "hello", "profile_ids": ["profile-1"]},
        },
    )

    assert response.status_code == 502
    assert response.get_json() == {
        "error": "代理连接 Buffer 超时，请检查代理协议、认证信息或更换可用代理后重试。"
    }


def test_extract_tiktok_url_from_buffer_payload_recursively_finds_platform_link():
    payload = {
        "data": {
            "post": {
                "id": "buffer-post-1",
                "serviceUpdateUrl": "https://www.tiktok.com/@brand/video/123",
                "assets": [{"source": "https://cdn.example.com/video.mp4"}],
            }
        }
    }

    assert (
        extract_tiktok_url_from_buffer_payload(payload)
        == "https://www.tiktok.com/@brand/video/123"
    )
