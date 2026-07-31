from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import pytest

from selector_probe.healing_runtime import HealingRuntime
from selector_probe.probe import run_healing_probe
from selector_probe.snapshot import SemanticNode, SemanticSnapshot
from selector_probe.store import SelectorProbeStore, _validated_bundle
from selector_probe.validator import ValidationRejected


def test_failure_screenshot_is_scoped_and_redacts_editable_regions(
    monkeypatch,
    tmp_path,
):
    page = object()
    contract = SimpleNamespace(
        required_state="feed_ready",
        accepted_roles=("button",),
        accepted_names=("comments",),
        name_mode="exact",
    )
    button = SemanticNode(
        backend_node_id=1,
        parent_backend_node_id=None,
        tag="button",
        role="button",
        name="comments",
        states={},
        attributes={},
        bounds=(100.0, 100.0, 50.0, 20.0),
        visible=True,
        in_viewport=True,
        actionable=True,
    )
    editable = SemanticNode(
        backend_node_id=2,
        parent_backend_node_id=None,
        tag="input",
        role="textbox",
        name="comment-input",
        states={"editable": True},
        attributes={"contenteditable": "true"},
        bounds=(105.0, 105.0, 20.0, 10.0),
        visible=True,
        in_viewport=True,
        actionable=True,
    )
    snapshot = SemanticSnapshot(
        nodes=(button, editable),
        viewport=(400, 300),
    )
    captured = {}

    async def capture(
        selected_page,
        regions,
        target_path,
        *,
        evidence_root,
    ):
        captured.update(
            page=selected_page,
            regions=regions,
            target_path=target_path,
            evidence_root=evidence_root,
        )
        return target_path

    monkeypatch.setattr(
        "selector_probe.healing_runtime.capture_redacted_screenshot",
        capture,
    )
    runtime = HealingRuntime.__new__(HealingRuntime)
    runtime.contracts = {"comment-entry": contract}
    runtime._page_handles = [SimpleNamespace(page=page)]
    runtime._latest_snapshots = {
        (id(page), "feed_ready"): snapshot,
    }
    runtime._run = lambda awaitable: asyncio.run(awaitable)

    target = tmp_path / "failure.jpg"
    result = runtime.capture_failure_screenshot(
        failed_aliases=("comment-entry",),
        target_path=target,
        evidence_root=tmp_path,
    )

    assert result == target
    assert captured["page"] is page
    assert {
        "x": 105.0,
        "y": 105.0,
        "width": 20.0,
        "height": 10.0,
    } in captured["regions"]
    assert len(captured["regions"]) == 5


def bundle_evidence(bundle):
    aliases = {
        alias: {
            "status": "ok",
            "candidate_id": definition["locators"][0]["id"],
        }
        for alias, definition in bundle["elements"].items()
    }
    validations = []
    for round_number in (1, 2):
        for profile_number in range(2):
            marker = f"{profile_number}:{round_number}"
            validations.append(
                {
                    "profile_mask": f"***p{profile_number:03d}",
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:"
                    + hashlib.sha256(f"reset:{marker}".encode()).hexdigest(),
                    "snapshot_hash": "sha256:"
                    + hashlib.sha256(
                        f"snapshot:{marker}".encode()
                    ).hexdigest(),
                    "page_generation": "sha256:"
                    + hashlib.sha256(
                        f"generation:{marker}".encode()
                    ).hexdigest(),
                    "aliases": copy.deepcopy(aliases),
                }
            )
    return {
        "status": "passed",
        "bundle_hash": bundle["bundle_hash"],
        "profiles_passed": 2,
        "rounds_passed": 2,
        "validations": validations,
    }


