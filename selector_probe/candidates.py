"""Deterministic, safety-bounded selector candidate synthesis."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Mapping

from browser_element_schema import normalize_element_definitions
from selector_probe.contracts import ElementContract
from selector_probe.snapshot import SemanticNode, SemanticSnapshot


ATTRIBUTE_SCORES = {
    "data-e2e": 100,
    "data-testid": 95,
    "aria-label": 80,
    "name": 75,
    "placeholder": 70,
    "id": 65,
    "contenteditable": 60,
    "type": 58,
    "aria-labelledby": 56,
}
ROLE_SCORE = 85
PARENT_CONSTRAINT_SCORE = 55
RELATIVE_XPATH_SCORE = 30
HISTORICAL_XPATH_SCORE = 10

ABSOLUTE_XPATH = re.compile(r"^\s*/(?!/)")
POSITIONAL = re.compile(
    r"(?:nth-(?:last-)?(?:child|of-type)|\[\s*\d+\s*\]|"
    r"\b(?:last|position)\s*\(\s*\))",
    re.IGNORECASE,
)
LONG_DIGITS = re.compile(r"\d{12,}")
UUID_VALUE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_EXECUTABLE = re.compile(
    r"(?:javascript\s*:|document\s*\.|window\s*\.|=>|\bfunction\s*\()",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?:authorization|bearer|cookie|credential|csrf|jwt|pass(?:word)?|"
    r"secret|session|token)",
    re.IGNORECASE,
)
_DESTRUCTIVE = re.compile(
    r"(?:^|[^a-z])(?:delete|remove|account|settings?|disable)(?:[^a-z]|$)|"
    r"(?:删除|移除|账号|账户|设置|禁用)",
    re.IGNORECASE,
)
_USER_FIELD = re.compile(
    r"(?:data-)?(?:author|creator|owner|user|video|item)[-_]?(?:id|uid)",
    re.IGNORECASE,
)
_USER_HANDLE = re.compile(r"(?:href\s*=\s*[\"']?|/)[@][A-Za-z0-9._-]{2,}")
_UGC_TEXT_SELECTOR = re.compile(
    r"(?::has-text\s*\(|\btext\s*=|\btext\s*\(\s*\)|"
    r"(?:contains|normalize-space)\s*\(\s*text\s*\()",
    re.IGNORECASE,
)
_GENERATED_CLASS = re.compile(
    r"(?:^|[.#\"'= ])(?:css|sc|jsx|emotion|styled)-[A-Za-z0-9_-]{5,}",
    re.IGNORECASE,
)
_SAFE_ATTRIBUTE_NAME = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
_SIMPLE_CSS = re.compile(
    r"^(?:(?P<tag>[a-z][a-z0-9-]*)\s*)?"
    r"\[(?P<name>[a-z][a-z0-9:_-]*)="
    r"(?P<quote>[\"'])(?P<value>[^\"'\\\]\r\n]+)(?P=quote)\]$",
    re.IGNORECASE,
)
_SIMPLE_RELATIVE_XPATH = re.compile(
    r"^\.//\*\[@(?P<name>[a-z][a-z0-9:_-]*)="
    r"(?P<quote>[\"'])(?P<value>[^\"'\\\]\r\n]+)(?P=quote)\]$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ScoredCandidate:
    score: int
    candidate: dict


def _safe_text(value: object, *, maximum: int = 160) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if (
        not text
        or len(text) > maximum
        or LONG_DIGITS.search(text)
        or UUID_VALUE.search(text)
        or _SENSITIVE.search(text)
        or _DESTRUCTIVE.search(text)
        or _GENERATED_CLASS.search(text)
    ):
        return ""
    return text


def _normalized_label(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _node_name(node: SemanticNode) -> str:
    if isinstance(node.name, str) and node.name.strip():
        return _safe_text(node.name, maximum=80)
    for attribute in ("aria-label", "placeholder"):
        name = _safe_text(node.attributes.get(attribute), maximum=80)
        if name:
            return name
    return ""


def _name_matches(contract: ElementContract, actual: str) -> bool:
    actual_key = _normalized_label(actual)
    if _DESTRUCTIVE.search(actual_key):
        return False
    for expected in contract.accepted_names:
        expected_key = _normalized_label(expected)
        if contract.name_mode == "exact" and actual_key == expected_key:
            return True
        if contract.name_mode == "locale_map" and actual_key == expected_key:
            return True
        if contract.name_mode == "contains":
            if expected_key.isascii():
                pattern = re.compile(
                    rf"(?<![a-z0-9_]){re.escape(expected_key)}(?![a-z0-9_])",
                    re.IGNORECASE,
                )
                if pattern.search(actual_key):
                    return True
            elif actual_key == expected_key:
                return True
    return False


def _matching_node(contract: ElementContract, node: SemanticNode) -> bool:
    if not node.visible or not node.in_viewport:
        return False
    if node.role.casefold() not in contract.accepted_roles:
        return False
    name = _node_name(node)
    return bool(name and _name_matches(contract, name))


def _anchor_eligible(
    contract: ElementContract,
    node: SemanticNode,
) -> bool:
    return (
        node.visible
        and node.in_viewport
        and node.role.casefold() in contract.accepted_roles
    )


def _has_ancestor_attribute(
    node: SemanticNode,
    node_by_id: Mapping[int, SemanticNode],
    name: str,
    value: str,
    *,
    max_depth: int = 8,
) -> bool:
    parent_id = node.parent_backend_node_id
    visited: set[int] = set()
    for _ in range(max_depth):
        if parent_id is None or parent_id in visited:
            return False
        visited.add(parent_id)
        parent = node_by_id.get(parent_id)
        if parent is None:
            return False
        if _node_has_attribute(parent, name, value):
            return True
        parent_id = parent.parent_backend_node_id
    return False


def _historical_anchor_nodes(
    contract: ElementContract,
    definition: object,
    nodes: tuple[SemanticNode, ...],
) -> tuple[SemanticNode, ...]:
    if not isinstance(definition, dict):
        return ()
    if definition.get("scope") != contract.scope:
        return ()
    raw_locators = definition.get("locators")
    if not isinstance(raw_locators, list):
        return ()

    node_by_id = {node.backend_node_id: node for node in nodes}
    matched: dict[int, SemanticNode] = {}
    for raw in raw_locators:
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            continue
        candidates: list[SemanticNode] = []
        locator_type = raw.get("type")
        if locator_type == "attribute":
            safe_attribute = _safe_attribute(
                raw.get("name"),
                raw.get("value"),
            )
            if not safe_attribute:
                continue
            name, value = safe_attribute
            descendant = raw.get("descendant")
            if descendant is None:
                candidates = [
                    node
                    for node in nodes
                    if _anchor_eligible(contract, node)
                    and _node_has_attribute(node, name, value)
                ]
            elif isinstance(descendant, dict):
                child_attribute = _safe_attribute(
                    descendant.get("name"),
                    descendant.get("value"),
                )
                child_role = descendant.get("role")
                if (
                    not child_attribute
                    or not isinstance(child_role, str)
                    or child_role.casefold() not in contract.accepted_roles
                ):
                    continue
                child_name, child_value = child_attribute
                candidates = [
                    node
                    for node in nodes
                    if _anchor_eligible(contract, node)
                    and node.role.casefold() == child_role.casefold()
                    and _node_has_attribute(
                        node,
                        child_name,
                        child_value,
                    )
                    and _has_ancestor_attribute(
                        node,
                        node_by_id,
                        name,
                        value,
                    )
                ]
        elif locator_type == "css":
            parsed = _parsed_css(raw.get("value"))
            if not parsed:
                continue
            tag, name, value = parsed
            candidates = [
                node
                for node in nodes
                if _anchor_eligible(contract, node)
                and (not tag or node.tag.casefold() == tag)
                and _node_has_attribute(node, name, value)
            ]
        elif locator_type == "xpath":
            parsed = _parsed_relative_xpath(raw.get("value"))
            if not parsed:
                continue
            name, value = parsed
            candidates = [
                node
                for node in nodes
                if _anchor_eligible(contract, node)
                and _node_has_attribute(node, name, value)
            ]
        if len(candidates) == 1:
            matched[candidates[0].backend_node_id] = candidates[0]
    return tuple(matched.values())


def _safe_attribute(name: object, value: object) -> tuple[str, str] | None:
    if not isinstance(name, str):
        return None
    normalized_name = name.strip().casefold()
    if (
        normalized_name not in ATTRIBUTE_SCORES
        or not _SAFE_ATTRIBUTE_NAME.fullmatch(normalized_name)
        or _USER_FIELD.search(normalized_name)
        or _SENSITIVE.search(normalized_name)
    ):
        return None
    normalized_value = _safe_text(value)
    if not normalized_value or _USER_HANDLE.search(normalized_value):
        return None
    return normalized_name, normalized_value


def _candidate_key(candidate: Mapping[str, object]) -> str:
    canonical = {
        key: value
        for key, value in candidate.items()
        if key not in {"id", "enabled", "fallback"}
    }
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(alias: str, candidate: Mapping[str, object]) -> str:
    material = f"{alias}\0{candidate.get('type', '')}\0{_candidate_key(candidate)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"probe-{digest}"


def _xpath_literal(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    if '"' not in value:
        return f'"{value}"'
    parts = value.split("'")
    arguments: list[str] = []
    for index, part in enumerate(parts):
        if part:
            arguments.append(f"'{part}'")
        if index < len(parts) - 1:
            arguments.append('"\'"')
    return f"concat({', '.join(arguments)})"


def _parsed_css(value: object) -> tuple[str, str, str] | None:
    selector = _safe_text(value, maximum=400)
    if (
        not selector
        or _EXECUTABLE.search(selector)
        or POSITIONAL.search(selector)
        or _USER_FIELD.search(selector)
        or _USER_HANDLE.search(selector)
        or _UGC_TEXT_SELECTOR.search(selector)
    ):
        return None
    match = _SIMPLE_CSS.fullmatch(selector)
    if not match:
        return None
    safe_attribute = _safe_attribute(match.group("name"), match.group("value"))
    if not safe_attribute:
        return None
    name, attribute_value = safe_attribute
    return (match.group("tag").casefold() if match.group("tag") else "", name, attribute_value)


def _parsed_relative_xpath(value: object) -> tuple[str, str] | None:
    selector = _safe_text(value, maximum=400)
    if (
        not selector
        or ABSOLUTE_XPATH.search(selector)
        or _EXECUTABLE.search(selector)
        or POSITIONAL.search(selector)
        or _USER_FIELD.search(selector)
        or _USER_HANDLE.search(selector)
        or _UGC_TEXT_SELECTOR.search(selector)
    ):
        return None
    match = _SIMPLE_RELATIVE_XPATH.fullmatch(selector)
    if not match:
        return None
    return _safe_attribute(match.group("name"), match.group("value"))


def _node_has_attribute(node: SemanticNode, name: str, value: str) -> bool:
    return node.attributes.get(name) == value


def _historical_candidates(
    contract: ElementContract,
    definition: object,
    matching_nodes: tuple[SemanticNode, ...],
    node_by_id: Mapping[int, SemanticNode],
) -> list[dict]:
    if not isinstance(definition, dict):
        return []
    if set(definition) != {"scope", "locators"}:
        return []
    if definition.get("scope") != contract.scope:
        return []
    raw_locators = definition.get("locators")
    if not isinstance(raw_locators, list):
        return []
    result: list[dict] = []
    for raw in raw_locators:
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            continue
        if (
            not isinstance(raw.get("id"), str)
            or not raw["id"].strip()
            or (
                "fallback" in raw
                and not isinstance(raw.get("fallback"), bool)
            )
        ):
            continue
        locator_type = raw.get("type")
        if locator_type == "css":
            if not {"id", "type", "value", "enabled"} <= set(raw) <= {
                "id",
                "type",
                "value",
                "enabled",
                "fallback",
            }:
                continue
            parsed = _parsed_css(raw.get("value"))
            if not parsed:
                continue
            tag, name, value = parsed
            if not any(
                (not tag or node.tag.casefold() == tag)
                and _node_has_attribute(node, name, value)
                for node in matching_nodes
            ):
                continue
            result.append(
                {
                    "type": "css",
                    "value": raw["value"],
                    "enabled": True,
                }
            )
            continue
        if locator_type == "xpath":
            if not {"id", "type", "value", "enabled"} <= set(raw) <= {
                "id",
                "type",
                "value",
                "enabled",
                "fallback",
            }:
                continue
            parsed = _parsed_relative_xpath(raw.get("value"))
            if not parsed:
                continue
            name, value = parsed
            if not any(
                _node_has_attribute(node, name, value)
                for node in matching_nodes
            ):
                continue
            result.append(
                {
                    "type": "xpath",
                    "value": raw["value"],
                    "enabled": True,
                }
            )
            continue
        if locator_type == "attribute":
            if not {"id", "type", "name", "value", "enabled"} <= set(raw) <= {
                "id",
                "type",
                "name",
                "value",
                "enabled",
                "fallback",
                "descendant",
            }:
                continue
            safe_attribute = _safe_attribute(raw.get("name"), raw.get("value"))
            if not safe_attribute:
                continue
            name, value = safe_attribute
            descendant = raw.get("descendant")
            if descendant is None:
                if not any(
                    _node_has_attribute(node, name, value)
                    for node in matching_nodes
                ):
                    continue
                result.append(
                    {
                        "type": "attribute",
                        "name": name,
                        "value": value,
                        "enabled": True,
                    }
                )
                continue
            if (
                not isinstance(descendant, dict)
                or set(descendant) != {"type", "name", "value", "role"}
                or descendant.get("type") != "attribute"
            ):
                continue
            child_attribute = _safe_attribute(
                descendant.get("name"), descendant.get("value")
            )
            child_role = descendant.get("role")
            if (
                not child_attribute
                or not isinstance(child_role, str)
                or child_role.casefold() not in contract.accepted_roles
            ):
                continue
            child_name, child_value = child_attribute
            relation_exists = any(
                node.role.casefold() == child_role.casefold()
                and _node_has_attribute(node, child_name, child_value)
                and _has_ancestor_attribute(
                    node,
                    node_by_id,
                    name,
                    value,
                )
                for node in matching_nodes
            )
            if not relation_exists:
                continue
            result.append(
                {
                    "type": "attribute",
                    "name": name,
                    "value": value,
                    "enabled": True,
                    "descendant": {
                        "type": "attribute",
                        "name": child_name,
                        "value": child_value,
                        "role": child_role.casefold(),
                    },
                }
            )
            continue
        if locator_type == "role":
            if not {
                "id",
                "type",
                "role",
                "name",
                "name_mode",
                "enabled",
            } <= set(raw) <= {
                "id",
                "type",
                "role",
                "name",
                "name_mode",
                "enabled",
                "fallback",
            }:
                continue
            role = raw.get("role")
            name = _safe_text(raw.get("name"), maximum=80)
            name_mode = raw.get("name_mode")
            if (
                not isinstance(role, str)
                or role.casefold() not in contract.accepted_roles
                or name_mode not in {"exact", "contains"}
                or not name
                or not _name_matches(contract, name)
            ):
                continue
            result.append(
                {
                    "type": "role",
                    "role": role.casefold(),
                    "name": name,
                    "name_mode": name_mode,
                    "enabled": True,
                }
            )
    return result


def _historical_score(candidate: Mapping[str, object]) -> int:
    locator_type = candidate.get("type")
    if locator_type == "attribute":
        name = str(candidate.get("name") or "")
        score = ATTRIBUTE_SCORES.get(name, HISTORICAL_XPATH_SCORE)
        if isinstance(candidate.get("descendant"), Mapping):
            score += PARENT_CONSTRAINT_SCORE
        return score
    if locator_type == "css":
        parsed = _parsed_css(candidate.get("value"))
        return (
            ATTRIBUTE_SCORES.get(parsed[1], HISTORICAL_XPATH_SCORE)
            if parsed
            else HISTORICAL_XPATH_SCORE
        )
    if locator_type == "role":
        return ROLE_SCORE
    if locator_type == "xpath":
        return RELATIVE_XPATH_SCORE
    return HISTORICAL_XPATH_SCORE


def generate_candidates(
    contract: ElementContract,
    snapshot: SemanticSnapshot,
    historical_definition: object = None,
) -> list[dict]:
    if not isinstance(contract, ElementContract):
        raise ValueError("contract must be an ElementContract")
    if not isinstance(snapshot, SemanticSnapshot):
        raise ValueError("snapshot must be a SemanticSnapshot")

    scored: list[_ScoredCandidate] = []
    node_by_id = {node.backend_node_id: node for node in snapshot.nodes}
    name_matches = tuple(
        node for node in snapshot.nodes if _matching_node(contract, node)
    )
    anchor_matches = _historical_anchor_nodes(
        contract,
        historical_definition,
        tuple(snapshot.nodes),
    )
    matching_nodes = tuple(
        {
            node.backend_node_id: node
            for node in (*name_matches, *anchor_matches)
        }.values()
    )

    for semantic_node in matching_nodes:

        direct_attributes: list[tuple[str, str]] = []
        for preferred_name in contract.preferred_attributes:
            safe_attribute = _safe_attribute(
                preferred_name, semantic_node.attributes.get(preferred_name)
            )
            if not safe_attribute:
                continue
            name, value = safe_attribute
            direct_attributes.append((name, value))
            scored.append(
                _ScoredCandidate(
                    ATTRIBUTE_SCORES[name],
                    {
                        "type": "attribute",
                        "name": name,
                        "value": value,
                        "enabled": True,
                    },
                )
            )

        role_name = _node_name(semantic_node)
        if role_name:
            scored.append(
                _ScoredCandidate(
                    ROLE_SCORE,
                    {
                        "type": "role",
                        "role": semantic_node.role.casefold(),
                        "name": role_name,
                        "name_mode": (
                            contract.name_mode
                            if contract.name_mode
                            in {"exact", "contains"}
                            else "exact"
                        ),
                        "enabled": True,
                    },
                )
            )

        parent = node_by_id.get(semantic_node.parent_backend_node_id)
        if parent is not None and direct_attributes:
            parent_attributes = [
                safe
                for raw_name, raw_value in parent.attributes.items()
                if (safe := _safe_attribute(raw_name, raw_value))
            ]
            if parent_attributes:
                parent_name, parent_value = max(
                    parent_attributes,
                    key=lambda item: (ATTRIBUTE_SCORES[item[0]], item[0], item[1]),
                )
                child_name, child_value = max(
                    direct_attributes,
                    key=lambda item: (ATTRIBUTE_SCORES[item[0]], item[0], item[1]),
                )
                scored.append(
                    _ScoredCandidate(
                        PARENT_CONSTRAINT_SCORE,
                        {
                            "type": "attribute",
                            "name": parent_name,
                            "value": parent_value,
                            "enabled": True,
                            "descendant": {
                                "type": "attribute",
                                "name": child_name,
                                "value": child_value,
                                "role": semantic_node.role.casefold(),
                            },
                        },
                    )
                )

        if direct_attributes:
            xpath_name, xpath_value = max(
                direct_attributes,
                key=lambda item: (ATTRIBUTE_SCORES[item[0]], item[0], item[1]),
            )
            xpath = f".//*[@{xpath_name}={_xpath_literal(xpath_value)}]"
            if _parsed_relative_xpath(xpath):
                scored.append(
                    _ScoredCandidate(
                        RELATIVE_XPATH_SCORE,
                        {
                            "type": "xpath",
                            "value": xpath,
                            "enabled": True,
                        },
                    )
                )

    historical = _historical_candidates(
        contract,
        historical_definition,
        matching_nodes,
        node_by_id,
    )
    for candidate in historical:
        scored.append(
            _ScoredCandidate(
                _historical_score(candidate),
                candidate,
            )
        )

    ranked = sorted(
        scored,
        key=lambda item: (
            -item.score,
            _candidate_key(item.candidate),
        ),
    )
    candidates: list[dict] = []
    seen: set[str] = set()
    for item in ranked:
        key = _candidate_key(item.candidate)
        if key in seen:
            continue
        seen.add(key)
        candidate = dict(item.candidate)
        candidate["id"] = _stable_id(contract.alias, candidate)
        if candidates:
            candidate["fallback"] = True
        candidates.append(candidate)
        if len(candidates) == 5:
            break

    if not candidates:
        return []
    normalized = normalize_element_definitions(
        {
            contract.alias: {
                "scope": contract.scope,
                "locators": candidates,
            }
        }
    )
    return normalized[contract.alias]["locators"]


__all__ = ["generate_candidates"]
