"""Deterministic validation for saved manual CSS/XPath locators."""

from __future__ import annotations

import inspect
import math
import re
from collections.abc import Mapping, Sequence

from .inventory import (
    ALLOWED_LOCATOR_TYPES,
    MAX_LOCATORS,
    _safe_locator_syntax,
)


_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MASK = re.compile(r"^\*\*\*(?:.{4})?$", re.DOTALL)
_LOCATOR_RESULT_KEYS = (
    "status",
    "failure_code",
    "match_count",
    "visible",
    "enabled",
    "hit_target",
)
_CENTER_HIT_SCRIPT = r"""
element => {
    const rect = element.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    const root = element.getRootNode();
    const hitSource = root && typeof root.elementFromPoint === "function"
        ? root
        : element.ownerDocument;
    const hit = hitSource.elementFromPoint(
        rect.left + rect.width / 2,
        rect.top + rect.height / 2
    );
    return Boolean(hit && (hit === element || element.contains(hit)));
}
"""


class ValidationRejected(RuntimeError):
    """Fail-closed error retained for store resource-boundary checks."""

    def __init__(
        self,
        code: str,
        *,
        profile_mask: str = "",
        round_number: int | None = None,
        alias: str = "",
        match_count: int = 0,
        required_state: str = "",
        failures: Sequence[Mapping[str, object]] = (),
    ) -> None:
        self.code = code if _SAFE_CODE.fullmatch(code) else "validation_rejected"
        self.profile_mask = (
            profile_mask
            if isinstance(profile_mask, str) and _MASK.fullmatch(profile_mask)
            else ""
        )
        self.round_number = round_number
        self.alias = alias.strip() if isinstance(alias, str) else ""
        self.match_count = (
            match_count
            if isinstance(match_count, int)
            and not isinstance(match_count, bool)
            and match_count >= 0
            else 0
        )
        self.required_state = (
            required_state.strip()[:64]
            if isinstance(required_state, str)
            else ""
        )
        self.failures = tuple(
            {
                "alias": str(item.get("alias") or ""),
                "code": str(item.get("code") or "validation_rejected"),
                "match_count": int(item.get("match_count") or 0),
                "required_state": str(item.get("required_state") or ""),
            }
            for item in failures
            if isinstance(item, Mapping)
        )
        suffix = f": {self.profile_mask}" if self.profile_mask else ""
        if round_number in {1, 2}:
            suffix += f" round {round_number}"
        super().__init__(self.code + suffix)


def _resource_check(
    value: object,
    *,
    code: str,
    max_nodes: int,
    max_containers: int,
    max_depth: int,
    max_string_bytes: int,
) -> None:
    """Reject cyclic, non-JSON, or over-budget evidence structures."""

    stack: list[tuple[object, int]] = [(value, 0)]
    seen: set[int] = set()
    nodes = containers = string_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValidationRejected(code)
        if isinstance(item, str):
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > max_string_bytes:
                raise ValidationRejected(code)
            continue
        if item is None or type(item) in {bool, int}:
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValidationRejected(code)
            continue
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                raise ValidationRejected(code)
            seen.add(identity)
            containers += 1
            if containers > max_containers:
                raise ValidationRejected(code)
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValidationRejected(code)
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        if isinstance(item, Sequence) and not isinstance(
            item, (str, bytes, bytearray)
        ):
            identity = id(item)
            if identity in seen:
                raise ValidationRejected(code)
            seen.add(identity)
            containers += 1
            if containers > max_containers:
                raise ValidationRejected(code)
            for child in item:
                stack.append((child, depth + 1))
            continue
        raise ValidationRejected(code)


def _locator_result(
    failure_code: str = "",
    *,
    match_count: int = 0,
    visible: bool = False,
    enabled: bool = False,
    hit_target: bool = False,
) -> dict[str, object]:
    bounded_count = (
        min(match_count, 1_000)
        if isinstance(match_count, int)
        and not isinstance(match_count, bool)
        and match_count >= 0
        else 0
    )
    result: dict[str, object] = {
        "status": "failed" if failure_code else "passed",
        "failure_code": failure_code,
        "match_count": bounded_count,
        "visible": visible is True,
        "enabled": enabled is True,
        "hit_target": hit_target is True,
    }
    return {key: result[key] for key in _LOCATOR_RESULT_KEYS}


def _manual_locator(value: object) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    locator_type = value.get("type")
    locator_value = value.get("value")
    if (
        not isinstance(locator_type, str)
        or not isinstance(locator_value, str)
        or locator_type not in ALLOWED_LOCATOR_TYPES
        or locator_value != locator_value.strip()
        or not locator_value
        or not _safe_locator_syntax(locator_type, locator_value)
    ):
        return None
    return {"type": locator_type, "value": locator_value}


async def _await(value: object) -> object:
    return await value if inspect.isawaitable(value) else value


