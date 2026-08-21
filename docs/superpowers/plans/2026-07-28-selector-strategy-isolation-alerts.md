# Selector Strategy Isolation and Alerts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce selector-aware strategy isolation, automatic recovery, manual pause precedence, alert-center/webhook delivery, and complete dashboard controls.

**Architecture:** A durable dependency index maps element aliases to strategies. Gate reasons are additive and projected to Redis; execution checks gates before acceptance, after reservation, before every action, and immediately before side effects. Probe failures create only alias-dependent gates and sanitized alerts; validated atomic publication clears only probe gates. Dashboard APIs and UI expose settings, health, history, manual controls, and alert lifecycle.

**Tech Stack:** Python 3.11+, Flask, Playwright runtime hooks, SQLite, Redis, requests, vanilla JavaScript, Node test runner, pytest.

## Global Constraints

- Complete observe-only and healing-registry plans first.
- Execute Tasks 1-5 of this plan, then
  `2026-07-28-local-management-auth.md`, then
  `2026-07-28-selector-probe-management-console.md`, and return here for
  Task 7 rollout verification.
- Pause only strategies referencing failed element aliases.
- Any uncleared gate reason pauses a strategy.
- Automatic recovery clears only `source=probe`.
- Manual controls clear only `source=manual`.
- Manual pause always survives automatic recovery.
- Check gates before scheduling, after work receipt/reservation, before every action, and immediately before side-effect dispatch.
- Never undo or retry an already dispatched click or submit.
- Queued unstarted runs may be delayed; partially executed runs are terminal and never auto-resume from the middle.
- Redis outage fails closed only for registry-managed strategies.
- Infrastructure probe failure retries after 15, 30, and 60 minutes; 36 hours without a valid probe pauses all probe-managed strategies.
- Alerts contain sanitized codes, aliases, strategy IDs, masked profiles, versions, retry summaries, and redacted screenshots only.
- Screenshots retain seven days.
- Webhook failure never blocks gate creation, probe cleanup, or durable alert recording.
- Repository currently has no Git metadata. Do not initialize Git without user approval.

---

## File structure

Create:

- `selector_probe/gates.py` — dependency graph, effective gates, Redis projection.
- `selector_probe/alerts.py` — durable alert lifecycle and deduplication.
- `selector_probe/webhook.py` — signed webhook payloads and retry delivery.
- `selector_probe/redaction.py` — evidence and screenshot redaction policy.
- `tests/test_selector_probe_gates.py`
- `tests/test_selector_probe_runtime_gates.py`
- `tests/test_selector_probe_alerts.py`
- `tests/test_selector_probe_webhook.py`
- `tests/test_selector_probe_redaction.py`
- `tests-js/selector-probe-ui.test.js`

Modify:

- `selector_probe/store.py` — gate, alert, and webhook-outbox schema.
- `selector_probe/probe.py` — create/clear probe gates and alerts.
- `selector_probe/registry.py` — clear probe gates only after reconciled publication.
- `selector_probe/blueprint.py` — settings, health, gate, alert, and webhook routes.
- `selector_probe/worker.py` — webhook delivery, screenshot cleanup, 36-hour safety gate.
- `browser_strategy_runtime.py:574-679` — injected action-level gate checks.
- `gateway/app.py:6507-6624` — request-level gate checks and paused results.
- `gateway/app.py:1907-2052` — probe, health, gate, and alert markup.
- `gateway/static/browser_strategy_ui.js` — controller state and rendering.
- `gateway/static/dashboard_shell.css` — health/gate/alert styling.
- `gateway/settings_store.py` — public secret metadata and probe settings merge behavior.
- `tests/test_browser_strategy_runtime.py`
- `tests/test_app.py`
- `tests/test_settings_routes.py`
- `tests-js/browser-strategy-ui.test.js`

## Task 1: Dependency index and additive gate reasons

**Files:**

- Create: `selector_probe/gates.py`
- Modify: `selector_probe/store.py`
- Test: `tests/test_selector_probe_gates.py`
- Test: `tests/test_selector_probe_store.py`

**Interfaces:**

- Produces: `build_dependency_index(strategies) -> dict[str, tuple[StrategyDependency, ...]]`.
- Produces: `StrategyGateService`.
- `pause_for_aliases(aliases, reason_code, selector_version_id) -> tuple[str, ...]`.
- `set_manual_pause(strategy_id, paused, actor) -> dict`.
- `clear_probe_reasons(strategy_ids, selector_version_id) -> tuple[str, ...]`.
- `check(strategy_id) -> GateDecision`.

- [ ] **Step 1: Write failing dependency tests**

```python
from selector_probe.gates import build_dependency_index


def test_dependency_index_maps_alias_to_exact_strategy_actions():
    strategies = [{
        "id": "comment-flow",
        "name": "Comment flow",
        "actions": [
            {"id": "entry", "type": "click", "params": {"element": "评论入口"}},
            {"id": "wait", "type": "pause", "params": {"duration_seconds": [1, 1]}},
            {"id": "submit", "type": "click", "params": {"element": "评论提交按钮"}},
        ],
    }]
    index = build_dependency_index(strategies)
    assert [(item.strategy_id, item.action_id) for item in index["评论入口"]] == [
        ("comment-flow", "entry")
    ]
    assert [(item.strategy_id, item.action_id) for item in index["评论提交按钮"]] == [
        ("comment-flow", "submit")
    ]
    assert "wait" not in str(index)
```

