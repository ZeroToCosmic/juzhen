"""通过 AdsPower 返回的 Puppeteer CDP 地址控制浏览器页面。"""

from __future__ import annotations

import json
import time
from typing import Any

from websocket import create_connection

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - GUI 环境会在依赖检查阶段提示缺失
    sync_playwright = None


class CdpClient:
    def __init__(self, ws_url: str, timeout: float = 10.0):
        # AdsPower's Chromium rejects websocket-client's synthetic Origin header
        # unless the browser was launched with a permissive remote-origin flag.
        # CDP is local here, so omit Origin instead of weakening the browser.
        self.websocket = create_connection(
            ws_url,
            timeout=timeout,
            suppress_origin=True,
        )
        self._message_id = 0

    def close(self):
        self.websocket.close()

    def command(self, method: str, params: dict | None = None, session_id: str | None = None):
        self._message_id += 1
        payload = {"id": self._message_id, "method": method, "params": params or {}}
        if session_id:
            payload["sessionId"] = session_id
        self.websocket.send(json.dumps(payload))
        while True:
            response = json.loads(self.websocket.recv())
            if response.get("id") == self._message_id:
                if response.get("error"):
                    raise RuntimeError(response["error"].get("message", "CDP 调用失败"))
                return response.get("result", {})

    def page_targets(self) -> list[dict[str, Any]]:
        targets = self.command("Target.getTargets").get("targetInfos", [])
        return [target for target in targets if target.get("type") == "page"]

    def page_session(self) -> tuple[str, list[dict[str, Any]]]:
        pages = self.page_targets()
        if not pages:
            raise RuntimeError("浏览器中没有可操作的页面 tab")
        attached = self.command(
            "Target.attachToTarget",
            {"targetId": pages[0]["targetId"], "flatten": True},
        )
        return attached["sessionId"], pages


def wait_for_cdp(ws_url: str, timeout: float = 15.0, interval: float = 0.5) -> bool:
    """等待 AdsPower 返回的 CDP 地址真正可以连接。"""

    if not ws_url:
        raise ValueError("缺少 ws.puppeteer 调试地址")
    if timeout <= 0 or interval <= 0:
        raise ValueError("timeout 和 interval 必须大于 0")
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while True:
        client = None
        try:
            remaining = max(deadline - time.monotonic(), 0.1)
            client = CdpClient(ws_url, timeout=min(5.0, remaining))
            client.page_targets()
            return True
        except Exception as error:
            last_error = error
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(interval, remaining))
    raise RuntimeError(f"CDP 调试地址在 {timeout:.1f} 秒内未就绪：{last_error}") from last_error


def _navigate_with_playwright(ws_url: str, url: str, wait_seconds: float = 2.0) -> dict:
    if sync_playwright is None:
        raise RuntimeError("未安装 Playwright，无法连接 AdsPower 浏览器")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(ws_url, timeout=10_000)
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("AdsPower 调试地址没有可用的浏览器上下文")
        context = contexts[0]
        pages = list(context.pages)
        page = pages[0] if pages else context.new_page()
        closed_tabs = 0
        for extra_page in pages[1:]:
            extra_page.close()
            closed_tabs += 1
        page.goto(url, wait_until="commit", timeout=30_000)
        if wait_seconds > 0:
            page.wait_for_timeout(int(wait_seconds * 1000))
        for extra_page in list(context.pages):
            if extra_page is not page:
                extra_page.close()
                closed_tabs += 1
        if not page.url or page.url == "about:blank":
            raise RuntimeError("Playwright 导航后页面仍是 blank 空白页")
        return {"url": url, "closed_tabs": closed_tabs, "current_url": page.url}


def navigate_and_close_other_tabs(ws_url: str, url: str, wait_seconds: float = 2.0) -> dict:
    """使用 Playwright 导航并清理旧 Tab，失败时回退到原始 CDP。"""

    if not url.startswith(("http://", "https://")):
        raise ValueError("网址必须以 http:// 或 https:// 开头")
    playwright_error = None
    try:
        return _navigate_with_playwright(ws_url, url, wait_seconds)
    except Exception as error:
        playwright_error = error
    try:
        return _navigate_and_close_other_tabs_cdp(ws_url, url, wait_seconds)
    except Exception as cdp_error:
        raise RuntimeError(
            f"Playwright 导航失败：{playwright_error}；CDP 兜底也失败：{cdp_error}"
        ) from cdp_error


