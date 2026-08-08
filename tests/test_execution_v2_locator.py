import asyncio

import pytest

from execution_v2.locator import LocatorResolutionError, StrictLocatorResolver


class FakeHandle:
    def __init__(
        self, name, *, visible=True, box=None, disabled=False, editable=True, child_frame=None
    ):
        self.name = name
        self.visible = visible
        self.box = box if box is not None else {"x": 10, "y": 20, "width": 100, "height": 40}
        self.disabled = disabled
        self.editable = editable
        self.child_frame = child_frame

    async def is_visible(self):
        return self.visible

    async def bounding_box(self):
        return self.box

    async def is_disabled(self):
        return self.disabled

    async def is_editable(self):
        return self.editable

    async def content_frame(self):
        return self.child_frame


class FakeLocator:
    def __init__(self, handles=()):
        self.handles = list(handles)

    async def count(self):
        return len(self.handles)

    async def element_handle(self):
        return self.handles[0] if self.handles else None

    def nth(self, index):
        return FakeLocator(self.handles[index:index + 1])


class FakeFrame:
    def __init__(self, css=None, xpath=None, roles=None, viewport=None):
        self.css = css or {}
        self.xpath = xpath or {}
        self.roles = roles or {}
        self.viewport = viewport or {"width": 200, "height": 120}
        self.calls = []

    def locator(self, selector):
        self.calls.append(("locator", selector))
        if selector.startswith("xpath="):
            return self.xpath.get(selector[6:], FakeLocator())
        return self.css.get(selector, FakeLocator())

    def get_by_role(self, role, *, name, exact):
        self.calls.append(("role", role, name, exact))
        return self.roles.get((role, name), FakeLocator())

    async def evaluate(self, script, pair=None):
        if "window.innerWidth" in script:
            return self.viewport
        assert script == "(pair) => pair[0] === pair[1]"
        return pair[0] is pair[1]


def definition(*locators, frame_path=()):
    return {"frame_path": list(frame_path), "locators": list(locators)}


def resolve(page, definition_value, **kwargs):
    return asyncio.run(StrictLocatorResolver().resolve(page, definition_value, **kwargs))


@pytest.mark.parametrize(
    ("handles", "code"),
    [([], "locator_not_found"), ([FakeHandle("one"), FakeHandle("two")], "locator_not_unique")],
)
def test_resolver_rejects_zero_or_many_matches(handles, code):
    page = FakeFrame(css={"#target": FakeLocator(handles)})

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(page, definition({"type": "css", "value": "#target", "priority": 1}))

    assert caught.value.code == "no_valid_locator"
    assert caught.value.diagnostics[0]["code"] == code


@pytest.mark.parametrize(
    ("handle", "code"),
    [
        (FakeHandle("hidden", visible=False), "locator_not_visible"),
        (FakeHandle("empty", box={"x": 0, "y": 0, "width": 0, "height": 10}), "locator_zero_box"),
        (FakeHandle("disabled", disabled=True), "locator_disabled"),
    ],
)
def test_resolver_rejects_non_operable_elements(handle, code):
    page = FakeFrame(css={"#target": FakeLocator([handle])})

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(page, definition({"type": "css", "value": "#target", "priority": 1}))

    assert caught.value.diagnostics[0]["code"] == code


def test_resolver_requires_editable_input_targets():
    page = FakeFrame(css={"#target": FakeLocator([FakeHandle("readonly", editable=False)])})

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(
            page,
            definition({"type": "css", "value": "#target", "priority": 1}),
            require_editable=True,
        )

    assert caught.value.diagnostics[0]["code"] == "locator_not_editable"


def test_resolver_uses_highest_priority_when_candidates_resolve_same_handle():
    target = FakeHandle("target")
    page = FakeFrame(
        css={"#target": FakeLocator([target])},
        xpath={"//button": FakeLocator([target])},
    )

    resolved = resolve(
        page,
        definition(
            {"type": "xpath", "value": "//button", "priority": 20},
            {"type": "css", "value": "#target", "priority": 10},
        ),
    )

    assert resolved.handle is target
    assert resolved.locator_type == "css"
    assert resolved.box == {"x": 10, "y": 20, "width": 100, "height": 40}
    assert resolved.bounding_box == resolved.box
    assert len(resolved.diagnostics) == 2


