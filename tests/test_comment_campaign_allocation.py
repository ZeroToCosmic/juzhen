import pytest

from datetime import datetime, timedelta, timezone

from comment_campaign.allocation import (
    AllocationError,
    allocate,
    profile_matches,
    recommend_profiles,
    validate_template_tree,
)
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


NOW = datetime(2026, 8, 11, tzinfo=timezone.utc)


def test_window_eligibility_ignores_historical_account_and_region_fields():
    step = _step("window", required=())
    profile = {
        **_profile("window"),
        "expected_username": "",
        "login_verified": False,
        "region": "CN",
    }

    assert profile_matches(step, profile, eligibility_at=NOW) is True


def test_recommendation_uses_complete_augmenting_path_and_display_order():
    flexible = _step("flexible", required=())
    constrained = _step("constrained", required=("only-a",))
    profile_b = {**_profile("b"), "display_profile": "Bravo"}
    profile_a = {**_profile("a", ["only-a"]), "display_profile": "Alpha"}

    result = recommend_profiles(
        [flexible, constrained], [profile_b, profile_a], eligibility_at=NOW
    )

    assert result == ["a", "b"]


@pytest.mark.parametrize(
    ("profiles", "reason"),
    [
        ([_profile("a")], "insufficient_profiles"),
        ([{**_profile("a"), "enabled": False}, _profile("b")], "profile_disabled"),
        ([{**_profile("a"), "health_status": "unhealthy"}, _profile("b")], "profile_unhealthy"),
        ([{**_profile("a"), "cooldown_until": (NOW + timedelta(minutes=1)).isoformat()}, _profile("b")], "profile_in_cooldown"),
        ([{**_profile("a"), "tags": []}, {**_profile("b"), "tags": []}], "profile_tag_mismatch"),
        ([{**_profile("a"), "language": "zh"}, {**_profile("b"), "language": "zh"}], "profile_language_mismatch"),
    ],
)
def test_allocation_failure_details_are_safe_and_reasoned(profiles, reason):
    steps = [_step("one", required=("required",)), _step("two", required=("required",))]
    if reason == "profile_language_mismatch":
        steps = [{**_step("one"), "language": "en"}, {**_step("two"), "language": "en"}]
    with pytest.raises(AllocationError) as caught:
        recommend_profiles(steps, profiles, eligibility_at=NOW)

    assert caught.value.details["reason"] == reason
    assert set(caught.value.details) <= {"reason", "required_count", "eligible_count", "display_profiles"}


def test_hall_failure_is_distinct_from_window_eligibility_failure():
    steps = [
        _step("one", required=("x",)), _step("two", required=("x",)),
        _step("three", required=("y",)),
    ]
    profiles = [
        {**_profile("a", ["x"]), "display_profile": "A"},
        {**_profile("b", ["y"]), "display_profile": "B"},
        {**_profile("c", ["y"]), "display_profile": "C"},
    ]

    with pytest.raises(AllocationError) as caught:
        recommend_profiles(steps, profiles, eligibility_at=NOW)

    assert caught.value.details == {
        "reason": "complete_matching_not_found",
        "required_count": 3,
        "eligible_count": 3,
    }


@pytest.mark.parametrize(
    "steps, profiles",
    [
        ([_step(f"step-{index}") for index in range(101)], [_profile("a")]),
        ([_step("step")], [_profile(f"profile-{index}") for index in range(301)]),
        ([_step("step")], [_profile(f"profile-{index}") for index in range(300)] + [_profile("profile-0")]),
    ],
)
def test_oversized_or_duplicate_match_graphs_have_no_fake_allocation_reason(steps, profiles):
    texts = {step["id"]: {"text": f"text-{step['id']}"} for step in steps}

    for operation in (
        lambda: recommend_profiles(steps, profiles, eligibility_at=NOW),
        lambda: allocate(
            steps, profiles, texts, "seed", mode="independent",
            eligibility_at=NOW,
        ),
    ):
        with pytest.raises(AllocationError) as caught:
            operation()
        assert caught.value.details == {}
