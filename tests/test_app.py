import asyncio
import importlib
import copy
import hashlib
import json
import re
import secrets
import threading

import pytest

from browser_element_resolver import LocatorResolutionError
from browser_public_identity import mask_profile_id
from browser_video_switch import VideoSwitchError
from gateway.settings_store import load_settings, save_settings

from gateway.app import (
    CONTROL_PAGE_HTML,
    DASHBOARD_PAGE_HTML,
    SETTINGS_PAGE_HTML,
    create_app,
)


@pytest.mark.parametrize(
    ("method", "path", "operator_status"),
    [
        ("GET", "/", 200),
        ("GET", "/api/browser/elements", 200),
        ("PUT", "/api/browser/elements", 403),
        ("GET", "/api/browser/strategies", 200),
        ("PUT", "/api/browser/strategies", 403),
        ("POST", "/api/browser/execute-strategy", 403),
        ("GET", "/api/settings", 200),
        ("PUT", "/api/settings", 403),
    ],
)
def test_operator_route_matrix(
    operator_client,
    method,
    path,
    operator_status,
):
    response = operator_client.open(
        path,
        method=method,
        json={} if method != "GET" else None,
    )

    assert response.status_code == operator_status


@pytest.mark.parametrize(
    "template",
    (DASHBOARD_PAGE_HTML, SETTINGS_PAGE_HTML, CONTROL_PAGE_HTML),
    ids=("dashboard", "settings", "control"),
)
def test_management_templates_install_same_origin_authenticated_fetch(
    template,
):
    assert 'meta name="csrf-token"' in template
    assert "management_fetch.js" in template


@pytest.mark.parametrize("path", ("/", "/settings"))
def test_management_pages_render_current_csrf_token(admin_client, path):
    response = admin_client.get(path)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert (
        f'<meta name="csrf-token" content="{admin_client.csrf_token}">'
        in html
    )


@pytest.fixture(autouse=True)
def fake_window_tiling(monkeypatch):
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda hints: {
            "count": len(hints),
            "requested_count": len(hints),
            "matched_count": len(hints),
            "layout": [],
            "missing": [],
        },
    )


def window_tiler_result(profile_ids, *, missing=None, scale_results=None):
    return {
        "count": len(profile_ids),
        "requested_count": len(profile_ids),
        "matched_count": len(profile_ids),
        "columns": len(profile_ids),
        "rows": 1,
        "work_area": {"left": 0, "top": 0, "width": 1920, "height": 1080},
        "layout": [],
        "missing": list(missing or []),
        "page_scale": 0.85,
        "scale_results": scale_results
        or [
            {"profile_id": profile_id, "scale": 0.85, "status": "scaled"}
            for profile_id in profile_ids
        ],
    }


def combined_strategy_result(target_url, *, closed_tabs=0, **extra):
    result = {
        "status": "ok",
        "current_url": target_url,
        "closed_tabs": closed_tabs,
        "stages": [
            {"stage": "wait_for_cdp", "status": "ok"},
            {
                "stage": "close_other_tabs",
                "status": "ok",
                "closed_tabs": closed_tabs,
            },
            {
                "stage": "navigate",
                "status": "ok",
                "target_url": target_url,
                "current_url": target_url,
            },
            {"stage": "execute_actions", "status": "ok"},
        ],
    }
    result.update(extra)
    return result


def execute_auto_strategy_with_layout(monkeypatch, tmp_path, layout):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "profile_no": "",
            "name": "",
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-ok", "profile-fail")
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda _profiles, **_kwargs: (sessions, layout),
    )
    runtime_calls = []
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda _ws_url, url: {"current_url": url, "closed_tabs": 0, "stages": []},
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda ws_url, target_url, *_args: runtime_calls.append(ws_url)
        or combined_strategy_result(target_url, verified_interactions=1),
    )
    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "auto:auto-demo",
            "windows": [
                {"profile_id": "profile-ok"},
                {"profile_id": "profile-fail"},
            ],
        },
    )
    return response, runtime_calls


@pytest.mark.parametrize(
    ("method", "route"),
    [
        ("GET", "/api/browser/action-config"),
        ("PUT", "/api/browser/action-config"),
        ("GET", "/api/browser/auto-strategies"),
        ("PUT", "/api/browser/auto-strategies"),
        ("POST", "/api/browser/auto-strategies/generate"),
    ],
)
def test_legacy_strategy_routes_are_gone_and_do_not_write(
    monkeypatch, tmp_path, method, route
):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {"keep": "//keep"},
                "interaction_patterns": [],
                "block_strategies": [],
                "action_strategies": [{"id": "legacy-manual"}],
                "auto_strategies": [{"id": "legacy-auto"}],
            }
        }
    )
    before = config_path.read_bytes()

    response = create_app().test_client().open(
        route,
        method=method,
        json={
            "elements": {"overwrite": "//overwrite"},
            "strategies": [{"id": "overwrite"}],
            "strategy_id": "legacy-auto",
        },
    )

    assert response.status_code == 410
    assert "/api/browser/elements" in response.get_json()["error"]
    assert "/api/browser/strategies" in response.get_json()["error"]
    assert config_path.read_bytes() == before


def test_element_put_persists_locator_definition_and_renames_strategy_reference(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {"评论入口": "//button"},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "comment-strategy",
                        "name": "Comment",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "comment-click",
                                "type": "click",
                                "params": {
                                    "element": "评论入口",
                                    "button": "left",
                                    "click_count": 1,
                                    "hold_seconds": [0.05, 0.15],
                                    "trajectory": {"source": "builtin", "id": "bezier"},
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    client = create_app().test_client()
    definition = {
        "scope": "active_video",
        "locators": [{
            "id": "comment-primary",
            "type": "attribute",
            "name": "data-e2e",
            "value": "comment-icon",
            "enabled": True,
        }],
    }

    response = client.put(
        "/api/browser/elements",
        json={"elements": {"新评论入口": definition}, "rename_from": "评论入口"},
    )

    assert response.status_code == 200
    assert response.get_json()["elements"]["新评论入口"] == definition
    assert client.get("/api/browser/elements").get_json()["elements"]["新评论入口"] == definition
    strategy = client.get("/api/browser/strategies").get_json()["strategies"][0]
    assert strategy["actions"][0]["params"]["element"] == "新评论入口"


def test_canonical_browser_api_persist_and_reload_preserves_locator_and_strategy_state(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()
    elements = {
        "commentEntry": {
            "scope": "visible_comment_panel",
            "locators": [
                {
                    "id": "entry-xpath",
                    "type": "xpath",
                    "value": "//button[@data-e2e='comment-icon']",
                    "enabled": True,
                    "fallback": True,
                },
                {
                    "id": "entry-role",
                    "type": "role",
                    "role": "button",
                    "name": "Comments",
                    "name_mode": "exact",
                    "enabled": True,
                },
            ],
        }
    }
    strategies = [
        {
            "id": "comment-flow",
            "name": "Comment flow",
            "run_mode": "once",
            "batch_size": 2,
            "actions": [
                {
                    "id": "open-comments",
                    "type": "click",
                    "params": {
                        "element": "commentEntry",
                        "button": "left",
                        "click_count": 1,
                        "hold_seconds": [0.05, 0.15],
                        "trajectory": {"source": "builtin", "id": "bezier"},
                    },
                },
                {
                    "id": "switch-videos",
                    "type": "scroll_down",
                    "params": {
                        "distance": 120,
                        "total_count": [30, 50],
                        "burst_count": [1, 1],
                        "interval_seconds": [0.1, 0.3],
                    },
                },
            ],
        }
    ]

    element_response = client.put(
        "/api/browser/elements", json={"elements": elements}
    )
    strategy_response = client.put(
        "/api/browser/strategies", json={"strategies": strategies}
    )

    assert element_response.status_code == 200
    assert element_response.get_json()["elements"] == elements
    assert strategy_response.status_code == 200
    assert (
        strategy_response.get_json()["strategies"][0]["actions"][1]["params"][
            "total_count"
        ]
        == [30, 50]
    )

    refreshed_client = create_app().test_client()
    reloaded_elements = refreshed_client.get("/api/browser/elements").get_json()[
        "elements"
    ]
    reloaded_strategy = refreshed_client.get("/api/browser/strategies").get_json()[
        "strategies"
    ][0]
    assert reloaded_elements == elements
    assert [locator["id"] for locator in reloaded_elements["commentEntry"]["locators"]] == [
        "entry-xpath",
        "entry-role",
    ]
    assert reloaded_elements["commentEntry"]["scope"] == "visible_comment_panel"
    assert reloaded_strategy["actions"][0]["params"]["element"] == "commentEntry"
    assert reloaded_strategy["actions"][1]["params"]["total_count"] == [30, 50]
    stored_browser = load_settings()["browser"]
    assert stored_browser["action_elements"] == elements
    assert (
        stored_browser["block_strategies"][0]["actions"][1]["params"]["total_count"]
        == [30, 50]
    )


def test_element_get_persists_legacy_xpath_even_when_marked_v3(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    legacy_xpath = "//article[@id='one-column-item-1']//button"
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 3,
                "action_elements": {"评论入口": legacy_xpath},
                "interaction_patterns": [],
                "block_strategies": [],
            }
        }
    )

    response = create_app().test_client().get("/api/browser/elements")

    assert response.status_code == 200
    assert response.get_json()["elements"]["评论入口"]["locators"][0]["value"] == legacy_xpath
    stored_browser = load_settings()["browser"]
    assert stored_browser["strategy_schema_version"] == 3
    assert stored_browser["action_elements"]["评论入口"]["locators"][0]["value"] == legacy_xpath


def test_element_get_persists_legacy_xpath_whitespace_losslessly(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    legacy_xpath = "  //article[@id='one-column-item-1']//button  "
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 3,
                "action_elements": {"评论入口": legacy_xpath},
                "interaction_patterns": [],
                "block_strategies": [],
            }
        }
    )

    response = create_app().test_client().get("/api/browser/elements")

    assert response.status_code == 200
    assert response.get_json()["elements"]["评论入口"]["locators"][0]["value"] == legacy_xpath
    stored_locator = load_settings()["browser"]["action_elements"]["评论入口"]["locators"][0]
    assert stored_locator["value"] == legacy_xpath


def test_control_page_exposes_block_strategy_editor_and_hides_legacy_debug_copy(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    response = create_app().test_client().get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="browser-strategy-list-view"' in html
    assert 'id="browser-strategy-editor-view"' in html
    assert 'id="browser-block-palette"' in html
    assert 'id="browser-auto-strategy-flow"' not in html
    assert 'data-auto-step="' not in html
    assert "策略 JSON 输入" not in html
    assert "模型生成要求" not in html
    assert "动作策略（JSON 数组）" not in html
    assert "网页元素 XPath（JSON，可自定义名称）" not in html
    assert "使用生成文案" not in html
    assert "生成随机预览" not in html


def test_control_page_exposes_scoped_ordered_element_dialog(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    html = create_app().test_client().get("/").get_data(as_text=True)

    for marker in (
        'id="browser-element-scope"',
        'id="browser-element-locators"',
        'id="browser-element-add-locator"',
        'id="browser-element-test"',
        'id="browser-element-template"',
        'id="browser-element-test-results"',
    ):
        assert marker in html
    assert "高级 XPath" in html


def test_browser_strategy_controller_static_asset_and_canonical_action_catalog(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    client = create_app().test_client()

    static_response = client.get("/static/browser_strategy_ui.js")
    static_text = static_response.get_data(as_text=True)
    static_response.close()
    catalog_response = client.get("/api/browser/action-catalog")
    page = client.get("/").get_data(as_text=True)

    assert static_response.status_code == 200
    assert "createBrowserStrategyUI" in static_text
    assert catalog_response.status_code == 200
    payload = catalog_response.get_json()
    assert set(payload["catalog"]) == {
        "move", "click", "scroll_up", "scroll_down", "keyboard_input", "pause"
    }
    assert payload["defaults"]["move"]["trajectory"] == {"source": "builtin", "id": "bezier"}
    assert payload["defaults"]["keyboard_input"]["typing"] == {
        "source": "builtin", "interval_ms": [50, 250]
    }
    assert page.count("window.BrowserStrategyUI.init()") == 1


def test_browser_settings_copy_clarifies_adspower_api_and_manual_cdp_usage(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))

    client = create_app().test_client()
    control_html = client.get("/").get_data(as_text=True)
    settings_html = client.get("/settings").get_data(as_text=True)

    for html in (control_html, settings_html):
        assert "http://local.adspower.net:50325" in html
        assert "AdsPower 模式下请留空" in html
        assert "每次执行策略前都会重新打开默认网址并清理旧 Tab" in html


def test_control_page_renders_backend_validation_and_window_stage_errors():
    html = create_app().test_client().get("/").get_data(as_text=True)

    assert "result.status === 400" in html
    assert "result.data.error" in html
    assert "item.profile_id" in html
    assert "item.stage" in html
    assert "item.error" in html


def test_browser_execute_strategy_can_reference_auto_strategy(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    called = []

    def fake_run(ws_url, target_url, strategy, elements, patterns, _resolver):
        called.append((ws_url, strategy["id"], elements, patterns))
        return combined_strategy_result(target_url, closed_tabs=1, verified_interactions=1)

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp", fake_run
    )
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda _ws_url, url, **_kwargs: {"url": url, "closed_tabs": 1, "current_url": url},
    )
    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    assert response.get_json()["results"][0]["status"] == "ok"
    assert called[0][0:2] == ("ws://profile-1", "auto:auto-demo")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_sync_tabs_response_sanitizes_all_nested_browser_data(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://internal-session"
    public_url = "https://name:password@example.com/path?token=url-token&view=grid"
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda _ws_url, _url: {
            "url": public_url,
            "current_url": public_url,
            "closed_tabs": 1,
            "ws_url": "ws://nested-session",
            "details": {
                "authorization": "Bearer bearer-secret",
                "api_key": "api-secret",
                "secret": "nested-secret",
            },
        },
    )

    response = create_app().test_client().post(
        "/api/browser/sync-tabs",
        json={"url": public_url, "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    serialized = json.dumps(payload)
    assert payload["url"].startswith("https://example.com/path?")
    assert "view=grid" in payload["url"]
    for secret in (
        "name",
        "password",
        "url-token",
        "ws://",
        "bearer-secret",
        "api-secret",
        "nested-secret",
    ):
        assert secret not in serialized
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_auto_strategy_starts_and_tiles_before_combined_execution(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    events = []

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            events.append(("start", profile_id))
            return f"ws://secret-{profile_id}"

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda hints: events.append(("tile", [item["profile_id"] for item in hints]))
        or {"layout": [], "missing": []},
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda ws_url, url, **_kwargs: events.append(("prepare", ws_url, url))
        or {"url": url, "closed_tabs": 1, "current_url": url},
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda ws_url, target_url, *_args: events.append(
            ("prepare_and_run", ws_url, target_url)
        )
        or combined_strategy_result(
            target_url, closed_tabs=1, verified_interactions=1
        ),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["task_id"]
    assert payload["results"][0] == {
        "profile_id": "***le-1",
        "status": "ok",
        "stage": "execute_actions",
        "attempts": 1,
        "target_url": "https://example.com/landing",
        "current_url": "https://example.com/landing",
        "closed_tabs": 1,
        "stages": [
            {"stage": "wait_for_cdp", "status": "ok"},
            {"stage": "close_other_tabs", "status": "ok", "closed_tabs": 1},
            {
                "stage": "navigate",
                "status": "ok",
                "target_url": "https://example.com/landing",
                "current_url": "https://example.com/landing",
            },
            {"stage": "execute_actions", "status": "ok"},
        ],
        "verified_interactions": 1,
    }
    assert events == [
        ("start", "profile-1"),
        ("tile", ["profile-1"]),
        (
            "prepare_and_run",
            "ws://secret-profile-1",
            "https://example.com/landing",
        ),
    ]
    assert "ws_url" not in json.dumps(payload)
    assert app_module.ACTIVE_BROWSER_SESSIONS == {
        "profile-1": "ws://secret-profile-1"
    }
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_auto_strategy_isolates_session_start_failure(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    starts = []
    runtime_calls = []

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            starts.append(profile_id)
            if profile_id == "profile-fail":
                raise RuntimeError("cannot start ws://secret-endpoint?token=secret")
            return f"ws://secret-{profile_id}"

        def get_browser_active(self, _profile_id):
            return {"status": "inactive"}

        def stop_browser(self, _profile_id):
            return {"status": "stopped"}

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows", lambda _hints: {"layout": [], "missing": []}
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda _ws_url, url, **_kwargs: {"url": url, "closed_tabs": 0, "current_url": url},
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda ws_url, target_url, *_args: runtime_calls.append(ws_url)
        or combined_strategy_result(target_url, verified_interactions=1),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "auto:auto-demo",
            "windows": [
                {"profile_id": "profile-ok"},
                {"profile_id": "profile-fail"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert [item["profile_id"] for item in payload["results"]] == [
        "***e-ok",
        "***fail",
    ]
    assert payload["results"][0]["status"] == "ok"
    assert payload["results"][1]["status"] == "failed"
    assert payload["results"][1]["stage"] == "start_browser"
    assert payload["results"][1]["attempts"] == 3
    assert payload["results"][1]["target_url"] == "https://www.tiktok.com/"
    assert runtime_calls == ["ws://secret-profile-ok"]
    assert starts.count("profile-fail") == 3
    serialized = json.dumps(payload)
    assert "ws://" not in serialized
    assert "token=secret" not in serialized
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_auto_strategy_isolates_runtime_failure(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "profile_no": "",
            "name": "",
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-ok", "profile-fail")
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda _profiles, **_kwargs: (sessions, {"layout": [], "missing": []}),
    )
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda _ws_url, url: {
            "current_url": url,
            "closed_tabs": 0,
            "stages": [],
        },
    )

    def fake_run(ws_url, target_url, *_args):
        if ws_url == "ws://profile-fail":
            raise RuntimeError("runtime failed")
        return combined_strategy_result(target_url, verified_interactions=1)

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp", fake_run
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "auto:auto-demo",
            "windows": [
                {"profile_id": "profile-ok"},
                {"profile_id": "profile-fail"},
            ],
        },
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [item["status"] for item in results] == ["ok", "failed"]
    assert results[1]["profile_id"] == "***fail"
    assert results[1]["stage"] == "execute_actions"
    assert results[1]["error"] == "runtime failed"


@pytest.mark.parametrize(
    ("endpoint", "strategy_id"),
    [
        ("/api/browser/execute-strategy", "auto:auto-demo"),
        ("/api/browser/execute-strategy", "manual:manual-demo"),
        ("/api/browser/open-tile", ""),
    ],
)
def test_browser_routes_release_session_lease_when_executor_setup_fails(
    monkeypatch, tmp_path, endpoint, strategy_id
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                    "action_elements": {
                        "entry": "//entry",
                        "input": "//textarea",
                    "submit": "//button",
                    "cta": "//button",
                },
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                            "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "name": "Manual",
                        "actions": [{"type": "click", "element": "cta"}],
                    }
                ],
            }
        }
    )
    session = {
        "profile_id": "profile-1",
        "profile_no": "",
        "name": "",
        "status": "ready",
        "stage": "session_check",
        "attempts": 0,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    release_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda _profiles, **_kwargs: (
            [session],
            window_tiler_result(["profile-1"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda results, *, request_close=False: release_calls.append(
            (results, request_close)
        ),
    )
    monkeypatch.setattr(
        "browser_actions.validate_action_config",
        lambda elements, strategies: (elements, strategies),
    )

    class ExplodingExecutor:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError("executor setup failed")

    monkeypatch.setattr(app_module, "ThreadPoolExecutor", ExplodingExecutor)
    payload = {"windows": [{"profile_id": "profile-1"}]}
    if strategy_id:
        payload["strategy_id"] = strategy_id

    response = create_app().test_client().post(endpoint, json=payload)

    assert response.status_code == 500
    assert release_calls == [([session], False)]


@pytest.mark.parametrize(
    ("endpoint", "dependency"),
    [
        ("/api/browser/sync-tabs", "browser_cdp.navigate_and_close_other_tabs"),
        ("/api/browser/read-elements", "element_inspection"),
    ],
)
def test_browser_read_routes_hold_lease_until_cdp_operation_finishes(
    monkeypatch, endpoint, dependency
):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://shared"
    assert app_module.acquire_browser_session_use("profile-1", "ws://shared")
    operation_started = threading.Event()
    finish_operation = threading.Event()
    stopped = []
    responses = []
    monkeypatch.setattr(
        app_module, "_stop_browser_profile", lambda profile_id: stopped.append(profile_id)
    )

    def blocking_operation(*_args, **_kwargs):
        operation_started.set()
        assert finish_operation.wait(timeout=3)
        return [] if endpoint.endswith("read-elements") else {"closed_tabs": 0}

    if dependency == "element_inspection":
        monkeypatch.setattr(app_module, "inspect_browser_elements_on_cdp", blocking_operation)
    else:
        monkeypatch.setattr(dependency, blocking_operation)
    payload = {"windows": [{"profile_id": "profile-1"}]}
    if endpoint.endswith("sync-tabs"):
        payload["url"] = "https://www.tiktok.com/"
    else:
        payload["elements"] = {"title": "//h1"}
    app = create_app()

    def call_route():
        responses.append(app.test_client().post(endpoint, json=payload))

    operation = threading.Thread(target=call_route)
    operation.start()
    assert operation_started.wait(timeout=3)

    app_module.release_browser_session_use(
        "profile-1", "ws://shared", request_close=True
    )

    assert stopped == []
    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-1": "ws://shared"}
    finish_operation.set()
    operation.join(timeout=3)
    assert not operation.is_alive()
    assert responses[0].status_code == 200
    assert stopped == ["profile-1"]
    assert app_module.ACTIVE_BROWSER_SESSIONS == {}
    assert app_module.BROWSER_SESSION_LEASES == {}


def test_browser_execute_manual_strategy_starts_profile_before_actions(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/manual",
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "name": "手动 Demo",
                        "actions": [{"type": "click", "element": "cta"}],
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    events = []

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            events.append(("start", profile_id))
            return f"ws://secret-{profile_id}"

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(
        "browser_actions.validate_action_config",
        lambda elements, strategies: (elements, strategies),
    )
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda hints: events.append(("tile", [item["profile_id"] for item in hints]))
        or {"layout": [], "missing": []},
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda ws_url, url: events.append(("prepare", ws_url, url))
        or {"url": url, "closed_tabs": 0, "current_url": url},
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda ws_url, target_url, strategy, *_args: events.append(
            (
                "prepare_and_run",
                ws_url,
                target_url,
                strategy["actions"][0]["params"]["element"],
            )
        )
        or combined_strategy_result(
            target_url,
            actions=[{"action_id": "manual:manual-demo:action:1"}],
        ),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": "profile-k1dxxcto"}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["task_id"]
    result = payload["results"][0]
    assert result["profile_id"] == "***xcto"
    assert result["status"] == "ok"
    assert result["stage"] == "execute_actions"
    assert result["attempts"] == 1
    assert result["target_url"] == "https://example.com/manual"
    assert events == [
        ("start", "profile-k1dxxcto"),
        ("tile", ["profile-k1dxxcto"]),
        (
            "prepare_and_run",
            "ws://secret-profile-k1dxxcto",
            "https://example.com/manual",
            "cta",
        ),
    ]
    assert "ws_url" not in json.dumps(payload)
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_strategy_rejects_non_object_request():
    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json=[],
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "请求格式无效，必须是 JSON 对象"


def test_browser_execute_strategy_rejects_more_than_eight_windows(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {"id": "manual-demo", "actions": [{"type": "click", "element": "cta"}]}
                ],
            }
        }
    )
    monkeypatch.setattr(
        "browser_actions.validate_action_config",
        lambda elements, strategies: (elements, strategies),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": f"profile-{index}"} for index in range(9)],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "请选择 1 到 8 个浏览器窗口"


def test_browser_execute_strategy_rejects_invalid_auto_strategy_before_start(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "missing-submit",
                    }
                ],
            }
        }
    )
    orchestrator_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda profiles, **_kwargs: orchestrator_calls.append(profiles) or ([], None),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "auto:auto-demo",
            "windows": [{"profile_id": "profile-1"}],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "strategy needs repair before execution: "
        "click references missing element: missing-submit"
    )
    assert orchestrator_calls == []


