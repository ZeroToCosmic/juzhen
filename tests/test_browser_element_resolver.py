import asyncio
import re

import pytest

from browser_element_schema import normalize_element_definitions
from browser_element_resolver import (
    LocatorResolutionError,
    inspect_element,
    inspect_visible_element,
    resolve_element,
    resolve_scope,
    resolve_visible_element,
)


class FakeNode:
    def __init__(self, tag, attributes=None, *, visible=True, enabled=True, rect=None, text="", state="ready"):
        self.tag = tag
        self.attributes = attributes or {}
        self.visible = visible
        self.enabled = enabled
        self.rect = rect
        self.text = text
        self.state = state
        self.children = []
        self.parent = None

    def append(self, child):
        child.parent = self
        self.children.append(child)
        return child


class FakeLocator:
    def __init__(self, nodes):
        self.nodes = nodes

    def locator(self, selector):
        if self.nodes:
            root = self.nodes[0]
            while root.parent is not None:
                root = root.parent
            if getattr(root, "raise_for_selector", None) == selector:
                raise RuntimeError(f"selector failure: {selector}; page-text-sentinel")
        if selector.startswith("xpath="):
            return FakeLocator(self._xpath(selector.removeprefix("xpath=")))
        return FakeLocator(self._css(selector))

    def get_by_role(self, role, name=None, exact=False):
        matched = [node for node in self._descendants(include_self=True) if node.attributes.get("role") == role]
        if name is not None:
            matched = [node for node in matched if self._matches_name(node, name, exact)]
        return FakeLocator(matched)

    async def count(self):
        return len(self.nodes)

    def nth(self, index):
        return FakeLocator([self.nodes[index]])

    async def is_visible(self):
        return len(self.nodes) == 1 and self.nodes[0].visible

    async def is_enabled(self):
        return len(self.nodes) == 1 and self.nodes[0].enabled

    async def get_attribute(self, name):
        return self.nodes[0].attributes.get(name)

    async def bounding_box(self):
        return self.nodes[0].rect

    async def evaluate(self, _expression):
        node = self.nodes[0]
        state = node.state
        class_names = node.attributes.get("class", "").split()
        has_exit_class = any(re.search(r"(?:^|[-_])(?:leave|exit|exiting)(?:[-_]|$)", name, re.I) for name in class_names)
        has_enter_active = any(re.search(r"(?:^|[-_])enter-active(?:[-_]|$)", name, re.I) for name in class_names)
        has_enter_done = any(re.search(r"(?:^|[-_])enter-done(?:[-_]|$)", name, re.I) for name in class_names)
        return {
            "connected": state != "detached",
            "hidden": state in {"hidden", "display_none", "invisible", "opacity_zero", "pointer_none"}
            or node.attributes.get("aria-hidden") == "true" or "inert" in node.attributes,
            "exiting": state == "exiting" or has_exit_class or (has_enter_active and not has_enter_done),
            "covered": state == "covered",
        }

    def _descendants(self, include_self=False):
        result = []
        stack = list(self.nodes)
        while stack:
            node = stack.pop(0)
            if include_self or node not in self.nodes:
                result.append(node)
            stack[0:0] = node.children
        return result

    def _css(self, selector):
        if selector.startswith("#"):
            return [node for node in self._descendants(include_self=True) if node.attributes.get("id") == selector[1:]]
        tag_match = re.match(r"^([a-z]+)?(?:\[([^=\]]+)=\"([^\"]*)\"\])?$", selector)
        if not tag_match:
            raise AssertionError(f"unsupported fake CSS selector: {selector}")
        tag, name, value = tag_match.groups()
        return [
            node for node in self._descendants(include_self=True)
            if (tag is None or node.tag == tag) and (name is None or node.attributes.get(name) == value)
        ]

    def _xpath(self, expression):
        if expression == "ancestor::section[1]":
            result = []
            for node in self.nodes:
                parent = node.parent
                while parent is not None and parent.tag != "section":
                    parent = parent.parent
                if parent is not None:
                    result.append(parent)
            return result
        id_match = re.fullmatch(r"//\*\[@id=(.+)\]", expression)
        if id_match:
            identifier = self._xpath_literal_value(id_match.group(1))
            return [node for node in self._descendants(include_self=True) if node.attributes.get("id") == identifier]
        match = re.fullmatch(r"//([a-z]+)\[@([^=]+)='([^']+)'\]", expression)
        if not match:
            raise AssertionError(f"unsupported fake XPath selector: {expression}")
        tag, name, value = match.groups()
        return [node for node in self._descendants(include_self=True) if node.tag == tag and node.attributes.get(name) == value]

    @staticmethod
    def _xpath_literal_value(expression):
        values = [single or double for single, double in re.findall(r"'([^']*)'|\"([^\"]*)\"", expression)]
        return "".join(values)

    @staticmethod
    def _matches_name(node, name, exact):
        actual = node.attributes.get("aria-label", node.text)
        if hasattr(name, "search"):
            return bool(name.search(actual))
        return actual == name if exact else name in actual


