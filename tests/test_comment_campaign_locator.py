import asyncio
import pytest

from comment_campaign.errors import CampaignValidationError
from comment_campaign.locator import locate_comment_input, read_tiktok_identity, verify_video


class Page:
    url = "https://www.tiktok.com/@owner/video/12345678"

    async def evaluate(self, _script):
        return [self.url]


def test_verify_video_requires_a_visible_link_to_the_active_video():
    assert asyncio.run(verify_video(Page(), "12345678"))["visible_video_href"].endswith("/12345678")

    class WrongVisibleVideo(Page):
        async def evaluate(self, _script):
            return ["https://www.tiktok.com/@owner/video/87654321"]

    with pytest.raises(CampaignValidationError, match="target_video_mismatch"):
        asyncio.run(verify_video(WrongVisibleVideo(), "12345678"))


def test_verify_video_rejects_a_different_current_video():
    page = Page()
    page.url = "https://www.tiktok.com/@owner/video/87654321"
    with pytest.raises(CampaignValidationError, match="target_video_mismatch"):
        asyncio.run(verify_video(page, "12345678"))


def test_comment_input_uses_strict_resolver_with_editability():
    calls = []

    class Resolver:
        async def resolve(self, page, definition, **kwargs):
            calls.append(kwargs)
            return "input"

    assert asyncio.run(
        locate_comment_input(Page(), {"locators": []}, resolver=Resolver())
    ) == "input"
    assert calls == [{"require_editable": True}]


def test_identity_uses_the_strict_resolver_without_editability():
    calls = []

    class Handle:
        async def evaluate(self, _script):
            return {"text": "Creator", "href": "https://www.tiktok.com/@creator"}

    class Resolver:
        async def resolve(self, page, definition, **kwargs):
            calls.append(kwargs)
            return Handle()

    evidence = asyncio.run(read_tiktok_identity(Page(), {"locators": []}, resolver=Resolver()))
    assert evidence.account_key == "creator"
    assert calls == [{"require_editable": False}]


def test_identity_rejects_two_visible_nodes_from_the_strict_resolver():
    class TwoVisibleNodes:
        def locator(self, _selector):
            return self

        async def count(self):
            return 2

    definition = {
        "frame_path": [],
        "locators": [{"type": "css", "value": "[data-e2e='account']", "priority": 0}],
    }
    with pytest.raises(CampaignValidationError) as caught:
        asyncio.run(read_tiktok_identity(TwoVisibleNodes(), definition))
    assert caught.value.code == "tiktok_identity_unavailable"