def test_browser_execute_strategy_rejects_invalid_manual_strategy_before_start(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "actions": [{"type": "click", "element": "missing-cta"}],
                    }
                ],
            }
        }
    )
    orchestrator_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda profiles, **_kwargs: orchestrator_calls.append(profiles) or ([], None),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": "profile-1"}],
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == (
        "strategy needs repair before execution: "
        "click references missing element: missing-cta"
    )
    assert orchestrator_calls == []


def test_blank_browser_default_url_falls_back_to_tiktok(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings({"browser": {"default_url": "   "}})

    assert app_module.get_browser_target_url({}) == "https://www.tiktok.com/"


def test_public_browser_payload_redacts_credentials_inside_public_urls():
    app_module = importlib.import_module("gateway.app")

    public = app_module.public_browser_payload(
        {
            "target_url": "https://user:password@example.com/path?token=secret-token&view=grid",
            "current_url": "https://example.com/page?api_key=secret-key",
        }
    )

    serialized = json.dumps(public)
    assert "password" not in serialized
    assert "secret-token" not in serialized
    assert "secret-key" not in serialized
    assert "view=grid" in public["target_url"]


def test_public_browser_payload_redacts_hostile_headers_paths_and_session_query():
    app_module = importlib.import_module("gateway.app")

    public = app_module.public_browser_payload(
        {
            "error": (
                "browser disconnected; Cookie: first=COOKIE-ONE, second=COOKIE-TWO\n"
                "Authorization: Digest username=AUTH-USER, response=AUTH-RESPONSE"
            ),
            "current_url": (
                "https://example.com/safe/token/PATH-TOKEN/"
                "session=PATH-SESSION/item?session=QUERY-SESSION&view=grid"
            ),
        }
    )

    serialized = json.dumps(public)
    for secret in (
        "COOKIE-ONE",
        "COOKIE-TWO",
        "AUTH-USER",
        "AUTH-RESPONSE",
        "PATH-TOKEN",
        "PATH-SESSION",
        "QUERY-SESSION",
    ):
        assert secret not in serialized
    assert public["current_url"].startswith("https://example.com/safe/")
    assert "view=grid" in public["current_url"]


def test_public_browser_payload_uses_normalized_sensitive_keys_for_urls_text_and_scalars():
    app_module = importlib.import_module("gateway.app")

    public = app_module.public_browser_payload(
        {
            "error": "browser disconnected; session=ZXCV123",
            "current_url": (
                "https://example.com/safe/access-token/QWER456/item?view=grid"
                "#session_id=ASDF789"
            ),
            "nested": {"sessions": "BNMV456"},
            "sessions": [{"profile_id": "profile-k1dxxcto", "status": "ok"}],
        }
    )

    serialized = json.dumps(public)
    for secret in ("ZXCV123", "QWER456", "ASDF789", "BNMV456"):
        assert secret not in serialized
    assert "view=grid" in public["current_url"]
    assert public["sessions"] == [{"profile_id": "***xcto", "status": "ok"}]
    assert public["nested"] == {}


@pytest.mark.parametrize(
    "profile_id",
    [
        123456,
        ["profile-list-secret"],
        ("profile-tuple-secret",),
        {"status": "profile-dictionary-secret"},
    ],
)
def test_public_browser_payload_masks_non_string_profile_id_as_one_string(
    profile_id,
):
    app_module = importlib.import_module("gateway.app")

    public = app_module.public_browser_payload({"profile_id": profile_id})

    assert public["profile_id"] == mask_profile_id(profile_id)
    assert isinstance(public["profile_id"], str)


def test_adspower_window_list_retains_full_profile_id_for_form_submission(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setattr(
        app_module,
        "fetch_adspower_windows",
        lambda: {"windows": [{"profile_id": "profile-k1dxxcto", "name": "Test"}]},
    )

    response = create_app().test_client().get("/api/browser/adspower-windows")

    assert response.status_code == 200
    assert response.get_json()["windows"][0]["profile_id"] == "profile-k1dxxcto"


@pytest.mark.parametrize(
    ("field", "value", "secret_values", "safe_value"),
    [
        (
            "current_url",
            "https://example.com/safe?view=grid#session%5Fid=ZXCV123",
            ("ZXCV123",),
            "view=grid",
        ),
        (
            "current_url",
            "https://example.com/safe?view=grid#%73ession%5Fid=ZXCV123",
            ("ZXCV123",),
            "view=grid",
        ),
        (
            "current_url",
            "https://example.com/safe?view=grid#session[]=QWER456",
            ("QWER456",),
            "view=grid",
        ),
        (
            "current_url",
            "https://example.com/safe?view=grid#access-token/ASDF789",
            ("ASDF789",),
            "view=grid",
        ),
        (
            "error",
            "browser disconnected; session[id]=ASDF789; stage=connect",
            ("ASDF789",),
            "stage=connect",
        ),
        (
            "error",
            "browser disconnected; %73ession%5Fid=ASDF789; stage=connect",
            ("ASDF789",),
            "stage=connect",
        ),
        (
            "error",
            "browser disconnected; credential=COOKIE1, COOKIE2; stage=connect",
            ("COOKIE1", "COOKIE2"),
            "stage=connect",
        ),
        (
            "error",
            "browser disconnected; session=ONE123,TWO456; stage=connect",
            ("ONE123", "TWO456"),
            "stage=connect",
        ),
    ],
)
def test_public_browser_payload_conservatively_projects_structured_secrets(
    field,
    value,
    secret_values,
    safe_value,
):
    app_module = importlib.import_module("gateway.app")

    public_value = app_module.public_browser_payload({field: value})[field]

    for secret_value in secret_values:
        assert secret_value not in public_value
    assert safe_value in public_value
    if field == "current_url":
        assert public_value.startswith("https://example.com/safe?view=grid")


@pytest.mark.parametrize(
    "header_form",
    ["Cookie:", "Cookie=", "Authorization:", "Authorization="],
)
@pytest.mark.parametrize(
    "nested_key",
    ["status", "reason", "error", "message", "stage"],
)
@pytest.mark.parametrize("delimiter", [",", ";"])
@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_public_browser_payload_projects_complete_header_value(
    header_form,
    nested_key,
    delimiter,
    quote,
):
    app_module = importlib.import_module("gateway.app")
    random_secret = secrets.token_urlsafe(18)
    message = (
        f"diagnostic; {header_form} "
        f"{quote}session=FIXED{delimiter} {nested_key}={random_secret}{quote}"
    )

    public_error = app_module.public_browser_payload({"error": message})["error"]

    assert random_secret not in public_error
    assert public_error == f"diagnostic; {header_form} [redacted]"


@pytest.mark.parametrize(
    "header_form",
    ["Cookie:", "Cookie=", "Authorization:", "Authorization="],
)
@pytest.mark.parametrize(
    "status",
    ["missing", "expired", "invalid", "not configured"],
)
@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_public_browser_payload_preserves_only_complete_safe_header_value(
    header_form,
    status,
    quote,
):
    app_module = importlib.import_module("gateway.app")
    message = f"diagnostic; {header_form} {quote}{status}{quote}"

    assert app_module.public_browser_payload({"error": message})["error"] == message


def test_public_browser_payload_redacts_scheme_prefixed_safe_header_value():
    app_module = importlib.import_module("gateway.app")
    message = "diagnostic; Authorization: Basic missing"

    public_error = app_module.public_browser_payload({"error": message})["error"]

    assert public_error == "diagnostic; Authorization: [redacted]"


@pytest.mark.parametrize(
    "status_key",
    [
        "token_status",
        "credential_status",
        "cookie_status",
        "api_key_status",
        "access_key_status",
    ],
)
@pytest.mark.parametrize(
    "status_value",
    ["missing", "expired", "invalid", "not configured"],
)
def test_public_browser_payload_preserves_only_safe_structured_sensitive_status(
    status_key,
    status_value,
):
    app_module = importlib.import_module("gateway.app")

    public = app_module.public_browser_payload(
        {
            status_key: status_value,
            "nested": {status_key: "LIVE-SENSITIVE-VALUE"},
        }
    )

    assert public[status_key] == status_value
    assert public["nested"] == {}
    assert "LIVE-SENSITIVE-VALUE" not in json.dumps(public)


@pytest.mark.parametrize("sensitive_key", ["access_key", "accesskey"])
def test_public_browser_payload_drops_access_key_variants(sensitive_key):
    app_module = importlib.import_module("gateway.app")

    public = app_module.public_browser_payload(
        {
            "error": "browser disconnected",
            "nested": {sensitive_key: "ACCESS-KEY-VALUE"},
        }
    )

    assert public["error"] == "browser disconnected"
    assert public["nested"] == {}
    assert "ACCESS-KEY-VALUE" not in json.dumps(public)


@pytest.mark.parametrize(
    ("message", "secret_values"),
    [
        (
            "browser disconnected; access_key_id=AKID-SECRET",
            ("AKID-SECRET",),
        ),
        (
            "browser disconnected; Authorization=Basic BASIC-EQUAL-SECRET",
            ("BASIC-EQUAL-SECRET",),
        ),
        (
            "browser disconnected; token SPACE-TOKEN",
            ("SPACE-TOKEN",),
        ),
        (
            "browser disconnected; Cookie: "
            "session=COOKIE-SESSION; csrf=COOKIE-CSRF",
            ("COOKIE-SESSION", "COOKIE-CSRF"),
        ),
        (
            'browser disconnected; credential="ALPHA-BRAVO CHARLIE-DELTA"',
            ("ALPHA-BRAVO", "CHARLIE-DELTA"),
        ),
        (
            "browser disconnected; password='ECHO-FOXTROT GOLF-HOTEL'",
            ("ECHO-FOXTROT", "GOLF-HOTEL"),
        ),
        (
            'browser disconnected; ACCESSKEYID : "INDIA-JULIET KILO-LIMA"',
            ("INDIA-JULIET", "KILO-LIMA"),
        ),
        (
            'browser disconnected; Authorization Basic "MIKE-NOVEMBER OSCAR-PAPA"',
            ("MIKE-NOVEMBER", "OSCAR-PAPA"),
        ),
        (
            "browser disconnected; api key 'QUEBEC-ROMEO SIERRA-TANGO'",
            ("QUEBEC-ROMEO", "SIERRA-TANGO"),
        ),
        (
            'browser disconnected; Cookie: "session=UNIFORM-VICTOR; '
            'csrf=WHISKEY-XRAY"',
            ("UNIFORM-VICTOR", "WHISKEY-XRAY"),
        ),
    ],
    ids=[
        "access-key-id",
        "authorization-equals-basic",
        "space-separated-token",
        "complete-cookie-header",
        "double-quoted-value",
        "single-quoted-value",
        "normalized-access-key-id",
        "authorization-space-quoted",
        "api-key-space-quoted",
        "quoted-cookie-header",
    ],
)
def test_public_browser_payload_redacts_malicious_credential_text(
    message, secret_values
):
    app_module = importlib.import_module("gateway.app")

    public_error = app_module.public_browser_payload({"error": message})["error"]

    assert "browser disconnected" in public_error
    for secret_value in secret_values:
        assert secret_value not in public_error


@pytest.mark.parametrize(
    "message",
    [
        "browser disconnected; token: missing",
        "browser disconnected; token=expired",
        "browser disconnected; secret: invalid",
        "browser disconnected; credential=not configured",
        "browser disconnected; api_key: not configured",
        "browser disconnected; Cookie: invalid",
    ],
)
def test_public_browser_payload_preserves_safe_credential_status(message):
    app_module = importlib.import_module("gateway.app")

    assert app_module.public_browser_payload({"error": message})["error"] == message


def test_open_tile_rejects_http_url_without_host(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    orchestrator_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda profiles, **_kwargs: orchestrator_calls.append(profiles) or ([], None),
    )

    response = create_app().test_client().post(
        "/api/browser/open-tile",
        json={"url": "https://", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "url must be a valid http:// or https:// URL"
    assert orchestrator_calls == []


def test_browser_execute_strategy_reports_tile_failure_per_window(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    runtime_calls = []

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            return f"ws://secret-{profile_id}"

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda _hints: (_ for _ in ()).throw(
            RuntimeError("tile failed at ws://secret-layout?token=private")
        ),
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda *_args, **_kwargs: runtime_calls.append("prepare"),
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda *_args, **_kwargs: runtime_calls.append("run"),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["profile_id"] == "***le-1"
    assert result["status"] == "failed"
    assert result["stage"] == "tile"
    assert result["attempts"] == 1
    assert result["target_url"] == "https://www.tiktok.com/"
    assert "ws://" not in result["error"]
    assert "token=private" not in result["error"]
    assert runtime_calls == []
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_strategy_skips_only_profile_missing_from_layout(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    runtime_calls = []

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            return f"ws://secret-{profile_id}"

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda _hints: {"layout": [], "missing": ["profile-missing"]},
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda ws_url, url, **_kwargs: {
            "url": url,
            "closed_tabs": 0,
            "current_url": url,
        },
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda ws_url, target_url, *_args: runtime_calls.append(ws_url)
        or combined_strategy_result(target_url, verified_interactions=1),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "auto:auto-demo",
            "windows": [
                {"profile_id": "profile-ok"},
                {"profile_id": "profile-missing"},
            ],
        },
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert results[0]["status"] == "ok"
    assert results[1] == {
        "profile_id": "***sing",
        "status": "failed",
        "stage": "tile",
        "attempts": 1,
        "target_url": "https://www.tiktok.com/",
        "error": "窗口平铺失败：***sing",
    }
    assert runtime_calls == ["ws://secret-profile-ok"]
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        (
            {"missing": ["profile-missing"]},
            "窗口平铺失败：***sing",
        ),
        (
            {"missing": ["move failed for profile-missing after retry"]},
            "窗口平铺失败：move failed for ***sing after retry",
        ),
        (
            {
                "error": (
                    "tiler failed for profile-missing; "
                    "unrelated profile-missing-extra remains"
                )
            },
            "tiler failed for ***sing; unrelated profile-missing-extra remains",
        ),
        (
            {
                "scale_results": [
                    {
                        "profile_id": "profile-missing",
                        "status": "failed",
                        "error": "zoom failed for profile-missing",
                    }
                ]
            },
            "窗口缩放失败：zoom failed for ***sing",
        ),
    ],
)
def test_browser_tile_error_masks_known_profile_tokens(layout, expected):
    app_module = importlib.import_module("gateway.app")

    assert app_module.browser_tile_error(
        layout,
        "profile-missing",
        ["profile-ok", "profile-missing"],
    ) == expected


def test_browser_execute_strategy_isolates_scale_failure_from_window_tiler_shape(
    monkeypatch, tmp_path
):
    layout = window_tiler_result(
        ["profile-ok", "profile-fail"],
        scale_results=[
            {"profile_id": "profile-ok", "scale": 0.85, "status": "scaled"},
            {
                "profile_id": "profile-fail",
                "scale": 0.85,
                "status": "failed",
                "error": "zoom failed",
            },
        ],
    )

    response, runtime_calls = execute_auto_strategy_with_layout(
        monkeypatch, tmp_path, layout
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [item["status"] for item in results] == ["ok", "failed"]
    assert results[1]["stage"] == "tile"
    assert results[1]["error"] == "窗口缩放失败：zoom failed"
    assert runtime_calls == ["ws://profile-ok"]


def test_browser_execute_strategy_stops_all_ready_profiles_for_unattributed_tile_failure(
    monkeypatch, tmp_path
):
    layout = window_tiler_result(
        ["profile-ok", "profile-fail"],
        missing=["hwnd=101 title=AdsPower: move failed: window rect verification failed"],
    )

    response, runtime_calls = execute_auto_strategy_with_layout(
        monkeypatch, tmp_path, layout
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [item["status"] for item in results] == ["failed", "failed"]
    assert all(item["stage"] == "tile" for item in results)
    assert all("window rect verification failed" in item["error"] for item in results)
    assert runtime_calls == []


def test_browser_execute_strategy_stops_all_ready_profiles_for_unattributed_scale_failure(
    monkeypatch, tmp_path
):
    layout = window_tiler_result(
        ["profile-ok", "profile-fail"],
        scale_results=[
            {"profile_id": "profile-ok", "scale": 0.85, "status": "scaled"},
            {
                "profile_id": "",
                "scale": 0.85,
                "status": "failed",
                "error": "unattributed zoom failure",
            },
        ],
    )

    response, runtime_calls = execute_auto_strategy_with_layout(
        monkeypatch, tmp_path, layout
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [item["status"] for item in results] == ["failed", "failed"]
    assert all(item["stage"] == "tile" for item in results)
    assert all("unattributed zoom failure" in item["error"] for item in results)
    assert runtime_calls == []


def test_browser_execute_auto_strategy_uses_combined_runtime(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    call_order = []

    def fake_wait(ws_url, timeout=0, **_kwargs):
        call_order.append(("wait_for_cdp", ws_url, timeout))
        return True

    def fake_navigate(ws_url, url, **_kwargs):
        call_order.append(("navigate_and_close_other_tabs", ws_url, url))
        return {"url": url, "closed_tabs": 2, "current_url": url}

    def fake_run(ws_url, target_url, strategy, elements, patterns, _resolver):
        call_order.append(
            (
                "run_prepared_block_strategy_on_cdp",
                ws_url,
                target_url,
                strategy["id"],
            )
        )
        return combined_strategy_result(
            target_url, closed_tabs=2, verified_interactions=1
        )

    monkeypatch.setattr("browser_cdp.wait_for_cdp", fake_wait)
    monkeypatch.setattr("browser_cdp.navigate_and_close_other_tabs", fake_navigate)
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp", fake_run
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "ok"
    assert result["target_url"] == "https://example.com/landing"
    assert result["current_url"] == "https://example.com/landing"
    assert result["closed_tabs"] == 2
    assert result["stages"] == [
        {"stage": "wait_for_cdp", "status": "ok"},
        {"stage": "close_other_tabs", "status": "ok", "closed_tabs": 2},
        {
            "stage": "navigate",
            "status": "ok",
            "target_url": "https://example.com/landing",
            "current_url": "https://example.com/landing",
        },
        {"stage": "execute_actions", "status": "ok"},
    ]
    assert call_order == [
        ("wait_for_cdp", "ws://profile-1", 30.0),
        (
            "run_prepared_block_strategy_on_cdp",
            "ws://profile-1",
            "https://example.com/landing",
            "auto:auto-demo",
        ),
    ]
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


@pytest.mark.parametrize(
    ("failure_stage", "expected_stage", "expected_current_url"),
    [
        ("connect", "connect", ""),
        ("prepare", "prepare_page", "https://example.com/before"),
        ("action", "execute_actions", "https://www.tiktok.com/"),
    ],
)
def test_gateway_classifies_real_runtime_failure_contract_for_all_stages(
    monkeypatch,
    tmp_path,
    failure_stage,
    expected_stage,
    expected_current_url,
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "scroll-1",
                                "type": "scroll_down",
                                "params": {
                                    "distance": 120,
                                    "total_count": [1, 1],
                                    "burst_count": [1, 1],
                                    "interval_seconds": [0, 0],
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": "profile-1",
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": "ws://127.0.0.1:55001/devtools/browser/secret",
            "error": "",
        }
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-1"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )

    class Mouse:
        async def wheel(self, _x, _y):
            if failure_stage == "action":
                raise RuntimeError("wheel failed")

    class Page:
        def __init__(self):
            self.url = "https://example.com/before"
            self.mouse = Mouse()

        def is_closed(self):
            return False

        async def evaluate(self, _expression):
            return "visible"

        async def goto(self, url, **_options):
            if failure_stage == "prepare":
                raise RuntimeError("navigation failed")
            self.url = url

        async def wait_for_timeout(self, _milliseconds):
            return None

        async def close(self):
            return None

    page = Page()
    context = type("Context", (), {"pages": [page]})()
    browser = type(
        "Browser",
        (),
        {"contexts": [context], "is_connected": lambda self: True},
    )()
    context.browser = browser

    class Chromium:
        async def connect_over_cdp(self, _ws_url, timeout):
            assert timeout == 10_000
            if failure_stage == "connect":
                raise RuntimeError(
                    "connect failed at ws://127.0.0.1:55001/devtools/browser/secret"
                )
            return browser

    class Playwright:
        chromium = Chromium()

        async def stop(self):
            return None

    class Starter:
        async def start(self):
            return Playwright()

    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: Starter())

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "windows": [{"profile_id": "profile-1"}],
        },
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "failed"
    assert result["stage"] == expected_stage
    assert result["current_url"] == expected_current_url
    assert result["reason"] == result["error"]
    assert "devtools/browser" not in response.get_data(as_text=True)


def test_browser_execute_auto_strategy_reports_wait_failure_as_prepare_stage(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    runtime_called = []
    def fake_wait(_ws_url, timeout=0, **_kwargs):
        if timeout == 30.0:
            return True
        raise RuntimeError("CDP not ready")

    monkeypatch.setattr("browser_cdp.wait_for_cdp", fake_wait)
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda *_args, **_kwargs: {"url": "https://example.com/landing", "closed_tabs": 1, "current_url": "https://example.com/landing"},
    )
    def fail_combined(_ws_url, target_url, *_args):
        raise app_module.BrowserStageError(
            "wait_for_cdp", target_url, "CDP not ready"
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fail_combined,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    result = response.get_json()["results"][0]
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["profile_id"] == "***le-1"
    assert result["stage"] == "wait_for_cdp"
    assert result["target_url"] == "https://example.com/landing"
    assert result["reason"] == "CDP not ready"
    assert runtime_called == []
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_auto_strategy_reports_navigation_exception_as_prepare_stage(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    runtime_called = []
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("navigation blew up")),
    )
    def fail_combined(_ws_url, target_url, *_args):
        raise app_module.BrowserStageError(
            "navigate", target_url, "navigation blew up"
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fail_combined,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    result = response.get_json()["results"][0]
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["profile_id"] == "***le-1"
    assert result["stage"] == "navigate"
    assert result["target_url"] == "https://example.com/landing"
    assert result["reason"] == "navigation blew up"
    assert runtime_called == []
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_auto_strategy_isolates_navigation_failure_per_profile_and_logs_stage(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", tmp_path / "browser.jsonl")
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS.update(
        {
            "profile-ok": "ws://profile-ok",
            "profile-fail": "ws://profile-fail",
        }
    )
    runtime_calls = []

    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)

    def fake_navigate(ws_url, url, **_kwargs):
        if ws_url == "ws://profile-fail":
            raise RuntimeError("navigation blew up")
        return {"url": url, "closed_tabs": 2, "current_url": url}

    def fake_run(ws_url, target_url, strategy, elements, patterns, _resolver):
        if ws_url == "ws://profile-fail":
            raise app_module.BrowserStageError(
                "navigate", target_url, "navigation blew up"
            )
        runtime_calls.append((ws_url, strategy["id"]))
        return combined_strategy_result(
            target_url, closed_tabs=2, verified_interactions=1
        )

    monkeypatch.setattr("browser_cdp.navigate_and_close_other_tabs", fake_navigate)
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp", fake_run
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "auto:auto-demo",
            "windows": [
                {"profile_id": "profile-ok"},
                {"profile_id": "profile-fail"},
            ],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["results"] == [
        {
            "profile_id": "***e-ok",
            "status": "ok",
            "stage": "execute_actions",
            "attempts": 0,
            "target_url": "https://example.com/landing",
            "current_url": "https://example.com/landing",
            "closed_tabs": 2,
            "stages": [
                {"stage": "wait_for_cdp", "status": "ok"},
                {"stage": "close_other_tabs", "status": "ok", "closed_tabs": 2},
                {
                    "stage": "navigate",
                    "status": "ok",
                    "target_url": "https://example.com/landing",
                    "current_url": "https://example.com/landing",
                },
                {"stage": "execute_actions", "status": "ok"},
            ],
            "verified_interactions": 1,
        },
        {
            "profile_id": "***fail",
            "status": "failed",
            "stage": "navigate",
            "attempts": 0,
            "target_url": "https://example.com/landing",
            "current_url": "",
            "reason": "navigation blew up",
            "error": "navigation blew up",
        },
    ]
    assert runtime_calls == [("ws://profile-ok", "auto:auto-demo")]

    logs_payload = create_app().test_client().get("/api/browser/logs?limit=10").get_json()
    assert logs_payload["count"] == 1
    assert logs_payload["logs"][0]["operation"] == "execute_strategy"
    assert logs_payload["logs"][0]["payload"]["results"][1]["stage"] == "navigate"
    assert (
        logs_payload["logs"][0]["payload"]["results"][1]["target_url"]
        == "https://example.com/landing"
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_auto_strategy_stops_when_prepare_fails(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    runtime_called = []

    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda *_args, **_kwargs: {"url": "https://example.com/landing", "closed_tabs": 1, "current_url": "about:blank"},
    )
    def fail_combined(_ws_url, target_url, *_args):
        raise app_module.BrowserStageError(
            "navigate",
            target_url,
            "navigation settled on unexpected URL: about:blank",
            "about:blank",
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fail_combined,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "auto:auto-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "failed"
    assert result["profile_id"] == "***le-1"
    assert result["stage"] == "navigate"
    assert result["target_url"] == "https://example.com/landing"
    assert "about:blank" in result["error"]
    assert result["reason"] == result["error"]
    assert runtime_called == []
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_manual_strategy_uses_combined_runtime(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "name": "手动 Demo",
                        "actions": [{"type": "click", "element": "cta"}],
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    call_order = []

    monkeypatch.setattr(
        "browser_actions.validate_action_config",
        lambda elements, strategies: (elements, strategies),
    )
    monkeypatch.setattr(
        "browser_cdp.wait_for_cdp",
        lambda ws_url, timeout=0, **_kwargs: call_order.append(("wait_for_cdp", ws_url, timeout)) or True,
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda ws_url, url, **_kwargs: call_order.append(("navigate_and_close_other_tabs", ws_url, url)) or {"url": url, "closed_tabs": 1, "current_url": url},
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda ws_url, target_url, strategy, *_args: call_order.append(
            (
                "run_prepared_block_strategy_on_cdp",
                ws_url,
                target_url,
                strategy["id"],
            )
        )
        or combined_strategy_result(
            target_url,
            closed_tabs=1,
            actions=[{"action_id": "manual:manual-demo:action:1"}],
        ),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "manual:manual-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["status"] == "ok"
    assert result["target_url"] == "https://example.com/landing"
    assert result["current_url"] == "https://example.com/landing"
    assert result["closed_tabs"] == 1
    assert result["stages"] == [
        {"stage": "wait_for_cdp", "status": "ok"},
        {"stage": "close_other_tabs", "status": "ok", "closed_tabs": 1},
        {
            "stage": "navigate",
            "status": "ok",
            "target_url": "https://example.com/landing",
            "current_url": "https://example.com/landing",
        },
        {"stage": "execute_actions", "status": "ok"},
    ]
    assert call_order == [
        ("wait_for_cdp", "ws://profile-1", 30.0),
        (
            "run_prepared_block_strategy_on_cdp",
            "ws://profile-1",
            "https://example.com/landing",
            "manual:manual-demo",
        ),
    ]
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_manual_strategy_reports_wait_failure_as_prepare_stage(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "name": "手动 Demo",
                        "actions": [{"type": "click", "element": "cta"}],
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    monkeypatch.setattr(
        "browser_actions.validate_action_config",
        lambda elements, strategies: (elements, strategies),
    )
    def fake_wait(_ws_url, timeout=0, **_kwargs):
        if timeout == 30.0:
            return True
        raise RuntimeError("CDP wait failed")

    monkeypatch.setattr("browser_cdp.wait_for_cdp", fake_wait)
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda *_args, **_kwargs: {"url": "https://example.com/landing", "closed_tabs": 1, "current_url": "https://example.com/landing"},
    )
    actions_called = []
    monkeypatch.setattr(
        "browser_cdp.execute_xpath_action",
        lambda *_args, **_kwargs: actions_called.append(True),
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda _ws_url, target_url, *_args: (_ for _ in ()).throw(
            app_module.BrowserStageError(
                "wait_for_cdp", target_url, "CDP wait failed"
            )
        ),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "manual:manual-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    result = response.get_json()["results"][0]
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["profile_id"] == "***le-1"
    assert result["stage"] == "wait_for_cdp"
    assert result["target_url"] == "https://example.com/landing"
    assert result["reason"] == "CDP wait failed"
    assert actions_called == []
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_execute_manual_strategy_reports_navigation_exception_as_prepare_stage(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/landing",
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "name": "手动 Demo",
                        "actions": [{"type": "click", "element": "cta"}],
                    }
                ],
            }
        }
    )
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    monkeypatch.setattr(
        "browser_actions.validate_action_config",
        lambda elements, strategies: (elements, strategies),
    )
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("navigation failed")),
    )
    actions_called = []
    monkeypatch.setattr(
        "browser_cdp.execute_xpath_action",
        lambda *_args, **_kwargs: actions_called.append(True),
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda _ws_url, target_url, *_args: (_ for _ in ()).throw(
            app_module.BrowserStageError(
                "navigate", target_url, "navigation failed"
            )
        ),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "manual:manual-demo", "windows": [{"profile_id": "profile-1"}]},
    )

    result = response.get_json()["results"][0]
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["profile_id"] == "***le-1"
    assert result["stage"] == "navigate"
    assert result["target_url"] == "https://example.com/landing"
    assert result["reason"] == "navigation failed"
    assert actions_called == []
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_batch_task_rejects_more_than_eight_windows_per_batch(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ]
            }
        }
    )
    response = create_app().test_client().post(
        "/api/browser/batch-tasks",
        json={"strategy_id": "auto:auto-demo", "batch_size": 9, "windows": []},
    )

    assert response.status_code == 400
    assert "1 到 8" in response.get_json()["error"]


@pytest.mark.parametrize(
    ("strategy", "elements", "message"),
    [
        (
            {
                "entry_element": "",
                "input_element": "input",
                "submit_element": "submit",
            },
            {"entry": "//entry", "input": "//textarea", "submit": "//button"},
            "strategy needs repair before execution: click references missing element: ",
        ),
        (
            {
                "entry_element": "entry",
                "input_element": "input",
                "submit_element": "deleted-submit",
            },
            {"entry": "//entry", "input": "//textarea"},
            "strategy needs repair before execution: click references missing element: deleted-submit",
        ),
    ],
)
def test_batch_task_rejects_invalid_strategy_before_side_effects(
    monkeypatch, tmp_path, strategy, elements, message
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": elements,
                "auto_strategies": [{"id": "auto-invalid", **strategy}],
            }
        }
    )
    app_module.BROWSER_BATCH_TASKS.clear()
    fetch_calls = []
    batch_calls = []
    thread_calls = []
    monkeypatch.setattr(
        app_module,
        "fetch_adspower_windows",
        lambda: fetch_calls.append(True)
        or {"windows": [{"profile_id": "profile-1"}]},
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.build_batches",
        lambda items, size: batch_calls.append((items, size)) or [items],
    )

    class FakeThread:
        def __init__(self, *_args, **_kwargs):
            thread_calls.append("constructed")

        def start(self):
            thread_calls.append("started")

    client = create_app().test_client()
    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)

    response = client.post(
        "/api/browser/batch-tasks",
        json={"strategy_id": "auto:auto-invalid"},
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": message}
    assert app_module.BROWSER_BATCH_TASKS == {}
    assert batch_calls == []
    assert fetch_calls == []
    assert thread_calls == []


def test_batch_task_returns_400_for_malformed_legacy_strategy(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {
                    "entry": "//entry",
                    "input": "//textarea",
                    "submit": "//button",
                },
                "auto_strategies": [
                    {
                        "id": "auto-malformed",
                        "click_elements": "entry,input,submit",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ],
            }
        }
    )
    app_module.BROWSER_BATCH_TASKS.clear()
    thread_calls = []
    client = create_app().test_client()
    monkeypatch.setattr(
        app_module.threading,
        "Thread",
        lambda *_args, **_kwargs: thread_calls.append(True),
    )

    response = client.post(
        "/api/browser/batch-tasks",
        json={"strategy_id": "auto:auto-malformed", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "strategy needs repair before execution: click references missing element: "
    }
    assert app_module.BROWSER_BATCH_TASKS == {}
    assert thread_calls == []


def test_batch_task_creates_twenty_five_batches_for_one_hundred_windows(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "action_elements": {"entry": "//entry", "input": "//textarea", "submit": "//button"},
                "auto_strategies": [
                    {
                        "id": "auto-demo",
                        "name": "自动 Demo",
                        "entry_element": "entry",
                        "input_element": "input",
                        "submit_element": "submit",
                    }
                ]
            }
        }
    )
    app_module.BROWSER_BATCH_TASKS.clear()
    monkeypatch.setattr(app_module, "run_browser_batch_task", lambda *_args: None)
    windows = [{"profile_id": f"profile-{index}"} for index in range(100)]

    response = create_app().test_client().post(
        "/api/browser/batch-tasks",
        json={"strategy_id": "auto:auto-demo", "batch_size": 4, "windows": windows},
    )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["total_windows"] == 100
    assert payload["total_batches"] == 25
    status = create_app().test_client().get(f"/api/browser/batch-tasks/{payload['id']}")
    assert status.status_code == 200


