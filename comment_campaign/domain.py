"""Small immutable values and the campaign state machines."""

from __future__ import annotations

from enum import StrEnum

from .errors import StateTransitionError


class CampaignMode(StrEnum):
    INDEPENDENT = "independent"
    THREADED = "threaded"


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    PLANNED = "planned"
    AWAITING_CAMPAIGN_APPROVAL = "awaiting_campaign_approval"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class AssignmentStatus(StrEnum):
    PLANNED = "planned"
    WAITING_DEPENDENCY = "waiting_dependency"
    OPENING_PROFILE = "opening_profile"
    LOCATING_VIDEO = "locating_video"
    LOCATING_PARENT = "locating_parent"
    PREPARING_COMMENT = "preparing_comment"
    AWAITING_STEP_APPROVAL = "awaiting_step_approval"
    SUBMITTING = "submitting"
    VERIFYING_RECEIPT = "verifying_receipt"
    PUBLISHED_VERIFIED = "published_verified"
    PUBLISHED_UNVERIFIED = "published_unverified"
    PAUSED = "paused"
    PAUSED_DEPENDENCY = "paused_dependency"
    FAILED = "failed"
    CANCELLED = "cancelled"


CAMPAIGN_TRANSITIONS = {
    "draft": {"planned", "cancelled"},
    "planned": {"awaiting_campaign_approval", "draft", "cancelled"},
    "awaiting_campaign_approval": {"queued", "draft", "cancelled"},
    "queued": {"running", "paused", "cancelled"},
    "running": {"paused", "failed", "completed", "cancelled"},
    "paused": {"queued", "cancelled"},
    "failed": set(),
    "completed": set(),
    "cancelled": set(),
}

ASSIGNMENT_TRANSITIONS = {
    "planned": {"waiting_dependency", "opening_profile", "paused_dependency", "cancelled"},
    "waiting_dependency": {"opening_profile", "paused_dependency", "cancelled"},
    "opening_profile": {"locating_video", "failed", "paused", "paused_dependency"},
    "locating_video": {"locating_parent", "preparing_comment", "failed", "paused", "paused_dependency"},
    "locating_parent": {"preparing_comment", "failed", "paused_dependency"},
    "preparing_comment": {"awaiting_step_approval", "failed", "paused", "paused_dependency"},
    "awaiting_step_approval": {"submitting", "paused", "paused_dependency", "cancelled"},
    "submitting": {"verifying_receipt", "published_unverified"},
    "verifying_receipt": {"published_verified", "published_unverified"},
    "published_unverified": {"published_verified", "paused"},
    "published_verified": set(),
    "paused": {"opening_profile", "waiting_dependency", "cancelled"},
    "paused_dependency": {"waiting_dependency", "cancelled"},
    "failed": set(),
    "cancelled": set(),
}


def _transition(current: CampaignStatus | AssignmentStatus | str, target: CampaignStatus | AssignmentStatus | str, transitions: dict[str, set[str]]) -> str:
    current_value = str(current.value if isinstance(current, StrEnum) else current)
    target_value = str(target.value if isinstance(target, StrEnum) else target)
    if target_value not in transitions.get(current_value, set()):
        raise StateTransitionError(current_value, target_value)
    return target_value


def transition_campaign(current: CampaignStatus | str, target: CampaignStatus | str) -> str:
    return _transition(current, target, CAMPAIGN_TRANSITIONS)


def transition_assignment(current: AssignmentStatus | str, target: AssignmentStatus | str) -> str:
    return _transition(current, target, ASSIGNMENT_TRANSITIONS)
