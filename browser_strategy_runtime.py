"""Canonical runtime for persisted browser block strategies."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import random
import re
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from browser_actions import execute_action
from browser_element_resolver import LocatorResolutionError
from browser_strategy_config import normalize_block_strategies, normalize_elements


_SAFE_LOCATOR_DIAGNOSTIC_KEYS = frozenset(
    {
        "actionable_count",
        "article_count",
        "candidate_id",
        "candidate_type",
        "center_intersection_count",
        "container_count",
        "input_count",
        "matching_article_id_count",
        "panel_count",
        "phase",
        "raw_count",
        "timeout_seconds",
        "usable_input_count",
        "usable_panel_count",
        "visible_article_count",
        "visible_container_count",
        "visible_count",
        "visible_input_count",
        "visible_panel_count",
    }
)
_SAFE_LOCATOR_TYPES = frozenset({"attribute", "css", "role", "xpath"})
_SAFE_LOCATOR_SCOPES = frozenset(
    {"active_video", "page", "visible_comment_panel"}
)
_SAFE_ACTION_NUMERIC_FIELDS = frozenset(
    {
        "click_count",
        "completed_switches",
        "count",
        "distance",
        "duration_seconds",
        "hold_seconds",
        "requested_switches",
        "wheel_events",
    }
)
def _safe_locator_diagnostics(value):
    if not isinstance(value, dict):
        return {}
    safe = {
        key: item
        for key, item in value.items()
        if key in _SAFE_LOCATOR_DIAGNOSTIC_KEYS
        and isinstance(item, (str, int, float, bool))
    }
    candidates = value.get("candidates")
    if isinstance(candidates, list):
        safe["candidates"] = [
            {
                key: candidate[key]
                for key in (
                    "id",
                    "type",
                    "raw_count",
                    "visible_count",
                    "actionable_count",
                )
                if key in candidate
                and isinstance(candidate[key], (str, int, float, bool))
            }
            for candidate in candidates
            if isinstance(candidate, dict)
        ]
    return safe


def _safe_locator_error(error: BaseException) -> dict | None:
    if isinstance(error, LocatorResolutionError):
        return {
            "code": error.code,
            "alias": error.alias,
            "scope": error.scope,
            "diagnostics": _safe_locator_diagnostics(error.diagnostics),
        }
    locator = getattr(error, "locator", None)
    return locator if isinstance(locator, dict) else None


def _safe_locator_measurement(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    scope = value.get("scope")
    candidate_id = value.get("candidate_id")
    candidate_type = value.get("candidate_type")
    if (
        scope not in _SAFE_LOCATOR_SCOPES
        or not isinstance(candidate_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", candidate_id)
        is None
        or candidate_type not in _SAFE_LOCATOR_TYPES
    ):
        return {}
    return {
        "scope": scope,
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
    }


def _masked_switch_identity(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return ""
    if re.fullmatch(r"[0-9a-f]{12}", value):
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _safe_switch_records(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    records = []
    for item in value:
        if not isinstance(item, dict):
            continue
        before = _masked_switch_identity(item.get("from"))
        after = _masked_switch_identity(item.get("to"))
        wheel_events = item.get("wheel_events")
        if (
            not before
            or not after
            or isinstance(wheel_events, bool)
            or not isinstance(wheel_events, int)
        ):
            continue
        records.append(
            {
                "from": before,
                "to": after,
                "wheel_events": max(wheel_events, 0),
            }
        )
    return records


def _safe_action_result(action: dict, value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    safe = {
        "action_id": action["id"],
        "type": action["type"],
        "status": "ok",
    }
    element = str(action.get("params", {}).get("element") or "")
    if element:
        safe["element"] = element
    for field in _SAFE_ACTION_NUMERIC_FIELDS:
        item = raw.get(field)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            safe[field] = max(item, 0)
    for field, allowed in (
        ("button", {"left", "middle", "right"}),
        ("postcondition", {"not_configured", "observed"}),
        ("trajectory_source", {"ghost-cursor", "recorded-pattern"}),
    ):
        item = raw.get(field)
        if item in allowed:
            safe[field] = item
    locator = _safe_locator_measurement(raw.get("locator"))
    if locator:
        safe["locator"] = locator
    if "switches" in raw:
        safe["switches"] = _safe_switch_records(raw["switches"])
    return safe


def _copy_safe_switch_error_fields(target: BaseException, source: BaseException) -> None:
    code = getattr(source, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", code):
        target.code = code
    for field in (
        "requested_switches",
        "completed_switches",
        "wheel_events",
    ):
        item = getattr(source, field, None)
        if isinstance(item, int) and not isinstance(item, bool):
            setattr(target, field, max(item, 0))
    switches = getattr(source, "switches", None)
    if isinstance(switches, (list, tuple)):
        target.switches = _safe_switch_records(switches)


def _copy_completed_actions(value: object) -> list[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        return []
    copied = []
    for item in value:
        action_id = item.get("action_id")
        action_type = item.get("type")
        element = item.get("element", "")
        cycle = item.get("cycle")
        action_index = item.get("action_index")
        if (
            not isinstance(action_id, str)
            or not action_id
            or not isinstance(action_type, str)
            or not action_type
            or item.get("status") != "ok"
            or not isinstance(element, str)
            or isinstance(cycle, bool)
            or not isinstance(cycle, int)
            or cycle < 1
            or isinstance(action_index, bool)
            or not isinstance(action_index, int)
            or action_index < 1
        ):
            return []
        copied.append(
            {
                **_safe_action_result(
                    {
                        "id": action_id,
                        "type": action_type,
                        "params": {"element": element},
                    },
                    item,
                ),
                "cycle": cycle,
                "action_index": action_index,
            }
        )
    return copied


class BlockExecutionError(RuntimeError):
    """One action failed after a strategy cycle had started."""

    def __init__(
        self,
        action_id: str,
        action_index: int,
        action_type: str,
        reason: str,
        page_recoveries: list[dict] | None = None,
        *,
        locator: dict | None = None,
        source: BaseException | None = None,
        cycle: int | None = None,
        completed_actions: list[dict] | None = None,
    ) -> None:
        self.action_id = action_id
        self.action_index = action_index
        self.action_type = action_type
        self.reason = reason
        self.page_recoveries = list(page_recoveries or [])
        self.locator = locator
        self.locator_diagnostics = locator
        self.completed_actions = _copy_completed_actions(completed_actions)
        if isinstance(cycle, int) and not isinstance(cycle, bool) and cycle > 0:
            self.cycle = cycle
        if source is not None:
            _copy_safe_switch_error_fields(self, source)
        super().__init__(
            f"action {action_index} ({action_type}, {action_id}) failed: {reason}"
        )


def _safe_gate_reasons(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    reasons = []
    for raw in value[:16]:
        public_dict = getattr(raw, "public_dict", None)
        if callable(public_dict):
            try:
                raw = public_dict()
            except Exception:
                continue
        if not isinstance(raw, dict):
            continue
        item = {}
        source = raw.get("source")
        if source in {"manual", "probe"}:
            item["source"] = source
        reason_code = raw.get("reason_code")
        if (
            isinstance(reason_code, str)
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason_code)
        ):
            item["reason_code"] = reason_code
        aliases = raw.get("aliases")
        if isinstance(aliases, (list, tuple)) and not isinstance(
            aliases,
            (str, bytes),
        ):
            safe_aliases = sorted(
                {
                    alias
                    for alias in aliases[:64]
                    if isinstance(alias, str)
                    and alias
                    and alias == alias.strip()
                    and len(alias) <= 128
                    and not any(
                        ord(character) < 32 or ord(character) == 127
                        for character in alias
                    )
                }
            )
            if safe_aliases:
                item["aliases"] = safe_aliases
        version = raw.get("selector_version_id")
        if (
            isinstance(version, str)
            and version
            and version == version.strip()
            and len(version) <= 128
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", version)
        ):
            item["selector_version_id"] = version
        if item:
            reasons.append(item)
    return reasons


def _safe_page_origin(value: object) -> str | None:
    if value == "":
        return ""
    if not isinstance(value, str) or len(value) > 512:
        return None
    try:
        parsed = urlsplit(value)
        port = f":{parsed.port}" if parsed.port else ""
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _safe_page_recoveries(value: object) -> list[dict]:
    if not isinstance(value, (list, tuple)):
        return []
    projected = []
    for raw in value[:64]:
        if not isinstance(raw, dict):
            continue
        item = {}
        for field in ("action_id", "action_type"):
            candidate = raw.get(field)
            if (
                isinstance(candidate, str)
                and candidate
                and candidate == candidate.strip()
                and len(candidate) <= 128
                and not any(
                    ord(character) < 32 or ord(character) == 127
                    for character in candidate
                )
            ):
                item[field] = candidate
        for field in ("old_page_origin", "new_page_origin"):
            origin = _safe_page_origin(raw.get(field))
            if origin is not None:
                item[field] = origin
        for field, allowed in (
            (
                "closure_type",
                {
                    "browser_disconnected",
                    "browser_closed",
                    "context_closed",
                    "page_closed",
                    "target_closed",
                    "target_detached",
                },
            ),
            (
                "closure_reason",
                {
                    "browser disconnected",
                    "browser has been closed",
                    "closed target",
                    "context closed",
                    "page closed",
                    "target closed",
                    "target detached",
                    "target page, context or browser has been closed",
                },
            ),
            ("status", {"failed", "recovered"}),
            (
                "outcome",
                {
                    "not_retried",
                    "recovered",
                    "replacement_not_found",
                    "retry_failed",
                },
            ),
        ):
            candidate = raw.get(field)
            if candidate in allowed:
                item[field] = candidate
        replacement_found = raw.get("replacement_found")
        if isinstance(replacement_found, bool):
            item["replacement_found"] = replacement_found
        for field in ("retry", "action_index", "cycle"):
            candidate = raw.get(field)
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and candidate >= (1 if field in {"action_index", "cycle"} else 0)
            ):
                item[field] = candidate
        if item:
            projected.append(item)
    return projected


class StrategyPausedError(RuntimeError):
    """A gate stopped execution before the next side effect."""

    code = "strategy_paused_during_execution"

    def __init__(
        self,
        strategy_id: str,
        action_id: str,
        action_index: int,
        reasons: object,
        completed_actions: object,
        *,
        action_type: str = "",
        cycle: int | None = None,
        page_recoveries: object = None,
    ) -> None:
        self.strategy_id = strategy_id if isinstance(strategy_id, str) else ""
        self.action_id = action_id if isinstance(action_id, str) else ""
        self.action_index = (
            action_index
            if isinstance(action_index, int)
            and not isinstance(action_index, bool)
            and action_index >= 0
            else 0
        )
        self.action_type = (
            action_type if isinstance(action_type, str) else ""
        )
        self.reasons = _safe_gate_reasons(reasons)
        self.completed_actions = _copy_completed_actions(completed_actions)
        self.page_recoveries = _safe_page_recoveries(page_recoveries)
        if isinstance(cycle, int) and not isinstance(cycle, bool) and cycle > 0:
            self.cycle = cycle
        super().__init__(
            f"strategy {self.strategy_id} paused before action "
            f"{self.action_index} ({self.action_id})"
        )


SENSITIVE_RUNTIME_KEY_MARKERS = (
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


def _normalize_runtime_key(key: object) -> str:
    decoded = unquote(str(key)).lower()
    bracket_normalized = re.sub(r"\[([^\]]*)\]", r"_\1", decoded)
    return re.sub(r"[^a-z0-9]", "", bracket_normalized)


def _is_sensitive_runtime_key(key: object) -> bool:
    normalized = _normalize_runtime_key(key)
    return any(marker in normalized for marker in SENSITIVE_RUNTIME_KEY_MARKERS)


RUNTIME_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>[a-z%][a-z0-9_%\[\].-]*"
    r"(?:[ \t]+[a-z0-9_%\[\].-]+){0,3})"
    r"[ \t]*(?P<separator>=|:)[ \t]*"
)
RUNTIME_SPACE_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>"
    r"access[ _.-]*key(?:[ _.-]*id)?|"
    r"api[ _.-]*key|authorization|cookie|credential|password|"
    r"secret|session(?:[ _.-]*id)?|token"
    r")(?P<separator>[ \t]+)"
)
RUNTIME_HEADER_PATTERN = re.compile(
    r"(?i)(?<![a-z0-9_%\[\].-])"
    r"(?P<key>cookie|authorization)"
    r"[ \t]*(?P<separator>:|=)[ \t]*"
)
SAFE_RUNTIME_DIAGNOSTIC_KEYS = {
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
SAFE_RUNTIME_CREDENTIAL_STATUSES = {
    "expired",
    "invalid",
    "missing",
    "not configured",
}


def _is_safe_runtime_credential_value(value: str) -> bool:
    normalized = value.strip().strip("\"'").casefold()
    if normalized in SAFE_RUNTIME_CREDENTIAL_STATUSES:
        return True
    scheme_and_status = normalized.split(None, 1)
    return (
        len(scheme_and_status) == 2
        and re.fullmatch(r"[a-z][a-z0-9_-]*", scheme_and_status[0]) is not None
        and scheme_and_status[1] in SAFE_RUNTIME_CREDENTIAL_STATUSES
    )


def _is_safe_runtime_header_value(value: str) -> bool:
    normalized = value.strip()
    if (
        len(normalized) >= 2
        and normalized[0] in "\"'"
        and normalized[-1] == normalized[0]
    ):
        normalized = normalized[1:-1].strip()
    return normalized.casefold() in SAFE_RUNTIME_CREDENTIAL_STATUSES


def _redact_runtime_header_line(line: str) -> str:
    match = RUNTIME_HEADER_PATTERN.search(line)
    if match is None or _is_safe_runtime_header_value(line[match.end() :]):
        return line
    return f"{line[:match.end()]}[redacted]"


def _sanitize_runtime_headers(value: str) -> str:
    sanitized = []
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        sanitized.extend(
            (_redact_runtime_header_line(body), line[len(body) :])
        )
    if not sanitized and value == "":
        return ""
    return "".join(sanitized)


def _trusted_runtime_assignment_boundary(matches, start_index):
    for match in matches[start_index + 1 :]:
        if _normalize_runtime_key(match.group("key")) in SAFE_RUNTIME_DIAGNOSTIC_KEYS:
            return match
    return None


def _redact_runtime_assignment_line(line: str) -> str:
    matches = sorted(
        [
            *RUNTIME_ASSIGNMENT_PATTERN.finditer(line),
            *RUNTIME_SPACE_ASSIGNMENT_PATTERN.finditer(line),
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
        if not _is_sensitive_runtime_key(match.group("key")):
            continue
        boundary = _trusted_runtime_assignment_boundary(matches, index)
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
        if _is_safe_runtime_credential_value(raw_value):
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


def _sanitize_runtime_assignments(value: str) -> str:
    sanitized = []
    for line in value.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        sanitized.extend((_redact_runtime_assignment_line(body), line[len(body) :]))
    if not sanitized and value == "":
        return ""
    return "".join(sanitized)


def _sanitize_runtime_structure(value: str) -> str:
    parts = value.split("/")
    projected = []
    redact_next = False
    for part in parts:
        if redact_next:
            projected.append("[redacted]")
            redact_next = False
            continue
        if "=" in part or ":" in part or "&" in part:
            projected.append(part)
            continue
        sensitive = _is_sensitive_runtime_key(part.lstrip("#"))
        projected.append("[redacted]" if sensitive else part)
        redact_next = sensitive
    return "/".join(projected)


def _sanitize_runtime_structured_tokens(value: str) -> str:
    projected = []
    for token in re.split(r"(\s+)", value):
        positions = [index for index in (token.find("#"), token.find("/")) if index >= 0]
        if not positions:
            projected.append(token)
            continue
        start = min(positions)
        projected.append(token[:start] + _sanitize_runtime_structure(token[start:]))
    return "".join(projected)


def _safe_runtime_reason(error: BaseException) -> str:
    text = str(error)
    text = re.sub(
        r"\b(?:wss?|https?)://[^\s,;\)\]\}>\"']+",
        "[redacted-url]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"(?i)(\bbearer\s+)[^\s,;]+",
        r"\1[redacted]",
        _sanitize_runtime_structured_tokens(text),
    )
    return _sanitize_runtime_assignments(_sanitize_runtime_headers(text))


class StrategyRuntimeError(RuntimeError):
    """Stable staged failure contract for the combined CDP runtime."""

    def __init__(
        self,
        stage: str,
        reason: str,
        *,
        target_url: str = "",
        current_url: str = "",
        page_recoveries: list[dict] | None = None,
        source: BaseException | None = None,
    ) -> None:
        self.stage = stage
        self.reason = _safe_runtime_reason(RuntimeError(reason))
        self.target_url = target_url
        self.current_url = current_url
        self.page_recoveries = list(page_recoveries or [])
        if source is not None:
            for field in ("action_id", "action_index", "action_type", "cycle"):
                if hasattr(source, field):
                    setattr(self, field, getattr(source, field))
            _copy_safe_switch_error_fields(self, source)
            if hasattr(source, "completed_actions"):
                self.completed_actions = _copy_completed_actions(
                    getattr(source, "completed_actions")
                )
        self.locator = _safe_locator_error(source) if source is not None else None
        self.locator_diagnostics = self.locator
        super().__init__(self.reason)


def _current_page_url(context) -> str:
    for page in reversed(list(getattr(context, "pages", []))):
        try:
            if not page.is_closed():
                return str(page.url or "")
        except Exception:
            continue
    return ""


def build_batches(items: list[Any], batch_size: int) -> list[list[Any]]:
    """Split windows into bounded batches without dropping a remainder."""

    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError) as error:
        raise ValueError("每批窗口数量必须是整数") from error
    if not 1 <= batch_size <= 8:
        raise ValueError("每批窗口数量必须在 1 到 8 之间")
    return [
        items[index : index + batch_size]
        for index in range(0, len(items), batch_size)
    ]


def _validate_strategy(strategy, elements, patterns) -> dict:
    if not isinstance(strategy, dict):
        raise ValueError("block strategy must be a JSON object")
    if strategy.get("status") == "needs_repair" or strategy.get("repair_errors"):
        reasons = strategy.get("repair_errors") or []
        detail = f": {'; '.join(str(item) for item in reasons)}" if reasons else ""
        raise ValueError(f"strategy needs repair before execution{detail}")
    normalized = normalize_block_strategies(
        [strategy],
        elements,
        patterns,
    )[0]
    if normalized.get("status") != "ready":
        raise ValueError("strategy needs repair before execution")
    if normalized["run_mode"] == "loop" and not normalized["actions"]:
        raise ValueError("loop strategy must contain at least one action")
    return normalized


async def _check_strategy_gate(gate_check, strategy_id, action) -> None:
    if gate_check is None:
        return
    try:
        decision = gate_check(strategy_id, action)
        if inspect.isawaitable(decision):
            decision = await decision
    except asyncio.CancelledError:
        raise
    except Exception:
        raise StrategyPausedError(
            strategy_id=strategy_id,
            action_id=str(action.get("id") if action else ""),
            action_index=0,
            reasons=[
                {
                    "source": "probe",
                    "reason_code": "registry_unavailable",
                }
            ],
            completed_actions=[],
            action_type=str(action.get("type") if action else ""),
        ) from None
    if isinstance(decision, dict):
        allowed = decision.get("allowed")
        reasons = decision.get("reasons")
    else:
        allowed = getattr(decision, "allowed", None)
        reasons = getattr(decision, "reasons", None)
    if allowed is True:
        return
    safe_reasons = _safe_gate_reasons(reasons)
    if allowed is not False or not safe_reasons:
        safe_reasons = [
            {
                "source": "probe",
                "reason_code": "registry_unavailable",
            }
        ]
    raise StrategyPausedError(
        strategy_id=strategy_id,
        action_id=str(action.get("id") if action else ""),
        action_index=0,
        reasons=safe_reasons,
        completed_actions=[],
        action_type=str(action.get("type") if action else ""),
    )


async def _notify_action_dispatch(on_action_dispatch, strategy_id, action) -> None:
    if on_action_dispatch is None:
        return
    result = on_action_dispatch(strategy_id, action)
    if inspect.isawaitable(result):
        await result


async def run_block_strategy(
    page,
    strategy,
    elements,
    patterns,
    text_resolver,
    *,
    rng=random,
    sleep_fn=asyncio.sleep,
    monotonic_fn=time.monotonic,
    execute_fn=execute_action,
    page_lifecycle=None,
    gate_check=None,
    on_action_dispatch=None,
) -> dict:
    """Validate and execute one canonical strategy on an existing page."""

    canonical_elements = normalize_elements(elements)
    normalized = _validate_strategy(strategy, canonical_elements, patterns)
    await _check_strategy_gate(gate_check, normalized["id"], None)
    pattern_map = {item["id"]: item for item in patterns}
    results = []
    page_recoveries = []
    cycles = 0
    sampled_duration = None
    deadline = None
    if normalized["run_mode"] == "loop":
        sampled_duration = float(
            rng.uniform(*normalized["loop_duration_minutes"])
        )
        deadline = monotonic_fn() + sampled_duration * 60

    while normalized["run_mode"] == "once" or monotonic_fn() < deadline:
        cycles += 1
        for index, action in enumerate(normalized["actions"], start=1):
            try:
                await _check_strategy_gate(
                    gate_check,
                    normalized["id"],
                    action,
                )

                action_dispatched = False

                async def invoke(selected_page):
                    async def before_side_effect():
                        nonlocal action_dispatched
                        await _check_strategy_gate(
                            gate_check,
                            normalized["id"],
                            action,
                        )
                        if not action_dispatched:
                            await _notify_action_dispatch(
                                on_action_dispatch,
                                normalized["id"],
                                action,
                            )
                            action_dispatched = True

                    kwargs = {
                        "rng": rng,
                        "sleep_fn": sleep_fn,
                        "before_side_effect": before_side_effect,
                    }
                    if page_lifecycle is not None:
                        kwargs["page_lifecycle"] = page_lifecycle
                    return await execute_fn(
                        selected_page,
                        action,
                        canonical_elements,
                        pattern_map,
                        text_resolver,
                        **kwargs,
                    )

                if page_lifecycle is None:
                    result = await invoke(page)
                else:
                    page, result, recovery_events = await page_lifecycle.execute(
                        page,
                        action,
                        invoke,
                    )
                    if isinstance(recovery_events, dict):
                        recovery_events = [recovery_events]
                    for recovery in recovery_events or []:
                        recovery["action_index"] = index
                        if normalized["run_mode"] == "loop":
                            recovery["cycle"] = cycles
                        page_recoveries.append(recovery)
            except StrategyPausedError as error:
                attached_recoveries = [
                    dict(item)
                    for item in getattr(error, "page_recoveries", [])
                    if isinstance(item, dict)
                ]
                raise StrategyPausedError(
                    strategy_id=normalized["id"],
                    action_id=action["id"],
                    action_index=index,
                    reasons=error.reasons,
                    completed_actions=[dict(item) for item in results],
                    action_type=action["type"],
                    cycle=cycles,
                    page_recoveries=[
                        *[dict(item) for item in page_recoveries],
                        *attached_recoveries,
                    ],
                ) from error
            except Exception as error:
                error_recoveries = []
                for recovery in getattr(error, "page_recoveries", []):
                    if not isinstance(recovery, dict):
                        continue
                    error_recoveries.append(dict(recovery))
                raise BlockExecutionError(
                    action["id"],
                    index,
                    action["type"],
                    str(error),
                    error_recoveries,
                    locator=_safe_locator_error(error),
                    source=error,
                    cycle=cycles,
                    completed_actions=[dict(item) for item in results],
                ) from error
            page = result.pop("_active_page", page)
            for recovery in result.pop("_page_recoveries", []):
                recovery["action_index"] = index
                if normalized["run_mode"] == "loop":
                    recovery["cycle"] = cycles
                page_recoveries.append(recovery)
            results.append(
                {
                    **_safe_action_result(action, result),
                    "cycle": cycles,
                    "action_index": index,
                }
            )
        if normalized["run_mode"] == "once":
            break

    return {
        "status": "ok",
        "strategy_id": normalized["id"],
        "run_mode": normalized["run_mode"],
        "cycles": cycles,
        "sampled_duration_minutes": sampled_duration,
        "actions": results,
        "page_recoveries": page_recoveries,
    }


async def _run_block_strategy_on_cdp(
    ws_url,
    strategy,
    elements,
    patterns,
    text_resolver,
) -> dict:
    # Reject invalid and repair-needed state before Playwright touches the browser.
    _validate_strategy(strategy, elements, patterns)
    try:
        from playwright.async_api import async_playwright
    except ImportError as error:  # pragma: no cover - dependency check handles this
        raise RuntimeError("未安装 Playwright，无法运行积木策略") from error

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(
            ws_url, timeout=10_000
        )
        contexts = list(browser.contexts)
        if not contexts:
            raise RuntimeError("ws.puppeteer 未返回可操作的浏览器上下文")
        context = contexts[0]
        pages = list(context.pages)
        page = pages[0] if pages else await context.new_page()
        return await run_block_strategy(
            page,
            strategy,
            elements,
            patterns,
            text_resolver,
        )
    finally:
        await playwright.stop()


def run_block_strategy_on_cdp(
    ws_url,
    strategy,
    elements,
    patterns,
    text_resolver,
) -> dict:
    """Connect over CDP without closing the user-owned AdsPower browser."""

    return asyncio.run(
        _run_block_strategy_on_cdp(
            ws_url,
            strategy,
            elements,
            patterns,
            text_resolver,
        )
    )


async def _run_prepared_block_strategy_on_cdp(
    ws_url,
    target_url,
    strategy,
    elements,
    patterns,
    text_resolver,
    *,
    gate_check=None,
    on_action_dispatch=None,
) -> dict:
    _validate_strategy(strategy, elements, patterns)
    try:
        from playwright.async_api import async_playwright

        from browser_page_lifecycle import PageLifecycle, prepare_target_page

        playwright = await async_playwright().start()
    except Exception as error:
        raise StrategyRuntimeError(
            "connect",
            str(error),
            target_url=target_url,
            source=error,
        ) from error
    context = None
    primary_error = None
    try:
        try:
            browser = await playwright.chromium.connect_over_cdp(
                ws_url, timeout=10_000
            )
            contexts = list(browser.contexts)
            if not contexts:
                raise RuntimeError(
                    "ws.puppeteer did not return an operable browser context"
                )
            context = contexts[0]
            if not context.pages:
                await context.new_page()
        except Exception as error:
            raise StrategyRuntimeError(
                "connect",
                str(error),
                target_url=target_url,
                current_url=_current_page_url(context),
                source=error,
            ) from error

        lifecycle = PageLifecycle(context, target_url)
        try:
            page, prepared = await prepare_target_page(lifecycle, target_url)
        except Exception as error:
            raise StrategyRuntimeError(
                "prepare_page",
                str(error),
                target_url=target_url,
                current_url=_current_page_url(context),
                source=error,
            ) from error
        try:
            executed = await run_block_strategy(
                page,
                strategy,
                elements,
                patterns,
                text_resolver,
                page_lifecycle=lifecycle,
                gate_check=gate_check,
                on_action_dispatch=on_action_dispatch,
            )
        except StrategyPausedError:
            raise
        except Exception as error:
            raise StrategyRuntimeError(
                "execute_actions",
                str(error),
                target_url=target_url,
                current_url=_current_page_url(context),
                page_recoveries=getattr(error, "page_recoveries", []),
                source=error,
            ) from error
        prepared["stages"].append(
            {"stage": "execute_actions", "status": "ok"}
        )
        return {**executed, **prepared}
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            await playwright.stop()
        except Exception as cleanup_error:
            if primary_error is None:
                raise StrategyRuntimeError(
                    "cleanup",
                    str(cleanup_error),
                    target_url=target_url,
                    current_url=_current_page_url(context),
                    source=cleanup_error,
                ) from cleanup_error


def run_prepared_block_strategy_on_cdp(
    ws_url,
    target_url,
    strategy,
    elements,
    patterns,
    text_resolver,
    *,
    gate_check=None,
    on_action_dispatch=None,
) -> dict:
    """Prepare and execute through one user-owned AdsPower CDP connection."""

    return asyncio.run(
        _run_prepared_block_strategy_on_cdp(
            ws_url,
            target_url,
            strategy,
            elements,
            patterns,
            text_resolver,
            gate_check=gate_check,
            on_action_dispatch=on_action_dispatch,
        )
    )


__all__ = [
    "BlockExecutionError",
    "StrategyPausedError",
    "StrategyRuntimeError",
    "build_batches",
    "run_block_strategy",
    "run_block_strategy_on_cdp",
    "run_prepared_block_strategy_on_cdp",
]