def test_resolver_rejects_candidates_that_point_to_different_handles():
    page = FakeFrame(
        css={"#one": FakeLocator([FakeHandle("one")])},
        xpath={"//two": FakeLocator([FakeHandle("two")])},
    )

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(
            page,
            definition(
                {"type": "css", "value": "#one", "priority": 10},
                {"type": "xpath", "value": "//two", "priority": 20},
            ),
        )

    assert caught.value.code == "locator_conflict"


def test_resolver_uses_public_css_xpath_role_and_frame_traversal_without_first_or_nth():
    target = FakeHandle("target")
    leaf = FakeFrame(roles={("button", "Open comments"): FakeLocator([target])})
    iframe_handle = FakeHandle("iframe", child_frame=leaf)
    page = FakeFrame(css={"iframe#comments": FakeLocator([iframe_handle])})

    resolved = resolve(
        page,
        definition(
            {"type": "role", "role": "button", "name": "Open comments", "priority": 1},
            frame_path=("iframe#comments",),
        ),
    )

    assert resolved.handle is target
    assert page.calls == [("locator", "iframe#comments")]
    assert leaf.calls == [("role", "button", "Open comments", True)]


def test_resolver_fails_explicitly_when_frame_cannot_be_entered():
    page = FakeFrame(css={"iframe#missing": FakeLocator()})

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(
            page,
            definition({"type": "css", "value": "#target", "priority": 1}, frame_path=("iframe#missing",)),
        )

    assert caught.value.code == "frame_path_invalid"


def test_action_resolution_replaces_fixed_video_item_with_current_viewport_anchor():
    old = FakeHandle(
        "old", box={"x": 10, "y": 300, "width": 100, "height": 80}
    )
    current = FakeHandle(
        "current", box={"x": 20, "y": 10, "width": 120, "height": 90}
    )
    fixed = '#one-column-item-0 > [data-e2e="feed-video"] > .random-class'
    page = FakeFrame(css={
        fixed: FakeLocator([old]),
        '[data-e2e="feed-video"]': FakeLocator([old, current]),
    })

    resolved = resolve(
        page,
        definition({"type": "css", "value": fixed, "priority": 10}),
        require_in_viewport=True,
        allow_viewport_fallback=True,
    )

    assert resolved.handle is current
    assert resolved.locator_type == "css_viewport"


@pytest.mark.parametrize(
    ("handles", "code"),
    [
        ([], "current_viewport_target_not_found"),
        (
            [
                FakeHandle(
                    "one", box={"x": 10, "y": 10, "width": 30, "height": 30}
                ),
                FakeHandle(
                    "two", box={"x": 60, "y": 10, "width": 30, "height": 30}
                ),
            ],
            "current_viewport_target_ambiguous",
        ),
    ],
)
def test_current_viewport_fallback_fails_closed_for_zero_or_many_candidates(
    handles, code
):
    fixed = '#one-column-item-0 [data-e2e="comment-icon"]'
    page = FakeFrame(css={
        fixed: FakeLocator(),
        '[data-e2e="comment-icon"]': FakeLocator(handles),
    })

    with pytest.raises(LocatorResolutionError) as caught:
        resolve(
            page,
            definition({"type": "css", "value": fixed, "priority": 10}),
            require_in_viewport=True,
            allow_viewport_fallback=True,
        )

    assert caught.value.code == code


def test_readiness_resolution_never_uses_current_viewport_fallback():
    old = FakeHandle(
        "old", box={"x": 10, "y": 300, "width": 100, "height": 80}
    )
    fixed = '#one-column-item-0 [data-e2e="feed-video"]'
    page = FakeFrame(css={
        fixed: FakeLocator([old]),
        '[data-e2e="feed-video"]': FakeLocator([FakeHandle("current")]),
    })

    resolved = resolve(
        page, definition({"type": "css", "value": fixed, "priority": 10})
    )

    assert resolved.handle is old
    assert page.calls == [("locator", fixed)]
