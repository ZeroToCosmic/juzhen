"""Golden-vector tests for immutable action checksums."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from remote_actions.checksums import (
    ChecksumError,
    content_checksum,
    content_projection,
    release_checksum,
)


ROOT = Path(__file__).resolve().parents[1]
VECTORS_PATH = ROOT / "tests" / "fixtures" / "remote_actions" / "checksum_vectors.json"


def _vectors() -> list[dict]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))["vectors"]


def test_checksum_fixture_contains_fifty_independent_vectors() -> None:
    assert len(_vectors()) == 50


def test_checksum_vectors_match_python() -> None:
    for vector in _vectors():
        assert content_checksum(vector["content_input"]) == vector["content_checksum"]
        assert release_checksum(**vector["release_input"]) == vector["release_checksum"]


def test_content_projection_ignores_non_executable_metadata() -> None:
    release = _vectors()[0]["content_input"] | {
        "action_id": "act_00000000000000000000000000",
        "revision": 99,
        "name": "display only",
        "runtime_params": {"target_url": "https://ignored.test"},
    }
    assert content_projection(release) == content_projection(_vectors()[0]["content_input"])
    assert content_checksum(release) == _vectors()[0]["content_checksum"]


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), "\ud800"],
)
def test_checksum_rejects_values_rfc8785_cannot_canonicalize(value: object) -> None:
    document = dict(_vectors()[0]["content_input"])
    document["snapshot"] = {"bad": value}
    with pytest.raises(ChecksumError):
        content_checksum(document)


def test_checksum_rejects_missing_projection_fields_and_invalid_release_identity() -> None:
    incomplete = dict(_vectors()[0]["content_input"])
    incomplete.pop("snapshot")
    with pytest.raises(ChecksumError):
        content_checksum(incomplete)

    with pytest.raises(ChecksumError):
        release_checksum("local-1", 1, _vectors()[0]["content_checksum"])
    with pytest.raises(ChecksumError):
        release_checksum("act_00000000000000000000000000", 0, "sha256:bad")
    with pytest.raises(ChecksumError):
        release_checksum("act_" + ("Z" * 26), 1, _vectors()[0]["content_checksum"])


def test_checksum_integer_domain_matches_javascript_safe_integers() -> None:
    document = dict(_vectors()[0]["content_input"])
    document["snapshot"] = {"value": 2**53 - 1}
    assert content_checksum(document).startswith("sha256:")
    document["snapshot"] = {"value": 2**53}
    with pytest.raises(ChecksumError):
        content_checksum(document)

    checksum = _vectors()[0]["content_checksum"]
    assert release_checksum(
        "act_00000000000000000000000000", 2**53 - 1, checksum
    ).startswith("sha256:")
    with pytest.raises(ChecksumError):
        release_checksum("act_00000000000000000000000000", 2**53, checksum)


@pytest.mark.parametrize("value", [("array",), {"not-json"}])
def test_checksum_rejects_python_only_container_shapes(value: object) -> None:
    document = dict(_vectors()[0]["content_input"])
    document["snapshot"] = {"value": value}
    with pytest.raises(ChecksumError, match="unsupported JSON value"):
        content_checksum(document)
