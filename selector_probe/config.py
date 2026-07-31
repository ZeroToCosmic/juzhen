from __future__ import annotations

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
    site: str
    environment: str
    timezone: str
    daily_time: time
    target_url: str
    target_origin: str
    test_profile_ids: tuple[str, ...]
    model_id: str
    observe_only: bool
    webhook: WebhookConfig

    def public_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "site": self.site,
            "environment": self.environment,
            "timezone": self.timezone,
            "daily_time": self.daily_time.strftime("%H:%M"),
            "target_url": self.target_url,
            "test_profile_ids": [
                mask_profile_id(item) for item in self.test_profile_ids
            ],
            "model_id": self.model_id,
            "observe_only": self.observe_only,
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


def normalize_probe_config(value: object) -> ProbeConfig:
    if not isinstance(value, dict):
        raise ValueError("selector_probe must be a JSON object")

    timezone = _required_text(value.get("timezone"), "selector_probe.timezone")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as error:
        raise ValueError("selector_probe.timezone must be a valid timezone") from error

    raw_time = _required_text(value.get("daily_time"), "selector_probe.daily_time")
    if re.fullmatch(r"\d{2}:\d{2}", raw_time) is None:
        raise ValueError("selector_probe.daily_time must use HH:MM")
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        daily_time = time(int(hour_text), int(minute_text))
    except (TypeError, ValueError) as error:
        raise ValueError("selector_probe.daily_time must use HH:MM") from error

    target_url = _required_text(value.get("target_url"), "selector_probe.target_url")
    parsed = urlsplit(target_url)
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
            "selector_probe.target_url must be an HTTPS TikTok URL without credentials"
        )

    raw_profiles = value.get("test_profile_ids", [])
    if not isinstance(raw_profiles, (list, tuple)):
        raise ValueError("selector_probe.test_profile_ids must be an array")
    if any(
        not isinstance(item, str) or not item.strip() for item in raw_profiles
    ):
        raise ValueError(
            "selector_probe.test_profile_ids must contain non-empty strings"
        )
    profiles = tuple(dict.fromkeys(item.strip() for item in raw_profiles))
    enabled = value.get("enabled") is True
    if enabled and len(profiles) < 2:
        raise ValueError("selector_probe requires at least two unique test profiles")

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
        site=_required_text(value.get("site") or "tiktok", "selector_probe.site"),
        environment=_required_text(
            value.get("environment") or "production",
            "selector_probe.environment",
        ),
        timezone=timezone,
        daily_time=daily_time,
        target_url=target_url,
        target_origin=f"{parsed.scheme}://{parsed.netloc}",
        test_profile_ids=profiles,
        model_id=str(value.get("model_id") or "").strip(),
        observe_only=value.get("observe_only") is not False,
        webhook=webhook,
    )
