import asyncio

import pytest

from browser_strategy_config import DEFAULT_ACTION_PARAMS
from browser_strategy_runtime import StrategyPausedError, run_block_strategy


def action(action_id):
    params = dict(DEFAULT_ACTION_PARAMS["click"])
    params["element"] = "target"
    return {"id": action_id, "type": "click", "params": params}


def strategy(*actions, run_mode="once"):
    value = {
        "id": "comment-flow",
        "name": "Comment flow",
        "run_mode": run_mode,
        "batch_size": 1,
        "actions": list(actions),
        "status": "ready",
    }
    if run_mode == "loop":
        value["loop_duration_minutes"] = [1, 1]
    return value


def elements():
    return {
        "target": {
            "scope": "page",
            "locators": [
                {
                    "id": "target",
                    "type": "css",
                    "value": "button",
                    "enabled": True,
                    "fallback": False,
                }
            ],
        }
    }


def run(coro):
    return asyncio.run(coro)


def test_gate_stops_before_first_action():
    executed = []

    def gate_check(_strategy_id, current_action):
        return {
            "allowed": current_action is None,
            "reasons": [
                {
                    "source": "probe",
                    "reason_code": "selector_validation_failed",
                }
            ],
        }

    async def execute_fn(*_args, **_kwargs):
        executed.append("executed")
        return {"status": "ok"}

    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=execute_fn,
            )
        )

    assert caught.value.action_id == "click-1"
    assert caught.value.action_index == 1
    assert caught.value.cycle == 1
    assert caught.value.completed_actions == []
    assert executed == []


def test_gate_appearing_after_click_never_retries_or_resumes_remainder():
    checks = 0
    executed = []

    def gate_check(_strategy_id, _action):
        nonlocal checks
        checks += 1
        return {
            "allowed": checks <= 3,
            "reasons": [] if checks <= 3 else [{"source": "manual"}],
        }

    async def execute_fn(_page, current_action, *_args, **kwargs):
        await kwargs["before_side_effect"]()
        executed.append(current_action["id"])
        return {"action_id": current_action["id"], "status": "ok"}

    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1"), action("click-2")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=execute_fn,
            )
        )

    assert checks == 4
    assert executed == ["click-1"]
    assert caught.value.action_id == "click-2"
    assert caught.value.action_index == 2
    assert caught.value.completed_actions[0]["action_id"] == "click-1"
    assert caught.value.reasons == [{"source": "manual"}]


def test_async_gate_is_checked_after_normalization_and_at_both_action_boundaries():
    checks = []
    executed = []

    async def gate_check(strategy_id, current_action):
        checks.append(
            (strategy_id, current_action["id"] if current_action else None)
        )
        return {"allowed": True, "reasons": []}

    async def execute_fn(_page, current_action, *_args, **kwargs):
        await kwargs["before_side_effect"]()
        executed.append(current_action["id"])
        return {"status": "ok"}

    result = run(
        run_block_strategy(
            page=object(),
            strategy=strategy(action("click-1")),
            elements=elements(),
            patterns=[],
            text_resolver=lambda *_: "",
            gate_check=gate_check,
            execute_fn=execute_fn,
        )
    )

    assert checks == [
        ("comment-flow", None),
        ("comment-flow", "click-1"),
        ("comment-flow", "click-1"),
    ]
    assert executed == ["click-1"]
    assert result["status"] == "ok"


def test_each_mutation_rechecks_gate_but_dispatch_hook_runs_once_per_action():
    checks = []
    dispatched = []
    mutations = []

    def gate_check(_strategy_id, current_action):
        checks.append(current_action["id"] if current_action else None)
        return {"allowed": True, "reasons": []}

    async def execute_fn(_page, current_action, *_args, **kwargs):
        for mutation in ("move", "click"):
            await kwargs["before_side_effect"]()
            mutations.append(mutation)
        return {"action_id": current_action["id"], "status": "ok"}

    result = run(
        run_block_strategy(
            page=object(),
            strategy=strategy(action("click-1")),
            elements=elements(),
            patterns=[],
            text_resolver=lambda *_: "",
            gate_check=gate_check,
            execute_fn=execute_fn,
            on_action_dispatch=lambda _strategy_id, current_action: (
                dispatched.append(current_action["id"])
            ),
        )
    )

    assert checks == [None, "click-1", "click-1", "click-1"]
    assert dispatched == ["click-1"]
    assert mutations == ["move", "click"]
    assert result["status"] == "ok"


