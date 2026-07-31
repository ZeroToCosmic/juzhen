from types import SimpleNamespace

import pytest
import requests

from adspower import AdsPowerController, AdsPowerError


def test_start_browser_returns_puppeteer_url(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"code": 0, "data": {"ws": {"puppeteer": "ws://debug"}}},
        )

    monkeypatch.setattr("adspower.requests.get", fake_get)

    controller = AdsPowerController(base_url="http://127.0.0.1:50325")

    assert controller.start_browser("profile-1") == "ws://debug"
    assert calls[0][0].endswith("/api/v1/browser/start")
    assert calls[0][1]["params"] == {
        "user_id": "profile-1",
        "open_tabs": 1,
        "ip_tab": 0,
    }


def test_start_browser_retries_three_times_and_waits(monkeypatch):
    attempts = []
    waits = []

    def fake_get(*args, **kwargs):
        attempts.append(1)
        raise requests.exceptions.Timeout("AdsPower unavailable")

    monkeypatch.setattr("adspower.requests.get", fake_get)
    monkeypatch.setattr("adspower.time.sleep", waits.append)

    with pytest.raises(AdsPowerError, match="已重试 3 次"):
        AdsPowerController().start_browser("profile-1")

    assert len(attempts) == 3
    assert waits == [2.0, 2.0]


def test_stop_browser_calls_stop_endpoint(monkeypatch):
    response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"code": 0, "data": {"status": "stopped"}},
    )
    monkeypatch.setattr("adspower.requests.get", lambda *args, **kwargs: response)

    assert AdsPowerController().stop_browser("profile-2") == {"status": "stopped"}


def test_get_browser_active_uses_active_endpoint(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"code": 0, "data": {"status": "active"}},
        )

    monkeypatch.setattr("adspower.requests.get", fake_get)

    assert AdsPowerController().get_browser_active("profile-3") == {"status": "active"}
    assert calls[0][0].endswith("/api/v1/browser/active")
    assert calls[0][1]["params"] == {"user_id": "profile-3"}


def test_get_browser_active_rejects_empty_profile_id():
    with pytest.raises(ValueError, match="profile_id"):
        AdsPowerController().get_browser_active(" ")
