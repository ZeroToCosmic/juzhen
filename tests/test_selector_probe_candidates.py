import pytest

from browser_element_schema import (
    TIKTOK_COMMENT_TEMPLATE,
    normalize_element_definitions,
)
from selector_probe.candidates import generate_candidates
from selector_probe.contracts import default_tiktok_contracts, normalize_contracts
from selector_probe.snapshot import SemanticNode, SemanticSnapshot


def comment_contract():
    return next(iter(default_tiktok_contracts().values()))


def node(
    *,
    backend_node_id=42,
    parent_backend_node_id=10,
    tag="button",
    role="button",
    name="Comments",
    attributes=None,
    visible=True,
    in_viewport=True,
    actionable=False,
):
    return SemanticNode(
        backend_node_id=backend_node_id,
        parent_backend_node_id=parent_backend_node_id,
        tag=tag,
        role=role,
        name=name,
        states={"disabled": False},
        attributes=(
            attributes
            if attributes is not None
            else {"data-e2e": "comment-icon", "aria-label": "Comments"}
        ),
        bounds=(10.0, 20.0, 30.0, 40.0),
        visible=visible,
        in_viewport=in_viewport,
        actionable=actionable,
    )


def snapshot(*nodes):
    return SemanticSnapshot(nodes=tuple(nodes), scope="active_video")


def test_data_e2e_precedes_role_and_absolute_historical_xpath_is_dropped():
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "old",
                "type": "xpath",
                "value": "/html/body/div[3]/button[1]",
                "enabled": True,
                "fallback": True,
            }
        ],
    }

    candidates = generate_candidates(
        comment_contract(), snapshot(node()), historical
    )

    assert candidates[0]["type"] == "attribute"
    assert candidates[0]["name"] == "data-e2e"
    assert candidates[0]["value"] == "comment-icon"
    assert candidates[1]["type"] == "role"
    assert all(not item.get("value", "").startswith("/") for item in candidates)


def test_phase_one_actionable_false_does_not_filter_semantic_node():
    candidates = generate_candidates(comment_contract(), snapshot(node(actionable=False)))

    assert candidates
    assert candidates[0]["value"] == "comment-icon"


def test_nameless_comment_entry_uses_canonical_stable_anchor():
    contract = tuple(default_tiktok_contracts().values())[0]

    candidates = generate_candidates(
        contract,
        snapshot(
            node(
                name="",
                attributes={"data-e2e": "comment-icon"},
            )
        ),
        TIKTOK_COMMENT_TEMPLATE[contract.alias],
    )

    assert candidates[0]["type"] == "attribute"
    assert candidates[0]["name"] == "data-e2e"
    assert candidates[0]["value"] == "comment-icon"


def test_nameless_comment_input_uses_parent_descendant_anchor():
    contract = tuple(default_tiktok_contracts().values())[1]
    parent = node(
        backend_node_id=10,
        parent_backend_node_id=None,
        tag="div",
        role="generic",
        name="",
        attributes={"data-e2e": "comment-input"},
    )
    wrapper = node(
        backend_node_id=12,
        parent_backend_node_id=10,
        tag="div",
        role="generic",
        name="",
        attributes={},
    )
    child = node(
        backend_node_id=11,
        parent_backend_node_id=12,
        tag="div",
        role="textbox",
        name="",
        attributes={"contenteditable": "true"},
    )

    candidates = generate_candidates(
        contract,
        snapshot(parent, wrapper, child),
        TIKTOK_COMMENT_TEMPLATE[contract.alias],
    )

    constrained = [
        item
        for item in candidates
        if item["type"] == "attribute" and "descendant" in item
    ]
    assert constrained
    assert candidates[0] == constrained[0]
    assert constrained[0]["value"] == "comment-input"
    assert constrained[0]["descendant"]["role"] == "textbox"


def test_wrong_role_or_duplicate_nameless_anchor_remains_rejected():
    contract = tuple(default_tiktok_contracts().values())[0]
    historical = TIKTOK_COMMENT_TEMPLATE[contract.alias]
    wrong_role = snapshot(
        node(
            role="link",
            name="",
            attributes={"data-e2e": "comment-icon"},
        )
    )
    duplicate = snapshot(
        node(
            backend_node_id=41,
            name="",
            attributes={"data-e2e": "comment-icon"},
        ),
        node(
            backend_node_id=42,
            name="",
            attributes={"data-e2e": "comment-icon"},
        ),
    )

    assert generate_candidates(contract, wrong_role, historical) == []
    assert generate_candidates(contract, duplicate, historical) == []


