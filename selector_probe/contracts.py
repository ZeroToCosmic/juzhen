"""Immutable semantic intent contracts for selector discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

from browser_element_schema import ELEMENT_SCOPES, TIKTOK_COMMENT_TEMPLATE


SAFE_PROBE_ACTIONS = {"inspect_only", "open_read_only", "close_read_only"}
NAME_MODES = {"exact", "contains", "locale_map"}
VALID_STATES = {"feed_ready", "comment_panel_open", "comment_panel_closed"}
VALID_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "dialog",
    "group",
    "link",
    "listbox",
    "menuitem",
    "option",
    "radio",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}
VALID_ATTRIBUTES = {
    "data-e2e",
    "data-testid",
    "aria-label",
    "aria-labelledby",
    "name",
    "placeholder",
    "id",
    "contenteditable",
    "type",
}
_CONTRACT_FIELDS = {
    "intent",
    "required_state",
    "scope",
    "accepted_roles",
    "accepted_names",
    "preferred_attributes",
    "postcondition",
    "probe_action",
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
_SAFE_CLOSE_NAMES = {
    "close",
    "close comments",
    "dismiss",
    "dismiss comments",
    "关闭",
    "关闭评论",
    "收起",
    "收起评论",
}
_SAFE_CLOSE_ATTRIBUTES = {"data-e2e", "aria-label"}


@dataclass(frozen=True)
class ElementContract:
    alias: str
    intent: str
    required_state: str
    scope: str
    accepted_roles: tuple[str, ...]
    name_mode: str
    accepted_names: tuple[str, ...]
    preferred_attributes: tuple[str, ...]
    postcondition: str
    probe_action: str

    def public_dict(self) -> dict[str, object]:
        return {
            "intent": self.intent,
            "required_state": self.required_state,
            "scope": self.scope,
            "accepted_roles": list(self.accepted_roles),
            "accepted_names": {
                "mode": self.name_mode,
                "values": list(self.accepted_names),
            },
            "preferred_attributes": list(self.preferred_attributes),
            "postcondition": self.postcondition,
            "probe_action": self.probe_action,
        }


def _template_locators(definition: object) -> list[dict]:
    if not isinstance(definition, dict):
        return []
    locators = definition.get("locators")
    if not isinstance(locators, list):
        return []
    return [item for item in locators if isinstance(item, dict)]


def _is_comment_entry_definition(definition: object) -> bool:
    return any(
        locator.get("type") == "attribute"
        and locator.get("name") == "data-e2e"
        and locator.get("value") == "comment-icon"
        for locator in _template_locators(definition)
    )


def _is_comment_input_definition(definition: object) -> bool:
    for locator in _template_locators(definition):
        descendant = locator.get("descendant")
        if (
            locator.get("type") == "attribute"
            and locator.get("name") == "data-e2e"
            and locator.get("value") == "comment-input"
            and isinstance(descendant, dict)
            and descendant.get("type") == "attribute"
            and descendant.get("name") == "contenteditable"
            and descendant.get("value") == "true"
            and descendant.get("role") == "textbox"
        ):
            return True
    return False


def _is_comment_submit_definition(definition: object) -> bool:
    for locator in _template_locators(definition):
        if (
            locator.get("type") == "attribute"
            and locator.get("name") == "data-e2e"
            and locator.get("value") == "comment-post"
        ):
            return True
        if (
            locator.get("type") == "css"
            and isinstance(locator.get("value"), str)
            and re.fullmatch(
                r"""button\[data-e2e=(?:"comment-post"|'comment-post')\]""",
                locator["value"].strip(),
                re.IGNORECASE,
            )
        ):
            return True
    return False


def _canonical_tiktok_aliases() -> tuple[str, str, str]:
    classifiers = (
        _is_comment_entry_definition,
        _is_comment_input_definition,
        _is_comment_submit_definition,
    )
    matches = [
        [
            alias
            for alias, definition in TIKTOK_COMMENT_TEMPLATE.items()
            if classifier(definition)
        ]
        for classifier in classifiers
    ]
    if (
        any(len(items) != 1 for items in matches)
        or len({items[0] for items in matches if items}) != 3
    ):
        raise RuntimeError(
            "TikTok comment template must unambiguously identify entry, input, and submit"
        )
    return matches[0][0], matches[1][0], matches[2][0]


def _required_text(value: object, field: str, *, maximum: int = 240) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a non-empty string")
    text = " ".join(value.split())
    if not text or len(text) > maximum:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _string_tuple(
    value: object,
    field: str,
    *,
    normalize_identifier: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _required_text(item, field, maximum=120)
        if normalize_identifier:
            text = text.casefold()
            if not _IDENTIFIER.fullmatch(text):
                raise ValueError(f"{field} contains an invalid identifier")
        key = text.casefold()
        if key in seen:
            raise ValueError(f"{field} values must be unique")
        seen.add(key)
        result.append(text)
    return tuple(result)


def _normalize_contract(alias: str, value: object) -> ElementContract:
    if not isinstance(value, dict) or set(value) != _CONTRACT_FIELDS:
        raise ValueError(f"contract {alias!r} has an invalid parameter shape")

    intent = _required_text(value["intent"], f"contract {alias!r} intent")
    required_state = _required_text(
        value["required_state"], f"contract {alias!r} required_state"
    ).casefold()
    if required_state not in VALID_STATES:
        raise ValueError(f"unsupported required_state: {required_state}")

    scope = _required_text(value["scope"], f"contract {alias!r} scope").casefold()
    if scope not in ELEMENT_SCOPES:
        raise ValueError(f"unsupported contract scope: {scope}")

    accepted_roles = _string_tuple(
        value["accepted_roles"],
        f"contract {alias!r} accepted role",
        normalize_identifier=True,
    )
    if any(role not in VALID_ROLES for role in accepted_roles):
        raise ValueError(f"contract {alias!r} contains an unsupported role")

    names = value["accepted_names"]
    if (
        not isinstance(names, dict)
        or set(names) != {"mode", "values"}
    ):
        raise ValueError(f"contract {alias!r} accepted_names has an invalid shape")
    name_mode = _required_text(
        names["mode"], f"contract {alias!r} name mode"
    ).casefold()
    if name_mode not in NAME_MODES:
        raise ValueError(f"unsupported contract name mode: {name_mode}")
    accepted_names = _string_tuple(
        names["values"], f"contract {alias!r} accepted_names"
    )

    preferred_attributes = _string_tuple(
        value["preferred_attributes"],
        f"contract {alias!r} preferred attribute",
        normalize_identifier=True,
    )
    if any(name not in VALID_ATTRIBUTES for name in preferred_attributes):
        raise ValueError(f"contract {alias!r} contains an unsupported preferred attribute")

    postcondition_value = value["postcondition"]
    if not isinstance(postcondition_value, str):
        raise ValueError(f"contract {alias!r} postcondition must be a string")
    postcondition = postcondition_value.strip().casefold()
    if postcondition and postcondition not in VALID_STATES:
        raise ValueError(f"unsupported postcondition: {postcondition}")

    probe_action = _required_text(
        value["probe_action"], f"contract {alias!r} probe_action"
    ).casefold()
    if probe_action not in SAFE_PROBE_ACTIONS:
        raise ValueError(f"unsupported probe_action: {probe_action}")
    if probe_action == "close_read_only":
        normalized_close_names = {
            " ".join(name.split()).casefold() for name in accepted_names
        }
        safe_close_tuple = (
            intent == "close the visible comment panel"
            and required_state == "comment_panel_open"
            and scope == "visible_comment_panel"
            and postcondition == "comment_panel_closed"
            and accepted_roles == ("button",)
            and name_mode in {"exact", "locale_map"}
            and bool(normalized_close_names)
            and normalized_close_names <= _SAFE_CLOSE_NAMES
            and set(preferred_attributes) <= _SAFE_CLOSE_ATTRIBUTES
        )
        if not safe_close_tuple:
            raise ValueError(
                "close_read_only requires the canonical safe close contract"
            )
    if probe_action == "open_read_only":
        entry_alias, _input_alias, _submit_alias = _canonical_tiktok_aliases()
        safe_open_tuple = (
            alias == entry_alias
            and intent == "open the active video's comment panel"
            and required_state == "feed_ready"
            and scope == "active_video"
            and postcondition == "comment_panel_open"
            and accepted_roles == ("button",)
            and name_mode == "locale_map"
            and accepted_names
            == ("Comments", "Open comments", "评论", "打开评论")
            and preferred_attributes == ("data-e2e", "aria-label")
        )
        if not safe_open_tuple:
            raise ValueError(
                "open_read_only is restricted to the canonical TikTok comment entry"
            )

    return ElementContract(
        alias=alias,
        intent=intent,
        required_state=required_state,
        scope=scope,
        accepted_roles=accepted_roles,
        name_mode=name_mode,
        accepted_names=accepted_names,
        preferred_attributes=preferred_attributes,
        postcondition=postcondition,
        probe_action=probe_action,
    )


def normalize_contracts(value: object) -> dict[str, ElementContract]:
    if not isinstance(value, Mapping):
        raise ValueError("contracts must be a JSON object")
    normalized: dict[str, ElementContract] = {}
    for raw_alias, raw_contract in value.items():
        if not isinstance(raw_alias, str):
            raise ValueError("contract aliases must be non-empty strings")
        alias = raw_alias.strip()
        if not alias:
            raise ValueError("contract aliases must be non-empty strings")
        if alias in normalized:
            raise ValueError("contract aliases must be unique")
        normalized[alias] = _normalize_contract(alias, raw_contract)
    return normalized


def default_tiktok_contracts() -> dict[str, ElementContract]:
    """Return safe defaults keyed by the repository's canonical TikTok aliases."""

    entry_alias, input_alias, submit_alias = _canonical_tiktok_aliases()
    return normalize_contracts(
        {
            entry_alias: {
                "intent": "open the active video's comment panel",
                "required_state": "feed_ready",
                "scope": "active_video",
                "accepted_roles": ["button"],
                "accepted_names": {
                    "mode": "locale_map",
                    "values": ["Comments", "Open comments", "评论", "打开评论"],
                },
                "preferred_attributes": ["data-e2e", "aria-label"],
                "postcondition": "comment_panel_open",
                "probe_action": "open_read_only",
            },
            input_alias: {
                "intent": "editable comment textbox in the visible comment panel",
                "required_state": "comment_panel_open",
                "scope": "visible_comment_panel",
                "accepted_roles": ["textbox"],
                "accepted_names": {
                    "mode": "contains",
                    "values": ["comment", "评论"],
                },
                "preferred_attributes": [
                    "data-e2e",
                    "contenteditable",
                    "aria-label",
                ],
                "postcondition": "",
                "probe_action": "inspect_only",
            },
            submit_alias: {
                "intent": "comment submit button in the visible comment panel",
                "required_state": "comment_panel_open",
                "scope": "visible_comment_panel",
                "accepted_roles": ["button"],
                "accepted_names": {
                    "mode": "locale_map",
                    "values": [
                        "Post",
                        "Submit",
                        "Publish",
                        "发布",
                        "发送",
                    ],
                },
                "preferred_attributes": ["data-e2e", "aria-label"],
                "postcondition": "",
                "probe_action": "inspect_only",
            },
        }
    )


__all__ = [
    "ElementContract",
    "NAME_MODES",
    "SAFE_PROBE_ACTIONS",
    "default_tiktok_contracts",
    "normalize_contracts",
]
