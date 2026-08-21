from __future__ import annotations

import asyncio
import math
import re
import sys
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


STABLE_ATTRIBUTES = {
    "data-e2e",
    "data-testid",
    "aria-label",
    "aria-labelledby",
    "name",
    "id",
    "placeholder",
    "role",
    "contenteditable",
    "type",
}
MAX_SEMANTIC_NODES = 500

_COMPUTED_STYLES = (
    "display",
    "visibility",
    "pointer-events",
    "opacity",
)
_SAFE_STATES = {
    "atomic",
    "busy",
    "checked",
    "disabled",
    "editable",
    "expanded",
    "focusable",
    "focused",
    "haspopup",
    "hidden",
    "invalid",
    "level",
    "modal",
    "multiline",
    "multiselectable",
    "orientation",
    "pressed",
    "readonly",
    "required",
    "selected",
}
_SAFE_STATE_STRING_VALUES = {
    "both",
    "dialog",
    "false",
    "grammar",
    "grid",
    "horizontal",
    "inline",
    "list",
    "listbox",
    "menu",
    "mixed",
    "none",
    "spelling",
    "tree",
    "true",
    "vertical",
}
_CONTROL_ROLES = {
    "button",
    "checkbox",
    "combobox",
    "listbox",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "option",
    "radio",
    "scrollbar",
    "searchbox",
    "slider",
    "spinbutton",
    "switch",
    "tab",
    "textbox",
}
_INTERACTIVE_TAGS = {
    "button",
    "input",
    "option",
    "select",
    "summary",
    "textarea",
}
_TEXTUAL_ROLES = {
    "caption",
    "code",
    "definition",
    "heading",
    "listitem",
    "paragraph",
    "statictext",
    "term",
}
_SENSITIVE_KEY_RE = re.compile(
    r"(?:auth(?:orization)?|bearer|cookie|credential|csrf|jwt|pass(?:word)?|"
    r"secret|session|token)",
    re.IGNORECASE,
)
_LONG_DIGITS_RE = re.compile(r"\d{12,}")
_UNIX_TIMESTAMP_RE = re.compile(r"(?<!\d)(?:1[5-9]|2[0-2])\d{8}(?!\d)")
_ISO_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?\b",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_HANDLE_RE = re.compile(r"(?:https?://[^\s]+)?/@[A-Za-z0-9._-]+", re.IGNORECASE)
_JWT_RE = re.compile(
    r"(?:bearer\s+)?[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}\.[A-Za-z0-9_-]{3,}",
    re.IGNORECASE,
)
_LONG_SECRET_RE = re.compile(r"\b(?:[0-9a-f]{24,}|[A-Za-z0-9_+/=-]{32,})\b")
_GENERATED_VALUE_RE = re.compile(
    r"^(?:css|sc|jsx|emotion|styled)-[A-Za-z0-9_-]{5,}$",
    re.IGNORECASE,
)
_UI_LABEL_PATTERNS = (
    (
        "comment-input",
        re.compile(
            r"(?:comment-input|(?:add|write|leave)\s+(?:a\s+)?comment|"
            r"写评论|添加评论|留下评论|说点什么)",
            re.IGNORECASE,
        ),
    ),
    (
        "close-comments",
        re.compile(
            r"(?:close\s+comments?|关闭评论(?:区)?)",
            re.IGNORECASE,
        ),
    ),
    (
        "comments",
        re.compile(
            r"(?:(?:open|view)\s+comments?|comments?(?:\s+button)?|"
            r"(?:打开|查看)?评论(?:区|按钮)?)",
            re.IGNORECASE,
        ),
    ),
    ("reply", re.compile(r"(?:reply|replies|回复)", re.IGNORECASE)),
    ("close", re.compile(r"(?:close|关闭)", re.IGNORECASE)),
    ("search", re.compile(r"(?:search|搜索)", re.IGNORECASE)),
    ("send", re.compile(r"(?:send|发送)", re.IGNORECASE)),
    (
        "publish",
        re.compile(r"(?:post|publish|submit|发布|发表|提交)", re.IGNORECASE),
    ),
    (
        "like",
        re.compile(r"(?:like|unlike|喜欢|点赞|取消点赞)", re.IGNORECASE),
    ),
    ("share", re.compile(r"(?:share|分享)", re.IGNORECASE)),
    (
        "next",
        re.compile(r"(?:next(?:\s+(?:video|item|page))?|下一条|下一个|下一页)", re.IGNORECASE),
    ),
    (
        "previous",
        re.compile(
            r"(?:previous|prev)(?:\s+(?:video|item|page))?|上一条|上一个|上一页",
            re.IGNORECASE,
        ),
    ),
    ("play", re.compile(r"(?:play|播放)", re.IGNORECASE)),
    ("pause", re.compile(r"(?:pause|暂停)", re.IGNORECASE)),
    ("cancel", re.compile(r"(?:cancel|取消)", re.IGNORECASE)),
    ("more", re.compile(r"(?:more|more options|更多)", re.IGNORECASE)),
)
_SAFE_REFERENCE_RE = re.compile(r"[A-Za-z][A-Za-z0-9:_-]{0,63}")
_SAFE_REFERENCE_HINT_RE = re.compile(
    r"(?:comment|reply|close|search|send|submit|publish|post|like|share|"
    r"next|prev|play|pause|modal|dialog|input|button|label)",
    re.IGNORECASE,
)
_SAFE_FORM_NAMES = {
    "comment",
    "message",
    "q",
    "query",
    "reply",
    "search",
    "submit",
}


@dataclass(frozen=True)
class SemanticNode:
    backend_node_id: int
    parent_backend_node_id: int | None
    tag: str
    role: str
    name: str
    states: dict[str, bool | str | int | float | None]
    attributes: dict[str, str]
    bounds: tuple[float, float, float, float] | None
    visible: bool
    in_viewport: bool
    actionable: bool


@dataclass(frozen=True)
class SemanticSnapshot:
    nodes: tuple[SemanticNode, ...]
    scope: str = "page"
    viewport: tuple[int, int] | None = None

    def model_payload(self) -> dict[str, object]:
        nodes: list[dict[str, object]] = []
        for node in self.nodes:
            role = _clean_identifier(node.role)
            tag = _clean_identifier(node.tag)
            nodes.append(
                {
                    "backend_node_id": node.backend_node_id,
                    "parent_backend_node_id": node.parent_backend_node_id,
                    "tag": tag,
                    "role": role,
                    "name": _sanitize_accessible_name(node.name, role, tag),
                    "states": _sanitize_states(node.states),
                    "attributes": _sanitize_attributes(
                        node.attributes,
                        role=role,
                        tag=tag,
                    ),
                    "bounds": node.bounds,
                    "visible": node.visible,
                    "in_viewport": node.in_viewport,
                    "actionable": node.actionable,
                }
            )
        return {
            "scope": self.scope,
            "viewport": self.viewport,
            "nodes": nodes,
        }


def _clean_identifier(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    if not value or len(value) > 64:
        return ""
    if not re.fullmatch(r"[a-z][a-z0-9:_-]*", value):
        return ""
    return value


def _safe_text(value: object, *, maximum: int = 120) -> str:
    if not isinstance(value, str):
        return ""
    result = " ".join(value.split())
    if not result or len(result) > maximum:
        return ""
    if (
        _LONG_DIGITS_RE.search(result)
        or _UNIX_TIMESTAMP_RE.search(result)
        or _ISO_TIMESTAMP_RE.search(result)
        or _UUID_RE.search(result)
        or _HANDLE_RE.search(result)
        or _JWT_RE.search(result)
        or _LONG_SECRET_RE.search(result)
        or _GENERATED_VALUE_RE.fullmatch(result)
    ):
        return ""
    return result


def _sanitize_accessible_name(value: object, role: str, tag: str) -> str:
    role = role.lower()
    tag = tag.lower()
    if role in _TEXTUAL_ROLES:
        return ""
    if role not in _CONTROL_ROLES and tag not in _INTERACTIVE_TAGS:
        return ""
    text = _safe_text(value, maximum=80)
    if not text:
        return ""
    for category, pattern in _UI_LABEL_PATTERNS:
        if pattern.fullmatch(text):
            return category
    return ""


def _safe_reference(value: object) -> str:
    value = _safe_text(value, maximum=64)
    if (
        not value
        or not _SAFE_REFERENCE_RE.fullmatch(value)
        or not _SAFE_REFERENCE_HINT_RE.search(value)
    ):
        return ""
    return value


def _sanitize_attributes(
    attributes: object,
    *,
    role: str = "",
    tag: str = "",
) -> dict[str, str]:
    if not isinstance(attributes, Mapping):
        return {}
    result: dict[str, str] = {}
    for raw_name, raw_value in attributes.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip().lower()
        if name not in STABLE_ATTRIBUTES or _SENSITIVE_KEY_RE.search(name):
            continue
        value = _safe_text(raw_value)
        if not value or _SENSITIVE_KEY_RE.search(value):
            continue
        if name == "id":
            if (
                _GENERATED_VALUE_RE.fullmatch(value)
                or re.fullmatch(r"[A-Za-z_-]*\d{6,}[A-Za-z0-9_-]*", value)
            ):
                continue
            value = _safe_reference(value)
        elif name == "aria-label":
            value = _sanitize_accessible_name(value, role, tag)
        elif name == "aria-labelledby":
            value = _safe_reference(value)
        elif name == "placeholder":
            value = _sanitize_accessible_name(value, role, tag)
        elif name == "name" and value.casefold() not in _SAFE_FORM_NAMES:
            value = ""
        elif name == "role":
            value = _clean_identifier(value)
        elif name == "contenteditable":
            value = value.lower() if value.lower() in {"true", "false"} else ""
        elif name == "type":
            value = (
                value.lower()
                if value.lower()
                in {
                    "button",
                    "checkbox",
                    "email",
                    "radio",
                    "search",
                    "submit",
                    "text",
                }
                else ""
            )
        if value:
            result[name] = value
    return result


def _primitive_ax_value(value: object) -> bool | str | int | float | None | object:
    if isinstance(value, Mapping):
        value = value.get("value")
    if isinstance(value, float) and not math.isfinite(value):
        return _UNSAFE
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return _UNSAFE


_UNSAFE = object()


def _sanitize_states(
    states: object,
) -> dict[str, bool | str | int | float | None]:
    if not isinstance(states, Mapping):
        return {}
    result: dict[str, bool | str | int | float | None] = {}
    for raw_name, raw_value in states.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip().lower()
        if name not in _SAFE_STATES or _SENSITIVE_KEY_RE.search(name):
            continue
        value = _primitive_ax_value(raw_value)
        if value is _UNSAFE:
            continue
        if isinstance(value, str):
            value = _safe_text(value, maximum=32).lower()
            if value not in _SAFE_STATE_STRING_VALUES:
                continue
        result[name] = value
    return result


def _required_list(value: object, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"malformed DOM snapshot {field}")
    return value


def _optional_list(value: object, field: str) -> list[Any]:
    if value is None:
        return []
    return _required_list(value, field)


def _string_at(strings: Sequence[object], index: object, field: str) -> str:
    if (
        not isinstance(index, int)
        or isinstance(index, bool)
        or index < 0
        or index >= len(strings)
        or not isinstance(strings[index], str)
    ):
        raise ValueError(f"malformed DOM snapshot {field} string index")
    return strings[index]


def _optional_string_at(
    strings: Sequence[object],
    index: object,
    field: str,
) -> str:
    if index == -1:
        return ""
    return _string_at(strings, index, field)


def _value_at(values: Sequence[Any], index: int, default: Any) -> Any:
    return values[index] if index < len(values) else default


def _decode_attributes(
    strings: Sequence[object],
    encoded: object,
    *,
    tag: str,
) -> dict[str, str]:
    if encoded is None:
        return {}
    if not isinstance(encoded, list) or len(encoded) % 2:
        raise ValueError("malformed DOM snapshot node attributes")
    decoded: dict[str, str] = {}
    for index in range(0, len(encoded), 2):
        name = _string_at(strings, encoded[index], "attribute name")
        value = _optional_string_at(
            strings,
            encoded[index + 1],
            "attribute value",
        )
        decoded[name] = value
    return _sanitize_attributes(
        decoded,
        role=_clean_identifier(decoded.get("role")),
        tag=_clean_identifier(tag),
    )


def _decode_styles(
    strings: Sequence[object],
    encoded: object,
) -> dict[str, str]:
    if encoded is None:
        return {}
    if not isinstance(encoded, list):
        raise ValueError("malformed DOM snapshot layout styles")
    result: dict[str, str] = {}
    for index, style_name in enumerate(_COMPUTED_STYLES):
        if index >= len(encoded):
            break
        value = _optional_string_at(strings, encoded[index], "computed style")
        if value:
            result[style_name] = value
    return result


def _normalize_bounds(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            for item in value
        )
    ):
        raise ValueError("malformed DOM snapshot layout bounds")
    return tuple(float(item) for item in value)  # type: ignore[return-value]


def _is_visible(
    bounds: tuple[float, float, float, float] | None,
    styles: Mapping[str, str],
) -> bool:
    if bounds is None or bounds[2] <= 0 or bounds[3] <= 0:
        return False
    if styles.get("display", "").strip().lower() == "none":
        return False
    if styles.get("visibility", "").strip().lower() in {"hidden", "collapse"}:
        return False
    try:
        if float(styles.get("opacity", "1") or "1") <= 0:
            return False
    except ValueError:
        return False
    return True


def _intersects_viewport(
    bounds: tuple[float, float, float, float] | None,
    viewport: tuple[int, int] | None,
    scroll_offset: tuple[float, float] = (0.0, 0.0),
) -> bool:
    if bounds is None:
        return False
    if viewport is None:
        return True
    x, y, width, height = bounds
    viewport_width, viewport_height = viewport
    scroll_x, scroll_y = scroll_offset
    return (
        width > 0
        and height > 0
        and x < scroll_x + viewport_width
        and y < scroll_y + viewport_height
        and x + width > scroll_x
        and y + height > scroll_y
    )


def _normalize_viewport(
    viewport: object,
) -> tuple[int, int] | None:
    if viewport is None:
        return None
    if isinstance(viewport, Mapping):
        viewport = (viewport.get("width"), viewport.get("height"))
    if (
        not isinstance(viewport, (tuple, list))
        or len(viewport) != 2
        or any(
            not isinstance(item, (int, float))
            or isinstance(item, bool)
            or not math.isfinite(float(item))
            for item in viewport
        )
    ):
        raise ValueError("viewport must contain numeric width and height")
    width, height = int(viewport[0]), int(viewport[1])
    if width <= 0 or height <= 0:
        raise ValueError("viewport must be positive")
    return width, height


def _normalize_scroll_offset(
    x: object,
    y: object,
) -> tuple[float, float]:
    values = (x, y)
    if any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not math.isfinite(float(item))
        for item in values
    ):
        raise ValueError("malformed DOM snapshot document scroll offset")
    return float(x), float(y)


