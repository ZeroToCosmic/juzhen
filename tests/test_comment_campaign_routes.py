from flask import Flask
import pytest

from comment_campaign.blueprint import create_comment_campaign_blueprint
from comment_campaign.errors import (
    CampaignValidationError,
    RevisionConflictError,
    StateTransitionError,
)


class FakeService:
    def __init__(self):
        self.calls = []

    def list_templates(self):
        self.calls.append("list_templates")
        return [{
            "id": "template",
            "raw_profile_id": "legacy-secret",
            "raw_adspower_id": "RAW_ADSPOWER_SENTINEL",
            "nested": {
                "raw_adspower_ids": ["RAW_ADSPOWER_SENTINEL_2"],
                "diagnostic": "failure at wss://private-endpoint",
            },
            "profile_ref": "safe",
        }]

    def create_template(self, payload):
        self.calls.append("create_template")
        return {"id": "template", "name": payload.name}

    def get_template(self, template_id):
        self.calls.append("get_template")
        return None if template_id == "missing" else {"id": template_id}

    def approve_campaign(self, campaign_id, expected_revision):
        self.calls.append("approve_campaign")
        return {"id": campaign_id, "revision": expected_revision + 1}

    def __getattr__(self, name):
        def operation(*_args, **_kwargs):
            self.calls.append(name)
            return {"operation": name}

        return operation


def _client():
    app = Flask(__name__)
    service = FakeService()
    app.register_blueprint(create_comment_campaign_blueprint(service))
    return app.test_client(), service


def _template_payload():
    return {
        "name": "thread",
        "description": "",
        "supported_modes": ["threaded"],
        "language": "en",
        "tags": [],
        "steps": [{
            "id": "root", "label": "owner", "content_source": "fixed",
            "fixed_text": "hello", "content_library_id": "",
            "content_item_id": "", "parent_step_id": None,
            "required_profile_tags": [], "excluded_profile_tags": [], "language": "en",
        }],
    }


def test_template_routes_use_envelopes_pydantic_and_redaction():
    client, service = _client()

    listed = client.get("/api/browser-v2/comment-templates")
    created = client.post("/api/browser-v2/comment-templates", json=_template_payload())
    invalid = client.post(
        "/api/browser-v2/comment-templates",
        json={**_template_payload(), "unknown": True},
    )

    assert listed.status_code == 200
    assert listed.get_json() == {"data": [{
        "id": "template",
        "nested": {"diagnostic": "[redacted]"},
        "profile_ref": "safe",
    }]}
    rendered = listed.get_data(as_text=True)
    assert "RAW_ADSPOWER_SENTINEL" not in rendered
    assert "wss://" not in rendered
    assert created.status_code == 201
    assert created.get_json() == {"data": {"id": "template", "name": "thread"}}
    assert invalid.status_code == 422
    assert invalid.get_json()["error"]["code"] == "validation_failed"
    assert service.calls == ["list_templates", "create_template"]


def test_missing_and_future_actions_fail_closed():
    client, _service = _client()

    missing = client.get("/api/browser-v2/comment-templates/missing")
    action = client.post(
        "/api/browser-v2/comment-campaigns/campaign/approve",
        json={"expected_revision": 1},
    )
    bad_action = client.post(
        "/api/browser-v2/comment-campaigns/campaign/approve",
        json={"expected_revision": 1, "extra": True},
    )

    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "not_found"
    assert action.status_code == 202
    assert action.get_json() == {"data": {"id": "campaign", "revision": 2}}
    assert bad_action.status_code == 422


def test_future_route_without_capability_is_explicitly_unavailable_and_get_queries_are_closed():
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(object()))
    client = app.test_client()

    unavailable = client.get("/api/browser-v2/comment-campaign-health")
    repeated = client.get("/api/browser-v2/comment-templates?x=1&x=2")
    unknown = client.get("/api/browser-v2/comment-templates?x=1")

    assert unavailable.status_code == 503
    assert unavailable.get_json()["error"]["code"] == "runtime_unavailable"
    assert repeated.status_code == 400
    assert repeated.get_json()["error"]["code"] == "invalid_request"
    assert unknown.status_code == 400
    assert unknown.get_json()["error"]["code"] == "invalid_request"


