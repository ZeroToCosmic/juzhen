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


def test_list_profiles_uses_user_list_without_empty_user_id_and_normalizes_rows(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"code": 0, "data": {"list": [
                {"user_id": "profile-1", "name": "one", "status": "active", "group_name": "test"},
                {"profile_id": "profile-2", "name": "two", "status": "inactive"},
                {"name": "invalid"},
            ]}},
        )

    monkeypatch.setattr("adspower.requests.get", fake_get)
    profiles = AdsPowerController().list_profiles(page=2, page_size=200)

    assert calls[0][0].endswith("/api/v1/user/list")
    assert calls[0][1]["params"] == {"page": 2, "page_size": 200}
    assert profiles == [
        {"id": "profile-1", "name": "one", "status": "active", "group_name": "test"},
        {"id": "profile-2", "name": "two", "status": "inactive"},
    ]


@pytest.mark.parametrize("kwargs", [{"page": 0}, {"page": True}, {"page_size": 0}, {"page_size": 201}])
def test_list_profiles_validates_paging(kwargs):
    with pytest.raises(ValueError):
        AdsPowerController().list_profiles(**kwargs)


def test_list_profiles_retries_transient_error(monkeypatch):
    attempts = []
    waits = []

    def fake_get(*_args, **_kwargs):
        attempts.append(1)
        raise requests.exceptions.Timeout("offline")

    monkeypatch.setattr("adspower.requests.get", fake_get)
    monkeypatch.setattr("adspower.time.sleep", waits.append)
    with pytest.raises(AdsPowerError):
        AdsPowerController().list_profiles()
    assert len(attempts) == 3
    assert waits == [2.0, 2.0]


def test_list_all_profiles_reads_every_page(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    pages = {
        1: [{"id": f"p-{index}"} for index in range(200)],
        2: [{"id": f"p-{index}"} for index in range(200, 300)],
    }
    monkeypatch.setattr(
        controller,
        "_list_profile_page",
        lambda *, page, page_size: (pages.get(page, []), len(pages.get(page, [])), None),
    )

    rows = controller.list_all_profiles()

    assert [row["id"] for row in rows] == [f"p-{index}" for index in range(300)]


def test_list_all_profiles_reads_an_exact_multiple_through_empty_page(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    calls = []
    pages = {
        1: [{"id": f"p-{index}"} for index in range(200)],
        2: [{"id": f"p-{index}"} for index in range(200, 400)],
    }

    def list_page(*, page, page_size):
        calls.append(page)
        rows = pages.get(page, [])
        return rows, len(rows), None

    monkeypatch.setattr(controller, "_list_profile_page", list_page)

    rows = controller.list_all_profiles()

    assert [row["id"] for row in rows] == [f"p-{index}" for index in range(400)]
    assert calls == [1, 2, 3]


def test_list_all_profiles_continues_after_full_raw_page_with_invalid_rows(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    first_page = [{"id": "valid-0"}] + [{} for _ in range(199)]
    pages = {
        1: ([{"id": "valid-0"}], len(first_page), None),
        2: ([{"id": "valid-1"}], 1, None),
    }
    monkeypatch.setattr(
        controller,
        "_list_profile_page",
        lambda *, page, page_size: pages.get(page, ([], 0, None)),
    )

    assert [row["id"] for row in controller.list_all_profiles()] == ["valid-0", "valid-1"]


def test_list_all_profiles_honors_max_profiles_and_deduplicates_raw_ids(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    first_page = [{"id": "duplicate"}] + [{"id": f"p-{index}"} for index in range(199)]
    second_page = [{"id": "duplicate"}] + [{"id": f"p-{index}"} for index in range(199, 399)]
    pages = {
        1: (first_page, 200, None),
        2: (second_page, 200, None),
        3: ([], 0, None),
    }
    monkeypatch.setattr(
        controller,
        "_list_profile_page",
        lambda *, page, page_size: pages.get(page, ([], 0, None)),
    )

    rows = controller.list_all_profiles(max_profiles=250)

    assert len(rows) == 250
    assert [row["id"] for row in rows] == ["duplicate", *[f"p-{index}" for index in range(249)]]


def test_list_all_profiles_rejects_unbounded_full_duplicate_pages(monkeypatch):
    controller = AdsPowerController(max_retries=1)
    calls = []

    def list_page(*, page, page_size):
        calls.append(page)
        return [{"id": "duplicate"}], page_size, None

    monkeypatch.setattr(controller, "_list_profile_page", list_page)

    with pytest.raises(AdsPowerError, match="AdsPower profile pagination limit exceeded"):
        controller.list_all_profiles(page_size=200)

    assert calls == list(range(1, 27))


def test_list_all_profiles_propagates_a_later_page_error(monkeypatch):
    controller = AdsPowerController(max_retries=1)

    def list_page(*, page, page_size):
        if page == 2:
            raise AdsPowerError("page-two-failed")
        return [{"id": f"p-{index}"} for index in range(page_size)], page_size, None

    monkeypatch.setattr(controller, "_list_profile_page", list_page)

    with pytest.raises(AdsPowerError, match="page-two-failed"):
        controller.list_all_profiles(page_size=200)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": True},
        {"page_size": "200"},
        {"page_size": 200.0},
        {"page_size": 0},
        {"page_size": 201},
        {"max_profiles": True},
        {"max_profiles": "1000"},
        {"max_profiles": 1000.0},
        {"max_profiles": 0},
        {"max_profiles": 5001},
    ],
)
def test_list_all_profiles_validates_limits(kwargs):
    with pytest.raises(ValueError):
        AdsPowerController().list_all_profiles(**kwargs)