class FakePage(FakeLocator):
    def __init__(self, root):
        super().__init__([root])
        self.root = root
        self.mutations = []

    async def evaluate(self, expression):
        assert expression == "document.documentElement.outerHTML"
        return self._serialize(self.root)

    @classmethod
    def _serialize(cls, node):
        attributes = "".join(f" {key}=\"{value}\"" for key, value in sorted(node.attributes.items()))
        return f"<{node.tag}{attributes}>{node.text}{''.join(cls._serialize(child) for child in node.children)}</{node.tag}>"


class ReorderingNthArticleLocator:
    def __init__(self, column, index):
        self.column = column
        self.index = index

    def _current(self):
        return [node for node in self.column.children if node.tag == "article"][self.index]

    def locator(self, selector):
        return FakeLocator([self._current()]).locator(selector)

    async def count(self):
        return 1

    async def is_visible(self):
        return self._current().visible

    async def is_enabled(self):
        return self._current().enabled

    async def get_attribute(self, name):
        return self._current().attributes.get(name)

    async def bounding_box(self):
        return self._current().rect

    async def evaluate(self, expression):
        return await FakeLocator([self._current()]).evaluate(expression)


class ReorderingArticleLocator:
    def __init__(self, column):
        self.column = column

    def _articles(self):
        return [node for node in self.column.children if node.tag == "article"]

    async def count(self):
        return len(self._articles())

    def nth(self, index):
        return ReorderingNthArticleLocator(self.column, index)


class ReorderingContainerLocator(FakeLocator):
    def nth(self, index):
        assert index == 0
        return self

    def locator(self, selector):
        if selector == 'article[data-e2e="recommend-list-item-container"]':
            return ReorderingArticleLocator(self.nodes[0])
        return super().locator(selector)


class ReorderingPage(FakePage):
    def locator(self, selector):
        locator = super().locator(selector)
        if selector == "#column-list-container":
            return ReorderingContainerLocator(locator.nodes)
        return locator


def make_page():
    root = FakeNode("html")
    column = root.append(FakeNode("div", {"id": "column-list-container"}, rect={"x": 0, "y": 1, "width": 400, "height": 800}))
    one = column.append(FakeNode("article", {"id": "one-column-item-4", "data-e2e": "recommend-list-item-container"}, rect={"x": 0, "y": 0, "width": 400, "height": 400}))
    one.append(FakeNode("div", {"data-e2e": "comment-icon", "role": "button"}))
    two = column.append(FakeNode("article", {"id": "one-column-item-5", "data-e2e": "recommend-list-item-container"}, rect={"x": 0, "y": 400, "width": 400, "height": 400}))
    two.append(FakeNode("div", {"data-e2e": "comment-icon", "role": "button"}))
    hidden = root.append(FakeNode("section", {"data-comment-panel": "", "hidden": ""}, visible=False))
    hidden.append(FakeNode("div", {"data-e2e": "comment-input"}, visible=False))
    panel = root.append(FakeNode("section", {"data-comment-panel": ""}))
    comment_input = panel.append(FakeNode("div", {"data-e2e": "comment-input"}))
    input_wrapper = comment_input.append(FakeNode("div"))
    nested_wrapper = input_wrapper.append(FakeNode("div"))
    nested_wrapper.append(
        FakeNode(
            "div",
            {"contenteditable": "true", "role": "textbox"},
        )
    )
    panel.append(FakeNode("button", {"data-e2e": "comment-post", "role": "button"}, text="Post"))
    return FakePage(root), two


