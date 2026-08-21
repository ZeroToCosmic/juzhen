"""Safe, short-lived live element picker for an AdsPower test profile."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import json
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from adspower import AdsPowerController
from browser_cdp import wait_for_cdp
from selector_probe.inventory import (
    MAX_INVENTORY_ITEMS,
    MAX_RAW_ITEMS,
    normalize_inventory,
    normalize_recorded_step,
)
from selector_probe.scheduler import RedisLease
from selector_probe.session import _active_browser


ACTIVE_STATES = frozenset({"starting", "ready", "selecting"})
TERMINAL_STATES = frozenset(
    {"confirmed", "cancelled", "expired", "failed"}
)
PAGE_STATES = frozenset({"feed_ready", "comment_panel_open"})
MAX_SELECTIONS = 20
MAX_RECORDED_STEPS = 50
ACTIVE_TTL_SECONDS = 300
TERMINAL_TTL_SECONDS = 600
PAGE_READY_TIMEOUT_SECONDS = 90
_GENERIC_READY_SCRIPT = (
    "Boolean(document.body && document.readyState !== 'loading' "
    "&& /^https?:/.test(window.location.href))"
)
_INVENTORY_SCAN_SCRIPT = "window.__selectorProbeBrowse?.scan?.()"


class PickerError(RuntimeError):
    def __init__(self, code: str, status_code: int = 400):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _clean_text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:maximum]


def _url_origin(value: object) -> str:
    selected = _clean_text(value, 2000)
    try:
        parsed = urlsplit(selected)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    host = parsed.hostname.casefold()
    default_port = 80 if parsed.scheme.casefold() == "http" else 443
    if port is not None and port != default_port:
        host = f"{host}:{port}"
    return f"{parsed.scheme.casefold()}://{host}"


async def _wait_for_generic_page(
    page: object,
    *,
    expected_origin: str,
    stop_event: threading.Event,
    deadline: float,
) -> None:
    loop = asyncio.get_running_loop()
    while not stop_event.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise PickerError("picker_page_readiness_timeout")
        current_origin = _url_origin(getattr(page, "url", ""))
        if current_origin and current_origin != expected_origin:
            raise PickerError("picker_cross_origin_navigation")
        try:
            if await page.evaluate(_GENERIC_READY_SCRIPT) is True:
                current_origin = _url_origin(getattr(page, "url", ""))
                if current_origin != expected_origin:
                    if current_origin:
                        raise PickerError("picker_cross_origin_navigation")
                else:
                    return
        except PickerError:
            raise
        except Exception:
            if page.is_closed():
                raise PickerError("picker_page_closed") from None
        await asyncio.sleep(min(0.25, max(0.0, remaining)))


async def _restore_browse_script(
    page: object,
    *,
    browse_arguments: Mapping[str, object],
    expected_origin: str,
    stop_event: threading.Event,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PAGE_READY_TIMEOUT_SECONDS
    while not stop_event.is_set():
        await _wait_for_generic_page(
            page,
            expected_origin=expected_origin,
            stop_event=stop_event,
            deadline=deadline,
        )
        try:
            await page.evaluate(_BROWSE_SCRIPT, browse_arguments)
            return
        except Exception:
            if page.is_closed():
                raise PickerError("picker_page_closed") from None
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PickerError("picker_page_readiness_timeout")
            await asyncio.sleep(min(0.25, remaining))


def _inventory_exceeds_public_limit(raw_items: object) -> bool:
    if not isinstance(raw_items, Sequence) or isinstance(
        raw_items, (str, bytes, bytearray)
    ):
        return False
    identities: set[str] = set()
    for raw in raw_items[:MAX_RAW_ITEMS]:
        if not isinstance(raw, Mapping):
            continue
        target_key = _clean_text(raw.get("target_key"), 200)
        if target_key:
            identity = ":".join(
                (
                    "target",
                    _clean_text(raw.get("frame_key"), 120),
                    _clean_text(raw.get("shadow_key"), 160),
                    target_key,
                )
            )
        else:
            normalized = normalize_inventory([raw], limit=1)
            if not normalized:
                continue
            identity = f'fingerprint:{normalized[0]["fingerprint"]}'
        identities.add(identity)
        if len(identities) > MAX_INVENTORY_ITEMS:
            return True
    return False


class RedisPickerRepository:
    _fallback_guard = threading.Lock()
    _fallback_locks: dict[int, threading.Lock] = {}
    _CAS_SCRIPT = r"""
local raw = redis.call('GET', KEYS[1])
local incoming = cjson.decode(ARGV[1])
local ttl = tonumber(ARGV[2])
local initial = ARGV[3] == '1'
if not raw then
  if not initial then return 0 end
  redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
  return 1
