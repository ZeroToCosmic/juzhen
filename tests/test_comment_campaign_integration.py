from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import threading
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

import gateway.app as app_module
from comment_campaign.service import CommentCampaignService
from comment_campaign.store import CampaignStore
from gateway.app import create_app


class FakeCampaignService:
    def __init__(self):
        self.closed = 0

    def list_templates(self):
        return []

    def close(self):
        self.closed += 1


def test_campaign_service_is_lazy_cached_and_local_guarded(tmp_path):
    created = []

    def factory():
        service = FakeCampaignService()
        created.append(service)
        return service

    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": factory,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })
    client = app.test_client()

    first = client.get("/api/browser-v2/comment-templates", base_url="http://127.0.0.1:5000")
    second = client.get("/api/browser-v2/comment-templates", base_url="http://127.0.0.1:5000")
    remote = client.get(
        "/api/browser-v2/comment-templates",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )

    assert first.get_json() == {"data": []}
    assert second.status_code == 200
    assert remote.status_code == 403
    assert len(created) == 1
    app.extensions["comment_campaign_close"]()
    assert created[0].closed == 1


def test_remote_campaign_request_is_rejected_before_service_creation(tmp_path):
    created = []
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
            lambda: created.append(FakeCampaignService()) or created[-1]
        ),
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-templates",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )

    assert response.status_code == 403
    assert created == []


def test_remote_campaign_import_preview_is_rejected_before_service_creation(tmp_path):
    created = []
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
            lambda: created.append(FakeCampaignService()) or created[-1]
        ),
    })

    remote_address = app.test_client().post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(b"workbook"), "trees.xlsx")},
        content_type="multipart/form-data",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )
    foreign_host = app.test_client().post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(b"workbook"), "trees.xlsx")},
        content_type="multipart/form-data",
        base_url="http://example.test",
    )
    remote_commit = app.test_client().post(
        "/api/browser-v2/comment-template-imports",
        json={"trees": [{"name": "A", "nodes": [{
            "node_no": "1", "parent_node_no": None, "text": "root",
        }]}]},
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    )
    foreign_commit = app.test_client().post(
        "/api/browser-v2/comment-template-imports",
        json={"trees": [{"name": "A", "nodes": [{
            "node_no": "1", "parent_node_no": None, "text": "root",
        }]}]},
        base_url="http://example.test",
    )

    assert {
        remote_address.status_code,
        foreign_host.status_code,
        remote_commit.status_code,
        foreign_commit.status_code,
    } == {403}
    assert created == []


def test_campaign_factory_is_singleton_under_concurrent_first_access(tmp_path):
    created = []
    created_lock = threading.Lock()

    def factory():
        with created_lock:
            service = FakeCampaignService()
            created.append(service)
            return service

    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": factory,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })
    service_factory = app.extensions["comment_campaign_service_factory"]
    barrier = threading.Barrier(8)

    def get_service():
        barrier.wait(timeout=3)
        return service_factory()

    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(lambda _value: get_service(), range(8)))

    assert len(created) == 1
    assert all(service is created[0] for service in services)


