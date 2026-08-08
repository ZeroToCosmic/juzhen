from datetime import time

import pytest

from selector_probe.config import ProbeConfig, normalize_probe_config


def valid_config():
    return {
        "enabled": True,
        "rollout_mode": "publish",
        "timezone": "Asia/Shanghai",
        "schedule_time": "03:00",
        "target_origin": "https://www.tiktok.com/",
        "test_profile_ids": ["profile-a", "profile-b"],
        "dedicated_test_profile_ids": ["profile-a", "profile-b"],
        "page_timeout_seconds": 90,
        "redis": {
            "namespace": "selector_registry",
            "password": "redis-secret",
        },
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
    assert result.schedule_time == time(3, 0)
    assert result.timezone == "Asia/Shanghai"
    assert result.target_origin == "https://www.tiktok.com"
    assert result.test_profile_ids == ("profile-a", "profile-b")
    assert result.dedicated_test_profile_ids == ("profile-a", "profile-b")
    assert result.page_timeout_seconds == 90


def test_public_config_masks_profiles_and_webhook_secret():
    result = normalize_probe_config(valid_config()).public_dict()
    assert result["test_profile_ids"] == ["***le-a", "***le-b"]
    assert result["dedicated_test_profile_ids"] == ["***le-a", "***le-b"]
    assert "signing_secret" not in result["webhook"]
    assert "password" not in result["redis"]


def test_invalid_timezone_is_reported_as_value_error():
    value = valid_config()
    value["timezone"] = "Missing/Timezone"

    with pytest.raises(ValueError, match="timezone"):
        normalize_probe_config(value)


@pytest.mark.parametrize("schedule_time", ["3:00", "03:0", "03:00:00", "0300"])
def test_schedule_time_requires_two_digit_hh_mm(schedule_time):
    value = valid_config()
    value["schedule_time"] = schedule_time

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
    value["dedicated_test_profile_ids"] = []

    assert normalize_probe_config(value).test_profile_ids == ()


@pytest.mark.parametrize(
    "target_url",
    [
        "https://example.com/",
        "https://tiktok.com.evil.example/",
        "https://user:password@www.tiktok.com/",
    ],
)
def test_target_origin_rejects_non_tiktok_and_embedded_credentials(target_url):
    value = valid_config()
    value["target_origin"] = target_url

    with pytest.raises(ValueError, match="target_origin"):
        normalize_probe_config(value)


def test_probe_config_drops_model_and_contract_settings():
    value = valid_config()
    value.update(
        {
            "model_id": "old-model",
            "model": {"id": "old-model"},
            "observe_only": False,
            "contracts": {"comment": {"accepted_roles": ["button"]}},
            "semantic_contracts": {"comment": {}},
            "prompt_version": "legacy",
        }
    )

    result = normalize_probe_config(value)
    public = result.public_dict()

    assert result.rollout_mode == "publish"
    assert result.observe_only is False
    assert set(public) == {
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
    for obsolete in (
        "model_id",
        "model",
        "contracts",
        "semantic_contracts",
        "prompt_version",
    ):
        assert obsolete not in public


@pytest.mark.parametrize("value", [None, True, 0, 9, 301, "invalid"])
def test_page_timeout_invalid_values_normalize_to_ninety(value):
    config = valid_config()
    config["page_timeout_seconds"] = value

    assert normalize_probe_config(config).page_timeout_seconds == 90


@pytest.mark.parametrize("value", [10, 45, 300, "120"])
def test_page_timeout_accepts_bounded_seconds(value):
    config = valid_config()
    config["page_timeout_seconds"] = value

    assert normalize_probe_config(config).page_timeout_seconds == int(value)


def test_enabled_probe_requires_two_dedicated_profiles():
    value = valid_config()
    value["dedicated_test_profile_ids"] = ["profile-a"]

    with pytest.raises(ValueError, match="dedicated"):
        normalize_probe_config(value)


def test_dedicated_profiles_must_be_selected_test_profiles():
    value = valid_config()
    value["dedicated_test_profile_ids"] = ["profile-a", "profile-c"]

    with pytest.raises(ValueError, match="selected test profiles"):
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
