from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest

from tiktok_stats.client import (
    AccountNotFound,
    ContractChanged,
    CookieInvalid,
    PostPage,
    PostSnapshot,
    ProfileSnapshot,
    UpstreamUnavailable,
)
from tiktok_stats.collector import Collector
from tiktok_stats.secrets import CookieSecretStore
from tiktok_stats.store import StatsStore


def api_error(error_type, endpoint="test", message="synthetic failure", status_code=None):
    return error_type(
        {
            "endpoint": endpoint,
            "status_code": status_code,
            "response_keys": [],
            "message": message,
        }
    )


def post(video_id, views, likes=1, comments=1, shares=0):
    return PostSnapshot(
        video_id=video_id,
        created_at=1_700_000_000,
        description=f"post {video_id}",
        view_count=views,
        like_count=likes,
        comment_count=comments,
        share_count=shares,
    )


def profile(sec_uid, *, posts, likes, followers=100, following=5):
    return ProfileSnapshot(
        sec_uid=sec_uid,
        username="creator",
        follower_count=followers,
        following_count=following,
        likes_count=likes,
        post_count=posts,
    )


class FakeClock:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self):
        if len(self.values) > 1:
            return self.values.pop(0)
        return self.values[0]


class FakeRng:
    def __init__(self, value=0.25):
        self.value = value

    def random(self):
        return self.value


class FakeProtector:
    def protect(self, value):
        return value[::-1]

    def unprotect(self, value):
        return value[::-1]


class FakeClient:
    def __init__(self, *, resolved=None, profiles=None, pages=None):
        self.resolved = dict(resolved or {})
        self.profiles = dict(profiles or {})
        self.pages = dict(pages or {})
        self.calls = []

    @staticmethod
    def _value(source, key):
        value = source[key]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    def resolve_sec_uid(self, username):
        self.calls.append(("resolve", username))
        return self._value(self.resolved, username)

    def fetch_profile(self, sec_uid):
        self.calls.append(("profile", sec_uid))
        return self._value(self.profiles, sec_uid)

    def iter_posts(self, sec_uid, *, cursor=None):
        normalized_cursor = 0 if cursor is None else cursor
        self.calls.append(("posts", sec_uid, normalized_cursor))
        yield self._value(self.pages, (sec_uid, normalized_cursor))


def make_collector(store, client, clock, **options):
    sleeps = []
    options.setdefault("base_request_delay", 0)
    collector = Collector(store, client, clock, sleeps.append, FakeRng(), **options)
    return collector, sleeps


def rows(store, sql, parameters=()):
    return [dict(row) for row in store.connection.execute(sql, parameters)]


def test_incremental_resolves_and_caches_sec_uid_stops_on_known_post_and_never_writes_daily(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.replace_full_posts(account["id"], [{"video_id": "known", "view_count": 1}])
    client = FakeClient(
        resolved={"Creator": "sec-1"},
        profiles={"sec-1": profile("sec-1", posts=3, likes=30)},
        pages={
            ("sec-1", 0): PostPage((post("new", 9),), 10),
            ("sec-1", 10): PostPage((post("known", 7), post("older", 3)), 20),
        },
    )
    collector, _ = make_collector(
        store, client, FakeClock(datetime(2026, 7, 22, 1, tzinfo=UTC))
    )

    result = collector.collect_incremental(account["id"], run_id=None)

    assert result.status == "completed"
    assert client.calls == [
        ("resolve", "Creator"),
        ("profile", "sec-1"),
        ("posts", "sec-1", 0),
        ("posts", "sec-1", 10),
    ]
    assert store.account_by_id(account["id"])["sec_uid"] == "sec-1"
    snapshot = rows(store, "SELECT * FROM account_snapshots")[0]
    assert (snapshot["snapshot_type"], snapshot["coverage"]) == (
        "incremental",
        "profile_recent",
    )
    assert snapshot["business_date"] == "2026-07-22"
    assert {
        row["video_id"]: row["view_count"]
        for row in rows(store, "SELECT video_id, view_count FROM posts_current")
    } == {"known": 7, "new": 9}
    assert store.count_rows("daily_account_metrics") == 0
    store.close()


def test_incremental_uses_cached_sec_uid_and_retries_only_temporary_failures_with_jitter(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "cached-sec")
    client = FakeClient(
        profiles={
            "cached-sec": [
                api_error(UpstreamUnavailable, "profile"),
                profile("cached-sec", posts=1, likes=2),
            ]
        },
        pages={("cached-sec", 0): PostPage((post("one", 4),), None)},
    )
    collector, sleeps = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 2, tzinfo=UTC)),
        max_attempts=3,
        base_retry_delay=2.0,
    )

    result = collector.collect_incremental(account["id"], run_id=None)

    assert result.status == "completed"
    assert result.retry_count == 1
    assert sleeps == [2.5]
    assert client.calls[:2] == [
        ("profile", "cached-sec"),
        ("profile", "cached-sec"),
    ]
    assert not any(call[0] == "resolve" for call in client.calls)
    store.close()


