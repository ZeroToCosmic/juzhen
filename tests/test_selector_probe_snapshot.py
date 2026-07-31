import asyncio
from dataclasses import fields

import pytest

from selector_probe.snapshot import (
    MAX_SEMANTIC_NODES,
    SemanticNode,
    SemanticSnapshot,
    build_semantic_snapshot,
    decode_dom_snapshot,
    extract_semantic_snapshot,
)


def _dom_node(**overrides):
    value = {
        "backend_node_id": 42,
        "parent_backend_node_id": None,
        "tag": "button",
        "attributes": {"data-e2e": "comment-icon"},
        "bounds": [10, 20, 30, 40],
        "visible": True,
        "in_viewport": True,
        "computed_styles": {
            "display": "block",
            "visibility": "visible",
            "pointer-events": "auto",
            "opacity": "1",
        },
    }
    value.update(overrides)
    return value


def test_snapshot_uses_the_contract_dataclass_shapes():
    assert [item.name for item in fields(SemanticNode)] == [
        "backend_node_id",
        "parent_backend_node_id",
        "tag",
        "role",
        "name",
        "states",
        "attributes",
        "bounds",
        "visible",
        "in_viewport",
        "actionable",
    ]
    assert [item.name for item in fields(SemanticSnapshot)] == [
        "nodes",
        "scope",
        "viewport",
    ]


def test_snapshot_joins_ax_and_dom_by_backend_node_id():
    ax_nodes = [
        {
            "nodeId": "ax-1",
            "backendDOMNodeId": 42,
            "role": {"value": "button"},
            "name": {"value": "Comments"},
            "properties": [
                {"name": "disabled", "value": {"value": False}},
                {"name": "value", "value": {"value": "private comment"}},
            ],
        }
    ]
    snapshot = build_semantic_snapshot(ax_nodes, [_dom_node()])
    node = snapshot.nodes[0]
    assert node.role == "button"
    assert node.name == "comments"
    assert node.states == {"disabled": False}
    assert node.attributes == {"data-e2e": "comment-icon"}
    assert node.bounds == (10.0, 20.0, 30.0, 40.0)
    assert node.actionable is False


def test_snapshot_keeps_semantic_nodes_and_required_ancestors_only():
    dom_nodes = [
        _dom_node(
            backend_node_id=1,
            tag="main",
            attributes={},
            bounds=[0, 0, 500, 500],
        ),
        _dom_node(
            backend_node_id=2,
            parent_backend_node_id=1,
            tag="section",
            attributes={},
        ),
        _dom_node(
            backend_node_id=3,
            parent_backend_node_id=2,
            tag="button",
        ),
        _dom_node(
            backend_node_id=4,
            parent_backend_node_id=1,
            tag="div",
            attributes={},
        ),
    ]
    ax_nodes = [
        {
            "backendDOMNodeId": 3,
            "role": {"value": "button"},
            "name": {"value": "Comments"},
        }
    ]
    snapshot = build_semantic_snapshot(ax_nodes, dom_nodes)
    assert [node.backend_node_id for node in snapshot.nodes] == [1, 2, 3]


def test_model_payload_drops_user_session_and_comment_shaped_values():
    ax_nodes = [
        {
            "backendDOMNodeId": 9,
            "role": {"value": "StaticText"},
            "name": {"value": "This is a private comment body"},
            "properties": [
                {"name": "description", "value": {"value": "secret comment"}},
                {"name": "checked", "value": {"value": "false"}},
                {"name": "authToken", "value": {"value": "secret"}},
            ],
        }
    ]
    snapshot = build_semantic_snapshot(
        ax_nodes,
        [
            _dom_node(
                backend_node_id=9,
                tag="a",
                attributes={
                    "href": "/@private-user/video/7523456789012345678",
                    "data-e2e": "comment-icon",
                    "aria-label": "@private-user",
                    "id": "550e8400-e29b-41d4-a716-446655440000",
                    "name": "authorization",
                    "class": "css-1a2b3c",
                },
            )
        ],
    )
    payload = snapshot.model_payload()
    text = str(payload)
    assert "private-user" not in text
    assert "7523456789012345678" not in text
    assert "private comment" not in text
    assert "secret" not in text
    assert "550e8400" not in text
    assert "authorization" not in text
    assert "css-1a2b3c" not in text
    assert "comment-icon" in text
    assert payload["nodes"][0]["name"] == ""
    assert payload["nodes"][0]["states"] == {"checked": "false"}


