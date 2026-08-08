"""Public-safe normalization for manually selected interactive elements."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import math
import re
from urllib.parse import urlsplit


MAX_INVENTORY_ITEMS = 500
MAX_RAW_ITEMS = 1000
MAX_LOCATORS = 6
ALLOWED_LOCATOR_TYPES = frozenset({"css", "xpath"})
ALLOWED_ATTRIBUTES = (
    "data-e2e",
    "data-testid",
    "id",
    "name",
    "placeholder",
    "aria-label",
    "contenteditable",
    "type",
    "tabindex",
)
FORBIDDEN_XPATH_PREFIXES = ("/html", "//html")

_PUBLIC_ITEM_KEYS = (
    "selection_id",
    "fingerprint",
    "tag",
    "input_type",
    "text",
    "role",
    "name",
    "attributes",
    "frame_key",
    "shadow",
    "shadow_key",
    "region",
    "locators",
    "locatable",
    "match_counts",
    "visible",
    "enabled",
    "hit_target",
    "target_match",
)
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_LONG_HEX_RE = re.compile(r"[0-9a-f]{8,}", re.IGNORECASE)
_LONG_DECIMAL_RE = re.compile(r"\d{6,}")
_CSS_ID_RE = re.compile(r"#([a-zA-Z0-9_-]+)")
_ATTRIBUTE_ID_RE = re.compile(
    r"(?:\[\s*id\s*[*^$|~]?=|@id\s*=)\s*(['\"])(.*?)\1",
    re.IGNORECASE,
)
_CSS_TAG_RE = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9_-]*|\*)")
_CSS_ID_TOKEN_RE = re.compile(r"#[a-zA-Z_][a-zA-Z0-9_-]*")
_CSS_ATTRIBUTE_RE = re.compile(
    r"\s*([a-zA-Z][a-zA-Z0-9_-]*)\s*"
    r"(?:=\s*(?:'([^']*)'|\"([^\"]*)\"))?\s*"
)
_CSS_NTH_RE = re.compile(r":nth-of-type\((\d+)\)", re.IGNORECASE)
_XPATH_ANCHOR_RE = re.compile(
    r"//\*\[@([a-zA-Z][a-zA-Z0-9_-]*)"
    r"(?:\s*=\s*(['\"])([^'\"]*)\2)?\](.*)"
)
_XPATH_STEP_RE = re.compile(
    r"/([a-zA-Z][a-zA-Z0-9_-]*)(?:\[(\d+)\])?"
)
_XPATH_RELATIVE_STEP_RE = re.compile(
    r"(?:\./|/)([a-zA-Z][a-zA-Z0-9_-]*)(?:\[(\d+)\])?"
)
_PRIVATE_TEXT_TAGS = frozenset({"form", "input", "option", "select", "textarea"})
_SELECTION_ID_RE = re.compile(r"selection-[A-Za-z0-9._:-]{1,54}\Z")


def _clean_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:maximum]


def _is_dynamic_id(value: str) -> bool:
    return bool(
        _UUID_RE.search(value)
        or _LONG_HEX_RE.search(value)
        or _LONG_DECIMAL_RE.search(value)
    )


def _dynamic_id_locator(value: str, dynamic_id: str = "") -> bool:
    if dynamic_id and dynamic_id in value:
        return True
    for match in _CSS_ID_RE.finditer(value):
        if _is_dynamic_id(match.group(1)):
            return True
    return any(_is_dynamic_id(match.group(2)) for match in _ATTRIBUTE_ID_RE.finditer(value))


def _css_segments(value: str) -> list[str] | None:
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    bracket_depth = 0
    for character in value:
        if quote:
            current.append(character)
            if character == quote:
                quote = ""
            continue
        if character in {"'", '"'} and bracket_depth:
            quote = character
            current.append(character)
            continue
        if character == "[":
            bracket_depth += 1
            current.append(character)
            continue
        if character == "]":
            if bracket_depth == 0:
                return None
            bracket_depth -= 1
            current.append(character)
            continue
        if character == ">" and bracket_depth == 0:
            selected = "".join(current).strip()
            if not selected:
                return None
            segments.append(selected)
            current = []
            continue
        current.append(character)
    if quote or bracket_depth:
        return None
    selected = "".join(current).strip()
    if not selected:
        return None
    segments.append(selected)
    return segments


def _safe_css(value: str) -> bool:
    segments = _css_segments(value)
    if not segments or len(segments) > 4:
        return False
    nth_count = 0
    for segment in segments:
        position = 0
        token_count = 0
        tag = _CSS_TAG_RE.match(segment, position)
        if tag:
            position = tag.end()
            token_count += 1
        while position < len(segment):
            if segment[position] == "#":
                identifier = _CSS_ID_TOKEN_RE.match(segment, position)
                if identifier is None or _is_dynamic_id(identifier.group()[1:]):
                    return False
                position = identifier.end()
                token_count += 1
                continue
            if segment[position] == "[":
                end = segment.find("]", position + 1)
                if end < 0:
                    return False
                attribute = _CSS_ATTRIBUTE_RE.fullmatch(
                    segment[position + 1 : end]
                )
                if attribute is None:
                    return False
                name = attribute.group(1).casefold()
                selected_value = attribute.group(2) or attribute.group(3) or ""
                if name not in ALLOWED_ATTRIBUTES:
                    return False
                if name == "id" and _is_dynamic_id(selected_value):
                    return False
                position = end + 1
                token_count += 1
                continue
            nth = _CSS_NTH_RE.match(segment, position)
            if nth is None:
                return False
            index = int(nth.group(1))
            nth_count += 1
            if index < 1 or index > 100 or nth_count > 3:
                return False
            position = nth.end()
            token_count += 1
            if position != len(segment):
                return False
        if token_count == 0:
            return False
    return True


def _safe_xpath_steps(tail: str, *, maximum: int) -> bool:
    if not tail:
        return True
    position = 0
    count = 0
    while position < len(tail):
        step = _XPATH_STEP_RE.match(tail, position)
        if step is None:
            return False
        count += 1
        if count > maximum:
            return False
        index = int(step.group(2) or "1")
        if index < 1 or index > 100:
            return False
        position = step.end()
    return True


def _safe_relative_xpath(value: str) -> bool:
    if not value.startswith("./"):
        return False
    position = 0
    count = 0
    while position < len(value):
        step = _XPATH_RELATIVE_STEP_RE.match(value, position)
        if step is None:
            return False
        count += 1
        if count > 4:
            return False
        index = int(step.group(2) or "1")
        if index < 1 or index > 100:
            return False
        position = step.end()
    return count > 0


def _safe_xpath(value: str) -> bool:
    anchor = _XPATH_ANCHOR_RE.fullmatch(value)
    if anchor is not None:
        attribute = anchor.group(1).casefold()
        selected_value = anchor.group(3) or ""
        return (
            attribute in ALLOWED_ATTRIBUTES
            and not (attribute == "id" and _is_dynamic_id(selected_value))
            and _safe_xpath_steps(anchor.group(4), maximum=3)
        )
    return _safe_relative_xpath(value)


def _safe_locator_syntax(locator_type: str, value: str) -> bool:
    if "javascript:" in value.casefold() or len(value) > 500:
        return False
    if locator_type == "css":
        return _safe_css(value)
    return _safe_xpath(value)


def _locator(raw: object) -> dict[str, object] | None:
    if not isinstance(raw, Mapping):
        return None
    locator_type = str(raw.get("type") or "").strip().casefold()
    value = str(raw.get("value") or "").strip()
    match_count = raw.get("match_count")
    if locator_type not in ALLOWED_LOCATOR_TYPES or not value:
        return None
    if not _safe_locator_syntax(locator_type, value):
        return None
    if (
        isinstance(match_count, bool)
        or not isinstance(match_count, int)
        or match_count < 0
    ):
        return None
    return {
        "type": locator_type,
        "value": value,
        "match_count": match_count,
    }


def _recorded_locator(raw: object) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    locator_type = str(raw.get("type") or "").strip().casefold()
    value = str(raw.get("value") or "").strip()
    if locator_type not in ALLOWED_LOCATOR_TYPES or not value:
        return None
    if not _safe_locator_syntax(locator_type, value) or _dynamic_id_locator(value):
        return None
    return {"type": locator_type, "value": value}


def _number(value: object, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    selected = float(value)
    return selected if math.isfinite(selected) else default


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _region(raw: object) -> dict[str, float]:
    value = raw if isinstance(raw, Mapping) else {}
    raw_x = _number(value.get("x"))
    raw_y = _number(value.get("y"))
    raw_width = max(0.0, _number(value.get("width")))
    raw_height = max(0.0, _number(value.get("height")))
    left = _clamp(raw_x)
    top = _clamp(raw_y)
    right = max(left, _clamp(raw_x + raw_width))
    bottom = max(top, _clamp(raw_y + raw_height))
    return {
        "x": round(left, 6),
        "y": round(top, 6),
        "width": round(right - left, 6),
        "height": round(bottom - top, 6),
    }


def _attributes(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping):
        return {}
    normalized: dict[str, object] = {
        str(key).strip().casefold(): value for key, value in raw.items()
    }
    result: dict[str, str] = {}
    for name in ALLOWED_ATTRIBUTES:
        selected = _clean_text(normalized.get(name), 160)
        if selected:
            result[name] = selected
    return result


def _fingerprint(
    tag: str,
    attributes: Mapping[str, str],
    region: Mapping[str, float],
    frame_key: str,
    shadow: bool,
    shadow_key: str,
) -> str:
    stable_attributes = {
        name: attributes[name]
        for name in ALLOWED_ATTRIBUTES
        if name in attributes
        and not (name == "id" and _is_dynamic_id(attributes[name]))
    }
    canonical = json.dumps(
        {
            "tag": tag,
            "attributes": stable_attributes,
            "frame_key": frame_key,
            "shadow": shadow,
            "shadow_key": shadow_key,
            "center": [
                round(region["x"] + region["width"] / 2, 2),
                round(region["y"] + region["height"] / 2, 2),
            ],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _normalize_item(raw: Mapping[str, object]) -> dict[str, object]:
    tag = _clean_text(raw.get("tag"), 24).casefold()
    attributes = _attributes(raw.get("attributes"))
    input_type = _clean_text(
        raw.get("input_type") or attributes.get("type"), 32
    ).casefold()
    region = _region(raw.get("region"))
    frame_key = _clean_text(raw.get("frame_key"), 120)
    shadow = raw.get("shadow") is True
    shadow_key = _clean_text(raw.get("shadow_key"), 160)
    fingerprint = _fingerprint(
        tag, attributes, region, frame_key, shadow, shadow_key
    )
    dynamic_id = attributes.get("id", "")
    if not _is_dynamic_id(dynamic_id):
        dynamic_id = ""

    locators: list[dict[str, object]] = []
    seen_locators: set[tuple[str, str]] = set()
    raw_locators = raw.get("locators")
    if isinstance(raw_locators, Sequence) and not isinstance(
        raw_locators, (str, bytes, bytearray)
    ):
        for candidate in raw_locators[: MAX_LOCATORS * 4]:
            selected = _locator(candidate)
            if selected is None or _dynamic_id_locator(
                str(selected["value"]), dynamic_id
            ):
                continue
            key = (str(selected["type"]), str(selected["value"]))
            if key in seen_locators:
                continue
            seen_locators.add(key)
            locators.append(selected)
            if len(locators) == MAX_LOCATORS:
                break

    match_counts = {
        f'{item["type"]}:{item["value"]}': item["match_count"]
        for item in locators
    }
    visible = raw.get("visible") is True
    enabled = raw.get("enabled") is True
    hit_target = raw.get("hit_target") is True
    target_match = raw.get("target_match") is True
    text = _clean_text(raw.get("text"), 240)
    name = _clean_text(raw.get("name"), 160)
    private_text = tag in _PRIVATE_TEXT_TAGS or attributes.get(
        "contenteditable", ""
    ).casefold() in {
        "true",
        "plaintext-only",
    }
    if private_text:
        text = ""
        name = ""
    return {
        "selection_id": f"selection-{fingerprint.removeprefix('sha256:')[:24]}",
        "fingerprint": fingerprint,
        "tag": tag,
        "input_type": input_type,
        "text": text,
        "role": _clean_text(raw.get("role"), 48).casefold(),
        "name": name,
        "attributes": attributes,
        "frame_key": frame_key,
        "shadow": shadow,
        "shadow_key": shadow_key,
        "region": region,
        "locators": locators,
        "locatable": (
            visible
            and enabled
            and hit_target
            and target_match
            and any(item["match_count"] == 1 for item in locators)
        ),
        "match_counts": match_counts,
        "visible": visible,
        "enabled": enabled,
        "hit_target": hit_target,
        "target_match": target_match,
    }


def public_inventory_item(value: Mapping[str, object]) -> dict[str, object]:
    """Return the bounded inventory shape allowed outside the probe service."""

    if not isinstance(value, Mapping):
        value = {}
    attributes = _attributes(value.get("attributes"))
    dynamic_id = attributes.get("id", "")
    if not _is_dynamic_id(dynamic_id):
        dynamic_id = ""
    locators: list[dict[str, object]] = []
    raw_locators = value.get("locators")
    if isinstance(raw_locators, Sequence) and not isinstance(
        raw_locators, (str, bytes, bytearray)
    ):
        for raw in raw_locators[: MAX_LOCATORS * 4]:
            selected = _locator(raw)
            if selected is None or _dynamic_id_locator(
                str(selected["value"]), dynamic_id
            ):
                continue
            locators.append(selected)
            if len(locators) == MAX_LOCATORS:
                break
    visible = value.get("visible") is True
    enabled = value.get("enabled") is True
    hit_target = value.get("hit_target") is True
    target_match = value.get("target_match") is True
    tag = _clean_text(value.get("tag"), 24).casefold()
    text = _clean_text(value.get("text"), 240)
    name = _clean_text(value.get("name"), 160)
    private_text = tag in _PRIVATE_TEXT_TAGS or attributes.get(
        "contenteditable", ""
    ).casefold() in {"true", "plaintext-only"}
    if private_text:
        text = ""
        name = ""
    result: dict[str, object] = {
        "selection_id": _clean_text(value.get("selection_id"), 64),
        "fingerprint": _clean_text(value.get("fingerprint"), 80),
        "tag": tag,
        "input_type": _clean_text(value.get("input_type"), 32).casefold(),
        "text": text,
        "role": _clean_text(value.get("role"), 48).casefold(),
        "name": name,
        "attributes": attributes,
        "frame_key": _clean_text(value.get("frame_key"), 120),
        "shadow": value.get("shadow") is True,
        "shadow_key": _clean_text(value.get("shadow_key"), 160),
        "region": _region(value.get("region"))
        if isinstance(value.get("region"), Mapping)
        else {},
        "locators": locators,
        "locatable": (
            visible
            and enabled
            and hit_target
            and target_match
            and any(item["match_count"] == 1 for item in locators)
        ),
        "match_counts": {
            f'{item["type"]}:{item["value"]}': item["match_count"]
            for item in locators
        },
        "visible": visible,
        "enabled": enabled,
        "hit_target": hit_target,
        "target_match": target_match,
    }
    return {key: result[key] for key in _PUBLIC_ITEM_KEYS}


def normalize_inventory(
    raw_items: object,
    *,
    selection_ids: Mapping[str, str] | None = None,
    limit: int = MAX_INVENTORY_ITEMS,
) -> list[dict[str, object]]:
    """Normalize a bounded DOM scan without applying semantic matching."""

    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        return []
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return []
    selected_limit = min(limit, MAX_INVENTORY_ITEMS)
    known_ids = selection_ids if isinstance(selection_ids, Mapping) else {}

    grouped: dict[str, tuple[int, dict[str, object]]] = {}
    for index, raw in enumerate(raw_items[:MAX_RAW_ITEMS]):
        if not isinstance(raw, Mapping):
            continue
        item = _normalize_item(raw)
        target_key = _clean_text(raw.get("target_key"), 200)
        identity = (
            f'target:{item["frame_key"]}:{item["shadow_key"]}:{target_key}'
            if target_key
            else f'scope:{item["fingerprint"]}'
        )
        current = grouped.get(identity)
        rank = (bool(item["locatable"]), len(item["locators"]))
        if current is None:
            grouped[identity] = (index, item)
            continue
        current_rank = (
            bool(current[1]["locatable"]),
            len(current[1]["locators"]),
        )
        if rank > current_rank:
            grouped[identity] = (current[0], item)

    candidates = list(grouped.values())
    candidates.sort(
        key=lambda pair: (
            not bool(pair[1]["locatable"]),
            float(pair[1]["region"]["y"]),
            float(pair[1]["region"]["x"]),
            str(pair[1]["fingerprint"]),
            pair[0],
        )
    )
    result: list[dict[str, object]] = []
    used_selection_ids: set[str] = set()
    for _, item in candidates[:selected_limit]:
        fingerprint = str(item["fingerprint"])
        reused = _clean_text(known_ids.get(fingerprint), 64)
        if (
            _SELECTION_ID_RE.fullmatch(reused) is not None
            and reused not in used_selection_ids
        ):
            selection_id = reused
        else:
            digest = fingerprint.removeprefix("sha256:")
            selection_id = f"selection-{digest[:24]}"
            length = 28
            while selection_id in used_selection_ids and length <= len(digest):
                selection_id = f"selection-{digest[:length]}"
                length += 4
            suffix = 2
            base = selection_id
            while selection_id in used_selection_ids:
                selection_id = f"{base[:60]}-{suffix}"
                suffix += 1
        item["selection_id"] = selection_id
        used_selection_ids.add(selection_id)
        result.append(public_inventory_item(item))
    return result


def _safe_url(value: object) -> str:
    selected = _clean_text(value, 4000)
    if not selected:
        return ""
    try:
        parsed = urlsplit(selected)
        scheme = parsed.scheme.casefold()
        hostname = (parsed.hostname or "").casefold()
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if scheme not in {"http", "https"} or not hostname:
        return ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        rendered_host = f"{rendered_host}:{port}"
    path = parsed.path or "/"
    return f"{scheme}://{rendered_host}{path}"[:2000]


def normalize_recorded_step(raw: object) -> dict[str, object]:
    """Normalize one user-performed page action for deterministic replay."""

    if not isinstance(raw, Mapping):
        raise ValueError("invalid_recorded_step")
    sequence = raw.get("sequence")
    locator = _recorded_locator(raw.get("locator"))
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or sequence < 1
        or locator is None
    ):
        raise ValueError("invalid_recorded_step")
    return {
        "sequence": sequence,
        "locator": locator,
        "url_before": _safe_url(raw.get("url_before")),
        "url_after": _safe_url(raw.get("url_after")),
        "recorded_at": _clean_text(raw.get("recorded_at"), 64),
        "frame_key": _clean_text(raw.get("frame_key"), 120),
        "shadow": raw.get("shadow") is True,
        "shadow_key": _clean_text(raw.get("shadow_key"), 160),
    }


__all__ = [
    "ALLOWED_ATTRIBUTES",
    "ALLOWED_LOCATOR_TYPES",
    "FORBIDDEN_XPATH_PREFIXES",
    "MAX_INVENTORY_ITEMS",
    "MAX_LOCATORS",
    "MAX_RAW_ITEMS",
    "normalize_inventory",
    "normalize_recorded_step",
    "public_inventory_item",
]