def test_incremental_safety_page_limit_commits_profile_recent_without_an_extra_request(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={"sec": profile("sec", posts=3, likes=3)},
        pages={
            ("sec", 0): PostPage((post("a", 1),), 10),
            ("sec", 10): PostPage((post("b", 1),), 20),
        },
    )
    collector, _ = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 2, tzinfo=UTC)),
        max_incremental_pages=2,
    )

    result = collector.collect_incremental(account["id"], None)

    assert result.status == "completed"
    assert result.posts_seen == 2
    assert client.calls == [
        ("profile", "sec"),
        ("posts", "sec", 0),
        ("posts", "sec", 10),
    ]
    snapshot = rows(store, "SELECT * FROM account_snapshots")[0]
    assert snapshot["coverage"] == "profile_recent"
    store.close()


def test_request_spacing_uses_injected_sleeper_and_rng_between_serial_requests(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={"sec": profile("sec", posts=2, likes=2)},
        pages={
            ("sec", 0): PostPage((post("a", 1),), 10),
            ("sec", 10): PostPage((post("b", 1),), None),
        },
    )
    collector, sleeps = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 2, tzinfo=UTC)),
        base_request_delay=1,
    )

    assert collector.collect_incremental(account["id"], None).status == "completed"
    assert sleeps == [1.25, 1.25]
    store.close()


def test_run_isolates_account_failure_and_finishes_partial(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    missing = store.upsert_account("Missing", "missing")
    good = store.upsert_account("Good", "good")
    client = FakeClient(
        resolved={
            "Missing": api_error(AccountNotFound, "resolve", "account not found"),
            "Good": "good-sec",
        },
        profiles={"good-sec": profile("good-sec", posts=0, likes=0)},
        pages={("good-sec", 0): PostPage((), None)},
    )
    collector, sleeps = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 3, tzinfo=UTC)),
        max_workers=1,
    )

    result = collector.run_collection("incremental", [missing["id"], good["id"]])

    assert result.status == "partial"
    assert [item.status for item in result.account_results] == ["failed", "completed"]
    assert result.success_count == 1
    assert result.failure_count == 1
    assert sleeps == []
    assert store.count_rows("account_snapshots") == 1
    run = rows(store, "SELECT * FROM collection_runs")[0]
    assert run["status"] == "partial"
    assert "account_not_found" in run["details_json"]
    store.close()


def test_run_deduplicates_requested_account_ids_before_fetch_and_persistence(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={"sec": profile("sec", posts=0, likes=0)},
        pages={("sec", 0): PostPage((), None)},
    )
    collector, _ = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 3, tzinfo=UTC)),
        max_workers=1,
    )

    result = collector.run_collection(
        "incremental", [account["id"], account["id"], account["id"]]
    )

    assert [item.account_id for item in result.account_results] == [account["id"]]
    assert client.calls == [("profile", "sec"), ("posts", "sec", 0)]
    assert store.count_rows("account_snapshots") == 1
    store.close()