class FakeSessionManager:
    def __init__(self, _client, *, allowed_profile_ids, **_kwargs):
        self.allowed = tuple(allowed_profile_ids)
        self.events = _client.events
        self.handles = []
        self.pages = []

    def open_profiles(self, profile_ids):
        assert tuple(profile_ids) == self.allowed
        self.events.append("profiles:open")
        self.handles = [
            SimpleNamespace(
                profile_id=profile_id,
                profile_mask=f"***p{index:03d}",
                ws_url=f"ws://test-{index}",
                started_by_probe=True,
            )
            for index, profile_id in enumerate(profile_ids)
        ]
        return self.handles

    async def open_probe_page(self, _playwright, handle):
        self.events.append(f"page:open:{handle.profile_mask}")
        page = SimpleNamespace(url="https://www.tiktok.com/")
        page_handle = SimpleNamespace(profile=handle, page=page)
        self.pages.append(page_handle)
        return page_handle

    async def close_owned_pages(self, pages):
        self.events.append("pages:close")
        assert list(pages) == self.pages
        return [
            {
                "profile_mask": item.profile.profile_mask,
                "stage": "close_page",
                "ok": True,
                "code": "",
            }
            for item in pages
        ]

    def stop_owned_profiles(self, handles):
        self.events.append("profiles:stop")
        assert list(handles) == self.handles
        return [
            {
                "profile_mask": item.profile_mask,
                "stage": "stop_profile",
                "ok": True,
                "code": "",
            }
            for item in handles
        ]


class FakePlaywright:
    def __init__(self, events):
        self.events = events

    async def stop(self):
        self.events.append("playwright:stop")


class FakeRunner:
    def __init__(self, events):
        self.events = events

    async def ensure_state(self, _page, state, _elements):
        self.events.append(f"state:{state}")
        return {"state": state}


class FakeStore:
    def __init__(self, events):
        self.events = events
        self.saved = None

    def seed_contracts(self, contracts):
        self.events.append("contracts:seed")
        self.contracts = dict(contracts)

    def list_contracts(self, **_kwargs):
        return {
            alias: {
                key: value
                for key, value in contract.items()
                if key not in {"site", "environment", "enabled"}
            }
            for alias, contract in self.contracts.items()
        }

    def store_validated_version(self, **payload):
        self.events.append("version:store")
        self.saved = payload
        return "sel-new"

    def get_version(self, version):
        assert version == "sel-new"
        return {"id": version, "status": "published"}


class FakeRegistry:
    def __init__(self, events, active=None):
        self.events = events
        self.active = active

    def get_active(self):
        self.events.append("registry:active")
        return self.active


