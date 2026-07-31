import asyncio
import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

import selector_probe.validator as validator_module
from selector_probe.contracts import ElementContract
from selector_probe.state_runner import ProbeSafetyError
from selector_probe.validator import (
    ResetCapture,
    ValidationRejected,
    _ensure_state,
    validate_bundle_on_page,
    validate_two_rounds,
)


ALIAS = "comment-entry"


def test_ensure_state_preserves_safe_comment_readiness_code():
    class Runner:
        async def ensure_state(self, *_args, **_kwargs):
            raise ProbeSafetyError(
                "comment_panel_readiness_timeout",
                "open_comment_panel",
            )

    async def scenario():
        with pytest.raises(ValidationRejected) as caught:
            await _ensure_state(
                Runner(),
                object(),
                "comment_panel_open",
                {},
                "required_state_failed",
            )

        assert caught.value.code == "comment_panel_readiness_timeout"
        assert caught.value.required_state == "comment_panel_open"

    asyncio.run(scenario())


def test_page_validation_prioritizes_readiness_over_selector_failures(
    monkeypatch,
):
    panel_definition = definition()
    panel_definition["scope"] = "visible_comment_panel"
    elements = {
        "feed-entry": definition(),
        "comment-input": panel_definition,
    }
    contracts = {
        "feed-entry": contract(alias="feed-entry"),
        "comment-input": contract(
            alias="comment-input",
            required_state="comment_panel_open",
            scope="visible_comment_panel",
        ),
    }

    async def inspect(*_args):
        return {
            "status": "error",
            "code": "element_candidate_not_found",
        }

    class Runner:
        async def ensure_state(self, _page, state, _elements):
            if state == "comment_panel_open":
                raise ProbeSafetyError(
                    "comment_panel_snapshot_unstable",
                    "open_comment_panel",
                )
            return {"state": state}

    monkeypatch.setattr(
        validator_module,
        "inspect_visible_element",
        inspect,
    )

    with pytest.raises(ValidationRejected) as caught:
        asyncio.run(
            validate_bundle_on_page(
                object(),
                make_bundle(elements),
                contracts,
                Runner(),
            )
        )

    assert caught.value.code == "comment_panel_snapshot_unstable"
    assert [item["code"] for item in caught.value.failures] == [
        "zero_match",
        "comment_panel_snapshot_unstable",
    ]