async def _xpath_matches(page: object, value: str) -> tuple[int, list[object]]:
    locator_factory = getattr(page, "locator", None)
    if callable(locator_factory):
        xpath_locator = locator_factory(f"xpath={value}")
        all_method = getattr(xpath_locator, "all", None)
        if callable(all_method):
            matches = await _await(all_method())
            if not isinstance(matches, Sequence) or isinstance(
                matches, (str, bytes, bytearray)
            ):
                raise TypeError("invalid_xpath_matches")
            return len(matches), list(matches)
        count_method = getattr(xpath_locator, "count", None)
        if callable(count_method):
            raw_count = await _await(count_method())
            if (
                isinstance(raw_count, bool)
                or not isinstance(raw_count, int)
                or raw_count < 0
            ):
                raise TypeError("invalid_xpath_count")
            if raw_count != 1:
                return raw_count, []
            nth_method = getattr(xpath_locator, "nth", None)
            if not callable(nth_method):
                raise TypeError("xpath_node_unavailable")
            return 1, [nth_method(0)]
    query = getattr(page, "query_selector_all", None)
    if not callable(query):
        raise TypeError("xpath_query_unavailable")
    matches = await _await(query(f"xpath={value}"))
    if not isinstance(matches, Sequence) or isinstance(
        matches, (str, bytes, bytearray)
    ):
        raise TypeError("invalid_xpath_matches")
    return len(matches), list(matches)


async def _deterministic_matches(
    page: object,
    locator: Mapping[str, str],
) -> tuple[int, list[object]]:
    if locator["type"] == "xpath":
        return await _xpath_matches(page, locator["value"])
    query = getattr(page, "query_selector_all", None)
    if not callable(query):
        raise TypeError("css_query_unavailable")
    matches = await _await(query(locator["value"]))
    if not isinstance(matches, Sequence) or isinstance(
        matches, (str, bytes, bytearray)
    ):
        raise TypeError("invalid_css_matches")
    return len(matches), list(matches)


async def _node_state(node: object, field: str) -> bool:
    if isinstance(node, Mapping):
        return node.get(field) is True
    method = getattr(node, f"is_{field}", None)
    if not callable(method):
        raise TypeError(f"node_{field}_unavailable")
    return await _await(method()) is True


async def _center_hit(page: object, node: object) -> bool:
    if isinstance(node, Mapping):
        return node.get("hit_target") is True
    evaluate = getattr(node, "evaluate", None)
    if callable(evaluate):
        return await _await(evaluate(_CENTER_HIT_SCRIPT)) is True
    page_evaluate = getattr(page, "evaluate", None)
    if not callable(page_evaluate):
        raise TypeError("hit_test_unavailable")
    return await _await(page_evaluate(_CENTER_HIT_SCRIPT, node)) is True


async def validate_locator(
    page: object,
    locator: Mapping[str, object],
) -> dict[str, object]:
    """Validate one saved inventory-safe CSS/XPath locator exactly."""

    selected = _manual_locator(locator)
    if selected is None:
        return _locator_result("selector_query_invalid")
    try:
        match_count, matches = await _deterministic_matches(page, selected)
        if match_count == 0:
            return _locator_result("selector_zero_match")
        if match_count != 1:
            return _locator_result("selector_ambiguous", match_count=match_count)
        if len(matches) != 1:
            return _locator_result("selector_query_invalid")
        node = matches[0]
        visible = await _node_state(node, "visible")
        enabled = await _node_state(node, "enabled")
        hit_target = await _center_hit(page, node) if visible else False
    except Exception:
        return _locator_result("selector_query_invalid")
    if not visible:
        return _locator_result(
            "selector_hidden", match_count=1, enabled=enabled
        )
    if not enabled:
        return _locator_result(
            "selector_disabled",
            match_count=1,
            visible=True,
            hit_target=hit_target,
        )
    if not hit_target:
        return _locator_result(
            "selector_hit_test_failed",
            match_count=1,
            visible=True,
            enabled=True,
        )
    return _locator_result(
        match_count=1,
        visible=True,
        enabled=True,
        hit_target=True,
    )


async def validate_element(
    page: object,
    definition: Mapping[str, object],
) -> dict[str, object]:
    """Try saved locators in order and stop at first deterministic pass."""

    raw_locators = (
        definition.get("locators") if isinstance(definition, Mapping) else None
    )
    locators = (
        list(raw_locators[:MAX_LOCATORS])
        if isinstance(raw_locators, Sequence)
        and not isinstance(raw_locators, (str, bytes, bytearray))
        else []
    )
    results: list[dict[str, object]] = []
    selected_locator: dict[str, str] | None = None
    selected_index: int | None = None
    for index, raw_locator in enumerate(locators):
        normalized = _manual_locator(raw_locator)
        result = await validate_locator(
            page,
            raw_locator if isinstance(raw_locator, Mapping) else {},
        )
        results.append({"index": index, "locator": normalized, **result})
        if result["status"] == "passed" and normalized is not None:
            selected_locator = normalized
            selected_index = index
            break
    failure_code = ""
    if selected_locator is None:
        failure_code = (
            str(results[-1]["failure_code"])
            if results
            else "selector_zero_match"
        )
    return {
        "status": "passed" if selected_locator is not None else "failed",
        "failure_code": failure_code,
        "selected_locator": selected_locator,
        "selected_locator_index": selected_index,
        "locator_results": results,
    }


__all__ = [
    "ValidationRejected",
    "validate_element",
    "validate_locator",
]
