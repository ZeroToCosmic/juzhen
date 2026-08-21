# Selector Probe Observe-Only Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily, dedicated-profile, read-only TikTok selector probe that records AX+DOM evidence without publishing selectors or pausing strategies.

**Architecture:** A launcher-managed Python worker reads normalized probe settings, claims a Redis lease, starts only allowlisted AdsPower test profiles, prepares allowlisted page states, extracts a compact AX+DOM semantic graph, validates current locators, persists evidence in SQLite, and closes only probe-owned windows. This phase is observe-only: no LLM, Redis selector publication, or strategy gates.

**Tech Stack:** Python 3.11+, Playwright async API, Chrome DevTools Protocol, Redis 5-6 client, SQLite, Flask, pytest, Windows launcher.

## Global Constraints

- Default schedule: `03:00 Asia/Shanghai`.
- Missed schedules execute once after recovery; never replay every missed day.
- Require at least two dedicated AdsPower test profiles.
- Allowed state actions: navigation, reload, wait, bounded scroll, open comment panel, close comment panel.
- Forbidden actions: text input, submit, like, follow, publish, account mutation, arbitrary LLM actions.
- Lease TTL: 120 seconds; heartbeat: 30 seconds.
- Probe never controls production or unlisted profiles.
- Probe closes only windows it started.
- Full profile IDs, cookies, credentials, CDP endpoints, full DOM, and comment content never enter public logs.
- Existing Playwright Locator resolver remains unchanged in semantics.
- Repository currently has no Git metadata. Do not initialize Git or add commit steps without user approval.

---

## File structure

Create:

- `selector_probe/__init__.py` — public package exports.
- `selector_probe/config.py` — strict settings normalization.
- `selector_probe/store.py` — phase-1 SQLite schema and evidence persistence.
- `selector_probe/scheduler.py` — daily due-slot calculation and Redis lease.
- `selector_probe/session.py` — AdsPower test-profile ownership lifecycle.
- `selector_probe/state_runner.py` — read-only page-state transitions.
- `selector_probe/snapshot.py` — CDP AX+DOM extraction and sanitization.
- `selector_probe/probe.py` — observe-only orchestration.
- `selector_probe/worker.py` — long-lived worker CLI.
- `tests/test_selector_probe_config.py`
- `tests/test_selector_probe_store.py`
- `tests/test_selector_probe_scheduler.py`
- `tests/test_selector_probe_session.py`
- `tests/test_selector_probe_state_runner.py`
- `tests/test_selector_probe_snapshot.py`
- `tests/test_selector_probe_observe.py`

Modify:

- `gateway/settings_store.py` — add selector-probe defaults and secret masking.
- `launcher.py` — supervise the selector-probe worker.
- `tests/test_settings_store.py`
- `tests/test_launcher_restart.py`
- `requirements.txt` is unchanged; its existing `redis>=5,<7` declaration is sufficient.

## Task 1: Strict probe configuration

**Files:**

- Create: `selector_probe/__init__.py`
- Create: `selector_probe/config.py`
- Modify: `gateway/settings_store.py:39-131`
- Test: `tests/test_selector_probe_config.py`
- Test: `tests/test_settings_store.py`

**Interfaces:**

- Produces: `ProbeConfig`.
- Produces: `normalize_probe_config(value: object) -> ProbeConfig`.
- Produces: `ProbeConfig.public_dict() -> dict`.
- Consumes later: every probe worker and API task.

- [ ] **Step 1: Write failing normalization tests**

```python
from datetime import time

import pytest

from selector_probe.config import ProbeConfig, normalize_probe_config


def valid_config():
    return {
        "enabled": True,
        "site": "tiktok",
        "environment": "production",
        "timezone": "Asia/Shanghai",
        "daily_time": "03:00",
        "target_url": "https://www.tiktok.com/",
        "test_profile_ids": ["profile-a", "profile-b"],
        "model_id": "grok-main",
        "observe_only": True,
        "webhook": {
            "enabled": False,
            "type": "generic",
            "url": "",
            "signing_secret": "",
        },
    }


def test_normalize_probe_config_requires_two_unique_profiles():
    value = valid_config()
    value["test_profile_ids"] = ["profile-a", "profile-a"]
    with pytest.raises(ValueError, match="at least two unique"):
        normalize_probe_config(value)


def test_normalize_probe_config_locks_schedule_and_origin():
    result = normalize_probe_config(valid_config())
    assert isinstance(result, ProbeConfig)
    assert result.daily_time == time(3, 0)
    assert result.timezone == "Asia/Shanghai"
    assert result.target_origin == "https://www.tiktok.com"
    assert result.test_profile_ids == ("profile-a", "profile-b")


def test_public_config_masks_profiles_and_webhook_secret():
    result = normalize_probe_config(valid_config()).public_dict()
    assert result["test_profile_ids"] == ["***le-a", "***le-b"]
    assert "signing_secret" not in result["webhook"]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_config.py -q -p no:cacheprovider
```

