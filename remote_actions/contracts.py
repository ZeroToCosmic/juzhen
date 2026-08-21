"""Fail-closed JSON decoding and JSON Schema validation."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource

from .enums import MessageType
from .parameters import ParameterBindingError, validate_parameter_contract


PROTOCOL_VERSION = "1.0"
MAX_WSS_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_RUNTIME_PARAMS_BYTES = 256 * 1024
MAX_RELEASE_CONTENT_BYTES = MAX_WSS_BYTES - (64 * 1024)
MAX_JSON_DEPTH = 32

_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_DIR = _ROOT / "docs" / "architecture" / "messaging" / "schemas" / "remote-actions"
_MESSAGE_SCHEMAS: Mapping[MessageType, str] = {
    MessageType.WORK_ORDER_DELIVER: "work-order-v1.schema.json",
    MessageType.COMMAND_ACK: "command-ack-v1.schema.json",
    MessageType.WORK_ORDER_CANCEL: "cancel-command-v1.schema.json",
    MessageType.PROGRESS_EVENT: "progress-event-v1.schema.json",
    MessageType.TERMINAL_REFERENCE: "terminal-reference-v1.schema.json",
    MessageType.RECONCILE_REQUEST: "reconcile-request-v1.schema.json",
    MessageType.RECONCILE_RESPONSE: "reconcile-response-v1.schema.json",
}


class ContractDecodeError(ValueError):
    """Raised when JSON cannot be decoded without weakening the contract."""


class MessageTooLargeError(ContractDecodeError):
    """Raised when a wire message or frozen subdocument exceeds its byte limit."""


class ProtocolVersionError(ValueError):
    """Raised when the peer asks for an unsupported protocol version."""


class ContractSemanticError(ValueError):
    """Raised when related fields are individually valid but contradictory."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractDecodeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ContractDecodeError(f"non-finite JSON number: {value}")


def _assert_strict_json(value: Any, depth: int = 1) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ContractDecodeError(f"JSON depth exceeds {MAX_JSON_DEPTH}")
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractDecodeError("non-finite JSON number")
        return
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ContractDecodeError("JSON object keys must be strings")
            _assert_strict_json(child, depth + 1)
        return
    if isinstance(value, list):
        for child in value:
            _assert_strict_json(child, depth + 1)
        return
    raise ContractDecodeError(f"unsupported JSON value type: {type(value).__name__}")


def load_json_strict(raw: bytes | str, *, max_bytes: int = MAX_WSS_BYTES) -> Any:
    """Decode one JSON value while rejecting ambiguous or unsafe extensions."""

    if isinstance(raw, bytes):
        encoded = raw
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ContractDecodeError("JSON must be valid UTF-8") from exc
    elif isinstance(raw, str):
        text = raw
        encoded = raw.encode("utf-8")
    else:
        raise TypeError("raw JSON must be bytes or str")

    if len(encoded) > max_bytes:
        raise MessageTooLargeError(f"JSON message exceeds {max_bytes} bytes")

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except ContractDecodeError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ContractDecodeError("invalid JSON") from exc
    _assert_strict_json(value)
    return value


def _encoded_size(value: Any, label: str) -> int:
    _assert_strict_json(value)
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ContractDecodeError(f"{label} is not strict JSON") from exc
    return len(encoded)


def _assert_max_encoded_size(value: Any, limit: int, label: str) -> None:
    if _encoded_size(value, label) > limit:
        raise MessageTooLargeError(f"{label} exceeds {limit} bytes")


@lru_cache(maxsize=1)
def _schemas() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(_SCHEMA_DIR.glob("*.schema.json"))
    }


@lru_cache(maxsize=1)
def _registry() -> Registry:
    return Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema))
        for schema in _schemas().values()
    )


@lru_cache(maxsize=None)
def _validator(schema_name: str) -> Draft202012Validator:
    return Draft202012Validator(
        _schemas()[schema_name],
        registry=_registry(),
        format_checker=FormatChecker(),
    )


def validate_message(message_type: MessageType | str, payload: dict[str, Any]) -> None:
    """Validate one message payload and enforce non-Schema byte/depth limits."""

    kind = MessageType(message_type)
    if not isinstance(payload, dict):
        raise ContractDecodeError("message payload must be an object")
    _assert_max_encoded_size(payload, MAX_WSS_BYTES, "message payload")
    if kind is MessageType.WORK_ORDER_DELIVER:
        definition = payload.get("definition")
        if isinstance(definition, dict) and "snapshot" in definition:
            _assert_max_encoded_size(
                definition["snapshot"],
                MAX_SNAPSHOT_BYTES,
                "snapshot",
            )
        if "runtime_params" in payload:
            _assert_max_encoded_size(
                payload["runtime_params"],
                MAX_RUNTIME_PARAMS_BYTES,
                "runtime_params",
            )
    _validator(_MESSAGE_SCHEMAS[kind]).validate(payload)
    if kind is MessageType.WORK_ORDER_DELIVER:
        _validate_work_order_semantics(payload)