def canonical_hash(elements):
    payload = json.dumps(
        elements,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def definition(*locators):
    return {
        "scope": "active_video",
        "locators": list(locators) or [
            {
                "id": "entry",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }
        ],
    }


def make_bundle(elements=None):
    elements = elements or {ALIAS: definition()}
    return {"bundle_hash": canonical_hash(elements), "elements": elements}


def contract(**overrides):
    item = ElementContract(
        alias=ALIAS,
        intent="inspect the comment entry",
        required_state="feed_ready",
        scope="active_video",
        accepted_roles=("button",),
        name_mode="contains",
        accepted_names=("Comments",),
        preferred_attributes=("data-e2e", "aria-label"),
        postcondition="",
        probe_action="inspect_only",
    )
    return replace(item, **overrides)


def test_page_validation_collects_all_bad_aliases(monkeypatch):
    aliases = ("first-entry", "second-entry")
    elements = {
        alias: definition(
            {
                "id": f"candidate-{index}",
                "type": "attribute",
                "name": "data-e2e",
                "value": f"comment-icon-{index}",
                "enabled": True,
            }
        )
        for index, alias in enumerate(aliases)
    }
    contracts = {
        alias: replace(contract(), alias=alias)
        for alias in aliases
    }

    async def inspect(_page, alias, definition):
        del definition
        ambiguous = alias == aliases[0]
        return {
            "status": "error",
            "code": (
                "element_candidate_ambiguous"
                if ambiguous
                else "element_candidate_not_found"
            ),
            "alias": alias,
            "scope": "active_video",
            "diagnostics": {
                "candidates": [
                    {"actionable_count": 2 if ambiguous else 0}
                ]
            },
        }

    class Runner:
        async def ensure_state(self, _page, state, _elements):
            return {"state": state}

    monkeypatch.setattr(
        validator_module,
        "inspect_visible_element",
        inspect,
    )

    with pytest.raises(ValidationRejected) as caught:
        asyncio.run(
            validate_bundle_on_page(
                object(),
                make_bundle(elements),
                contracts,
                Runner(),
            )
        )

    assert caught.value.code == "selector_validation_failed"
    assert [item["alias"] for item in caught.value.failures] == list(
        aliases
    )
    assert [item["code"] for item in caught.value.failures] == [
        "multiple_match",
        "zero_match",
    ]


class FakeSnapshot:
    def __init__(self, marker):
        self.marker = marker

    def model_payload(self):
        return {"scope": "page", "nodes": [{"marker": self.marker}]}


def generation_hash(profile_mask, round_number):
    return "sha256:" + hashlib.sha256(
        f"{profile_mask}:{round_number}:document".encode()
    ).hexdigest()


async def unit_reset(_target, round_number, profile_mask):
    return ResetCapture(
        snapshot=FakeSnapshot(f"{profile_mask}:{round_number}"),
        page_generation=generation_hash(profile_mask, round_number),
    )


def passed_evidence(
    _handle,
    _round_number,
    current_bundle,
    _challenge,
    _reset_evidence,
    *,
    candidate="entry",
):
    return {
        "status": "passed",
        "bundle_hash": current_bundle["bundle_hash"],
        "aliases": {
            ALIAS: {"status": "ok", "candidate_id": candidate},
        },
        "actions": [],
    }


def run_two_rounds(
    inspect_fn,
    *,
    handles=("***le-a", "***le-b"),
    value=None,
    reset_fn=unit_reset,
    snapshot_extractor=None,
    ready_fn=None,
):
    kwargs = {"reset_fn": reset_fn, "ready_fn": ready_fn}
    if snapshot_extractor is not None:
        kwargs["snapshot_extractor"] = snapshot_extractor
    return validate_two_rounds(
        handles=handles,
        bundle=value or make_bundle(),
        contracts={ALIAS: contract()},
        inspect_fn=inspect_fn,
        **kwargs,
    )


def test_two_profiles_two_challenged_rounds_have_fixed_order_and_safe_evidence():
    async def scenario():
        calls = []

        async def inspect_fn(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            calls.append((handle, round_number, challenge))
            return passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )

        result = await run_two_rounds(inspect_fn)
        assert [(item[0], item[1]) for item in calls] == [
            ("***le-a", 1),
            ("***le-b", 1),
            ("***le-a", 2),
            ("***le-b", 2),
        ]
        assert len({item[2] for item in calls}) == 4
        assert result["profiles_passed"] == 2
        assert result["rounds_passed"] == 2
        assert len(result["validations"]) == 4
        assert "challenge" not in str(result)

    asyncio.run(scenario())


def test_bundle_hash_is_recomputed_from_canonical_elements_before_inspection():
    async def scenario():
        bad = make_bundle()
        bad["bundle_hash"] = "sha256:" + "0" * 64

        async def inspect_fn(*_args):
            raise AssertionError("bad hash must fail before inspection")

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(inspect_fn, value=bad)
        assert caught.value.code == "bundle_hash_invalid"

    asyncio.run(scenario())


def test_cyclic_bundle_is_rejected_before_normalization_or_hashing():
    async def scenario():
        cyclic = make_bundle()
        cyclic["loop"] = cyclic
        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(lambda *_args: None, value=cyclic)
        assert caught.value.code == "bundle_resource_limit"

    asyncio.run(scenario())


def test_inspection_cannot_self_report_or_mutate_freshness_evidence():
    async def scenario():
        async def forged(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            result["reset_evidence"] = {
                "reloaded": True,
                "fresh": True,
            }
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(forged)
        assert caught.value.code == "evidence_schema_invalid"

        async def mutating(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            reset_evidence["reloaded"] = False
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(mutating)
        assert caught.value.code == "reset_evidence_mutated"

    asyncio.run(scenario())


def test_same_generation_or_same_snapshot_object_is_not_fresh():
    async def scenario():
        async def inspect_ok(*args):
            return passed_evidence(*args)

        async def same_generation(_target, round_number, profile_mask):
            return ResetCapture(
                snapshot=FakeSnapshot(f"{profile_mask}:{round_number}"),
                page_generation=generation_hash(profile_mask, 1),
            )

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(
                inspect_ok,
                reset_fn=same_generation,
            )
        assert caught.value.code == "page_generation_not_fresh"

        snapshots = {
            "***le-a": FakeSnapshot("a"),
            "***le-b": FakeSnapshot("b"),
        }

        async def same_snapshot(_target, round_number, profile_mask):
            return ResetCapture(
                snapshot=snapshots[profile_mask],
                page_generation=generation_hash(
                    profile_mask,
                    round_number,
                ),
            )

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(
                inspect_ok,
                reset_fn=same_snapshot,
            )
        assert caught.value.code == "snapshot_not_fresh"

    asyncio.run(scenario())


class FakeResetPage:
    def __init__(self, label, calls):
        self.label = label
        self.calls = calls
        self.generation = 0

    async def reload(self, *, wait_until):
        assert wait_until == "domcontentloaded"
        self.generation += 1
        self.calls.append((self.label, "reload", self.generation))

    async def wait_for_load_state(self, state):
        assert state == "domcontentloaded"
        self.calls.append((self.label, "wait", self.generation))

    async def evaluate(self, _script):
        self.calls.append((self.label, "generation", self.generation))
        return {
            "time_origin": 1_000.0 + self.generation,
            "url": f"https://www.tiktok.com/{self.label}",
        }


def test_default_reset_owns_reload_ready_snapshot_generation_before_inspect():
    async def scenario():
        calls = []
        handles = tuple(
            SimpleNamespace(
                profile=SimpleNamespace(
                    profile_id=f"profile-{label}",
                    profile_mask=f"***le-{label}",
                    ws_url=f"ws://profile-{label}",
                ),
                page=FakeResetPage(label, calls),
            )
            for label in ("a", "b")
        )

        async def ready(page):
            calls.append((page.label, "ready", page.generation))
            return {"state": "feed_ready"}

        async def extract(page):
            calls.append((page.label, "snapshot", page.generation))
            return FakeSnapshot(f"{page.label}:{page.generation}")

        async def inspect(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            page = handle.page
            calls.append((page.label, "inspect", page.generation))
            assert reset_evidence["challenge"] == challenge
            assert reset_evidence["round_number"] == round_number
            return passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )

        result = await run_two_rounds(
            inspect,
            handles=handles,
            reset_fn=None,
            snapshot_extractor=extract,
            ready_fn=ready,
        )
        assert result["status"] == "passed"
        for label in ("a", "b"):
            assert [
                item[1]
                for item in calls
                if item[0] == label
            ] == [
                "reload",
                "wait",
                "ready",
                "snapshot",
                "generation",
                "inspect",
                "reload",
                "wait",
                "ready",
                "snapshot",
                "generation",
                "inspect",
            ]

    asyncio.run(scenario())


def test_default_reset_rejects_missing_reload_before_inspection():
    async def scenario():
        handles = (
            SimpleNamespace(
                profile=SimpleNamespace(
                    profile_id="profile-a",
                    profile_mask="***le-a",
                    ws_url="ws://profile-a",
                ),
                page=SimpleNamespace(),
            ),
            SimpleNamespace(
                profile=SimpleNamespace(
                    profile_id="profile-b",
                    profile_mask="***le-b",
                    ws_url="ws://profile-b",
                ),
                page=SimpleNamespace(),
            ),
        )

        async def inspect(*_args):
            raise AssertionError("inspection must not run without reload")

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(
                inspect,
                handles=handles,
                reset_fn=None,
            )
        assert caught.value.code == "profile_page_missing"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("candidate_for_call", "code"),
    [
        (lambda call: "missing", "candidate_evidence_invalid"),
        (
            lambda call: "entry" if call < 2 else "fallback",
            "candidate_changed",
        ),
    ],
)
def test_candidate_id_must_exist_enabled_and_match_all_four_calls(
    candidate_for_call,
    code,
):
    async def scenario():
        calls = 0

        async def inspect_fn(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            nonlocal calls
            calls += 1
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
                candidate=candidate_for_call(calls),
            )
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(inspect_fn)
        assert caught.value.code == code

    asyncio.run(scenario())


def test_failure_is_immediate_and_cancelled_error_propagates():
    async def scenario():
        calls = []

        async def failed(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            calls.append(handle)
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            if handle == "***le-b":
                result["status"] = "failed"
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(failed)
        assert caught.value.code == "profile_validation_failed"
        assert calls == ["***le-a", "***le-b"]

        async def cancelled(*_args):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await run_two_rounds(cancelled)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("handles", "code"),
    [
        (("***le-a",), "profiles_insufficient"),
        (("***le-a", "***le-a"), "profiles_duplicate"),
        (("production-profile-a", "***le-b"), "profile_handle_unmasked"),
        (tuple(f"***le-{index}" for index in range(9)), "profiles_too_many"),
    ],
)
def test_profile_handles_are_bounded_unique_and_masked(handles, code):
    async def scenario():
        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(lambda *_args: None, handles=handles)
        assert caught.value.code == code
        assert "production-profile-a" not in str(caught.value)

    asyncio.run(scenario())


def test_all_handles_raw_secrets_and_forbidden_actions_are_scanned():
    raw_a = "production-profile-a"
    raw_b = "production-profile-b"
    handles = (
        SimpleNamespace(
            profile=SimpleNamespace(
                profile_id=raw_a,
                profile_mask="***le-a",
                ws_url="ws://secret-a",
            )
        ),
        SimpleNamespace(
            profile=SimpleNamespace(
                profile_id=raw_b,
                profile_mask="***le-b",
                ws_url="ws://secret-b",
            )
        ),
    )

    async def scenario():
        async def leaked(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            result["debug"] = raw_b
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(leaked, handles=handles)
        assert caught.value.code == "evidence_sensitive"
        assert raw_b not in str(caught.value)

        async def forbidden(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            result["actions"] = ["submit"]
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(forbidden)
        assert caught.value.code == "forbidden_action_evidence"

    asyncio.run(scenario())


def test_cyclic_or_oversized_evidence_is_rejected_before_serialization():
    async def scenario():
        async def cyclic(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            result["loop"] = result
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(cyclic)
        assert caught.value.code == "evidence_resource_limit"

        async def huge(
            handle,
            round_number,
            current_bundle,
            challenge,
            reset_evidence,
        ):
            result = passed_evidence(
                handle,
                round_number,
                current_bundle,
                challenge,
                reset_evidence,
            )
            result["huge"] = "x" * 20_000
            return result

        with pytest.raises(ValidationRejected) as caught:
            await run_two_rounds(huge)
        assert caught.value.code == "evidence_resource_limit"

    asyncio.run(scenario())


class FakeElementHandle:
    def __init__(self, identity):
        self.identity = identity
        self.disposed = False

    async def dispose(self):
        self.disposed = True


class FakeLocator:
    def __init__(
        self,
        *,
        identity="node-1",
        role="button",
        name="Comments",
        attributes=None,
        count=1,
        actionable=True,
        visible=None,
        enabled=None,
    ):
        self.identity = identity
        self.role = role
        self.name = name
        self.attributes = attributes or {
            "data-e2e": "comment-icon",
            "aria-label": "Comments",
        }
        self._count = count
        self.actionable = actionable
        self.visible = actionable if visible is None else visible
        self.enabled = actionable if enabled is None else enabled
        self.handles = []

    async def count(self):
        return self._count

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def aria_snapshot(self):
        return {"role": self.role, "name": self.name}

    async def evaluate(self, script, argument=None):
        if "firstNode" in script:
            return (
                isinstance(argument, FakeElementHandle)
                and argument.identity == self.identity
            )
        return {
            "role": self.role,
            "name": self.name,
            "attributes": self.attributes,
            "actionable": self.actionable,
        }

    async def element_handle(self):
        handle = FakeElementHandle(self.identity)
        self.handles.append(handle)
        return handle


class FakeStateRunner:
    def __init__(self, *, wrong_postcondition=False):
        self.calls = []
        self.wrong_postcondition = wrong_postcondition

    async def ensure_state(self, _page, state, elements):
        self.calls.append((state, elements))
        if self.wrong_postcondition and state == "comment_panel_open":
            return {"state": "feed_ready"}
        return {"state": state}


def install_page_fakes(
    monkeypatch,
    locators,
    *,
    primary_id="entry",
    primary_type="attribute",
):
    queue = list(locators)
    calls = []

    async def inspect(_page, alias, current_definition):
        return {
            "status": "ok",
            "alias": alias,
            "scope": current_definition["scope"],
            "candidate": {"id": primary_id, "type": primary_type},
            "diagnostics": {},
        }

    async def resolve(_page, alias, current_definition):
        index = len(calls)
        locator = queue[min(index, len(queue) - 1)]
        candidate = current_definition["locators"][0]
        calls.append(candidate["id"])
        return SimpleNamespace(
            locator=locator,
            alias=alias,
            scope=current_definition["scope"],
            candidate={"id": candidate["id"], "type": candidate["type"]},
            diagnostics={},
        )

    async def no_sleep(seconds):
        assert seconds == 0.25

    monkeypatch.setattr(
        validator_module,
        "inspect_visible_element",
        inspect,
    )
    monkeypatch.setattr(
        validator_module,
        "resolve_visible_element",
        resolve,
    )
    monkeypatch.setattr(validator_module, "_sleep", no_sleep)
    return calls


def test_page_validator_re_resolves_after_250ms_and_proves_same_node(monkeypatch):
    async def scenario():
        first = FakeLocator(identity="stable")
        second = FakeLocator(identity="stable")
        calls = install_page_fakes(monkeypatch, [first, second])
        result = await validate_bundle_on_page(
            object(),
            make_bundle(),
            {ALIAS: contract()},
            FakeStateRunner(),
        )
        assert result["status"] == "passed"
        assert result["aliases"][ALIAS]["candidate_id"] == "entry"
        assert calls == ["entry", "entry"]
        assert first.handles[0].disposed is True

    asyncio.run(scenario())


def test_inspect_only_disabled_control_is_visible_stable_not_clicked(
    monkeypatch,
):
    async def scenario():
        first = FakeLocator(
            identity="stable-disabled",
            actionable=False,
            visible=True,
            enabled=False,
        )
        second = FakeLocator(
            identity="stable-disabled",
            actionable=False,
            visible=True,
            enabled=False,
        )
        install_page_fakes(monkeypatch, [first, second])

        result = await validate_bundle_on_page(
            object(),
            make_bundle(),
            {ALIAS: contract(probe_action="inspect_only")},
            FakeStateRunner(),
        )

        assert result["status"] == "passed"
        assert result["aliases"][ALIAS]["actionable"] is False

    asyncio.run(scenario())


def test_open_read_only_disabled_control_remains_rejected(monkeypatch):
    async def scenario():
        disabled = FakeLocator(
            identity="stable-disabled",
            actionable=False,
            visible=True,
            enabled=False,
        )
        install_page_fakes(monkeypatch, [disabled, disabled])

        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(),
                {
                    ALIAS: contract(
                        probe_action="open_read_only",
                        postcondition="comment_panel_open",
                    )
                },
                FakeStateRunner(),
            )

        assert caught.value.code == "element_not_actionable"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("second", "code"),
    [
        (FakeLocator(identity="replacement"), "element_identity_changed"),
        (FakeLocator(identity="stable", count=0), "element_unstable"),
        (FakeLocator(identity="stable", actionable=False), "element_unstable"),
        (
            FakeLocator(identity="stable", role="link"),
            "semantic_role_mismatch",
        ),
        (
            FakeLocator(identity="stable", name="Share"),
            "semantic_name_mismatch",
        ),
    ],
)
def test_second_resolution_rejects_identity_stability_or_semantic_change(
    monkeypatch,
    second,
    code,
):
    async def scenario():
        install_page_fakes(
            monkeypatch,
            [FakeLocator(identity="stable"), second],
        )
        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(),
                {ALIAS: contract()},
                FakeStateRunner(),
            )
        assert caught.value.code == code

    asyncio.run(scenario())


def test_every_enabled_fallback_is_independently_validated(monkeypatch):
    async def scenario():
        locators = [
            {
                "id": "entry",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            },
            {
                "id": "fallback",
                "type": "css",
                "value": 'button[aria-label="Comments"]',
                "enabled": True,
                "fallback": True,
            },
        ]
        elements = {ALIAS: definition(*locators)}
        calls = install_page_fakes(
            monkeypatch,
            [
                FakeLocator(identity="same"),
                FakeLocator(identity="same"),
                FakeLocator(identity="same"),
                FakeLocator(identity="same"),
            ],
        )
        result = await validate_bundle_on_page(
            object(),
            make_bundle(elements),
            {ALIAS: contract()},
            FakeStateRunner(),
        )
        assert result["status"] == "passed"
        assert calls == ["entry", "entry", "fallback", "fallback"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "locator",
    [
        {
            "id": "unsafe",
            "type": "css",
            "value": "button:nth-child(2)",
            "enabled": True,
        },
        {
            "id": "unsafe",
            "type": "xpath",
            "value": "/html/body/button",
            "enabled": True,
        },
    ],
)
def test_unsafe_css_or_xpath_is_rejected_before_page_access(monkeypatch, locator):
    async def scenario():
        async def inspect(*_args):
            raise AssertionError("unsafe selector must fail before page access")

        monkeypatch.setattr(
            validator_module,
            "inspect_visible_element",
            inspect,
        )
        elements = {ALIAS: definition(locator)}
        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(elements),
                {ALIAS: contract()},
                FakeStateRunner(),
            )
        assert caught.value.code == "selector_unsafe"

    asyncio.run(scenario())


def test_accessibility_semantics_are_injected_and_dom_implicit_role_is_not_trusted(
    monkeypatch,
):
    async def scenario():
        locator = FakeLocator(role="", name="")
        install_page_fakes(monkeypatch, [locator, locator])

        async def ax_inspector(_page, _locator):
            return {"role": "button", "name": "Comments"}

        result = await validate_bundle_on_page(
            object(),
            make_bundle(),
            {ALIAS: contract()},
            FakeStateRunner(),
            ax_inspector=ax_inspector,
        )
        assert result["status"] == "passed"

        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(),
                {ALIAS: contract()},
                FakeStateRunner(),
                ax_inspector=lambda *_args: asyncio.sleep(
                    0,
                    result={"role": "", "name": "Comments"},
                ),
            )
        assert caught.value.code == "semantic_role_mismatch"

    asyncio.run(scenario())