def test_real_runtime_runs_deterministic_full_validation_publish_and_cleanup():
    events = []
    client = SimpleNamespace(events=events)
    store = FakeStore(events)
    registry = FakeRegistry(events)
    config = SimpleNamespace(
        target_url="https://www.tiktok.com/",
        test_profile_ids=("dedicated-a", "dedicated-b"),
        model_id="",
        site="tiktok",
        environment="production",
    )

    async def start_playwright():
        events.append("playwright:start")
        return FakePlaywright(events)

    async def snapshot_extractor(_page):
        events.append("snapshot")
        return SemanticSnapshot(nodes=())

    def candidate_generator(contract, snapshot, historical):
        assert isinstance(snapshot, SemanticSnapshot)
        events.append(f"candidate:{contract.alias}")
        return [
            {
                "id": f"candidate-{len(events)}",
                "type": "role",
                "role": contract.accepted_roles[0],
                "name": contract.accepted_names[0],
                "name_mode": (
                    contract.name_mode
                    if contract.name_mode in {"exact", "contains"}
                    else "exact"
                ),
                "enabled": True,
            }
        ]

    async def page_validator(_page, bundle, contracts, _runner):
        events.append("candidate:validate")
        return {
            "status": "passed",
            "bundle_hash": bundle["bundle_hash"],
            "aliases": {
                alias: {
                    "status": "ok",
                    "candidate_id": definition["locators"][0]["id"],
                }
                for alias, definition in bundle["elements"].items()
            },
            "actions": [
                contract.probe_action
                for contract in contracts.values()
                if contract.probe_action != "inspect_only"
            ],
        }

    async def full_validator(
        *,
        handles,
        bundle,
        contracts,
        inspect_fn,
        reset_fn,
    ):
        events.append("task4:validate_two_rounds")
        assert len(handles) == 2
        assert callable(inspect_fn)
        assert callable(reset_fn)
        assert set(contracts) == set(bundle["elements"])
        return bundle_evidence(bundle)

    def reconciler(selected_store, selected_registry):
        assert selected_store is store
        assert selected_registry is registry
        events.append("registry:reconcile")
        return {"acknowledged": 1, "version": "sel-new"}

    runtime = HealingRuntime(
        config=config,
        settings={"selector_probe": {}},
        store=store,
        registry=registry,
        adspower_client=client,
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=start_playwright,
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=snapshot_extractor,
        candidate_generator=candidate_generator,
        page_validator=page_validator,
        full_validator=full_validator,
        reconciler=reconciler,
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        result = run_healing_probe(runtime)

    assert result["status"] == "published", result
    assert result["new_version"] == "sel-new"
    assert store.saved["evidence"]["profiles_passed"] == 2
    assert events.index("task4:validate_two_rounds") < events.index(
        "version:store"
    )
    assert events.index("version:store") < events.index(
        "registry:reconcile"
    )
    assert events[-3:] == [
        "pages:close",
        "playwright:stop",
        "profiles:stop",
    ]
    assert "dedicated-a" not in json.dumps(result)


def test_forced_element_validation_publishes_draft_when_active_is_healthy():
    calls = []
    candidate, _bundle_hash = _validated_bundle({
        "elements": {
            "draft-element": {
                "scope": "active_video",
                "locators": [
                    {
                        "id": "draft-primary",
                        "type": "attribute",
                        "name": "data-e2e",
                        "value": "draft-element",
                        "enabled": True,
                    }
                ],
            }
        }
    })

    class Runtime:
        model_call = None

        def validate_active(self):
            calls.append("active")
            return {"status": "healthy", "evidence": {"status": "passed"}}

        def deterministic_candidates(self, **_kwargs):
            calls.append("deterministic")
            return candidate

        def fresh_validation_context(self, **_kwargs):
            raise AssertionError("repair context should not be needed")

        def validate_candidate(self, value):
            assert value is candidate
            calls.append("candidate")
            return {"status": "passed"}

        def repair_candidate(self, **_kwargs):
            raise AssertionError("repair should not be needed")

        def full_validate(self, value):
            assert value is candidate
            calls.append("full")
            return bundle_evidence(value)

        def store_and_publish(self, value, evidence):
            assert value is candidate
            assert evidence["profiles_passed"] == 2
            calls.append("publish")
            return {
                "version": "sel-draft",
                "published": True,
                "reconciled": True,
            }

    result = run_healing_probe(
        Runtime(),
        force_requested_candidate=True,
        initial_failed_aliases=("draft-element",),
    )

    assert result["status"] == "published"
    assert result["published"] is True
    assert result["reconciled"] is True
    assert result["new_version"] == "sel-draft"
    assert calls == [
        "active",
        "deterministic",
        "candidate",
        "full",
        "publish",
    ]


def test_deterministic_candidate_failure_preserves_safe_error_code():
    class CandidateUnavailable(RuntimeError):
        code = "semantic_snapshot_empty"

    runtime = SimpleNamespace(
        model_call=None,
        validate_active=lambda: {
            "status": "failed",
            "failure_class": "selector",
            "failed_aliases": ["评论入口"],
        },
        deterministic_candidates=lambda **_kwargs: (
            _ for _ in ()
        ).throw(CandidateUnavailable()),
        fresh_validation_context=lambda **_kwargs: {},
        validate_candidate=lambda _value: {},
        repair_candidate=lambda **_kwargs: {},
        full_validate=lambda _value: {},
        store_and_publish=lambda _value, _evidence: {},
    )

    result = run_healing_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "semantic_snapshot_empty"


def test_runtime_cleanup_runs_when_healing_raises():
    events = []
    client = SimpleNamespace(events=events)
    config = SimpleNamespace(
        target_url="https://www.tiktok.com/",
        test_profile_ids=("dedicated-a", "dedicated-b"),
        model_id="",
        site="tiktok",
        environment="production",
    )
    runtime = HealingRuntime(
        config=config,
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events),
        adspower_client=client,
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        candidate_generator=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("boom")
        ),
        wait_for_cdp=lambda _url: True,
    )

    try:
        with runtime:
            runtime.deterministic_candidates()
    except RuntimeError as error:
        assert str(error) == "boom"

    assert events[-3:] == [
        "pages:close",
        "playwright:stop",
        "profiles:stop",
    ]


