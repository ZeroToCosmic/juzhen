"""Globally unique, lexicographically sortable action identifiers."""

from __future__ import annotations

import secrets
import re
import threading
import time


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_MAX_TIMESTAMP = (1 << 48) - 1
_MAX_RANDOM = (1 << 80) - 1
_lock = threading.Lock()
_last_timestamp = -1
_last_random = -1
ACTION_ID_PATTERN = re.compile(r"^act_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


def validate_action_id(value: object) -> str:
    if not isinstance(value, str) or not ACTION_ID_PATTERN.fullmatch(value):
        raise ValueError("invalid action_id")
    return value


def _encode_ulid(value: int) -> str:
    characters = ["0"] * 26
    for index in range(25, -1, -1):
        characters[index] = _CROCKFORD[value & 31]
        value >>= 5
    return "".join(characters)


def new_action_id(
    *,
    timestamp_ms: int | None = None,
    random_bytes: bytes | None = None,
) -> str:
    """Create an `act_` ULID, monotonic for calls in the same millisecond."""

    global _last_random, _last_timestamp

    timestamp = int(time.time_ns() // 1_000_000) if timestamp_ms is None else timestamp_ms
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("timestamp_ms must be an integer")
    if timestamp < 0 or timestamp > _MAX_TIMESTAMP:
        raise ValueError("timestamp_ms is outside the ULID range")

    entropy = secrets.token_bytes(10) if random_bytes is None else random_bytes
    if not isinstance(entropy, bytes) or len(entropy) != 10:
        raise ValueError("random_bytes must contain exactly 10 bytes")
    candidate = int.from_bytes(entropy, "big")

    with _lock:
        if timestamp == _last_timestamp and candidate <= _last_random:
            if _last_random == _MAX_RANDOM:
                raise OverflowError("ULID randomness exhausted for this millisecond")
            candidate = _last_random + 1
        _last_timestamp = timestamp
        _last_random = candidate

    return f"act_{_encode_ulid((timestamp << 80) | candidate)}"
