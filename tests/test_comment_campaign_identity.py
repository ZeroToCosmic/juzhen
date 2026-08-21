import asyncio
from dataclasses import FrozenInstanceError

import pytest

from comment_campaign.errors import CampaignValidationError
from comment_campaign.identity import AccountObservation, normalize_tiktok_account_key
from comment_campaign.locator import read_tiktok_identity, verify_logged_in_username


class Handle:
    def __init__(self, value):
        self.value = value

    async def evaluate(self, _script):
        return self.value


class Resolver:
    def __init__(self, value=None, error=None):
        self.value = value
        self.error = error

    async def resolve(self, _page, _definition, **_kwargs):
        if self.error is not None:
            raise self.error
        return Handle(self.value)


def test_account_observation_is_immutable_and_serializable():
    observation = AccountObservation("creator", "Creator", None, "2026-08-11T00:00:00Z")
    assert observation.as_dict() == {
        "account_key": "creator",
        "visible_username": "Creator",
        "canonical_href": None,
        "observed_at": "2026-08-11T00:00:00Z",
    }
    with pytest.raises(FrozenInstanceError):
        observation.account_key = "other"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (" @Name ", "name"),
        ("＠Ｍｉｘｅｄ．Ｎａｍｅ", "mixed.name"),
        ("UPPER_case", "upper_case"),
    ],
)
def test_normalize_tiktok_account_key(value, expected):
    assert normalize_tiktok_account_key(value) == expected


@pytest.mark.parametrize("value", ["", "@@name", "has space", "bad/name", "a" * 25, "name\x00"])
def test_normalize_tiktok_account_key_rejects_invalid_values(value):
    with pytest.raises(CampaignValidationError, match="tiktok_identity_unavailable"):
        normalize_tiktok_account_key(value)


def test_identity_prefers_canonical_href_and_keeps_display_separate():
    observation = asyncio.run(read_tiktok_identity(
        object(), {}, resolver=Resolver({
            "text": "Display Name",
            "href": "https://www.tiktok.com/@Canonical.Handle",
        })
    ))
    assert observation.account_key == "canonical.handle"
    assert observation.visible_username == "Display Name"
    assert observation.canonical_href == "https://www.tiktok.com/@Canonical.Handle"
    assert observation.observed_at.endswith("Z")


def test_identity_uses_a_single_visible_handle_only_when_href_is_absent():
    observation = asyncio.run(read_tiktok_identity(
        object(), {}, resolver=Resolver({"text": "@Creator.Name", "href": ""})
    ))
    assert observation.account_key == "creator.name"
    assert observation.canonical_href is None


def test_explicit_logged_out_state_requires_tiktok_login():
    with pytest.raises(CampaignValidationError) as caught:
        asyncio.run(read_tiktok_identity(
            object(), {}, resolver=Resolver({
                "text": "Log in", "href": "", "logged_in": False,
            })
        ))
    assert caught.value.code == "tiktok_login_required"


@pytest.mark.parametrize(
    "href",
    [
        "http://www.tiktok.com/@creator",
        "https://www.tiktok.com.evil.example/@creator",
        "https://evil.example/@creator",
        "https://attacker@www.tiktok.com/@creator",
        "https://www.tiktok.com:443/@creator",
        "https://www.tiktok.com/@creator?x=1",
        "https://www.tiktok.com/@creator#fragment",
        "https://www.tiktok.com/@creator/extra",
    ],
)
def test_identity_rejects_noncanonical_tiktok_hrefs(href):
    with pytest.raises(CampaignValidationError, match="tiktok_identity_unavailable"):
        asyncio.run(read_tiktok_identity(object(), {}, resolver=Resolver({"text": "Creator", "href": href})))


@pytest.mark.parametrize(
    "resolver",
    [
        Resolver({"text": "@wrong", "href": "https://www.tiktok.com/@right"}),
        Resolver({"text": "", "href": ""}),
        Resolver({"text": "Log in", "href": "", "logged_in": False}),
        Resolver(error=RuntimeError("DOM sentinel https://private.example")),
    ],
)
def test_identity_is_fail_closed(resolver):
    with pytest.raises(CampaignValidationError) as caught:
        asyncio.run(read_tiktok_identity(object(), {}, resolver=resolver))
    assert caught.value.code in {"tiktok_login_required", "tiktok_identity_unavailable"}
    assert "private.example" not in str(caught.value)


def test_compatibility_verifier_uses_exact_normalized_identity():
    resolver = Resolver({"text": "Creator", "href": "https://tiktok.com/@Creator"})
    evidence = asyncio.run(verify_logged_in_username(object(), "@creator", {}, resolver=resolver))
    assert evidence["account_key"] == "creator"
    with pytest.raises(CampaignValidationError, match="tiktok_identity_changed"):
        asyncio.run(verify_logged_in_username(object(), "different", {}, resolver=resolver))
