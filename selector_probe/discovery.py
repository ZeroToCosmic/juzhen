"""Bounded, public-safe discovery of interactive semantic nodes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json

from browser_public_identity import mask_profile_id
from selector_probe.contracts import default_tiktok_contracts


INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "menuitem",
        "tab",
        "switch",
    }
)
STABLE_ATTRIBUTE_NAMES = (
    "data-e2e",
    "data-testid",
    "aria-label",
    "name",
    "placeholder",
    "contenteditable",
    "type",
    "id",
)
_PAGE_STATES = frozenset(
    {"feed_ready", "comment_panel_open", "comment_panel_closed"}
)


def _fingerprint(
    page_state: str,
    role: str,
    name: str,
    attributes: Mapping[str, str],
) -> str:
    stable = {
        key: attributes[key]
        for key in STABLE_ATTRIBUTE_NAMES
        if isinstance(attributes.get(key), str) and attributes[key]
    }
    canonical = json.dumps(
        {
            "page_state": page_state,
            "role": role,
            "name": name,
            "attributes": stable,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_states(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key)[:64]: item
        for key, item in list(value.items())[:32]
        if isinstance(key, str)
        and isinstance(item, (bool, int, float, str, type(None)))
    }


def _recommended_locators(
    role: str,
    name: str,
    attributes: Mapping[str, str],
) -> list[dict[str, object]]:
    for attribute in STABLE_ATTRIBUTE_NAMES:
        value = attributes.get(attribute)
        if isinstance(value, str) and value:
            return [
                {
                    "id": f"probe-{attribute.replace('_', '-')}",
                    "type": "attribute",
                    "name": attribute,
                    "value": value,
                    "enabled": True,
                }
            ]
    if role and name:
        return [
            {
                "id": "probe-role-name",
                "type": "role",
                "role": role,
                "name": name,
                "name_mode": "exact",
                "enabled": True,
            }
        ]
    return []


def discover_interactive_candidates(
    snapshot: Mapping[str, object],
    *,
    page_state: str,
    profile_mask: str,
) -> list[dict[str, object]]:
    if page_state not in _PAGE_STATES:
        raise ValueError("page_state is unsupported")
    nodes = snapshot.get("nodes", []) if isinstance(snapshot, Mapping) else []
    if not isinstance(nodes, Sequence) or isinstance(
        nodes, (str, bytes, bytearray)
    ):
        return []
    result: list[dict[str, object]] = []
    for node in nodes[:500]:
        if not isinstance(node, Mapping):
            continue
        role = str(node.get("role") or "").strip().casefold()
        if role not in INTERACTIVE_ROLES:
            continue
        if (
            node.get("visible") is not True
            or node.get("in_viewport") is not True
        ):
            continue
        raw_attributes = node.get("attributes")
        raw_attributes = (
            raw_attributes if isinstance(raw_attributes, Mapping) else {}
        )
        attributes = {
            key: value[:160]
            for key in STABLE_ATTRIBUTE_NAMES
            if isinstance((value := raw_attributes.get(key)), str)
            and value
            and len(value) <= 160
        }
        name = str(node.get("name") or "").strip()[:160]
        result.append(
            {
                "fingerprint": _fingerprint(
                    page_state, role, name, attributes
                ),
                "page_state": page_state,
                "scope": (
                    "visible_comment_panel"
                    if page_state == "comment_panel_open"
                    else "active_video"
                ),
                "profile_mask": mask_profile_id(profile_mask),
                "role": role,
                "name": name,
                "states": _safe_states(node.get("states")),
                "attributes": attributes,
                "visible": True,
                "in_viewport": True,
                "actionable": node.get("actionable") is True,
                "recommended_locators": _recommended_locators(
                    role, name, attributes
                ),
            }
        )
    return result[:200]


def merge_discovery_candidates(
    validations: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    grouped: dict[str, dict[str, object]] = {}
    if not isinstance(validations, Sequence) or isinstance(
        validations, (str, bytes, bytearray)
    ):
        return []
    for validation in validations:
        if not isinstance(validation, Mapping):
            continue
        evidence: object = validation.get(
            "evidence", validation.get("evidence_json")
        )
        if isinstance(evidence, str):
            try:
                evidence = json.loads(evidence)
            except (TypeError, ValueError):
                evidence = {}
        if not isinstance(evidence, Mapping):
            continue
        discoveries = evidence.get("discoveries", [])
        if not isinstance(discoveries, Sequence) or isinstance(
            discoveries, (str, bytes, bytearray)
        ):
            continue
        fallback_mask = mask_profile_id(validation.get("profile_mask"))
        for raw in discoveries[:200]:
            if not isinstance(raw, Mapping):
                continue
            fingerprint = str(raw.get("fingerprint") or "")
            if not fingerprint.startswith("sha256:"):
                continue
            if fingerprint not in grouped:
                grouped[fingerprint] = {
                    key: raw.get(key)
                    for key in (
                        "fingerprint",
                        "page_state",
                        "scope",
                        "role",
                        "name",
                        "states",
                        "attributes",
                        "actionable",
                        "recommended_locators",
                    )
                }
                grouped[fingerprint]["profile_masks"] = []
            profile_mask = mask_profile_id(
                raw.get("profile_mask") or fallback_mask
            )
            masks = grouped[fingerprint]["profile_masks"]
            if profile_mask and profile_mask not in masks:
                masks.append(profile_mask)
    result: list[dict[str, object]] = []
    for item in grouped.values():
        item["profile_masks"] = sorted(item["profile_masks"])
        item["profile_count"] = len(item["profile_masks"])
        result.append(item)
    result.sort(
        key=lambda item: (
            str(item.get("page_state") or ""),
            str(item.get("role") or ""),
            str(item.get("name") or ""),
            str(item.get("fingerprint") or ""),
        )
    )
    return result[:200]


def comment_entry_definition(
    candidates: Sequence[Mapping[str, object]],
    *,
    allow_unverified: bool = False,
) -> dict[str, object] | None:
    contracts = default_tiktok_contracts()
    entry_contracts = [
        contract
        for contract in contracts.values()
        if contract.required_state == "feed_ready"
        and contract.postcondition == "comment_panel_open"
        and contract.probe_action == "open_read_only"
    ]
    if len(entry_contracts) != 1:
        return None
    accepted_names = {
        " ".join(name.split()).casefold()
        for name in entry_contracts[0].accepted_names
    }
    accepted_names.add("comments")
    matches: list[Mapping[str, object]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        attributes = candidate.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        name = " ".join(str(candidate.get("name") or "").split()).casefold()
        if (
            candidate.get("page_state") == "feed_ready"
            and candidate.get("role") == "button"
            and (
                candidate.get("actionable") is True
                or (
                    allow_unverified
                    and candidate.get("visible") is True
                    and candidate.get("in_viewport") is True
                )
            )
            and (
                attributes.get("data-e2e") == "comment-icon"
                or name in accepted_names
            )
        ):
            matches.append(candidate)
    attribute_matches = [
        candidate
        for candidate in matches
        if isinstance(candidate.get("attributes"), Mapping)
        and candidate["attributes"].get("data-e2e") == "comment-icon"
    ]
    if attribute_matches:
        matches = attribute_matches
    if len(matches) != 1:
        return None
    attributes = matches[0].get("attributes")
    attributes = attributes if isinstance(attributes, Mapping) else {}
    if attributes.get("data-e2e") == "comment-icon":
        locator = {
            "id": "probe-comment-entry",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-icon",
            "enabled": True,
        }
    else:
        locator = {
            "id": "probe-comment-entry",
            "type": "role",
            "role": "button",
            "name": str(matches[0].get("name") or ""),
            "name_mode": "exact",
            "enabled": True,
        }
    return {"scope": "active_video", "locators": [locator]}


__all__ = [
    "INTERACTIVE_ROLES",
    "STABLE_ATTRIBUTE_NAMES",
    "comment_entry_definition",
    "discover_interactive_candidates",
    "merge_discovery_candidates",
]