async def _async_value(value):
    return value


def test_active_health_uses_task4_two_profile_two_round_validation():
    events = []
    client = SimpleNamespace(events=events)
    registry = FakeRegistry(events)
    config = SimpleNamespace(
        target_url="https://www.tiktok.com/",
        test_profile_ids=("dedicated-a", "dedicated-b"),
        model_id="",
        site="tiktok",
        environment="production",
    )

    async def full_validator(**kwargs):
        events.append("task4:active")
        assert len(kwargs["handles"]) == 2
        return bundle_evidence(kwargs["bundle"])

    runtime = HealingRuntime(
        config=config,
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=registry,
        adspower_client=client,
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        candidate_generator=lambda *_args: (_ for _ in ()).throw(
            AssertionError("healthy active must not generate")
        ),
        full_validator=full_validator,
        wait_for_cdp=lambda _url: True,
    )
    elements = {}
    for index, (alias, contract) in enumerate(runtime.contracts.items()):
        elements[alias] = {
            "scope": contract.scope,
            "locators": [
                {
                    "id": f"active-{index}",
                    "type": "role",
                    "role": contract.accepted_roles[0],
                    "name": contract.accepted_names[0],
                    "name_mode": (
                        contract.name_mode
                        if contract.name_mode in {"exact", "contains"}
                        else "exact"
                    ),
                    "enabled": True,
                }
            ],
        }
    encoded = json.dumps(
        elements,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    registry.active = {
        "version": "sel-active",
        "bundle_hash": "sha256:"
        + hashlib.sha256(encoded.encode()).hexdigest(),
        "elements": elements,
    }

    with runtime:
        result = run_healing_probe(runtime)

    assert result["status"] == "healthy"
    assert result["validation_evidence"]["profiles_passed"] == 2
    assert len(result["validation_evidence"]["validations"]) == 4
    assert events.count("task4:active") == 1
    assert not any(item.startswith("candidate:") for item in events)


def test_fresh_context_uses_failed_alias_required_state_not_global_panel():
    events = []
    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        runtime.fresh_validation_context(failed_aliases=("评论入口",))

    assert "state:feed_ready" in events
    assert "state:comment_panel_open" not in events


def test_validation_failure_preserves_alias_code_count_and_state():
    runtime = HealingRuntime.__new__(HealingRuntime)
    runtime.contracts = {}
    failure = runtime._validation_failure(
        ValidationRejected(
            "multiple_match",
            alias="评论入口",
            match_count=2,
            required_state="feed_ready",
        )
    )

    assert failure == {
        "status": "failed",
        "failure_class": "selector",
        "failed_aliases": ["评论入口"],
        "code": "multiple_match",
        "match_count": 2,
        "required_state": "feed_ready",
    }


def test_runtime_classifies_comment_readiness_as_infrastructure():
    runtime = HealingRuntime.__new__(HealingRuntime)
    runtime.contracts = {
        "feed": SimpleNamespace(required_state="feed_ready"),
        "comment-input": SimpleNamespace(
            required_state="comment_panel_open"
        ),
        "comment-submit": SimpleNamespace(
            required_state="comment_panel_open"
        ),
    }

    result = runtime._validation_failure(
        ValidationRejected(
            "comment_panel_readiness_timeout",
            required_state="comment_panel_open",
        )
    )

    assert result == {
        "status": "failed",
        "failure_class": "infrastructure",
        "failed_aliases": ["comment-input", "comment-submit"],
        "code": "comment_panel_readiness_timeout",
        "required_state": "comment_panel_open",
    }


def test_comment_panel_element_missing_remains_selector_failure():
    runtime = HealingRuntime.__new__(HealingRuntime)
    runtime.contracts = {}

    result = runtime._validation_failure(
        ValidationRejected(
            "comment_panel_element_missing",
            alias="comment-submit",
            required_state="comment_panel_open",
        )
    )

    assert result["failure_class"] == "selector"
    assert result["failed_aliases"] == ["comment-submit"]


@pytest.mark.parametrize(
    "failure_code",
    (
        "comment_panel_readiness_timeout",
        "comment_panel_snapshot_unstable",
    ),
)
def test_comment_readiness_failure_never_calls_repair_or_model(
    failure_code,
):
    calls = []
    runtime = SimpleNamespace(
        model_call=lambda *_args, **_kwargs: calls.append("model"),
        validate_active=lambda: {
            "status": "failed",
            "failure_class": "infrastructure",
            "failed_aliases": ["comment-input", "comment-submit"],
            "code": failure_code,
            "required_state": "comment_panel_open",
        },
        deterministic_candidates=lambda **_kwargs: calls.append(
            "deterministic"
        ),
        fresh_validation_context=lambda **_kwargs: {},
        validate_candidate=lambda _value: {},
        repair_candidate=lambda **_kwargs: calls.append("repair"),
        full_validate=lambda _value: {},
        store_and_publish=lambda _value, _evidence: {},
    )

    result = run_healing_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == failure_code
    assert result["required_state"] == "comment_panel_open"
    assert result["proposed_pause_aliases"] == [
        "comment-input",
        "comment-submit",
    ]
    assert calls == []


def test_all_alias_failure_narrows_to_submit_and_uses_panel_context():
    events = []
    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events, active=None),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        candidate_generator=lambda contract, *_args: (
            []
            if "submit button" in contract.intent
            else [
                {
                    "id": f"{contract.alias}-role",
                    "type": "role",
                    "role": contract.accepted_roles[0],
                    "name": contract.accepted_names[0],
                    "name_mode": "exact",
                    "enabled": True,
                }
            ]
        ),
        wait_for_cdp=lambda _url: True,
    )
    submit_alias = next(
        alias
        for alias, contract in runtime.contracts.items()
        if "submit button" in contract.intent
    )

    with runtime:
        result = run_healing_probe(
            runtime,
            repair_fn=lambda **_kwargs: None,
        )

    states = [
        event.removeprefix("state:")
        for event in events
        if event.startswith("state:")
    ]
    assert result["proposed_pause_aliases"] == [submit_alias]
    assert states[-3:] == ["comment_panel_open"] * 3


