"""Fixed errors for the Comment Campaign boundary."""

from __future__ import annotations


ERROR_CODES = frozenset(
    {
        "adspower_unavailable",
        "profile_start_failed",
        "cdp_connect_failed",
        "profile_identity_mismatch",
        "target_video_invalid",
        "target_video_mismatch",
        "comment_panel_not_ready",
        "comment_input_not_found",
        "parent_comment_not_found",
        "parent_comment_ambiguous",
        "comment_author_mismatch",
        "reply_target_mismatch",
        "comment_submit_uncertain",
        "comment_receipt_unverified",
        "profile_close_failed",
        "redis_unavailable",
        "worker_unavailable",
        "allocation_unsatisfied",
        "template_invalid",
        "content_library_unavailable",
        "approval_revision_mismatch",
        "revision_conflict",
        "invalid_state_transition",
    }
)


class CampaignError(Exception):
    """A stable, non-sensitive error suitable for persistence and API mapping."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code if code in ERROR_CODES else "invalid_state_transition"
        super().__init__(message or self.code)


class CampaignValidationError(CampaignError):
    pass


class StateTransitionError(CampaignError):
    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__("invalid_state_transition", f"{current} cannot transition to {target}")


class RevisionConflictError(CampaignError):
    def __init__(self, identifier: str) -> None:
        super().__init__("revision_conflict", f"revision conflict: {identifier}")


class CampaignNotFoundError(CampaignError):
    def __init__(self, identifier: str) -> None:
        super().__init__("invalid_state_transition", f"not found: {identifier}")