def test_batch_runner_uses_leased_orchestrator_sessions_and_requests_safe_close(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings({"browser": {"action_elements": {}}})
    task_id = "leased-batch"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {"id": task_id}
    ensure_calls = []
    release_calls = []
    session = {
        "profile_id": "profile-1",
        "profile_no": "",
        "name": "",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }

    def fake_ensure(profiles, *, lease_sessions=False):
        ensure_calls.append((profiles, lease_sessions))
        return [session], window_tiler_result(["profile-1"])

    monkeypatch.setattr(app_module, "ensure_browser_profile_sessions", fake_ensure)
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda results, *, request_close=False: release_calls.append(
            (results, request_close)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "AdsPowerController",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("batch runner must not start or stop sessions directly")
        ),
    )
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs", lambda *_args: {"closed_tabs": 0}
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda _ws_url, target_url, *_args: combined_strategy_result(
            target_url, verified_interactions=1
        ),
    )

    app_module.run_browser_batch_task(
        create_app(),
        task_id,
        [{"profile_id": "profile-1"}],
        1,
        {"comment_brand_id": ""},
        "https://www.tiktok.com/",
    )

    assert ensure_calls == [([{"profile_id": "profile-1"}], True)]
    assert release_calls == [([session], True)]
    assert app_module.BROWSER_BATCH_TASKS[task_id]["status"] == "completed"


