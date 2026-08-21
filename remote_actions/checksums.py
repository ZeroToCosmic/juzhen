"""RFC 8785 / SHA-256 checksums for immutable action releases."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any, Mapping

import rfc8785

from .identifiers import validate_action_id


CONTENT_ALGORITHM = "action-content-jcs-sha256-v1"
RELEASE_ALGORITHM = "action-release-jcs-sha256-v1"
CONTENT_FIELDS = (
    "executor_kind",
    "definition_schema_version",
    "parameter_schema",
    "result_schema",
    "snapshot",
    "execution_defaults",
)

_CHECKSUM = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SAFE_INTEGER = (1 << 53) - 1


class ChecksumError(ValueError):
    """Raised when checksum input is incomplete or cannot be canonicalized."""


def content_projection(release: Mapping[str, Any]) -> dict[str, Any]:
    """Project only fields that affect executable action content."""

    try:
        return {field: release[field] for field in CONTENT_FIELDS}
    except (KeyError, TypeError) as exc:
        raise ChecksumError("action content is missing a checksum field") from exc


def _digest(document: Any) -> str:
    def assert_number_domain(value: Any) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int):
            if abs(value) > _MAX_SAFE_INTEGER:
                raise ChecksumError("integer exceeds the cross-language safe range")
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ChecksumError("non-finite number cannot be canonicalized")
            if value.is_integer() and abs(value) > _MAX_SAFE_INTEGER:
                raise ChecksumError("integer-valued number exceeds the safe range")
            return
        if isinstance(value, str):
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ChecksumError("JSON object keys must be strings")
                assert_number_domain(child)
            return
        if isinstance(value, list):
            for child in value:
                assert_number_domain(child)
            return
        raise ChecksumError(f"unsupported JSON value type: {type(value).__name__}")

    assert_number_domain(document)
    try:
        canonical = rfc8785.dumps(document)
    except (rfc8785.CanonicalizationError, TypeError, ValueError) as exc:
        raise ChecksumError("value cannot be canonicalized as RFC 8785 JSON") from exc
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def content_checksum(release: Mapping[str, Any]) -> str:
    return _digest(content_projection(release))


def release_checksum(action_id: str, revision: int, content_checksum: str) -> str:
    try:
        validate_action_id(action_id)
    except ValueError as exc:
        raise ChecksumError("invalid action_id") from exc
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 1
        or revision > _MAX_SAFE_INTEGER
    ):
        raise ChecksumError("revision must be a positive safe integer")
    if not isinstance(content_checksum, str) or not _CHECKSUM.fullmatch(content_checksum):
        raise ChecksumError("invalid content_checksum")
    return _digest(
        {
            "action_id": action_id,
            "revision": revision,
            "content_checksum": content_checksum,
        }
    )
