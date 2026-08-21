"""Deterministic incremental and full TikTok collection pipelines."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .client import (
    AccountNotFound,
    AccountPrivate,
    ContractChanged,
    CookieInvalid,
    PostSnapshot,
    ProfileSnapshot,
    TikTokApiError,
    UpstreamUnavailable,
)
from .secrets import redact_text


@dataclass(frozen=True)
class AccountCollectionResult:
    account_id: int
    status: str
    snapshot_id: int | None = None
    posts_seen: int = 0
    retry_count: int = 0
    error_code: str | None = None
    error_message: str | None = None
    baseline_status: str | None = None
    cookie_status_error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True)
class RunResult:
    run_id: int
    run_type: str
    status: str
    account_results: tuple[AccountCollectionResult, ...]
    cookie_status_error: str | None = None

    @property
    def results(self) -> tuple[AccountCollectionResult, ...]:
        return self.account_results

    @property
    def success_count(self) -> int:
        return sum(result.succeeded for result in self.account_results)

    @property
    def failure_count(self) -> int:
        return len(self.account_results) - self.success_count

    @property
    def retry_count(self) -> int:
        return sum(result.retry_count for result in self.account_results)


@dataclass(frozen=True)
class _StagedCollection:
    account_id: int
    sec_uid: str
    profile: ProfileSnapshot
    posts: tuple[PostSnapshot, ...]
    retry_count: int


@dataclass(frozen=True)
class _FetchOutcome:
    account_id: int
    staged: _StagedCollection | None
    retry_count: int
    error: Exception | None


@dataclass
class _BreakerState:
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    notified: bool = False
    status_update_error: str | None = None


class _CookieCircuitOpen(RuntimeError):
    pass


class Collector:
    """Collect accounts with bounded retries and account-scoped atomic writes."""

    def __init__(
        self,
        store,
        client,
        clock,
        sleeper: Callable[[float], Any],
        rng,
        *,
        max_workers: int = 4,
        max_attempts: int = 3,
        base_retry_delay: float = 1.0,
        base_request_delay: float = 0.25,
        max_incremental_pages: int = 20,
        cookie_status_callback: Callable[[bool], Any] | Any | None = None,
        client_factory: Callable[[], Any] | None = None,
    ):
        if not isinstance(max_workers, int) or isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        if base_retry_delay < 0:
            raise ValueError("base_retry_delay must be non-negative")
        if base_request_delay < 0:
            raise ValueError("base_request_delay must be non-negative")
        if (
            not isinstance(max_incremental_pages, int)
            or isinstance(max_incremental_pages, bool)
            or max_incremental_pages < 1
        ):
            raise ValueError("max_incremental_pages must be a positive integer")
        if not callable(sleeper):
            raise TypeError("sleeper must be callable")
        if client_factory is not None and not callable(client_factory):
            raise TypeError("client_factory must be callable")
        self.store = store
        self.client = client
        self.clock = clock
        self.sleeper = sleeper
        self.rng = rng
        self.max_workers = max_workers
        self.max_attempts = max_attempts
        self.base_retry_delay = float(base_retry_delay)
        self.base_request_delay = float(base_request_delay)
        self.max_incremental_pages = max_incremental_pages
        self.cookie_status_callback = cookie_status_callback
        self.client_factory = client_factory
        self._timing_lock = threading.Lock()

    def collect_incremental(self, account_id: int, run_id: int | None) -> AccountCollectionResult:
        return self._collect_one(
            account_id, run_id, full=False, business_date=None, breaker=_BreakerState()
        )

    def collect_full(
        self, account_id: int, run_id: int | None, business_date: str
    ) -> AccountCollectionResult:
        return self._collect_one(
            account_id,
            run_id,
            full=True,
            business_date=business_date,
            breaker=_BreakerState(),
        )

    def run_collection(
        self, run_type: str, account_ids: Sequence[int] | None = None
    ) -> RunResult:
        if run_type not in {"incremental", "full"}:
            raise ValueError("run_type must be 'incremental' or 'full'")
        breaker = _BreakerState()
        business_date = self._shanghai_business_date() if run_type == "full" else None
        unique_account_ids = None if account_ids is None else _deduplicate_ids(account_ids)
        accounts = self.store.enabled_accounts(unique_account_ids)
        run_id = self.store.start_run(run_type)
        work = [
            (
                account,
                self.store.known_post_ids(int(account["id"]))
                if run_type == "incremental"
                else set(),
            )
            for account in accounts
        ]

        isolated_clients = self.client_factory is not None or callable(
            getattr(self.client, "fork", None)
        )
        worker_count = self.max_workers if isolated_clients else 1
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(
                    self._fetch_worker_outcome,
                    account,
                    run_type == "full",
                    known_posts,
                    breaker,
                    isolated_clients,
                )
                for account, known_posts in work
            ]
            outcomes = [future.result() for future in futures]

        results = tuple(
            self._persist_outcome(
                outcome,
                run_id=run_id,
                full=run_type == "full",
                business_date=business_date,
            )
            for outcome in outcomes
        )
        status = _run_status(results)
        self.store.finish_run(
            run_id,
            status,
            details_json=_run_details(results, breaker.status_update_error),
        )
        return RunResult(
            run_id=run_id,
            run_type=run_type,
            status=status,
            account_results=results,
            cookie_status_error=breaker.status_update_error,
        )

    def _collect_one(
        self,
        account_id: int,
        run_id: int | None,
        *,
        full: bool,
        business_date: str | None,
        breaker: _BreakerState,
    ) -> AccountCollectionResult:
        account = self.store.account_by_id(account_id)
        if account is None or account["status"] != "enabled":
            return AccountCollectionResult(
                account_id=account_id,
                status="failed",
                error_code="account_unavailable",
                error_message="tracked account is unavailable",
            )
        known_posts = set() if full else self.store.known_post_ids(account_id)
        outcome = self._fetch_outcome(account, full, known_posts, breaker, self.client)
        result = self._persist_outcome(
            outcome,
            run_id=run_id,
            full=full,
            business_date=business_date,
        )
        if breaker.status_update_error is not None:
            result = replace(result, cookie_status_error=breaker.status_update_error)
        return result

    def _fetch_worker_outcome(
        self,
        account: Mapping[str, Any],
        full: bool,
        known_posts: set[str],
        breaker: _BreakerState,
        isolated_client: bool,
    ) -> _FetchOutcome:
        client = None
        try:
            client = self._new_worker_client() if isolated_client else self.client
            return self._fetch_outcome(account, full, known_posts, breaker, client)
        except Exception as error:
            return _FetchOutcome(
                account_id=int(account["id"]),
                staged=None,
                retry_count=0,
                error=error,
            )
        finally:
            if isolated_client and client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    def _new_worker_client(self):
        if self.client_factory is not None:
            return self.client_factory()
        return self.client.fork()

    def _fetch_outcome(
        self,
        account: Mapping[str, Any],
        full: bool,
        known_posts: set[str],
        breaker: _BreakerState,
        client,
    ) -> _FetchOutcome:
        account_id = int(account["id"])
        retries = [0]
        request_count = [0]
        try:
            self._ensure_requests_allowed(breaker)
            sec_uid = account.get("sec_uid")
            if not isinstance(sec_uid, str) or not sec_uid:
                sec_uid = self._request(
                    lambda: client.resolve_sec_uid(str(account["username"])),
                    retries,
                    request_count,
                    breaker,
                )
            profile = self._request(
                lambda: client.fetch_profile(sec_uid), retries, request_count, breaker
            )
            posts = self._fetch_posts(
                sec_uid, full, known_posts, retries, request_count, breaker, client
            )
            return _FetchOutcome(
                account_id=account_id,
                staged=_StagedCollection(
                    account_id=account_id,
                    sec_uid=profile.sec_uid,
                    profile=profile,
                    posts=posts,
                    retry_count=retries[0],
                ),
                retry_count=retries[0],
                error=None,
            )
        except Exception as error:
            return _FetchOutcome(
                account_id=account_id,
                staged=None,
                retry_count=retries[0],
                error=error,
            )

    def _fetch_posts(
        self,
        sec_uid: str,
        full: bool,
        known_posts: set[str],
        retries: list[int],
        request_count: list[int],
        breaker: _BreakerState,
        client,
    ) -> tuple[PostSnapshot, ...]:
        cursor: int | None = None
        collected: list[PostSnapshot] = []
        seen: set[str] = set()
        requested_cursors = {0}
        pages_seen = 0
        while True:
            page = self._request(
                lambda cursor=cursor: next(client.iter_posts(sec_uid, cursor=cursor)),
                retries,
                request_count,
                breaker,
            )
            pages_seen += 1
            reached_known = False
            for post in page.posts:
                if post.video_id in seen:
                    raise ContractChanged(
                        {
                            "endpoint": "fetch_user_post",
                            "status_code": None,
                            "response_keys": [],
                            "message": "duplicate post in pagination",
                        }
                    )
                seen.add(post.video_id)
                collected.append(post)
                if not full and post.video_id in known_posts:
                    reached_known = True
                    break
            if reached_known or page.next_cursor is None:
                return tuple(collected)
            if not full and pages_seen >= self.max_incremental_pages:
                return tuple(collected)
            if page.next_cursor in requested_cursors:
                raise ContractChanged(
                    {
                        "endpoint": "fetch_user_post",
                        "status_code": None,
                        "response_keys": [],
                        "message": "post pagination cursor cycle",
                    }
                )
            cursor = page.next_cursor
            requested_cursors.add(cursor)

    def _request(
        self,
        operation: Callable[[], Any],
        retries: list[int],
        request_count: list[int],
        breaker: _BreakerState,
    ) -> Any:
        self._pace_request(request_count)
        for attempt in range(1, self.max_attempts + 1):
            self._ensure_requests_allowed(breaker)
            try:
                return operation()
            except CookieInvalid:
                self._open_cookie_breaker(breaker)
                raise
            except UpstreamUnavailable as error:
                if not _temporary_upstream_failure(error) or attempt == self.max_attempts:
                    raise
                with self._timing_lock:
                    jitter = float(self.rng.random()) * self.base_retry_delay
                    delay = self.base_retry_delay * (2 ** (attempt - 1)) + jitter
                    self.sleeper(delay)
                retries[0] += 1
        raise AssertionError("unreachable retry state")

    def _persist_outcome(
        self,
        outcome: _FetchOutcome,
        *,
        run_id: int | None,
        full: bool,
        business_date: str | None,
    ) -> AccountCollectionResult:
        if outcome.error is not None:
            return _error_result(outcome.account_id, outcome.error, outcome.retry_count)
        staged = outcome.staged
        if staged is None:
            raise AssertionError("successful fetch outcome has no staged data")
        captured_at = self._utc_now_iso()
        try:
            self.store.cache_sec_uid(staged.account_id, staged.sec_uid)
            profile = _profile_mapping(staged.profile)
            posts = tuple(_post_mapping(post) for post in staged.posts)
            if full:
                if business_date is None:
                    raise ValueError("business_date is required for full collection")
                snapshot_id, baseline_status = self.store.record_complete_collection(
                    staged.account_id,
                    captured_at=captured_at,
                    business_date=business_date,
                    profile=profile,
                    posts=posts,
                    run_id=run_id,
                )
            else:
                snapshot_id = self.store.record_incremental_collection(
                    staged.account_id,
                    captured_at=captured_at,
                    business_date=self._business_date_from_utc(captured_at),
                    profile=profile,
                    posts=posts,
                    run_id=run_id,
                )
                baseline_status = None
        except Exception as error:
            return _error_result(staged.account_id, error, staged.retry_count)
        return AccountCollectionResult(
            account_id=staged.account_id,
            status="completed",
            snapshot_id=snapshot_id,
            posts_seen=len(staged.posts),
            retry_count=staged.retry_count,
            baseline_status=baseline_status,
        )

    def _pace_request(self, request_count: list[int]) -> None:
        if request_count[0] and self.base_request_delay:
            with self._timing_lock:
                delay = self.base_request_delay * (1 + float(self.rng.random()))
                self.sleeper(delay)
        request_count[0] += 1

    @staticmethod
    def _ensure_requests_allowed(breaker: _BreakerState) -> None:
        if breaker.event.is_set():
            raise _CookieCircuitOpen("Cookie circuit breaker is open")

    def _open_cookie_breaker(self, breaker: _BreakerState) -> None:
        breaker.event.set()
        with breaker.lock:
            if breaker.notified:
                return
            breaker.notified = True
            callback = self.cookie_status_callback
            if callback is None:
                return
            try:
                if callable(callback):
                    callback(False)
                elif hasattr(callback, "mark_validation"):
                    callback.mark_validation(
                        False, "Cookie validation failed", self._clock_datetime()
                    )
                else:
                    raise TypeError(
                        "cookie_status_callback must be callable or expose mark_validation"
                    )
            except Exception:
                breaker.status_update_error = "cookie_status_update_failed"

    def _clock_value(self) -> datetime | str:
        if callable(self.clock):
            return self.clock()
        if hasattr(self.clock, "now"):
            return self.clock.now()
        raise TypeError("clock must be callable or expose now()")

    def _utc_now_iso(self) -> str:
        parsed = self._clock_datetime()
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _clock_datetime(self) -> datetime:
        value = self._clock_value()
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            parsed = value
        else:
            raise TypeError("clock must return datetime or ISO timestamp")
        if parsed.tzinfo is None:
            raise ValueError("clock datetime must be timezone-aware")
        return parsed

    def _shanghai_business_date(self) -> str:
        return self._clock_datetime().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()

    @staticmethod
    def _business_date_from_utc(captured_at: str) -> str:
        parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        return parsed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def _profile_mapping(profile: ProfileSnapshot) -> dict[str, int]:
    return {
        "follower_count": profile.follower_count,
        "following_count": profile.following_count,
        "likes_count": profile.likes_count,
        "post_count": profile.post_count,
    }


def _post_mapping(post: PostSnapshot) -> dict[str, int | str]:
    return {
        "video_id": post.video_id,
        "created_at": post.created_at,
        "description": post.description,
        "view_count": post.view_count,
        "like_count": post.like_count,
        "comment_count": post.comment_count,
        "share_count": post.share_count,
    }


def _error_result(
    account_id: int, error: Exception, retry_count: int
) -> AccountCollectionResult:
    error_code = _error_code(error)
    if isinstance(error, TikTokApiError):
        message = redact_text(str(error))[:300]
    elif isinstance(error, _CookieCircuitOpen):
        message = "Cookie circuit breaker is open"
    else:
        message = "collection failed"
    return AccountCollectionResult(
        account_id=account_id,
        status="failed",
        retry_count=retry_count,
        error_code=error_code,
        error_message=message,
    )


def _error_code(error: Exception) -> str:
    if isinstance(error, CookieInvalid):
        return "cookie_invalid"
    if isinstance(error, _CookieCircuitOpen):
        return "cookie_circuit_open"
    if isinstance(error, AccountNotFound):
        return "account_not_found"
    if isinstance(error, AccountPrivate):
        return "account_private"
    if isinstance(error, ContractChanged):
        return "contract_changed"
    if isinstance(error, UpstreamUnavailable):
        return "upstream_unavailable"
    return "collection_error"


def _temporary_upstream_failure(error: UpstreamUnavailable) -> bool:
    status_code = error.summary.get("status_code")
    return status_code is None or status_code in {408, 425, 429} or (
        isinstance(status_code, int)
        and not isinstance(status_code, bool)
        and 500 <= status_code <= 599
    )


def _run_status(results: Sequence[AccountCollectionResult]) -> str:
    successes = sum(result.succeeded for result in results)
    if successes == len(results):
        return "completed"
    if successes:
        return "partial"
    return "failed"


def _run_details(
    results: Sequence[AccountCollectionResult], cookie_status_error: str | None = None
) -> str:
    payload = {
        "account_count": len(results),
        "success_count": sum(result.succeeded for result in results),
        "failure_count": sum(not result.succeeded for result in results),
        "retry_count": sum(result.retry_count for result in results),
        "errors": [
            {
                "account_id": result.account_id,
                "code": result.error_code,
                "message": result.error_message,
            }
            for result in results
            if not result.succeeded
        ],
        "cookie_status_error": cookie_status_error,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _deduplicate_ids(account_ids: Sequence[int]) -> list[int]:
    return list(dict.fromkeys(int(account_id) for account_id in account_ids))