@pytest.fixture
def definitions():
    return normalize_element_definitions({
        "评论入口": {
            "scope": "active_video",
            "locators": [{"id": "entry", "type": "attribute", "name": "data-e2e", "value": "comment-icon", "enabled": True}],
        },
        "页面评论入口": {
            "scope": "page",
            "locators": [{"id": "entry", "type": "attribute", "name": "data-e2e", "value": "comment-icon", "enabled": True}],
        },
        "评论输入框": {
            "scope": "visible_comment_panel",
            "locators": [{"id": "input", "type": "attribute", "name": "data-e2e", "value": "comment-input", "enabled": True, "descendant": {"type": "attribute", "name": "contenteditable", "value": "true", "role": "textbox"}}],
        },
        "评论提交": {
            "scope": "visible_comment_panel",
            "locators": [
                {"id": "missing-semantic", "type": "attribute", "name": "data-e2e", "value": "missing", "enabled": True},
                {"id": "xpath-fallback", "type": "xpath", "value": "//button[@data-e2e='comment-post']", "enabled": True, "fallback": True},
            ],
        },
        "CSS提交": {
            "scope": "visible_comment_panel",
            "locators": [{"id": "css-submit", "type": "css", "value": "button[data-e2e=\"comment-post\"]", "enabled": True}],
        },
        "Role提交": {
            "scope": "visible_comment_panel",
            "locators": [{"id": "role-submit", "type": "role", "role": "button", "name": "os", "name_mode": "contains", "enabled": True}],
        },
    })


def test_active_video_scope_uses_center_article(definitions):
    page, _ = make_page()

    result = asyncio.run(resolve_element(page, "评论入口", definitions["评论入口"]))

    assert asyncio.run(result.locator.get_attribute("data-e2e")) == "comment-icon"
    assert result.diagnostics["scope_target"] == "one-column-item-5"


def test_page_scope_rejects_ambiguous_candidate(definitions):
    page, _ = make_page()

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_element(page, "页面评论入口", definitions["页面评论入口"]))

    assert caught.value.code == "element_candidate_ambiguous"
    assert caught.value.diagnostics["candidates"][0]["raw_count"] == 2


def test_inspection_does_not_focus_scroll_click_or_type(definitions):
    page, _ = make_page()
    before = asyncio.run(page.evaluate("document.documentElement.outerHTML"))

    result = asyncio.run(inspect_element(page, "评论输入框", definitions["评论输入框"]))

    after = asyncio.run(page.evaluate("document.documentElement.outerHTML"))
    assert result["status"] == "ok"
    assert before == after
    assert page.mutations == []
    assert "selector" not in str(result).casefold()
    assert "comment-post" not in str(result)


def test_semantic_miss_uses_ordered_xpath_fallback(definitions):
    page, _ = make_page()

    result = asyncio.run(resolve_element(page, "评论提交", definitions["评论提交"]))

    assert result.candidate["id"] == "xpath-fallback"
    assert asyncio.run(result.locator.get_attribute("data-e2e")) == "comment-post"


def test_visible_probe_resolution_accepts_unique_disabled_submit(definitions):
    page, _ = make_page()
    submit = page.locator('button[data-e2e="comment-post"]').nodes[0]
    submit.enabled = False

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(
            resolve_element(
                page,
                "CSS提交",
                definitions["CSS提交"],
            )
        )
    visible = asyncio.run(
        resolve_visible_element(
            page,
            "CSS提交",
            definitions["CSS提交"],
        )
    )
    inspected = asyncio.run(
        inspect_visible_element(
            page,
            "CSS提交",
            definitions["CSS提交"],
        )
    )

    assert caught.value.code == "element_candidate_not_found"
    assert visible.candidate["id"] == "css-submit"
    assert inspected["status"] == "ok"
    assert (
        inspected["diagnostics"]["candidates"][0]["actionable_count"]
        == 0
    )