def test_multi_worker_run_uses_distinct_clients_and_overlaps_upstream_work(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    first = store.upsert_account("First", "first")
    second = store.upsert_account("Second", "second")
    store.cache_sec_uid(first["id"], "sec-first")
    store.cache_sec_uid(second["id"], "sec-second")
    barrier = threading.Barrier(2, timeout=2)
    state = {"created": [], "profiles": [], "closed": []}
    state_lock = threading.Lock()

    class IsolatedClient:
        def __init__(self, identity):
            self.identity = identity

        def fetch_profile(self, sec_uid):
            with state_lock:
                state["profiles"].append((self.identity, sec_uid))
            barrier.wait()
            return profile(sec_uid, posts=0, likes=0)

        def iter_posts(self, sec_uid, *, cursor=None):
            yield PostPage((), None)

        def close(self):
            with state_lock:
                state["closed"].append(self.identity)

    def client_factory():
        with state_lock:
            identity = len(state["created"]) + 1
            state["created"].append(identity)
        return IsolatedClient(identity)

    collector, _ = make_collector(
        store,
        object(),
        FakeClock(datetime(2026, 7, 22, 3, tzinfo=UTC)),
        max_workers=2,
        client_factory=client_factory,
    )

    result = collector.run_collection("incremental", [first["id"], second["id"]])

    assert result.success_count == 2
    assert state["created"] == [1, 2]
    assert {identity for identity, _ in state["profiles"]} == {1, 2}
    assert {sec_uid for _, sec_uid in state["profiles"]} == {"sec-first", "sec-second"}
    assert sorted(state["closed"]) == [1, 2]
    store.close()


def test_client_factory_failure_is_account_scoped_and_run_still_finishes(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    first = store.upsert_account("First", "first")
    second = store.upsert_account("Second", "second")
    store.cache_sec_uid(first["id"], "sec-first")
    store.cache_sec_uid(second["id"], "sec-second")
    attempts = [api_error(UpstreamUnavailable, "factory", "factory unavailable")]

    class WorkingClient:
        def fetch_profile(self, sec_uid):
            return profile(sec_uid, posts=0, likes=0)

        def iter_posts(self, sec_uid, *, cursor=None):
            yield PostPage((), None)

    def client_factory():
        if attempts:
            raise attempts.pop()
        return WorkingClient()

    collector, _ = make_collector(
        store,
        object(),
        FakeClock(datetime(2026, 7, 22, 3, tzinfo=UTC)),
        max_workers=1,
        client_factory=client_factory,
    )

    result = collector.run_collection("incremental", [first["id"], second["id"]])

    assert [item.status for item in result.account_results] == ["failed", "completed"]
    assert result.status == "partial"
    run = rows(store, "SELECT status FROM collection_runs")[0]
    assert run["status"] == "partial"
    store.close()


def test_permanent_upstream_http_failure_is_not_retried(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={
            "sec": api_error(
                UpstreamUnavailable,
                "profile",
                "bad request",
                status_code=400,
            )
        }
    )
    collector, sleeps = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 3, tzinfo=UTC)),
        max_attempts=3,
    )

    result = collector.collect_incremental(account["id"], None)

    assert result.error_code == "upstream_unavailable"
    assert result.retry_count == 0
    assert sleeps == []
    assert client.calls == [("profile", "sec")]
    store.close()


