"""Strict one-Profile-to-one-CDP-session bindings for browser execution V2."""

from __future__ import annotations

import inspect
from typing import Any, Protocol

from .models import BrowserBinding


class SessionBindingError(RuntimeError):
    """The CDP endpoint cannot be represented as one safe browser binding."""


class SessionFactory(Protocol):
    async def connect(self, profile_id: str, ws_url: str) -> BrowserBinding: ...


class PlaywrightSessionFactory:
    """Connect a supplied Playwright async instance to one AdsPower CDP endpoint."""

    def __init__(self, playwright: Any, *, timeout: float = 10_000) -> None:
        if playwright is None or not hasattr(playwright, "chromium"):
            raise TypeError("playwright must expose chromium.connect_over_cdp")
        self._playwright = playwright
        self._timeout = timeout

    async def connect(self, profile_id: str, ws_url: str) -> BrowserBinding:
        browser = await _await_if_needed(
            self._playwright.chromium.connect_over_cdp(ws_url, timeout=self._timeout)
        )
        contexts = tuple(getattr(browser, "contexts", ()))
        if len(contexts) != 1:
            raise SessionBindingError("CDP endpoint must expose exactly one context")

        context = contexts[0]
        pages = tuple(
            page for page in getattr(context, "pages", ()) if not _page_is_closed(page)
        )
        if not pages:
            raise SessionBindingError("CDP endpoint has no live page")

        non_blank_pages = [page for page in pages if _page_url(page) != "about:blank"]
        if len(non_blank_pages) > 1:
            raise SessionBindingError("CDP endpoint has multiple non-blank pages")
        page = non_blank_pages[0] if non_blank_pages else pages[0]

        for other_page in pages:
            if other_page is not page and _page_url(other_page) == "about:blank":
                await _await_if_needed(other_page.close())

        return BrowserBinding(profile_id, ws_url, browser, context, page)


def _page_url(page: Any) -> str:
    return str(getattr(page, "url", "about:blank") or "about:blank")


def _page_is_closed(page: Any) -> bool:
    is_closed = getattr(page, "is_closed", None)
    return bool(is_closed() if callable(is_closed) else is_closed)


async def _await_if_needed(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


__all__ = ["PlaywrightSessionFactory", "SessionBindingError", "SessionFactory"]
