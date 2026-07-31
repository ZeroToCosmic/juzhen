import copy
from contextlib import closing
import json
import sqlite3

import pytest

from gateway.app import create_app, select_model_for_generation
from gateway.settings_store import save_settings
from init_db import init_db


@pytest.mark.parametrize(
    ("method", "path", "expected_status"),
    [
        ("GET", "/", 200),
        ("GET", "/api/browser/elements", 200),
        ("PUT", "/api/browser/elements", 400),
        ("GET", "/api/browser/strategies", 200),
        ("PUT", "/api/browser/strategies", 400),
        ("POST", "/api/browser/execute-strategy", 400),
        ("GET", "/api/settings", 200),
        ("PUT", "/api/settings", 200),
    ],
)
def test_administrator_route_matrix(
    admin_client,
    method,
    path,
    expected_status,
):
    response = admin_client.open(
        path,
        method=method,
        json={} if method != "GET" else None,
    )

    assert response.status_code == expected_status
    assert response.status_code not in {401, 403}


def test_settings_page_is_served():
    client = create_app().test_client()

    response = client.get("/settings")

    assert response.status_code == 200
    assert "代理主机".encode("utf-8") in response.data


def test_model_presets_api_returns_only_public_provider_details():
    response = create_app().test_client().get("/api/model-presets")

    assert response.status_code == 200
    assert response.get_json()["deepseek"]["models"] == [
        "deepseek-chat",
        "deepseek-reasoner",
    ]
    assert "api_key" not in json.dumps(response.get_json())


def test_settings_api_masks_all_stored_credentials_and_preserves_them_on_unrelated_save(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    secrets = {
        "proxy_password": "fake-proxy-password",
        "proxy_pool_password": "fake-pool-password",
        "r2_token": "fake-r2-token",
        "r2_access": "fake-r2-access",
        "r2_secret": "fake-r2-secret",
        "adspower_key": "fake-adspower-key",
        "model_key": "fake-model-key",
        "selector_probe_secret": "fake-selector-probe-secret",
    }
    save_settings(
        {
            "proxy": {"host": "proxy.example.test", "password": secrets["proxy_password"]},
            "proxy_pool": {
                "raw": f"203.0.113.8:9000:user:{secrets['proxy_pool_password']}",
            },
            "r2": {
                "account_id": "account-id",
                "account_token": secrets["r2_token"],
                "access_key_id": secrets["r2_access"],
                "secret_access_key": secrets["r2_secret"],
                "bucket": "bucket-name",
            },
            "adspower": {
                "base_url": "http://local.adspower.net:50325",
                "api_key": secrets["adspower_key"],
            },
            "models": {
                "default_model_id": "model-a",
                "items": [_model_item("model-a", secrets["model_key"])],
            },
            "selector_probe": {
                "enabled": False,
                "test_profile_ids": ["private-profile-a", "private-profile-b"],
                "webhook": {
                    "enabled": True,
                    "type": "generic",
                    "url": "https://hooks.example.test/probe",
                    "signing_secret": secrets["selector_probe_secret"],
                },
            },
        },
        config_path,
    )
    client = create_app().test_client()

    loaded = client.get("/api/settings").get_json()
    serialized = json.dumps(loaded)

    assert all(secret not in serialized for secret in secrets.values())
    assert loaded["proxy"]["password"] == ""
    assert loaded["proxy_pool"]["raw"] == ""
    assert loaded["proxy_pool"]["items"] == []
    assert loaded["r2"]["account_token"] == ""
    assert loaded["r2"]["access_key_id"] == ""
    assert loaded["r2"]["secret_access_key"] == ""
    assert loaded["adspower"]["api_key"] == ""
    assert loaded["models"]["items"][0]["api_key"] == ""
    assert "selector_probe" not in loaded
    assert loaded["_secrets_configured"] == {
        "proxy": {"password": True},
        "proxy_pool": {"raw": True},
        "r2": {
            "account_token": True,
            "access_key_id": True,
            "secret_access_key": True,
        },
        "adspower": {"api_key": True},
        "models": {"items": [{"api_key": True}]},
        "selector_probe": {"webhook": {"signing_secret": True}},
    }

    loaded["browser"]["task_goal"] = "unrelated-change"
    response = client.put("/api/settings", json=loaded)

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["proxy"]["password"] == secrets["proxy_password"]
    assert secrets["proxy_pool_password"] in saved["proxy_pool"]["raw"]
    assert saved["r2"]["account_token"] == secrets["r2_token"]
    assert saved["r2"]["access_key_id"] == secrets["r2_access"]
    assert saved["r2"]["secret_access_key"] == secrets["r2_secret"]
    assert saved["adspower"]["api_key"] == secrets["adspower_key"]
    assert saved["models"]["items"][0]["api_key"] == secrets["model_key"]
    assert (
        saved["selector_probe"]["webhook"]["signing_secret"]
        == secrets["selector_probe_secret"]
    )
    assert saved["selector_probe"]["test_profile_ids"] == [
        "private-profile-a",
        "private-profile-b",
    ]
    assert "_secrets_configured" not in saved


def test_model_preset_switch_keeps_existing_key_and_enabled_state_private(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "models": {
                "default_model_id": "grok-main",
                "items": [
                    {
                        "id": "grok-main",
                        "provider": "grok",
                        "enabled": True,
                        "base_url": "https://api.x.ai/v1",
                        "api_key": "fake-existing-model-key",
                        "model": "grok-4.5",
                        "mode": "responses",
                    }
                ],
            }
        },
        config_path,
    )
    client = create_app().test_client()

    assert client.get("/api/settings").get_json()["models"]["items"][0]["api_key"] == ""

    response = client.put(
        "/api/settings",
        json={
            "models": {
                "items": [
                    {
                        "id": "grok-main",
                        "provider": "deepseek",
                        "base_url": "https://api.deepseek.com/v1",
                        "api_key": "",
                        "model": "deepseek-chat",
                        "mode": "chat",
                        "enabled": True,
                    }
                ]
            }
        },
    )

    assert response.status_code == 200
    assert response.get_json()["models"]["items"][0]["api_key"] == ""
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["models"]["items"][0]["api_key"] == "fake-existing-model-key"
    assert saved["models"]["items"][0]["enabled"] is True


