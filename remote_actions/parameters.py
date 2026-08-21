"""Allow-list-only runtime parameter binding for frozen action snapshots."""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError

from .publication import is_https_url


_FORMAT_CHECKER = FormatChecker()
_FORMAT_CHECKER.checks("https-url")(is_https_url)


class ParameterBindingError(ValueError):
    """Raised when runtime values or declared binding locations are invalid."""


def _decode_pointer(pointer: str) -> list[str]:
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ParameterBindingError("binding pointer must be an absolute JSON Pointer")
    tokens: list[str] = []
    for raw_token in pointer[1:].split("/"):
        index = 0
        decoded = ""
        while index < len(raw_token):
            character = raw_token[index]
            if character != "~":
                decoded += character
                index += 1
                continue
            if index + 1 >= len(raw_token) or raw_token[index + 1] not in "01":
                raise ParameterBindingError("binding pointer has an invalid escape")
            decoded += "~" if raw_token[index + 1] == "0" else "/"
            index += 2
        if decoded == "-":
            raise ParameterBindingError("binding pointer cannot append array items")
        tokens.append(decoded)
    return tokens


def _replace_existing(document: Any, tokens: list[str], value: Any) -> None:
    if not tokens:
        raise ParameterBindingError("binding pointer cannot replace the snapshot root")
    current = document
    for token in tokens[:-1]:
        if isinstance(current, dict) and token in current:
            current = current[token]
        elif isinstance(current, list) and token.isdigit() and int(token) < len(current):
            current = current[int(token)]
        else:
            raise ParameterBindingError("binding pointer does not resolve in snapshot")

    final = tokens[-1]
    if isinstance(current, dict) and final in current:
        current[final] = copy.deepcopy(value)
    elif isinstance(current, list) and final.isdigit() and int(final) < len(current):
        current[int(final)] = copy.deepcopy(value)
    else:
        raise ParameterBindingError("binding pointer does not resolve in snapshot")


def _validation_schema(parameter_schema: dict[str, Any]) -> dict[str, Any]:
    schema = copy.deepcopy(parameter_schema)
    schema.pop("bindings", None)
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), dict):
        raise ParameterBindingError("parameter schema must declare object properties")
    if schema.get("additionalProperties") is not False:
        raise ParameterBindingError("parameter schema must reject additional properties")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ParameterBindingError("invalid parameter JSON Schema") from exc
    return schema


def validate_parameter_contract(
    snapshot: dict[str, Any],
    parameter_schema: dict[str, Any],
) -> None:
    """Validate publish-time bindings without requiring concrete runtime values."""

    if not isinstance(snapshot, dict) or not isinstance(parameter_schema, dict):
        raise ParameterBindingError("snapshot and parameter schema must be objects")
    schema = _validation_schema(parameter_schema)
    bindings = parameter_schema.get("bindings")
    if not isinstance(bindings, dict):
        raise ParameterBindingError("parameter schema must declare bindings")
    declared = set(schema["properties"])
    if set(bindings) != declared:
        raise ParameterBindingError("every declared parameter must have one binding")

    pointers: list[str] = []
    for name, binding in bindings.items():
        if not isinstance(binding, dict) or set(binding) != {"pointer", "type"}:
            raise ParameterBindingError("binding declaration is invalid")
        pointer = binding.get("pointer")
        if not isinstance(pointer, str):
            raise ParameterBindingError("binding declaration is missing pointer")
        if binding["type"] != schema["properties"][name].get("type"):
            raise ParameterBindingError("binding type conflicts with parameter schema")
        _replace_existing(copy.deepcopy(snapshot), _decode_pointer(pointer), None)
        pointers.append(pointer)
    if len(pointers) != len(set(pointers)):
        raise ParameterBindingError("parameter bindings must target unique pointers")


def bind_parameters(
    snapshot: dict[str, Any],
    parameter_schema: dict[str, Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    """Return a copy with values applied only at declared, existing pointers."""

    if not isinstance(snapshot, dict) or not isinstance(values, dict):
        raise ParameterBindingError("snapshot and values must be objects")
    if not isinstance(parameter_schema, dict):
        raise ParameterBindingError("parameter schema must be an object")

    schema = _validation_schema(parameter_schema)
    validate_parameter_contract(snapshot, parameter_schema)
    bindings = parameter_schema.get("bindings")
    assert isinstance(bindings, dict)
    declared = set(schema["properties"])
    if not set(bindings).issubset(declared):
        raise ParameterBindingError("binding name is not a declared parameter")
    if not set(values).issubset(bindings):
        raise ParameterBindingError("runtime parameter has no declared binding")

    try:
        Draft202012Validator(schema, format_checker=_FORMAT_CHECKER).validate(values)
    except ValidationError as exc:
        raise ParameterBindingError("runtime parameters do not match their schema") from exc

    result = copy.deepcopy(snapshot)
    for name, value in values.items():
        binding = bindings[name]
        if not isinstance(binding, dict) or set(binding) != {"pointer", "type"}:
            raise ParameterBindingError("binding declaration is invalid")
        if "pointer" not in binding:
            raise ParameterBindingError("binding declaration is missing pointer")
        if binding["type"] != schema["properties"][name].get("type"):
            raise ParameterBindingError("binding type conflicts with parameter schema")
        _replace_existing(result, _decode_pointer(binding["pointer"]), value)
    return result