def test_partial_deterministic_bundle_merges_successful_submit_repair():
    events = []
    store = FakeStore(events)
    registry = FakeRegistry(events, active=None)
    repaired = []
    full_results = []

    def locator(contract, marker):
        return {
            "id": (
                f"{marker}-"
                + hashlib.sha256(contract.alias.encode()).hexdigest()[:8]
            ),
            "type": "role",
            "role": contract.accepted_roles[0],
            "name": contract.accepted_names[0],
            "name_mode": "exact",
            "enabled": True,
        }

    def deterministic(contract, *_args):
        return [] if "submit button" in contract.intent else [
            locator(contract, "deterministic")
        ]

    def repair(contract, _snapshot, _history, _failure, _attempt, _call):
        repaired.append(contract.alias)
        return [locator(contract, "repaired")]

    async def page_validator(_page, bundle, contracts, _runner):
        return {
            "status": "passed",
            "bundle_hash": bundle["bundle_hash"],
            "aliases": {
                alias: {
                    "status": "ok",
                    "candidate_id": definition["locators"][0]["id"],
                }
                for alias, definition in bundle["elements"].items()
            },
            "actions": [
                contract.probe_action
                for contract in contracts.values()
                if contract.probe_action != "inspect_only"
            ],
        }

    async def full_validator(**kwargs):
        evidence = bundle_evidence(kwargs["bundle"])
        full_results.append((kwargs["bundle"], evidence))
        return evidence

    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=store,
        registry=registry,
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        candidate_generator=deterministic,
        repair_generator=repair,
        page_validator=page_validator,
        full_validator=full_validator,
        reconciler=lambda *_args: {
            "acknowledged": 1,
            "version": "sel-new",
        },
        wait_for_cdp=lambda _url: True,
    )
    submit_alias = next(
        alias
        for alias, contract in runtime.contracts.items()
        if "submit button" in contract.intent
    )

    with runtime:
        result = run_healing_probe(runtime)

    assert result["status"] == "published", result
    assert repaired == [submit_alias]
    assert set(store.saved["bundle"]["elements"]) == set(runtime.contracts)


