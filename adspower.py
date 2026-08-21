"""AdsPower Local API 控制器。"""

from __future__ import annotations

import os
import time
from typing import Any

import requests


DEFAULT_ADSPOWER_URL = "http://local.adspower.net:50325"


class AdsPowerError(RuntimeError):
    """AdsPower 请求在重试后仍然失败。"""


class AdsPowerDependencyError(AdsPowerError):
    """A public dependency error which carries no AdsPower response details."""

    _SAFE_REASONS = frozenset({
        "connected", "timeout", "connection_refused", "authentication_failed",
        "invalid_response", "not_configured",
    })

    def __init__(self, reason: str) -> None:
        if reason not in self._SAFE_REASONS:
            raise ValueError("unsupported AdsPower dependency reason")
        self.reason = reason
        super().__init__(reason)


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
        profile_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        request_params = dict(params or {})
        if profile_id is not None:
            normalized_profile_id = str(profile_id).strip()
            if not normalized_profile_id:
                raise ValueError("profile_id cannot be empty")
            request_params["user_id"] = normalized_profile_id
        dependency_reason: str | None = None
        try:
            response = requests.get(
                f"{self.base_url}{endpoint}",
                params=request_params,
                headers=self._headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            dependency_reason = (
                "authentication_failed" if status in {401, 403} else "invalid_response"
            )
        except requests.ConnectionError as error:
            if _is_explicit_connection_refused(error):
                dependency_reason = "connection_refused"
            else:
                dependency_reason = "invalid_response"
        except requests.Timeout:
            dependency_reason = "timeout"
        except requests.RequestException:
            dependency_reason = "invalid_response"
        except (TypeError, ValueError):
            dependency_reason = "invalid_response"
        if dependency_reason is not None:
            raise AdsPowerDependencyError(dependency_reason)
        if not isinstance(payload, dict):
            raise AdsPowerDependencyError("invalid_response")
        code = payload.get("code")
        if code not in (None, 0):
            reason = (
                "authentication_failed"
                if _is_business_authentication_failure(code, payload)
                else "invalid_response"
            )
            raise AdsPowerDependencyError(reason)
        data = payload.get("data", payload)
        if not isinstance(data, (dict, list)):
            raise AdsPowerDependencyError("invalid_response")
        return data

    def _request_with_retry(
        self,
        endpoint: str,
        profile_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return self._request(endpoint, profile_id, params)
            except (requests.RequestException, AdsPowerError, ValueError, TypeError) as error:
                # AdsPower 未启动、窗口被占用、网络超时和异常响应都进入统一重试流程。
                last_error = error
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay)
        if isinstance(last_error, AdsPowerDependencyError):
            raise last_error
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
        if not isinstance(data, dict):
            raise AdsPowerError("AdsPower browser start response is invalid")
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
        data = self._request_with_retry("/api/v1/browser/stop", profile_id)
        if not isinstance(data, dict):
            raise AdsPowerError("AdsPower browser stop response is invalid")
        return data

    def get_browser_active(self, profile_id: str) -> dict[str, Any]:
        """查询指定窗口当前是否仍处于活动状态。"""

        profile_id = str(profile_id or "").strip()
        if not profile_id:
            raise ValueError("profile_id 不能为空")
        data = self._request_with_retry("/api/v1/browser/active", profile_id)
        if not isinstance(data, dict):
            raise AdsPowerError("AdsPower active response is invalid")
        return data

    def list_profiles(self, *, page: int = 1, page_size: int = 200) -> list[dict[str, Any]]:
        """Return a small internal profile list without passing an empty user_id."""

        profiles, _raw_count, _total = self._list_profile_page(
            page=page,
            page_size=page_size,
        )
        return profiles

    def _list_profile_page(
        self, *, page: int, page_size: int
    ) -> tuple[list[dict[str, Any]], int, int | None]:
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError("page must be a positive integer")
        if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 200:
            raise ValueError("page_size must be an integer between 1 and 200")
        data = self._request_with_retry(
            "/api/v1/user/list", None, {"page": page, "page_size": page_size}
        )
        if isinstance(data, list):
            raw_rows = data
        elif "list" in data:
            raw_rows = data["list"]
        else:
            raise AdsPowerDependencyError("invalid_response")
        if not isinstance(raw_rows, list):
            raise AdsPowerDependencyError("invalid_response")
        total = self._profile_list_total(data) if isinstance(data, dict) else None
        profiles: list[dict[str, Any]] = []
        for row in raw_rows:
            if not isinstance(row, dict):
                continue
            profile_id = row.get("user_id", row.get("profile_id", row.get("id", "")))
            if not isinstance(profile_id, str) or not profile_id.strip():
                continue
            record: dict[str, Any] = {
                "id": profile_id.strip(),
                "name": str(row.get("name", "") or "").strip(),
                "status": str(row.get("status", "") or "").strip(),
            }
            group_name = row.get("group_name")
            if isinstance(group_name, str) and group_name.strip():
                record["group_name"] = group_name.strip()
            profiles.append(record)
        return profiles, len(raw_rows), total

    @staticmethod
    def _profile_list_total(data: dict[str, Any]) -> int | None:
        for key in ("total", "total_num", "total_count"):
            value = data.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and value.isdecimal():
                return int(value)
        return None

    def list_all_profiles(
        self, *, page_size: int = 200, max_profiles: int = 1000
    ) -> list[dict[str, Any]]:
        if (
            isinstance(page_size, bool)
            or not isinstance(page_size, int)
            or not 1 <= page_size <= 200
        ):
            raise ValueError("page_size must be an integer between 1 and 200")
        if (
            isinstance(max_profiles, bool)
            or not isinstance(max_profiles, int)
            or not 1 <= max_profiles <= 5000
        ):
            raise ValueError("max_profiles must be between 1 and 5000")
        profiles: list[dict[str, Any]] = []
        profile_ids: set[str] = set()
        page = 1
        max_pages = (5000 + page_size - 1) // page_size + 1
        while len(profiles) < max_profiles:
            rows, raw_count, total = self._list_profile_page(
                page=page,
                page_size=page_size,
            )
            for row in rows:
                profile_id = row["id"]
                if profile_id in profile_ids:
                    continue
                profiles.append(row)
                profile_ids.add(profile_id)
                if len(profiles) == max_profiles:
                    break
            if total is not None and page * page_size >= total:
                break
            if total is None and raw_count < page_size:
                break
            if page == max_pages:
                raise AdsPowerDependencyError("invalid_response")
            page += 1
        return profiles


def _is_explicit_connection_refused(error: BaseException) -> bool:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if isinstance(current, OSError) and getattr(current, "winerror", None) == 10061:
            return True
        if isinstance(current, OSError) and getattr(current, "errno", None) in {61, 111, 10061}:
            return True
        for nested in (current.__cause__, current.__context__, *current.args):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _is_business_authentication_failure(code: object, payload: dict[str, Any]) -> bool:
    """Only the documented ``-1`` API-key messages are authentication failures."""
    if code != -1:
        return False
    message = str(payload.get("msg") or payload.get("message") or "").strip().casefold()
    return any(
        message.startswith(prefix)
        for prefix in (
            "api key invalid",
            "invalid api key",
            "api key is invalid",
            "authentication failed",
            "unauthorized",
        )
    )


__all__ = ["AdsPowerController", "AdsPowerDependencyError", "AdsPowerError"]
