"""Strict identity contract for the Comment Campaign worker health lease."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re


_HEALTH_VALUE = re.compile(
    r"worker:v2:(?P<pid>[1-9][0-9]{0,19}):"
    r"(?P<project_fingerprint>[0-9a-f]{64}):"
    r"(?P<owner_nonce>[0-9a-f]{32})"
)
_LEGACY_HEALTH_VALUE = re.compile(
    r"worker:(?P<pid>[1-9][0-9]{0,19}):[0-9a-f]{32}"
)


@dataclass(frozen=True)
class WorkerIdentity:
    pid: int
    project_fingerprint: str
    owner_nonce: str


def project_fingerprint(root: str | Path) -> str:
    normalized = os.path.normcase(str(Path(root).resolve()))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_worker_health_value(
    pid: int,
    root: str | Path,
    owner_nonce: str,
) -> str:
    if type(pid) is not int or pid <= 0:
        raise ValueError("worker pid must be a positive integer")
    fingerprint = project_fingerprint(root)
    value = f"worker:v2:{pid}:{fingerprint}:{owner_nonce}"
    if _HEALTH_VALUE.fullmatch(value) is None:
        raise ValueError("worker owner nonce is invalid")
    return value


def parse_worker_health_value(value: object) -> WorkerIdentity | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    match = _HEALTH_VALUE.fullmatch(value)
    if match is None:
        return None
    return WorkerIdentity(
        pid=int(match.group("pid")),
        project_fingerprint=match.group("project_fingerprint"),
        owner_nonce=match.group("owner_nonce"),
    )


def extract_legacy_worker_pid(value: object) -> int | None:
    """Extract only the PID from the known legacy shape for manual cleanup."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    match = _LEGACY_HEALTH_VALUE.fullmatch(value)
    return int(match.group("pid")) if match is not None else None


__all__ = [
    "WorkerIdentity",
    "build_worker_health_value",
    "extract_legacy_worker_pid",
    "parse_worker_health_value",
    "project_fingerprint",
]
