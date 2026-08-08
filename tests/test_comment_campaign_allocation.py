import pytest

from comment_campaign.allocation import AllocationError, allocate, validate_template_tree
from comment_campaign.errors import CampaignValidationError


def _profile(ref, tags=()):
    return {"profile_ref": ref, "display_profile": ref, "expected_username": ref, "enabled": True, "login_verified": True, "health_status": "healthy", "tags": list(tags), "language": "en", "cooldown_until": None}


def _step(identifier, parent=None, required=()):
    return {"id": identifier, "parent_step_id": parent, "required_profile_tags": list(required), "excluded_profile_tags": [], "language": "en"}


def test_augmenting_matching_is_deterministic_and_keeps_template_position():
    steps = [_step("reply", "root"), _step("root", None, ["root"])]
    profiles = [_profile("a", ["root"]), _profile("b")]
    texts = {"reply": {"text": "reply text"}, "root": {"text": "root text"}}
    first = allocate(steps, profiles, texts, "seed", mode="threaded", campaign_id="campaign")
    second = allocate(steps, profiles, texts, "seed", mode="threaded", campaign_id="campaign")
    assert [row.as_dict() for row in first] == [row.as_dict() for row in second]
    assert [(row.step_id, row.position) for row in first] == [("root", 1), ("reply", 2)]
    assert {row.step_id: row.profile_ref for row in first} == {"root": "a", "reply": "b"}


def test_allocation_is_bounded_for_300_profiles_and_requires_full_solution():
    profiles = [_profile(f"profile-{index}") for index in range(300)]
    steps = [_step(f"step-{index}") for index in range(100)]
    texts = {step["id"]: {"text": f"text-{step['id']}"} for step in steps}
    plan = allocate(steps, profiles, texts, "seed", mode="independent", campaign_id="campaign")
    assert len(plan) == 100
    assert len({row.profile_ref for row in plan}) == 100
    with pytest.raises(AllocationError, match="allocation_unsatisfied"):
        allocate(steps[:2], profiles[:1], {key: value for key, value in texts.items() if key in {"step-0", "step-1"}}, "seed", mode="independent")


def test_template_tree_rejects_incompatible_parents_and_cycles():
    with pytest.raises(CampaignValidationError):
        validate_template_tree("independent", [_step("child", "root"), _step("root")])
    with pytest.raises(CampaignValidationError):
        validate_template_tree("threaded", [_step("a", "b"), _step("b", "a")])


def test_allocation_rejects_normalized_duplicate_copy():
    with pytest.raises(AllocationError, match="allocation_unsatisfied"):
        allocate(
            [_step("first"), _step("second")], [_profile("a"), _profile("b")],
            {"first": {"text": "Hello   world"}, "second": {"text": "Hello world"}},
            "seed", mode="independent",
        )