def test_full_traversal_builds_first_day_baseline_then_preserves_positive_and_negative_deltas(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={
            "sec": [
                profile("sec", posts=2, likes=20),
                profile("sec", posts=3, likes=15),
                profile("sec", posts=4, likes=17),
            ]
        },
        pages={
            ("sec", 0): [
                PostPage((post("a", 40, comments=4),), 10),
                PostPage((post("a", 80, comments=5), post("b", 50, comments=3)), None),
                PostPage((post("a", 90, comments=4), post("b", 60, comments=3)), None),
            ],
            ("sec", 10): PostPage((post("b", 60, comments=6),), None),
        },
    )
    collector, _ = make_collector(
        store,
        client,
        FakeClock(
            datetime(2026, 7, 21, 15, tzinfo=UTC),
            datetime(2026, 7, 22, 15, tzinfo=UTC),
            datetime(2026, 7, 22, 16, tzinfo=UTC),
        ),
    )

    first = collector.collect_full(account["id"], None, "2026-07-21")
    second = collector.collect_full(account["id"], None, "2026-07-22")
    replacement = collector.collect_full(account["id"], None, "2026-07-22")

    assert first.status == second.status == replacement.status == "completed"
    first_metric = store.daily_metric(account["id"], "2026-07-21")
    assert first_metric["baseline_status"] == "first_day"
    assert [first_metric[key] for key in ("posts_delta", "likes_delta", "views_delta", "comments_delta")] == [None] * 4
    assert [first_metric[key] for key in ("posts_total", "likes_total", "views_total", "comments_total")] == [2, 20, 100, 10]
    latest = store.daily_metric(account["id"], "2026-07-22")
    assert latest["baseline_status"] == "ready"
    assert [latest[key] for key in ("posts_delta", "likes_delta", "views_delta", "comments_delta")] == [2, -3, 50, -3]
    assert [latest[key] for key in ("posts_total", "likes_total", "views_total", "comments_total")] == [4, 17, 150, 7]
    assert latest["snapshot_id"] == replacement.snapshot_id
    full_snapshots = rows(store, "SELECT * FROM account_snapshots ORDER BY captured_at")
    assert [
        full_snapshots[-1][key]
        for key in (
            "covered_post_count",
            "posts_like_count",
            "posts_view_count",
            "posts_comment_count",
        )
    ] == [2, 2, 150, 7]
    assert client.calls.count(("posts", "sec", 10)) == 1
    store.close()


def test_missing_previous_day_keeps_all_deltas_null(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={"sec": [profile("sec", posts=1, likes=5), profile("sec", posts=2, likes=9)]},
        pages={
            ("sec", 0): [
                PostPage((post("a", 10),), None),
                PostPage((post("a", 20), post("b", 3)), None),
            ]
        },
    )
    collector, _ = make_collector(
        store,
        client,
        FakeClock(
            datetime(2026, 7, 20, 15, tzinfo=UTC),
            datetime(2026, 7, 22, 15, tzinfo=UTC),
        ),
    )

    collector.collect_full(account["id"], None, "2026-07-20")
    collector.collect_full(account["id"], None, "2026-07-22")

    metric = store.daily_metric(account["id"], "2026-07-22")
    assert metric["baseline_status"] == "missing_previous"
    assert [metric[key] for key in ("posts_delta", "likes_delta", "views_delta", "comments_delta")] == [None] * 4
    assert [metric[key] for key in ("posts_total", "likes_total", "views_total", "comments_total")] == [2, 9, 23, 2]
    store.close()


def test_collector_rejects_cross_iterator_cursor_cycle_before_duplicate_request(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={"sec": profile("sec", posts=2, likes=2)},
        pages={
            ("sec", 0): PostPage((post("a", 1),), 10),
            ("sec", 10): PostPage((post("b", 1),), 0),
        },
    )
    collector, _ = make_collector(
        store, client, FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC))
    )

    result = collector.collect_full(account["id"], None, "2026-07-22")

    assert result.status == "failed"
    assert result.error_code == "contract_changed"
    assert client.calls == [
        ("profile", "sec"),
        ("posts", "sec", 0),
        ("posts", "sec", 10),
    ]
    assert store.count_rows("account_snapshots") == 0
    store.close()


