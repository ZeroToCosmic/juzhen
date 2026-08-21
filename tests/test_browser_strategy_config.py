import copy
import json

import pytest

from browser_strategy_config import (
    ACTION_CATALOG,
    element_references,
    load_or_migrate_strategy_state,
    normalize_block_strategies,
    normalize_elements,
    normalize_patterns,
    pattern_references,
)
from gateway.app import create_app
from gateway.settings_store import DEFAULT_SETTINGS


def _elements():
    return normalize_elements(
        {"entry": "//entry", "input": "//textarea", "submit": "//button"}
    )


def _patterns():
    return normalize_patterns(
        [
            {
                "id": "mouse-natural",
                "name": "Natural mouse",
                "type": "mouse",
                "data": {
                    "points": [
                        {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 0},
                        {"x_ratio": 0.8, "y_ratio": 0.7, "dt_ms": 20},
                    ],
                    "sample_count": 2,
                    "total_duration_ms": 20,
                },
            },
            {
                "id": "keyboard-natural",
                "name": "Natural keyboard",
                "type": "keyboard",
                "data": {
                    "intervals_ms": [50, 70],
                    "hold_ms": [10, 12],
                    "sample_count": 2,
                    "total_duration_ms": 142,
                },
            },
        ]
    )


def _normalize_viewport_move(delta_viewport):
    return normalize_block_strategies(
        [
            {
                "id": "strategy-move",
                "name": "Viewport move",
                "run_mode": "once",
                "batch_size": 1,
                "actions": [
                    {
                        "id": "move-viewport",
                        "type": "move",
                        "params": {
                            "target_mode": "viewport",
                            "element": "",
                            "delta_viewport": delta_viewport,
                            "trajectory": {"source": "builtin", "id": "bezier"},
                            "duration_seconds": [0.2, 0.8],
                        },
                    }
                ],
            }
        ],
        {},
        [],
    )


def test_catalog_contains_exactly_six_visible_actions_in_required_order():
    assert list(ACTION_CATALOG) == [
        "move",
        "click",
        "scroll_up",
        "scroll_down",
        "keyboard_input",
        "pause",
    ]
    assert ACTION_CATALOG == {
        "move": {"label": "\u79fb\u52a8", "pattern_type": "mouse"},
        "click": {"label": "\u70b9\u51fb", "pattern_type": "mouse"},
        "scroll_up": {"label": "\u5411\u4e0a\u6eda\u52a8", "pattern_type": None},
        "scroll_down": {"label": "\u5411\u4e0b\u6eda\u52a8", "pattern_type": None},
        "keyboard_input": {"label": "\u952e\u76d8\u8f93\u5165", "pattern_type": "keyboard"},
        "pause": {"label": "\u505c\u6b62\uff08\u7b49\u5f85\uff09", "pattern_type": None},
    }


def _scroll_strategy(action_type="scroll_down", params=None):
    return {
        "id": "scroll-strategy",
        "name": "Scroll strategy",
        "run_mode": "once",
        "batch_size": 1,
        "actions": [{
            "id": "scroll-action",
            "type": action_type,
            "params": params or {
                "distance": 480,
                "total_count": [3, 7],
                "burst_count": [2, 5],
                "interval_seconds": [0.2, 0.6],
            },
        }],
    }


@pytest.mark.parametrize(
    ("field", "count_range"),
    [
        ("total_count", [0, 1]),
        ("total_count", [-1, 1]),
        ("total_count", [1.5, 2]),
        ("total_count", [3, 2]),
        ("total_count", [True, 2]),
        ("burst_count", [0, 1]),
        ("burst_count", [1, 1.5]),
        ("burst_count", [4, 3]),
    ],
)
def test_scroll_counts_require_positive_ordered_integers(field, count_range):
    strategy = _scroll_strategy()
    strategy["actions"][0]["params"][field] = count_range

    with pytest.raises(ValueError, match=f"scroll {field}"):
        normalize_block_strategies([strategy], {}, [])


@pytest.mark.parametrize("change", [("extra", 1), ("burst_count", None)])
def test_scroll_params_require_exact_keys(change):
    strategy = _scroll_strategy()
    key, value = change
    if value is None:
        del strategy["actions"][0]["params"][key]
    else:
        strategy["actions"][0]["params"][key] = value

    with pytest.raises(ValueError, match="invalid parameter shape"):
        normalize_block_strategies([strategy], {}, [])


