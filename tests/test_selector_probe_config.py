from datetime import time

import pytest

from selector_probe.config import ProbeConfig, normalize_probe_config


def valid_config():
    return {
        "enabled": True,
        "site": "tiktok",
        "environment": "production",
        "timezone": "Asia/Shanghai",
        "daily_time": "03:00",
        "target_url": "https://www.tiktok.com/",
        "test_profile_ids": ["profile-a", "profile-b"],
        "model_id": "grok-main",
        "observe_only": True,
        "webhook": {
            "enabled": False,
            "type": "generic",
            "url": "",
            "signing_secret": "",
        },
    }


def test_normalize_probe_config_requires_two_unique_profiles():
    value = valid_config()
    value["test_profile_ids"] = ["profile-a", "profile-a"]
    with pytest.raises(ValueError, match="at least two unique"):
        normalize_probe_config(value)


def test_normalize_probe_config_locks_schedule_and_origin():
    result = normalize_probe_config(valid_config())
    assert isinstance(result, ProbeConfig)
    assert result.daily_time == time(3, 0)
    assert result.timezone == "Asia/Shanghai"
    assert result.target_origin == "https://www.tiktok.com"
    assert result.test_profile_ids == ("profile-a", "profile-b")


def test_public_config_masks_profiles_and_webhook_secret():
    result = normalize_probe_config(valid_config()).public_dict()
    assert result["test_profile_ids"] == ["***le-a", "***le-b"]
    assert "signing_secret" not in result["webhook"]


def test_invalid_timezone_is_reported_as_value_error():
    value = valid_config()
    value["timezone"] = "Missing/Timezone"

    with pytest.raises(ValueError, match="timezone"):
        normalize_probe_config(value)


@pytest.mark.parametrize("daily_time", ["3:00", "03:0", "03:00:00", "0300"])
def test_daily_time_requires_two_digit_hh_mm(daily_time):
    value = valid_config()
    value["daily_time"] = daily_time

    with pytest.raises(ValueError, match="HH:MM"):
        normalize_probe_config(value)


@pytest.mark.parametrize(
    "profiles",
    [
        "profile-a,profile-b",
        None,
        ["profile-a", 2],
        ["profile-a", ""],
    ],
)
def test_profile_ids_require_an_array_of_non_empty_strings(profiles):
    value = valid_config()
    value["test_profile_ids"] = profiles

    with pytest.raises(ValueError, match="test_profile_ids"):
        normalize_probe_config(value)


@pytest.mark.parametrize("enabled_value", [False, None])
def test_disabled_probe_allows_no_profiles(enabled_value):
    value = valid_config()
    value["enabled"] = enabled_value
    value["test_profile_ids"] = []

    assert normalize_probe_config(value).test_profile_ids == ()


@pytest.mark.parametrize(
    "target_url",
    [
        "https://example.com/",
        "https://tiktok.com.evil.example/",
        "https://user:password@www.tiktok.com/",
    ],
)
def test_target_url_rejects_non_tiktok_and_embedded_credentials(target_url):
    value = valid_config()
    value["target_url"] = target_url

    with pytest.raises(ValueError, match="target_url"):
        normalize_probe_config(value)


@pytest.mark.parametrize(
    ("webhook", "message"),
    [
        (
            {"enabled": True, "type": "", "url": "https://hooks.example.test"},
            "type",
        ),
        (
            {"enabled": True, "type": "generic", "url": ""},
            "URL",
        ),
        (
            {
                "enabled": True,
                "type": "generic",
                "url": "http://hooks.example.test",
            },
            "HTTPS",
        ),
    ],
)
def test_enabled_webhook_requires_type_and_https_url(webhook, message):
    value = valid_config()
    value["webhook"] = webhook

    with pytest.raises(ValueError, match=message):
        normalize_probe_config(value)


def test_enabled_generic_webhook_requires_strong_signing_secret():
    value = valid_config()
    value["webhook"] = {
        "enabled": True,
        "type": "generic",
        "url": "https://hooks.example.test/probe",
        "signing_secret": "weak",
    }

    with pytest.raises(ValueError, match="strong signing secret"):
        normalize_probe_config(value)


@pytest.mark.parametrize("webhook", [[], ""])
def test_webhook_rejects_non_object_empty_values(webhook):
    value = valid_config()
    value["webhook"] = webhook

    with pytest.raises(ValueError, match="webhook must be a JSON object"):
        normalize_probe_config(value)
