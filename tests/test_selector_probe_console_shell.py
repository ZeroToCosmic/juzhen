import re


def test_control_page_exposes_exact_selector_probe_tab_shell(admin_client):
    html = admin_client.get("/").get_data(as_text=True)

    assert html.count('role="tablist"') == 1
    assert html.count('role="tab"') == 7
    assert html.count('role="tabpanel"') == 7
    assert re.findall(r'data-selector-probe-tab="([^"]+)"', html) == [
        "overview",
        "elements",
        "gates",
        "runs",
        "versions",
        "alerts",
        "settings",
    ]
    for label in ("总览", "元素", "策略闸门", "探针运行", "版本", "告警", "设置"):
        assert f">{label}</button>" in html
    for tab_id in (
        "overview",
        "elements",
        "gates",
        "runs",
        "versions",
        "alerts",
        "settings",
    ):
        assert f'aria-controls="selector-probe-panel-{tab_id}"' in html
        assert f'id="selector-probe-panel-{tab_id}"' in html
    assert html.count('aria-selected="true"') == 1
    assert html.count('data-selector-probe-panel="overview"') == 1
    assert 'aria-live="polite"' in html


def test_control_page_loads_probe_assets_before_browser_controller(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    script = admin_client.client.get("/static/selector_probe_ui.js")
    stylesheet = admin_client.client.get("/static/selector_probe.css")
    dashboard_stylesheet = admin_client.client.get("/static/dashboard_shell.css")

    try:
        assert script.status_code == 200
        assert stylesheet.status_code == 200
        assert dashboard_stylesheet.status_code == 200
        assert html.index("management_fetch.js") < html.index("selector_probe_ui.js")
        assert html.index("selector_probe_ui.js") < html.index("browser_strategy_ui.js")
        assert html.index("dashboard_shell.css") < html.index("selector_probe.css")
        assert html.count("selector_probe.css") == 1
        for marker in (
            'id="selector-probe-health"',
            'id="selector-probe-refresh"',
            'id="selector-probe-run-now"',
            'id="selector-probe-unread-alerts"',
            'id="browser-elements-manager"',
            'id="browser-strategy-list-view"',
            'id="browser-strategy-editor-view"',
        ):
            assert marker in html
        for marker in (
            'id="selector-overview-summaries"',
            'id="selector-overview-priority"',
            'id="selector-overview-events"',
            'id="selector-element-add"',
            'id="selector-element-filters"',
            'id="selector-element-counts"',
            'id="selector-element-rows"',
            'id="selector-element-page-size"',
            'id="selector-element-wizard"',
            'id="selector-element-detail"',
            'id="selector-element-validation-matrix"',
            'id="selector-element-detail-evidence"',
            'id="selector-element-detail-candidates"',
            'id="selector-element-detail-repairs"',
            'id="selector-element-detail-history"',
            'id="selector-element-migration-dialog"',
            'id="selector-gate-counts"',
            'id="selector-gate-rows"',
            'id="selector-run-now"',
            'id="selector-run-rows"',
            'id="selector-version-rows"',
            'id="selector-alert-counts"',
            'id="selector-alert-rows"',
            'id="selector-operation-confirm-dialog"',
            'id="selector-operation-detail-dialog"',
            'id="selector-settings-form"',
            'id="selector-settings-basic"',
            'id="selector-settings-profiles"',
            'id="selector-settings-model"',
            'id="selector-settings-redis"',
            'id="selector-settings-webhook"',
            'id="selector-settings-permissions"',
            'id="selector-account-rows"',
            'id="selector-temporary-password-dialog"',
        ):
            assert marker in html
        assert '<option value="20">20</option>' in html
        assert '<option value="50">50</option>' in html
        assert '<option value="100">100</option>' in html
        assert '<option value="yes">已引用</option>' in html
        assert '<option value="no">未引用</option>' in html
        assert 'placeholder="名称、ID 或策略"' in html
        assert "Role、稳定属性" not in html
        wizard = html[
            html.index('id="selector-element-wizard"'):
            html.index('id="selector-element-migration-dialog"')
        ]
        assert 'name="javascript"' not in wizard
        assert 'name="xpath"' not in wizard
        assert 'id="selector-element-advanced-locators"' not in wizard
        assert "Locator 候选由探针生成并以只读方式展示" in wizard
        assert "保留当前 Locator" in html
        assert "策略依赖保持不变" in html
        assert "不会自动开启强制执行" in html
        assert "确认告警只记录归属，不清除策略闸门" in html
        assert "留空保持不变" in html
        assert "独立清除 API Key" in html
        assert "仅显示一次" in html
        assert "激活此版本" not in html
        assert "继续部分" not in html
        probe_script = script.get_data(as_text=True)
        assert ".innerHTML =" not in probe_script
        assert "screenshot_path" not in probe_script
        assert "source.api_key" not in probe_script
        assert "source.password" not in probe_script
        assert "source.signing_secret" not in probe_script
        css = stylesheet.get_data(as_text=True)
        dashboard_css = dashboard_stylesheet.get_data(as_text=True)
        assert ".selector-summary-grid" in css
        assert ".selector-element-filters" in css
        assert ".selector-operation-row" in css
        assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
        assert "selector_probe.css" not in dashboard_css
    finally:
        script.close()
        stylesheet.close()
        dashboard_stylesheet.close()
