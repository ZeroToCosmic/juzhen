"""Fixed boundary scans for the campaign's redacted public contracts."""

from __future__ import annotations

import pytest
from pathlib import Path
import json

from comment_campaign.errors import CampaignValidationError
from comment_campaign.blueprint import _redact
from comment_campaign.receipts import safe_evidence_path


@pytest.mark.parametrize("value", [
    "../receipt.png", "receipt.jpg", "A" * 32 + ".png", "a" * 31 + ".png",
    "a" * 32 + ".PNG", "nested/" + "a" * 32 + ".png", "C:/" + "a" * 32 + ".png",
])
def test_evidence_path_accepts_only_lowercase_uuid_png_basename(value):
    with pytest.raises(CampaignValidationError, match="comment_receipt_unverified"):
        safe_evidence_path(value)


def test_evidence_path_accepts_exact_lowercase_uuid_png_basename():
    assert safe_evidence_path("evidence/0123456789abcdef0123456789abcdef.png") == "evidence/0123456789abcdef0123456789abcdef.png"


def test_public_payload_redaction_recursively_removes_transport_and_secret_fields():
    public = _redact({
        "profile_ref": "profile_ref_safe", "raw_profile_id": "raw-secret",
        "raw_adspower_id": "RAW_ADSPOWER_SENTINEL",
        "endpoint": "wss://secret.example", "nested": {
            "raw_adspower_ids": ["RAW_ADSPOWER_SENTINEL_2"],
            "cookie": "session", "authorization": "Bearer secret", "api_key": "key",
            "diagnostic": "connect failed at ws://secret.example",
            "safe": ["visible", {"ws_url": "ws://secret", "receipt_id": "receipt_safe"}],
        },
    })
    rendered = repr(public).lower()
    for forbidden in ("raw-secret", "raw_adspower_sentinel", "wss://", "ws://", "session", "bearer secret", "api_key"):
        assert forbidden not in rendered
    assert public["profile_ref"] == "profile_ref_safe"
    assert public["nested"]["safe"] == ["visible", {"receipt_id": "receipt_safe"}]


def test_public_artifacts_and_fake_rq_arguments_never_leak_transport_secrets():
    api = _redact({"raw_profile_id": "raw-profile-secret", "ws_url": "ws://secret", "cookie": "cookie-secret", "authorization": "bearer-secret", "api_key": "api-key-secret", "profile_ref": "profile_ref_safe", "receipt_id": "receipt_safe"})
    rq_args = ("campaign-safe", "assignment-safe", 3)
    root = Path(__file__).resolve().parents[1]
    artifacts = [api, rq_args, {"profile_ref": "profile_ref_safe", "receipt_id": "receipt_safe"}, (root / "gateway/templates/comment_campaign.html").read_text(encoding="utf-8"), (root / "docs/superpowers/reports/2026-08-07-comment-campaign-verification.md").read_text(encoding="utf-8")]
    rendered = json.dumps(artifacts, ensure_ascii=False).casefold()
    for forbidden in ("raw-profile-secret", "ws://", "wss://", "cookie-secret", "bearer-secret", "api-key-secret"):
        assert forbidden not in rendered
    assert "profile_ref_safe" in rendered and "receipt_safe" in rendered


@pytest.mark.parametrize("value", [
    "evidence%2F0123456789abcdef0123456789abcdef.png",
    "evidence\\0123456789abcdef0123456789abcdef.png",
    "evidence/0123456789abcdef0123456789abcdef.PNG",
    "evidence/../0123456789abcdef0123456789abcdef.png",
])
def test_evidence_path_rejects_encoded_backslash_case_and_traversal_variants(value):
    with pytest.raises(CampaignValidationError):
        safe_evidence_path(value)