def test_default_ax_path_parses_playwright_aria_snapshot(monkeypatch):
    async def scenario():
        first = FakeLocator()
        second = FakeLocator()

        async def aria_snapshot():
            return '- button "Comments"'

        first.aria_snapshot = aria_snapshot
        second.aria_snapshot = aria_snapshot
        install_page_fakes(monkeypatch, [first, second])
        result = await validate_bundle_on_page(
            object(),
            make_bundle(),
            {ALIAS: contract()},
            FakeStateRunner(),
        )
        assert result["status"] == "passed"

    asyncio.run(scenario())


def test_safe_postcondition_only_and_postcondition_must_be_observed(monkeypatch):
    async def scenario():
        install_page_fakes(
            monkeypatch,
            [FakeLocator(), FakeLocator()],
        )
        safe = contract(
            postcondition="comment_panel_open",
            probe_action="open_read_only",
        )
        runner = FakeStateRunner()
        result = await validate_bundle_on_page(
            object(),
            make_bundle(),
            {ALIAS: safe},
            runner,
        )
        assert result["actions"] == ["open_read_only"]
        assert [item[0] for item in runner.calls] == [
            "feed_ready",
            "comment_panel_open",
        ]

        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(),
                {ALIAS: replace(safe, probe_action="submit")},
                FakeStateRunner(),
            )
        assert caught.value.code == "contract_action_forbidden"

        with pytest.raises(ValidationRejected) as caught:
            await validate_bundle_on_page(
                object(),
                make_bundle(),
                {ALIAS: safe},
                FakeStateRunner(wrong_postcondition=True),
            )
        assert caught.value.code == "postcondition_failed"

    asyncio.run(scenario())
