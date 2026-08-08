import re


def test_control_page_exposes_exact_selector_probe_menu_shell(admin_client):
    html = admin_client.get("/").get_data(as_text=True)

    assert html.count('role="tablist"') == 1
    assert html.count('role="tab"') == 4
    assert html.count('role="tabpanel"') == 4
    assert re.findall(r'data-selector-probe-tab="([^"]+)"', html) == [
        "collect",
        "managed",
        "operations",
        "settings",
    ]
    for label in ("采集元素", "已选元素", "运行与告警", "系统设置"):
        assert f">{label}</button>" in html
    for tab_id in (
        "collect",
        "managed",
        "operations",
        "settings",
    ):
        assert f'aria-controls="selector-probe-panel-{tab_id}"' in html
        assert f'id="selector-probe-panel-{tab_id}"' in html
    assert html.count('aria-selected="true"') == 1
    assert html.count('data-selector-probe-panel="collect"') == 1
    assert not re.search(
        r"selector-probe-tab-(overview|elements|gates|runs|versions|alerts)",
        html,
    )
    assert 'aria-live="polite"' in html


def test_control_page_loads_probe_assets_before_browser_controller(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    inventory_script = admin_client.client.get(
        "/static/selector_inventory_ui.js"
    )
    script = admin_client.client.get("/static/selector_probe_ui.js")
    stylesheet = admin_client.client.get("/static/selector_probe.css")
    dashboard_stylesheet = admin_client.client.get("/static/dashboard_shell.css")

    try:
        assert inventory_script.status_code == 200
        assert script.status_code == 200
        assert stylesheet.status_code == 200
        assert dashboard_stylesheet.status_code == 200
        assert html.index("management_fetch.js") < html.index("selector_inventory_ui.js")
        assert html.index("selector_inventory_ui.js") < html.index("selector_probe_ui.js")
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
            'id="selector-element-picker"',
            'id="selector-inventory-list"',
            'id="selector-inventory-search"',
            'id="selector-element-filters"',
            'id="selector-element-counts"',
            'id="selector-managed-elements"',
            'id="selector-element-page-size"',
            'id="selector-gate-counts"',
            'id="selector-gate-rows"',
            'id="selector-run-now"',
            'id="selector-run-rows"',
            'id="selector-run-current-steps"',
            'id="selector-run-stage-detail"',
            'id="selector-run-technical-details"',
            'id="selector-run-technical-lines"',
            'id="selector-version-rows"',
            'id="selector-alert-counts"',
            'id="selector-alert-rows"',
            'id="selector-operation-confirm-dialog"',
            'id="selector-operation-detail-dialog"',
            'id="selector-settings-form"',
            'id="selector-settings-basic"',
            'id="selector-settings-profiles"',
            'id="selector-settings-redis"',
            'id="selector-settings-webhook"',
            'id="selector-settings-permissions"',
            'id="selector-account-rows"',
        ):
            assert marker in html
        assert '<option value="20">20</option>' in html
        assert '<option value="50">50</option>' in html
        assert '<option value="100">100</option>' in html
        assert '<option value="yes">已引用</option>' in html
        assert '<option value="no">未引用</option>' in html
        assert 'placeholder="自定义名称或元素 ID"' in html
        assert "Role/Name 仅帮助阅读，不参与路径匹配" in html
        assert "人工暂停/恢复不会被探针自动覆盖" in html
        assert "留空保持不变" in html
        probe_console = html[
            html.index('id="panel-strategies"'):
            html.index('<script src="/static/selector_inventory_ui.js"')
        ]
        assert "API Key" not in probe_console
        assert "LLM" not in probe_console
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
        assert ".selector-run-stage-card" in css
        assert ".selector-run-stage-status.is-failed" in css
        assert ".selector-run-technical-details" in css
        assert ".selector-console-nav" in css
        assert ".selector-inventory-grid" in css
        assert "grid-template-columns: repeat(5, minmax(0, 1fr))" in css
        assert "selector_probe.css" not in dashboard_css
    finally:
        inventory_script.close()
        script.close()
        stylesheet.close()
        dashboard_stylesheet.close()
