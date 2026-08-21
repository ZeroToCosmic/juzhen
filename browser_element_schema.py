from __future__ import annotations

import copy
import hashlib
from typing import Any


ELEMENT_SCOPES = {"page", "active_video", "visible_comment_panel"}
LOCATOR_TYPES = {"attribute", "role", "css", "xpath"}


def _stable_locator_id(alias: str, selector: str) -> str:
    digest = hashlib.sha256(f"{alias}\0{selector}".encode("utf-8")).hexdigest()[:16]
    return f"locator-{digest}"


def _text(value: Any, description: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{description} must not be empty")
    return text


def _exact_keys(value: dict, allowed: set[str], required: set[str], description: str) -> None:
    if not required <= set(value) or not set(value) <= allowed:
        raise ValueError(f"{description} has an invalid parameter shape")


def _boolean(value: Any, description: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{description} must be a boolean")
    return value


def _normalize_descendant(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("attribute locator descendant must be an object")
    _exact_keys(
        value,
        {"type", "name", "value", "role"},
        {"type", "name", "value", "role"},
        "attribute locator descendant",
    )
    if _text(value["type"], "attribute locator descendant type") != "attribute":
        raise ValueError("unsupported descendant locator type")
    return {
        "type": "attribute",
        "name": _text(value["name"], "attribute locator descendant name"),
        "value": _text(value["value"], "attribute locator descendant value"),
        "role": _text(value["role"], "attribute locator descendant role"),
    }


def _normalize_locator(alias: str, value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"locator must be an object: {alias}")
    locator_type = _text(value.get("type"), "locator type")
    if locator_type not in LOCATOR_TYPES:
        raise ValueError(f"unsupported locator type: {locator_type}")

    if locator_type == "attribute":
        _exact_keys(
            value,
            {"id", "type", "name", "value", "enabled", "fallback", "descendant"},
            {"id", "type", "name", "value", "enabled"},
            "attribute locator",
        )
        normalized = {
            "id": _text(value["id"], "locator ID"),
            "type": locator_type,
            "name": _text(value["name"], "attribute locator name"),
            "value": _text(value["value"], "attribute locator value"),
            "enabled": _boolean(value["enabled"], "locator enabled"),
        }
        if "fallback" in value:
            normalized["fallback"] = _boolean(value["fallback"], "locator fallback")
        if "descendant" in value:
            normalized["descendant"] = _normalize_descendant(value["descendant"])
        return normalized

    if locator_type == "role":
        _exact_keys(
            value,
            {"id", "type", "role", "name", "name_mode", "enabled", "fallback"},
            {"id", "type", "role", "name", "name_mode", "enabled"},
            "role locator",
        )
        name_mode = _text(value["name_mode"], "role locator name_mode")
        if name_mode not in {"exact", "contains"}:
            raise ValueError(f"unsupported role locator name_mode: {name_mode}")
        normalized = {
            "id": _text(value["id"], "locator ID"),
            "type": locator_type,
            "role": _text(value["role"], "role locator role"),
            "name": _text(value["name"], "role locator name"),
            "name_mode": name_mode,
            "enabled": _boolean(value["enabled"], "locator enabled"),
        }
        if "fallback" in value:
            normalized["fallback"] = _boolean(value["fallback"], "locator fallback")
        return normalized

    _exact_keys(
        value,
        {"id", "type", "value", "enabled", "fallback"},
        {"id", "type", "value", "enabled"},
        f"{locator_type} locator",
    )
    raw_selector = value["value"]
    if not isinstance(raw_selector, str) or not raw_selector.strip():
        raise ValueError(f"{locator_type} locator value must not be empty")
    selector = raw_selector
    if "javascript:" in selector.casefold():
        raise ValueError("locator selectors must not contain executable JavaScript")
    normalized = {
        "id": _text(value["id"], "locator ID"),
        "type": locator_type,
        "value": selector,
        "enabled": _boolean(value["enabled"], "locator enabled"),
    }
    if "fallback" in value:
        normalized["fallback"] = _boolean(value["fallback"], "locator fallback")
    return normalized


def migrate_element_definition(alias: str, value: object) -> dict:
    if isinstance(value, str):
        selector = value
        if not selector.strip():
            raise ValueError(f"element selector must not be empty: {alias}")
        return {
            "scope": "page",
            "locators": [{
                "id": _stable_locator_id(alias, selector),
                "type": "xpath",
                "value": selector,
                "enabled": True,
                "fallback": True,
            }],
        }
    if not isinstance(value, dict):
        raise ValueError(f"element definition must be an object: {alias}")
    return copy.deepcopy(value)


def normalize_element_definitions(value: object) -> dict[str, dict]:
    if not isinstance(value, dict):
        raise ValueError("elements must be a JSON object")
    normalized = {}
    for raw_alias, raw_definition in value.items():
        alias = str(raw_alias or "").strip()
        if not alias or alias in normalized:
            raise ValueError("element aliases must be non-empty and unique")
        definition = migrate_element_definition(alias, raw_definition)
        _exact_keys(
            definition,
            {"scope", "locators"},
            {"scope", "locators"},
            f"element definition: {alias}",
        )
        scope = str(definition.get("scope") or "").strip()
        if scope not in ELEMENT_SCOPES:
            raise ValueError(f"unsupported element scope: {scope}")
        raw_locators = definition.get("locators")
        if not isinstance(raw_locators, list) or not raw_locators:
            raise ValueError(f"element locators must be a non-empty list: {alias}")
        locators = [_normalize_locator(alias, item) for item in raw_locators]
        if len({item["id"] for item in locators}) != len(locators):
            raise ValueError(f"locator IDs must be unique: {alias}")
        if not any(item["enabled"] for item in locators):
            raise ValueError(f"element needs one enabled locator: {alias}")
        normalized[alias] = {"scope": scope, "locators": locators}
    return normalized


TIKTOK_COMMENT_TEMPLATE = {
    "评论入口": {
        "scope": "active_video",
        "locators": [{
            "id": "tiktok-comment-entry-primary",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-icon",
            "enabled": True,
        }],
    },
    "评论输入框": {
        "scope": "visible_comment_panel",
        "locators": [{
            "id": "tiktok-comment-input-primary",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-input",
            "enabled": True,
            "descendant": {
                "type": "attribute",
                "name": "contenteditable",
                "value": "true",
                "role": "textbox",
            },
        }],
    },
    "评论提交按钮": {
        "scope": "visible_comment_panel",
        "locators": [
            {
                "id": "tiktok-comment-submit-primary",
                "type": "css",
                "value": "button[data-e2e=\"comment-post\"]",
                "enabled": True,
            },
            {
                "id": "tiktok-comment-submit-role",
                "type": "role",
                "role": "button",
                "name": "Post",
                "name_mode": "exact",
                "enabled": True,
                "fallback": True,
            },
        ],
    },
}
