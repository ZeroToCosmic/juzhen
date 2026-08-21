from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    CLEANUP_BLOCKED = "cleanup_blocked"


class ProfileStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    CONNECTING_CDP = "connecting_cdp"
    TILING = "tiling"
    NAVIGATING = "navigating"
    WAITING_READINESS = "waiting_readiness"
    EXECUTING = "executing"
    CAPTURING_EVIDENCE = "capturing_evidence"
    CLOSING = "closing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CLEANUP_FAILED = "cleanup_failed"


class Stage(StrEnum):
    ADSPOWER_START = "adspower_start"
    CDP_CONNECT = "cdp_connect"
    WINDOW_TILE = "window_tile"
    NAVIGATE = "navigate"
    READINESS = "readiness"
    LOCATE_ELEMENT = "locate_element"
    EXECUTE_ACTION = "execute_action"
    CAPTURE_EVIDENCE = "capture_evidence"
    ADSPOWER_STOP = "adspower_stop"


@dataclass(frozen=True, slots=True)
class BrowserBinding:
    profile_id: str
    ws_url: str
    browser: Any
    context: Any
    page: Any


@dataclass(frozen=True, slots=True)
class ProfileOutcome:
    profile_id: str
    succeeded: bool
    stage: Stage
    error_code: str = ""
    error_summary: str = ""
    action_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
