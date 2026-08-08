import pytest

from comment_campaign.errors import CampaignValidationError
from comment_campaign.video import normalize_tiktok_video


def test_video_url_is_canonicalized_and_query_is_dropped():
    target = normalize_tiktok_video("https://www.tiktok.com/@alice_1/video/7469123456789012345?lang=en")
    assert target.video_id == "7469123456789012345"
    assert target.canonical_url == "https://www.tiktok.com/@alice_1/video/7469123456789012345"


@pytest.mark.parametrize("reference", [
    "http://www.tiktok.com/@alice/video/7469123456789012345",
    "https://www.tiktok.com.evil.test/@alice/video/7469123456789012345",
    "https://alice@www.tiktok.com/@alice/video/7469123456789012345",
    "https://www.tiktok.com:444/@alice/video/7469123456789012345",
    "https://www.tiktok.com/@alice%2Fvideo/7469123456789012345",
    "https://www.tiktok.com/@alice/not-video/7469123456789012345",
    "https://www.tiktok.com/@alice/video/7469123456789012345#fragment",
    "https://[::1/@alice/video/7469123456789012345",
    "https://\uff20www.tiktok.com/@alice/video/7469123456789012345",
])
def test_video_url_rejects_noncanonical_or_unsafe_references(reference):
    with pytest.raises(CampaignValidationError, match="target_video_invalid"):
        normalize_tiktok_video(reference)
