import copy
from dataclasses import FrozenInstanceError

import pytest

from browser_element_schema import TIKTOK_COMMENT_TEMPLATE
import selector_probe.contracts as contracts_module
from selector_probe.contracts import (
    ElementContract,
    default_tiktok_contracts,
    normalize_contracts,
)


def contract_value(**overrides):
    value = {
        "intent": "inspect a visible control",
        "required_state": "feed_ready",
        "scope": "active_video",
        "accepted_roles": ["button"],
        "accepted_names": {"mode": "contains", "values": ["Comments"]},
        "preferred_attributes": ["data-e2e", "aria-label"],
        "postcondition": "",
        "probe_action": "inspect_only",
    }
    value.update(overrides)
    return value


def test_default_contracts_follow_real_tiktok_aliases_and_safe_state_graph():
    contracts = default_tiktok_contracts()
    aliases = tuple(TIKTOK_COMMENT_TEMPLATE)

    assert tuple(contracts) == aliases
    assert contracts[aliases[0]].required_state == "feed_ready"
    assert contracts[aliases[0]].postcondition == "comment_panel_open"
    assert contracts[aliases[1]].required_state == "comment_panel_open"
    assert contracts[aliases[2]].required_state == "comment_panel_open"
    assert contracts[aliases[2]].probe_action == "inspect_only"
    assert "Publish" in contracts[aliases[2]].accepted_names


def test_contracts_are_immutable_and_use_immutable_nested_values():
    contract = next(iter(default_tiktok_contracts().values()))

    assert isinstance(contract, ElementContract)
    assert isinstance(contract.accepted_roles, tuple)
    assert isinstance(contract.accepted_names, tuple)
    assert isinstance(contract.preferred_attributes, tuple)
    with pytest.raises(FrozenInstanceError):
        contract.scope = "page"


def test_dynamic_element_contract_can_be_added_without_code_changes():
    result = normalize_contracts(
        {
            "关闭评论面板": contract_value(
                intent="close the visible comment panel",
                required_state="comment_panel_open",
                scope="visible_comment_panel",
                accepted_names={"mode": "locale_map", "values": ["Close", "关闭"]},
                postcondition="feed_ready",
                probe_action="inspect_only",
            )
        }
    )

    assert result["关闭评论面板"].accepted_names == ("Close", "关闭")
    assert result["关闭评论面板"].probe_action == "inspect_only"


@pytest.mark.parametrize("probe_action", ["input", "type", "submit", "publish"])
def test_contract_cannot_authorize_input_submit_or_publish(probe_action):
    with pytest.raises(ValueError, match="probe_action"):
        normalize_contracts({"unsafe": contract_value(probe_action=probe_action)})


def test_dynamic_contract_cannot_disguise_destructive_control_as_read_only_open():
    with pytest.raises(ValueError, match="open_read_only"):
        normalize_contracts(
            {
                "Delete account": contract_value(
                    intent="Delete account",
                    accepted_names={"mode": "exact", "values": ["Delete account"]},
                    postcondition="comment_panel_open",
                    probe_action="open_read_only",
                )
            }
        )


def test_canonical_entry_open_contract_requires_exact_safe_tuple():
    entry_alias = next(iter(default_tiktok_contracts()))
    with pytest.raises(ValueError, match="open_read_only"):
        normalize_contracts(
            {
                entry_alias: contract_value(
                    scope="page",
                    postcondition="comment_panel_open",
                    probe_action="open_read_only",
                )
            }
        )


def test_canonical_entry_cannot_hide_destructive_open_semantics():
    entry_alias = next(iter(default_tiktok_contracts()))
    with pytest.raises(ValueError, match="open_read_only"):
        normalize_contracts(
            {
                entry_alias: contract_value(
                    intent="Delete account",
                    accepted_names={
                        "mode": "locale_map",
                        "values": ["Delete account"],
                    },
                    postcondition="comment_panel_open",
                    probe_action="open_read_only",
                )
            }
        )