def test_batch_runner_preserves_block_failure_action_fields(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [],
            }
        }
    )
    task_id = "failed-block-batch"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {"id": task_id}
    session = {
        "profile_id": "profile-1",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: ([session], window_tiler_result(["profile-1"])),
    )
    monkeypatch.setattr(app_module, "release_browser_session_results", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda _ws_url, url: {"current_url": url, "closed_tabs": 0, "stages": []},
    )

    def fail_block(*_args, **_kwargs):
        from browser_strategy_runtime import BlockExecutionError

        raise BlockExecutionError("click-2", 2, "click", "target detached")

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fail_block,
    )

    app_module.run_browser_batch_task(
        create_app(),
        task_id,
        [{"profile_id": "profile-1"}],
        1,
        {
            "id": "strategy-1",
            "run_mode": "once",
            "actions": [
                {"id": "pause-1", "type": "pause", "params": {}},
                {"id": "click-2", "type": "click", "params": {}},
            ],
        },
        "https://www.tiktok.com/",
    )

    failure = app_module.BROWSER_BATCH_TASKS[task_id]["results"][0]
    assert failure["action_id"] == "click-2"
    assert failure["action_index"] == 2
    assert failure["action_type"] == "click"


def test_batch_task_list_and_get_sanitize_nested_results():
    app_module = importlib.import_module("gateway.app")
    task_id = "browser-batch-sensitive"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "completed",
        "target_url": "https://name:password@example.com/path?token=url-token&view=grid",
        "results": [
            {
                "profile_id": "profile-1",
                "status": "failed",
                "stage": "execute_actions",
                "action_id": "outerHTML",
                "action_index": 1,
                "action_type": "scroll_down",
                "cycle": 1,
                "code": "video_switch_xpath=//batch-code-secret",
                "error": "failed at ws://internal-session with Bearer bearer-secret",
                "locator": {
                    "code": "element_candidate_not_found",
                    "alias": "contenteditableAlias",
                    "scope": "active_video",
                    "diagnostics": {
                        "raw_count": 0,
                        "outerHTML_count": 9,
                        "xpath=_count": 7,
                    },
                },
                "outerHTML": "<main>private comment content</main>",
                "nested": {
                    "api_key": "api-secret",
                    "secret": "nested-secret",
                    "items": [
                        {
                            "selector": "xpath=//batch-get-secret",
                            "contenteditable_text": "contenteditable text",
                        }
                    ],
                },
            }
        ],
    }
    client = create_app().test_client()

    responses = [
        client.get("/api/browser/batch-tasks"),
        client.get(f"/api/browser/batch-tasks/{task_id}"),
    ]

    for response in responses:
        assert response.status_code == 200
        serialized = json.dumps(response.get_json())
        assert "https://example.com/path?" in serialized
        assert "view=grid" in serialized
        for secret in (
            "name",
            "password",
            "url-token",
            "ws://",
            "bearer-secret",
            "api-secret",
            "nested-secret",
                "outerHTML",
                "contenteditablealias",
                "video_switch_xpath",
                "xpath=_count",
                "private comment content",
            "xpath=",
            "contenteditable text",
        ):
            assert secret.casefold() not in serialized.casefold()
    app_module.BROWSER_BATCH_TASKS.clear()


class _SequenceGateService:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def check(self, strategy_id):
        self.calls.append(strategy_id)
        if len(self.decisions) > 1:
            return self.decisions.pop(0)
        return self.decisions[0]


def _gate_decision(allowed, reason_code="selector_validation_failed"):
    return {
        "strategy_id": "manual:manual-demo",
        "allowed": allowed,
        "effective_status": "active" if allowed else "paused",
        "reasons": []
        if allowed
        else [
            {
                "source": "probe",
                "reason_code": reason_code,
                "aliases": ["cta"],
                "selector_version_id": "sel-2",
                "created_at": "2026-07-29T03:00:00+08:00",
            }
        ],
    }


def _save_gate_test_strategy(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "default_url": "https://example.com/manual",
                "action_elements": {"cta": "//button"},
                "action_strategies": [
                    {
                        "id": "manual-demo",
                        "actions": [{"type": "click", "element": "cta"}],
                    }
                ],
            }
        }
    )


def test_execute_strategy_denied_before_any_profile_operation(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    gates = _SequenceGateService([_gate_decision(False)])
    profile_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: profile_calls.append(True),
    )

    response = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    ).test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": "profile-secret-1234"}],
        },
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "strategy_paused"
    assert response.get_json()["code"] == "strategy_paused"
    assert response.get_json()["reasons"][0]["reason_code"] == (
        "selector_validation_failed"
    )
    assert profile_calls == []
    assert "profile-secret-1234" not in response.get_data(as_text=True)


def test_execute_strategy_rechecks_gate_after_profile_reservation(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    gates = _SequenceGateService(
        [_gate_decision(True), _gate_decision(False)]
    )
    runtime_calls = []
    session = {
        "profile_id": "profile-secret-1234",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-secret-1234",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-secret-1234"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda *_args, **_kwargs: runtime_calls.append(True),
    )

    response = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    ).test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": "profile-secret-1234"}],
        },
    )

    result = response.get_json()["results"][0]
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["code"] == "strategy_paused_during_execution"
    assert result["profile_id"] == "***1234"
    assert runtime_calls == []


def test_execute_strategy_passes_gate_callback_and_sanitizes_mid_run_pause(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    gates = _SequenceGateService([_gate_decision(True)])
    session = {
        "profile_id": "profile-secret-1234",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-secret-1234",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-secret-1234"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda *_args, **_kwargs: None,
    )

    def pause_mid_run(
        _ws_url,
        _target_url,
        strategy,
        *_args,
        gate_check=None,
    ):
        from browser_strategy_runtime import StrategyPausedError

        assert gate_check(strategy["id"], strategy["actions"][0])["allowed"]
        raise StrategyPausedError(
            strategy["id"],
            strategy["actions"][0]["id"],
            1,
            _gate_decision(False)["reasons"],
            [],
            action_type="click",
            cycle=1,
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        pause_mid_run,
    )

    response = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    ).test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": "profile-secret-1234"}],
        },
    )

    result = response.get_json()["results"][0]
    assert result["code"] == "strategy_paused_during_execution"
    assert result["reason"] == "strategy_paused_during_execution"
    assert result["gate_reasons"][0]["aliases"] == ["cta"]
    serialized = json.dumps(response.get_json())
    assert "profile-secret-1234" not in serialized
    assert "ws://profile-secret-1234" not in serialized
    assert "//button" not in serialized


def test_unstarted_batch_task_is_delayed_when_strategy_gate_is_closed(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    gates = _SequenceGateService([_gate_decision(False)])
    task_id = "gate-delayed-batch"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "strategy_id": "manual:manual-demo",
        "results": [],
    }
    profile_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: profile_calls.append(True),
    )
    app = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    )

    app_module.run_browser_batch_task(
        app,
        task_id,
        [{"profile_id": "profile-secret-1234"}],
        1,
        {
            "id": "manual:manual-demo",
            "run_mode": "once",
            "actions": [],
        },
        "https://example.com/manual",
    )

    task = app_module.BROWSER_BATCH_TASKS[task_id]
    assert task["status"] == "delayed_gate"
    assert task["results"] == []
    assert profile_calls == []
    app_module.BROWSER_BATCH_TASKS.clear()


class _DependencyGateService(_SequenceGateService):
    def __init__(self, decisions, *, fail_on_rebuild=None):
        super().__init__(decisions)
        self.rebuilds = []
        self.fail_on_rebuild = fail_on_rebuild

    def rebuild_dependencies(self, strategies):
        self.rebuilds.append(copy.deepcopy(strategies))
        if self.fail_on_rebuild == len(self.rebuilds):
            raise RuntimeError("redis://secret dependency failure")


def test_strategy_save_rebuilds_dependency_index_before_persisting(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    app_module.load_persisted_strategy_state()
    gates = _DependencyGateService([_gate_decision(True)])
    candidate = [
        {
            "id": "saved-strategy",
            "name": "Saved",
            "run_mode": "once",
            "batch_size": 1,
            "actions": [
                {
                    "id": "saved-click",
                    "type": "click",
                    "params": {
                        "element": "cta",
                        "button": "left",
                        "click_count": 1,
                        "hold_seconds": [0, 0],
                        "trajectory": {"source": "builtin", "id": "bezier"},
                    },
                }
            ],
        }
    ]

    response = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    ).test_client().put(
        "/api/browser/strategies",
        json={"strategies": candidate},
    )

    assert response.status_code == 200
    normalized = response.get_json()["strategies"]
    assert gates.rebuilds[-1] == normalized
    assert load_settings()["browser"]["block_strategies"] == normalized


def test_strategy_save_rejects_dependency_failure_without_persisting(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    app_module.load_persisted_strategy_state()
    before = load_settings()["browser"]["block_strategies"]
    gates = _DependencyGateService(
        [_gate_decision(True)],
        fail_on_rebuild=2,
    )

    response = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    ).test_client().put(
        "/api/browser/strategies",
        json={
            "strategies": [
                {
                    "id": "must-not-save",
                    "name": "Must not save",
                    "run_mode": "once",
                    "batch_size": 1,
                    "actions": [],
                }
            ]
        },
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "dependency_index_unavailable"
    }
    assert load_settings()["browser"]["block_strategies"] == before
    assert "secret" not in response.get_data(as_text=True)


def test_strategy_write_failure_rolls_dependency_index_back_to_old_config(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    settings_module = importlib.import_module("gateway.settings_store")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    previous = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ]
    gates = _DependencyGateService([_gate_decision(True)])
    app = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    )
    monkeypatch.setattr(
        settings_module,
        "_replace_config_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("disk secret")
        ),
    )

    response = app.test_client().put(
        "/api/browser/strategies",
        json={
            "strategies": [
                {
                    "id": "not-persisted",
                    "name": "Not persisted",
                    "run_mode": "once",
                    "batch_size": 1,
                    "actions": [],
                }
            ]
        },
    )

    assert response.status_code == 503
    assert response.get_json() == {
        "error": "strategy_save_unavailable"
    }
    assert load_settings()["browser"]["block_strategies"] == previous
    assert gates.rebuilds[0] == previous
    assert gates.rebuilds[-1] == previous
    assert gates.rebuilds[1][0]["id"] == "not-persisted"
    assert "disk secret" not in response.get_data(as_text=True)


def test_strategy_write_and_dependency_rollback_failure_stays_fail_closed(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    settings_module = importlib.import_module("gateway.settings_store")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    previous = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ]
    strategy_id = previous[0]["id"]
    gates = _DependencyGateService(
        [_gate_decision(True)],
        fail_on_rebuild=3,
    )
    app = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    )
    monkeypatch.setattr(
        settings_module,
        "_replace_config_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("disk unavailable")
        ),
    )
    client = app.test_client()

    failed_save = client.put(
        "/api/browser/strategies",
        json={
            "strategies": [
                {
                    "id": "not-persisted",
                    "name": "Not persisted",
                    "run_mode": "once",
                    "batch_size": 1,
                    "actions": [],
                }
            ]
        },
    )
    execution = client.post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": strategy_id,
            "windows": [{"profile_id": "profile-1"}],
        },
    )

    assert failed_save.status_code == 503
    assert load_settings()["browser"]["block_strategies"] == previous
    assert execution.status_code == 409
    assert execution.get_json()["code"] == "strategy_paused"
    assert execution.get_json()["reasons"][0]["reason_code"] == (
        "dependency_index_unavailable"
    )


def test_first_gate_factory_use_migrates_existing_strategies(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    expected = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ]
    gates = _DependencyGateService([_gate_decision(False)])

    response = create_app(
        {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
    ).test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "manual:manual-demo",
            "windows": [{"profile_id": "profile-1"}],
        },
    )

    assert response.status_code == 409
    assert gates.rebuilds == [expected]


def test_default_gate_factory_uses_final_injected_probe_store_factory(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    expected = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ]
    custom_store_factory = lambda: object()
    captured = []
    gates = _DependencyGateService([_gate_decision(True)])

    def fake_default_gate_factory(*, store_factory):
        captured.append(store_factory)
        return gates

    monkeypatch.setattr(
        app_module,
        "default_selector_probe_gate_service_factory",
        fake_default_gate_factory,
    )

    app = create_app(
        {"SELECTOR_PROBE_STORE_FACTORY": custom_store_factory}
    )
    service = app.config["SELECTOR_PROBE_GATE_SERVICE_FACTORY"]()

    assert service is gates
    assert captured == [custom_store_factory]
    assert gates.rebuilds == [expected]


def test_first_dependency_migration_never_holds_migration_lock_while_loading(
    monkeypatch,
):
    app_module = importlib.import_module("gateway.app")
    settings_lock = threading.RLock()
    save_holds_settings = threading.Event()
    execution_loading = threading.Event()
    errors = []
    rebuilds = []

    class Service:
        def rebuild_dependencies(self, strategies):
            rebuilds.append(copy.deepcopy(strategies))

    def load_snapshot():
        if threading.current_thread().name == "gate-execution":
            execution_loading.set()
        if not settings_lock.acquire(timeout=1):
            raise RuntimeError("settings lock timeout")
        try:
            return {"block_strategies": [{"id": "snapshot", "actions": []}]}
        finally:
            settings_lock.release()

    monkeypatch.setattr(
        app_module,
        "load_persisted_strategy_state",
        load_snapshot,
    )
    factory = app_module._dependency_aware_gate_factory(Service)

    def execute_gate():
        try:
            factory()
        except Exception as error:
            errors.append(("execution", str(error)))

    def save_gate():
        try:
            with settings_lock:
                save_holds_settings.set()
                assert execution_loading.wait(1)
                factory()
        except Exception as error:
            errors.append(("save", str(error)))

    save_thread = threading.Thread(target=save_gate, name="gate-save")
    execution_thread = threading.Thread(
        target=execute_gate,
        name="gate-execution",
    )
    save_thread.start()
    assert save_holds_settings.wait(1)
    execution_thread.start()
    save_thread.join(2)
    execution_thread.join(2)

    assert not save_thread.is_alive()
    assert not execution_thread.is_alive()
    assert errors == []
    assert rebuilds == [[{"id": "snapshot", "actions": []}]]


