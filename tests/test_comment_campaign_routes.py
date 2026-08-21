from io import BytesIO
from pathlib import Path

from flask import Flask
from openpyxl import Workbook
import pytest

from comment_campaign.blueprint import create_comment_campaign_blueprint
from comment_campaign.errors import (
    AllocationError,
    CampaignValidationError,
    RevisionConflictError,
    StateTransitionError,
)
from adspower import AdsPowerDependencyError
from comment_campaign.service import CommentCampaignService
from comment_campaign.schemas import EmptyRequest
from comment_campaign.store import CampaignStore
from comment_campaign.template_import import (
    MAX_IMPORT_BYTES,
    MAX_IMPORT_ROWS,
    preview_comment_tree_workbook,
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

    def list_profile_metadata(self):
        self.calls.append("list_profile_metadata")
        return {"data": [], "meta": {"stale": True, "last_synced_at": None, "safe_reason": None}}

    def sync_profile_metadata(self):
        self.calls.append("sync_profile_metadata")
        return {"data": [], "meta": {"stale": False, "last_synced_at": None, "safe_reason": None}}

    def get_template(self, template_id):
        self.calls.append("get_template")
        return None if template_id == "missing" else {"id": template_id}

    def approve_campaign(self, campaign_id, expected_revision):
        self.calls.append("approve_campaign")
        return {"id": campaign_id, "revision": expected_revision + 1}

    def preview_template_import(self, filename, content):
        self.calls.append(("preview_template_import", filename, content))
        return {"trees": [{"name": "A", "nested": {"raw_adspower_id": "secret"}}]}

    def import_templates(self, payload):
        self.calls.append(("import_templates", payload))
        return {"created": [{"name": payload.trees[0].name}], "rejected": []}

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


def test_profile_metadata_get_and_explicit_sync_use_exact_envelope_and_strict_empty_body():
    client, service = _client()

    cached = client.get("/api/browser-v2/comment-profile-metadata")
    synced = client.post("/api/browser-v2/comment-profile-metadata/sync", json={})
    invalid = [
        client.post(
            "/api/browser-v2/comment-profile-metadata/sync",
            data="null", content_type="application/json",
        ),
        client.post("/api/browser-v2/comment-profile-metadata/sync", json=[]),
        client.post("/api/browser-v2/comment-profile-metadata/sync", json={"extra": True}),
    ]

    expected = {"data": [], "meta": {"stale": True, "last_synced_at": None, "safe_reason": None}}
    assert cached.status_code == 200
    assert cached.get_json() == expected
    assert synced.status_code == 200
    assert synced.get_json() == {
        "data": [],
        "meta": {"stale": False, "last_synced_at": None, "safe_reason": None},
    }
    assert [response.status_code for response in invalid] == [422, 422, 422]
    assert service.calls == ["list_profile_metadata", "sync_profile_metadata"]


def test_profile_selection_preview_is_strict_post_with_safe_result():
    client, service = _client()
    service.preview_profile_selection = lambda payload: {
        "required_count": 1,
        "eligible_count": 2,
        "profiles": [{"profile_ref": "profile_ref_safe", "display_profile": "Safe"}],
    }

    success = client.post(
        "/api/browser-v2/comment-profile-selection/preview",
        json={"template_id": "template", "mode": "independent"},
    )
    invalid = client.post(
        "/api/browser-v2/comment-profile-selection/preview",
        json={"template_id": "template", "mode": "independent", "extra": True},
    )

    assert success.status_code == 200
    assert success.get_json()["data"]["profiles"] == [{"profile_ref": "profile_ref_safe", "display_profile": "Safe"}]
    assert invalid.status_code == 422


def test_allocation_error_projects_only_safe_details():
    class AllocationService:
        def preview_profile_selection(self, _payload):
            error = AllocationError(
                "unknown_profile_ref", required_count=2, eligible_count=1,
                display_profiles=("Safe A", "Safe B"),
            )
            error.details["api_key"] = "must-not-leak"
            error.details["diagnostic"] = "wss://must-not-leak"
            error.details["eligible_count"] = "Authorization: Bearer SECRET"
            error.details["display_profiles"] = [{"profile_ref": "profile_ref_hidden"}]
            raise error

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(AllocationService()))
    response = app.test_client().post(
        "/api/browser-v2/comment-profile-selection/preview",
        json={"template_id": "template", "mode": "independent"},
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["details"] == {
        "reason": "unknown_profile_ref", "required_count": 2,
    }


@pytest.mark.parametrize("reason", ["not_configured", "invalid_response"])
def test_profile_sync_dependency_failure_preserves_cached_rows_without_secret(reason):
    store = CampaignStore("sqlite:///:memory:")
    store.initialize()
    store.sync_profile_identities([
        {"id": "raw-cached", "name": "Cached", "status": "active"},
    ])
    service = CommentCampaignService(
        store,
        profile_provider=lambda: (_ for _ in ()).throw(
            AdsPowerDependencyError(reason)
        ),
    )
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))

    response = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={}
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"] == service.list_profile_metadata()["data"]
    assert payload["meta"] == {
        "stale": True,
        "last_synced_at": store.profile_cache_last_synced_at(),
        "safe_reason": reason,
    }
    assert "raw-cached" not in response.get_data(as_text=True)


