from dataclasses import FrozenInstanceError

import pytest

from execution_v2.models import BrowserBinding, JobStatus, ProfileStatus, Stage


def test_browser_binding_is_immutable_and_keeps_one_profile_chain():
    binding = BrowserBinding("profile-1", "ws://one", object(), object(), object())

    assert binding.profile_id == "profile-1"
    assert binding.ws_url == "ws://one"
    with pytest.raises(FrozenInstanceError):
        binding.profile_id = "profile-2"


def test_public_state_values_are_stable():
    assert JobStatus.CLEANUP_BLOCKED.value == "cleanup_blocked"
    assert ProfileStatus.WAITING_READINESS.value == "waiting_readiness"
    assert Stage.ADSPOWER_STOP.value == "adspower_stop"