def test_sensitive_accessible_names_and_state_values_are_removed():
    snapshot = build_semantic_snapshot(
        [
            {
                "backendDOMNodeId": 42,
                "role": {"value": "button"},
                "name": {"value": "Open /@private-user"},
                "properties": [
                    {"name": "expanded", "value": {"value": True}},
                    {"name": "selected", "value": {"value": "Alice Chen"}},
                    {
                        "name": "invalid",
                        "value": {"value": "2026-07-28T03:00:00Z"},
                    },
                ],
            }
        ],
        [
            _dom_node(
                attributes={
                    "data-testid": "comment-1700000000",
                    "aria-label": "Bearer abc.def.ghi",
                    "placeholder": "Write a comment",
                }
            )
        ],
    )
    node = snapshot.nodes[0]
    assert node.name == ""
    assert node.states == {"expanded": True}
    assert node.attributes == {"placeholder": "comment-input"}


@pytest.mark.parametrize(
    ("label", "canonical"),
    [
        ("Comments", "comments"),
        ("评论", "comments"),
        ("Reply", "reply"),
        ("关闭评论", "close-comments"),
        ("Search", "search"),
        ("Send", "send"),
        ("发布", "publish"),
        ("Like", "like"),
        ("分享", "share"),
        ("Next", "next"),
        ("上一条", "previous"),
    ],
)
def test_accessible_control_names_are_reduced_to_safe_ui_categories(
    label,
    canonical,
):
    snapshot = build_semantic_snapshot(
        [
            {
                "backendDOMNodeId": 42,
                "role": {"value": "button"},
                "name": {"value": label},
            }
        ],
        [_dom_node(attributes={"aria-label": label})],
    )
    assert snapshot.nodes[0].name == canonical
    assert snapshot.nodes[0].attributes["aria-label"] == canonical


@pytest.mark.parametrize(
    "label",
    [
        "Great video",
        "private-user",
        "Alice Chen",
        "Reply Great video",
        "评论 这条视频真不错",
    ],
)
def test_unknown_short_comments_usernames_and_real_names_are_not_retained(label):
    snapshot = build_semantic_snapshot(
        [
            {
                "backendDOMNodeId": 42,
                "role": {"value": "button"},
                "name": {"value": label},
            }
        ],
        [_dom_node(attributes={"data-e2e": "comment-icon", "aria-label": label})],
    )
    assert snapshot.nodes[0].name == ""
    assert "aria-label" not in snapshot.nodes[0].attributes


def test_decode_dom_snapshot_resolves_string_indexes_sparse_arrays_and_viewport():
    payload = {
        "strings": [
            "HTML",
            "DIV",
            "BUTTON",
            "data-e2e",
            "comment-icon",
            "block",
            "visible",
            "auto",
            "1",
            "none",
        ],
        "documents": [
            {
                "nodes": {
                    "parentIndex": [-1, 0, 1],
                    "nodeName": [0, 1, 2],
                    "backendNodeId": [10, 11, 12],
                    "attributes": [[], [], [3, 4]],
                },
                "layout": {
                    "nodeIndex": [0, 2],
                    "bounds": [[0, 0, 800, 600], [790, 590, 20, 20]],
                    "styles": [[5, 6, 7, 8], [5, 6, 7]],
                },
            }
        ],
    }
    nodes = decode_dom_snapshot(payload, viewport=(800, 600))
    assert nodes[2]["parent_backend_node_id"] == 11
    assert nodes[2]["tag"] == "button"
    assert nodes[2]["attributes"] == {"data-e2e": "comment-icon"}
    assert nodes[2]["computed_styles"] == {
        "display": "block",
        "visibility": "visible",
        "pointer-events": "auto",
    }
    assert nodes[2]["visible"] is True
    assert nodes[2]["in_viewport"] is True


def test_decode_preserves_safe_control_aria_label_and_build_uses_dom_role_fallback():
    payload = {
        "strings": [
            "DIV",
            "role",
            "button",
            "aria-label",
            "Open comments",
        ],
        "documents": [
            {
                "nodes": {
                    "nodeName": [0],
                    "backendNodeId": [1],
                    "attributes": [[1, 2, 3, 4]],
                },
                "layout": {
                    "nodeIndex": [0],
                    "bounds": [[0, 0, 20, 20]],
                },
            }
        ],
    }
    dom_nodes = decode_dom_snapshot(payload, viewport=(100, 100))
    snapshot = build_semantic_snapshot([], dom_nodes, viewport=(100, 100))
    assert snapshot.nodes[0].role == "button"
    assert snapshot.nodes[0].attributes == {
        "role": "button",
        "aria-label": "comments",
    }
    assert snapshot.nodes[0].actionable is False


def test_decode_accepts_missing_attribute_value_string_index():
    payload = {
        "strings": ["BUTTON", "disabled", "data-e2e", "comment-icon"],
        "documents": [
            {
                "nodes": {
                    "nodeName": [0],
                    "backendNodeId": [1],
                    "attributes": [[1, -1, 2, 3]],
                }
            }
        ],
    }

    nodes = decode_dom_snapshot(payload)

    assert nodes[0]["attributes"] == {"data-e2e": "comment-icon"}


