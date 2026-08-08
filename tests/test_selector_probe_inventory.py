from __future__ import annotations

import pytest

from selector_probe.inventory import (
    MAX_INVENTORY_ITEMS,
    normalize_inventory,
    normalize_recorded_step,
    public_inventory_item,
)


def _item(**changes):
    value = {
        "target_key": "target-1",
        "tag": "button",
        "input_type": "button",
        "text": "Open comments",
        "role": "button",
        "name": "Comments",
        "attributes": {
            "data-e2e": "comment-icon",
            "onclick": "secret()",
        },
        "frame_key": "main",
        "shadow": False,
        "shadow_key": "document",
        "visible": True,
        "enabled": True,
        "hit_target": True,
        "target_match": True,
        "region": {"x": 0.8, "y": 0.4, "width": 0.1, "height": 0.1},
        "locators": [
            {
                "type": "css",
                "value": '[data-e2e="comment-icon"]',
                "match_count": 1,
            },
            {
                "type": "xpath",
                "value": "//*[@data-e2e='comment-icon']",
                "match_count": 1,
            },
        ],
    }
    value.update(changes)
    return value


def test_inventory_keeps_role_and_name_as_display_only_metadata():
    result = normalize_inventory([_item()])

    assert len(result) == 1
    assert result[0]["role"] == "button"
    assert result[0]["name"] == "Comments"
    assert result[0]["locatable"] is True
    assert result[0]["attributes"] == {"data-e2e": "comment-icon"}


def test_role_and_name_never_decide_whether_an_item_is_locatable():
    without_semantics = _item(role="", name="")
    semantics_only = _item(
        target_key="target-2",
        role="button",
        name="Post",
        locators=[],
    )

    result = normalize_inventory([semantics_only, without_semantics])

    assert [item["locatable"] for item in result] == [True, False]


def test_inventory_rejects_semantic_absolute_and_javascript_locators():
    raw = _item(
        tag="div",
        text="Post",
        attributes={"aria-label": "Post"},
        locators=[
            {"type": "role", "value": "button:Post", "match_count": 1},
            {"type": "xpath", "value": "/html/body/div[3]", "match_count": 1},
            {"type": "xpath", "value": "//html/body/div[3]", "match_count": 1},
            {"type": "css", "value": "javascript:alert(1)", "match_count": 1},
            {
                "type": "css",
                "value": "button:nth-of-type(2)",
                "match_count": 1,
            },
        ],
    )

    result = normalize_inventory([raw])

    assert [item["type"] for item in result[0]["locators"]] == ["css"]
    assert result[0]["locatable"] is True


@pytest.mark.parametrize(
    "value",
    [
        'input[value="private"]',
        'a[href="https://example.test"]',
        ".random-class",
        'button:has-text("Post")',
        "//*[text()='Post']",
        "(//*[@data-e2e='comment-icon'])[1]",
        "/descendant::button[1]",
    ],
)
def test_inventory_rejects_sensitive_random_or_xpath_bypass_paths(value):
    locator_type = "xpath" if value.startswith(("/", "(")) else "css"
    result = normalize_inventory(
        [_item(locators=[{"type": locator_type, "value": value, "match_count": 1}])]
    )[0]

    assert result["locators"] == []
    assert result["locatable"] is False


def test_inventory_accepts_only_bounded_deterministic_css_and_xpath_grammar():
    result = normalize_inventory(
        [
            _item(
                locators=[
                    {
                        "type": "css",
                        "value": '[data-e2e="feed"] > button:nth-of-type(2)',
                        "match_count": 1,
                    },
                    {
                        "type": "xpath",
                        "value": "//*[@data-testid='feed']/button[2]",
                        "match_count": 1,
                    },
                    {
                        "type": "css",
                        "value": "div > div > div > div > button:nth-of-type(101)",
                        "match_count": 1,
                    },
                ]
            )
        ]
    )[0]

    assert [item["type"] for item in result["locators"]] == ["css", "xpath"]


def test_inventory_accepts_strict_xpath_presence_anchor_with_short_child_path():
    result = normalize_inventory(
        [
            _item(
                locators=[
                    {
                        "type": "xpath",
                        "value": "//*[@data-testid]/button[2]",
                        "match_count": 1,
                    }
                ]
            )
        ]
    )[0]

    assert result["locators"] == [
        {
            "type": "xpath",
            "value": "//*[@data-testid]/button[2]",
            "match_count": 1,
        }
    ]


def test_locator_requires_a_non_negative_integer_match_count():
    raw = _item(
        locators=[
            {"type": "css", "value": "button", "match_count": True},
            {"type": "css", "value": "button", "match_count": -1},
            {"type": "css", "value": "button", "match_count": "1"},
            {"type": "css", "value": "button", "match_count": 2},
        ]
    )

    result = normalize_inventory([raw])

    assert result[0]["locators"] == [
        {"type": "css", "value": "button", "match_count": 2}
    ]
    assert result[0]["locatable"] is False


