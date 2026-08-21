from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from tiktok_stats.client import (
    AccountNotFound,
    AccountPrivate,
    ContractChanged,
    CookieInvalid,
    TikTokApiClient,
    UPSTREAM_CONTRACT_COMMIT,
    UpstreamUnavailable,
)


FIXTURES = Path(__file__).parent / "fixtures" / "tiktok"
COOKIE = "session" + "id=" + "test-cookie-value"
UPSTREAM_SOURCE = "app/api/endpoints/tiktok_web.py"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, *, params, headers, timeout):
        self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_for(*outcomes):
    session = FakeSession(outcomes)
    return TikTokApiClient("http://127.0.0.1:53281", lambda: COOKIE, session=session, timeout=7), session


def test_resolve_sec_uid_maps_username_and_lazily_supplies_cookie():
    client, session = client_for(FakeResponse(fixture("sec_uid.json")))

    assert client.resolve_sec_uid("example_creator") == "MS4wLjABAAAA-example-sec-uid"
    assert session.calls == [
        {
            "url": "http://127.0.0.1:53281/api/tiktok/web/get_sec_user_id",
            "params": {"url": "https://www.tiktok.com/@example_creator"},
            "headers": {"Cookie": COOKIE},
            "timeout": 7,
        }
    ]


def test_fork_preserves_configuration_and_creates_a_fresh_session(monkeypatch):
    client, original_session = client_for()
    fresh_session = object()
    monkeypatch.setattr("tiktok_stats.client.requests.Session", lambda: fresh_session)

    forked = client.fork()

    assert forked is not client
    assert forked.base_url == client.base_url
    assert forked._cookie_provider is client._cookie_provider
    assert forked._timeout == client._timeout
    assert forked._session is fresh_session
    assert forked._session is not original_session


def test_fetch_profile_normalizes_profile_counters():
    client, session = client_for(FakeResponse(fixture("profile.json"), status_code=201))

    profile = client.fetch_profile("MS4wLjABAAAA-example-sec-uid")

    assert profile.sec_uid == "MS4wLjABAAAA-example-sec-uid"
    assert profile.username == "example_creator"
    assert profile.follower_count == 1200
    assert profile.following_count == 34
    assert profile.likes_count == 5678
    assert profile.post_count == 9
    assert session.calls[0]["params"] == {"secUid": "MS4wLjABAAAA-example-sec-uid"}


def test_iter_posts_normalizes_counters_and_stops_after_final_cursor():
    client, session = client_for(
        FakeResponse(fixture("posts_page_1.json")),
        FakeResponse(fixture("posts_page_2.json")),
    )

    pages = list(client.iter_posts("MS4wLjABAAAA-example-sec-uid", cursor=0))

    assert [[post.video_id for post in page.posts] for page in pages] == [["video-1"], ["video-2"]]
    assert pages[0].posts[0].view_count == 101
    assert pages[0].posts[0].like_count == 12
    assert pages[0].posts[0].comment_count == 3
    assert pages[0].posts[0].share_count == 4
    assert pages[0].next_cursor == 1719999999
    assert pages[1].next_cursor is None
    assert [call["params"] for call in session.calls] == [
        {"secUid": "MS4wLjABAAAA-example-sec-uid", "cursor": 0},
        {"secUid": "MS4wLjABAAAA-example-sec-uid", "cursor": 1719999999},
    ]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (requests.Timeout("upstream timeout"), UpstreamUnavailable),
        (FakeResponse({"detail": "unavailable"}, status_code=503), UpstreamUnavailable),
    ],
)
def test_transient_failure_uses_one_attempt_and_leaves_retry_to_collector(outcome, expected):
    client, session = client_for(outcome)

    with pytest.raises(expected) as raised:
        client.resolve_sec_uid("example_creator")

    assert len(session.calls) == 1
    assert raised.value.__cause__ is None


def test_missing_local_cookie_provider_raises_cookie_invalid_without_a_request():
    session = FakeSession([])
    client = TikTokApiClient("http://127.0.0.1:53281", lambda: "", session=session)

    with pytest.raises(CookieInvalid):
        client.resolve_sec_uid("example_creator")

    assert session.calls == []


@pytest.mark.parametrize(
    ("status_message", "expected"),
    [
        ("Cookie has expired", CookieInvalid),
        ("user not found", AccountNotFound),
        ("private account", AccountPrivate),
    ],
)
def test_data_layer_failures_with_valid_wrapper_raise_stable_semantic_exceptions(
    status_message, expected
):
    payload = {
        "code": 200,
        "router": "/api/tiktok/web/get_sec_user_id",
        "data": {"statusCode": 1, "statusMsg": status_message},
    }
    client, _ = client_for(FakeResponse(payload))

    with pytest.raises(expected) as raised:
        client.resolve_sec_uid("example_creator")

    assert COOKIE not in str(raised.value)
    assert raised.value.summary["endpoint"] == "get_sec_user_id"
    assert raised.value.summary["status_code"] == 200
    assert raised.value.summary["response_keys"] == ["code", "data", "router"]


def test_fastapi_http_error_detail_is_upstream_unavailable_and_redacted():
    payload = {
        "detail": {
            "code": 400,
            "message": "An error occurred.",
            "router": "/api/tiktok/web/get_sec_user_id",
            "params": {"Cookie": COOKIE},
        }
    }
    client, _ = client_for(FakeResponse(payload, status_code=400))

    with pytest.raises(UpstreamUnavailable) as raised:
        client.resolve_sec_uid("example_creator")

    assert raised.value.summary == {
        "endpoint": "get_sec_user_id",
        "status_code": 400,
        "response_keys": ["detail"],
        "message": "An error occurred.",
    }
    assert COOKIE not in repr(raised.value.summary)