def test_decode_links_child_document_root_to_iframe_owner():
    payload = {
        "strings": [
            "HTML",
            "IFRAME",
            "#document",
            "BUTTON",
            "data-e2e",
            "comment-icon",
        ],
        "documents": [
            {
                "nodes": {
                    "parentIndex": [-1, 0],
                    "nodeName": [0, 1],
                    "backendNodeId": [10, 11],
                    "attributes": [[], []],
                    "contentDocumentIndex": {"index": [1], "value": [1]},
                }
            },
            {
                "nodes": {
                    "parentIndex": [-1, 0],
                    "nodeName": [2, 3],
                    "backendNodeId": [20, 21],
                    "attributes": [[], [4, 5]],
                },
                "layout": {
                    "nodeIndex": [1],
                    "bounds": [[0, 0, 20, 20]],
                },
            },
        ],
    }
    dom_nodes = decode_dom_snapshot(payload, viewport=(100, 100))
    assert dom_nodes[2]["parent_backend_node_id"] == 11
    snapshot = build_semantic_snapshot([], dom_nodes, viewport=(100, 100))
    assert [node.backend_node_id for node in snapshot.nodes] == [10, 11, 20, 21]


@pytest.mark.parametrize(
    "rare_data",
    [
        {"index": [1], "value": []},
        {"index": [9], "value": [1]},
        {"index": [1], "value": [9]},
        {"index": [1, 1], "value": [1, 1]},
        "bad",
    ],
)
def test_decode_rejects_malformed_content_document_sparse_data(rare_data):
    payload = {
        "strings": ["HTML", "IFRAME", "#document"],
        "documents": [
            {
                "nodes": {
                    "parentIndex": [-1, 0],
                    "nodeName": [0, 1],
                    "backendNodeId": [10, 11],
                    "contentDocumentIndex": rare_data,
                }
            },
            {
                "nodes": {
                    "parentIndex": [-1],
                    "nodeName": [2],
                    "backendNodeId": [20],
                }
            },
        ],
    }
    with pytest.raises(ValueError, match="DOM snapshot"):
        decode_dom_snapshot(payload)


def test_viewport_intersection_uses_document_scroll_offsets():
    payload = {
        "strings": ["DIV", "data-e2e", "comment-icon"],
        "documents": [
            {
                "scrollOffsetX": 100,
                "scrollOffsetY": 200,
                "nodes": {
                    "nodeName": [0, 0],
                    "backendNodeId": [1, 2],
                    "attributes": [[1, 2], [1, 2]],
                },
                "layout": {
                    "nodeIndex": [0, 1],
                    "bounds": [[110, 210, 20, 20], [10, 10, 20, 20]],
                },
            }
        ],
    }
    nodes = decode_dom_snapshot(payload, viewport=(80, 80))
    assert nodes[0]["bounds"] == (110.0, 210.0, 20.0, 20.0)
    assert nodes[0]["in_viewport"] is True
    assert nodes[1]["in_viewport"] is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"strings": "not-a-list", "documents": []},
        {"strings": [], "documents": "not-a-list"},
        {
            "strings": ["DIV"],
            "documents": [{"nodes": {"nodeName": [99], "backendNodeId": [1]}}],
        },
        {
            "strings": ["DIV", "data-e2e"],
            "documents": [
                {
                    "nodes": {
                        "nodeName": [0],
                        "backendNodeId": [1],
                        "attributes": [[1]],
                    }
                }
            ],
        },
    ],
)
def test_decode_dom_snapshot_rejects_malformed_cdp_payload(payload):
    with pytest.raises(ValueError, match="DOM snapshot"):
        decode_dom_snapshot(payload)


def test_snapshot_never_claims_actionability_without_dry_run_hit_testing():
    snapshot = build_semantic_snapshot(
        [
            {
                "backendDOMNodeId": 1,
                "role": {"value": "button"},
                "name": {"value": "Comments"},
                "properties": [],
            },
            {
                "backendDOMNodeId": 2,
                "role": {"value": "button"},
                "name": {"value": "Comments"},
                "properties": [
                    {"name": "disabled", "value": {"value": True}},
                ],
            },
        ],
        [
            _dom_node(
                backend_node_id=1,
                in_viewport=False,
                bounds=[1000, 1000, 20, 20],
            ),
            _dom_node(
                backend_node_id=2,
                attributes={"data-e2e": "submit"},
            ),
        ],
        viewport=(800, 600),
    )
    assert snapshot.nodes[0].visible is True
    assert snapshot.nodes[0].in_viewport is False
    assert snapshot.nodes[0].actionable is False
    assert snapshot.nodes[1].actionable is False


