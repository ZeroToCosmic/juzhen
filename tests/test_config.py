from gateway.config import load_proxy_config


def test_load_proxy_config_reads_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "missing-config.json"))
    monkeypatch.setenv("PROXY_HOST", "proxy.example.com")
    monkeypatch.setenv("PROXY_PORT", "8080")
    monkeypatch.setenv("PROXY_USER", "session-user")
    monkeypatch.setenv("PROXY_PASS", "secret")

    config = load_proxy_config()

    assert config.host == "proxy.example.com"
    assert config.port == "8080"
    assert config.username == "session-user"
    assert config.password == "secret"