@pytest.mark.parametrize("name_mode", ["exact", "locale_map"])
def test_dynamic_alias_can_define_strict_safe_close_contract(name_mode):
    result = normalize_contracts(
        {
            "自定义关闭按钮": contract_value(
                intent="close the visible comment panel",
                required_state="comment_panel_open",
                scope="visible_comment_panel",
                accepted_names={
                    "mode": name_mode,
                    "values": ["Close"] if name_mode == "exact" else ["Close", "关闭"],
                },
                preferred_attributes=["data-e2e", "aria-label"],
                postcondition="comment_panel_closed",
                probe_action="close_read_only",
            )
        }
    )

    assert result["自定义关闭按钮"].probe_action == "close_read_only"
    assert result["自定义关闭按钮"].postcondition == "comment_panel_closed"


@pytest.mark.parametrize(
    "overrides",
    [
        {"intent": "close the account panel"},
        {"required_state": "feed_ready"},
        {"scope": "active_video"},
        {"postcondition": "feed_ready"},
        {"accepted_roles": ["link"]},
        {"accepted_names": {"mode": "contains", "values": ["Close"]}},
        {"accepted_names": {"mode": "exact", "values": ["Delete"]}},
        {"accepted_names": {"mode": "locale_map", "values": ["Cancel account"]}},
        {"preferred_attributes": ["id"]},
    ],
)
def test_close_read_only_rejects_any_deviation_from_safe_tuple(overrides):
    value = contract_value(
        intent="close the visible comment panel",
        required_state="comment_panel_open",
        scope="visible_comment_panel",
        accepted_names={"mode": "locale_map", "values": ["Close", "Dismiss"]},
        preferred_attributes=["aria-label"],
        postcondition="comment_panel_closed",
        probe_action="close_read_only",
    )
    value.update(overrides)

    with pytest.raises(ValueError, match="close_read_only"):
        normalize_contracts({"dynamic-close": value})


def test_default_alias_roles_are_discovered_by_template_semantics_not_dict_order(
    monkeypatch,
):
    original = copy.deepcopy(TIKTOK_COMMENT_TEMPLATE)
    aliases = tuple(original)
    reordered = {
        aliases[2]: original[aliases[2]],
        aliases[0]: original[aliases[0]],
        aliases[1]: original[aliases[1]],
    }
    monkeypatch.setattr(contracts_module, "TIKTOK_COMMENT_TEMPLATE", reordered)

    result = default_tiktok_contracts()

    assert result[aliases[0]].probe_action == "open_read_only"
    assert result[aliases[1]].accepted_roles == ("textbox",)
    assert result[aliases[2]].probe_action == "inspect_only"


def test_ambiguous_template_semantics_fail_closed(monkeypatch):
    template = copy.deepcopy(TIKTOK_COMMENT_TEMPLATE)
    first_alias = next(iter(template))
    template["duplicate-comment-entry"] = copy.deepcopy(template[first_alias])
    monkeypatch.setattr(contracts_module, "TIKTOK_COMMENT_TEMPLATE", template)

    with pytest.raises(RuntimeError, match="unambiguously"):
        default_tiktok_contracts()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update({"unexpected": True}), "shape"),
        (lambda value: value.pop("scope"), "shape"),
        (lambda value: value.update({"required_state": "unknown"}), "required_state"),
        (lambda value: value.update({"scope": "document"}), "scope"),
        (lambda value: value.update({"accepted_roles": ["script"]}), "role"),
        (
            lambda value: value.update(
                {"accepted_names": {"mode": "prefix", "values": ["Comments"]}}
            ),
            "name mode",
        ),
        (
            lambda value: value.update(
                {"accepted_names": {"mode": "exact", "values": []}}
            ),
            "accepted_names",
        ),
        (
            lambda value: value.update({"preferred_attributes": ["onclick"]}),
            "preferred attribute",
        ),
    ],
)
def test_contract_rejects_unknown_fields_and_invalid_domain_values(mutation, message):
    value = contract_value()
    mutation(value)

    with pytest.raises(ValueError, match=message):
        normalize_contracts({"element": value})


def test_normalize_contracts_rejects_aliases_that_collide_after_trimming():
    with pytest.raises(ValueError, match="unique"):
        normalize_contracts(
            {
                "comments": contract_value(),
                " comments ": contract_value(),
            }
        )