def test_pause_inside_lifecycle_retry_boundary_prevents_duplicate_dispatch():
    checks = 0
    executed = []

    def gate_check(_strategy_id, _action):
        nonlocal checks
        checks += 1
        return {
            "allowed": checks <= 3,
            "reasons": [] if checks <= 3 else [{"source": "probe"}],
        }

    async def execute_fn(_page, current_action, *_args, **kwargs):
        await kwargs["before_side_effect"]()
        executed.append(current_action["id"])
        return {"status": "ok"}

    class Lifecycle:
        async def execute(self, page, _action, invoke):
            await invoke(page)
            try:
                await invoke(page)
            except StrategyPausedError as error:
                error.page_recoveries = [
                    {"status": "recovered", "outcome": "replacement"}
                ]
                raise

    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1"), action("never")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=execute_fn,
                page_lifecycle=Lifecycle(),
            )
        )

    assert executed == ["click-1"]
    assert caught.value.action_index == 1
    assert caught.value.completed_actions == []
    assert caught.value.page_recoveries == [{"status": "recovered"}]


def test_pause_reasons_are_bounded_and_drop_unknown_sensitive_fields():
    def gate_check(_strategy_id, current_action):
        return {
            "allowed": current_action is None,
            "reasons": [
                {
                    "source": "probe",
                    "reason_code": "selector_validation_failed",
                    "aliases": ["target"],
                    "selector_version_id": "sel-old",
                    "cookie": "secret",
                    "detail": "private page content",
                }
            ],
        }

    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=lambda *_args, **_kwargs: None,
            )
        )

    assert caught.value.reasons == [
        {
            "source": "probe",
            "reason_code": "selector_validation_failed",
            "aliases": ["target"],
            "selector_version_id": "sel-old",
        }
    ]
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "gate_result",
    [
        None,
        {},
        {"allowed": "yes", "reasons": []},
        {"allowed": False, "reasons": [{"cookie": "secret"}]},
    ],
)
def test_malformed_gate_decision_fails_closed_with_registry_unavailable(
    gate_result,
):
    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=lambda *_: gate_result,
                execute_fn=lambda *_args, **_kwargs: None,
            )
        )

    assert caught.value.action_index == 0
    assert caught.value.reasons == [
        {"source": "probe", "reason_code": "registry_unavailable"}
    ]


def test_gate_callback_exception_is_structured_pause_not_block_error():
    def gate_check(_strategy_id, _action):
        raise RuntimeError("redis password=private")

    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=lambda *_args, **_kwargs: None,
            )
        )

    assert caught.value.action_index == 0
    assert caught.value.reasons == [
        {"source": "probe", "reason_code": "registry_unavailable"}
    ]
    assert "private" not in str(caught.value)


def test_pause_page_recoveries_drop_unknown_and_sensitive_fields():
    checks = 0

    def gate_check(_strategy_id, _action):
        nonlocal checks
        checks += 1
        return {
            "allowed": checks <= 2,
            "reasons": [] if checks <= 2 else [{"source": "manual"}],
        }

    class Lifecycle:
        async def execute(self, page, _action, invoke):
            try:
                await invoke(page)
            except StrategyPausedError as error:
                error.page_recoveries = [
                    {
                        "action_id": "click-1",
                        "action_type": "click",
                        "old_page_origin": "https://www.tiktok.com",
                        "new_page_origin": "",
                        "closure_type": "page_closed",
                        "closure_reason": "page closed",
                        "replacement_found": False,
                        "retry": 0,
                        "status": "failed",
                        "outcome": "not_retried",
                        "cookie": "secret",
                        "raw_error": "private",
                    }
                ]
                raise

    async def execute_fn(_page, _current_action, *_args, **kwargs):
        await kwargs["before_side_effect"]()
        return {"status": "ok"}

    with pytest.raises(StrategyPausedError) as caught:
        run(
            run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=execute_fn,
                page_lifecycle=Lifecycle(),
            )
        )

    assert caught.value.page_recoveries == [
        {
            "action_id": "click-1",
            "action_type": "click",
            "old_page_origin": "https://www.tiktok.com",
            "new_page_origin": "",
            "closure_type": "page_closed",
            "closure_reason": "page closed",
            "replacement_found": False,
            "retry": 0,
            "status": "failed",
            "outcome": "not_retried",
        }
    ]
    assert "secret" not in str(caught.value.page_recoveries)
