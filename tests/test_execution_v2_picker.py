import asyncio

import pytest

from execution_v2.models import BrowserBinding
from execution_v2.picker import PickerError, PickerService, generate_locator_candidates
from execution_v2.store import ExecutionStore


class FakePage:
    def __init__(self):
        self.url = "https://www.tiktok.com/@example"
        self.bindings = {}
        self.init_scripts = []
        self.evaluate_calls = []

    async def expose_binding(self, name, callback):
        self.bindings[name] = callback

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    async def evaluate(self, script):
        self.evaluate_calls.append(script)


class Resolver:
    def __init__(self, *, error=None):
        self.definitions = []
        self.error = error

    async def resolve(self, page, definition, *, require_editable=False):
        self.definitions.append((page, definition, require_editable))
        if self.error:
            raise self.error
        return object()


def binding(page):
    return BrowserBinding("profile-1", "ws://profile-1", object(), object(), page)


def payload(**overrides):
    value = {
        "tag": "svg",
        "attributes": {"data-e2e": "comment-icon", "aria-label": "Open comments"},
        "role": "button",
        "name": "Open comments",
        "text_preview": "Open comments",
        "frame_path": [],
        "original_fingerprint": "orig-1",
        "actionable_ancestor_fingerprint": "button-1",
        "actionable_tag": "button",
        "bounding_box": {"x": 10, "y": 20, "width": 30, "height": 40},
        "unique_css": "button.comment-button",
        "relative_xpath": "//button[@aria-label='Open comments']",
    }
    value.update(overrides)
    return value


async def capture(page, item):
    await page.bindings["__executionV2PickerEvent"]({}, item)


def run(awaitable):
    return asyncio.run(awaitable)


def test_picker_explicit_start_injects_once_and_allows_multiple_saves_without_close():
    page = FakePage()
    resolver = Resolver()
    closed = []

    async def scenario():
        session = await PickerService(resolver=resolver).start(
            binding(page), "https://www.tiktok.com/", close=lambda: closed.append(True)
        )
        assert len(page.init_scripts) == 1
        await capture(page, payload())
        await session.next_selection()
        first = await session.save_selection("评论入口", "action", "click")
        await capture(page, payload(original_fingerprint="orig-2", actionable_ancestor_fingerprint="button-2"))
        await session.next_selection()
        second = await session.save_selection("评论提交", "action", "click")
        assert first["definition"]["locators"][0] == {
            "type": "css", "value": '[data-e2e="comment-icon"]', "priority": 10
        }
        assert second["name"] == "评论提交"
        assert len(session.selections) == 2
        assert closed == []
        assert len(resolver.definitions) == 2

    run(scenario())


def test_picker_sanitizes_browser_payload_and_orders_candidates():
    page = FakePage()
    resolver = Resolver()

    async def scenario():
        session = await PickerService(resolver=resolver).start(binding(page), "https://www.tiktok.com/")
        await capture(page, payload(cookie="secret", html="<body>secret</body>", password="secret"))
        selected = await session.next_selection()
        assert "cookie" not in selected
        assert "html" not in selected
        saved = await session.save_selection("评论入口", "action", "click")
        definition = saved["definition"]
        assert [item["priority"] for item in definition["locators"]] == sorted(
            item["priority"] for item in definition["locators"]
        )
        assert all("secret" not in str(item) for item in saved.values())
        assert definition["diagnostic_metadata"]["original_fingerprint"] == "orig-1"

    run(scenario())


def test_picker_rejects_empty_or_unvalidated_candidate_without_closing():
    page = FakePage()
    closed = []

    async def scenario():
        session = await PickerService(resolver=Resolver(error=RuntimeError("no_valid_locator"))).start(
            binding(page), "https://www.tiktok.com/", close=lambda: closed.append(True)
        )
        await capture(page, payload(attributes={}, role="", name="", unique_css="", relative_xpath="", text_preview=""))
        await session.next_selection()
        with pytest.raises(PickerError, match="picker_locator_missing"):
            await session.save_selection("没有路径", "action", "click")
        await capture(page, payload())
        await session.next_selection()
        with pytest.raises(PickerError, match="picker_locator_invalid"):
            await session.save_selection("没有路径", "action", "click")
        assert session.selections == ()
        assert closed == []

    run(scenario())