def test_candidate_order_and_ids_are_deterministic_and_schema_valid():
    semantic_snapshot = snapshot(node())

    first = generate_candidates(comment_contract(), semantic_snapshot)
    second = generate_candidates(comment_contract(), semantic_snapshot)

    assert first == second
    assert len(first) <= 5
    assert len({item["id"] for item in first}) == len(first)
    assert normalize_element_definitions(
        {
            comment_contract().alias: {
                "scope": comment_contract().scope,
                "locators": first,
            }
        }
    )[comment_contract().alias]["locators"] == first


def test_invisible_out_of_viewport_and_wrong_semantics_are_ignored():
    semantic_snapshot = snapshot(
        node(backend_node_id=1, visible=False),
        node(backend_node_id=2, in_viewport=False),
        node(backend_node_id=3, role="link"),
        node(backend_node_id=4, name="Like"),
    )

    assert generate_candidates(comment_contract(), semantic_snapshot) == []


@pytest.mark.parametrize(
    "name",
    [
        "Delete Comments",
        "Comments settings",
        "Disable Comments",
    ],
)
def test_locale_map_requires_explicit_safe_name_not_category_guessing(name):
    assert generate_candidates(comment_contract(), snapshot(node(name=name))) == []


def test_contains_mode_uses_word_boundaries():
    input_contract = tuple(default_tiktok_contracts().values())[1]
    semantic_snapshot = snapshot(
        node(
            role="textbox",
            name="commentary",
            attributes={"data-e2e": "comment-input"},
        )
    )

    assert generate_candidates(input_contract, semantic_snapshot) == []


def test_stable_parent_constraint_is_generated_after_direct_candidates():
    parent = node(
        backend_node_id=10,
        parent_backend_node_id=None,
        tag="article",
        role="group",
        name="",
        attributes={"data-e2e": "active-video"},
    )
    child = node()

    candidates = generate_candidates(comment_contract(), snapshot(parent, child))

    constrained = [
        item
        for item in candidates
        if item["type"] == "attribute" and "descendant" in item
    ]
    assert constrained
    assert constrained[0]["value"] == "active-video"
    assert constrained[0]["descendant"]["value"] == "comment-icon"


def test_safe_historical_css_and_relative_xpath_are_kept_as_last_resort():
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "css-old",
                "type": "css",
                "value": 'button[data-e2e="comment-icon"]',
                "enabled": True,
            },
            {
                "id": "xpath-old",
                "type": "xpath",
                "value": ".//*[@data-e2e='comment-icon']",
                "enabled": True,
            },
        ],
    }

    candidates = generate_candidates(
        comment_contract(),
        snapshot(
            node(
                attributes={"data-e2e": "comment-icon"},
            )
        ),
        historical,
    )

    assert any(
        item["type"] == "css"
        and item["value"] == 'button[data-e2e="comment-icon"]'
        for item in candidates
    )
    assert any(
        item["type"] == "xpath"
        and item["value"] == ".//*[@data-e2e='comment-icon']"
        for item in candidates
    )


@pytest.mark.parametrize(
    "selector",
    [
        "/html/body/button",
        "/body/button",
        "//div[2]/button",
        "//button[position()=2]",
        ".//button[@data-e2e='comment-icon']",
        ".//*[count(@*)=1]",
        ".//section/button[@data-e2e='comment-icon']",
        "button:nth-child(2)",
        "button:nth-last-of-type(2)",
        "button:first-child",
        '[data-video-id="1234567890123456789"]',
        '[data-user-id="1234"]',
        '[data-id="550e8400-e29b-41d4-a716-446655440000"]',
        'a[href="/@specific-user"]',
        'button:has-text("a viewer comment")',
        "//button[text()='a viewer comment']",
        "javascript:document.querySelector('button')",
    ],
)
def test_unsafe_historical_selectors_are_rejected(selector):
    locator_type = "xpath" if selector.startswith("/") else "css"
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "unsafe",
                "type": locator_type,
                "value": selector,
                "enabled": True,
            }
        ],
    }

    candidates = generate_candidates(
        comment_contract(),
        snapshot(node(attributes={"data-e2e": "comment-icon"})),
        historical,
    )

    assert all(item.get("value") != selector for item in candidates)


def test_sensitive_snapshot_values_never_become_candidates():
    candidates = generate_candidates(
        comment_contract(),
        snapshot(
            node(
                attributes={
                    "data-e2e": "token-secret",
                    "aria-label": "Comments",
                    "id": "user-1234567890123",
                }
            )
        ),
    )

    assert all("secret" not in str(item).casefold() for item in candidates)
    assert all("1234567890123" not in str(item) for item in candidates)


@pytest.mark.parametrize(
    "destructive_value",
    [
        "delete-account",
        "remove-profile",
        "account-settings",
        "disable-comments",
    ],
)
def test_destructive_attribute_values_never_become_candidates(destructive_value):
    candidates = generate_candidates(
        comment_contract(),
        snapshot(
            node(
                attributes={
                    "data-e2e": destructive_value,
                    "aria-label": "Comments",
                }
            )
        ),
    )

    assert all(destructive_value not in str(item) for item in candidates)


