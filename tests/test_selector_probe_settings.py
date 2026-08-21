import json

from gateway.settings_store import load_settings, save_settings


EXPECTED_PROBE_KEYS = {
    "enabled",
    "rollout_mode",
    "schedule_time",
    "timezone",
    "target_origin",
    "test_profile_ids",
    "dedicated_test_profile_ids",
    "page_timeout_seconds",
    "redis",
    "webhook",
}


def test_selector_probe_defaults_use_manual_inventory_settings(tmp_path):
    settings = load_settings(tmp_path / "config.json")

    assert set(settings["selector_probe"]) == EXPECTED_PROBE_KEYS
    assert settings["selector_probe"]["schedule_time"] == "03:00"
    assert settings["selector_probe"]["page_timeout_seconds"] == 90
    assert settings["selector_probe"]["rollout_mode"] == "observe"


def test_legacy_probe_keys_are_ignored_and_dropped_on_next_save(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "selector_probe": {
                    "enabled": False,
                    "daily_time": "04:30",
                    "target_url": "https://www.tiktok.com/foryou",
                    "test_profile_ids": [],
                    "model_id": "old-model",
                    "observe_only": False,
                    "contracts": {"comment": {"accepted_roles": ["button"]}},
                    "semantic_contracts": {"comment": {}},
                    "prompt_version": "legacy",
                }
            }
        ),
        encoding="utf-8",
    )

    loaded = load_settings(path)
    probe = loaded["selector_probe"]
    assert set(probe) == EXPECTED_PROBE_KEYS
    assert probe["schedule_time"] == "04:30"
    assert probe["target_origin"] == "https://www.tiktok.com/foryou"
    assert probe["rollout_mode"] == "publish"

    save_settings(loaded, path)
    persisted_probe = json.loads(path.read_text(encoding="utf-8"))["selector_probe"]
    assert set(persisted_probe) == EXPECTED_PROBE_KEYS


def test_probe_cleanup_does_not_remove_global_models(tmp_path):
    path = tmp_path / "config.json"
    settings = load_settings(path)
    settings["models"] = {
        "default_model_id": "strategy-model",
        "items": [
            {
                "id": "strategy-model",
                "provider": "custom",
                "enabled": True,
                "base_url": "https://models.example.test/v1",
                "api_key": "strategy-secret",
                "model": "strategy-model",
                "mode": "chat",
            }
        ],
    }
    settings["selector_probe"]["model_id"] = "obsolete-probe-model"

    saved = save_settings(settings, path)

    assert "model_id" not in saved["selector_probe"]
    assert saved["models"]["default_model_id"] == "strategy-model"
    assert saved["models"]["items"][0]["api_key"] == "strategy-secret"


def test_page_timeout_normalizes_to_default_at_settings_boundary(tmp_path):
    path = tmp_path / "config.json"

    for invalid in (None, True, 0, 9, 301, "invalid"):
        settings = load_settings(path)
        settings["selector_probe"]["page_timeout_seconds"] = invalid
        assert save_settings(settings, path)["selector_probe"][
            "page_timeout_seconds"
        ] == 90
