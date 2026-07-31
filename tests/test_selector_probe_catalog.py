from __future__ import annotations

import sqlite3
import json

import pytest

from selector_probe.catalog import ElementCatalog, ElementQuery
from browser_element_schema import TIKTOK_COMMENT_TEMPLATE
from selector_probe.contracts import default_tiktok_contracts
from selector_probe.store import (
    ElementHasDependenciesError,
    ElementMigrationConflictError,
    SelectorProbeStore,
    StaleElementRevisionError,
)


def test_seed_legacy_comment_elements_is_idempotent(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        assert store.seed_legacy_elements(
            TIKTOK_COMMENT_TEMPLATE,
            default_tiktok_contracts(),
        ) == 3
        assert store.seed_legacy_elements(
            TIKTOK_COMMENT_TEMPLATE,
            default_tiktok_contracts(),
        ) == 0
        rows = store.connection.execute(
            """
            SELECT id, management_source, published_status, draft_status,
                   scope
            FROM managed_elements ORDER BY id
            """
        ).fetchall()
        drafts = store.connection.execute(
            "SELECT element_id, candidates_json FROM element_drafts"
        ).fetchall()

    assert len(rows) == 3
    assert {row["management_source"] for row in rows} == {"legacy_manual"}
    assert {row["published_status"] for row in rows} == {"using_lkg"}
    assert {row["draft_status"] for row in rows} == {"draft"}
    assert {row["scope"] for row in rows} == {
        "active_video",
        "visible_comment_panel",
    }
    assert all(json.loads(row["candidates_json"]) for row in drafts)


def _add_element(
    store,
    element_id,
    *,
    published_status="healthy",
    draft_status=None,
    source="automatic",
    scope="active_video",
):
    store.upsert_managed_element_projection(
        element_id=element_id,
        display_name=element_id.replace("-", " ").title(),
        management_source=source,
        published_status=published_status,
        draft_status=draft_status,
        active_version_id="sel-active",
        scope=scope,
        primary_locator_type="attribute",
        last_validated_at="2026-07-29T03:00:00+00:00",
        actor_user_id=7,
        actor_username="catalog-admin",
    )


def _draft_payload(**changes):
    payload = {
        "display_name": "Share entry",
        "intent": "find the share entry for the active video",
        "required_state": "feed_ready",
        "scope": "active_video",
        "probe_action": "inspect_only",
        "accepted_roles": ["button"],
        "accepted_names": ["Share"],
        "name_mode": "exact",
        "preferred_attributes": ["data-e2e", "aria-label"],
        "postcondition": "",
    }
    payload.update(changes)
    return payload


@pytest.fixture
def catalog_store(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        _add_element(store, "failed-element", published_status="failed")
        _add_element(store, "lkg-element", published_status="using_lkg")
        _add_element(store, "draft-element", draft_status="draft")
        _add_element(
            store,
            "unavailable-element",
            published_status="probe_unavailable",
        )
        for index in range(43):
            _add_element(store, f"healthy-{index:02d}")
        store.replace_strategy_dependencies(
            [
                (
                    "healthy-00",
                    "share-flow",
                    "share-action",
                    "click",
                    "Share workflow",
                ),
                (
                    "healthy-00",
                    "share-flow",
                    "share-again",
                    "click",
                    "Share workflow",
                ),
            ]
        )
        yield store


def test_catalog_paginates_and_prioritizes_unhealthy_items(catalog_store):
    result = ElementCatalog(catalog_store).list(
        ElementQuery(page=1, page_size=20, status="all")
    )

    assert result.page == 1
    assert result.page_size == 20
    assert result.total == 47
    assert len(result.items) == 20
    assert [item.runtime_status for item in result.items[:3]] == [
        "failed",
        "using_lkg",
        "draft",
    ]
    assert result.revision >= 47


def test_catalog_legacy_merge_paging_matches_stable_global_order(
    catalog_store,
):
    with catalog_store.connection:
        catalog_store.connection.execute(
            """
            UPDATE managed_elements
            SET last_validated_at = NULL
            WHERE id IN (
                SELECT id
                FROM managed_elements
                WHERE id LIKE 'healthy-%'
                ORDER BY id DESC
                LIMIT 24
            )
            """
        )
    catalog = ElementCatalog(
        catalog_store,
        legacy_elements_provider=lambda: {
            "legacy-page-peer": {
                "scope": "active_video",
                "locators": [
                    {
                        "id": "legacy-page-peer-xpath",
                        "type": "xpath",
                        "value": "//button[@data-e2e='legacy-page-peer']",
                        "enabled": True,
                    }
                ],
            }
        },
    )

    full = catalog.list(ElementQuery(page=1, page_size=100))
    first = catalog.list(ElementQuery(page=1, page_size=20))
    second = catalog.list(ElementQuery(page=2, page_size=20))
    repeated = catalog.list(ElementQuery(page=2, page_size=20))
    paged_ids = [item.id for item in (*first.items, *second.items)]

    assert paged_ids == [item.id for item in full.items[:40]]
    assert len(paged_ids) == len(set(paged_ids)) == 40
    assert [item.id for item in second.items] == [
        item.id for item in repeated.items
    ]


def test_catalog_searches_dependency_id_and_name_and_counts_strategies(catalog_store):
    catalog = ElementCatalog(catalog_store)

    by_id = catalog.list(ElementQuery(search="share-flow"))
    by_name = catalog.list(ElementQuery(search="Share workflow"))

    assert [item.id for item in by_id.items] == ["healthy-00"]
    assert [item.id for item in by_name.items] == ["healthy-00"]
    assert by_name.items[0].dependency_count == 1


def test_catalog_escapes_like_wildcards_and_filters_references(catalog_store):
    _add_element(catalog_store, "literal-percent", source="legacy_manual")
    with catalog_store.connection:
        catalog_store.connection.execute(
            """
            UPDATE managed_elements
            SET display_name = 'Literal 100% _ marker'
            WHERE id = 'literal-percent'
            """
        )
    catalog = ElementCatalog(catalog_store)

    literal = catalog.list(ElementQuery(search="100% _"))
    referenced = catalog.list(ElementQuery(referenced="yes"))
    unreferenced = catalog.list(
        ElementQuery(
            source="legacy_manual",
            referenced="no",
        )
    )

    assert [item.id for item in literal.items] == ["literal-percent"]
    assert [item.id for item in referenced.items] == ["healthy-00"]
    assert [item.id for item in unreferenced.items] == ["literal-percent"]


@pytest.mark.parametrize(
    "query",
    [
        ElementQuery(page=0),
        ElementQuery(page=True),
        ElementQuery(page_size=21),
        ElementQuery(status="missing"),
        ElementQuery(source="missing"),
        ElementQuery(scope="missing"),
        ElementQuery(referenced="missing"),
    ],
)
def test_catalog_rejects_invalid_queries(catalog_store, query):
    with pytest.raises(ValueError, match="invalid_pagination|invalid_filter"):
        ElementCatalog(catalog_store).list(query)


def test_catalog_get_returns_immutable_record_or_none(catalog_store):
    catalog = ElementCatalog(catalog_store)

    record = catalog.get("healthy-00")

    assert record is not None
    assert record.id == "healthy-00"
    assert record.dependency_count == 1
    assert catalog.get("missing-element") is None


def test_catalog_revision_and_element_revision_advance_together(catalog_store):
    catalog = ElementCatalog(catalog_store)
    before = catalog.list(ElementQuery())
    current = catalog.get("healthy-00")

    _add_element(catalog_store, "healthy-00", published_status="using_lkg")
    after = catalog.list(ElementQuery())
    updated = catalog.get("healthy-00")

    assert after.revision == before.revision + 1
    assert updated.revision == current.revision + 1
    assert updated.runtime_status == "using_lkg"


@pytest.mark.parametrize(
    "query",
    [
        ElementQuery(page=True),
        ElementQuery(page_size=True),
        ElementQuery(page=1 << 63),
        ElementQuery(page_size=1 << 100),
    ],
)
def test_catalog_rejects_non_sqlite_pagination_before_query(query):
    class QueryMustNotRun:
        def list_managed_element_rows(self, **_kwargs):
            raise AssertionError("invalid pagination reached SQLite")

    with pytest.raises(ValueError, match=r"^invalid_pagination$"):
        ElementCatalog(QueryMustNotRun()).list(query)


def test_catalog_create_and_update_normalize_contract_and_audit_actor(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        catalog = ElementCatalog(
            store,
            element_id_factory=lambda: "element-fixed",
        )

        created = catalog.create_draft(
            _draft_payload(display_name="  Share   entry  "),
            41,
            "admin-user",
        )
        draft = catalog.draft(created.id)
        updated = catalog.update_draft(
            created.id,
            {
                "contract": {
                    **draft["contract"],
                    "intent": "find the visible share entry",
                }
            },
            created.revision,
            41,
            "admin-user",
        )
        audits = store.connection.execute(
            """
            SELECT event_type, actor_user_id, actor_username, target_id
            FROM selector_management_audit_events
            ORDER BY id
            """
        ).fetchall()

        assert created.id == "element-fixed"
        assert created.display_name == "Share entry"
        assert created.draft_status == "draft"
        assert draft["contract"]["accepted_names"] == {
            "mode": "exact",
            "values": ["Share"],
        }
        assert updated.revision == created.revision + 1
        assert [tuple(row) for row in audits] == [
            ("element_created", 41, "admin-user", "element-fixed"),
            ("element_draft_updated", 41, "admin-user", "element-fixed"),
        ]


@pytest.mark.parametrize(
    "unsafe",
    [
        {"xpath": "/html/body/button"},
        {"javascript": "javascript:alert(1)"},
        {"coordinates": {"x": 10, "y": 20}},
        {"id": "caller-controlled"},
    ],
)
def test_catalog_rejects_unknown_or_unsafe_create_fields_before_write(
    tmp_path,
    unsafe,
):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        catalog = ElementCatalog(
            store,
            element_id_factory=lambda: "element-unsafe",
        )

        with pytest.raises(ValueError, match="invalid parameter shape"):
            catalog.create_draft(
                {**_draft_payload(), **unsafe},
                41,
                "admin-user",
            )

        assert catalog.get("element-unsafe") is None
        assert store.catalog_revision() == 0


def test_catalog_update_and_delete_use_revision_and_dependency_guards(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        catalog = ElementCatalog(
            store,
            element_id_factory=lambda: "element-guarded",
        )
        created = catalog.create_draft(
            _draft_payload(),
            41,
            "admin-user",
        )
        contract = catalog.draft(created.id)["contract"]

        with pytest.raises(StaleElementRevisionError):
            catalog.update_draft(
                created.id,
                {"contract": contract},
                created.revision + 1,
                41,
                "admin-user",
            )

        store.replace_strategy_dependencies(
            [
                (
                    created.id,
                    "share-flow",
                    "open-share",
                    "click",
                    "Share flow",
                )
            ]
        )
        with pytest.raises(ElementHasDependenciesError):
            catalog.delete(
                created.id,
                created.revision,
                41,
                "admin-user",
            )

        assert catalog.get(created.id) is not None


def test_catalog_mutation_rolls_back_when_local_audit_write_fails(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        catalog = ElementCatalog(
            store,
            element_id_factory=lambda: "element-atomic",
        )
        created = catalog.create_draft(
            _draft_payload(),
            41,
            "admin-user",
        )
        original_contract = catalog.draft(created.id)["contract"]
        store.connection.execute(
            """
            CREATE TRIGGER reject_element_update_audit
            BEFORE INSERT ON selector_management_audit_events
            WHEN NEW.event_type = 'element_draft_updated'
            BEGIN
                SELECT RAISE(ABORT, 'audit unavailable');
            END
            """
        )
        store.connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="audit unavailable"):
            catalog.update_draft(
                created.id,
                {
                    "contract": {
                        **original_contract,
                        "intent": "changed intent",
                    }
                },
                created.revision,
                41,
                "admin-user",
            )

        assert catalog.get(created.id).revision == created.revision
        assert catalog.draft(created.id)["contract"] == original_contract


def test_catalog_legacy_migration_preserves_locator_and_dependencies(tmp_path):
    legacy_xpath = "/html/body/main/div[2]/button"
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        store.replace_strategy_dependencies(
            [
                (
                    "legacy-share",
                    "share-flow",
                    "open-share",
                    "click",
                    "Share flow",
                )
            ]
        )
        catalog = ElementCatalog(
            store,
            legacy_elements_provider=lambda: {
                "legacy-share": legacy_xpath
            },
        )
        before_migration = catalog.list(ElementQuery())
        legacy_record = catalog.get("legacy-share")
        dependencies_before = [
            tuple(row)
            for row in store.dependency_rows_for_aliases(["legacy-share"])
        ]

        assert before_migration.total == 1
        assert before_migration.items[0].migration_available is True
        assert legacy_record is not None
        assert legacy_record.management_source == "legacy_manual"
        assert legacy_record.draft_status is None
        assert legacy_record.revision == 0
        assert legacy_record.dependency_count == 1

        migrated = catalog.create_legacy_migration(
            "legacy-share",
            41,
            "admin-user",
            expected_revision=0,
        )

        assert migrated.management_source == "legacy_manual"
        assert migrated.migration_available is False
        assert migrated.draft_status == "draft"
        assert catalog.draft("legacy-share")["candidates"][0]["value"] == legacy_xpath
        assert [
            tuple(row)
            for row in store.dependency_rows_for_aliases(["legacy-share"])
        ] == dependencies_before
        with pytest.raises(ElementMigrationConflictError):
            catalog.create_legacy_migration(
                "legacy-share",
                41,
                "admin-user",
                expected_revision=migrated.revision,
            )
        assert (
            catalog.draft("legacy-share")["candidates"][0]["value"]
            == legacy_xpath
        )