@pytest.mark.parametrize(
    "dynamic_id",
    [
        "node-123456",
        "node-deadbeef",
        "550e8400-e29b-41d4-a716-446655440000",
    ],
)
def test_dynamic_id_is_displayed_but_removed_from_locators_and_fingerprint(
    dynamic_id,
):
    raw = _item(
        attributes={"id": dynamic_id},
        locators=[
            {"type": "css", "value": f"#{dynamic_id}", "match_count": 1},
            {
                "type": "xpath",
                "value": f"//*[@id='{dynamic_id}']",
                "match_count": 1,
            },
        ],
    )
    changed = _item(
        attributes={"id": "node-654321"},
        locators=[],
    )

    result = normalize_inventory([raw])[0]
    changed_result = normalize_inventory([changed])[0]

    assert result["attributes"]["id"] == dynamic_id
    assert result["locators"] == []
    assert result["locatable"] is False
    assert result["fingerprint"] == changed_result["fingerprint"]


def test_inventory_normalizes_region_and_sanitizes_bounded_strings():
    result = normalize_inventory(
        [
            _item(
                tag="  BUTTON\x00  ",
                text="  Open   comments\x00 now  ",
                frame_key="  main   frame  ",
                shadow=1,
                region={"x": -0.2, "y": 0.9, "width": 1.4, "height": 0.5},
            )
        ]
    )[0]

    assert result["tag"] == "button"
    assert result["text"] == "Open comments now"
    assert result["frame_key"] == "main frame"
    assert result["shadow"] is False
    assert result["region"] == {"x": 0.0, "y": 0.9, "width": 1.0, "height": 0.1}


@pytest.mark.parametrize("tag", ["input", "textarea", "select", "option", "form"])
def test_inventory_clears_text_from_editable_and_form_nodes(tag):
    result = normalize_inventory([_item(tag=tag, text="private form content")])[0]

    assert result["text"] == ""


def test_inventory_clears_text_from_contenteditable_node():
    result = normalize_inventory(
        [_item(tag="div", text="draft comment", attributes={"contenteditable": "true"})]
    )[0]

    assert result["text"] == ""


def test_inventory_deduplicates_target_key_preferring_locatable_candidate():
    first = _item(locators=[])
    better = _item(text="same target, better evidence")

    result = normalize_inventory([first, better])

    assert len(result) == 1
    assert result[0]["locatable"] is True
    assert result[0]["text"] == "same target, better evidence"


def test_inventory_caps_raw_and_public_items_and_sorts_locatable_first():
    ignored_after_raw_cap = [None] * 1000 + [
        _item(target_key="too-late")
    ]
    assert normalize_inventory(ignored_after_raw_cap) == []

    items = [
        _item(target_key=f"target-{index}", text=str(index))
        for index in range(600)
    ]
    items.insert(0, _item(target_key="unlocatable", locators=[]))
    result = normalize_inventory(items, limit=10_000)

    assert len(result) == MAX_INVENTORY_ITEMS
    assert result[0]["locatable"] is True
    assert result[-1]["locatable"] is True


def test_selection_id_is_stable_and_reuses_supplied_id_by_fingerprint():
    first = normalize_inventory([_item()])[0]
    rescanned = normalize_inventory(
        [_item(role="renamed-role", name="Renamed display metadata")],
        selection_ids={first["fingerprint"]: "selection-existing"},
    )[0]

    assert rescanned["fingerprint"] == first["fingerprint"]
    assert rescanned["selection_id"] == "selection-existing"


def test_fingerprint_separates_frames_and_shadow_scopes_and_deduplicates_unkeyed():
    main = _item(target_key="", frame_key="main", shadow=False)
    duplicate = _item(target_key="", frame_key="main", shadow=False)
    iframe = _item(target_key="", frame_key="frame-1", shadow=False)
    shadow = _item(
        target_key="", frame_key="main", shadow=True, shadow_key="host-1/root"
    )

    result = normalize_inventory([main, duplicate, iframe, shadow])

    assert len(result) == 3
    assert len({item["fingerprint"] for item in result}) == 3


def test_target_key_deduplication_is_scoped_by_frame_and_shadow_key():
    result = normalize_inventory(
        [
            _item(target_key="node-1", frame_key="main", shadow_key="document"),
            _item(target_key="node-1", frame_key="frame-1", shadow_key="document"),
            _item(target_key="node-1", frame_key="main", shadow_key="host-1/root"),
        ]
    )

    assert len(result) == 3
    assert len({item["fingerprint"] for item in result}) == 3


