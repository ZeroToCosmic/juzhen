"""Managed catalog for manually selected browser elements."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import math
import re
import secrets
from urllib.parse import urlsplit

from selector_probe.inventory import normalize_recorded_step

from .view_models import ElementRecord


ALLOWED_PAGE_SIZES = frozenset({20, 50, 100})
MAX_SQLITE_INTEGER = (1 << 63) - 1
ELEMENT_STATUSES = frozenset(
    {
        "pending_rebind",
        "draft",
        "validating",
        "healthy",
        "degraded",
        "invalid",
        "disabled",
    }
)
ALLOWED_STATUSES = frozenset({"all", *ELEMENT_STATUSES})
ALLOWED_REFERENCED = frozenset({"all", "yes", "no"})
CREATE_FIELDS = frozenset(
    {
        "display_name",
        "page_key",
        "target_origin",
        "url_pattern",
        "operation_steps",
        "fingerprint",
        "locators",
    }
)
DEFINITION_FIELDS = CREATE_FIELDS - {"display_name"}
MAX_OPERATION_STEPS = 20
MAX_LOCATORS = 6
MAX_DEFINITION_JSON_BYTES = 64 * 1024
MAX_FINGERPRINT_JSON_BYTES = 16 * 1024
MAX_URL_PATTERN_LENGTH = 2000

_DEFAULT_TARGET_ORIGINS = frozenset({"https://www.tiktok.com"})
_PAGE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}\Z")
_STEP_FIELDS = frozenset(
    {
        "sequence",
        "locator",
        "url_before",
        "url_after",
        "recorded_at",
        "frame_key",
        "shadow",
        "shadow_key",
    }
)


@dataclass(frozen=True)
class ElementQuery:
    page: int = 1
    page_size: int = 20
    search: str = ""
    status: str = "all"
    referenced: str = "all"


@dataclass(frozen=True)
class PageResult:
    items: tuple[ElementRecord, ...]
    page: int
    page_size: int
    total: int
    revision: int


class ElementCatalog:
    def __init__(
        self,
        store: object,
        *,
        element_id_factory: Callable[[], str] | None = None,
        allowed_target_origins: object = None,
        site: str = "tiktok",
        environment: str = "production",
    ):
        self.store = store
        self.element_id_factory = element_id_factory or (
            lambda: "element-" + secrets.token_hex(8)
        )
        self.allowed_target_origins = _allowed_origins(
            allowed_target_origins
        )
        self.site = str(site)
        self.environment = str(environment)

    def list(self, query: ElementQuery) -> PageResult:
        selected = _validated_query(query)
        rows, total, revision = self.store.list_managed_element_rows(
            page=selected.page,
            page_size=selected.page_size,
            search=selected.search,
            status=selected.status,
            referenced=selected.referenced,
        )
        return PageResult(
            items=tuple(_element_record(row) for row in rows),
            page=selected.page,
            page_size=selected.page_size,
            total=int(total),
            revision=int(revision),
        )

    def get(self, element_id: str) -> ElementRecord | None:
        row = self.store.get_managed_element_row(_element_id(element_id))
        return _element_record(row) if row is not None else None

    def draft(self, element_id: str) -> dict[str, object] | None:
        loader = getattr(self.store, "manual_element_definition", None)
        if not callable(loader):
            raise RuntimeError("manual element definition store is unavailable")
        definition = loader(_element_id(element_id))
        if definition is None:
            return None
        if not isinstance(definition, Mapping):
            raise RuntimeError("manual element definition is corrupt")
        return _bounded_json_object(
            definition,
            "manual element definition",
            MAX_DEFINITION_JSON_BYTES,
            error_type=RuntimeError,
        )

    def dependencies(
        self,
        element_id: str,
    ) -> tuple[dict[str, str], ...]:
        rows = self.store.managed_element_dependency_rows(
            _element_id(element_id)
        )
        return tuple(
            {
                "strategy_id": str(row["strategy_id"]),
                "strategy_name": str(row["strategy_name"]),
                "action_id": str(row["action_id"]),
                "action_type": str(row["action_type"]),
            }
            for row in rows
        )

    def history(self, element_id: str) -> tuple[dict[str, object], ...]:
        loader = getattr(
            self.store,
            "managed_element_version_history",
            None,
        )
        if not callable(loader):
            return ()
        return tuple(
            loader(
                _element_id(element_id),
                site=self.site,
                environment=self.environment,
                limit=100,
            )
        )

    def create_draft(
        self,
        payload: object,
        actor_user_id: int,
        actor_username: str = "unknown",
    ) -> ElementRecord:
        element_id = _element_id(self.element_id_factory())
        display_name, definition = _normalized_create_payload(
            payload,
            self.allowed_target_origins,
        )
        self.store.create_manual_element_draft(
            element_id=element_id,
            display_name=display_name,
            definition=definition,
            page_key=str(definition["page_key"]),
            target_origin=str(definition["target_origin"]),
            url_pattern=str(definition["url_pattern"]),
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return _required_record(self, element_id)

    def update_name(
        self,
        element_id: str,
        display_name: object,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str = "unknown",
    ) -> ElementRecord:
        selected_id = _element_id(element_id)
        self.store.update_manual_element_name(
            element_id=selected_id,
            display_name=_display_name(display_name),
            expected_revision=_positive_revision(expected_revision),
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return _required_record(self, selected_id)

    def rebind(
        self,
        element_id: str,
        definition: object,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str = "unknown",
    ) -> ElementRecord:
        selected_id = _element_id(element_id)
        normalized = _normalized_definition(
            definition,
            self.allowed_target_origins,
        )
        self.store.rebind_manual_element(
            element_id=selected_id,
            definition=normalized,
            page_key=str(normalized["page_key"]),
            target_origin=str(normalized["target_origin"]),
            url_pattern=str(normalized["url_pattern"]),
            expected_revision=_positive_revision(expected_revision),
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return _required_record(self, selected_id)

    def delete(
        self,
        element_id: str,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str = "unknown",
    ) -> None:
        self.store.delete_managed_element(
            element_id=_element_id(element_id),
            expected_revision=_positive_revision(expected_revision),
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )

    def require_revision(
        self,
        element_id: str,
        expected_revision: object,
    ) -> ElementRecord:
        from selector_probe.store import (
            ElementNotFoundError,
            StaleElementRevisionError,
        )

        selected_id = _element_id(element_id)
        expected = _positive_revision(expected_revision)
        record = self.get(selected_id)
        if record is None:
            raise ElementNotFoundError(selected_id)
        if record.revision != expected:
            raise StaleElementRevisionError(selected_id)
        return record


def _validated_query(value: ElementQuery) -> ElementQuery:
    if not isinstance(value, ElementQuery):
        raise TypeError("query must be an ElementQuery")
    if (
        isinstance(value.page, bool)
        or not isinstance(value.page, int)
        or value.page < 1
        or isinstance(value.page_size, bool)
        or value.page_size not in ALLOWED_PAGE_SIZES
    ):
        raise ValueError("invalid_pagination")
    if (value.page - 1) * value.page_size > MAX_SQLITE_INTEGER:
        raise ValueError("invalid_pagination")
    if not isinstance(value.search, str):
        raise ValueError("invalid_filter")
    search = value.search.strip()
    if len(search) > 128:
        raise ValueError("invalid_filter")
    if (
        value.status not in ALLOWED_STATUSES
        or value.referenced not in ALLOWED_REFERENCED
    ):
        raise ValueError("invalid_filter")
    return ElementQuery(
        page=value.page,
        page_size=value.page_size,
        search=search,
        status=value.status,
        referenced=value.referenced,
    )


def _element_record(row: object) -> ElementRecord:
    if not isinstance(row, Mapping):
        try:
            row = dict(row)
        except (TypeError, ValueError) as error:
            raise RuntimeError("managed element row is corrupt") from error
    last_validated_at = row.get("last_validated_at")
    return ElementRecord(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        status=str(row["status"]),
        page_key=str(row["page_key"]),
        primary_locator_type=str(row["primary_locator_type"]),
        dependency_count=int(row["dependency_count"]),
        last_validated_at=(
            str(last_validated_at)
            if last_validated_at is not None
            else None
        ),
        revision=int(row["revision"]),
    )


def _element_id(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("element_id is invalid")
    selected = value.strip()
    if not _valid_element_id(selected):
        raise ValueError("element_id is invalid")
    return selected


def _valid_element_id(value: str) -> bool:
    return (
        1 <= len(value) <= 128
        and "\x00" not in value
        and all(ord(character) >= 32 for character in value)
    )


def _display_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("display_name is invalid")
    selected = " ".join(value.replace("\x00", "").split())
    if not 1 <= len(selected) <= 120:
        raise ValueError("display_name is invalid")
    return selected


def _normalized_create_payload(
    payload: object,
    allowed_target_origins: frozenset[str],
) -> tuple[str, dict[str, object]]:
    if not isinstance(payload, Mapping) or set(payload) != CREATE_FIELDS:
        raise ValueError("element payload has an invalid parameter shape")
    display_name = _display_name(payload["display_name"])
    definition = _normalized_definition(
        {key: payload[key] for key in DEFINITION_FIELDS},
        allowed_target_origins,
    )
    return display_name, definition


def _normalized_definition(
    value: object,
    allowed_target_origins: frozenset[str],
) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != DEFINITION_FIELDS:
        raise ValueError("definition has an invalid parameter shape")
    page_key = _page_key(value["page_key"])
    target_origin = _target_origin(
        value["target_origin"],
        allowed_target_origins,
    )
    url_pattern = _url_pattern(value["url_pattern"], target_origin)
    operation_steps = _operation_steps(
        value["operation_steps"],
        target_origin,
    )
    fingerprint = _bounded_json_object(
        value["fingerprint"],
        "fingerprint",
        MAX_FINGERPRINT_JSON_BYTES,
    )
    locators = _locators(value["locators"])
    definition = {
        "page_key": page_key,
        "target_origin": target_origin,
        "url_pattern": url_pattern,
        "operation_steps": operation_steps,
        "fingerprint": fingerprint,
        "locators": locators,
    }
    _bounded_json_object(
        definition,
        "definition",
        MAX_DEFINITION_JSON_BYTES,
    )
    return definition


def _page_key(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("page_key is invalid")
    selected = value.strip()
    if _PAGE_KEY_RE.fullmatch(selected) is None:
        raise ValueError("page_key is invalid")
    return selected


def _allowed_origins(value: object) -> frozenset[str]:
    if value is None:
        return _DEFAULT_TARGET_ORIGINS
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or not value
    ):
        raise ValueError("allowed_target_origins is invalid")
    origins: set[str] = set()
    for raw in value:
        selected = _canonical_https_origin(raw)
        hostname = urlsplit(selected).hostname or ""
        if hostname != "tiktok.com" and not hostname.endswith(".tiktok.com"):
            raise ValueError("allowed_target_origins is invalid")
        origins.add(selected)
    return frozenset(origins)


def _target_origin(
    value: object,
    allowed_target_origins: frozenset[str],
) -> str:
    try:
        selected = _canonical_https_origin(value)
    except ValueError as error:
        raise ValueError("target_origin is invalid") from error
    if selected not in allowed_target_origins:
        raise ValueError("target_origin is invalid")
    return selected


def _canonical_https_origin(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 255:
        raise ValueError("origin is invalid")
    if any(ord(character) < 33 for character in value):
        raise ValueError("origin is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("origin is invalid") from error
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin is invalid")
    hostname = parsed.hostname.casefold()
    host = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        host = f"{host}:{port}"
    return f"https://{host}"


def _url_pattern(value: object, target_origin: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_URL_PATTERN_LENGTH
        or any(ord(character) < 33 for character in value)
    ):
        raise ValueError("url_pattern is invalid")
    try:
        parsed = urlsplit(value)
        origin = _canonical_https_origin(
            f"{parsed.scheme}://{parsed.netloc}"
        )
    except (TypeError, ValueError) as error:
        raise ValueError("url_pattern is invalid") from error
    if (
        origin != target_origin
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("url_pattern is invalid")
    return value


def _operation_steps(
    value: object,
    target_origin: str,
) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > MAX_OPERATION_STEPS:
        raise ValueError("operation_steps is invalid")
    result: list[dict[str, object]] = []
    for expected_sequence, raw in enumerate(value, start=1):
        if (
            not isinstance(raw, Mapping)
            or not {"sequence", "locator"} <= set(raw)
            or not set(raw) <= _STEP_FIELDS
        ):
            raise ValueError("operation_steps is invalid")
        try:
            normalized = normalize_recorded_step(raw)
        except ValueError as error:
            raise ValueError("operation_steps is invalid") from error
        if normalized["sequence"] != expected_sequence:
            raise ValueError("operation_steps is invalid")
        for url_key in ("url_before", "url_after"):
            selected_url = str(normalized[url_key])
            if selected_url and not _url_has_origin(
                selected_url,
                target_origin,
            ):
                raise ValueError("operation_steps is invalid")
        result.append(normalized)
    return result


def _url_has_origin(value: str, target_origin: str) -> bool:
    try:
        parsed = urlsplit(value)
        selected = _canonical_https_origin(
            f"{parsed.scheme}://{parsed.netloc}"
        )
    except (TypeError, ValueError):
        return False
    return selected == target_origin


def _locators(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_LOCATORS:
        raise ValueError("locators is invalid")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != {"type", "value"}:
            raise ValueError("locators is invalid")
        try:
            normalized = normalize_recorded_step(
                {"sequence": 1, "locator": raw}
            )["locator"]
        except ValueError as error:
            raise ValueError("locators is invalid") from error
        locator = {
            "type": str(normalized["type"]),
            "value": str(normalized["value"]),
        }
        key = (locator["type"], locator["value"])
        if key in seen:
            raise ValueError("locators is invalid")
        seen.add(key)
        result.append(locator)
    return result


def _bounded_json_object(
    value: object,
    name: str,
    maximum_bytes: int,
    *,
    error_type: type[Exception] = ValueError,
) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise error_type(f"{name} is invalid")
    try:
        encoded = json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, OverflowError) as error:
        raise error_type(f"{name} is invalid") from error
    if len(encoded) > maximum_bytes:
        raise error_type(f"{name} is invalid")
    try:
        decoded = json.loads(encoded)
    except (RecursionError, TypeError, ValueError) as error:
        raise error_type(f"{name} is invalid") from error
    if not isinstance(decoded, dict) or not _finite_json(decoded):
        raise error_type(f"{name} is invalid")
    return decoded


def _finite_json(value: object) -> bool:
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str) and _finite_json(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_finite_json(item) for item in value)
    return value is None or isinstance(value, (str, int, bool))


def _positive_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("expected_revision is invalid")
    return value


def _required_record(
    catalog: ElementCatalog,
    element_id: str,
) -> ElementRecord:
    record = catalog.get(element_id)
    if record is None:
        raise RuntimeError("managed element mutation was not persisted")
    return record


__all__ = [
    "ALLOWED_PAGE_SIZES",
    "ALLOWED_STATUSES",
    "CREATE_FIELDS",
    "DEFINITION_FIELDS",
    "ELEMENT_STATUSES",
    "ElementCatalog",
    "ElementQuery",
    "MAX_DEFINITION_JSON_BYTES",
    "MAX_LOCATORS",
    "MAX_OPERATION_STEPS",
    "MAX_SQLITE_INTEGER",
    "PageResult",
]
