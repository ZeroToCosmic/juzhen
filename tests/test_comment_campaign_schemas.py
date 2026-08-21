import pytest

from comment_campaign.schemas import CampaignCreate, CommentStepInput, ProfileMetadataUpsert, TemplateCreate


def _step(**updates):
    result = {
        "id": "root", "label": "root", "content_source": "fixed", "fixed_text": "hello",
        "content_library_id": "", "content_item_id": "", "parent_step_id": None,
        "required_profile_tags": [], "excluded_profile_tags": [], "language": "en",
    }
    result.update(updates)
    return result


def test_step_strips_parent_and_requires_exact_content_source():
    step = CommentStepInput.model_validate(_step(parent_step_id="   "))
    assert step.parent_step_id is None
    with pytest.raises(Exception):
        CommentStepInput.model_validate(_step(fixed_text="", content_library_id="library"))


def test_lists_reject_blank_and_duplicate_values_after_trimming():
    with pytest.raises(Exception):
        TemplateCreate.model_validate({
            "name": "template", "description": "", "supported_modes": ["threaded"], "language": "en",
            "tags": ["blue", " blue "], "steps": [_step()],
        })
    with pytest.raises(Exception):
        CommentStepInput.model_validate(_step(required_profile_tags=["en", " "]))
    with pytest.raises(Exception):
        CampaignCreate.model_validate({
            "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "https://example.test/video",
            "template_id": "template", "profile_refs": ["profile-a", " profile-a "],
        })
    with pytest.raises(Exception):
        CampaignCreate.model_validate({
            "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "https://example.test/video",
            "template_id": "template", "profile_refs": ["p" * 81],
        })


def test_strict_schema_rejects_unknown_fields_and_coercion():
    with pytest.raises(Exception):
        CampaignCreate.model_validate({
            "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "x",
            "template_id": "template", "profile_refs": ["profile-a"], "batch_size": "3",
        })
    with pytest.raises(Exception):
        CampaignCreate.model_validate({
            "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "x",
            "template_id": "template", "profile_refs": ["profile-a"], "extra": "no",
        })
    with pytest.raises(Exception):
        TemplateCreate.model_validate({
            "name": "template", "description": "x" * 501, "supported_modes": ["threaded"],
            "language": "en", "tags": [], "steps": [_step()],
        })
    nested = _step()
    nested["extra"] = "no"
    with pytest.raises(Exception):
        TemplateCreate.model_validate({
            "name": "template", "description": "", "supported_modes": ["threaded"],
            "language": "en", "tags": [], "steps": [nested],
        })


def test_scheduled_and_cooldown_times_must_be_aware_and_normalize_to_utc():
    with pytest.raises(Exception):
        CampaignCreate.model_validate({
            "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "x",
            "template_id": "template", "profile_refs": ["profile-a"], "start_mode": "scheduled",
            "scheduled_at": "2026-01-01T00:00:00",
        })
    campaign = CampaignCreate.model_validate({
        "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "x",
        "template_id": "template", "profile_refs": ["profile-a"], "start_mode": "scheduled",
        "scheduled_at": "2026-01-01T08:00:00+08:00",
    })
    assert campaign.scheduled_at == "2026-01-01T00:00:00+00:00"
    with pytest.raises(Exception):
        ProfileMetadataUpsert.model_validate({
            "profile_ref": "profile-a", "enabled": True, "login_verified": True, "health_status": "healthy",
            "cooldown_until": "2026-01-01T00:00:00",
        })


def test_datetime_strings_persist_through_store_boundary(tmp_path):
    from comment_campaign.store import CampaignStore

    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    store.create_template(TemplateCreate.model_validate({
        "name": "template", "description": "", "supported_modes": ["threaded"], "language": "en",
        "tags": [], "steps": [_step()],
    }), "template")
    profile_ref = store.sync_profile_identities([{"id": "raw", "name": "A", "status": "active"}])[0]["profile_ref"]
    metadata = store.upsert_profile_metadata(**ProfileMetadataUpsert.model_validate({
        "profile_ref": profile_ref, "enabled": True, "login_verified": True, "health_status": "healthy",
        "cooldown_until": "2026-01-01T08:00:00+08:00",
    }).model_dump())
    campaign = store.create_campaign(CampaignCreate.model_validate({
        "name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "x",
        "template_id": "template", "profile_refs": [profile_ref], "start_mode": "scheduled",
        "scheduled_at": "2026-01-01T08:00:00+08:00",
    }), "campaign", "12345678", "https://example.test/video")
    assert metadata["cooldown_until"] == "2026-01-01T00:00:00+00:00"
    assert campaign["scheduled_at"] == "2026-01-01T00:00:00+00:00"
