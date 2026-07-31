import hashlib

import pytest

from browser_element_schema import (
    TIKTOK_COMMENT_TEMPLATE,
    normalize_element_definitions,
)


def test_legacy_xpath_migrates_losslessly_and_idempotently():
    legacy = {"评论入口": "//article[@id='one-column-item-1']//button"}

    migrated = normalize_element_definitions(legacy)

    assert migrated["评论入口"]["scope"] == "page"
    assert migrated["评论入口"]["locators"][0]["type"] == "xpath"
    assert migrated["评论入口"]["locators"][0]["value"] == legacy["评论入口"]
    assert migrated["评论入口"]["locators"][0]["fallback"] is True
    assert normalize_element_definitions(migrated) == migrated


def test_legacy_xpath_preserves_leading_and_trailing_whitespace_and_uses_it_for_id():
    legacy_xpath = "  //article[@id='one-column-item-1']//button  "

    migrated = normalize_element_definitions({"评论入口": legacy_xpath})

    locator = migrated["评论入口"]["locators"][0]
    expected_digest = hashlib.sha256(
        f"评论入口\0{legacy_xpath}".encode("utf-8")
    ).hexdigest()[:16]
    assert locator["value"] == legacy_xpath
    assert locator["id"] == f"locator-{expected_digest}"
    assert normalize_element_definitions(migrated) == migrated


def test_tiktok_template_uses_scopes_and_semantic_primary_locators():
    assert TIKTOK_COMMENT_TEMPLATE["评论入口"]["scope"] == "active_video"
    assert TIKTOK_COMMENT_TEMPLATE["评论入口"]["locators"][0] == {
        "id": "tiktok-comment-entry-primary",
        "type": "attribute",
        "name": "data-e2e",
        "value": "comment-icon",
        "enabled": True,
    }
    assert TIKTOK_COMMENT_TEMPLATE["评论输入框"]["scope"] == "visible_comment_panel"
    assert TIKTOK_COMMENT_TEMPLATE["评论提交按钮"]["scope"] == "visible_comment_panel"


@pytest.mark.parametrize(
    "definition",
    [
        {"scope": "page", "locators": [{"id": "one", "type": "xpath", "value": "javascript:alert(1)", "enabled": True}]},
        {"scope": "page", "locators": [{"id": "one", "type": "role", "role": "button", "name": "Post", "name_mode": "prefix", "enabled": True}]},
        {"scope": "page", "locators": [{"id": "one", "type": "css", "value": "button", "enabled": True, "unexpected": True}]},
    ],
)
def test_locator_schema_rejects_unsafe_or_invalid_locator_shapes(definition):
    with pytest.raises(ValueError):
        normalize_element_definitions({"comment": definition})


def test_element_definition_rejects_unknown_keys():
    with pytest.raises(ValueError):
        normalize_element_definitions(
            {
                "comment": {
                    "scope": "page",
                    "locators": [{
                        "id": "comment-primary",
                        "type": "css",
                        "value": "button",
                        "enabled": True,
                    }],
                    "unexpected": True,
                }
            }
        )
