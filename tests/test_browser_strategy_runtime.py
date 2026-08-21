import asyncio
import builtins
import hashlib
import random
import secrets
from types import SimpleNamespace

import pytest

from browser_element_resolver import LocatorResolutionError
from browser_strategy_config import DEFAULT_ACTION_PARAMS
from browser_strategy_runtime import (
    BlockExecutionError,
    StrategyPausedError,
    StrategyRuntimeError,
    build_batches,
    run_block_strategy,
    run_block_strategy_on_cdp,
    run_prepared_block_strategy_on_cdp,
)
from browser_video_switch import VideoSwitchError


def action(action_id, action_type="pause", **params):
    values = dict(DEFAULT_ACTION_PARAMS[action_type])
    values.update(params)
    return {"id": action_id, "type": action_type, "params": values}


def strategy(*actions, run_mode="once", duration=None, status="ready"):
    value = {
        "id": "strategy-1",
        "name": "Strategy",
        "run_mode": run_mode,
        "batch_size": 4,
        "actions": list(actions),
        "status": status,
    }
    if run_mode == "loop":
        value["loop_duration_minutes"] = duration or [1, 1]
    if status == "needs_repair":
        value["repair_errors"] = ["missing element"]
    return value


def run(coro):
    return asyncio.run(coro)


def test_build_batches_splits_all_windows_without_dropping_remainder():
    batches = build_batches(list(range(100)), 4)

    assert len(batches) == 25
    assert batches[0] == [0, 1, 2, 3]
    assert batches[-1] == [96, 97, 98, 99]


def test_once_runs_complete_action_list_once_in_order():
    calls = []

    async def execute(_page, item, _elements, _patterns, _resolver, **_kwargs):
        calls.append(item["id"])
        return {"action_id": item["id"], "status": "ok"}

    result = run(
        run_block_strategy(
            object(),
            strategy(action("a"), action("b")),
            {},
            [],
            lambda _item: "",
            execute_fn=execute,
        )
    )

    assert calls == ["a", "b"]
    assert result["cycles"] == 1
    assert [item["action_id"] for item in result["actions"]] == ["a", "b"]


def test_dispatch_hook_waits_for_side_effect_gate_and_skips_failed_dispatch():
    checks = []
    dispatched = []
    executed = []

    def gate_check(_strategy_id, item):
        checks.append(item["id"] if item else None)
        allowed = len(checks) < 3
        return {
            "allowed": allowed,
            "reasons": []
            if allowed
            else [
                {
                    "source": "probe",
                    "reason_code": "selector_validation_failed",
                }
            ],
        }

    async def execute(_page, item, _elements, _patterns, _resolver, **kwargs):
        await kwargs["before_side_effect"]()
        executed.append(item["id"])
        return {"action_id": item["id"], "status": "ok"}

    with pytest.raises(StrategyPausedError):
        run(
            run_block_strategy(
                object(),
                strategy(action("a")),
                {},
                [],
                lambda _item: "",
                execute_fn=execute,
                gate_check=gate_check,
                on_action_dispatch=lambda _strategy_id, item: dispatched.append(
                    item["id"]
                ),
            )
        )

    assert checks == [None, "a", "a"]
    assert dispatched == []
    assert executed == []


def test_locator_preprocessing_failure_does_not_mark_action_dispatched(
    monkeypatch,
):
    dispatched = []

    async def fail_resolution(_page, alias, _elements):
        await asyncio.sleep(0)
        raise LocatorResolutionError(
            "element_candidate_not_found",
            alias,
            "document",
            {},
        )

    monkeypatch.setattr(
        "browser_actions._resolve_action_element",
        fail_resolution,
    )

    with pytest.raises(BlockExecutionError) as caught:
        run(
            run_block_strategy(
                object(),
                strategy(action("click", "click", element="entry")),
                {"entry": "//button"},
                [],
                lambda _item: "",
                gate_check=lambda *_args: {
                    "allowed": True,
                    "reasons": [],
                },
                on_action_dispatch=lambda _strategy_id, item: dispatched.append(
                    item["id"]
                ),
            )
        )

    assert caught.value.locator["code"] == "element_candidate_not_found"
    assert dispatched == []


def test_pause_action_never_marks_browser_side_effect_dispatched():
    dispatched = []

    result = run(
        run_block_strategy(
            object(),
            strategy(action("pause")),
            {},
            [],
            lambda _item: "",
            sleep_fn=lambda _seconds: asyncio.sleep(0),
            gate_check=lambda *_args: {
                "allowed": True,
                "reasons": [],
            },
            on_action_dispatch=lambda _strategy_id, item: dispatched.append(
                item["id"]
            ),
        )
    )

    assert result["actions"][0]["status"] == "ok"
    assert dispatched == []