def decode_dom_snapshot(
    payload: object,
    *,
    viewport: tuple[int, int] | None = None,
) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise ValueError("malformed DOM snapshot payload")
    strings = _required_list(payload.get("strings"), "strings")
    if any(not isinstance(item, str) for item in strings):
        raise ValueError("malformed DOM snapshot strings")
    documents = _required_list(payload.get("documents"), "documents")
    normalized_viewport = _normalize_viewport(viewport)
    result: list[dict[str, object]] = []

    child_document_owners: dict[int, int] = {}
    owner_nodes: set[tuple[int, int]] = set()
    for document_index, document in enumerate(documents):
        if not isinstance(document, Mapping):
            raise ValueError("malformed DOM snapshot document")
        nodes = document.get("nodes")
        if not isinstance(nodes, Mapping):
            raise ValueError("malformed DOM snapshot nodes")
        node_names = _required_list(nodes.get("nodeName"), "nodeName")
        backend_ids = _required_list(nodes.get("backendNodeId"), "backendNodeId")
        if len(node_names) != len(backend_ids):
            raise ValueError("malformed DOM snapshot node arrays")
        rare_content_documents = nodes.get("contentDocumentIndex")
        if rare_content_documents is None:
            continue
        if not isinstance(rare_content_documents, Mapping):
            raise ValueError(
                "malformed DOM snapshot contentDocumentIndex sparse data"
            )
        owner_indexes = _required_list(
            rare_content_documents.get("index"),
            "contentDocumentIndex.index",
        )
        child_indexes = _required_list(
            rare_content_documents.get("value"),
            "contentDocumentIndex.value",
        )
        if len(owner_indexes) != len(child_indexes):
            raise ValueError(
                "malformed DOM snapshot contentDocumentIndex arrays"
            )
        for owner_index, child_document_index in zip(
            owner_indexes,
            child_indexes,
        ):
            if (
                not isinstance(owner_index, int)
                or isinstance(owner_index, bool)
                or owner_index < 0
                or owner_index >= len(backend_ids)
                or not isinstance(child_document_index, int)
                or isinstance(child_document_index, bool)
                or child_document_index < 0
                or child_document_index >= len(documents)
                or child_document_index == document_index
            ):
                raise ValueError(
                    "malformed DOM snapshot contentDocumentIndex entry"
                )
            owner_key = (document_index, owner_index)
            if (
                owner_key in owner_nodes
                or child_document_index in child_document_owners
            ):
                raise ValueError(
                    "malformed DOM snapshot duplicate content document"
                )
            owner_backend_id = backend_ids[owner_index]
            if (
                not isinstance(owner_backend_id, int)
                or isinstance(owner_backend_id, bool)
                or owner_backend_id <= 0
            ):
                raise ValueError(
                    "malformed DOM snapshot content document owner"
                )
            owner_tag = _string_at(
                strings,
                node_names[owner_index],
                "content document owner node name",
            ).lower()
            if owner_tag not in {"frame", "iframe"}:
                raise ValueError(
                    "malformed DOM snapshot content document owner tag"
                )
            owner_nodes.add(owner_key)
            child_document_owners[child_document_index] = owner_backend_id

    for document_index, document in enumerate(documents):
        assert isinstance(document, Mapping)
        nodes = document["nodes"]
        assert isinstance(nodes, Mapping)
        node_names = _required_list(nodes.get("nodeName"), "nodeName")
        backend_ids = _required_list(nodes.get("backendNodeId"), "backendNodeId")
        parent_indexes = _optional_list(nodes.get("parentIndex"), "parentIndex")
        attributes = _optional_list(nodes.get("attributes"), "attributes")
        scroll_offset = _normalize_scroll_offset(
            document.get("scrollOffsetX", 0),
            document.get("scrollOffsetY", 0),
        )
        owner_backend_id = child_document_owners.get(document_index)
        child_root_index: int | None = None
        if owner_backend_id is not None:
            root_indexes = [
                node_index
                for node_index in range(len(node_names))
                if _value_at(parent_indexes, node_index, -1) == -1
            ]
            if len(root_indexes) != 1:
                raise ValueError(
                    "malformed DOM snapshot child document root"
                )
            child_root_index = root_indexes[0]

        layout_by_node: dict[int, dict[str, object]] = {}
        layout = document.get("layout")
        if layout is not None:
            if not isinstance(layout, Mapping):
                raise ValueError("malformed DOM snapshot layout")
            layout_indexes = _required_list(layout.get("nodeIndex"), "layout.nodeIndex")
            layout_bounds = _optional_list(layout.get("bounds"), "layout.bounds")
            layout_styles = _optional_list(layout.get("styles"), "layout.styles")
            for layout_position, raw_node_index in enumerate(layout_indexes):
                if (
                    not isinstance(raw_node_index, int)
                    or isinstance(raw_node_index, bool)
                    or raw_node_index < 0
                    or raw_node_index >= len(node_names)
                ):
                    raise ValueError("malformed DOM snapshot layout node index")
                bounds = _normalize_bounds(
                    _value_at(layout_bounds, layout_position, None)
                )
                styles = _decode_styles(
                    strings,
                    _value_at(layout_styles, layout_position, None),
                )
                layout_by_node[raw_node_index] = {
                    "bounds": bounds,
                    "computed_styles": styles,
                }

        for node_index, (raw_name, raw_backend_id) in enumerate(
            zip(node_names, backend_ids)
        ):
            if (
                not isinstance(raw_backend_id, int)
                or isinstance(raw_backend_id, bool)
                or raw_backend_id <= 0
            ):
                raise ValueError("malformed DOM snapshot backend node id")
            tag = _string_at(strings, raw_name, "node name").lower()
            raw_parent_index = _value_at(parent_indexes, node_index, -1)
            if (
                not isinstance(raw_parent_index, int)
                or isinstance(raw_parent_index, bool)
                or raw_parent_index < -1
                or raw_parent_index >= len(backend_ids)
                or raw_parent_index == node_index
            ):
                raise ValueError("malformed DOM snapshot parent index")
            parent_backend_id: int | None = None
            if raw_parent_index >= 0:
                raw_parent_backend_id = backend_ids[raw_parent_index]
                if (
                    not isinstance(raw_parent_backend_id, int)
                    or isinstance(raw_parent_backend_id, bool)
                    or raw_parent_backend_id <= 0
                ):
                    raise ValueError("malformed DOM snapshot parent backend node id")
                parent_backend_id = raw_parent_backend_id
            elif node_index == child_root_index:
                parent_backend_id = owner_backend_id
            decoded_attributes = _decode_attributes(
                strings,
                _value_at(attributes, node_index, None),
                tag=tag,
            )
            layout_data = layout_by_node.get(node_index, {})
            bounds = layout_data.get("bounds")
            styles = layout_data.get("computed_styles", {})
            assert bounds is None or isinstance(bounds, tuple)
            assert isinstance(styles, Mapping)
            visible = _is_visible(bounds, styles)
            result.append(
                {
                    "backend_node_id": raw_backend_id,
                    "parent_backend_node_id": parent_backend_id,
                    "tag": tag,
                    "attributes": decoded_attributes,
                    "bounds": bounds,
                    "computed_styles": dict(styles),
                    "scroll_offset": scroll_offset,
                    "visible": visible,
                    "in_viewport": visible
                    and _intersects_viewport(
                        bounds,
                        normalized_viewport,
                        scroll_offset,
                    ),
                }
            )
    return result


