"""Contract tests for remote-action JSON Schema version 1."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "docs" / "architecture" / "messaging" / "schemas" / "remote-actions"
VECTORS_PATH = ROOT / "tests" / "fixtures" / "remote_actions" / "protocol_vectors.json"


def _vectors() -> list[dict]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


def _schema(vector: dict) -> dict:
    path = SCHEMA_DIR / vector["schema"]
    return json.loads(path.read_text(encoding="utf-8"))


def _all_schemas() -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    ]


def _registry() -> Registry:
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in _all_schemas()
    )


def _validator(schema: dict) -> Draft202012Validator:
    return Draft202012Validator(
        schema,
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def _vector(schema_name: str, definition: str | None = None) -> dict:
    return next(
        item
        for item in _vectors()
        if item["schema"] == schema_name and item.get("definition") == definition
    )


def _mutation_schema(vector: dict) -> dict:
    schema = _schema(vector)
    definition = vector.get("definition")
    if not definition:
        return schema
    return {
        "$schema": schema["$schema"],
        "$defs": schema["$defs"],
        "$ref": f"#/$defs/{definition}",
    }


def _parent_at(document: object, path: list[str | int]) -> tuple[object, str | int]:
    current = document
    for part in path[:-1]:
        current = current[part]  # type: ignore[index]
    return current, path[-1]


def _remove_at(document: dict, path: list[str | int]) -> None:
    parent, key = _parent_at(document, path)
    del parent[key]  # type: ignore[index]


def _set_at(document: dict, path: list[str | int], value: object) -> None:
    parent, key = _parent_at(document, path)
    parent[key] = value  # type: ignore[index]


def _oversize_value(kind: str, length: int | None = None) -> object:
    if kind == "string":
        return "x" * (length or 81)
    if kind == "array":
        return [f"evt_{index:04d}" for index in range(257)]
    raise AssertionError(f"unsupported oversize kind: {kind}")


@pytest.mark.parametrize("vector", _vectors(), ids=lambda item: item["schema"])
def test_golden_vector_is_valid(vector: dict) -> None:
    schema = _schema(vector)
    Draft202012Validator.check_schema(schema)
    validator = _validator(schema)

    validator.validate(vector["valid"])


@pytest.mark.parametrize("vector", _vectors(), ids=lambda item: item["schema"])
@pytest.mark.parametrize("invalid_kind", ["missing", "wrong_type", "extra", "oversize"])
def test_invalid_vectors_are_rejected(vector: dict, invalid_kind: str) -> None:
    payload = copy.deepcopy(vector["valid"])
    mutation = vector[invalid_kind]
    if invalid_kind == "missing":
        _remove_at(payload, mutation["path"])
    elif invalid_kind == "wrong_type":
        _set_at(payload, mutation["path"], mutation["value"])
    elif invalid_kind == "extra":
        payload[mutation["property"]] = mutation["value"]
    else:
        _set_at(
            payload,
            mutation["path"],
            _oversize_value(mutation["kind"], mutation.get("length")),
        )

    validator = _validator(_mutation_schema(vector))
    errors = list(validator.iter_errors(payload))
    if invalid_kind == "oversize":
        expected_validator = "maxLength" if mutation["kind"] == "string" else "maxItems"
    else:
        expected_validator = {
            "missing": "required",
            "wrong_type": mutation.get("validator", "type"),
            "extra": "additionalProperties",
        }[invalid_kind]
    expected_path = mutation.get(
        "expected_path",
        mutation["path"][:-1] if invalid_kind == "missing" else mutation.get("path", []),
    )
    assert any(
        error.validator == expected_validator and list(error.path) == expected_path
        for error in errors
    ), (
        f"{vector['schema']} accepted {invalid_kind} mutation: {payload}"
    )


def test_wss_direction_controls_server_sequence() -> None:
    validator = _validator(_schema(_vector("wss-envelope-v1.schema.json")))
    payloads = {
        "WORK_ORDER_DELIVER": _vector("work-order-v1.schema.json")["valid"],
        "COMMAND_ACK": _vector("command-ack-v1.schema.json")["valid"],
        "WORK_ORDER_CANCEL": _vector("cancel-command-v1.schema.json")["valid"],
        "PROGRESS_EVENT": _vector("progress-event-v1.schema.json")["valid"],
        "TERMINAL_REFERENCE": _vector("terminal-reference-v1.schema.json")["valid"],
        "RECONCILE_REQUEST": _vector("reconcile-request-v1.schema.json")["valid"],
        "RECONCILE_RESPONSE": _vector("reconcile-response-v1.schema.json")["valid"],
    }
    central_types = {"WORK_ORDER_DELIVER", "WORK_ORDER_CANCEL", "RECONCILE_RESPONSE"}
    for index, (message_type, payload) in enumerate(payloads.items(), start=1):
        envelope = {
            "type": message_type,
            "protocol_version": "1.0",
            "message_id": f"msg_binding_{index}",
            "device_id": "dev_binding",
            "session_id": "sess_binding",
            "sent_at": "2026-08-20T10:00:00.000Z",
            "payload": payload,
        }
        if message_type in central_types:
            envelope["server_sequence"] = index
        validator.validate(envelope)

        wrong_direction = copy.deepcopy(envelope)
        if message_type in central_types:
            del wrong_direction["server_sequence"]
        else:
            wrong_direction["server_sequence"] = index
        assert list(validator.iter_errors(wrong_direction))

        wrong_payload = copy.deepcopy(envelope)
        wrong_payload["payload"] = {}
        assert list(validator.iter_errors(wrong_payload))


def _walk_properties(schema: dict):
    for name, child in schema.get("properties", {}).items():
        yield name, child
        if isinstance(child, dict):
            yield from _walk_properties(child)
    for child in schema.get("$defs", {}).values():
        yield from _walk_properties(child)


def test_id_fields_use_ascii_prefixes_and_80_character_limit() -> None:
    for schema in _all_schemas():
        for name, definition in _walk_properties(schema):
            field_type = definition.get("type")
            is_string = field_type == "string" or (
                isinstance(field_type, list) and "string" in field_type
            )
            if name.endswith("_id") and is_string:
                assert definition.get("maxLength", 80) <= 80, (schema["$id"], name)
                assert str(definition.get("pattern", "")).startswith("^"), (
                    schema["$id"],
                    name,
                )


def test_sequences_and_generations_are_bounded_to_signed_int64() -> None:
    for schema in _all_schemas():
        for name, definition in _walk_properties(schema):
            if name.endswith("server_sequence") or name.endswith("_generation"):
                assert definition["maximum"] == 9223372036854775807


def test_timestamps_require_utc_millisecond_precision() -> None:
    for schema in _all_schemas():
        for name, definition in _walk_properties(schema):
            if name.endswith("_at"):
                assert definition["pattern"].endswith("Z$")


def test_schema_size_annotations_are_frozen() -> None:
    envelope = _schema(_vector("wss-envelope-v1.schema.json"))
    work_order = _schema(_vector("work-order-v1.schema.json"))
    assert envelope["x-maxBytes"] == 1024 * 1024
    assert envelope["x-maxDepth"] == 32
    assert work_order["$defs"]["definition"]["properties"]["snapshot"]["x-maxBytes"] == 512 * 1024
    assert work_order["properties"]["runtime_params"]["x-maxBytes"] == 256 * 1024


@pytest.mark.parametrize(
    ("schema_name", "definition", "path"),
    [
        ("work-order-v1.schema.json", None, ["definition"]),
        ("work-order-v1.schema.json", None, ["resource_reservations", 0]),
        ("work-order-v1.schema.json", None, ["effect_plan", 0]),
        ("work-order-v1.schema.json", None, ["execution_policy"]),
        ("reconcile-request-v1.schema.json", None, ["active_work_orders", 0]),
        ("reconcile-response-v1.schema.json", None, ["missing_commands", 0]),
        ("reconcile-response-v1.schema.json", None, ["valid_leases", 0]),
        ("execution-outcome-v1.schema.json", "outcome_request", ["effects", 0]),
        ("execution-outcome-v1.schema.json", "outcome_request", ["error"]),
        ("execution-outcome-v1.schema.json", "outcome_request", ["evidence_manifest", 0]),
    ],
)
def test_nested_objects_reject_extra_properties(
    schema_name: str,
    definition: str | None,
    path: list[str | int],
) -> None:
    vector = _vector(schema_name, definition)
    payload = copy.deepcopy(vector["valid"])
    target = payload
    for part in path:
        target = target[part]
    target["unexpected"] = True
    errors = list(_validator(_mutation_schema(vector)).iter_errors(payload))

    def error_tree(error):
        yield error
        for child in error.context:
            yield from error_tree(child)

    errors = [nested for error in errors for nested in error_tree(error)]
    assert any(
        error.validator == "additionalProperties" and list(error.absolute_path) == path
        for error in errors
    ), [
        (error.validator, list(error.absolute_path), error.message) for error in errors
    ]


def test_command_ack_conditional_fields_are_strict() -> None:
    validator = _validator(_schema(_vector("command-ack-v1.schema.json")))
    received = copy.deepcopy(_vector("command-ack-v1.schema.json")["valid"])

    rejected = {**received, "ack_kind": "REJECTED"}
    assert list(validator.iter_errors(rejected))
    rejected["rejection_code"] = "RESOURCE_BUSY"
    validator.validate(rejected)

    terminal = {**received, "ack_kind": "ALREADY_TERMINAL"}
    assert list(validator.iter_errors(terminal))
    terminal["result_event_id"] = "evt_terminal_1"
    validator.validate(terminal)

    received["rejection_code"] = "RESOURCE_BUSY"
    assert list(validator.iter_errors(received))


def test_outcome_rejects_contradictory_terminal_states() -> None:
    vector = _vector("execution-outcome-v1.schema.json", "outcome_request")
    validator = _validator(_mutation_schema(vector))
    succeeded = copy.deepcopy(vector["valid"])

    succeeded["side_effect_state"] = "UNCERTAIN"
    succeeded["effects"][0]["status"] = "UNCERTAIN"
    succeeded["error"] = {
        "category": "SIDE_EFFECT_UNCERTAIN",
        "code": "unknown",
        "stage": "submit",
    }
    assert list(validator.iter_errors(succeeded))

    permit_missing = copy.deepcopy(vector["valid"])
    permit_missing["effects"][0]["permit_id"] = None
    assert list(validator.iter_errors(permit_missing))

    failed = copy.deepcopy(
        next(
            item["valid"]
            for item in _vectors()
            if item["schema"] == "execution-outcome-v1.schema.json"
            and item.get("definition") == "outcome_request"
            and item["valid"]["status"] == "FAILED"
        )
    )
    failed["error"] = {"category": "", "code": "", "stage": ""}
    assert list(validator.iter_errors(failed))


def test_ids_reject_wrong_prefix_unicode_and_81_characters() -> None:
    vector = _vector("work-order-v1.schema.json")
    validator = _validator(_schema(vector))
    for value in ["wrong_1", "cmd_命令", "cmd_" + ("x" * 76) + "y"]:
        payload = copy.deepcopy(vector["valid"])
        payload["command_id"] = value
        assert list(validator.iter_errors(payload)), value

    payload = copy.deepcopy(vector["valid"])
    payload["definition"]["action_id"] = "act_" + ("Z" * 26)
    assert list(validator.iter_errors(payload))


def test_timestamps_reject_missing_milliseconds_and_offsets() -> None:
    vector = _vector("command-ack-v1.schema.json")
    validator = _validator(_schema(vector))
    for value in ["2026-08-20T10:00:00Z", "2026-08-20T18:00:00.000+08:00"]:
        payload = copy.deepcopy(vector["valid"])
        payload["persisted_at"] = value
        assert list(validator.iter_errors(payload)), value


def test_int64_fields_reject_overflow() -> None:
    vector = _vector("work-order-v1.schema.json")
    validator = _validator(_schema(vector))
    payload = copy.deepcopy(vector["valid"])
    payload["lease_generation"] = 9223372036854775808
    assert any(
        error.validator == "maximum" and list(error.path) == ["lease_generation"]
        for error in validator.iter_errors(payload)
    )


def test_protocol_enums_are_frozen() -> None:
    ack = _schema(_vector("command-ack-v1.schema.json"))
    outcome = _schema(_vector("execution-outcome-v1.schema.json", "outcome_request"))
    assert ack["properties"]["ack_kind"]["enum"] == [
        "RECEIVED",
        "ACCEPTED",
        "REJECTED",
        "ALREADY_TERMINAL",
    ]
    assert outcome["$defs"]["outcome_request"]["properties"]["status"]["enum"] == [
        "SUCCEEDED",
        "PARTIALLY_SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "UNVERIFIED",
    ]
    assert outcome["$defs"]["error"]["properties"]["category"]["enum"] == [
        "",
        "VALIDATION",
        "RESOURCE",
        "TRANSIENT",
        "BUSINESS",
        "CANCELLED",
        "SIDE_EFFECT_UNCERTAIN",
    ]


def test_all_required_schema_files_have_vectors() -> None:
    expected = {
        "wss-envelope-v1.schema.json",
        "work-order-v1.schema.json",
        "command-ack-v1.schema.json",
        "cancel-command-v1.schema.json",
        "progress-event-v1.schema.json",
        "terminal-reference-v1.schema.json",
        "reconcile-request-v1.schema.json",
        "reconcile-response-v1.schema.json",
        "effect-permit-v1.schema.json",
        "execution-outcome-v1.schema.json",
    }
    assert {item["schema"] for item in _vectors()} == expected