def test_loop_samples_deadline_once_and_finishes_started_cycle():
    now = [0.0]
    calls = []

    class Rng:
        def uniform(self, minimum, maximum):
            assert (minimum, maximum) == (0.01, 0.01)
            return minimum

    async def execute(_page, item, _elements, _patterns, _resolver, **_kwargs):
        calls.append(item["id"])
        now[0] += 0.35
        return {"action_id": item["id"], "status": "ok"}

    result = run(
        run_block_strategy(
            object(),
            strategy(action("a"), action("b"), run_mode="loop", duration=[0.01, 0.01]),
            {},
            [],
            lambda _item: "",
            rng=Rng(),
            monotonic_fn=lambda: now[0],
            execute_fn=execute,
        )
    )

    assert calls == ["a", "b"]
    assert result["cycles"] == 1
    assert result["sampled_duration_minutes"] == 0.01


def test_loop_starts_another_complete_cycle_only_before_deadline():
    now = [0.0]
    calls = []

    async def execute(_page, item, _elements, _patterns, _resolver, **_kwargs):
        calls.append(item["id"])
        now[0] += 0.2
        return {"action_id": item["id"], "status": "ok"}

    result = run(
        run_block_strategy(
            object(),
            strategy(action("a"), run_mode="loop", duration=[0.01, 0.01]),
            {},
            [],
            lambda _item: "",
            rng=random.Random(1),
            monotonic_fn=lambda: now[0],
            execute_fn=execute,
        )
    )

    assert calls == ["a", "a", "a"]
    assert result["cycles"] == 3


