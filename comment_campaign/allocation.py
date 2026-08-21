"""Validation and bounded deterministic matching for campaign plans."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5
from unicodedata import normalize
import re

from .domain import CampaignMode
from .errors import AllocationError, CampaignValidationError


MAX_STEPS = 100
MAX_PROFILES = 300


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    assignment_id: str
    step_id: str
    profile_ref: str
    display_profile: str
    expected_username: str
    role: str
    resolved_text: str
    parent_assignment_id: str | None
    position: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _mode(value: CampaignMode | str) -> CampaignMode:
    try:
        return CampaignMode(value)
    except ValueError as exc:
        raise CampaignValidationError("template_invalid") from exc


def validate_template_tree(mode: CampaignMode | str, steps: Sequence[Any], *, supported_modes: Sequence[str] | None = None) -> None:
    """Validate the complete parent graph before any profile or text is chosen."""
    campaign_mode = _mode(mode)
    if not steps or len(steps) > MAX_STEPS:
        raise CampaignValidationError("template_invalid")
    if supported_modes is not None and campaign_mode.value not in set(supported_modes):
        raise CampaignValidationError("template_invalid")
    identifiers = [str(_field(step, "id", "")) for step in steps]
    if any(not identifier for identifier in identifiers) or len(identifiers) != len(set(identifiers)):
        raise CampaignValidationError("template_invalid")
    parents = {identifier: _field(step, "parent_step_id") or None for identifier, step in zip(identifiers, steps)}
    for identifier, parent in parents.items():
        if parent == identifier or (parent is not None and parent not in parents):
            raise CampaignValidationError("template_invalid")
        if campaign_mode is CampaignMode.INDEPENDENT and parent is not None:
            raise CampaignValidationError("template_invalid")
    if campaign_mode is CampaignMode.INDEPENDENT:
        return
    roots = [identifier for identifier, parent in parents.items() if parent is None]
    if len(roots) != 1:
        raise CampaignValidationError("template_invalid")
    colors: dict[str, int] = {}

    def visit(identifier: str) -> None:
        color = colors.get(identifier, 0)
        if color == 1:
            raise CampaignValidationError("template_invalid")
        if color == 2:
            return
        colors[identifier] = 1
        parent = parents[identifier]
        if parent is not None:
            visit(parent)
        colors[identifier] = 2

    for identifier in identifiers:
        visit(identifier)


def role_for(mode: CampaignMode | str, parent_step_id: str | None) -> str:
    campaign_mode = _mode(mode)
    if campaign_mode is CampaignMode.INDEPENDENT:
        return "commenter"
    return "owner" if parent_step_id is None else "participant"


def profile_matches(step: Any, profile: Mapping[str, Any], *, eligibility_at: datetime | None = None) -> bool:
    if profile.get("enabled") is not True:
        return False
    if profile.get("health_status") != "healthy":
        return False
    cooldown_until = profile.get("cooldown_until")
    if cooldown_until:
        try:
            cooldown = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
        except ValueError:
            return False
        if cooldown.tzinfo is None or cooldown > (eligibility_at or datetime.now(timezone.utc)):
            return False
    tags = set(profile.get("tags") or [])
    required = set(_field(step, "required_profile_tags", []) or [])
    excluded = set(_field(step, "excluded_profile_tags", []) or [])
    language = str(_field(step, "language", "") or "")
    return required <= tags and not (excluded & tags) and (not language or profile.get("language") == language)


def _cooldown_active(profile: Mapping[str, Any], eligibility_at: datetime) -> bool:
    cooldown_until = profile.get("cooldown_until")
    if not cooldown_until:
        return False
    try:
        cooldown = datetime.fromisoformat(str(cooldown_until).replace("Z", "+00:00"))
    except ValueError:
        return True
    return cooldown.tzinfo is None or cooldown > eligibility_at


def _allocation_error(
    steps: Sequence[Any], profiles: Sequence[Mapping[str, Any]], *, eligibility_at: datetime,
) -> AllocationError:
    required_count = len(steps)
    eligible_count = sum(
        any(profile_matches(step, profile, eligibility_at=eligibility_at) for step in steps)
        for profile in profiles
    )
    if len(profiles) < required_count:
        reason = "insufficient_profiles"
    elif any(profile.get("enabled") is not True for profile in profiles):
        reason = "profile_disabled"
    elif any(profile.get("health_status") != "healthy" for profile in profiles):
        reason = "profile_unhealthy"
    elif any(_cooldown_active(profile, eligibility_at) for profile in profiles):
        reason = "profile_in_cooldown"
    else:
        usable = [
            profile for profile in profiles
            if profile.get("enabled") is True
            and profile.get("health_status") == "healthy"
            and not _cooldown_active(profile, eligibility_at)
        ]

        def tags_match(step: Any, profile: Mapping[str, Any]) -> bool:
            tags = set(profile.get("tags") or [])
            required = set(_field(step, "required_profile_tags", []) or [])
            excluded = set(_field(step, "excluded_profile_tags", []) or [])
            return required <= tags and not (excluded & tags)

        if any(
            not any(tags_match(step, profile) for profile in usable)
            for step in steps
        ):
            reason = "profile_tag_mismatch"
        elif any(
            str(_field(step, "language", "") or "")
            and not any(
                tags_match(step, profile)
                and profile.get("language") == _field(step, "language", "")
                for profile in usable
            )
            for step in steps
        ):
            reason = "profile_language_mismatch"
        else:
            reason = "complete_matching_not_found"
    return AllocationError(
        reason, required_count=required_count, eligible_count=eligible_count,
    )


def _validate_match_graph(
    steps: Sequence[Any], profiles: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Reject malformed or oversized graph inputs without inventing a reason."""
    step_ids = [str(_field(step, "id") or "") for step in steps]
    if not step_ids or "" in step_ids or len(step_ids) != len(set(step_ids)):
        raise AllocationError()
    profile_refs = [
        str(profile.get("profile_ref") or "")
        if isinstance(profile, Mapping) else ""
        for profile in profiles
    ]
    if not profile_refs or "" in profile_refs or len(profile_refs) != len(set(profile_refs)):
        raise AllocationError()
    if len(step_ids) > MAX_STEPS or len(profile_refs) > MAX_PROFILES:
        raise AllocationError()
    return {profile_ref: profile for profile_ref, profile in zip(profile_refs, profiles)}


