import browser_cdp
import pytest


def test_cdp_client_suppresses_websocket_origin_for_adspower(monkeypatch):
    calls = []

    class FakeSocket:
        def close(self):
            return None

    def fake_create_connection(url, **options):
        calls.append((url, options))
        return FakeSocket()

    monkeypatch.setattr(browser_cdp, "create_connection", fake_create_connection)

    client = browser_cdp.CdpClient("ws://127.0.0.1:50001/devtools/browser/test", timeout=3)
    client.close()

    assert calls == [
        (
            "ws://127.0.0.1:50001/devtools/browser/test",
            {"timeout": 3, "suppress_origin": True},
        )
    ]


class _FakeCdpClient:
    def __init__(self, _ws_url):
        self.commands = []
        self.targets = [
            {"targetId": "target-1", "type": "page"},
            {"targetId": "target-2", "type": "page"},
        ]

    def page_session(self):
        pages = [target for target in self.targets if target["type"] == "page"]
        return "session-1", pages

    def page_targets(self):
        return list(self.targets)

    def command(self, method, params=None, session_id=None):
        self.commands.append((method, params, session_id))
        if method == "Target.closeTarget":
            target_id = (params or {}).get("targetId")
            self.targets = [target for target in self.targets if target["targetId"] != target_id]
        if method == "Target.attachToTarget":
            return {"sessionId": "session-1"}
        return {}

    def close(self):
        return None


def test_read_xpath_elements_scrolls_target_into_view(monkeypatch):
    expressions = []
    monkeypatch.setattr(browser_cdp, "CdpClient", _FakeCdpClient)

    def fake_evaluate(_client, _session_id, expression):
        expressions.append(expression)
        return {"exists": True, "alias": "title", "xpath": "//h1", "text": "Hello"}

    monkeypatch.setattr(browser_cdp, "_evaluate", fake_evaluate)

    result = browser_cdp.read_xpath_elements("ws://debug", {"title": "//h1"})

    assert result == [{"exists": True, "alias": "title", "xpath": "//h1", "text": "Hello"}]
    assert "scrollIntoView({block: 'center'})" in expressions[0]


def test_navigate_and_close_other_tabs_keeps_one_target_tab(monkeypatch):
    client = _FakeCdpClient("ws://debug")
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _ws_url: client)
    monkeypatch.setattr(browser_cdp, "_navigate_with_playwright", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("test raw CDP")))
    monkeypatch.setattr(browser_cdp.time, "sleep", lambda _seconds: None)

    def fake_evaluate(_client, _session_id, expression):
        if "window.location.href" in expression:
            return "https://www.tiktok.com/"
        return None

    monkeypatch.setattr(browser_cdp, "_evaluate", fake_evaluate)

    result = browser_cdp.navigate_and_close_other_tabs(
        "ws://debug", "https://www.tiktok.com/", wait_seconds=0
    )

    assert result == {
        "url": "https://www.tiktok.com/",
        "closed_tabs": 1,
        "current_url": "https://www.tiktok.com/",
    }
    assert client.commands[1][0] == "Page.navigate"
    assert client.commands[1][1]["url"] == "https://www.tiktok.com/"
    assert client.commands[2][0] == "Target.closeTarget"
    assert client.commands[2][1] == {"targetId": "target-2"}