def test_batch_with_only_session_failures_remains_delayable_when_gate_closes(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    gates = _SequenceGateService(
        [
            _gate_decision(True),
            _gate_decision(True),
            _gate_decision(False),
        ]
    )
    task_id = "gate-session-failures"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "strategy_id": "manual:manual-demo",
        "results": [],
    }
    failed_session = {
        "profile_id": "profile-1",
        "status": "failed",
        "stage": "session_start",
        "attempts": 3,
        "error": "temporary",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: ([failed_session], {"missing": []}),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda *_args, **_kwargs: None,
    )

    app_module.run_browser_batch_task(
        create_app(
            {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
        ),
        task_id,
        [{"profile_id": "profile-1"}, {"profile_id": "profile-2"}],
        1,
        {
            "id": "manual:manual-demo",
            "run_mode": "once",
            "actions": [],
        },
        "https://example.com/manual",
    )

    task = app_module.BROWSER_BATCH_TASKS[task_id]
    assert task["processed_windows"] == 1
    assert task["status"] == "delayed_gate"
    assert not task.get("finished_at")
    app_module.BROWSER_BATCH_TASKS.clear()


def test_batch_becomes_terminal_after_real_action_execution_starts(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    allow = _gate_decision(True)
    gates = _SequenceGateService(
        [allow, allow, allow, allow, allow, allow, _gate_decision(False)]
    )
    task_id = "gate-action-started"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "strategy_id": "manual:manual-demo",
        "results": [],
    }
    strategy = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ][0]
    session = {
        "profile_id": "profile-1",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-1"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda *_args, **_kwargs: None,
    )

    mutations = []

    class Locator:
        async def scroll_into_view_if_needed(self):
            mutations.append("scroll_into_view")

        async def bounding_box(self):
            return {"x": 20, "y": 10, "width": 40, "height": 20}

    class Mouse:
        async def move(self, *_args, **_kwargs):
            mutations.append("mouse_move")

        async def click(self, *_args, **_kwargs):
            mutations.append("mouse_click")

    page = type(
        "Page",
        (),
        {
            "viewport_size": {"width": 1280, "height": 720},
            "mouse": Mouse(),
        },
    )()
    locator = Locator()

    async def resolve_element(_page, alias, elements):
        await asyncio.sleep(0)
        return (
            elements[alias],
            type(
                "Resolved",
                (),
                {
                    "locator": locator,
                    "alias": alias,
                    "scope": "document",
                    "candidate": {"id": "test-css", "type": "css"},
                },
            )(),
        )

    monkeypatch.setattr(
        "browser_actions._resolve_action_element",
        resolve_element,
    )
    monkeypatch.setattr(
        "actions_dom.generate_ghost_path",
        lambda *_args, **_kwargs: [
            {"x": 100, "y": 100},
            {"x": 200, "y": 200},
        ],
    )

    def run_real_runtime(
        _ws_url,
        _target_url,
        runtime_strategy,
        elements,
        patterns,
        text_resolver,
        *,
        gate_check=None,
        on_action_dispatch=None,
    ):
        from browser_strategy_runtime import run_block_strategy

        return asyncio.run(
            run_block_strategy(
                page,
                runtime_strategy,
                elements,
                patterns,
                text_resolver,
                gate_check=gate_check,
                on_action_dispatch=on_action_dispatch,
            )
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        run_real_runtime,
    )

    app_module.run_browser_batch_task(
        create_app(
            {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
        ),
        task_id,
        [{"profile_id": "profile-1"}],
        1,
        strategy,
        "https://example.com/manual",
    )

    task = app_module.BROWSER_BATCH_TASKS[task_id]
    assert task["action_execution_started"] is True
    assert task["status"] == "failed"
    assert task["finished_at"]
    assert mutations == ["scroll_into_view"]
    app_module.BROWSER_BATCH_TASKS.clear()


def test_real_runtime_second_gate_pause_keeps_zero_action_batch_delayed(
    monkeypatch,
    tmp_path,
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    allow = _gate_decision(True)
    gates = _SequenceGateService(
        [allow, allow, allow, allow, allow, _gate_decision(False)]
    )
    task_id = "gate-real-runtime-zero-action"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "strategy_id": "manual:manual-demo",
        "results": [],
    }
    strategy_value = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ][0]
    session = {
        "profile_id": "profile-1",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-1"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda *_args, **_kwargs: None,
    )
    mutations = []

    class Locator:
        async def scroll_into_view_if_needed(self):
            mutations.append("scroll_into_view")

        async def bounding_box(self):
            mutations.append("bounding_box")
            return {"x": 20, "y": 10, "width": 40, "height": 20}

    class Mouse:
        async def move(self, *_args, **_kwargs):
            mutations.append("mouse_move")

        async def click(self, *_args, **_kwargs):
            mutations.append("mouse_click")

    page = type(
        "Page",
        (),
        {
            "viewport_size": {"width": 1280, "height": 720},
            "mouse": Mouse(),
        },
    )()

    async def resolve_element(_page, alias, elements):
        await asyncio.sleep(0)
        return (
            elements[alias],
            type(
                "Resolved",
                (),
                {
                    "locator": Locator(),
                    "alias": alias,
                    "scope": "document",
                    "candidate": {"id": "test-css", "type": "css"},
                },
            )(),
        )

    monkeypatch.setattr(
        "browser_actions._resolve_action_element",
        resolve_element,
    )

    def run_real_runtime(
        _ws_url,
        _target_url,
        runtime_strategy,
        elements,
        patterns,
        text_resolver,
        *,
        gate_check=None,
        on_action_dispatch=None,
    ):
        from browser_strategy_runtime import run_block_strategy

        return asyncio.run(
            run_block_strategy(
                page,
                runtime_strategy,
                elements,
                patterns,
                text_resolver,
                gate_check=gate_check,
                on_action_dispatch=on_action_dispatch,
            )
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        run_real_runtime,
    )

    app_module.run_browser_batch_task(
        create_app(
            {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
        ),
        task_id,
        [{"profile_id": "profile-1"}],
        1,
        strategy_value,
        "https://example.com/manual",
    )

    task = app_module.BROWSER_BATCH_TASKS[task_id]
    assert task["action_execution_started"] is False
    assert task["status"] == "delayed_gate"
    assert not task.get("finished_at")
    assert task["results"][0]["code"] == "strategy_paused_during_execution"
    assert mutations == []
    app_module.BROWSER_BATCH_TASKS.clear()


def test_batch_becomes_terminal_after_an_action_completed(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    _save_gate_test_strategy(tmp_path, monkeypatch)
    allow = _gate_decision(True)
    gates = _SequenceGateService([allow, allow, allow, _gate_decision(False)])
    task_id = "gate-action-completed"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "strategy_id": "manual:manual-demo",
        "results": [],
    }
    strategy = app_module.load_persisted_strategy_state()[
        "block_strategies"
    ][0]
    session = {
        "profile_id": "profile-1",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-1"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda _ws_url, target_url, *_args, **_kwargs:
        combined_strategy_result(
            target_url,
            actions=[
                {
                    "action_id": strategy["actions"][0]["id"],
                    "action_index": 1,
                    "cycle": 1,
                    "type": "click",
                    "status": "ok",
                }
            ],
        ),
    )

    app_module.run_browser_batch_task(
        create_app(
            {"SELECTOR_PROBE_GATE_SERVICE_FACTORY": lambda: gates}
        ),
        task_id,
        [{"profile_id": "profile-1"}, {"profile_id": "profile-2"}],
        1,
        strategy,
        "https://example.com/manual",
    )

    task = app_module.BROWSER_BATCH_TASKS[task_id]
    assert task["action_execution_completed"] is True
    assert task["status"] == "failed"
    assert task["finished_at"]
    app_module.BROWSER_BATCH_TASKS.clear()


def test_review_wave1_batch_stores_and_serves_only_projected_results(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [],
            }
        }
    )
    strategy = {
        "id": "canonical",
        "name": "Canonical",
        "run_mode": "once",
        "batch_size": 2,
        "actions": [
            {
                "id": "scroll-1",
                "type": "scroll_down",
                "params": {
                    "distance": 120,
                    "total_count": [1, 1],
                    "burst_count": [1, 1],
                    "interval_seconds": [0, 0],
                },
            }
        ],
    }
    task_id = "review-wave1-batch"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {
        "id": task_id,
        "status": "queued",
        "strategy_id": "canonical",
        "results": [],
    }
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-ok", "profile-failed")
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-ok", "profile-failed"]),
        ),
    )
    releases = []
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda results, *, request_close=False: releases.append(
            (results, request_close)
        ),
    )
    raw_numeric = "987654321012345678"
    raw_full_hash = "d" * 64

    def fake_run(ws_url, target_url, *_args):
        if ws_url.endswith("profile-ok"):
            return combined_strategy_result(
                target_url,
                cycles=1,
                actions=[
                    {
                        "action_id": "scroll-1",
                        "action_index": 1,
                        "cycle": 1,
                        "type": "scroll_down",
                        "status": "ok",
                        "requested_switches": 1,
                        "completed_switches": 1,
                        "wheel_events": 8,
                        "switches": [
                            {
                                "from": raw_numeric,
                                "to": raw_full_hash,
                                "wheel_events": 8,
                            }
                        ],
                    }
                ],
                outerHTML="<main>private comment content</main>",
                nested={"selector": "css=.batch-top-secret"},
                raw_numeric=raw_numeric,
            )
        from browser_strategy_runtime import BlockExecutionError, StrategyRuntimeError

        switch = VideoSwitchError(
            "video_switch_not_observed",
            requested_switches=1,
            completed_switches=0,
            wheel_events=8,
            switches=[
                {"from": raw_numeric, "to": raw_full_hash, "wheel_events": 8}
            ],
        )
        source = BlockExecutionError(
            "scroll-1",
            1,
            "scroll_down",
            "video_switch_not_observed; outerHTML private comment content",
            page_recoveries=[
                {
                    "action_id": "scroll-1",
                    "action_index": 1,
                    "action_type": "scroll_down",
                    "cycle": 1,
                    "retry": 1,
                    "status": "failed",
                    "outcome": "retry_failed",
                    "nested": {"selector": "xpath=//batch-recovery-secret"},
                }
            ],
            source=switch,
        )
        source.cycle = 1
        staged = StrategyRuntimeError(
            "execute_actions",
            str(source),
            target_url=target_url,
            page_recoveries=source.page_recoveries,
            source=source,
        )
        staged.cycle = 1
        raise staged

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_run,
    )

    app_module.run_browser_batch_task(
        create_app(),
        task_id,
        [{"profile_id": "profile-ok"}, {"profile_id": "profile-failed"}],
        2,
        strategy,
        "https://www.tiktok.com/@owner/video/987654321",
    )

    assert releases == [(sessions, True)]
    stored = app_module.BROWSER_BATCH_TASKS[task_id]
    successful, failed = stored["results"]
    assert successful["actions"][0]["requested_switches"] == 1
    assert successful["actions"][0]["switches"] == [
        {
            "from": hashlib.sha256(raw_numeric.encode()).hexdigest()[:12],
            "to": hashlib.sha256(raw_full_hash.encode()).hexdigest()[:12],
            "wheel_events": 8,
        }
    ]
    assert failed["code"] == "video_switch_not_observed"
    assert failed["requested_switches"] == 1
    assert failed["page_recoveries"][0]["retry"] == 1

    client = create_app().test_client()
    responses = [
        client.get("/api/browser/batch-tasks"),
        client.get(f"/api/browser/batch-tasks/{task_id}"),
    ]
    public_text = json.dumps(stored, ensure_ascii=False)
    public_text += log_path.read_text(encoding="utf-8")
    for response in responses:
        assert response.status_code == 200
        public_text += response.get_data(as_text=True)
    public_text = public_text.casefold()
    for forbidden in (
        "xpath=",
        "css=",
        "outerhtml",
        "contenteditable text",
        "private comment content",
        "/video/987654321",
        "cookie",
        "authorization",
        "devtools/browser",
        raw_numeric,
        raw_full_hash,
    ):
        assert forbidden not in public_text
    app_module.BROWSER_BATCH_TASKS.clear()


def test_ping_returns_ok_status():
    app = create_app()
    client = app.test_client()

    response = client.get("/ping")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_open_tile_syncs_custom_url_and_closes_other_tabs(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app = create_app()
    client = app.test_client()

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            return f"ws://{profile_id}"

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda hints: {
            "count": len(hints),
            "requested_count": len(hints),
            "matched_count": len(hints),
            "layout": [],
            "missing": [],
        },
    )
    navigated = []

    def fake_navigate(ws_url, url):
        navigated.append((ws_url, url))
        return {"url": url, "closed_tabs": 2, "current_url": url}

    monkeypatch.setattr("browser_cdp.navigate_and_close_other_tabs", fake_navigate)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    app_module.ACTIVE_BROWSER_SESSIONS.clear()

    response = client.post(
        "/api/browser/open-tile",
        json={
            "url": "https://example.com/target",
            "windows": [{"profile_id": "profile-1"}, {"profile_id": "profile-2"}],
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["task_id"]
    assert payload["url"] == "https://example.com/target"
    assert payload["started"] == 2
    assert payload["results"] == [
        {
            "profile_id": "***le-1",
            "profile_no": "",
            "name": "",
            "status": "started",
            "stage": "session_start",
            "attempts": 1,
            "target_url": "https://example.com/target",
            "error": "",
        },
        {
            "profile_id": "***le-2",
            "profile_no": "",
            "name": "",
            "status": "started",
            "stage": "session_start",
            "attempts": 1,
            "target_url": "https://example.com/target",
            "error": "",
        },
    ]
    assert payload["navigation"] == [
        {
            "profile_id": "***le-1",
            "status": "ok",
            "stage": "navigate",
            "attempts": 1,
            "target_url": "https://example.com/target",
            "url": "https://example.com/target",
            "current_url": "https://example.com/target",
            "closed_tabs": 2,
        },
        {
            "profile_id": "***le-2",
            "status": "ok",
            "stage": "navigate",
            "attempts": 1,
            "target_url": "https://example.com/target",
            "url": "https://example.com/target",
            "current_url": "https://example.com/target",
            "closed_tabs": 2,
        },
    ]
    assert "ws_url" not in json.dumps(payload)
    assert "ws_puppeteer" not in json.dumps(payload)
    assert navigated == [
        ("ws://profile-1", "https://example.com/target"),
        ("ws://profile-2", "https://example.com/target"),
    ]


def test_open_tile_retries_prepare_until_third_success_without_restarting_session(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app = create_app()
    client = app.test_client()

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            return f"ws://{profile_id}"

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr("window_tiler.tile_browser_windows", lambda _hints: {"layout": [], "missing": []})
    attempts = {"ws://profile-1": 0}
    waits = []

    def flaky_navigate(ws_url, url):
        attempts[ws_url] = attempts.get(ws_url, 0) + 1
        if attempts[ws_url] < 3:
            raise RuntimeError("CDP 尚未就绪")
        return {"url": url, "closed_tabs": 3, "current_url": url}

    monkeypatch.setattr("browser_cdp.navigate_and_close_other_tabs", flaky_navigate)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(app_module.time, "sleep", waits.append)
    app_module.ACTIVE_BROWSER_SESSIONS.clear()

    response = client.post(
        "/api/browser/open-tile",
        json={"url": "https://www.tiktok.com/", "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    assert response.get_json()["navigation"] == [
        {
            "profile_id": "***le-1",
            "status": "ok",
            "stage": "navigate",
            "attempts": 3,
            "target_url": "https://www.tiktok.com/",
            "url": "https://www.tiktok.com/",
            "current_url": "https://www.tiktok.com/",
            "closed_tabs": 3,
        }
    ]
    assert attempts == {"ws://profile-1": 3}
    assert waits == [2, 2]


def test_open_tile_reports_prepare_failure_after_three_attempts(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    session = {
        "profile_id": "profile-1",
        "profile_no": "",
        "name": "",
        "status": "ready",
        "stage": "session_check",
        "attempts": 0,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda _profiles, **_kwargs: ([session], window_tiler_result(["profile-1"])),
    )
    prepare_calls = []
    waits = []

    def fail_prepare(_ws_url, target_url):
        prepare_calls.append(target_url)
        raise app_module.BrowserStageError("navigate", target_url, "navigation failed")

    monkeypatch.setattr(app_module, "prepare_browser_page", fail_prepare)
    monkeypatch.setattr(app_module.time, "sleep", waits.append)

    response = create_app().test_client().post(
        "/api/browser/open-tile",
        json={"windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    assert response.get_json()["navigation"] == [
        {
            "profile_id": "***le-1",
            "status": "failed",
            "stage": "navigate",
            "attempts": 3,
            "target_url": "https://www.tiktok.com/",
            "error": "navigation failed",
        }
    ]
    assert prepare_calls == ["https://www.tiktok.com/"] * 3
    assert waits == [2, 2]


def test_open_tile_restarts_profile_when_cdp_is_not_ready(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app = create_app()
    client = app.test_client()
    starts = []
    stops = []
    readiness_checks = []

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            starts.append(profile_id)
            return f"ws://{profile_id}/{len(starts)}"

        def stop_browser(self, profile_id):
            stops.append(profile_id)
            return {"status": "stopped"}

    def fake_wait(ws_url, **_kwargs):
        readiness_checks.append(ws_url)
        if len(readiness_checks) <= 2:
            raise TimeoutError("CDP 尚未就绪")
        return True

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr("browser_cdp.wait_for_cdp", fake_wait)
    monkeypatch.setattr("window_tiler.tile_browser_windows", lambda hints: {"layout": [], "missing": []})
    monkeypatch.setattr(
        "browser_cdp.navigate_and_close_other_tabs",
        lambda ws_url, url: {"url": url, "closed_tabs": 2, "current_url": url},
    )
    monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)
    app_module.ACTIVE_BROWSER_SESSIONS.clear()

    response = client.post(
        "/api/browser/open-tile",
        json={"windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["started"] == 1
    assert payload["url"] == "https://www.tiktok.com/"
    assert payload["navigation"][0]["status"] == "ok"
    assert payload["results"][0]["attempts"] == 3
    assert payload["navigation"][0]["attempts"] == 1
    assert starts == ["profile-1", "profile-1", "profile-1"]
    assert stops == ["profile-1", "profile-1"]


def test_failed_session_restart_removes_stale_active_and_keeps_ready_profile(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS.update(
        {
            "profile-fail": "ws://stale-fail",
            "profile-ok": "ws://healthy-ok",
        }
    )

    class FakeController:
        def __init__(self, **_kwargs):
            pass

        def start_browser(self, profile_id):
            if profile_id == "profile-fail":
                raise RuntimeError("restart failed")
            raise AssertionError(f"healthy profile restarted: {profile_id}")

        def get_browser_active(self, _profile_id):
            return {"status": "inactive"}

        def stop_browser(self, _profile_id):
            return {"status": "stopped"}

    monkeypatch.setattr(app_module, "AdsPowerController", FakeController)
    monkeypatch.setattr(app_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        "browser_cdp.wait_for_cdp",
        lambda ws_url, **_kwargs: ws_url == "ws://healthy-ok",
    )
    monkeypatch.setattr(
        "window_tiler.tile_browser_windows",
        lambda _hints: window_tiler_result(["profile-ok"]),
    )

    results, _layout = app_module.ensure_browser_profile_sessions(
        [{"profile_id": "profile-fail"}, {"profile_id": "profile-ok"}]
    )

    assert [item["status"] for item in results] == ["failed", "ready"]
    response = create_app().test_client().get("/api/browser/sessions")
    assert response.get_json() == {
        "count": 1,
        "sessions": [{"profile_id": "***e-ok", "status": "active"}],
    }
    assert app_module.ACTIVE_BROWSER_SESSIONS == {"profile-ok": "ws://healthy-ok"}
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_sessions_api_lists_active_profiles_without_ws_endpoint():
    app_module = importlib.import_module("gateway.app")
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS.update(
        {"profile-1": "ws://127.0.0.1:50001/devtools/browser/one"}
    )

    response = create_app().test_client().get("/api/browser/sessions")

    assert response.status_code == 200
    assert response.get_json() == {
        "count": 1,
        "sessions": [{"profile_id": "***le-1", "status": "active"}],
    }
    app_module.ACTIVE_BROWSER_SESSIONS.clear()


def test_browser_logs_api_reads_backend_operation_log(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", tmp_path / "browser.jsonl")
    app_module.record_browser_log(
        "execute_strategy",
        {
            "status": "failed",
            "ws_url": "ws://secret-endpoint",
            "nested": {"access_token": "secret-token"},
            "error": "failed at wss://secret-endpoint/path?token=private",
        },
    )

    response = create_app().test_client().get("/api/browser/logs?limit=10")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["count"] == 1
    assert payload["logs"][0]["operation"] == "execute_strategy"
    serialized = json.dumps(payload["logs"], ensure_ascii=False)
    assert "ws_url" not in serialized
    assert "ws://" not in serialized
    assert "wss://" not in serialized
    assert "secret-token" not in serialized
    assert "token=private" not in serialized


def test_browser_logs_api_sanitizes_legacy_log_entries(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    log_path.write_text(
        json.dumps(
            {
                "operation": "legacy",
                "payload": {
                    "ws_puppeteer": "ws://legacy-secret",
                    "error": "Bearer legacy-token",
                },
            }
        ),
        encoding="utf-8",
    )

    response = create_app().test_client().get("/api/browser/logs?limit=10")

    assert response.status_code == 200
    serialized = json.dumps(response.get_json(), ensure_ascii=False)
    assert "ws_puppeteer" not in serialized
    assert "ws://legacy-secret" not in serialized
    assert "legacy-token" not in serialized


def test_create_app_scrubs_sensitive_values_from_legacy_browser_log_file(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    log_path.write_text(
        json.dumps(
            {
                "operation": "legacy",
                "payload": {
                    "ws_puppeteer": "ws://legacy-secret-endpoint",
                    "nested": {"api_key": "legacy-api-secret"},
                    "error": "Bearer legacy-bearer-secret",
                },
            }
        ),
        encoding="utf-8",
    )

    create_app()

    raw = log_path.read_text(encoding="utf-8")
    assert "ws://" not in raw
    assert "legacy-secret-endpoint" not in raw
    assert "legacy-api-secret" not in raw
    assert "legacy-bearer-secret" not in raw


def test_block_strategy_needs_repair_is_rejected_before_session_side_effects(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "broken",
                        "name": "Broken",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "click-1",
                                "type": "click",
                                "params": {
                                    "element": "missing",
                                    "button": "left",
                                    "click_count": 1,
                                    "hold_seconds": [0.05, 0.15],
                                    "trajectory": {"source": "builtin", "id": "bezier"},
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: calls.append(True),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": "broken", "windows": [{"profile_id": "p1"}]},
    )

    assert response.status_code == 400
    assert "needs repair" in response.get_json()["error"]
    assert calls == []


def test_execution_reservation_allows_different_profiles_concurrently_and_releases():
    app_module = importlib.import_module("gateway.app")
    barrier = threading.Barrier(2)
    entered = []
    errors = []

    def reserve(profile_id):
        try:
            with app_module.browser_profile_execution_reservation(profile_id):
                entered.append(profile_id)
                barrier.wait(timeout=2)
                if profile_id == "profile-1":
                    raise RuntimeError("runtime failed")
        except RuntimeError as error:
            if str(error) != "runtime failed":
                errors.append(error)
        except Exception as error:
            errors.append(error)

    first = threading.Thread(target=reserve, args=("profile-1",))
    second = threading.Thread(target=reserve, args=("profile-2",))
    first.start()
    second.start()
    first.join(timeout=3)
    second.join(timeout=3)

    assert sorted(entered) == ["profile-1", "profile-2"]
    assert errors == []
    with app_module.browser_profile_execution_reservation("profile-1"):
        with pytest.raises(app_module.BrowserExecutionBusyError):
            with app_module.browser_profile_execution_reservation("profile-1"):
                pytest.fail("same-profile reservation must fail fast")


def test_normal_and_batch_same_profile_do_not_overlap_and_failure_releases_reservation(
    monkeypatch,
    tmp_path,
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "pause-1",
                                "type": "pause",
                                "params": {"duration_seconds": [0, 0]},
                            }
                        ],
                    }
                ],
            }
        }
    )

    def fake_sessions(profiles, **_kwargs):
        sessions = [
            {
                "profile_id": item["profile_id"],
                "status": "ready",
                "stage": "session_start",
                "attempts": 1,
                "ws_url": f"ws://{item['profile_id']}",
                "error": "",
            }
            for item in profiles
        ]
        return sessions, window_tiler_result(
            [item["profile_id"] for item in profiles]
        )

    monkeypatch.setattr(app_module, "ensure_browser_profile_sessions", fake_sessions)
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )
    first_entered = threading.Event()
    finish_first = threading.Event()
    runtime_calls = []
    runtime_calls_lock = threading.Lock()

    def fake_runtime(_ws_url, target_url, *_args):
        with runtime_calls_lock:
            runtime_calls.append(target_url)
            call_number = len(runtime_calls)
        if call_number == 1:
            first_entered.set()
            assert finish_first.wait(timeout=3)
            raise RuntimeError("normal runtime failed")
        return combined_strategy_result(
            target_url,
            actions=[],
            page_recoveries=[],
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_runtime,
    )
    normal_response = {}

    def run_normal():
        normal_response["value"] = create_app().test_client().post(
            "/api/browser/execute-strategy",
            json={
                "strategy_id": "canonical",
                "windows": [{"profile_id": "profile-1"}],
            },
        )

    normal_thread = threading.Thread(target=run_normal)
    normal_thread.start()
    assert first_entered.wait(timeout=3)

    busy_task_id = "busy-batch"
    app_module.BROWSER_BATCH_TASKS[busy_task_id] = {"id": busy_task_id}
    app_module.run_browser_batch_task(
        create_app(),
        busy_task_id,
        [{"profile_id": "profile-1"}],
        1,
        {"id": "canonical", "actions": []},
        "https://www.tiktok.com/",
    )
    busy_result = app_module.BROWSER_BATCH_TASKS[busy_task_id]["results"][0]
    assert busy_result["status"] == "failed"
    assert busy_result["stage"] == "execution_busy"
    assert len(runtime_calls) == 1

    finish_first.set()
    normal_thread.join(timeout=3)
    assert normal_response["value"].get_json()["results"][0]["status"] == "failed"

    retry_task_id = "retry-batch"
    app_module.BROWSER_BATCH_TASKS[retry_task_id] = {"id": retry_task_id}
    app_module.run_browser_batch_task(
        create_app(),
        retry_task_id,
        [{"profile_id": "profile-1"}],
        1,
        {"id": "canonical", "actions": []},
        "https://www.tiktok.com/",
    )
    retry_result = app_module.BROWSER_BATCH_TASKS[retry_task_id]["results"][0]
    assert retry_result["status"] == "ok"
    assert len(runtime_calls) == 2


def test_block_strategy_execution_isolates_runtime_failure_per_profile(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 2,
                        "actions": [
                            {
                                "id": "pause-1",
                                "type": "pause",
                                "params": {"duration_seconds": [0, 0]},
                            }
                        ],
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("ok", "bad")
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (sessions, window_tiler_result(["ok", "bad"])),
    )
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda _ws_url, url: {"current_url": url, "closed_tabs": 1, "stages": []},
    )

    def fake_run(ws_url, target_url, *_args):
        if ws_url.endswith("bad"):
            raise RuntimeError("boom")
        return combined_strategy_result(target_url, actions=[], cycles=1)

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp", fake_run
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "windows": [{"profile_id": "ok"}, {"profile_id": "bad"}],
        },
    )

    assert response.status_code == 200
    assert [item["status"] for item in response.get_json()["results"]] == ["ok", "failed"]


def test_combined_strategy_reports_safe_verified_switches_and_scoped_locator(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {
                    "entry": {
                        "scope": "active_video",
                        "locators": [
                            {
                                "id": "tiktok-comment-entry-primary",
                                "type": "attribute",
                                "name": "data-e2e",
                                "value": "comment-icon",
                                "enabled": True,
                            }
                        ],
                    }
                },
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "scroll-1",
                                "type": "scroll_down",
                                "params": {
                                    "distance": 120,
                                    "total_count": [3, 3],
                                    "burst_count": [1, 1],
                                    "interval_seconds": [0, 0],
                                },
                            },
                            {
                                "id": "click-1",
                                "type": "click",
                                "params": {
                                    "element": "entry",
                                    "button": "left",
                                    "click_count": 1,
                                    "hold_seconds": [0.05, 0.15],
                                    "trajectory": {
                                        "source": "builtin",
                                        "id": "bezier",
                                    },
                                },
                            },
                        ],
                    }
                ],
            }
        }
    )
    session = {
        "profile_id": "profile-1",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-1"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )
    raw_numeric_id = "987654321012345678"
    raw_full_hash = "a" * 64
    runner_result = combined_strategy_result(
        "https://www.tiktok.com/@owner/video/987654321",
        cycles=1,
        actions=[
            {
                "action_id": "scroll-1",
                "action_index": 1,
                "cycle": 1,
                "type": "scroll_down",
                "status": "ok",
                "requested_switches": 3,
                "completed_switches": 3,
                "wheel_events": 23,
                "switches": [
                    {
                        "from": "a1b2c3d4e5f6",
                        "to": "c3d4e5f6a7b8",
                        "wheel_events": 8,
                        "full_fingerprint": "video:987654321",
                    },
                    {
                        "from": raw_numeric_id,
                        "to": raw_full_hash,
                        "wheel_events": 7,
                    },
                    {
                        "from": "e5f6a7b8c9d0",
                        "to": "a7b8c9d0e1f2",
                        "wheel_events": 8,
                    },
                ],
                "video_id": "987654321",
            },
            {
                "action_id": "click-1",
                "action_index": 2,
                "cycle": 1,
                "type": "click",
                "status": "ok",
                "element": "entry",
                "locator": {
                    "scope": "active_video",
                    "candidate_id": "tiktok-comment-entry-primary",
                    "candidate_type": "attribute",
                    "selector": "xpath=//button[@data-secret='raw']",
                },
                "selector": "css=.raw-selector",
                "outerHTML": "<button>private comment content</button>",
                "text": "private comment content",
                "contenteditable_text": "contenteditable text",
                "cookie": "cookie-secret",
                "authorization": "Bearer auth-secret",
                "endpoint": "ws://127.0.0.1/devtools/browser/raw-id",
            },
        ],
        outerHTML="<main>private comment content</main>",
        nested={
            "items": [
                {
                    "selector": "xpath=//top-level-secret",
                    "contenteditable_text": "contenteditable text",
                    "raw_numeric_id": raw_numeric_id,
                }
            ]
        },
        raw_numeric_id=raw_numeric_id,
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda *_args, **_kwargs: runner_result,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "url": "https://www.tiktok.com/@owner/video/987654321",
            "windows": [{"profile_id": "profile-1"}],
        },
    )

    assert response.status_code == 200
    actions = response.get_json()["results"][0]["actions"]
    assert len(actions) == 2
    assert actions[0]["action_id"] == "scroll-1"
    assert actions[0]["requested_switches"] == 3
    assert actions[0]["completed_switches"] == 3
    assert actions[0]["wheel_events"] == 23
    assert actions[0]["switches"][0] == {
        "from": "a1b2c3d4e5f6",
        "to": "c3d4e5f6a7b8",
        "wheel_events": 8,
    }
    assert actions[0]["switches"][1] == {
        "from": hashlib.sha256(raw_numeric_id.encode()).hexdigest()[:12],
        "to": hashlib.sha256(raw_full_hash.encode()).hexdigest()[:12],
        "wheel_events": 7,
    }
    assert all(
        re.fullmatch(r"[0-9a-f]{12}", switch[field])
        for switch in actions[0]["switches"]
        for field in ("from", "to")
    )
    assert actions[1]["element"] == "entry"
    assert actions[1]["locator"] == {
        "scope": "active_video",
        "candidate_id": "tiktok-comment-entry-primary",
        "candidate_type": "attribute",
    }
    public_text = (
        response.get_data(as_text=True)
        + log_path.read_text(encoding="utf-8")
    ).casefold()
    for forbidden in (
        "xpath=",
        "css=",
        "outerhtml",
        "contenteditable text",
        "private comment content",
        "/video/987654321",
        "cookie-secret",
        "cookie",
        "auth-secret",
        "authorization",
        "devtools/browser",
        "full_fingerprint",
        raw_numeric_id,
        raw_full_hash,
    ):
        assert forbidden not in public_text


