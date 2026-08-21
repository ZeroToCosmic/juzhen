import copy
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import gateway.settings_store as settings_store
from gateway.settings_store import load_settings, save_settings


def test_selector_probe_defaults_and_secret_round_trip(tmp_path):
    path = tmp_path / "config.json"
    settings = load_settings(path)
    assert settings["selector_probe"]["schedule_time"] == "03:00"
    assert settings["selector_probe"]["page_timeout_seconds"] == 90
    assert settings["selector_probe"]["timezone"] == "Asia/Shanghai"
    settings["selector_probe"]["webhook"]["signing_secret"] = "secret"
    save_settings(settings, path)
    assert (
        load_settings(path)["selector_probe"]["webhook"]["signing_secret"]
        == "secret"
    )


def test_locator_order_scope_and_scroll_range_survive_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    canonical_elements = {
        "评论入口": {
            "scope": "active_video",
            "locators": [
                {
                    "id": "comment-attribute",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-icon",
                    "enabled": True,
                },
                {
                    "id": "comment-xpath",
                    "type": "xpath",
                    "value": "//button[@data-e2e='comment-icon']",
                    "enabled": True,
                    "fallback": True,
                },
            ],
        }
    }

    save_settings(
        {
            "browser": {
                "strategy_schema_version": 3,
                "action_elements": canonical_elements,
                "block_strategies": [
                    {
                        "id": "comment-flow",
                        "name": "comment flow",
                        "run_mode": "once",
                        "batch_size": 2,
                        "actions": [
                            {
                                "id": "switch-videos",
                                "type": "scroll_down",
                                "params": {
                                    "distance": 120,
                                    "total_count": [30, 50],
                                    "burst_count": [1, 1],
                                    "interval_seconds": [0.1, 0.3],
                                },
                            }
                        ],
                    }
                ],
            },
        }
    )

    reloaded = load_settings()["browser"]
    assert reloaded["action_elements"] == canonical_elements
    assert (
        reloaded["block_strategies"][0]["actions"][0]["params"]["total_count"]
        == [30, 50]
    )