def _model_item(model_id, api_key, *, enabled=True, provider="custom", model=None):
    item = {
        "provider": provider,
        "enabled": enabled,
        "base_url": "https://models.example.test/v1",
        "api_key": api_key,
        "model": model or f"model-{model_id or 'legacy'}",
        "mode": "chat",
    }
    if model_id is not None:
        item["id"] = model_id
    return item


def _without_key(item):
    submitted = copy.deepcopy(item)
    submitted["api_key"] = ""
    return submitted


def test_settings_api_reorders_submitted_models_and_preserves_default_and_matching_keys(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    model_a = _model_item("A", "fake-key-a")
    model_b = _model_item("B", "fake-key-b")
    save_settings(
        {"models": {"default_model_id": "A", "items": [model_a, model_b]}},
        config_path,
    )

    response = create_app().test_client().put(
        "/api/settings",
        json={"models": {"items": [_without_key(model_b), _without_key(model_a)]}},
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.get_json()["models"]["items"]] == ["B", "A"]
    assert all(item["api_key"] == "" for item in response.get_json()["models"]["items"])
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in saved["models"]["items"]] == ["B", "A"]
    assert [item["api_key"] for item in saved["models"]["items"]] == [
        "fake-key-b",
        "fake-key-a",
    ]
    assert saved["models"]["default_model_id"] == "A"


def test_settings_api_deletes_unsubmitted_models_and_explicitly_updates_default(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    model_a = _model_item("A", "fake-key-a")
    model_b = _model_item("B", "fake-key-b")
    save_settings(
        {"models": {"default_model_id": "A", "items": [model_a, model_b]}},
        config_path,
    )
    submitted_b = copy.deepcopy(model_b)
    submitted_b["api_key"] = "fake-key-b-updated"

    response = create_app().test_client().put(
        "/api/settings",
        json={"models": {"default_model_id": "B", "items": [submitted_b]}},
    )

    assert response.status_code == 200
    assert response.get_json()["models"]["items"][0]["api_key"] == ""
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in saved["models"]["items"]] == ["B"]
    assert saved["models"]["items"][0]["api_key"] == "fake-key-b-updated"
    assert saved["models"]["default_model_id"] == "B"