def test_verified_switch_failure_isolated_per_profile_with_partial_measurements(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {
                    "entry": {
                        "scope": "active_video",
                        "locators": [
                            {
                                "id": "tiktok-comment-entry-primary",
                                "type": "attribute",
                                "name": "data-e2e",
                                "value": "comment-icon",
                                "enabled": True,
                            }
                        ],
                    }
                },
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 2,
                        "actions": [
                            {
                                "id": "scroll-1",
                                "type": "scroll_down",
                                "params": {
                                    "distance": 120,
                                    "total_count": [3, 3],
                                    "burst_count": [1, 1],
                                    "interval_seconds": [0, 0],
                                },
                            },
                            {
                                "id": "click-1",
                                "type": "click",
                                "params": {
                                    "element": "entry",
                                    "button": "left",
                                    "click_count": 1,
                                    "hold_seconds": [0.05, 0.15],
                                    "trajectory": {
                                        "source": "builtin",
                                        "id": "bezier",
                                    },
                                },
                            },
                        ],
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-1", "profile-2")
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-1", "profile-2"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )

    def fake_run(ws_url, target_url, *_args):
        if ws_url.endswith("profile-1"):
            return combined_strategy_result(
                target_url,
                cycles=1,
                actions=[
                    {
                        "action_id": "scroll-1",
                        "action_index": 1,
                        "cycle": 1,
                        "type": "scroll_down",
                        "status": "ok",
                        "requested_switches": 3,
                        "completed_switches": 3,
                        "wheel_events": 23,
                        "switches": [
                            {
                                "from": "a1b2c3d4e5f6",
                                "to": "c3d4e5f6a7b8",
                                "wheel_events": 8,
                            }
                        ],
                    },
                    {
                        "action_id": "click-1",
                        "action_index": 2,
                        "cycle": 1,
                        "type": "click",
                        "status": "ok",
                        "element": "entry",
                        "locator": {
                            "scope": "active_video",
                            "candidate_id": "tiktok-comment-entry-primary",
                            "candidate_type": "attribute",
                            "selector": "xpath=//button[@data-secret='raw']",
                        },
                        "postcondition": "observed",
                        "text": "private comment content",
                    },
                ],
            )
        from browser_strategy_runtime import BlockExecutionError, StrategyRuntimeError
        from browser_video_switch import VideoSwitchError

        source = BlockExecutionError(
            "scroll-1",
            1,
            "scroll_down",
            "video_switch_not_observed; outerHTML private comment content",
            page_recoveries=[
                {
                    "action_id": "scroll-1",
                    "action_index": 1,
                    "action_type": "scroll_down",
                    "cycle": 1,
                    "retry": 1,
                    "status": "failed",
                    "outcome": "retry_failed",
                    "outerHTML": "<article>private comment content</article>",
                    "nested": {"selector": "css=.recovery-secret"},
                }
            ],
            source=VideoSwitchError(
                "video_switch_not_observed",
                requested_switches=3,
                completed_switches=1,
                wheel_events=17,
                switches=[
                    {
                        "from": "987654321012345678",
                        "to": "b" * 64,
                        "wheel_events": 7,
                        "full_fingerprint": "video:987654321",
                    }
                ],
            ),
        )
        source.cycle = 1
        staged = StrategyRuntimeError(
            "execute_actions",
            str(source),
            target_url=target_url,
            page_recoveries=source.page_recoveries,
            source=source,
        )
        staged.cycle = 1
        raise staged

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_run,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "windows": [
                {"profile_id": "profile-1"},
                {"profile_id": "profile-2"},
            ],
        },
    )

    assert response.status_code == 200
    successful, failed = response.get_json()["results"]
    assert successful["status"] == "ok"
    assert successful["actions"][0]["completed_switches"] == 3
    assert successful["actions"][1]["element"] == "entry"
    assert successful["actions"][1]["locator"]["scope"] == "active_video"
    assert failed["status"] == "failed"
    assert failed["stage"] == "execute_actions"
    assert failed["code"] == "video_switch_not_observed"
    assert failed["requested_switches"] == 3
    assert failed["completed_switches"] == 1
    assert failed["wheel_events"] == 17
    assert failed["switches"] == [
        {
            "from": hashlib.sha256(b"987654321012345678").hexdigest()[:12],
            "to": hashlib.sha256(("b" * 64).encode()).hexdigest()[:12],
            "wheel_events": 7,
        }
    ]
    assert failed["page_recoveries"][0]["retry"] == 1
    with app_module.browser_profile_execution_reservation("profile-1"):
        pass
    with app_module.browser_profile_execution_reservation("profile-2"):
        pass
    public_text = (
        response.get_data(as_text=True) + log_path.read_text(encoding="utf-8")
    ).casefold()
    for forbidden in (
        "xpath=",
        "css=",
        "outerhtml",
        "contenteditable text",
        "private comment content",
        "/video/987654321",
        "cookie",
        "authorization",
        "devtools/browser",
        "full_fingerprint",
        "987654321012345678",
        "b" * 64,
    ):
        assert forbidden not in public_text


@pytest.mark.parametrize(
    "error",
    [
        LocatorResolutionError(
            "element_candidate_not_found",
            "entry",
            "active_video",
            {},
        ),
        VideoSwitchError("video_switch_not_observed"),
    ],
)
def test_execution_reservation_releases_after_locator_or_switch_error(error):
    app_module = importlib.import_module("gateway.app")

    with pytest.raises(type(error)):
        with app_module.browser_profile_execution_reservation("profile-error"):
            raise error

    with app_module.browser_profile_execution_reservation("profile-error"):
        pass


@pytest.mark.parametrize(
    "mutate",
    [
        lambda actions: actions.__setitem__(
            0, {**actions[0], "type": "click"}
        ),
        lambda actions: actions.__setitem__(1, dict(actions[0])),
        lambda actions: actions.reverse(),
        lambda actions: actions.__setitem__(
            1, {**actions[1], "action_index": 1}
        ),
    ],
    ids=("mismatched-type", "duplicate", "reordered", "forged-index"),
)
def test_review_wave1_execution_projection_rejects_noncanonical_occurrences(mutate):
    app_module = importlib.import_module("gateway.app")
    strategy = {
        "id": "canonical",
        "run_mode": "once",
        "actions": [
            {"id": "scroll-1", "type": "scroll_down", "params": {}},
            {"id": "pause-1", "type": "pause", "params": {}},
        ],
    }
    actions = [
        {
            "action_id": "scroll-1",
            "action_index": 1,
            "cycle": 1,
            "type": "scroll_down",
            "status": "ok",
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 8,
            "switches": [
                {
                    "from": "a1b2c3d4e5f6",
                    "to": "c3d4e5f6a7b8",
                    "wheel_events": 8,
                }
            ],
        },
        {
            "action_id": "pause-1",
            "action_index": 2,
            "cycle": 1,
            "type": "pause",
            "status": "ok",
            "duration_seconds": 0.1,
        },
    ]
    mutate(actions)

    projected = app_module.public_strategy_execution_result(
        {"status": "ok", "cycles": 1, "actions": actions},
        strategy,
    )

    assert projected["actions"] == []


def test_public_strategy_failure_projects_prior_safe_completed_actions():
    app_module = importlib.import_module("gateway.app")
    from browser_strategy_runtime import BlockExecutionError, StrategyRuntimeError

    strategy = {
        "id": "failure-evidence",
        "run_mode": "once",
        "actions": [
            {"id": "scroll-1", "type": "scroll_down", "params": {}},
            {"id": "comment-1", "type": "click", "params": {}},
        ],
    }
    source = BlockExecutionError(
        "comment-1",
        2,
        "click",
        "private comment content",
        cycle=1,
        completed_actions=[
            {
                "action_id": "scroll-1",
                "type": "scroll_down",
                "status": "ok",
                "cycle": 1,
                "action_index": 1,
                "requested_switches": 1,
                "completed_switches": 1,
                "wheel_events": 6,
                "switches": [
                    {
                        "from": "a1b2c3d4e5f6",
                        "to": "c3d4e5f6a7b8",
                        "wheel_events": 6,
                        "full_fingerprint": "video:private",
                    }
                ],
                "input_text": "private comment content",
                "selector": "xpath=//private-selector",
            }
        ],
    )
    error = StrategyRuntimeError("execute_actions", str(source), source=source)

    public = app_module.public_strategy_failure_result(
        profile_id="profile-1",
        attempts=1,
        target_url="https://www.tiktok.com/",
        error=error,
        strategy=strategy,
        elements={},
    )

    assert public["actions"] == [
        {
            "action_id": "scroll-1",
            "action_index": 1,
            "cycle": 1,
            "type": "scroll_down",
            "status": "ok",
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 6,
            "switches": [
                {
                    "from": "a1b2c3d4e5f6",
                    "to": "c3d4e5f6a7b8",
                    "wheel_events": 6,
                }
            ],
        }
    ]
    serialized = json.dumps(public).casefold()
    for forbidden in (
        "private comment content",
        "private-selector",
        "full_fingerprint",
        "input_text",
        "selector",
    ):
        assert forbidden not in serialized


def test_public_strategy_failure_rejects_mismatched_completed_actions():
    app_module = importlib.import_module("gateway.app")
    from browser_strategy_runtime import BlockExecutionError, StrategyRuntimeError

    strategy = {
        "id": "failure-evidence",
        "run_mode": "once",
        "actions": [
            {"id": "scroll-1", "type": "scroll_down", "params": {}},
            {"id": "comment-1", "type": "click", "params": {}},
        ],
    }
    source = BlockExecutionError(
        "comment-1",
        2,
        "click",
        "comment entry unavailable",
        cycle=1,
        completed_actions=[
            {
                "action_id": "forged-scroll",
                "type": "scroll_down",
                "status": "ok",
                "cycle": 1,
                "action_index": 1,
                "requested_switches": 1,
                "completed_switches": 1,
                "wheel_events": 6,
            }
        ],
    )
    error = StrategyRuntimeError("execute_actions", str(source), source=source)

    public = app_module.public_strategy_failure_result(
        profile_id="profile-1",
        attempts=1,
        target_url="https://www.tiktok.com/",
        error=error,
        strategy=strategy,
        elements={},
    )

    assert public["actions"] == []