def test_campaign_blueprint_inherits_management_auth(monkeypatch, tmp_path):
    created = []
    monkeypatch.setattr(app_module, "load_or_create_session_key", lambda _path: "test")
    state_dir = tmp_path / "management-state"
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": False,
        "MANAGEMENT_STATE_DIR": state_dir,
        "MANAGEMENT_DB_PATH": state_dir / "management.db",
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_SERVICE_FACTORY": (
        lambda: created.append(FakeCampaignService()) or created[-1]
        ),
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-templates"
    )
    import_response = app.test_client().post(
        "/api/browser-v2/comment-template-imports",
        json={"trees": [{"name": "A", "nodes": [{
            "node_no": "1", "parent_node_no": None, "text": "root",
        }]}]},
    )
    preview_response = app.test_client().post(
        "/api/browser-v2/comment-template-imports/preview",
        data={"file": (BytesIO(b"workbook"), "trees.xlsx")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 401
    assert import_response.status_code == 403
    assert preview_response.status_code == 403
    assert created == []


def test_import_does_not_change_existing_campaign_template_revision(tmp_path):
    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    service = CommentCampaignService(store)
    original = service.create_template({
        "name": "Stable tree",
        "description": "",
        "supported_modes": ["threaded"],
        "language": "",
        "tags": [],
        "steps": [{
            "id": "stable-root",
            "label": "楼主评论",
            "content_source": "fixed",
            "fixed_text": "stable text",
            "content_library_id": "",
            "content_item_id": "",
            "parent_step_id": None,
            "required_profile_tags": [],
            "excluded_profile_tags": [],
            "language": "",
        }, {
            "id": "stable-reply",
            "label": "第1层回复",
            "content_source": "fixed",
            "fixed_text": "stable reply",
            "content_library_id": "",
            "content_item_id": "",
            "parent_step_id": "stable-root",
            "required_profile_tags": [],
            "excluded_profile_tags": [],
            "language": "",
        }],
    }, "stable-template")
    profile_refs = [row["profile_ref"] for row in store.sync_profile_identities([
        {"id": "raw-profile", "name": "Profile", "status": "active"},
        {"id": "raw-profile-2", "name": "Profile 2", "status": "active"},
    ])]
    for index, profile_ref in enumerate(profile_refs):
        store.upsert_profile_metadata(
            profile_ref=profile_ref,
            expected_username=f"user{index}",
            enabled=True,
            login_verified=True,
            tags=[],
            language="",
            region="",
            cooldown_until=None,
            health_status="healthy",
        )
    campaign = service.create_campaign({
        "name": "Stable campaign",
        "mode": "threaded",
        "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": original["id"],
        "template_revision": original["revision"],
        "profile_refs": profile_refs,
    }, "stable-campaign")
    planned = service.plan_campaign(campaign["id"], seed="stable-seed")
    frozen_before = {
        "template_snapshot": planned["campaign"]["template_snapshot"],
        "assignments": [{
            "step_id": row["step_id"],
            "resolved_text": row["resolved_text"],
            "parent_assignment_id": row["parent_assignment_id"],
        } for row in planned["assignments"]],
    }

    service.import_templates({"trees": [{
        "name": "Imported tree",
        "nodes": [{"node_no": "1", "parent_node_no": None, "text": "new text"}],
    }]})

    detail = service.get_campaign_detail(campaign["id"])
    assert detail["campaign"]["template_id"] == "stable-template"
    assert detail["campaign"]["template_revision"] == original["revision"]
    assert detail["campaign"]["template_snapshot"] == frozen_before["template_snapshot"]
    assert [{
        "step_id": row["step_id"],
        "resolved_text": row["resolved_text"],
        "parent_assignment_id": row["parent_assignment_id"],
    } for row in detail["assignments"]] == frozen_before["assignments"]


def test_locked_campaign_uses_frozen_tree_after_template_is_deleted(
    tmp_path, monkeypatch
):
    store = CampaignStore(f"sqlite:///{tmp_path / 'locked.db'}")
    store.initialize()
    queue_calls = []

    class Queue:
        def enqueue_prepare_generation(self, campaign_id, generation, identity_generation):
            queue_calls.append((campaign_id, generation, identity_generation))
            return {"id": f"prepare-{campaign_id}-g{generation}"}

    executor_calls = []

    class Executor:
        async def prepare_batch(self, campaign_id, assignment_ids, identity_generation):
            executor_calls.append(("prepare", campaign_id, tuple(assignment_ids)))
            return SimpleNamespace(
                prepared=tuple(assignment_ids), failed=(), close_confirmed=True
            )

        async def submit_assignment(self, campaign_id, assignment_id, revision):
            executor_calls.append(("submit", campaign_id, assignment_id, revision))
            return {"assignment_id": assignment_id, "status": "fake-submitted"}

    service = CommentCampaignService(
        store, queue_coordinator=Queue(), executor=Executor()
    )
    template = service.create_template({
        "name": "Frozen tree", "description": "",
        "supported_modes": ["threaded"], "language": "", "tags": [],
        "steps": [{
            "id": "root", "label": "root", "content_source": "fixed",
            "fixed_text": "frozen root", "content_library_id": "",
            "content_item_id": "", "parent_step_id": None,
            "required_profile_tags": [], "excluded_profile_tags": [], "language": "",
        }, {
            "id": "reply", "label": "reply", "content_source": "fixed",
            "fixed_text": "frozen reply", "content_library_id": "",
            "content_item_id": "", "parent_step_id": "root",
            "required_profile_tags": [], "excluded_profile_tags": [], "language": "",
        }],
    }, "frozen-template")
    profile_refs = [row["profile_ref"] for row in store.sync_profile_identities([
        {"id": "raw-one", "name": "One", "status": "active"},
        {"id": "raw-two", "name": "Two", "status": "active"},
    ])]
    for profile_ref in profile_refs:
        store.upsert_profile_metadata(
            profile_ref=profile_ref, expected_username="user", enabled=True,
            login_verified=True, tags=[], language="", region="",
            cooldown_until=None, health_status="healthy",
        )
    campaign = service.create_campaign({
        "name": "Frozen campaign", "mode": "threaded",
        "target_source": "manual_url",
        "target_reference": "https://www.tiktok.com/@owner/video/12345678",
        "template_id": template["id"], "profile_refs": profile_refs,
    }, "frozen-campaign")
    planned = service.plan_campaign(campaign["id"], seed="frozen")
    locked = service.lock_plan(campaign["id"], planned["campaign"]["revision"])
    frozen_before = json.dumps({
        "template_id": locked["template_id"],
        "template_revision": locked["template_revision"],
        "template_snapshot": locked["template_snapshot"],
        "assignments": [{
            "assignment_id": row["assignment_id"],
            "step_id": row["step_id"],
            "resolved_text": row["resolved_text"],
            "parent_assignment_id": row["parent_assignment_id"],
            "role": row["role"], "position": row["position"],
        } for row in store.list_assignments(campaign["id"])],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    disabled = service.disable_template(template["id"], template["revision"])
    service.delete_template(template["id"], disabled["revision"])

    monkeypatch.setattr(
        store, "get_template",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("locked execution read current template")
        ),
    )
    approved = service.approve_campaign(campaign["id"], locked["revision"])
    running = store.transition_campaign_status(
        campaign["id"], approved["campaign"]["revision"], "running"
    )
    assignments = store.list_assignments(campaign["id"])
    frozen = store.freeze_campaign_identities(
        campaign["id"], running["revision"], 0, tuple({
            "assignment_id": row["assignment_id"], "profile_ref": row["profile_ref"],
            "account_key": f"frozen.{index}", "visible_username": f"Frozen {index}",
            "canonical_href": f"https://www.tiktok.com/@frozen.{index}",
            "observed_at": "2026-08-11T00:00:00Z",
            "target_video": {
                "video_id": "12345678",
                "canonical_url": "https://www.tiktok.com/@owner/video/12345678",
            },
            "element_binding": {
                "id": "account", "revision": 1, "definition_sha256": "a" * 64,
            },
        } for index, row in enumerate(assignments)))
    prepared = service.prepare_campaign(
        campaign["id"], frozen["prepare_generation"], frozen["identity_generation"]
    )
    assignment = store.list_assignments(campaign["id"])[0]
    submitted = service.submit_assignment(
        campaign["id"], assignment["assignment_id"], assignment["revision"]
    )
    detail = service.get_campaign_detail(campaign["id"])
    frozen_after = json.dumps({
        "template_id": detail["campaign"]["template_id"],
        "template_revision": detail["campaign"]["template_revision"],
        "template_snapshot": detail["campaign"]["template_snapshot"],
        "assignments": [{
            "assignment_id": row["assignment_id"],
            "step_id": row["step_id"],
            "resolved_text": row["resolved_text"],
            "parent_assignment_id": row["parent_assignment_id"],
            "role": row["role"], "position": row["position"],
        } for row in detail["assignments"]],
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    assert approved["campaign"]["status"] == "queued"
    assert prepared["close_confirmed"] is True
    assert submitted["status"] == "fake-submitted"
    assert frozen_after == frozen_before
    assert queue_calls and {call[0] for call in executor_calls} == {"prepare", "submit"}


def test_import_preview_and_commit_do_not_touch_external_dependencies(tmp_path):
    def forbidden_call(*_args, **_kwargs):
        raise AssertionError("import touched an external dependency")

    class Bomb:
        def __getattr__(self, _name):
            return forbidden_call

    store = CampaignStore(f"sqlite:///{tmp_path / 'campaign.db'}")
    store.initialize()
    service = CommentCampaignService(
        store,
        publish_result_resolver=forbidden_call,
        content_resolver=forbidden_call,
        profile_provider=forbidden_call,
        queue_coordinator=Bomb(),
        executor=Bomb(),
        adspower_probe=forbidden_call,
    )
    workbook = Workbook()
    workbook.active.append(["评论树名称", "节点序号", "回复节点序号", "评论文案"])
    workbook.active.append(["A", "1", "", "root"])
    output = BytesIO()
    workbook.save(output)

    preview = service.preview_template_import("trees.xlsx", output.getvalue())
    result = service.import_templates({"trees": [{
        "name": tree["name"],
        "nodes": [{
            "node_no": node["node_no"],
            "parent_node_no": node["parent_node_no"],
            "text": node["text"],
        } for node in tree["nodes"]],
    } for tree in preview["trees"]]})

    assert [item["name"] for item in result["created"]] == ["A"]


def test_default_campaign_profile_provider_syncs_only_on_explicit_request_and_whitelists(
    monkeypatch, tmp_path
):
    class FakeController:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        def list_all_profiles(self):
            type(self).calls += 1
            return [{
                "id": "raw-adspower-secret", "name": "Alice",
                "status": "active", "group_name": "private-group",
            }]

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(app_module, "load_settings", lambda: {
        "adspower": {"base_url": "http://fake", "api_key": "fake-key"},
    })
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })

    client = app.test_client()
    cached = client.get(
        "/api/browser-v2/comment-profile-metadata",
        base_url="http://127.0.0.1:5000",
    )

    assert cached.status_code == 200
    assert cached.get_json()["data"] == []
    assert FakeController.calls == 0

    synced = client.post(
        "/api/browser-v2/comment-profile-metadata/sync", json={},
        base_url="http://127.0.0.1:5000",
    )
    cached_again = client.get(
        "/api/browser-v2/comment-profile-metadata",
        base_url="http://127.0.0.1:5000",
    )

    assert synced.status_code == 200
    rows = synced.get_json()["data"]
    assert rows[0]["name"] == "Alice"
    assert rows[0]["configured"] is True
    assert rows[0]["profile_ref"].startswith("profile_ref_")
    assert FakeController.calls == 1
    assert "raw-adspower-secret" not in repr(synced.get_json())
    assert "group_name" not in rows[0]
    assert cached_again.status_code == 200
    assert cached_again.get_json()["data"] == rows
    assert FakeController.calls == 1


def test_campaign_workbench_page_uses_local_management_shell_without_raw_profile_ids(tmp_path):
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })

    response = app.test_client().get(
        "/comment-campaigns", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert 'meta name="csrf-token"' in page
    assert page.index("management_fetch.js") < page.index("comment_campaign.js")
    assert 'href="/comment-campaigns"' in page
    assert "raw-adspower-secret" not in page
    assert "评论 Campaign" in page
    assert app.test_client().get(
        "/comment-campaigns", base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    ).status_code == 403
    assert app.test_client().get(
        "/comment-campaign-evidence/0123456789abcdef0123456789abcdef.png",
        base_url="http://127.0.0.1:5000",
        environ_overrides={"REMOTE_ADDR": "192.0.2.1"},
    ).status_code == 403


def test_campaign_settings_write_requires_admin_and_csrf(tmp_path, monkeypatch):
    from werkzeug.security import generate_password_hash
    from gateway.auth_store import AuthStore
    from gateway.management_db import open_management_db

    monkeypatch.setattr(app_module, "load_or_create_session_key", lambda _path: "test")
    state_dir = tmp_path / "management"
    database_path = state_dir / "management.db"
    connection = open_management_db(database_path)
    try:
        users = AuthStore(connection)
        password = generate_password_hash("valid password 123", method="scrypt")
        users.create_user("admin", password, "administrator", must_change_password=False)
        users.create_user("operator", password, "operator", must_change_password=False)
    finally:
        connection.close()
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": False,
        "MANAGEMENT_STATE_DIR": state_dir,
        "MANAGEMENT_DB_PATH": database_path,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
    })

    def login(username):
        client = app.test_client()
        client.get("/login")
        with client.session_transaction() as values:
            csrf = values["csrf_token"]
        response = client.post("/api/auth/login", json={"username": username, "password": "valid password 123"}, headers={"X-CSRF-Token": csrf})
        return client, response.get_json()["csrf_token"]

    admin, admin_csrf = login("admin")
    operator, operator_csrf = login("operator")
    payload = {
        "expected_revision": 1, "entry_element_id": "entry",
        "input_element_id": "input", "submit_element_id": "submit",
        "account_element_id": "account",
    }

    operator = operator.put("/api/browser-v2/comment-settings", json=payload, headers={"X-CSRF-Token": operator_csrf})
    missing_csrf = admin.put("/api/browser-v2/comment-settings", json=payload)
    assert admin.put("/api/browser-v2/comment-settings", json=payload, headers={"X-CSRF-Token": admin_csrf}).status_code != 403

    assert operator.status_code == 403
    assert missing_csrf.status_code == 403