def test_historical_selector_must_match_current_semantic_node():
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "stale-css",
                "type": "css",
                "value": 'button[data-e2e="stale-comment-icon"]',
                "enabled": True,
            },
            {
                "id": "parent-only",
                "type": "attribute",
                "name": "data-e2e",
                "value": "active-video",
                "enabled": True,
            },
        ],
    }
    parent = node(
        backend_node_id=10,
        parent_backend_node_id=None,
        tag="article",
        role="group",
        name="",
        attributes={"data-e2e": "active-video"},
    )

    candidates = generate_candidates(
        comment_contract(), snapshot(parent, node()), historical
    )

    assert all(item.get("value") != "stale-comment-icon" for item in candidates)
    assert all(
        item.get("value") != "active-video" or "descendant" in item
        for item in candidates
    )


def test_historical_locator_with_unknown_action_field_is_rejected():
    selector = 'button[data-e2e="comment-icon"]'
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "smuggled-action",
                "type": "css",
                "value": selector,
                "enabled": True,
                "action": "click",
            }
        ],
    }

    candidates = generate_candidates(
        comment_contract(),
        snapshot(node(attributes={"data-e2e": "comment-icon"})),
        historical,
    )

    assert all(item.get("value") != selector for item in candidates)


def test_historical_attribute_descendant_is_preserved_only_for_real_parent_child():
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "parent-child",
                "type": "attribute",
                "name": "data-e2e",
                "value": "active-video",
                "enabled": True,
                "descendant": {
                    "type": "attribute",
                    "name": "contenteditable",
                    "value": "true",
                    "role": "button",
                },
            }
        ],
    }
    parent = node(
        backend_node_id=10,
        parent_backend_node_id=None,
        tag="article",
        role="group",
        name="",
        attributes={"data-e2e": "active-video"},
    )
    child = node(
        attributes={
            "data-e2e": "comment-icon",
            "contenteditable": "true",
        }
    )

    candidates = generate_candidates(
        comment_contract(), snapshot(parent, child), historical
    )

    assert any(item.get("descendant") == historical["locators"][0]["descendant"] for item in candidates)


def test_invalid_historical_descendant_relationship_is_not_degraded_to_parent():
    historical = {
        "scope": "active_video",
        "locators": [
            {
                "id": "wrong-child",
                "type": "attribute",
                "name": "data-e2e",
                "value": "active-video",
                "enabled": True,
                "descendant": {
                    "type": "attribute",
                    "name": "contenteditable",
                    "value": "false",
                    "role": "button",
                },
            }
        ],
    }
    parent = node(
        backend_node_id=10,
        parent_backend_node_id=None,
        tag="article",
        role="group",
        name="",
        attributes={"data-e2e": "active-video"},
    )
    child = node(attributes={"data-e2e": "comment-icon", "contenteditable": "true"})

    candidates = generate_candidates(
        comment_contract(), snapshot(parent, child), historical
    )

    assert all(
        not (
            item.get("value") == "active-video"
            and item.get("descendant", {}).get("name") == "contenteditable"
        )
        for item in candidates
    )


def test_backend_node_id_swaps_do_not_change_top_five_candidates():
    first = snapshot(
        node(
            backend_node_id=10,
            parent_backend_node_id=None,
            attributes={"data-e2e": "comment-alpha", "aria-label": "Comments"},
        ),
        node(
            backend_node_id=20,
            parent_backend_node_id=None,
            attributes={"data-e2e": "comment-beta", "aria-label": "Comments"},
        ),
    )
    swapped = snapshot(
        node(
            backend_node_id=20,
            parent_backend_node_id=None,
            attributes={"data-e2e": "comment-alpha", "aria-label": "Comments"},
        ),
        node(
            backend_node_id=10,
            parent_backend_node_id=None,
            attributes={"data-e2e": "comment-beta", "aria-label": "Comments"},
        ),
    )

    assert generate_candidates(comment_contract(), first) == generate_candidates(
        comment_contract(), swapped
    )


def test_dynamic_contract_generates_candidates_for_new_alias():
    contract = normalize_contracts(
        {
            "分享入口": {
                "intent": "inspect the share control",
                "required_state": "feed_ready",
                "scope": "active_video",
                "accepted_roles": ["button"],
                "accepted_names": {"mode": "contains", "values": ["Share"]},
                "preferred_attributes": ["data-e2e", "aria-label"],
                "postcondition": "",
                "probe_action": "inspect_only",
            }
        }
    )["分享入口"]

    candidates = generate_candidates(
        contract,
        snapshot(
            node(
                name="Share",
                attributes={"data-e2e": "share-icon", "aria-label": "Share"},
            )
        ),
    )

    assert candidates[0]["value"] == "share-icon"
