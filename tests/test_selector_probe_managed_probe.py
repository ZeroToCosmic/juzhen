import asyncio

import pytest

from selector_probe.probe import run_managed_probe


def passing_validation(*, profiles=2, rounds=2, consistent=True):
    validations = []
    for profile_number in range(1, profiles + 1):
        for round_number in range(1, rounds + 1):
            marker = profile_number * 10 + round_number
            validations.append({
                "profile_mask": f"***P{profile_number:03d}",
                "round_number": round_number,
                "reset_evidence_hash": f"sha256:{marker:064x}",
                "snapshot_hash": f"sha256:{marker + 100:064x}",
                "page_generation": f"sha256:{marker + 200:064x}",
                "aliases": {
                    "comment-input": {
                        "status": "ok",
                        "candidate_id": "saved-css",
                    },
                },
            })
    return {
        "status": "passed",
        "profiles_passed": profiles,
        "rounds_passed": rounds,
        "consistent": consistent,
        "validations": validations,
        "elements": {
            "comment-input": {
                "status": "passed",
                "attempt_count": 1,
                "selected_locator_index": 0,
            },
        },
    }


class FakeRuntime:
    def __init__(self, validation=None, publication=None):
        self.validation = validation or passing_validation()
        self.publication = publication or {
            "published": True,
            "reconciled": True,
            "version": "manual-v2",
        }
        self.calls = []
        self.stages = []
        self.manual_gate_clear_count = 0

    def load_candidate(self):
        self.calls.append("load_candidate")
        return {"elements": {"comment-input": {"locators": []}}}

    def validate_matrix(self, candidate, *, max_attempts):
        self.calls.append(("validate_matrix", candidate, max_attempts))
        return self.validation

    def promote_saved_fallbacks(self, candidate, validation):
        self.calls.append(("promote_saved_fallbacks", candidate, validation))
        return {"elements": candidate["elements"], "promoted": True}

    def prepare_publication(self, promoted, validation):
        self.calls.append(("prepare_publication", promoted, validation))
        return {"version": "manual-v2", "elements": promoted["elements"]}

    def store_and_publish(self, bundle, validation):
        self.calls.append(("store_and_publish", bundle, validation))
        return self.publication

    def record_business_stage(self, name, status, **details):
        self.stages.append((name, status, details))


def test_managed_probe_calls_strict_runtime_contract_and_publishes():
    runtime = FakeRuntime()

    result = run_managed_probe(runtime)

    assert result["status"] == "published"
    assert result["published"] is True
    assert result["reconciled"] is True
    assert result["new_version"] == "manual-v2"
    assert runtime.calls[0] == "load_candidate"
    assert runtime.calls[1][0] == "validate_matrix"
    assert runtime.calls[1][2] == 3
    assert [call[0] for call in runtime.calls[2:]] == [
        "promote_saved_fallbacks",
        "prepare_publication",
        "store_and_publish",
    ]
    assert [stage[0] for stage in runtime.stages] == [
        "validate_elements",
        "protect_or_recover",
        "alert_and_cleanup",
    ]


def test_managed_probe_empty_candidate_waits_for_operator_selection():
    runtime = FakeRuntime()
    runtime.load_candidate = lambda: {"elements": {}}

    result = run_managed_probe(runtime)

    assert result["status"] == "awaiting_element_selection"
    assert result["failure_code"] == "awaiting_element_selection"
    assert runtime.stages == []


def test_managed_probe_accepts_preloaded_candidate_without_loading_again():
    runtime = FakeRuntime()
    candidate = runtime.load_candidate()
    runtime.calls.clear()

    result = run_managed_probe(runtime, candidate=candidate)

    assert result["status"] == "published"
    assert all(call != "load_candidate" for call in runtime.calls)


def test_managed_probe_limits_retries_and_isolates_failed_element_aliases():
    validation = passing_validation()
    validation.update({
        "status": "failed",
        "elements": {
            "comment-entry": {
                "status": "passed",
                "attempt_count": 1,
                "selected_locator_index": 0,
            },
            "comment-input": {
                "status": "failed",
                "failure_code": "selector_zero_match",
                "attempt_count": 99,
            },
        },
    })
    runtime = FakeRuntime(validation=validation)

    result = run_managed_probe(runtime)

    assert result["status"] == "selector_validation_failed"
    assert result["failure_code"] == "selector_zero_match"
    assert result["attempt_count"] == 3
    assert result["proposed_pause_aliases"] == ["comment-input"]
    assert not any(
        call[0] == "promote_saved_fallbacks"
        for call in runtime.calls
        if isinstance(call, tuple)
    )


@pytest.mark.parametrize(
    "validation",
    [
        passing_validation(profiles=1),
        passing_validation(rounds=1),
        passing_validation(consistent=False),
    ],
)
def test_managed_probe_fails_closed_on_incomplete_matrix(validation):
    runtime = FakeRuntime(validation=validation)

    result = run_managed_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "validation_matrix_incomplete"
    assert result["published"] is False


def test_managed_probe_rejects_duplicate_profile_round_evidence():
    validation = passing_validation()
    validation["validations"][1] = dict(validation["validations"][0])

    result = run_managed_probe(FakeRuntime(validation=validation))

    assert result["failure_code"] == "validation_matrix_incomplete"


def test_managed_probe_rejects_malformed_validation_as_infrastructure():
    runtime = FakeRuntime()
    runtime.validation = None

    result = run_managed_probe(runtime)

    assert result["status"] == "infrastructure_unavailable"
    assert result["failure_code"] == "validation_matrix_incomplete"
    assert result["proposed_pause_aliases"] == []


def test_managed_probe_requires_atomic_publish_and_reconcile():
    runtime = FakeRuntime(publication={
        "published": True,
        "reconciled": False,
        "version": "manual-v2",
    })

    result = run_managed_probe(runtime)

    assert result["status"] == "publication_failed"
    assert result["failure_code"] == "publication_incomplete"


def test_managed_probe_never_clears_manual_gate():
    runtime = FakeRuntime()

    run_managed_probe(runtime)

    assert runtime.manual_gate_clear_count == 0


def test_managed_probe_observe_mode_validates_without_publication_methods():
    runtime = FakeRuntime()
    runtime.promote_saved_fallbacks = None
    runtime.prepare_publication = None
    runtime.store_and_publish = None

    result = run_managed_probe(runtime, publish=False)

    assert result["status"] == "healthy"
    assert result["published"] is False
    assert result["validation_evidence"] == runtime.validation
    assert [
        call[0] if isinstance(call, tuple) else call
        for call in runtime.calls
    ] == ["load_candidate", "validate_matrix"]


def test_managed_probe_propagates_cancellation():
    runtime = FakeRuntime()

    def cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    runtime.validate_matrix = cancel

    with pytest.raises(asyncio.CancelledError):
        run_managed_probe(runtime)