- [ ] **Step 2: Write failing additive-reason tests**

```python
def test_probe_recovery_never_clears_manual_pause(gate_service):
    gate_service.set_manual_pause("comment-flow", True, actor="admin")
    gate_service.pause_for_aliases(
        ("评论入口",),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )
    gate_service.clear_probe_reasons(("comment-flow",), "sel-new")
    decision = gate_service.check("comment-flow")
    assert decision.allowed is False
    assert [item.source for item in decision.reasons] == ["manual"]


def test_failed_alias_pauses_only_dependent_strategy(gate_service):
    paused = gate_service.pause_for_aliases(
        ("评论提交按钮",),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )
    assert paused == ("comment-flow",)
    assert gate_service.check("reader-flow").allowed is True
```

- [ ] **Step 3: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_gates.py -q -p no:cacheprovider
```

Expected: import failure.

- [ ] **Step 4: Add durable gate schema**

```sql
CREATE TABLE IF NOT EXISTS strategy_dependencies (
    alias TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    action_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    PRIMARY KEY(alias, strategy_id, action_id)
);
CREATE TABLE IF NOT EXISTS strategy_gate_reasons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('probe', 'manual')),
    reason_code TEXT NOT NULL,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    selector_version_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    cleared_at TEXT,
    cleared_by TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_gate_reason
ON strategy_gate_reasons(
    strategy_id,
    source,
    reason_code,
    selector_version_id
)
WHERE cleared_at IS NULL;
```

Rebuild dependencies by validating the complete new index first, then replacing
all rows in one SQLite transaction.

- [ ] **Step 5: Implement gate decisions and Redis projection**

Use:

```python
@dataclass(frozen=True)
class GateReason:
    source: str
    reason_code: str
    aliases: tuple[str, ...]
    selector_version_id: str
    created_at: str


@dataclass(frozen=True)
class GateDecision:
    strategy_id: str
    allowed: bool
    reasons: tuple[GateReason, ...]
```

Redis projection key:

```text
strategy_gate:{environment}:{strategy_id}
```

Projection value is the complete effective decision JSON. Rebuild it after each
durable gate transaction. If Redis projection fails, keep durable reasons and
return `registry_unavailable` for managed strategies.

- [ ] **Step 6: Run gate tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_gates.py tests/test_selector_probe_store.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 2: Runtime gate checks without duplicate actions

**Files:**

- Modify: `browser_strategy_runtime.py:574-679`
- Test: `tests/test_selector_probe_runtime_gates.py`
- Test: `tests/test_browser_strategy_runtime.py`

**Interfaces:**

- Add optional `gate_check: Callable[[str, dict | None], Awaitable[object] | object] | None`.
- Produces: `StrategyPausedError(strategy_id, action_id, action_index, reasons, completed_actions)`.
- Gate callback receives `(strategy_id, action_or_none)`.

- [ ] **Step 1: Write failing pre-action pause test**

```python
import asyncio

import pytest

from browser_strategy_config import DEFAULT_ACTION_PARAMS
from browser_strategy_runtime import StrategyPausedError, run_block_strategy


def action(action_id):
    params = dict(DEFAULT_ACTION_PARAMS["click"])
    params["element"] = "目标"
    return {"id": action_id, "type": "click", "params": params}


def strategy(*actions):
    return {
        "id": "comment-flow",
        "name": "Comment flow",
        "run_mode": "once",
        "batch_size": 1,
        "actions": list(actions),
        "status": "ready",
    }


def elements():
    return {
        "目标": {
            "scope": "page",
            "locators": [{
                "id": "target",
                "type": "css",
                "value": "button",
                "enabled": True,
                "fallback": False,
            }],
        }
    }


def test_gate_stops_before_first_action():
    async def scenario():
        executed = []

        def gate_check(_strategy_id, current_action):
            return {
                "allowed": current_action is None,
                "reasons": [{
                    "source": "probe",
                    "reason_code": "selector_validation_failed",
                }],
            }

        async def execute_fn(*_args, **_kwargs):
            executed.append("executed")
            return {"status": "ok"}

        with pytest.raises(StrategyPausedError) as caught:
            await run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=execute_fn,
            )
        assert caught.value.action_index == 1
        assert executed == []

    asyncio.run(scenario())