def test_real_store_order_middle_failure_keeps_later_successes_and_publishes(
    tmp_path,
):
    events = []

    class PublishingStore(SelectorProbeStore):
        def store_validated_version(self, **payload):
            self.saved = payload
            return "sel-new"

        def get_version(self, version):
            return {"id": version, "status": "published"}

    def locator(contract, marker):
        return {
            "id": (
                f"{marker}-"
                + hashlib.sha256(contract.alias.encode()).hexdigest()[:8]
            ),
            "type": "role",
            "role": contract.accepted_roles[0],
            "name": contract.accepted_names[0],
            "name_mode": "exact",
            "enabled": True,
        }

    with PublishingStore(tmp_path / "probe.db") as store:
        runtime = HealingRuntime(
            config=SimpleNamespace(
                target_url="https://www.tiktok.com/",
                test_profile_ids=("dedicated-a", "dedicated-b"),
                model_id="",
                site="tiktok",
                environment="production",
            ),
            settings={"selector_probe": {}},
            store=store,
            registry=FakeRegistry(events, active=None),
            adspower_client=SimpleNamespace(events=events),
            elements={},
            session_manager_factory=FakeSessionManager,
            playwright_starter=lambda: _async_value(FakePlaywright(events)),
            state_runner_factory=lambda **_kwargs: FakeRunner(events),
            snapshot_extractor=lambda _page: _async_value(
                SemanticSnapshot(nodes=())
            ),
            wait_for_cdp=lambda _url: True,
        )
        ordered_aliases = sorted(runtime.contracts)
        failed_alias = ordered_aliases[1]
        runtime.candidate_generator = (
            lambda contract, *_args: (
                []
                if contract.alias == failed_alias
                else [locator(contract, "deterministic")]
            )
        )
        repaired = []
        runtime.repair_generator = (
            lambda contract, *_args: (
                repaired.append(contract.alias)
                or [locator(contract, "repaired")]
            )
        )

        async def page_validator(_page, bundle, contracts, _runner):
            return {
                "status": "passed",
                "bundle_hash": bundle["bundle_hash"],
                "aliases": {
                    alias: {
                        "status": "ok",
                        "candidate_id": definition["locators"][0]["id"],
                    }
                    for alias, definition in bundle["elements"].items()
                },
                "actions": [
                    contract.probe_action
                    for contract in contracts.values()
                    if contract.probe_action != "inspect_only"
                ],
            }

        runtime.page_validator = page_validator
        runtime.full_validator = lambda **kwargs: _async_value(
            bundle_evidence(kwargs["bundle"])
        )
        runtime.reconciler = lambda *_args: {
            "acknowledged": 1,
            "version": "sel-new",
        }

        with runtime:
            result = run_healing_probe(runtime)

        assert result["status"] == "published", result
        assert repaired == [failed_alias]
        assert set(store.saved["bundle"]["elements"]) == set(ordered_aliases)
        assert store.saved["bundle"]["elements"][
            ordered_aliases[2]
        ]["locators"][0]["id"].startswith("deterministic-")


def test_lease_loss_stops_browser_work_and_cleans_up_without_publish():
    events = []
    lost = {"value": False}
    candidates = []

    def lease_guard(*, renew=False):
        events.append(f"lease:{renew}")
        if lost["value"]:
            raise RuntimeError("probe_lease_lost")

    def candidate_generator(contract, *_args):
        candidates.append(contract.alias)
        lost["value"] = True
        return [
            {
                "id": "candidate-safe",
                "type": "role",
                "role": contract.accepted_roles[0],
                "name": contract.accepted_names[0],
                "name_mode": "exact",
                "enabled": True,
            }
        ]

    store = FakeStore(events)
    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=store,
        registry=FakeRegistry(events, active=None),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        candidate_generator=candidate_generator,
        lease_guard=lease_guard,
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        result = run_healing_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert candidates and len(candidates) == 1
    assert "version:store" not in events
    assert events[-3:] == [
        "pages:close",
        "playwright:stop",
        "profiles:stop",
    ]


