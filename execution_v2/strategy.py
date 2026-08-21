"""Closed-schema validation for isolated browser execution V2 strategies."""

from __future__ import annotations

import math
from typing import Any
from urllib.parse import urlsplit


class StrategyValidationError(ValueError):
    """A strategy definition is not safe to save or execute."""


class StrategyNotFoundError(KeyError):
    """No saved V2 strategy exists for the supplied identifier."""


class StrategyRevisionConflictError(RuntimeError):
    """The caller attempted to overwrite a newer saved strategy revision."""


_DEFINITION_KEYS = {
    "target_url",
    "ready_element_id",
    "readiness_timeout_seconds",
    "run_mode",
    "loop_duration_minutes",
    "actions",
}
_ACTION_TYPES = {"move", "scroll", "click", "input", "wait"}
_ACTION_KEYS = {
    "move": {"id", "type", "element_id", "duration_seconds"},
    "scroll": {"id", "type", "direction", "distance_pixels", "count", "interval_seconds"},
    "click": {
        "id", "type", "element_id", "button", "click_count", "hold_seconds", "after_seconds"
    },
    "input": {
        "id", "type", "element_id", "content_source", "fixed_text", "content_library_id", "interval_ms"
    },
    "wait": {"id", "type", "duration_seconds"},
}


