import asyncio

import pytest

from selector_probe.validator import (
    ValidationRejected,
    _resource_check,
    validate_element,
    validate_locator,
)


def visible_node(**overrides):
    node = {"visible": True, "enabled": True, "hit_target": True}
    node.update(overrides)
    return node


class XPathLocator:
    def __init__(self, matches):
        self.matches = list(matches)

    async def all(self):
        return list(self.matches)


class FakePage:
    def __init__(self, css_matches=None, xpath_matches=None, query_error=None):
        self.css_matches = dict(css_matches or {})
        self.xpath_matches = dict(xpath_matches or {})
        self.query_error = query_error
        self.queries = []

    async def query_selector_all(self, selector):
        self.queries.append(selector)
        if self.query_error is not None:
            raise self.query_error
        if selector.startswith("xpath="):
            return list(self.xpath_matches.get(selector[6:], []))
        return list(self.css_matches.get(selector, []))

    def locator(self, selector):
        self.queries.append(selector)
        if self.query_error is not None:
            raise self.query_error
        assert selector.startswith("xpath=")
        return XPathLocator(self.xpath_matches.get(selector[6:], []))


def test_validator_ignores_display_role_and_name_metadata():
    async def scenario():
        selector = '[data-e2e="comment-icon"]'
        page = FakePage(css_matches={selector: [visible_node()]})
        result = await validate_element(
            page,
            {
                "display": {"role": "link", "name": "Changed Name"},
                "role": "anything",
                "name": "anything else",
                "locators": [{"type": "css", "value": selector}],
            },
        )
        assert result["status"] == "passed"
        assert result["selected_locator"] == {
            "type": "css",
            "value": selector,
        }
        assert result["selected_locator_index"] == 0
        assert page.queries == [selector]

    asyncio.run(scenario())


def test_validator_reports_zero_and_ambiguous_matches():
    async def scenario():
        zero = await validate_locator(
            FakePage(),
            {"type": "css", "value": '[data-e2e="missing"]'},
        )
        many = await validate_locator(
            FakePage(css_matches={"button": [visible_node(), visible_node()]}),
            {"type": "css", "value": "button"},
        )
        assert zero["failure_code"] == "selector_zero_match"
        assert zero["match_count"] == 0
        assert many["failure_code"] == "selector_ambiguous"
        assert many["match_count"] == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("node", "failure_code"),
    [
        (visible_node(visible=False), "selector_hidden"),
        (visible_node(enabled=False), "selector_disabled"),
        (visible_node(hit_target=False), "selector_hit_test_failed"),
    ],
)
def test_validator_requires_visible_enabled_center_hit(node, failure_code):
    async def scenario():
        result = await validate_locator(
            FakePage(css_matches={"button": [node]}),
            {"type": "css", "value": "button"},
        )
        assert result["failure_code"] == failure_code
        assert set(result) == {
            "status",
            "failure_code",
            "match_count",
            "visible",
            "enabled",
            "hit_target",
        }

    asyncio.run(scenario())


def test_validator_supports_xpath_and_saved_fallback_order():
    async def scenario():
        xpath = '//*[@data-e2e="comment-icon"]'
        page = FakePage(
            css_matches={"button": []},
            xpath_matches={xpath: [visible_node()]},
        )
        result = await validate_element(
            page,
            {
                "locators": [
                    {"type": "css", "value": "button"},
                    {"type": "xpath", "value": xpath},
                    {"type": "css", "value": "input"},
                ]
            },
        )
        assert result["status"] == "passed"
        assert result["selected_locator_index"] == 1
        assert [item["failure_code"] for item in result["locator_results"]] == [
            "selector_zero_match",
            "",
        ]
        assert page.queries == ["button", f"xpath={xpath}"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "locator",
    [
        {"type": "role", "value": "button"},
        {"type": "css", "value": "text=Comments"},
        {"type": "xpath", "value": "/html/body/button"},
        {"type": "xpath", "value": "//button[contains(., 'Comments')]"},
        {"type": "css", "value": " button"},
    ],
)
def test_validator_rejects_non_inventory_locator_before_query(locator):
    async def scenario():
        page = FakePage()
        result = await validate_locator(page, locator)
        assert result["failure_code"] == "selector_query_invalid"
        assert page.queries == []

    asyncio.run(scenario())


def test_validator_converts_query_exception_to_bounded_failure():
    async def scenario():
        result = await validate_locator(
            FakePage(query_error=RuntimeError("browser detail")),
            {"type": "css", "value": "button"},
        )
        assert result == {
            "status": "failed",
            "failure_code": "selector_query_invalid",
            "match_count": 0,
            "visible": False,
            "enabled": False,
            "hit_target": False,
        }
        assert "browser detail" not in str(result)

    asyncio.run(scenario())


def test_resource_check_rejects_cycles_and_non_finite_values():
    cyclic = []
    cyclic.append(cyclic)
    for value in (cyclic, {"value": float("inf")}):
        with pytest.raises(ValidationRejected) as caught:
            _resource_check(
                value,
                code="evidence_resource_limit",
                max_nodes=32,
                max_containers=8,
                max_depth=4,
                max_string_bytes=128,
            )
        assert caught.value.code == "evidence_resource_limit"


def test_validator_module_has_no_semantic_pipeline_exports():
    import selector_probe.validator as module

    for name in (
        "validate_two_rounds",
        "validate_bundle_on_page",
        "ResetCapture",
        "ValidationEvidence",
    ):
        assert not hasattr(module, name)
