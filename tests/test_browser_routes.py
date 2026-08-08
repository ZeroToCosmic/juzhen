import asyncio
import importlib
import json
import threading

import pytest

from browser_element_resolver import LocatorResolutionError
from gateway.app import create_app


def ready_session(profile, ws_url, attempts=1):
    return {
        "profile_id": profile["profile_id"],
        "profile_no": str(profile.get("profile_no") or ""),
        "name": str(profile.get("name") or ""),
        "status": "ready",
        "stage": "session_start",
        "attempts": attempts,
        "ws_url": ws_url,
        "error": "",
    }


def patch_session_dependencies(monkeypatch, app_module, fake_ensure):
    class FakeController:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr("gateway.browser_legacy.AdsPowerController", FakeController)
    monkeypatch.setattr(
        "gateway.browser_orchestrator.ensure_profile_session", fake_ensure
    )
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda hints: {
            "count": len(hints),
            "requested_count": len(hints),
            "matched_count": len(hints),
            "layout": [],
            "missing": [],
            "scale_results": [],
        },
    )


def run_session_request(app_module, profile_id, results, errors):
    try:
        results.append(
            app_module.ensure_browser_profile_sessions([{"profile_id": profile_id}])
        )
    except BaseException as error:  # Preserve worker failures for the main test thread.
        errors.append(error)


