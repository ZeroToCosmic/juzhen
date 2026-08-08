from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import time
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from browser_public_identity import mask_profile_id


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool
    type: str
    url: str
    signing_secret: str


@dataclass(frozen=True)
class ProbeConfig:
    enabled: bool
    rollout_mode: str
    timezone: str
    schedule_time: time
    target_origin: str
    test_profile_ids: tuple[str, ...]
    dedicated_test_profile_ids: tuple[str, ...]
    page_timeout_seconds: int
    redis: dict[str, object]
    webhook: WebhookConfig

    @property
    def site(self) -> str:
        """Compatibility identity until store callers use the fixed namespace."""

        return "tiktok"

    @property
    def environment(self) -> str:
        """Compatibility identity until store callers use the fixed namespace."""

        return "production"

    @property
    def daily_time(self) -> time:
        return self.schedule_time

    @property
    def target_url(self) -> str:
        return self.target_origin

    @property
    def observe_only(self) -> bool:
        return self.rollout_mode == "observe"

    def public_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "rollout_mode": self.rollout_mode,
            "timezone": self.timezone,
            "schedule_time": self.schedule_time.strftime("%H:%M"),
            "target_origin": self.target_origin,
            "test_profile_ids": [
                mask_profile_id(item) for item in self.test_profile_ids
            ],
            "dedicated_test_profile_ids": [
                mask_profile_id(item)
                for item in self.dedicated_test_profile_ids
            ],
            "page_timeout_seconds": self.page_timeout_seconds,
            "redis": {
                "namespace": str(self.redis.get("namespace") or ""),
                "url_configured": bool(self.redis.get("url")),
                "password_configured": bool(self.redis.get("password")),
            },
            "webhook": {
                "enabled": self.webhook.enabled,
                "type": self.webhook.type,
                "url_configured": bool(self.webhook.url),
                "signing_secret_configured": bool(self.webhook.signing_secret),
            },
        }


def _required_text(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _profile_ids(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be an array")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{name} must contain non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


def _page_timeout(value: object) -> int:
    if isinstance(value, bool):
        return 90
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 90
    return result if 10 <= result <= 300 else 90


def normalize_probe_config(value: object) -> ProbeConfig:
    if not isinstance(value, dict):
        raise ValueError("selector_probe must be a JSON object")

    timezone = _required_text(value.get("timezone"), "selector_probe.timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("selector_probe.timezone must be a valid timezone") from error

    raw_time = _required_text(
        value.get("schedule_time", value.get("daily_time", "03:00")),
        "selector_probe.schedule_time",
    )
    if re.fullmatch(r"\d{2}:\d{2}", raw_time) is None:
        raise ValueError("selector_probe.schedule_time must use HH:MM")
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        schedule_time = time(int(hour_text), int(minute_text))
    except (TypeError, ValueError) as error:
        raise ValueError("selector_probe.schedule_time must use HH:MM") from error

    target_origin = _required_text(
        value.get(
            "target_origin",
            value.get("target_url", "https://www.tiktok.com"),
        ),
        "selector_probe.target_origin",
    )
    parsed = urlsplit(target_origin)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    is_tiktok_host = hostname == "tiktok.com" or hostname.endswith(".tiktok.com")
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or not is_tiktok_host
    ):
        raise ValueError(
            "selector_probe.target_origin must be HTTPS TikTok without credentials"
        )

    profiles = _profile_ids(
        value.get("test_profile_ids", []),
        "selector_probe.test_profile_ids",
    )
    dedicated_profiles = _profile_ids(
        value.get("dedicated_test_profile_ids", list(profiles)),
        "selector_probe.dedicated_test_profile_ids",
    )
    enabled = value.get("enabled") is True
    if enabled and len(profiles) < 2:
        raise ValueError("selector_probe requires at least two unique test profiles")
    if enabled and len(dedicated_profiles) < 2:
        raise ValueError(
            "selector_probe requires at least two dedicated test profiles"
        )
    if not set(dedicated_profiles).issubset(profiles):
        raise ValueError(
            "selector_probe dedicated profiles must be selected test profiles"
        )

    rollout_mode = str(value.get("rollout_mode") or "").strip()
    if not rollout_mode:
        rollout_mode = "observe" if value.get("observe_only") is not False else "publish"
    if rollout_mode not in {"observe", "publish", "enforce"}:
        raise ValueError("selector_probe.rollout_mode is unsupported")

    redis_value = value.get("redis")
    if redis_value is None:
        redis_value = {}
    if not isinstance(redis_value, Mapping):
        raise ValueError("selector_probe.redis must be a JSON object")
    redis_config = dict(redis_value)

    webhook_value = value.get("webhook")
    if webhook_value is None:
        webhook_value = {}
    if not isinstance(webhook_value, dict):
        raise ValueError("selector_probe.webhook must be a JSON object")
    webhook_enabled = webhook_value.get("enabled") is True
    webhook_type = str(webhook_value.get("type") or "").strip()
    if not webhook_type and not webhook_enabled:
        webhook_type = "generic"
    webhook_url = str(webhook_value.get("url") or "").strip()
    webhook = WebhookConfig(
        enabled=webhook_enabled,
        type=webhook_type,
        url=webhook_url,
        signing_secret=str(webhook_value.get("signing_secret") or "").strip(),
    )
    if webhook.enabled and not webhook.type:
        raise ValueError("enabled selector_probe webhook needs a type")
    if webhook.enabled and webhook.type not in {
        "generic",
        "slack",
        "dingtalk",
    }:
        raise ValueError("enabled selector_probe webhook type is unsupported")
    if webhook.enabled and not webhook.url:
        raise ValueError("enabled selector_probe webhook needs a URL")
    webhook_url_parts = urlsplit(webhook.url)
    if webhook.enabled and (
        webhook_url_parts.scheme != "https" or not webhook_url_parts.netloc
    ):
        raise ValueError("enabled selector_probe webhook needs an HTTPS URL")
    if (
        webhook.enabled
        and webhook.type == "generic"
        and len(webhook.signing_secret.encode("utf-8")) < 32
    ):
        raise ValueError(
            "enabled generic webhook needs a strong signing secret"
        )

    return ProbeConfig(
        enabled=enabled,
        rollout_mode=rollout_mode,
        timezone=timezone,
        schedule_time=schedule_time,
        target_origin=f"{parsed.scheme}://{parsed.netloc}",
        test_profile_ids=profiles,
        dedicated_test_profile_ids=dedicated_profiles,
        page_timeout_seconds=_page_timeout(value.get("page_timeout_seconds", 90)),
        redis=redis_config,
        webhook=webhook,
    )
