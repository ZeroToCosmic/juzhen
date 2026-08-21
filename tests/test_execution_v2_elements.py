import json

import pytest

from execution_v2.elements import ElementInUseError, ElementValidationError, normalize_element_definition
from execution_v2.store import ExecutionStore


def valid_definition(**changes):
    definition = {
        "url_pattern": "https://www.tiktok.com/*",
        "frame_path": [],
        "locators": [
            {"type": "css", "value": "[data-e2e='comment-icon']", "priority": 10},
            {"type": "xpath", "value": "//button[@aria-label='Open comments']", "priority": 60},
        ],
        "diagnostic_metadata": {"tag": "button", "text": ""},
        "screenshot_path": "artifacts/picker/session-1/comment.png",
    }
    definition.update(changes)
    return definition


@pytest.mark.parametrize(
    ("definition", "message"),
    [
        ({"url_pattern": "https://www.tiktok.com/*"}, "definition keys"),
        (valid_definition(url_pattern="http://www.tiktok.com/*"), "url_pattern"),
        (valid_definition(url_pattern="https://www.tiktok.com/*#fragment"), "url_pattern"),
        (valid_definition(frame_path=[""]), "frame_path"),
        (valid_definition(locators=[]), "locators"),
        (valid_definition(locators=[{"type": "css", "value": ".x"}]), "locator keys"),
        (valid_definition(locators=[{"type": "text", "value": "Comment", "priority": 1}]), "locator type"),
        (valid_definition(locators=[{"type": "role", "role": "button", "name": "", "priority": 1}]), "locator name"),
    ],
)
def test_normalize_element_definition_rejects_invalid_schema(definition, message):
    with pytest.raises(ElementValidationError, match=message):
        normalize_element_definition(definition)


def test_normalize_element_definition_returns_closed_canonical_copy():
    normalized = normalize_element_definition(valid_definition())

    assert normalized == valid_definition()
    assert normalized is not valid_definition()


def test_element_create_rename_repick_status_and_persistence(tmp_path):
    path = tmp_path / "execution_v2.db"
    store = ExecutionStore(path)
    store.initialize()

    created = store.create_element(
        "element-1", "评论入口", "action", "click", valid_definition()
    )
    assert created["revision"] == 1
    assert created["status"] == "active"
    assert created["definition"] == valid_definition()
    assert store.count_element_references("element-1") == 0

    renamed = store.rename_element(
        "element-1", "打开评论", expected_revision=created["revision"]
    )
    assert renamed["name"] == "打开评论"
    assert renamed["revision"] == 1

    repicked_definition = valid_definition(
        locators=[{"type": "css", "value": "[data-e2e='comment-button']", "priority": 10}]
    )
    repicked = store.repick_element(
        "element-1", repicked_definition, expected_revision=renamed["revision"]
    )
    assert repicked["id"] == "element-1"
    assert repicked["revision"] == 2
    assert repicked["status"] == "active"
    assert repicked["definition"] == repicked_definition

    disabled = store.set_element_status(
        "element-1", "disabled", expected_revision=repicked["revision"]
    )
    assert disabled["status"] == "disabled"
    assert disabled["revision"] == 2

    reopened = ExecutionStore(path)
    reopened.initialize()
    assert reopened.get_element("element-1")["name"] == "打开评论"
    assert reopened.list_elements()[0]["revision"] == 2
    with reopened.connect() as connection:
        revisions = connection.execute(
            "SELECT revision, definition_json FROM element_revisions WHERE element_id = ? ORDER BY revision",
            ("element-1",),
        ).fetchall()
    assert [row["revision"] for row in revisions] == [1, 2]
    assert json.loads(revisions[1]["definition_json"]) == repicked_definition


def test_element_create_rejects_invalid_enums_and_stale_repick(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()

    with pytest.raises(ElementValidationError, match="purpose"):
        store.create_element("element-1", "Name", "other", "click", valid_definition())
    with pytest.raises(ElementValidationError, match="kind"):
        store.create_element("element-1", "Name", "action", "other", valid_definition())
    with pytest.raises(ElementValidationError, match="status"):
        store.create_element(
            "element-1", "Name", "action", "click", valid_definition(), status="other"
        )

    created = store.create_element("element-1", "Name", "action", "click", valid_definition())
    with pytest.raises(Exception, match="revision"):
        store.repick_element("element-1", valid_definition(), expected_revision=0)
    assert store.get_element("element-1")["revision"] == created["revision"]


def test_referenced_element_cannot_be_deleted_and_invalid_action_json_is_ignored(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    created = store.create_element("element-1", "Name", "action", "click", valid_definition())

    with store.connect() as connection:
        connection.execute(
            "INSERT INTO strategies (id, name, enabled, revision, definition_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("strategy-1", "Strategy", 1, 1, "{}", created["created_at"], created["updated_at"]),
        )
        connection.execute(
            "INSERT INTO strategy_actions (strategy_id, position, action_json) VALUES (?, ?, ?)",
            ("strategy-1", 1, "not-json"),
        )
        connection.execute(
            "INSERT INTO strategy_actions (strategy_id, position, action_json) VALUES (?, ?, ?)",
            ("strategy-1", 2, json.dumps({"type": "click", "element_id": "element-1"})),
        )

    assert store.count_element_references("element-1") == 1
    with pytest.raises(ElementInUseError, match="element-1"):
        store.delete_element("element-1", expected_revision=created["revision"])


def test_unreferenced_element_deletes_its_revision_history(tmp_path):
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    created = store.create_element("element-1", "Name", "action", "click", valid_definition())

    store.delete_element("element-1", expected_revision=created["revision"])

    assert store.get_element("element-1") is None
    with store.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM element_revisions WHERE element_id = ?", ("element-1",)
        ).fetchone()[0] == 0