def match_profiles(
    steps: Sequence[Any], profiles: Sequence[Mapping[str, Any]], *,
    eligibility_at: datetime, order_key: Any,
) -> dict[str, str]:
    """Return a full bounded Kuhn matching from one immutable eligibility view."""
    profile_by_ref = _validate_match_graph(steps, profiles)
    candidates = {
        str(_field(step, "id")): sorted(
            (ref for ref, profile in profile_by_ref.items()
             if profile_matches(step, profile, eligibility_at=eligibility_at)),
            key=lambda ref, step_id=str(_field(step, "id")): order_key(step_id, ref),
        )
        for step in steps
    }
    if any(not values for values in candidates.values()):
        raise _allocation_error(steps, profiles, eligibility_at=eligibility_at)
    ordered = sorted(steps, key=lambda step: (len(candidates[str(_field(step, "id"))]), str(_field(step, "id"))))
    matched_profile: dict[str, str] = {}

    def assign(step_id: str, seen: set[str]) -> bool:
        for profile_ref in candidates[step_id]:
            if profile_ref in seen:
                continue
            seen.add(profile_ref)
            existing = matched_profile.get(profile_ref)
            if existing is None or assign(existing, seen):
                matched_profile[profile_ref] = step_id
                return True
        return False

    for step in ordered:
        if not assign(str(_field(step, "id")), set()):
            raise _allocation_error(steps, profiles, eligibility_at=eligibility_at)
    return {step_id: profile_ref for profile_ref, step_id in matched_profile.items()}


