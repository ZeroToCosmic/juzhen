"""Version 2 browser block-strategy configuration and legacy migration."""

from __future__ import annotations

import copy
import math
from typing import Any

from browser_element_schema import normalize_element_definitions


SCROLL_WHEEL_DELTA = 120


ACTION_CATALOG: dict[str, dict] = {
    "move": {"label": "\u79fb\u52a8", "pattern_type": "mouse"},
    "click": {"label": "\u70b9\u51fb", "pattern_type": "mouse"},
    "scroll_up": {"label": "\u5411\u4e0a\u6eda\u52a8", "pattern_type": None},
    "scroll_down": {"label": "\u5411\u4e0b\u6eda\u52a8", "pattern_type": None},
    "keyboard_input": {"label": "\u952e\u76d8\u8f93\u5165", "pattern_type": "keyboard"},
    "pause": {"label": "\u505c\u6b62\uff08\u7b49\u5f85\uff09", "pattern_type": None},
}

DEFAULT_ACTION_PARAMS: dict[str, dict] = {
    "move": {
        "target_mode": "element",
        "element": "",
        "delta_viewport": [0.0, 0.0],
        "trajectory": {"source": "builtin", "id": "bezier"},
        "duration_seconds": [0.2, 0.8],
    },
    "click": {
        "element": "",
        "button": "left",
        "click_count": 1,
        "hold_seconds": [0.05, 0.15],
        "trajectory": {"source": "builtin", "id": "bezier"},
    },
    "scroll_up": {
        "distance": SCROLL_WHEEL_DELTA,
        "total_count": [1, 1],
        "burst_count": [1, 1],
        "interval_seconds": [0.1, 0.3],
    },
    "scroll_down": {
        "distance": SCROLL_WHEEL_DELTA,
        "total_count": [1, 1],
        "burst_count": [1, 1],
        "interval_seconds": [0.1, 0.3],
    },
    "keyboard_input": {
        "element": "",
        "content": {"source": "fixed", "text": "", "brand_id": ""},
        "typing": {"source": "builtin", "interval_ms": [50, 250]},
    },
    "pause": {"duration_seconds": [1.0, 1.0]},
}

_CONTENT_KEYS = {
    "text", "key", "keys", "character", "characters", "value", "clipboard", "password",
}


class _MissingReferenceError(ValueError):
    """A repairable reference error in an otherwise valid action."""