def test_model_fallback_uses_first_enabled_item_in_submitted_order(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    model_a = _model_item("A", "fake-key-a")
    model_b = _model_item("B", "fake-key-b")
    save_settings(
        {"models": {"default_model_id": "", "items": [model_a, model_b]}},
        config_path,
    )

    create_app().test_client().put(
        "/api/settings",
        json={"models": {"items": [_without_key(model_b), _without_key(model_a)]}},
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert select_model_for_generation(saved)["id"] == "B"


def test_blank_key_without_id_does_not_reuse_key_from_identified_model(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    identified = _model_item("A", "fake-key-a")
    submitted = _model_item(None, "")
    save_settings(
        {"models": {"default_model_id": "A", "items": [identified]}},
        config_path,
    )

    create_app().test_client().put(
        "/api/settings",
        json={"models": {"items": [submitted]}},
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["models"]["items"][0]["api_key"] == ""


def test_blank_key_preserves_single_legacy_model_without_id(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    legacy = _model_item(None, "fake-legacy-key")
    save_settings(
        {"models": {"default_model_id": "", "items": [legacy]}},
        config_path,
    )
    submitted = _model_item(None, "", provider="grok", model="grok-4.5")

    create_app().test_client().put(
        "/api/settings",
        json={"models": {"items": [submitted]}},
    )

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["models"]["items"][0]["api_key"] == "fake-legacy-key"


def test_restore_settings_response_masks_restored_model_keys(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {"models": {"default_model_id": "A", "items": [_model_item("A", "fake-key-a")]}},
        config_path,
    )
    save_settings({"browser": {"task_goal": "create-backup"}}, config_path)

    response = create_app().test_client().post("/api/settings/restore-latest")

    assert response.status_code == 200
    assert response.get_json()["settings"]["models"]["items"][0]["api_key"] == ""


def _gui_settings_payload(loaded, *, first_model_updates=None, browser_updates=None):
    loaded_models = copy.deepcopy(loaded["models"])
    loaded_models["items"][0].update(first_model_updates or {})
    return {
        "models": loaded_models,
        "browser": browser_updates or {},
    }


def test_gui_unrelated_settings_save_preserves_all_loaded_models_and_later_default(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    model_a = _model_item("A", "fake-key-a")
    model_b = _model_item("B", "fake-key-b", provider="grok")
    save_settings(
        {"models": {"default_model_id": "B", "items": [model_a, model_b]}},
        config_path,
    )
    client = create_app().test_client()
    loaded = client.get("/api/settings").get_json()

    response = client.put(
        "/api/settings",
        json=_gui_settings_payload(
            loaded,
            browser_updates={"task_goal": "gui-unrelated-save"},
        ),
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in saved["models"]["items"]] == ["A", "B"]
    assert [item["api_key"] for item in saved["models"]["items"]] == [
        "fake-key-a",
        "fake-key-b",
    ]
    assert saved["models"]["items"][1]["provider"] == "grok"
    assert saved["models"]["default_model_id"] == "B"
    assert select_model_for_generation(saved)["id"] == "B"


def test_gui_first_model_edit_preserves_loaded_model_tail_and_order(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    model_a = _model_item("A", "fake-key-a")
    model_b = _model_item("B", "fake-key-b", provider="grok")
    save_settings(
        {"models": {"default_model_id": "B", "items": [model_a, model_b]}},
        config_path,
    )
    client = create_app().test_client()
    loaded = client.get("/api/settings").get_json()

    response = client.put(
        "/api/settings",
        json=_gui_settings_payload(
            loaded,
            first_model_updates={"model": "model-A-edited"},
        ),
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert [item["id"] for item in saved["models"]["items"]] == ["A", "B"]
    assert saved["models"]["items"][0]["model"] == "model-A-edited"
    assert saved["models"]["items"][0]["api_key"] == "fake-key-a"
    assert saved["models"]["items"][1] == model_b
    assert saved["models"]["default_model_id"] == "B"


def test_blank_key_does_not_inherit_from_duplicate_existing_id(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    first = _model_item("A", "fake-duplicate-key-one", model="model-one")
    second = _model_item("A", "fake-duplicate-key-two", model="model-two")
    save_settings(
        {"models": {"default_model_id": "A", "items": [first, second]}},
        config_path,
    )
    submitted = _model_item("A", "", model="model-submitted")

    response = create_app().test_client().put(
        "/api/settings",
        json={"models": {"items": [submitted]}},
    )

    assert response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["models"]["items"][0]["api_key"] == ""


def test_settings_api_rejects_duplicate_submitted_model_ids_without_writing(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    original = _model_item("A", "fake-original-key")
    save_settings(
        {"models": {"default_model_id": "A", "items": [original]}},
        config_path,
    )

    response = create_app().test_client().put(
        "/api/settings",
        json={
            "models": {
                "items": [
                    _model_item("A", "", model="model-one"),
                    _model_item("A", "", model="model-two"),
                ]
            }
        },
    )

    assert response.status_code == 400
    assert "duplicate" in response.get_json()["error"].lower()
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["models"]["items"] == [original]



def test_dashboard_script_does_not_call_removed_browser_session_refresh():
    response = create_app().test_client().get("/")
    html = response.get_data(as_text=True)

    assert "refreshBrowserSessions();" not in html
    assert "refreshAdsPowerWindows();" in html


def test_settings_api_reads_and_saves_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    client = create_app().test_client()

    save_response = client.put(
        "/api/settings",
        json={
            "proxy": {
                "host": "proxy.example.com",
                "port": "8080",
                "username": "user",
                "password": "secret",
            },
            "services": {
                "ipinfo_url": "https://example.com/ip.json",
                "buffer_graphql_url": "https://example.com/graphql",
            },
            "timeouts": {
                "ip_check_seconds": 12,
                "buffer_publish_seconds": 34,
            },
            "browser": {
                "cdp_url": "http://127.0.0.1:9222",
                "task_goal": "publish",
            },
            "proxy_pool": {
                "protocol": "http",
                "raw": "192.53.69.143:6781:nsucssou:3mjeb2p392yk",
            },
        },
    )

    assert save_response.status_code == 200
    assert save_response.get_json()["proxy"]["host"] == "proxy.example.com"
    assert save_response.get_json()["proxy"]["password"] == ""
    assert save_response.get_json()["proxy_pool"]["raw"] == ""
    assert save_response.get_json()["proxy_pool"]["items"] == []
    assert save_response.get_json()["proxy_pool"]["protocol"] == "http"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["proxy"]["password"] == "secret"
    assert saved["proxy_pool"]["items"][0]["host"] == "192.53.69.143"

    get_response = client.get("/api/settings")

    assert get_response.status_code == 200
    assert get_response.get_json()["browser"]["task_goal"] == "publish"
    assert get_response.get_json()["proxy_pool"]["raw"] == ""


def test_settings_api_partial_update_preserves_existing_sections(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "raw": "192.53.69.143:6781:nsucssou:3mjeb2p392yk",
            },
            "r2": {
                "account_id": "account-1",
                "account_token": "token-1",
                "access_key_id": "access-1",
                "secret_access_key": "secret-1",
                "bucket": "tiktokvideo",
                "endpoint_url": "https://account-1.r2.cloudflarestorage.com",
            },
            "adspower": {
                "base_url": "http://local.adspower.net:50325",
                "api_key": "adspower-key",
            },
        },
        config_path,
    )

    response = create_app().test_client().put(
        "/api/settings",
        json={"browser": {"default_url": "https://example.com/"}},
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["browser"]["default_url"] == "https://example.com/"
    assert data["proxy_pool"]["raw"] == ""
    assert data["r2"]["bucket"] == "tiktokvideo"
    assert data["r2"]["access_key_id"] == ""
    assert data["adspower"]["base_url"] == "http://local.adspower.net:50325"
    assert data["adspower"]["api_key"] == ""
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["proxy_pool"]["raw"].startswith("192.53.69.143")
    assert saved["r2"]["access_key_id"] == "access-1"
    assert saved["adspower"]["api_key"] == "adspower-key"


def test_settings_api_does_not_clear_existing_credentials_from_blank_form_fields(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy": {"password": "proxy-password"},
            "r2": {
                "account_id": "account-1",
                "account_token": "token-1",
                "access_key_id": "access-1",
                "secret_access_key": "secret-1",
                "bucket": "tiktokvideo",
                "public_base_url": "https://cdn.example.test",
                "prefix": "videos/",
            },
            "adspower": {
                "base_url": "http://local.adspower.net:50325",
                "api_key": "adspower-key",
            },
        },
        config_path,
    )

    response = create_app().test_client().put(
        "/api/settings",
        json={
            "proxy": {"password": ""},
            "r2": {
                "account_id": "",
                "account_token": "",
                "access_key_id": "",
                "secret_access_key": "",
                "bucket": "",
                "public_base_url": "",
                "prefix": "",
            },
            "adspower": {"base_url": "", "api_key": ""},
        },
    )

    data = response.get_json()
    assert response.status_code == 200
    assert data["proxy"]["password"] == ""
    assert data["r2"]["account_id"] == "account-1"
    assert data["r2"]["account_token"] == ""
    assert data["r2"]["access_key_id"] == ""
    assert data["r2"]["secret_access_key"] == ""
    assert data["r2"]["bucket"] == "tiktokvideo"
    assert data["r2"]["public_base_url"] == "https://cdn.example.test"
    assert data["r2"]["prefix"] == "videos/"
    assert data["adspower"]["base_url"] == "http://local.adspower.net:50325"
    assert data["adspower"]["api_key"] == ""
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["proxy"]["password"] == "proxy-password"
    assert saved["r2"]["account_token"] == "token-1"
    assert saved["r2"]["access_key_id"] == "access-1"
    assert saved["r2"]["secret_access_key"] == "secret-1"
    assert saved["adspower"]["api_key"] == "adspower-key"


def test_settings_api_replaces_invalid_runtime_zero_values_with_defaults(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"timeouts":{"ip_check_seconds":0,"buffer_publish_seconds":0},'
        '"publish_queue":{"interval_seconds":0},'
        '"publish_sampling":{"interval_seconds":0,"min_age_hours":0}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    data = create_app().test_client().get("/api/settings").get_json()

    assert data["timeouts"] == {
        "ip_check_seconds": 10,
        "buffer_publish_seconds": 30,
    }
    assert data["publish_queue"]["interval_seconds"] == 8
    assert data["publish_sampling"]["interval_seconds"] == 300
    assert data["publish_sampling"]["min_age_hours"] == 0


def test_settings_default_existing_proxy_pool_to_socks5(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"proxy_pool":{"raw":"203.0.113.8:9000:user2:pass2"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    response = create_app().test_client().get("/api/settings")

    assert response.status_code == 200
    assert response.get_json()["proxy_pool"]["protocol"] == "socks5"


def test_settings_api_rejects_invalid_proxy_pool_line(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()

    response = client.put(
        "/api/settings",
        json={"proxy_pool": {"raw": "192.53.69.143:6781:only-user"}},
    )

    assert response.status_code == 400
    assert "host:port:username:password" in response.get_json()["error"]


def test_settings_status_and_restore_latest_recover_a_corrupt_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings({"browser": {"task_goal": "recoverable"}}, config_path)
    save_settings({"browser": {"task_goal": "newer"}}, config_path)
    config_path.write_text('{"browser":', encoding="utf-8")
    client = create_app().test_client()

    health_response = client.get("/api/settings/status")

    assert health_response.status_code == 200
    assert health_response.get_json()["ok"] is False
    assert health_response.get_json()["backup_available"] is True
    assert set(health_response.get_json()) == {
        "ok",
        "error",
        "backup_available",
        "latest_backup",
    }

    blocked_save = client.put("/api/settings", json={"browser": {"task_goal": "blocked"}})
    assert blocked_save.status_code == 409
    assert config_path.read_text(encoding="utf-8") == '{"browser":'

    restore_response = client.post("/api/settings/restore-latest")

    assert restore_response.status_code == 200
    assert restore_response.get_json()["settings"]["browser"]["task_goal"] == "recoverable"
    assert restore_response.get_json()["status"]["ok"] is True


def test_restore_latest_settings_returns_404_with_only_an_invalid_backup(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"browser":', encoding="utf-8")
    config_path.with_name(
        "config.json.backup.00000000000000000001.100"
    ).write_text('{"browser":', encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()

    health = client.get("/api/settings/status").get_json()

    assert health["backup_available"] is False
    assert health["latest_backup"] is None

    response = client.post("/api/settings/restore-latest")

    assert response.status_code == 404
    assert "备份" in response.get_json()["error"]


def test_proxy_pool_status_counts_assigned_sessions(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    db_path = tmp_path / "accounts.db"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "raw": "192.53.69.143:6781:nsucssou:3mjeb2p392yk\n203.0.113.8:9000:user2:pass2"
            }
        },
        config_path,
    )
    init_db(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id,
                buffer_account_id,
                proxy_session,
                status
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                "ads-1",
                "buffer-1",
                "192.53.69.143:6781:nsucssou:3mjeb2p392yk",
                "active",
            ),
        )

    app = create_app()
    app.config["ACCOUNTS_DB_PATH"] = db_path
    client = app.test_client()

    response = client.get("/api/proxy-pool/status")

    assert response.status_code == 200
    assert response.get_json()["total"] == 2
    assert response.get_json()["assigned"] == 1
    assert response.get_json()["remaining"] == 1
    assert response.get_json()["items"][0]["assigned"] is True


def test_proxy_pool_status_returns_paginated_items(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    db_path = tmp_path / "accounts.db"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy_pool": {
                "raw": "\n".join(
                    f"203.0.113.{index}:9000:user{index}:pass{index}"
                    for index in range(1, 501)
                )
            }
        },
        config_path,
    )
    init_db(db_path)
    app = create_app()
    app.config["ACCOUNTS_DB_PATH"] = db_path

    response = app.test_client().get("/api/proxy-pool/status?page=3&page_size=50&search=user")

    data = response.get_json()
    assert response.status_code == 200
    assert data["total"] == 500
    assert data["filtered_total"] == 500
    assert data["page"] == 3
    assert data["page_size"] == 50
    assert len(data["items"]) == 50
    assert data["items"][0]["username"] == "user101"


def _assert_migrated_xpath_elements(actual, expected):
    assert set(actual) == set(expected)
    for alias, xpath in expected.items():
        definition = actual[alias]
        assert definition["scope"] == "page"
        assert len(definition["locators"]) == 1
        locator = definition["locators"][0]
        assert locator["id"].startswith("locator-")
        assert locator["type"] == "xpath"
        assert locator["value"] == xpath
        assert locator["enabled"] is True
        assert locator["fallback"] is True


def test_browser_local_save_routes_preserve_existing_config(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "proxy": {"host": "proxy.example.test", "port": "8080"},
            "models": {
                "default_model_id": "test-model",
                "items": [
                    {
                        "id": "test-model",
                        "provider": "test-provider",
                        "enabled": True,
                        "base_url": "https://model.example.test/v1",
                        "api_key": "fake-model-api-key",
                        "model": "test-model-name",
                        "mode": "chat",
                    }
                ],
            },
            "r2": {
                "account_id": "test-account",
                "access_key_id": "fake-r2-access-key",
                "secret_access_key": "fake-r2-secret-key",
                "bucket": "test-bucket",
                "public_base_url": "https://cdn.example.test",
                "prefix": "videos/",
            },
            "adspower": {
                "base_url": "http://adspower.example.test:50325",
                "api_key": "fake-adspower-api-key",
            },
        },
        config_path,
    )
    client = create_app().test_client()

    def assert_existing_config_is_preserved(saved):
        assert saved["proxy"]["host"] == "proxy.example.test"
        assert saved["models"]["default_model_id"] == "test-model"
        assert saved["models"]["items"][0]["api_key"] == "fake-model-api-key"
        assert saved["r2"]["account_id"] == "test-account"
        assert saved["r2"]["access_key_id"] == "fake-r2-access-key"
        assert saved["r2"]["secret_access_key"] == "fake-r2-secret-key"
        assert saved["r2"]["bucket"] == "test-bucket"
        assert saved["r2"]["public_base_url"] == "https://cdn.example.test"
        assert saved["r2"]["prefix"] == "videos/"
        assert saved["adspower"]["base_url"] == "http://adspower.example.test:50325"
        assert saved["adspower"]["api_key"] == "fake-adspower-api-key"

    elements_response = client.put(
        "/api/browser/elements",
        json={"elements": {
            "entry": "//entry",
            "input": "//textarea",
            "submit": "//button",
        }},
    )

    assert elements_response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    _assert_migrated_xpath_elements(
        saved["browser"]["action_elements"],
        {"entry": "//entry", "input": "//textarea", "submit": "//button"},
    )
    assert saved["browser"]["strategy_schema_version"] == 3
    assert_existing_config_is_preserved(saved)

    patterns_response = client.put(
        "/api/browser/patterns",
        json={"patterns": [_mouse_pattern()]},
    )

    assert patterns_response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["browser"]["interaction_patterns"] == [_mouse_pattern()]
    assert_existing_config_is_preserved(saved)

    strategies_response = client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "block", "name": "Block", "run_mode": "once", "batch_size": 1,
            "actions": [_block_click("entry")],
        }]},
    )

    assert strategies_response.status_code == 200
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["browser"]["block_strategies"][0]["id"] == "block"
    assert_existing_config_is_preserved(saved)


def _block_click(element):
    return {
        "id": "a",
        "type": "click",
        "params": {
            "element": element,
            "button": "left",
            "click_count": 1,
            "hold_seconds": [0.05, 0.15],
            "trajectory": {"source": "builtin", "id": "bezier"},
        },
    }


def _mouse_pattern(pattern_id="mouse"):
    return {
        "id": pattern_id,
        "name": "Mouse pattern",
        "type": "mouse",
        "data": {
            "points": [
                {"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0},
                {"x_ratio": 0.6, "y_ratio": 0.6, "dt_ms": 10},
            ],
            "sample_count": 2,
            "total_duration_ms": 10,
        },
    }


def test_strategy_resources_save_survive_new_app_instance(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    first = create_app().test_client()

    saved_elements = first.put("/api/browser/elements", json={"elements": {"入口": "//button"}})
    pattern = _mouse_pattern()
    saved_patterns = first.put("/api/browser/patterns", json={"patterns": [pattern]})
    action = _block_click("入口")
    action["params"]["trajectory"] = {"source": "pattern", "id": pattern["id"]}
    strategy = {
        "id": "persisted",
        "name": "Persisted strategy",
        "run_mode": "once",
        "batch_size": 1,
        "actions": [action],
    }
    saved_strategies = first.put("/api/browser/strategies", json={"strategies": [strategy]})
    second = create_app().test_client()
    persisted = json.loads(config_path.read_text(encoding="utf-8"))["browser"]

    assert [saved_elements.status_code, saved_patterns.status_code, saved_strategies.status_code] == [200, 200, 200]
    canonical_elements = saved_elements.get_json()["elements"]
    _assert_migrated_xpath_elements(canonical_elements, {"入口": "//button"})
    assert saved_patterns.get_json()["patterns"] == [pattern]
    assert saved_strategies.get_json()["strategies"][0]["id"] == "persisted"
    assert second.get("/api/browser/elements").get_json()["elements"] == canonical_elements
    assert second.get("/api/browser/patterns").get_json()["patterns"] == [pattern]
    assert second.get("/api/browser/strategies").get_json()["strategies"][0]["id"] == "persisted"
    assert persisted["action_elements"] == canonical_elements
    assert persisted["strategy_schema_version"] == 3
    assert persisted["interaction_patterns"] == [pattern]
    assert persisted["block_strategies"][0]["id"] == "persisted"


def test_element_deletion_replaces_mapping_and_preserves_unrelated_settings(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "models": {"default_model_id": "keep-me"},
            "r2": {"account_id": "keep-account"},
            "adspower": {"api_key": "keep-key"},
            "browser": {
                "cdp_url": "http://keep-browser",
                "strategy_schema_version": 2,
                "action_elements": {"keep": "//keep", "remove": "//remove"},
                "interaction_patterns": [],
                "block_strategies": [],
            },
        },
        config_path,
    )

    response = create_app().test_client().put(
        "/api/browser/elements",
        json={"elements": {"keep": "//keep"}},
    )
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert response.status_code == 200
    _assert_migrated_xpath_elements(
        saved["browser"]["action_elements"], {"keep": "//keep"}
    )
    assert saved["browser"]["strategy_schema_version"] == 3
    assert saved["browser"]["cdp_url"] == "http://keep-browser"
    assert saved["models"]["default_model_id"] == "keep-me"
    assert saved["r2"]["account_id"] == "keep-account"
    assert saved["adspower"]["api_key"] == "keep-key"


def test_referenced_element_delete_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()
    assert client.put("/api/browser/elements", json={"elements": {"entry": "//entry"}}).status_code == 200
    assert client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
            "actions": [_block_click("entry")],
        }]},
    ).status_code == 200

    response = client.put("/api/browser/elements", json={"elements": {}})

    assert response.status_code == 409
    assert response.get_json()["references"] == [{"strategy_id": "s", "action_id": "a", "index": 1}]


def test_element_rename_rewrites_strategy_reference_atomically(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()
    client.put("/api/browser/elements", json={"elements": {"entry": "//entry"}})
    client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
            "actions": [_block_click("entry")],
        }]},
    )

    response = client.put(
        "/api/browser/elements",
        json={"elements": {"landing": "//entry"}, "rename_from": "entry"},
    )

    assert response.status_code == 200
    _assert_migrated_xpath_elements(
        response.get_json()["elements"], {"landing": "//entry"}
    )
    assert client.get("/api/browser/strategies").get_json()["strategies"][0]["actions"][0]["params"]["element"] == "landing"