def test_visible_probe_resolution_accepts_covered_inspect_only_input(
    definitions,
):
    page, _ = make_page()
    textbox = page.locator('[contenteditable="true"]').nodes[0]
    textbox.state = "covered"
    input_definition = next(
        definition
        for definition in definitions.values()
        if any(
            isinstance(locator.get("descendant"), dict)
            for locator in definition["locators"]
        )
    )

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_element(page, "comment-input", input_definition))
    visible = asyncio.run(
        resolve_visible_element(page, "comment-input", input_definition)
    )

    assert caught.value.code == "element_candidate_not_found"
    assert visible.candidate["id"] == "input"


def test_second_resolution_tracks_replaced_dom_node(definitions):
    page, active_article = make_page()
    first = asyncio.run(resolve_element(page, "评论入口", definitions["评论入口"]))
    old_node = first.locator.nodes[0]
    active_article.children[0] = FakeNode("div", {"data-e2e": "comment-icon", "role": "button"})
    active_article.children[0].parent = active_article

    second = asyncio.run(resolve_element(page, "评论入口", definitions["评论入口"]))

    assert second.locator.nodes[0] is not old_node


def test_ambiguous_resolution_never_uses_first(definitions, monkeypatch):
    page, _ = make_page()

    def forbidden_first(self):
        raise AssertionError("resolver must not guess with first()")

    monkeypatch.setattr(FakeLocator, "first", forbidden_first, raising=False)
    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_element(page, "页面评论入口", definitions["页面评论入口"]))

    assert caught.value.code == "element_candidate_ambiguous"


def test_visible_comment_panel_scope_rejects_two_visible_inputs(definitions):
    page, _ = make_page()
    duplicate_panel = page.root.append(FakeNode("section", {"data-comment-panel": ""}))
    duplicate_panel.append(FakeNode("div", {"data-e2e": "comment-input"}))

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_element(page, "评论输入框", definitions["评论输入框"]))

    assert caught.value.code == "element_scope_not_found"
    assert caught.value.diagnostics["visible_input_count"] == 2


def test_failed_inspection_diagnostics_exclude_selector_and_page_text():
    page, _ = make_page()
    definition = normalize_element_definitions({
        "private-alias": {
            "scope": "page",
            "locators": [{"id": "private-id", "type": "css", "value": "button[data-e2e=\"private-selector-sentinel\"]", "enabled": True}],
        },
    })["private-alias"]

    result = asyncio.run(inspect_element(page, "private-alias", definition))

    assert result["status"] == "error"
    assert "private-selector-sentinel" not in str(result)
    assert "Post" not in str(result)
    assert "<html" not in str(result)


@pytest.mark.parametrize("alias", ["CSS提交", "Role提交"])
def test_css_and_role_candidates_resolve_within_visible_panel(definitions, alias):
    page, _ = make_page()

    result = asyncio.run(resolve_element(page, alias, definitions[alias]))

    assert asyncio.run(result.locator.get_attribute("data-e2e")) == "comment-post"


def test_inspection_converts_locator_errors_to_safe_result():
    page, _ = make_page()
    selector = 'button[data-e2e="private-selector-sentinel"]'
    page.root.raise_for_selector = selector
    definition = normalize_element_definitions({
        "private-alias": {
            "scope": "page",
            "locators": [{"id": "private-id", "type": "css", "value": selector, "enabled": True}],
        },
    })["private-alias"]

    result = asyncio.run(inspect_element(page, "private-alias", definition))

    assert result["status"] == "error"
    assert result["code"] == "element_resolution_failed"
    assert "private-selector-sentinel" not in str(result)
    assert "page-text-sentinel" not in str(result)


