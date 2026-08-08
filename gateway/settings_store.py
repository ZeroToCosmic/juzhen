import copy
import json
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv

from gateway.proxy_pool import format_proxy_pool, parse_proxy_pool


PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SETTINGS_LOCK = threading.RLock()
_MAX_CONFIG_BACKUPS = 5
_CONFIG_READ_ERRORS: dict[Path, str] = {}
_PRESERVE_BLANK_KEYS = {
    "account_id",
    "account_token",
    "access_key_id",
    "api_key",
    "base_url",
    "bucket",
    "endpoint_url",
    "password",
    "prefix",
    "public_base_url",
    "raw",
    "secret_access_key",
    "signing_secret",
    "token",
}
_REPLACE_ON_UPDATE_PATHS = {
    ("browser", "action_elements"),
}
_SELECTOR_PROBE_KEYS = (
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
)


DEFAULT_SETTINGS = {
    "proxy": {
        "host": "",
        "port": "",
        "username": "",
        "password": "",
    },
    "proxy_pool": {
        "protocol": "socks5",
        "raw": "",
        "items": [],
    },
    "services": {
        "ipinfo_url": "https://ipinfo.io/json",
        "buffer_graphql_url": "https://api.buffer.com",
    },
    "timeouts": {
        "ip_check_seconds": 10,
        "buffer_publish_seconds": 30,
    },
    "publish_queue": {
        "interval_seconds": 8,
    },
    "publish_sampling": {
        "enabled": True,
        "interval_seconds": 300,
        "min_age_hours": 24,
    },
    "browser": {
        "cdp_url": "",
        "task_goal": "",
        "default_url": "https://www.tiktok.com/",
        "auto_strategies": [],
        "strategy_schema_version": 0,
        "action_elements": {},
        "interaction_patterns": [],
        "block_strategies": [],
    },
    "adspower": {
        "base_url": "http://local.adspower.net:50325",
        "api_key": "",
        "default_group_id": "",
    },
    "models": {
        "default_model_id": "grok-main",
        "items": [
            {
                "id": "grok-main",
                "provider": "grok",
                "enabled": True,
                "base_url": "https://api.x.ai/v1",
                "api_key": "",
                "model": "grok-4.5",
                "mode": "responses",
            },
            {
                "id": "deepseek-main",
                "provider": "deepseek",
                "enabled": False,
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "",
                "model": "deepseek-chat",
                "mode": "chat",
            },
            {
                "id": "glm-main",
                "provider": "glm",
                "enabled": False,
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "",
                "model": "glm-4.5",
                "mode": "chat",
            },
            {
                "id": "qwen-main",
                "provider": "qwen",
                "enabled": False,
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "",
                "model": "qwen-plus",
                "mode": "chat",
            },
            {
                "id": "gpt-main",
                "provider": "gpt",
                "enabled": False,
                "base_url": "https://api.openai.com/v1",
                "api_key": "",
                "model": "gpt-4.1",
                "mode": "responses",
            },
        ],
    },
    "selector_probe": {
        "enabled": False,
        "rollout_mode": "observe",
        "timezone": "Asia/Shanghai",
        "schedule_time": "03:00",
        "target_origin": "https://www.tiktok.com",
        "test_profile_ids": [],
        "dedicated_test_profile_ids": [],
        "page_timeout_seconds": 90,
        "redis": {
            "url": "",
            "namespace": "selector_registry",
            "password": "",
        },
        "webhook": {
            "enabled": False,
            "type": "generic",
            "url": "",
            "signing_secret": "",
        },
    },
    "execution_strategies": {
        "items": [
            {
                "id": "steady_reader",
                "label": "Steady reader",
                "mouseMoves": 3,
                "clicks": 0,
                "scrolls": 2,
                "moveSteps": [12, 22],
                "pauseMs": [300, 900],
                "scrollDelta": [260, 520],
                "text_prompt": "像稳定阅读者一样浏览页面，先观察主要内容，再少量滚动。",
            },
            {
                "id": "curious_scanner",
                "label": "Curious scanner",
                "mouseMoves": 5,
                "clicks": 0,
                "scrolls": 3,
                "moveSteps": [8, 18],
                "pauseMs": [120, 420],
                "scrollDelta": [180, 420],
                "text_prompt": "像快速扫读者一样浏览页面，鼠标移动更频繁，滚动节奏稍快。",
            },
            {
                "id": "slow_reviewer",
                "label": "Slow reviewer",
                "mouseMoves": 2,
                "clicks": 0,
                "scrolls": 2,
                "moveSteps": [18, 30],
                "pauseMs": [700, 1400],
                "scrollDelta": [120, 300],
                "text_prompt": "像认真复核者一样浏览页面，停留更久，滚动幅度更小。",
            },
        ],
    },
    "r2": {
        "account_id": "",
        "account_token": "",
        "access_key_id": "",
        "secret_access_key": "",
        "bucket": "",
        "endpoint_url": "",
        "public_base_url": "",
        "prefix": "",
    },
}