end
if initial then return -1 end
local current = cjson.decode(raw)
if current['_owner'] ~= incoming['_owner'] then return -2 end
local current_revision = tonumber(current['revision']) or 0
local incoming_revision = tonumber(incoming['revision']) or 0
local expected_revision = tonumber(ARGV[4])
local expected_token = ARGV[5]
if current_revision ~= expected_revision or tostring(current['_storage_token'] or '') ~= expected_token then return -3 end
if incoming_revision ~= expected_revision + 1 then return -4 end
local current_status = tostring(current['status'] or '')
local incoming_status = tostring(incoming['status'] or '')
local terminal = {confirmed=true, cancelled=true, expired=true, failed=true}
if terminal[current_status] and (ARGV[6] ~= '1' or current_status ~= incoming_status) then return -5 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ttl)
return 1
"""

    def __init__(self, client: object, *, key_prefix: str):
        self.client = client
        self.key_prefix = str(key_prefix).rstrip(":")

    def key(self, session_id: str) -> str:
        return f"{self.key_prefix}:picker:{session_id}"

    @classmethod
    def _fallback_lock(cls, client: object) -> threading.Lock:
        with cls._fallback_guard:
            return cls._fallback_locks.setdefault(id(client), threading.Lock())

    @staticmethod
    def _terminal(status: object) -> bool:
        return status in TERMINAL_STATES

    def compare_and_save(
        self,
        value: Mapping[str, object],
        ttl_seconds: int,
        *,
        expected_revision: int | None = None,
        expected_token: str = "",
        initial: bool = False,
        terminal_cleanup: bool = False,
    ) -> bool:
        payload = json.dumps(
            value, ensure_ascii=True, separators=(",", ":")
        )
        key = self.key(str(value["session_id"]))
        current = self.load(str(value["session_id"]))
        if current is not None and self._terminal(current.get("status")):
            if not terminal_cleanup or current.get("status") != value.get("status"):
                return False
            allowed = {
                "revision",
                "_storage_token",
                "cleanup",
                "finished_at",
                "failure_code",
            }
            keys = set(current) | set(value)
            if any(
                key_name not in allowed
                and current.get(key_name) != value.get(key_name)
                for key_name in keys
            ):
                return False
        evaluator = getattr(self.client, "eval", None)
        if callable(evaluator):
            result = evaluator(
                self._CAS_SCRIPT,
                1,
                key,
                payload,
                int(ttl_seconds),
                1 if initial else 0,
                -1 if expected_revision is None else expected_revision,
                expected_token,
                1 if terminal_cleanup else 0,
            )
            return result == 1

        # Test/in-memory clients have no Lua engine. One client-scoped lock
        # preserves the same compare-and-set semantics across service objects.
        with self._fallback_lock(self.client):
            current = self.load(str(value["session_id"]))
            if current is None:
                if not initial:
                    return False
            elif initial:
                return False
            else:
                if current.get("_owner") != value.get("_owner"):
                    return False
                current_revision = int(current.get("revision") or 0)
                incoming_revision = int(value.get("revision") or 0)
                if (
                    current_revision != expected_revision
                    or current.get("_storage_token") != expected_token
                    or incoming_revision != current_revision + 1
                ):
                    return False
                current_terminal = self._terminal(current.get("status"))
                if current_terminal:
                    if (
                        not terminal_cleanup
                        or current.get("status") != value.get("status")
                    ):
                        return False
            self.client.set(key, payload, ex=ttl_seconds)
            return True

    def load(self, session_id: str) -> dict[str, object] | None:
        raw = self.client.get(self.key(session_id))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if not isinstance(raw, str) or not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return value if isinstance(value, dict) else None


class PickerService:
    """Own picker state, shared lease, expiry, and one background runner."""

    def __init__(
        self,
        redis_client: object,
        *,
        lease_key: str,
        key_prefix: str,
        runner: Callable[..., object],
        lease_factory: Callable[..., object] = RedisLease,
        active_ttl_seconds: int = ACTIVE_TTL_SECONDS,
        terminal_ttl_seconds: int = TERMINAL_TTL_SECONDS,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.repository = RedisPickerRepository(
            redis_client, key_prefix=key_prefix
        )
        self.redis_client = redis_client
        self.lease_key = lease_key
        self.runner = runner
        self.lease_factory = lease_factory
        self.active_ttl_seconds = active_ttl_seconds
        self.terminal_ttl_seconds = terminal_ttl_seconds
        self.now_fn = now_fn
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, object]] = {}

    @staticmethod
    def _public(value: Mapping[str, object]) -> dict[str, object]:
        return {
            key: json.loads(json.dumps(item))
            for key, item in value.items()
            if not key.startswith("_")
        }

    def _save(
        self,
        session: dict[str, object],
        *,
        expected_revision: int | None = None,
        expected_token: str = "",
        initial: bool = False,
        terminal_cleanup: bool = False,
    ) -> bool:
        ttl = (
            self.terminal_ttl_seconds
            if session.get("status") in TERMINAL_STATES
            else self.active_ttl_seconds
        )
        session["_storage_token"] = uuid.uuid4().hex
        persistent = {
            key: item
            for key, item in session.items()
            if key
            not in {
                "_stop_event",
                "_thread",
                "_lease",
                "_timer",
                "_context",
                "_profile_id",
            }
        }
        return self.repository.compare_and_save(
            persistent,
            ttl,
            expected_revision=expected_revision,
            expected_token=expected_token,
            initial=initial,
            terminal_cleanup=terminal_cleanup,
        )

    def _owned(self, session_id: str, actor_user_id: int) -> dict[str, object]:
        session = self._sessions.get(session_id)
        if session is None:
            session = self.repository.load(session_id)
        if session is None:
            raise PickerError("picker_not_found", 404)
        if session.get("_actor_user_id") != actor_user_id:
            raise PickerError("picker_not_found", 404)
        return session

    def start(
        self,
        *,
        profile_id: str,
        profile_mask: str,
        page_state: str,
        actor_user_id: int,
        context: Mapping[str, object],
    ) -> dict[str, object]:
        if page_state not in PAGE_STATES:
            raise PickerError("invalid_picker_page_state")
        if not isinstance(actor_user_id, int) or actor_user_id < 1:
            raise PickerError("invalid_picker_actor")
        session_id = uuid.uuid4().hex
        owner = f"picker:{session_id}"
        lease = self.lease_factory(
            self.redis_client,
            self.lease_key,
            owner,
            ttl_seconds=120,
            heartbeat_seconds=30,
        )
        with self._lock:
            if any(
                item.get("status") in ACTIVE_STATES
                for item in self._sessions.values()
            ):
                raise PickerError("picker_busy", 409)
            if lease.acquire() is not True:
                raise PickerError("picker_busy", 409)
            now = self.now_fn().astimezone(UTC)
            stop_event = threading.Event()
            session: dict[str, object] = {
                "session_id": session_id,
                "status": "starting",
                "mode": "browse",
                "profile_mask": profile_mask,
                "page_state": page_state,
                "inventory": [],
                "inventory_revision": 0,
                "recorded_steps": [],
                "truncated": False,
                "last_scanned_at": "",
                "selections": [],
                "selection_count": 0,
                "max_selections": MAX_SELECTIONS,
                "revision": 1,
                "cleanup": "pending",
                "failure_code": "",
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=self.active_ttl_seconds)).isoformat(),
                "_owner": owner,
                "_actor_user_id": actor_user_id,
                "_profile_id": profile_id,
                "_context": dict(context),
                "_stop_event": stop_event,
                "_lease": lease,
            }
            self._sessions[session_id] = session
            if not self._save(session, initial=True):
                self._sessions.pop(session_id, None)
                lease.release()
                raise PickerError("picker_busy", 409)
            timer = threading.Timer(
                self.active_ttl_seconds,
                self._expire,
                args=(session_id,),
            )
            timer.daemon = True
            session["_timer"] = timer
            thread = threading.Thread(
                target=self._run,
                args=(session_id,),
                name=f"selector-picker-{session_id[:8]}",
                daemon=True,
            )
            session["_thread"] = thread
            timer.start()
            thread.start()
            return self._public(session)

    def _expire(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.get("status") not in ACTIVE_STATES:
                return
            storage_revision = int(session["revision"])
            storage_token = str(session.get("_storage_token") or "")
            session["status"] = "expired"
            session["failure_code"] = "picker_expired"
            session["revision"] = int(session["revision"]) + 1
            stop = session.get("_stop_event")
            if isinstance(stop, threading.Event):
                stop.set()
            if not self._save(
                session,
                expected_revision=storage_revision,
                expected_token=storage_token,
            ):
                persisted = self.repository.load(session_id)
                if persisted is not None:
                    private = {
                        key: value
                        for key, value in session.items()
                        if key.startswith("_") and key not in persisted
                    }
                    session.clear()
                    session.update(persisted)
                    session.update(private)

    def _update_ready(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.get("status") != "starting":
                return
            storage_revision = int(session["revision"])
            storage_token = str(session.get("_storage_token") or "")
            session["status"] = "ready"
            session["revision"] = int(session["revision"]) + 1
            self._save(
                session,
                expected_revision=storage_revision,
                expected_token=storage_token,
            )

    def _update_inventory(
        self, session_id: str, raw_items: object, truncated: bool
    ) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.get("status") not in ACTIVE_STATES:
                return
            previous = session.get("inventory")
            previous = previous if isinstance(previous, list) else []
            stable_ids = {
                str(item.get("fingerprint")): str(item.get("selection_id"))
                for item in previous
                if isinstance(item, Mapping)
                and item.get("fingerprint")
                and item.get("selection_id")
            }
            normalized = normalize_inventory(
                raw_items,
                selection_ids=stable_ids,
                limit=MAX_INVENTORY_ITEMS,
            )
            previous_by_locator: dict[tuple[str, str, str, str], str] = {}
            for item in previous:
                if not isinstance(item, Mapping):
                    continue
                selected_id = str(item.get("selection_id") or "")
                for locator in item.get("locators", []):
                    if isinstance(locator, Mapping):
                        previous_by_locator[
                            (
                                str(item.get("frame_key") or ""),
                                str(item.get("shadow_key") or ""),
                                str(locator.get("type") or ""),
                                str(locator.get("value") or ""),
                            )
                        ] = selected_id
            reused_ids: set[str] = set()
            for item in normalized:
                for locator in item.get("locators", []):
                    key = (
                        str(item.get("frame_key") or ""),
                        str(item.get("shadow_key") or ""),
                        str(locator.get("type") or ""),
                        str(locator.get("value") or ""),
                    )
                    selected_id = previous_by_locator.get(key, "")
                    if selected_id and selected_id not in reused_ids:
                        item["selection_id"] = selected_id
                        reused_ids.add(selected_id)
                        break
                else:
                    reused_ids.add(str(item["selection_id"]))
            previous_fingerprints = [
                str(item.get("fingerprint"))
                for item in previous
                if isinstance(item, Mapping)
            ]
            next_fingerprints = [str(item["fingerprint"]) for item in normalized]
            changed = previous_fingerprints != next_fingerprints
            next_truncated = bool(truncated) or _inventory_exceeds_public_limit(
                raw_items
            )
            truncated_changed = session.get("truncated") != next_truncated
            session["last_scanned_at"] = self.now_fn().astimezone(UTC).isoformat()
            session["inventory"] = normalized
            if not changed and not truncated_changed:
                return
            storage_revision = int(session["revision"])
            storage_token = str(session.get("_storage_token") or "")
            if changed:
                session["inventory_revision"] = int(
                    session.get("inventory_revision") or 0
                ) + 1
                session["revision"] = int(session["revision"]) + 1
            if truncated_changed:
                session["truncated"] = next_truncated
                if not changed:
                    session["revision"] = int(session["revision"]) + 1
            self._save(
                session,
                expected_revision=storage_revision,
                expected_token=storage_token,
            )

    def _record_action(self, session_id: str, raw: object) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None or session.get("status") not in ACTIVE_STATES:
                return
            steps = session.get("recorded_steps")
            if not isinstance(steps, list):
                steps = []
                session["recorded_steps"] = steps
            if len(steps) >= MAX_RECORDED_STEPS:
                return
            if not isinstance(raw, Mapping):
                return
            candidate = dict(raw)
            candidate["sequence"] = len(steps) + 1
            try:
                step = normalize_recorded_step(candidate)
            except ValueError:
                return
            storage_revision = int(session["revision"])
            storage_token = str(session.get("_storage_token") or "")
            steps.append(step)
            session["revision"] = int(session["revision"]) + 1
            self._save(
                session,
                expected_revision=storage_revision,
                expected_token=storage_token,
            )

    def _run(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions[session_id]
            lease = session["_lease"]
            stop_event = session["_stop_event"]
            context = dict(session["_context"])
            profile_id = str(session["_profile_id"])
            page_state = str(session["page_state"])
        heartbeat_stop = threading.Event()

        def heartbeat() -> None:
            next_renew = time.monotonic() + 30
            while not heartbeat_stop.wait(0.5):
                persisted = self.repository.load(session_id)
                if persisted and persisted.get("status") in TERMINAL_STATES:
                    with self._lock:
                        current = self._sessions.get(session_id)
                        if current and current.get("status") in ACTIVE_STATES:
                            for field in (
                                "status",
                                "inventory",
                                "inventory_revision",
                                "recorded_steps",
                                "truncated",
                                "last_scanned_at",
                                "selections",
                                "selection_count",
                                "revision",
                                "failure_code",
                            ):
                                if field in persisted:
                                    current[field] = persisted[field]
                    stop_event.set()
                    return
                if time.monotonic() < next_renew:
                    continue
                try:
                    renewed = lease.renew()
                except Exception:
                    renewed = False
                if renewed is True:
                    next_renew = time.monotonic() + 30
                    continue
                with self._lock:
                    current = self._sessions.get(session_id)
                    if current and current.get("status") in ACTIVE_STATES:
                        storage_revision = int(current["revision"])
                        storage_token = str(
                            current.get("_storage_token") or ""
                        )
                        current["status"] = "failed"
                        current["failure_code"] = "picker_lease_lost"
                        current["revision"] = int(current["revision"]) + 1
                        self._save(
                            current,
                            expected_revision=storage_revision,
                            expected_token=storage_token,
                        )
                stop_event.set()
                return

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"selector-picker-lease-{session_id[:8]}",
            daemon=True,
        )
        heartbeat_thread.start()
        run_error = ""
        try:
            self.runner(
                profile_id=profile_id,
                page_state=page_state,
                context=context,
                ready_sink=lambda: self._update_ready(session_id),
                inventory_sink=lambda raw, truncated: self._update_inventory(
                    session_id, raw, truncated
                ),
                action_sink=lambda raw: self._record_action(session_id, raw),
                stop_event=stop_event,
            )
        except Exception as error:
            run_error = _clean_text(getattr(error, "code", None), 64)
            if not run_error:
                run_error = {
                    "TimeoutError": "picker_timeout",
                    "CancelledError": "picker_cancelled",
                }.get(type(error).__name__, "picker_runtime_failed")
        finally:
            heartbeat_stop.set()
            timer = None
            with self._lock:
                current = self._sessions.get(session_id)
                if current is not None:
                    timer = current.get("_timer")
                    persisted = self.repository.load(session_id)
                    if persisted is not None:
                        private = {
                            key: value
                            for key, value in current.items()
                            if key.startswith("_") and key not in persisted
                        }
                        current.clear()
                        current.update(persisted)
                        current.update(private)
                    storage_revision = int(current["revision"])
                    storage_token = str(
                        current.get("_storage_token") or ""
                    )
                    terminal_cleanup = current.get("status") in TERMINAL_STATES
                    if not terminal_cleanup:
                        current["status"] = "failed"
                        current["failure_code"] = run_error or "picker_closed"
                    elif run_error:
                        current["failure_code"] = run_error
                    current["cleanup"] = (
                        "failed"
                        if run_error == "picker_cleanup_failed"
                        else "passed"
                    )
                    current["revision"] = int(current["revision"]) + 1
                    current["finished_at"] = self.now_fn().astimezone(UTC).isoformat()
                    self._save(
                        current,
                        expected_revision=storage_revision,
                        expected_token=storage_token,
                        terminal_cleanup=terminal_cleanup,
                    )
            if isinstance(timer, threading.Timer):
                timer.cancel()
            try:
                lease.release()
            except Exception:
                with self._lock:
                    current = self._sessions.get(session_id)
                    if current is not None:
                        persisted = self.repository.load(session_id)
                        if persisted is None:
                            return
                        private = {
                            key: value
                            for key, value in current.items()
                            if key.startswith("_") and key not in persisted
                        }
                        current.clear()
                        current.update(persisted)
                        current.update(private)
                        storage_revision = int(current["revision"])
                        storage_token = str(
                            current.get("_storage_token") or ""
                        )
                        current["cleanup"] = "lease_release_failed"
                        current["revision"] = int(current["revision"]) + 1
                        self._save(
                            current,
                            expected_revision=storage_revision,
                            expected_token=storage_token,
                            terminal_cleanup=(
                                current.get("status") in TERMINAL_STATES
                            ),
                        )

    def get(self, session_id: str, *, actor_user_id: int) -> dict[str, object]:
        with self._lock:
            return self._public(self._owned(session_id, actor_user_id))

    def _finish(
        self,
        session_id: str,
        *,
        actor_user_id: int,
        expected_revision: int,
        status: str,
        selections: Sequence[Mapping[str, str]] | None = None,
    ) -> dict[str, object]:
        with self._lock:
            session = self._owned(session_id, actor_user_id)
            if session.get("status") not in {"ready", "selecting"}:
                raise PickerError("picker_not_active", 409)
            if expected_revision != session.get("revision"):
                raise PickerError("stale_picker_revision", 409)
            storage_revision = int(session["revision"])
            storage_token = str(session.get("_storage_token") or "")
            if status == "confirmed":
                if (
                    not isinstance(selections, Sequence)
                    or isinstance(selections, (str, bytes, bytearray))
                    or not selections
                    or len(selections) > MAX_SELECTIONS
                    or any(not isinstance(item, Mapping) for item in selections)
                ):
                    raise PickerError("invalid_picker_selection")
                normalized_selections: list[tuple[str, str]] = []
                seen_ids: set[str] = set()
                seen_names: set[str] = set()
                for raw in selections:
                    if set(raw) != {"selection_id", "display_name"}:
                        raise PickerError("invalid_picker_selection")
                    selection_id = _clean_text(raw.get("selection_id"), 64)
                    display_name = _clean_text(raw.get("display_name"), 121)
                    if not selection_id or not 1 <= len(display_name) <= 120:
                        raise PickerError("invalid_picker_selection")
                    name_key = unicodedata.normalize(
                        "NFKC", display_name
                    ).casefold()
                    if name_key in seen_names:
                        raise PickerError("duplicate_element_name")
                    if selection_id in seen_ids:
                        raise PickerError("invalid_picker_selection")
                    seen_ids.add(selection_id)
                    seen_names.add(name_key)
                    normalized_selections.append((selection_id, display_name))
                inventory = session.get("inventory")
                inventory = inventory if isinstance(inventory, list) else []
                by_id = {
                    item.get("selection_id"): item
                    for item in inventory
                    if isinstance(item, Mapping)
                }
                if any(item[0] not in by_id for item in normalized_selections):
                    raise PickerError("invalid_picker_selection")
                named: list[dict[str, object]] = []
                for selection_id, display_name in normalized_selections:
                    selected = dict(by_id[selection_id])
                    selected["display_name"] = display_name
                    named.append(selected)
                session["selections"] = named
                session["selection_count"] = len(named)
            session["status"] = status
            session["revision"] = int(session["revision"]) + 1
            session["cleanup"] = "running"
            if not self._save(
                session,
                expected_revision=storage_revision,
                expected_token=storage_token,
            ):
                persisted = self.repository.load(session_id)
                if persisted is not None:
                    private = {
                        key: value
                        for key, value in session.items()
                        if key.startswith("_") and key not in persisted
                    }
                    session.clear()
                    session.update(persisted)
                    session.update(private)
                if session.get("status") in TERMINAL_STATES:
                    raise PickerError("picker_not_active", 409)
                raise PickerError("stale_picker_revision", 409)
            stop = session.get("_stop_event")
            if isinstance(stop, threading.Event):
                stop.set()
            result = self._public(session)
        thread = session.get("_thread")
        if isinstance(thread, threading.Thread):
            thread.join(timeout=3)
            with self._lock:
                result = self._public(self._owned(session_id, actor_user_id))
        return result

    def confirm(
        self,
        session_id: str,
        *,
        actor_user_id: int,
        expected_revision: int,
        selections: Sequence[Mapping[str, str]],
    ) -> dict[str, object]:
        return self._finish(
            session_id,
            actor_user_id=actor_user_id,
            expected_revision=expected_revision,
            status="confirmed",
            selections=selections,
        )

    def cancel(
        self,
        session_id: str,
        *,
        actor_user_id: int,
        expected_revision: int,
    ) -> dict[str, object]:
        return self._finish(
            session_id,
            actor_user_id=actor_user_id,
            expected_revision=expected_revision,
            status="cancelled",
        )


_BROWSE_SCRIPT = r"""
({bindingName, maxRawItems}) => {
  const key = "__selectorProbeBrowse";
  window[key]?.teardown?.();
  const baseSelector = [
    "a", "button", "input", "textarea", "select", "option", "summary",
    "[contenteditable='true']", "[tabindex]", "[data-e2e]", "[data-testid]",
    "[onclick]", "[role='button']", "[role='link']", "[role='textbox']",
    "[role='checkbox']", "[role='radio']", "[role='switch']", "[role='tab']",
    "[role='menuitem']", "[role='option']", "[role='combobox']"
  ].join(",");
  const attributeNames = [
    "data-e2e", "data-testid", "id", "name", "placeholder", "aria-label",
    "contenteditable", "type", "tabindex"
  ];
  const listeners = [];
  const installedRoots = new WeakSet();
  let actionSequence = 0;

  const clean = (value, maximum = 160) => String(value || "")
    .replace(/\u0000/g, "").replace(/\s+/g, " ").trim().slice(0, maximum);
  const dynamicId = (value) => /[0-9a-f]{8,}/i.test(value) || /\d{6,}/.test(value) ||
    /[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/i.test(value);
  const safeValue = (value) => value && value.length <= 160 && !/["'\\\]]/.test(value);
  const safeId = (value) => /^[A-Za-z_][A-Za-z0-9_-]{0,159}$/.test(value) && !dynamicId(value);
  const cssAttribute = (name, value) => `[${name}="${value}"]`;
  const xpathAttribute = (name, value) => `//*[@${name}='${value}']`;
  const queryCss = (root, selector, target) => {
    try {
      const matches = Array.from(root.querySelectorAll(selector));
      return {count: matches.length, target: matches.length === 1 && matches[0] === target};
    } catch (_) { return {count: 0, target: false}; }
  };
  const queryXpath = (root, value, target) => {
    try {
      if (root.nodeType === 11) return {count: 0, target: false};
      const doc = root.nodeType === Node.DOCUMENT_NODE ? root : root.ownerDocument;
      const result = doc.evaluate(value, root, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      return {count: result.snapshotLength, target: result.snapshotLength === 1 && result.snapshotItem(0) === target};
    } catch (_) { return {count: 0, target: false}; }
  };
  const nthIndex = (element) => {
    let index = 1;
    for (let node = element.previousElementSibling; node; node = node.previousElementSibling) {
      if (node.tagName === element.tagName) index += 1;
    }
    return Math.min(index, 100);
  };
  const cssSegment = (element) => `${element.tagName.toLowerCase()}:nth-of-type(${nthIndex(element)})`;
  const xpathSegment = (element) => `${element.tagName.toLowerCase()}[${nthIndex(element)}]`;
  const stableAnchor = (element) => {
    for (const name of attributeNames) {
      const value = clean(element.getAttribute(name));
      if (!safeValue(value) || (name === "id" && !safeId(value))) continue;
      return {name, value};
    }
    return null;
  };
  const addLocator = (locators, type, value, evidence) => {
    if (locators.length >= 32 || !value || locators.some((item) => item.type === type && item.value === value)) return;
    locators.push({type, value, match_count: evidence.count, target: evidence.target});
  };
  const locatorsFor = (element, root) => {
    const locators = [];
    const addAttribute = (name, value) => {
      if (!safeValue(value) || (name === "id" && !safeId(value))) return;
      const css = name === "id" ? `#${value}` : cssAttribute(name, value);
      addLocator(locators, "css", css, queryCss(root, css, element));
      if (root.nodeType !== 11) {
        const xpath = xpathAttribute(name, value);
        addLocator(locators, "xpath", xpath, queryXpath(root, xpath, element));
      }
    };
    for (const name of ["data-e2e", "data-testid"]) addAttribute(name, clean(element.getAttribute(name)));
    addAttribute("id", clean(element.id));
    for (const name of ["name", "placeholder", "aria-label", "contenteditable", "type", "tabindex"]) {
      addAttribute(name, clean(element.getAttribute(name)));
    }
    const pair = ["name", "type", "contenteditable", "tabindex"]
      .map((name) => [name, clean(element.getAttribute(name))])
      .filter((item) => safeValue(item[1]));
    if (pair.length >= 2) {
      const css = `${element.tagName.toLowerCase()}${cssAttribute(pair[0][0], pair[0][1])}${cssAttribute(pair[1][0], pair[1][1])}`;
      addLocator(locators, "css", css, queryCss(root, css, element));
    }
    let child = element;
    const cssTail = [];
    const xpathTail = [];
    for (let depth = 0; depth < 3 && child?.parentElement; depth += 1) {
      cssTail.unshift(cssSegment(child));
      xpathTail.unshift(xpathSegment(child));
      const parent = child.parentElement;
      const anchor = stableAnchor(parent);
      if (anchor) {
        const cssBase = anchor.name === "id" ? `#${anchor.value}` : cssAttribute(anchor.name, anchor.value);
        const css = `${cssBase} > ${cssTail.join(" > ")}`;
        addLocator(locators, "css", css, queryCss(root, css, element));
        if (root.nodeType !== 11) {
          const xpath = `${xpathAttribute(anchor.name, anchor.value)}/${xpathTail.join("/")}`;
          addLocator(locators, "xpath", xpath, queryXpath(root, xpath, element));
        }
        break;
      }
      if (parent === root || parent === root.documentElement) break;
      child = parent;
    }
    if (cssTail.length) {
      const css = cssTail.join(" > ");
      addLocator(locators, "css", css, queryCss(root, css, element));
    }
    return locators
      .map((item, order) => ({...item, order}))
      .sort((left, right) => {
        const leftUnique = left.match_count === 1 && left.target ? 1 : 0;
        const rightUnique = right.match_count === 1 && right.target ? 1 : 0;
        return rightUnique - leftUnique || left.order - right.order;
      })
      .slice(0, 6)
      .map(({order, ...item}) => item);
  };
  const roleFor = (element) => {
    const explicit = clean(element.getAttribute("role"), 48).toLowerCase();
    if (explicit) return explicit;
    const tag = element.tagName.toLowerCase();
    if (tag === "button" || tag === "summary") return "button";
    if (tag === "a" && element.hasAttribute("href")) return "link";
    if (tag === "textarea" || element.isContentEditable) return "textbox";
    if (tag !== "input") return "";
    const type = clean(element.getAttribute("type") || "text", 32).toLowerCase();
    if (["button", "submit", "reset"].includes(type)) return "button";
    if (["checkbox", "radio"].includes(type)) return type;
    return "textbox";
  };
  const structuralKey = (element, root) => {
    const parts = [];
    let current = element;
    while (current && current !== root && current.parentElement && parts.length < 8) {
      parts.unshift(cssSegment(current));
      current = current.parentElement;
    }
    return clean(parts.join(" > "), 200);
  };
  const isVisible = (element, rect, win) => {
    const style = win.getComputedStyle(element);
    return rect.width > 0 && rect.height > 0 && style.display !== "none" &&
      style.visibility !== "hidden" && Number(style.opacity || 1) > 0 &&
      rect.bottom > 0 && rect.right > 0 && rect.top < win.innerHeight && rect.left < win.innerWidth;
  };
  const actionable = (event) => {
    for (const value of event.composedPath?.() || []) {
      if (value?.nodeType === 1 && value.matches?.(baseSelector)) return value;
    }
    return event.target?.nodeType === 1 ? event.target.closest?.(baseSelector) : null;
  };
  const installRecorder = (root, meta) => {
    if (installedRoots.has(root)) return;
    installedRoots.add(root);
    const click = (event) => {
      const element = actionable(event);
      if (!element || !root.contains(element)) return;
      const locator = locatorsFor(element, root)
        .find((item) => item.match_count === 1 && item.target);
      if (!locator) return;
      const win = element.ownerDocument.defaultView;
      const receiver = win?.[bindingName] || window[bindingName];
      if (typeof receiver !== "function") return;
      actionSequence += 1;
      const url = clean(win?.location?.href, 2000);
      receiver({
        sequence: actionSequence,
        locator: {type: locator.type, value: locator.value},
        url_before: url,
        url_after: url,
        recorded_at: new Date().toISOString(),
        frame_key: meta.frameKey,
        shadow: meta.shadowKey !== "document",
        shadow_key: meta.shadowKey,
      });
    };
    root.addEventListener("click", click, true);
    listeners.push([root, click]);
  };
  const scan = () => {
    const items = [];
    let rawCount = 0;
    let truncated = false;
    const topWidth = Math.max(document.documentElement.clientWidth || 0, window.innerWidth || 0, 1);
    const topHeight = Math.max(document.documentElement.clientHeight || 0, window.innerHeight || 0, 1);
    const visitRoot = (root, meta) => {
      installRecorder(root, meta);
      let nodes = [];
      try { nodes = Array.from(root.querySelectorAll(baseSelector)); } catch (_) { return; }
      const targets = new Set(nodes);
      for (const element of targets) {
        const win = element.ownerDocument.defaultView;
        const rect = element.getBoundingClientRect();
        if (!isVisible(element, rect, win)) continue;
        rawCount += 1;
        if (rawCount > maxRawItems) { truncated = true; continue; }
        const centerX = rect.left + rect.width / 2;
        const centerY = rect.top + rect.height / 2;
        let hit = null;
        try { hit = element.ownerDocument.elementFromPoint(centerX, centerY); } catch (_) {}
        const hitTarget = Boolean(hit && (element === hit || element.contains(hit)));
        const disabled = element.matches(":disabled") || element.getAttribute("aria-disabled") === "true";
        const attributes = {};
        for (const name of attributeNames) {
          const value = clean(element.getAttribute(name));
          if (value) attributes[name] = value;
        }
        const locators = locatorsFor(element, root);
        const left = Math.max(0, Math.min(meta.offsetX + rect.left, topWidth));
        const top = Math.max(0, Math.min(meta.offsetY + rect.top, topHeight));
        const right = Math.max(left, Math.min(meta.offsetX + rect.right, topWidth));
        const bottom = Math.max(top, Math.min(meta.offsetY + rect.bottom, topHeight));
        items.push({
          target_key: structuralKey(element, root),
          tag: element.tagName.toLowerCase(),
          input_type: clean(element.getAttribute("type"), 32).toLowerCase(),
          text: clean(element.innerText || element.textContent, 240),
          role: roleFor(element),
          name: clean(attributes["aria-label"] || attributes.placeholder || element.innerText || element.textContent, 160),
          attributes,
          frame_key: meta.frameKey,
          shadow: meta.shadowKey !== "document",
          shadow_key: meta.shadowKey,
          region: {x: left / topWidth, y: top / topHeight, width: (right - left) / topWidth, height: (bottom - top) / topHeight},
          locators: locators.map(({type, value, match_count}) => ({type, value, match_count})),
          visible: true,
          enabled: !disabled,
          hit_target: hitTarget,
          target_match: locators.some((item) => item.match_count === 1 && item.target),
        });
      }
      let shadowHosts = [];
      try {
        shadowHosts = Array.from(root.querySelectorAll("*"))
          .filter((element) => element.shadowRoot?.mode === "open");
      } catch (_) {}
      for (const host of shadowHosts) {
        visitRoot(host.shadowRoot, {
          ...meta,
          shadowKey: `${meta.shadowKey}/${structuralKey(host, root)}`.slice(0, 160),
        });
      }
      let frames = [];
      try { frames = Array.from(root.querySelectorAll("iframe")); } catch (_) {}
      frames.forEach((frame, index) => {
        try {
          const childDocument = frame.contentDocument;
          if (!childDocument?.documentElement) return;
          const rect = frame.getBoundingClientRect();
          visitRoot(childDocument, {
            frameKey: `${meta.frameKey}/frame:${index + 1}`.slice(0, 120),
            shadowKey: "document",
            offsetX: meta.offsetX + rect.left,
            offsetY: meta.offsetY + rect.top,
          });
        } catch (_) {}
      });
    };
    visitRoot(document, {frameKey: "main", shadowKey: "document", offsetX: 0, offsetY: 0});
    return {items, truncated: truncated || rawCount > maxRawItems};
  };
  window[key] = {
    scan,
    teardown() {
      for (const [root, listener] of listeners) root.removeEventListener("click", listener, true);
      listeners.length = 0;
      delete window[key];
    },
  };
  return true;
}
"""


def run_browser_picker(
    *,
    profile_id: str,
    page_state: str,
    context: Mapping[str, object],
    ready_sink: Callable[[], None],
    inventory_sink: Callable[[object, bool], None],
    action_sink: Callable[[object], None],
    stop_event: threading.Event,
) -> None:
    """Run one picker page and synchronously retain ownership until stopped."""

    async def run() -> None:
        settings = context.get("settings")
        if not isinstance(settings, Mapping):
            raise PickerError("picker_settings_unavailable", 503)
        probe = settings.get("selector_probe")
        probe = probe if isinstance(probe, Mapping) else {}
        adspower = settings.get("adspower")
        adspower = adspower if isinstance(adspower, Mapping) else {}
        target_url = _clean_text(probe.get("target_url"), 2000)
        target_origin = _url_origin(target_url)
        if not target_url or not target_origin:
            raise PickerError("picker_target_url_unavailable", 503)
        controller = AdsPowerController(
            base_url=_clean_text(adspower.get("base_url"), 1000) or None,
            api_key=str(adspower.get("api_key") or ""),
        )
        started_by_picker = False
        page = None
        playwright = None
        try:
            try:
                active, ws_url = _active_browser(
                    controller.get_browser_active(profile_id)
                )
            except Exception:
                raise PickerError("picker_profile_status_failed", 503) from None
            if active and not ws_url:
                raise PickerError("picker_profile_cdp_unavailable", 503)
            if not active:
                # Track ownership before the call: AdsPower can open a window
                # and still fail before returning its CDP endpoint.
                started_by_picker = True
                try:
                    ws_url = controller.start_browser(profile_id)
                except Exception:
                    raise PickerError("picker_profile_open_failed", 503) from None
            try:
                cdp_ready = wait_for_cdp(ws_url, timeout=30) is True
            except Exception:
                cdp_ready = False
            if not cdp_ready:
                raise PickerError("picker_cdp_timeout", 503)
            from playwright.async_api import async_playwright

            playwright = await async_playwright().start()
            try:
                browser = await playwright.chromium.connect_over_cdp(ws_url)
            except Exception:
                raise PickerError("picker_cdp_connect_failed", 503) from None
            contexts = getattr(browser, "contexts", [])
            if not contexts:
                raise PickerError("picker_browser_context_unavailable", 503)
            page = await contexts[0].new_page()
            ready_deadline = (
                asyncio.get_running_loop().time()
                + PAGE_READY_TIMEOUT_SECONDS
            )
            try:
                await page.goto(
                    target_url,
                    wait_until="domcontentloaded",
                    timeout=PAGE_READY_TIMEOUT_SECONDS * 1000,
                )
            except Exception:
                if page.is_closed():
                    raise PickerError("picker_page_closed") from None
            await _wait_for_generic_page(
                page,
                expected_origin=target_origin,
                stop_event=stop_event,
                deadline=ready_deadline,
            )
            if stop_event.is_set():
                return
            binding_name = f"__selectorPicker_{uuid.uuid4().hex}"

            async def receive(_source: object, raw: object) -> None:
                action_sink(raw)

            await page.expose_binding(binding_name, receive)
            browse_arguments = {
                "bindingName": binding_name,
                "maxRawItems": MAX_RAW_ITEMS,
            }
            await _restore_browse_script(
                page,
                browse_arguments=browse_arguments,
                expected_origin=target_origin,
                stop_event=stop_event,
            )
            ready_sink()
            while not stop_event.is_set():
                if page.is_closed():
                    raise PickerError("picker_page_closed")
                try:
                    result = await page.evaluate(_INVENTORY_SCAN_SCRIPT)
                except Exception:
                    if page.is_closed():
                        raise PickerError("picker_page_closed") from None
                    await _restore_browse_script(
                        page,
                        browse_arguments=browse_arguments,
                        expected_origin=target_origin,
                        stop_event=stop_event,
                    )
                    continue
                if not isinstance(result, Mapping):
                    await _restore_browse_script(
                        page,
                        browse_arguments=browse_arguments,
                        expected_origin=target_origin,
                        stop_event=stop_event,
                    )
                    continue
                inventory_sink(
                    result.get("items"), result.get("truncated") is True
                )
                for _ in range(10):
                    if stop_event.is_set():
                        break
                    await asyncio.sleep(0.1)
        finally:
            cleanup_failed = False
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.evaluate(
                            "window.__selectorProbeBrowse?.teardown?.()"
                        )
                        await page.close()
                except Exception:
                    cleanup_failed = True
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    cleanup_failed = True
            if started_by_picker:
                try:
                    controller.stop_browser(profile_id)
                except Exception:
                    cleanup_failed = True
            if cleanup_failed:
                raise PickerError("picker_cleanup_failed")

    asyncio.run(run())


def build_picker_service(
    *,
    settings: Mapping[str, object],
    redis_client: object,
) -> PickerService:
    probe = settings.get("selector_probe")
    probe = probe if isinstance(probe, Mapping) else {}
    redis_settings = probe.get("redis")
    redis_settings = (
        redis_settings if isinstance(redis_settings, Mapping) else {}
    )
    namespace = _clean_text(redis_settings.get("namespace"), 128) or "selector_registry"
    environment = _clean_text(probe.get("environment"), 64) or "production"
    site = _clean_text(probe.get("site"), 64) or "tiktok"
    prefix = f"{namespace}:{environment}:{site}"
    return PickerService(
        redis_client,
        lease_key=f"{prefix}:lease",
        key_prefix=prefix,
        runner=run_browser_picker,
    )


__all__ = [
    "MAX_SELECTIONS",
    "PAGE_STATES",
    "PickerError",
    "PickerService",
    "build_picker_service",
    "run_browser_picker",
]