def _mapping(value: Any, description: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be a JSON object")
    return value


def _text(value: Any, description: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{description} must not be empty")
    return text


def _number(value: Any, description: str, *, minimum: float = 0, maximum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{description} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{description} must be a number") from error
    if not math.isfinite(number) or number < minimum or (maximum is not None and number > maximum):
        raise ValueError(f"{description} is out of range")
    return number


def _integer(value: Any, description: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    number = _number(value, description, minimum=minimum, maximum=maximum)
    if not number.is_integer():
        raise ValueError(f"{description} must be an integer")
    return int(number)


def _range(value: Any, description: str, *, minimum: float = 0, maximum: float | None = None, integer: bool = False) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{description} must be a two-value range")
    converter = _integer if integer else _number
    values = [converter(item, description, minimum=minimum, maximum=maximum) for item in value]
    if values[0] > values[1]:
        raise ValueError(f"{description} minimum cannot exceed maximum")
    return values


def _coordinate_pair(value: Any, description: str, *, minimum: float, maximum: float) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{description} must be a two-value coordinate pair")
    return [
        _number(item, description, minimum=minimum, maximum=maximum)
        for item in value
    ]


def _exact_keys(value: dict, keys: set[str], description: str) -> None:
    if set(value) != keys:
        raise ValueError(f"{description} has an invalid parameter shape")


def normalize_elements(value) -> dict[str, dict]:
    """Compatibility wrapper for canonical element definition normalization."""

    return normalize_element_definitions(value)


def _contains_content_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(key).casefold() in _CONTENT_KEYS or _contains_content_key(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_content_key(item) for item in value)
    return False


def _normalize_mouse_data(value: Any) -> dict:
    data = _mapping(value, "mouse pattern data")
    _exact_keys(data, {"points", "sample_count", "total_duration_ms"}, "mouse pattern data")
    if not isinstance(data["points"], list):
        raise ValueError("mouse pattern points must be a list")
    if len(data["points"]) < 2:
        raise ValueError("mouse pattern must contain at least 2 points")
    points = []
    for point in data["points"]:
        point = _mapping(point, "mouse pattern point")
        _exact_keys(point, {"x_ratio", "y_ratio", "dt_ms"}, "mouse pattern point")
        points.append({
            "x_ratio": _number(point["x_ratio"], "x_ratio", minimum=0, maximum=1),
            "y_ratio": _number(point["y_ratio"], "y_ratio", minimum=0, maximum=1),
            "dt_ms": _number(point["dt_ms"], "dt_ms"),
        })
    sample_count = _integer(data["sample_count"], "mouse pattern sample_count")
    if sample_count != len(points):
        raise ValueError("mouse pattern sample_count must match points")
    return {
        "points": points,
        "sample_count": sample_count,
        "total_duration_ms": _number(data["total_duration_ms"], "mouse pattern total_duration_ms"),
    }


def _normalize_keyboard_data(value: Any) -> dict:
    data = _mapping(value, "keyboard pattern data")
    if _contains_content_key(data):
        raise ValueError("keyboard pattern data must not contain content")
    _exact_keys(data, {"intervals_ms", "hold_ms", "sample_count", "total_duration_ms"}, "keyboard pattern data")
    if not isinstance(data["intervals_ms"], list) or not isinstance(data["hold_ms"], list):
        raise ValueError("keyboard pattern timing values must be lists")
    if len(data["intervals_ms"]) < 2 or len(data["hold_ms"]) < 2:
        raise ValueError("keyboard pattern must contain at least 2 timing samples")
    intervals = [_number(item, "keyboard interval_ms") for item in data["intervals_ms"]]
    holds = [_number(item, "keyboard hold_ms") for item in data["hold_ms"]]
    sample_count = _integer(data["sample_count"], "keyboard pattern sample_count")
    if sample_count != len(intervals) or sample_count != len(holds):
        raise ValueError("keyboard pattern sample_count must match timing values")
    return {
        "intervals_ms": intervals,
        "hold_ms": holds,
        "sample_count": sample_count,
        "total_duration_ms": _number(data["total_duration_ms"], "keyboard pattern total_duration_ms"),
    }


def normalize_patterns(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("patterns must be a list")
    normalized = []
    identifiers: set[str] = set()
    names: set[str] = set()
    for item in value:
        item = _mapping(item, "pattern")
        pattern_id = _text(item.get("id"), "pattern id")
        name = _text(item.get("name"), "pattern name")
        pattern_type = str(item.get("type") or "").strip()
        if pattern_id in identifiers or name in names:
            raise ValueError("pattern IDs and names must be unique")
        if pattern_type == "mouse":
            data = _normalize_mouse_data(item.get("data"))
        elif pattern_type == "keyboard":
            data = _normalize_keyboard_data(item.get("data"))
        else:
            raise ValueError("pattern type must be mouse or keyboard")
        identifiers.add(pattern_id)
        names.add(name)
        normalized.append({"id": pattern_id, "name": name, "type": pattern_type, "data": data})
    return normalized


def _normalize_pattern_source(value: Any, expected_type: str, patterns: dict[str, dict], description: str, repair_errors: list[str] | None = None) -> dict:
    source = _mapping(value, description)
    source_type = str(source.get("source") or "").strip()
    if source_type == "builtin":
        _exact_keys(source, {"source", "id"}, description)
        return {"source": "builtin", "id": _text(source.get("id"), f"{description} id")}
    if source_type == "pattern":
        _exact_keys(source, {"source", "id"}, description)
        pattern_id = str(source.get("id") or "").strip()
        pattern = patterns.get(pattern_id)
        if pattern is None:
            error = f"{description} references missing pattern: {pattern_id}"
            if repair_errors is None:
                raise _MissingReferenceError(error)
            repair_errors.append(error)
            return {"source": "pattern", "id": pattern_id}
        if pattern["type"] != expected_type:
            raise ValueError(f"{description} pattern type does not match")
        return {"source": "pattern", "id": pattern_id}
    raise ValueError(f"{description} source must be builtin or pattern")


def _normalize_typing(value: Any, patterns: dict[str, dict], repair_errors: list[str] | None = None) -> dict:
    typing = _mapping(value, "typing")
    source = str(typing.get("source") or "").strip()
    if source == "builtin":
        _exact_keys(typing, {"source", "interval_ms"}, "typing")
        return {"source": "builtin", "interval_ms": _range(typing.get("interval_ms"), "typing interval_ms", minimum=0, maximum=60000, integer=True)}
    if source == "pattern":
        return _normalize_pattern_source(typing, "keyboard", patterns, "typing", repair_errors)
    raise ValueError("typing source must be builtin or pattern")


def _normalize_content(value: Any) -> dict:
    content = _mapping(value, "keyboard content")
    _exact_keys(content, {"source", "text", "brand_id"}, "keyboard content")
    source = str(content.get("source") or "").strip()
    if source not in {"fixed", "generated_comment"}:
        raise ValueError("keyboard content source is invalid")
    text = str(content.get("text") or "")
    brand_id = str(content.get("brand_id") or "").strip()
    if source == "fixed" and not text.strip():
        raise ValueError("fixed keyboard content text must not be empty")
    if source == "fixed" and brand_id:
        raise ValueError("fixed keyboard content cannot specify a brand")
    return {"source": source, "text": text, "brand_id": brand_id}


def _normalize_action(value: Any, elements: dict[str, str], patterns: dict[str, dict], repair_errors: list[str] | None = None) -> dict:
    action = _mapping(value, "action")
    _exact_keys(action, {"id", "type", "params"}, "action")
    action_id = _text(action.get("id"), "action id")
    action_type = str(action.get("type") or "").strip()
    if action_type not in ACTION_CATALOG:
        raise ValueError(f"unsupported action type: {action_type}")
    params = _mapping(action.get("params"), "action params")
    if action_type == "move":
        _exact_keys(params, set(DEFAULT_ACTION_PARAMS["move"]), "move params")
        target_mode = str(params.get("target_mode") or "").strip()
        if target_mode not in {"element", "viewport"}:
            raise ValueError("move target_mode is invalid")
        element = str(params.get("element") or "").strip()
        if target_mode == "element" and element not in elements:
            error = f"move references missing element: {element}"
            if repair_errors is None:
                raise _MissingReferenceError(error)
            repair_errors.append(error)
        if target_mode == "viewport" and element:
            raise ValueError("viewport move cannot reference an element")
        delta = _coordinate_pair(
            params.get("delta_viewport"),
            "move delta_viewport",
            minimum=-1,
            maximum=1,
        )
        return {"id": action_id, "type": action_type, "params": {
            "target_mode": target_mode,
            "element": element,
            "delta_viewport": delta,
            "trajectory": _normalize_pattern_source(params.get("trajectory"), "mouse", patterns, "trajectory", repair_errors),
            "duration_seconds": _range(params.get("duration_seconds"), "move duration_seconds", minimum=0.001, maximum=600),
        }}
    if action_type == "click":
        _exact_keys(params, set(DEFAULT_ACTION_PARAMS["click"]), "click params")
        element = str(params.get("element") or "").strip()
        if element not in elements:
            error = f"click references missing element: {element}"
            if repair_errors is None:
                raise _MissingReferenceError(error)
            repair_errors.append(error)
        button = str(params.get("button") or "").strip()
        if button not in {"left", "middle", "right"}:
            raise ValueError("click button is invalid")
        return {"id": action_id, "type": action_type, "params": {
            "element": element,
            "button": button,
            "click_count": _integer(params.get("click_count"), "click_count", minimum=1, maximum=3),
            "hold_seconds": _range(params.get("hold_seconds"), "click hold_seconds", minimum=0, maximum=60),
            "trajectory": _normalize_pattern_source(params.get("trajectory"), "mouse", patterns, "trajectory", repair_errors),
        }}
    if action_type in {"scroll_up", "scroll_down"}:
        _exact_keys(params, set(DEFAULT_ACTION_PARAMS[action_type]), "scroll params")
        _integer(params.get("distance"), "scroll distance", minimum=1, maximum=10000)
        return {"id": action_id, "type": action_type, "params": {
            "distance": SCROLL_WHEEL_DELTA,
            "total_count": _range(params.get("total_count"), "scroll total_count", minimum=1, maximum=10000, integer=True),
            "burst_count": _range(params.get("burst_count"), "scroll burst_count", minimum=1, maximum=10000, integer=True),
            "interval_seconds": _range(params.get("interval_seconds"), "scroll interval_seconds", minimum=0, maximum=600),
        }}
    if action_type == "keyboard_input":
        _exact_keys(params, set(DEFAULT_ACTION_PARAMS["keyboard_input"]), "keyboard_input params")
        element = str(params.get("element") or "").strip()
        if element not in elements:
            error = f"keyboard input references missing element: {element}"
            if repair_errors is None:
                raise _MissingReferenceError(error)
            repair_errors.append(error)
        return {"id": action_id, "type": action_type, "params": {
            "element": element,
            "content": _normalize_content(params.get("content")),
            "typing": _normalize_typing(params.get("typing"), patterns, repair_errors),
        }}
    _exact_keys(params, set(DEFAULT_ACTION_PARAMS["pause"]), "pause params")
    return {"id": action_id, "type": action_type, "params": {
        "duration_seconds": _range(params.get("duration_seconds"), "pause duration_seconds", minimum=0, maximum=600),
    }}


def normalize_block_strategies(value, elements, patterns, *, allow_repair=False) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("block strategies must be a list")
    normalized_elements = normalize_elements(elements)
    normalized_patterns = normalize_patterns(patterns)
    patterns_by_id = {pattern["id"]: pattern for pattern in normalized_patterns}
    strategies: list[dict] = []
    strategy_ids: set[str] = set()
    for item in value:
        strategy = _mapping(item, "block strategy")
        strategy_id = _text(strategy.get("id"), "strategy id")
        if strategy_id in strategy_ids:
            raise ValueError("strategy IDs must be unique")
        name = _text(strategy.get("name"), "strategy name")
        run_mode = str(strategy.get("run_mode") or "").strip()
        if run_mode not in {"once", "loop"}:
            raise ValueError("strategy run_mode must be once or loop")
        batch_size = _integer(strategy.get("batch_size", 1), "strategy batch_size", minimum=1, maximum=8)
        if not isinstance(strategy.get("actions"), list):
            raise ValueError("strategy actions must be a list")
        result = {"id": strategy_id, "name": name, "run_mode": run_mode, "batch_size": batch_size}
        if run_mode == "loop":
            result["loop_duration_minutes"] = _range(strategy.get("loop_duration_minutes"), "loop_duration_minutes", minimum=0.001, maximum=1440)
        action_ids: set[str] = set()
        actions = []
        repair_errors = list(strategy.get("_migration_repair_errors", []))
        if any(not isinstance(error, str) or not error for error in repair_errors):
            raise ValueError("migration repair errors must be non-empty strings")
        for action in strategy["actions"]:
            normalized_action = _normalize_action(
                action,
                normalized_elements,
                patterns_by_id,
                repair_errors if allow_repair else None,
            )
            if normalized_action["id"] in action_ids:
                raise ValueError("action IDs must be unique within a strategy")
            action_ids.add(normalized_action["id"])
            actions.append(normalized_action)
        result["actions"] = actions
        if repair_errors:
            result["status"] = "needs_repair"
            result["repair_errors"] = repair_errors
        else:
            result["status"] = "ready"
        strategies.append(result)
        strategy_ids.add(strategy_id)
    return strategies


def _element_for_action(action: dict) -> str:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    return str(params.get("element") or "").strip()


def element_references(strategies, alias) -> list[dict]:
    alias = str(alias or "").strip()
    references = []
    for strategy in strategies if isinstance(strategies, list) else []:
        for index, action in enumerate(strategy.get("actions", []) if isinstance(strategy, dict) else [], start=1):
            if isinstance(action, dict) and _element_for_action(action) == alias:
                references.append({"strategy_id": strategy.get("id"), "action_id": action.get("id"), "index": index})
    return references


def pattern_references(strategies, pattern_id) -> list[dict]:
    pattern_id = str(pattern_id or "").strip()
    references = []
    for strategy in strategies if isinstance(strategies, list) else []:
        for index, action in enumerate(strategy.get("actions", []) if isinstance(strategy, dict) else [], start=1):
            params = action.get("params") if isinstance(action, dict) and isinstance(action.get("params"), dict) else {}
            sources = (params.get("trajectory"), params.get("typing"))
            if any(
                isinstance(source, dict)
                and source.get("source") == "pattern"
                and source.get("id") == pattern_id
                for source in sources
            ):
                references.append({"strategy_id": strategy.get("id"), "action_id": action.get("id"), "index": index})
    return references


def _legacy_action(action: Any, strategy_id: str, index: int) -> tuple[dict | None, str | None]:
    action = action if isinstance(action, dict) else {}
    action_type = str(action.get("type") or "").strip()
    if action_type == "input":
        action_type = "keyboard_input"
    if action_type not in ACTION_CATALOG:
        return None, f"unsupported legacy manual action type: {action_type or '<empty>'}"
    params = copy.deepcopy(DEFAULT_ACTION_PARAMS[action_type])
    alias = str(action.get("element") or "").strip()
    if action_type == "move":
        params["element"] = alias
    elif action_type in {"click", "keyboard_input"}:
        params["element"] = alias
    if action_type == "keyboard_input":
        source = action.get("content_source") == "generated_comment" or bool(action.get("generated_comment"))
        params["content"] = {
            "source": "generated_comment" if source else "fixed",
            "text": "" if source else str(action.get("text") or ""),
            "brand_id": str(action.get("comment_brand_id") or "").strip() if source else "",
        }
    if action_type in {"scroll_up", "scroll_down"}:
        if "distance" in action:
            params["distance"] = action["distance"]
        if "duration" in action:
            params["interval_seconds"] = [action["duration"], action["duration"]]
    if action_type == "pause" and "duration" in action:
        params["duration_seconds"] = [action["duration"], action["duration"]]
    return {"id": f"manual:{strategy_id}:action:{index}", "type": action_type, "params": params}, None


def _legacy_manual_strategy(value: Any, index: int) -> dict:
    strategy = value if isinstance(value, dict) else {}
    legacy_id = str(strategy.get("id") or f"strategy-{index}").strip() or f"strategy-{index}"
    actions = []
    repair_errors = []
    for action_index, action in enumerate(
        strategy.get("actions") if isinstance(strategy.get("actions"), list) else [], start=1
    ):
        migrated_action, repair_error = _legacy_action(action, legacy_id, action_index)
        if migrated_action is not None:
            actions.append(migrated_action)
        if repair_error is not None:
            repair_errors.append(repair_error)
    return {
        "id": f"manual:{legacy_id}",
        "name": str(strategy.get("name") or legacy_id).strip() or legacy_id,
        "run_mode": "once",
        "batch_size": strategy.get("batch_size", 1),
        "actions": actions,
        "_migration_repair_errors": repair_errors,
    }


def _legacy_auto_strategy(value: Any, index: int) -> dict:
    strategy = value if isinstance(value, dict) else {}
    legacy_id = str(strategy.get("id") or f"strategy-{index}").strip() or f"strategy-{index}"
    entry = str(strategy.get("entry_element") or "").strip()
    input_element = str(strategy.get("input_element") or "").strip()
    submit = str(strategy.get("submit_element") or "").strip()
    def action(action_index: int, action_type: str, params: dict) -> dict:
        return {"id": f"auto:{legacy_id}:action:{action_index}", "type": action_type, "params": params}
    return {
        "id": f"auto:{legacy_id}",
        "name": str(strategy.get("name") or legacy_id).strip() or legacy_id,
        "run_mode": "loop",
        "loop_duration_minutes": copy.deepcopy(strategy.get("total_duration_minutes", [3, 5])),
        "batch_size": strategy.get("batch_size", 4),
        "actions": [
            action(1, "pause", {"duration_seconds": copy.deepcopy(strategy.get("stay_seconds", [3, 10]))}),
            action(2, "scroll_down", {
                "distance": strategy.get("scroll_distance", 600),
                "total_count": copy.deepcopy(strategy.get("scroll_threshold", [30, 50])),
                "burst_count": copy.deepcopy(strategy.get("scrolls_per_round", [1, 3])),
                "interval_seconds": copy.deepcopy(strategy.get("scroll_interval_seconds", [1, 3])),
            }),
            action(3, "pause", {"duration_seconds": copy.deepcopy(strategy.get("pause_seconds", [3, 10]))}),
            action(4, "click", {**copy.deepcopy(DEFAULT_ACTION_PARAMS["click"]), "element": entry}),
            action(5, "keyboard_input", {
                **copy.deepcopy(DEFAULT_ACTION_PARAMS["keyboard_input"]),
                "element": input_element,
                "content": {"source": "generated_comment", "text": "", "brand_id": str(strategy.get("comment_brand_id") or "").strip()},
            }),
            action(6, "click", {**copy.deepcopy(DEFAULT_ACTION_PARAMS["click"]), "element": submit}),
        ],
    }


def load_or_migrate_strategy_state(browser) -> tuple[dict, bool]:
    """Return a copied version-3 browser state and whether legacy data was migrated."""

    state = copy.deepcopy(_mapping(browser, "browser settings"))
    original_state = copy.deepcopy(state)
    elements = normalize_elements(state.get("action_elements", {}))
    patterns = normalize_patterns(state.get("interaction_patterns", []))
    if state.get("strategy_schema_version") in {2, 3}:
        state["action_elements"] = elements
        state["interaction_patterns"] = patterns
        state["block_strategies"] = normalize_block_strategies(
            state.get("block_strategies", []), elements, patterns, allow_repair=True
        )
        state["strategy_schema_version"] = 3
        return state, state != original_state
    legacy_strategies = [
        *[_legacy_manual_strategy(item, index) for index, item in enumerate(state.get("action_strategies", []) if isinstance(state.get("action_strategies"), list) else [], start=1)],
        *[_legacy_auto_strategy(item, index) for index, item in enumerate(state.get("auto_strategies", []) if isinstance(state.get("auto_strategies"), list) else [], start=1)],
    ]
    state["strategy_schema_version"] = 3
    state["action_elements"] = elements
    state["interaction_patterns"] = patterns
    state["block_strategies"] = normalize_block_strategies(legacy_strategies, elements, patterns, allow_repair=True)
    return state, True


__all__ = [
    "ACTION_CATALOG", "DEFAULT_ACTION_PARAMS", "SCROLL_WHEEL_DELTA", "normalize_elements", "normalize_patterns",
    "normalize_block_strategies", "load_or_migrate_strategy_state", "element_references", "pattern_references",
]