```

- [ ] **Step 2: Write failing mid-run non-duplication test**

```python
def test_gate_appearing_after_click_never_retries_or_resumes_remainder():
    async def scenario():
        checks = 0
        executed = []

        def gate_check(_strategy_id, _action):
            nonlocal checks
            checks += 1
            allowed = checks <= 3
            return {
                "allowed": allowed,
                "reasons": [] if allowed else [{"source": "manual"}],
            }

        async def execute_fn(_page, current_action, *_args, **_kwargs):
            executed.append(current_action["id"])
            return {"action_id": current_action["id"], "status": "ok"}

        with pytest.raises(StrategyPausedError) as caught:
            await run_block_strategy(
                page=object(),
                strategy=strategy(action("click-1"), action("click-2")),
                elements=elements(),
                patterns=[],
                text_resolver=lambda *_: "",
                gate_check=gate_check,
                execute_fn=execute_fn,
            )
        assert executed == ["click-1"]
        assert caught.value.completed_actions[0]["action_id"] == "click-1"

    asyncio.run(scenario())
```

- [ ] **Step 3: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_runtime_gates.py -q -p no:cacheprovider
```

Expected: `run_block_strategy` rejects `gate_check` or
`StrategyPausedError` is missing.

- [ ] **Step 4: Implement gate hook**

Add:

```python
async def _check_strategy_gate(gate_check, strategy_id, action):
    if gate_check is None:
        return
    decision = gate_check(strategy_id, action)
    if inspect.isawaitable(decision):
        decision = await decision
    if isinstance(decision, dict) and decision.get("allowed") is False:
        raise StrategyPausedError(
            strategy_id=strategy_id,
            action_id=str(action.get("id") if action else ""),
            action_index=0,
            reasons=list(decision.get("reasons") or []),
            completed_actions=[],
        )
```

Call:

1. once after strategy normalization with `action=None`;
2. before every action;
3. inside `invoke` immediately before `execute_fn`.

When rethrowing from the loop, preserve:

- real action index;
- current cycle;
- safe reasons;
- completed actions;
- page recoveries.

Never catch `StrategyPausedError` as a locator error.

- [ ] **Step 5: Run runtime tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_runtime_gates.py tests/test_browser_strategy_runtime.py tests/test_browser_actions.py -q -p no:cacheprovider -W error
```

Expected: all tests pass; action invocation counts remain unchanged.

## Task 3: Request-level gates and delayed unstarted runs

**Files:**

- Modify: `gateway/app.py:6507-6624`
- Modify: `selector_probe/blueprint.py`
- Test: `tests/test_app.py`
- Test: `tests/test_selector_probe_routes.py`

**Interfaces:**

- Request gate returns HTTP `409` with code `strategy_paused`.
- Mid-run pause returns per-profile failure code
  `strategy_paused_during_execution`.
- Unstarted asynchronous jobs store `delayed_gate` rather than being dropped.

- [ ] **Step 1: Write failing request-gate test**

```python
def test_paused_strategy_is_rejected_before_profile_start(client, monkeypatch):
    starts = []
    monkeypatch.setattr(fake_gate_service, "check", lambda _strategy_id: {
        "allowed": False,
        "reasons": [{"source": "probe", "aliases": ["评论入口"]}],
    })
    monkeypatch.setattr(fake_adspower, "start_browser", lambda profile_id: starts.append(profile_id))
    response = client.post("/api/browser/execute-strategy", json=execute_payload())
    assert response.status_code == 409
    assert response.get_json()["code"] == "strategy_paused"
    assert starts == []
```

- [ ] **Step 2: Write failing unrelated-strategy test**

```python
def test_unrelated_strategy_runs_while_comment_strategy_is_paused(client, monkeypatch):
    monkeypatch.setattr(
        fake_gate_service,
        "check",
        lambda strategy_id: {
            "allowed": strategy_id == "reader-flow",
            "reasons": [] if strategy_id == "reader-flow" else [{"source": "probe"}],
        },
    )
    response = client.post(
        "/api/browser/execute-strategy",
        json=execute_payload(strategy_id="reader-flow"),
    )
    assert response.status_code == 200
```

- [ ] **Step 3: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py -k "paused_strategy or unrelated_strategy" -q -p no:cacheprovider
```

Expected: routes execute without consulting gates.

- [ ] **Step 4: Inject gate service through app config**

Add:

```python
app.config.setdefault("SELECTOR_PROBE_GATE_SERVICE_FACTORY", default_gate_service_factory)
```

In execute route:

1. resolve canonical strategy;
2. obtain gate decision before any profile operation;
3. return sanitized `409 strategy_paused` when denied;
4. after per-profile reservation, check again;
5. pass `gate_check` into `run_prepared_block_strategy_on_cdp`;
6. map `StrategyPausedError` without raw selectors or full profile IDs.

Do not revoke or delete queued jobs. Mark jobs that have not started
`delayed_gate`. Partially executed jobs remain terminal and require a new run.

- [ ] **Step 5: Add gate APIs**

Routes:

- `GET /api/selector-probe/gates`
- `POST /api/selector-probe/strategies/<strategy_id>/pause`
- `POST /api/selector-probe/strategies/<strategy_id>/resume`

Manual pause body:

```json
{"reason": "operator_pause"}
```

Manual resume clears only manual reasons. Return remaining effective reasons.

