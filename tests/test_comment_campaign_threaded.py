import asyncio

from comment_campaign.errors import CampaignValidationError
from comment_campaign.locator import LocateLimits, _unique_direct_control, locate_parent_comment, open_scoped_reply


class FakePage:
    def __init__(self, batches):
        self.batches, self.index, self.scrolls = batches, 0, 0

    async def comment_candidates(self):
        return self.batches[self.index]

    async def scroll_comment_panel(self):
        self.scrolls += 1
        self.index = min(self.index + 1, len(self.batches) - 1)


def test_parent_locator_rejects_ambiguous_fallback_match():
    receipt = {"video_id": "1", "expected_username": "alice", "normalized_text": "same"}
    page = FakePage([[{"visible": True, "video_id": "1", "author_username": "alice", "text": "same"}, {"visible": True, "video_id": "1", "author_username": "alice", "text": "same"}]])
    try:
        asyncio.run(locate_parent_comment(page, receipt, LocateLimits(timeout_seconds=1, max_scrolls=0)))
    except CampaignValidationError as exc:
        assert exc.code == "parent_comment_ambiguous"
    else:
        raise AssertionError("ambiguous parent must fail closed")


def test_parent_locator_scrolls_until_exact_platform_id_is_visible():
    page = FakePage([[{"visible": True, "platform_comment_id": "old"}], [{"visible": True, "platform_comment_id": "wanted"}]])
    found = asyncio.run(locate_parent_comment(page, {"platform_comment_id": "wanted"}, LocateLimits(timeout_seconds=1, max_scrolls=1)))
    assert found["platform_comment_id"] == "wanted"
    assert page.scrolls == 1


def test_missing_platform_id_falls_back_to_exact_author_video_and_text():
    receipt = {
        "platform_comment_id": "stale-id", "video_id": "1",
        "expected_username": "alice", "normalized_text": "same",
    }
    candidate = {
        "visible": True, "video_id": "1", "author_username": "@Alice",
        "text": "same", "platform_comment_id": "fresh-id",
    }
    assert asyncio.run(locate_parent_comment(
        FakePage([[candidate]]), receipt, LocateLimits(timeout_seconds=1, max_scrolls=0)
    )) == candidate


def test_stale_parent_id_fallback_freezes_the_current_parent_scope():
    candidate = {
        "visible": True, "video_id": "1", "author_username": "alice",
        "text": "same", "platform_comment_id": "current-id",
        "reply_click": lambda: None, "composer_target": "alice",
        "reply_composer": {"input": object(), "submit": object()},
    }

    async def scenario():
        found = await locate_parent_comment(
            FakePage([[candidate]]),
            {"platform_comment_id": "stale-id", "video_id": "1", "expected_username": "alice", "normalized_text": "same"},
            LocateLimits(timeout_seconds=1, max_scrolls=0),
        )
        return await open_scoped_reply(found, "alice")

    scope = asyncio.run(scenario())
    assert scope["parent_scope"]["parent_platform_comment_id"] == "current-id"


def test_hidden_parent_is_not_accepted():
    page = FakePage([[{"visible": False, "platform_comment_id": "wanted"}]])
    try:
        asyncio.run(locate_parent_comment(
            page, {"platform_comment_id": "wanted"},
            LocateLimits(timeout_seconds=1, max_scrolls=0),
        ))
    except CampaignValidationError as exc:
        assert exc.code == "parent_comment_not_found"
    else:
        raise AssertionError("hidden parent must fail closed")


def test_pause_descendants_keeps_unrelated_branch_planned():
    from comment_campaign.store import CampaignStore
    store = CampaignStore("sqlite:///:memory:")
    store.initialize()
    # The implementation exposes a graph-only primitive for a transaction-safe
    # descendant walk; no browser or submission is involved.
    paused = store.pause_descendant_ids([
        {"assignment_id": "root-a", "parent_assignment_id": None},
        {"assignment_id": "child-a", "parent_assignment_id": "root-a"},
        {"assignment_id": "grand-a", "parent_assignment_id": "child-a"},
        {"assignment_id": "root-b", "parent_assignment_id": None},
    ], "root-a")
    assert paused == ["child-a", "grand-a"]


def test_scoped_reply_target_mismatch_never_returns_input_or_submit():
    clicked = []
    parent = {
        "author_username": "alice", "composer_target": "other",
        "reply_click": lambda: clicked.append(True),
        "reply_composer": {"input": object(), "submit": object()},
    }
    try:
        asyncio.run(open_scoped_reply(parent, "alice"))
    except CampaignValidationError as exc:
        assert exc.code == "reply_target_mismatch"
    else:
        raise AssertionError("wrong reply target must fail before input")
    assert clicked == [True]


def test_nearest_comment_container_selects_grandchild_parent_controls_only():
    parent_handle = object()

    class Candidate:
        def __init__(self, owner): self.owner = owner
        async def evaluate(self, _script, parent): return self.owner is parent

    class Collection:
        def __init__(self, rows): self.rows = rows
        async def count(self): return len(self.rows)
        def nth(self, index): return self.rows[index]

    direct = Candidate(parent_handle)
    nested_child = Candidate(object())

    class Parent:
        async def element_handle(self): return parent_handle
        def locator(self, _selector): return Collection([nested_child, direct])

    assert asyncio.run(_unique_direct_control(Parent(), "button")) is direct
