from selector_probe.discovery import (
    comment_entry_definition,
    discover_interactive_candidates,
    merge_discovery_candidates,
)


def semantic_snapshot(*nodes):
    return {"scope": "page", "viewport": [1280, 720], "nodes": list(nodes)}


def test_discovery_keeps_interactive_safe_nodes_and_rejects_hidden_nodes():
    result = discover_interactive_candidates(
        semantic_snapshot(
            {
                "backend_node_id": 10,
                "parent_backend_node_id": 1,
                "tag": "button",
                "role": "button",
                "name": "Comments",
                "states": {},
                "attributes": {"data-e2e": "comment-icon"},
                "bounds": [10, 10, 40, 40],
                "visible": True,
                "in_viewport": True,
                "actionable": True,
            },
            {
                "backend_node_id": 11,
                "role": "button",
                "name": "Hidden",
                "states": {},
                "attributes": {},
                "bounds": None,
                "visible": False,
                "in_viewport": False,
                "actionable": False,
            },
        ),
        page_state="feed_ready",
        profile_mask="***0001",
    )

    assert len(result) == 1
    assert result[0]["role"] == "button"
    assert result[0]["attributes"] == {"data-e2e": "comment-icon"}
    assert "backend_node_id" not in result[0]
    assert "bounds" not in result[0]


def test_cross_profile_merge_reports_consistency():
    comment = {
        "fingerprint": "sha256:comment",
        "page_state": "feed_ready",
        "scope": "active_video",
        "role": "button",
        "name": "Comments",
        "attributes": {"data-e2e": "comment-icon"},
        "actionable": True,
    }
    merged = merge_discovery_candidates(
        [
            {
                "profile_mask": "***0001",
                "page_state": "feed_ready",
                "evidence": {"discoveries": [comment]},
            },
            {
                "profile_mask": "***0002",
                "page_state": "feed_ready",
                "evidence": {"discoveries": [comment]},
            },
        ]
    )

    assert merged[0]["profile_count"] == 2
    assert merged[0]["profile_masks"] == ["***0001", "***0002"]


def test_comment_entry_definition_requires_one_safe_actionable_match():
    candidate = {
        "fingerprint": "sha256:comment",
        "page_state": "feed_ready",
        "scope": "active_video",
        "role": "button",
        "name": "comments",
        "attributes": {"data-e2e": "comment-icon"},
        "actionable": True,
    }

    definition = comment_entry_definition([candidate])

    assert definition == {
        "scope": "active_video",
        "locators": [
            {
                "id": "probe-comment-entry",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }
        ],
    }
    assert comment_entry_definition([candidate, candidate]) is None

    unverified = {
        **candidate,
        "actionable": False,
        "visible": True,
        "in_viewport": True,
    }
    assert comment_entry_definition([unverified]) is None
    assert comment_entry_definition(
        [unverified],
        allow_unverified=True,
    ) == definition