def test_navigate_creates_a_page_when_ads_power_starts_without_one(monkeypatch):
    class EmptyPageClient(_FakeCdpClient):
        def __init__(self, _ws_url):
            super().__init__(_ws_url)
            self.targets = []

        def command(self, method, params=None, session_id=None):
            result = super().command(method, params, session_id)
            if method == "Target.createTarget":
                self.targets = [{"targetId": "created-target", "type": "page"}]
                return {"targetId": "created-target"}
            if method == "Target.attachToTarget":
                return {"sessionId": "created-session"}
            return result

    client = EmptyPageClient("ws://debug")
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _ws_url: client)
    monkeypatch.setattr(browser_cdp, "_navigate_with_playwright", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("test raw CDP")))
    monkeypatch.setattr(browser_cdp.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(browser_cdp, "_evaluate", lambda *_args, **_kwargs: "https://www.tiktok.com/")

    result = browser_cdp.navigate_and_close_other_tabs(
        "ws://debug", "https://www.tiktok.com/", wait_seconds=0
    )

    assert result == {
        "url": "https://www.tiktok.com/",
        "closed_tabs": 0,
        "current_url": "https://www.tiktok.com/",
    }
    assert [command[0] for command in client.commands[:3]] == [
        "Target.createTarget",
        "Target.attachToTarget",
        "Page.navigate",
    ]


def test_playwright_navigation_uses_the_first_page_and_closes_other_pages(monkeypatch):
    class FakePage:
        def __init__(self, context, name):
            self.context = context
            self.name = name
            self.url = "about:blank"
            self.closed = False
            self.navigated_to = None

        def close(self):
            self.closed = True
            self.context._pages.remove(self)

        def goto(self, url, **_kwargs):
            self.navigated_to = url
            self.url = url

        def wait_for_timeout(self, _milliseconds):
            return None

    class FakeContext:
        def __init__(self):
            self._pages = []

        @property
        def pages(self):
            return list(self._pages)

        def new_page(self):
            page = FakePage(self, "created")
            self._pages.append(page)
            return page

    class FakeChromium:
        def __init__(self, context):
            self.context = context

        def connect_over_cdp(self, _ws_url, **_kwargs):
            return type("FakeBrowser", (), {"contexts": [self.context]})()

    class FakePlaywright:
        def __init__(self, context):
            self.chromium = FakeChromium(context)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    context = FakeContext()
    first = FakePage(context, "first")
    second = FakePage(context, "second")
    context._pages.extend([first, second])
    monkeypatch.setattr(browser_cdp, "sync_playwright", lambda: FakePlaywright(context))

    result = browser_cdp._navigate_with_playwright(
        "ws://debug", "https://www.tiktok.com/", wait_seconds=0
    )

    assert result == {
        "url": "https://www.tiktok.com/",
        "closed_tabs": 1,
        "current_url": "https://www.tiktok.com/",
    }
    assert first.navigated_to == "https://www.tiktok.com/"
    assert second.closed is True


def test_execute_xpath_action_supports_scroll_and_pause(monkeypatch):
    client = _FakeCdpClient("ws://debug")
    monkeypatch.setattr(browser_cdp, "CdpClient", lambda _ws_url: client)
    monkeypatch.setattr(browser_cdp.time, "sleep", lambda _seconds: None)

    scroll_result = browser_cdp.execute_xpath_action(
        "ws://debug",
        {"type": "scroll_down", "duration": 0.5, "distance": 300},
        {},
    )
    pause_result = browser_cdp.execute_xpath_action(
        "ws://debug",
        {"type": "pause", "duration": 0.1},
        {},
    )

    assert scroll_result["status"] == "ok"
    assert scroll_result["distance"] == 300
    assert pause_result == {"type": "pause", "element": "", "status": "ok", "duration": 0.1}
    assert sum(command[0] == "Input.dispatchMouseEvent" for command in client.commands) >= 3


def test_wait_for_cdp_retries_until_endpoint_is_ready(monkeypatch):
    attempts = []
    waits = []

    class ReadyClient:
        def __init__(self, _ws_url, timeout=10.0):
            attempts.append(timeout)
            if len(attempts) < 3:
                raise ConnectionError("CDP not ready")

        def page_targets(self):
            return [{"targetId": "target-1", "type": "page"}]

        def close(self):
            return None

    monkeypatch.setattr(browser_cdp, "CdpClient", ReadyClient)
    monkeypatch.setattr(browser_cdp.time, "sleep", waits.append)

    assert browser_cdp.wait_for_cdp("ws://debug", timeout=2, interval=0.1) is True
    assert len(attempts) == 3
    assert waits == [0.1, 0.1]
