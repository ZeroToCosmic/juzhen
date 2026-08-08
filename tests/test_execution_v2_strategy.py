import copy

import pytest

from execution_v2.strategy import (
    StrategyRevisionConflictError,
    StrategyValidationError,
    normalize_strategy_definition,
)
from execution_v2.store import ExecutionStore


def _definition():
    return {
        "url_pattern": "https://www.tiktok.com/*",
        "frame_path": [],
        "locators": [{"type": "css", "value": "[data-e2e='button']", "priority": 1}],
        "diagnostic_metadata": {},
        "screenshot_path": "",
    }


def _element(element_id, *, purpose="action", kind="click", status="active", revision=1):
    return {
        "id": element_id,
        "name": element_id,
        "purpose": purpose,
        "kind": kind,
        "status": status,
        "revision": revision,
        "definition": _definition(),
    }


def _strategy(**changes):
    value = {
        "target_url": "https://www.tiktok.com/@example",
        "ready_element_id": "ready-1",
        "readiness_timeout_seconds": 20,
        "run_mode": "once",
        "loop_duration_minutes": None,
        "actions": [
            {"id": "move-1", "type": "move", "element_id": "click-1", "duration_seconds": [0.2, 0.5]},
            {"id": "scroll-1", "type": "scroll", "direction": "down", "distance_pixels": [120, 240], "count": [1, 2], "interval_seconds": [0.1, 0.2]},
            {"id": "click-1", "type": "click", "element_id": "click-1", "button": "left", "click_count": 1, "hold_seconds": [0.01, 0.02], "after_seconds": [0, 0.1]},
            {"id": "input-1", "type": "input", "element_id": "input-1", "content_source": "fixed", "fixed_text": "hello", "content_library_id": "", "interval_ms": [20, 40]},
            {"id": "wait-1", "type": "wait", "duration_seconds": [0.2, 0.5]},
        ],
    }
    value.update(changes)
    return value


def _elements():
    return {
        "ready-1": _element("ready-1", purpose="readiness", kind="generic"),
        "click-1": _element("click-1", kind="click"),
        "input-1": _element("input-1", kind="input"),
    }


def test_normalize_strategy_accepts_exact_v2_shape_and_actions_in_order():
    normalized = normalize_strategy_definition(_strategy(), elements_by_id=_elements())

    assert [action["id"] for action in normalized["actions"]] == [
        "move-1", "scroll-1", "click-1", "input-1", "wait-1"
    ]
    assert normalized["loop_duration_minutes"] is None


def test_click_accepts_generic_action_element():
    elements = _elements()
    elements["click-1"]["kind"] = "generic"

    normalized = normalize_strategy_definition(_strategy(), elements_by_id=elements)

    assert normalized["actions"][2]["element_id"] == "click-1"


@pytest.mark.parametrize(
    "change",
    [
        {"run_mode": "loop"},
        {"actions": [{"id": "old", "type": "scroll_down"}]},
        {"actions": [{"id": "old", "type": "keyboard_input"}]},
        {"actions": [{"id": "old", "type": "wait", "duration_seconds": [1, 1], "generated_comment": True}]},
        {"unexpected": True},
    ],
)
def test_normalize_strategy_rejects_legacy_or_unknown_fields(change):
    with pytest.raises(StrategyValidationError):
        normalize_strategy_definition(_strategy(**change), elements_by_id=_elements())


def test_normalize_strategy_requires_active_ready_element_and_matching_action_kinds():
    elements = _elements()
    elements["ready-1"]["purpose"] = "action"
    with pytest.raises(StrategyValidationError, match="ready_element"):
        normalize_strategy_definition(_strategy(), elements_by_id=elements)

    elements = _elements()
    elements["click-1"]["kind"] = "input"
    with pytest.raises(StrategyValidationError, match="click"):
        normalize_strategy_definition(_strategy(), elements_by_id=elements)

    elements = _elements()
    elements["input-1"]["status"] = "disabled"
    with pytest.raises(StrategyValidationError, match="active"):
        normalize_strategy_definition(_strategy(), elements_by_id=elements)


