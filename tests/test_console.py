import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest

from gateway.app import CONTROL_PAGE_HTML, DASHBOARD_PAGE_HTML, SETTINGS_PAGE_HTML, create_app


class _HtmlNode:
    def __init__(self, tag, attrs=None, parent=None, position=-1):
        self.tag = tag
        self.attrs = dict(attrs or [])
        self.parent = parent
        self.position = position
        self.children = []
        self.text = []


class _TreeParser(HTMLParser):
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("#document")
        self._stack = [self.root]
        self._position = 0

    def handle_starttag(self, tag, attrs):
        node = _HtmlNode(tag, attrs, self._stack[-1], self._position)
        self._position += 1
        self._stack[-1].children.append(node)
        if tag not in self._VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = _HtmlNode(tag, attrs, self._stack[-1], self._position)
        self._position += 1
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                return

    def handle_data(self, data):
        self._stack[-1].text.append(data)


def _parse_html(markup):
    parser = _TreeParser()
    parser.feed(markup)
    parser.close()
    return parser.root


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _element_by_id(root, element_id):
    matches = [node for node in _walk(root) if node.attrs.get("id") == element_id]
    assert len(matches) == 1, f"expected one #{element_id}, found {len(matches)}"
    return matches[0]


def _descendants_with_attr(root, attribute):
    return [node for node in _walk(root) if node is not root and attribute in node.attrs]


def _assert_descendant(parent, child):
    assert child in set(_walk(parent)) - {parent}


def _assert_strategy_palette_contract(root):
    palette = _element_by_id(root, "browser-block-palette")
    block_nodes = _descendants_with_attr(palette, "data-block-type")
    assert all(node.tag == "button" for node in block_nodes)
    assert [node.attrs["data-block-type"] for node in block_nodes] == [
        "move",
        "click",
        "scroll_up",
        "scroll_down",
        "keyboard_input",
        "pause",
    ]


def test_proxy_pool_placeholders_use_only_reserved_documentation_credentials():
    placeholders = []
    for page in (DASHBOARD_PAGE_HTML, SETTINGS_PAGE_HTML, CONTROL_PAGE_HTML):
        placeholders.extend(
            re.findall(
                r'name="proxy_pool\.raw"[^>]*placeholder="([^"]*)"',
                page,
            )
        )

    assert placeholders == [
        "203.0.113.10:1080:example_user:example_password",
    ] * 3


def test_dashboard_removes_overview_controls():
    page = create_app().test_client().get("/").get_data(as_text=True)

    assert 'data-panel="overview"' not in page
    assert 'id="panel-overview"' not in page
    assert 'id="overview-next"' not in page
    assert 'id="overview-status"' not in page
    assert 'document.querySelector("#overview-next")' not in page
    assert 'document.querySelector("#overview-status")' not in page


def test_dashboard_opens_settings_by_default():
    page = create_app().test_client().get("/").get_data(as_text=True)

    assert '<a class="dashboard-nav-link active" href="/console/settings" aria-current="page">系统设置</a>' in page
    assert 'href="/console/settings" data-panel=' not in page
    assert '<section class="panel active" id="panel-settings">' in page
    assert '<h2 id="title">集中配置</h2>' in page


def test_dashboard_page_is_primary_workflow_console():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "Agent 自动化主控台" in page
    assert "账号与窗口" in page
    assert "系统设置" in page
    assert "内容发布" in page
    assert "任务执行" in page
    assert "/api/account/next" in page


def test_dashboard_uses_modern_apple_inspired_visual_system():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert '--app-bg: #f5f5f7' in page
    assert '-apple-system' in page
    assert 'backdrop-filter: blur(22px)' in page
    assert 'box-shadow: 0 18px 50px rgba(15, 23, 42, 0.08)' in page
    assert 'border-radius: 18px' in page
    assert 'transition: all 160ms ease' in page


def test_one_click_start_script_uses_hidden_pythonw_entry():
    root = Path(create_app().root_path).parent
    command = (root / "start_console.cmd").read_text(encoding="utf-8").lower()
    script = (root / "start_console.vbs").read_text(encoding="utf-8").lower()

    assert "wscript.exe" in command
    assert "start_console.vbs" in command
    assert "pause" not in command
    assert "pythonw.exe" in script
    assert "launcher.py" in script
    assert ", 0, false" in script
    assert "fileexists(launcherpath)" in script
    assert "on error resume next" in script
    assert "err.number" in script
    assert "unable to start launcher. reinstall dependencies and try again." in script