def test_profile_sync_store_error_remains_fixed_500():
    class BrokenStore(CampaignStore):
        def sync_profile_identities(self, _profiles):
            raise RuntimeError("db SECRET")

    store = BrokenStore("sqlite:///:memory:")
    store.initialize()
    service = CommentCampaignService(
        store, profile_provider=lambda: [{"id": "raw-secret", "name": "A", "status": "active"}]
    )
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))

    response = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={}
    )

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "internal_error"
    assert "SECRET" not in response.get_data(as_text=True)


def test_profile_sync_malformed_provider_value_remains_fixed_500():
    store = CampaignStore("sqlite:///:memory:")
    store.initialize()
    service = CommentCampaignService(store, profile_provider=lambda: [{"id": "raw-only"}])
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))

    response = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={}
    )

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "internal_error"


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: ValueError("provider SECRET"),
        lambda: TypeError("store SECRET"),
        lambda: EmptyRequest.model_validate({"extra": "SECRET"}),
    ],
)
def test_profile_sync_service_value_type_and_validation_errors_remain_fixed_500(
    error_factory,
):
    class BrokenSyncService:
        def sync_profile_metadata(self):
            raise error_factory()

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(BrokenSyncService()))

    response = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={}
    )

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "internal_error"
    assert "SECRET" not in response.get_data(as_text=True)


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: ValueError("factory SECRET"),
        lambda: TypeError("factory SECRET"),
        lambda: EmptyRequest.model_validate({"extra": "SECRET"}),
    ],
)
def test_profile_sync_factory_value_type_and_validation_errors_remain_fixed_500(
    error_factory,
):
    def broken_factory():
        raise error_factory()

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(broken_factory))

    response = app.test_client().post(
        "/api/browser-v2/comment-profile-metadata/sync", json={}
    )

    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "internal_error"
    assert "SECRET" not in response.get_data(as_text=True)


def test_profile_sync_writes_require_admin_and_csrf(admin_client, operator_client):
    path = "/api/browser-v2/comment-profile-metadata/sync"

    class SyncService:
        def sync_profile_metadata(self):
            return {
                "data": [],
                "meta": {"stale": False, "last_synced_at": None, "safe_reason": None},
            }

    admin_client.client.application.config["COMMENT_CAMPAIGN_SERVICE_FACTORY"] = SyncService
    operator_client.client.application.config["COMMENT_CAMPAIGN_SERVICE_FACTORY"] = SyncService

    missing_csrf = admin_client.client.post(path, json={})
    operator = operator_client.post(path, json={})
    administrator = admin_client.post(path, json={})

    assert missing_csrf.status_code == 403
    assert operator.status_code == 403
    assert administrator.status_code == 200


