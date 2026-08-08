"""Guard the public symbol surface of gateway.app across refactors.

Baseline snapshot taken before the Blueprint split began (245 symbols).
Any future import of a previously public name from gateway.app must keep
working; this test fails if a baseline symbol disappears.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.app import (
    CONTROL_PAGE_HTML,
    DASHBOARD_PAGE_HTML,
    SETTINGS_PAGE_HTML,
    create_app,
)

BASELINE_PATH = Path(__file__).resolve().parent / "gateway_app_symbols.txt"


def test_gateway_app_symbols_preserved():
    import gateway.app as module

    baseline = {
        line.strip()
        for line in BASELINE_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    missing = sorted(name for name in baseline if not hasattr(module, name))
    assert missing == [], f"symbols removed from gateway.app: {missing}"


@pytest.mark.parametrize(
    "name",
    [
        "create_app",
        "CONTROL_PAGE_HTML",
        "DASHBOARD_PAGE_HTML",
        "SETTINGS_PAGE_HTML",
        "public_settings",
        "load_persisted_strategy_state",
        "publish_to_buffer",
        "select_model_for_generation",
        "generate_proxy_url",
        "discover_accounts",
        "fetch_ip_info",
    ],
)
def test_public_imports_still_work(name):
    import gateway.app as module

    assert hasattr(module, name)