def test_campaign_evidence_route_rejects_non_uuid_paths(tmp_path):
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": tmp_path / "evidence",
    })
    client = app.test_client()

    response = client.get(
        "/comment-campaign-evidence/../secret.txt",
        base_url="http://127.0.0.1:5000",
    )

    assert response.status_code == 404


def test_campaign_evidence_rejects_symlink_and_serves_only_no_store_png(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    name = "0123456789abcdef0123456789abcdef.png"
    (evidence_dir / name).write_bytes(b"png")
    app = create_app({
        "TESTING": True, "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "COMMENT_CAMPAIGN_EVIDENCE_DIR": evidence_dir,
    })
    client = app.test_client()

    served = client.get(f"/comment-campaign-evidence/{name}", base_url="http://127.0.0.1:5000")

    assert served.status_code == 200
    assert "no-store" in served.headers["Cache-Control"]
    link = evidence_dir / "fedcba9876543210fedcba9876543210.png"
    try:
        link.symlink_to(evidence_dir / name)
    except OSError:
        pytest.skip("symlinks are unavailable in this test environment")
    assert client.get(f"/comment-campaign-evidence/{link.name}", base_url="http://127.0.0.1:5000").status_code == 404


def test_campaign_settings_are_a_strict_read_only_comment_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "load_settings", lambda: {
        "adspower": {},
        "comment_campaign": {"element_bindings": {
            "entry_element_id": "entry-1", "input_element_id": "input-1",
            "submit_element_id": "submit-1", "account_element_id": "account-1",
            "legacy_selector": "must-not-leak",
        }},
    })
    app = create_app({
        "TESTING": True,
        "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
    })

    response = app.test_client().get(
        "/api/browser-v2/comment-settings", base_url="http://127.0.0.1:5000"
    )

    assert response.status_code == 200
    assert response.get_json() == {"data": {
        "element_bindings": {
            "entry_element_id": "entry-1", "input_element_id": "input-1",
            "submit_element_id": "submit-1", "account_element_id": "account-1",
        },
        "configured": True,
        "revision": 1,
        "can_write": True,
    }}


