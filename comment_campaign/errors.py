"""Fixed errors for the Comment Campaign boundary."""

from __future__ import annotations

from typing import Any


ERROR_CODES = frozenset(
    {
        "adspower_unavailable",
        "profile_start_failed",
        "cdp_connect_failed",
        "profile_identity_mismatch",
        "tiktok_identity_unavailable",
        "tiktok_identity_changed",
        "tiktok_login_required",
        "duplicate_tiktok_account",
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
        "template_unavailable",
        "content_library_unavailable",
        "approval_revision_mismatch",
        "revision_conflict",
        "invalid_state_transition",
        "unsupported_import_type",
        "import_file_invalid",
        "import_file_too_large",
        "import_tree_failed",
    }
)


class CampaignError(Exception):
    """A stable, non-sensitive error suitable for persistence and API mapping."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code if code in ERROR_CODES else "invalid_state_transition"
        super().__init__(message or self.code)


class CampaignValidationError(CampaignError):
    pass


ALLOCATION_REASONS = frozenset({
    "unknown_profile_ref",
    "insufficient_profiles",
    "profile_disabled",
    "profile_unhealthy",
    "profile_in_cooldown",
    "profile_tag_mismatch",
    "profile_language_mismatch",
    "complete_matching_not_found",
})


def allocation_details(
    reason: str | None, *, required_count: int | None = None,
    eligible_count: int | None = None, display_profiles: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the only public details shape for allocation failures."""
    details: dict[str, Any] = {}
    if reason is not None:
        details["reason"] = (
            reason if reason in ALLOCATION_REASONS else "complete_matching_not_found"
        )
    if type(required_count) is int and required_count >= 0:
        details["required_count"] = required_count
    if type(eligible_count) is int and eligible_count >= 0:
        details["eligible_count"] = eligible_count
    if display_profiles and all(isinstance(value, str) for value in display_profiles):
        details["display_profiles"] = list(display_profiles[:2])
    return details


class AllocationError(CampaignError):
    """A planning failure whose API details are deliberately non-sensitive."""

    def __init__(
        self,
        reason: str | None = None,
        *,
        required_count: int | None = None,
        eligible_count: int | None = None,
        display_profiles: tuple[str, ...] = (),
    ) -> None:
        super().__init__("allocation_unsatisfied")
        self.details = allocation_details(
            reason, required_count=required_count, eligible_count=eligible_count,
            display_profiles=display_profiles,
        )


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


class DuplicateTikTokAccountError(Exception):
    """Internal-only duplicate identity signal; never expose its raw account key."""

    def __init__(self, account_key: str, visible_username: str, assignment_ids: tuple[str, ...]) -> None:
        self.account_key = account_key
        self.visible_username = visible_username
        self.assignment_ids = assignment_ids
        super().__init__("duplicate_tiktok_account")
