from __future__ import annotations

from contextlib import closing
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway.app import create_app
from init_db import init_db
from tiktok_stats.queries import StatisticsQueryService
from tiktok_stats.store import StatsStore


TEST_SECRET = "session" + "id=" + "route-secret-value"
HISTORIC_SECRET = "historic-" + "sensitive-value"


class FakeProtector:
    def protect(self, value: bytes) -> bytes:
        return b"cipher:" + value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        assert value.startswith(b"cipher:")
        return value[len(b"cipher:") :][::-1]


class TrackingQueryService(StatisticsQueryService):
    opened = 0
    closed = 0

    def __init__(self, path):
        type(self).opened += 1
        super().__init__(path)

    def close(self):
        if self._owns_connection:
            type(self).closed += 1
        super().close()


class TrackingStore(StatsStore):
    opened = 0
    closed = 0

    def __init__(self, path):
        type(self).opened += 1
        super().__init__(path)

    def close(self):
        type(self).closed += 1
        super().close()


@pytest.fixture
def route_app(tmp_path: Path):
    stats_path = tmp_path / "stats.db"
    cookie_path = tmp_path / "cookie.json"
    accounts_path = tmp_path / "accounts.db"
    init_db(accounts_path)
    with closing(sqlite3.connect(accounts_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO accounts (
                ads_power_user_id, buffer_account_id, proxy_session, account_name,
                buffer_token, buffer_profile_ids, buffer_channels, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                "legacy-1", "legacy-1", "", "Legacy Account", "",
                "[]",
                json.dumps([
                    {
                        "id": "channel-1", "service": "tiktok",
                        "descriptor": "@ExistingCreator", "displayName": "TikTok",
                    }
                ]),
            ),
        )

    with StatsStore(stats_path) as store:
        alpha = store.add_tracked_account("Alpha", "alpha")
        store.upsert_daily_metric(
            alpha["id"], "2026-07-21", baseline_status="ready",
            posts_total=12, likes_total=120, views_total=1200, comments_total=24,
            posts_delta=2, likes_delta=20, views_delta=200, comments_delta=4,
        )
        run_id = store.start_run("incremental", scheduled_for="2026-07-21T03:00:00Z")
        store.finish_run(
            run_id, "partial",
            details_json=json.dumps(
                {"Cookie": HISTORIC_SECRET, "error": f"token={HISTORIC_SECRET}"}
            ),
        )

    dispatch_calls = []
    validation_calls = []
    status_calls = []
    TrackingQueryService.opened = TrackingQueryService.closed = 0
    TrackingStore.opened = TrackingStore.closed = 0

    def dispatch(run_type, account_ids=None):
        dispatch_calls.append((run_type, account_ids))
        return {"run_id": "manual-1", "state": "enqueued", "run_type": run_type}

    def validate_cookie(value):
        validation_calls.append(value)
        return {"valid": True, "message": f"validated Cookie={value}"}

    def status_provider():
        status_calls.append(True)
        return {"scraper": {"running": True}, "worker": {"running": False}}

    app = create_app(
        {
            "TESTING": True,
            "TIKTOK_STATS_DB_PATH": stats_path,
            "TIKTOK_STATS_COOKIE_PATH": cookie_path,
            "TIKTOK_STATS_EXISTING_ACCOUNTS_DB_PATH": accounts_path,
            "TIKTOK_STATS_QUERY_FACTORY": TrackingQueryService,
            "TIKTOK_STATS_STORE_FACTORY": TrackingStore,
            "TIKTOK_STATS_SECRET_STORE_FACTORY": lambda path: __import__(
                "tiktok_stats.secrets", fromlist=["CookieSecretStore"]
            ).CookieSecretStore(path, protector=FakeProtector()),
            "TIKTOK_STATS_COOKIE_VALIDATOR": validate_cookie,
            "TIKTOK_STATS_RUN_DISPATCHER": dispatch,
            "TIKTOK_STATS_STATUS_PROVIDER": status_provider,
        }
    )
    app.extensions["route_test"] = {
        "stats_path": stats_path,
        "cookie_path": cookie_path,
        "dispatch_calls": dispatch_calls,
        "validation_calls": validation_calls,
        "status_calls": status_calls,
    }
    return app


@pytest.fixture
def client(route_app):
    return route_app.test_client()


def test_page_and_get_accounts_are_read_only_and_close_query_services(route_app, client):
    state = route_app.extensions["route_test"]
    with closing(sqlite3.connect(state["stats_path"])) as connection:
        before = tuple(connection.execute(
            "SELECT (SELECT COUNT(*) FROM tracked_accounts), (SELECT COUNT(*) FROM collection_runs)"
        ).fetchone())

    page = client.get("/tiktok-stats")
    accounts = client.get("/api/tiktok-stats/accounts")

    assert page.status_code == 200
    page_text = page.get_data(as_text=True)
    assert "TikTok" in page_text
    assert 'id="tiktok-stats-app"' in page_text
    assert "/static/tiktok_stats.js" in page_text
    assert "/static/tiktok_stats.css" in page_text
    assert "/static/dashboard_shell.css" in page_text
    assert 'class="dashboard-shell"' in page_text
    assert 'class="dashboard-sidebar"' in page_text
    assert 'href="/tiktok-stats"' in page_text
    stats_href = page_text.index('href="/tiktok-stats"')
    stats_link = page_text[page_text.rfind("<a", 0, stats_href):page_text.index(">", stats_href)]
    assert 'aria-current="page"' in stats_link
    assert 'class="site-header"' not in page_text
    expected_hrefs = [
        '/?panel=settings',
        '/?panel=accounts',
        '/?panel=proxy-config',
        '/?panel=content',
        '/?panel=publish',
        '/?panel=publish-results',
        '/tiktok-stats',
        '/?panel=browser',
        '/?panel=strategies',
    ]
    positions = [page_text.index(f'href="{href}"') for href in expected_hrefs]
    assert positions == sorted(positions)
    assert accounts.status_code == 200
    payload = accounts.get_json()
    assert payload["accounts"][0]["username"] == "Alpha"
    assert payload["existing_candidates"][0]["username"] == "ExistingCreator"
    assert payload["existing_candidates"][0]["candidate_id"]
    assert state["dispatch_calls"] == []
    assert TrackingStore.opened == 0
    assert TrackingQueryService.opened == TrackingQueryService.closed == 1
    with closing(sqlite3.connect(state["stats_path"])) as connection:
        after = tuple(connection.execute(
            "SELECT (SELECT COUNT(*) FROM tracked_accounts), (SELECT COUNT(*) FROM collection_runs)"
        ).fetchone())
    assert after == before


def test_text_import_reports_added_existing_reactivated_and_invalid(client):
    disabled = client.post("/api/tiktok-stats/accounts", json={"text": "DisabledOne"}).get_json()
    disabled_id = disabled["items"][0]["account"]["id"]
    assert client.patch(
        f"/api/tiktok-stats/accounts/{disabled_id}", json={"enabled": False}
    ).status_code == 200

    response = client.post(
        "/api/tiktok-stats/accounts",
        json={"text": "@NewCreator\nAlpha\nDisabledOne\nnot valid!"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["summary"] == {"added": 1, "existing": 1, "reactivated": 1, "invalid": 1}
    assert [item["status"] for item in payload["items"]] == [
        "added", "existing", "reactivated", "invalid"
    ]


def test_existing_import_only_accepts_projected_candidate_ids(client):
    candidate = client.get("/api/tiktok-stats/accounts").get_json()["existing_candidates"][0]
    ok = client.post(
        "/api/tiktok-stats/accounts/from-existing",
        json={"candidate_ids": [candidate["candidate_id"]]},
    )
    rejected = client.post(
        "/api/tiktok-stats/accounts/from-existing",
        json={"candidate_ids": ["invented-candidate"]},
    )

    assert ok.status_code == 200
    assert ok.get_json()["items"][0]["account"]["username"] == "ExistingCreator"
    assert rejected.status_code == 400
    assert rejected.get_json()["error"]["code"] == "invalid_candidate_id"


def test_patch_supports_username_and_explicit_enable_disable(client):
    account_id = client.get("/api/tiktok-stats/accounts").get_json()["accounts"][0]["id"]

    response = client.patch(
        f"/api/tiktok-stats/accounts/{account_id}",
        json={"username": "@Renamed.Creator", "enabled": False},
    )

    assert response.status_code == 200
    assert response.get_json()["account"]["username"] == "Renamed.Creator"
    assert response.get_json()["account"]["status"] == "disabled"
    assert client.patch(
        f"/api/tiktok-stats/accounts/{account_id}", json={"enabled": "false"}
    ).status_code == 400
    assert client.patch("/api/tiktok-stats/accounts/9999", json={"enabled": True}).status_code == 404


def test_patch_rejects_invalid_or_conflicting_username_with_stable_json(client):
    first = client.get("/api/tiktok-stats/accounts").get_json()["accounts"][0]["id"]
    second = client.post("/api/tiktok-stats/accounts", json={"text": "Second"}).get_json()["items"][0]["account"]["id"]

    invalid = client.patch(f"/api/tiktok-stats/accounts/{first}", json={"username": "not valid!"})
    conflict = client.patch(f"/api/tiktok-stats/accounts/{second}", json={"username": "alpha"})

    assert invalid.status_code == 400
    assert invalid.get_json()["error"]["code"] == "invalid_username"
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "username_conflict"


@pytest.mark.parametrize(
    ("body", "content_type", "code"),
    [
        ("[]", "application/json", "json_object_required"),
        ('"text"', "application/json", "json_object_required"),
        ("{", "application/json", "invalid_json"),
        ("plain", "text/plain", "invalid_json"),
    ],
)
def test_json_body_must_be_a_valid_object(client, body, content_type, code):
    response = client.post(
        "/api/tiktok-stats/accounts", data=body, content_type=content_type
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code


def test_unknown_body_fields_and_enum_values_are_stable_errors(client):
    unknown = client.post("/api/tiktok-stats/accounts", json={"text": "one", "extra": True})
    bad_run = client.post("/api/tiktok-stats/runs", json={"run_type": "cleanup"})
    assert unknown.status_code == 400
    assert unknown.get_json()["error"]["code"] == "unknown_field"
    assert bad_run.status_code == 400
    assert bad_run.get_json()["error"]["code"] == "invalid_run_type"


@pytest.mark.parametrize("body", [{"text": "  \n"}, {"usernames": []}])
def test_account_import_rejects_an_empty_submission(client, body):
    response = client.post("/api/tiktok-stats/accounts", json=body)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_import"


def test_cookie_save_get_and_validate_never_expose_plaintext(route_app, client, caplog):
    state = route_app.extensions["route_test"]
    saved = client.put("/api/tiktok-stats/settings/cookie", json={"cookie": TEST_SECRET})
    public = client.get("/api/tiktok-stats/settings/cookie")
    validated = client.post("/api/tiktok-stats/settings/cookie/validate", json={})

    assert saved.status_code == public.status_code == validated.status_code == 200
    assert saved.get_json()["status"]["configured"] is True
    assert public.get_json()["status"]["state"] == "configured"
    assert validated.get_json()["status"]["state"] == "valid"
    assert state["validation_calls"] == [TEST_SECRET]
    bodies = saved.data + public.data + validated.data
    assert TEST_SECRET.encode() not in bodies
    assert TEST_SECRET not in state["cookie_path"].read_text(encoding="utf-8")
    assert TEST_SECRET not in caplog.text
    assert TEST_SECRET.encode() not in state["stats_path"].read_bytes()


def test_cookie_put_rejects_empty_and_get_never_invokes_validation(client):
    assert client.put("/api/tiktok-stats/settings/cookie", json={"cookie": ""}).status_code == 400
    assert client.get("/api/tiktok-stats/settings/cookie").status_code == 200


def test_cookie_validation_does_not_mark_a_concurrently_replaced_cookie_valid(route_app):
    entered_validator = threading.Event()
    release_validator = threading.Event()
    old_cookie = TEST_SECRET + "-old"
    new_cookie = TEST_SECRET + "-new"

    def blocking_validator(value):
        assert value == old_cookie
        entered_validator.set()
        assert release_validator.wait(5)
        return {"valid": True}

    route_app.config["TIKTOK_STATS_COOKIE_VALIDATOR"] = blocking_validator
    assert route_app.test_client().put(
        "/api/tiktok-stats/settings/cookie", json={"cookie": old_cookie}
    ).status_code == 200
    result = {}

    def validate_in_request():
        with route_app.test_client() as thread_client:
            response = thread_client.post(
                "/api/tiktok-stats/settings/cookie/validate", json={}
            )
            result["status_code"] = response.status_code
            result["payload"] = response.get_json()

    thread = threading.Thread(target=validate_in_request)
    thread.start()
    assert entered_validator.wait(5)
    assert route_app.test_client().put(
        "/api/tiktok-stats/settings/cookie", json={"cookie": new_cookie}
    ).status_code == 200
    release_validator.set()
    thread.join(5)

    assert not thread.is_alive()
    assert result["status_code"] == 200
    assert result["payload"]["status"]["state"] == "configured"
    current = route_app.test_client().get(
        "/api/tiktok-stats/settings/cookie"
    ).get_json()["status"]
    assert current["state"] == "configured"


def test_manual_run_dispatches_only_explicit_posts(route_app, client):
    state = route_app.extensions["route_test"]
    get_before = client.get("/api/tiktok-stats/runs")
    status = client.get("/api/tiktok-stats/status")
    submitted = client.post(
        "/api/tiktok-stats/runs", json={"run_type": "full", "account_ids": [1]}
    )
    get_after = client.get("/api/tiktok-stats/runs")

    assert get_before.status_code == status.status_code == get_after.status_code == 200
    assert submitted.status_code == 202
    assert submitted.get_json()["run"] == {
        "run_id": "manual-1", "state": "enqueued", "run_type": "full"
    }
    assert state["dispatch_calls"] == [("full", [1])]
    assert status.get_json()["scraper"]["running"] is True
    assert status.get_json()["worker"]["running"] is False
    assert "configured" in status.get_json()


def test_manual_run_rejects_unknown_account_without_dispatch(route_app, client):
    state = route_app.extensions["route_test"]
    response = client.post(
        "/api/tiktok-stats/runs", json={"run_type": "incremental", "account_ids": [9999]}
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_account_ids"
    assert state["dispatch_calls"] == []


def test_run_list_redacts_sensitive_details(client):
    response = client.get("/api/tiktok-stats/runs")
    assert response.status_code == 200
    assert HISTORIC_SECRET.encode() not in response.data
    details = response.get_json()["runs"][0]["details"]
    assert details["Cookie"] == "[REDACTED]"
    assert "[REDACTED]" in details["error"]


def test_summary_table_detail_and_trends_delegate_to_read_query_contract(client):
    summary = client.get("/api/tiktok-stats/summary?date=2026-07-21")
    table = client.get(
        "/api/tiktok-stats/table?date=2026-07-21&sort=views_delta&direction=desc&page=1&page_size=20"
    )
    detail = client.get(
        "/api/tiktok-stats/accounts/1/detail?start_date=2026-07-21&end_date=2026-07-21"
    )
    trends = client.get(
        "/api/tiktok-stats/trends?metric=comments_delta&start_date=2026-07-21&end_date=2026-07-21&page=1&page_size=20"
    )

    assert summary.get_json()["posts_delta"] == 2
    assert table.get_json()["rows"][0]["views_delta"] == 200
    assert detail.get_json()["account"]["username"] == "Alpha"
    assert trends.get_json()["metric"] == "comments_delta"


@pytest.mark.parametrize(
    "url",
    [
        "/api/tiktok-stats/table?date=bad-date",
        "/api/tiktok-stats/table?date=2026-07-21&sort=DROP",
        "/api/tiktok-stats/table?date=2026-07-21&direction=sideways",
        "/api/tiktok-stats/table?date=2026-07-21&page=0",
        "/api/tiktok-stats/table?date=2026-07-21&page_size=501",
        "/api/tiktok-stats/trends?metric=token&start_date=2026-07-21&end_date=2026-07-21",
        "/api/tiktok-stats/accounts/0/detail?start_date=2026-07-21&end_date=2026-07-21",
        "/api/tiktok-stats/accounts/1/detail?start_date=2026-07-22&end_date=2026-07-21",
    ],
)
def test_invalid_query_values_return_stable_json_errors(client, url):
    response = client.get(url)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_query"


@pytest.mark.parametrize(
    "url",
    [
        "/api/tiktok-stats/accounts?unexpected=1",
        "/api/tiktok-stats/status?unexpected=1",
        "/api/tiktok-stats/runs?unexpected=1",
    ],
)
def test_unknown_query_parameters_never_become_server_errors(client, url):
    response = client.get(url)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_query"


@pytest.mark.parametrize(
    "url",
    [
        "/api/tiktok-stats/runs?page=0",
        "/api/tiktok-stats/runs?page_size=501",
        "/api/tiktok-stats/runs?page=not-a-number",
        "/api/tiktok-stats/table?date=2026-07-21&page=0",
        "/api/tiktok-stats/table?date=2026-07-21&page_size=501",
        "/api/tiktok-stats/trends?metric=posts_delta&start_date=2026-07-21&end_date=2026-07-21&page=0",
        "/api/tiktok-stats/trends?metric=posts_delta&start_date=2026-07-21&end_date=2026-07-21&page_size=501",
    ],
)
def test_all_paginated_get_routes_return_stable_errors_at_boundaries(client, url):
    response = client.get(url)
    assert response.status_code == 400
    assert response.is_json
    assert response.get_json()["error"]["code"] == "invalid_query"


def test_each_request_closes_request_scoped_store_and_query(route_app, client):
    client.get("/api/tiktok-stats/table?date=2026-07-21")
    client.post("/api/tiktok-stats/accounts", json={"text": "Lifecycle"})
    assert TrackingQueryService.opened == TrackingQueryService.closed
    assert TrackingStore.opened == TrackingStore.closed


def test_publish_statistics_compatibility_endpoint_still_exists(route_app, client):
    response = client.get("/api/publish/stats?date=2026-07-21")
    assert response.status_code == 200
    assert isinstance(response.get_json(), dict)