def normalize_strategy_definition(
    value: Any, *, elements_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Return a closed-schema V2 strategy definition validated against saved elements."""

    if not isinstance(value, dict) or set(value) != _DEFINITION_KEYS:
        raise StrategyValidationError("definition keys must match the V2 strategy schema")
    if not isinstance(elements_by_id, dict):
        raise StrategyValidationError("elements_by_id must be an object")

    ready_element_id = _text(value["ready_element_id"], "ready_element_id")
    _validate_element(
        elements_by_id, ready_element_id, purpose="readiness", action_type="ready_element"
    )
    run_mode = value["run_mode"]
    if run_mode not in {"once", "duration"}:
        raise StrategyValidationError("run_mode must be once or duration")
    loop_duration = value["loop_duration_minutes"]
    if run_mode == "once":
        if loop_duration is not None:
            raise StrategyValidationError("loop_duration_minutes must be null for once")
    else:
        loop_duration = _range(
            loop_duration, "loop_duration_minutes", minimum=0.001, maximum=1440
        )

    actions_value = value["actions"]
    if not isinstance(actions_value, list):
        raise StrategyValidationError("actions must be a list")
    action_ids: set[str] = set()
    actions = []
    for action in actions_value:
        normalized = _normalize_action(action, elements_by_id)
        if normalized["id"] in action_ids:
            raise StrategyValidationError("action ids must be unique")
        action_ids.add(normalized["id"])
        actions.append(normalized)

    return {
        "target_url": _target_url(value["target_url"]),
        "ready_element_id": ready_element_id,
        "readiness_timeout_seconds": _number(
            value["readiness_timeout_seconds"],
            "readiness_timeout_seconds",
            minimum=0.1,
            maximum=600,
        ),
        "run_mode": run_mode,
        "loop_duration_minutes": loop_duration,
        "actions": actions,
    }


def normalize_strategy(value: Any, elements_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Compatibility alias for the explicit V2 strategy-definition validator."""

    return normalize_strategy_definition(value, elements_by_id=elements_by_id)


def referenced_element_ids(definition: dict[str, Any]) -> tuple[str, ...]:
    """Return all element IDs needed by a validated definition, in stable first-use order."""

    identifiers = [definition["ready_element_id"]]
    for action in definition["actions"]:
        element_id = action.get("element_id")
        if element_id is not None:
            identifiers.append(element_id)
    return tuple(dict.fromkeys(identifiers))


def _normalize_action(action: Any, elements_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(action, dict):
        raise StrategyValidationError("action must be an object")
    action_type = action.get("type")
    if action_type not in _ACTION_TYPES:
        raise StrategyValidationError("action type must be move, scroll, click, input, or wait")
    if set(action) != _ACTION_KEYS[action_type]:
        raise StrategyValidationError(f"{action_type} action keys are invalid")
    action_id = _text(action["id"], "action id")
    if action_type == "move":
        element_id = _text(action["element_id"], "move element_id")
        _validate_element(elements_by_id, element_id, purpose="action", action_type="move")
        return {
            "id": action_id,
            "type": "move",
            "element_id": element_id,
            "duration_seconds": _range(
                action["duration_seconds"], "move duration_seconds", minimum=0.001, maximum=600
            ),
        }
    if action_type == "scroll":
        direction = action["direction"]
        if direction not in {"up", "down"}:
            raise StrategyValidationError("scroll direction must be up or down")
        return {
            "id": action_id,
            "type": "scroll",
            "direction": direction,
            "distance_pixels": _range(
                action["distance_pixels"], "scroll distance_pixels", minimum=1, maximum=10000, integer=True
            ),
            "count": _range(action["count"], "scroll count", minimum=1, maximum=10000, integer=True),
            "interval_seconds": _range(
                action["interval_seconds"], "scroll interval_seconds", minimum=0, maximum=600
            ),
        }
    if action_type == "click":
        element_id = _text(action["element_id"], "click element_id")
        _validate_element(
            elements_by_id,
            element_id,
            purpose="action",
            kinds={"click", "generic"},
            action_type="click",
        )
        button = action["button"]
        if button not in {"left", "middle", "right"}:
            raise StrategyValidationError("click button must be left, middle, or right")
        return {
            "id": action_id,
            "type": "click",
            "element_id": element_id,
            "button": button,
            "click_count": _integer(action["click_count"], "click_count", minimum=1, maximum=3),
            "hold_seconds": _range(action["hold_seconds"], "click hold_seconds", minimum=0, maximum=60),
            "after_seconds": _range(action["after_seconds"], "click after_seconds", minimum=0, maximum=600),
        }
    if action_type == "input":
        element_id = _text(action["element_id"], "input element_id")
        _validate_element(
            elements_by_id,
            element_id,
            purpose="action",
            kinds={"input"},
            action_type="input",
        )
        content_source = action["content_source"]
        if content_source not in {"fixed", "library"}:
            raise StrategyValidationError("input content_source must be fixed or library")
        fixed_text = _string(action["fixed_text"], "fixed_text")
        library_id = _string(action["content_library_id"], "content_library_id")
        if content_source == "fixed":
            if not fixed_text.strip() or library_id:
                raise StrategyValidationError("fixed input requires fixed_text and no content_library_id")
        elif fixed_text or not library_id.strip():
            raise StrategyValidationError("library input requires content_library_id and no fixed_text")
        return {
            "id": action_id,
            "type": "input",
            "element_id": element_id,
            "content_source": content_source,
            "fixed_text": fixed_text,
            "content_library_id": library_id,
            "interval_ms": _range(
                action["interval_ms"], "input interval_ms", minimum=0, maximum=60000, integer=True
            ),
        }
    return {
        "id": action_id,
        "type": "wait",
        "duration_seconds": _range(
            action["duration_seconds"], "wait duration_seconds", minimum=0, maximum=600
        ),
    }


def _validate_element(
    elements_by_id: dict[str, dict[str, Any]],
    element_id: str,
    *,
    purpose: str,
    action_type: str,
    kinds: set[str] | None = None,
) -> None:
    element = elements_by_id.get(element_id)
    if not isinstance(element, dict):
        raise StrategyValidationError(f"{action_type} references missing element: {element_id}")
    if element.get("purpose") != purpose:
        raise StrategyValidationError(f"{action_type} element must have purpose {purpose}")
    if element.get("status") != "active":
        raise StrategyValidationError(f"{action_type} element must be active")
    if kinds is not None and element.get("kind") not in kinds:
        expected = " or ".join(sorted(kinds))
        raise StrategyValidationError(f"{action_type} element must have kind {expected}")


def _target_url(value: Any) -> str:
    url = _text(value, "target_url")
    try:
        parsed = urlsplit(url)
        _ = parsed.port
    except ValueError as error:
        raise StrategyValidationError("target_url must be an HTTPS URL") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise StrategyValidationError("target_url must be an HTTPS URL")
    return url


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StrategyValidationError(f"{field} must be a non-empty string")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise StrategyValidationError(f"{field} must be a string")
    return value


def _number(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategyValidationError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise StrategyValidationError(f"{field} is out of range")
    return number


def _integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
    number = _number(value, field, minimum=minimum, maximum=maximum)
    if not number.is_integer():
        raise StrategyValidationError(f"{field} must be an integer")
    return int(number)


def _range(
    value: Any, field: str, *, minimum: float, maximum: float, integer: bool = False
) -> list[float] | list[int]:
    if not isinstance(value, list) or len(value) != 2:
        raise StrategyValidationError(f"{field} must be a two-value range")
    converter = _integer if integer else _number
    result = [converter(item, field, minimum=minimum, maximum=maximum) for item in value]
    if result[0] > result[1]:
        raise StrategyValidationError(f"{field} minimum cannot exceed maximum")
    return result