def recommend_profiles(
    steps: Sequence[Any], profiles: Sequence[Mapping[str, Any]], *, eligibility_at: datetime,
) -> list[str]:
    """Choose exactly one safe Profile ref per step in stable display order."""
    by_ref = {str(profile.get("profile_ref") or ""): profile for profile in profiles}
    chosen = match_profiles(
        steps, profiles, eligibility_at=eligibility_at,
        order_key=lambda _step_id, ref: (
            str(by_ref[ref].get("display_profile") or "").casefold(), ref,
        ),
    )
    return sorted(
        chosen.values(),
        key=lambda ref: (str(by_ref[ref].get("display_profile") or "").casefold(), ref),
    )


def _candidate_key(seed: str, step_id: str, profile_ref: str) -> str:
    return sha256(f"{seed}\0{step_id}\0{profile_ref}".encode()).hexdigest()


def _text_for(texts: Mapping[str, Any], step_id: str) -> tuple[str, str]:
    value = texts.get(step_id)
    if isinstance(value, Mapping):
        item_id = str(value.get("content_item_id") or "")
        text = str(value.get("text") or "")
    else:
        item_id, text = "", str(value or "")
    if not text:
        raise AllocationError()
    return item_id, text


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", " ", normalize("NFKC", value).strip())


def allocate(steps: Sequence[Any], profiles: Sequence[Mapping[str, Any]], texts: Mapping[str, Any], seed: str, *, mode: CampaignMode | str, campaign_id: str = "", eligibility_at: datetime | None = None) -> list[PlannedAssignment]:
    """Find a complete deterministic one-profile-per-step matching.

    This is Kuhn's augmenting-path algorithm, not exponential backtracking.  The
    schema bounds the graph to 100 steps and 300 profiles.
    """
    campaign_mode = _mode(mode)
    _validate_match_graph(steps, profiles)
    validate_template_tree(campaign_mode, steps)
    eligibility_at = eligibility_at or datetime.now(timezone.utc)
    chosen = match_profiles(
        steps, profiles, eligibility_at=eligibility_at,
        order_key=lambda step_id, ref: _candidate_key(str(seed), step_id, ref),
    )
    profile_by_ref = _validate_match_graph(steps, profiles)
    resolved = {str(_field(step, "id")): _text_for(texts, str(_field(step, "id"))) for step in steps}
    normalized_texts = [_normalized_text(text) for _item_id, text in resolved.values()]
    if any(not text for text in normalized_texts) or len(normalized_texts) != len(set(normalized_texts)):
        raise AllocationError()
    by_id = {str(_field(step, "id")): step for step in steps}
    original_index = {str(_field(step, "id")): index for index, step in enumerate(steps)}
    positions: dict[str, int] = {}
    pending = set(by_id)
    while pending:
        ready = sorted(
            (identifier for identifier in pending if _field(by_id[identifier], "parent_step_id") not in pending),
            key=original_index.__getitem__,
        )
        if not ready:
            raise AllocationError()
        for identifier in ready:
            positions[identifier] = len(positions) + 1
            pending.remove(identifier)
    assignment_ids = {
        identifier: str(uuid5(NAMESPACE_URL, f"comment-campaign:{campaign_id}:{identifier}"))
        for identifier in by_id
    }
    return [
        PlannedAssignment(
            assignment_id=assignment_ids[identifier], step_id=identifier,
            profile_ref=chosen[identifier], display_profile=str(profile_by_ref[chosen[identifier]].get("display_profile") or ""),
            expected_username="",
            role=role_for(campaign_mode, _field(by_id[identifier], "parent_step_id")),
            resolved_text=resolved[identifier][1],
            parent_assignment_id=assignment_ids.get(_field(by_id[identifier], "parent_step_id")),
            position=positions[identifier],
        )
        for identifier in sorted(by_id, key=lambda value: positions[value])
    ]
