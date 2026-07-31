"""AdsPower Local API 控制器。"""

from __future__ import annotations

import os
import time
from typing import Any

import requests


DEFAULT_ADSPOWER_URL = "http://local.adspower.net:50325"


class AdsPowerError(RuntimeError):
    """AdsPower 请求在重试后仍然失败。"""


class AdsPowerController:
    """封装 AdsPower 本地 API 的浏览器窗口开关操作。"""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        if max_retries < 1:
            raise ValueError("max_retries 必须大于 0")
        self.base_url = (
            base_url or os.getenv("ADSPOWER_BASE_URL", DEFAULT_ADSPOWER_URL)
        ).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("ADSPOWER_API_KEY", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _request(
        self,
        endpoint: str,
        profile_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_params = {"user_id": profile_id, **(params or {})}
        response = requests.get(
            f"{self.base_url}{endpoint}",
            params=request_params,
            headers=self._headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise AdsPowerError("AdsPower 返回的数据格式不是 JSON 对象")
        code = payload.get("code")
        if code not in (None, 0):
            message = payload.get("msg") or payload.get("message") or code
            raise AdsPowerError(f"AdsPower API 错误：{message}")
        data = payload.get("data", payload)
        if not isinstance(data, dict):
            raise AdsPowerError("AdsPower 返回的 data 字段格式错误")
        return data

    def _request_with_retry(
        self,
        endpoint: str,
        profile_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._request(endpoint, profile_id, params)
            except (requests.RequestException, AdsPowerError, ValueError, TypeError) as error:
                # AdsPower 未启动、窗口被占用、网络超时和异常响应都进入统一重试流程。
                last_error = error
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
        raise AdsPowerError(
            f"AdsPower 请求失败，已重试 {self.max_retries} 次：{last_error}"
        ) from last_error

    def start_browser(self, profile_id: str) -> str:
        """启动指定窗口，成功后返回 ws.puppeteer 调试地址。"""

        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("profile_id 不能为空")
        data = self._request_with_retry(
            "/api/v1/browser/start",
            profile_id,
            {"open_tabs": 1, "ip_tab": 0},
        )
        ws = data.get("ws") or {}
        puppeteer_url = ws.get("puppeteer") if isinstance(ws, dict) else None
        if not puppeteer_url:
            raise AdsPowerError("AdsPower 启动成功但没有返回 ws.puppeteer 调试地址")
        return str(puppeteer_url)

    def stop_browser(self, profile_id: str) -> dict[str, Any]:
        """停止指定浏览器窗口，并返回 AdsPower 的响应数据。"""

        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("profile_id 不能为空")
        return self._request_with_retry("/api/v1/browser/stop", profile_id)

    def get_browser_active(self, profile_id: str) -> dict[str, Any]:
        """查询指定窗口当前是否仍处于活动状态。"""

        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("profile_id 不能为空")
        return self._request_with_retry("/api/v1/browser/active", profile_id)


__all__ = ["AdsPowerController", "AdsPowerError"]
