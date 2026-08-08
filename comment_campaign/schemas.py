"""Strict Pydantic 2 request shapes for the Comment Campaign API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


class CommentStepInput(_StrictInput):
    id: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=100)
    content_source: Literal["fixed", "library"]
    fixed_text: str = Field(default="", max_length=2200)
    content_library_id: str = Field(default="", max_length=120)
    content_item_id: str = Field(default="", max_length=120)
    parent_step_id: str | None = Field(default=None, max_length=120)
    required_profile_tags: list[str] = Field(default_factory=list, max_length=20)
    excluded_profile_tags: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="", max_length=32)

    @field_validator("parent_step_id")
    @classmethod
    def normalize_blank_parent(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("required_profile_tags", "excluded_profile_tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        _validate_string_list(values, "profile tags")
        return values

    @model_validator(mode="after")
    def validate_content_source(self) -> "CommentStepInput":
        if self.content_source == "fixed":
            if not self.fixed_text or self.content_library_id or self.content_item_id:
                raise ValueError("fixed steps require fixed_text only")
        elif not self.content_library_id or self.fixed_text:
            raise ValueError("library steps require content_library_id and cannot include fixed_text")
        return self


class TemplateCreate(_StrictInput):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    supported_modes: list[Literal["independent", "threaded"]] = Field(min_length=1, max_length=2)
    language: str = Field(default="", max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=20)
    steps: list[CommentStepInput] = Field(min_length=1, max_length=100)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        _validate_string_list(values, "tags")
        return values

    @model_validator(mode="after")
    def validate_step_ids(self) -> "TemplateCreate":
        identifiers = [step.id for step in self.steps]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("step ids must be unique")
        if len(self.supported_modes) != len(set(self.supported_modes)):
            raise ValueError("supported_modes must be unique")
        return self


class TemplateUpdate(TemplateCreate):
    expected_revision: int = Field(ge=1)


class CampaignCreate(_StrictInput):
    name: str = Field(min_length=1, max_length=100)
    mode: Literal["independent", "threaded"]
    target_source: Literal["manual_url", "publish_result"]
    target_reference: str = Field(min_length=1, max_length=2000)
    template_id: str = Field(min_length=1, max_length=120)
    template_revision: int | None = Field(default=None, ge=1)
    profile_refs: list[str] = Field(min_length=1, max_length=300)
    batch_size: int = Field(default=3, ge=1, le=8)
    allocation_seed: str = Field(default="", max_length=256)
    start_mode: Literal["manual", "scheduled"] = "manual"
    scheduled_at: str | None = Field(default=None, max_length=40)

    @field_validator("profile_refs")
    @classmethod
    def validate_profile_refs(cls, values: list[str]) -> list[str]:
        _validate_string_list(values, "profile_refs")
        if any(len(value) > 80 for value in values):
            raise ValueError("profile_refs cannot exceed 80 characters")
        return values

    @model_validator(mode="after")
    def validate_schedule_and_profiles(self) -> "CampaignCreate":
        if len(self.profile_refs) != len(set(self.profile_refs)):
            raise ValueError("profile_refs must be unique")
        if self.start_mode == "scheduled" and self.scheduled_at is None:
            raise ValueError("scheduled campaigns require scheduled_at")
        if self.start_mode == "manual" and self.scheduled_at is not None:
            raise ValueError("manual campaigns cannot include scheduled_at")
        return self

    @field_validator("scheduled_at")
    @classmethod
    def normalize_scheduled_at(cls, value: str | None) -> str | None:
        return _utc_datetime(value, "scheduled_at")


class ProfileMetadataUpsert(_StrictInput):
    profile_ref: str = Field(min_length=1, max_length=80)
    expected_username: str = Field(default="", max_length=120)
    enabled: bool
    login_verified: bool
    tags: list[str] = Field(default_factory=list, max_length=20)
    language: str = Field(default="", max_length=32)
    region: str = Field(default="", max_length=32)
    cooldown_until: str | None = Field(default=None, max_length=40)
    health_status: str = Field(min_length=1, max_length=40)

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, values: list[str]) -> list[str]:
        _validate_string_list(values, "tags")
        return values

    @field_validator("cooldown_until")
    @classmethod
    def normalize_cooldown_until(cls, value: str | None) -> str | None:
        return _utc_datetime(value, "cooldown_until")


class AssignmentOverride(_StrictInput):
    expected_revision: int = Field(ge=1)
    profile_ref: str = Field(min_length=1, max_length=80)


class ExpectedRevision(_StrictInput):
    expected_revision: int = Field(ge=1)


class PlanRequest(_StrictInput):
    expected_revision: int = Field(ge=1)
    allocation_seed: str = Field(default="", max_length=256)


class CampaignPauseRequest(ExpectedRevision):
    reason: str = Field(min_length=1, max_length=500)


class ResolveUnverifiedRequest(ExpectedRevision):
    resolution: Literal["published", "not_published"]
    reason: str = Field(min_length=1, max_length=500)


class RejectSubmitRequest(ExpectedRevision):
    reason: str = Field(min_length=1, max_length=500)


class CommentSettingsUpdate(ExpectedRevision):
    entry_element_id: str = Field(min_length=1, max_length=120)
    input_element_id: str = Field(min_length=1, max_length=120)
    submit_element_id: str = Field(min_length=1, max_length=120)
    account_element_id: str = Field(min_length=1, max_length=120)


def _validate_string_list(values: list[str], label: str) -> None:
    if any(not value for value in values):
        raise ValueError(f"{label} cannot contain blank values")
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must be unique")


def _utc_datetime(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()
