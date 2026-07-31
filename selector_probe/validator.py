"""Fail-closed validation of immutable selector bundles."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from browser_element_resolver import (
    inspect_visible_element,
    resolve_visible_element,
)
from browser_element_schema import normalize_element_definitions
from browser_public_identity import mask_profile_id

from .contracts import ElementContract, SAFE_PROBE_ACTIONS, VALID_STATES
from .repair import _parse_css, _parse_xpath
from .snapshot import STABLE_ATTRIBUTES, extract_semantic_snapshot


_sleep = asyncio.sleep

_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MASK = re.compile(r"^\*\*\*(?:.{4})?$", re.DOTALL)
_ATTR = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
_SIMPLE_CSS = re.compile(
    r"^(?:[a-z][a-z0-9-]*)?\[(?P<name>[a-z][a-z0-9:_-]*)="
    r"(?P<q>[\"'])(?P<value>[^\"'\\\]\r\n]+)(?P=q)\]$",
    re.IGNORECASE,
)
_SIMPLE_XPATH = re.compile(
    r"^\.//(?:\*|[a-z][a-z0-9-]*)\[@(?P<name>[a-z][a-z0-9:_-]*)="
    r"(?P<q>[\"'])(?P<value>[^\"'\\\]\r\n]+)(?P=q)\]$",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(
    r"(?:auth|bearer|cookie|credential|csrf|jwt|password|secret|session|token)",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"\d{12,}")
_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_HANDLE = re.compile(r"(?:https?://[^\s]+)?/@[A-Za-z0-9._-]+")
_FORBIDDEN_ACTIONS = {
    "account_update",
    "follow",
    "input",
    "keyboard_input",
    "like",
    "publish",
    "submit",
    "type",
}
_RESULT_FIELDS = {
    "status",
    "bundle_hash",
    "aliases",
    "actions",
}
_DOM_EVIDENCE_SCRIPT = r"""
element => {
    const labelledBy = String(element.getAttribute("aria-labelledby") || "")
        .split(/\s+/)
        .filter(Boolean)
        .map(id => document.getElementById(id))
        .filter(Boolean)
        .map(node => String(node.innerText || node.textContent || "").trim())
        .filter(Boolean)
        .join(" ");
    const role = String(element.getAttribute("role") || "").trim().toLowerCase();
    const name = String(
        element.getAttribute("aria-label")
        || labelledBy
        || element.getAttribute("placeholder")
        || element.getAttribute("title")
        || ""
    );
    const names = [
        "data-e2e", "data-testid", "aria-label", "aria-labelledby",
        "name", "id", "placeholder", "role", "contenteditable", "type"
    ];
    const attributes = {};
    for (const attributeName of names) {
        const value = element.getAttribute(attributeName);
        if (value !== null) attributes[attributeName] = value;
    }
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    const hit = rect.width > 0 && rect.height > 0
        ? document.elementFromPoint(
            rect.left + rect.width / 2,
            rect.top + rect.height / 2
        )
        : null;
    const actionable = Boolean(
        element.isConnected
        && rect.width > 0
        && rect.height > 0
        && style.display !== "none"
        && style.visibility === "visible"
        && style.pointerEvents !== "none"
        && Number.parseFloat(style.opacity || "1") !== 0
        && !element.disabled
        && element.getAttribute("aria-disabled") !== "true"
        && !element.closest("[inert]")
        && (!hit || hit === element || element.contains(hit))
    );
    return {role, name, attributes, actionable};
}
"""
_SAME_NODE_SCRIPT = "(element, firstNode) => element === firstNode"


class ValidationRejected(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        profile_mask: str = "",
        round_number: int | None = None,
        alias: str = "",
        match_count: int = 0,
        required_state: str = "",
        failures: Sequence[Mapping[str, object]] = (),
    ):
        self.code = code if _SAFE_CODE.fullmatch(code) else "validation_rejected"
        self.profile_mask = profile_mask if _is_mask(profile_mask) else ""
        self.round_number = round_number
        self.alias = alias.strip() if isinstance(alias, str) else ""
        self.match_count = (
            match_count
            if isinstance(match_count, int)
            and not isinstance(match_count, bool)
            and match_count >= 0
            else 0
        )
        self.required_state = (
            required_state
            if required_state in VALID_STATES
            else ""
        )
        self.failures = tuple(
            {
                "alias": str(item.get("alias") or ""),
                "code": str(item.get("code") or "validation_rejected"),
                "match_count": int(item.get("match_count") or 0),
                "required_state": str(item.get("required_state") or ""),
            }
            for item in failures
            if isinstance(item, Mapping)
        )
        suffix = f": {self.profile_mask}" if self.profile_mask else ""
        if round_number in {1, 2}:
            suffix += f" round {round_number}"
        super().__init__(self.code + suffix)


@dataclass(frozen=True)
class ValidationEvidence:
    bundle_hash: str
    profiles_passed: int
    rounds_passed: int
    validations: tuple[dict[str, object], ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "status": "passed",
            "bundle_hash": self.bundle_hash,
            "profiles_passed": self.profiles_passed,
            "rounds_passed": self.rounds_passed,
            "validations": [copy.deepcopy(item) for item in self.validations],
        }


@dataclass(frozen=True)
class ResetCapture:
    """Trusted reset result; the validator hashes ``snapshot.model_payload()``."""

    snapshot: object
    page_generation: str


InspectFn = Callable[
    [Any, int, dict[str, object], str, dict[str, object]],
    Awaitable[dict[str, object]],
]
AXInspector = Callable[[object, object], Awaitable[Mapping[str, object]]]
ResetFn = Callable[[object, int, str], Awaitable[ResetCapture]]
SnapshotExtractor = Callable[[object], Awaitable[object]]
ReadyFn = Callable[[object], Awaitable[object]]


def _is_mask(value: object) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and _MASK.fullmatch(value) is not None
        and mask_profile_id(value) == value
    )


def _resource_check(
    value: object,
    *,
    code: str,
    max_nodes: int,
    max_containers: int,
    max_depth: int,
    max_string_bytes: int,
) -> None:
    stack: list[tuple[object, int]] = [(value, 0)]
    seen: set[int] = set()
    nodes = containers = string_bytes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValidationRejected(code)
        if isinstance(item, str):
            string_bytes += len(item.encode("utf-8"))
            if string_bytes > max_string_bytes:
                raise ValidationRejected(code)
            continue
        if item is None or type(item) in {bool, int}:
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValidationRejected(code)
            continue
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in seen:
                raise ValidationRejected(code)
            seen.add(identity)
            containers += 1
            if containers > max_containers:
                raise ValidationRejected(code)
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValidationRejected(code)
                stack.append((key, depth + 1))
                stack.append((child, depth + 1))
            continue
        if isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray),
        ):
            identity = id(item)
            if identity in seen:
                raise ValidationRejected(code)
            seen.add(identity)
            containers += 1
            if containers > max_containers:
                raise ValidationRejected(code)
            for child in item:
                stack.append((child, depth + 1))
            continue
        raise ValidationRejected(code)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _full_fingerprint(value: object) -> str:
    _resource_check(
        value,
        code="bundle_resource_limit",
        max_nodes=4_096,
        max_containers=1_024,
        max_depth=12,
        max_string_bytes=262_144,
    )
    return _sha256(value)


def _normalize_bundle(value: object) -> dict[str, object]:
    _resource_check(
        value,
        code="bundle_resource_limit",
        max_nodes=4_096,
        max_containers=1_024,
        max_depth=12,
        max_string_bytes=262_144,
    )
    if not isinstance(value, Mapping) or set(value) != {
        "bundle_hash",
        "elements",
    }:
        raise ValidationRejected("bundle_invalid")
    raw_elements = value.get("elements")
    if not isinstance(raw_elements, dict) or not raw_elements:
        raise ValidationRejected("bundle_invalid")
    try:
        elements = normalize_element_definitions(raw_elements)
    except (TypeError, ValueError):
        raise ValidationRejected("bundle_invalid") from None
    if elements != raw_elements:
        raise ValidationRejected("bundle_invalid")
    expected_hash = _sha256(elements)
    if value.get("bundle_hash") != expected_hash:
        raise ValidationRejected("bundle_hash_invalid")
    return {"bundle_hash": expected_hash, "elements": elements}


def _contracts(
    value: object,
    elements: Mapping[str, object],
) -> dict[str, ElementContract]:
    if not isinstance(value, Mapping) or set(value) != set(elements):
        raise ValidationRejected("contracts_invalid")
    result: dict[str, ElementContract] = {}
    for alias, definition in elements.items():
        item = value.get(alias)
        if (
            not isinstance(item, ElementContract)
            or item.alias != alias
            or item.scope != definition.get("scope")
        ):
            raise ValidationRejected("contracts_invalid")
        if item.probe_action not in SAFE_PROBE_ACTIONS:
            raise ValidationRejected("contract_action_forbidden")
        if item.probe_action != "inspect_only" and not item.postcondition:
            raise ValidationRejected("contracts_invalid")
        result[alias] = item
    return result


def _literal_safe(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 240
        and value == value.strip()
        and not _LONG_DIGITS.search(value)
        and not _UUID.search(value)
        and not _HANDLE.search(value)
        and not _SENSITIVE.search(value)
    )


def _name_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _name_matches(contract: ElementContract, actual: object) -> bool:
    actual_key = _name_key(actual)
    if not actual_key:
        return False
    for expected in contract.accepted_names:
        expected_key = _name_key(expected)
        if contract.name_mode in {"exact", "locale_map"}:
            if actual_key == expected_key:
                return True
        elif expected_key.isascii():
            if re.search(
                rf"(?<![a-z0-9_]){re.escape(expected_key)}(?![a-z0-9_])",
                actual_key,
            ):
                return True
        elif expected_key in actual_key:
            return True
    return False


def _selector_safe(candidate: Mapping[str, object], contract: ElementContract) -> None:
    kind = candidate.get("type")
    if kind == "attribute":
        name = candidate.get("name")
        value = candidate.get("value")
        if (
            not isinstance(name, str)
            or name not in STABLE_ATTRIBUTES
            or not _ATTR.fullmatch(name)
            or not _literal_safe(value)
        ):
            raise ValidationRejected("selector_unsafe")
        descendant = candidate.get("descendant")
        if descendant is not None:
            if (
                not isinstance(descendant, Mapping)
                or descendant.get("type") != "attribute"
                or descendant.get("name") not in STABLE_ATTRIBUTES
                or not _literal_safe(descendant.get("value"))
                or descendant.get("role") not in contract.accepted_roles
            ):
                raise ValidationRejected("selector_unsafe")
        return
    if kind == "role":
        if (
            candidate.get("role") not in contract.accepted_roles
            or not _name_matches(contract, candidate.get("name"))
        ):
            raise ValidationRejected("selector_unsafe")
        return
    if kind not in {"css", "xpath"}:
        raise ValidationRejected("selector_unsafe")
    value = candidate.get("value")
    try:
        if kind == "css":
            _parse_css(
                value,
                accepted_roles=contract.accepted_roles,
                name_mode=contract.name_mode,
                accepted_names=contract.accepted_names,
            )
        else:
            _parse_xpath(
                value,
                accepted_roles=contract.accepted_roles,
                name_mode=contract.name_mode,
                accepted_names=contract.accepted_names,
            )
        return
    except ValueError:
        pattern = _SIMPLE_CSS if kind == "css" else _SIMPLE_XPATH
        match = pattern.fullmatch(value) if isinstance(value, str) else None
        if (
            match is None
            or match.group("name").casefold() not in STABLE_ATTRIBUTES
            or not _literal_safe(match.group("value"))
        ):
            raise ValidationRejected("selector_unsafe") from None


def _candidate_ids(
    elements: Mapping[str, dict],
    contracts: Mapping[str, ElementContract],
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for alias, definition in elements.items():
        enabled = [
            item
            for item in definition["locators"]
            if item.get("enabled") is True
        ]
        if not enabled or len(enabled) > 5:
            raise ValidationRejected("bundle_invalid")
        ids: set[str] = set()
        for candidate in enabled:
            candidate_id = candidate.get("id")
            if (
                not isinstance(candidate_id, str)
                or not _SAFE_ID.fullmatch(candidate_id)
            ):
                raise ValidationRejected("bundle_invalid")
            _selector_safe(candidate, contracts[alias])
            ids.add(candidate_id)
        result[alias] = ids
    return result


def _handle_identity(handle: object) -> tuple[str, tuple[str, ...]]:
    if isinstance(handle, str):
        if not _is_mask(handle):
            raise ValidationRejected("profile_handle_unmasked")
        return handle, ()
    profile = getattr(handle, "profile", None)
    source = profile if profile is not None else handle
    profile_mask = getattr(source, "profile_mask", "")
    if not _is_mask(profile_mask):
        raise ValidationRejected("profile_handle_unmasked")
    secrets_found: list[str] = []
    raw_id = getattr(source, "profile_id", "")
    if raw_id:
        if (
            not isinstance(raw_id, str)
            or mask_profile_id(raw_id) != profile_mask
        ):
            raise ValidationRejected("profile_handle_unmasked")
        secrets_found.append(raw_id)
    for field in ("ws_url", "cdp_url"):
        item = getattr(source, field, "")
        if isinstance(item, str) and item:
            secrets_found.append(item)
    return profile_mask, tuple(secrets_found)


def _scan_evidence(value: object, secrets_found: Sequence[str]) -> None:
    stack: list[tuple[object, bool]] = [(value, False)]
    sensitive_keys = (
        "authorization",
        "cookie",
        "credential",
        "password",
        "profile_id",
        "secret",
        "token",
        "ws_url",
        "cdp_url",
    )
    while stack:
        item, action_context = stack.pop()
        if isinstance(item, Mapping):
            for key, child in item.items():
                normalized = key.casefold()
                if (
                    key.casefold().startswith(("ws://", "wss://"))
                    or any(
                        secret and secret in key
                        for secret in secrets_found
                    )
                ):
                    raise ValidationRejected("evidence_sensitive")
                if normalized != "challenge" and any(
                    marker in normalized for marker in sensitive_keys
                ):
                    raise ValidationRejected("evidence_sensitive")
                stack.append(
                    (
                        child,
                        action_context
                        or normalized in {"action", "actions", "action_evidence"},
                    )
                )
        elif isinstance(item, Sequence) and not isinstance(
            item,
            (str, bytes, bytearray),
        ):
            for child in item:
                stack.append((child, action_context))
        elif isinstance(item, str):
            if (
                item.casefold().startswith(("ws://", "wss://"))
                or any(secret and secret in item for secret in secrets_found)
            ):
                raise ValidationRejected("evidence_sensitive")
            if action_context and item.casefold() in _FORBIDDEN_ACTIONS:
                raise ValidationRejected("forbidden_action_evidence")


def _alias_evidence(
    raw: object,
    *,
    candidate_ids: Mapping[str, set[str]],
    baseline: dict[str, str],
) -> dict[str, dict[str, str]]:
    if not isinstance(raw, Mapping) or set(raw) != set(candidate_ids):
        raise ValidationRejected("candidate_evidence_invalid")
    result: dict[str, dict[str, str]] = {}
    for alias in sorted(candidate_ids):
        item = raw.get(alias)
        if (
            not isinstance(item, Mapping)
            or set(item) != {"status", "candidate_id"}
            or item.get("status") != "ok"
        ):
            raise ValidationRejected("candidate_evidence_invalid")
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or not _SAFE_ID.fullmatch(candidate_id):
            raise ValidationRejected("candidate_evidence_invalid")
        if alias in baseline and baseline[alias] != candidate_id:
            raise ValidationRejected("candidate_changed")
        if candidate_id not in candidate_ids[alias]:
            raise ValidationRejected("candidate_evidence_invalid")
        baseline.setdefault(alias, candidate_id)
        result[alias] = {"status": "ok", "candidate_id": candidate_id}
    return result


def _validate_call_evidence(
    raw: object,
    *,
    bundle_hash: str,
    all_secrets: Sequence[str],
    candidate_ids: Mapping[str, set[str]],
    candidate_baseline: dict[str, str],
    expected_actions: list[str],
) -> dict[str, dict[str, str]]:
    _resource_check(
        raw,
        code="evidence_resource_limit",
        max_nodes=512,
        max_containers=128,
        max_depth=8,
        max_string_bytes=16_384,
    )
    _scan_evidence(raw, all_secrets)
    if not isinstance(raw, Mapping) or set(raw) != _RESULT_FIELDS:
        raise ValidationRejected("evidence_schema_invalid")
    if raw.get("status") != "passed":
        raise ValidationRejected("profile_validation_failed")
    if raw.get("bundle_hash") != bundle_hash:
        raise ValidationRejected("bundle_hash_changed")
    if raw.get("actions") != expected_actions:
        raise ValidationRejected("action_evidence_invalid")
    return _alias_evidence(
        raw.get("aliases"),
        candidate_ids=candidate_ids,
        baseline=candidate_baseline,
    )


def _page_for_handle(handle: object) -> object | None:
    page = getattr(handle, "page", None)
    if page is not None:
        return page
    if all(callable(getattr(handle, name, None)) for name in ("reload", "evaluate")):
        return handle
    return None


def _snapshot_payload(snapshot: object) -> tuple[dict[str, object], str]:
    model_payload = getattr(snapshot, "model_payload", None)
    if not callable(model_payload):
        raise ValidationRejected("snapshot_evidence_invalid")
    try:
        payload = model_payload()
    except Exception:
        raise ValidationRejected("snapshot_evidence_invalid") from None
    _resource_check(
        payload,
        code="snapshot_evidence_invalid",
        max_nodes=4_096,
        max_containers=1_024,
        max_depth=12,
        max_string_bytes=262_144,
    )
    if not isinstance(payload, dict):
        raise ValidationRejected("snapshot_evidence_invalid")
    return payload, _sha256(payload)


def _capture_value(value: object) -> ResetCapture:
    if not isinstance(value, ResetCapture):
        raise ValidationRejected("reset_capture_invalid")
    generation = value.page_generation
    if (
        not isinstance(generation, str)
        or not _HASH.fullmatch(generation)
    ):
        raise ValidationRejected("snapshot_evidence_invalid")
    return value


async def _default_reset_capture(
    page: object,
    *,
    snapshot_extractor: SnapshotExtractor,
    ready_fn: ReadyFn | None,
) -> ResetCapture:
    reload_page = getattr(page, "reload", None)
    wait_for_load_state = getattr(page, "wait_for_load_state", None)
    evaluate = getattr(page, "evaluate", None)
    if not all(
        callable(item)
        for item in (reload_page, wait_for_load_state, evaluate)
    ):
        raise ValidationRejected("profile_page_missing")
    if ready_fn is None:
        raise ValidationRejected("ready_check_missing")
    try:
        await reload_page(wait_until="domcontentloaded")
        await wait_for_load_state("domcontentloaded")
        ready = await ready_fn(page)
        ready_ok = (
            ready is True
            or (
                isinstance(ready, Mapping)
                and (
                    ready.get("ready") is True
                    or ready.get("state") == "feed_ready"
                )
            )
        )
        if not ready_ok:
            raise ValidationRejected("page_not_ready")
        snapshot = await snapshot_extractor(page)
        generation_payload = await evaluate(
            "() => ({time_origin: performance.timeOrigin, url: location.href})"
        )
    except asyncio.CancelledError:
        raise
    except ValidationRejected:
        raise
    except Exception:
        raise ValidationRejected("reset_capture_failed") from None
    if (
        not isinstance(generation_payload, Mapping)
        or set(generation_payload) != {"time_origin", "url"}
        or not isinstance(generation_payload.get("time_origin"), (int, float))
        or isinstance(generation_payload.get("time_origin"), bool)
        or not math.isfinite(float(generation_payload["time_origin"]))
        or float(generation_payload["time_origin"]) <= 0
        or not isinstance(generation_payload.get("url"), str)
        or not generation_payload["url"]
    ):
        raise ValidationRejected("snapshot_evidence_invalid")
    capture = ResetCapture(
        snapshot=snapshot,
        page_generation=_sha256(dict(generation_payload)),
    )
    return _capture_value(capture)


async def _owned_reset_capture(
    handle: object,
    *,
    round_number: int,
    profile_mask: str,
    reset_fn: ResetFn | None,
    snapshot_extractor: SnapshotExtractor,
    ready_fn: ReadyFn | None,
) -> ResetCapture:
    page = _page_for_handle(handle)
    if reset_fn is None:
        if page is None:
            raise ValidationRejected("profile_page_missing")
        return await _default_reset_capture(
            page,
            snapshot_extractor=snapshot_extractor,
            ready_fn=ready_fn,
        )
    target = page if page is not None else handle
    try:
        raw = await reset_fn(target, round_number, profile_mask)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValidationRejected("reset_capture_failed") from None
    return _capture_value(raw)


async def validate_two_rounds(
    handles: object,
    bundle: object,
    contracts: object,
    inspect_fn: InspectFn,
    *,
    reset_fn: ResetFn | None = None,
    snapshot_extractor: SnapshotExtractor = extract_semantic_snapshot,
    ready_fn: ReadyFn | None = None,
) -> dict[str, object]:
    if not isinstance(handles, Sequence) or isinstance(
        handles,
        (str, bytes, bytearray),
    ):
        raise ValidationRejected("profiles_invalid")
    if len(handles) < 2:
        raise ValidationRejected("profiles_insufficient")
    if len(handles) > 8:
        raise ValidationRejected("profiles_too_many")
    identities = [_handle_identity(item) for item in handles]
    masks = [item[0] for item in identities]
    if len(set(masks)) != len(masks):
        raise ValidationRejected("profiles_duplicate")
    all_secrets = tuple(
        secret
        for _mask, raw_secrets in identities
        for secret in raw_secrets
    )

    source_fingerprint = _full_fingerprint(bundle)
    canonical_bundle = _normalize_bundle(bundle)
    elements = canonical_bundle["elements"]
    assert isinstance(elements, dict)
    normalized_contracts = _contracts(contracts, elements)
    enabled_ids = _candidate_ids(elements, normalized_contracts)
    expected_actions = [
        item.probe_action
        for item in normalized_contracts.values()
        if item.probe_action != "inspect_only"
    ]

    generations: dict[str, set[str]] = {}
    snapshot_objects: dict[str, list[object]] = {}
    candidate_baseline: dict[str, str] = {}
    validations: list[dict[str, object]] = []
    for round_number in (1, 2):
        for index, handle in enumerate(handles):
            profile_mask = identities[index][0]
            if _full_fingerprint(bundle) != source_fingerprint:
                raise ValidationRejected(
                    "bundle_mutated",
                    profile_mask=profile_mask,
                    round_number=round_number,
                )
            call_bundle = copy.deepcopy(canonical_bundle)
            call_fingerprint = _full_fingerprint(call_bundle)
            challenge = secrets.token_urlsafe(24)
            try:
                capture = await _owned_reset_capture(
                    handle,
                    round_number=round_number,
                    profile_mask=profile_mask,
                    reset_fn=reset_fn,
                    snapshot_extractor=snapshot_extractor,
                    ready_fn=ready_fn,
                )
                _snapshot_payload_value, snapshot_hash = _snapshot_payload(
                    capture.snapshot
                )
            except asyncio.CancelledError:
                raise
            except ValidationRejected as error:
                raise ValidationRejected(
                    error.code,
                    profile_mask=profile_mask,
                    round_number=round_number,
                    alias=error.alias,
                    match_count=error.match_count,
                    required_state=error.required_state,
                    failures=error.failures,
                ) from None
            generation_seen = generations.setdefault(profile_mask, set())
            object_seen = snapshot_objects.setdefault(profile_mask, [])
            if capture.page_generation in generation_seen:
                raise ValidationRejected(
                    "page_generation_not_fresh",
                    profile_mask=profile_mask,
                    round_number=round_number,
                )
            if any(capture.snapshot is item for item in object_seen):
                raise ValidationRejected(
                    "snapshot_not_fresh",
                    profile_mask=profile_mask,
                    round_number=round_number,
                )
            generation_seen.add(capture.page_generation)
            object_seen.append(capture.snapshot)
            reset_evidence = {
                "challenge": challenge,
                "profile_mask": profile_mask,
                "round_number": round_number,
                "reloaded": True,
                "feed_ready": True,
                "snapshot_hash": snapshot_hash,
                "page_generation": capture.page_generation,
            }
            reset_digest = _sha256(reset_evidence)
            inspect_reset = copy.deepcopy(reset_evidence)
            inspect_reset_fingerprint = _sha256(inspect_reset)
            try:
                raw = await inspect_fn(
                    handle,
                    round_number,
                    call_bundle,
                    challenge,
                    inspect_reset,
                )
            except asyncio.CancelledError:
                raise
            except ValidationRejected as error:
                raise ValidationRejected(
                    error.code,
                    profile_mask=profile_mask,
                    round_number=round_number,
                    alias=error.alias,
                    match_count=error.match_count,
                    required_state=error.required_state,
                    failures=error.failures,
                ) from None
            except Exception:
                raise ValidationRejected(
                    "inspection_failed",
                    profile_mask=profile_mask,
                    round_number=round_number,
                ) from None
            if _sha256(inspect_reset) != inspect_reset_fingerprint:
                raise ValidationRejected(
                    "reset_evidence_mutated",
                    profile_mask=profile_mask,
                    round_number=round_number,
                )
            if (
                _full_fingerprint(call_bundle) != call_fingerprint
                or _full_fingerprint(bundle) != source_fingerprint
            ):
                raise ValidationRejected(
                    "bundle_mutated",
                    profile_mask=profile_mask,
                    round_number=round_number,
                )
            try:
                aliases = _validate_call_evidence(
                    raw,
                    bundle_hash=str(canonical_bundle["bundle_hash"]),
                    all_secrets=all_secrets,
                    candidate_ids=enabled_ids,
                    candidate_baseline=candidate_baseline,
                    expected_actions=expected_actions,
                )
            except ValidationRejected as error:
                raise ValidationRejected(
                    error.code,
                    profile_mask=profile_mask,
                    round_number=round_number,
                    alias=error.alias,
                    match_count=error.match_count,
                    required_state=error.required_state,
                    failures=error.failures,
                ) from None
            validations.append(
                {
                    "profile_mask": profile_mask,
                    "round_number": round_number,
                    "reset_evidence_hash": reset_digest,
                    "snapshot_hash": snapshot_hash,
                    "page_generation": capture.page_generation,
                    "aliases": aliases,
                }
            )

    evidence = ValidationEvidence(
        bundle_hash=str(canonical_bundle["bundle_hash"]),
        profiles_passed=len(masks),
        rounds_passed=2,
        validations=tuple(validations),
    ).public_dict()
    _resource_check(
        evidence,
        code="evidence_resource_limit",
        max_nodes=2_048,
        max_containers=512,
        max_depth=8,
        max_string_bytes=65_536,
    )
    if len(_canonical_json(evidence).encode("utf-8")) > 65_536:
        raise ValidationRejected("evidence_resource_limit")
    return evidence


def _normalize_ax(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != {"role", "name"}:
        raise ValidationRejected("semantic_evidence_invalid")
    role = _name_key(raw.get("role"))
    name = _name_key(raw.get("name"))
    return {"role": role, "name": name}


def _parse_aria_snapshot(raw: object) -> dict[str, str] | None:
    if isinstance(raw, Mapping):
        return _normalize_ax(raw)
    if not isinstance(raw, str):
        return None
    first = next(
        (line.strip() for line in raw.splitlines() if line.strip()),
        "",
    )
    match = re.match(
        r"^-?\s*(?P<role>[a-z][a-z0-9_-]*)"
        r"(?:\s+\"(?P<name>(?:[^\"\\]|\\.)*)\")?",
        first,
        re.IGNORECASE,
    )
    if not match:
        return None
    encoded = '"' + (match.group("name") or "") + '"'
    try:
        name = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    return _normalize_ax({"role": match.group("role"), "name": name})


async def _dom_evidence(locator: object) -> dict[str, object]:
    evaluate = getattr(locator, "evaluate", None)
    if not callable(evaluate):
        raise ValidationRejected("semantic_evidence_invalid")
    try:
        raw = await evaluate(_DOM_EVIDENCE_SCRIPT)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValidationRejected("semantic_evidence_invalid") from None
    if not isinstance(raw, Mapping) or set(raw) != {
        "role",
        "name",
        "attributes",
        "actionable",
    }:
        raise ValidationRejected("semantic_evidence_invalid")
    attributes = raw.get("attributes")
    if not isinstance(attributes, Mapping):
        raise ValidationRejected("semantic_evidence_invalid")
    clean_attributes: dict[str, str] = {}
    for name, value in attributes.items():
        if (
            not isinstance(name, str)
            or name not in STABLE_ATTRIBUTES
            or not isinstance(value, str)
            or len(value) > 512
        ):
            raise ValidationRejected("semantic_evidence_invalid")
        clean_attributes[name] = value
    if type(raw.get("actionable")) is not bool:
        raise ValidationRejected("semantic_evidence_invalid")
    return {
        "role": _name_key(raw.get("role")),
        "name": _name_key(raw.get("name")),
        "attributes": clean_attributes,
        "actionable": raw["actionable"],
    }


async def _default_ax_inspector(
    _page: object,
    locator: object,
) -> Mapping[str, object]:
    aria_snapshot = getattr(locator, "aria_snapshot", None)
    if callable(aria_snapshot):
        try:
            parsed = _parse_aria_snapshot(await aria_snapshot())
        except asyncio.CancelledError:
            raise
        except Exception:
            parsed = None
        if parsed is not None:
            return parsed
    dom = await _dom_evidence(locator)
    # Fail closed: fallback trusts explicit role/ARIA only; it never invents
    # an implicit role from tag names.
    return {"role": dom["role"], "name": dom["name"]}


async def _semantics(
    page: object,
    locator: object,
    ax_inspector: AXInspector,
) -> dict[str, object]:
    dom = await _dom_evidence(locator)
    try:
        ax = _normalize_ax(await ax_inspector(page, locator))
    except asyncio.CancelledError:
        raise
    except ValidationRejected:
        raise
    except Exception:
        raise ValidationRejected("semantic_evidence_invalid") from None
    return {**dom, **ax}


async def _actionable(locator: object) -> bool:
    try:
        if await locator.count() != 1:
            return False
        if not await locator.is_visible() or not await locator.is_enabled():
            return False
        return (await _dom_evidence(locator))["actionable"] is True
    except asyncio.CancelledError:
        raise
    except ValidationRejected:
        raise
    except Exception:
        return False


async def _visible_stable(locator: object) -> bool:
    try:
        return await locator.count() == 1 and await locator.is_visible()
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


def _verify_semantics(
    contract: ElementContract,
    candidate: Mapping[str, object],
    semantics: Mapping[str, object],
) -> None:
    if semantics.get("role") not in contract.accepted_roles:
        raise ValidationRejected("semantic_role_mismatch")
    semantic_name = semantics.get("name")
    if semantic_name and not _name_matches(contract, semantic_name):
        raise ValidationRejected("semantic_name_mismatch")
    if not semantic_name and candidate.get("type") != "attribute":
        raise ValidationRejected("semantic_name_mismatch")
    attributes = semantics.get("attributes")
    assert isinstance(attributes, dict)
    if candidate.get("type") == "attribute":
        descendant = candidate.get("descendant")
        if isinstance(descendant, Mapping):
            if (
                attributes.get(descendant.get("name"))
                != descendant.get("value")
                or semantics.get("role") != descendant.get("role")
            ):
                raise ValidationRejected("semantic_attribute_mismatch")
        elif attributes.get(candidate.get("name")) != candidate.get("value"):
            raise ValidationRejected("semantic_attribute_mismatch")


def _resolved_candidate(
    resolved: object,
    *,
    contract: ElementContract,
    candidate: Mapping[str, object],
) -> object:
    actual = getattr(resolved, "candidate", None)
    if (
        getattr(resolved, "scope", None) != contract.scope
        or not isinstance(actual, Mapping)
        or actual.get("id") != candidate.get("id")
        or actual.get("type") != candidate.get("type")
    ):
        raise ValidationRejected("candidate_changed")
    locator = getattr(resolved, "locator", None)
    if locator is None:
        raise ValidationRejected("element_resolution_failed")
    return locator


async def _resolve_one(
    page: object,
    alias: str,
    scope: str,
    candidate: dict,
    contract: ElementContract,
) -> object:
    try:
        resolved = await resolve_visible_element(
            page,
            alias,
            {"scope": scope, "locators": [candidate]},
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValidationRejected("element_resolution_failed") from None
    return _resolved_candidate(
        resolved,
        contract=contract,
        candidate=candidate,
    )


async def _dispose(handle: object) -> None:
    dispose = getattr(handle, "dispose", None)
    if callable(dispose):
        try:
            await dispose()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass


async def _validate_candidate(
    page: object,
    alias: str,
    definition: dict,
    candidate: dict,
    contract: ElementContract,
    ax_inspector: AXInspector,
) -> bool:
    first = await _resolve_one(
        page,
        alias,
        definition["scope"],
        candidate,
        contract,
    )
    if not await _visible_stable(first):
        raise ValidationRejected("element_not_actionable")
    first_actionable = await _actionable(first)
    if contract.probe_action != "inspect_only" and not first_actionable:
        raise ValidationRejected("element_not_actionable")
    before = await _semantics(page, first, ax_inspector)
    _verify_semantics(contract, candidate, before)
    element_handle = getattr(first, "element_handle", None)
    if not callable(element_handle):
        raise ValidationRejected("element_identity_unavailable")
    try:
        first_handle = await element_handle()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValidationRejected("element_identity_unavailable") from None
    if first_handle is None:
        raise ValidationRejected("element_identity_unavailable")
    try:
        await _sleep(0.25)
        second = await _resolve_one(
            page,
            alias,
            definition["scope"],
            candidate,
            contract,
        )
        if not await _visible_stable(second):
            raise ValidationRejected("element_unstable")
        second_actionable = await _actionable(second)
        if contract.probe_action != "inspect_only" and not second_actionable:
            raise ValidationRejected("element_unstable")
        after = await _semantics(page, second, ax_inspector)
        _verify_semantics(contract, candidate, after)
        evaluate = getattr(second, "evaluate", None)
        if not callable(evaluate):
            raise ValidationRejected("element_identity_unavailable")
        try:
            same_node = await evaluate(_SAME_NODE_SCRIPT, first_handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            same_node = False
        if same_node is not True:
            raise ValidationRejected("element_identity_changed")
        if before != after:
            raise ValidationRejected("element_unstable")
        return first_actionable and second_actionable
    finally:
        await _dispose(first_handle)


async def _ensure_state(
    runner: object,
    page: object,
    state: str,
    elements: dict,
    code: str,
) -> None:
    try:
        result = await runner.ensure_state(page, state, elements)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValidationRejected(code) from None
    if not isinstance(result, Mapping) or result.get("state") != state:
        raise ValidationRejected(code)


def _inspection_failure(value: object) -> tuple[str, int]:
    if not isinstance(value, Mapping):
        return "element_inspection_failed", 0
    code = value.get("code")
    diagnostics = value.get("diagnostics")
    candidates = (
        diagnostics.get("candidates", ())
        if isinstance(diagnostics, Mapping)
        else ()
    )
    counts = [
        item.get("actionable_count", 0)
        for item in candidates
        if isinstance(item, Mapping)
        and isinstance(item.get("actionable_count", 0), int)
        and not isinstance(item.get("actionable_count", 0), bool)
    ]
    if code == "element_candidate_ambiguous":
        return "multiple_match", max(counts, default=2)
    if code == "element_candidate_not_found":
        return "zero_match", 0
    return "element_inspection_failed", max(counts, default=0)


async def validate_bundle_on_page(
    page: object,
    bundle: object,
    contracts: object,
    state_runner: object,
    *,
    ax_inspector: AXInspector | None = None,
) -> dict[str, object]:
    source_fingerprint = _full_fingerprint(bundle)
    canonical = _normalize_bundle(bundle)
    elements = canonical["elements"]
    assert isinstance(elements, dict)
    normalized_contracts = _contracts(contracts, elements)
    _candidate_ids(elements, normalized_contracts)
    inspect_ax = ax_inspector or _default_ax_inspector

    alias_evidence: dict[str, dict[str, object]] = {}
    actions: list[str] = []
    alias_failures: list[dict[str, object]] = []
    for alias, definition in elements.items():
        contract = normalized_contracts[alias]
        try:
            await _ensure_state(
                state_runner,
                page,
                contract.required_state,
                elements,
                "required_state_failed",
            )
            if _full_fingerprint(bundle) != source_fingerprint:
                raise ValidationRejected("bundle_mutated")
            inspection = await inspect_visible_element(
                page,
                alias,
                definition,
            )
            if (
                not isinstance(inspection, Mapping)
                or inspection.get("status") != "ok"
                or inspection.get("scope") != contract.scope
            ):
                code, count = _inspection_failure(inspection)
                raise ValidationRejected(code, match_count=count)
            primary = inspection.get("candidate")
            if not isinstance(primary, Mapping):
                raise ValidationRejected("element_inspection_failed")
            primary_id = primary.get("id")
            primary_type = primary.get("type")
            enabled = [
                candidate
                for candidate in definition["locators"]
                if candidate.get("enabled") is True
            ]
            if not any(
                item.get("id") == primary_id and item.get("type") == primary_type
                for item in enabled
            ):
                raise ValidationRejected("candidate_changed")
            primary_actionable = False
            for candidate in enabled:
                candidate_actionable = await _validate_candidate(
                    page,
                    alias,
                    definition,
                    candidate,
                    contract,
                    inspect_ax,
                )
                if (
                    candidate.get("id") == primary_id
                    and candidate.get("type") == primary_type
                ):
                    primary_actionable = candidate_actionable

            postcondition = ""
            if contract.probe_action != "inspect_only":
                await _ensure_state(
                    state_runner,
                    page,
                    contract.postcondition,
                    elements,
                    "postcondition_failed",
                )
                actions.append(contract.probe_action)
                postcondition = contract.postcondition
        except asyncio.CancelledError:
            raise
        except ValidationRejected as error:
            alias_failures.append(
                {
                    "alias": error.alias or alias,
                    "code": error.code,
                    "match_count": error.match_count,
                    "required_state": (
                        error.required_state or contract.required_state
                    ),
                }
            )
            continue
        except Exception:
            alias_failures.append(
                {
                    "alias": alias,
                    "code": "element_inspection_failed",
                    "match_count": 0,
                    "required_state": contract.required_state,
                }
            )
            continue
        alias_evidence[alias] = {
            "status": "ok",
            "candidate_id": primary_id,
            "scope": contract.scope,
            "role_matched": True,
            "name_matched": True,
            "actionable": primary_actionable,
            "stable": True,
            "postcondition": postcondition,
        }
        if _full_fingerprint(bundle) != source_fingerprint:
            raise ValidationRejected("bundle_mutated")

    if alias_failures:
        first = alias_failures[0]
        raise ValidationRejected(
            (
                first["code"]
                if len(alias_failures) == 1
                else "selector_validation_failed"
            ),
            alias=str(first["alias"]),
            match_count=int(first["match_count"]),
            required_state=str(first["required_state"]),
            failures=alias_failures,
        )

    evidence = {
        "status": "passed",
        "bundle_hash": canonical["bundle_hash"],
        "aliases": alias_evidence,
        "actions": actions,
    }
    _resource_check(
        evidence,
        code="evidence_resource_limit",
        max_nodes=1_024,
        max_containers=256,
        max_depth=6,
        max_string_bytes=16_384,
    )
    if len(_canonical_json(evidence).encode("utf-8")) > 16_384:
        raise ValidationRejected("evidence_resource_limit")
    return evidence


__all__ = [
    "ResetCapture",
    "ValidationEvidence",
    "ValidationRejected",
    "validate_bundle_on_page",
    "validate_two_rounds",
]