def test_scroll_defaults_keep_hidden_legacy_burst_count_at_one():
    from browser_strategy_config import DEFAULT_ACTION_PARAMS, SCROLL_WHEEL_DELTA

    assert SCROLL_WHEEL_DELTA == 120
    assert DEFAULT_ACTION_PARAMS["scroll_up"]["distance"] == SCROLL_WHEEL_DELTA
    assert DEFAULT_ACTION_PARAMS["scroll_down"]["distance"] == SCROLL_WHEEL_DELTA
    assert DEFAULT_ACTION_PARAMS["scroll_up"]["burst_count"] == [1, 1]
    assert DEFAULT_ACTION_PARAMS["scroll_down"]["burst_count"] == [1, 1]


def test_scroll_normalization_replaces_legacy_distance_with_fixed_delta():
    from browser_strategy_config import SCROLL_WHEEL_DELTA

    strategy = _scroll_strategy(params={
        "distance": 600,
        "total_count": [3, 7],
        "burst_count": [1, 1],
        "interval_seconds": [0.2, 0.6],
    })

    normalized = normalize_block_strategies([strategy], {}, [])

    assert normalized[0]["actions"][0]["params"]["distance"] == SCROLL_WHEEL_DELTA


def test_scroll_refresh_and_restart_round_trip_preserves_visible_and_hidden_parameters(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    expected = copy.deepcopy(_scroll_strategy("scroll_up")["actions"][0]["params"])
    expected["distance"] = 120

    first = create_app().test_client()
    saved = first.put(
        "/api/browser/strategies",
        json={"strategies": [_scroll_strategy("scroll_up")]},
    )
    refreshed = first.get("/api/browser/strategies")
    restarted = create_app().test_client().get("/api/browser/strategies")
    persisted = json.loads(config_path.read_text(encoding="utf-8"))

    assert saved.status_code == 200
    assert refreshed.status_code == 200
    assert restarted.status_code == 200
    assert saved.get_json()["strategies"][0]["actions"][0]["params"] == expected
    assert refreshed.get_json()["strategies"][0]["actions"][0]["params"] == expected
    assert restarted.get_json()["strategies"][0]["actions"][0]["params"] == expected
    assert persisted["browser"]["block_strategies"][0]["actions"][0]["params"] == expected


@pytest.mark.parametrize("delta_viewport", [[0.2, -0.3], [-0.7, 0.4]])
def test_viewport_move_delta_components_do_not_require_range_ordering(delta_viewport):
    strategies = _normalize_viewport_move(delta_viewport)

    assert strategies[0]["actions"][0]["params"]["delta_viewport"] == delta_viewport


@pytest.mark.parametrize(
    "delta_viewport",
    [
        [0.1],
        [0.1, 0.2, 0.3],
        [-1.01, 0],
        [0, 1.01],
        ["right", 0],
        [float("inf"), 0],
        [True, 0],
    ],
)
def test_viewport_move_delta_rejects_invalid_coordinate_pairs(delta_viewport):
    with pytest.raises(ValueError, match="move delta_viewport"):
        _normalize_viewport_move(delta_viewport)


def test_keyboard_pattern_containing_text_is_rejected():
    with pytest.raises(ValueError, match="content"):
        normalize_patterns(
            [
                {
                    "id": "keyboard-unsafe",
                    "name": "Unsafe",
                    "type": "keyboard",
                    "data": {"text": "secret", "intervals_ms": [20], "hold_ms": [5]},
                }
            ]
        )


@pytest.mark.parametrize(
    "pattern",
    [
        {
            "id": "mouse-short",
            "name": "Mouse short",
            "type": "mouse",
            "data": {
                "points": [{"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 0}],
                "sample_count": 1,
                "total_duration_ms": 0,
            },
        },
        {
            "id": "keyboard-short",
            "name": "Keyboard short",
            "type": "keyboard",
            "data": {
                "intervals_ms": [50],
                "hold_ms": [10],
                "sample_count": 1,
                "total_duration_ms": 60,
            },
        },
    ],
    ids=["mouse", "keyboard"],
)
def test_persisted_patterns_require_at_least_two_recorded_samples(pattern):
    with pytest.raises(ValueError, match="at least 2"):
        normalize_patterns([pattern])


@pytest.mark.parametrize(
    "pattern",
    [
        {
            "id": "mouse-negative-dt",
            "name": "Mouse negative dt",
            "type": "mouse",
            "data": {
                "points": [
                    {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": -1},
                    {"x_ratio": 0.2, "y_ratio": 0.3, "dt_ms": 1},
                ],
                "sample_count": 2,
                "total_duration_ms": 1,
            },
        },
        {
            "id": "mouse-negative-duration",
            "name": "Mouse negative duration",
            "type": "mouse",
            "data": {
                "points": [
                    {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 0},
                    {"x_ratio": 0.2, "y_ratio": 0.3, "dt_ms": 1},
                ],
                "sample_count": 2,
                "total_duration_ms": -1,
            },
        },
        {
            "id": "keyboard-negative-interval",
            "name": "Keyboard negative interval",
            "type": "keyboard",
            "data": {
                "intervals_ms": [-1, 10],
                "hold_ms": [1, 1],
                "sample_count": 2,
                "total_duration_ms": 11,
            },
        },
        {
            "id": "keyboard-negative-hold",
            "name": "Keyboard negative hold",
            "type": "keyboard",
            "data": {
                "intervals_ms": [1, 10],
                "hold_ms": [-1, 1],
                "sample_count": 2,
                "total_duration_ms": 11,
            },
        },
        {
            "id": "keyboard-negative-duration",
            "name": "Keyboard negative duration",
            "type": "keyboard",
            "data": {
                "intervals_ms": [1, 10],
                "hold_ms": [1, 1],
                "sample_count": 2,
                "total_duration_ms": -1,
            },
        },
    ],
)
def test_persisted_patterns_reject_negative_timing(pattern):
    with pytest.raises(ValueError):
        normalize_patterns([pattern])


def test_valid_mouse_and_keyboard_patterns_normalize_without_content_or_absolute_coordinates():
    patterns = _patterns()

    assert patterns[0]["data"] == {
        "points": [
            {"x_ratio": 0.1, "y_ratio": 0.2, "dt_ms": 0},
            {"x_ratio": 0.8, "y_ratio": 0.7, "dt_ms": 20},
        ],
        "sample_count": 2,
        "total_duration_ms": 20,
    }
    assert patterns[1]["data"] == {
        "intervals_ms": [50, 70],
        "hold_ms": [10, 12],
        "sample_count": 2,
        "total_duration_ms": 142,
    }


def test_action_element_and_pattern_references_validate():
    strategies = normalize_block_strategies(
        [
            {
                "id": "strategy-one",
                "name": "One",
                "run_mode": "once",
                "batch_size": 1,
                "actions": [
                    {
                        "id": "move-one",
                        "type": "move",
                        "params": {
                            "target_mode": "element",
                            "element": "entry",
                            "delta_viewport": [0, 0],
                            "trajectory": {"source": "pattern", "id": "mouse-natural"},
                            "duration_seconds": [0.2, 0.8],
                        },
                    },
                    {
                        "id": "type-one",
                        "type": "keyboard_input",
                        "params": {
                            "element": "input",
                            "content": {"source": "fixed", "text": "hello", "brand_id": ""},
                            "typing": {"source": "pattern", "id": "keyboard-natural"},
                        },
                    },
                ],
            }
        ],
        _elements(),
        _patterns(),
    )

    assert strategies[0]["status"] == "ready"
    assert strategies[0]["actions"][0]["params"]["element"] == "entry"


@pytest.mark.parametrize("text", ["", "   ", "\t\n"])
def test_fixed_keyboard_content_rejects_empty_or_whitespace_text(text):
    with pytest.raises(ValueError, match="fixed keyboard content text"):
        normalize_block_strategies(
            [
                {
                    "id": "strategy-empty-input",
                    "name": "Empty input",
                    "run_mode": "once",
                    "batch_size": 1,
                    "actions": [
                        {
                            "id": "type-empty",
                            "type": "keyboard_input",
                            "params": {
                                "element": "input",
                                "content": {
                                    "source": "fixed",
                                    "text": text,
                                    "brand_id": "",
                                },
                                "typing": {
                                    "source": "builtin",
                                    "interval_ms": [50, 250],
                                },
                            },
                        }
                    ],
                }
            ],
            _elements(),
            [],
        )


def test_reference_helpers_return_exact_one_based_locations():
    strategies = [
        {
            "id": "strategy-one",
            "actions": [
                {"id": "move-one", "params": {"element": "entry"}},
                {"id": "click-one", "params": {"element": "submit", "trajectory": {"source": "pattern", "id": "mouse-natural"}}},
            ],
        }
    ]

    assert element_references(strategies, "submit") == [
        {"strategy_id": "strategy-one", "action_id": "click-one", "index": 2}
    ]
    assert pattern_references(strategies, "mouse-natural") == [
        {"strategy_id": "strategy-one", "action_id": "click-one", "index": 2}
    ]


def test_manual_strategy_migration_preserves_fixed_and_generated_content_sources():
    browser = {
        "action_elements": {"input": "//textarea"},
        "action_strategies": [
            {
                "id": "manual-one",
                "name": "Manual one",
                "actions": [
                    {"type": "input", "element": "input", "text": "fixed text"},
                    {"type": "input", "element": "input", "generated_comment": True, "comment_brand_id": "brand-1"},
                ],
            }
        ],
    }

    migrated, changed = load_or_migrate_strategy_state(browser)

    assert changed is True
    actions = migrated["block_strategies"][0]["actions"]
    assert actions[0]["type"] == "keyboard_input"
    assert actions[0]["params"]["content"] == {
        "source": "fixed", "text": "fixed text", "brand_id": ""
    }
    assert actions[1]["params"]["content"] == {
        "source": "generated_comment", "text": "", "brand_id": "brand-1"
    }


def test_auto_strategy_migration_creates_exact_six_block_order_and_preserves_ranges():
    browser = {
        "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
        "auto_strategies": [
            {
                "id": "auto-one",
                "name": "Auto one",
                "total_duration_minutes": [3, 5],
                "stay_seconds": [3, 10],
                "scrolls_per_round": [1, 3],
                "scroll_interval_seconds": [1, 3],
                "scroll_threshold": [30, 50],
                "pause_seconds": [3, 10],
                "scroll_distance": 640,
                "batch_size": 4,
                "entry_element": "entry",
                "input_element": "input",
                "submit_element": "submit",
                "comment_brand_id": "brand-1",
            }
        ],
    }

    migrated, _ = load_or_migrate_strategy_state(browser)

    strategy = migrated["block_strategies"][0]
    assert strategy["run_mode"] == "loop"
    assert strategy["loop_duration_minutes"] == [3.0, 5.0]
    assert strategy["batch_size"] == 4
    assert [action["type"] for action in strategy["actions"]] == [
        "pause", "scroll_down", "pause", "click", "keyboard_input", "click"
    ]
    assert strategy["actions"][0]["params"]["duration_seconds"] == [3.0, 10.0]
    assert strategy["actions"][1]["params"] == {
        "distance": 120,
        "total_count": [30, 50],
        "burst_count": [1, 3],
        "interval_seconds": [1.0, 3.0],
    }
    assert strategy["actions"][2]["params"]["duration_seconds"] == [3.0, 10.0]
    assert strategy["actions"][4]["params"]["content"]["brand_id"] == "brand-1"


def test_migrated_legacy_scroll_keeps_hidden_burst_range_after_reload():
    browser = {
        "auto_strategies": [{
            "id": "legacy-scroll",
            "scrolls_per_round": [2, 6],
            "scroll_threshold": [9, 12],
        }],
    }

    migrated, changed = load_or_migrate_strategy_state(browser)
    reloaded, reloaded_changed = load_or_migrate_strategy_state(migrated)
    scroll = reloaded["block_strategies"][0]["actions"][1]

    assert changed is True
    assert reloaded_changed is False
    assert scroll["type"] == "scroll_down"
    assert scroll["params"]["total_count"] == [9, 12]
    assert scroll["params"]["burst_count"] == [2, 6]


def test_missing_legacy_aliases_become_needs_repair_without_mutating_input():
    browser = {
        "action_elements": {"input": "//textarea"},
        "action_strategies": [
            {"id": "manual-one", "actions": [{"type": "click", "element": "missing"}]}
        ],
    }
    original = copy.deepcopy(browser)

    migrated, _ = load_or_migrate_strategy_state(browser)

    assert browser == original
    assert migrated["block_strategies"][0]["status"] == "needs_repair"
    assert migrated["block_strategies"][0]["repair_errors"]


@pytest.mark.parametrize(
    "action",
    [
        {"id": "script-one", "type": "script", "params": {}},
        {"id": "click-one", "type": "click", "params": {"element": "entry"}},
    ],
)
def test_v2_repair_mode_rejects_unknown_types_and_malformed_action_params(action):
    with pytest.raises(ValueError):
        normalize_block_strategies(
            [{"id": "strategy-one", "name": "One", "run_mode": "once", "actions": [action]}],
            _elements(),
            _patterns(),
            allow_repair=True,
        )


def test_unknown_legacy_manual_action_is_not_migrated_as_pause_and_needs_repair():
    migrated, _ = load_or_migrate_strategy_state(
        {"action_strategies": [{"id": "manual-one", "actions": [{"type": "script"}]}]}
    )

    strategy = migrated["block_strategies"][0]
    assert strategy["actions"] == []
    assert strategy["status"] == "needs_repair"
    assert any("unsupported legacy manual action type: script" in error for error in strategy["repair_errors"])


def test_rerunning_migration_returns_unchanged_data():
    migrated, _ = load_or_migrate_strategy_state(
        {"action_elements": {"cta": "//button"}, "action_strategies": [{"id": "manual", "actions": [{"type": "click", "element": "cta"}]}]}
    )
    rerun, changed = load_or_migrate_strategy_state(migrated)

    assert changed is False
    assert rerun == migrated


def test_browser_defaults_expose_version_zero_and_empty_new_collections():
    browser = DEFAULT_SETTINGS["browser"]

    assert browser["strategy_schema_version"] == 0
    assert browser["action_elements"] == {}
    assert browser["interaction_patterns"] == []
    assert browser["block_strategies"] == []