def test_hidden_entry_keeps_dashboard_owned_by_launcher():
    root = Path(create_app().root_path).parent
    command = (root / "start_console.cmd").read_text(encoding="utf-8")
    launcher = (root / "launcher.py").read_text(encoding="utf-8")

    assert "http://127.0.0.1:5000/" in launcher
    assert "Start-Process 'http://127.0.0.1:5000/'" not in command


def test_hidden_entry_vbs_parses_with_windows_script_host(tmp_path):
    cscript = shutil.which("cscript.exe")
    if cscript is None:
        pytest.skip("cscript.exe is unavailable")

    root = Path(create_app().root_path).parent
    source = (root / "start_console.vbs").read_bytes()
    option_explicit = b"Option Explicit"
    option_end = source.index(b"\n", source.index(option_explicit)) + 1
    script = tmp_path / "start_console.vbs"
    script.write_bytes(source[:option_end] + b"WScript.Quit 0\r\n" + source[option_end:])

    completed = subprocess.run(
        [cscript, "//nologo", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_launcher_cannot_start_before_environment_check(monkeypatch):
    from launcher import LauncherApp

    warnings = []
    monkeypatch.setattr("launcher.messagebox.showwarning", lambda title, message: warnings.append((title, message)))

    class NotChecked:
        check_completed = False

    LauncherApp.start(NotChecked())

    assert warnings


def test_status_api_reports_configuration_completeness(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "proxy": {
            "host": "proxy.example.com",
            "port": "8080",
            "username": "user",
            "password": "secret"
          },
          "services": {
            "ipinfo_url": "https://example.com/ip.json",
            "buffer_graphql_url": "https://example.com/graphql"
          },
          "browser": {
            "cdp_url": "http://127.0.0.1:9222"
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.get_json() == {
        "service": {"running": True},
        "config": {
            "proxy_configured": True,
            "services_configured": True,
            "browser_configured": True,
        },
        "browser": {
            "cdp_url": "http://127.0.0.1:9222",
            "task_goal": "",
        },
    }


def test_status_api_accepts_adspower_configuration_without_manual_cdp_url(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"adspower":{"base_url":"http://local.adspower.net:50325",'
        '"api_key":"adspower-key"},'
        '"browser":{"default_url":"https://www.tiktok.com/"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    response = create_app().test_client().get("/api/status")

    assert response.status_code == 200
    assert response.get_json()["config"]["browser_configured"] is True


def test_dashboard_has_bulk_proxy_pool_configuration():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "批量代理 IP 池" in page
    assert "203.0.113.10:1080:example_user:example_password" in page
    assert 'name="proxy_pool.protocol"' in page
    assert '<option value="socks5">SOCKS5</option>' in page
    assert '<option value="http">HTTP / HTTPS</option>' in page


def test_status_api_treats_proxy_pool_as_proxy_configured(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "proxy_pool": {
            "items": [
              {
                "host": "192.53.69.143",
                "port": "6781",
                "username": "nsucssou",
                "password": "3mjeb2p392yk"
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()

    response = client.get("/api/status")

    assert response.status_code == 200
    assert response.get_json()["config"]["proxy_configured"] is True


def test_dashboard_has_proxy_pool_status_widgets():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "IP池总数" in page
    assert "已分配" in page
    assert "剩余" in page
    assert "proxy-pool-total" in page
    assert "/api/proxy-pool/status" in page


def test_proxy_pool_detail_list_lives_under_proxy_configuration_page():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]
    proxy_panel = page.split('<section class="panel" id="panel-proxy-config">', 1)[1].split(
        '<section class="panel" id="panel-content">',
        1,
    )[0]

    assert 'id="proxy-pool-list"' not in settings_panel
    assert 'id="proxy-pool-open"' in settings_panel
    assert 'id="proxy-pool-list"' in proxy_panel
    assert 'id="proxy-pool-search"' in proxy_panel
    assert 'id="proxy-pool-page-size"' in proxy_panel
    assert 'id="proxy-pool-prev"' in proxy_panel
    assert 'id="proxy-pool-next"' in proxy_panel
    assert 'id="proxy-pool-page-meta"' in proxy_panel
    assert 'showPanel("proxy-config")' in page
    assert 'page_size' in page


def test_dashboard_has_buffer_channel_discovery_controls():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "同步选中" in page
    assert "同步全部" in page
    assert "/api/accounts/discover" in page


def test_dashboard_has_buffer_account_import_controls():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "导入 Buffer 账号" in page
    assert "buffer-import-text" in page
    assert "buffer-import-file" in page
    assert "/api/accounts/import" in page


def test_dashboard_has_manual_buffer_account_submit_controls():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "buffer-manual-account-name" in page
    assert "buffer-manual-token" in page
    assert "buffer-manual-api" in page
    assert "buffer-manual-account-id" in page
    assert "buffer-submit-account" in page
    assert "js-edit-account" in page
    assert "editBufferAccount" in page
    assert "submitBufferAccount" in page


def test_dashboard_has_available_accounts_list():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "可用账号列表" in page
    assert "accounts-body" in page
    assert "refreshAccounts" in page
    assert "/api/accounts" in page
    assert "/api/accounts/save" in page


def test_dashboard_account_roster_is_simplified_for_batch_sync():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    accounts_panel = page.split('<section class="panel" id="panel-accounts">', 1)[1].split(
        '<section class="panel" id="panel-proxy-config">',
        1,
    )[0]
    assert "账号 ID" not in accounts_panel
    assert "执行结果" not in accounts_panel
    assert "获取可用账号" not in accounts_panel
    assert "更新账号状态" not in accounts_panel
    assert "同步 Buffer Channels" not in accounts_panel
    assert "提交手动账号" not in accounts_panel
    assert "导入并发现绑定账号" not in accounts_panel
    assert "提交账号" in accounts_panel
    assert "accounts-select-all" in accounts_panel
    assert "accounts-sync-selected" in accounts_panel
    assert "accounts-sync-all" in accounts_panel
    assert "account-output" not in accounts_panel
    assert 'render("#account-output"' not in page


def test_dashboard_splits_proxy_config_and_publish_management_pages():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert 'href="/console/settings"' in page
    assert 'href="/console/publishing"' in page
    assert 'id="panel-proxy-config"' in page
    assert 'id="panel-publish"' in page
    assert "账号代理分配" in page
    assert "proxy-assignment-body" in page
    assert "自动分配" in page
    assert "手动代理" in page
    assert "/api/accounts/proxy" in page


def test_dashboard_has_content_management_page():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert 'href="/console/publishing"' in page
    assert 'id="panel-content"' in page
    assert "视频内容库" in page
    assert "品牌文案库" in page
    assert "/api/content/videos/sync" in page
    assert "/api/content/brands" in page
    assert "/api/content/copy" in page


def test_refresh_brands_keeps_brand_rendering_without_removed_auto_strategy_hook():
    page = create_app().test_client().get("/").data.decode("utf-8")
    refresh_brands = page.split("async function refreshBrands()", 1)[1].split(
        "function formatContentDate", 1
    )[0]

    assert "refreshBrowserAutoElementOptions" not in page
    assert "state.brands = payload.brands || []" in refresh_brands
    assert "publishSelect.innerHTML = options" in refresh_brands
    assert "renderBrandFolders()" in refresh_brands
    assert "await refreshPublishCopyItems()" in refresh_brands
    assert "renderPublishSelectors()" in refresh_brands


def test_content_management_uses_brand_folder_grid_and_import_dialog():
    client = create_app().test_client()

    page = client.get("/").data.decode("utf-8")
    content_panel = page.split(
        '<section class="panel" id="panel-content">',
        1,
    )[1].split('<section class="panel" id="panel-publish">', 1)[0]

    assert 'id="content-brand-overview"' in content_panel
    assert 'id="content-brand-grid"' in content_panel
    assert 'id="content-brand-detail"' in content_panel
    assert 'id="content-import-dialog"' in content_panel
    assert 'id="content-brand-dialog"' in content_panel
    assert 'id="content-rename-dialog"' in content_panel
    assert 'accept=".xlsx,.csv,.tsv"' in content_panel
    assert "新建品牌" in content_panel
    assert "导入表格" in content_panel
    assert "删除品牌" not in content_panel
    assert "/api/content/copy/import" in page
    assert 'method: "PATCH"' in page


def test_dashboard_publish_management_has_queue_and_results_without_legacy_stats():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    publish_panel = page.split('<section class="panel" id="panel-publish">', 1)[1].split(
        '<section class="panel" id="panel-browser">',
        1,
    )[0]
    assert "手动测试发布" in publish_panel
    assert "批量创建发布" in publish_panel
    assert "每日定时发布" in publish_panel
    assert "手动测试" in publish_panel
    assert "批量创建" in publish_panel
    assert "发布结果管理" in publish_panel
    assert "/api/publish/queue/manual-test" in page
    assert "/api/publish/queue/batch" in page
    assert "/api/publish/results" in page
    assert "/api/publish/stats" not in page
    assert "/api/publish/logs/cleanup" in page


def test_dashboard_links_to_new_stats_page_and_removes_legacy_stats_panel():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert 'href="/console/collection-results"' in page

    publish_panel = page.split('<section class="panel" id="panel-publish">', 1)[1].split(
        '<section class="panel" id="panel-publish-results">',
        1,
    )[0]
    results_panel = page.split('<section class="panel" id="panel-publish-results">', 1)[1].split(
        '<section class="panel" id="panel-browser">',
        1,
    )[0]

    assert 'href="/console/publishing"' in page
    assert 'id="publish-batch-dialog"' in publish_panel
    assert 'id="publish-batch-account-list"' in publish_panel
    assert 'id="publish-results-body"' not in publish_panel
    assert 'id="stats-count"' not in publish_panel
    assert 'id="publish-results-body"' in results_panel
    assert 'name="publish_sampling.enabled"' in page
    assert 'name="publish_sampling.interval_seconds"' in page
    assert 'name="publish_sampling.min_age_hours"' in page
    assert "proxy_display" in page
    for legacy_marker in (
        'id="panel-publish-stats"',
        'id="publish-refresh-stats"',
        'id="stats-filter-date"',
        'id="stats-sort"',
        'id="stats-refresh"',
        'id="stats-count"',
        'id="stats-success"',
        'id="stats-likes"',
        'id="stats-views"',
        'id="stats-comments"',
        'id="stats-engagement"',
        'id="publish-stats-body"',
        "function refreshPublishStats",
        "function statsFilterQuery",
        "function runAutoSampleTick",
    ):
        assert legacy_marker not in page


def test_dashboard_uses_shared_expanded_sidebar_with_real_panel_urls():
    page = create_app().test_client().get("/").get_data(as_text=True)

    expected_links = [
        ('/console/overview', '>运行总览</a>'),
        ('/console/tasks', '>任务执行</a>'),
        ('/console/actions', '>动作库</a>'),
        ('/console/publishing', '>内容发布</a>'),
        ('/console/collection', '>数据采集</a>'),
        ('/console/collection-results', '>采集结果</a>'),
        ('/console/accounts-windows', '>账号与窗口</a>'),
        ('/console/page-elements', '>页面元素</a>'),
        ('/console/receipts', '>回执与证据</a>'),
        ('/console/settings', '>系统设置</a>'),
    ]
    positions = []
    for href, marker in expected_links:
        link = f'href="{href}"'
        assert link in page
        assert marker in page
        positions.append(page.index(link))

    assert positions == sorted(positions)
    assert 'class="dashboard-shell"' in page
    assert 'class="dashboard-sidebar"' in page
    assert 'href="/console/settings"' in page
    assert 'aria-current="page"' in page
    assert "/static/dashboard_shell.css" in page
    assert "/static/dashboard_navigation.js" in page


def test_publish_management_has_batch_and_daily_sections_with_date_time_pickers():
    page = create_app().test_client().get("/").data.decode("utf-8")
    publish_panel = page.split('<section class="panel" id="panel-publish">', 1)[1].split(
        '<section class="panel" id="panel-publish-results">',
        1,
    )[0]

    assert "批量创建发布" in publish_panel
    assert "每日定时发布" in publish_panel
    assert 'id="publish-batch-runs-body"' in publish_panel
    assert 'id="publish-daily-schedules-body"' in publish_panel
    assert 'id="publish-daily-dialog"' in publish_panel
    assert 'id="publish-daily-account-list"' in publish_panel
    assert 'id="publish-manual-date"' in publish_panel
    assert 'id="publish-manual-time"' in publish_panel
    assert 'id="publish-batch-date"' in publish_panel
    assert 'id="publish-batch-time"' in publish_panel
    assert 'id="publish-daily-start-date"' in publish_panel
    assert 'id="publish-daily-time-input"' in publish_panel
    assert 'type="date"' in publish_panel
    assert 'type="time"' in publish_panel
    assert "/api/publish/queue/batches" in page
    assert 'method: "GET"' in page


def test_publish_management_can_edit_and_delete_batch_and_daily_tasks():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert "js-edit-batch-run" in page
    assert "js-delete-batch-run" in page
    assert "js-edit-daily-schedule" in page
    assert "js-delete-daily-schedule" in page
    assert 'method: "PATCH"' in page
    assert '"DELETE"' in page
    assert "/api/publish/queue/batches/" in page
    assert "/api/publish/schedule/daily/" in page


def test_publish_management_uses_short_time_format_and_aligned_actions():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert "function compactDateTime" in page
    assert "compactDateTime(run.created_at" in page
    assert "compactDateTime(run.scheduled_at" in page
    assert "compactDateTime(schedule.updated_at" in page
    assert "compactDateTime(task.scheduled_at" in page
    assert "table-actions" in page
    assert 'class="table-actions"' in page


def test_dashboard_settings_has_publish_queue_interval():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert 'name="publish_queue.interval_seconds"' in page
    assert '"publish_queue.interval_seconds"' in page


def test_dashboard_settings_groups_configuration_by_domain():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]

    assert 'class="settings-sections"' in settings_panel
    assert 'data-settings-group="proxy"' in settings_panel
    assert 'data-settings-group="services"' in settings_panel
    assert 'data-settings-group="storage"' in settings_panel
    assert 'data-settings-group="browser"' in settings_panel
    assert 'data-settings-group="adspower"' in settings_panel
    assert 'data-settings-group="models"' in settings_panel
    assert 'data-settings-group="publishing"' in settings_panel
    assert settings_panel.index('data-settings-group="proxy"') < settings_panel.index(
        'name="proxy.host"'
    )
    assert settings_panel.index('data-settings-group="models"') < settings_panel.index(
        'name="models.default_model_id"'
    )


def test_dashboard_settings_checks_health_before_save_and_offers_restore():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]

    assert 'id="settings-health-status"' in settings_panel
    assert 'id="settings-restore-latest"' in settings_panel
    assert 'id="settings-save"' in settings_panel
    assert 'fetch("/api/settings/status")' in page
    assert 'postJson("/api/settings/restore-latest", {})' in page
    assert "if (!health.ok)" in page


def test_dashboard_settings_has_r2_configuration_fields():
    client = create_app().test_client()

    response = client.get("/")

    assert response.status_code == 200
    page = response.data.decode("utf-8")
    assert "R2 Account Token" in page
    assert 'name="r2.account_token"' in page
    assert 'name="r2.bucket"' in page
    assert 'name="r2.endpoint_url"' in page


def test_dashboard_settings_only_show_current_buffer_graphql_endpoint():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert 'name="services.buffer_graphql_url"' in page
    assert 'name="services.buffer_create_update_url"' not in page


def test_dashboard_has_direct_agent_controls():
    page = create_app().test_client().get("/").data.decode("utf-8")
    browser_panel = page.split('<section class="panel" id="panel-browser">', 1)[1].split(
        '<section class="panel" id="panel-strategies">',
        1,
    )[0]

    assert 'id="direct-agent-profile-no"' not in browser_panel
    assert 'id="direct-agent-start"' not in browser_panel
    assert 'id="search-agent-start"' not in browser_panel
    assert 'id="direct-agent-result"' not in browser_panel
    assert 'id="adspower-window-list"' in browser_panel
    assert 'id="adspower-refresh-windows"' in browser_panel
    assert 'id="browser-session-list"' not in browser_panel
    assert 'id="browser-refresh-sessions"' not in browser_panel
    assert 'id="browser-element-results"' not in browser_panel
    assert 'id="browser-read-elements"' not in browser_panel
    assert "/api/browser/adspower-windows" in page


def test_dashboard_has_model_configuration_fields():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]

    assert 'name="models.default_model_id"' in settings_panel
    assert 'name="models.items.0.provider"' in settings_panel
    assert 'name="models.items.0.base_url"' in settings_panel
    assert 'name="models.items.0.api_key"' in settings_panel
    assert 'name="models.items.0.model"' in settings_panel


def test_dashboard_model_configuration_uses_public_presets_for_two_level_selection():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]
    preset_switching = page.split("function applyModelPreset", 1)[1].split(
        "function", 1
    )[0]

    assert 'id="model-provider"' in settings_panel
    assert 'id="model-preset-model"' in settings_panel
    assert 'id="model-custom-name-field"' in settings_panel
    assert 'name="models.items.0.enabled"' in settings_panel
    assert 'fetch("/api/model-presets")' in page
    assert '<option value="grok">' not in settings_panel
    assert '<option value="deepseek">' not in settings_panel
    assert 'modelField("provider").addEventListener("change"' in page
    assert 'document.querySelector("#model-preset-model").addEventListener("change"' in page
    assert '"models.items.0.enabled"' in page
    assert "api_key" not in preset_switching


def test_dashboard_model_presets_failure_degrades_without_partial_settings_state():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]
    load_settings = page.split("async function loadSettings()", 1)[1].split(
        "function modelField", 1
    )[0]
    load_presets = page.split("async function loadModelPresets()", 1)[1].split(
        "async function", 1
    )[0]

    assert 'id="model-presets-status"' in settings_panel
    assert 'id="model-presets-refresh"' in settings_panel
    assert "catch" in load_presets
    assert "renderModelPresetFallback" in load_presets
    assert "模型预设加载失败" in load_presets
    assert "return false" in load_presets
    assert load_settings.index("await loadModelPresets()") < load_settings.index(
        "settingsLoaded = true"
    )
    assert 'document.querySelector("#model-presets-refresh").addEventListener' in page


def test_dashboard_settings_payload_preserves_loaded_model_tail():
    page = create_app().test_client().get("/").data.decode("utf-8")
    payload_helper = page.split("function preserveLoadedModelItems", 1)[1].split(
        "async function", 1
    )[0]
    save_settings = page.split("async function saveSettings", 1)[1].split(
        "async function", 1
    )[0]

    assert "loadedSettings.models?.items" in payload_helper
    assert "loadedItems.slice(1)" in payload_helper
    assert "editedItems[0]" in payload_helper
    assert "preserveLoadedModelItems(settings, currentSettings)" in save_settings


def test_dashboard_has_adspower_configuration_fields():
    page = create_app().test_client().get("/").data.decode("utf-8")
    settings_panel = page.split('<section class="panel active" id="panel-settings">', 1)[1].split(
        '<section class="panel" id="panel-accounts">',
        1,
    )[0]

    assert 'name="adspower.base_url"' in settings_panel
    assert 'name="adspower.api_key"' in settings_panel
    assert 'name="browser.default_url"' in settings_panel
    assert 'type="password" autocomplete="off"' in settings_panel


def test_strategy_palette_contract_rejects_an_extra_nested_block():
    wrong_markup = """
      <div id="browser-block-palette">
        <button data-block-type="move"></button>
        <button data-block-type="click"></button>
        <button data-block-type="scroll_up"></button>
        <button data-block-type="scroll_down"></button>
        <button data-block-type="keyboard_input"></button>
        <button data-block-type="pause"></button>
        <button data-block-type="unexpected"></button>
      </div>
    """

    with pytest.raises(AssertionError):
        _assert_strategy_palette_contract(_parse_html(wrong_markup))


def test_dashboard_has_one_block_strategy_manager():
    page = create_app().test_client().get("/").data.decode("utf-8")
    root = _parse_html(page)
    panel = _element_by_id(root, "panel-strategies")
    elements_manager = _element_by_id(root, "browser-elements-manager")
    pattern_library = _element_by_id(root, "browser-pattern-library")
    list_view = _element_by_id(root, "browser-strategy-list-view")
    editor_view = _element_by_id(root, "browser-strategy-editor-view")

    assert panel.tag == "section"
    assert elements_manager.position < pattern_library.position < list_view.position
    for section in (elements_manager, pattern_library, list_view, editor_view):
        _assert_descendant(panel, section)

    _assert_descendant(elements_manager, _element_by_id(root, "browser-action-elements-list"))
    _assert_descendant(elements_manager, _element_by_id(root, "browser-element-add"))
    for control_id in ("browser-pattern-record-mouse", "browser-pattern-record-keyboard"):
        _assert_descendant(pattern_library, _element_by_id(root, control_id))
    _assert_descendant(pattern_library, _element_by_id(root, "browser-pattern-list"))

    strategy_list = _element_by_id(root, "browser-strategy-list")
    strategy_template = _element_by_id(root, "browser-strategy-card-template")
    _assert_descendant(list_view, strategy_list)
    _assert_descendant(list_view, strategy_template)
    _assert_descendant(list_view, _element_by_id(root, "browser-strategy-create"))
    for field in (
        "data-strategy-name",
        "data-strategy-mode",
        "data-strategy-action-count",
        "data-strategy-repair-state",
        "data-strategy-save-state",
        "data-strategy-open",
    ):
        assert len(_descendants_with_attr(strategy_template, field)) == 1

    assert "is-hidden" in editor_view.attrs.get("class", "").split()
    for control_id in (
        "browser-strategy-back",
        "browser-strategy-name",
        "browser-strategy-rename",
        "browser-strategy-delete",
        "browser-strategy-save",
        "browser-strategy-save-state",
        "browser-strategy-loop-minutes-min",
        "browser-strategy-loop-minutes-max",
        "browser-strategy-batch-size",
    ):
        _assert_descendant(editor_view, _element_by_id(root, control_id))

    run_mode = _element_by_id(root, "browser-strategy-run-mode")
    _assert_descendant(editor_view, run_mode)
    assert [
        node.attrs.get("value")
        for node in _walk(run_mode)
        if node.tag == "option"
    ] == ["once", "loop"]

    palette = _element_by_id(root, "browser-block-palette")
    _assert_descendant(editor_view, palette)
    _assert_strategy_palette_contract(root)

    strategy_actions = _element_by_id(root, "browser-strategy-actions")
    block_template = _element_by_id(root, "browser-block-card-template")
    _assert_descendant(editor_view, strategy_actions)
    _assert_descendant(editor_view, block_template)
    assert strategy_actions.position < block_template.position
    for field in (
        "data-action-type",
        "data-action-summary",
        "data-block-edit",
        "data-block-up",
        "data-block-down",
        "data-block-delete",
    ):
        assert len(_descendants_with_attr(block_template, field)) == 1

    parameter_dialog = _element_by_id(root, "browser-block-parameter-dialog")
    _assert_descendant(panel, parameter_dialog)
    _assert_descendant(
        parameter_dialog,
        _element_by_id(root, "browser-block-parameter-fields"),
    )

    body = next(node for node in _walk(root) if node.tag == "body")
    body_scripts = [node for node in body.children if node.tag == "script"]
    external_script = body_scripts[-1]
    shared_helpers = body_scripts[-2]
    assert external_script.attrs.get("src") == "/static/browser_strategy_ui.js"
    assert "src" not in shared_helpers.attrs
    assert "function escapeHtml" in "".join(shared_helpers.text)
    assert shared_helpers.position < external_script.position
    assert body.children.index(external_script) == body.children.index(shared_helpers) + 1

    assert 'id="strategy-json"' not in page
    assert 'id="strategy-generate-prompt"' not in page
    assert 'id="strategy-generate"' not in page
    assert 'id="browser-action-strategies"' not in page
    assert 'id="browser-element-dialog"' in page
    assert '<textarea id="browser-action-elements"' not in page
    assert 'id="browser-strategy-select"' not in page
    assert 'id="browser-action-add"' not in page
    assert 'id="browser-action-save"' not in page
    assert 'id="browser-action-load"' not in page
    assert 'id="browser-auto-strategy-manager"' not in page
    assert 'id="browser-auto-total-min"' not in page
    assert 'id="browser-auto-entry-element"' not in page
    assert 'id="browser-auto-input-element"' not in page
    assert 'id="browser-auto-submit-element"' not in page
    assert '>策略管理<' not in page
    assert '>添加动作<' not in page


def test_browser_takeover_button_and_element_alias_have_clear_ui():
    page = create_app().test_client().get("/").data.decode("utf-8")

    assert 'id="adspower-open-tile"' in page
    assert '>打开窗口<' in page
    assert '打开、平铺并同步网址（最多 8 个）' not in page
    assert ".browser-element-row code" in page
    assert "color: #0b5d43" in page
    assert 'id="browser-auto-click-element-picker"' not in page
    assert 'id="browser-auto-click-order-list"' not in page
    assert 'id="browser-auto-click-order"' not in page
    assert "有序点击元素" not in page
    assert 'id="browser-strategy-batch-size"' in page
    assert 'id="browser-elements-manager"' in page


def test_execution_strategies_api_reads_and_saves_settings(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    client = create_app().test_client()

    initial = client.get("/api/execution-strategies")

    assert initial.status_code == 200
    assert initial.get_json()["items"][0]["id"] == "steady_reader"

    response = client.put(
        "/api/execution-strategies",
        json={
            "items": [
                {
                    "id": "night_reader",
                    "label": "Night reader",
                    "mouseMoves": 4,
                    "clicks": 1,
                    "scrolls": 3,
                    "moveSteps": [10, 20],
                    "pauseMs": [300, 800],
                    "scrollDelta": [200, 500],
                    "text_prompt": "read slowly",
                }
            ]
        },
    )

    assert response.status_code == 200
    assert response.get_json()["items"][0]["id"] == "night_reader"
    assert client.get("/api/execution-strategies").get_json()["items"][0]["clicks"] == 1


def test_execution_strategy_generation_uses_configured_model(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "models": {
            "default_model_id": "test-model",
            "items": [
              {
                "id": "test-model",
                "provider": "gpt",
                "enabled": true,
                "base_url": "https://model.example/v1",
                "api_key": "secret",
                "model": "gpt-test",
                "mode": "chat"
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": """
                            [
                              {
                                "id": "fast_scan",
                                "label": "Fast scan",
                                "mouseMoves": 4,
                                "clicks": 0,
                                "scrolls": 3,
                                "moveSteps": [8, 16],
                                "pauseMs": [120, 360],
                                "scrollDelta": [240, 560],
                                "text_prompt": "scan visible page blocks"
                              }
                            ]
                            """
                        }
                    }
                ]
            }

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers, timeout))
        return FakeResponse()

    monkeypatch.setattr("gateway.app.requests.post", fake_post)

    response = create_app().test_client().post(
        "/api/execution-strategies/generate",
        json={"prompt": "生成一个快速浏览策略"},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["items"][0]["id"] == "fast_scan"
    assert calls[0][0] == "https://model.example/v1/chat/completions"
    assert calls[0][2] == {"Authorization": "Bearer secret"}


def test_adspower_windows_api_returns_sanitized_profiles(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"adspower": {"base_url": "http://local.adspower.test:50325", "api_key": "local-key"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "data": {
                    "list": [
                        {
                            "profile_id": "abc123",
                            "profile_no": "7",
                            "name": "window-7",
                            "group_name": "TikTok",
                            "username": "person@example.com",
                            "password": "secret",
                        }
                    ]
                }
            }

    def fake_get(url, params=None, timeout=None, headers=None):
        assert url == "http://local.adspower.test:50325/api/v1/user/list"
        assert params == {"page": 1, "page_size": 200}
        assert timeout == 10
        assert headers == {"Authorization": "Bearer local-key"}
        return FakeResponse()

    monkeypatch.setattr("gateway.app.requests.get", fake_get)

    response = create_app().test_client().get("/api/browser/adspower-windows")

    assert response.status_code == 200
    assert response.get_json() == {
        "count": 1,
        "windows": [
            {
                "profile_id": "abc123",
                "profile_no": "7",
                "name": "window-7",
                "group_name": "TikTok",
                "username": "person@example.com",
            }
        ],
    }


def test_adspower_windows_api_reports_adspower_error(monkeypatch, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"adspower": {"base_url": "http://local.adspower.test:50325"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"code": -1, "msg": "Require api-key"}

    monkeypatch.setattr(
        "gateway.app.requests.get",
        lambda *args, **kwargs: FakeResponse(),
    )

    response = create_app().test_client().get("/api/browser/adspower-windows")

    assert response.status_code == 502
    assert response.get_json() == {
        "count": 0,
        "error": "AdsPower request failed: Require api-key",
        "windows": [],
    }


def test_direct_agent_api_rejects_missing_profile_selector():
    response = create_app().test_client().post(
        "/api/browser/direct-agent",
        json={"max_steps": 3},
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "error": "profile_id or profile_no is required",
    }


def test_direct_agent_api_starts_node_process(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4321

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr("gateway.app.subprocess.Popen", fake_popen)

    response = create_app().test_client().post(
        "/api/browser/direct-agent",
        json={
            "profile_no": "7",
            "max_steps": 4,
            "url": "https://www.tiktok.com/login",
            "close_after_run": True,
        },
    )

    assert response.status_code == 202
    data = response.get_json()
    assert data["status"] == "started"
    assert data["pid"] == 4321
    assert data["command"] == [
        "npm",
        "run",
        "direct-agent",
        "--",
        "--profile-no",
        "7",
        "--url",
        "[redacted-url]",
        "--max-steps",
        "4",
    ]
    assert calls[0][0] == [
        "npm",
        "run",
        "direct-agent",
        "--",
        "--profile-no",
        "7",
        "--url",
        "https://www.tiktok.com/login",
        "--max-steps",
        "4",
    ]
    assert calls[0][1]["shell"] is False


def test_search_agent_api_starts_node_process(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 9876

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    monkeypatch.setattr("gateway.app.subprocess.Popen", fake_popen)

    response = create_app().test_client().post(
        "/api/browser/search-agent",
        json={
            "profile_nos": "1,2",
            "url": "https://example.com",
            "login_check_xpath": "//button[@data-user]",
            "search_xpath": "//input[@name='q']",
            "query": "hello",
            "strategy": "curious_scanner",
        },
    )

    assert response.status_code == 202
    data = response.get_json()
    assert data["status"] == "started"
    assert data["pid"] == 9876
    assert data["command"] == [
        "npm",
        "run",
        "search-agent",
        "--",
        "--profile-nos",
        "1,2",
        "--url",
        "[redacted-url]",
        "--login-check-xpath",
        "//button[@data-user]",
        "--search-xpath",
        "//input[@name='q']",
        "--query",
        "hello",
        "--strategy",
        "curious_scanner",
    ]
    assert calls[0][0] == [
        "npm",
        "run",
        "search-agent",
        "--",
        "--profile-nos",
        "1,2",
        "--url",
        "https://example.com",
        "--login-check-xpath",
        "//button[@data-user]",
        "--search-xpath",
        "//input[@name='q']",
        "--query",
        "hello",
        "--strategy",
        "curious_scanner",
    ]
    assert calls[0][1]["shell"] is False
