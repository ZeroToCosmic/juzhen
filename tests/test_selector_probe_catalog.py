from __future__ import annotations

import json

import pytest

from selector_probe.catalog import (
    CREATE_FIELDS,
    ElementCatalog,
    ElementQuery,
)
from selector_probe.store import (
    ElementHasDependenciesError,
    SelectorProbeStore,
    StaleElementRevisionError,
)


def _manual_payload(**changes):
    payload = {
        "display_name": "Comment input",
        "page_key": "comment-panel",
        "target_origin": "https://www.tiktok.com",
        "url_pattern": "https://www.tiktok.com/*",
        "operation_steps": [],
        "fingerprint": {
            "tag": "div",
            "attributes": {"data-e2e": "comment-input"},
        },
        "locators": [
            {
                "type": "css",
                "value": '[data-e2e="comment-input"]',
            }
        ],
    }
    payload.update(changes)
    return payload


@pytest.fixture
def catalog_store(tmp_path):
    with SelectorProbeStore(tmp_path / "selector-probe.db") as store:
        yield store


def test_catalog_create_fields_are_manual_definition_only():
    assert CREATE_FIELDS == {
        "display_name",
        "page_key",
        "target_origin",
        "url_pattern",
        "operation_steps",
        "fingerprint",
        "locators",
    }