@pytest.mark.parametrize(
    ("method", "path", "payload", "status", "operation"),
    [
        ("GET", "/comment-templates/template", None, 200, "get_template"),
        ("PUT", "/comment-templates/template", {**_template_payload(), "expected_revision": 1}, 200, "update_template"),
        ("POST", "/comment-templates/template/disable", {"expected_revision": 1}, 200, "disable_template"),
        ("POST", "/comment-templates/template/enable", {"expected_revision": 2}, 200, "enable_template"),
        ("POST", "/comment-templates/template/delete", {"expected_revision": 3}, 200, "delete_template"),
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
    assert set(response.get_json()) == (
        {"data", "meta"}
        if operation == "list_profile_metadata"
        else {"data"}
    )
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


@pytest.mark.parametrize("action", ["enable", "delete"])
@pytest.mark.parametrize(
    "payload",
    [{}, {"expected_revision": "1"}, {"expected_revision": 1, "extra": True}],
)
def test_template_lifecycle_routes_require_strict_expected_revision(action, payload):
    client, service = _client()

    response = client.post(
        f"/api/browser-v2/comment-templates/template/{action}", json=payload
    )

    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_failed"
    assert action + "_template" not in service.calls


def test_template_unavailable_maps_to_fixed_409_without_exception_details():
    class UnavailableService:
        def list_templates(self):
            raise CampaignValidationError("template_unavailable", "secret template id")

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(UnavailableService()))
    response = app.test_client().get("/api/browser-v2/comment-templates")

    assert response.status_code == 409
    assert response.get_json() == {
        "error": {
            "code": "template_unavailable",
            "message": "所选评论树已停用或删除。",
        }
    }
    assert "secret" not in response.get_data(as_text=True)


def test_template_lifecycle_success_response_is_recursively_redacted():
    class SecretLifecycleService:
        def enable_template(self, _template_id, _revision):
            return {
                "id": "safe",
                "nested": {
                    "raw_adspower_id": "raw-secret",
                    "cookie": "cookie-secret",
                    "diagnostic": "failed at ws://secret.example",
                },
            }

    app = Flask(__name__)
    app.register_blueprint(
        create_comment_campaign_blueprint(SecretLifecycleService())
    )
    response = app.test_client().post(
        "/api/browser-v2/comment-templates/template/enable",
        json={"expected_revision": 2},
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "data": {"id": "safe", "nested": {"diagnostic": "[redacted]"}}
    }
    serialized = response.get_data(as_text=True)
    assert "raw-secret" not in serialized
    assert "cookie-secret" not in serialized


def test_template_lifecycle_api_preserves_error_priority_and_hides_deleted(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    store.initialize()
    service = CommentCampaignService(store)
    service.create_template(_template_payload(), "template")
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))
    client = app.test_client()
    base = "/api/browser-v2/comment-templates/template"

    missing = client.post(
        "/api/browser-v2/comment-templates/missing/enable",
        json={"expected_revision": 1},
    )
    invalid_enable = client.post(
        base + "/enable", json={"expected_revision": 1}
    )
    disabled = client.post(
        base + "/disable", json={"expected_revision": 1}
    ).get_json()["data"]
    stale_enable = client.post(
        base + "/enable", json={"expected_revision": 1}
    )
    enabled = client.post(
        base + "/enable", json={"expected_revision": disabled["revision"]}
    ).get_json()["data"]
    stale_delete = client.post(
        base + "/delete", json={"expected_revision": disabled["revision"]}
    )
    invalid_delete = client.post(
        base + "/delete", json={"expected_revision": enabled["revision"]}
    )
    disabled = client.post(
        base + "/disable", json={"expected_revision": enabled["revision"]}
    ).get_json()["data"]
    deleted = client.post(
        base + "/delete", json={"expected_revision": disabled["revision"]}
    )

    assert missing.status_code == 404
    assert invalid_enable.status_code == 409
    assert invalid_enable.get_json()["error"]["code"] == "invalid_state_transition"
    assert stale_enable.status_code == 409
    assert stale_enable.get_json()["error"]["code"] == "revision_conflict"
    assert stale_delete.status_code == 409
    assert stale_delete.get_json()["error"]["code"] == "revision_conflict"
    assert invalid_delete.status_code == 409
    assert invalid_delete.get_json()["error"]["code"] == "invalid_state_transition"
    assert deleted.status_code == 200
    assert client.get(base).status_code == 404
    assert client.get("/api/browser-v2/comment-templates").get_json() == {"data": []}
    assert client.post(
        base + "/delete", json={"expected_revision": 5}
    ).status_code == 404
    store.close()


