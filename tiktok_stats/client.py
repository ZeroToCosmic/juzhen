"""Narrow, contract-checked adapter for the local TikTok scraper service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit

import requests

from .secrets import redact_secrets, redact_text


DEFAULT_TIMEOUT_SECONDS = 15
UPSTREAM_CONTRACT_COMMIT = "42784ffc83a72a516bfe952153ad7e2a3998d16c"
_MAX_ERROR_MESSAGE_LENGTH = 300
_MAX_RESPONSE_KEY_COUNT = 20
_MAX_RESPONSE_KEY_LENGTH = 80


@dataclass(frozen=True)
class ProfileSnapshot:
    sec_uid: str
    username: str
    follower_count: int
    following_count: int
    likes_count: int
    post_count: int


@dataclass(frozen=True)
class PostSnapshot:
    video_id: str
    created_at: int
    description: str
    view_count: int
    like_count: int
    comment_count: int
    share_count: int


@dataclass(frozen=True)
class PostPage:
    posts: tuple[PostSnapshot, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class _UpstreamResponse:
    payload: Mapping[str, Any]
    status_code: int
    response_keys: tuple[str, ...]


class TikTokApiError(RuntimeError):
    """A stable upstream failure with only bounded, non-secret diagnostics."""

    def __init__(self, summary: Mapping[str, Any]):
        self.summary = dict(summary)
        super().__init__(f"{self.summary['endpoint']}: {self.summary['message']}")


class CookieInvalid(TikTokApiError):
    pass


class AccountNotFound(TikTokApiError):
    pass


class AccountPrivate(TikTokApiError):
    pass


class UpstreamUnavailable(TikTokApiError):
    pass


class ContractChanged(TikTokApiError):
    pass


class TikTokApiClient:
    """Fetch and normalize the three supported local scraper endpoints."""

    def __init__(
        self,
        base_url: str,
        cookie_provider: Callable[[], str],
        session: requests.Session | Any | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.base_url = _validated_base_url(base_url)
        if not callable(cookie_provider):
            raise TypeError("cookie_provider must be callable")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("timeout must be a positive number")
        self._cookie_provider = cookie_provider
        self._session = session if session is not None else requests.Session()
        self._timeout = timeout

    def fork(self) -> "TikTokApiClient":
        """Clone immutable configuration while allocating an independent HTTP session."""
        return TikTokApiClient(
            self.base_url,
            self._cookie_provider,
            timeout=self._timeout,
        )

    def close(self) -> None:
        close = getattr(self._session, "close", None)
        if callable(close):
            close()

    def resolve_sec_uid(self, username: str) -> str:
        response = self._request(
            "get_sec_user_id",
            {"url": f"https://www.tiktok.com/@{_required_string(username, 'username')}"},
        )
        try:
            return _required_string(response.payload.get("data"), "data")
        except ContractChanged:
            raise self._contract_changed("get_sec_user_id", response) from None

    def fetch_profile(self, sec_uid: str) -> ProfileSnapshot:
        requested_sec_uid = _required_string(sec_uid, "sec_uid")
        response = self._request("fetch_user_profile", {"secUid": requested_sec_uid})
        try:
            data = _required_mapping(response.payload.get("data"), "data")
            user_info = _required_mapping(data.get("userInfo"), "data.userInfo")
            user = _required_mapping(user_info.get("user"), "data.userInfo.user")
            stats = _required_mapping(user_info.get("stats"), "data.userInfo.stats")
            return ProfileSnapshot(
                sec_uid=_required_string(user.get("secUid"), "data.userInfo.user.secUid"),
                username=_required_string(user.get("uniqueId"), "data.userInfo.user.uniqueId"),
                follower_count=_required_int(stats.get("followerCount"), "data.userInfo.stats.followerCount"),
                following_count=_required_int(stats.get("followingCount"), "data.userInfo.stats.followingCount"),
                likes_count=_required_int(stats.get("heartCount"), "data.userInfo.stats.heartCount"),
                post_count=_required_int(stats.get("videoCount"), "data.userInfo.stats.videoCount"),
            )
        except ContractChanged:
            raise self._contract_changed("fetch_user_profile", response) from None

    def iter_posts(self, sec_uid: str, *, cursor: int | None = None) -> Iterator[PostPage]:
        requested_sec_uid = _required_string(sec_uid, "sec_uid")
        current_cursor = 0 if cursor is None else _cursor_or_none(cursor)
        requested_cursors: set[int] = set()
        while True:
            requested_cursors.add(current_cursor)
            params: dict[str, int | str] = {"secUid": requested_sec_uid}
            params["cursor"] = current_cursor
            response = self._request("fetch_user_post", params)
            try:
                data = _required_mapping(response.payload.get("data"), "data")
                raw_posts = data.get("itemList")
                if not isinstance(raw_posts, list):
                    raise ContractChanged(_summary_from_field("data.itemList"))
                posts = tuple(_normalize_post(post) for post in raw_posts)
                has_more = _has_more(data.get("hasMore"))
                if has_more:
                    next_cursor = _required_int(data.get("cursor"), "data.cursor")
                    if next_cursor in requested_cursors:
                        raise ContractChanged(_summary_from_field("data.cursor"))
                else:
                    next_cursor = None
            except ContractChanged:
                raise self._contract_changed("fetch_user_post", response) from None
            yield PostPage(posts=posts, next_cursor=next_cursor)
            if next_cursor is None:
                return
            current_cursor = next_cursor

    def _request(self, endpoint: str, params: Mapping[str, int | str]) -> _UpstreamResponse:
        cookie = self._cookie_provider()
        if not isinstance(cookie, str) or not cookie:
            raise CookieInvalid(_summary(endpoint, None, (), "Cookie is unavailable"))
        transport_failure: dict[str, Any] | None = None
        try:
            response = self._session.get(
                f"{self.base_url}/api/tiktok/web/{endpoint}",
                params=dict(params),
                headers={"Cookie": cookie},
                timeout=self._timeout,
            )
        except requests.RequestException as error:
            transport_failure = _summary(endpoint, None, (), _redact_cookie(str(error), cookie))
        if transport_failure is not None:
            raise UpstreamUnavailable(transport_failure)

        status_code = getattr(response, "status_code", None)
        payload = _response_json(response)
        response_keys = _shape_keys(payload, cookie)
        if not isinstance(status_code, int):
            raise ContractChanged(_summary(endpoint, None, response_keys, "response has no status code"))
        if not 200 <= status_code < 300:
            raise UpstreamUnavailable(
                _summary(
                    endpoint, status_code, response_keys, _redact_cookie(_payload_message(payload), cookie)
                )
            )
        if not isinstance(payload, Mapping):
            raise ContractChanged(_summary(endpoint, status_code, response_keys, "response contract changed"))
        code = payload.get("code")
        if not isinstance(code, int) or isinstance(code, bool):
            raise ContractChanged(_summary(endpoint, status_code, response_keys, "response contract changed"))
        if code != 200:
            raise UpstreamUnavailable(
                _summary(endpoint, status_code, response_keys, _redact_cookie(_payload_message(payload), cookie))
            )
        expected_router = f"/api/tiktok/web/{endpoint}"
        if payload.get("router") != expected_router:
            raise ContractChanged(_summary(endpoint, status_code, response_keys, "response contract changed"))
        data = payload.get("data")
        if isinstance(data, Mapping) and _is_data_layer_failure(data):
            message = _redact_cookie(_data_layer_message(data), cookie)
            raise _semantic_error_type(message)(
                _summary(endpoint, status_code, response_keys, message)
            )
        return _UpstreamResponse(payload=payload, status_code=status_code, response_keys=response_keys)

    def _contract_changed(self, endpoint: str, response: _UpstreamResponse) -> ContractChanged:
        return ContractChanged(
            _summary(endpoint, response.status_code, response.response_keys, "response contract changed")
        )


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("base_url must be a string")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise ValueError("base_url must use loopback HTTP")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("base_url must not include a path, query, fragment, or credentials")
    return value.rstrip("/")


def _response_json(response: Any) -> object:
    try:
        return response.json()
    except (TypeError, ValueError, requests.RequestException):
        return None


def _required_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractChanged(_summary_from_field(name))
    return value


def _required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractChanged(_summary_from_field(name))
    return value


def _required_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractChanged(_summary_from_field(name))
    return value


def _cursor_or_none(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("cursor must be a non-negative integer or None")
    return value


def _has_more(value: object) -> bool:
    if value in (0, False):
        return False
    if value in (1, True):
        return True
    raise ContractChanged(_summary_from_field("data.has_more"))


def _normalize_post(value: object) -> PostSnapshot:
    if not isinstance(value, Mapping):
        raise ContractChanged(_summary_from_field("data.itemList[]"))
    statistics = value.get("stats")
    if not isinstance(statistics, Mapping):
        raise ContractChanged(_summary_from_field("data.itemList[].stats"))
    return PostSnapshot(
        video_id=_required_string(value.get("id"), "data.itemList[].id"),
        created_at=_required_int(value.get("createTime"), "data.itemList[].createTime"),
        description=_required_string(value.get("desc"), "data.itemList[].desc"),
        view_count=_required_int(statistics.get("playCount"), "data.itemList[].stats.playCount"),
        like_count=_required_int(statistics.get("diggCount"), "data.itemList[].stats.diggCount"),
        comment_count=_required_int(statistics.get("commentCount"), "data.itemList[].stats.commentCount"),
        share_count=_required_int(statistics.get("shareCount"), "data.itemList[].stats.shareCount"),
    )


def _semantic_error_type(message: str) -> type[TikTokApiError]:
    lowered = message.lower()
    if "cookie" in lowered or "login" in lowered:
        return CookieInvalid
    if "not found" in lowered:
        return AccountNotFound
    if "private" in lowered:
        return AccountPrivate
    return UpstreamUnavailable


def _payload_message(payload: object) -> str:
    if isinstance(payload, Mapping):
        for key in ("message", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        detail = payload.get("detail")
        if isinstance(detail, Mapping):
            value = detail.get("message")
            if isinstance(value, str) and value:
                return value
    return "upstream request failed"


def _is_data_layer_failure(data: Mapping[str, Any]) -> bool:
    status_code = data.get("statusCode")
    return isinstance(status_code, int) and not isinstance(status_code, bool) and status_code != 0


def _data_layer_message(data: Mapping[str, Any]) -> str:
    message = data.get("statusMsg")
    return message if isinstance(message, str) and message else "upstream data-layer failure"


def _shape_keys(value: object, cookie: str) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(
        _safe_summary_key(str(key), cookie)
        for key in sorted(value, key=lambda item: str(item))[:_MAX_RESPONSE_KEY_COUNT]
    )


def _safe_summary_key(value: str, cookie: str) -> str:
    if redact_secrets({value: value})[value] == "[REDACTED]":
        return "[REDACTED]"
    return redact_text(value).replace(cookie, "[REDACTED]")[:_MAX_RESPONSE_KEY_LENGTH]


def _redact_cookie(message: str, cookie: str) -> str:
    return redact_text(message).replace(cookie, "[REDACTED]")


def _summary(endpoint: str, status_code: int | None, response_keys: tuple[str, ...], message: str) -> dict[str, Any]:
    return {
        "endpoint": endpoint,
        "status_code": status_code,
        "response_keys": list(response_keys),
        "message": redact_text(message)[:_MAX_ERROR_MESSAGE_LENGTH],
    }


def _summary_from_field(name: str) -> dict[str, Any]:
    return _summary("contract", None, (), f"invalid required field: {name}")
