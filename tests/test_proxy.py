from gateway.proxy import generate_proxy_url
from gateway.settings_store import save_settings


def test_generate_proxy_url_adds_account_id_to_proxy_username(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "missing-config.json"))
    monkeypatch.setenv("PROXY_HOST", "proxy.example.com")
    monkeypatch.setenv("PROXY_PORT", "8080")
    monkeypatch.setenv("PROXY_USER", "base-user")
    monkeypatch.setenv("PROXY_PASS", "secret")

    proxy_url = generate_proxy_url("account-123")

    assert (
        proxy_url
        == "http://base-user-zone-custom-session-account-123:secret@proxy.example.com:8080"
    )


def test_generate_proxy_url_uses_proxy_pool_when_available(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "items": [
                    {
                        "host": "192.53.69.143",
                        "port": "6781",
                        "username": "nsucssou",
                        "password": "3mjeb2p392yk",
                    },
                    {
                        "host": "203.0.113.8",
                        "port": "9000",
                        "username": "user2",
                        "password": "pass2",
                    },
                ]
            }
        },
        config_path,
    )

    first = generate_proxy_url("account-123")
    second = generate_proxy_url("account-123")

    assert first == second
    assert "-zone-custom-session-account-123" not in first
    assert first in {
        "socks5h://nsucssou:3mjeb2p392yk@192.53.69.143:6781",
        "socks5h://user2:pass2@203.0.113.8:9000",
    }


def test_generate_proxy_url_supports_http_static_proxy_pool(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "protocol": "http",
                "raw": "203.0.113.8:9000:user2:pass2",
            }
        },
        config_path,
    )

    assert generate_proxy_url("account-123") == (
        "http://user2:pass2@203.0.113.8:9000"
    )
