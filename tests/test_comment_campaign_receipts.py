from datetime import datetime, timezone

from comment_campaign.receipts import build_receipt, normalize_comment_text, verify_receipt_candidates


def test_receipt_normalizes_text_and_uses_a_safe_uuid_screenshot_name():
    assert normalize_comment_text("  A\u00a0comment\n") == "A comment"
    receipt = build_receipt(
        video_id="12345678", profile_ref="profile_ref_safe", expected_username="alice",
        text="  A\u00a0comment\n", screenshot_path="evidence/3e0ea1e8b5584b4c9a65768f291d80b2.png",
    )
    assert receipt["normalized_text_hash"]
    assert receipt["screenshot_path"].endswith(".png")
    assert "profile_ref_safe" == receipt["profile_ref"]


def test_receipt_candidates_require_exactly_one_new_matching_node():
    receipt = build_receipt(video_id="12345678", profile_ref="profile_ref_safe", expected_username="alice", text="hello", screenshot_path="evidence/3e0ea1e8b5584b4c9a65768f291d80b2.png")
    candidate = {"video_id": "12345678", "profile_ref": "profile_ref_safe", "text": "hello", "visible": True, "observed_at": datetime.now(timezone.utc).isoformat(), "stable_attributes": {"data-e2e": "comment"}, "platform_comment_id": "one"}
    assert verify_receipt_candidates(before=[], after=[], receipt=receipt) is None
    assert verify_receipt_candidates(before=[], after=[candidate], receipt=receipt) == candidate
    assert verify_receipt_candidates(before=[], after=[candidate, {**candidate, "platform_comment_id": "two"}], receipt=receipt) is None


def test_receipt_candidate_can_match_username_but_requires_stable_node_evidence():
    receipt = build_receipt(
        video_id="12345678", profile_ref="profile_ref_safe",
        expected_username="Alice", text="hello",
        screenshot_path="evidence/3e0ea1e8b5584b4c9a65768f291d80b2.png",
    )
    candidate = {
        "video_id": "12345678", "author_username": "@alice", "text": "hello",
        "visible": True, "observed_at": datetime.now(timezone.utc).isoformat(),
        "stable_attributes": {}, "platform_comment_id": "comment-1",
    }
    assert verify_receipt_candidates(before=[], after=[candidate], receipt=receipt) == candidate
    assert verify_receipt_candidates(
        before=[], after=[{**candidate, "platform_comment_id": ""}], receipt=receipt
    ) is None


def test_threaded_receipt_must_remain_under_the_exact_parent_comment():
    receipt = build_receipt(
        video_id="12345678", profile_ref="profile_ref_safe",
        expected_username="alice", text="reply",
        screenshot_path="evidence/3e0ea1e8b5584b4c9a65768f291d80b2.png",
        parent_receipt_id="receipt_parent",
        parent_platform_comment_id="parent-1",
    )
    candidate = {
        "video_id": "12345678", "profile_ref": "profile_ref_safe",
        "text": "reply", "visible": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "platform_comment_id": "child-1", "stable_attributes": {"comment_id": "child-1"},
    }
    assert verify_receipt_candidates(
        before=[], after=[{**candidate, "parent_platform_comment_id": "other-parent"}],
        receipt=receipt,
    ) is None
    scoped = {**candidate, "parent_platform_comment_id": "parent-1"}
    assert verify_receipt_candidates(before=[], after=[scoped], receipt=receipt) == scoped


def test_threaded_receipt_without_a_current_parent_scope_cannot_verify_top_level():
    receipt = build_receipt(
        video_id="12345678", profile_ref="profile_ref_safe",
        expected_username="alice", text="reply", screenshot_path=None,
        parent_receipt_id="receipt_parent",
    )
    top_level = {
        "video_id": "12345678", "profile_ref": "profile_ref_safe",
        "text": "reply", "visible": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "platform_comment_id": "top-level", "stable_attributes": {"comment_id": "top-level"},
    }
    assert verify_receipt_candidates(before=[], after=[top_level], receipt=receipt) is None