def test_lease_loss_cancels_hanging_async_capture_immediately():
    events = []
    lost = threading.Event()

    def lease_guard(*, renew=False):
        if lost.is_set():
            raise RuntimeError("probe_lease_lost")

    class HangingRunner(FakeRunner):
        async def ensure_state(self, _page, _state, _elements):
            await asyncio.Event().wait()

    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: HangingRunner(events),
        lease_guard=lease_guard,
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        timer = threading.Timer(0.05, lost.set)
        timer.start()
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="probe_lease_lost"):
            runtime.fresh_validation_context()
        elapsed = time.monotonic() - started
        timer.join()

    assert elapsed < 0.5
    assert events[-3:] == [
        "pages:close",
        "playwright:stop",
        "profiles:stop",
    ]


def test_lease_loss_abandons_hanging_model_request_immediately():
    events = []
    lost = threading.Event()
    release_request = threading.Event()

    def lease_guard(*, renew=False):
        if lost.is_set():
            raise RuntimeError("probe_lease_lost")

    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="model",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        model_selector=lambda *_args: object(),
        model_request=lambda *_args: release_request.wait(5),
        lease_guard=lease_guard,
        wait_for_cdp=lambda _url: True,
    )

    try:
        with runtime:
            timer = threading.Timer(0.05, lost.set)
            timer.start()
            started = time.monotonic()
            with pytest.raises(RuntimeError, match="probe_lease_lost"):
                runtime.model_call([], {})
            elapsed = time.monotonic() - started
            timer.join()
    finally:
        release_request.set()

    assert elapsed < 0.5
    assert events[-3:] == [
        "pages:close",
        "playwright:stop",
        "profiles:stop",
    ]


def test_lease_loss_after_store_cancels_outbox_before_reconcile():
    events = []
    lost = {"value": False}

    def lease_guard(*, renew=False):
        if lost["value"]:
            raise RuntimeError("probe_lease_lost")

    class FencedStore(FakeStore):
        def store_validated_version(self, **payload):
            version = super().store_validated_version(**payload)
            lost["value"] = True
            return version

        def cancel_validated_version(self, version, **fence):
            events.append(("version:cancel", version, fence))
            return True

    reconciled = []
    store = FencedStore(events)
    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=store,
        registry=FakeRegistry(events),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        reconciler=lambda *_args: reconciled.append(True),
        lease_guard=lease_guard,
        probe_run_id=7,
        attempt_token="owner-token",
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        elements = {
            alias: {
                "scope": contract.scope,
                "locators": [
                    {
                        "id": "fenced-" + hashlib.sha256(
                            alias.encode()
                        ).hexdigest()[:8],
                        "type": "role",
                        "role": contract.accepted_roles[0],
                        "name": contract.accepted_names[0],
                        "name_mode": "exact",
                        "enabled": True,
                    }
                ],
            }
            for alias, contract in runtime.contracts.items()
        }
        candidate = {"elements": elements}
        with pytest.raises(RuntimeError, match="probe_lease_lost"):
            runtime.store_and_publish(candidate, {})

    assert reconciled == []
    assert ("version:cancel", "sel-new", {
        "probe_run_id": 7,
        "attempt_token": "owner-token",
    }) in events