def test_campaign_settings_put_is_active_kind_checked_and_revision_guarded(tmp_path, monkeypatch):
    from execution_v2.store import ExecutionStore

    definition = {
        "url_pattern": "https://www.tiktok.com/*", "frame_path": [],
        "locators": [{"type": "css", "value": ".comment", "priority": 1}],
        "diagnostic_metadata": {}, "screenshot_path": "",
    }
    elements = ExecutionStore(tmp_path / "execution.db")
    elements.initialize()
    for identifier, kind in (("entry", "click"), ("input", "input"), ("submit", "click"), ("account", "click")):
        elements.create_element(identifier, identifier, "action", kind, definition)
    persisted = {"adspower": {}, "comment_campaign": {"revision": 3, "element_bindings": {
        "entry_element_id": "old-entry", "input_element_id": "old-input",
        "submit_element_id": "old-submit", "account_element_id": "old-account",
    }}, "unrelated": {"preserved": True}}
    monkeypatch.setattr(app_module, "load_settings", lambda: persisted)
    monkeypatch.setattr(app_module, "mutate_settings", lambda mutator: mutator(persisted) or persisted)
    app = create_app({
        "TESTING": True, "LOCAL_DIRECT_MODE": True,
        "COMMENT_CAMPAIGN_DB_URL": f"sqlite:///{tmp_path / 'campaign.db'}",
        "EXECUTION_V2_DB_PATH": tmp_path / "execution.db",
    })
    client = app.test_client()
    body = {"expected_revision": 3, "entry_element_id": "entry", "input_element_id": "input", "submit_element_id": "submit", "account_element_id": "account"}

    saved = client.put("/api/browser-v2/comment-settings", json=body, base_url="http://127.0.0.1:5000")
    stale = client.put("/api/browser-v2/comment-settings", json=body, base_url="http://127.0.0.1:5000")
    extra = client.put("/api/browser-v2/comment-settings", json={**body, "expected_revision": 4, "extra": "blocked"}, base_url="http://127.0.0.1:5000")
    blank = client.put("/api/browser-v2/comment-settings", json={**body, "expected_revision": 4, "input_element_id": ""}, base_url="http://127.0.0.1:5000")

    assert saved.status_code == 200
    assert saved.get_json()["data"]["revision"] == 4
    assert stale.status_code == 409
    assert extra.status_code == blank.status_code == 422
    assert persisted["comment_campaign"] == {"revision": 4, "element_bindings": {
        "entry_element_id": "entry", "input_element_id": "input",
        "submit_element_id": "submit", "account_element_id": "account",
    }}
    assert persisted["unrelated"] == {"preserved": True}