def test_snapshot_enforces_deterministic_hard_node_limit_with_ancestors():
    dom_nodes = [
        _dom_node(
            backend_node_id=1,
            tag="main",
            attributes={},
            bounds=[0, 0, 100, 100],
        )
    ]
    for backend_id in range(2, MAX_SEMANTIC_NODES + 30):
        dom_nodes.append(
            _dom_node(
                backend_node_id=backend_id,
                parent_backend_node_id=1,
                attributes={"data-e2e": f"control-{backend_id}"},
            )
        )
    snapshot = build_semantic_snapshot([], list(reversed(dom_nodes)))
    assert len(snapshot.nodes) == MAX_SEMANTIC_NODES
    assert 1 in {node.backend_node_id for node in snapshot.nodes}
    assert MAX_SEMANTIC_NODES in {
        node.backend_node_id for node in snapshot.nodes
    }
    assert MAX_SEMANTIC_NODES + 1 not in {
        node.backend_node_id for node in snapshot.nodes
    }


def test_non_page_scope_is_rejected_instead_of_being_mislabeled():
    with pytest.raises(ValueError, match="scope"):
        build_semantic_snapshot([], [], scope="active_video")


class FakeSession:
    def __init__(self, *, capture_error=None, disable_error=None):
        self.capture_error = capture_error
        self.disable_error = disable_error
        self.commands = []
        self.detached = False

    async def send(self, command, params=None):
        self.commands.append((command, params))
        if command == "Accessibility.disable" and self.disable_error is not None:
            raise self.disable_error
        if command == "Accessibility.getFullAXTree":
            if self.capture_error is not None:
                raise self.capture_error
            return {
                "nodes": [
                    {
                        "backendDOMNodeId": 12,
                        "role": {"value": "button"},
                        "name": {"value": "Comments"},
                    }
                ]
            }
        if command == "DOMSnapshot.captureSnapshot":
            return {
                "strings": [
                    "HTML",
                    "BUTTON",
                    "data-e2e",
                    "comment-icon",
                    "block",
                    "visible",
                    "auto",
                    "1",
                ],
                "documents": [
                    {
                        "nodes": {
                            "parentIndex": [-1, 0],
                            "nodeName": [0, 1],
                            "backendNodeId": [10, 12],
                            "attributes": [[], [2, 3]],
                        },
                        "layout": {
                            "nodeIndex": [0, 1],
                            "bounds": [[0, 0, 800, 600], [10, 10, 20, 20]],
                            "styles": [[4, 5, 6, 7], [4, 5, 6, 7]],
                        },
                    }
                ],
            }
        return {}

    async def detach(self):
        self.detached = True


class FakeContext:
    def __init__(self, session):
        self.session = session

    async def new_cdp_session(self, page):
        assert page is not None
        return self.session


class FakePage:
    def __init__(self, session, viewport_size=None):
        self.context = FakeContext(session)
        self.viewport_size = viewport_size

    async def evaluate(self, expression):
        assert "innerWidth" in expression
        return {"width": 800, "height": 600}


def test_extract_snapshot_captures_cdp_and_always_cleans_up():
    async def scenario():
        session = FakeSession()
        snapshot = await extract_semantic_snapshot(
            FakePage(session, {"width": 800, "height": 600}),
        )
        assert snapshot.scope == "page"
        assert snapshot.viewport == (800, 600)
        assert [node.backend_node_id for node in snapshot.nodes] == [10, 12]
        assert (
            "DOMSnapshot.captureSnapshot",
            {
                "computedStyles": [
                    "display",
                    "visibility",
                    "pointer-events",
                    "opacity",
                ],
                "includeDOMRects": True,
                "includePaintOrder": True,
            },
        ) in session.commands
        assert ("Accessibility.disable", None) in session.commands
        assert session.detached is True

    asyncio.run(scenario())


def test_extract_rejects_non_page_scope_before_opening_cdp_session():
    async def scenario():
        session = FakeSession()
        with pytest.raises(ValueError, match="scope"):
            await extract_semantic_snapshot(
                FakePage(session),
                scope="active_video",
            )
        assert session.commands == []

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("capture failed"), asyncio.CancelledError()])
def test_extract_snapshot_disables_and_detaches_after_failure_or_cancellation(error):
    async def scenario():
        session = FakeSession(capture_error=error)
        with pytest.raises(type(error)):
            await extract_semantic_snapshot(FakePage(session))
        assert ("Accessibility.disable", None) in session.commands
        assert session.detached is True

    asyncio.run(scenario())


def test_extract_snapshot_detaches_even_when_accessibility_disable_fails():
    async def scenario():
        session = FakeSession(disable_error=RuntimeError("disable failed"))
        with pytest.raises(RuntimeError, match="disable failed"):
            await extract_semantic_snapshot(FakePage(session))
        assert session.detached is True

    asyncio.run(scenario())