@pytest.mark.parametrize("state", ["hidden", "exiting", "detached", "covered"])
def test_visible_comment_panel_rejects_unusable_panel_states(definitions, state):
    page, _ = make_page()
    page.locator("section").nodes[1].state = state

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_element(page, "评论输入框", definitions["评论输入框"]))

    assert caught.value.code == "element_scope_not_found"


def test_active_video_scope_rejects_duplicate_article_id():
    page, _ = make_page()
    page.locator("article").nodes[0].attributes["id"] = "one-column-item-5"

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_scope(page, "active_video"))

    assert caught.value.code == "element_scope_not_found"


def test_active_video_scope_never_interpolates_special_character_id():
    page, _ = make_page()
    special_id = 'one-column-item-5"]:has(*)'
    page.locator("article").nodes[1].attributes["id"] = special_id

    scope_locator, diagnostics = asyncio.run(resolve_scope(page, "active_video"))

    assert asyncio.run(scope_locator.count()) == 1
    assert diagnostics["scope_target"] == special_id


def test_active_video_scope_locator_stays_bound_after_live_collection_reorders():
    page, _ = make_page()
    live_page = ReorderingPage(page.root)

    scope_locator, _ = asyncio.run(resolve_scope(live_page, "active_video"))
    column = live_page.locator("#column-list-container").nodes[0]
    column.children[0], column.children[1] = column.children[1], column.children[0]

    assert asyncio.run(scope_locator.get_attribute("id")) == "one-column-item-5"


def test_active_video_scope_handles_id_with_single_and_double_quotes():
    page, _ = make_page()
    quoted_id = 'video\'"item'
    page.locator("article").nodes[1].attributes["id"] = quoted_id

    scope_locator, diagnostics = asyncio.run(resolve_scope(page, "active_video"))

    assert asyncio.run(scope_locator.count()) == 1
    assert asyncio.run(scope_locator.get_attribute("id")) == quoted_id
    assert diagnostics["scope_target"] == quoted_id


@pytest.mark.parametrize(
    ("attributes", "state"),
    [
        ({"class": "comment-sidebar-transition-exit-active"}, "ready"),
        ({"class": "comment-sidebar-transition-leave-active"}, "ready"),
        ({"class": "comment-sidebar-transition-enter-active"}, "ready"),
        ({"aria-hidden": "true"}, "ready"),
        ({"inert": ""}, "ready"),
        ({}, "display_none"),
        ({}, "invisible"),
        ({}, "opacity_zero"),
        ({}, "pointer_none"),
    ],
)
def test_visible_comment_panel_rejects_lifecycle_and_interaction_states(definitions, attributes, state):
    page, _ = make_page()
    panel = page.locator("section").nodes[1]
    panel.attributes.update(attributes)
    panel.state = state

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_element(page, "评论输入框", definitions["评论输入框"]))

    assert caught.value.code == "element_scope_not_found"


def test_visible_comment_panel_accepts_enter_done(definitions):
    page, _ = make_page()
    page.locator("section").nodes[1].attributes["class"] = "comment-sidebar-transition-enter-done"

    result = asyncio.run(resolve_element(page, "评论输入框", definitions["评论输入框"]))

    assert result.diagnostics["scope_target"] == "visible_comment_panel"


@pytest.mark.parametrize(
    ("scope", "selector"),
    [
        ("active_video", "#column-list-container"),
        ("visible_comment_panel", '[data-e2e="comment-input"]'),
    ],
)
def test_public_resolve_scope_hides_locator_exception_details(scope, selector):
    page, _ = make_page()
    page.root.raise_for_selector = selector

    with pytest.raises(LocatorResolutionError) as caught:
        asyncio.run(resolve_scope(page, scope))

    assert caught.value.code == "element_resolution_failed"
    assert caught.value.diagnostics == {"phase": "scope_query"}
    assert "selector failure" not in str(caught.value)
    assert "page-text-sentinel" not in str(caught.value)
