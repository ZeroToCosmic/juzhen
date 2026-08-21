"""Tests for globally unique remote-action identifiers."""

from __future__ import annotations

import re

import pytest

from remote_actions.identifiers import new_action_id, validate_action_id


ACTION_ID_PATTERN = re.compile(r"^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def test_action_id_has_fixed_ulid_shape() -> None:
    generated = new_action_id()
    assert ACTION_ID_PATTERN.fullmatch(generated)
    assert validate_action_id(generated) == generated
    with pytest.raises(ValueError, match="invalid action_id"):
        validate_action_id("act_" + ("Z" * 26))


def test_action_id_fixed_timestamp_and_randomness() -> None:
    assert new_action_id(timestamp_ms=0, random_bytes=b"\x00" * 10) == (
        "act_00000000000000000000000000"
    )


def test_action_ids_are_monotonic_within_same_millisecond() -> None:
    first = new_action_id(timestamp_ms=1_700_000_000_000, random_bytes=b"\x00" * 10)
    second = new_action_id(timestamp_ms=1_700_000_000_000, random_bytes=b"\x00" * 10)
    assert first < second


def test_new_and_copy_operations_receive_distinct_ids() -> None:
    original = new_action_id()
    copied = new_action_id()
    assert original != copied


@pytest.mark.parametrize(
    ("timestamp_ms", "random_bytes"),
    [(-1, b"\x00" * 10), (2**48, b"\x00" * 10), (0, b"too-short")],
)
def test_action_id_rejects_invalid_sources(timestamp_ms: int, random_bytes: bytes) -> None:
    with pytest.raises(ValueError):
        new_action_id(timestamp_ms=timestamp_ms, random_bytes=random_bytes)
