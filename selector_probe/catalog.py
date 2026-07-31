"""Paginated selector element catalog backed by durable projections."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
import secrets

from browser_element_schema import (
    ELEMENT_SCOPES,
    normalize_element_definitions,
)
from selector_probe.contracts import normalize_contracts

from .view_models import ElementRecord


ALLOWED_PAGE_SIZES = frozenset({20, 50, 100})
MAX_SQLITE_INTEGER = (1 << 63) - 1
ALLOWED_STATUSES = frozenset(
    {
        "all",
        "healthy",
        "using_lkg",
        "draft",
        "failed",
        "probe_unavailable",
        "disabled",
    }
)
ALLOWED_SOURCES = frozenset(
    {"all", "automatic", "legacy_manual", "disabled"}
)
ALLOWED_REFERENCED = frozenset({"all", "yes", "no"})
CREATE_FIELDS = frozenset(
    {
        "display_name",
        "intent",
        "required_state",
        "scope",
        "probe_action",
        "accepted_roles",
        "accepted_names",
        "name_mode",
        "preferred_attributes",
        "postcondition",
    }
)
_CREATE_REQUIRED_FIELDS = frozenset(
    {
        "display_name",
        "intent",
        "required_state",
        "scope",
        "probe_action",
    }
)


@dataclass(frozen=True)
class ElementQuery:
    page: int = 1
    page_size: int = 20
    search: str = ""
    status: str = "all"
    source: str = "all"
    scope: str = "all"
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
        legacy_elements_provider: Callable[[], object] | None = None,
        element_id_factory: Callable[[], str] | None = None,
        site: str = "tiktok",
        environment: str = "production",
    ):
        self.store = store
        self.legacy_elements_provider = legacy_elements_provider
        self.element_id_factory = element_id_factory or (
            lambda: "element-" + secrets.token_hex(8)
        )
        self.site = str(site)
        self.environment = str(environment)

    def list(self, query: ElementQuery) -> PageResult:
        selected = _validated_query(query)
        legacy = self._legacy_records(selected)
        if legacy:
            requested = min(
                selected.page * selected.page_size,
                MAX_SQLITE_INTEGER,
            )
            rows, managed_total, revision = (
                self.store.list_managed_element_rows(
                    page=1,
                    page_size=requested,
                    search=selected.search,
                    status=selected.status,
                    source=selected.source,
                    scope=selected.scope,
                    referenced=selected.referenced,
                )
            )
            records = [
                *(_element_record(row) for row in rows),
                *legacy,
            ]
            records.sort(key=_element_priority)
            offset = (selected.page - 1) * selected.page_size
            return PageResult(
                items=tuple(records[offset : offset + selected.page_size]),
                page=selected.page,
                page_size=selected.page_size,
                total=managed_total + len(legacy),
                revision=revision,
            )
        rows, total, revision = self.store.list_managed_element_rows(
            page=selected.page,
            page_size=selected.page_size,
            search=selected.search,
            status=selected.status,
            source=selected.source,
            scope=selected.scope,
            referenced=selected.referenced,
        )
        return PageResult(
            items=tuple(_element_record(row) for row in rows),
            page=selected.page,
            page_size=selected.page_size,
            total=total,
            revision=revision,
        )

    def get(self, element_id: str) -> ElementRecord | None:
        selected_id = _element_id(element_id)
        row = self.store.get_managed_element_row(selected_id)
        if row is not None:
            return _element_record(row)
        return next(
            (
                record
                for record in self._legacy_records(ElementQuery())
                if record.id == selected_id
            ),
            None,
        )

    def draft(self, element_id: str) -> dict[str, object] | None:
        selected_id = _element_id(element_id)
        row = self.store.managed_element_draft_row(selected_id)
        if row is None:
            return None
        return {
            "contract": _decoded_json_object(
                row["contract_json"],
                "element draft contract",
            ),
            "candidates": _decoded_json_array(
                row["candidates_json"],
                "element draft candidates",
            ),
            "validation": _decoded_json_object(
                row["validation_json"],
                "element draft validation",
            ),
            "base_version_id": str(row["base_version_id"]),
            "revision": int(row["revision"]),
        }

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

    def _legacy_records(
        self,
        query: ElementQuery,
    ) -> tuple[ElementRecord, ...]:
        if not callable(self.legacy_elements_provider):
            return ()
        raw = self.legacy_elements_provider()
        if not isinstance(raw, Mapping):
            return ()
        managed_ids_loader = getattr(self.store, "managed_element_ids", None)
        managed_ids = (
            set(managed_ids_loader())
            if callable(managed_ids_loader)
            else set()
        )
        aliases = [
            alias
            for alias in raw
            if isinstance(alias, str)
            and _valid_element_id(alias)
            and alias not in managed_ids
        ]
        dependencies = self.store.dependency_rows_for_aliases(aliases)
        dependency_map: dict[str, set[str]] = {}
        searchable_dependencies: dict[str, list[str]] = {}
        for row in dependencies:
            alias = str(row["alias"])
            dependency_map.setdefault(alias, set()).add(
                str(row["strategy_id"])
            )
            searchable_dependencies.setdefault(alias, []).extend(
                (
                    str(row["strategy_id"]),
                    str(row["strategy_name"]),
                )
            )
        records: list[ElementRecord] = []
        for alias in aliases:
            try:
                definition = normalize_element_definitions(
                    {alias: raw[alias]}
                )[alias]
            except (TypeError, ValueError):
                continue
            record = ElementRecord(
                id=alias,
                display_name=alias,
                management_source="legacy_manual",
                published_status="probe_unavailable",
                draft_status=None,
                scope=str(definition["scope"]),
                primary_locator_type=str(
                    definition["locators"][0]["type"]
                    if definition["locators"]
                    else ""
                ),
                dependency_count=len(dependency_map.get(alias, set())),
                last_validated_at=None,
                revision=0,
                migration_available=True,
            )
            if not _legacy_matches(
                record,
                query,
                searchable_dependencies.get(alias, []),
            ):
                continue
            records.append(record)
        return tuple(records)

    def create_draft(
        self,
        payload: object,
        actor_user_id: int,
        actor_username: str = "unknown",
    ) -> ElementRecord:
        element_id = _element_id(self.element_id_factory())
        display_name, contract = _normalized_create_payload(
            element_id,
            payload,
        )
        self.store.create_managed_element_draft(
            element_id=element_id,
            display_name=display_name,
            contract=contract,
            scope=str(contract["scope"]),
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return _required_record(self, element_id)

    def update_draft(
        self,
        element_id: str,
        payload: object,
        expected_revision: int,
        actor_user_id: int,
        actor_username: str = "unknown",
    ) -> ElementRecord:
        selected_id = _element_id(element_id)
        contract = _normalized_update_payload(selected_id, payload)
        self.store.update_managed_element_draft(
            element_id=selected_id,
            contract=contract,
            scope=str(contract["scope"]),
            expected_revision=expected_revision,
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
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )

    def create_legacy_migration(
        self,
        element_id: str,
        actor_user_id: int,
        actor_username: str = "unknown",
        *,
        expected_revision: int,
    ) -> ElementRecord:
        selected_id = _element_id(element_id)
        if not callable(self.legacy_elements_provider):
            raise RuntimeError("legacy element provider is unavailable")
        raw_definitions = self.legacy_elements_provider()
        if (
            not isinstance(raw_definitions, Mapping)
            or selected_id not in raw_definitions
        ):
            from selector_probe.store import ElementNotFoundError

            raise ElementNotFoundError(selected_id)
        definition = normalize_element_definitions(
            {selected_id: raw_definitions[selected_id]}
        )[selected_id]
        self.store.migrate_legacy_element(
            element_id=selected_id,
            display_name=selected_id,
            definition=definition,
            expected_revision=expected_revision,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
        )
        return _required_record(self, selected_id)

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
        or value.source not in ALLOWED_SOURCES
        or value.scope not in {"all", *ELEMENT_SCOPES}
        or value.referenced not in ALLOWED_REFERENCED
    ):
        raise ValueError("invalid_filter")
    return ElementQuery(
        page=value.page,
        page_size=value.page_size,
        search=search,
        status=value.status,
        source=value.source,
        scope=value.scope,
        referenced=value.referenced,
    )


def _element_record(row: object) -> ElementRecord:
    return ElementRecord(
        id=str(row["id"]),
        display_name=str(row["display_name"]),
        management_source=str(row["management_source"]),
        published_status=str(row["published_status"]),
        draft_status=(
            str(row["draft_status"])
            if row["draft_status"] is not None
            else None
        ),
        scope=str(row["scope"]),
        primary_locator_type=str(row["primary_locator_type"]),
        dependency_count=int(row["dependency_count"]),
        last_validated_at=(
            str(row["last_validated_at"])
            if row["last_validated_at"] is not None
            else None
        ),
        revision=int(row["revision"]),
    )


def _legacy_matches(
    record: ElementRecord,
    query: ElementQuery,
    dependency_text: list[str],
) -> bool:
    if query.status not in {"all", record.runtime_status}:
        return False
    if query.source not in {"all", record.management_source}:
        return False
    if query.scope not in {"all", record.scope}:
        return False
    if query.referenced == "yes" and record.dependency_count == 0:
        return False
    if query.referenced == "no" and record.dependency_count > 0:
        return False
    if query.search:
        needle = query.search.casefold()
        searchable = (
            record.id,
            record.display_name,
            *dependency_text,
        )
        if not any(needle in value.casefold() for value in searchable):
            return False
    return True


def _element_priority(record: ElementRecord) -> tuple[object, ...]:
    if record.published_status == "failed":
        priority = 1
    elif record.published_status == "using_lkg":
        priority = 2
    elif record.draft_status is not None:
        priority = 3
    elif record.published_status == "probe_unavailable":
        priority = 4
    else:
        priority = 5
    return (
        priority,
        0 if record.last_validated_at is None else 1,
        record.last_validated_at or "",
        record.display_name,
        record.id,
    )


def _element_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("element_id is invalid")
    return value


def _valid_element_id(value: object) -> bool:
    try:
        _element_id(value)
    except ValueError:
        return False
    return True


def _display_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("display_name is invalid")
    selected = " ".join(value.split())
    if not selected or len(selected) > 128:
        raise ValueError("display_name is invalid")
    return selected


def _normalized_create_payload(
    element_id: str,
    payload: object,
) -> tuple[str, dict[str, object]]:
    if not isinstance(payload, Mapping):
        raise ValueError("element payload must be an object")
    fields = set(payload)
    if (
        not _CREATE_REQUIRED_FIELDS <= fields
        or not fields <= CREATE_FIELDS
    ):
        raise ValueError("element payload has an invalid parameter shape")
    display_name = _display_name(payload["display_name"])
    contract = {
        "intent": payload["intent"],
        "required_state": payload["required_state"],
        "scope": payload["scope"],
        "accepted_roles": payload.get("accepted_roles", ["button"]),
        "accepted_names": {
            "mode": payload.get("name_mode", "exact"),
            "values": payload.get("accepted_names", [display_name]),
        },
        "preferred_attributes": payload.get(
            "preferred_attributes",
            ["data-e2e", "aria-label"],
        ),
        "postcondition": payload.get("postcondition", ""),
        "probe_action": payload["probe_action"],
    }
    normalized = normalize_contracts({element_id: contract})[element_id]
    return display_name, normalized.public_dict()


def _normalized_update_payload(
    element_id: str,
    payload: object,
) -> dict[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != {"contract"}:
        raise ValueError("draft payload has an invalid parameter shape")
    normalized = normalize_contracts(
        {element_id: payload["contract"]}
    )[element_id]
    return normalized.public_dict()


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


def _decoded_json_object(value: object, name: str) -> dict[str, object]:
    decoded = _decoded_json(value, name)
    if not isinstance(decoded, dict):
        raise RuntimeError(f"{name} is corrupt")
    return decoded


def _decoded_json_array(value: object, name: str) -> list[object]:
    decoded = _decoded_json(value, name)
    if not isinstance(decoded, list):
        raise RuntimeError(f"{name} is corrupt")
    return decoded


def _decoded_json(value: object, name: str) -> object:
    if not isinstance(value, str):
        raise RuntimeError(f"{name} is corrupt")
    try:
        return json.loads(value)
    except (RecursionError, TypeError, ValueError) as error:
        raise RuntimeError(f"{name} is corrupt") from error


__all__ = [
    "ALLOWED_PAGE_SIZES",
    "ALLOWED_STATUSES",
    "CREATE_FIELDS",
    "ElementCatalog",
    "ElementQuery",
    "MAX_SQLITE_INTEGER",
    "PageResult",
]