@pytest.mark.parametrize(
    ("method", "path", "payload", "status", "operation"),
    [
        ("GET", "/comment-templates/template", None, 200, "get_template"),
        ("PUT", "/comment-templates/template", {**_template_payload(), "expected_revision": 1}, 200, "update_template"),
        ("POST", "/comment-templates/template/disable", {"expected_revision": 1}, 200, "disable_template"),
        ("GET", "/comment-profile-metadata", None, 200, "list_profile_metadata"),
        ("POST", "/comment-profile-metadata", {"profile_ref": "profile_ref_a", "enabled": True, "login_verified": True, "tags": [], "language": "en", "region": "", "cooldown_until": None, "health_status": "healthy"}, 200, "upsert_profile_metadata"),
        ("GET", "/comment-campaigns", None, 200, "list_campaigns"),
        ("POST", "/comment-campaigns", {"name": "campaign", "mode": "threaded", "target_source": "manual_url", "target_reference": "https://www.tiktok.com/@a/video/12345678", "template_id": "template", "profile_refs": ["profile_ref_a"]}, 201, "create_campaign"),
        ("GET", "/comment-campaigns/campaign", None, 200, "get_campaign_detail"),
        ("POST", "/comment-campaigns/campaign/plan", {"expected_revision": 1, "allocation_seed": "seed"}, 200, "plan_campaign"),
        ("POST", "/comment-campaigns/campaign/reallocate", {"expected_revision": 1, "allocation_seed": "seed"}, 200, "reallocate_campaign"),
        ("PUT", "/comment-campaigns/campaign/assignments/assignment", {"expected_revision": 1, "profile_ref": "profile_ref_a"}, 200, "override_assignment"),
        ("POST", "/comment-campaigns/campaign/lock-plan", {"expected_revision": 1}, 200, "lock_plan"),
        ("POST", "/comment-campaigns/campaign/pause", {"expected_revision": 1, "reason": "manual"}, 200, "pause_campaign"),
        ("POST", "/comment-campaigns/campaign/resume", {"expected_revision": 1}, 202, "resume_campaign"),
        ("POST", "/comment-campaigns/campaign/cancel", {"expected_revision": 1}, 200, "cancel_campaign"),
        ("GET", "/comment-campaigns/campaign/approvals", None, 200, "list_approvals"),
        ("POST", "/comment-campaigns/campaign/assignments/assignment/approve-submit", {"expected_revision": 1}, 202, "approve_submit"),
        ("POST", "/comment-campaigns/campaign/assignments/assignment/reject-submit", {"expected_revision": 1, "reason": "manual"}, 200, "reject_submit"),
        ("POST", "/comment-campaigns/campaign/assignments/assignment/resolve-unverified", {"expected_revision": 1, "resolution": "published", "reason": "manual"}, 200, "resolve_unverified"),
        ("GET", "/comment-campaigns/campaign/receipts", None, 200, "list_receipts"),
        ("GET", "/comment-campaigns/campaign/attempts", None, 200, "list_attempts"),
        ("GET", "/comment-campaign-health", None, 200, "health"),
        ("GET", "/comment-settings", None, 200, "get_comment_settings"),
        ("PUT", "/comment-settings", {"expected_revision": 1, "entry_element_id": "entry", "input_element_id": "input", "submit_element_id": "submit", "account_element_id": "account"}, 200, "update_comment_settings"),
    ],
)
def test_remaining_route_contracts_delegate_to_service(method, path, payload, status, operation):
    client, service = _client()

    response = client.open(
        f"/api/browser-v2{path}", method=method, json=payload
    )

    assert response.status_code == status
    assert set(response.get_json()) == {"data"}
    assert service.calls[-1] == operation


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RevisionConflictError("secret-record"), 409, "revision_conflict"),
        (StateTransitionError("secret-current", "secret-target"), 409, "invalid_state_transition"),
        (CampaignValidationError("target_video_invalid", "secret URL"), 422, "target_video_invalid"),
        (CampaignValidationError("adspower_unavailable", "secret endpoint"), 503, "adspower_unavailable"),
    ],
)
def test_domain_errors_keep_stable_codes_and_fixed_messages(error, status, code):
    class RaisingService:
        def list_templates(self):
            raise error

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(RaisingService()))
    response = app.test_client().get("/api/browser-v2/comment-templates")

    assert response.status_code == status
    assert response.get_json()["error"]["code"] == code
    assert "secret" not in response.get_data(as_text=True).lower()


def test_success_payload_redaction_is_recursive_and_preserves_safe_references():
    class SecretService:
        def list_templates(self):
            return {
                "profile_ref": "profile_ref_safe",
                "nested": [{
                    "RAW-PROFILE-ID": "raw-secret",
                    "Cookie": "cookie-secret",
                    "socket": "  WSS://secret.example/devtools  ",
                    "assignment_id": "assignment-safe",
                }],
            }

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(SecretService()))
    response = app.test_client().get("/api/browser-v2/comment-templates")

    assert response.status_code == 200
    payload = response.get_json()["data"]
    assert payload["profile_ref"] == "profile_ref_safe"
    assert payload["nested"] == [{
        "socket": "[redacted]",
        "assignment_id": "assignment-safe",
    }]
    assert "raw-secret" not in response.get_data(as_text=True)
    assert "cookie-secret" not in response.get_data(as_text=True)
