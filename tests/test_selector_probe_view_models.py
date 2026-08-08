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
        status="healthy",
        page_key="comment-panel",
        primary_locator_type="css",
        dependency_count=2,
        last_validated_at="2026-07-29T03:00:00+00:00",
        revision=4,
    )


def test_detail_exposes_manual_definition_and_omits_sensitive_data(element_record):
    payload = public_element_detail(
        element_record,
        definition={
            "page_key": "comment-panel",
            "target_origin": "https://www.tiktok.com",
            "url_pattern": "https://www.tiktok.com/*",
            "operation_steps": [],
            "fingerprint": {
                "tag": "button",
                "role": "button",
                "position_hint": {
                    "x": 0.8,
                    "y": 0.4,
                    "width": 0.1,
                    "height": 0.1,
                },
                "cdp": "ws://127.0.0.1:9222/devtools/browser/secret",
                "endpoint": "<html>private</html>",
            },
            "locators": [
                {"type": "css", "value": '[data-e2e="comment"]'}
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
        validation={
            "status": "passed",
            "raw_dom": "<html>secret</html>",
            "prompt": "private prompt",
            "model_output": "private output",
        },
        alerts=[{"id": "alert-1", "token": "drop-me"}],
        strategy_controls={"paused": False, "profile_id": "drop-me"},
    )

    text = str(payload)
    assert payload["definition"]["locators"] == [
        {"type": "css", "value": '[data-e2e="comment"]'}
    ]
    assert payload["definition"]["fingerprint"] == {
        "tag": "button",
        "role": "button",
        "position_hint": {
            "x": 0.8,
            "y": 0.4,
            "width": 0.1,
            "height": 0.1,
        },
    }
    assert payload["dependencies"] == [
        {
            "strategy_id": "comment-flow",
            "strategy_name": "Comment workflow",
            "action_id": "entry",
            "action_type": "click",
        }
    ]
    for forbidden in (
        "<html>",
        "private prompt",
        "private output",
        "drop-me",
        "devtools/browser",
        "<html>private</html>",
    ):
        assert forbidden not in text
    for obsolete in ("contract", "repairs", "candidate_comparison"):
        assert obsolete not in payload


def test_summary_and_error_have_exact_public_shapes(element_record):
    summary = public_element_summary(element_record)
    error = public_error(
        "invalid_filter",
        message="Invalid filter",
        details={"field": "status", "token": "drop-me"},
    )

    assert summary["status"] == "healthy"
    assert set(summary) == {
        "id",
        "display_name",
        "status",
        "page_key",
        "primary_locator_type",
        "dependency_count",
        "last_validated_at",
        "revision",
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