def test_normalize_strategy_requires_unique_action_ids_and_duration_range():
    duplicate = _strategy(actions=[
        {"id": "same", "type": "wait", "duration_seconds": [1, 1]},
        {"id": "same", "type": "wait", "duration_seconds": [1, 1]},
    ])
    with pytest.raises(StrategyValidationError, match="unique"):
        normalize_strategy_definition(duplicate, elements_by_id=_elements())

    duration = _strategy(run_mode="duration", loop_duration_minutes=[5, 3])
    with pytest.raises(StrategyValidationError, match="loop_duration"):
        normalize_strategy_definition(duration, elements_by_id=_elements())


def test_store_strategy_crud_is_transactional_and_uses_revision_lock(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    for element in _elements().values():
        store.create_element(
            element["id"], element["name"], element["purpose"], element["kind"], _definition()
        )

    created = store.create_strategy("strategy-1", "V2 test strategy", _strategy())
    assert created["revision"] == 1
    assert [row["position"] for row in store.list_strategy_actions("strategy-1")] == [1, 2, 3, 4, 5]

    replacement = _strategy(actions=[{"id": "wait-2", "type": "wait", "duration_seconds": [1, 1]}])
    updated = store.update_strategy("strategy-1", "Changed", replacement, True, expected_revision=1)
    assert updated["revision"] == 2
    assert updated["actions"] == replacement["actions"]
    assert store.list_strategy_actions("strategy-1")[0]["id"] == "wait-2"

    with pytest.raises(StrategyRevisionConflictError):
        store.update_strategy("strategy-1", "Changed", replacement, True, expected_revision=1)

    disabled = store.set_strategy_enabled("strategy-1", False, expected_revision=2)
    assert disabled["enabled"] is False
    assert disabled["revision"] == 3
    store.delete_strategy("strategy-1", expected_revision=3)
    assert store.get_strategy("strategy-1") is None


def test_snapshot_is_immutable_and_contains_all_referenced_element_revisions(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    for element in _elements().values():
        store.create_element(
            element["id"], element["name"], element["purpose"], element["kind"], _definition()
        )
    store.create_strategy("strategy-1", "V2 test strategy", _strategy())

    snapshot = store.build_execution_snapshot("strategy-1")
    expected = copy.deepcopy(snapshot)
    assert snapshot["strategy"]["revision"] == 1
    assert {item["id"] for item in snapshot["elements"]} == {"ready-1", "click-1", "input-1"}

    changed_definition = _definition()
    changed_definition["locators"] = [{"type": "css", "value": "[data-e2e='changed']", "priority": 1}]
    store.repick_element("click-1", changed_definition, expected_revision=1)
    assert snapshot == expected
    assert store.build_execution_snapshot("strategy-1")["elements"][1]["revision"] == 2


def test_store_rolls_back_strategy_write_when_action_insert_fails(tmp_path, monkeypatch):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    for element in _elements().values():
        store.create_element(
            element["id"], element["name"], element["purpose"], element["kind"], _definition()
        )

    original = store._insert_strategy_actions

    def fail_after_insert(connection, strategy_id, actions):
        original(connection, strategy_id, actions[:1])
        raise RuntimeError("insert failure")

    monkeypatch.setattr(store, "_insert_strategy_actions", fail_after_insert)
    with pytest.raises(RuntimeError, match="insert failure"):
        store.create_strategy("strategy-1", "V2 test strategy", _strategy())

    assert store.get_strategy("strategy-1") is None


def test_store_rolls_back_strategy_update_when_action_insert_fails(tmp_path, monkeypatch):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    for element in _elements().values():
        store.create_element(
            element["id"], element["name"], element["purpose"], element["kind"], _definition()
        )
    store.create_strategy("strategy-1", "Original", _strategy())

    def fail_insert(*_args):
        raise RuntimeError("insert failure")

    monkeypatch.setattr(store, "_insert_strategy_actions", fail_insert)
    with pytest.raises(RuntimeError, match="insert failure"):
        store.update_strategy(
            "strategy-1", "Changed", _strategy(actions=[]), True, expected_revision=1
        )

    assert store.get_strategy("strategy-1")["name"] == "Original"
    assert store.get_strategy("strategy-1")["revision"] == 1
    assert len(store.get_strategy("strategy-1")["actions"]) == 5
