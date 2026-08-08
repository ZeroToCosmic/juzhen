"""Strict public projections for selector element management."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
import re

from selector_probe.inventory import normalize_recorded_step


_PROFILE_MASK = re.compile(r"^\*\*\*(?:.{4})?$", re.DOTALL)
_SENSITIVE_PARTS = (
    "authorization",
    "cookie",
    "dom",
    "html",
    "model",
    "prompt",
    "profile",
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
@dataclass(frozen=True)
class ElementRecord:
    id: str
    display_name: str
    status: str
    page_key: str
    primary_locator_type: str
    dependency_count: int
    last_validated_at: str | None
    revision: int


def public_element_summary(record: ElementRecord) -> dict[str, object]:
    if not isinstance(record, ElementRecord):
        raise TypeError("record must be an ElementRecord")
    return {
        "id": record.id,
        "display_name": record.display_name,
        "status": record.status,
        "page_key": record.page_key,
        "primary_locator_type": record.primary_locator_type,
        "dependency_count": record.dependency_count,
        "last_validated_at": record.last_validated_at,
        "revision": record.revision,
    }


def public_element_detail(
    record: ElementRecord,
    definition: object,
    dependencies: object,
    *,
    validation: object = None,
    history: object = None,
    alerts: object = None,
    strategy_controls: object = None,
) -> dict[str, object]:
    payload = public_element_summary(record)
    payload["definition"] = _public_manual_definition(definition)
    payload["dependencies"] = _public_dependencies(dependencies)
    payload["validation"] = _public_details(validation, depth=0) or {}
    payload["history"] = _public_version_history(history)
    payload["alerts"] = _public_sequence_details(alerts, maximum=100)
    controls = _public_details(strategy_controls, depth=0)
    payload["strategy_controls"] = (
        controls if isinstance(controls, Mapping) else {}
    )
    return payload


def _public_manual_definition(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        return None
    result: dict[str, object] = {}
    for field, maximum in (
        ("page_key", 120),
        ("target_origin", 255),
        ("url_pattern", 2000),
    ):
        selected = value.get(field)
        if isinstance(selected, str):
            result[field] = selected[:maximum]
    steps: list[dict[str, object]] = []
    raw_steps = value.get("operation_steps")
    if isinstance(raw_steps, Sequence) and not isinstance(
        raw_steps, (str, bytes, bytearray)
    ):
        for raw in raw_steps[:20]:
            try:
                steps.append(normalize_recorded_step(raw))
            except ValueError:
                continue
    result["operation_steps"] = steps
    result["fingerprint"] = _public_fingerprint(value.get("fingerprint"))
    locators: list[dict[str, str]] = []
    raw_locators = value.get("locators")
    if isinstance(raw_locators, Sequence) and not isinstance(
        raw_locators, (str, bytes, bytearray)
    ):
        for raw in raw_locators[:6]:
            if not isinstance(raw, Mapping):
                continue
            try:
                selected = normalize_recorded_step(
                    {"sequence": 1, "locator": raw}
                )["locator"]
            except ValueError:
                continue
            locators.append(
                {
                    "type": str(selected["type"]),
                    "value": str(selected["value"]),
                }
            )
    result["locators"] = locators
    return result


def _public_fingerprint(value: object) -> dict[str, object]:
    """Project display metadata without accepting arbitrary nested data."""

    if not isinstance(value, Mapping):
        return {}
    result: dict[str, object] = {}
    for field, maximum in (
        ("tag", 24),
        ("input_type", 32),
        ("role", 48),
        ("name", 160),
        ("frame_key", 120),
        ("shadow_key", 160),
    ):
        selected = value.get(field)
        if isinstance(selected, str):
            result[field] = selected.replace("\x00", "")[:maximum]
    if isinstance(value.get("shadow"), bool):
        result["shadow"] = value["shadow"]
    raw_attributes = value.get("attributes")
    attributes: dict[str, str] = {}
    if isinstance(raw_attributes, Mapping):
        for key in (
            "data-e2e",
            "data-testid",
            "id",
            "name",
            "placeholder",
            "aria-label",
            "contenteditable",
            "type",
            "tabindex",
        ):
            selected = raw_attributes.get(key)
            if isinstance(selected, str):
                attributes[key] = selected.replace("\x00", "")[:160]
    if attributes:
        result["attributes"] = attributes
    for field in ("region", "position_hint"):
        selected = _public_normalized_region(value.get(field))
        if selected:
            result[field] = selected
    return result


def _public_normalized_region(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for field in ("x", "y", "width", "height"):
        selected = value.get(field)
        if (
            isinstance(selected, (int, float))
            and not isinstance(selected, bool)
            and math.isfinite(float(selected))
            and 0 <= float(selected) <= 1
        ):
            result[field] = float(selected)
    return result if len(result) == 4 else {}


def _public_sequence_details(
    value: object,
    *,
    maximum: int,
) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(
        value, (str, bytes, bytearray)
    ):
        return []
    return [
        selected
        for item in value[:maximum]
        if (selected := _public_details(item, depth=0)) is not None
    ]


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
    "public_element_summary",
    "public_error",
]