def test_two_failed_states_use_each_alias_own_fresh_snapshot():
    events = []
    seen = {}

    class StateRunner(FakeRunner):
        async def ensure_state(self, page, state, _elements):
            page.current_state = state
            return await super().ensure_state(page, state, _elements)

    async def snapshot_extractor(page):
        return SemanticSnapshot(nodes=(), scope=page.current_state)

    def repair(contract, snapshot, *_args):
        seen[contract.alias] = snapshot.scope
        return [
                {
                    "id": "repair-" + hashlib.sha256(
                        contract.alias.encode()
                    ).hexdigest()[:8],
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "safe-" + hashlib.sha256(
                        contract.alias.encode()
                    ).hexdigest()[:8],
                    "enabled": True,
                }
        ]

    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: StateRunner(events),
        snapshot_extractor=snapshot_extractor,
        repair_generator=repair,
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        feed_alias = next(
            alias
            for alias, contract in runtime.contracts.items()
            if contract.required_state == "feed_ready"
        )
        panel_alias = next(
            alias
            for alias, contract in runtime.contracts.items()
            if contract.required_state == "comment_panel_open"
        )
        elements = {
            alias: {
                "scope": contract.scope,
                "locators": [
                    {
                        "id": "old-" + hashlib.sha256(
                            alias.encode()
                        ).hexdigest()[:8],
                        "type": "role",
                        "role": contract.accepted_roles[0],
                        "name": contract.accepted_names[0],
                        "name_mode": "exact",
                        "enabled": True,
                    }
                ],
            }
            for alias, contract in runtime.contracts.items()
        }
        context = runtime.fresh_validation_context(
            failed_aliases=(feed_alias, panel_alias),
        )
        candidate = runtime.repair_candidate(
            attempt=1,
            context=context,
            failure={
                "failed_aliases": [feed_alias, panel_alias],
                "code": "zero_match",
                "match_count": 0,
            },
            previous_candidate={"elements": elements},
            model_call=lambda *_args: {},
        )

    assert candidate is not None
    assert seen == {
        feed_alias: "feed_ready",
        panel_alias: "comment_panel_open",
    }


def test_runtime_rejects_same_failed_anchor_across_three_repairs():
    events = []
    generated = []

    def repair(_contract, _snapshot, _history, _failure, attempt, _call):
        generated.append(attempt)
        return [
            {
                "id": f"reused-{attempt}",
                "type": "css",
                "value": '[aria-label="Comments"]',
                "enabled": True,
            }
        ]

    runtime = HealingRuntime(
        config=SimpleNamespace(
            target_url="https://www.tiktok.com/",
            test_profile_ids=("dedicated-a", "dedicated-b"),
            model_id="",
            site="tiktok",
            environment="production",
        ),
        settings={"selector_probe": {}},
        store=FakeStore(events),
        registry=FakeRegistry(events),
        adspower_client=SimpleNamespace(events=events),
        elements={},
        session_manager_factory=FakeSessionManager,
        playwright_starter=lambda: _async_value(FakePlaywright(events)),
        state_runner_factory=lambda **_kwargs: FakeRunner(events),
        snapshot_extractor=lambda _page: _async_value(
            SemanticSnapshot(nodes=())
        ),
        repair_generator=repair,
        wait_for_cdp=lambda _url: True,
    )

    with runtime:
        alias = next(iter(runtime.contracts))
        contract = runtime.contracts[alias]
        previous = {
            "elements": {
                item_alias: {
                    "scope": item_contract.scope,
                    "locators": [
                        (
                            {
                                "id": "failed-anchor",
                                "type": "attribute",
                                "name": "aria-label",
                                "value": "Comments",
                                "enabled": True,
                            }
                            if item_alias == alias
                            else {
                                "id": "old-" + hashlib.sha256(
                                    item_alias.encode()
                                ).hexdigest()[:8],
                                "type": "role",
                                "role": item_contract.accepted_roles[0],
                                "name": item_contract.accepted_names[0],
                                "name_mode": "exact",
                                "enabled": True,
                            }
                        )
                    ],
                }
                for item_alias, item_contract in runtime.contracts.items()
            }
        }
        context = runtime.fresh_validation_context(
            failed_aliases=(alias,),
        )
        results = [
            runtime.repair_candidate(
                attempt=attempt,
                context=context,
                failure={
                    "failed_aliases": [alias],
                    "code": "zero_match",
                    "match_count": 0,
                },
                previous_candidate=previous,
                prohibited_methods=(),
                model_call=lambda *_args: {},
            )
            for attempt in (1, 2, 3)
        ]

    assert results == [None, None, None]
    assert generated == [1, 2, 3]
