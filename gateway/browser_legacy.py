"""Legacy browser execution helpers (migrated from gateway/app.py)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import os
import random
import re
import secrets
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import requests
from flask import request, session

from adspower import AdsPowerController, AdsPowerError
from browser_element_resolver import inspect_element
from browser_public_identity import mask_profile_id
from browser_strategy_config import load_or_migrate_strategy_state
from gateway.content_store import (
    list_brands,
    list_copy_items,
    now_iso as content_now_iso,
)
from gateway.settings_store import load_settings, mutate_settings, save_settings
from selector_probe.blueprint import check_strategy_gate

def load_persisted_strategy_state() -> dict:
    """Load the version-3 strategy state and persist a legacy migration once."""

    result = {}

    def migrate(settings):
        browser, changed = load_or_migrate_strategy_state(settings.get("browser", {}))
        result["browser"] = browser
        if not changed:
            return None
        settings["browser"] = browser
        return settings

    mutate_settings(migrate)
    return result["browser"]


def mutate_persisted_strategy_state(mutator) -> dict:
    """Mutate normalized browser strategy state under the settings-store lock."""

    def update(settings):
        browser, _changed = load_or_migrate_strategy_state(settings.get("browser", {}))
        settings["browser"] = mutator(browser)
        return settings

    return mutate_settings(update)["browser"]


class _StrategyReferenceConflict(ValueError):
    def __init__(self, resource: str, references: list[dict]):
        super().__init__(f"{resource} is referenced by block strategies")
        self.references = references




PROJECT_ROOT = Path(__file__).resolve().parent.parent
ACTIVE_BROWSER_SESSIONS = {}
ACTIVE_BROWSER_SESSIONS_LOCK = threading.Lock()
ACTIVE_PATTERN_RECORDINGS = {}
ACTIVE_PATTERN_RECORDINGS_LOCK = threading.Lock()
BROWSER_SESSION_LEASES = {}
BROWSER_PROFILE_SESSION_LOCKS = {}
BROWSER_PROFILE_SESSION_LOCKS_LOCK = threading.Lock()
BROWSER_PROFILE_EXECUTIONS = set()
BROWSER_PROFILE_EXECUTIONS_LOCK = threading.Lock()
BROWSER_LOG_PATH = PROJECT_ROOT / "logs" / "browser_operations.jsonl"
BROWSER_LOG_LOCK = threading.Lock()
BROWSER_BATCH_TASKS = {}
BROWSER_BATCH_TASKS_LOCK = threading.Lock()


def is_valid_browser_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not re.search(r"\s", value)
    )


def sanitize_public_browser_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{parsed.port}" if parsed.port is not None else host
    path_segments = []
    redact_next = False
    for segment in parsed.path.split("/"):
        sensitive = is_sensitive_browser_key(segment)
        path_segments.append("[redacted]" if redact_next or sensitive else segment)
        redact_next = sensitive or segment.casefold() == "video"
    path = "/".join(path_segments)
    query = []
    for item_key, item_value in parse_qsl(parsed.query, keep_blank_values=True):
        if is_sensitive_browser_key(item_key):
            item_value = "[redacted]"
        query.append((item_key, item_value))
    fragment = sanitize_public_browser_fragment(parsed.fragment)
    return urlunsplit(
        (parsed.scheme, netloc, path, urlencode(query), fragment)
    )


def sanitize_public_browser_origin(value: str) -> str:
    parsed = urlsplit(sanitize_public_browser_url(value))
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


SENSITIVE_BROWSER_KEY_MARKERS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
)


def normalize_sensitive_browser_key(key: object) -> str:
    decoded = unquote(str(key)).lower()
    bracket_normalized = re.sub(r"\[([^\]]*)\]", r"_\1", decoded)
    return re.sub(r"[^a-z0-9]", "", bracket_normalized)


def is_sensitive_browser_key(key: object) -> bool:
    normalized = normalize_sensitive_browser_key(key)
    return any(marker in normalized for marker in SENSITIVE_BROWSER_KEY_MARKERS)


def is_sensitive_browser_payload_key(key: object, value: object) -> bool:
    normalized = normalize_sensitive_browser_key(key)
    if (
        normalized == "sessions"
        and isinstance(value, (list, tuple))
        and all(isinstance(item, dict) for item in value)
    ):
        return False
    return is_sensitive_browser_key(key)


SAFE_PUBLIC_CREDENTIAL_STATUSES = {
    "expired",
    "invalid",
    "missing",
    "not configured",
}
PUBLIC_CREDENTIAL_VALUE_PATTERN = (
    r'"[^"\r\n]*"|\'[^\'\r\n]*\'|not\s+configured|[^\s,;&]+'
)
PUBLIC_BROWSER_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>[a-z%][a-z0-9_%\[\].-]*"
    r"(?:[ \t]+[a-z0-9_%\[\].-]+){0,3})"
    r"[ \t]*(?P<separator>=|:)[ \t]*"
)
PUBLIC_BROWSER_SPACE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>"
    r"access[ _.-]*key(?:[ _.-]*id)?|"
    r"api[ _.-]*key|authorization|cookie|credential|password|"
    r"secret|session(?:[ _.-]*id)?|token"
    r")(?P<separator>[ \t]+)"
)
PUBLIC_BROWSER_HEADER_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>cookie|authorization)"
    r"[ \t]*(?P<separator>:|=)[ \t]*"
)
SAFE_PUBLIC_DIAGNOSTIC_KEYS = {
    "actionid",
    "actionindex",
    "actiontype",
    "attempts",
    "currenturl",
    "error",
    "message",
    "outcome",
    "profileid",
    "reason",
    "retry",
    "stage",
    "status",
    "targeturl",
}


def is_safe_public_credential_value(value: str) -> bool:
    normalized = value.strip().strip("\"'").casefold()
    status = normalized
    scheme_and_status = normalized.split(None, 1)
    if normalized not in SAFE_PUBLIC_CREDENTIAL_STATUSES and len(
        scheme_and_status
    ) == 2 and re.fullmatch(
        r"[a-z][a-z0-9_-]*", scheme_and_status[0]
    ):
        status = scheme_and_status[1]
    return status in SAFE_PUBLIC_CREDENTIAL_STATUSES


def is_safe_public_header_value(value: str) -> bool:
    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] in "\"'"
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1].strip()
    return normalized.casefold() in SAFE_PUBLIC_CREDENTIAL_STATUSES


def _redact_public_browser_header_line(line: str) -> str:
    match = PUBLIC_BROWSER_HEADER_PATTERN.search(line)
    if match is None or is_safe_public_header_value(line[match.end() :]):
        return line
    return f"{line[:match.end()]}[redacted]"


def sanitize_public_browser_headers(value: str) -> str:
    sanitized = []
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        sanitized.extend(
            (_redact_public_browser_header_line(body), line[len(body) :])
        )
    if not sanitized and value == "":
        return ""
    return "".join(sanitized)


def redact_public_browser_credential(match) -> str:
    if is_safe_public_credential_value(match.group("value")):
        return match.group(0)
    return f"{match.group('prefix')}[redacted]"


def _trusted_public_assignment_boundary(matches, start_index):
    for match in matches[start_index + 1 :]:
        normalized = normalize_sensitive_browser_key(match.group("key"))
        if normalized in SAFE_PUBLIC_DIAGNOSTIC_KEYS:
            return match
    return None


def _redact_public_assignment_line(line: str) -> str:
    matches = sorted(
        [
            *PUBLIC_BROWSER_ASSIGNMENT_PATTERN.finditer(line),
            *PUBLIC_BROWSER_SPACE_ASSIGNMENT_PATTERN.finditer(line),
        ],
        key=lambda match: (match.start(), -match.end()),
    )
    matches = [
        match
        for index, match in enumerate(matches)
        if index == 0 or match.start() >= matches[index - 1].end()
    ]
    intervals = []
    for index, match in enumerate(matches):
        if not is_sensitive_browser_key(match.group("key")):
            continue
        boundary = _trusted_public_assignment_boundary(matches, index)
        boundary_start = boundary.start() if boundary is not None else len(line)
        gap = line[match.end() : boundary_start]
        structural_suffix = re.search(r"([,;]?[ \t]*)$", gap)
        redact_end = (
            match.end() + structural_suffix.start()
            if structural_suffix is not None
            else boundary_start
        )
        if redact_end <= match.end():
            continue
        raw_value = line[match.end() : redact_end]
        if is_safe_public_credential_value(raw_value):
            continue
        intervals.append((match.end(), redact_end))

    if not intervals:
        return line
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    result = []
    cursor = 0
    for start, end in merged:
        result.extend((line[cursor:start], "[redacted]"))
        cursor = end
    result.append(line[cursor:])
    return "".join(result)


def sanitize_public_browser_assignments(value: str) -> str:
    sanitized = []
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        sanitized.append(_redact_public_assignment_line(body))
        sanitized.append(line[len(body) :])
    if not sanitized and value == "":
        return ""
    return "".join(sanitized)


def _sanitize_public_fragment_assignment(value: str) -> str:
    for separator in ("=", ":"):
        if separator not in value:
            continue
        key, item_value = value.split(separator, 1)
        if is_sensitive_browser_key(key) and not is_safe_public_credential_value(
            item_value
        ):
            return f"{key}{separator}[redacted]"
        return value
    return value


def sanitize_public_browser_fragment(value: str) -> str:
    segments = []
    redact_next = False
    for segment in value.split("/"):
        if redact_next:
            segments.append("[redacted]")
            redact_next = False
            continue
        if "=" in segment or ":" in segment or "&" in segment:
            segments.append(
                "&".join(
                    _sanitize_public_fragment_assignment(item)
                    for item in segment.split("&")
                )
            )
            continue
        sensitive = is_sensitive_browser_key(segment)
        segments.append("[redacted]" if sensitive else segment)
        redact_next = sensitive
    return "/".join(segments)


def sanitize_public_browser_text(
    value: str, *, redact_urls: bool = True
) -> str:
    text = value
    if redact_urls:
        text = re.sub(
            r"\b(?:wss?|https?)://[^\s,;\)\]\}>\"']+",
            "[redacted-url]",
            text,
            flags=re.IGNORECASE,
        )
    text = re.sub(
        rf"(?i)(?P<prefix>\bbearer\s+)"
        rf"(?P<value>{PUBLIC_CREDENTIAL_VALUE_PATTERN})",
        redact_public_browser_credential,
        text,
    )
    text = sanitize_public_browser_headers(text)
    text = sanitize_public_browser_assignments(text)
    return re.sub(
        r"(?i)/devtools/browser/[^\s,;\)\]\}>\"']+",
        "[redacted-devtools-endpoint]",
        text,
    )


def public_browser_payload(value, key: str = ""):
    from gateway.browser_orchestrator import public_session_result

    if normalize_sensitive_browser_key(key) == "profileid":
        return mask_profile_id(value)
    if isinstance(value, dict):
        public = {}
        for item_key, item_value in value.items():
            if is_sensitive_browser_payload_key(item_key, item_value):
                normalized_key = normalize_sensitive_browser_key(item_key)
                normalized_value = (
                    str(item_value).strip().casefold()
                    if isinstance(item_value, str)
                    else ""
                )
                if (
                    normalized_key.endswith("status")
                    and normalized_value in SAFE_PUBLIC_CREDENTIAL_STATUSES
                ):
                    public[item_key] = normalized_value
                continue
            if item_key not in public_session_result({item_key: None}):
                continue
            public[item_key] = public_browser_payload(item_value, str(item_key))
        return public
    if isinstance(value, (list, tuple)):
        return [public_browser_payload(item) for item in value]
    if isinstance(value, BaseException):
        return public_browser_payload(str(value), key)
    if isinstance(value, str) and (
        key == "origin" or key.endswith("_origin")
    ):
        if is_valid_browser_url(value):
            return sanitize_public_browser_origin(value)
    if isinstance(value, str) and key in {"url", "target_url", "current_url"}:
        if is_valid_browser_url(value):
            return sanitize_public_browser_url(value)
    if isinstance(value, str):
        return sanitize_public_browser_text(value)
    return value


def sanitize_browser_log_file() -> None:
    """Rewrite legacy browser logs through the current public-data boundary."""

    if not BROWSER_LOG_PATH.exists():
        return
    temporary_path = BROWSER_LOG_PATH.with_name(
        f".{BROWSER_LOG_PATH.name}.{uuid4().hex}.tmp"
    )
    try:
        with BROWSER_LOG_LOCK:
            sanitized_lines = []
            for line in BROWSER_LOG_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    # A malformed legacy line cannot be proven safe to retain.
                    continue
                sanitized_lines.append(
                    json.dumps(public_browser_payload(entry), ensure_ascii=False)
                )
            payload = "\n".join(sanitized_lines)
            if payload:
                payload += "\n"
            temporary_path.write_text(payload, encoding="utf-8")
            os.replace(temporary_path, BROWSER_LOG_PATH)
    except OSError as error:
        LOGGER.warning("Browser log sanitization failed: %s", error)
    finally:
        temporary_path.unlink(missing_ok=True)


def record_browser_log(operation: str, payload: dict) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "payload": public_browser_payload(payload),
    }
    try:
        BROWSER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with BROWSER_LOG_LOCK:
            with BROWSER_LOG_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def get_adspower_base_url():
    return (
        load_settings()
        .get("adspower", {})
        .get("base_url", "http://local.adspower.net:50325")
    ).rstrip("/")


def get_adspower_headers():
    api_key = str(
        load_settings()
        .get("adspower", {})
        .get("api_key", "")
    ).strip() or os.getenv("ADSPOWER_API_KEY", "").strip()
    if not api_key:
        return None
    return {"Authorization": f"Bearer {api_key}"}


def selected_browser_sessions(selected):
    requested = selected or []
    requested_ids = []
    for item in requested:
        profile_id = item if isinstance(item, str) else item.get("profile_id")
        if profile_id:
            requested_ids.append(str(profile_id))
    if not requested_ids:
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            requested_ids = list(ACTIVE_BROWSER_SESSIONS)
    requested_ids = list(dict.fromkeys(requested_ids))
    sessions = []
    for profile_id in requested_ids:
        with browser_profile_session_lock(profile_id):
            with ACTIVE_BROWSER_SESSIONS_LOCK:
                ws_url = ACTIVE_BROWSER_SESSIONS.get(profile_id)
                if ws_url and not _acquire_browser_session_use_locked(
                    profile_id, ws_url
                ):
                    ws_url = None
            sessions.append((profile_id, ws_url))
    return sessions


def release_selected_browser_sessions(sessions) -> None:
    for profile_id, ws_url in sessions:
        if ws_url:
            release_browser_session_use(profile_id, ws_url)


def normalize_selected_browser_profiles(selected) -> list[dict]:
    if not isinstance(selected, list) or not 1 <= len(selected) <= 8:
        raise ValueError("请选择 1 到 8 个浏览器窗口")
    profiles = []
    for item in selected:
        if isinstance(item, str):
            item = {"profile_id": item}
        if not isinstance(item, dict):
            raise ValueError("窗口选择格式无效")
        profile_id = str(item.get("profile_id") or "").strip()
        if not profile_id:
            raise ValueError("每个窗口都必须包含 profile_id")
        profiles.append(
            {
                "profile_id": profile_id,
                "profile_no": str(item.get("profile_no") or ""),
                "name": str(item.get("name") or ""),
            }
        )
    return profiles


class BrowserExecutionBusyError(RuntimeError):
    stage = "execution_busy"
    current_url = ""
    page_recoveries = ()

    def __init__(self, profile_id: str):
        self.reason = "profile already has a strategy execution in progress"
        super().__init__(self.reason)


@contextmanager
def browser_profile_execution_reservation(profile_id: str):
    profile_id = str(profile_id)
    with BROWSER_PROFILE_EXECUTIONS_LOCK:
        if profile_id in BROWSER_PROFILE_EXECUTIONS:
            raise BrowserExecutionBusyError(profile_id)
        BROWSER_PROFILE_EXECUTIONS.add(profile_id)
    try:
        yield
    finally:
        with BROWSER_PROFILE_EXECUTIONS_LOCK:
            BROWSER_PROFILE_EXECUTIONS.discard(profile_id)


@contextmanager
def browser_profile_session_lock(profile_id: str):
    with BROWSER_PROFILE_SESSION_LOCKS_LOCK:
        entry = BROWSER_PROFILE_SESSION_LOCKS.get(profile_id)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            BROWSER_PROFILE_SESSION_LOCKS[profile_id] = entry
        entry["users"] += 1
    try:
        with entry["lock"]:
            yield
    finally:
        with BROWSER_PROFILE_SESSION_LOCKS_LOCK:
            entry["users"] -= 1
            if (
                entry["users"] == 0
                and BROWSER_PROFILE_SESSION_LOCKS.get(profile_id) is entry
            ):
                BROWSER_PROFILE_SESSION_LOCKS.pop(profile_id, None)


def _acquire_browser_session_use_locked(profile_id: str, ws_url: str) -> bool:
    if ACTIVE_BROWSER_SESSIONS.get(profile_id) != ws_url:
        return False
    key = (profile_id, ws_url)
    lease = BROWSER_SESSION_LEASES.setdefault(
        key, {"users": 0, "close_requested": False}
    )
    lease["users"] += 1
    return True


def acquire_browser_session_use(profile_id: str, ws_url: str) -> bool:
    with browser_profile_session_lock(profile_id):
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            return _acquire_browser_session_use_locked(profile_id, ws_url)


def _stop_browser_profile(profile_id: str) -> None:
    adspower_settings = load_settings().get("adspower", {})
    controller = AdsPowerController(
        base_url=adspower_settings.get("base_url") or get_adspower_base_url(),
        api_key=adspower_settings.get("api_key") or os.getenv("ADSPOWER_API_KEY", ""),
    )
    controller.stop_browser(profile_id)


def release_browser_session_use(
    profile_id: str,
    ws_url: str,
    *,
    request_close: bool = False,
    stop_browser=None,
) -> None:
    should_stop = False
    key = (profile_id, ws_url)
    with browser_profile_session_lock(profile_id):
        with ACTIVE_BROWSER_SESSIONS_LOCK:
            lease = BROWSER_SESSION_LEASES.get(key)
            if lease is None:
                return
            if request_close:
                lease["close_requested"] = True
            lease["users"] = max(int(lease.get("users", 0)) - 1, 0)
            if lease["users"] == 0:
                if (
                    lease.get("close_requested")
                    and ACTIVE_BROWSER_SESSIONS.get(profile_id) == ws_url
                ):
                    ACTIVE_BROWSER_SESSIONS.pop(profile_id, None)
                    should_stop = True
                BROWSER_SESSION_LEASES.pop(key, None)
        if should_stop:
            try:
                (stop_browser or _stop_browser_profile)(profile_id)
            except Exception as error:
                with ACTIVE_BROWSER_SESSIONS_LOCK:
                    ACTIVE_BROWSER_SESSIONS.setdefault(profile_id, ws_url)
                record_browser_log(
                    "session_stop_failed",
                    {"profile_id": profile_id, "error": str(error)},
                )


def release_browser_session_results(
    session_results: list[dict], *, request_close: bool = False
) -> None:
    for item in session_results:
        if item.get("status") == "ready" and item.get("ws_url"):
            release_browser_session_use(
                item["profile_id"],
                item["ws_url"],
                request_close=request_close,
            )


def ensure_browser_profile_sessions(
    profiles: list[dict], *, lease_sessions: bool = False
) -> tuple[list[dict], dict | None]:
    from browser_cdp import wait_for_cdp
    from gateway.browser_orchestrator import ensure_profile_session

    adspower_settings = load_settings().get("adspower", {})
    controller = AdsPowerController(
        base_url=adspower_settings.get("base_url") or get_adspower_base_url(),
        api_key=adspower_settings.get("api_key") or os.getenv("ADSPOWER_API_KEY", ""),
    )

    def ensure_one(profile):
        profile_id = profile["profile_id"]
        with browser_profile_session_lock(profile_id):
            with ACTIVE_BROWSER_SESSIONS_LOCK:
                current_ws = ACTIVE_BROWSER_SESSIONS.get(profile_id)
                current_lease = BROWSER_SESSION_LEASES.get(
                    (profile_id, current_ws), {}
                )
                current_in_use = bool(
                    current_ws and int(current_lease.get("users", 0)) > 0
                )
            if current_in_use:
                try:
                    current_ready = bool(wait_for_cdp(current_ws, timeout=30.0))
                except Exception:
                    current_ready = False
                result = {
                    "profile_id": profile_id,
                    "profile_no": str(profile.get("profile_no") or ""),
                    "name": str(profile.get("name") or ""),
                    "status": "ready" if current_ready else "failed",
                    "stage": "session_check" if current_ready else "session_busy",
                    "attempts": 0,
                    "ws_url": current_ws if current_ready else "",
                    "error": (
                        ""
                        if current_ready
                        else "当前窗口正被其他任务使用且 CDP 检查失败；为避免中断任务，未重启该窗口"
                    ),
                }
            else:
                result = ensure_profile_session(
                    profile,
                    current_ws,
                    controller,
                    lambda ws_url: wait_for_cdp(ws_url, timeout=30.0),
                    retries=3,
                    sleep_fn=time.sleep,
                )
            with ACTIVE_BROWSER_SESSIONS_LOCK:
                if result.get("status") == "ready":
                    ACTIVE_BROWSER_SESSIONS[profile_id] = result["ws_url"]
                    if lease_sessions:
                        _acquire_browser_session_use_locked(
                            profile_id, result["ws_url"]
                        )
                else:
                    if (
                        not current_in_use
                        and ACTIVE_BROWSER_SESSIONS.get(profile_id) == current_ws
                    ):
                        ACTIVE_BROWSER_SESSIONS.pop(profile_id, None)
            return result

    with ThreadPoolExecutor(max_workers=len(profiles)) as executor:
        session_results = list(executor.map(ensure_one, profiles))

    ready = [item for item in session_results if item.get("status") == "ready"]

    layout = None
    if ready:
        from window_tiler import tile_browser_windows

        hints = [
            {
                "profile_id": item["profile_id"],
                "profile_no": item.get("profile_no", ""),
                "name": item.get("name", ""),
                "ws_puppeteer": item["ws_url"],
            }
            for item in ready
        ]
        try:
            layout = tile_browser_windows(hints)
        except Exception as error:
            safe_error = public_browser_payload({"error": str(error)})["error"]
            layout = {
                "count": 0,
                "layout": [],
                "missing": [f"窗口平铺失败：{safe_error}"],
                "error": safe_error,
            }
    return session_results, layout


def browser_tile_error(
    layout: dict | None,
    profile_id: str,
    ready_profile_ids: list[str] | None = None,
) -> str:
    if not layout:
        return ""
    profile_ids = [
        str(item_id).strip()
        for item_id in (ready_profile_ids or [profile_id])
        if str(item_id).strip()
    ]

    def public_tile_message(value: object) -> str:
        message = str(value)
        for item_id in sorted(set(profile_ids), key=len, reverse=True):
            message = re.sub(
                rf"(?<![\w-]){re.escape(item_id)}(?![\w-])",
                lambda _match, masked=mask_profile_id(item_id): masked,
                message,
            )
        return public_browser_payload({"error": message})["error"]

    if layout.get("error"):
        return public_tile_message(layout["error"])
    errors = []
    for item in layout.get("missing") or []:
        message = str(item)
        matches = [item_id for item_id in profile_ids if message == item_id]
        if not matches:
            matches = [
                item_id
                for item_id in profile_ids
                if re.search(
                    rf"(?<![\w-]){re.escape(item_id)}(?![\w-])",
                    message,
                )
            ]
        if len(matches) != 1 or matches[0] == profile_id:
            safe_message = public_tile_message(message)
            errors.append(f"窗口平铺失败：{safe_message}")
    for item in layout.get("scale_results") or []:
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        failed_profile_id = str(item.get("profile_id") or "").strip()
        if failed_profile_id in profile_ids:
            matches = [failed_profile_id]
        else:
            matches = []
        if len(matches) != 1 or matches[0] == profile_id:
            message = str(item.get("error") or failed_profile_id or "页面缩放失败")
            safe_message = public_tile_message(message)
            errors.append(f"窗口缩放失败：{safe_message}")
    return "；".join(errors)


class BrowserStageError(RuntimeError):
    def __init__(self, stage: str, target_url: str, reason: str, current_url: str = ""):
        super().__init__(reason)
        self.stage = stage
        self.target_url = target_url
        self.reason = reason
        self.current_url = current_url


def get_browser_target_url(payload: dict | None = None) -> str:
    browser_settings = load_settings().get("browser", {})
    requested_url = str((payload or {}).get("url") or "").strip()
    configured_url = str(browser_settings.get("default_url") or "").strip()
    return requested_url or configured_url or "https://www.tiktok.com/"


def get_async_playwright():
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:  # pragma: no cover - dependency check handles this
        raise RuntimeError("Playwright is unavailable for element inspection") from error
    return async_playwright


async def _current_inspection_page(context):
    pages = []
    for page in list(getattr(context, "pages", [])):
        try:
            if not page.is_closed():
                pages.append(page)
        except Exception:
            continue
    if not pages:
        raise RuntimeError("no active page available for element inspection")
    for page in reversed(pages):
        try:
            if await page.evaluate("document.visibilityState") == "visible":
                return page
        except Exception:
            continue
    return pages[-1]


async def _inspect_browser_elements_on_cdp(ws_url: str, elements: dict) -> list[dict]:
    playwright = await get_async_playwright()().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(ws_url, timeout=10_000)
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("no operable browser context for element inspection")
        page = await _current_inspection_page(contexts[0])
        results = []
        for alias, definition in elements.items():
            try:
                results.append(await inspect_element(page, alias, definition))
            except LocatorResolutionError as error:
                results.append(
                    {
                        "status": "error",
                        "code": error.code,
                        "alias": alias,
                        "scope": definition["scope"],
                        "diagnostics": error.diagnostics,
                    }
                )
            except Exception:
                results.append(
                    {
                        "status": "error",
                        "code": "element_inspection_failed",
                        "alias": alias,
                        "scope": definition["scope"],
                        "diagnostics": {},
                    }
                )
        return results
    finally:
        await playwright.stop()


def inspect_browser_elements_on_cdp(ws_url: str, elements: dict) -> list[dict]:
    """Inspect the currently active CDP page without browser actions or navigation."""

    return asyncio.run(_inspect_browser_elements_on_cdp(ws_url, elements))


_PUBLIC_LOCATOR_ERROR_CODES = {
    "element_alias_missing",
    "element_candidate_ambiguous",
    "element_candidate_not_found",
    "element_inspection_failed",
    "element_not_actionable",
    "element_postcondition_not_observed",
    "element_resolution_failed",
    "element_scope_not_found",
}
_PUBLIC_LOCATOR_TYPES = {"attribute", "css", "role", "xpath"}
_PUBLIC_VIDEO_SWITCH_ERROR_CODES = {
    "video_switch_closed_target",
    "video_switch_interval_failed",
    "video_switch_not_observed",
    "video_switch_recovery_failed",
    "video_switch_state_capture_failed",
    "video_switch_timeout",
}
_PUBLIC_DIAGNOSTIC_COUNT_KEYS = {
    "actionable_count",
    "article_count",
    "center_intersection_count",
    "container_count",
    "input_count",
    "matching_article_id_count",
    "panel_count",
    "raw_count",
    "usable_input_count",
    "usable_panel_count",
    "visible_article_count",
    "visible_container_count",
    "visible_count",
    "visible_input_count",
    "visible_panel_count",
}
_PUBLIC_DIAGNOSTIC_PHASES = {
    "editable_check",
    "inspection",
    "locator_query",
    "scope_query",
}


def _safe_inspection_diagnostics(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    safe = {}
    for key, item in value.items():
        if (
            key in _PUBLIC_DIAGNOSTIC_COUNT_KEYS
            and isinstance(item, int)
            and not isinstance(item, bool)
        ):
            safe[key] = max(item, 0)
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        public_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            public_candidate = {}
            candidate_id = candidate.get("id")
            if isinstance(candidate_id, str) and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate_id
            ):
                public_candidate["id"] = _public_identifier(
                    candidate_id, "candidate"
                )
            candidate_type = candidate.get("type")
            if candidate_type in _PUBLIC_LOCATOR_TYPES:
                public_candidate["type"] = candidate_type
            for count_key in ("raw_count", "visible_count", "actionable_count"):
                count = candidate.get(count_key)
                if isinstance(count, int) and not isinstance(count, bool):
                    public_candidate[count_key] = max(count, 0)
            public_candidates.append(public_candidate)
        safe["candidates"] = public_candidates
    return safe


def public_element_inspection(result: object, alias: str, definition: dict) -> dict:
    raw = result if isinstance(result, dict) else {}
    public = {
        "status": "ok" if raw.get("status") == "ok" else "error",
        "alias": alias,
        "scope": definition["scope"],
        "diagnostics": _safe_inspection_diagnostics(raw.get("diagnostics")),
    }
    if public["status"] == "ok":
        candidate = raw.get("candidate")
        if isinstance(candidate, dict):
            candidate_id = candidate.get("id")
            candidate_type = candidate.get("type")
            if (
                isinstance(candidate_id, str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate_id)
                and candidate_type in _PUBLIC_LOCATOR_TYPES
            ):
                public["candidate"] = {
                    "id": _public_identifier(candidate_id, "candidate"),
                    "type": candidate_type,
                }
    else:
        code = raw.get("code")
        public["code"] = (
            code
            if code in _PUBLIC_LOCATOR_ERROR_CODES
            else "element_inspection_failed"
        )
    return public


_PUBLIC_STRATEGY_SCOPES = {"active_video", "page", "visible_comment_panel"}
_PUBLIC_ACTION_TYPES = {
    "move",
    "click",
    "scroll_up",
    "scroll_down",
    "keyboard_input",
    "pause",
}
_PUBLIC_EXECUTION_STAGES = {
    "cleanup",
    "close_other_tabs",
    "connect",
    "execute_actions",
    "execution_busy",
    "navigate",
    "prepare_page",
    "session_check",
    "session_start",
    "session_busy",
    "start_browser",
    "tile",
    "validation",
    "wait_for_cdp",
}
_PUBLIC_RECOVERY_STATUSES = {"failed", "recovered"}
_PUBLIC_RECOVERY_OUTCOMES = {
    "not_retried",
    "recovered",
    "replacement_not_found",
    "retry_failed",
}
_PUBLIC_CLOSURE_TYPES = {
    "browser_closed",
    "browser_disconnected",
    "context_closed",
    "page_closed",
    "target_closed",
    "target_detached",
}
_PUBLIC_CLOSURE_REASONS = {
    "browser disconnected",
    "browser has been closed",
    "closed target",
    "context closed",
    "page closed",
    "target closed",
    "target detached",
    "target page, context or browser has been closed",
}
_PUBLIC_FAILURE_MESSAGES = {
    "CDP not ready",
    "CDP wait failed",
    "navigation blew up",
    "navigation failed",
    "navigation settled on unexpected URL: about:blank",
    "runtime failed",
}


def _public_identifier(value: object, prefix: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if prefix == "profile":
        return mask_profile_id(value)
    decoded = unquote(text).casefold()
    normalized = normalize_sensitive_browser_key(text)
    forbidden = (
        is_sensitive_browser_key(text)
        or any(
            marker in normalized
            for marker in (
                "outerhtml",
                "selector",
                "contenteditable",
                "privatecomment",
                "commentcontent",
                "endpoint",
                "devtoolsbrowser",
            )
        )
        or normalized.startswith(("css", "xpath"))
        or any(
            marker in decoded
            for marker in (
                "css=",
                "xpath=",
                "//",
                "ws://",
                "wss://",
                "http://",
                "https://",
                "<",
                ">",
                "[",
                "]",
            )
        )
    )
    if (
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", text)
        and not forbidden
    ):
        return text
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}"


def _public_nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _public_nonnegative_number(value: object) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _public_http_url(value: object) -> str:
    text = str(value or "").strip()
    return sanitize_public_browser_url(text) if is_valid_browser_url(text) else ""


def _public_origin(value: object) -> str:
    text = str(value or "").strip()
    return sanitize_public_browser_origin(text) if is_valid_browser_url(text) else ""


def _masked_public_switch_identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return ""
    if re.fullmatch(r"[0-9a-f]{12}", value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _canonical_element(action: dict, elements: dict) -> tuple[str, dict | None]:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    alias = str(params.get("element") or "")
    definition = elements.get(alias) if isinstance(elements, dict) else None
    return alias, definition if isinstance(definition, dict) else None


def _public_strategy_locator(
    value: object,
    action: dict,
    elements: dict,
) -> dict:
    if not isinstance(value, dict):
        return {}
    alias, definition = _canonical_element(action, elements)
    if not alias or definition is None:
        return {}
    scope = value.get("scope")
    candidate_id = value.get("candidate_id")
    candidate_type = value.get("candidate_type")
    candidates = definition.get("locators")
    if (
        scope != definition.get("scope")
        or scope not in _PUBLIC_STRATEGY_SCOPES
        or not isinstance(candidate_id, str)
        or candidate_type not in _PUBLIC_LOCATOR_TYPES
        or not isinstance(candidates, list)
        or not any(
            isinstance(candidate, dict)
            and candidate.get("id") == candidate_id
            and candidate.get("type") == candidate_type
            for candidate in candidates
        )
    ):
        return {}
    return {
        "scope": scope,
        "candidate_id": _public_identifier(candidate_id, "candidate"),
        "candidate_type": candidate_type,
    }


def _public_strategy_switches(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    switches = []
    for item in value:
        if not isinstance(item, dict):
            continue
        before = _masked_public_switch_identity(item.get("from"))
        after = _masked_public_switch_identity(item.get("to"))
        wheel_events = item.get("wheel_events")
        if (
            not before
            or not after
            or isinstance(wheel_events, bool)
            or not isinstance(wheel_events, int)
            or wheel_events < 0
        ):
            continue
        switches.append(
            {
                "from": before,
                "to": after,
                "wheel_events": max(wheel_events, 0),
            }
        )
    return switches


def public_strategy_action_result(
    value: object,
    action: dict,
    action_index: int,
    cycle: int,
    elements: dict,
) -> dict:
    raw = value if isinstance(value, dict) else {}
    action_type = action.get("type")
    public = {
        "action_id": _public_identifier(action.get("id"), "action"),
        "action_index": action_index,
        "cycle": cycle,
        "type": action_type,
        "status": "ok",
    }
    alias, _definition = _canonical_element(action, elements)
    if alias:
        public["element"] = _public_identifier(alias, "element")

    if action_type in {"scroll_up", "scroll_down"}:
        requested = _public_nonnegative_int(raw.get("requested_switches"))
        completed = _public_nonnegative_int(raw.get("completed_switches"))
        wheel_events = _public_nonnegative_int(raw.get("wheel_events"))
        if (
            requested is None
            or completed is None
            or wheel_events is None
            or completed > requested
        ):
            return {}
        public.update(
            {
                "requested_switches": requested,
                "completed_switches": completed,
                "wheel_events": wheel_events,
                "switches": _public_strategy_switches(raw.get("switches")),
            }
        )
        for field in ("count", "distance"):
            item = _public_nonnegative_int(raw.get(field))
            if item is not None:
                public[field] = item
        return public

    if action_type in {"move", "click", "keyboard_input"}:
        locator = _public_strategy_locator(raw.get("locator"), action, elements)
        if locator:
            public["locator"] = locator
    if action_type in {"move", "pause"}:
        duration = _public_nonnegative_number(raw.get("duration_seconds"))
        if duration is not None:
            public["duration_seconds"] = duration
    if action_type == "move" and raw.get("trajectory_source") in {
        "ghost-cursor",
        "recorded-pattern",
    }:
        public["trajectory_source"] = raw["trajectory_source"]
    if action_type == "click":
        params = action.get("params") if isinstance(action.get("params"), dict) else {}
        public["button"] = params.get("button", "left")
        click_count = _public_nonnegative_int(raw.get("click_count"))
        hold_seconds = _public_nonnegative_number(raw.get("hold_seconds"))
        if click_count is not None:
            public["click_count"] = click_count
        if hold_seconds is not None:
            public["hold_seconds"] = hold_seconds
        if raw.get("postcondition") in {"not_configured", "observed"}:
            public["postcondition"] = raw["postcondition"]
        if raw.get("trajectory_source") in {"ghost-cursor", "recorded-pattern"}:
            public["trajectory_source"] = raw["trajectory_source"]
    return public


def _public_strategy_stage(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    stage = value.get("stage")
    status = value.get("status")
    if stage not in _PUBLIC_EXECUTION_STAGES or status not in {"failed", "ok"}:
        return {}
    public = {"stage": stage, "status": status}
    closed_tabs = _public_nonnegative_int(value.get("closed_tabs"))
    if closed_tabs is not None:
        public["closed_tabs"] = closed_tabs
    for key in ("target_url", "current_url"):
        url = _public_http_url(value.get(key))
        if url:
            public[key] = url
    return public


def _canonical_action_occurrence(
    value: object,
    strategy: dict,
) -> tuple[dict, int, int] | None:
    if not isinstance(value, dict):
        return None
    actions = strategy.get("actions")
    if not isinstance(actions, list):
        return None
    action_index = _public_nonnegative_int(value.get("action_index"))
    raw_cycle = value.get("cycle")
    cycle = (
        1
        if strategy.get("run_mode") == "once" and raw_cycle in (None, 1)
        else _public_nonnegative_int(raw_cycle)
    )
    if (
        action_index is None
        or action_index < 1
        or action_index > len(actions)
        or cycle is None
        or cycle < 1
    ):
        return None
    action = actions[action_index - 1]
    if (
        not isinstance(action, dict)
        or value.get("action_id") != action.get("id")
        or value.get("type", value.get("action_type")) != action.get("type")
    ):
        return None
    return action, action_index, cycle


def _public_strategy_recovery(
    value: object,
    strategy: dict,
    *,
    expected_action: tuple[dict, int, int] | None = None,
) -> dict:
    if not isinstance(value, dict):
        return {}
    legacy_reason = value.get("reason")
    if (
        not any(
            key in value for key in ("action_id", "action_index", "action_type", "cycle")
        )
        and legacy_reason in _PUBLIC_CLOSURE_TYPES
    ):
        return {"reason": legacy_reason}
    occurrence = _canonical_action_occurrence(value, strategy)
    if occurrence is None and expected_action is not None:
        expected, expected_index, expected_cycle = expected_action
        supplied_index = value.get("action_index")
        supplied_cycle = value.get("cycle")
        supplied_type = value.get("action_type", value.get("type"))
        if (
            value.get("action_id") == expected.get("id")
            and supplied_index in (None, expected_index)
            and supplied_cycle in (None, expected_cycle)
            and supplied_type in (None, expected.get("type"))
        ):
            occurrence = expected_action
    if occurrence is None:
        return {}
    action, action_index, cycle = occurrence
    if expected_action is not None and (
        action_index != expected_action[1] or cycle != expected_action[2]
    ):
        return {}
    public = {
        "action_id": _public_identifier(action.get("id"), "action"),
        "action_index": action_index,
        "action_type": action.get("type"),
        "cycle": cycle,
    }
    profile_id = _public_identifier(value.get("profile_id"), "profile")
    if profile_id:
        public["profile_id"] = profile_id
    for key in ("old_page_origin", "new_page_origin"):
        origin = _public_origin(value.get(key))
        if origin:
            public[key] = origin
    closure_type = value.get("closure_type")
    if closure_type in _PUBLIC_CLOSURE_TYPES:
        public["closure_type"] = closure_type
    closure_reason = value.get("closure_reason")
    if closure_reason in _PUBLIC_CLOSURE_REASONS:
        public["closure_reason"] = closure_reason
    if isinstance(value.get("replacement_found"), bool):
        public["replacement_found"] = value["replacement_found"]
    retry = _public_nonnegative_int(value.get("retry"))
    if retry is not None:
        public["retry"] = retry
    if value.get("status") in _PUBLIC_RECOVERY_STATUSES:
        public["status"] = value["status"]
    if value.get("outcome") in _PUBLIC_RECOVERY_OUTCOMES:
        public["outcome"] = value["outcome"]
    return public


def _public_strategy_recoveries(
    value: object,
    strategy: dict,
    *,
    expected_action: tuple[dict, int, int] | None = None,
) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    public = []
    for item in value:
        recovery = _public_strategy_recovery(
            item,
            strategy,
            expected_action=expected_action,
        )
        if recovery:
            public.append(recovery)
    return public


def public_strategy_execution_result(
    result: object,
    strategy: dict,
    elements: dict | None = None,
) -> dict:
    raw = result if isinstance(result, dict) else {}
    elements = elements if isinstance(elements, dict) else {}
    public = {}
    if raw.get("status") == "ok":
        public["status"] = "ok"
    cycles = _public_nonnegative_int(raw.get("cycles"))
    if cycles is not None:
        public["cycles"] = cycles
    sampled_duration = _public_nonnegative_number(raw.get("sampled_duration_minutes"))
    if sampled_duration is not None:
        public["sampled_duration_minutes"] = sampled_duration
    for key in ("target_url", "current_url"):
        url = _public_http_url(raw.get(key))
        if url:
            public[key] = url
    for key in ("closed_tabs", "verified_interactions"):
        item = _public_nonnegative_int(raw.get(key))
        if item is not None:
            public[key] = item
    if isinstance(raw.get("stages"), (list, tuple)):
        public["stages"] = [
            stage
            for item in raw["stages"]
            if (stage := _public_strategy_stage(item))
        ]
    if "page_recoveries" in raw:
        public["page_recoveries"] = _public_strategy_recoveries(
            raw["page_recoveries"],
            strategy,
        )

    actions = raw.get("actions")
    if isinstance(actions, (list, tuple)):
        canonical_actions = strategy.get("actions")
        expected = []
        if (
            isinstance(canonical_actions, list)
            and cycles is not None
            and cycles > 0
        ):
            expected = [
                (cycle, action_index, action)
                for cycle in range(1, cycles + 1)
                for action_index, action in enumerate(canonical_actions, start=1)
            ]
        valid = len(actions) == len(expected)
        if valid:
            for raw_action, (cycle, action_index, action) in zip(actions, expected):
                if (
                    not isinstance(raw_action, dict)
                    or raw_action.get("action_id") != action.get("id")
                    or raw_action.get("type") != action.get("type")
                    or raw_action.get("action_index") != action_index
                    or raw_action.get("cycle") != cycle
                    or raw_action.get("status") != "ok"
                ):
                    valid = False
                    break
        projected_actions = []
        if valid:
            for raw_action, (cycle, action_index, action) in zip(actions, expected):
                projected = public_strategy_action_result(
                    raw_action,
                    action,
                    action_index,
                    cycle,
                    elements,
                )
                if not projected:
                    valid = False
                    break
                projected_actions.append(projected)
        public["actions"] = projected_actions if valid else []
    return public


def _public_locator_diagnostics(value: object, definition: dict) -> dict:
    if not isinstance(value, dict):
        return {}
    public = {
        key: max(item, 0)
        for key, item in value.items()
        if key in _PUBLIC_DIAGNOSTIC_COUNT_KEYS
        and isinstance(item, int)
        and not isinstance(item, bool)
    }
    if value.get("phase") in _PUBLIC_DIAGNOSTIC_PHASES:
        public["phase"] = value["phase"]
    if value.get("container_box") == "missing":
        public["container_box"] = "missing"
    if value.get("scope_target") in {
        "missing_id",
        "page",
        "visible_comment_panel",
    }:
        public["scope_target"] = value["scope_target"]
    candidates = value.get("candidates")
    canonical = {
        (candidate.get("id"), candidate.get("type"))
        for candidate in definition.get("locators", [])
        if isinstance(candidate, dict)
    }
    if isinstance(candidates, list):
        public_candidates = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            identity = (candidate.get("id"), candidate.get("type"))
            if identity not in canonical:
                continue
            item = {
                "id": _public_identifier(identity[0], "candidate"),
                "type": identity[1],
            }
            for key in ("raw_count", "visible_count", "actionable_count"):
                count = _public_nonnegative_int(candidate.get(key))
                if count is not None:
                    item[key] = count
            public_candidates.append(item)
        public["candidates"] = public_candidates
    candidate_identity = (value.get("candidate_id"), value.get("candidate_type"))
    if candidate_identity in canonical:
        public["candidate_id"] = _public_identifier(
            candidate_identity[0], "candidate"
        )
        public["candidate_type"] = candidate_identity[1]
    timeout_seconds = _public_nonnegative_number(value.get("timeout_seconds"))
    if timeout_seconds is not None:
        public["timeout_seconds"] = timeout_seconds
    return public


def _public_strategy_locator_error(
    value: object,
    action: dict,
    elements: dict,
) -> dict:
    if not isinstance(value, dict):
        return {}
    alias, definition = _canonical_element(action, elements)
    code = value.get("code")
    source_alias = value.get("alias")
    if (
        not alias
        or definition is None
        or not (
            source_alias == alias
            or (
                source_alias == ""
                and code
                in {"element_resolution_failed", "element_scope_not_found"}
            )
        )
        or value.get("scope") != definition.get("scope")
    ):
        return {}
    if code not in _PUBLIC_LOCATOR_ERROR_CODES:
        return {}
    public = {
        "code": code,
        "alias": _public_identifier(alias, "element"),
        "scope": definition["scope"],
    }
    diagnostics = _public_locator_diagnostics(value.get("diagnostics"), definition)
    if diagnostics:
        public["diagnostics"] = diagnostics
    return public


def _canonical_failure_occurrence(
    error: BaseException,
    strategy: dict,
) -> tuple[dict, int, int] | None:
    actions = strategy.get("actions")
    action_index = _public_nonnegative_int(getattr(error, "action_index", None))
    if (
        not isinstance(actions, list)
        or action_index is None
        or action_index < 1
        or action_index > len(actions)
    ):
        return None
    action = actions[action_index - 1]
    if (
        not isinstance(action, dict)
        or getattr(error, "action_id", None) != action.get("id")
        or getattr(error, "action_type", None) != action.get("type")
    ):
        return None
    raw_cycle = getattr(error, "cycle", None)
    if strategy.get("run_mode") == "once" and raw_cycle in (None, 1):
        cycle = 1
    else:
        cycle = _public_nonnegative_int(raw_cycle)
        if cycle is None or cycle < 1:
            return None
    return action, action_index, cycle


def _public_completed_strategy_actions(
    value: object,
    strategy: dict,
    elements: dict,
    failure: tuple[dict, int, int] | None,
) -> list[dict]:
    if not isinstance(value, (list, tuple)) or failure is None:
        return []
    failure_cycle, failure_index = failure[2], failure[1]
    previous = (0, 0)
    public_actions = []
    for item in value:
        occurrence = _canonical_action_occurrence(item, strategy)
        if occurrence is None or not isinstance(item, dict) or item.get("status") != "ok":
            return []
        action, action_index, cycle = occurrence
        position = (cycle, action_index)
        if position >= (failure_cycle, failure_index) or position <= previous:
            return []
        public_action = public_strategy_action_result(
            item,
            action,
            action_index,
            cycle,
            elements,
        )
        if not public_action:
            return []
        public_actions.append(public_action)
        previous = position
    return public_actions


def _public_failure_message(error: BaseException, stage: str, code: str = "") -> str:
    if code:
        return code
    raw_reason = str(getattr(error, "reason", error))
    if raw_reason in _PUBLIC_FAILURE_MESSAGES:
        return raw_reason
    reason = raw_reason.casefold()
    if "browser disconnected" in reason:
        return "browser disconnected"
    if stage == "execution_busy":
        return "execution_busy"
    return f"{stage}_failed"


def _public_strategy_gate_reasons(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    public = []
    for reason in value:
        if not isinstance(reason, dict):
            continue
        source = reason.get("source")
        reason_code = reason.get("reason_code")
        if (
            source not in {"manual", "probe"}
            or not isinstance(reason_code, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code)
        ):
            continue
        aliases = reason.get("aliases")
        item = {
            "source": source,
            "reason_code": reason_code,
            "aliases": [
                _public_identifier(alias, "element")
                for alias in aliases
                if isinstance(alias, str) and alias
            ][:256]
            if isinstance(aliases, (list, tuple))
            else [],
        }
        selector_version_id = reason.get("selector_version_id")
        if isinstance(selector_version_id, str) and len(selector_version_id) <= 128:
            item["selector_version_id"] = _public_identifier(
                selector_version_id,
                "selector-version",
            )
        created_at = reason.get("created_at")
        if isinstance(created_at, str) and re.fullmatch(
            r"[0-9T:+\-Z.]{1,64}",
            created_at,
        ):
            item["created_at"] = created_at
        public.append(item)
    return public


def public_strategy_failure_result(
    *,
    profile_id: object,
    attempts: object,
    target_url: object,
    error: BaseException,
    strategy: dict,
    elements: dict,
    default_stage: str = "execute_actions",
) -> dict:
    raw_stage = getattr(error, "stage", default_stage)
    stage = raw_stage if raw_stage in _PUBLIC_EXECUTION_STAGES else default_stage
    public = {
        "profile_id": _public_identifier(profile_id, "profile"),
        "status": "failed",
        "stage": stage,
        "error": _public_failure_message(error, stage),
        "reason": _public_failure_message(error, stage),
    }
    safe_attempts = _public_nonnegative_int(attempts)
    if safe_attempts is not None:
        public["attempts"] = safe_attempts
    safe_target_url = _public_http_url(target_url)
    if safe_target_url:
        public["target_url"] = safe_target_url
    safe_current_url = _public_http_url(getattr(error, "current_url", ""))
    public["current_url"] = safe_current_url
    if getattr(error, "code", "") == "strategy_paused_during_execution":
        public.update(
            {
                "code": "strategy_paused_during_execution",
                "error": "strategy_paused_during_execution",
                "reason": "strategy_paused_during_execution",
                "gate_reasons": _public_strategy_gate_reasons(
                    getattr(error, "reasons", None)
                ),
            }
        )

    occurrence = _canonical_failure_occurrence(error, strategy)
    if occurrence is not None:
        action, action_index, cycle = occurrence
        public.update(
            {
                "action_id": _public_identifier(action.get("id"), "action"),
                "action_index": action_index,
                "action_type": action.get("type"),
                "cycle": cycle,
            }
        )
        code = getattr(error, "code", None)
        if action.get("type") in {"scroll_up", "scroll_down"}:
            requested = _public_nonnegative_int(
                getattr(error, "requested_switches", None)
            )
            completed = _public_nonnegative_int(
                getattr(error, "completed_switches", None)
            )
            wheel_events = _public_nonnegative_int(
                getattr(error, "wheel_events", None)
            )
            if (
                code in _PUBLIC_VIDEO_SWITCH_ERROR_CODES
                and requested is not None
                and completed is not None
                and wheel_events is not None
                and completed <= requested
            ):
                public.update(
                    {
                        "code": code,
                        "requested_switches": requested,
                        "completed_switches": completed,
                        "wheel_events": wheel_events,
                        "switches": _public_strategy_switches(
                            getattr(error, "switches", None)
                        ),
                    }
                )
        elif action.get("type") in {"move", "click", "keyboard_input"}:
            locator = _public_strategy_locator_error(
                getattr(error, "locator", None),
                action,
                elements,
            )
            if locator and code == locator["code"]:
                public["code"] = code
                public["locator"] = locator
        if public.get("code"):
            public["error"] = public["code"]
            public["reason"] = public["code"]

    recoveries = _public_strategy_recoveries(
        getattr(error, "page_recoveries", None),
        strategy,
        expected_action=occurrence,
    )
    if recoveries:
        public["page_recoveries"] = recoveries
    if hasattr(error, "completed_actions"):
        public["actions"] = _public_completed_strategy_actions(
            getattr(error, "completed_actions"),
            strategy,
            elements,
            occurrence,
        )
    return public


def _public_stored_locator(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    scope = value.get("scope")
    candidate_type = value.get("candidate_type")
    candidate_id = _public_identifier(value.get("candidate_id"), "candidate")
    if (
        scope in _PUBLIC_STRATEGY_SCOPES
        and candidate_type in _PUBLIC_LOCATOR_TYPES
        and candidate_id
    ):
        return {
            "scope": scope,
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
        }
    code = value.get("code")
    alias = _public_identifier(value.get("alias"), "element")
    if (
        code in _PUBLIC_LOCATOR_ERROR_CODES
        and scope in _PUBLIC_STRATEGY_SCOPES
        and alias
    ):
        public = {"code": code, "alias": alias, "scope": scope}
        diagnostics = value.get("diagnostics")
        if isinstance(diagnostics, dict):
            safe_diagnostics = {
                key: item
                for key, item in diagnostics.items()
                if key in _PUBLIC_DIAGNOSTIC_COUNT_KEYS
                and _public_nonnegative_int(item) is not None
            }
            if diagnostics.get("phase") in _PUBLIC_DIAGNOSTIC_PHASES:
                safe_diagnostics["phase"] = diagnostics["phase"]
            candidate_id = _public_identifier(
                diagnostics.get("candidate_id"), "candidate"
            )
            candidate_type = diagnostics.get("candidate_type")
            if candidate_id and candidate_type in _PUBLIC_LOCATOR_TYPES:
                safe_diagnostics["candidate_id"] = candidate_id
                safe_diagnostics["candidate_type"] = candidate_type
            timeout_seconds = _public_nonnegative_number(
                diagnostics.get("timeout_seconds")
            )
            if timeout_seconds is not None:
                safe_diagnostics["timeout_seconds"] = timeout_seconds
            if safe_diagnostics:
                public["diagnostics"] = safe_diagnostics
        return public
    return {}


def _public_stored_action(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    action_type = value.get("type")
    action_id = _public_identifier(value.get("action_id"), "action")
    action_index = _public_nonnegative_int(value.get("action_index"))
    cycle = _public_nonnegative_int(value.get("cycle"))
    if (
        action_type not in _PUBLIC_ACTION_TYPES
        or value.get("status") != "ok"
        or not action_id
        or action_index is None
        or action_index < 1
        or cycle is None
        or cycle < 1
    ):
        return {}
    public = {
        "action_id": action_id,
        "action_index": action_index,
        "cycle": cycle,
        "type": action_type,
        "status": "ok",
    }
    element = _public_identifier(value.get("element"), "element")
    if element:
        public["element"] = element
    for key in (
        "requested_switches",
        "completed_switches",
        "wheel_events",
        "count",
        "distance",
        "click_count",
    ):
        item = _public_nonnegative_int(value.get(key))
        if item is not None:
            public[key] = item
    for key in ("duration_seconds", "hold_seconds"):
        item = _public_nonnegative_number(value.get(key))
        if item is not None:
            public[key] = item
    if value.get("button") in {"left", "middle", "right"}:
        public["button"] = value["button"]
    if value.get("postcondition") in {"not_configured", "observed"}:
        public["postcondition"] = value["postcondition"]
    if value.get("trajectory_source") in {"ghost-cursor", "recorded-pattern"}:
        public["trajectory_source"] = value["trajectory_source"]
    locator = _public_stored_locator(value.get("locator"))
    if locator:
        public["locator"] = locator
    if action_type in {"scroll_up", "scroll_down"}:
        public["switches"] = _public_strategy_switches(value.get("switches"))
    return public


def _public_stored_recovery(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    if value.get("reason") in _PUBLIC_CLOSURE_TYPES:
        return {"reason": value["reason"]}
    action_id = _public_identifier(value.get("action_id"), "action")
    action_type = value.get("action_type")
    action_index = _public_nonnegative_int(value.get("action_index"))
    cycle = _public_nonnegative_int(value.get("cycle"))
    if (
        not action_id
        or action_type not in _PUBLIC_ACTION_TYPES
        or action_index is None
        or action_index < 1
        or cycle is None
        or cycle < 1
    ):
        return {}
    public = {
        "action_id": action_id,
        "action_index": action_index,
        "action_type": action_type,
        "cycle": cycle,
    }
    profile_id = _public_identifier(value.get("profile_id"), "profile")
    if profile_id:
        public["profile_id"] = profile_id
    for key in ("old_page_origin", "new_page_origin"):
        origin = _public_origin(value.get(key))
        if origin:
            public[key] = origin
    if value.get("closure_type") in _PUBLIC_CLOSURE_TYPES:
        public["closure_type"] = value["closure_type"]
    if value.get("closure_reason") in _PUBLIC_CLOSURE_REASONS:
        public["closure_reason"] = value["closure_reason"]
    if isinstance(value.get("replacement_found"), bool):
        public["replacement_found"] = value["replacement_found"]
    retry = _public_nonnegative_int(value.get("retry"))
    if retry is not None:
        public["retry"] = retry
    if value.get("status") in _PUBLIC_RECOVERY_STATUSES:
        public["status"] = value["status"]
    if value.get("outcome") in _PUBLIC_RECOVERY_OUTCOMES:
        public["outcome"] = value["outcome"]
    return public


def _public_stored_error(value: object, stage: str) -> str:
    text = str(value or "")
    if (
        text in _PUBLIC_LOCATOR_ERROR_CODES
        or text in _PUBLIC_VIDEO_SWITCH_ERROR_CODES
    ):
        return text
    if "browser disconnected" in text.casefold():
        return "browser disconnected"
    if stage == "navigate" and text == "navigation blew up":
        return text
    if text in {"execution_busy", "batch_task_failed"}:
        return text
    return f"{stage}_failed"


def public_browser_batch_result(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    stage = (
        raw.get("stage")
        if raw.get("stage") in _PUBLIC_EXECUTION_STAGES
        else "execute_actions"
    )
    status = raw.get("status") if raw.get("status") in {"ok", "failed"} else "failed"
    public = {
        "profile_id": _public_identifier(raw.get("profile_id"), "profile"),
        "status": status,
        "stage": stage,
    }
    for key in ("attempts", "cycles", "closed_tabs", "verified_interactions"):
        item = _public_nonnegative_int(raw.get(key))
        if item is not None:
            public[key] = item
    duration = _public_nonnegative_number(raw.get("sampled_duration_minutes"))
    if duration is not None:
        public["sampled_duration_minutes"] = duration
    strategy_id = _public_identifier(raw.get("strategy_id"), "strategy")
    if strategy_id:
        public["strategy_id"] = strategy_id
    if raw.get("run_mode") in {"once", "loop"}:
        public["run_mode"] = raw["run_mode"]
    for key in ("target_url", "current_url"):
        url = _public_http_url(raw.get(key))
        if url:
            public[key] = url
    if status == "failed":
        gate_paused = (
            raw.get("code") or raw.get("error") or raw.get("reason")
        ) == "strategy_paused_during_execution"
        error = _public_stored_error(
            raw.get("code") or raw.get("error") or raw.get("reason"),
            stage,
        )
        if gate_paused:
            error = "strategy_paused_during_execution"
        public["error"] = error
        public["reason"] = error
        if gate_paused:
            public["code"] = error
            public["gate_reasons"] = _public_strategy_gate_reasons(
                raw.get("gate_reasons")
            )
        action_id = _public_identifier(raw.get("action_id"), "action")
        action_type = raw.get("action_type")
        action_index = _public_nonnegative_int(raw.get("action_index"))
        cycle = _public_nonnegative_int(raw.get("cycle"))
        if (
            action_id
            and action_type in _PUBLIC_ACTION_TYPES
            and action_index is not None
            and action_index > 0
            and cycle is not None
            and cycle > 0
        ):
            public.update(
                {
                    "action_id": action_id,
                    "action_index": action_index,
                    "action_type": action_type,
                    "cycle": cycle,
                }
            )
            if (
                error in _PUBLIC_LOCATOR_ERROR_CODES
                or error in _PUBLIC_VIDEO_SWITCH_ERROR_CODES
            ):
                public["code"] = error
            for key in (
                "requested_switches",
                "completed_switches",
                "wheel_events",
            ):
                item = _public_nonnegative_int(raw.get(key))
                if item is not None:
                    public[key] = item
            if action_type in {"scroll_up", "scroll_down"}:
                public["switches"] = _public_strategy_switches(raw.get("switches"))
            locator = _public_stored_locator(raw.get("locator"))
            if locator:
                public["locator"] = locator
    actions = raw.get("actions")
    if isinstance(actions, (list, tuple)):
        public["actions"] = [
            action for item in actions if (action := _public_stored_action(item))
        ]
    recoveries = raw.get("page_recoveries")
    if isinstance(recoveries, (list, tuple)):
        public["page_recoveries"] = [
            recovery
            for item in recoveries
            if (recovery := _public_stored_recovery(item))
        ]
    stages = raw.get("stages")
    if isinstance(stages, (list, tuple)):
        public["stages"] = [
            stage_item
            for item in stages
            if (stage_item := _public_strategy_stage(item))
        ]
    return public


def public_browser_batch_task(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    public = {}
    task_id = _public_identifier(raw.get("id"), "task")
    if task_id:
        public["id"] = task_id
    if raw.get("status") in {
        "queued",
        "running",
        "delayed_gate",
        "completed",
        "failed",
    }:
        public["status"] = raw["status"]
    strategy_id = _public_identifier(raw.get("strategy_id"), "strategy")
    if strategy_id:
        public["strategy_id"] = strategy_id
    for key in (
        "batch_size",
        "total_windows",
        "total_batches",
        "current_batch",
        "completed_batches",
        "processed_windows",
        "failed_windows",
    ):
        item = _public_nonnegative_int(raw.get(key))
        if item is not None:
            public[key] = item
    for key in ("created_at", "finished_at"):
        item = raw.get(key)
        if isinstance(item, str) and re.fullmatch(r"[0-9T:+\-Z.]{1,64}", item):
            public[key] = item
    target_url = _public_http_url(raw.get("target_url"))
    if target_url:
        public["target_url"] = target_url
    if raw.get("error"):
        public["error"] = "batch_task_failed"
    results = raw.get("results")
    public["results"] = (
        [public_browser_batch_result(item) for item in results]
        if isinstance(results, (list, tuple))
        else []
    )
    return public


def prepare_browser_page(ws_url: str, target_url: str) -> dict:
    from browser_cdp import navigate_and_close_other_tabs, wait_for_cdp

    try:
        wait_for_cdp(ws_url, timeout=5.0)
    except BrowserStageError:
        raise
    except Exception as error:
        raise BrowserStageError(
            stage="wait_for_cdp",
            target_url=target_url,
            reason=str(error),
        ) from error
    try:
        navigation = navigate_and_close_other_tabs(ws_url, target_url)
    except BrowserStageError:
        raise
    except Exception as error:
        raise BrowserStageError(
            stage="navigate",
            target_url=target_url,
            reason=str(error),
        ) from error
    current_url = str(navigation.get("current_url") or "").strip()
    if not current_url or current_url == "about:blank":
        raise BrowserStageError(
            stage="navigate",
            target_url=target_url,
            reason=f"目标页准备失败：导航后页面仍为 {current_url or '空地址'}",
            current_url=current_url,
        )
    return {
        "target_url": target_url,
        "current_url": current_url,
        "closed_tabs": int(navigation.get("closed_tabs") or 0),
        "stages": [
            {"stage": "wait_for_cdp", "status": "ok"},
            {
                "stage": "close_other_tabs",
                "status": "ok",
                "closed_tabs": int(navigation.get("closed_tabs") or 0),
            },
            {
                "stage": "navigate",
                "status": "ok",
                "target_url": target_url,
                "current_url": current_url,
            },
        ],
    }


def sanitize_adspower_profile(profile):
    return {
        "profile_id": str(profile.get("profile_id") or profile.get("user_id") or ""),
        "profile_no": str(profile.get("profile_no") or profile.get("serial_number") or ""),
        "name": str(profile.get("name") or ""),
        "group_name": str(profile.get("group_name") or ""),
        "username": str(profile.get("username") or ""),
    }


def fetch_adspower_windows():
    response = requests.get(
        f"{get_adspower_base_url()}/api/v1/user/list",
        params={"page": 1, "page_size": 200},
        headers=get_adspower_headers(),
        timeout=10,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") not in {None, 0}:
        raise RuntimeError(
            f"AdsPower request failed: {payload.get('msg') or payload.get('message') or payload.get('code')}"
        )
    data = payload.get("data", payload)
    windows = [
        sanitize_adspower_profile(profile)
        for profile in data.get("list", [])
    ]
    return {"count": len(windows), "windows": windows}


def collect_strategy_comments(data_dir, brand_id=""):
    if brand_id:
        return list_copy_items(data_dir, brand_id)
    comments = []
    for brand in list_brands(data_dir):
        comments.extend(list_copy_items(data_dir, brand.get("id", "")))
    return comments


def strategy_comment_texts(values):
    """Return usable text from the existing copy-library item shapes."""

    texts = []
    for item in values or []:
        if isinstance(item, dict):
            value = item.get("body") or item.get("text") or item.get("content") or ""
            tags = item.get("tags") or []
            if tags:
                value = f"{value}\n\n{' '.join(str(tag) for tag in tags)}"
        else:
            value = item
        value = str(value or "").strip()
        if value:
            texts.append(value)
    return texts


def build_strategy_text_resolver(data_dir, *, rng=None):
    """Resolve fixed text or a random existing copy-library item per input block."""

    rng = rng or random.Random()
    cache = {}

    def resolve(action):
        content = action.get("params", {}).get("content", {})
        if content.get("source") == "fixed":
            return str(content.get("text") or "")
        if content.get("source") != "generated_comment":
            raise ValueError("keyboard content source is invalid")
        brand_id = str(content.get("brand_id") or "").strip()
        if brand_id not in cache:
            cache[brand_id] = strategy_comment_texts(
                collect_strategy_comments(data_dir, brand_id)
            )
        if not cache[brand_id]:
            raise ValueError("内容管理中没有可用文案")
        return rng.choice(cache[brand_id])

    return resolve


def build_execution_v2_content_library_provider(data_dir):
    """Expose existing copy-library metadata through the closed V2 contract."""

    async def provide():
        brands = await asyncio.to_thread(list_brands, data_dir)
        return [
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or "").strip(),
                "copy_count": item.get("copy_count", 0),
            }
            for item in brands
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]

    return provide


def build_execution_v2_text_resolver(data_dir, *, rng=None):
    """Select one existing library item independently for each V2 input action."""

    from execution_v2.actions import ActionExecutionError

    rng = rng or random.Random()

    async def resolve(action):
        library_id = str(action.get("content_library_id") or "").strip()
        if not library_id:
            raise ActionExecutionError("content_library_unavailable")
        values = await asyncio.to_thread(list_copy_items, data_dir, library_id)
        texts = strategy_comment_texts(values)
        if not texts:
            raise ActionExecutionError("content_library_unavailable")
        return rng.choice(texts)

    return resolve


def update_browser_batch_task(task_id, **updates):
    with BROWSER_BATCH_TASKS_LOCK:
        task = BROWSER_BATCH_TASKS.get(task_id)
        if task is None:
            return None
        task.update(updates)
        return dict(task)


def browser_strategy_gate_check(app, strategy_id, _action=None):
    failed_dependencies = app.config.get(
        "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
        set(),
    )
    if strategy_id in failed_dependencies:
        return {
            "strategy_id": strategy_id,
            "allowed": False,
            "effective_status": "paused",
            "reasons": [
                {
                    "source": "probe",
                    "reason_code": "dependency_index_unavailable",
                    "aliases": [],
                    "selector_version_id": "",
                }
            ],
        }
    return check_strategy_gate(
        app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"],
        strategy_id,
    )


def _close_app_gate_service(service):
    first_error = None
    seen = set()
    resources = (
        (service,)
        if callable(getattr(service, "close", None))
        else (
            getattr(service, "redis", None),
            getattr(service, "store", None),
        )
    )
    for resource in resources:
        if resource is None or id(resource) in seen:
            continue
        seen.add(id(resource))
        close = getattr(resource, "close", None)
        if not callable(close):
            continue
        try:
            close()
        except BaseException as error:
            if first_error is None:
                first_error = error
    if first_error is not None:
        raise first_error


def _dependency_aware_gate_factory(factory):
    migration_lock = threading.Lock()
    migrated = False

    def open_service():
        nonlocal migrated
        service = factory()
        rebuild = getattr(service, "rebuild_dependencies", None)
        if not callable(rebuild) or migrated:
            return service
        try:
            strategies = load_persisted_strategy_state()[
                "block_strategies"
            ]
            with migration_lock:
                if not migrated:
                    rebuild(strategies)
                    migrated = True
            return service
        except BaseException:
            try:
                _close_app_gate_service(service)
            except BaseException:
                pass
            raise

    return open_service


def _rebuild_strategy_dependencies(app, strategies):
    service = app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"]()
    try:
        rebuild = getattr(service, "rebuild_dependencies", None)
        if not callable(rebuild):
            raise RuntimeError("gate service cannot rebuild dependencies")
        rebuild(strategies)
    except BaseException:
        try:
            _close_app_gate_service(service)
        except BaseException:
            pass
        raise
    _close_app_gate_service(service)


def _strategy_ids(strategies):
    return {
        str(strategy.get("id") or "")
        for strategy in strategies
        if isinstance(strategy, dict) and strategy.get("id")
    }


def _rollback_strategy_dependencies(app, previous, candidate):
    affected = _strategy_ids(previous) | _strategy_ids(candidate)
    try:
        _rebuild_strategy_dependencies(app, previous)
    except Exception:
        app.config.setdefault(
            "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
            set(),
        ).update(affected)
        return False
    app.config.setdefault(
        "SELECTOR_PROBE_DEPENDENCY_SYNC_FAILED",
        set(),
    ).difference_update(affected)
    return True


def _strategy_gate_error(strategy_id, decision):
    from browser_strategy_runtime import StrategyPausedError

    return StrategyPausedError(
        strategy_id,
        "",
        0,
        decision.get("reasons", []),
        [],
    )


def _run_prepared_strategy_with_gate(
    runner,
    args,
    gate_check,
    on_action_dispatch=None,
):
    try:
        parameters = inspect.signature(runner).parameters.values()
        parameter_names = {parameter.name for parameter in parameters}
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        parameter_names = set()
        accepts_kwargs = True
    kwargs = {}
    if "gate_check" in parameter_names or accepts_kwargs:
        kwargs["gate_check"] = gate_check
    if (
        on_action_dispatch is not None
        and (
            "on_action_dispatch" in parameter_names
            or accepts_kwargs
        )
    ):
        kwargs["on_action_dispatch"] = on_action_dispatch
    return runner(*args, **kwargs)


def _record_browser_batch_task(task_id):
    with BROWSER_BATCH_TASKS_LOCK:
        stored_task = dict(BROWSER_BATCH_TASKS.get(task_id, {}))
    record_browser_log(
        "batch_task",
        public_browser_payload(
            {
                "task_id": task_id,
                "results": public_browser_batch_task(stored_task)["results"],
            }
        ),
    )


def run_browser_batch_task(app, task_id, profiles, batch_size, strategy, target_url):
    from browser_strategy_runtime import (
        build_batches,
        run_prepared_block_strategy_on_cdp,
    )

    browser = load_persisted_strategy_state()
    elements = browser["action_elements"]
    patterns = browser["interaction_patterns"]
    batches = build_batches(profiles, batch_size)
    strategy_id = str(strategy.get("id") or "").strip()
    action_execution_started = threading.Event()
    action_execution_completed = threading.Event()
    gate_check = lambda strategy_id, action=None: browser_strategy_gate_check(
        app,
        strategy_id,
        action,
    )
    initial_gate = (
        gate_check(strategy_id)
        if strategy_id
        else {"allowed": True, "reasons": []}
    )
    if initial_gate.get("allowed") is not True:
        update_browser_batch_task(
            task_id,
            status="delayed_gate",
            gate_reasons=initial_gate.get("reasons", []),
        )
        _record_browser_batch_task(task_id)
        return
    update_browser_batch_task(task_id, status="running", total_batches=len(batches))
    all_results = []

    try:
        for index, batch in enumerate(batches, start=1):
            current_gate = (
                gate_check(strategy_id)
                if strategy_id
                else {"allowed": True, "reasons": []}
            )
            if current_gate.get("allowed") is not True:
                execution_is_terminal = (
                    action_execution_started.is_set()
                    or action_execution_completed.is_set()
                )
                update_browser_batch_task(
                    task_id,
                    status=(
                        "failed"
                        if execution_is_terminal
                        else "delayed_gate"
                    ),
                    error=(
                        "strategy_paused_during_execution"
                        if execution_is_terminal
                        else ""
                    ),
                    gate_reasons=current_gate.get("reasons", []),
                    finished_at=(
                        content_now_iso()
                        if execution_is_terminal
                        else None
                    ),
                    action_execution_started=(
                        action_execution_started.is_set()
                    ),
                    action_execution_completed=(
                        action_execution_completed.is_set()
                    ),
                )
                _record_browser_batch_task(task_id)
                return
            update_browser_batch_task(
                task_id,
                current_batch=index,
                status="running",
            )
            session_results, layout = ensure_browser_profile_sessions(
                batch, lease_sessions=True
            )
            successful = [
                item for item in session_results if item.get("status") == "ready"
            ]
            ready_profile_ids = [item["profile_id"] for item in successful]

            def run_one(item):
                try:
                    tile_error = browser_tile_error(
                        layout, item["profile_id"], ready_profile_ids
                    )
                    if tile_error:
                        return {
                            "profile_id": item["profile_id"],
                            "status": "failed",
                            "stage": "tile",
                            "error": tile_error,
                        }
                    with browser_profile_execution_reservation(
                        item["profile_id"]
                    ):
                        reserved_gate = (
                            gate_check(strategy_id)
                            if strategy_id
                            else {"allowed": True, "reasons": []}
                        )
                        if reserved_gate.get("allowed") is not True:
                            raise _strategy_gate_error(
                                strategy_id,
                                reserved_gate,
                            )

                        def runtime_gate_check(
                            checked_strategy_id,
                            action=None,
                        ):
                            return gate_check(
                                checked_strategy_id,
                                action,
                            )

                        def on_action_dispatch(
                            _checked_strategy_id,
                            _action,
                        ):
                            action_execution_started.set()

                        raw_result = _run_prepared_strategy_with_gate(
                            run_prepared_block_strategy_on_cdp,
                            (
                                item["ws_url"],
                                target_url,
                                strategy,
                                elements,
                                patterns,
                                build_strategy_text_resolver(
                                    app.config["CONTENT_DATA_DIR"]
                                ),
                            ),
                            runtime_gate_check,
                            on_action_dispatch,
                        )
                        if (
                            isinstance(raw_result, dict)
                            and raw_result.get("actions")
                        ):
                            action_execution_completed.set()
                        result = public_strategy_execution_result(
                            raw_result,
                            strategy,
                            elements,
                        )
                    return {
                        **result,
                        "profile_id": item["profile_id"],
                        "status": "ok",
                        "stage": "execute_actions",
                    }
                except Exception as error:
                    if getattr(error, "completed_actions", None):
                        action_execution_completed.set()
                    return public_strategy_failure_result(
                        profile_id=item["profile_id"],
                        attempts=item.get("attempts", 0),
                        target_url=target_url,
                        error=error,
                        strategy=strategy,
                        elements=elements,
                    )

            try:
                with ThreadPoolExecutor(max_workers=max(len(successful), 1)) as executor:
                    batch_results = (
                        list(executor.map(run_one, successful)) if successful else []
                    )
            finally:
                release_browser_session_results(
                    session_results, request_close=True
                )
            batch_results.extend(
                {
                    "profile_id": item.get("profile_id", ""),
                    "status": "failed",
                    "error": item.get("error", "窗口启动失败"),
                }
                for item in session_results
                if item.get("status") != "ready"
            )
            batch_results = [
                public_browser_batch_result(item) for item in batch_results
            ]
            all_results.extend(batch_results)
            update_browser_batch_task(
                task_id,
                completed_batches=index,
                processed_windows=len(all_results),
                failed_windows=len([item for item in all_results if item.get("status") == "failed"]),
                results=list(all_results),
                action_execution_started=action_execution_started.is_set(),
                action_execution_completed=action_execution_completed.is_set(),
            )
            if any(
                item.get("code")
                == "strategy_paused_during_execution"
                for item in batch_results
            ):
                execution_is_terminal = (
                    action_execution_started.is_set()
                    or action_execution_completed.is_set()
                )
                update_browser_batch_task(
                    task_id,
                    status=(
                        "failed"
                        if execution_is_terminal
                        else "delayed_gate"
                    ),
                    error=(
                        "strategy_paused_during_execution"
                        if execution_is_terminal
                        else ""
                    ),
                    finished_at=(
                        content_now_iso()
                        if execution_is_terminal
                        else None
                    ),
                )
                _record_browser_batch_task(task_id)
                return
        update_browser_batch_task(task_id, status="completed", finished_at=content_now_iso())
    except Exception:
        update_browser_batch_task(
            task_id,
            status="failed",
            error="batch_task_failed",
            results=[public_browser_batch_result(item) for item in all_results],
        )
    _record_browser_batch_task(task_id)


def build_direct_agent_command(payload):
    profile_id = str(payload.get("profile_id") or "").strip()
    profile_no = str(payload.get("profile_no") or "").strip()
    if not profile_id and not profile_no:
        raise ValueError("profile_id or profile_no is required")

    try:
        max_steps = int(payload.get("max_steps") or 10)
    except (TypeError, ValueError) as error:
        raise ValueError("max_steps must be between 1 and 50") from error

    if max_steps < 1 or max_steps > 50:
        raise ValueError("max_steps must be between 1 and 50")

    command = ["npm", "run", "direct-agent", "--"]
    if profile_id:
      command.extend(["--profile-id", profile_id])
    else:
      command.extend(["--profile-no", profile_no])

    url = str(payload.get("url") or "").strip()
    if url:
        command.extend(["--url", url])

    command.extend(["--max-steps", str(max_steps)])

    if payload.get("close_after_run") is False:
        command.append("--no-close")

    return command


def build_search_agent_command(payload):
    url = str(payload.get("url") or "").strip()
    search_xpath = str(payload.get("search_xpath") or "").strip()
    if not url:
        raise ValueError("url is required")
    if not search_xpath:
        raise ValueError("search_xpath is required")

    command = ["npm", "run", "search-agent", "--"]

    profile_ids = str(payload.get("profile_ids") or "").strip()
    profile_nos = str(payload.get("profile_nos") or "").strip()
    if profile_ids:
        command.extend(["--profile-ids", profile_ids])
    elif profile_nos:
        command.extend(["--profile-nos", profile_nos])

    command.extend(["--url", url])

    login_check_xpath = str(payload.get("login_check_xpath") or "").strip()
    if login_check_xpath:
        command.extend(["--login-check-xpath", login_check_xpath])

    command.extend(["--search-xpath", search_xpath])

    query = str(payload.get("query") or "").strip()
    if query:
        command.extend(["--query", query])

    strategy = str(payload.get("strategy") or "rotate").strip()
    if strategy:
        command.extend(["--strategy", strategy])

    if payload.get("close_after_run") is False:
        command.append("--no-close")

    return command


def normalize_execution_strategy(strategy):
    if not isinstance(strategy, dict):
        raise ValueError("strategy must be an object")

    normalized = {
        "id": str(strategy.get("id") or "").strip(),
        "label": str(strategy.get("label") or "").strip(),
        "mouseMoves": int(strategy.get("mouseMoves", 0)),
        "clicks": int(strategy.get("clicks", 0)),
        "scrolls": int(strategy.get("scrolls", 0)),
        "moveSteps": _normalize_number_range(strategy.get("moveSteps"), "moveSteps"),
        "pauseMs": _normalize_number_range(strategy.get("pauseMs"), "pauseMs"),
        "scrollDelta": _normalize_number_range(strategy.get("scrollDelta"), "scrollDelta"),
        "text_prompt": str(
            strategy.get("text_prompt")
            or strategy.get("textPrompt")
            or ""
        ).strip(),
    }
    if not normalized["id"]:
        raise ValueError("strategy id is required")
    if normalized["mouseMoves"] < 0 or normalized["clicks"] < 0 or normalized["scrolls"] < 0:
        raise ValueError("strategy counts must be zero or greater")
    return normalized


def _normalize_number_range(value, field_name):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError(f"{field_name} must be a two-item list")
    lower = int(value[0])
    upper = int(value[1])
    if lower < 0 or upper < lower:
        raise ValueError(f"{field_name} range is invalid")
    return [lower, upper]


def normalize_execution_strategies(payload):
    items = payload.get("items") if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    return {"items": [normalize_execution_strategy(item) for item in items]}


def save_execution_strategies(payload):
    normalized = normalize_execution_strategies(payload)
    settings = load_settings()
    settings["execution_strategies"] = normalized
    save_settings(settings)
    return normalized


def select_model_for_generation(settings):
    models = settings.get("models", {})
    target_id = str(models.get("default_model_id") or "").strip()
    enabled_models = [
        item for item in models.get("items", [])
        if item.get("enabled", True)
    ]
    for item in enabled_models:
        if not target_id or item.get("id") == target_id:
            return item
    if enabled_models:
        return enabled_models[0]
    raise ValueError("No enabled model is configured")


def build_strategy_generation_prompt(user_prompt):
    return (
        "请生成浏览器执行策略 JSON 数组。只返回 JSON，不要 Markdown。"
        "每个策略必须包含 id, label, mouseMoves, clicks, scrolls, "
        "moveSteps, pauseMs, scrollDelta, text_prompt。"
        "moveSteps/pauseMs/scrollDelta 必须是两个数字组成的数组。"
        f"\n需求：{user_prompt or '生成三种自然的人类浏览策略'}"
    )


def request_model_text(model_config, prompt):
    base_url = str(model_config.get("base_url") or "").rstrip("/")
    api_key = str(model_config.get("api_key") or "").strip()
    model_name = str(model_config.get("model") or "").strip()
    mode = str(model_config.get("mode") or "chat").strip()
    if not base_url or not api_key or not model_name:
        raise ValueError("model base_url, api_key, and model are required")

    headers = {"Authorization": f"Bearer {api_key}"}
    if mode == "responses":
        response = requests.post(
            f"{base_url}/responses",
            json={
                "model": model_name,
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": prompt}],
                    }
                ],
            },
            headers=headers,
            timeout=60,
        )
    else:
        response = requests.post(
            f"{base_url}/chat/completions",
            json={
                "model": model_name,
                "messages": [{"role": "user", "content": prompt}],
            },
            headers=headers,
            timeout=60,
        )
    response.raise_for_status()
    return extract_model_text(response.json())


def extract_model_text(payload):
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    choices = payload.get("choices") or []
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "\n".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict)
            )

    output = payload.get("output") or []
    texts = []
    for item in output:
        for part in item.get("content", []):
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    return "\n".join(texts)


def parse_strategy_json_from_text(text):
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("model returned empty content")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\[[\s\S]*\]", raw)
        if not match:
            raise ValueError("model did not return a JSON array")
        parsed = json.loads(match.group(0))
    return normalize_execution_strategies(parsed)


def generate_execution_strategies(prompt):
    settings = load_settings()
    model = select_model_for_generation(settings)
    text = request_model_text(model, build_strategy_generation_prompt(prompt))
    return parse_strategy_json_from_text(text)