def test_public_strategy_failure_rejects_malformed_completed_action_list():
    app_module = importlib.import_module("gateway.app")

    strategy = {
        "id": "failure-evidence",
        "run_mode": "once",
        "actions": [
            {"id": "scroll-1", "type": "scroll_down", "params": {}},
            {"id": "comment-1", "type": "click", "params": {}},
        ],
    }
    error = RuntimeError("comment entry unavailable")
    error.stage = "execute_actions"
    error.action_id = "comment-1"
    error.action_index = 2
    error.action_type = "click"
    error.cycle = 1
    error.completed_actions = [
        {
            "action_id": "scroll-1",
            "type": "scroll_down",
            "status": "ok",
            "cycle": 1,
            "action_index": 1,
            "requested_switches": 1,
            "completed_switches": 1,
            "wheel_events": 6,
            "switches": [],
        },
        "forged action",
    ]

    public = app_module.public_strategy_failure_result(
        profile_id="profile-1",
        attempts=1,
        target_url="https://www.tiktok.com/",
        error=error,
        strategy=strategy,
        elements={},
    )

    assert public["actions"] == []


@pytest.mark.parametrize(
    ("failure_index", "completed_indices"),
    [
        (3, [1, 1]),
        (3, [2, 1]),
        (2, [1, 2]),
        (2, [1, 3]),
    ],
    ids=("duplicate", "out-of-order", "equal-to-failure", "after-failure"),
)
def test_public_strategy_failure_rejects_invalid_completed_action_order(
    failure_index,
    completed_indices,
):
    app_module = importlib.import_module("gateway.app")
    strategy = {
        "id": "ordered-failure-evidence",
        "run_mode": "once",
        "actions": [
            {"id": f"pause-{index}", "type": "pause", "params": {}}
            for index in range(1, 4)
        ],
    }
    error = RuntimeError("pause unavailable")
    error.stage = "execute_actions"
    error.action_id = f"pause-{failure_index}"
    error.action_index = failure_index
    error.action_type = "pause"
    error.cycle = 1
    error.completed_actions = [
        {
            "action_id": f"pause-{index}",
            "type": "pause",
            "status": "ok",
            "cycle": 1,
            "action_index": index,
        }
        for index in completed_indices
    ]

    public = app_module.public_strategy_failure_result(
        profile_id="profile-1",
        attempts=1,
        target_url="https://www.tiktok.com/",
        error=error,
        strategy=strategy,
        elements={},
    )

    assert public["actions"] == []


def test_review_wave2_regex_valid_forbidden_ids_are_masked_in_response_and_log(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    strategy_id = "outerHTML"
    action_id = "selectorSecret"
    alias = "contenteditableText"
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {alias: "//button"},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": strategy_id,
                        "name": "Selector shaped",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": action_id,
                                "type": "click",
                                "params": {
                                    "element": alias,
                                    "button": "left",
                                    "click_count": 1,
                                    "hold_seconds": [0.05, 0.15],
                                    "trajectory": {
                                        "source": "builtin",
                                        "id": "bezier",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    session = {
        "profile_id": "profile-1",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-1",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: ([session], window_tiler_result(["profile-1"])),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        lambda *_args, **_kwargs: combined_strategy_result(
            "https://www.tiktok.com/",
            cycles=1,
            actions=[
                {
                    "action_id": action_id,
                    "action_index": 1,
                    "cycle": 1,
                    "type": "click",
                    "status": "ok",
                    "element": alias,
                }
            ],
        ),
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={"strategy_id": strategy_id, "windows": [{"profile_id": "profile-1"}]},
    )

    assert response.status_code == 200
    payload = response.get_json()
    action_result = payload["results"][0]["actions"][0]
    assert re.fullmatch(r"strategy-[0-9a-f]{12}", payload["strategy_id"])
    assert re.fullmatch(r"action-[0-9a-f]{12}", action_result["action_id"])
    assert re.fullmatch(r"element-[0-9a-f]{12}", action_result["element"])
    public_text = (
        response.get_data(as_text=True) + log_path.read_text(encoding="utf-8")
    ).casefold()
    for forbidden in ("outerhtml", "selectorsecret", "contenteditabletext"):
        assert forbidden not in public_text


@pytest.mark.parametrize(
    ("code", "source_alias", "diagnostics", "expected_diagnostics"),
    [
        (
            "element_candidate_not_found",
            "entry",
            {
                "raw_count": 0,
                "selector": "css=.locator-secret",
                "outerHTML_count": 9,
                "xpath=_count": 7,
            },
            {"raw_count": 0},
        ),
        (
            "element_scope_not_found",
            "",
            {
                "container_count": 0,
                "outerHTML_count": 9,
                "xpath=_count": 7,
            },
            {"container_count": 0},
        ),
        (
            "element_not_actionable",
            "entry",
            {
                "phase": "editable_check",
                "selector_count": 4,
            },
            {"phase": "editable_check"},
        ),
        (
            "element_postcondition_not_observed",
            "entry",
            {
                "candidate_id": "comment-primary",
                "candidate_type": "attribute",
                "timeout_seconds": 5.0,
                "outerHTML_count": 9,
            },
            {
                "candidate_id": "comment-primary",
                "candidate_type": "attribute",
                "timeout_seconds": 5.0,
            },
        ),
    ],
)
def test_review_wave2_locator_failures_are_projected_and_release_route_reservation(
    monkeypatch,
    tmp_path,
    code,
    source_alias,
    diagnostics,
    expected_diagnostics,
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    definition = {
        "scope": "active_video",
        "locators": [
            {
                "id": "comment-primary",
                "type": "attribute",
                "name": "data-e2e",
                "value": "comment-icon",
                "enabled": True,
            }
        ],
    }
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {"entry": definition},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "locator-failure",
                        "name": "Locator failure",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "click-1",
                                "type": "click",
                                "params": {
                                    "element": "entry",
                                    "button": "left",
                                    "click_count": 1,
                                    "hold_seconds": [0.05, 0.15],
                                    "trajectory": {
                                        "source": "builtin",
                                        "id": "bezier",
                                    },
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    session = {
        "profile_id": "profile-locator",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-locator",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-locator"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )

    def fail_locator(_ws_url, target_url, *_args):
        from browser_strategy_runtime import BlockExecutionError, StrategyRuntimeError

        source = BlockExecutionError(
            "click-1",
            1,
            "click",
            f"{code}; outerHTML private comment content",
            page_recoveries=[
                {
                    "action_id": "click-1",
                    "action_index": 1,
                    "action_type": "click",
                    "cycle": 1,
                    "retry": 1,
                    "status": "failed",
                    "outcome": "retry_failed",
                    "selector": "xpath=//recovery-secret",
                }
            ],
            locator={
                "code": code,
                "alias": source_alias,
                "scope": "active_video",
                "diagnostics": diagnostics,
            },
        )
        source.code = code
        source.cycle = 1
        staged = StrategyRuntimeError(
            "execute_actions",
            str(source),
            target_url=target_url,
            page_recoveries=source.page_recoveries,
            source=source,
        )
        staged.cycle = 1
        raise staged

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fail_locator,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "locator-failure",
            "windows": [{"profile_id": "profile-locator"}],
        },
    )

    assert response.status_code == 200
    failure = response.get_json()["results"][0]
    assert failure["status"] == "failed"
    assert failure["code"] == code
    assert failure["error"] == code
    assert failure["reason"] == code
    assert failure["action_id"] == "click-1"
    assert failure["action_index"] == 1
    assert failure["action_type"] == "click"
    assert failure["cycle"] == 1
    assert failure["locator"] == {
        "code": code,
        "alias": "entry",
        "scope": "active_video",
        "diagnostics": expected_diagnostics,
    }
    assert failure["page_recoveries"][0]["retry"] == 1
    public_text = (
        response.get_data(as_text=True) + log_path.read_text(encoding="utf-8")
    ).casefold()
    for forbidden in (
        "xpath=",
        "css=",
        "outerhtml",
        "selector_count",
        "xpath=_count",
        "private comment content",
    ):
        assert forbidden not in public_text
    with app_module.browser_profile_execution_reservation("profile-locator"):
        pass


def test_review_wave2_bound_malicious_switch_code_is_rejected(monkeypatch, tmp_path):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "forged-failure",
                        "name": "Forged failure",
                        "run_mode": "once",
                        "batch_size": 1,
                        "actions": [
                            {
                                "id": "scroll-1",
                                "type": "scroll_down",
                                "params": {
                                    "distance": 120,
                                    "total_count": [1, 1],
                                    "burst_count": [1, 1],
                                    "interval_seconds": [0, 0],
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    session = {
        "profile_id": "profile-forged",
        "status": "ready",
        "stage": "session_start",
        "attempts": 1,
        "ws_url": "ws://profile-forged",
        "error": "",
    }
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            [session],
            window_tiler_result(["profile-forged"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )

    def fail_forged(_ws_url, target_url, *_args):
        from browser_strategy_runtime import BlockExecutionError, StrategyRuntimeError

        switch = VideoSwitchError(
            "video_switch_xpath=//secret",
            requested_switches=1,
            completed_switches=0,
            wheel_events=8,
            switches=[
                {
                    "from": "987654321012345678",
                    "to": "c" * 64,
                    "wheel_events": 8,
                }
            ],
        )
        source = BlockExecutionError(
            "scroll-1",
            1,
            "scroll_down",
            "private comment content",
            source=switch,
        )
        source.cycle = 1
        staged = StrategyRuntimeError(
            "execute_actions",
            str(source),
            target_url=target_url,
            source=source,
        )
        staged.cycle = 1
        raise staged

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fail_forged,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "forged-failure",
            "windows": [{"profile_id": "profile-forged"}],
        },
    )

    assert response.status_code == 200
    failure = response.get_json()["results"][0]
    assert failure["status"] == "failed"
    assert failure["action_id"] == "scroll-1"
    assert failure["action_index"] == 1
    assert failure["action_type"] == "scroll_down"
    assert failure["cycle"] == 1
    for rejected_field in (
        "code",
        "requested_switches",
        "completed_switches",
        "wheel_events",
        "switches",
        "locator",
    ):
        assert rejected_field not in failure
    serialized = (
        response.get_data(as_text=True) + log_path.read_text(encoding="utf-8")
    ).casefold()
    assert "video_switch_xpath" not in serialized
    assert "xpath=" not in serialized
    assert "private comment content" not in serialized
    assert "987654321012345678" not in serialized
    assert "c" * 64 not in serialized


def test_block_strategy_execution_uses_one_combined_connection_per_profile(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 2,
                        "actions": [
                            {
                                "id": "pause-1",
                                "type": "pause",
                                "params": {"duration_seconds": [0, 0]},
                            }
                        ],
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-1", "profile-2")
    ]
    release_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-1", "profile-2"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda results, *, request_close=False: release_calls.append(
            (results, request_close)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda *_args: pytest.fail("separate page preparation is forbidden"),
    )
    calls = []

    def fake_combined(ws_url, target_url, strategy, elements, patterns, resolver):
        calls.append((ws_url, target_url, strategy["id"]))
        return {
            "status": "ok",
            "actions": [],
            "page_recoveries": [{"reason": "target_detached"}],
            "current_url": target_url,
            "closed_tabs": 1,
            "stages": [
                {"stage": "navigate", "status": "ok"},
                {"stage": "execute_actions", "status": "ok"},
            ],
        }

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_combined,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "windows": [
                {"profile_id": "profile-1"},
                {"profile_id": "profile-2"},
            ],
        },
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [item["status"] for item in results] == ["ok", "ok"]
    assert sorted(calls) == [
        ("ws://profile-1", "https://www.tiktok.com/", "canonical"),
        ("ws://profile-2", "https://www.tiktok.com/", "canonical"),
    ]
    assert release_calls == [(sessions, False)]
    assert results[0]["page_recoveries"] == [{"reason": "target_detached"}]
    assert [stage["stage"] for stage in results[0]["stages"]].count(
        "execute_actions"
    ) == 1


def test_block_strategy_execution_keeps_other_profile_success_on_disconnect(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 2,
                        "actions": [
                            {
                                "id": "pause-1",
                                "type": "pause",
                                "params": {"duration_seconds": [0, 0]},
                            }
                        ],
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-ok", "profile-bad")
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-ok", "profile-bad"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda *_args: pytest.fail("separate page preparation is forbidden"),
    )

    def fake_combined(ws_url, target_url, strategy, elements, patterns, resolver):
        if ws_url == "ws://profile-bad":
            raise RuntimeError("browser disconnected")
        return {
            "status": "ok",
            "actions": [],
            "page_recoveries": [],
            "current_url": target_url,
            "closed_tabs": 1,
            "stages": [
                {"stage": "navigate", "status": "ok"},
                {"stage": "execute_actions", "status": "ok"},
            ],
        }

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_combined,
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "windows": [
                {"profile_id": "profile-ok"},
                {"profile_id": "profile-bad"},
            ],
        },
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert [item["status"] for item in results] == ["ok", "failed"]
    assert results[1]["error"] == "browser disconnected"


def test_block_strategy_lifecycle_isolated_and_logged_without_secrets(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [
                    {
                        "id": "canonical",
                        "name": "Canonical",
                        "run_mode": "once",
                        "batch_size": 2,
                        "actions": [
                            {
                                "id": "scroll-1",
                                "type": "scroll_down",
                                "params": {
                                    "distance": 120,
                                    "total_count": [1, 1],
                                    "burst_count": [1, 1],
                                    "interval_seconds": [0, 0],
                                },
                            }
                        ],
                    }
                ],
            }
        }
    )
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": (
                f"ws://127.0.0.1:5500{index}/devtools/browser/"
                f"{profile_id}?api_key=api-key"
            ),
            "error": "",
        }
        for index, profile_id in enumerate(("profile-1", "profile-2"), start=1)
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-1", "profile-2"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )
    sensitive_values = (
        "plain-token",
        "plain-secret",
        "plain-password",
        "session-cookie",
        "plain-credential",
        "dXNlcjpwYXNz",
        "plain-bearer",
        "route-access-key",
        "ROUTE-BASIC",
        "ROUTE-SPACE-TOKEN",
        "ROUTE-COOKIE",
        "ROUTE-CSRF",
        "ROUTE-ALPHA",
        "ROUTE-BETA",
    )

    def fake_combined(ws_url, target_url, *_args):
        if "profile-2" in ws_url:
            raise RuntimeError(
                "browser disconnected; token=plain-token; "
                "secret=plain-secret; password=plain-password; "
                "cookie=session-cookie; credential=plain-credential; "
                "Authorization: Basic dXNlcjpwYXNz; "
                "Bearer plain-bearer; at "
                "ws://127.0.0.1:55002/devtools/browser/profile-2"
                "?api_key=api-key; access_key_id=route-access-key; "
                "Authorization=Basic ROUTE-BASIC; "
                "token ROUTE-SPACE-TOKEN; "
                'credential="ROUTE-ALPHA ROUTE-BETA"; '
                "Cookie: session=ROUTE-COOKIE; csrf=ROUTE-CSRF"
            )
        return combined_strategy_result(
            target_url,
            actions=[],
            page_recoveries=[
                {
                    "profile_id": "profile-1",
                    "action_id": "scroll-1",
                    "action_index": 1,
                    "action_type": "scroll_down",
                    "old_page_origin": "https://www.tiktok.com",
                    "new_page_origin": "https://www.tiktok.com",
                    "retry": 1,
                    "status": "recovered",
                    "cookies": [{"name": "session", "value": "api-key"}],
                    "password": "api-key",
                    "ws_url": (
                        "ws://127.0.0.1:55001/devtools/browser/profile-1"
                    ),
                }
            ],
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_combined,
    )
    original_record_browser_log = app_module.record_browser_log
    execute_log_payloads = []

    def assert_public_then_record(operation, payload):
        if operation == "execute_strategy":
            serialized = json.dumps(payload, ensure_ascii=False).casefold()
            for value in sensitive_values:
                assert value.casefold() not in serialized
            assert "api-key" not in serialized
            assert "devtools/browser" not in serialized
            execute_log_payloads.append(payload)
        original_record_browser_log(operation, payload)

    monkeypatch.setattr(
        app_module, "record_browser_log", assert_public_then_record
    )

    response = create_app().test_client().post(
        "/api/browser/execute-strategy",
        json={
            "strategy_id": "canonical",
            "windows": [
                {"profile_id": "profile-1"},
                {"profile_id": "profile-2"},
            ],
        },
    )

    assert response.status_code == 200
    results = response.get_json()["results"]
    assert results[0]["status"] == "ok"
    recovery = results[0]["page_recoveries"][0]
    assert recovery["profile_id"] == "***le-1"
    assert recovery["action_id"] == "scroll-1"
    assert recovery["old_page_origin"] == "https://www.tiktok.com"
    assert recovery["retry"] == 1
    assert "cookies" not in recovery
    assert "password" not in recovery
    assert "ws_url" not in recovery
    assert results[1]["status"] == "failed"
    assert results[1]["profile_id"] == "***le-2"
    assert "browser disconnected" in results[1]["error"]

    assert len(execute_log_payloads) == 1
    response_text = response.get_data(as_text=True).casefold()
    assert "api-key" not in response_text
    assert "devtools/browser" not in response_text
    for value in sensitive_values:
        assert value.casefold() not in response_text
    log_text = log_path.read_text(encoding="utf-8")
    assert "profile-1" not in log_text
    assert "***le-1" in log_text
    assert "scroll-1" in log_text
    assert "https://www.tiktok.com" in log_text
    assert "api-key" not in log_text.casefold()
    assert "devtools/browser" not in log_text.casefold()
    for value in sensitive_values:
        assert value.casefold() not in log_text.casefold()


def test_batch_runner_uses_one_combined_connection_per_profile(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [],
            }
        }
    )
    task_id = "combined-batch"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {"id": task_id}
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": f"ws://{profile_id}",
            "error": "",
        }
        for profile_id in ("profile-1", "profile-2")
    ]
    release_calls = []
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-1", "profile-2"]),
        ),
    )
    monkeypatch.setattr(
        app_module,
        "release_browser_session_results",
        lambda results, *, request_close=False: release_calls.append(
            (results, request_close)
        ),
    )
    monkeypatch.setattr(
        app_module,
        "prepare_browser_page",
        lambda *_args: pytest.fail("separate page preparation is forbidden"),
    )
    calls = []

    def fake_combined(ws_url, target_url, strategy, elements, patterns, resolver):
        calls.append((ws_url, target_url, strategy["id"]))
        return {
            "status": "ok",
            "actions": [],
            "page_recoveries": [{"reason": "page_closed"}],
            "current_url": target_url,
            "closed_tabs": 1,
            "stages": [
                {"stage": "navigate", "status": "ok"},
                {"stage": "execute_actions", "status": "ok"},
            ],
        }

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_combined,
    )

    app_module.run_browser_batch_task(
        create_app(),
        task_id,
        [{"profile_id": "profile-1"}, {"profile_id": "profile-2"}],
        2,
        {
            "id": "canonical",
            "run_mode": "once",
            "actions": [
                {"id": "scroll-1", "type": "scroll_down", "params": {}}
            ],
        },
        "https://www.tiktok.com/",
    )

    results = app_module.BROWSER_BATCH_TASKS[task_id]["results"]
    assert [item["status"] for item in results] == ["ok", "ok"]
    assert sorted(calls) == [
        ("ws://profile-1", "https://www.tiktok.com/", "canonical"),
        ("ws://profile-2", "https://www.tiktok.com/", "canonical"),
    ]
    assert release_calls == [(sessions, True)]
    assert results[0]["page_recoveries"] == [{"reason": "page_closed"}]
    assert [stage["stage"] for stage in results[0]["stages"]].count(
        "execute_actions"
    ) == 1