def test_picker_install_bundles_cypress_for_production_and_picker_definition_persists(tmp_path):
    page = FakePage()
    resolver = Resolver()

    async def scenario():
        session = await PickerService(resolver=resolver).start(binding(page), "https://www.tiktok.com/")
        assert "__executionV2UniqueSelector" in page.init_scripts[0]
        assert "@cypress/unique-selector/lib/index" in page.init_scripts[0]
        await capture(page, payload())
        await session.next_selection()
        return await session.save_selection("评论入口", "action", "click")

    saved = run(scenario())
    store = ExecutionStore(tmp_path / "execution_v2.db")
    store.initialize()
    created = store.create_element("element-1", saved["name"], saved["purpose"], saved["kind"], saved["definition"])
    assert created["definition"]["screenshot_path"] == ""


def test_picker_keeps_cypress_nth_child_fallback_for_strict_validation():
    candidates = generate_locator_candidates(
        {
            "tag": "button", "attributes": {}, "role": "", "name": "", "text_preview": "",
            "relative_xpath": "", "unique_css": ".toolbar > :nth-child(2)",
        }
    )
    assert candidates == [{"type": "css", "value": ".toolbar > :nth-child(2)", "priority": 50}]


def test_picker_uses_store_enums_and_requires_editability_by_kind():
    page = FakePage()
    resolver = Resolver()

    async def scenario():
        session = await PickerService(resolver=resolver).start(binding(page), "https://www.tiktok.com/")
        await capture(page, payload())
        await session.next_selection()
        await session.save_selection("评论输入", "action", "input")

    run(scenario())
    assert resolver.definitions[0][2] is True


def test_picker_preserves_empty_contenteditable_and_builds_exact_candidate():
    page = FakePage()
    resolver = Resolver()

    async def scenario():
        session = await PickerService(resolver=resolver).start(
            binding(page), "https://www.tiktok.com/"
        )
        await capture(
            page,
            payload(
                tag="div",
                actionable_tag="div",
                attributes={"contenteditable": "", "role": "textbox"},
                role="textbox",
                name="",
                text_preview="",
                unique_css=".comment-editor",
                relative_xpath="",
            ),
        )
        selected = await session.next_selection()
        assert selected["attributes"]["contenteditable"] == ""
        return await session.save_selection("评论输入", "action", "input")

    saved = run(scenario())
    assert {
        "type": "css", "value": '[contenteditable=""]', "priority": 21
    } in saved["definition"]["locators"]


def test_input_picker_failure_is_specific_and_selection_can_retry():
    page = FakePage()
    resolver = Resolver(error=RuntimeError("not editable"))

    async def scenario():
        session = await PickerService(resolver=resolver).start(
            binding(page), "https://www.tiktok.com/"
        )
        await capture(page, payload())
        await session.next_selection()
        with pytest.raises(PickerError, match="picker_input_target_not_editable"):
            await session.save_selection("评论输入", "action", "input")
        resolver.error = None
        saved = await session.save_selection("评论输入", "action", "input")
        assert saved["kind"] == "input"

    run(scenario())


def test_picker_finish_or_cancel_removes_overlay_and_closes_through_callback():
    async def scenario():
        page = FakePage()
        closed = []
        session = await PickerService(resolver=Resolver()).start(
            binding(page), "https://www.tiktok.com/", close=lambda: closed.append("finish")
        )
        await session.finish()
        assert closed == ["finish"]
        assert any("uninstall" in script for script in page.evaluate_calls)
        with pytest.raises(PickerError, match="picker_not_active"):
            await session.next_selection()

        page = FakePage()
        session = await PickerService(resolver=Resolver()).start(
            binding(page), "https://www.tiktok.com/", close=lambda: closed.append("cancel")
        )
        await session.cancel()
        assert closed == ["finish", "cancel"]

    run(scenario())
