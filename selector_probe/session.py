"""Own AdsPower probe profiles and probe-created browser pages explicitly."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from browser_public_identity import mask_profile_id


_SAFE_STAGE_SUMMARIES = {
    "profile_start_rejected": "AdsPower rejected Profile start",
    "active_cdp_unavailable": (
        "Active Profile did not expose a CDP endpoint"
    ),
    "cdp_unavailable": "CDP endpoint did not become reachable",
    "cdp_connect_failed": "Playwright could not connect over CDP",
    "profile_cdp_collision": "Profiles resolved to one CDP browser",
    "probe_page_duplicate": "Profile already owns a probe Page",
}


def _progress(
    sink: Callable[[dict[str, object]], None] | None,
    *,
    stage: str,
    profile_id: str,
    status: str,
    attempt: int,
    failure_code: str = "",
) -> None:
    if sink is None:
        return
    sink(
        {
            "name": stage,
            "profile_mask": mask_profile_id(profile_id),
            "status": status,
            "attempt_count": attempt,
            "failure_code": failure_code,
            "summary": _SAFE_STAGE_SUMMARIES.get(failure_code, ""),
        }
    )


class ProbeSessionError(RuntimeError):
    """A sanitized profile-session failure safe for probe logs."""

    def __init__(
        self,
        code: str,
        profile_mask: str = "",
        *,
        cleanup_results: Sequence[dict[str, object]] = (),
    ) -> None:
        self.code = code
        self.profile_mask = mask_profile_id(profile_mask)
        self._cleanup_results: list[dict[str, object]] = []
        self.extend_cleanup_results(cleanup_results)
        suffix = f" ({self.profile_mask})" if self.profile_mask else ""
        super().__init__(f"{code}{suffix}")

    @property
    def cleanup_results(self) -> list[dict[str, object]]:
        return [dict(result) for result in self._cleanup_results]

    def extend_cleanup_results(
        self,
        cleanup_results: Sequence[dict[str, object]],
    ) -> None:
        if not isinstance(cleanup_results, Sequence) or isinstance(
            cleanup_results, (str, bytes, bytearray)
        ):
            raise TypeError("cleanup_results must be a sequence")
        for result in cleanup_results:
            if not isinstance(result, dict):
                raise TypeError("cleanup_results contains an invalid result")
            self._cleanup_results.append(_sanitize_cleanup_result(result))


_CLEANUP_STAGES = frozenset({"close_page", "stop_profile"})
_CLEANUP_CODES = frozenset(
    {
        "",
        "page_close_cancelled",
        "page_close_failed",
        "profile_stop_failed",
    }
)


def _sanitize_cleanup_result(value: dict[str, object]) -> dict[str, object]:
    raw_stage = value.get("stage")
    stage = raw_stage if raw_stage in _CLEANUP_STAGES else "stop_profile"
    raw_code = value.get("code")
    code = raw_code if raw_code in _CLEANUP_CODES else "profile_stop_failed"
    return {
        "profile_mask": mask_profile_id(value.get("profile_mask")),
        "stage": stage,
        "ok": value.get("ok") is True,
        "code": code,
    }


def _cleanup_result(
    profile_mask: str,
    stage: str,
    *,
    ok: bool,
    code: str,
) -> dict[str, object]:
    return _sanitize_cleanup_result(
        {
            "profile_mask": profile_mask,
            "stage": stage,
            "ok": ok,
            "code": code,
        }
    )


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _is_cdp_url(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    parsed = urlsplit(value)
    return parsed.scheme.lower() in {"ws", "wss", "http", "https"} and bool(
        parsed.netloc
    )


def _cdp_identity(value: str) -> tuple[str, str, int | None, str]:
    parsed = urlsplit(value)
    return (
        parsed.scheme.casefold(),
        (parsed.hostname or "").casefold(),
        parsed.port,
        parsed.path.rstrip("/") or "/",
    )


@dataclass(frozen=True)
class ProfileHandle:
    profile_id: str = field(repr=False)
    profile_mask: str
    ws_url: str = field(repr=False)
    started_by_probe: bool

    def __post_init__(self) -> None:
        profile_id = _required_text(self.profile_id, "profile_id")
        profile_mask = _required_text(self.profile_mask, "profile_mask")
        if profile_mask != mask_profile_id(profile_id):
            raise ValueError("profile_mask must be the masked profile_id")
        if not _is_cdp_url(self.ws_url):
            raise ValueError("ws_url must be a valid CDP URL")
        if type(self.started_by_probe) is not bool:
            raise TypeError("started_by_probe must be a bool")


@dataclass(frozen=True)
class ProbePageHandle:
    profile: ProfileHandle
    page: object = field(repr=False)
    created_by_probe: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.profile, ProfileHandle):
            raise TypeError("profile must be a ProfileHandle")
        if self.page is None:
            raise ValueError("page must not be None")
        if type(self.created_by_probe) is not bool:
            raise TypeError("created_by_probe must be a bool")


def _strict_profile_sequence(
    values: object,
    name: str,
    *,
    require_two: bool,
) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        raise TypeError(f"{name} must be a sequence of profile IDs")

    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        profile_id = _required_text(value, f"{name} item")
        if profile_id not in seen:
            normalized.append(profile_id)
            seen.add(profile_id)

    if require_two and len(normalized) < 2:
        raise ValueError(f"{name} requires at least two unique profile IDs")
    return tuple(normalized)


def _strict_handle_sequence(
    values: object,
    handle_type: type,
    name: str,
) -> tuple[Any, ...]:
    if not isinstance(values, Sequence) or isinstance(
        values, (str, bytes, bytearray)
    ):
        raise TypeError(f"{name} must be a sequence")
    result = tuple(values)
    if any(not isinstance(value, handle_type) for value in result):
        raise TypeError(f"{name} contains an invalid handle")
    return result


def _active_browser(value: object) -> tuple[bool, str]:
    if not isinstance(value, dict):
        raise TypeError("active response must be a JSON object")
    data = value.get("data", value)
    if not isinstance(data, dict):
        raise TypeError("active response data must be a JSON object")
    status = data.get("status")
    if not isinstance(status, str):
        raise TypeError("active response status must be a string")
    normalized_status = status.strip().casefold()
    if normalized_status == "inactive":
        return False, ""
    if normalized_status != "active":
        raise ValueError("active response status is unsupported")
    ws = data.get("ws")
    candidate = ws.get("puppeteer") if isinstance(ws, dict) else ws
    return True, candidate if _is_cdp_url(candidate) else ""


class ProbeSessionManager:
    """Manage only allowlisted profiles and resources created by the probe."""

    def __init__(
        self,
        adspower_client: object,
        *,
        allowed_profile_ids: Sequence[str],
        wait_for_cdp: Callable[[str], object],
        stop_requested: Callable[[], bool] | None = None,
        progress_sink: Callable[[dict[str, object]], None] | None = None,
        sleep_fn: Callable[[float], object] = time.sleep,
        readiness_attempts: int = 3,
    ) -> None:
        for method_name in (
            "get_browser_active",
            "start_browser",
            "stop_browser",
        ):
            if not callable(getattr(adspower_client, method_name, None)):
                raise TypeError(
                    f"adspower_client.{method_name} must be callable"
                )
        if not callable(wait_for_cdp):
            raise TypeError("wait_for_cdp must be callable")
        if stop_requested is not None and not callable(stop_requested):
            raise TypeError("stop_requested must be callable")
        if progress_sink is not None and not callable(progress_sink):
            raise TypeError("progress_sink must be callable")
        if not callable(sleep_fn):
            raise TypeError("sleep_fn must be callable")
        if (
            isinstance(readiness_attempts, bool)
            or not isinstance(readiness_attempts, int)
            or not 1 <= readiness_attempts <= 5
        ):
            raise ValueError("readiness_attempts must be between 1 and 5")
        self._client = adspower_client
        self._allowed_profile_ids = frozenset(
            _strict_profile_sequence(
                allowed_profile_ids,
                "allowed_profile_ids",
                require_two=True,
            )
        )
        self._wait_for_cdp = wait_for_cdp
        self._stop_requested = stop_requested or (lambda: False)
        self._progress_sink = progress_sink
        self._sleep_fn = sleep_fn
        self._readiness_attempts = readiness_attempts
        self._page_profile_ids: set[str] = set()

    def _require_running(self) -> None:
        if self._stop_requested():
            raise asyncio.CancelledError()

    def _profile_endpoint(
        self,
        profile_id: str,
    ) -> tuple[str, bool]:
        profile_mask = mask_profile_id(profile_id)
        for attempt in range(1, self._readiness_attempts + 1):
            self._require_running()
            active = self._client.get_browser_active(profile_id)
            self._require_running()
            is_active, ws_url = _active_browser(active)
            if ws_url:
                _progress(
                    self._progress_sink,
                    stage="cdp_endpoint",
                    profile_id=profile_id,
                    status="passed",
                    attempt=attempt,
                )
                return ws_url, False
            if is_active:
                _progress(
                    self._progress_sink,
                    stage="cdp_endpoint",
                    profile_id=profile_id,
                    status="running",
                    attempt=attempt,
                )
                if attempt < self._readiness_attempts:
                    self._sleep_fn(float(min(attempt, 2)))
                    continue
                _progress(
                    self._progress_sink,
                    stage="cdp_endpoint",
                    profile_id=profile_id,
                    status="failed",
                    attempt=attempt,
                    failure_code="active_cdp_unavailable",
                )
                raise ProbeSessionError(
                    "preexisting_profile_unhealthy",
                    profile_mask,
                )
            break

        started_by_probe = True
        last_code = "profile_start_rejected"
        for attempt in range(1, self._readiness_attempts + 1):
            self._require_running()
            try:
                candidate = self._client.start_browser(profile_id)
            except Exception:
                candidate = ""
            if _is_cdp_url(candidate):
                _progress(
                    self._progress_sink,
                    stage="profile_start",
                    profile_id=profile_id,
                    status="passed",
                    attempt=attempt,
                )
                return str(candidate), started_by_probe
            _progress(
                self._progress_sink,
                stage="profile_start",
                profile_id=profile_id,
                status=(
                    "running"
                    if attempt < self._readiness_attempts
                    else "failed"
                ),
                attempt=attempt,
                failure_code=(
                    "" if attempt < self._readiness_attempts else last_code
                ),
            )
            if attempt < self._readiness_attempts:
                self._sleep_fn(float(min(attempt, 2)))
        cleanup = self._stop_profile(profile_id, profile_mask)
        raise ProbeSessionError(
            "profile_open_failed",
            profile_mask,
            cleanup_results=[cleanup],
        )

    def open_profiles(
        self,
        profile_ids: Sequence[str],
    ) -> list[ProfileHandle]:
        requested = _strict_profile_sequence(
            profile_ids,
            "profile_ids",
            require_two=True,
        )
        for profile_id in requested:
            if profile_id not in self._allowed_profile_ids:
                raise ValueError(
                    f"profile {mask_profile_id(profile_id)} is not allowlisted"
                )

        handles: list[ProfileHandle] = []
        endpoint_identities: set[tuple[str, str, int | None, str]] = set()
        try:
            for profile_id in requested:
                self._require_running()
                profile_mask = mask_profile_id(profile_id)
                try:
                    ws_url, started_by_probe = self._profile_endpoint(
                        profile_id
                    )

                    handle = ProfileHandle(
                        profile_id=profile_id,
                        profile_mask=profile_mask,
                        ws_url=ws_url,
                        started_by_probe=started_by_probe,
                    )
                    if started_by_probe:
                        handles.append(handle)
                    endpoint_identity = _cdp_identity(ws_url)
                    if endpoint_identity in endpoint_identities:
                        _progress(
                            self._progress_sink,
                            stage="profile_binding",
                            profile_id=profile_id,
                            status="failed",
                            attempt=1,
                            failure_code="profile_cdp_collision",
                        )
                        raise ProbeSessionError(
                            "profile_cdp_collision",
                            profile_mask,
                        )
                    endpoint_identities.add(endpoint_identity)
                    self._require_running()
                    ready = False
                    for attempt in range(
                        1, self._readiness_attempts + 1
                    ):
                        self._require_running()
                        if self._wait_for_cdp(ws_url) is True:
                            ready = True
                            _progress(
                                self._progress_sink,
                                stage="cdp_ready",
                                profile_id=profile_id,
                                status="passed",
                                attempt=attempt,
                            )
                            break
                        _progress(
                            self._progress_sink,
                            stage="cdp_ready",
                            profile_id=profile_id,
                            status=(
                                "running"
                                if attempt < self._readiness_attempts
                                else "failed"
                            ),
                            attempt=attempt,
                            failure_code=(
                                ""
                                if attempt < self._readiness_attempts
                                else "cdp_unavailable"
                            ),
                        )
                        if attempt < self._readiness_attempts:
                            self._sleep_fn(float(min(attempt, 2)))
                    if not ready:
                        raise ProbeSessionError(
                            "cdp_unavailable",
                            profile_mask,
                        )
                    self._require_running()
                    if not started_by_probe:
                        handles.append(handle)
                except ProbeSessionError:
                    raise
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise ProbeSessionError(
                        "profile_open_failed",
                        profile_mask,
                    ) from None
        except BaseException as error:
            cleanup_results = self.stop_owned_profiles(handles)
            if isinstance(error, ProbeSessionError):
                error.extend_cleanup_results(cleanup_results)
            raise
        return handles

    async def open_probe_page(
        self,
        playwright: object,
        handle: ProfileHandle,
    ) -> ProbePageHandle:
        if not isinstance(handle, ProfileHandle):
            raise TypeError("handle must be a ProfileHandle")
        if handle.profile_id in self._page_profile_ids:
            raise ProbeSessionError(
                "probe_page_duplicate",
                handle.profile_mask,
            )
        try:
            chromium = getattr(playwright, "chromium")
            connect_over_cdp = getattr(chromium, "connect_over_cdp")
            if not callable(connect_over_cdp):
                raise TypeError
            browser = await connect_over_cdp(handle.ws_url)
        except Exception:
            raise ProbeSessionError(
                "cdp_connect_failed",
                handle.profile_mask,
            ) from None

        contexts = getattr(browser, "contexts", None)
        if (
            not isinstance(contexts, Sequence)
            or isinstance(contexts, (str, bytes, bytearray))
            or not contexts
        ):
            raise ProbeSessionError(
                "browser_context_unavailable",
                handle.profile_mask,
            )

        context = contexts[0]
        new_page = getattr(context, "new_page", None)
        if not callable(new_page):
            raise ProbeSessionError(
                "browser_context_unavailable",
                handle.profile_mask,
            )
        try:
            page = await new_page()
            if page is None:
                raise TypeError
        except Exception:
            raise ProbeSessionError(
                "probe_page_open_failed",
                handle.profile_mask,
            ) from None
        self._page_profile_ids.add(handle.profile_id)
        _progress(
            self._progress_sink,
            stage="profile_page_binding",
            profile_id=handle.profile_id,
            status="passed",
            attempt=1,
        )
        return ProbePageHandle(
            profile=handle,
            page=page,
            created_by_probe=True,
        )

    async def close_owned_pages(
        self,
        page_handles: Sequence[ProbePageHandle],
    ) -> list[dict[str, object]]:
        handles = _strict_handle_sequence(
            page_handles,
            ProbePageHandle,
            "page_handles",
        )
        results: list[dict[str, object]] = []
        cancellation: asyncio.CancelledError | None = None
        for handle in handles:
            if not handle.created_by_probe:
                continue
            result = _cleanup_result(
                handle.profile.profile_mask,
                "close_page",
                ok=True,
                code="",
            )
            try:
                close = getattr(handle.page, "close", None)
                if not callable(close):
                    raise TypeError
                await close()
            except asyncio.CancelledError as error:
                result["ok"] = False
                result["code"] = "page_close_cancelled"
                if cancellation is None:
                    cancellation = error
            except Exception:
                result["ok"] = False
                result["code"] = "page_close_failed"
            results.append(result)
        if cancellation is not None:
            raise cancellation
        return results

    def stop_owned_profiles(
        self,
        profile_handles: Sequence[ProfileHandle],
    ) -> list[dict[str, object]]:
        handles = _strict_handle_sequence(
            profile_handles,
            ProfileHandle,
            "profile_handles",
        )
        results: list[dict[str, object]] = []
        for handle in handles:
            if not handle.started_by_probe:
                continue
            results.append(
                self._stop_profile(handle.profile_id, handle.profile_mask)
            )
        return results

    def _stop_profile(
        self,
        profile_id: str,
        profile_mask: str,
    ) -> dict[str, object]:
        result = _cleanup_result(
            profile_mask,
            "stop_profile",
            ok=True,
            code="",
        )
        try:
            response = self._client.stop_browser(profile_id)
            if not isinstance(response, dict):
                raise TypeError
        except Exception:
            result["ok"] = False
            result["code"] = "profile_stop_failed"
        return result


__all__ = [
    "ProbePageHandle",
    "ProbeSessionError",
    "ProbeSessionManager",
    "ProfileHandle",
]
