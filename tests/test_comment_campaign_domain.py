import pytest

from comment_campaign.domain import transition_assignment, transition_campaign
from comment_campaign.errors import StateTransitionError


def test_campaign_and_assignment_state_machines_reject_unlisted_edges():
    assert transition_campaign("draft", "planned") == "planned"
    assert transition_assignment("submitting", "published_unverified") == "published_unverified"
    with pytest.raises(StateTransitionError):
        transition_campaign("completed", "queued")
    with pytest.raises(StateTransitionError):
        transition_assignment("submitting", "awaiting_step_approval")


def test_submitting_cannot_be_reused_after_recovery_path():
    assert transition_assignment("verifying_receipt", "published_unverified") == "published_unverified"
    with pytest.raises(StateTransitionError):
        transition_assignment("published_unverified", "submitting")
