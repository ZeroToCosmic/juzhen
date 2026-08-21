from __future__ import annotations

import json

import pytest

from gateway.app import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(
        {
            "LOCAL_DIRECT_MODE": True,
            "TESTING": True,
            "TIKTOK_STATS_DB_PATH": tmp_path / "stats.db",
            "AGENT_DEVICE_ID": "",
        }
    )
    return app.test_client()


@pytest.mark.parametrize(
    ("path", "marker", "active"),
    [
        ("/console/overview", 'id="console-overview"', ">运行总览</a>"),
        ("/console/tasks", 'id="console-tasks"', ">任务执行</a>"),
        ("/console/actions", 'id="console-actions"', ">动作库</a>"),
        ("/console/publishing", 'id="console-publishing"', ">内容发布</a>"),
        ("/console/collection", 'id="console-collection"', ">数据采集</a>"),
        ("/console/collection-results", 'id="console-collection-results"', ">采集结果</a>"),
        ("/console/accounts-windows", 'id="console-accounts-windows"', ">账号与窗口</a>"),
        ("/console/page-elements", 'id="console-page-elements"', ">页面元素</a>"),
        ("/console/receipts", 'id="console-receipts"', ">回执与证据</a>"),
    ],
)
def test_console_pages_render_shared_shell_and_one_active_item(client, path, marker, active):
    response = client.get(path)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert marker in html
    assert 'class="dashboard-sidebar"' in html
    assert 'name="csrf-token"' in html
    active_start = html.rfind("<a", 0, html.index(active) + len(active))
    active_tag = html[active_start:html.index(">", active_start)]
    assert 'aria-current="page"' in active_tag
    assert html.count('aria-current="page"') == 1


def test_console_index_redirects_to_overview(client):
    response = client.get("/console")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/console/overview")


def test_console_actions_new_browser_strategy_uses_native_editor_route(client):
    html = client.get("/console/actions").get_data(as_text=True)

    assert 'href="/console/actions/browser-strategies/new"' in html
    assert ">新建浏览器策略</a>" in html
    assert "/browser-v2?view=strategies" not in html


def test_console_actions_new_comment_campaign_uses_native_create_route(client):
    html = client.get("/console/actions").get_data(as_text=True)

    assert 'href="/console/actions/comment-campaigns/new"' in html
    assert ">新建评论 Campaign</a>" in html


def test_console_comment_trees_is_native_action_library_page(client):
    response = client.get("/console/actions/comment-trees")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="console-comment-trees"' in html
    assert 'class="dashboard-sidebar"' in html
    assert 'href="/console/actions"' in html
    assert "console_comment_trees.css" in html
    assert "comment_tree_editor.js" in html
    assert "console_comment_trees.js" in html
    assert html.index("comment_tree_editor.js") < html.index("console_comment_trees.js")
    assert 'accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"' in html
    assert 'id="campaign-drawer"' not in html

    for workspace_id in (
        "comment-tree-list-workspace",
        "comment-tree-editor-workspace",
        "comment-tree-import-workspace",
    ):
        assert f'id="{workspace_id}"' in html

    for marker in (
        "返回动作库",
        ">刷新<",
        ">新建评论树<",
        ">Excel 导入<",
        "搜索",
        "模式",
        "状态",
        "启用中的评论树",
        "已停用的评论树",
        'id="comment-trees-status"',
    ):
        assert marker in html


def test_action_library_links_to_comment_tree_management(client):
    html = client.get("/console/actions").get_data(as_text=True)

    assert 'href="/console/actions/comment-trees"' in html
    assert ">评论树管理</a>" in html


def test_console_comment_campaign_create_is_native_console_page(client):
    response = client.get("/console/actions/comment-campaigns/new")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="console-comment-campaign-create"' in html
    assert 'class="dashboard-sidebar"' in html
    assert 'name="csrf-token"' in html
    assert 'href="/console/actions"' in html
    assert "返回动作库" in html
    assert "创建 Campaign" in html
    assert "console_comment_campaign_create.css" in html
    assert "console_comment_campaign_create.js" in html
    for marker in (
        'id="campaign-create-form"',
        'id="campaign-name"',
        'id="campaign-target-reference"',
        'id="campaign-mode"',
        'id="campaign-template"',
        'id="campaign-batch-size"',
        'id="campaign-profile-body"',
        'id="campaign-create-status"',
    ):
        assert marker in html
    assert html.count('aria-current="page"') == 1
    active_start = html.rfind("<a", 0, html.index(">动作库</a>") + len(">动作库</a>"))
    active_tag = html[active_start:html.index(">", active_start)]
    assert 'aria-current="page"' in active_tag
    for legacy_marker in (
        'id="comment-campaign-app"',
        'id="comment-campaign-list"',
        'id="comment-campaign-preview"',
        'id="comment-campaign-approvals"',
        'id="campaign-drawer"',
    ):
        assert legacy_marker not in html


def test_console_runtime_redirects_to_local_runtime_section(client):
    response = client.get("/console/runtime")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/console/overview#local-runtime")


def test_overview_owns_local_runtime_and_sidebar_has_no_runtime_item(client):
    html = client.get("/console/overview").get_data(as_text=True)

    assert 'id="local-runtime"' in html
    assert 'href="/console/runtime"' not in html
    assert ">运行环境</a>" not in html