def test_batch_lifecycle_isolated_and_logged_from_public_payload(
    monkeypatch, tmp_path
):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setenv("APP_CONFIG_PATH", str(tmp_path / "config.json"))
    log_path = tmp_path / "browser.jsonl"
    monkeypatch.setattr(app_module, "BROWSER_LOG_PATH", log_path)
    save_settings(
        {
            "browser": {
                "strategy_schema_version": 2,
                "action_elements": {},
                "interaction_patterns": [],
                "block_strategies": [],
            }
        }
    )
    task_id = "lifecycle-batch"
    app_module.BROWSER_BATCH_TASKS.clear()
    app_module.BROWSER_BATCH_TASKS[task_id] = {"id": task_id}
    sessions = [
        {
            "profile_id": profile_id,
            "status": "ready",
            "stage": "session_start",
            "attempts": 1,
            "ws_url": (
                f"ws://127.0.0.1:5510{index}/devtools/browser/"
                f"{profile_id}?api_key=batch-api-key"
            ),
            "error": "",
        }
        for index, profile_id in enumerate(("profile-1", "profile-2"), start=1)
    ]
    monkeypatch.setattr(
        app_module,
        "ensure_browser_profile_sessions",
        lambda *_args, **_kwargs: (
            sessions,
            window_tiler_result(["profile-1", "profile-2"]),
        ),
    )
    monkeypatch.setattr(
        app_module, "release_browser_session_results", lambda *_args, **_kwargs: None
    )
    sensitive_values = (
        "batch-token",
        "batch-secret",
        "batch-password",
        "batch-cookie",
        "batch-credential",
        "YmF0Y2g6cGFzcw==",
        "batch-api-key",
        "batch-bearer",
    )

    def fake_combined(ws_url, target_url, *_args):
        if "profile-2" in ws_url:
            raise RuntimeError(
                "browser disconnected; token=batch-token; "
                "secret=batch-secret; password=batch-password; "
                "cookie=batch-cookie; credential=batch-credential; "
                "api_key=batch-api-key; "
                "Authorization: Basic YmF0Y2g6cGFzcw==; "
                "Bearer batch-bearer; at "
                "ws://127.0.0.1:55102/devtools/browser/profile-2"
            )
        return combined_strategy_result(
            target_url,
            actions=[],
            page_recoveries=[
                {
                    "profile_id": "profile-1",
                    "action_id": "scroll-1",
                    "action_index": 1,
                    "action_type": "scroll_down",
                    "old_page_origin": "https://www.tiktok.com",
                    "new_page_origin": "https://www.tiktok.com",
                    "retry": 1,
                    "status": "recovered",
                }
            ],
        )

    monkeypatch.setattr(
        "browser_strategy_runtime.run_prepared_block_strategy_on_cdp",
        fake_combined,
    )
    original_record_browser_log = app_module.record_browser_log
    batch_log_payloads = []

    def assert_public_then_record(operation, payload):
        if operation == "batch_task":
            serialized = json.dumps(payload, ensure_ascii=False).casefold()
            for value in sensitive_values:
                assert value.casefold() not in serialized
            assert "devtools/browser" not in serialized
            batch_log_payloads.append(payload)
        original_record_browser_log(operation, payload)

    monkeypatch.setattr(
        app_module, "record_browser_log", assert_public_then_record
    )

    app_module.run_browser_batch_task(
        create_app(),
        task_id,
        [{"profile_id": "profile-1"}, {"profile_id": "profile-2"}],
        2,
        {
            "id": "canonical",
            "run_mode": "once",
            "actions": [
                {"id": "scroll-1", "type": "scroll_down", "params": {}}
            ],
        },
        "https://www.tiktok.com/",
    )

    results = app_module.BROWSER_BATCH_TASKS[task_id]["results"]
    assert results[0]["status"] == "ok"
    assert results[0]["page_recoveries"][0]["retry"] == 1
    assert results[1]["status"] == "failed"
    assert results[1]["profile_id"] == "***le-2"
    assert "browser disconnected" in results[1]["error"]

    assert len(batch_log_payloads) == 1
    log_entry = json.loads(log_path.read_text(encoding="utf-8"))
    assert log_entry["operation"] == "batch_task"
    logged_results = log_entry["payload"]["results"]
    recovery = logged_results[0]["page_recoveries"][0]
    assert recovery["profile_id"] == "***le-1"
    assert recovery["action_id"] == "scroll-1"
    assert recovery["old_page_origin"] == "https://www.tiktok.com"
    assert recovery["retry"] == 1
    assert logged_results[1]["profile_id"] == "***le-2"
    assert "browser disconnected" in logged_results[1]["error"]
    log_text = log_path.read_text(encoding="utf-8").casefold()
    for value in sensitive_values:
        assert value.casefold() not in log_text
    assert "devtools/browser" not in log_text
    app_module.BROWSER_BATCH_TASKS.clear()


def test_block_strategy_text_resolver_handles_fixed_and_brand_copy(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    calls = []
    monkeypatch.setattr(
        app_module,
        "collect_strategy_comments",
        lambda _data_dir, brand_id="": calls.append(brand_id)
        or [{"body": "Brand copy", "tags": ["#one"]}],
    )
    resolver = app_module.build_strategy_text_resolver(
        "unused", rng=type("Rng", (), {"choice": lambda _self, values: values[0]})()
    )

    fixed = resolver(
        {"params": {"content": {"source": "fixed", "text": "Fixed", "brand_id": ""}}}
    )
    generated = resolver(
        {
            "params": {
                "content": {
                    "source": "generated_comment",
                    "text": "",
                    "brand_id": "brand-1",
                }
            }
        }
    )

    assert fixed == "Fixed"
    assert generated == "Brand copy\n\n#one"
    assert calls == ["brand-1"]


def test_v2_content_library_provider_returns_metadata_only(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    monkeypatch.setattr(
        app_module,
        "list_brands",
        lambda _data_dir: [
            {"id": "ofs", "name": "OFS", "copy_count": 40, "updated_at": "secret"}
        ],
    )

    provider = app_module.build_execution_v2_content_library_provider("unused")

    assert asyncio.run(provider()) == [
        {"id": "ofs", "name": "OFS", "copy_count": 40}
    ]


def test_v2_text_resolver_picks_from_requested_library(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    calls = []
    monkeypatch.setattr(
        app_module,
        "list_copy_items",
        lambda _data_dir, brand_id: calls.append(brand_id)
        or [{"body": "first"}, {"body": "second"}],
    )
    resolver = app_module.build_execution_v2_text_resolver(
        "unused",
        rng=type("Rng", (), {"choice": lambda _self, values: values[-1]})(),
    )

    assert asyncio.run(resolver({"content_library_id": "ofs"})) == "second"
    assert calls == ["ofs"]


def test_v2_text_resolver_rejects_missing_or_empty_library(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    from execution_v2.actions import ActionExecutionError

    monkeypatch.setattr(app_module, "list_copy_items", lambda _data_dir, _brand_id: [])
    resolver = app_module.build_execution_v2_text_resolver("unused")

    for action in ({"content_library_id": ""}, {"content_library_id": "empty"}):
        with pytest.raises(ActionExecutionError, match="content_library_unavailable") as error:
            asyncio.run(resolver(action))
        assert getattr(error.value, "code", "") == "content_library_unavailable"


def _reset_pattern_recording_state(app_module):
    app_module.ACTIVE_PATTERN_RECORDINGS.clear()
    app_module.ACTIVE_BROWSER_SESSIONS.clear()
    app_module.BROWSER_SESSION_LEASES.clear()


def test_pattern_recording_start_requires_one_open_profile_and_releases_lease(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    prepared = []
    monkeypatch.setattr(
        "browser_pattern_recorder.prepare_recording",
        lambda ws_url, recording_id, pattern_type: prepared.append(
            (ws_url, recording_id, pattern_type)
        )
        or {
            "recording_id": recording_id,
            "type": pattern_type,
            "status": "ready",
            "sample_count": 0,
        },
    )
    client = create_app().test_client()

    response = client.post(
        "/api/browser/pattern-recordings/start",
        json={"windows": [{"profile_id": "profile-1"}], "type": "mouse"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    recording_id = payload["recording_id"]
    assert prepared == [("ws://profile-1", recording_id, "mouse")]
    assert app_module.ACTIVE_PATTERN_RECORDINGS[recording_id] == {
        "profile_id": "profile-1",
        "ws_url": "ws://profile-1",
        "type": "mouse",
    }
    assert app_module.BROWSER_SESSION_LEASES == {}
    assert "ws_url" not in json.dumps(payload)
    _reset_pattern_recording_state(app_module)


def test_pattern_recording_start_never_holds_state_lock_during_cdp_prepare(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    lock_was_available = []

    def fake_prepare(_ws_url, recording_id, pattern_type):
        acquired = app_module.ACTIVE_PATTERN_RECORDINGS_LOCK.acquire(blocking=False)
        lock_was_available.append(acquired)
        if acquired:
            app_module.ACTIVE_PATTERN_RECORDINGS_LOCK.release()
        return {
            "recording_id": recording_id,
            "type": pattern_type,
            "status": "ready",
            "sample_count": 0,
        }

    monkeypatch.setattr("browser_pattern_recorder.prepare_recording", fake_prepare)

    response = create_app().test_client().post(
        "/api/browser/pattern-recordings/start",
        json={"windows": [{"profile_id": "profile-1"}], "type": "keyboard"},
    )

    assert response.status_code == 200
    assert lock_was_available == [True]
    _reset_pattern_recording_state(app_module)


def test_pattern_recording_reservation_rejects_concurrent_start_for_same_profile(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    prepare_entered = threading.Event()
    allow_prepare_to_finish = threading.Event()
    prepare_calls = []

    def slow_prepare(_ws_url, recording_id, pattern_type):
        prepare_calls.append(recording_id)
        prepare_entered.set()
        assert allow_prepare_to_finish.wait(timeout=5)
        return {
            "recording_id": recording_id,
            "type": pattern_type,
            "status": "ready",
            "sample_count": 0,
        }

    monkeypatch.setattr("browser_pattern_recorder.prepare_recording", slow_prepare)
    first_result = {}

    def start_first():
        first_result["response"] = create_app().test_client().post(
            "/api/browser/pattern-recordings/start",
            json={"windows": [{"profile_id": "profile-1"}], "type": "mouse"},
        )

    worker = threading.Thread(target=start_first)
    worker.start()
    assert prepare_entered.wait(timeout=5)
    second = create_app().test_client().post(
        "/api/browser/pattern-recordings/start",
        json={"windows": [{"profile_id": "profile-1"}], "type": "keyboard"},
    )
    allow_prepare_to_finish.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert second.status_code == 409
    assert first_result["response"].status_code == 200
    assert len(prepare_calls) == 1
    assert len(app_module.ACTIVE_PATTERN_RECORDINGS) == 1
    _reset_pattern_recording_state(app_module)


def test_pattern_recording_prepare_failure_clears_reservation_and_lease(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    monkeypatch.setattr(
        "browser_pattern_recorder.prepare_recording",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("inject failed")),
    )

    response = create_app().test_client().post(
        "/api/browser/pattern-recordings/start",
        json={"windows": [{"profile_id": "profile-1"}], "type": "mouse"},
    )

    assert response.status_code == 400
    assert app_module.ACTIVE_PATTERN_RECORDINGS == {}
    assert app_module.BROWSER_SESSION_LEASES == {}
    _reset_pattern_recording_state(app_module)


def test_pattern_recording_commit_race_cleans_injected_page_state(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    cleaned = []

    def prepare_then_replace(_ws_url, recording_id, pattern_type):
        with app_module.ACTIVE_PATTERN_RECORDINGS_LOCK:
            app_module.ACTIVE_PATTERN_RECORDINGS[recording_id] = {
                "profile_id": "replacement",
                "ws_url": "ws://replacement",
                "type": pattern_type,
            }
        return {
            "recording_id": recording_id,
            "type": pattern_type,
            "status": "ready",
            "sample_count": 0,
        }

    monkeypatch.setattr("browser_pattern_recorder.prepare_recording", prepare_then_replace)
    monkeypatch.setattr(
        "browser_pattern_recorder.finish_recording",
        lambda ws_url, recording_id: cleaned.append((ws_url, recording_id)),
    )

    response = create_app().test_client().post(
        "/api/browser/pattern-recordings/start",
        json={"windows": [{"profile_id": "profile-1"}], "type": "mouse"},
    )

    assert response.status_code == 400
    recording_id = next(iter(app_module.ACTIVE_PATTERN_RECORDINGS))
    assert cleaned == [("ws://profile-1", recording_id)]
    assert app_module.BROWSER_SESSION_LEASES == {}
    _reset_pattern_recording_state(app_module)


@pytest.mark.parametrize(
    "body",
    [
        {"windows": [], "type": "mouse"},
        {"windows": [{"profile_id": "one"}, {"profile_id": "two"}], "type": "mouse"},
        {"windows": [{"profile_id": "one"}], "type": "text"},
    ],
)
def test_pattern_recording_start_rejects_invalid_selection_or_type(monkeypatch, body):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)

    response = create_app().test_client().post(
        "/api/browser/pattern-recordings/start", json=body
    )

    assert response.status_code == 400
    assert app_module.ACTIVE_PATTERN_RECORDINGS == {}


def test_pattern_recording_start_rejects_profile_that_is_not_open(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)

    response = create_app().test_client().post(
        "/api/browser/pattern-recordings/start",
        json={"windows": [{"profile_id": "closed"}], "type": "keyboard"},
    )

    assert response.status_code == 400
    assert "已打开" in response.get_json()["error"]
    assert app_module.ACTIVE_PATTERN_RECORDINGS == {}


def test_pattern_recording_status_rechecks_matching_session_and_releases_lease(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    app_module.ACTIVE_PATTERN_RECORDINGS["rec-1"] = {
        "profile_id": "profile-1",
        "ws_url": "ws://profile-1",
        "type": "mouse",
    }
    calls = []
    monkeypatch.setattr(
        "browser_pattern_recorder.read_recording",
        lambda ws_url, recording_id: calls.append((ws_url, recording_id))
        or {
            "recording_id": recording_id,
            "type": "mouse",
            "status": "recording",
            "sample_count": 4,
        },
    )

    response = create_app().test_client().get(
        "/api/browser/pattern-recordings/rec-1"
    )

    assert response.status_code == 200
    assert response.get_json()["sample_count"] == 4
    assert calls == [("ws://profile-1", "rec-1")]
    assert app_module.BROWSER_SESSION_LEASES == {}
    assert "rec-1" in app_module.ACTIVE_PATTERN_RECORDINGS
    _reset_pattern_recording_state(app_module)


def test_pattern_recording_finish_returns_temporary_sample_and_forgets_state(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    app_module.ACTIVE_PATTERN_RECORDINGS["rec-1"] = {
        "profile_id": "profile-1",
        "ws_url": "ws://profile-1",
        "type": "keyboard",
    }
    sample = {
        "intervals_ms": [30.0, 40.0],
        "hold_ms": [10.0, 12.0],
        "sample_count": 2,
        "total_duration_ms": 82.0,
    }
    monkeypatch.setattr(
        "browser_pattern_recorder.finish_recording",
        lambda ws_url, recording_id: {
            "recording_id": recording_id,
            "type": "keyboard",
            "status": "finished",
            "sample": sample,
        },
    )

    response = create_app().test_client().post(
        "/api/browser/pattern-recordings/rec-1/stop"
    )

    assert response.status_code == 200
    assert response.get_json()["sample"] == sample
    assert app_module.ACTIVE_PATTERN_RECORDINGS == {}
    assert app_module.BROWSER_SESSION_LEASES == {}
    _reset_pattern_recording_state(app_module)


@pytest.mark.parametrize("operation", ["status", "finish"])
def test_pattern_recording_changed_cdp_session_is_context_invalid(monkeypatch, operation):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://replacement"
    app_module.ACTIVE_PATTERN_RECORDINGS["rec-1"] = {
        "profile_id": "profile-1",
        "ws_url": "ws://original",
        "type": "mouse",
    }
    endpoint = "/api/browser/pattern-recordings/rec-1"
    if operation == "finish":
        endpoint += "/stop"
        response = create_app().test_client().post(endpoint)
    else:
        response = create_app().test_client().get(endpoint)

    assert response.status_code == 409
    assert response.get_json()["error"] == "录制上下文已失效"
    assert app_module.ACTIVE_PATTERN_RECORDINGS == {}
    _reset_pattern_recording_state(app_module)


def test_pattern_recording_navigation_loss_is_context_invalid(monkeypatch):
    app_module = importlib.import_module("gateway.app")
    _reset_pattern_recording_state(app_module)
    app_module.ACTIVE_BROWSER_SESSIONS["profile-1"] = "ws://profile-1"
    app_module.ACTIVE_PATTERN_RECORDINGS["rec-1"] = {
        "profile_id": "profile-1",
        "ws_url": "ws://profile-1",
        "type": "keyboard",
    }
    monkeypatch.setattr(
        "browser_pattern_recorder.read_recording",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("录制上下文已失效")),
    )

    response = create_app().test_client().get(
        "/api/browser/pattern-recordings/rec-1"
    )

    assert response.status_code == 409
    assert response.get_json()["error"] == "录制上下文已失效"
    assert app_module.ACTIVE_PATTERN_RECORDINGS == {}
    assert app_module.BROWSER_SESSION_LEASES == {}
    _reset_pattern_recording_state(app_module)


def test_selector_probe_blueprint_uses_lazy_injected_factories():
    calls = []

    class Store:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def list_runs(self, *, limit, offset):
            calls.append(("runs", limit, offset))
            return []

        def list_versions(self, *, limit, offset):
            calls.append(("versions", limit, offset))
            return []

    class Registry:
        def get_active(self):
            calls.append(("active",))
            return {
                "version": "sel-app",
                "bundle_hash": "sha256:" + "b" * 64,
                "elements": {
                    "comment_entry": {
                        "scope": "active_video",
                        "locators": [
                            {
                                "id": "loc-app",
                                "type": "role",
                                "role": "button",
                                "name": "Comments",
                                "name_mode": "exact",
                                "enabled": True,
                            }
                        ],
                    }
                },
            }

    app = create_app(
        {
            "TESTING": True,
            "SELECTOR_PROBE_STORE_FACTORY": Store,
            "SELECTOR_PROBE_REGISTRY_FACTORY": Registry,
            "SELECTOR_PROBE_RUN_DISPATCHER": lambda _request_id, _done: True,
        }
    )
    assert calls == []

    client = app.test_client()
    active = client.get("/api/selector-probe/active")
    runs = client.get("/api/selector-probe/runs")

    assert active.status_code == 200
    assert active.get_json()["version"] == "sel-app"
    assert runs.status_code == 200
    assert calls == [("active",), ("runs", 50, 0)]


def test_default_selector_probe_dispatcher_does_not_connect_during_app_create(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        "selector_probe.blueprint._dispatcher_redis_client",
        lambda: calls.append("redis"),
    )

    app = create_app({"TESTING": True})

    assert app.config["SELECTOR_PROBE_RUN_DISPATCHER"]
    assert calls == []