def test_catalog_creates_manual_draft_and_audits_actor(catalog_store):
    catalog = ElementCatalog(
        catalog_store,
        element_id_factory=lambda: "element-fixed",
    )

    record = catalog.create_draft(
        _manual_payload(display_name="  Comment   input  "),
        actor_user_id=41,
        actor_username="admin-user",
    )
    definition = catalog.draft(record.id)
    audit = catalog_store.connection.execute(
        """
        SELECT event_type, actor_user_id, actor_username, target_id
        FROM selector_management_audit_events
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()

    assert record.id == "element-fixed"
    assert record.display_name == "Comment input"
    assert record.status == "draft"
    assert record.page_key == "comment-panel"
    assert definition == {
        "page_key": "comment-panel",
        "target_origin": "https://www.tiktok.com",
        "url_pattern": "https://www.tiktok.com/*",
        "operation_steps": [],
        "fingerprint": {
            "tag": "div",
            "attributes": {"data-e2e": "comment-input"},
        },
        "locators": [
            {
                "type": "css",
                "value": '[data-e2e="comment-input"]',
            }
        ],
    }
    assert tuple(audit) == (
        "element_created",
        41,
        "admin-user",
        "element-fixed",
    )


def test_catalog_preserves_position_hint_inside_display_fingerprint(
    catalog_store,
):
    catalog = ElementCatalog(catalog_store)
    record = catalog.create_draft(
        _manual_payload(
            fingerprint={
                "tag": "button",
                "role": "button",
                "name": "Comments",
                "position_hint": {
                    "x": 0.8,
                    "y": 0.4,
                    "width": 0.1,
                    "height": 0.1,
                },
            }
        ),
        7,
        "admin",
    )

    definition = catalog.draft(record.id)

    assert definition["fingerprint"]["position_hint"]["x"] == 0.8
    assert definition["fingerprint"]["role"] == "button"
    assert definition["locators"] == [
        {"type": "css", "value": '[data-e2e="comment-input"]'}
    ]


def test_catalog_update_name_changes_name_only(catalog_store):
    catalog = ElementCatalog(
        catalog_store,
        element_id_factory=lambda: "element-name",
    )
    created = catalog.create_draft(_manual_payload(), 7, "admin")
    before = catalog.draft(created.id)

    renamed = catalog.update_name(
        created.id,
        "  Primary   comment field  ",
        created.revision,
        7,
        "admin",
    )

    assert renamed.display_name == "Primary comment field"
    assert renamed.revision == created.revision + 1
    assert catalog.draft(created.id) == before


def test_catalog_rebind_replaces_definition_and_returns_draft(
    catalog_store,
):
    catalog = ElementCatalog(
        catalog_store,
        element_id_factory=lambda: "element-rebind",
    )
    created = catalog.create_draft(_manual_payload(), 7, "admin")
    replacement = _manual_payload(
        display_name="Ignored by rebind",
        page_key="feed",
        url_pattern="https://www.tiktok.com/foryou*",
        fingerprint={"tag": "button", "position_hint": {"x": 0.9}},
        locators=[{"type": "xpath", "value": "//*[@data-e2e='comment-icon']"}],
    )

    rebound = catalog.rebind(
        created.id,
        {key: value for key, value in replacement.items() if key != "display_name"},
        created.revision,
        7,
        "admin",
    )

    assert rebound.display_name == created.display_name
    assert rebound.status == "draft"
    assert rebound.page_key == "feed"
    assert rebound.revision == created.revision + 1
    assert catalog.draft(created.id)["locators"][0]["type"] == "xpath"


@pytest.mark.parametrize(
    "changes",
    [
        {"intent": "find comments"},
        {"accepted_roles": ["button"]},
        {"accepted_names": ["Comments"]},
        {"scope": "active_video"},
        {"probe_action": "inspect_only"},
        {"xpath": "/html/body/button"},
    ],
)
def test_catalog_rejects_semantic_unknown_and_unsafe_create_fields(
    catalog_store,
    changes,
):
    catalog = ElementCatalog(
        catalog_store,
        element_id_factory=lambda: "element-unsafe",
    )

    with pytest.raises(ValueError, match="invalid parameter shape"):
        catalog.create_draft({**_manual_payload(), **changes}, 7, "admin")

    assert catalog.get("element-unsafe") is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"target_origin": "http://www.tiktok.com"}, "target_origin"),
        ({"target_origin": "https://example.com"}, "target_origin"),
        ({"target_origin": "https://user@www.tiktok.com"}, "target_origin"),
        ({"target_origin": "https://www.tiktok.com/path"}, "target_origin"),
        ({"url_pattern": "https://example.com/*"}, "url_pattern"),
        ({"url_pattern": "https://www.tiktok.com/*#secret"}, "url_pattern"),
        ({"page_key": "bad page key"}, "page_key"),
        ({"operation_steps": [{}] * 21}, "operation_steps"),
        (
            {
                "locators": [
                    {"type": "css", "value": f'[data-e2e="item-{index}"]'}
                    for index in range(7)
                ]
            },
            "locators",
        ),
        ({"locators": [{"type": "role", "value": "button"}]}, "locators"),
        ({"locators": [{"type": "xpath", "value": "/html/body/button"}]}, "locators"),
        ({"locators": [{"type": "css", "value": "button.primary"}]}, "locators"),
        ({"fingerprint": {"text": "x" * 70000}}, "fingerprint"),
    ],
)
def test_catalog_rejects_invalid_manual_definition(
    catalog_store,
    changes,
    message,
):
    catalog = ElementCatalog(catalog_store)

    with pytest.raises(ValueError, match=message):
        catalog.create_draft(_manual_payload(**changes), 7, "admin")


def test_catalog_normalizes_at_most_twenty_replay_steps(catalog_store):
    steps = [
        {
            "sequence": index,
            "locator": {
                "type": "css",
                "value": f'[data-e2e="step-{index}"]',
            },
            "url_before": "https://www.tiktok.com/foryou?private=1",
            "url_after": "https://www.tiktok.com/foryou#comments",
            "recorded_at": "2026-08-04T03:00:00+00:00",
            "frame_key": "main",
            "shadow": False,
            "shadow_key": "",
        }
        for index in range(1, 21)
    ]
    catalog = ElementCatalog(catalog_store)

    record = catalog.create_draft(
        _manual_payload(operation_steps=steps),
        7,
        "admin",
    )

    saved = catalog.draft(record.id)["operation_steps"]
    assert len(saved) == 20
    assert saved[0]["sequence"] == 1
    assert saved[0]["url_before"] == "https://www.tiktok.com/foryou"
    assert saved[-1]["sequence"] == 20


def test_catalog_revision_and_dependency_guards(catalog_store):
    catalog = ElementCatalog(
        catalog_store,
        element_id_factory=lambda: "element-guarded",
    )
    created = catalog.create_draft(_manual_payload(), 7, "admin")

    with pytest.raises(StaleElementRevisionError):
        catalog.update_name(
            created.id,
            "Changed",
            created.revision + 1,
            7,
            "admin",
        )

    catalog_store.replace_strategy_dependencies(
        [
            (
                created.id,
                "comment-flow",
                "open-comments",
                "click",
                "Comment flow",
            )
        ]
    )
    with pytest.raises(ElementHasDependenciesError):
        catalog.delete(created.id, created.revision, 7, "admin")

    assert catalog.get(created.id) is not None


def test_catalog_lists_and_filters_new_statuses(catalog_store):
    catalog = ElementCatalog(
        catalog_store,
        element_id_factory=iter(("element-a", "element-b")).__next__,
    )
    catalog.create_draft(
        _manual_payload(display_name="Alpha"),
        7,
        "admin",
    )
    catalog.create_draft(
        _manual_payload(display_name="Beta"),
        7,
        "admin",
    )

    result = catalog.list(ElementQuery(status="draft", search="Alpha"))

    assert result.total == 1
    assert [item.display_name for item in result.items] == ["Alpha"]
    assert result.revision >= 2


@pytest.mark.parametrize(
    "query",
    [
        ElementQuery(page=0),
        ElementQuery(page=True),
        ElementQuery(page_size=21),
        ElementQuery(status="failed"),
        ElementQuery(referenced="missing"),
    ],
)
def test_catalog_rejects_invalid_queries(catalog_store, query):
    with pytest.raises(ValueError, match="invalid_pagination|invalid_filter"):
        ElementCatalog(catalog_store).list(query)


def test_catalog_definition_is_bounded_json(catalog_store):
    catalog = ElementCatalog(catalog_store)
    payload = _manual_payload()

    record = catalog.create_draft(payload, 7, "admin")
    serialized = json.dumps(
        catalog.draft(record.id),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(serialized) < 65536
