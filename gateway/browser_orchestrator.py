"""AdsPower browser-session startup orchestration."""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from browser_public_identity import mask_profile_id


_URL_PATTERN = re.compile(r"\b(?:wss?|https?)://[^\s,;\)\]\}>\"']+", re.IGNORECASE)
_API_KEY_PATTERN = re.compile(
    r"\b(api[\s_-]*key)(\s*(?:=|:)\s*|\s+)([^\s,;]+)", re.IGNORECASE
)
_BEARER_PATTERN = re.compile(r"\b(bearer)(\s+)([^\s,;]+)", re.IGNORECASE)


def _sanitize_text(value: str) -> str:
    text = _URL_PATTERN.sub("[redacted-url]", value)
    text = _API_KEY_PATTERN.sub(r"\1\2[redacted]", text)
    return _BEARER_PATTERN.sub(r"\1\2[redacted]", text)


def _safe_error(value: object) -> str:
    """Return error text without endpoint URLs or credential values."""

    if isinstance(value, BaseException):
        sanitized_args = _sanitize_public_value(value.args)
        if len(sanitized_args) == 1 and isinstance(sanitized_args[0], str):
            return sanitized_args[0]
        sanitized = sanitized_args
    elif isinstance(value, (dict, list, tuple)):
        sanitized = _sanitize_public_value(value)
    else:
        return _sanitize_text(str(value or ""))

    return json.dumps(
        sanitized,
        ensure_ascii=False,
        default=lambda item: _sanitize_text(str(item)),
    )


def _is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    return any(
        marker in normalized
        for marker in (
            "apikey",
            "token",
            "authorization",
            "secret",
            "wsurl",
            "wspuppeteer",
        )
    )


def _sanitize_public_value(value: Any, key: object = "") -> Any:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized == "profileid":
        return mask_profile_id(value)
    if isinstance(value, dict):
        return {
            item_key: _sanitize_public_value(item, item_key)
            for item_key, item in value.items()
            if not _is_sensitive_key(item_key)
        }
    if isinstance(value, list):
        return [_sanitize_public_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_public_value(item) for item in value)
    if isinstance(value, str):
        return _sanitize_text(value)
    return value


def _session_result(
    profile: dict[str, Any],
    *,
    status: str,
    stage: str,
    attempts: int,
    ws_url: str = "",
    error: object = "",
) -> dict[str, Any]:
    return {
        "profile_id": str(profile.get("profile_id") or "").strip(),
        "profile_no": str(profile.get("profile_no") or ""),
        "name": str(profile.get("name") or ""),
        "status": status,
        "stage": stage,
        "attempts": attempts,
        "ws_url": ws_url,
        "error": _safe_error(error),
    }


def _active_reason(controller: Any, profile_id: str) -> str:
    try:
        active = controller.get_browser_active(profile_id)
    except Exception as error:
        return f"活动状态查询失败：{_safe_error(error)}"

    if not isinstance(active, dict):
        return "活动状态查询返回无效数据"
    status = active.get("status") or active.get("state")
    message = active.get("msg") or active.get("message")
    if message:
        return f"活动状态：{_safe_error(message)}"
    if status is not None:
        return f"活动状态：{_safe_error(status)}"
    return "活动状态查询完成"


def ensure_profile_session(
    profile: dict[str, Any],
    current_ws: str | None,
    controller: Any,
    wait_for_cdp: Callable[[str], object],
    retries: int = 3,
    sleep_fn: Callable[[float], object] = time.sleep,
) -> dict[str, Any]:
    """Return a ready session or a structured, per-profile failure result."""

    profile = profile if isinstance(profile, dict) else {}
    if isinstance(retries, bool) or not isinstance(retries, int) or retries <= 0:
        return _session_result(
            profile,
            status="failed",
            stage="validation",
            attempts=0,
            error="retries 必须是大于 0 的整数",
        )
    retries = min(retries, 3)

    profile_id = str(profile.get("profile_id") or "").strip()
    if not profile_id:
        return _session_result(
            profile,
            status="failed",
            stage="session_check",
            attempts=0,
            error="profile_id 不能为空",
        )

    ws_url = str(current_ws or "").strip()
    if ws_url:
        try:
            if wait_for_cdp(ws_url):
                return _session_result(
                    profile,
                    status="ready",
                    stage="session_check",
                    attempts=0,
                    ws_url=ws_url,
                )
        except Exception:
            pass
        _active_reason(controller, profile_id)
        try:
            controller.stop_browser(profile_id)
        except Exception:
            pass

    last_stage = "start_browser"
    last_error = ""
    last_ws_url = ""
    for attempt in range(1, retries + 1):
        last_ws_url = ""
        try:
            last_ws_url = str(controller.start_browser(profile_id) or "").strip()
            if not last_ws_url:
                raise RuntimeError("AdsPower 启动成功但没有返回 CDP 地址")
        except Exception as error:
            last_stage = "start_browser"
            last_error = _safe_error(error)
        else:
            try:
                if wait_for_cdp(last_ws_url):
                    return _session_result(
                        profile,
                        status="ready",
                        stage="session_start",
                        attempts=attempt,
                        ws_url=last_ws_url,
                    )
                last_error = "CDP 未就绪"
            except Exception as error:
                last_error = _safe_error(error) or "CDP 未就绪"
            last_stage = "wait_for_cdp"

        last_error = f"{last_error}；{_active_reason(controller, profile_id)}"
        if attempt < retries:
            try:
                controller.stop_browser(profile_id)
            except Exception:
                pass
            sleep_fn(2.0)

    return _session_result(
        profile,
        status="failed",
        stage=last_stage,
        attempts=retries,
        ws_url=last_ws_url,
        error=last_error,
    )


def public_session_result(result: dict[str, Any]) -> dict[str, Any]:
    """Copy a session result for responses or logs without sensitive CDP data."""

    return _sanitize_public_value(result)


__all__ = ["ensure_profile_session", "public_session_result"]
