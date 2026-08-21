"""HTTP boundary for isolated browser execution V2.

Routes in this module intentionally know nothing about SQLite, Playwright, or
AdsPower.  They only validate a small public request contract, invoke the
injected V2 service, and return a scrubbed response.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify, request

from execution_v2.elements import (
    ElementInUseError,
    ElementNotFoundError,
    ElementRevisionConflictError,
    ElementValidationError,
)
from execution_v2.picker import PickerError
from execution_v2.strategy import (
    StrategyNotFoundError,
    StrategyRevisionConflictError,
    StrategyValidationError,
)
from execution_v2.wheel_calibration import WheelCalibrationError


class BrowserV2ApiError(RuntimeError):
    """Public, fixed-code error suitable for a service implementation."""

    status = 400
    code = "invalid_request"


class BrowserV2NotFoundError(BrowserV2ApiError):
    status = 404
    code = "not_found"


class BrowserV2ConflictError(BrowserV2ApiError):
    status = 409
    code = "conflict"


class BrowserV2ValidationError(BrowserV2ApiError):
    status = 422
    code = "validation_failed"


class BrowserV2UnavailableError(BrowserV2ApiError):
    status = 503
    code = "runtime_unavailable"


_SENSITIVE_KEY_NAMES = {
    "password",
    "secret",
    "token",
    "access_token",
    "auth_token",
    "refresh_token",
    "csrf_token",
    "id_token",
    "cookie",
    "cookies",
    "authorization",
    "websocket",
    "ws_url",
    "api_key",
    "apikey",
}
_SAFE_ERROR_MESSAGES = {
    "invalid_request": "请求格式无效。",
    "not_found": "请求资源不存在。",
    "conflict": "当前数据已变化或仍被引用。",
    "validation_failed": "请求未通过业务校验。",
    "input_target_not_editable": "未能定位唯一可编辑输入框，请点选输入文字区域后重试。",
    "runtime_unavailable": "浏览器执行服务暂不可用。",
    "internal_error": "请求处理失败。",
    "wheel_calibration_missing": "尚未完成滚轮校准，请先在页面点选器中校准。",
    "wheel_calibration_inconsistent": "三次滚轮结果不一致，请重新校准。",
    "wheel_calibration_video_not_changed": "本次滚动没有切换视频，请重新操作。",
    "wheel_calibration_multiple_videos": "本次滚动切换了多个视频，请重新校准。",
    "wheel_calibration_unsupported_delta_mode": "当前滚轮单位无法可靠回放。",
    "wheel_calibration_context_lost": "校准窗口已关闭或页面上下文失效。",
    "wheel_calibration_replay_not_observed": "自动回放未能切换视频，请重新校准。",
    "wheel_calibration_replay_unstable": "自动回放后页面未恢复稳定，请重新校准。",
    "calibrated_video_switch_not_observed": "回放校准滚轮后未观察到视频切换。",
}


def create_browser_v2_blueprint(service_or_factory: object) -> Blueprint:
    """Create V2 API blueprint with a service instance or zero-argument factory."""

    blueprint = Blueprint("browser_execution_v2", __name__, url_prefix="/api/browser-v2")

    def service() -> object:
        return service_or_factory() if callable(service_or_factory) else service_or_factory

    @blueprint.get("/elements")
    def list_elements():
        return _data(_call(service(), "list_elements"))

    @blueprint.post("/elements")
    def create_element():
        payload = _json_object(
            allowed={"id", "name", "purpose", "kind", "definition", "status"},
            required={"id", "name", "purpose", "kind", "definition"},
        )
        return _data(_call(service(), "create_element", **payload), 201)

    @blueprint.get("/elements/<element_id>")
    def get_element(element_id: str):
        return _data(_required(_call(service(), "get_element", element_id)))

    @blueprint.put("/elements/<element_id>")
    def update_element(element_id: str):
        payload = _json_object(
            allowed={"expected_revision", "name", "definition", "status"},
            required={"expected_revision"},
        )
        if not ({"name", "definition", "status"} & payload.keys()):
            raise BrowserV2ApiError()
        _positive_int(payload["expected_revision"])
        return _data(_call(service(), "update_element", element_id, **payload))

    @blueprint.delete("/elements/<element_id>")
    def delete_element(element_id: str):
        payload = _json_object(allowed={"expected_revision"}, required={"expected_revision"})
        _positive_int(payload["expected_revision"])
        _call(service(), "delete_element", element_id, **payload)
        return _data({})

    @blueprint.post("/elements/<element_id>/validate")
    def validate_element(element_id: str):
        payload = _json_object(allowed={"profile_token"}, required={"profile_token"})
        _non_empty_string(payload["profile_token"])
        return _data(_call(service(), "validate_element", element_id, **payload))

    @blueprint.get("/profiles")
    def list_profiles():
        return _data(_call(service(), "list_profiles"))

    @blueprint.get("/content-libraries")
    def list_content_libraries():
        return _data(_call(service(), "list_content_libraries"))

    @blueprint.post("/picker/start")
    def start_picker():
        payload = _json_object(
            allowed={"profile_token", "target_url"}, required={"profile_token", "target_url"}
        )
        _non_empty_string(payload["profile_token"])
        _non_empty_string(payload["target_url"])
        return _data(_call(service(), "start_picker", **payload), 202)

    @blueprint.get("/picker/<session_id>")
    def get_picker(session_id: str):
        return _data(_required(_call(service(), "get_picker", session_id)))

    @blueprint.post("/picker/<session_id>/save")
    def save_picker_selection(session_id: str):
        payload = _json_object(
            allowed={"name", "purpose", "kind", "element_id", "expected_revision"},
            required={"name", "purpose", "kind"},
        )
        _non_empty_string(payload["name"])
        if payload["purpose"] not in {"action", "readiness"}:
            raise BrowserV2ApiError()
        if payload["kind"] not in {"click", "input", "generic"}:
            raise BrowserV2ApiError()
        has_element_id = "element_id" in payload
        has_revision = "expected_revision" in payload
        if has_element_id != has_revision:
            raise BrowserV2ApiError()
        if has_element_id:
            _non_empty_string(payload["element_id"])
            _positive_int(payload["expected_revision"])
        return _data(_call(service(), "save_picker_selection", session_id, **payload), 201)

    @blueprint.post("/picker/<session_id>/finish")
    def finish_picker(session_id: str):
        _json_object(allowed=set(), required=set())
        return _data(_call(service(), "finish_picker", session_id))

    @blueprint.post("/picker/<session_id>/cancel")
    def cancel_picker(session_id: str):
        _json_object(allowed=set(), required=set())
        return _data(_call(service(), "cancel_picker", session_id), 202)

    @blueprint.get("/wheel-calibration")
    def get_wheel_calibration():
        return _data(_call(service(), "get_wheel_calibration"))

    @blueprint.post("/wheel-calibration/start")
    def start_wheel_calibration():
        payload = _json_object(
            allowed={"profile_token", "target_url"},
            required={"profile_token", "target_url"},
        )
        _non_empty_string(payload["profile_token"])
        _non_empty_string(payload["target_url"])
        return _data(_call(service(), "start_wheel_calibration", **payload), 202)

    @blueprint.post("/wheel-calibration/cancel")
    def cancel_wheel_calibration():
        _json_object(allowed=set(), required=set())
        return _data(_call(service(), "cancel_wheel_calibration"), 202)

    @blueprint.get("/strategies")
    def list_strategies():
        return _data(_call(service(), "list_strategies"))

    @blueprint.post("/strategies")
    def create_strategy():
        payload = _json_object(
            allowed={"id", "name", "definition", "enabled"},
            required={"id", "name", "definition"},
        )
        return _data(_call(service(), "create_strategy", **payload), 201)

    @blueprint.get("/strategies/<strategy_id>")
    def get_strategy(strategy_id: str):
        return _data(_required(_call(service(), "get_strategy", strategy_id)))

    @blueprint.put("/strategies/<strategy_id>")
    def update_strategy(strategy_id: str):
        payload = _json_object(
            allowed={"expected_revision", "name", "definition", "enabled"},
            required={"expected_revision"},
        )
        if not ({"name", "definition", "enabled"} & payload.keys()):
            raise BrowserV2ApiError()
        _positive_int(payload["expected_revision"])
        return _data(_call(service(), "update_strategy", strategy_id, **payload))

    @blueprint.delete("/strategies/<strategy_id>")
    def delete_strategy(strategy_id: str):
        payload = _json_object(allowed={"expected_revision"}, required={"expected_revision"})
        _positive_int(payload["expected_revision"])
        _call(service(), "delete_strategy", strategy_id, **payload)
        return _data({})

    @blueprint.post("/jobs")
    def start_job():
        payload = _json_object(
            allowed={"strategy_id", "profile_tokens", "batch_size"},
            required={"strategy_id", "profile_tokens"},
        )
        _non_empty_string(payload["strategy_id"])
        _profile_tokens(payload["profile_tokens"])
        if "batch_size" in payload:
            _bounded_int(payload["batch_size"], 1, 8)
        return _data(_call(service(), "start_job", **payload), 202)

    @blueprint.get("/jobs/<job_id>")
    def get_job(job_id: str):
        return _data(_required(_call(service(), "get_job", job_id)))

    @blueprint.post("/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        _json_object(allowed=set(), required=set())
        return _data(_call(service(), "cancel_job", job_id), 202)

    @blueprint.get("/jobs/<job_id>/results")
    def get_job_results(job_id: str):
        return _data(_required(_call(service(), "get_results", job_id)))

    @blueprint.get("/history")
    def get_history():
        allowed = {"limit", "offset"}
        if set(request.args) - allowed:
            raise BrowserV2ApiError()
        limit = _query_int("limit", default=50, minimum=1, maximum=200)
        offset = _query_int("offset", default=0, minimum=0, maximum=1000000)
        return _data(_call(service(), "history", limit=limit, offset=offset))

    @blueprint.errorhandler(Exception)
    def handle_error(error: Exception):
        # Flask selects this blueprint handler for exceptions raised by the
        # legacy app-wide authentication guard before an API view runs.  Keep
        # the old auth contract instead of translating it into V2's 500.
        if type(error).__name__ == "AuthError" and type(error).__module__ == "gateway.auth_service":
            return jsonify({"code": error.code}), error.status
        status, code = _error_status(error)
        return _error(code), status

    return blueprint


def _call(service: object, method: str, *args: object, **kwargs: object) -> Any:
    function = getattr(service, method, None)
    if not callable(function):
        raise BrowserV2UnavailableError()
    return function(*args, **kwargs)


def _required(value: Any) -> Any:
    if value is None:
        raise BrowserV2NotFoundError()
    return value


def _json_object(*, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not request.is_json:
        raise BrowserV2ApiError()
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not required <= payload.keys() or not payload.keys() <= allowed:
        raise BrowserV2ApiError()
    return payload


def _non_empty_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BrowserV2ApiError()
    return value


def _positive_int(value: Any) -> int:
    return _bounded_int(value, 1, 2**31 - 1)


def _bounded_int(value: Any, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BrowserV2ApiError()
    return value


def _profile_tokens(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > 300:
        raise BrowserV2ApiError()
    identities = [_non_empty_string(item) for item in value]
    if len(set(identities)) != len(identities):
        raise BrowserV2ApiError()
    return identities


def _query_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    value = request.args.get(name)
    if value is None:
        return default
    if not value.isdecimal():
        raise BrowserV2ApiError()
    return _bounded_int(int(value), minimum, maximum)


def _data(value: Any, status: int = 200):
    return jsonify({"data": _redact(value)}), status


def _error(code: str):
    return jsonify({"error": {"code": code, "message": _SAFE_ERROR_MESSAGES[code]}})


def _error_status(error: Exception) -> tuple[int, str]:
    if isinstance(error, BrowserV2ApiError):
        return error.status, error.code
    if isinstance(error, (ElementNotFoundError, StrategyNotFoundError, KeyError)):
        return 404, "not_found"
    if isinstance(error, (ElementRevisionConflictError, StrategyRevisionConflictError, ElementInUseError)):
        return 409, "conflict"
    if isinstance(error, PickerError) and str(error) == "picker_input_target_not_editable":
        return 422, "input_target_not_editable"
    if isinstance(error, WheelCalibrationError):
        code = str(error.code)
        return 422, code if code in _SAFE_ERROR_MESSAGES else "validation_failed"
    if isinstance(error, (ElementValidationError, StrategyValidationError, PickerError, ValueError, TypeError)):
        return 422, "validation_failed"
    name = type(error).__name__.lower()
    if "unavailable" in name or "runtime" in name and "error" in name:
        return 503, "runtime_unavailable"
    if "notfound" in name or "not_found" in name:
        return 404, "not_found"
    if "conflict" in name or "inuse" in name or "in_use" in name:
        return 409, "conflict"
    if "validation" in name or "invalid" in name:
        return 422, "validation_failed"
    return 500, "internal_error"


def _redact(value: Any) -> Any:
    """Recursively remove secrets and raw V2 transport/profile identifiers."""

    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _forbidden_key(text_key):
                continue
            cleaned[text_key] = "[redacted]" if _sensitive_key(text_key) else _redact(item)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and value.lower().startswith(("ws://", "wss://")):
        return "[redacted]"
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    # Opaque tokens are public V2 handles.  Never redact them: UI must return
    # these exact values to start picker validation and jobs.  All raw profile
    # identity fields remain forbidden, as do authentication tokens.
    if normalized in {"profile_token", "profile_tokens", "session_id", "job_id"}:
        return False
    return normalized in _SENSITIVE_KEY_NAMES


def _forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    # Do not expose a key whose value is known to be an AdsPower identity,
    # even redacted: public V2 objects use only opaque profile_token handles.
    return normalized in {"profile_id", "profile_ids", "raw_profile_id", "raw_profile_ids"} or (
        normalized.endswith("_profile_id") or normalized.endswith("_profile_ids")
    )


__all__ = [
    "BrowserV2ApiError",
    "BrowserV2ConflictError",
    "BrowserV2NotFoundError",
    "BrowserV2UnavailableError",
    "BrowserV2ValidationError",
    "create_browser_v2_blueprint",
]