@pytest.mark.parametrize("action", ["enable", "delete"])
def test_template_lifecycle_writes_require_admin_and_csrf(
    action, admin_client, operator_client
):
    path = f"/api/browser-v2/comment-templates/missing/{action}"

    missing_csrf = admin_client.client.post(
        path, json={"expected_revision": 1}
    )
    operator = operator_client.post(path, json={"expected_revision": 1})
    administrator = admin_client.post(path, json={"expected_revision": 1})

    assert missing_csrf.status_code == 403
    assert operator.status_code == 403
    assert administrator.status_code == 404


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


def _import_commit_payload():
    return {"trees": [{"name": "A", "nodes": [{"node_no": "1", "parent_node_no": None, "text": "root"}]}]}


def _valid_import_xlsx(*, data_rows=1):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["tree_name", "node_no", "parent_node_no", "text"])
    for index in range(data_rows):
        worksheet.append([f"Tree {index}", "1", None, "root"])
    output = BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def test_import_routes_use_exact_multipart_part_and_commit_schema():
    client, service = _client()
    preview = client.post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(b"workbook"), "trees.xlsx")},
        content_type="multipart/form-data",
    )
    committed = client.post("/api/browser-v2/comment-template-imports", json=_import_commit_payload())

    assert preview.status_code == 200
    assert preview.get_json() == {"data": {"trees": [{"name": "A", "nested": {}}]}}
    assert service.calls[-2] == ("preview_template_import", "trees.xlsx", b"workbook")
    assert committed.status_code == 201
    assert committed.get_json() == {"data": {"created": [{"name": "A"}], "rejected": []}}
    assert service.calls[-1][0] == "import_templates"
    assert service.calls[-1][1].trees[0].name == "A"


def test_import_preview_rejects_query_form_duplicate_or_extra_file_parts():
    client, service = _client()
    cases = [
        ("/api/browser-v2/comment-template-imports/preview?x=1", {"file": (BytesIO(b"x"), "trees.xlsx")}),
        ("/api/browser-v2/comment-template-imports/preview", {"file": (BytesIO(b"x"), "trees.xlsx"), "note": "x"}),
        ("/api/browser-v2/comment-template-imports/preview", {"file": [(BytesIO(b"x"), "one.xlsx"), (BytesIO(b"x"), "two.xlsx")]}),
        ("/api/browser-v2/comment-template-imports/preview", {"file": (BytesIO(b"x"), "trees.xlsx"), "other": (BytesIO(b"x"), "other.xlsx")}),
    ]

    for path, data in cases:
        response = client.post(path, data=data, content_type="multipart/form-data")
        assert response.status_code == 400
        assert response.get_json()["error"]["code"] == "invalid_request"
    assert service.calls == []


def test_import_routes_keep_fixed_errors_and_do_not_leak_unknown_exceptions():
    class ErrorService:
        def preview_template_import(self, _filename, _content):
            raise CampaignValidationError("unsupported_import_type", "secret type")

        def import_templates(self, _payload):
            raise RuntimeError("database secret")

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(ErrorService()))
    client = app.test_client()
    preview = client.post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(b"x"), "trees.txt")}, content_type="multipart/form-data",
    )
    committed = client.post("/api/browser-v2/comment-template-imports", json=_import_commit_payload())

    assert preview.status_code == 422
    assert preview.get_json() == {"error": {"code": "unsupported_import_type", "message": "仅支持 .xlsx 文件。"}}
    assert committed.status_code == 500
    assert committed.get_json()["error"]["code"] == "internal_error"
    assert "secret" not in committed.get_data(as_text=True)