def _navigate_and_close_other_tabs_cdp(ws_url: str, url: str, wait_seconds: float = 2.0) -> dict:
    """在窗口内打开同一个网址，并关闭其他 tab。"""

    if not url.startswith(("http://", "https://")):
        raise ValueError("网址必须以 http:// 或 https:// 开头")
    client = CdpClient(ws_url)
    try:
        pages = client.page_targets()
        if not pages:
            created = client.command("Target.createTarget", {"url": "about:blank"})
            created_target_id = str(created.get("targetId") or "")
            if not created_target_id:
                raise RuntimeError("AdsPower 浏览器未创建出可导航的页面 Tab")
            pages = [{"targetId": created_target_id, "type": "page"}]
        attached = client.command(
            "Target.attachToTarget",
            {"targetId": pages[0]["targetId"], "flatten": True},
        )
        session_id = attached.get("sessionId")
        if not session_id:
            raise RuntimeError("无法连接到 AdsPower 当前页面 Tab")
        keep_target_id = pages[0]["targetId"]
        navigation = client.command("Page.navigate", {"url": url}, session_id)
        if navigation.get("errorText"):
            raise RuntimeError(f"目标网址打开失败：{navigation['errorText']}")

        closed_target_ids: set[str] = set()

        def close_other_pages() -> list[dict[str, Any]]:
            remaining = []
            for target in client.page_targets():
                target_id = str(target.get("targetId") or "")
                if target_id and target_id != keep_target_id:
                    client.command("Target.closeTarget", {"targetId": target_id})
                    closed_target_ids.add(target_id)
                    remaining.append(target)
            return remaining

        close_other_pages()
        time.sleep(max(wait_seconds, 0))
        remaining = close_other_pages()
        if remaining:
            close_other_pages()
            remaining = [
                target
                for target in client.page_targets()
                if str(target.get("targetId") or "") != keep_target_id
            ]
        if remaining:
            raise RuntimeError(f"关闭旧 Tab 后仍剩余 {len(remaining)} 个")
        current_url = _evaluate(
            client,
            session_id,
            "window.location.href || ''",
        )
        return {
            "url": url,
            "closed_tabs": len(closed_target_ids),
            "current_url": str(current_url or "").strip(),
        }
    finally:
        client.close()


def _evaluate(client: CdpClient, session_id: str, expression: str):
    result = client.command(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id,
    )
    exception = result.get("exceptionDetails")
    if exception:
        raise RuntimeError(exception.get("text", "页面脚本执行失败"))
    return result.get("result", {}).get("value")


def read_xpath_elements(ws_url: str, xpaths: dict[str, str]) -> list[dict[str, Any]]:
    """读取当前页面中指定 XPath 元素的存在性和文本。"""

    client = CdpClient(ws_url)
    try:
        session_id, _pages = client.page_session()
        results = []
        for alias, xpath in xpaths.items():
            expression = """(function() {
              const result = document.evaluate(%s, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
              const element = result.singleNodeValue;
              if (!element) return {exists: false, alias: %s, xpath: %s};
              element.scrollIntoView({block: 'center'});
              return {exists: true, alias: %s, xpath: %s, tag: element.tagName,
                text: (element.innerText || element.textContent || '').trim(),
                html: element.outerHTML.slice(0, 2000)};
            })()""" % tuple(json.dumps(value) for value in (xpath, alias, xpath, alias, xpath))
            results.append(_evaluate(client, session_id, expression))
        return results
    finally:
        client.close()


def execute_xpath_action(ws_url: str, action: dict[str, Any], elements: dict[str, str], text: str = "") -> dict[str, Any]:
    """对 XPath 元素执行 move/click/input 动作。"""

    action_type = str(action.get("type") or "").lower()
    alias = str(action.get("element") or "")
    if action_type == "pause":
        duration = float(action.get("duration", 1))
        if duration <= 0:
            raise ValueError("pause duration must be greater than zero")
        time.sleep(duration)
        return {"type": action_type, "element": "", "status": "ok", "duration": duration}
    if action_type in {"scroll_up", "scroll_down"}:
        distance = int(action.get("distance", 600))
        duration = float(action.get("duration", 1))
        if distance <= 0 or duration <= 0:
            raise ValueError("scroll distance and duration must be greater than zero")
        client = CdpClient(ws_url)
        try:
            session_id, _pages = client.page_session()
            viewport = _evaluate(
                client,
                session_id,
                "({width: window.innerWidth || 1280, height: window.innerHeight || 720})",
            ) or {"width": 1280, "height": 720}
            steps = max(3, min(20, round(duration * 8)))
            delta_y = (distance if action_type == "scroll_down" else -distance) / steps
            for _ in range(steps):
                client.command(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": float(viewport.get("width", 1280)) / 2,
                        "y": float(viewport.get("height", 720)) / 2,
                        "deltaX": 0,
                        "deltaY": delta_y,
                    },
                    session_id,
                )
                time.sleep(duration / steps)
            return {
                "type": action_type,
                "element": "",
                "status": "ok",
                "distance": distance,
                "duration": duration,
            }
        finally:
            client.close()
    xpath = elements.get(alias)
    if not xpath:
        raise ValueError(f"未找到动作引用的 XPath 元素：{alias}")
    client = CdpClient(ws_url)
    try:
        session_id, _pages = client.page_session()
        expression = """(function() {
          const element = document.evaluate(%s, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
          if (!element) return {ok: false, error: '元素不存在'};
          element.scrollIntoView({behavior: 'smooth', block: 'center'});
          %s
          return {ok: true};
        })()""" % (
            json.dumps(xpath),
            {
                "move": "",
                "click": "element.click();",
                "input": "element.focus(); const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set; if (setter) setter.call(element, %s); else element.textContent = %s; element.dispatchEvent(new Event('input', {bubbles: true})); element.dispatchEvent(new Event('change', {bubbles: true}));" % (json.dumps(text), json.dumps(text)),
            }.get(action_type, "throw new Error('不支持的动作类型');"),
        )
        result = _evaluate(client, session_id, expression)
        if not result or not result.get("ok"):
            raise RuntimeError((result or {}).get("error", "动作执行失败"))
        return {"type": action_type, "element": alias, "status": "ok"}
    finally:
        client.close()
