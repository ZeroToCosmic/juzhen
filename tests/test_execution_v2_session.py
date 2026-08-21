import asyncio

import pytest

from execution_v2.session import PlaywrightSessionFactory, SessionBindingError


class FakePage:
    def __init__(self, url):
        self.url = url
        self.closed = False

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, pages):
        self.pages = pages


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts


class FakeChromium:
    def __init__(self, browser):
        self.browser = browser
        self.calls = []

    async def connect_over_cdp(self, ws_url, *, timeout):
        self.calls.append((ws_url, timeout))
        return self.browser


class FakePlaywright:
    def __init__(self, browser):
        self.chromium = FakeChromium(browser)


def test_session_factory_uses_profile_ws_and_one_target_page():
    selected_page = FakePage("https://www.tiktok.com/")
    extra_page = FakePage("about:blank")
    context = FakeContext([selected_page, extra_page])
    playwright = FakePlaywright(FakeBrowser([context]))
    factory = PlaywrightSessionFactory(playwright, timeout=12_345)

    binding = asyncio.run(factory.connect("p1", "ws://p1"))

    assert binding.profile_id == "p1"
    assert binding.ws_url == "ws://p1"
    assert binding.browser.contexts == [context]
    assert binding.context is context
    assert binding.page is selected_page
    assert extra_page.closed is True
    assert playwright.chromium.calls == [("ws://p1", 12_345)]


def test_session_factory_closes_extra_blank_pages_when_all_pages_are_blank():
    selected_page = FakePage("about:blank")
    extra_page = FakePage("about:blank")
    factory = PlaywrightSessionFactory(
        FakePlaywright(FakeBrowser([FakeContext([selected_page, extra_page])]))
    )

    binding = asyncio.run(factory.connect("p1", "ws://p1"))

    assert binding.page is selected_page
    assert extra_page.closed is True


def test_session_factory_rejects_multiple_contexts():
    factory = PlaywrightSessionFactory(
        FakePlaywright(FakeBrowser([FakeContext([]), FakeContext([])]))
    )

    with pytest.raises(SessionBindingError, match="exactly one context"):
        asyncio.run(factory.connect("p1", "ws://p1"))


def test_session_factory_rejects_multiple_non_blank_pages():
    factory = PlaywrightSessionFactory(
        FakePlaywright(
            FakeBrowser(
                [FakeContext([FakePage("https://one.example/"), FakePage("https://two.example/")])]
            )
        )
    )

    with pytest.raises(SessionBindingError, match="multiple non-blank"):
        asyncio.run(factory.connect("p1", "ws://p1"))