- [ ] **Step 6: Run route and runtime tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py tests/test_app.py tests/test_browser_strategy_runtime.py -k "gate or pause or execute_strategy" -q -p no:cacheprovider -W error
```

Expected: all selected tests pass.

## Task 4: Durable alerts, redaction, and signed webhooks

**Files:**

- Create: `selector_probe/alerts.py`
- Create: `selector_probe/redaction.py`
- Create: `selector_probe/webhook.py`
- Modify: `selector_probe/store.py`
- Modify: `selector_probe/worker.py`
- Test: `tests/test_selector_probe_alerts.py`
- Test: `tests/test_selector_probe_redaction.py`
- Test: `tests/test_selector_probe_webhook.py`

**Interfaces:**

- Produces: `AlertService.open_or_update`, `acknowledge`, `resolve`.
- Produces: `redact_evidence(value, profile_ids) -> object`.
- Produces:
  `capture_redacted_screenshot(page, regions, target_path) -> Path`.
- Produces: `WebhookDispatcher.deliver_due(now) -> dict`.

- [ ] **Step 1: Write failing alert-deduplication test**

```python
def test_repeated_failure_updates_one_open_alert(alert_service):
    first = alert_service.open_or_update(
        site="tiktok",
        failure_class="selector_validation_failed",
        aliases=("评论入口",),
        active_version="sel-old",
        details={"retry_count": 3},
    )
    second = alert_service.open_or_update(
        site="tiktok",
        failure_class="selector_validation_failed",
        aliases=("评论入口",),
        active_version="sel-old",
        details={"retry_count": 3},
    )
    assert second["id"] == first["id"]
    assert second["occurrence_count"] == 2
```

- [ ] **Step 2: Write failing redaction test**

```python
def test_alert_payload_removes_profiles_cdp_secrets_and_comment_text():
    result = redact_evidence(
        {
            "profile_id": "profile-complete-secret",
            "cdp_url": "ws://127.0.0.1/devtools/browser/secret",
            "authorization": "Bearer token",
            "comment_text": "private comment",
            "code": "selector_validation_failed",
        },
        profile_ids=("profile-complete-secret",),
    )
    text = str(result)
    assert "profile-complete-secret" not in text
    assert "devtools/browser" not in text
    assert "Bearer token" not in text
    assert "private comment" not in text
    assert "selector_validation_failed" in text
