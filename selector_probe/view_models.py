"""Strict public projections for selector element management."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import re


_PROFILE_MASK = re.compile(r"^\*\*\*(?:.{4})?$", re.DOTALL)
_SENSITIVE_PARTS = (
    "authorization",
    "cookie",
    "dom",
    "html",
    "model",
    "prompt",
    "raw",
    "secret",
    "token",
    "tree",
)
_ROUND_FIELDS = (
    "round_number",
    "result",
    "failure_code",
    "page_state",
    "started_at",
    "finished_at",
)
_DEPENDENCY_FIELDS = (
    "strategy_id",
    "strategy_name",
    "action_id",
    "action_type",
)
_REQUEST_RESULT_FIELDS = (
    "status",
    "failure_code",
    "version",
    "new_version",
    "published",
    "reconciled",
)
_REQUEST_ROUND_FIELDS = (
    "profile_mask",
    "round_number",
    "result",
    "status",
    "failure_code",
    "page_state",
    "match_count",
    "role_name_result",
    "visible",
    "in_viewport",
    "actionable",
    "postcondition_result",
    "started_at",
    "finished_at",
)
_REQUEST_REPAIR_FIELDS = (
    "attempt",
    "previous_method",
    "failure_code",
    "match_count",
    "new_method",
    "prompt_version",
    "model_id",
    "result",
)


@dataclass(frozen=True)
class ElementRecord:
    id: str
    display_name: str
    management_source: str
    published_status: str
    draft_status: str | None
    scope: str
    primary_locator_type: str
    dependency_count: int
    last_validated_at: str | None
    revision: int
    migration_available: bool = False

    @property
    def runtime_status(self) -> str:
        return self.draft_status or self.published_status


def public_element_summary(record: ElementRecord) -> dict[str, object]:
    if not isinstance(record, ElementRecord):
        raise TypeError("record must be an ElementRecord")
    return {
        "id": record.id,
        "display_name": record.display_name,
        "management_source": record.management_source,
        "published_status": record.published_status,
        "draft_status": record.draft_status,
        "runtime_status": record.runtime_status,
        "scope": record.scope,
        "primary_locator_type": record.primary_locator_type,
        "dependency_count": record.dependency_count,
        "last_validated_at": record.last_validated_at,
        "revision": record.revision,
        "migration_available": record.migration_available,
    }


def public_element_detail(
    record: ElementRecord,
    evidence: object,
    dependencies: object,
    *,
    candidate_comparison: object = None,
    repairs: object = None,
    history: object = None,
) -> dict[str, object]:
    payload = public_element_summary(record)
    payload["evidence"] = _public_evidence(evidence)
    payload["dependencies"] = _public_dependencies(dependencies)
    comparison = _public_candidate_comparison(candidate_comparison)
    payload["candidate_comparison"] = comparison
    payload["deterministic_candidates"] = comparison["deterministic"]
    payload["repaired_candidates"] = comparison["repaired"]
    payload["repairs"] = _public_request_records(
        repairs,
        _REQUEST_REPAIR_FIELDS,
        maximum=3,
    )
    payload["history"] = _public_version_history(history)
    return payload


def public_error(
    code: str,
    *,
    message: str,
    details: object = None,
) -> dict[str, object]:
    error = {
        "code": _bounded_text(code, "code", maximum=64),
        "message": _bounded_text(message, "message", maximum=240),
    }
    sanitized = _public_details(details, depth=0)
    if sanitized not in (None, {}, []):
        error["details"] = sanitized
    return {"error": error}


def public_element_request(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("element request must be a mapping")
    result: dict[str, object] = {}
    for field in ("request_id", "request_type", "element_id", "status"):
        selected = value.get(field)
        if isinstance(selected, str):
            result[field] = selected[:128]
    attempt_count = value.get("attempt_count")
    result["attempt_count"] = (
        attempt_count
        if isinstance(attempt_count, int)
        and not isinstance(attempt_count, bool)
        and attempt_count >= 0
        else 0
    )
    error_code = value.get("error_code")
    result["error_code"] = (
        error_code[:128] if isinstance(error_code, str) else ""
    )
    result["result"] = _public_element_request_result(value.get("result"))
    return result


def _public_element_request_result(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for field in _REQUEST_RESULT_FIELDS:
        selected = value.get(field)
        if isinstance(selected, bool):
            result[field] = selected
        elif isinstance(selected, str):
            result[field] = selected[:128]
    candidate = value.get("candidate")
    if isinstance(candidate, Mapping):
        scope = candidate.get("scope")
        locators = candidate.get("locators")
        if isinstance(scope, str) and isinstance(locators, Sequence):
            result["candidate"] = {
                "scope": scope[:64],
                "locators": [
                    locator
                    for raw in locators[:20]
                    if (locator := _public_request_locator(raw))
                ],
            }
    result["rounds"] = _public_request_records(
        value.get("rounds"),
        _REQUEST_ROUND_FIELDS,
        maximum=20,
    )
    result["repairs"] = _public_request_records(
        value.get("repairs"),
        _REQUEST_REPAIR_FIELDS,
        maximum=3,
    )
    return result


def _public_request_locator(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for field in (
        "id",
        "type",
        "name",
        "value",
        "role",
        "name_mode",
        "enabled",
        "fallback",
    ):
        selected = value.get(field)
        if isinstance(selected, bool):
            result[field] = selected
        elif isinstance(selected, str):
            result[field] = selected[:500]
    descendant = _public_request_locator(value.get("descendant"))
    if descendant:
        result["descendant"] = descendant
    return result


def _public_candidate_comparison(value: object) -> dict[str, object]:
    selected = value if isinstance(value, Mapping) else {}
    result: dict[str, object] = {}
    for kind in ("active", "deterministic", "repaired"):
        raw_locators = selected.get(kind)
        if not isinstance(raw_locators, Sequence) or isinstance(
            raw_locators,
            (str, bytes, bytearray),
        ):
            result[kind] = []
            continue
        result[kind] = [
            locator
            for raw in raw_locators[:20]
            if (locator := _public_request_locator(raw))
        ]
    return result


def _public_version_history(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    result: list[dict[str, object]] = []
    fields = (
        "version_id",
        "status",
        "base_version_id",
        "bundle_hash",
        "created_at",
        "validated_at",
        "published_at",
    )
    for raw in value[:100]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, object] = {}
        for field in fields:
            selected = raw.get(field)
            if isinstance(selected, str):
                item[field] = selected[:256]
            elif selected is None and field in {
                "validated_at",
                "published_at",
            }:
                item[field] = None
        if item.get("version_id"):
            result.append(item)
    return result


def _public_request_records(
    value: object,
    fields: Sequence[str],
    *,
    maximum: int,
) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return []
    result: list[dict[str, object]] = []
    for raw in value[:maximum]:
        if not isinstance(raw, Mapping):
            continue
        item: dict[str, object] = {}
        for field in fields:
            selected = raw.get(field)
            if isinstance(selected, bool):
                item[field] = selected
            elif isinstance(selected, int):
                item[field] = selected
            elif isinstance(selected, str):
                item[field] = selected[:128]
        result.append(item)
    return result


def _public_evidence(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    profile_mask = value.get("profile_mask")
    if isinstance(profile_mask, str) and _PROFILE_MASK.fullmatch(profile_mask):
        result["profile_mask"] = profile_mask
    for field in ("status", "last_validated_at"):
        selected = value.get(field)
        if isinstance(selected, str):
            result[field] = selected[:128]
    raw_rounds = value.get("rounds")
    rounds: list[dict[str, object]] = []
    if isinstance(raw_rounds, Sequence) and not isinstance(
        raw_rounds,
        (str, bytes, bytearray),
    ):
        for raw_round in raw_rounds[:20]:
            if not isinstance(raw_round, Mapping):
                continue
            item: dict[str, object] = {}
            for field in _ROUND_FIELDS:
                selected = raw_round.get(field)
                if isinstance(selected, bool):
                    continue
                if isinstance(selected, int):
                    item[field] = selected
                elif isinstance(selected, str):
                    item[field] = selected[:128]
            rounds.append(item)
    result["rounds"] = rounds
    return result


def _public_dependencies(value: object) -> list[dict[str, str]]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    result: list[dict[str, str]] = []
    for raw_item in value[:100]:
        if not isinstance(raw_item, Mapping):
            continue
        item: dict[str, str] = {}
        for field in _DEPENDENCY_FIELDS:
            selected = raw_item.get(field)
            if isinstance(selected, str):
                item[field] = selected[:128]
        result.append(item)
    return result


def _public_details(value: object, *, depth: int) -> object:
    if depth > 4:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:500]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in list(value.items())[:50]:
            if not isinstance(key, str) or _sensitive_key(key):
                continue
            result[key[:128]] = _public_details(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [
            _public_details(item, depth=depth + 1)
            for item in value[:50]
        ]
    return None


def _sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return any(part in normalized for part in _SENSITIVE_PARTS)


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
    ):
        raise ValueError(f"{name} is invalid")
    return value


__all__ = [
    "ElementRecord",
    "public_element_detail",
    "public_element_request",
    "public_element_summary",
    "public_error",
]
