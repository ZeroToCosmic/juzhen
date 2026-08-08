import requests
import sqlite3

from gateway.app import create_app
from init_db import init_db


def test_check_ip_returns_ipinfo_through_account_proxy(monkeypatch):
    captured = {}

    def fake_generate_proxy_url(account_id):
        captured["account_id"] = account_id
        return "http://proxy-url"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ip": "203.0.113.10", "country": "US"}

    def fake_get(url, proxies, timeout):
        captured["url"] = url
        captured["proxies"] = proxies
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.ip_checker.requests.get", fake_get)

    client = create_app().test_client()
    response = client.post("/check_ip", json={"account_id": "account-123"})

    assert response.status_code == 200
    assert response.get_json() == {"ip": "203.0.113.10", "country": "US"}
    assert captured == {
        "account_id": "account-123",
        "url": "https://ipinfo.io/json",
        "proxies": {"http": "http://proxy-url", "https": "http://proxy-url"},
        "timeout": 10,
    }


def test_check_ip_uses_configured_service_url_and_timeout(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "services": {"ipinfo_url": "https://example.com/ip.json"},
          "timeouts": {"ip_check_seconds": 12}
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    captured = {}

    def fake_generate_proxy_url(account_id):
        return "http://proxy-url"

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ip": "203.0.113.10"}

    def fake_get(url, proxies, timeout):
        captured["url"] = url
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.ip_checker.requests.get", fake_get)

    client = create_app().test_client()
    response = client.post("/check_ip", json={"account_id": "account-123"})

    assert response.status_code == 200
    assert captured == {"url": "https://example.com/ip.json", "timeout": 12}


def test_check_ip_requires_account_id():
    client = create_app().test_client()

    response = client.post("/check_ip", json={})

    assert response.status_code == 400
    assert response.get_json() == {"error": "account_id is required"}


def test_check_ip_returns_bad_gateway_when_proxy_request_fails(monkeypatch):
    def fake_generate_proxy_url(account_id):
        return "http://proxy-url"

    def fake_get(url, proxies, timeout):
        raise requests.RequestException("proxy failed")

    monkeypatch.setattr("gateway.publish_queue.generate_proxy_url", fake_generate_proxy_url)
    monkeypatch.setattr("gateway.ip_checker.requests.get", fake_get)

    client = create_app().test_client()
    response = client.post("/check_ip", json={"account_id": "account-123"})

    assert response.status_code == 502
    assert response.get_json() == {"error": "failed to fetch ip info through proxy"}


def test_check_ip_prefers_assigned_account_proxy(monkeypatch, tmp_path):
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
            (
                "buffer-account-1",
                "buffer-account-1",
                "203.0.113.8:9000:user2:pass2",
                "Brand One",
                "token-one",
                "active",
            ),
        )
    captured = {}

    def fake_fetch_ip_info(proxy_url):
        captured["proxy_url"] = proxy_url
        return {"ip": "203.0.113.8"}

    monkeypatch.setattr("gateway.routes_ip.fetch_ip_info", fake_fetch_ip_info)

    app = create_app()
    app.config["ACCOUNTS_DB_PATH"] = db_path
    response = app.test_client().post(
        "/check_ip",
        json={"account_id": "buffer-account-1"},
    )

    assert response.status_code == 200
    assert captured["proxy_url"] == "socks5h://user2:pass2@203.0.113.8:9000"