def validate_release_content(content: dict[str, Any]) -> None:
    """Ensure frozen action content can be embedded in a legal WorkOrder."""

    if not isinstance(content, dict):
        raise ContractDecodeError("release content must be an object")
    snapshot = content.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ContractDecodeError("release snapshot must be an object")
    _assert_max_encoded_size(snapshot, MAX_SNAPSHOT_BYTES, "snapshot")
    _assert_max_encoded_size(content, MAX_RELEASE_CONTENT_BYTES, "release content")
    required = {
        "executor_kind",
        "definition_schema_version",
        "parameter_schema",
        "result_schema",
        "snapshot",
        "execution_defaults",
    }
    if set(content) != required:
        raise ContractSemanticError("release content fields do not match the contract")
    try:
        validate_parameter_contract(snapshot, content["parameter_schema"])
        Draft202012Validator.check_schema(content["result_schema"])
    except (ParameterBindingError, SchemaError, TypeError) as exc:
        raise ContractSemanticError(f"invalid release schema: {exc}") from exc


def _validate_work_order_semantics(payload: dict[str, Any]) -> None:
    for reservation in payload["resource_reservations"]:
        if reservation["resource_type"] == "account":
            expected = f"account:{reservation['account_id']}"
        else:
            expected = f"window:{reservation['device_id']}:{reservation['window_ref']}"
        if reservation["resource_key"] != expected:
            raise ContractSemanticError("resource_key does not match reservation identity")

    effect_ids = [effect["effect_id"] for effect in payload["effect_plan"]]
    if len(effect_ids) != len(set(effect_ids)):
        raise ContractSemanticError("effect_id values must be unique")
    known_effects = set(effect_ids)
    for effect in payload["effect_plan"]:
        dependencies = effect["dependencies"]
        if len(dependencies) != len(set(dependencies)):
            raise ContractSemanticError("effect dependencies must be unique")
        if effect["effect_id"] in dependencies:
            raise ContractSemanticError("effect cannot depend on itself")
        if not set(dependencies).issubset(known_effects):
            raise ContractSemanticError("effect dependency is not in the effect plan")

    dependencies_by_effect = {
        effect["effect_id"]: effect["dependencies"] for effect in payload["effect_plan"]
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(effect_id: str) -> None:
        if effect_id in visiting:
            raise ContractSemanticError("effect dependency graph contains a cycle")
        if effect_id in visited:
            return
        visiting.add(effect_id)
        for dependency in dependencies_by_effect[effect_id]:
            visit(dependency)
        visiting.remove(effect_id)
        visited.add(effect_id)

    for effect_id in effect_ids:
        visit(effect_id)


def validate_execution_outcome(
    payload: dict[str, Any],
    *,
    expected_effect_ids: set[str] | None = None,
) -> None:
    """Validate an Outcome and, when known, its frozen WorkOrder effect set."""

    if not isinstance(payload, dict):
        raise ContractDecodeError("execution outcome must be an object")
    _assert_max_encoded_size(payload, MAX_WSS_BYTES, "execution outcome")
    outcome_schema = _schemas()["execution-outcome-v1.schema.json"]
    schema = {
        "$schema": outcome_schema["$schema"],
        "$defs": outcome_schema["$defs"],
        "$ref": "#/$defs/outcome_request",
    }
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)
    effect_ids = [effect["effect_id"] for effect in payload["effects"]]
    if len(effect_ids) != len(set(effect_ids)):
        raise ContractSemanticError("outcome effect_id values must be unique")
    if expected_effect_ids is not None and set(effect_ids) != expected_effect_ids:
        raise ContractSemanticError("outcome effects do not match the frozen effect plan")


def parse_wss_message(raw: bytes | str) -> tuple[MessageType, dict[str, Any]]:
    """Strictly decode and validate one raw WSS message."""

    message = load_json_strict(raw, max_bytes=MAX_WSS_BYTES)
    if not isinstance(message, dict):
        raise ContractDecodeError("WSS envelope must be an object")
    if message.get("protocol_version") != PROTOCOL_VERSION:
        raise ProtocolVersionError(
            f"unsupported protocol version: {message.get('protocol_version')!r}"
        )
    _assert_max_encoded_size(message, MAX_WSS_BYTES, "WSS message")
    _validator("wss-envelope-v1.schema.json").validate(message)
    kind = MessageType(message["type"])
    validate_message(kind, message["payload"])
    return kind, message


def validate_wss_message(raw: bytes | str) -> MessageType:
    """Validate one raw WSS message and return its frozen message type."""

    return parse_wss_message(raw)[0]