def test_error_summary_removes_even_an_unlabelled_cookie_echo():
    bare_cookie = "bare-cookie-secret"
    session = FakeSession(
        [
            FakeResponse(
                {
                    "code": 200,
                    "router": "/api/tiktok/web/get_sec_user_id",
                    "data": {"statusCode": 1, "statusMsg": f"Cookie denied {bare_cookie}"},
                }
            )
        ]
    )
    client = TikTokApiClient(
        "http://127.0.0.1:53281", lambda: bare_cookie, session=session
    )

    with pytest.raises(CookieInvalid) as raised:
        client.resolve_sec_uid("example_creator")

    assert bare_cookie not in str(raised.value)
    assert bare_cookie not in repr(raised.value.summary)


def test_changed_response_shape_fails_before_partial_normalization():
    client, _ = client_for(FakeResponse({"code": 200, "data": {"userInfo": {"stats": {}}}}))

    with pytest.raises(ContractChanged) as raised:
        client.fetch_profile("MS4wLjABAAAA-example-sec-uid")

    assert raised.value.summary == {
        "endpoint": "fetch_user_profile",
        "status_code": 200,
        "response_keys": ["code", "data"],
        "message": "response contract changed",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"code": 200, "data": "MS4wLjABAAAA-example-sec-uid"},
        {
            "code": 200,
            "router": "/api/tiktok/web/fetch_user_profile",
            "data": "MS4wLjABAAAA-example-sec-uid",
        },
    ],
)
def test_success_wrapper_requires_the_expected_router(payload):
    client, _ = client_for(FakeResponse(payload))

    with pytest.raises(ContractChanged) as raised:
        client.resolve_sec_uid("example_creator")

    assert raised.value.summary["endpoint"] == "get_sec_user_id"
    assert raised.value.summary["status_code"] == 200


def test_contract_commit_is_recorded_for_fixture_review():
    assert UPSTREAM_CONTRACT_COMMIT == "42784ffc83a72a516bfe952153ad7e2a3998d16c"
    assert UPSTREAM_SOURCE == "app/api/endpoints/tiktok_web.py"


def test_post_cursor_cycle_stops_before_repeating_a_request():
    first = fixture("posts_page_1.json")
    first["data"]["cursor"] = 5
    second = fixture("posts_page_1.json")
    second["data"]["cursor"] = 0
    client, session = client_for(FakeResponse(first), FakeResponse(second))

    with pytest.raises(ContractChanged) as raised:
        list(client.iter_posts("MS4wLjABAAAA-example-sec-uid", cursor=0))

    assert len(session.calls) == 2
    assert raised.value.summary["endpoint"] == "fetch_user_post"
    assert raised.value.summary["status_code"] == 200


def test_non_initial_post_cursor_cycle_stops_before_repeating_a_request():
    first = fixture("posts_page_1.json")
    first["data"]["cursor"] = 5
    second = fixture("posts_page_1.json")
    second["data"]["cursor"] = 10
    third = fixture("posts_page_1.json")
    third["data"]["cursor"] = 5
    client, session = client_for(FakeResponse(first), FakeResponse(second), FakeResponse(third))

    with pytest.raises(ContractChanged) as raised:
        list(client.iter_posts("MS4wLjABAAAA-example-sec-uid", cursor=0))

    assert len(session.calls) == 3
    assert [call["params"]["cursor"] for call in session.calls] == [0, 5, 10]
    assert raised.value.summary["endpoint"] == "fetch_user_post"


def test_default_cursor_is_zero_and_a_zero_cursor_cycle_stops_after_one_request():
    response = fixture("posts_page_1.json")
    response["data"]["cursor"] = 0
    client, session = client_for(FakeResponse(response))

    with pytest.raises(ContractChanged) as raised:
        list(client.iter_posts("MS4wLjABAAAA-example-sec-uid"))

    assert [call["params"] for call in session.calls] == [
        {"secUid": "MS4wLjABAAAA-example-sec-uid", "cursor": 0}
    ]
    assert raised.value.summary["endpoint"] == "fetch_user_post"


def test_post_contract_failure_retains_endpoint_status_and_bounded_redacted_keys():
    payload = {
        "code": 200,
        "router": "/api/tiktok/web/fetch_user_post",
        "Cookie": "do-not-expose",
        COOKIE: "do-not-expose",
        **{f"very-long-key-{index}-" + ("x" * 120): index for index in range(40)},
        "data": {"itemList": "not-a-list", "hasMore": False, "cursor": 0},
    }
    client, _ = client_for(FakeResponse(payload, status_code=202))

    with pytest.raises(ContractChanged) as raised:
        list(client.iter_posts("MS4wLjABAAAA-example-sec-uid", cursor=0))

    summary = raised.value.summary
    assert summary["endpoint"] == "fetch_user_post"
    assert summary["status_code"] == 202
    assert len(summary["response_keys"]) <= 20
    assert all(len(key) <= 80 for key in summary["response_keys"])
    assert "Cookie" not in repr(summary)
    assert COOKIE not in repr(summary)
    assert len(repr(summary)) <= 2_200


@pytest.mark.parametrize(
    "base_url",
    [
        "https://127.0.0.1:53281",
        "http://localhost:53281",
        "http://192.168.1.10:53281",
        "http://127.0.0.1:53281/path",
    ],
)
def test_client_rejects_non_loopback_http_service_urls(base_url):
    with pytest.raises(ValueError):
        TikTokApiClient(base_url, lambda: COOKIE, session=FakeSession([]))
