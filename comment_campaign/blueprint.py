"""Strict HTTP boundary for the local Comment Campaign workbench.

This module deliberately exposes planning data only.  Queueing and browser
submission are introduced by later stages, and their routes fail closed here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from flask import Blueprint, jsonify, request
from pydantic import BaseModel, ValidationError

from .errors import (
    CampaignError,
    CampaignNotFoundError,
    RevisionConflictError,
    StateTransitionError,
)
from .schemas import (
    AssignmentOverride,
    CampaignCreate,
    CampaignPauseRequest,
    CommentSettingsUpdate,
    ExpectedRevision,
    PlanRequest,
    ProfileMetadataUpsert,
    RejectSubmitRequest,
    ResolveUnverifiedRequest,
    TemplateCreate,
    TemplateUpdate,
)


_MESSAGES = {
    "invalid_request": "请求格式无效。",
    "validation_failed": "请求未通过业务校验。",
    "not_found": "请求的资源不存在。",
    "revision_conflict": "数据已变更，请刷新后重试。",
    "runtime_unavailable": "该操作尚未就绪。",
    "adspower_unavailable": "AdsPower 服务暂不可用。",
    "content_library_unavailable": "文案库暂不可用。",
    "internal_error": "请求处理失败。",
    "profile_start_failed": "Profile 启动失败。",
    "cdp_connect_failed": "浏览器连接失败。",
    "profile_identity_mismatch": "登录身份校验失败。",
    "target_video_invalid": "目标视频无效。",
    "target_video_mismatch": "当前视频与目标不一致。",
    "comment_panel_not_ready": "评论区尚未就绪。",
    "comment_input_not_found": "未找到评论输入框。",
    "parent_comment_not_found": "未找到父评论。",
    "parent_comment_ambiguous": "父评论匹配不唯一。",
    "comment_author_mismatch": "评论作者不匹配。",
    "reply_target_mismatch": "回复目标不匹配。",
    "comment_submit_uncertain": "评论提交结果无法确认。",
    "comment_receipt_unverified": "评论回执未验证。",
    "profile_close_failed": "Profile 关闭失败。",
    "redis_unavailable": "Redis 服务暂不可用。",
    "worker_unavailable": "Worker 服务暂不可用。",
    "allocation_unsatisfied": "没有满足条件的完整分配方案。",
    "template_invalid": "评论模板无效。",
    "approval_revision_mismatch": "审批版本不匹配。",
    "invalid_state_transition": "当前状态不允许此操作。",
}
_UNAVAILABLE_CODES = frozenset(
    {"adspower_unavailable", "content_library_unavailable", "redis_unavailable", "worker_unavailable"}
)
_ALLOWED_STATUS = frozenset(
    {"draft", "planned", "awaiting_campaign_approval", "queued", "running", "paused", "failed", "completed", "cancelled"}
)
_ALLOWED_PROFILE_KEYS = frozenset(
    {"profile_ref", "display_profile", "campaign_id", "assignment_id", "receipt_id"}
)


class CampaignApiError(RuntimeError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code if code in _MESSAGES else "internal_error"
        self.status = status
        super().__init__(self.code)


def create_comment_campaign_blueprint(service_or_factory: object) -> Blueprint:
    """Create the planning API around an injected service or lazy factory."""

    blueprint = Blueprint(
        "comment_campaign", __name__, url_prefix="/api/browser-v2"
    )

    def service() -> object:
        return service_or_factory() if callable(service_or_factory) else service_or_factory

    @blueprint.before_request
    def reject_duplicate_get_query():
        if request.method == "GET":
            allowed = (
                {"status", "limit", "offset"}
                if request.endpoint == "comment_campaign.list_campaigns"
                else set()
            )
            if set(request.args) - allowed or any(
                len(values) != 1 for _key, values in request.args.lists()
            ):
                raise CampaignApiError("invalid_request")

    @blueprint.url_value_preprocessor
    def validate_path_ids(_endpoint: str | None, values: dict[str, Any] | None):
        for key in ("template_id", "campaign_id", "assignment_id"):
            value = (values or {}).get(key)
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or len(value) > 120
            ):
                raise CampaignApiError("invalid_request")

    @blueprint.get("/comment-templates")
    def list_templates():
        return _data(_call(service(), "list_templates"))

    @blueprint.post("/comment-templates")
    def create_template():
        payload = _parse(TemplateCreate)
        return _data(_call(service(), "create_template", payload), 201)

    @blueprint.get("/comment-templates/<template_id>")
    def get_template(template_id: str):
        return _data(_required(_call(service(), "get_template", template_id)))

    @blueprint.put("/comment-templates/<template_id>")
    def update_template(template_id: str):
        payload = _parse(TemplateUpdate)
        return _data(_call(service(), "update_template", template_id, payload))

    @blueprint.post("/comment-templates/<template_id>/disable")
    def disable_template(template_id: str):
        payload = _parse(ExpectedRevision)
        return _data(
            _call(service(), "disable_template", template_id, payload.expected_revision)
        )

    @blueprint.get("/comment-profile-metadata")
    def list_profile_metadata():
        return _data(_call(service(), "list_profile_metadata"))

    @blueprint.post("/comment-profile-metadata")
    def upsert_profile_metadata():
        payload = _parse(ProfileMetadataUpsert)
        return _data(
            _call(service(), "upsert_profile_metadata", payload.model_dump())
        )

    @blueprint.get("/comment-campaigns")
    def list_campaigns():
        _validate_campaign_query()
        status = request.args.get("status")
        return _data(
            _call(
                service(),
                "list_campaigns",
                status=status,
                limit=_query_int("limit", 50, 1, 200),
                offset=_query_int("offset", 0, 0, 1_000_000),
            )
        )

    @blueprint.post("/comment-campaigns")
    def create_campaign():
        payload = _parse(CampaignCreate)
        return _data(_call(service(), "create_campaign", payload), 201)

    @blueprint.get("/comment-campaigns/<campaign_id>")
    def get_campaign(campaign_id: str):
        return _data(
            _required(_call(service(), "get_campaign_detail", campaign_id))
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/plan")
    def plan_campaign(campaign_id: str):
        payload = _parse(PlanRequest)
        return _data(
            _call(
                service(), "plan_campaign", campaign_id,
                payload.allocation_seed or None,
                expected_revision=payload.expected_revision,
            )
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/reallocate")
    def reallocate_campaign(campaign_id: str):
        payload = _parse(PlanRequest)
        return _data(
            _call(
                service(), "reallocate_campaign", campaign_id,
                payload.allocation_seed or None,
                expected_revision=payload.expected_revision,
            )
        )

    @blueprint.put("/comment-campaigns/<campaign_id>/assignments/<assignment_id>")
    def override_assignment(campaign_id: str, assignment_id: str):
        payload = _parse(AssignmentOverride)
        return _data(
            _call(
                service(),
                "override_assignment",
                campaign_id,
                assignment_id,
                payload.model_dump(),
            )
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/lock-plan")
    def lock_plan(campaign_id: str):
        payload = _parse(ExpectedRevision)
        return _data(_call(service(), "lock_plan", campaign_id, payload.expected_revision))

    @blueprint.post("/comment-campaigns/<campaign_id>/approve")
    def approve_campaign(campaign_id: str):
        payload = _parse(ExpectedRevision)
        return _data(
            _call(service(), "approve_campaign", campaign_id, payload.expected_revision),
            202,
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/pause")
    def pause_campaign(campaign_id: str):
        payload = _parse(CampaignPauseRequest)
        return _data(
            _call(
                service(), "pause_campaign", campaign_id,
                payload.expected_revision, payload.reason,
            )
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/resume")
    def resume_campaign(campaign_id: str):
        payload = _parse(ExpectedRevision)
        return _data(
            _call(service(), "resume_campaign", campaign_id, payload.expected_revision),
            202,
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/cancel")
    def cancel_campaign(campaign_id: str):
        payload = _parse(ExpectedRevision)
        return _data(
            _call(service(), "cancel_campaign", campaign_id, payload.expected_revision)
        )

    @blueprint.get("/comment-campaigns/<campaign_id>/approvals")
    def approvals(campaign_id: str):
        return _data(_call(service(), "list_approvals", campaign_id))

    @blueprint.post("/comment-campaigns/<campaign_id>/assignments/<assignment_id>/approve-submit")
    def approve_submit(campaign_id: str, assignment_id: str):
        payload = _parse(ExpectedRevision)
        return _data(
            _call(
                service(), "approve_submit", campaign_id, assignment_id,
                payload.expected_revision,
            ),
            202,
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/assignments/<assignment_id>/reject-submit")
    def reject_submit(campaign_id: str, assignment_id: str):
        payload = _parse(RejectSubmitRequest)
        return _data(
            _call(
                service(), "reject_submit", campaign_id, assignment_id,
                payload.expected_revision, payload.reason,
            )
        )

    @blueprint.post("/comment-campaigns/<campaign_id>/assignments/<assignment_id>/resolve-unverified")
    def resolve_unverified(campaign_id: str, assignment_id: str):
        payload = _parse(ResolveUnverifiedRequest)
        return _data(
            _call(
                service(), "resolve_unverified", campaign_id, assignment_id,
                payload.expected_revision, payload.resolution, payload.reason,
            )
        )

    @blueprint.get("/comment-campaigns/<campaign_id>/receipts")
    def receipts(campaign_id: str):
        return _data(_call(service(), "list_receipts", campaign_id))

    @blueprint.get("/comment-campaigns/<campaign_id>/attempts")
    def attempts(campaign_id: str):
        return _data(_call(service(), "list_attempts", campaign_id))

    @blueprint.get("/comment-campaign-health")
    def health():
        return _data(_call(service(), "health"))

    @blueprint.get("/comment-settings")
    def comment_settings():
        return _data(_call(service(), "get_comment_settings"))

    @blueprint.put("/comment-settings")
    def update_comment_settings():
        payload = _parse(CommentSettingsUpdate)
        return _data(_call(service(), "update_comment_settings", payload.model_dump()))

    @blueprint.errorhandler(Exception)
    def handle_error(error: Exception):
        if type(error).__name__ == "AuthError" and type(error).__module__ == "gateway.auth_service":
            return jsonify({"code": error.code}), error.status
        status, code = _error_status(error)
        return _error(code), status

    return blueprint


def _parse(model: type[BaseModel]) -> BaseModel:
    if not request.is_json:
        raise CampaignApiError("invalid_request")
    try:
        return model.model_validate(request.get_json(silent=True))
    except ValidationError as exc:
        raise CampaignApiError("validation_failed", 422) from exc


def _call(service: object, method: str, *args: object, **kwargs: object) -> Any:
    function = getattr(service, method, None)
    if not callable(function):
        raise CampaignApiError("runtime_unavailable", 503)
    return function(*args, **kwargs)


def _required(value: Any) -> Any:
    if value is None:
        raise CampaignApiError("not_found", 404)
    return value


def _validate_campaign_query() -> None:
    if set(request.args) - {"status", "limit", "offset"}:
        raise CampaignApiError("invalid_request")
    status = request.args.get("status")
    if status is not None and status not in _ALLOWED_STATUS:
        raise CampaignApiError("invalid_request")


def _query_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = request.args.get(name)
    if value is None:
        return default
    if not value.isdecimal():
        raise CampaignApiError("invalid_request")
    parsed = int(value)
    if not minimum <= parsed <= maximum:
        raise CampaignApiError("invalid_request")
    return parsed


def _data(value: Any, status: int = 200):
    return jsonify({"data": _redact(value)}), status


def _error(code: str):
    safe_code = code if code in _MESSAGES else "internal_error"
    return jsonify({"error": {"code": safe_code, "message": _MESSAGES[safe_code]}})


def _error_status(error: Exception) -> tuple[int, str]:
    if isinstance(error, CampaignApiError):
        return error.status, error.code
    if isinstance(error, CampaignNotFoundError):
        return 404, "not_found"
    if isinstance(error, RevisionConflictError):
        return 409, "revision_conflict"
    if isinstance(error, StateTransitionError):
        return 409, "invalid_state_transition"
    if isinstance(error, CampaignError):
        if error.code in _UNAVAILABLE_CODES:
            return 503, error.code
        if error.code in {"revision_conflict", "approval_revision_mismatch"}:
            return 409, error.code
        return 422, error.code
    if isinstance(error, (ValueError, TypeError, ValidationError)):
        return 422, "validation_failed"
    return 500, "internal_error"


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            if _forbidden_key(text_key):
                continue
            cleaned[text_key] = _redact(item)
        return cleaned
    if isinstance(value, (list, tuple, set)):
        return [_redact(item) for item in value]
    if isinstance(value, str) and any(
        marker in value.casefold() for marker in ("ws://", "wss://")
    ):
        return "[redacted]"
    return value


def _forbidden_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in _ALLOWED_PROFILE_KEYS:
        return False
    return (
        normalized in {
            "profile_id", "profile_ids", "raw_profile_id", "raw_profile_ids",
            "raw_adspower_id", "raw_adspower_ids", "raw_id", "raw_ids",
            "ws_url", "websocket", "password", "secret", "token", "access_token",
        }
        or normalized.endswith("_profile_id")
        or normalized.endswith("_profile_ids")
        or "cookie" in normalized
        or "authorization" in normalized
        or "api_key" in normalized
    )


__all__ = ["CampaignApiError", "create_comment_campaign_blueprint"]