def test_import_preview_maps_request_entity_too_large_to_fixed_413_envelope():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1
    app.register_blueprint(create_comment_campaign_blueprint(FakeService()))

    response = app.test_client().post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(b"workbook"), "trees.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "error": {
            "code": "import_file_too_large",
            "message": "导入文件不能超过 2 MiB 或 5000 数据行。",
        },
    }


def test_import_preview_maps_type_content_and_route_size_errors_without_leaks():
    class PreviewService:
        def preview_template_import(self, filename, content):
            return preview_comment_tree_workbook(filename, content)

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(PreviewService()))
    client = app.test_client()
    cases = [
        ("trees.csv", b"x", "unsupported_import_type", "仅支持 .xlsx 文件。"),
        ("trees.xlsx", b"not a workbook", "import_file_invalid", "导入文件无效或已损坏。"),
        (
            "trees.xlsx",
            b"x" * (MAX_IMPORT_BYTES + 1),
            "import_file_too_large",
            "导入文件不能超过 2 MiB 或 5000 数据行。",
        ),
    ]

    for filename, content, code, message in cases:
        response = client.post(
            "/api/browser-v2/comment-template-imports/preview",
            data={"file": (BytesIO(content), filename)}, content_type="multipart/form-data",
        )
        assert response.status_code == (413 if code == "import_file_too_large" else 422)
        assert response.get_json() == {"error": {"code": code, "message": message}}


def test_import_commit_uses_fixed_import_tree_failed_message():
    class ErrorService:
        def import_templates(self, _payload):
            raise CampaignValidationError("import_tree_failed", "secret persistence detail")

    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(ErrorService()))
    response = app.test_client().post(
        "/api/browser-v2/comment-template-imports", json=_import_commit_payload()
    )

    assert response.status_code == 422
    assert response.get_json() == {
        "error": {"code": "import_tree_failed", "message": "评论树导入失败。"},
    }


def test_import_commit_inherits_management_role_and_csrf(
    admin_client, operator_client, tmp_path
):
    payload = _import_commit_payload()
    workbook = _valid_import_xlsx()
    app = admin_client.client.application
    campaign_root = tmp_path / "comment-campaign"

    assert "comment_campaign_service" not in app.extensions
    assert app.config["COMMENT_CAMPAIGN_DB_URL"] == (
        f"sqlite:///{(campaign_root / 'comment_campaign.db').as_posix()}"
    )
    assert Path(app.config["COMMENT_CAMPAIGN_EVIDENCE_DIR"]) == (
        campaign_root / "evidence"
    )

    missing_csrf = admin_client.client.post(
        "/api/browser-v2/comment-template-imports", json=payload
    )
    operator = operator_client.post(
        "/api/browser-v2/comment-template-imports", json=payload
    )
    administrator = admin_client.post(
        "/api/browser-v2/comment-template-imports", json=payload
    )
    missing_preview_csrf = admin_client.client.post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(workbook), "trees.xlsx")},
        content_type="multipart/form-data",
    )
    operator_preview = operator_client.post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(workbook), "trees.xlsx")},
        content_type="multipart/form-data",
    )
    administrator_preview = admin_client.post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(workbook), "trees.xlsx")},
        content_type="multipart/form-data",
    )

    assert missing_csrf.status_code == 403
    assert operator.status_code == 403
    assert administrator.status_code == 201
    assert missing_preview_csrf.status_code == 403
    assert operator_preview.status_code == 403
    assert administrator_preview.status_code == 200
    assert "comment_campaign_service" in app.extensions


def test_import_preview_maps_more_than_5000_rows_to_fixed_413_envelope(tmp_path):
    content = _valid_import_xlsx(data_rows=MAX_IMPORT_ROWS + 1)
    assert len(content) < MAX_IMPORT_BYTES

    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    service = CommentCampaignService(store)
    app = Flask(__name__)
    app.register_blueprint(create_comment_campaign_blueprint(service))

    response = app.test_client().post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(content), "trees.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 413
    assert response.get_json() == {
        "error": {
            "code": "import_file_too_large",
            "message": "导入文件不能超过 2 MiB 或 5000 数据行。",
        },
    }