def test_interrupted_full_traversal_retains_prior_complete_data_and_deletions_wait_for_success(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(
        profiles={
            "sec": [
                profile("sec", posts=2, likes=10),
                profile("sec", posts=1, likes=12),
                profile("sec", posts=1, likes=12),
            ]
        },
        pages={
            ("sec", 0): [
                PostPage((post("a", 5), post("b", 5)), None),
                PostPage((post("a", 8),), 10),
                PostPage((post("a", 8),), None),
            ],
            ("sec", 10): api_error(ContractChanged, "posts", "response contract changed"),
        },
    )
    collector, _ = make_collector(
        store,
        client,
        FakeClock(
            datetime(2026, 7, 21, 15, tzinfo=UTC),
            datetime(2026, 7, 22, 15, tzinfo=UTC),
            datetime(2026, 7, 22, 16, tzinfo=UTC),
        ),
    )

    collector.collect_full(account["id"], None, "2026-07-21")
    interrupted = collector.collect_full(account["id"], None, "2026-07-22")

    assert interrupted.status == "failed"
    assert store.count_rows("account_snapshots") == 1
    assert store.daily_metric(account["id"], "2026-07-22") is None
    assert {row["video_id"]: row["is_deleted"] for row in rows(store, "SELECT * FROM posts_current")} == {"a": 0, "b": 0}

    completed = collector.collect_full(account["id"], None, "2026-07-22")

    assert completed.status == "completed"
    post_rows = {row["video_id"]: row for row in rows(store, "SELECT * FROM posts_current")}
    assert {video_id: row["is_deleted"] for video_id, row in post_rows.items()} == {
        "a": 0,
        "b": 1,
    }
    assert post_rows["a"]["deleted_detected_at"] is None
    assert post_rows["b"]["deleted_detected_at"] == "2026-07-22T15:00:00.000Z"
    store.upsert_recent_posts(
        account["id"],
        [{
            "video_id": "b",
            "view_count": 6,
            "like_count": 1,
            "comment_count": 1,
            "share_count": 0,
        }],
        observed_at="2026-07-22T17:00:00.000Z",
    )
    reappeared = rows(store, "SELECT * FROM posts_current WHERE video_id = 'b'")[0]
    assert reappeared["is_deleted"] == 0
    assert reappeared["deleted_detected_at"] is None
    store.close()


def test_cookie_invalid_opens_global_breaker_updates_status_and_starts_no_later_requests(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    first = store.upsert_account("First", "first")
    second = store.upsert_account("Second", "second")
    store.insert_snapshot(first["id"], captured_at="2026-07-20T00:00:00Z")
    client = FakeClient(
        resolved={
            "First": api_error(CookieInvalid, "resolve", "Cookie invalid"),
            "Second": "must-not-be-requested",
        }
    )
    cookie_states = []
    collector, _ = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC)),
        max_workers=1,
        cookie_status_callback=cookie_states.append,
    )

    result = collector.run_collection("incremental", [first["id"], second["id"]])

    assert result.status == "failed"
    assert [item.error_code for item in result.account_results] == [
        "cookie_invalid",
        "cookie_circuit_open",
    ]
    assert client.calls == [("resolve", "First")]
    assert cookie_states == [False]
    assert store.count_rows("account_snapshots") == 1
    assert rows(store, "SELECT status FROM collection_runs")[0]["status"] == "failed"
    store.close()


def test_cookie_invalid_updates_approved_secret_store_and_next_direct_collection_is_new_scope(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    first = store.upsert_account("First", "first")
    second = store.upsert_account("Second", "second")
    secret_store = CookieSecretStore(tmp_path / "cookie.json", protector=FakeProtector())
    secret_store.save_cookie("synthetic-test-cookie")
    client = FakeClient(
        resolved={
            "First": api_error(CookieInvalid, "resolve", "Cookie invalid"),
            "Second": "second-sec",
        },
        profiles={"second-sec": profile("second-sec", posts=0, likes=0)},
        pages={("second-sec", 0): PostPage((), None)},
    )
    collector, _ = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC)),
        cookie_status_callback=secret_store,
    )

    failed = collector.collect_incremental(first["id"], None)
    succeeded = collector.collect_incremental(second["id"], None)

    assert failed.error_code == "cookie_invalid"
    assert secret_store.public_status()["state"] == "invalid"
    assert secret_store.public_status()["checked_at"] == "2026-07-22T15:00:00Z"
    assert succeeded.status == "completed"
    store.close()


