"""Fixed boundary scans for the campaign's redacted public contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from flask import Flask
import conftest as suite_config
from adspower import AdsPowerDependencyError

from comment_campaign.blueprint import create_comment_campaign_blueprint
from comment_campaign.errors import CampaignValidationError
import comment_campaign.executor as executor_module
from comment_campaign.executor import CommentExecutor
from comment_campaign.blueprint import _redact
from comment_campaign.receipts import safe_evidence_path
from comment_campaign.service import CommentCampaignService
from comment_campaign.store import CampaignStore
from comment_campaign.queueing import QueueCoordinator
from gateway.app import create_app


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
    assert _redact({"resolved_text": "Bearer of good news"}) == {
        "resolved_text": "Bearer of good news",
    }
    assert _redact({"diagnostic": "Authorization: Bearer SECRET"}) == {
        "diagnostic": "[redacted]",
    }


def test_import_api_recursively_redacts_forbidden_nested_values():
    class SecretImportService:
        def import_templates(self, _payload):
            return {
                "created": [{
                    "name": "safe tree",
                    "nested": {
                        "raw_adspower_id": "raw-secret",
                        "cookie": "cookie-secret",
                        "diagnostic": "failed at ws://secret.example",
                    },
                }],
                "rejected": [],
            }

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(SecretImportService()))
    response = app.test_client().post(
        "/api/browser-v2/comment-template-imports",
        json={"trees": [{"name": "A", "nodes": [{
            "node_no": "1", "parent_node_no": None, "text": "root",
        }]}]},
    )

    serialized = json.dumps(response.get_json()).casefold()
    assert response.status_code == 201
    assert "raw_adspower_id" not in serialized
    assert "raw-secret" not in serialized
    assert "cookie" not in serialized
    assert "ws://" not in serialized


def test_public_api_attempt_receipt_health_and_preview_boundaries_redact_sentinels():
    secret = {
        "raw_adspower_id": "RAW_ADSPOWER_SENTINEL",
        "cookie": "COOKIE_SENTINEL",
        "authorization": "Authorization Bearer SENTINEL",
        "api_key": "API_KEY_SENTINEL",
        "endpoint": "wss://SENTINEL",
        "diagnostic": "exception via ws://SENTINEL",
        "profile_ref": "profile_ref_safe",
        "receipt_id": "receipt_safe",
    }

    class SecretService:
        def list_profile_metadata(self):
            return {"data": [dict(secret)], "meta": {"stale": False, "safe_reason": None, "last_synced_at": None}}

        def preview_profile_selection(self, _payload):
            return {"required_count": 1, "eligible_count": 1, "profiles": [dict(secret)]}

        def get_campaign_detail(self, _campaign_id):
            return {"campaign": dict(secret), "assignments": [dict(secret)]}

        def list_attempts(self, _campaign_id):
            return [dict(secret)]

        def list_receipts(self, _campaign_id):
            return [dict(secret)]

        def health(self):
            return dict(secret)

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(SecretService()))
    client = app.test_client()
    responses = [
        client.get("/api/browser-v2/comment-profile-metadata"),
        client.post("/api/browser-v2/comment-profile-selection/preview", json={"template_id": "tree", "mode": "independent"}),
        client.get("/api/browser-v2/comment-campaigns/campaign"),
        client.get("/api/browser-v2/comment-campaigns/campaign/attempts"),
        client.get("/api/browser-v2/comment-campaigns/campaign/receipts"),
        client.get("/api/browser-v2/comment-campaign-health"),
    ]

    rendered = "\n".join(response.get_data(as_text=True) for response in responses).casefold()
    assert all(response.status_code == 200 for response in responses)
    for forbidden in (
        "raw_adspower_sentinel", "cookie_sentinel", "authorization bearer",
        "api_key_sentinel", "ws://", "wss://", "exception via",
    ):
        assert forbidden not in rendered
    assert "profile_ref_safe" in rendered and "receipt_safe" in rendered


def test_persisted_attempt_and_receipt_sentinels_are_redacted_by_real_api(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    service = CommentCampaignService(store)
    template = service.create_template({
        "name": "tree", "description": "", "supported_modes": ["independent"],
        "language": "", "tags": [], "steps": [{
            "id": "root", "label": "root", "content_source": "fixed",
            "fixed_text": "safe", "content_library_id": "", "content_item_id": "",
            "parent_step_id": None, "required_profile_tags": [],
            "excluded_profile_tags": [], "language": "",
        }],
    }, "tree")
    profile_ref = store.sync_profile_identities([
        {"id": "RAW_ADSPOWER_SENTINEL", "name": "Profile", "status": "active"},
    ])[0]["profile_ref"]
    store.upsert_profile_metadata(
        profile_ref=profile_ref, expected_username="", enabled=True,
        login_verified=True, tags=[], language="", region="", cooldown_until=None,
        health_status="healthy",
    )
    campaign = service.create_campaign({
        "name": "campaign", "mode": "independent", "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": template["id"], "template_revision": template["revision"],
        "profile_refs": [profile_ref],
    }, "campaign")
    assignment = service.plan_campaign(campaign["id"], seed="seed")["assignments"][0]
    for summary in (
        "RAW_ADSPOWER_SENTINEL", "COOKIE_SENTINEL", "Authorization: Bearer SENTINEL", "api_key=SENTINEL",
    ):
        store.append_attempt(assignment["assignment_id"], "prepare", "failed", error_summary=summary)
    store.save_receipt(assignment["assignment_id"], {
        "status": "published_unverified", "diagnostic": "ws://SENTINEL",
        "raw_adspower_id": "RAW_ADSPOWER_SENTINEL",
    })
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))
    web_app = create_app({
        "TESTING": True, "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": lambda: service,
    })

    class FakeRedis:
        def set(self, *_args, **_kwargs): return True
        def eval(self, *_args, **_kwargs): return 1

    class FakeQueue:
        def __init__(self): self.calls = []
        def fetch_job(self, _job_id): return None
        def enqueue(self, function, *args, **kwargs):
            self.calls.append((function, args, kwargs))
            return {"id": kwargs["job_id"]}

    queue = FakeQueue()
    coordinator = QueueCoordinator(queue, redis=FakeRedis())
    coordinator.enqueue_prepare_generation("campaign", 1, 1)
    coordinator.enqueue_submit("campaign", assignment["assignment_id"], 1)
    payload = "\n".join([
        app.test_client().get("/api/browser-v2/comment-campaigns/campaign/attempts").get_data(as_text=True),
        app.test_client().get("/api/browser-v2/comment-campaigns/campaign/receipts").get_data(as_text=True),
        web_app.test_client().get("/comment-campaigns", base_url="http://127.0.0.1:5000").get_data(as_text=True),
        json.dumps(queue.calls),
    ]).casefold()
    assert "error_summary" not in payload
    for forbidden in ("raw_adspower_sentinel", "cookie_sentinel", "authorization:", "api_key=", "ws://"):
        assert forbidden not in payload


def test_cache_sync_failure_and_selection_use_fakes_without_external_execution(
    tmp_path, external_bombs
):
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    provider_state = {"rows": [{"id": "RAW_ADSPOWER_SENTINEL", "name": "Profile", "status": "active"}]}

    def provider():
        rows = provider_state["rows"]
        if isinstance(rows, Exception):
            raise rows
        return rows

    service = CommentCampaignService(store, profile_provider=provider)
    template = service.create_template({
        "name": "tree", "description": "", "supported_modes": ["independent"],
        "language": "", "tags": [], "steps": [{
            "id": "root", "label": "root", "content_source": "fixed",
            "fixed_text": "safe", "content_library_id": "", "content_item_id": "",
            "parent_step_id": None, "required_profile_tags": [],
            "excluded_profile_tags": [], "language": "",
        }],
    }, "tree")
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))
    client = app.test_client()

    synced = client.post("/api/browser-v2/comment-profile-metadata/sync", json={})
    preview = client.post("/api/browser-v2/comment-profile-selection/preview", json={
        "template_id": template["id"], "mode": "independent",
    })
    provider_state["rows"] = AdsPowerDependencyError("timeout")
    stale = client.post("/api/browser-v2/comment-profile-metadata/sync", json={})
    provider_state["rows"] = RuntimeError("Authorization: Bearer ERROR_SENTINEL ws://ERROR_SENTINEL")
    failed = client.post("/api/browser-v2/comment-profile-metadata/sync", json={})
    cached = client.get("/api/browser-v2/comment-profile-metadata")

    assert synced.status_code == preview.status_code == stale.status_code == cached.status_code == 200
    assert failed.status_code == 500
    assert synced.get_json()["meta"]["stale"] is False
    assert preview.get_json()["data"]["profiles"]
    assert stale.get_json()["meta"] == {
        "stale": True, "safe_reason": "timeout",
        "last_synced_at": stale.get_json()["meta"]["last_synced_at"],
    }
    rendered = "\n".join(response.get_data(as_text=True) for response in (synced, failed, cached)).casefold()
    for forbidden in ("raw_adspower_sentinel", "authorization:", "error_sentinel", "ws://"):
        assert forbidden not in rendered
    assert external_bombs.submit.attempts == 0


@pytest.mark.parametrize("action", ["enable", "delete"])
def test_template_lifecycle_foreign_requests_fail_before_factory(
    action, tmp_path
):
    created = []
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
            lambda: created.append(object()) or created[-1]
        ),
    })
    path = f"/api/browser-v2/comment-templates/template/{action}"

    remote = app.test_client().post(
        path,
        json={"expected_revision": 1},
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )
    foreign_host = app.test_client().post(
        path,
        json={"expected_revision": 1},
        base_url="http://example.test",
    )

    assert remote.status_code == 403
    assert foreign_host.status_code == 403
    assert created == []


def test_profile_sync_foreign_requests_fail_before_factory(tmp_path):
    created = []
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
            lambda: created.append(object()) or created[-1]
        ),
    })

    remote = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={},
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )
    foreign_host = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={},
        base_url="http://example.test",
    )

    assert remote.status_code == 403
    assert foreign_host.status_code == 403
    assert created == []


def test_openapi_documents_strict_comment_tree_import_contract():
    root = Path(__file__).resolve().parents[1]
    spec = json.loads(
        (root / "docs/architecture/api/openapi.yaml").read_text(encoding="utf-8")
    )
    preview = spec["paths"]["/api/browser-v2/comment-template-imports/preview"]["post"]
    commit = spec["paths"]["/api/browser-v2/comment-template-imports"]["post"]
    schemas = spec["components"]["schemas"]

    upload = preview["requestBody"]["content"]["multipart/form-data"]["schema"]
    assert upload["required"] == ["file"]
    assert upload["additionalProperties"] is False
    assert upload["properties"]["file"] == {"type": "string", "format": "binary"}
    assert {"200", "413", "422"} <= set(preview["responses"])
    assert {"201", "422"} <= set(commit["responses"])
    assert commit["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/TemplateImportCommit",
    }
    assert schemas["TemplateImportCommit"]["additionalProperties"] is False
    assert schemas["ImportedCommentTree"]["additionalProperties"] is False
    assert set(schemas["ImportedCommentTreeNode"]["properties"]) == {
        "node_no", "parent_node_no", "text",
    }
    assert {"row", "position"} <= set(
        schemas["CommentTreePreviewNode"]["properties"]
    )
    assert {"valid", "errors"} <= set(schemas["CommentTreePreview"]["properties"])


def test_openapi_documents_strict_comment_template_lifecycle_contract():
    root = Path(__file__).resolve().parents[1]
    spec = json.loads(
        (root / "docs/architecture/api/openapi.yaml").read_text(encoding="utf-8")
    )
    schemas = spec["components"]["schemas"]

    assert schemas["ExpectedRevision"] == {
        "type": "object",
        "properties": {
            "expected_revision": {"type": "integer", "minimum": 1},
        },
        "required": ["expected_revision"],
        "additionalProperties": False,
    }
    template = schemas["CommentTemplate"]
    assert template["properties"]["lifecycle_status"] == {
        "type": "string",
        "enum": ["enabled", "disabled", "deleted"],
    }
    assert "deleted templates are hidden" in template["description"]

    for action in ("disable", "enable", "delete"):
        operation = spec["paths"][
            f"/api/browser-v2/comment-templates/{{template_id}}/{action}"
        ]["post"]
        assert operation["requestBody"]["required"] is True
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ExpectedRevision",
        }
        assert operation["responses"]["200"]["content"]["application/json"][
            "schema"
        ] == {"$ref": "#/components/schemas/CommentTemplateEnvelope"}
        assert {"200", "401", "403", "404", "409", "422"} <= set(
            operation["responses"]
        )


def test_openapi_documents_profile_sync_selection_and_identity_contract():
    root = Path(__file__).resolve().parents[1]
    spec = json.loads(
        (root / "docs/architecture/api/openapi.yaml").read_text(encoding="utf-8")
    )
    paths, schemas = spec["paths"], spec["components"]["schemas"]

    metadata = paths["/api/browser-v2/comment-profile-metadata"]["get"]
    sync = paths["/api/browser-v2/comment-profile-metadata/sync"]["post"]
    preview = paths["/api/browser-v2/comment-profile-selection/preview"]["post"]
    assert metadata["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CommentProfileCacheEnvelope",
    }
    upsert = paths["/api/browser-v2/comment-profile-metadata"]["post"]
    create = paths["/api/browser-v2/comment-campaigns"]["post"]
    assert upsert["requestBody"]["required"] is True
    assert upsert["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProfileMetadataUpsert",
    }
    assert create["requestBody"]["required"] is True
    assert create["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CampaignCreate",
    }
    assert create["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CommentCampaignEnvelope",
    }
    detail = paths["/api/browser-v2/comment-campaigns/{campaign_id}"]["get"]
    assert detail["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CommentCampaignDetailEnvelope",
    }
    assert upsert["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CommentProfileMetadataEnvelope",
    }
    assert sync["requestBody"]["required"] is True
    assert sync["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EmptyRequest",
    }
    assert preview["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProfileSelectionPreview",
    }
    assert preview["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ProfileSelectionPreviewEnvelope",
    }
    assert preview["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CampaignValidationErrorEnvelope",
    }
    assert "503" not in preview["responses"]
    assert schemas["ProfileSelectionPreview"]["additionalProperties"] is False
    assert {"name", "status", "expected_username", "created_at", "updated_at"} <= set(
        schemas["CommentProfile"]["properties"]
    )
    assert "identity_generation" not in schemas["CampaignCreate"]["properties"]
    assert schemas["CommentCampaignRead"]["properties"]["identity_generation"] == {
        "type": "integer", "minimum": 0, "readOnly": True,
    }
    assert schemas["CommentAssignmentRead"]["properties"]["identity_generation"] == {
        "type": "integer", "minimum": 0, "readOnly": True,
    }
    assert paths["/api/browser-v2/comment-campaigns/{campaign_id}/plan"]["post"]["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/CampaignValidationErrorEnvelope",
    }
    assert schemas["AllocationFailureDetails"]["properties"]["reason"]["enum"] == [
        "unknown_profile_ref", "insufficient_profiles", "profile_disabled",
        "profile_unhealthy", "profile_in_cooldown", "profile_tag_mismatch",
        "profile_language_mismatch", "complete_matching_not_found",
    ]
    duplicate = schemas["IdentityFailureDetails"]
    assert duplicate["properties"]["display_profiles"]["maxItems"] == 2
    assert set(duplicate["properties"]) == {"display_profiles", "visible_username"}
    assert schemas["CommentAssignmentRead"]["properties"]["evidence"]["properties"]["identity_failure"] == {
        "$ref": "#/components/schemas/IdentityFailureDetails",
    }
    assert "details" not in schemas["AllocationErrorEnvelope"]["properties"]["error"]["required"]
    assert schemas["CampaignValidationErrorEnvelope"] == {
        "anyOf": [
            {"$ref": "#/components/schemas/ErrorEnvelope"},
            {"$ref": "#/components/schemas/AllocationErrorEnvelope"},
        ],
    }


def test_pytest_campaign_store_guard_normalizes_production_sqlite_paths(tmp_path):
    production = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "comment_campaign"
        / "comment_campaign.db"
    ).resolve()
    variants = (
        "sqlite:///data/comment_campaign/comment_campaign.db",
        f"sqlite:///{production.as_posix()}",
        "sqlite:///" + str(production).replace("/", "\\"),
    )

    assert all(
        suite_config._is_production_campaign_database(value) for value in variants
    )
    assert not suite_config._is_production_campaign_database(
        f"sqlite:///{tmp_path / 'campaign.db'}"
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/browser-v2/comment-templates",
        "/api/browser-v2/comment-settings",
    ],
)
def test_testing_gateway_default_campaign_database_fails_before_open(path):
    before = suite_config._production_campaign_db_snapshot()
    app = create_app({"TESTING": True, "LOCAL_DIRECT_MODE": True})

    response = app.test_client().get(path, base_url="http://127.0.0.1:5000")

    assert response.status_code == 500
    assert response.get_json() == {
        "error": {
            "code": "internal_error",
            "message": "\u8bf7\u6c42\u5904\u7406\u5931\u8d25\u3002",
        }
    }
    assert "comment_campaign_service" not in app.extensions
    with pytest.raises(AssertionError, match=suite_config._PRODUCTION_CAMPAIGN_DB_ERROR):
        app.extensions["comment_campaign_service_factory"]()
    assert suite_config._production_campaign_db_snapshot() == before


def test_testing_gateway_can_construct_campaign_service_with_tmp_database(tmp_path):
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-templates", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert response.get_json() == {"data": []}
    assert (tmp_path / "campaign.db").exists()


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


def test_default_testing_app_cannot_construct_production_campaign_store(tmp_path):
    del tmp_path
    before = suite_config._production_campaign_db_snapshot()
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": None,
    })

    response = app.test_client().get("/api/browser-v2/comment-templates")

    assert response.status_code == 500
    assert suite_config._production_campaign_db_snapshot() == before


def test_comment_campaign_test_tripwires_reject_real_execution(external_bombs):
    with pytest.raises(AssertionError, match="real AdsPower start forbidden"):
        external_bombs.adspower.start_browser("raw-id")
    with pytest.raises(AssertionError, match="real submit click forbidden"):
        external_bombs.submit.click()


def test_health_probe_installs_external_bombs(external_bombs):
    with pytest.raises(AssertionError, match="real HTTP forbidden"):
        external_bombs.http("http://example.test")
    with pytest.raises(AssertionError, match="real AdsPower start forbidden"):
        external_bombs.adspower.start_browser("raw-id")
    with pytest.raises(AssertionError, match="real Playwright/CDP connect forbidden"):
        asyncio.run(external_bombs.connect(None, "raw-id", "ws://example.test"))
    with pytest.raises(AssertionError, match="real submit click forbidden"):
        external_bombs.submit.click()
    assert external_bombs.submit.attempts == 1


def test_comment_campaign_module_blocks_requests_post(monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("network escaped Session.request tripwire")

    monkeypatch.setattr(requests.sessions.Session, "send", no_network)

    with pytest.raises(AssertionError, match="real HTTP forbidden"):
        requests.post("http://example.test")


def _approved_submit_executor():
    class Store:
        campaign = {
            "id": "campaign",
            "video_id": "12345678",
            "status": "running",
            "revision": 1,
            "identity_generation": 1,
        }
        assignment = {
            "assignment_id": "assignment",
            "campaign_id": "campaign",
            "profile_ref": "profile",
            "expected_username": "creator",
            "resolved_text": "hello",
            "revision": 1,
            "status": "awaiting_step_approval",
            "identity_generation": 1,
            "evidence": {"account_preflight": {"identity_generation": 1}},
        }
        error_code = None

        def get_campaign(self, _campaign_id):
            return dict(self.campaign)

        def get_assignment(self, _assignment_id):
            return dict(self.assignment)

        def get_approval(self, *_args):
            return {"consumed_at": None}

        def consume_submit_approval(self, *_args):
            return None

        def begin_submitting(self, *_args):
            self.assignment.update(status="submitting", revision=2)
            return dict(self.assignment)

        def update_assignment_status(self, *_args):
            self.assignment.update(status="verifying_receipt", revision=3)
            return dict(self.assignment)

        def save_receipt_and_transition(self, *_args, **kwargs):
            self.error_code = kwargs["error_code"]
            self.assignment["status"] = "published_unverified"
            return {"status": "published_unverified"}

    class Gateway:
        async def open_one(self, *_args, **_kwargs):
            return SimpleNamespace(page=object(), profile_id="raw-profile")

        async def refresh_leases(self, *_args, **_kwargs):
            return True

        async def close_bindings(self, *_args, **_kwargs):
            return {"raw-profile": True}

        async def release_campaign_lease(self, *_args, **_kwargs):
            return None

    store = Store()
    executor = CommentExecutor(store, Gateway(), locator_resolver=None)
    executor._preparation_evidence = lambda *_args, **_kwargs: {}

    async def verified_identity(*_args, **_kwargs):
        return "creator"

    executor._runtime_identity_or_stop = verified_identity
    return executor, store


def test_default_submit_handle_bomb_covers_prepared_submit(
    external_bombs, monkeypatch
):
    executor, store = _approved_submit_executor()

    async def prepared_page(*_args, **_kwargs):
        return {}

    monkeypatch.setattr(executor, "_prepare_page", prepared_page)

    result = asyncio.run(
        executor.submit_assignment("campaign", "assignment", 1)
    )

    assert result == {"status": "published_unverified"}
    assert store.error_code == "comment_submit_uncertain"
    assert external_bombs.submit.attempts == 1


def test_threaded_reply_scope_preserves_input_and_bombs_submit(external_bombs):
    input_handle = object()
    raw_submit = object()
    parent = {
        "author_username": "creator",
        "reply_composer": {"input": input_handle, "submit": raw_submit},
    }

    scope = asyncio.run(executor_module.open_scoped_reply(parent, "creator"))

    assert scope["input"] is input_handle
    assert scope["submit"] is external_bombs.submit
    with pytest.raises(AssertionError, match="real submit click forbidden"):
        scope["submit"].click()
    assert external_bombs.submit.attempts == 1


def test_explicit_counting_submit_fake_can_override_default_bomb(monkeypatch):
    executor, store = _approved_submit_executor()
    clicks = []

    class CountingSubmit:
        async def click(self):
            clicks.append(True)

    async def prepared_page(*_args, **_kwargs):
        return {"_submit": CountingSubmit()}

    async def screenshot(_page):
        return "evidence/0123456789abcdef0123456789abcdef.png"

    async def unverified(*_args, **_kwargs):
        return False, {}

    monkeypatch.setattr(executor, "_prepare_page", prepared_page)
    monkeypatch.setattr(executor, "_screenshot", screenshot)
    monkeypatch.setattr(executor, "_verify_post_click", unverified)

    result = asyncio.run(
        executor.submit_assignment("campaign", "assignment", 1)
    )

    assert clicks == [True]
    assert result == {"status": "published_unverified"}
    assert store.error_code == "comment_receipt_unverified"