def test_referenced_pattern_delete_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()
    client.put("/api/browser/elements", json={"elements": {"entry": "//entry"}})
    client.put("/api/browser/patterns", json={"patterns": [_mouse_pattern()]})
    strategy = _block_click("entry")
    strategy["params"]["trajectory"] = {"source": "pattern", "id": "mouse"}
    client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
            "actions": [strategy],
        }]},
    )

    response = client.put("/api/browser/patterns", json={"patterns": []})

    assert response.status_code == 409
    assert response.get_json()["references"] == [{"strategy_id": "s", "action_id": "a", "index": 1}]


def test_referenced_pattern_type_change_is_rejected_without_writing(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()
    client.put("/api/browser/elements", json={"elements": {"entry": "//entry"}})
    client.put("/api/browser/patterns", json={"patterns": [_mouse_pattern()]})
    strategy = _block_click("entry")
    strategy["params"]["trajectory"] = {"source": "pattern", "id": "mouse"}
    client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
            "actions": [strategy],
        }]},
    )
    before = config_path.read_bytes()

    response = client.put(
        "/api/browser/patterns",
        json={"patterns": [{
            "id": "mouse",
            "name": "Changed to keyboard",
            "type": "keyboard",
            "data": {
                "intervals_ms": [50, 70],
                "hold_ms": [10, 12],
                "sample_count": 2,
                "total_duration_ms": 142,
            },
        }]},
    )

    assert response.status_code == 400
    assert "pattern type does not match" in response.get_json()["error"]
    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    "invalid_pattern",
    [
        {
            "id": "mouse-short",
            "name": "Mouse short",
            "type": "mouse",
            "data": {
                "points": [{"x_ratio": 0.5, "y_ratio": 0.5, "dt_ms": 0}],
                "sample_count": 1,
                "total_duration_ms": 0,
            },
        },
        {
            "id": "keyboard-short",
            "name": "Keyboard short",
            "type": "keyboard",
            "data": {
                "intervals_ms": [50],
                "hold_ms": [10],
                "sample_count": 1,
                "total_duration_ms": 60,
            },
        },
        {
            "id": "mouse-negative-time",
            "name": "Mouse negative time",
            "type": "mouse",
            "data": {
                "points": [
                    {"x_ratio": 0.1, "y_ratio": 0.1, "dt_ms": 0},
                    {"x_ratio": 0.2, "y_ratio": 0.2, "dt_ms": -1},
                ],
                "sample_count": 2,
                "total_duration_ms": 0,
            },
        },
        {
            "id": "keyboard-negative-time",
            "name": "Keyboard negative time",
            "type": "keyboard",
            "data": {
                "intervals_ms": [10, 20],
                "hold_ms": [1, -1],
                "sample_count": 2,
                "total_duration_ms": 30,
            },
        },
    ],
    ids=["mouse-short", "keyboard-short", "mouse-negative", "keyboard-negative"],
)
def test_invalid_recording_pattern_put_returns_400_without_writing(
    monkeypatch, tmp_path, invalid_pattern
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()
    assert client.put(
        "/api/browser/elements", json={"elements": {"keep": "//keep"}}
    ).status_code == 200
    before = config_path.read_bytes()

    response = client.put(
        "/api/browser/patterns", json={"patterns": [invalid_pattern]}
    )

    assert response.status_code == 400
    assert config_path.read_bytes() == before


def test_builtin_pattern_id_does_not_block_custom_pattern_delete(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()
    client.put("/api/browser/elements", json={"elements": {"entry": "//entry"}})
    client.put("/api/browser/patterns", json={"patterns": [_mouse_pattern("bezier")]})
    client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "s", "name": "S", "run_mode": "once", "batch_size": 1,
            "actions": [_block_click("entry")],
        }]},
    )

    response = client.put("/api/browser/patterns", json={"patterns": []})

    assert response.status_code == 200
    assert response.get_json()["patterns"] == []