def test_cookie_status_callback_failure_does_not_mask_invalid_cookie(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    first = store.upsert_account("First", "first")
    second = store.upsert_account("Second", "second")
    client = FakeClient(
        resolved={
            "First": api_error(CookieInvalid, "resolve", "Cookie invalid"),
            "Second": "must-not-be-requested",
        }
    )

    def failing_callback(_valid):
        raise RuntimeError("sensitive callback detail")

    collector, _ = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC)),
        max_workers=1,
        cookie_status_callback=failing_callback,
    )

    result = collector.run_collection("incremental", [first["id"], second["id"]])

    assert [item.error_code for item in result.account_results] == [
        "cookie_invalid",
        "cookie_circuit_open",
    ]
    assert result.cookie_status_error == "cookie_status_update_failed"
    details = rows(store, "SELECT details_json FROM collection_runs")[0]["details_json"]
    assert json.loads(details)["cookie_status_error"] == "cookie_status_update_failed"
    assert "sensitive callback detail" not in details
    store.close()


def test_direct_collection_exposes_cookie_status_update_failure(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    client = FakeClient(
        resolved={"Creator": api_error(CookieInvalid, "resolve", "Cookie invalid")}
    )

    def failing_callback(_valid):
        raise RuntimeError("do not expose this detail")

    collector, _ = make_collector(
        store,
        client,
        FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC)),
        cookie_status_callback=failing_callback,
    )

    result = collector.collect_incremental(account["id"], None)

    assert result.error_code == "cookie_invalid"
    assert result.cookie_status_error == "cookie_status_update_failed"
    assert "do not expose this detail" not in repr(result)
    store.close()


def test_concurrent_cookie_breaker_allows_admitted_request_to_finish_but_no_later_request(
    tmp_path,
):
    store = StatsStore(tmp_path / "stats.db")
    first = store.upsert_account("First", "first")
    second = store.upsert_account("Second", "second")
    store.cache_sec_uid(first["id"], "sec-invalid")
    store.cache_sec_uid(second["id"], "sec-admitted")
    admitted = threading.Event()
    breaker_observed = threading.Event()
    calls = []
    notifications = []
    lock = threading.Lock()

    class BreakerClient:
        def fetch_profile(self, sec_uid):
            with lock:
                calls.append(("profile", sec_uid, id(self)))
            if sec_uid == "sec-admitted":
                admitted.set()
                assert breaker_observed.wait(2)
                return profile(sec_uid, posts=0, likes=0)
            assert admitted.wait(2)
            raise api_error(CookieInvalid, "profile", "Cookie invalid")

        def iter_posts(self, sec_uid, *, cursor=None):
            with lock:
                calls.append(("posts", sec_uid, id(self)))
            yield PostPage((), None)

    def notify(valid):
        notifications.append(valid)
        breaker_observed.set()

    collector, _ = make_collector(
        store,
        object(),
        FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC)),
        max_workers=2,
        client_factory=BreakerClient,
        cookie_status_callback=notify,
    )

    result = collector.run_collection("incremental", [first["id"], second["id"]])

    assert [item.error_code for item in result.account_results] == [
        "cookie_invalid",
        "cookie_circuit_open",
    ]
    assert notifications == [False]
    assert {call[1] for call in calls if call[0] == "profile"} == {
        "sec-invalid",
        "sec-admitted",
    }
    assert not any(call[0] == "posts" for call in calls)
    assert len({call[2] for call in calls}) == 2
    store.close()


def test_process_control_exception_is_not_converted_to_account_failure(tmp_path):
    store = StatsStore(tmp_path / "stats.db")
    account = store.upsert_account("Creator", "creator")
    store.cache_sec_uid(account["id"], "sec")
    client = FakeClient(profiles={"sec": KeyboardInterrupt()})
    collector, _ = make_collector(
        store, client, FakeClock(datetime(2026, 7, 22, 15, tzinfo=UTC))
    )

    with pytest.raises(KeyboardInterrupt):
        collector.collect_incremental(account["id"], None)
    store.close()
