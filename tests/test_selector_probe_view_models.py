from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from selector_probe.view_models import (
    ElementRecord,
    public_element_detail,
    public_element_summary,
    public_error,
)


@pytest.fixture
def element_record():
    return ElementRecord(
        id="comment-entry",
        display_name="Comment entry",
        management_source="automatic",
        published_status="healthy",
        draft_status=None,
        scope="active_video",
        primary_locator_type="attribute",
        dependency_count=2,
        last_validated_at="2026-07-29T03:00:00+00:00",
        revision=4,
    )


def test_detail_omits_raw_browser_and_model_data(element_record):
    payload = public_element_detail(
        element_record,
        evidence={
            "profile_id": "full-profile-secret",
            "profile_mask": "***3A7F",
            "raw_dom": "<html>secret</html>",
            "raw_ax": {"secret": True},
            "prompt": "private prompt",
            "model_output": "private output",
            "rounds": [
                {
                    "round_number": 1,
                    "result": "passed",
                    "failure_code": "",
                    "raw_dom": "nested secret",
                }
            ],
        },
        dependencies=(
            {
                "strategy_id": "comment-flow",
                "strategy_name": "Comment workflow",
                "action_id": "entry",
                "action_type": "click",
                "secret": "drop-me",
            },
        ),
    )

    text = str(payload)
    assert "***3A7F" in text
    assert payload["dependencies"] == [
        {
            "strategy_id": "comment-flow",
            "strategy_name": "Comment workflow",
            "action_id": "entry",
            "action_type": "click",
        }
    ]
    for forbidden in (
        "full-profile-secret",
        "<html>",
        "private prompt",
        "private output",
        "nested secret",
        "drop-me",
    ):
        assert forbidden not in text


def test_summary_and_error_have_exact_public_shapes(element_record):
    summary = public_element_summary(element_record)
    error = public_error(
        "invalid_filter",
        message="Invalid filter",
        details={"field": "status", "token": "drop-me"},
    )

    assert summary["runtime_status"] == "healthy"
    assert set(summary) == {
        "id",
        "display_name",
        "management_source",
        "published_status",
        "draft_status",
        "runtime_status",
        "scope",
        "primary_locator_type",
        "dependency_count",
        "last_validated_at",
        "revision",
        "migration_available",
    }
    assert error == {
        "error": {
            "code": "invalid_filter",
            "message": "Invalid filter",
            "details": {"field": "status"},
        }
    }


def test_element_record_is_frozen(element_record):
    with pytest.raises(FrozenInstanceError):
        element_record.display_name = "changed"