def get_config_path(path=None) -> Path:
    if path is not None:
        return _resolve_config_path(path)

    load_dotenv(PROJECT_ROOT / ".env")
    configured_path = os.getenv("APP_CONFIG_PATH")
    if configured_path:
        return _resolve_config_path(configured_path)

    return PROJECT_ROOT / "config.json"


def _resolve_config_path(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def merge_settings(overrides: dict | None) -> dict:
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    _deep_merge(settings, overrides or {})
    return settings


def load_settings(path=None) -> dict:
    with _SETTINGS_LOCK:
        return _load_settings(path)


def _load_settings(path=None) -> dict:
    config_path = get_config_path(path)
    loaded = _read_config(config_path)
    _normalize_selector_probe_settings(loaded)

    settings = merge_settings(loaded)
    _normalize_runtime_settings(settings)
    _apply_legacy_proxy_env_fallback(settings)
    _normalize_proxy_pool(settings)
    return settings


def _read_config(config_path: Path) -> dict:
    try:
        if not config_path.exists():
            _CONFIG_READ_ERRORS.pop(config_path, None)
            return {}
        loaded = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("configuration root must be a JSON object")
    except OSError:
        _CONFIG_READ_ERRORS[config_path] = "配置文件无法读取，请检查文件权限或从备份恢复。"
        return {}
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        _CONFIG_READ_ERRORS[config_path] = "配置文件无法读取，请从最近备份恢复。"
        _backup_corrupt_config(config_path)
        return {}

    _CONFIG_READ_ERRORS.pop(config_path, None)
    return loaded


def get_config_health(path=None) -> dict:
    with _SETTINGS_LOCK:
        config_path = get_config_path(path)
        _read_config(config_path)
        backups = _list_valid_config_backups(config_path)
        return {
            "ok": config_path not in _CONFIG_READ_ERRORS,
            "error": _CONFIG_READ_ERRORS.get(config_path),
            "backup_available": bool(backups),
            "latest_backup": backups[0].name if backups else None,
        }


def save_settings(settings: dict, path=None) -> dict:
    with _SETTINGS_LOCK:
        return _save_settings(settings, path)


def _save_settings(settings: dict, path=None) -> dict:
    submitted = copy.deepcopy(settings)
    _normalize_selector_probe_settings(submitted)
    merged = merge_settings(submitted)
    _normalize_runtime_settings(merged)
    _normalize_proxy_pool(merged)
    config_path = get_config_path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(merged, indent=2, ensure_ascii=False)
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        _backup_existing_config(config_path)
        _replace_config_file(temporary_path, config_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return merged


def _replace_config_file(source: Path, target: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def list_config_backups(path=None) -> list[Path]:
    with _SETTINGS_LOCK:
        return _list_config_backups(get_config_path(path))


def _list_config_backups(config_path: Path) -> list[Path]:
    return sorted(
        config_path.parent.glob(f"{config_path.name}.backup.*"),
        key=lambda backup: _backup_order(backup, config_path),
        reverse=True,
    )


def _list_valid_config_backups(config_path: Path) -> list[Path]:
    return [
        backup
        for backup in _list_config_backups(config_path)
        if _is_valid_config_backup(backup)
    ]


def _backup_order(backup_path: Path, config_path: Path) -> tuple[int, int]:
    suffix = backup_path.name.removeprefix(f"{config_path.name}.backup.")
    sequence_text, separator, _ = suffix.partition(".")
    if separator and sequence_text.isdigit():
        return 1, int(sequence_text)
    try:
        return 0, int(suffix)
    except ValueError:
        return 0, backup_path.stat().st_mtime_ns


def _next_backup_sequence(config_path: Path) -> int:
    sequences = []
    for backup in config_path.parent.glob(f"{config_path.name}.backup.*"):
        backup_kind, order = _backup_order(backup, config_path)
        if backup_kind == 1:
            sequences.append(order)
    return max(sequences, default=0) + 1


def _backup_existing_config(config_path: Path) -> None:
    if not config_path.exists():
        return

    sequence = _next_backup_sequence(config_path)
    backup_path = config_path.with_name(
        f"{config_path.name}.backup.{sequence:020d}.{time.time_ns()}"
    )
    shutil.copy2(config_path, backup_path)
    for stale_backup in _list_config_backups(config_path)[_MAX_CONFIG_BACKUPS:]:
        stale_backup.unlink()


def restore_latest_backup(path=None) -> dict:
    with _SETTINGS_LOCK:
        config_path = get_config_path(path)
        backups = _list_valid_config_backups(config_path)
        if not backups:
            raise FileNotFoundError("没有可恢复的配置备份")

        latest_backup = backups[0]
        _backup_existing_config(config_path)
        _restore_config_backup(latest_backup, config_path)
        return _load_settings(path)


def restore_latest_backup_preserving(
    section_names,
    path=None,
) -> dict:
    """Restore one backup while retaining selected live top-level sections."""

    if isinstance(section_names, (str, bytes)) or not isinstance(
        section_names, (list, tuple, set, frozenset)
    ):
        raise TypeError("section_names must be a collection of strings")
    selected_sections = tuple(
        dict.fromkeys(
            name
            for name in section_names
            if isinstance(name, str) and name
        )
    )
    if len(selected_sections) != len(section_names):
        raise ValueError("section_names must contain unique non-empty strings")
    with _SETTINGS_LOCK:
        config_path = get_config_path(path)
        backups = _list_valid_config_backups(config_path)
        if not backups:
            raise FileNotFoundError("没有可恢复的配置备份")
        current = _load_settings(path)
        restored = json.loads(backups[0].read_text(encoding="utf-8"))
        if not isinstance(restored, dict):
            raise ValueError("configuration backup root must be an object")
        for name in selected_sections:
            if name in current:
                restored[name] = copy.deepcopy(current[name])
        return _save_settings(restored, path)


def _is_valid_config_backup(backup_path: Path) -> bool:
    try:
        return isinstance(json.loads(backup_path.read_text(encoding="utf-8")), dict)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _restore_config_backup(backup_path: Path, config_path: Path) -> None:
    temporary_path: Path | None = None
    try:
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)
        shutil.copy2(backup_path, temporary_path)
        _replace_config_file(temporary_path, config_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def update_settings(updates: dict | None, path=None) -> dict:
    """Merge a partial update into the persisted settings before saving it."""

    with _SETTINGS_LOCK:
        config_path = get_config_path(path)
        current = _load_settings(path)
        if config_path in _CONFIG_READ_ERRORS:
            raise ValueError(_CONFIG_READ_ERRORS[config_path])
        updates = copy.deepcopy(updates or {})
        _preserve_model_items(updates, current)
        _deep_merge_preserving_credentials(current, updates)
        return _save_settings(current, path)


def mutate_settings(mutator, path=None) -> dict:
    """Apply a full-settings mutation while holding the settings read/write lock."""

    if not callable(mutator):
        raise TypeError("settings mutator must be callable")
    with _SETTINGS_LOCK:
        config_path = get_config_path(path)
        current = _load_settings(path)
        if config_path in _CONFIG_READ_ERRORS:
            raise ValueError(_CONFIG_READ_ERRORS[config_path])
        updated = mutator(current)
        if updated is None:
            return current
        if not isinstance(updated, dict):
            raise TypeError("settings mutator must return a JSON object or None")
        return _save_settings(updated, path)


def _preserve_model_items(updates: dict, current_settings: dict) -> None:
    """Preserve blank model keys while holding the settings read/write lock."""

    models_update = updates.get("models")
    submitted_items = models_update.get("items") if isinstance(models_update, dict) else None
    if not isinstance(submitted_items, list):
        return

    existing_items = current_settings.get("models", {}).get("items", [])
    submitted_ids = [
        str(item.get("id"))
        for item in submitted_items
        if isinstance(item, dict) and item.get("id")
    ]
    duplicate_submitted_ids = {
        item_id for item_id in submitted_ids if submitted_ids.count(item_id) > 1
    }
    if duplicate_submitted_ids:
        duplicate_list = ", ".join(sorted(duplicate_submitted_ids))
        raise ValueError(f"models.items contains duplicate id: {duplicate_list}")

    by_id = {}
    for item in existing_items:
        if isinstance(item, dict) and item.get("id"):
            by_id.setdefault(str(item.get("id")), []).append(item)
    legacy_items = [
        item
        for item in existing_items
        if isinstance(item, dict) and not item.get("id")
    ]
    identity_fields = ("provider", "base_url", "model", "mode")
    merged_items = []
    for submitted in submitted_items:
        if not isinstance(submitted, dict):
            continue
        item_id = str(submitted.get("id") or "")
        existing_matches = by_id.get(item_id, []) if item_id else []
        existing = existing_matches[0] if len(existing_matches) == 1 else None
        if not item_id:
            exact_legacy_matches = [
                item
                for item in legacy_items
                if all(item.get(field) == submitted.get(field) for field in identity_fields)
            ]
            if len(exact_legacy_matches) == 1:
                existing = exact_legacy_matches[0]
            elif (
                len(submitted_items) == 1
                and len(existing_items) == 1
                and len(legacy_items) == 1
            ):
                existing = legacy_items[0]
        merged = copy.deepcopy(submitted)
        if not str(submitted.get("api_key") or "").strip():
            merged["api_key"] = existing.get("api_key", "") if existing else ""
        merged_items.append(merged)
    models_update["items"] = merged_items


def _backup_corrupt_config(config_path: Path) -> None:
    """Keep a copy of an unreadable config before falling back to defaults."""

    backup_path = config_path.with_name(f"{config_path.name}.corrupt")
    if backup_path.exists():
        backup_path = config_path.with_name(
            f"{config_path.name}.corrupt.{time.strftime('%Y%m%d%H%M%S')}"
        )
    try:
        shutil.copy2(config_path, backup_path)
    except OSError:
        # A read-only or concurrently removed file must not prevent startup.
        pass


def _deep_merge(base: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _normalize_runtime_settings(settings: dict) -> None:
    positive_defaults = {
        ("timeouts", "ip_check_seconds"): 10,
        ("timeouts", "buffer_publish_seconds"): 30,
        ("publish_queue", "interval_seconds"): 8,
        ("publish_sampling", "interval_seconds"): 300,
    }
    for (section, key), default in positive_defaults.items():
        value = settings.setdefault(section, {}).get(key)
        try:
            if int(value) <= 0:
                raise ValueError
        except (TypeError, ValueError):
            settings[section][key] = default

    sampling = settings.setdefault("publish_sampling", {})
    try:
        if int(sampling.get("min_age_hours")) < 0:
            raise ValueError
    except (TypeError, ValueError):
        sampling["min_age_hours"] = 24

    _normalize_selector_probe_settings(settings)


def _normalize_selector_probe_settings(settings: dict) -> None:
    if not isinstance(settings, dict):
        return
    raw_probe = settings.get("selector_probe")
    if raw_probe is None:
        return
    if not isinstance(raw_probe, dict):
        raw_probe = {}

    canonical = {
        key: copy.deepcopy(raw_probe[key])
        for key in _SELECTOR_PROBE_KEYS
        if key in raw_probe
    }
    if "schedule_time" not in canonical and "daily_time" in raw_probe:
        canonical["schedule_time"] = raw_probe["daily_time"]
    if "target_origin" not in canonical and "target_url" in raw_probe:
        canonical["target_origin"] = raw_probe["target_url"]
    if "rollout_mode" not in canonical:
        canonical["rollout_mode"] = (
            "observe" if raw_probe.get("observe_only") is not False else "publish"
        )
    if "dedicated_test_profile_ids" not in canonical:
        canonical["dedicated_test_profile_ids"] = copy.deepcopy(
            raw_probe.get("test_profile_ids", [])
        )
    canonical["page_timeout_seconds"] = _normalized_probe_page_timeout(
        canonical.get("page_timeout_seconds", 90)
    )
    settings["selector_probe"] = canonical


def _normalized_probe_page_timeout(value: object) -> int:
    if isinstance(value, bool):
        return 90
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 90
    return result if 10 <= result <= 300 else 90


def _deep_merge_preserving_credentials(base: dict, overrides: dict, path: tuple[str, ...] = ()) -> None:
    for key, value in overrides.items():
        key_path = path + (key,)
        if key_path in _REPLACE_ON_UPDATE_PATHS:
            base[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge_preserving_credentials(base[key], value, key_path)
        elif (
            key in _PRESERVE_BLANK_KEYS
            and not str(value or "").strip()
            and str(base.get(key) or "").strip()
        ):
            continue
        else:
            base[key] = value


def _apply_legacy_proxy_env_fallback(settings: dict) -> None:
    proxy = settings["proxy"]
    env_map = {
        "host": "PROXY_HOST",
        "port": "PROXY_PORT",
        "username": "PROXY_USER",
        "password": "PROXY_PASS",
    }

    for setting_key, env_key in env_map.items():
        if not proxy.get(setting_key) and os.getenv(env_key):
            proxy[setting_key] = os.getenv(env_key)


def _normalize_proxy_pool(settings: dict) -> None:
    proxy_pool = settings.setdefault("proxy_pool", {"raw": "", "items": []})
    protocol = str(proxy_pool.get("protocol") or "socks5").lower()
    if protocol not in {"socks5", "http"}:
        raise ValueError("Proxy pool protocol must be socks5 or http")
    proxy_pool["protocol"] = protocol
    raw = proxy_pool.get("raw", "")
    items = proxy_pool.get("items") or []

    if raw:
        items = parse_proxy_pool(raw)
        proxy_pool["items"] = items
    else:
        proxy_pool["raw"] = format_proxy_pool(items)


def public_settings(settings: dict) -> dict:
    """Return settings safe to place in API responses and the browser DOM."""

    public = copy.deepcopy(settings)
    public["_secrets_configured"] = {
        "proxy": {
            "password": bool(settings.get("proxy", {}).get("password")),
        },
        "proxy_pool": {
            "raw": bool(settings.get("proxy_pool", {}).get("raw")),
        },
        "r2": {
            key: bool(settings.get("r2", {}).get(key))
            for key in ("account_token", "access_key_id", "secret_access_key")
        },
        "adspower": {
            "api_key": bool(settings.get("adspower", {}).get("api_key")),
        },
        "models": {
            "items": [
                {"api_key": bool(item.get("api_key"))}
                for item in settings.get("models", {}).get("items", [])
                if isinstance(item, dict)
            ],
        },
        "selector_probe": {
            "webhook": {
                "signing_secret": bool(
                    settings.get("selector_probe", {})
                    .get("webhook", {})
                    .get("signing_secret")
                ),
            },
        },
    }
    public.pop("selector_probe", None)
    public.get("proxy", {})["password"] = ""
    proxy_pool = public.get("proxy_pool", {})
    proxy_pool["raw"] = ""
    proxy_pool["items"] = []
    r2 = public.get("r2", {})
    for key in ("account_token", "access_key_id", "secret_access_key"):
        r2[key] = ""
    public.get("adspower", {})["api_key"] = ""
    for item in public.get("models", {}).get("items", []):
        if isinstance(item, dict):
            item["api_key"] = ""
    return public