```

- [ ] **Step 3: Write failing webhook signature test**

```python
def test_generic_webhook_uses_timestamped_hmac_signature():
    captured = {}

    def request_fn(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeResponse(200)

    dispatcher = WebhookDispatcher(
        request_fn=request_fn,
        url="https://hooks.example.test/probe",
        signing_secret="secret",
    )
    dispatcher.send({"alert_id": 1, "code": "selector_validation_failed"}, timestamp=1000)
    assert captured["headers"]["X-Selector-Probe-Timestamp"] == "1000"
    expected = hmac.new(
        b"secret",
        b"1000." + captured["data"],
        hashlib.sha256,
    ).hexdigest()
    assert captured["headers"]["X-Selector-Probe-Signature"] == expected
```

- [ ] **Step 4: Run and verify RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_alerts.py tests/test_selector_probe_redaction.py tests/test_selector_probe_webhook.py -q -p no:cacheprovider
```

Expected: imports fail.

- [ ] **Step 5: Add alert and delivery schema**

```sql
CREATE TABLE IF NOT EXISTS probe_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_class TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    strategy_ids_json TEXT NOT NULL,
    active_version TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrence_count INTEGER NOT NULL,
    details_json TEXT NOT NULL,
    screenshot_path TEXT NOT NULL DEFAULT '',
    acknowledged_at TEXT,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_open_probe_alert
ON probe_alerts(fingerprint)
WHERE status IN ('open', 'acknowledged');
CREATE TABLE IF NOT EXISTS webhook_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL REFERENCES probe_alerts(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
```

Fingerprint:

```python
sha256(
    f"{site}\0{failure_class}\0{','.join(sorted(aliases))}\0{active_version}".encode()
).hexdigest()
```

- [ ] **Step 6: Implement evidence and screenshot redaction**

Evidence redaction recursively:

- removes keys matching `cookie`, `authorization`, `token`, `secret`,
  `comment_text`, `input_text`, `cdp_url`, `ws_url`, `dom`, `html`, or
  `accessibility_tree`;
- replaces exact configured profile IDs with `mask_profile_id`;
- limits strings to 500 characters;
- limits lists to 50 items and dicts to 100 keys.

Screenshot redaction:

- capture only the relevant viewport or scope box;
- expand configured sensitive rectangles by 4 CSS pixels;
- fill redacted regions with opaque black;
- remove image metadata;
- write JPEG quality 70;
- create opaque, fixed-position black overlay nodes for every sensitive
  rectangle with `page.evaluate`;
- capture JPEG quality 70 directly from Playwright while overlays exist;
- return only the redacted JPEG bytes or redacted final path;
- remove all overlay nodes in `finally`;
- never create or retain an unredacted intermediate;
- verify the resulting JPEG contains no EXIF, XMP, or comment metadata.

Do not add Pillow. Keep `requirements.txt` unchanged.

- [ ] **Step 7: Implement webhook retries**

Timeout: 10 seconds.

Retry delays:

```python
RETRY_SECONDS = (60, 300, 1800, 7200, 21600)
```

After five failures, mark delivery `failed` and retain the dashboard alert.
Webhook payload never includes local filesystem paths; screenshots are served
through authenticated local dashboard routes or omitted from external webhook
when no safe external URL exists.

- [ ] **Step 8: Add seven-day cleanup test**

```python
def test_cleanup_removes_only_expired_alert_screenshots(tmp_path, alert_service):
    old = tmp_path / "old.jpg"
    recent = tmp_path / "recent.jpg"
    old.write_bytes(b"old")
    recent.write_bytes(b"recent")
    alert_service.record_screenshot(alert_id=1, path=old, created_at="2026-07-20T00:00:00Z")
    alert_service.record_screenshot(alert_id=2, path=recent, created_at="2026-07-27T00:00:00Z")
    deleted = alert_service.cleanup_screenshots(now="2026-07-28T00:00:00Z", retention_days=7)
    assert deleted == 1
    assert old.exists() is False
    assert recent.exists() is True
```

- [ ] **Step 9: Run alert tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_alerts.py tests/test_selector_probe_redaction.py tests/test_selector_probe_webhook.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 5: Probe failure gates, recovery, and 36-hour safety policy

**Files:**

- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/registry.py`
- Modify: `selector_probe/worker.py`
- Test: `tests/test_selector_probe_observe.py`
- Test: `tests/test_selector_probe_registry.py`
- Test: `tests/test_selector_probe_gates.py`

**Interfaces:**

- Selector failure creates gates only for failed aliases.
- Publication reconciliation clears only matching probe gates.
- `enforce_probe_freshness(now, last_validated_at, managed_strategy_ids)`.

- [ ] **Step 1: Write failing selector-failure isolation test**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryDecision:
    allowed: bool
    reasons: tuple


class MemoryGates:
    def __init__(self):
        self.dependencies = {
            "评论入口": ("comment-flow",),
            "阅读入口": ("reader-flow",),
        }
        self.reasons = {}
        self.paused = ()

    def pause_for_aliases(self, aliases, **_context):
        affected = sorted({
            strategy_id
            for alias in aliases
            for strategy_id in self.dependencies.get(alias, ())
        })
        for strategy_id in affected:
            self.reasons.setdefault(strategy_id, set()).add("probe")
        self.paused = tuple(affected)
        return self.paused

    def set_manual_pause(self, strategy_id, paused, actor):
        del actor
        reasons = self.reasons.setdefault(strategy_id, set())
        if paused:
            reasons.add("manual")
        else:
            reasons.discard("manual")

    def clear_probe_reasons(self, strategy_ids, _selector_version_id):
        for strategy_id in strategy_ids:
            self.reasons.setdefault(strategy_id, set()).discard("probe")

    def pause_managed(self, strategy_ids, **_context):
        self.paused = tuple(sorted(strategy_ids))
        for strategy_id in self.paused:
            self.reasons.setdefault(strategy_id, set()).add("probe")
        return self.paused

    def check(self, strategy_id):
        sources = tuple(
            type("Reason", (), {"source": source})()
            for source in sorted(self.reasons.get(strategy_id, set()))
        )
        return MemoryDecision(allowed=not sources, reasons=sources)


class MemoryAlerts:
    def __init__(self):
        self.open_count = 0

    def create(self, **_payload):
        self.open_count += 1


class EnforcingFailureRuntime:
    rollout_mode = "enforce"

    def __init__(self):
        self.gates = MemoryGates()
        self.alerts = MemoryAlerts()

    def validate_active(self):
        return {"status": "failed", "failed_aliases": ["评论入口"]}

    def validation_context(self):
        return {
            "active_bundle": {"version": "sel-old", "elements": {}},
            "snapshot": {"nodes": []},
            "contracts": {"评论入口": {"intent": "open comments"}},
        }

    def validate_candidate(self, bundle):
        return {
            "status": "failed",
            "failed_aliases": ["评论入口"],
            "bundle": bundle,
        }

    def store_and_publish(self, _bundle):
        raise AssertionError("failed candidate must not publish")


def test_final_selector_failure_pauses_only_alias_dependents():
    runtime = EnforcingFailureRuntime()
    result = run_healing_probe(
        runtime=runtime,
        repair_fn=lambda attempt, **_kwargs: {
            "version": f"failed-{attempt}",
            "elements": {"评论入口": {"scope": "active_video", "locators": []}},
        },
    )
    assert result["status"] == "selector_validation_failed"
    assert result["paused_strategies"] == ["comment-flow"]
    assert runtime.gates.check("reader-flow").allowed is True
    assert runtime.alerts.open_count == 1
```

- [ ] **Step 2: Write failing recovery-precedence test**

```python
def test_published_recovery_clears_probe_reason_not_manual_reason():
    gates = MemoryGates()
    gates.pause_for_aliases(
        ("评论入口",),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )
    gates.set_manual_pause("comment-flow", True, actor="admin")
    reconcile_successful_publication(
        selector_version_id="sel-new",
        covered_aliases=("评论入口",),
        gates=gates,
    )
    decision = gates.check("comment-flow")
    assert decision.allowed is False
    assert [item.source for item in decision.reasons] == ["manual"]
```

- [ ] **Step 3: Write failing 36-hour freshness test**

```python
def test_probe_unavailable_pauses_managed_strategies_only_after_36_hours():
    gates = MemoryGates()
    enforce_probe_freshness(
        now="2026-07-28T15:00:00Z",
        last_validated_at="2026-07-27T02:59:59Z",
        managed_strategy_ids=("comment-flow",),
        gate_service=gates,
    )
    assert gates.paused == ("comment-flow",)
    assert "reader-flow" not in gates.paused
```

- [ ] **Step 4: Implement exact failure policy**

Selector failure after three repairs:

1. preserve current active and last-known-good bundles;
2. create probe reasons for strategies in failed-alias dependency index;
3. write Redis gate projections;
4. create deduplicated alert and webhook outbox event;
5. clean up probe-owned windows.

Infrastructure failure:

- retry after 15, 30, and 60 minutes;
- do not generate candidates or alter selectors;
- set `probe_unavailable`;
- after 36 hours without valid evidence, add
  `probe_validation_stale` only to managed strategies.

Successful reconciled publication:

- clear `selector_validation_failed`, `probe_validation_stale`, and
  `registry_unavailable` probe reasons only for strategies whose aliases are
  covered by the published bundle;
- leave all manual reasons;
- resolve matching alerts and queue one recovery webhook.

- [ ] **Step 5: Run policy tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_observe.py tests/test_selector_probe_registry.py tests/test_selector_probe_gates.py tests/test_selector_probe_alerts.py -q -p no:cacheprovider -W error
```

Expected: all tests pass.

## Task 6: Dashboard settings, health, gates, and alerts

> **Superseded integration task:** Do not execute the steps in Task 6. The
> approved management-UI supplement expanded this scope and added local
> authentication. Execute
> `docs/superpowers/plans/2026-07-28-local-management-auth.md` and
> `docs/superpowers/plans/2026-07-28-selector-probe-management-console.md`
> instead. Task 7 remains the final rollout gate after both plans pass.

**Files:**

- Modify: `selector_probe/blueprint.py`
- Modify: `gateway/app.py:1907-2052`
- Modify: `gateway/static/browser_strategy_ui.js`
- Modify: `gateway/static/dashboard_shell.css`
- Test: `tests/test_selector_probe_routes.py`
- Test: `tests/test_app.py`
- Create: `tests-js/selector-probe-ui.test.js`
- Modify: `tests-js/browser-strategy-ui.test.js`

**Interfaces:**

- Controller state adds `probe`, `elementHealth`, `strategyGates`, `alerts`.
- Routes add settings, run-now, history, manual gate, alert acknowledge, and
  webhook test operations.

- [ ] **Step 1: Write failing controller-load test**

```javascript
const assert = require("node:assert/strict");
const test = require("node:test");

const {createBrowserStrategyUI} = require("../gateway/static/browser_strategy_ui");


function response(status, data) {
  return {status, data};
}


function resources(url) {
  const values = {
    "/api/browser/elements": response(200, {elements: {}}),
    "/api/browser/patterns": response(200, {patterns: []}),
    "/api/browser/strategies": response(200, {strategies: []}),
    "/api/browser/action-catalog": response(200, {catalog: {}, defaults: {}}),
    "/api/content/brands": response(200, {brands: []}),
    "/api/selector-probe/status": response(200, {status: "healthy"}),
    "/api/selector-probe/gates": response(200, {gates: []}),
    "/api/selector-probe/alerts": response(200, {alerts: []}),
  };
  return values[url];
}


function controller() {
  return createBrowserStrategyUI({
    requestJson: async (url) => resources(url),
    selectedBrowserWindows: () => [],
    setTimeout: () => 1,
    clearTimeout() {},
    addBeforeUnload() {},
    removeBeforeUnload() {},
    confirm: () => true,
    nowId: (prefix) => `${prefix}_1`,
    render() {},
    targetElementSelectors: () => [],
  });
}


test("init loads probe health gates and alerts without mutating element drafts", async () => {
  const requests = [];
  const controller = createBrowserStrategyUI({
    requestJson: async (url) => {
      requests.push(url);
      return resources(url);
    },
    selectedBrowserWindows: () => [],
    setTimeout: () => 1,
    clearTimeout() {},
    addBeforeUnload() {},
    removeBeforeUnload() {},
    confirm: () => true,
    nowId: (prefix) => `${prefix}_1`,
    render() {},
    targetElementSelectors: () => [],
  });
  await controller.init();
  assert.equal(controller.state.probe.status, "healthy");
  assert.deepEqual(controller.state.strategyGates, []);
  assert.deepEqual(controller.state.alerts, []);
  assert.ok(requests.includes("/api/selector-probe/status"));
});
```

- [ ] **Step 2: Write failing strategy-option gate test**

```javascript
test("execution options disable only effectively paused strategies", () => {
  const ui = controller();
  ui.state.strategies = [
    {id: "comment-flow", name: "Comment", status: "ready", actions: []},
    {id: "reader-flow", name: "Reader", status: "ready", actions: []},
  ];
  ui.state.strategyGates = [
    {strategy_id: "comment-flow", effective_status: "paused", reasons: [{source: "probe"}]},
  ];
  const select = {
    options: [],
    replaceChildren(...items) {
      this.options = items;
    },
    ownerDocument: {
      createElement() {
        return {value: "", textContent: "", disabled: false};
      },
    },
  };
  const options = ui.syncExecutionOptions(select);
  assert.equal(options.find((item) => item.value === "comment-flow").disabled, true);
  assert.equal(options.find((item) => item.value === "reader-flow").disabled, false);
});
```

- [ ] **Step 3: Run and verify RED**

```powershell
node --test tests-js/selector-probe-ui.test.js
```

Expected: probe state and APIs are absent.

- [ ] **Step 4: Add API routes**

Add:

- `GET /api/selector-probe/settings`
- `PUT /api/selector-probe/settings`
- `GET /api/selector-probe/status`
- `GET /api/selector-probe/active`
- `GET /api/selector-probe/runs`
- `GET /api/selector-probe/versions`
- `GET /api/selector-probe/gates`
- `GET /api/selector-probe/alerts`
- `POST /api/selector-probe/run-now`
- `POST /api/selector-probe/webhook/test`
- `POST /api/selector-probe/strategies/<strategy_id>/pause`
- `POST /api/selector-probe/strategies/<strategy_id>/resume`
- `POST /api/selector-probe/alerts/<int:alert_id>/acknowledge`
- `POST /api/selector-probe/alerts/<int:alert_id>/resolve`

Rules:

- settings PUT uses existing locked settings path;
- require at least two unique profiles before enabling;
- never return full profile IDs or webhook secret;
- webhook test sends a synthetic sanitized payload;
- run-now returns `202`;
- manual gate routes audit actor as `local-admin`;
- screenshot route serves only retained sanitized files by numeric alert ID.

- [ ] **Step 5: Add dashboard markup**

Add sections:

- probe enabled, time, timezone;
- masked test-profile list;
- model selector;
- webhook type, URL, secret, test button;
- run-now button and current status;
- element health/version/evidence badges;
- strategy gate reasons and manual controls;
- alert list with acknowledge/resolve;
- history dialog;
- sanitized screenshot dialog.

Use `textContent`, `replaceChildren`, and DOM-created nodes. Do not render API
values through `innerHTML`.

- [ ] **Step 6: Extend controller**

Initialization loads:

```javascript
[
  ["/api/selector-probe/status", "probe", null],
  ["/api/selector-probe/gates", "strategyGates", "gates"],
  ["/api/selector-probe/alerts", "alerts", "alerts"],
]
```

Add methods:

- `loadProbeResources`;
- `saveProbeSettings`;
- `runProbeNow`;
- `testProbeWebhook`;
- `setManualStrategyPause`;
- `acknowledgeAlert`;
- `resolveAlert`;
- `loadProbeHistory`.

`syncExecutionOptions` disables strategies with an effective paused gate in
addition to `needs_repair`.

- [ ] **Step 7: Add route security and UI tests**

Tests must prove:

- secret fields return configured booleans only;
- complete profile IDs never appear;
- unrelated strategy remains enabled;
- manual resume leaves probe reason visible;
- auto recovery leaves manual reason visible;
- alert values render as text, not HTML;
- failed saves keep form state;
- run-now busy state is shown;
- screenshot URL uses numeric alert ID only.

- [ ] **Step 8: Run UI and route tests**

```powershell
node --test tests-js/selector-probe-ui.test.js tests-js/browser-strategy-ui.test.js
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_routes.py tests/test_app.py tests/test_settings_routes.py -k "selector_probe or gate or alert or browser_strategy_ui" -q -p no:cacheprovider -W error
```

Expected: all selected tests pass.

## Task 7: Rollout flags and final verification

**Files:**

- Modify: `selector_probe/config.py`
- Modify: `selector_probe/probe.py`
- Modify: `selector_probe/worker.py`
- Modify: `gateway/settings_store.py`
- Create: `docs/superpowers/reports/2026-07-28-selector-probe-verification.md`
- Test: all selector-probe tests and supported suites.

**Interfaces:**

- Adds rollout mode: `observe`, `publish`, `enforce`.
- Default remains `observe` on migration.

- [ ] **Step 1: Replace `observe_only` with explicit rollout mode**

Normalize:

```python
ROLLOUT_MODES = {"observe", "publish", "enforce"}
```

Migration:

- existing `observe_only=True` becomes `rollout_mode="observe"`;
- existing `observe_only=False` becomes `rollout_mode="publish"`;
- new default is `observe`;
- only `enforce` creates gates;
- all modes retain read-only probe actions.

- [ ] **Step 2: Add rollout-boundary tests**

Implement the pure boundary helper
`execute_rollout(mode, validated_bundle, publish_fn, enforce_fn) -> dict`.
It calls `publish_fn` only for `publish` and `enforce`, then calls
`enforce_fn` only for `enforce` and only after publication succeeds.

```python
@pytest.mark.parametrize(
    ("mode", "publishes", "enforces"),
    [
        ("observe", False, False),
        ("publish", True, False),
        ("enforce", True, True),
    ],
)
def test_rollout_mode_boundaries(mode, publishes, enforces):
    calls = []
    result = execute_rollout(
        mode,
        validated_bundle={"version": "sel-new", "elements": {}},
        publish_fn=lambda _bundle: calls.append("publish") or "published",
        enforce_fn=lambda _bundle: calls.append("enforce"),
    )
    assert ("publish" in calls) is publishes
    assert ("enforce" in calls) is enforces
    assert result["mode"] == mode
```

- [ ] **Step 3: Run all focused selector-probe tests**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_selector_probe_config.py tests/test_selector_probe_store.py tests/test_selector_probe_scheduler.py tests/test_selector_probe_session.py tests/test_selector_probe_state_runner.py tests/test_selector_probe_snapshot.py tests/test_selector_probe_contracts.py tests/test_selector_probe_candidates.py tests/test_selector_probe_model_client.py tests/test_selector_probe_repair.py tests/test_selector_probe_validator.py tests/test_selector_probe_registry.py tests/test_selector_probe_gates.py tests/test_selector_probe_runtime_gates.py tests/test_selector_probe_alerts.py tests/test_selector_probe_redaction.py tests/test_selector_probe_webhook.py tests/test_selector_probe_observe.py tests/test_selector_probe_routes.py -q -p no:cacheprovider -W error
node --test tests-js/selector-probe-ui.test.js
```

Expected: all tests pass.

- [ ] **Step 4: Run supported Python and Node suites**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
npm.cmd run test:node
```

Expected: zero failures.

- [ ] **Step 5: Compile changed Python modules**

```powershell
.\.venv\Scripts\python.exe -m py_compile selector_probe\__init__.py selector_probe\config.py selector_probe\store.py selector_probe\scheduler.py selector_probe\session.py selector_probe\state_runner.py selector_probe\snapshot.py selector_probe\contracts.py selector_probe\candidates.py selector_probe\model_client.py selector_probe\repair.py selector_probe\validator.py selector_probe\registry.py selector_probe\gates.py selector_probe\alerts.py selector_probe\redaction.py selector_probe\webhook.py selector_probe\probe.py selector_probe\worker.py selector_probe\blueprint.py browser_strategy_runtime.py gateway\app.py launcher.py
```

Expected: exit code 0.

- [ ] **Step 6: Perform observe-mode live acceptance**

Use only configured dedicated test profiles. Verify:

- two profiles start;
- existing windows are not closed;
- comment panel opens and closes read-only;
- no typing, submit, like, follow, publish, or account mutation occurs;
- two fresh rounds produce sanitized evidence;
- probe-owned windows close;
- alert screenshot is redacted;
- no full profile ID appears in API or JSONL logs.

Do not enable publish or enforce during this step.

- [ ] **Step 7: Perform publish-mode acceptance**

After seven consecutive successful observe runs:

- switch to `publish`;
- run two-profile/two-round validation;
- verify one complete Active Bundle;
- force Redis publication interruption;
- verify no partial Active Bundle;
- restart worker;
- verify outbox reconciliation;
- confirm no gates change.

- [ ] **Step 8: Perform enforce-mode isolation acceptance**

After publish-mode fault tests pass:

- switch to `enforce`;
- simulate one failed alias;
- verify only dependent strategies pause;
- verify unrelated strategy executes;
- add a manual pause;
- publish a recovered selector bundle;
- verify probe reason clears and manual reason remains;
- verify queued unstarted run delays;
- verify partial run never resumes mid-sequence.

- [ ] **Step 9: Write verification report**

Record:

- exact commands and pass counts;
- rollout mode used for each live step;
- masked profile IDs;
- Active Bundle version and hash;
- two-profile/two-round evidence;
- publication interruption and reconciliation evidence;
- gate isolation evidence;
- manual-pause precedence;
- webhook and screenshot-redaction evidence;
- window ownership cleanup;
- any unverified boundary;
- `Commits: none` while Git remains uninitialized.

## Final completion

Feature is complete when:

- all three rollout modes honor boundaries;
- two dedicated profiles pass two fresh rounds;
- LLM cannot bypass validator;
- Redis publication is atomic and recoverable;
- selector failures pause only dependent strategies;
- Redis outage fails closed only for managed strategies;
- automatic recovery preserves manual pause;
- partially executed strategies never auto-resume;
- alerts deduplicate and webhooks retry safely;
- screenshots expire after seven days;
- dashboard controls and history work;
- full Python and Node suites pass;
- live acceptance causes no external side effect.
