import asyncio
import pytest

from comment_campaign.errors import CampaignValidationError
from comment_campaign.executor import CommentExecutor


def test_submit_never_clicks_without_current_unconsumed_approval():
    class Store:
        def get_campaign(self, _): return {"id": "campaign", "video_id": "12345678", "status": "running"}
        def get_assignment(self, _): return {"assignment_id": "a1", "campaign_id": "campaign", "revision": 2, "status": "awaiting_step_approval"}
        def get_approval(self, *_): return None

    executor = CommentExecutor(Store(), gateway=None, locator_resolver=None)
    with pytest.raises(CampaignValidationError, match="approval_revision_mismatch"):
        asyncio.run(executor.submit_assignment("campaign", "a1", 2))