def test_action_failure_reports_id_one_based_index_type_and_reason():
    async def execute(_page, item, _elements, _patterns, _resolver, **_kwargs):
        if item["id"] == "bad":
            raise RuntimeError("target detached")
        return {"action_id": item["id"], "status": "ok"}

    with pytest.raises(BlockExecutionError) as caught:
        run(
            run_block_strategy(
                object(),
                strategy(action("good"), action("bad", "scroll_down"), action("never")),
                {},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    error = caught.value
    assert error.action_id == "bad"
    assert error.action_index == 2
    assert error.action_type == "scroll_down"
    assert error.reason == "target detached"
    assert "target detached" in str(error)


def test_action_failure_preserves_only_safe_completed_actions_for_staging():
    async def execute(_page, item, _elements, _patterns, _resolver, **_kwargs):
        if item["id"] == "comment-1":
            raise RuntimeError("comment entry unavailable")
        return {
            "action_id": item["id"],
            "status": "ok",
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 6,
            "switches": [
                {
                    "from": "a1b2c3d4e5f6",
                    "to": "c3d4e5f6a7b8",
                    "wheel_events": 6,
                    "full_fingerprint": "video:private",
                }
            ],
            "text": "private comment content",
        }

    with pytest.raises(BlockExecutionError) as caught:
        run(
            run_block_strategy(
                object(),
                strategy(
                    action(
                        "scroll-1",
                        "scroll_down",
                        total_count=[1, 1],
                        interval_seconds=[0, 0],
                    ),
                    action("comment-1", "click", element="entry"),
                ),
                {"entry": "//button[@data-role='comment-entry']"},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    expected = [
        {
            "action_id": "scroll-1",
            "type": "scroll_down",
            "status": "ok",
            "cycle": 1,
            "action_index": 1,
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 6,
            "switches": [
                {
                    "from": "a1b2c3d4e5f6",
                    "to": "c3d4e5f6a7b8",
                    "wheel_events": 6,
                }
            ],
        }
    ]
    assert caught.value.completed_actions == expected

    staged = StrategyRuntimeError(
        "execute_actions",
        str(caught.value),
        source=caught.value,
    )

    assert staged.completed_actions == expected


def test_block_error_recursively_copies_completed_action_switches():
    completed_actions = [
        {
            "action_id": "scroll-1",
            "type": "scroll_down",
            "status": "ok",
            "cycle": 1,
            "action_index": 1,
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 6,
            "switches": [
                {
                    "from": "a1b2c3d4e5f6",
                    "to": "c3d4e5f6a7b8",
                    "wheel_events": 6,
                }
            ],
        }
    ]
    error = BlockExecutionError(
        "comment-1",
        2,
        "click",
        "comment entry unavailable",
        cycle=1,
        completed_actions=completed_actions,
    )

    completed_actions[0]["switches"][0]["wheel_events"] = 99
    completed_actions[0]["switches"].append(
        {"from": "forged", "to": "forged", "wheel_events": 99}
    )

    assert error.completed_actions[0]["switches"] == [
        {
            "from": "a1b2c3d4e5f6",
            "to": "c3d4e5f6a7b8",
            "wheel_events": 6,
        }
    ]


def test_staged_error_recursively_copies_block_completed_action_switches():
    block = BlockExecutionError(
        "comment-1",
        2,
        "click",
        "comment entry unavailable",
        cycle=1,
        completed_actions=[
            {
                "action_id": "scroll-1",
                "type": "scroll_down",
                "status": "ok",
                "cycle": 1,
                "action_index": 1,
                "requested_switches": 1,
                "completed_switches": 1,
                "wheel_events": 6,
                "switches": [
                    {
                        "from": "a1b2c3d4e5f6",
                        "to": "c3d4e5f6a7b8",
                        "wheel_events": 6,
                    }
                ],
            }
        ],
    )
    staged = StrategyRuntimeError("execute_actions", str(block), source=block)

    block.completed_actions[0]["switches"][0]["wheel_events"] = 99
    block.completed_actions[0]["switches"].clear()

    assert staged.completed_actions[0]["switches"] == [
        {
            "from": "a1b2c3d4e5f6",
            "to": "c3d4e5f6a7b8",
            "wheel_events": 6,
        }
    ]


def test_action_failure_preserves_failed_page_recovery_metadata():
    recovery = {
        "action_id": "bad",
        "retry": 1,
        "status": "failed",
        "outcome": "retry_failed",
    }

    async def execute(_page, _item, _elements, _patterns, _resolver, **_kwargs):
        error = RuntimeError("replacement action failed")
        error.page_recoveries = [recovery]
        raise error

    with pytest.raises(BlockExecutionError) as caught:
        run(
            run_block_strategy(
                object(),
                strategy(action("bad")),
                {},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    assert caught.value.page_recoveries == [recovery]


def test_runtime_passes_canonical_element_definitions_to_actions():
    received = []

    async def execute(_page, _item, elements, _patterns, _resolver, **_kwargs):
        received.append(elements["entry"])
        return {"action_id": "click", "status": "ok"}

    run(
        run_block_strategy(
            object(),
            strategy(action("click", "click", element="entry")),
            {"entry": "//button[@data-secret='never-return-this']"},
            [],
            lambda _item: "",
            execute_fn=execute,
        )
    )

    assert received[0]["scope"] == "page"
    assert received[0]["locators"][0]["type"] == "xpath"


def test_action_locator_error_is_safely_attached_to_block_error():
    async def execute(*_args, **_kwargs):
        raise LocatorResolutionError(
            "element_candidate_not_found",
            "评论入口",
            "active_video",
            {
                "selector": "//secret-selector",
                "candidates": [
                    {
                        "id": "entry-primary",
                        "type": "attribute",
                        "raw_count": 0,
                        "visible_count": 0,
                        "actionable_count": 0,
                        "selector": "//secret-selector",
                    }
                ],
            },
        )

    with pytest.raises(BlockExecutionError) as caught:
        run(
            run_block_strategy(
                object(),
                strategy(action("click", "click", element="entry")),
                {"entry": "//button"},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    assert caught.value.locator == {
        "code": "element_candidate_not_found",
        "alias": "评论入口",
        "scope": "active_video",
        "diagnostics": {
            "candidates": [
                {
                    "id": "entry-primary",
                    "type": "attribute",
                    "raw_count": 0,
                    "visible_count": 0,
                    "actionable_count": 0,
                }
            ]
        },
    }
    assert "secret-selector" not in str(caught.value.locator)


def test_staged_runtime_error_preserves_safe_locator_diagnostics():
    locator = {
        "code": "element_candidate_not_found",
        "alias": "评论入口",
        "scope": "active_video",
        "diagnostics": {"candidates": []},
    }
    source = BlockExecutionError(
        "click",
        1,
        "click",
        "element_candidate_not_found",
        locator=locator,
    )

    error = StrategyRuntimeError(
        "execute_actions",
        str(source),
        source=source,
    )

    assert error.stage == "execute_actions"
    assert error.locator == locator
    assert error.locator_diagnostics == locator


def test_runtime_retains_only_safe_verified_switch_and_locator_measurements():
    async def execute(_page, item, _elements, _patterns, _resolver, **_kwargs):
        if item["type"] == "scroll_down":
            return {
                "action_id": item["id"],
                "type": item["type"],
                "status": "ok",
                "requested_switches": 3,
                "completed_switches": 3,
                "wheel_events": 23,
                "switches": [
                    {
                        "from": "a1b2c3d4e5f6",
                        "to": "c3d4e5f6a7b8",
                        "wheel_events": 8,
                        "full_fingerprint": "video:raw-video-id",
                    }
                ],
                "full_fingerprint": "video:raw-video-id",
                "video_id": "raw-video-id",
            }
        return {
            "action_id": item["id"],
            "type": item["type"],
            "status": "ok",
            "element": "entry",
            "locator": {
                "scope": "active_video",
                "candidate_id": "tiktok-comment-entry-primary",
                "candidate_type": "attribute",
                "selector": "xpath=//button[@data-secret='raw']",
            },
            "selector": "css=.raw-selector",
            "text": "private comment content",
            "outerHTML": "<button>private comment content</button>",
        }

    result = run(
        run_block_strategy(
            object(),
            strategy(
                action(
                    "scroll-1",
                    "scroll_down",
                    total_count=[3, 3],
                    interval_seconds=[0, 0],
                ),
                action("click-1", "click", element="entry"),
            ),
            {"entry": "//button"},
            [],
            lambda _item: "",
            execute_fn=execute,
        )
    )

    scroll_result, click_result = result["actions"]
    assert scroll_result["requested_switches"] == 3
    assert scroll_result["completed_switches"] == 3
    assert scroll_result["wheel_events"] == 23
    assert scroll_result["switches"] == [
        {"from": "a1b2c3d4e5f6", "to": "c3d4e5f6a7b8", "wheel_events": 8}
    ]
    assert click_result["locator"] == {
        "scope": "active_video",
        "candidate_id": "tiktok-comment-entry-primary",
        "candidate_type": "attribute",
    }
    serialized = str(result).casefold()
    for forbidden in (
        "selector",
        "full_fingerprint",
        "raw-video-id",
        "private comment content",
        "outerhtml",
    ):
        assert forbidden not in serialized


def test_video_switch_partial_measurements_reach_staged_runtime_error():
    partial_switches = [
        {"from": "a1b2c3d4e5f6", "to": "c3d4e5f6a7b8", "wheel_events": 8}
    ]

    async def execute(*_args, **_kwargs):
        raise VideoSwitchError(
            "video_switch_not_observed",
            requested_switches=3,
            completed_switches=1,
            wheel_events=17,
            switches=partial_switches,
            safe_fingerprint="e5f6",
        )

    with pytest.raises(BlockExecutionError) as caught:
        run(
            run_block_strategy(
                object(),
                strategy(
                    action(
                        "scroll-1",
                        "scroll_down",
                        total_count=[3, 3],
                        interval_seconds=[0, 0],
                    )
                ),
                {},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    staged = StrategyRuntimeError(
        "execute_actions",
        str(caught.value),
        source=caught.value,
    )

    assert staged.code == "video_switch_not_observed"
    assert staged.requested_switches == 3
    assert staged.completed_switches == 1
    assert staged.wheel_events == 17
    assert staged.switches == partial_switches
    assert not hasattr(staged, "safe_fingerprint")


def test_review_wave1_runtime_remasks_noncanonical_switch_identities():
    raw_numeric = "987654321012345678"
    raw_full_hash = "a" * 64

    async def execute(_page, item, *_args, **_kwargs):
        return {
            "action_id": item["id"],
            "type": item["type"],
            "status": "ok",
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 8,
            "switches": [
                {
                    "from": raw_numeric,
                    "to": raw_full_hash,
                    "wheel_events": 8,
                }
            ],
        }

    result = run(
        run_block_strategy(
            object(),
            strategy(
                action(
                    "scroll-1",
                    "scroll_down",
                    total_count=[1, 1],
                    interval_seconds=[0, 0],
                )
            ),
            {},
            [],
            lambda _item: "",
            execute_fn=execute,
        )
    )

    assert result["actions"][0]["switches"] == [
        {
            "from": hashlib.sha256(raw_numeric.encode()).hexdigest()[:12],
            "to": hashlib.sha256(raw_full_hash.encode()).hexdigest()[:12],
            "wheel_events": 8,
        }
    ]

    source = VideoSwitchError(
        "video_switch_not_observed",
        requested_switches=1,
        completed_switches=0,
        wheel_events=8,
        switches=[{"from": raw_numeric, "to": raw_full_hash, "wheel_events": 8}],
    )
    block = BlockExecutionError(
        "scroll-1",
        1,
        "scroll_down",
        str(source),
        source=source,
    )
    staged = StrategyRuntimeError("execute_actions", str(block), source=block)

    assert staged.switches == [
        {
            "from": hashlib.sha256(raw_numeric.encode()).hexdigest()[:12],
            "to": hashlib.sha256(raw_full_hash.encode()).hexdigest()[:12],
            "wheel_events": 8,
        }
    ]
    assert raw_numeric not in str(staged.switches)
    assert raw_full_hash not in str(staged.switches)


def test_needs_repair_is_rejected_before_execute_side_effect():
    calls = []

    async def execute(*_args, **_kwargs):
        calls.append(True)

    with pytest.raises(ValueError, match="needs repair"):
        run(
            run_block_strategy(
                object(),
                strategy(action("a"), status="needs_repair"),
                {},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    assert calls == []


def test_invalid_reference_is_rejected_before_execute_side_effect():
    calls = []

    async def execute(*_args, **_kwargs):
        calls.append(True)

    with pytest.raises(ValueError, match="missing element"):
        run(
            run_block_strategy(
                object(),
                strategy(action("click", "click", element="missing")),
                {},
                [],
                lambda _item: "",
                execute_fn=execute,
            )
        )

    assert calls == []


def test_strategy_uses_replacement_page_after_pause():
    first = object()
    second = object()
    pages = [first, second]
    calls = []

    class Lifecycle:
        async def execute(self, current, item, callback):
            selected = pages.pop(0)
            result = await callback(selected)
            event = (
                {
                    "action_id": item["id"],
                    "action_type": item["type"],
                    "old_page_origin": "https://www.tiktok.com:8443",
                    "new_page_origin": "https://www.tiktok.com",
                    "retry": 1,
                    "status": "recovered",
                }
                if selected is second
                else None
            )
            return selected, result, event

    async def execute(page, item, *_args, **_kwargs):
        calls.append((page, item["id"]))
        return {"action_id": item["id"], "status": "ok"}

    result = run(
        run_block_strategy(
            first,
            strategy(action("pause"), action("scroll", "scroll_down")),
            {},
            [],
            lambda _item: "",
            execute_fn=execute,
            page_lifecycle=Lifecycle(),
        )
    )

    assert calls == [(first, "pause"), (second, "scroll")]
    assert result["page_recoveries"] == [
        {
            "action_id": "scroll",
            "action_type": "scroll_down",
            "old_page_origin": "https://www.tiktok.com:8443",
            "new_page_origin": "https://www.tiktok.com",
            "retry": 1,
            "status": "recovered",
            "action_index": 2,
        }
    ]


def test_cdp_runner_uses_first_page_and_always_stops_playwright(monkeypatch):
    page = object()
    events = []

    class Chromium:
        async def connect_over_cdp(self, ws_url, timeout):
            events.append(("connect", ws_url, timeout))
            return SimpleNamespace(
                contexts=[SimpleNamespace(pages=[page])],
                close=lambda: events.append(("forbidden", "browser.close")),
            )

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            events.append(("stop",))

    class Starter:
        async def start(self):
            events.append(("start",))
            return Playwright()

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: Starter()
    )

    result = run_block_strategy_on_cdp(
        "ws://profile",
        strategy(action("pause", duration_seconds=[0, 0])),
        {},
        [],
        lambda _item: "",
    )

    assert result["status"] == "ok"
    assert events == [
        ("start",),
        ("connect", "ws://profile", 10_000),
        ("stop",),
    ]


def test_prepared_cdp_runner_prepares_before_actions_with_one_connection(
    monkeypatch,
):
    events = []

    class Mouse:
        async def move(self, x, y):
            events.append(("move", x, y))

        async def wheel(self, x, y):
            events.append(("wheel", x, y))

    class Page:
        def __init__(self):
            self.url = "about:blank"
            self.mouse = Mouse()

        def is_closed(self):
            return False

        async def evaluate(self, expression):
            if expression == "document.visibilityState":
                return "visible"
            video_index = sum(event[0] == "wheel" for event in events)
            return {
                "identity": f"video:{video_index}",
                "container_x": 0,
                "container_y": 0,
                "container_width": 360,
                "container_height": 945,
                "scroll_top": video_index * 945,
            }

        async def goto(self, url, **options):
            events.append(("goto", url, options))
            self.url = url

        async def wait_for_timeout(self, milliseconds):
            events.append(("wait_for_timeout", milliseconds))

    page = Page()

    class Context:
        def __init__(self):
            self.pages = [page]
            self.browser = None

        async def new_page(self):
            raise AssertionError("existing page must be reused")

    context = Context()

    class Browser:
        contexts = [context]

        def is_connected(self):
            return True

        async def close(self):
            events.append(("forbidden", "browser.close"))

    context.browser = Browser()

    class Chromium:
        async def connect_over_cdp(self, ws_url, timeout):
            events.append(("connect", ws_url, timeout))
            return context.browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            events.append(("stop",))

    class Starter:
        async def start(self):
            events.append(("start",))
            return Playwright()

    monkeypatch.setattr(
        "playwright.async_api.async_playwright", lambda: Starter()
    )

    result = run_prepared_block_strategy_on_cdp(
        "ws://profile",
        "https://www.tiktok.com/@target",
        strategy(
            action(
                "scroll",
                "scroll_down",
                total_count=[1, 1],
                burst_count=[1, 1],
                interval_seconds=[0, 0],
            )
        ),
        {},
        [],
        lambda _item: "",
    )

    prepared_before_actions = next(
        index for index, event in enumerate(events) if event[0] == "goto"
    ) < next(index for index, event in enumerate(events) if event[0] == "wheel")
    assert result["current_url"] == "https://www.tiktok.com/@target"
    assert events.count(("connect", "ws://profile", 10_000)) == 1
    assert events.count(("stop",)) == 1
    assert ("forbidden", "browser.close") not in events
    assert prepared_before_actions is True


@pytest.mark.parametrize(
    ("failure_stage", "expected_stage", "expected_current_url"),
    [
        ("connect", "connect", ""),
        ("prepare", "prepare_page", "https://example.com/before"),
        ("action", "execute_actions", "https://www.tiktok.com/@target"),
    ],
)
def test_prepared_cdp_runner_exposes_real_staged_failure_contract(
    monkeypatch,
    failure_stage,
    expected_stage,
    expected_current_url,
):
    events = []

    class Mouse:
        async def move(self, _x, _y):
            return None

        async def wheel(self, _x, _y):
            if failure_stage == "action":
                raise RuntimeError("wheel failed")

    class Page:
        def __init__(self):
            self.url = "https://example.com/before"
            self.mouse = Mouse()

        def is_closed(self):
            return False

        async def evaluate(self, expression):
            if expression == "document.visibilityState":
                return "visible"
            return {
                "identity": "video:0",
                "container_x": 0,
                "container_y": 0,
                "container_width": 360,
                "container_height": 945,
                "scroll_top": 0,
            }

        async def goto(self, url, **_options):
            if failure_stage == "prepare":
                raise RuntimeError("navigation failed")
            self.url = url

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def close(self):
            return None

    page = Page()
    context = SimpleNamespace(pages=[page])
    browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
    context.browser = browser

    class Chromium:
        async def connect_over_cdp(self, _ws_url, timeout):
            assert timeout == 10_000
            if failure_stage == "connect":
                raise RuntimeError(
                    "connect failed at ws://127.0.0.1:55001/devtools/browser/secret"
                )
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            events.append("stop")

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())

    with pytest.raises(StrategyRuntimeError) as caught:
        run_prepared_block_strategy_on_cdp(
            "ws://127.0.0.1:55001/devtools/browser/secret",
            "https://www.tiktok.com/@target",
            strategy(
                action(
                    "scroll",
                    "scroll_down",
                    total_count=[1, 1],
                    burst_count=[1, 1],
                    interval_seconds=[0, 0],
                )
            ),
            {},
            [],
            lambda _item: "",
        )

    error = caught.value
    assert error.stage == expected_stage
    assert error.current_url == expected_current_url
    assert error.reason in {
        "connect failed at [redacted-url]",
        "navigation failed",
        "action 1 (scroll_down, scroll) failed: video_switch_state_capture_failed",
    }
    assert "devtools/browser" not in error.reason
    assert events == ["stop"]


def test_strategy_runtime_error_redacts_normalized_sensitive_keys_and_paths():
    error = StrategyRuntimeError(
        "connect",
        (
            "failed at /access-token/ZXCV123/item "
            "#session_id=QWER456 session=ASDF789"
        ),
    )

    for secret in ("ZXCV123", "QWER456", "ASDF789"):
        assert secret not in error.reason
    assert error.stage == "connect"


@pytest.mark.parametrize(
    ("message", "secret_values"),
    [
        ("diagnostic #session%5Fid=ZXCV123; stage=connect", ("ZXCV123",)),
        ("diagnostic #%73ession%5Fid=ZXCV123; stage=connect", ("ZXCV123",)),
        ("diagnostic #session[]=QWER456; stage=connect", ("QWER456",)),
        ("diagnostic #access-token/ASDF789; stage=connect", ("ASDF789",)),
        ("diagnostic session[id]=ASDF789; stage=connect", ("ASDF789",)),
        (
            "diagnostic credential=COOKIE1, COOKIE2; stage=connect",
            ("COOKIE1", "COOKIE2"),
        ),
        (
            "diagnostic session=ONE123,TWO456; stage=connect",
            ("ONE123", "TWO456"),
        ),
        (
            'diagnostic Authorization Basic "ONE123 TWO456"; stage=connect',
            ("ONE123", "TWO456"),
        ),
        (
            "diagnostic api key 'ONE123 TWO456'; stage=connect",
            ("ONE123", "TWO456"),
        ),
    ],
)
def test_strategy_runtime_error_conservatively_projects_structured_secrets(
    message,
    secret_values,
):
    reason = StrategyRuntimeError("connect", message).reason

    for secret_value in secret_values:
        assert secret_value not in reason
    assert "diagnostic" in reason
    assert "stage=connect" in reason


@pytest.mark.parametrize(
    "key",
    [
        "session%5Fid",
        "%73ession%5Fid",
        "session[]",
        "session[id]",
        "access-token",
    ],
)
@pytest.mark.parametrize("separator", ["=", ":"])
@pytest.mark.parametrize("value_separator", [",", ";"])
def test_strategy_runtime_error_fuzzes_normalized_assignment_shapes(
    key,
    separator,
    value_separator,
):
    reason = StrategyRuntimeError(
        "connect",
        (
            f"diagnostic {key}{separator}ZXCV123"
            f"{value_separator}QWER456; stage=connect"
        ),
    ).reason

    assert "ZXCV123" not in reason
    assert "QWER456" not in reason
    assert "diagnostic" in reason
    assert "stage=connect" in reason


@pytest.mark.parametrize(
    "header_form",
    ["Cookie:", "Cookie=", "Authorization:", "Authorization="],
)
@pytest.mark.parametrize(
    "nested_key",
    ["status", "reason", "error", "message", "stage"],
)
@pytest.mark.parametrize("delimiter", [",", ";"])
@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_strategy_runtime_error_projects_complete_header_value(
    header_form,
    nested_key,
    delimiter,
    quote,
):
    random_secret = secrets.token_urlsafe(18)
    message = (
        f"diagnostic; {header_form} "
        f"{quote}session=FIXED{delimiter} {nested_key}={random_secret}{quote}"
    )

    reason = StrategyRuntimeError("connect", message).reason

    assert random_secret not in reason
    assert reason == f"diagnostic; {header_form} [redacted]"


@pytest.mark.parametrize(
    "header_form",
    ["Cookie:", "Cookie=", "Authorization:", "Authorization="],
)
@pytest.mark.parametrize(
    "status",
    ["missing", "expired", "invalid", "not configured"],
)
@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_strategy_runtime_error_preserves_only_complete_safe_header_value(
    header_form,
    status,
    quote,
):
    message = f"diagnostic; {header_form} {quote}{status}{quote}"

    assert StrategyRuntimeError("connect", message).reason == message


def test_strategy_runtime_error_redacts_scheme_prefixed_safe_header_value():
    reason = StrategyRuntimeError(
        "connect",
        "diagnostic; Authorization: Basic missing",
    ).reason

    assert reason == "diagnostic; Authorization: [redacted]"


def test_prepared_cdp_runner_classifies_playwright_import_failure_as_connect(
    monkeypatch,
):
    original_import = builtins.__import__

    def fail_playwright_import(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise RuntimeError("import failed; session=ZXCV123")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_playwright_import)

    with pytest.raises(StrategyRuntimeError) as caught:
        run_prepared_block_strategy_on_cdp(
            "ws://profile",
            "https://www.tiktok.com/@target",
            strategy(action("pause", duration_seconds=[0, 0])),
            {},
            [],
            lambda _item: "",
        )

    assert caught.value.stage == "connect"
    assert caught.value.current_url == ""
    assert "ZXCV123" not in caught.value.reason


def test_prepared_cdp_runner_classifies_playwright_start_failure_as_connect(
    monkeypatch,
):
    class Starter:
        async def start(self):
            raise RuntimeError("start failed; session=ZXCV123")

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())

    with pytest.raises(StrategyRuntimeError) as caught:
        run_prepared_block_strategy_on_cdp(
            "ws://profile",
            "https://www.tiktok.com/@target",
            strategy(action("pause", duration_seconds=[0, 0])),
            {},
            [],
            lambda _item: "",
        )

    assert caught.value.stage == "connect"
    assert caught.value.current_url == ""
    assert "ZXCV123" not in caught.value.reason


@pytest.mark.parametrize(
    ("failure_stage", "expected_stage", "primary_reason"),
    [
        ("connect", "connect", "connect failed"),
        ("prepare", "prepare_page", "navigation failed"),
        ("action", "execute_actions", "video_switch_state_capture_failed"),
    ],
)
def test_prepared_cdp_runner_preserves_primary_failure_when_stop_also_fails(
    monkeypatch,
    failure_stage,
    expected_stage,
    primary_reason,
):
    class Mouse:
        async def move(self, _x, _y):
            return None

        async def wheel(self, _x, _y):
            if failure_stage == "action":
                raise RuntimeError("wheel failed")

    class Page:
        def __init__(self):
            self.url = "https://example.com/before"
            self.mouse = Mouse()

        def is_closed(self):
            return False

        async def evaluate(self, expression):
            if expression == "document.visibilityState":
                return "visible"
            return {
                "identity": "video:0",
                "container_x": 0,
                "container_y": 0,
                "container_width": 360,
                "container_height": 945,
                "scroll_top": 0,
            }

        async def goto(self, url, **_options):
            if failure_stage == "prepare":
                raise RuntimeError("navigation failed")
            self.url = url

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def close(self):
            return None

    page = Page()
    context = SimpleNamespace(pages=[page])
    browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
    context.browser = browser

    class Chromium:
        async def connect_over_cdp(self, _ws_url, timeout):
            assert timeout == 10_000
            if failure_stage == "connect":
                raise RuntimeError("connect failed")
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            raise RuntimeError("cleanup failed; session=ZXCV123")

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())

    with pytest.raises(StrategyRuntimeError) as caught:
        run_prepared_block_strategy_on_cdp(
            "ws://profile",
            "https://www.tiktok.com/@target",
            strategy(
                action(
                    "scroll",
                    "scroll_down",
                    total_count=[1, 1],
                    burst_count=[1, 1],
                    interval_seconds=[0, 0],
                )
            ),
            {},
            [],
            lambda _item: "",
        )

    assert caught.value.stage == expected_stage
    assert primary_reason in caught.value.reason
    assert "cleanup failed" not in caught.value.reason
    assert "ZXCV123" not in caught.value.reason


def test_prepared_cdp_runner_wraps_stop_only_failure_as_cleanup(monkeypatch):
    class Page:
        url = "https://www.tiktok.com/@target"

        def is_closed(self):
            return False

        async def evaluate(self, _expression):
            return "visible"

        async def goto(self, url, **_options):
            self.url = url

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def close(self):
            return None

    page = Page()
    context = SimpleNamespace(pages=[page])
    browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
    context.browser = browser

    class Chromium:
        async def connect_over_cdp(self, _ws_url, timeout):
            assert timeout == 10_000
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            raise RuntimeError("cleanup failed; session=ZXCV123")

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())

    with pytest.raises(StrategyRuntimeError) as caught:
        run_prepared_block_strategy_on_cdp(
            "ws://profile",
            "https://www.tiktok.com/@target",
            strategy(action("pause", duration_seconds=[0, 0])),
            {},
            [],
            lambda _item: "",
        )

    assert caught.value.stage == "cleanup"
    assert caught.value.current_url == "https://www.tiktok.com/@target"
    assert "cleanup failed" in caught.value.reason
    assert "ZXCV123" not in caught.value.reason


def test_prepared_cdp_runner_attaches_failed_scroll_recovery_to_staged_error(
    monkeypatch,
):
    context = SimpleNamespace(pages=[])

    class ReplacementMouse:
        async def move(self, _x, _y):
            return None

        async def wheel(self, _x, _y):
            raise RuntimeError("replacement wheel failed")

    class OriginalMouse:
        async def move(self, _x, _y):
            return None

        async def wheel(self, _x, _y):
            context.pages.append(replacement)
            raise RuntimeError(
                "Mouse.wheel: Target page, context or browser has been closed"
            )

    class Page:
        def __init__(self, url, mouse):
            self.url = url
            self.mouse = mouse

        def is_closed(self):
            return False

        async def evaluate(self, expression):
            if expression == "document.visibilityState":
                return "visible"
            return {
                "identity": "video:0",
                "container_x": 0,
                "container_y": 0,
                "container_width": 360,
                "container_height": 945,
                "scroll_top": 0,
            }

        async def goto(self, url, **_options):
            self.url = url

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def close(self):
            return None

    original = Page("about:blank", OriginalMouse())
    replacement = Page(
        "https://www.tiktok.com/replacement",
        ReplacementMouse(),
    )
    context.pages.append(original)
    browser = SimpleNamespace(contexts=[context], is_connected=lambda: True)
    context.browser = browser

    class Chromium:
        async def connect_over_cdp(self, _ws_url, timeout):
            assert timeout == 10_000
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            return None

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())

    with pytest.raises(StrategyRuntimeError) as caught:
        run_prepared_block_strategy_on_cdp(
            "ws://profile",
            "https://www.tiktok.com/",
            strategy(
                action(
                    "scroll",
                    "scroll_down",
                    total_count=[1, 1],
                    burst_count=[1, 1],
                    interval_seconds=[0, 0],
                )
            ),
            {},
            [],
            lambda _item: "",
        )

    error = caught.value
    assert error.stage == "execute_actions"
    assert error.current_url == "https://www.tiktok.com/replacement"
    assert error.page_recoveries == [
        {
            "action_id": "scroll",
            "action_type": "scroll_down",
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "https://www.tiktok.com",
            "closure_type": "target_closed",
            "closure_reason": "target page, context or browser has been closed",
            "replacement_found": True,
            "retry": 1,
            "status": "failed",
            "outcome": "retry_failed",
        }
    ]