def join_threads(*threads):
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_browser_session_close_waits_for_other_users_and_preserves_replacement():
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    stopped = []
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://shared"

    assert app_module.acquire_browser_session_use("profile-1", "ws://shared") is True
    assert app_module.acquire_browser_session_use("profile-1", "ws://shared") is True

    app_module.release_browser_session_use(
        "profile-1",
        "ws://shared",
        request_close=True,
        stop_browser=stopped.append,
    )

    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-1": "ws://shared"}
    assert stopped == []

    app_module.release_browser_session_use(
        "profile-1",
        "ws://shared",
        stop_browser=stopped.append,
    )

    assert app_module.ACTIVE_BROWSER_SESSIONS == {}
    assert stopped == ["profile-1"]
    assert app_module.BROWSER_SESSION_LEASES == {}

    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://old"
    assert app_module.acquire_browser_session_use("profile-1", "ws://old") is True
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://replacement"

    app_module.release_browser_session_use(
        "profile-1",
        "ws://old",
        request_close=True,
        stop_browser=stopped.append,
    )

    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-1": "ws://replacement"}
    assert stopped == ["profile-1"]
    assert app_module.BROWSER_SESSION_LEASES == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_failed_final_stop_keeps_session_tracked(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://still-running"
    assert app_module.acquire_browser_session_use("profile-1", "ws://still-running")
    logged = []
    monkeypatch.setattr(
        "gateway.browser_legacy.record_browser_log",
        lambda operation, payload: logged.append((operation, payload)),
    )

    def fail_stop(_profile_id):
        raise RuntimeError("stop failed")

    app_module.release_browser_session_use(
        "profile-1",
        "ws://still-running",
        request_close=True,
        stop_browser=fail_stop,
    )

    assert app_module.ACTIVE_BROWSER_SESSIONS == {
        "profile-1": "ws://still-running"
    }
    assert app_module.BROWSER_SESSION_LEASES == {}
    assert logged[0][0] == "session_stop_failed"
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_ensure_browser_profile_sessions_acquires_requested_lease(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()

    def fake_ensure(profile, current_ws, *_args, **_kwargs):
        return ready_session(profile, current_ws or "ws://leased")

    patch_session_dependencies(monkeypatch, app_module, fake_ensure)

    results, _layout = app_module.ensure_browser_profile_sessions(
        [{"profile_id": "profile-1"}], lease_sessions=True
    )

    assert results[0]["status"] == "ready"
    assert app_module.BROWSER_SESSION_LEASES == {
        ("profile-1", "ws://leased"): {"users": 1, "close_requested": False}
    }

    app_module.release_browser_session_results(results)
    assert app_module.BROWSER_SESSION_LEASES == {}
    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-1": "ws://leased"}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_session_close_serializes_stop_before_same_profile_replacement(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://old"
    assert app_module.acquire_browser_session_use("profile-1", "ws://old")
    stop_entered = threading.Event()
    allow_stop = threading.Event()
    ensure_entered = threading.Event()
    stop_observed_active = []

    def fake_stop(_profile_id):
        stop_entered.set()
        assert allow_stop.wait(timeout=3)
        with app_module.ACTIVE_BROWSER_SESSIONS_LOCK:
            stop_observed_active.append(
                app_module.ACTIVE_BROWSER_SESSIONS.get("profile-1")
            )

    def fake_ensure(profile, _current_ws, *_args, **_kwargs):
        ensure_entered.set()
        return ready_session(profile, "ws://replacement")

    patch_session_dependencies(monkeypatch, app_module, fake_ensure)
    closing = threading.Thread(
        target=app_module.release_browser_session_use,
        args=("profile-1", "ws://old"),
        kwargs={"request_close": True, "stop_browser": fake_stop},
    )
    replacing = threading.Thread(
        target=app_module.ensure_browser_profile_sessions,
        args=([{"profile_id": "profile-1"}],),
    )

    closing.start()
    assert stop_entered.wait(timeout=3)
    replacing.start()
    assert not ensure_entered.wait(timeout=0.1)
    allow_stop.set()
    join_threads(closing, replacing)

    assert stop_observed_active == [None]
    assert app_module.ACTIVE_BROWSER_SESSIONS == {
        "profile-1": "ws://replacement"
    }
    assert app_module.BROWSER_SESSION_LEASES == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_in_use_unhealthy_session_is_not_stopped_or_replaced(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://in-use"
    assert app_module.acquire_browser_session_use("profile-1", "ws://in-use")
    calls = {"start": 0, "stop": 0}

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def get_browser_active(self, _profile_id):
            return {"status": "active"}

        def start_browser(self, _profile_id):
            calls["start"] += 1
            return "ws://replacement"

        def stop_browser(self, _profile_id):
            calls["stop"] += 1

    monkeypatch.setattr("gateway.browser_legacy.AdsPowerController", FakeController)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(app_module.time, "sleep", lambda *_args: None)

    results, _layout = app_module.ensure_browser_profile_sessions(
        [{"profile_id": "profile-1"}], lease_sessions=True
    )

    assert results[0]["status"] == "failed"
    assert results[0]["stage"] == "session_busy"
    assert calls == {"start": 0, "stop": 0}
    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-1": "ws://in-use"}
    assert app_module.BROWSER_SESSION_LEASES == {
        ("profile-1", "ws://in-use"): {"users": 1, "close_requested": False}
    }
    app_module.release_browser_session_use("profile-1", "ws://in-use")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_same_profile_concurrent_requests_do_not_start_browser_twice(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    count_lock = threading.Lock()
    start_count = 0

    def fake_ensure(profile, current_ws, *_args, **_kwargs):
        nonlocal start_count
        if current_ws:
            return ready_session(profile, current_ws, attempts=0)
        with count_lock:
            start_count += 1
            attempt = start_count
        if attempt == 1:
            first_started.set()
            assert release_first.wait(timeout=3)
        else:
            second_started.set()
        return ready_session(profile, f"ws://started-{attempt}")

    patch_session_dependencies(monkeypatch, app_module, fake_ensure)
    results = []
    errors = []
    request_a = threading.Thread(
        target=run_session_request,
        args=(app_module, "profile-1", results, errors),
    )
    request_b = threading.Thread(
        target=run_session_request,
        args=(app_module, "profile-1", results, errors),
    )

    request_a.start()
    assert first_started.wait(timeout=1)
    request_b.start()
    second_started.wait(timeout=0.5)
    release_first.set()
    join_threads(request_a, request_b)

    assert errors == []
    assert start_count == 1
    assert app_module.ACTIVE_BROWSER_SESSIONS == {
        "profile-1": "ws://started-1"
    }
    assert getattr(app_module, "BROWSER_PROFILE_SESSION_LOCKS", {}) == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_slower_older_same_profile_result_cannot_overwrite_newer_state(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    count_lock = threading.Lock()
    call_count = 0

    def fake_ensure(profile, _current_ws, *_args, **_kwargs):
        nonlocal call_count
        with count_lock:
            call_count += 1
            call_number = call_count
        if call_number == 1:
            first_entered.set()
            assert release_first.wait(timeout=3)
            return ready_session(profile, "ws://older")
        second_entered.set()
        return ready_session(profile, "ws://newer")

    patch_session_dependencies(monkeypatch, app_module, fake_ensure)
    results = []
    errors = []
    request_a = threading.Thread(
        target=run_session_request,
        args=(app_module, "profile-1", results, errors),
    )
    request_b = threading.Thread(
        target=run_session_request,
        args=(app_module, "profile-1", results, errors),
    )

    request_a.start()
    assert first_entered.wait(timeout=1)
    request_b.start()
    second_entered.wait(timeout=0.5)
    release_first.set()
    join_threads(request_a, request_b)

    assert errors == []
    assert call_count == 2
    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-1": "ws://newer"}
    assert getattr(app_module, "BROWSER_PROFILE_SESSION_LOCKS", {}) == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_different_profiles_can_ensure_sessions_concurrently(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    ensure_barrier = threading.Barrier(2)

    def fake_ensure(profile, _current_ws, *_args, **_kwargs):
        ensure_barrier.wait(timeout=2)
        return ready_session(profile, f"ws://{profile['profile_id']}")

    patch_session_dependencies(monkeypatch, app_module, fake_ensure)
    results = []
    errors = []
    profile_one = threading.Thread(
        target=run_session_request,
        args=(app_module, "profile-1", results, errors),
    )
    profile_two = threading.Thread(
        target=run_session_request,
        args=(app_module, "profile-2", results, errors),
    )

    profile_one.start()
    profile_two.start()
    join_threads(profile_one, profile_two)

    assert errors == []
    assert app_module.ACTIVE_BROWSER_SESSIONS == {
        "profile-1": "ws://profile-1",
        "profile-2": "ws://profile-2",
    }
    assert getattr(app_module, "BROWSER_PROFILE_SESSION_LOCKS", {}) == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_element_test_route_inspects_profiles_read_only_and_isolates_failures(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS.update(
        {"profile-1": "ws://profile-1", "profile-2": "ws://profile-2"}
    )
    monkeypatch.setattr("gateway.browser_legacy.BROWSER_LOG_PATH", tmp_path / "browser.jsonl")
    elements = {
        "评论入口": {
            "scope": "page",
            "locators": [
                {
                    "id": "comment-entry",
                    "type": "css",
                    "value": ".raw-selector-should-never-leak",
                    "enabled": True,
                }
            ],
        }
    }
    calls = []
    stopped = []

    class FakePage:
        def __init__(self, profile_id):
            self.profile_id = profile_id
            self.url = "https://www.tiktok.com/@example/video/1"

        def is_closed(self):
            return False

        async def evaluate(self, expression):
            assert expression == "document.visibilityState"
            return "visible"

        async def click(self, *_args, **_kwargs):
            raise AssertionError("inspection must not click")

    class FakeChromium:
        async def connect_over_cdp(self, ws_url, **_kwargs):
            profile_id = ws_url.rsplit("/", 1)[-1]
            page = FakePage(profile_id)
            return type(
                "FakeBrowser",
                (), {"contexts": [type("FakeContext", (), {"pages": [page]})()]},
            )()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        async def start(self):
            return self

        async def stop(self):
            stopped.append(True)

    async def fake_inspect(page, alias, definition):
        calls.append((page.profile_id, alias))
        if page.profile_id == "profile-2":
            raise LocatorResolutionError(
                "element_candidate_not_found",
                alias,
                definition["scope"],
                {
                    "selector": ".raw-selector-should-never-leak",
                    "html": "<section>private page text</section>",
                    "cookie": "session-cookie",
                    "cdp_url": "ws://secret-cdp-endpoint",
                },
            )
        return {
            "status": "ok",
            "alias": alias,
            "scope": definition["scope"],
            "candidate": {"id": "comment-entry", "type": "css"},
            "diagnostics": {
                "candidates": [
                    {
                        "id": "comment-entry",
                        "type": "css",
                        "raw_count": 1,
                        "visible_count": 1,
                        "actionable_count": 1,
                        "selector": ".raw-selector-should-never-leak",
                    }
                ]
            },
        }

    monkeypatch.setattr("gateway.browser_legacy.get_async_playwright", lambda: FakePlaywright, raising=False)
    monkeypatch.setattr("gateway.browser_legacy.inspect_element", fake_inspect, raising=False)

    response = create_app().test_client().post(
        "/api/browser/elements/test",
        json={"windows": ["profile-1", "profile-2"], "elements": elements},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert [item["profile_id"] for item in payload["results"]] == [
        "***le-1",
        "***le-2",
    ]
    assert calls == [("profile-1", "评论入口"), ("profile-2", "评论入口")]
    assert payload["results"][0]["elements"][0]["status"] == "ok"
    assert payload["results"][1]["elements"][0]["status"] == "error"
    assert payload["results"][0]["elements"][0]["diagnostics"] == {
        "candidates": [
            {
                "id": "comment-entry",
                "type": "css",
                "raw_count": 1,
                "visible_count": 1,
                "actionable_count": 1,
            }
        ]
    }
    public_text = response.get_data(as_text=True) + (tmp_path / "browser.jsonl").read_text(
        encoding="utf-8"
    )
    for sensitive_value in (
        ".raw-selector-should-never-leak",
        "<section>private page text</section>",
        "session-cookie",
        "ws://secret-cdp-endpoint",
    ):
        assert sensitive_value not in public_text
    assert stopped == [True, True]
    assert app_module.BROWSER_SESSION_LEASES == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_tiktok_template_route_returns_a_copy():
    client = create_app().test_client()

    first = client.get("/api/browser/elements/templates/tiktok-comment").get_json()
    first["elements"]["评论入口"]["scope"] = "page"
    second = client.get("/api/browser/elements/templates/tiktok-comment").get_json()

    assert second["elements"]["评论入口"]["scope"] == "active_video"


def test_element_test_route_reports_each_requested_alias_when_inspection_fails(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    monkeypatch.setattr("gateway.browser_legacy.inspect_browser_elements_on_cdp", lambda *_args: [])
    elements = {
        "评论入口": {
            "scope": "page",
            "locators": [
                {
                    "id": "comment-entry",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-icon",
                    "enabled": True,
                }
            ],
        }
    }

    response = create_app().test_client().post(
        "/api/browser/elements/test",
        json={"windows": ["profile-1"], "elements": elements},
    )

    assert response.status_code == 200
    assert response.get_json()["results"][0]["elements"] == [
        {
            "status": "error",
            "code": "element_inspection_failed",
            "alias": "评论入口",
            "scope": "page",
            "diagnostics": {},
        }
    ]
    assert app_module.BROWSER_SESSION_LEASES == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


@pytest.mark.parametrize("failure_stage", ["connect", "select_page"])
def test_element_test_route_returns_safe_errors_for_each_alias_after_profile_failure(
    monkeypatch, tmp_path, failure_stage
):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS.update(
        {"profile-ok": "ws://profile-ok", "profile-fail": "ws://profile-fail"}
    )
    monkeypatch.setattr("gateway.browser_legacy.BROWSER_LOG_PATH", tmp_path / "browser.jsonl")
    elements = {
        "评论入口": {
            "scope": "page",
            "locators": [
                {
                    "id": "comment-entry",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-icon",
                    "enabled": True,
                }
            ],
        },
        "提交评论": {
            "scope": "page",
            "locators": [
                {
                    "id": "comment-submit",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-post",
                    "enabled": True,
                }
            ],
        },
    }
    inspected_aliases = []
    stopped = []

    class OpenPage:
        url = "https://www.tiktok.com/@example/video/1"

        def is_closed(self):
            return False

        async def evaluate(self, expression):
            assert expression == "document.visibilityState"
            return "visible"

    class ClosedPage:
        def is_closed(self):
            return True

    class FakeChromium:
        async def connect_over_cdp(self, ws_url, **_kwargs):
            if ws_url.endswith("profile-fail") and failure_stage == "connect":
                raise RuntimeError(
                    "selector=.private-selector html=<section>private page text</section> "
                    "cdp=ws://private-cdp-endpoint"
                )
            pages = [ClosedPage()] if ws_url.endswith("profile-fail") else [OpenPage()]
            return type(
                "FakeBrowser",
                (), {"contexts": [type("FakeContext", (), {"pages": pages})()]},
            )()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        async def start(self):
            return self

        async def stop(self):
            stopped.append(True)

    async def fake_inspect(_page, alias, definition):
        inspected_aliases.append(alias)
        return {
            "status": "ok",
            "alias": alias,
            "scope": definition["scope"],
            "candidate": {"id": definition["locators"][0]["id"], "type": "attribute"},
            "diagnostics": {"candidates": []},
        }

    monkeypatch.setattr("gateway.browser_legacy.get_async_playwright", lambda: FakePlaywright)
    monkeypatch.setattr("gateway.browser_legacy.inspect_element", fake_inspect)

    response = create_app().test_client().post(
        "/api/browser/elements/test",
        data=json.dumps(
            {"windows": ["profile-ok", "profile-fail"], "elements": elements},
            ensure_ascii=False,
        ),
        content_type="application/json",
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert inspected_aliases == ["评论入口", "提交评论"]
    assert [item["status"] for item in results[0]["elements"]] == ["ok", "ok"]
    assert results[1]["status"] == "failed"
    assert results[1]["elements"] == [
        {
            "status": "error",
            "code": "element_inspection_failed",
            "alias": alias,
            "scope": definition["scope"],
            "diagnostics": {},
        }
        for alias, definition in elements.items()
    ]
    public_text = response.get_data(as_text=True) + (tmp_path / "browser.jsonl").read_text(
        encoding="utf-8"
    )
    for sensitive_value in (
        ".private-selector",
        "<section>private page text</section>",
        "ws://private-cdp-endpoint",
    ):
        assert sensitive_value not in public_text
    assert stopped == [True, True]
    assert app_module.BROWSER_SESSION_LEASES == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_read_elements_route_migrates_legacy_xpath_before_inspection(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    seen = []

    def fake_inspect_on_cdp(_ws_url, elements):
        seen.append(elements)
        return [
            {
                "status": "ok",
                "alias": "旧元素",
                "scope": "page",
                "candidate": {"id": "legacy-id", "type": "xpath"},
                "diagnostics": {"candidates": []},
            }
        ]

    monkeypatch.setattr(
        "gateway.browser_legacy.inspect_browser_elements_on_cdp",
        fake_inspect_on_cdp,
        raising=False,
    )

    response = create_app().test_client().post(
        "/api/browser/read-elements",
        json={"windows": ["profile-1"], "elements": {"旧元素": "//button"}},
    )

    assert response.status_code == 200
    assert seen[0]["旧元素"]["locators"][0]["type"] == "xpath"
    assert app_module.BROWSER_SESSION_LEASES == {}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
