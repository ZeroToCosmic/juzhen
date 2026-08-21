"""Closed-schema validation for V2 manually picked page elements."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit


_DEFINITION_KEYS = {
    "url_pattern",
    "frame_path",
    "locators",
    "diagnostic_metadata",
    "screenshot_path",
}
_CSS_OR_XPATH_LOCATOR_KEYS = {"type", "value", "priority"}
_ROLE_LOCATOR_KEYS = {"type", "role", "name", "priority"}
_LOCATOR_TYPES = {"css", "xpath", "role"}
_URL_PATTERN = re.compile(r"https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[^\s?#]*)?\Z")


class ElementValidationError(ValueError):
    """The element record or its locator definition is not safe to store."""


class ElementNotFoundError(KeyError):
    """No element exists for the supplied identifier."""


class ElementRevisionConflictError(RuntimeError):
    """The caller attempted a write against a stale element revision."""


RevisionConflictError = ElementRevisionConflictError


class ElementInUseError(RuntimeError):
    """An element still belongs to one or more saved strategy actions."""


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ElementValidationError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ElementValidationError(f"{field} must be a string")
    return value


def _validate_url_pattern(value: Any) -> str:
    pattern = _non_empty_string(value, "url_pattern")
    if not _URL_PATTERN.fullmatch(pattern):
        raise ElementValidationError("url_pattern must be an HTTPS URL pattern")
    try:
        parsed = urlsplit(pattern)
        port = parsed.port
    except ValueError as error:
        raise ElementValidationError("url_pattern must be an HTTPS URL pattern") from error
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ElementValidationError("url_pattern must be an HTTPS URL pattern")
    return pattern


def _normalize_locator(locator: Any) -> dict[str, Any]:
    if not isinstance(locator, dict):
        raise ElementValidationError("locator must be an object")
    locator_type = locator.get("type")
    if locator_type not in _LOCATOR_TYPES:
        raise ElementValidationError("locator type must be css, xpath, or role")
    expected_keys = (
        _ROLE_LOCATOR_KEYS if locator_type == "role" else _CSS_OR_XPATH_LOCATOR_KEYS
    )
    if set(locator) != expected_keys:
        raise ElementValidationError("locator keys do not match locator type")
    priority = locator.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        raise ElementValidationError("locator priority must be a non-negative integer")
    if locator_type == "role":
        return {
            "type": "role",
            "role": _non_empty_string(locator.get("role"), "locator role"),
            "name": _non_empty_string(locator.get("name"), "locator name"),
            "priority": priority,
        }
    return {
        "type": locator_type,
        "value": _non_empty_string(locator.get("value"), "locator value"),
        "priority": priority,
    }


def normalize_element_definition(value: Any) -> dict[str, Any]:
    """Validate and return an isolated, closed-schema element definition copy."""

    if not isinstance(value, dict) or set(value) != _DEFINITION_KEYS:
        raise ElementValidationError("definition keys must match the V2 element schema")
    frame_path = value["frame_path"]
    if not isinstance(frame_path, list) or any(
        not isinstance(item, str) or not item.strip() for item in frame_path
    ):
        raise ElementValidationError("frame_path must be a list of non-empty selectors")
    locators = value["locators"]
    if not isinstance(locators, list) or not locators:
        raise ElementValidationError("locators must be a non-empty list")
    metadata = value["diagnostic_metadata"]
    if not isinstance(metadata, dict) or any(not isinstance(key, str) for key in metadata):
        raise ElementValidationError("diagnostic_metadata must be an object with string keys")
    try:
        json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ElementValidationError("diagnostic_metadata must be JSON serializable") from error
    return {
        "url_pattern": _validate_url_pattern(value["url_pattern"]),
        "frame_path": list(frame_path),
        "locators": [_normalize_locator(locator) for locator in locators],
        "diagnostic_metadata": json.loads(json.dumps(metadata, ensure_ascii=False)),
        "screenshot_path": _optional_string(value["screenshot_path"], "screenshot_path"),
    }
