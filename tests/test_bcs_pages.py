"""BCS frontend pages render tests (gateway side)."""

from __future__ import annotations

import pytest

from gateway.app import create_app


def _app():
    return create_app({"LOCAL_DIRECT_MODE": True, "TESTING": True})


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/bcs", "dashboard"),
        ("/bcs/devices", "devices"),
        ("/bcs/tasks", "tasks"),
    ],
)
def test_bcs_pages_render(path, expected):
    client = _app().test_client()
    response = client.get(path)
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "业务控制系统" in html
    assert f'id="panel-{expected}"' in html
    assert "bcs.js" in html


def test_bcs_pages_include_sidebar_and_csrf():
    client = _app().test_client()
    html = client.get("/bcs").get_data(as_text=True)
    assert "自动化主控台" in html
    assert 'name="csrf-token"' in html


def test_bcs_pages_use_text_content_safe_pattern():
    client = _app().test_client()
    html = client.get("/bcs/devices").get_data(as_text=True)
    assert "devices-body" in html