Expected: collection fails with `ModuleNotFoundError: No module named 'selector_probe'`.

- [ ] **Step 3: Implement configuration normalization**

Create `selector_probe/config.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from browser_public_identity import mask_profile_id


@dataclass(frozen=True)
class WebhookConfig:
    enabled: bool
    type: str
    url: str
    signing_secret: str


@dataclass(frozen=True)
class ProbeConfig:
    enabled: bool
    site: str
    environment: str
    timezone: str
    daily_time: time
    target_url: str
    target_origin: str
    test_profile_ids: tuple[str, ...]
    model_id: str
    observe_only: bool
    webhook: WebhookConfig

    def public_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "site": self.site,
            "environment": self.environment,
            "timezone": self.timezone,
            "daily_time": self.daily_time.strftime("%H:%M"),
            "target_url": self.target_url,
            "test_profile_ids": [mask_profile_id(item) for item in self.test_profile_ids],
            "model_id": self.model_id,
            "observe_only": self.observe_only,
            "webhook": {
                "enabled": self.webhook.enabled,
                "type": self.webhook.type,
                "url_configured": bool(self.webhook.url),
                "signing_secret_configured": bool(self.webhook.signing_secret),
            },
        }


def _required_text(value: object, name: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def normalize_probe_config(value: object) -> ProbeConfig:
    if not isinstance(value, dict):
        raise ValueError("selector_probe must be a JSON object")
    timezone = _required_text(value.get("timezone"), "selector_probe.timezone")
    ZoneInfo(timezone)
    raw_time = _required_text(value.get("daily_time"), "selector_probe.daily_time")
    try:
        hour_text, minute_text = raw_time.split(":", 1)
        daily_time = time(int(hour_text), int(minute_text))
    except (TypeError, ValueError) as error:
        raise ValueError("selector_probe.daily_time must use HH:MM") from error
    target_url = _required_text(value.get("target_url"), "selector_probe.target_url")
    parsed = urlsplit(target_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("selector_probe.target_url must be an HTTPS URL")
    profiles = tuple(
        dict.fromkeys(
            str(item or "").strip()
            for item in value.get("test_profile_ids", [])
            if str(item or "").strip()
        )
    )
    if len(profiles) < 2:
        raise ValueError("selector_probe requires at least two unique test profiles")
    webhook_value = value.get("webhook") or {}
    if not isinstance(webhook_value, dict):
        raise ValueError("selector_probe.webhook must be a JSON object")
    webhook = WebhookConfig(
        enabled=webhook_value.get("enabled") is True,
        type=str(webhook_value.get("type") or "generic").strip(),
        url=str(webhook_value.get("url") or "").strip(),
        signing_secret=str(webhook_value.get("signing_secret") or "").strip(),
    )
    if webhook.enabled and not webhook.url:
        raise ValueError("enabled selector_probe webhook needs a URL")
    return ProbeConfig(
        enabled=value.get("enabled") is True,
        site=_required_text(value.get("site") or "tiktok", "selector_probe.site"),
        environment=_required_text(
            value.get("environment") or "production",
            "selector_probe.environment",
        ),
        timezone=timezone,
        daily_time=daily_time,
        target_url=target_url,
        target_origin=f"{parsed.scheme}://{parsed.netloc}",
        test_profile_ids=profiles,
        model_id=str(value.get("model_id") or "").strip(),
        observe_only=value.get("observe_only") is not False,
        webhook=webhook,
    )
```

Create `selector_probe/__init__.py`:

```python
from .config import ProbeConfig, WebhookConfig, normalize_probe_config

__all__ = ["ProbeConfig", "WebhookConfig", "normalize_probe_config"]
```

Add this exact default under `DEFAULT_SETTINGS` in `gateway/settings_store.py`:

```python
"selector_probe": {
    "enabled": False,
    "site": "tiktok",
    "environment": "production",
    "timezone": "Asia/Shanghai",
    "daily_time": "03:00",
    "target_url": "https://www.tiktok.com/",
    "test_profile_ids": [],
    "model_id": "",
    "observe_only": True,
    "webhook": {
        "enabled": False,
        "type": "generic",
        "url": "",
        "signing_secret": "",
    },
},
```