def test_canonical_browser_state_survives_fresh_process_restart(tmp_path):
    config_path = tmp_path / "config.json"
    expected = {
        "strategy_schema_version": 3,
        "action_elements": {
            "评论入口": {
                "scope": "visible_comment_panel",
                "locators": [
                    {
                        "id": "comment-role",
                        "type": "role",
                        "role": "button",
                        "name": "评论",
                        "name_mode": "exact",
                        "enabled": True,
                    },
                    {
                        "id": "comment-xpath",
                        "type": "xpath",
                        "value": "//button[@data-e2e='comment-icon']",
                        "enabled": True,
                        "fallback": True,
                    },
                ],
            }
        },
        "block_strategies": [
            {
                "id": "comment-flow",
                "name": "comment flow",
                "run_mode": "once",
                "batch_size": 2,
                "actions": [
                    {
                        "id": "switch-videos",
                        "type": "scroll_up",
                        "params": {
                            "distance": 120,
                            "total_count": [30, 50],
                            "burst_count": [1, 1],
                            "interval_seconds": [0.1, 0.3],
                        },
                    }
                ],
            }
        ],
    }
    save_settings({"browser": expected}, config_path)
    probe = """
import json
import sys
from gateway.settings_store import load_settings

browser = load_settings(sys.argv[1])["browser"]
canonical = {
    "strategy_schema_version": browser["strategy_schema_version"],
    "action_elements": browser["action_elements"],
    "block_strategies": browser["block_strategies"],
}
print(json.dumps(canonical))
"""

    completed = subprocess.run(
        [sys.executable, "-c", probe, str(config_path)],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == expected


def test_model_key_preservation_is_atomic_with_concurrent_settings_updates(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    model = {
        "id": "model-a",
        "provider": "custom",
        "enabled": True,
        "base_url": "https://models.example.test/v1",
        "api_key": "old-key",
        "model": "model-a",
        "mode": "chat",
    }
    save_settings({"models": {"default_model_id": "model-a", "items": [model]}}, config_path)
    original_preserve = settings_store._preserve_model_items
    blank_entered = threading.Event()
    explicit_entered = threading.Event()
    release_blank = threading.Event()
    errors = []

    def controlled_preserve(updates, current):
        if threading.current_thread().name == "blank-model-save":
            blank_entered.set()
            assert release_blank.wait(timeout=3)
        else:
            explicit_entered.set()
        return original_preserve(updates, current)

    monkeypatch.setattr(settings_store, "_preserve_model_items", controlled_preserve)

    def run(name, api_key):
        try:
            settings_store.update_settings(
                {
                    "models": {
                        "items": [{**model, "api_key": api_key}],
                    }
                },
                config_path,
            )
        except BaseException as error:
            errors.append(error)

    blank = threading.Thread(target=run, name="blank-model-save", args=("blank", ""))
    explicit = threading.Thread(target=run, name="explicit-model-save", args=("explicit", "new-key"))

    blank.start()
    assert blank_entered.wait(timeout=1)
    explicit.start()
    assert explicit_entered.wait(timeout=0.2) is False
    release_blank.set()
    blank.join(timeout=3)
    explicit.join(timeout=3)

    assert not blank.is_alive()
    assert not explicit.is_alive()
    assert errors == []
    assert load_settings(config_path)["models"]["items"][0]["api_key"] == "new-key"


def test_default_config_path_is_stable_when_process_starts_from_another_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("APP_CONFIG_PATH", raising=False)

    expected = Path(settings_store.__file__).resolve().parent.parent / "config.json"

    assert settings_store.get_config_path() == expected


def test_relative_config_path_from_env_is_relative_to_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APP_CONFIG_PATH", "data/config.json")

    expected = Path(settings_store.__file__).resolve().parent.parent / "data" / "config.json"

    assert settings_store.get_config_path() == expected


def test_invalid_config_is_preserved_and_defaults_are_returned(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"broken":', encoding="utf-8")

    settings = load_settings(config_path)

    assert settings["browser"]["default_url"] == "https://www.tiktok.com/"
    assert config_path.exists()
    assert config_path.with_name("config.json.corrupt").exists()


def test_load_settings_returns_defaults_when_file_is_missing(tmp_path):
    settings = load_settings(tmp_path / "missing-config.json")

    assert settings["proxy_pool"]["items"] == []
    assert settings["proxy_pool"]["raw"] == ""
    assert settings["services"]["ipinfo_url"] == "https://ipinfo.io/json"
    assert settings["services"]["buffer_graphql_url"] == "https://api.buffer.com"
    assert "buffer_create_update_url" not in settings["services"]
    assert settings["timeouts"]["ip_check_seconds"] == 10
    assert settings["timeouts"]["buffer_publish_seconds"] == 30
    assert settings["publish_sampling"] == {
        "enabled": True,
        "interval_seconds": 300,
        "min_age_hours": 24,
    }
    assert settings["adspower"]["base_url"] == "http://local.adspower.net:50325"
    assert settings["adspower"]["api_key"] == ""
    assert settings["models"]["default_model_id"] == "grok-main"
    assert settings["models"]["items"][0]["provider"] == "grok"
    assert [strategy["id"] for strategy in settings["execution_strategies"]["items"]] == [
        "steady_reader",
        "curious_scanner",
        "slow_reviewer",
    ]
    assert "text_prompt" in settings["execution_strategies"]["items"][0]
    assert settings["r2"] == {
        "account_id": "",
        "account_token": "",
        "access_key_id": "",
        "secret_access_key": "",
        "bucket": "",
        "endpoint_url": "",
        "public_base_url": "",
        "prefix": "",
    }


def test_load_settings_merges_saved_values_with_defaults(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"proxy": {"host": "proxy.example.com"}}),
        encoding="utf-8",
    )

    settings = load_settings(config_path)

    assert settings["proxy"]["host"] == "proxy.example.com"
    assert settings["proxy"]["port"] == ""
    assert settings["services"]["ipinfo_url"] == "https://ipinfo.io/json"


def test_save_settings_writes_merged_config(tmp_path):
    config_path = tmp_path / "config.json"

    saved = save_settings(
        {
            "proxy": {"host": "proxy.example.com", "port": "8080"},
            "browser": {"cdp_url": "http://127.0.0.1:9222"},
        },
        config_path,
    )

    reloaded = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved == reloaded
    assert reloaded["proxy"]["host"] == "proxy.example.com"
    assert reloaded["proxy"]["port"] == "8080"
    assert reloaded["browser"]["cdp_url"] == "http://127.0.0.1:9222"
    assert reloaded["timeouts"]["buffer_publish_seconds"] == 30


def test_save_settings_parses_proxy_pool_raw_text(tmp_path):
    config_path = tmp_path / "config.json"

    saved = save_settings(
        {
            "proxy_pool": {
                "raw": "192.53.69.143:6781:nsucssou:3mjeb2p392yk\n203.0.113.8:9000:user2:pass2"
            }
        },
        config_path,
    )

    assert saved["proxy_pool"]["items"] == [
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
    assert saved["proxy_pool"]["raw"].startswith("192.53.69.143:6781")


def test_config_backups_keep_five_versions_and_restore_latest(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings({"browser": {"task_goal": "initial"}}, config_path)

    for version in range(7):
        settings_store.update_settings(
            {"browser": {"task_goal": f"version-{version}"}}, config_path
        )

    backups = settings_store.list_config_backups(config_path)

    assert len(backups) == 5
    restored = settings_store.restore_latest_backup(config_path)
    assert restored["browser"]["task_goal"] == "version-5"
    assert load_settings(config_path)["browser"]["task_goal"] == "version-5"


def test_backup_creation_order_survives_repeated_or_reversed_clock(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    timestamps = iter([200, 200, 100, 100])
    monkeypatch.setattr(settings_store.time, "time_ns", timestamps.__next__)
    save_settings({"browser": {"task_goal": "initial"}}, config_path)

    for version in range(3):
        settings_store.update_settings(
            {"browser": {"task_goal": f"version-{version}"}}, config_path
        )

    backups = settings_store.list_config_backups(config_path)
    backed_up_goals = [
        json.loads(backup.read_text(encoding="utf-8"))["browser"]["task_goal"]
        for backup in backups
    ]

    assert len({backup.name for backup in backups}) == 3
    assert backed_up_goals == ["version-1", "version-0", "initial"]
    restored = settings_store.restore_latest_backup(config_path)
    assert restored["browser"]["task_goal"] == "version-1"


def test_restore_preserves_current_version_as_recoverable_backup(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings({"browser": {"task_goal": "initial"}}, config_path)
    settings_store.update_settings(
        {"browser": {"task_goal": "version-0"}}, config_path
    )
    settings_store.update_settings(
        {"browser": {"task_goal": "version-1"}}, config_path
    )

    restored = settings_store.restore_latest_backup(config_path)
    backups_after_restore = settings_store.list_config_backups(config_path)
    protected_goal = json.loads(
        backups_after_restore[0].read_text(encoding="utf-8")
    )["browser"]["task_goal"]

    assert restored["browser"]["task_goal"] == "version-0"
    assert protected_goal == "version-1"
    recovered = settings_store.restore_latest_backup(config_path)
    assert recovered["browser"]["task_goal"] == "version-1"


def test_protected_restore_merges_before_one_atomic_save(
    monkeypatch,
    tmp_path,
):
    config_path = tmp_path / "config.json"
    archived = settings_store.load_settings(config_path)
    archived["browser"]["task_goal"] = "archived"
    archived["selector_probe"]["enabled"] = False
    archived["models"]["items"][0]["api_key"] = "archived-model-key"
    archived["adspower"]["api_key"] = "archived-profile-key"
    settings_store.save_settings(archived, config_path)

    live = settings_store.load_settings(config_path)
    live["browser"]["task_goal"] = "current"
    live["selector_probe"]["enabled"] = True
    live["models"]["items"][0]["api_key"] = "live-model-key"
    live["adspower"]["api_key"] = "live-profile-key"
    settings_store.save_settings(live, config_path)

    original_save = settings_store._save_settings
    calls = []

    def counted_save(settings, path=None):
        calls.append(copy.deepcopy(settings))
        return original_save(settings, path)

    monkeypatch.setattr(settings_store, "_save_settings", counted_save)

    restored = settings_store.restore_latest_backup_preserving(
        ("selector_probe", "models", "adspower"),
        config_path,
    )

    assert len(calls) == 1
    assert restored["browser"]["task_goal"] == "archived"
    assert restored["selector_probe"]["enabled"] is True
    assert (
        restored["models"]["items"][0]["api_key"]
        == "live-model-key"
    )
    assert restored["adspower"]["api_key"] == "live-profile-key"


def test_update_settings_deep_merges_nested_sections(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings(
        {
            "browser": {
                "default_url": "https://existing.example.test/",
                "task_goal": "before",
            },
            "r2": {"bucket": "test-bucket"},
        },
        config_path,
    )

    updated = settings_store.update_settings(
        {"browser": {"task_goal": "after"}}, config_path
    )

    assert updated["browser"]["task_goal"] == "after"
    assert updated["browser"]["default_url"] == "https://existing.example.test/"
    assert updated["r2"]["bucket"] == "test-bucket"


def test_mutate_settings_runs_against_current_settings_and_saves_once(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings(
        {"browser": {"task_goal": "before"}, "r2": {"bucket": "kept"}},
        config_path,
    )

    def mutate(settings):
        assert settings["browser"]["task_goal"] == "before"
        settings["browser"]["task_goal"] = "after"
        return settings

    updated = settings_store.mutate_settings(mutate, config_path)

    assert updated["browser"]["task_goal"] == "after"
    assert updated["r2"]["bucket"] == "kept"
    assert load_settings(config_path)["browser"]["task_goal"] == "after"


def test_mutate_settings_serializes_read_modify_write_transactions(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings({"browser": {"task_goal": "before"}}, config_path)
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    errors = []

    def first_mutation(settings):
        settings["browser"]["task_goal"] = "first"
        first_entered.set()
        assert release_first.wait(timeout=3)
        return settings

    def second_mutation(settings):
        assert settings["browser"]["task_goal"] == "first"
        second_entered.set()
        settings["browser"]["task_goal"] = "second"
        return settings

    def run(mutation):
        try:
            settings_store.mutate_settings(mutation, config_path)
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=run, args=(first_mutation,))
    second = threading.Thread(target=run, args=(second_mutation,))
    first.start()
    assert first_entered.wait(timeout=1)
    second.start()
    assert second_entered.wait(timeout=0.2) is False
    release_first.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert load_settings(config_path)["browser"]["task_goal"] == "second"


def test_restore_latest_backup_explains_when_no_backup_exists(tmp_path):
    with pytest.raises(FileNotFoundError, match="没有.*备份"):
        settings_store.restore_latest_backup(tmp_path / "config.json")


def test_config_health_tracks_corruption_and_restore_clears_read_error(tmp_path):
    config_path = tmp_path / "config.json"
    save_settings({"browser": {"task_goal": "recoverable"}}, config_path)
    settings_store.update_settings({"browser": {"task_goal": "newer"}}, config_path)
    config_path.write_text('{"browser":', encoding="utf-8")

    load_settings(config_path)
    health = settings_store.get_config_health(config_path)

    assert health["ok"] is False
    assert health["error"]
    assert health["backup_available"] is True
    assert health["latest_backup"]
    assert set(health) == {"ok", "error", "backup_available", "latest_backup"}
    with pytest.raises(ValueError, match="配置文件无法读取"):
        settings_store.update_settings({"browser": {"task_goal": "must-not-save"}}, config_path)
    assert config_path.read_text(encoding="utf-8") == '{"browser":'

    restored = settings_store.restore_latest_backup(config_path)

    assert restored["browser"]["task_goal"] == "recoverable"
    assert settings_store.get_config_health(config_path)["ok"] is True


def test_health_and_restore_skip_newest_invalid_backup(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"browser":', encoding="utf-8")
    valid_backup = config_path.with_name(
        "config.json.backup.00000000000000000001.100"
    )
    invalid_backup = config_path.with_name(
        "config.json.backup.00000000000000000002.200"
    )
    valid_backup.write_text(
        json.dumps({"browser": {"task_goal": "recoverable-older"}}),
        encoding="utf-8",
    )
    invalid_backup.write_text('{"browser":', encoding="utf-8")

    health = settings_store.get_config_health(config_path)

    assert health["backup_available"] is True
    assert health["latest_backup"] == valid_backup.name

    restored = settings_store.restore_latest_backup(config_path)

    assert restored["browser"]["task_goal"] == "recoverable-older"


def test_health_and_restore_reject_only_invalid_backups(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    invalid_backup = config_path.with_name(
        "config.json.backup.00000000000000000001.100"
    )
    invalid_backup.write_text('{"browser":', encoding="utf-8")

    health = settings_store.get_config_health(config_path)

    assert health["backup_available"] is False
    assert health["latest_backup"] is None
    with pytest.raises(FileNotFoundError, match="没有.*备份"):
        settings_store.restore_latest_backup(config_path)


def test_config_health_records_read_oserror_without_exposing_details(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def fail_config_read(path, *args, **kwargs):
        if path == config_path:
            raise PermissionError("private-config-fragment")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_config_read)

    health = settings_store.get_config_health(config_path)

    assert health["ok"] is False
    assert health["error"]
    assert "private-config-fragment" not in json.dumps(health)
    assert set(health) == {"ok", "error", "backup_available", "latest_backup"}