def test_conflicting_reused_selection_id_falls_back_to_unique_generated_id():
    first = normalize_inventory([_item(target_key="one")])[0]
    second = normalize_inventory(
        [_item(target_key="two", frame_key="frame-2")]
    )[0]
    result = normalize_inventory(
        [
            _item(target_key="one"),
            _item(target_key="two", frame_key="frame-2"),
        ],
        selection_ids={
            first["fingerprint"]: "selection-reused",
            second["fingerprint"]: "selection-reused",
        },
    )

    assert len({item["selection_id"] for item in result}) == 2
    assert sum(item["selection_id"] == "selection-reused" for item in result) == 1


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "existing-id",
        "selection-has a space",
        "selection-../../secret",
        "selection-" + "a" * 55,
    ],
)
def test_reused_selection_id_requires_bounded_safe_selection_format(unsafe_id):
    first = normalize_inventory([_item()])[0]
    rescanned = normalize_inventory(
        [_item()], selection_ids={first["fingerprint"]: unsafe_id}
    )[0]

    assert rescanned["selection_id"] != unsafe_id
    assert rescanned["selection_id"].startswith("selection-")


@pytest.mark.parametrize("failed_flag", ["visible", "enabled", "hit_target", "target_match"])
def test_locatable_requires_all_actionability_checks(failed_flag):
    raw = _item()
    raw[failed_flag] = False

    result = normalize_inventory([raw])[0]

    assert result[failed_flag] is False
    assert result["locatable"] is False


def test_public_inventory_item_has_only_the_public_schema():
    normalized = normalize_inventory([_item()])[0]
    normalized["secret"] = "must-not-leak"

    public = public_inventory_item(normalized)

    assert set(public) == {
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
    }
    assert "secret" not in public


def test_public_inventory_item_handles_malformed_nested_values():
    public = public_inventory_item(
        {
            "attributes": "not-a-map",
            "region": None,
            "locators": ["bad", None, {"type": "css"}],
            "match_counts": [1],
        }
    )

    assert public["attributes"] == {}
    assert public["region"] == {}
    assert public["locators"] == []
    assert public["match_counts"] == {}


@pytest.mark.parametrize(
    ("tag", "attributes"),
    [
        ("form", {}),
        ("input", {}),
        ("textarea", {}),
        ("select", {}),
        ("option", {}),
        ("div", {"contenteditable": "true"}),
    ],
)
def test_public_projection_also_clears_private_form_text(tag, attributes):
    public = public_inventory_item(
        {
            "tag": tag,
            "text": "private typed value",
            "name": "private typed value",
            "attributes": attributes,
        }
    )

    assert public["text"] == ""
    assert public["name"] == ""


def test_normalization_clears_private_form_name_as_well_as_text():
    private = normalize_inventory(
        [
            _item(
                tag="textarea",
                text="draft comment",
                name="draft comment",
            )
        ]
    )[0]

    assert private["text"] == ""
    assert private["name"] == ""


def test_recorded_step_accepts_css_or_xpath_only():
    step = normalize_recorded_step(
        {
            "sequence": 1,
            "locator": {
                "type": "css",
                "value": '[data-e2e="comment-icon"]',
            },
            "url_before": "https://www.tiktok.com/",
            "url_after": "https://www.tiktok.com/",
            "recorded_at": "2026-08-04T03:00:00+00:00",
            "frame_key": "main",
            "shadow": True,
            "shadow_key": "host-1/root",
        }
    )

    assert step == {
        "sequence": 1,
        "locator": {
            "type": "css",
            "value": '[data-e2e="comment-icon"]',
        },
        "url_before": "https://www.tiktok.com/",
        "url_after": "https://www.tiktok.com/",
        "recorded_at": "2026-08-04T03:00:00+00:00",
        "frame_key": "main",
        "shadow": True,
        "shadow_key": "host-1/root",
    }


def test_recorded_step_sanitizes_urls_and_drops_credentials_query_and_fragment():
    step = normalize_recorded_step(
        {
            "sequence": 1,
            "locator": {"type": "css", "value": "button"},
            "url_before": "https://user:pass@Example.COM:8443/path?q=secret#part",
            "url_after": "javascript:alert(1)",
            "frame_key": " iframe-1 ",
            "shadow": 1,
            "shadow_key": " host-1 / root ",
        }
    )

    assert step["url_before"] == "https://example.com:8443/path"
    assert step["url_after"] == ""
    assert step["frame_key"] == "iframe-1"
    assert step["shadow"] is False
    assert step["shadow_key"] == "host-1 / root"


@pytest.mark.parametrize(
    "locator",
    [
        {"type": "role", "value": "button:Comments"},
        {"type": "xpath", "value": "/html/body/button"},
        {"type": "css", "value": "javascript:alert(1)"},
    ],
)
def test_recorded_step_rejects_unsafe_locator(locator):
    with pytest.raises(ValueError, match="invalid_recorded_step"):
        normalize_recorded_step({"sequence": 1, "locator": locator})