def test_console_tasks_api_is_device_scoped_and_read_only_when_unconfigured(client):
    response = client.get("/console/api/tasks")

    assert response.status_code == 200
    assert response.get_json() == {
        "connected": False,
        "reason": "device_not_configured",
        "tasks": [],
    }


def test_console_settings_renders_native_workspace(client):
    response = client.get("/console/settings")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="console-settings"' in html
    assert 'data-console-settings-workspace' in html
    assert "console_settings.js" in html
    assert "console_settings_page.js" in html
    assert "console_settings.css" in html
    assert 'aria-current="page"' in html
    assert response.headers.get("Location") is None
    assert "Selector Probe" not in html
    assert "中控同步" not in html
    assert "排空后重启" not in html

    for label in ("代理与网络", "浏览器与 AdsPower", "Buffer 与发布", "R2 存储", "数据采集", "模型服务"):
        assert label in html


def test_console_page_elements_is_a_native_page_and_keeps_picker_contract(client):
    response = client.get("/console/page-elements")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert 'id="console-page-elements"' in html
    assert 'id="v2-picker-form"' in html
    assert 'id="v2-elements-list"' in html
    assert "page_elements_controller.js" in html
    assert "console_page_elements.js" in html
    assert "Selector Probe" not in html
    assert "中控同步" not in html


@pytest.mark.parametrize(
    ("path", "mode", "strategy_id"),
    [
        ("/console/actions/browser-strategies/new", "new", None),
        ("/console/actions/browser-strategies/strategy-1/edit", "edit", "strategy-1"),
    ],
)
def test_console_browser_strategy_editor_is_a_native_console_page(client, path, mode, strategy_id):
    response = client.get(path)

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    bootstrap_start = html.index('<script id="console-browser-strategy-bootstrap"')
    bootstrap_start = html.index(">", bootstrap_start) + 1
    bootstrap_end = html.index("</script>", bootstrap_start)
    bootstrap = json.loads(html[bootstrap_start:bootstrap_end])

    assert bootstrap["mode"] == mode
    assert bootstrap["strategy_id"] == strategy_id
    assert 'class="dashboard-sidebar"' in html
    assert 'name="csrf-token"' in html
    assert 'href="/console/actions"' in html
    assert "返回动作库" in html
    assert "保存策略" in html
    assert "策略设置" in html
    assert "动作序列" in html
    assert html.count('aria-current="page"') == 1
    active_start = html.rfind("<a", 0, html.index(">动作库</a>") + len(">动作库</a>"))
    active_tag = html[active_start:html.index(">", active_start)]
    assert 'aria-current="page"' in active_tag
    assert "browser_strategy_editor_core.js" in html
    assert "console_browser_strategy_editor.js" in html
    assert "console_browser_strategy_editor.css" in html

    for legacy_marker in (
        "V2 独立执行模块",
        "v2-tabs",
        "执行中心",
        "Profile",
        "运行历史",
    ):
        assert legacy_marker not in html


def test_console_browser_strategy_editor_round_trips_unicode_and_space_strategy_id(client):
    strategy_id = "策略 1"
    response = client.get(f"/console/actions/browser-strategies/{strategy_id}/edit")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    bootstrap_start = html.index('<script id="console-browser-strategy-bootstrap"')
    bootstrap_start = html.index(">", bootstrap_start) + 1
    bootstrap_end = html.index("</script>", bootstrap_start)
    bootstrap = json.loads(html[bootstrap_start:bootstrap_end])

    assert bootstrap["mode"] == "edit"
    assert bootstrap["strategy_id"] == strategy_id


def test_console_browser_strategy_editor_rejects_incomplete_edit_url(client):
    response = client.get("/console/actions/browser-strategies//edit")

    assert response.status_code == 404


def test_sidebar_uses_approved_module_order(client):
    html = client.get("/console/overview").get_data(as_text=True)
    labels = [
        "运行总览", "任务执行", "动作库", "内容发布", "数据采集", "采集结果",
        "账号与窗口", "页面元素", "回执与证据", "系统设置",
    ]
    positions = [html.index(f">{label}</a>") for label in labels]
    assert positions == sorted(positions)


def test_legacy_entry_points_remain_available(client):
    for path in ("/", "/?panel=publish", "/browser-v2", "/comment-campaigns", "/tiktok-stats", "/bcs"):
        assert client.get(path).status_code == 200


def test_browser_v2_deep_links_keep_csrf_aware_authenticated_fetch(client):
    html = client.get("/browser-v2?view=elements").get_data(as_text=True)

    assert 'meta name="csrf-token"' in html
    assert "management_fetch.js" in html


@pytest.mark.parametrize(
    ("path", "script", "legacy_marker"),
    [
        ("/console/actions", "console_actions.js", "维护可在指纹浏览器窗口执行的动作定义"),
        ("/console/publishing", "console_publishing.js", "发布配置</td>"),
        ("/console/accounts-windows", "console_accounts_windows.js", "账号花名册</td>"),
        ("/console/receipts", "console_receipts.js", "浏览器执行记录</td>"),
    ],
)
def test_operational_modules_do_not_render_compatibility_launcher(client, path, script, legacy_marker):
    html = client.get(path).get_data(as_text=True)

    assert script in html
    assert 'class="console-page console-module-page"' not in html
    assert legacy_marker not in html