def _ax_text(node: Mapping[str, object], field: str) -> str:
    raw = node.get(field)
    if not isinstance(raw, Mapping):
        return ""
    value = raw.get("value")
    return value if isinstance(value, str) else ""


def _ax_states(node: Mapping[str, object]) -> dict[str, object]:
    properties = node.get("properties")
    if not isinstance(properties, list):
        return {}
    states: dict[str, object] = {}
    for item in properties:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        if isinstance(name, str):
            states[name] = item.get("value")
    return states


def _valid_backend_id(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _coerce_parent_id(value: object) -> int | None:
    if value is None:
        return None
    return _valid_backend_id(value)


def _coerce_bounds(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    return _normalize_bounds(value)


def _node_semantic_priority(
    tag: str,
    role: str,
    attributes: Mapping[str, str],
    has_ax_node: bool,
) -> int | None:
    if "data-e2e" in attributes:
        return 0
    if "data-testid" in attributes:
        return 1
    if any(
        name in attributes
        for name in ("aria-label", "placeholder", "name", "id")
    ):
        return 2
    if role in _CONTROL_ROLES or tag in _INTERACTIVE_TAGS:
        return 3
    if attributes:
        return 4
    if has_ax_node and role not in {"", "generic", "none", "presentation"}:
        return 5
    return None


def _normalize_scope(scope: object) -> str:
    if scope != "page":
        raise ValueError("semantic snapshot scope must be 'page'")
    return "page"


def _node_scroll_offset(node: Mapping[str, object]) -> tuple[float, float]:
    value = node.get("scroll_offset")
    if value is None:
        return 0.0, 0.0
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("DOM node scroll_offset must contain x and y")
    return _normalize_scroll_offset(value[0], value[1])


def _select_retained_ids(
    semantic_priorities: Mapping[int, int],
    dom_by_backend_id: Mapping[int, Mapping[str, object]],
) -> set[int]:
    retained: set[int] = set()
    for backend_id, _priority in sorted(
        semantic_priorities.items(),
        key=lambda item: (item[1], item[0]),
    ):
        path: list[int] = []
        current_id: int | None = backend_id
        seen: set[int] = set()
        valid_path = True
        while current_id is not None:
            if current_id in seen:
                valid_path = False
                break
            seen.add(current_id)
            current = dom_by_backend_id.get(current_id)
            if current is None:
                break
            path.append(current_id)
            parent = current.get("parent_backend_node_id")
            current_id = parent if isinstance(parent, int) else None
        if not valid_path:
            continue
        additions = set(path) - retained
        if len(retained) + len(additions) > MAX_SEMANTIC_NODES:
            continue
        retained.update(additions)
    return retained


def build_semantic_snapshot(
    ax_nodes: object,
    dom_nodes: object,
    *,
    scope: str = "page",
    viewport: tuple[int, int] | None = None,
) -> SemanticSnapshot:
    normalized_scope = _normalize_scope(scope)
    if not isinstance(ax_nodes, list):
        raise ValueError("AX nodes must be a list")
    if not isinstance(dom_nodes, list):
        raise ValueError("DOM nodes must be a list")
    normalized_viewport = _normalize_viewport(viewport)

    ax_by_backend_id: dict[int, Mapping[str, object]] = {}
    for raw_ax_node in ax_nodes:
        if not isinstance(raw_ax_node, Mapping):
            continue
        backend_id = _valid_backend_id(raw_ax_node.get("backendDOMNodeId"))
        if backend_id is None or raw_ax_node.get("ignored") is True:
            continue
        ax_by_backend_id[backend_id] = raw_ax_node

    normalized_dom: list[dict[str, object]] = []
    dom_by_backend_id: dict[int, dict[str, object]] = {}
    semantic_priorities: dict[int, int] = {}
    for raw_dom_node in dom_nodes:
        if not isinstance(raw_dom_node, Mapping):
            continue
        backend_id = _valid_backend_id(raw_dom_node.get("backend_node_id"))
        if backend_id is None or backend_id in dom_by_backend_id:
            continue
        parent_backend_id = _coerce_parent_id(
            raw_dom_node.get("parent_backend_node_id")
        )
        tag = _clean_identifier(raw_dom_node.get("tag"))
        raw_ax_node = ax_by_backend_id.get(backend_id)
        raw_attributes = raw_dom_node.get("attributes")
        role = _clean_identifier(
            _ax_text(raw_ax_node, "role") if raw_ax_node else ""
        )
        if not role and isinstance(raw_attributes, Mapping):
            role = _clean_identifier(raw_attributes.get("role"))
        attributes = _sanitize_attributes(
            raw_attributes,
            role=role,
            tag=tag,
        )
        states = _sanitize_states(_ax_states(raw_ax_node) if raw_ax_node else {})
        name = _sanitize_accessible_name(
            _ax_text(raw_ax_node, "name") if raw_ax_node else "",
            role,
            tag,
        )
        bounds = _coerce_bounds(raw_dom_node.get("bounds"))
        visible = raw_dom_node.get("visible") is True
        if bounds is None:
            visible = False
        if normalized_viewport is None:
            in_viewport = raw_dom_node.get("in_viewport") is True
        else:
            scroll_offset = _node_scroll_offset(raw_dom_node)
            in_viewport = visible and _intersects_viewport(
                bounds,
                normalized_viewport,
                scroll_offset,
            )
        node = {
            "backend_node_id": backend_id,
            "parent_backend_node_id": parent_backend_id,
            "tag": tag,
            "role": role,
            "name": name,
            "states": states,
            "attributes": attributes,
            "bounds": bounds,
            "visible": visible,
            "in_viewport": in_viewport,
            # A static AX+DOM capture cannot prove hit-test success or stability.
            # The later Playwright Dry-Run validator is the only authority.
            "actionable": False,
        }
        normalized_dom.append(node)
        dom_by_backend_id[backend_id] = node
        priority = _node_semantic_priority(
            tag,
            role,
            attributes,
            raw_ax_node is not None,
        )
        if priority is not None:
            semantic_priorities[backend_id] = priority

    retained_ids = _select_retained_ids(
        semantic_priorities,
        dom_by_backend_id,
    )

    nodes = tuple(
        SemanticNode(**node)  # type: ignore[arg-type]
        for node in sorted(
            normalized_dom,
            key=lambda item: int(item["backend_node_id"]),
        )
        if node["backend_node_id"] in retained_ids
    )
    return SemanticSnapshot(
        nodes=nodes,
        scope=normalized_scope,
        viewport=normalized_viewport,
    )


def _extract_ax_nodes(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise ValueError("malformed Accessibility tree payload")
    nodes = payload.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(node, dict) for node in nodes
    ):
        raise ValueError("malformed Accessibility tree nodes")
    return nodes


async def _page_viewport(page: object) -> tuple[int, int] | None:
    viewport = getattr(page, "viewport_size", None)
    if viewport is None:
        evaluate = getattr(page, "evaluate", None)
        if not callable(evaluate):
            return None
        viewport = await evaluate(
            "() => ({ width: window.innerWidth, height: window.innerHeight })"
        )
    return _normalize_viewport(viewport)


async def extract_semantic_snapshot(
    page: object,
    *,
    scope: str = "page",
) -> SemanticSnapshot:
    normalized_scope = _normalize_scope(scope)
    viewport = await _page_viewport(page)
    context = getattr(page, "context", None)
    new_cdp_session = getattr(context, "new_cdp_session", None)
    if not callable(new_cdp_session):
        raise TypeError("page context does not support CDP sessions")
    session = await new_cdp_session(page)
    send = getattr(session, "send", None)
    detach = getattr(session, "detach", None)
    if not callable(send) or not callable(detach):
        raise TypeError("invalid CDP session")

    cleanup_error: BaseException | None = None
    try:
        await send("Accessibility.enable")
        ax_payload = await send("Accessibility.getFullAXTree")
        dom_payload = await send(
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": list(_COMPUTED_STYLES),
                "includeDOMRects": True,
                "includePaintOrder": True,
            },
        )
        ax_nodes = _extract_ax_nodes(ax_payload)
        dom_nodes = decode_dom_snapshot(dom_payload, viewport=viewport)
        return build_semantic_snapshot(
            ax_nodes,
            dom_nodes,
            scope=normalized_scope,
            viewport=viewport,
        )
    finally:
        primary_error = sys.exc_info()[1]
        try:
            await send("Accessibility.disable")
        except (asyncio.CancelledError, Exception) as error:
            cleanup_error = error
        try:
            await detach()
        except (asyncio.CancelledError, Exception) as error:
            if cleanup_error is None:
                cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


__all__ = [
    "MAX_SEMANTIC_NODES",
    "STABLE_ATTRIBUTES",
    "SemanticNode",
    "SemanticSnapshot",
    "build_semantic_snapshot",
    "decode_dom_snapshot",
    "extract_semantic_snapshot",
]