Add `"signing_secret"` to `_PRESERVE_BLANK_KEYS`.

- [ ] **Step 4: Add settings round-trip tests**

```python
def test_selector_probe_defaults_and_secret_round_trip(tmp_path):
    path = tmp_path / "config.json"
    settings = load_settings(path)
    assert settings["selector_probe"]["daily_time"] == "03:00"
    assert settings["selector_probe"]["timezone"] == "Asia/Shanghai"
    settings["selector_probe"]["webhook"]["signing_secret"] = "secret"
    save_settings(settings, path)
    assert load_settings(path)["selector_probe"]["webhook"]["signing_secret"] == "secret"
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_config.py tests/test_settings_store.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

## Task 2: Durable observe-only store

**Files:**

- Create: `selector_probe/store.py`
- Test: `tests/test_selector_probe_store.py`

**Interfaces:**

- Produces: `SelectorProbeStore(path: Path)`.
- Produces: `start_run`, `finish_run`, `last_completed_slot`, `save_contracts`, and `record_validation`.
- Consumes: normalized JSON-safe evidence only.

- [ ] **Step 1: Write failing store tests**

```python
from selector_probe.store import SelectorProbeStore


def test_store_initializes_phase_one_schema_and_finishes_run(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        run_id = store.start_run(
            scheduled_for="2026-07-28T03:00:00+08:00",
            active_version_before="settings-v3",
        )
        store.record_validation(
            run_id=run_id,
            profile_mask="***le-a",
            round_number=1,
            page_state="feed_ready",
            result="passed",
            failure_code="",
            evidence={"aliases": {"评论入口": {"status": "ok"}}},
        )
        store.finish_run(run_id, status="completed", details={"observe_only": True})
        row = store.connection.execute(
            "SELECT status, details_json FROM probe_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert row["status"] == "completed"
        assert '"observe_only":true' in row["details_json"]


def test_contract_aliases_are_replaced_atomically(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.save_contracts({
            "评论入口": {"scope": "active_video", "required_state": "feed_ready"},
            "评论输入框": {"scope": "visible_comment_panel", "required_state": "comment_panel_open"},
        })
        store.save_contracts({
            "评论入口": {"scope": "active_video", "required_state": "feed_ready"},
        })
        aliases = [
            row["alias"]
            for row in store.connection.execute(
                "SELECT alias FROM element_probe_contracts ORDER BY alias"
            )
        ]
        assert aliases == ["评论入口"]
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_store.py -q -p no:cacheprovider
```

Expected: import fails because `selector_probe.store` does not exist.

- [ ] **Step 3: Implement schema and atomic methods**

Create `selector_probe/store.py` using one SQLite connection with
`row_factory=sqlite3.Row`, `PRAGMA foreign_keys=ON`, and:

```python
SCHEMA = """
CREATE TABLE IF NOT EXISTS probe_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scheduled_for TEXT NOT NULL UNIQUE,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    active_version_before TEXT NOT NULL DEFAULT '',
    published_version_after TEXT NOT NULL DEFAULT '',
    failed_aliases_json TEXT NOT NULL DEFAULT '[]',
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS element_probe_contracts (
    alias TEXT PRIMARY KEY,
    site TEXT NOT NULL DEFAULT 'tiktok',
    environment TEXT NOT NULL DEFAULT 'production',
    contract_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS selector_validation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_run_id INTEGER NOT NULL REFERENCES probe_runs(id) ON DELETE CASCADE,
    profile_mask TEXT NOT NULL,
    round_number INTEGER NOT NULL CHECK (round_number IN (1, 2)),
    page_state TEXT NOT NULL,
    result TEXT NOT NULL,
    failure_code TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL,
    screenshot_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validation_probe_run
ON selector_validation_runs(probe_run_id, profile_mask, round_number);
"""
```

Serialize JSON with:

```python
def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
```

Use `with self.connection:` transactions. `save_contracts` must validate all
aliases before deleting old rows, then replace the complete set inside one
transaction.

- [ ] **Step 4: Run store tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_store.py -q -p no:cacheprovider
```

Expected: all tests pass.

## Task 3: Daily scheduling and Redis lease

**Files:**

- Create: `selector_probe/scheduler.py`
- Test: `tests/test_selector_probe_scheduler.py`

**Interfaces:**

- Produces: `due_daily_slot(now_utc, last_completed_slot, timezone, daily_time) -> datetime | None`.
- Produces: `RedisLease(client, key, owner_id, ttl_seconds=120, heartbeat_seconds=30)`.
- `RedisLease.acquire() -> bool`, `renew() -> bool`, `release() -> bool`.

- [ ] **Step 1: Write failing scheduling and lease tests**

```python
from datetime import UTC, datetime, time

from selector_probe.scheduler import RedisLease, due_daily_slot


class FakeRedis:
    def __init__(self):
        self.values = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    def eval(self, script, numkeys, key, owner, ttl=None):
        if self.values.get(key) != owner:
            return 0
        if "del" in script:
            del self.values[key]
        return 1


def test_due_daily_slot_runs_once_after_missed_time():
    now = datetime(2026, 7, 28, 2, 0, tzinfo=UTC)  # 10:00 Shanghai
    slot = due_daily_slot(now, None, "Asia/Shanghai", time(3, 0))
    assert slot == datetime(2026, 7, 27, 19, 0, tzinfo=UTC)
    assert due_daily_slot(now, slot, "Asia/Shanghai", time(3, 0)) is None


def test_redis_lease_never_releases_another_owner():
    client = FakeRedis()
    first = RedisLease(client, "probe:lease", "owner-a")
    second = RedisLease(client, "probe:lease", "owner-b")
    assert first.acquire() is True
    assert second.acquire() is False
    assert second.release() is False
    assert first.release() is True
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_scheduler.py -q -p no:cacheprovider
```

Expected: import failure.

- [ ] **Step 3: Implement due-slot calculation and compare-owner Lua**

Use:

```python
RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""

RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""
```

`due_daily_slot` must:

1. convert `now_utc` to the configured zone;
2. create today's local scheduled datetime;
3. use yesterday only when local now is before today's scheduled time;
4. return no slot when `last_completed_slot >= candidate`;
5. return only the newest candidate.

`RedisLease.acquire` must call:

```python
client.set(key, owner_id, nx=True, ex=ttl_seconds)
```

`renew` and `release` must use the Lua scripts above. Never issue an unconditional
`DEL`.

- [ ] **Step 4: Run scheduler tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_scheduler.py -q -p no:cacheprovider
```

Expected: all tests pass.

## Task 4: AdsPower test-profile ownership

**Files:**

- Create: `selector_probe/session.py`
- Test: `tests/test_selector_probe_session.py`

**Interfaces:**

- Produces: `ProfileHandle(profile_id, profile_mask, ws_url, started_by_probe)`.
- Produces: `ProbePageHandle(profile, page, created_by_probe)`.
- Produces: `ProbeSessionManager.open_profiles(profile_ids) -> list[ProfileHandle]`.
- Produces:
  `await ProbeSessionManager.open_probe_page(playwright, handle) -> ProbePageHandle`.
- Produces:
  `await ProbeSessionManager.close_owned_pages(page_handles) -> list[dict]`.
- Produces:
  `ProbeSessionManager.stop_owned_profiles(profile_handles) -> list[dict]`.
- Consumes: `AdsPowerClient.get_browser_active`, `start_browser`, `stop_browser`.

- [ ] **Step 1: Write failing ownership tests**

```python
import pytest

from selector_probe.session import ProbeSessionManager


class FakeAdsPower:
    def __init__(self):
        self.active = {"existing": {"status": "Active", "ws": {"puppeteer": "ws://existing"}}}
        self.started = []
        self.stopped = []

    def get_browser_active(self, profile_id):
        return self.active.get(profile_id, {"status": "Inactive"})

    def start_browser(self, profile_id):
        self.started.append(profile_id)
        return f"ws://{profile_id}"

    def stop_browser(self, profile_id):
        self.stopped.append(profile_id)
        return {"code": 0}


def test_stop_owned_profiles_never_stops_preexisting_browser():
    client = FakeAdsPower()
    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("existing", "new-profile"),
        wait_for_cdp=lambda _url: True,
    )
    handles = manager.open_profiles(("existing", "new-profile"))
    assert [item.started_by_probe for item in handles] == [False, True]
    manager.stop_owned_profiles(handles)
    assert client.stopped == ["new-profile"]


def test_unlisted_profile_is_rejected_before_adspower_call():
    client = FakeAdsPower()
    manager = ProbeSessionManager(
        client,
        allowed_profile_ids=("profile-a", "profile-b"),
        wait_for_cdp=lambda _url: True,
    )
    with pytest.raises(ValueError, match="not allowlisted"):
        manager.open_profiles(("production-profile",))
    assert client.started == []
```

Add this page-ownership test:

```python
import asyncio

from selector_probe.session import ProfileHandle


class FakePage:
    def __init__(self, name):
        self.name = name
        self.closed = False

    async def close(self):
        self.closed = True


class FakeContext:
    def __init__(self, existing_pages):
        self.pages = list(existing_pages)

    async def new_page(self):
        page = FakePage("probe")
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]


class FakeChromium:
    def __init__(self, context):
        self.context = context

    async def connect_over_cdp(self, _ws_url):
        return FakeBrowser(self.context)


class FakePlaywright:
    def __init__(self, context):
        self.chromium = FakeChromium(context)


def test_probe_uses_new_tab_and_closes_only_that_tab():
    async def scenario():
        existing_page = FakePage("existing")
        context = FakeContext(existing_pages=[existing_page])
        manager = ProbeSessionManager(
            FakeAdsPower(),
            allowed_profile_ids=("profile-a", "profile-b"),
            wait_for_cdp=lambda _url: True,
        )
        profile = ProfileHandle("profile-a", "***le-a", "ws://profile-a", False)
        owned = await manager.open_probe_page(FakePlaywright(context), profile)
        await manager.close_owned_pages((owned,))
        assert owned.page.closed is True
        assert existing_page.closed is False

    asyncio.run(scenario())
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_session.py -q -p no:cacheprovider
```

Expected: import failure.

- [ ] **Step 3: Implement explicit ownership**

`open_profiles` must:

1. compare requested IDs against `allowed_profile_ids` before network calls;
2. require at least two unique IDs;
3. check `get_browser_active`;
4. reuse a valid existing CDP URL with `started_by_probe=False`;
5. otherwise call `start_browser` and set `started_by_probe=True`;
6. call `wait_for_cdp`;
7. if opening a later profile fails, stop only earlier handles marked
   `started_by_probe=True`;
8. return masked public IDs through `mask_profile_id`.

After CDP connection, `open_probe_page` always calls `context.new_page()` and
records that exact page object. It never reuses an entry from `context.pages`.
Cleanup first closes every recorded probe page, then stops only profiles with
`started_by_probe=True`. Both cleanup stages run from `finally` and continue
after an individual close failure.

The dataclass must be:

```python
@dataclass(frozen=True)
class ProfileHandle:
    profile_id: str
    profile_mask: str
    ws_url: str
    started_by_probe: bool


@dataclass(frozen=True)
class ProbePageHandle:
    profile: ProfileHandle
    page: object
    created_by_probe: bool = True
```

- [ ] **Step 4: Run session tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_session.py -q -p no:cacheprovider
```

Expected: all tests pass.

## Task 5: Read-only page-state runner

**Files:**

- Create: `selector_probe/state_runner.py`
- Test: `tests/test_selector_probe_state_runner.py`

**Interfaces:**

- Produces: `ProbeSafetyError(code, action)`.
- Produces: `ProbeStateRunner.ensure_state(page, state, elements) -> dict`.
- Supports states: `feed_ready`, `comment_panel_open`, `comment_panel_closed`.

- [ ] **Step 1: Write failing allowlist tests**

```python
import asyncio

import pytest

from selector_probe.state_runner import ProbeSafetyError, ProbeStateRunner


class FakePage:
    def __init__(self):
        self.calls = []

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", url))


def test_forbidden_transition_fails_before_page_action():
    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(target_url="https://www.tiktok.com/")
        with pytest.raises(ProbeSafetyError) as caught:
            await runner.dispatch(page, {"type": "keyboard_input", "text": "blocked"})
        assert caught.value.code == "probe_action_forbidden"
        assert page.calls == []

    asyncio.run(scenario())


def test_feed_ready_uses_navigation_and_readiness_not_sleep_only():
    async def ready(_page):
        return {"ready": True, "origin": "https://www.tiktok.com"}

    async def scenario():
        page = FakePage()
        runner = ProbeStateRunner(
            target_url="https://www.tiktok.com/",
            readiness_check=ready,
        )
        result = await runner.ensure_state(page, "feed_ready", {})
        assert result["state"] == "feed_ready"
        assert page.calls == [("goto", "https://www.tiktok.com/")]

    asyncio.run(scenario())
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_state_runner.py -q -p no:cacheprovider
```

Expected: import failure.

- [ ] **Step 3: Implement the closed action vocabulary**

Define:

```python
ALLOWED_ACTIONS = {
    "navigate",
    "reload",
    "wait_ready",
    "bounded_scroll",
    "open_comment_panel",
    "close_comment_panel",
}

FORBIDDEN_ACTIONS = {
    "keyboard_input",
    "submit",
    "like",
    "follow",
    "publish",
    "account_update",
}
```

`dispatch` rejects any action outside `ALLOWED_ACTIONS`.

`open_comment_panel` must resolve the configured comment-entry alias with
`resolve_element`, click exactly once, then verify `visible_comment_panel`.
`close_comment_panel` uses Escape or a configured read-only close locator and
verifies the panel is absent. No transition retries an already dispatched
click.

Readiness evidence must include:

- expected HTTPS origin;
- non-empty page title or root document;
- no supported CAPTCHA/login-block marker;
- Skeleton readiness timeout result;
- current page state.

- [ ] **Step 4: Run state-runner tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_state_runner.py tests/test_browser_element_resolver.py -q -p no:cacheprovider
```

Expected: all tests pass.

## Task 6: AX+DOM semantic snapshot

**Files:**

- Create: `selector_probe/snapshot.py`
- Test: `tests/test_selector_probe_snapshot.py`

**Interfaces:**

- Produces: `SemanticNode`.
- Produces: `SemanticSnapshot`.
- Produces: `extract_semantic_snapshot(page, *, scope="page") -> SemanticSnapshot`.
- Produces: `SemanticSnapshot.model_payload() -> dict`.

- [ ] **Step 1: Write failing join and sanitization tests**

```python
from selector_probe.snapshot import build_semantic_snapshot


def test_snapshot_joins_ax_and_dom_by_backend_node_id():
    ax_nodes = [{
        "nodeId": "ax-1",
        "backendDOMNodeId": 42,
        "role": {"value": "button"},
        "name": {"value": "Comments"},
        "properties": [{"name": "disabled", "value": {"value": False}}],
    }]
    dom_nodes = [{
        "backend_node_id": 42,
        "parent_backend_node_id": 10,
        "tag": "button",
        "attributes": {"data-e2e": "comment-icon", "class": "css-1a2b3c"},
        "bounds": [10, 20, 30, 40],
        "visible": True,
        "in_viewport": True,
    }]
    snapshot = build_semantic_snapshot(ax_nodes, dom_nodes)
    node = snapshot.nodes[0]
    assert node.role == "button"
    assert node.name == "Comments"
    assert node.attributes == {"data-e2e": "comment-icon"}


def test_model_payload_drops_user_and_session_shaped_values():
    snapshot = build_semantic_snapshot([], [{
        "backend_node_id": 9,
        "parent_backend_node_id": None,
        "tag": "a",
        "attributes": {
            "href": "/@private-user/video/7523456789012345678",
            "data-e2e": "comment-icon",
        },
        "bounds": [0, 0, 10, 10],
        "visible": True,
        "in_viewport": True,
    }])
    text = str(snapshot.model_payload())
    assert "private-user" not in text
    assert "7523456789012345678" not in text
    assert "comment-icon" in text
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_snapshot.py -q -p no:cacheprovider
```

Expected: import failure.

- [ ] **Step 3: Implement CDP capture and pure join function**

Use a Playwright CDP session:

```python
session = await page.context.new_cdp_session(page)
await session.send("Accessibility.enable")
try:
    ax = await session.send("Accessibility.getFullAXTree")
    dom = await session.send(
        "DOMSnapshot.captureSnapshot",
        {
            "computedStyles": ["display", "visibility", "pointer-events", "opacity"],
            "includeDOMRects": True,
            "includePaintOrder": True,
        },
    )
finally:
    await session.send("Accessibility.disable")
    await session.detach()
```

Implement a pure `decode_dom_snapshot(payload) -> list[dict]` that:

- resolves CDP string-table indexes;
- reconstructs node attributes;
- maps `backendNodeId`;
- maps parent indexes to parent backend IDs;
- maps layout bounds and computed styles;
- computes viewport intersection from `page.viewport_size` or evaluated
  `window.innerWidth/innerHeight`.

Keep only:

```python
STABLE_ATTRIBUTES = {
    "data-e2e",
    "data-testid",
    "aria-label",
    "aria-labelledby",
    "name",
    "id",
    "placeholder",
    "role",
    "contenteditable",
    "type",
}
```

Drop values matching:

- video IDs containing 12 or more consecutive digits;
- URLs containing user handles;
- UUIDs;
- timestamps;
- cookie/token/authorization attribute names;
- generated class strings.

The output dataclasses must be JSON serializable and contain no raw HTML.

Use these exact shapes so later candidate and validation tasks consume the
same types:

```python
@dataclass(frozen=True)
class SemanticNode:
    backend_node_id: int
    parent_backend_node_id: int | None
    tag: str
    role: str
    name: str
    states: dict[str, bool | str | int | float | None]
    attributes: dict[str, str]
    bounds: tuple[float, float, float, float] | None
    visible: bool
    in_viewport: bool
    actionable: bool


@dataclass(frozen=True)
class SemanticSnapshot:
    nodes: tuple[SemanticNode, ...]
    scope: str = "page"
    viewport: tuple[int, int] | None = None

    def model_payload(self) -> dict:
        return {
            "scope": self.scope,
            "viewport": self.viewport,
            "nodes": [
                {
                    "backend_node_id": node.backend_node_id,
                    "parent_backend_node_id": node.parent_backend_node_id,
                    "tag": node.tag,
                    "role": node.role,
                    "name": node.name,
                    "states": dict(node.states),
                    "attributes": dict(node.attributes),
                    "bounds": node.bounds,
                    "visible": node.visible,
                    "in_viewport": node.in_viewport,
                    "actionable": node.actionable,
                }
                for node in self.nodes
            ],
        }
```

- [ ] **Step 4: Add a fake-CDP integration test**

Use a fake page/context/session returning deterministic `getFullAXTree` and
`captureSnapshot` payloads. Assert:

- `Accessibility.disable` always runs;
- `session.detach` always runs;
- only semantic nodes and required ancestors remain;
- visible and viewport flags are retained.

- [ ] **Step 5: Run snapshot tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_snapshot.py -q -p no:cacheprovider
```

Expected: all tests pass.

## Task 7: Observe-only orchestration and worker service

**Files:**

- Create: `selector_probe/probe.py`
- Create: `selector_probe/worker.py`
- Modify: `launcher.py:248-330`
- Modify: `launcher.py:734-749`
- Modify: `launcher.py:844-868`
- Modify: `launcher.py:1031-1104`
- Test: `tests/test_selector_probe_observe.py`
- Test: `tests/test_launcher_restart.py`

**Interfaces:**

- Produces: `run_observe_probe(config, store, redis_client, adspower_client, clock) -> dict`.
- Produces: CLI `python -m selector_probe.worker serve|tick`.
- Consumes: Tasks 1-6.

- [ ] **Step 1: Write failing orchestration tests**

```python
import pytest

from selector_probe.probe import ProbeLeaseLost, run_observe_probe


class FixedClock:
    def __init__(self, value):
        self.value = value

    def now(self):
        return self.value


class FakeStore:
    def __init__(self, events):
        self.events = events

    def record(self, event, **_payload):
        self.events.append(event)


class FakeRedis:
    def __init__(self, events):
        self.events = events

    def acquire(self, *_args, **_kwargs):
        self.events.append("lease:acquire")
        return True

    def heartbeat(self, *_args, **_kwargs):
        return True

    def release(self, *_args, **_kwargs):
        self.events.append("lease:release")


class LeaseLosingRedis(FakeRedis):
    def __init__(self):
        super().__init__([])

    def heartbeat(self, *_args, **_kwargs):
        return False


class Handle:
    def __init__(self, profile_id):
        self.profile_id = profile_id
        self.profile_mask = f"***{profile_id[-4:]}"
        self.started_by_probe = True


class FakeAdsPower:
    def __init__(self, events):
        self.events = events

    def open_profiles(self, profile_ids):
        return tuple(Handle(item) for item in profile_ids)

    def stop_owned(self, handle):
        self.events.append(f"stop:{handle.profile_id}")


def fake_config():
    return type("Config", (), {
        "enabled": True,
        "observe_only": True,
        "test_profile_ids": ("profile-a", "profile-b"),
    })()


def test_observe_probe_records_two_profiles_and_always_cleans_up():
    events = []
    result = run_observe_probe(
        config=fake_config(),
        store=FakeStore(events),
        redis_client=FakeRedis(events),
        adspower_client=FakeAdsPower(events),
        clock=FixedClock("2026-07-28T03:00:00+08:00"),
        inspect_profile=lambda handle, _config: {
            "profile_mask": handle.profile_mask,
            "round": 1,
            "status": "passed",
            "evidence": {"snapshot_hash": handle.profile_mask},
        },
    )
    assert result["status"] == "completed"
    assert result["observe_only"] is True
    assert events.count("stop:profile-a") == 1
    assert events.count("stop:profile-b") == 1
    assert "publish" not in events
    assert "pause_strategy" not in events


def test_observe_probe_lease_loss_blocks_completion_and_cleans_up():
    with pytest.raises(ProbeLeaseLost):
        run_observe_probe(
            config=fake_config(),
            store=FakeStore([]),
            redis_client=LeaseLosingRedis(),
            adspower_client=FakeAdsPower([]),
            clock=FixedClock("2026-07-28T03:00:00+08:00"),
        )
```

- [ ] **Step 2: Run tests and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_observe.py -q -p no:cacheprovider
```

Expected: import failure for `selector_probe.probe`.

- [ ] **Step 3: Implement observe-only orchestration**

`run_observe_probe` must:

1. return `disabled` when probe is disabled;
2. compute one due slot;
3. acquire the environment/site Redis lease;
4. create a durable run row;
5. start a heartbeat thread that calls `lease.renew()` every 30 seconds;
6. open both allowlisted profiles;
7. connect Playwright over each CDP URL without closing the browser;
8. ensure required read-only states;
9. capture semantic snapshots;
10. inspect saved elements with `inspect_element`;
11. record sanitized evidence;
12. mark the run `completed` or a precise failure;
13. stop heartbeat;
14. close every probe-created page and no pre-existing page;
15. stop only profiles started by the probe;
16. release only its own lease.

In this phase hard-code:

```python
assert config.observe_only is True
```

Do not import candidate, LLM, publishing, or gate modules.

- [ ] **Step 4: Implement worker CLI**

`selector_probe/worker.py` must support:

```text
serve
tick
```

Use:

```python
stop_file_value = os.getenv("SELECTOR_PROBE_STOP_FILE", "").strip()
```

The `serve` loop:

- loads fresh settings before every tick;
- sleeps in 0.5-second increments for 30 seconds;
- exits on SIGINT, SIGTERM, or stop-file presence;
- catches one probe-run exception, records/logs only a safe code, and continues;
- never prints profile IDs or CDP URLs.

- [ ] **Step 5: Add launcher supervisor tests**

```python
def test_selector_probe_supervisor_starts_hidden_worker(tmp_path):
    popen = FakePopenFactory()
    supervisor = SelectorProbeWorkerSupervisor(
        popen_factory=popen,
        stop_file=tmp_path / "stop",
        log_path=tmp_path / "probe.log",
    )
    supervisor.start(environment={"PATH": "test"})
    assert popen.calls[0]["args"] == [
        sys.executable,
        "-m",
        "selector_probe.worker",
        "serve",
    ]
    assert popen.calls[0]["env"]["SELECTOR_PROBE_STOP_FILE"] == str(tmp_path / "stop")
```

- [ ] **Step 6: Implement launcher ownership**

Add `SelectorProbeWorkerSupervisor` following
`StatisticsWorkerSupervisor`, with:

- separate stop file;
- `logs/selector-probe-worker.log`;
- hidden process options;
- state, start, and stop methods.

Add it to:

- `LauncherApp.__init__`;
- `_stop_services_best_effort`;
- `_restart_services`;
- startup health checks;
- failure details.

Do not merge it into the statistics worker.

- [ ] **Step 7: Run phase-1 focused tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_config.py tests/test_selector_probe_store.py tests/test_selector_probe_scheduler.py tests/test_selector_probe_session.py tests/test_selector_probe_state_runner.py tests/test_selector_probe_snapshot.py tests/test_selector_probe_observe.py tests/test_launcher_restart.py -q -p no:cacheprovider -W error
```

Expected: all selected tests pass.

- [ ] **Step 8: Run existing locator and settings regressions**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_browser_element_schema.py tests/test_browser_element_resolver.py tests/test_settings_store.py tests/test_app.py -k "element or settings or launcher" -q -p no:cacheprovider -W error
node --test tests-js/browser-strategy-ui.test.js
```

Expected: all selected tests pass.

- [ ] **Step 9: Verify observe-only boundary**

Run a repository search:

```powershell
rg -n "publish|strategy_gate|pause_strategy|keyboard_input|submit" selector_probe
```

Expected:

- `publish`, `strategy_gate`, and `pause_strategy` absent from runtime code;
- forbidden action names occur only in the explicit denylist and tests;
- no selector-probe runtime path calls keyboard input or submit.

## Phase-1 completion

Deliverable is accepted when:

- worker starts and stops through launcher;
- daily due logic and lease tests pass;
- at least two allowlisted profiles are required;
- ownership tests prove pre-existing windows remain open;
- AX+DOM snapshot contains no raw HTML or sensitive identifiers;
- observe-only run stores evidence;
- no selector publication or strategy pause is possible;
- existing locator and UI suites remain green.