def test_strategies_reject_duplicate_ids_and_missing_references(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()

    duplicate = client.put(
        "/api/browser/strategies",
        json={"strategies": [
            {"id": "s", "name": "One", "run_mode": "once", "batch_size": 1, "actions": []},
            {"id": "s", "name": "Two", "run_mode": "once", "batch_size": 1, "actions": []},
        ]},
    )
    missing = client.put(
        "/api/browser/strategies",
        json={"strategies": [{
            "id": "missing", "name": "Missing", "run_mode": "once", "batch_size": 1,
            "actions": [_block_click("not-saved")],
        }]},
    )

    assert duplicate.status_code == 400
    assert missing.status_code == 400


def test_strategy_resource_routes_reject_non_object_bodies(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()

    responses = [
        client.put(route, json=["not", "an", "object"])
        for route in ("/api/browser/elements", "/api/browser/patterns", "/api/browser/strategies")
    ]

    assert [response.status_code for response in responses] == [400, 400, 400]


def test_first_resource_read_persists_legacy_migration_once(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    legacy_manual = [
        {"id": "legacy", "actions": [{"type": "click", "element": "entry"}]}
    ]
    legacy_auto = [{
        "id": "legacy-auto",
        "entry_element": "entry",
        "input_element": "input",
        "submit_element": "submit",
    }]
    config_path.write_text(
        json.dumps({"browser": {
            "action_elements": {
                "entry": "//entry", "input": "//textarea", "submit": "//button"
            },
            "action_strategies": legacy_manual,
            "auto_strategies": legacy_auto,
        }}),
        encoding="utf-8",
    )
    client = create_app().test_client()

    first = client.get("/api/browser/strategies")
    second = client.get("/api/browser/strategies")
    saved = json.loads(config_path.read_text(encoding="utf-8"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(list(config_path.parent.glob("config.json.backup.*"))) == 1
    assert saved["browser"]["strategy_schema_version"] == 3
    _assert_migrated_xpath_elements(
        saved["browser"]["action_elements"],
        {"entry": "//entry", "input": "//textarea", "submit": "//button"},
    )
    assert [item["id"] for item in saved["browser"]["block_strategies"]] == [
        "manual:legacy", "auto:legacy-auto"
    ]
    assert saved["browser"]["action_strategies"] == legacy_manual
    assert saved["browser"]["auto_strategies"] == legacy_auto
