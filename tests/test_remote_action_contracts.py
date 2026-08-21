"""Strict runtime contracts shared by Central and Agent."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from remote_actions.contracts import (
    ContractDecodeError,
    ContractSemanticError,
    MessageTooLargeError,
    ProtocolVersionError,
    load_json_strict,
    validate_release_content,
    validate_execution_outcome,
    validate_message,
    validate_wss_message,
)
from remote_actions.enums import (
    AckKind,
    ErrorCategory,
    MessageType,
    OutcomeStatus,
    PermitStatus,
    ProgressStatus,
    SideEffectState,
)
from remote_actions.parameters import ParameterBindingError, bind_parameters


ROOT = Path(__file__).resolve().parents[1]
VECTORS_PATH = ROOT / "tests" / "fixtures" / "remote_actions" / "protocol_vectors.json"


def _vectors() -> list[dict]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


def _vector(schema_name: str, definition: str | None = None) -> dict:
    return next(
        item
        for item in _vectors()
        if item["schema"] == schema_name and item.get("definition") == definition
    )


def _outcome(status: str) -> dict:
    return next(
        item["valid"]
        for item in _vectors()
        if item["schema"] == "execution-outcome-v1.schema.json"
        and item.get("definition") == "outcome_request"
        and item["valid"]["status"] == status
    )


def test_frozen_enums_are_exhaustive() -> None:
    assert {item.value for item in MessageType} == {
        "WORK_ORDER_DELIVER",
        "COMMAND_ACK",
        "WORK_ORDER_CANCEL",
        "PROGRESS_EVENT",
        "TERMINAL_REFERENCE",
        "RECONCILE_REQUEST",
        "RECONCILE_RESPONSE",
    }
    assert {item.value for item in AckKind} == {
        "RECEIVED",
        "ACCEPTED",
        "REJECTED",
        "ALREADY_TERMINAL",
    }
    assert {item.value for item in ProgressStatus} == {
        "RUNNING",
        "VERIFYING",
        "RECONCILING",
        "CHECKPOINT_BLOCKED",
    }
    assert {item.value for item in OutcomeStatus} == {
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "UNVERIFIED",
    }
    assert {item.value for item in SideEffectState} == {
        "NONE",
        "CONFIRMED",
        "UNCERTAIN",
        "MIXED",
    }
    assert {item.value for item in PermitStatus} == {
        "ISSUED",
        "CLOSED_UNUSED",
        "CONFIRMED",
        "UNCERTAIN",
    }
    assert {item.value for item in ErrorCategory} == {
        "VALIDATION",
        "RESOURCE",
        "TRANSIENT",
        "BUSINESS",
        "CANCELLED",
        "SIDE_EFFECT_UNCERTAIN",
    }


@pytest.mark.parametrize(
    ("message_type", "schema_name"),
    [
        (MessageType.WORK_ORDER_DELIVER, "work-order-v1.schema.json"),
        (MessageType.COMMAND_ACK, "command-ack-v1.schema.json"),
        (MessageType.WORK_ORDER_CANCEL, "cancel-command-v1.schema.json"),
        (MessageType.PROGRESS_EVENT, "progress-event-v1.schema.json"),
        (MessageType.TERMINAL_REFERENCE, "terminal-reference-v1.schema.json"),
        (MessageType.RECONCILE_REQUEST, "reconcile-request-v1.schema.json"),
        (MessageType.RECONCILE_RESPONSE, "reconcile-response-v1.schema.json"),
    ],
)
def test_validate_message_uses_the_frozen_schema(
    message_type: MessageType,
    schema_name: str,
) -> None:
    payload = copy.deepcopy(_vector(schema_name)["valid"])
    validate_message(message_type, payload)
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        validate_message(message_type, payload)


def test_validate_wss_message_checks_envelope_payload_and_protocol() -> None:
    envelope = copy.deepcopy(_vector("wss-envelope-v1.schema.json")["valid"])
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    assert validate_wss_message(raw) is MessageType.COMMAND_ACK

    envelope["protocol_version"] = "2.0"
    with pytest.raises(ProtocolVersionError):
        validate_wss_message(json.dumps(envelope).encode("utf-8"))


def test_wss_entrypoint_rejects_duplicate_keys_before_schema_validation() -> None:
    with pytest.raises(ContractDecodeError, match="duplicate"):
        validate_wss_message(
            b'{"type":"COMMAND_ACK","type":"COMMAND_ACK","protocol_version":"1.0"}'
        )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"a":1,"a":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
    ],
)
def test_load_json_strict_rejects_duplicate_keys_and_non_finite_numbers(raw: bytes) -> None:
    with pytest.raises(ContractDecodeError):
        load_json_strict(raw)


def test_load_json_strict_rejects_oversize_and_excess_depth() -> None:
    with pytest.raises(MessageTooLargeError):
        load_json_strict(b" " * (1024 * 1024 + 1))

    value: object = "leaf"
    for _ in range(33):
        value = {"node": value}
    with pytest.raises(ContractDecodeError, match="depth"):
        load_json_strict(json.dumps(value).encode("utf-8"))


def test_work_order_rejects_oversize_snapshot_and_runtime_parameters() -> None:
    work_order = copy.deepcopy(_vector("work-order-v1.schema.json")["valid"])
    work_order["definition"]["snapshot"] = {"data": "x" * (512 * 1024)}
    with pytest.raises(MessageTooLargeError, match="snapshot"):
        validate_message(MessageType.WORK_ORDER_DELIVER, work_order)

    work_order = copy.deepcopy(_vector("work-order-v1.schema.json")["valid"])
    work_order["runtime_params"] = {"data": "x" * (256 * 1024)}
    with pytest.raises(MessageTooLargeError, match="runtime_params"):
        validate_message(MessageType.WORK_ORDER_DELIVER, work_order)


@pytest.mark.parametrize("reservation_index", [0, 1])
def test_work_order_rejects_mismatched_resource_keys(reservation_index: int) -> None:
    work_order = copy.deepcopy(_vector("work-order-v1.schema.json")["valid"])
    work_order["resource_reservations"][reservation_index]["resource_key"] += "_wrong"
    with pytest.raises(ContractSemanticError, match="resource_key"):
        validate_message(MessageType.WORK_ORDER_DELIVER, work_order)


def test_work_order_rejects_duplicate_or_invalid_effect_dependencies() -> None:
    work_order = copy.deepcopy(_vector("work-order-v1.schema.json")["valid"])
    duplicate = copy.deepcopy(work_order["effect_plan"][0])
    duplicate["result_schema"] = {"type": "object", "maxProperties": 1}
    work_order["effect_plan"].append(duplicate)
    with pytest.raises(ContractSemanticError, match="unique"):
        validate_message(MessageType.WORK_ORDER_DELIVER, work_order)

    work_order = copy.deepcopy(_vector("work-order-v1.schema.json")["valid"])
    work_order["effect_plan"][0]["dependencies"] = ["effect_missing"]
    with pytest.raises(ContractSemanticError, match="not in the effect plan"):
        validate_message(MessageType.WORK_ORDER_DELIVER, work_order)

    work_order = copy.deepcopy(_vector("work-order-v1.schema.json")["valid"])
    first = work_order["effect_plan"][0]
    first["effect_id"] = "effect_a"
    first["dependencies"] = ["effect_b"]
    second = copy.deepcopy(first)
    second["effect_id"] = "effect_b"
    second["dependencies"] = ["effect_a"]
    work_order["effect_plan"].append(second)
    with pytest.raises(ContractSemanticError, match="cycle"):
        validate_message(MessageType.WORK_ORDER_DELIVER, work_order)


def test_outcome_effects_are_unique_and_match_the_work_order_plan() -> None:
    outcome = copy.deepcopy(_outcome("PARTIALLY_SUCCEEDED"))
    outcome["effects"][1]["effect_id"] = outcome["effects"][0]["effect_id"]
    with pytest.raises(ContractSemanticError, match="unique"):
        validate_execution_outcome(outcome)

    outcome = copy.deepcopy(_outcome("SUCCEEDED"))
    with pytest.raises(ContractSemanticError, match="frozen effect plan"):
        validate_execution_outcome(outcome, expected_effect_ids={"effect_required"})


def test_outcome_rejects_non_json_values_and_oversize_payloads() -> None:
    outcome = copy.deepcopy(_outcome("SUCCEEDED"))
    outcome["result_data"] = {"value": float("nan")}
    with pytest.raises(ContractDecodeError, match="non-finite"):
        validate_execution_outcome(outcome)

    outcome = copy.deepcopy(_outcome("SUCCEEDED"))
    outcome["result_data"] = {"value": "x" * (1024 * 1024)}
    with pytest.raises(MessageTooLargeError, match="execution outcome"):
        validate_execution_outcome(outcome)


def test_release_content_uses_work_order_snapshot_and_depth_limits() -> None:
    content = {
        "snapshot": {"data": "x" * (512 * 1024)},
        "parameter_schema": {},
        "result_schema": {},
        "execution_defaults": {},
    }
    with pytest.raises(MessageTooLargeError, match="snapshot"):
        validate_release_content(content)

    value: object = "leaf"
    for _ in range(33):
        value = {"node": value}
    with pytest.raises(ContractDecodeError, match="depth"):
        validate_release_content({**content, "snapshot": value})


def test_release_content_rejects_invalid_schemas_and_bindings() -> None:
    content = {
        "executor_kind": "fake",
        "definition_schema_version": "1.0",
        "parameter_schema": {
            "type": "object",
            "properties": {"target_url": {"type": "string"}},
            "required": ["target_url"],
            "additionalProperties": False,
            "bindings": {
                "target_url": {"pointer": "/runtime/target_url", "type": "string"}
            },
        },
        "result_schema": {"type": "object", "additionalProperties": True},
        "snapshot": {"runtime": {"target_url": ""}},
        "execution_defaults": {},
    }
    validate_release_content(content)

    invalid = copy.deepcopy(content)
    invalid["parameter_schema"]["bindings"]["target_url"]["pointer"] = "/missing"
    with pytest.raises(ContractSemanticError, match="invalid release schema"):
        validate_release_content(invalid)

    invalid = copy.deepcopy(content)
    invalid["parameter_schema"]["bindings"]["target_url"].pop("type")
    with pytest.raises(ContractSemanticError, match="invalid release schema"):
        validate_release_content(invalid)

    invalid = copy.deepcopy(content)
    invalid["parameter_schema"]["bindings"]["target_url"]["type"] = "object"
    with pytest.raises(ContractSemanticError, match="invalid release schema"):
        validate_release_content(invalid)

    invalid = copy.deepcopy(content)
    invalid["parameter_schema"]["properties"]["fallback_url"] = {"type": "string"}
    invalid["parameter_schema"]["bindings"]["fallback_url"] = {
        "pointer": "/runtime/target_url",
        "type": "string",
    }
    with pytest.raises(ContractSemanticError, match="unique pointers"):
        validate_release_content(invalid)

    invalid = copy.deepcopy(content)
    invalid["result_schema"] = {"type": "not-a-json-schema-type"}
    with pytest.raises(ContractSemanticError, match="invalid release schema"):
        validate_release_content(invalid)


def test_failed_pre_effect_can_retain_closed_unused_permit_id() -> None:
    outcome = copy.deepcopy(_outcome("FAILED"))
    outcome["effects"][0]["permit_id"] = "permit_closed_unused_1"
    validate_execution_outcome(
        outcome,
        expected_effect_ids={outcome["effects"][0]["effect_id"]},
    )


def test_cancelled_outcome_accepts_closed_unused_pre_effect_failure() -> None:
    outcome = copy.deepcopy(_outcome("CANCELLED"))
    assert outcome["effects"][0]["status"] == "FAILED_PRE_EFFECT"
    assert outcome["effects"][0]["permit_id"].startswith("permit_")
    validate_execution_outcome(
        outcome,
        expected_effect_ids={outcome["effects"][0]["effect_id"]},
    )


@pytest.mark.parametrize("category", ["VALIDATION", "RESOURCE"])
def test_validation_and_resource_failures_are_ack_rejections(category: str) -> None:
    outcome = copy.deepcopy(_outcome("FAILED"))
    outcome["error"]["category"] = category
    with pytest.raises(ValidationError):
        validate_execution_outcome(outcome)


@pytest.mark.parametrize("field", ["code", "stage"])
def test_non_success_outcome_requires_stable_error_details(field: str) -> None:
    outcome = copy.deepcopy(_outcome("FAILED"))
    outcome["error"][field] = ""
    with pytest.raises(ValidationError):
        validate_execution_outcome(outcome)


SNAPSHOT = {
    "target": {"url": "old"},
    "execution": {"timeout": 30},
    "content": {"text": "old"},
}
PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "target_url": {"type": "string", "maxLength": 2048},
        "input_text": {"type": "string", "maxLength": 4096},
    },
    "required": ["target_url"],
    "additionalProperties": False,
    "bindings": {
        "target_url": {"pointer": "/target/url", "type": "string"},
        "input_text": {"pointer": "/content/text", "type": "string"},
    },
}


def test_bind_parameters_changes_only_declared_pointers() -> None:
    bound = bind_parameters(
        SNAPSHOT,
        PARAMETER_SCHEMA,
        {"target_url": "https://tiktok.example/v/1", "input_text": "hello"},
    )
    assert bound == {
        "target": {"url": "https://tiktok.example/v/1"},
        "execution": {"timeout": 30},
        "content": {"text": "hello"},
    }
    assert SNAPSHOT["target"]["url"] == "old"


@pytest.mark.parametrize(
    "params",
    [
        {"unknown": 1, "target_url": "https://example.test"},
        {"target_url": 7},
        {"target_url": {"$merge": {"execution": {"timeout": 0}}}},
    ],
)
def test_bind_parameters_rejects_undeclared_or_wrong_values(params: dict) -> None:
    with pytest.raises(ParameterBindingError):
        bind_parameters(SNAPSHOT, PARAMETER_SCHEMA, params)


@pytest.mark.parametrize(
    "schema",
    [
        {
            **PARAMETER_SCHEMA,
            "bindings": {"target_url": {"pointer": "target/url", "type": "string"}},
        },
        {
            **PARAMETER_SCHEMA,
            "bindings": {
                "target_url": {"pointer": "/missing/value", "type": "string"}
            },
        },
        {
            **PARAMETER_SCHEMA,
            "bindings": {"unknown": {"pointer": "/target/url", "type": "string"}},
        },
        {
            **PARAMETER_SCHEMA,
            "bindings": {
                "target_url": {"pointer": "/target/url"},
                "input_text": {"pointer": "/content/text", "type": "string"},
            },
        },
        {
            **PARAMETER_SCHEMA,
            "bindings": {
                "target_url": {"pointer": "/target/url", "type": "object"},
                "input_text": {"pointer": "/content/text", "type": "string"},
            },
        },
    ],
)
def test_bind_parameters_rejects_invalid_or_undeclared_binding_paths(schema: dict) -> None:
    with pytest.raises(ParameterBindingError):
        bind_parameters(SNAPSHOT, schema, {"target_url": "https://example.test"})
