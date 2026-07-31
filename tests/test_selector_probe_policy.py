from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from browser_element_schema import normalize_element_definitions
from selector_probe import worker
from selector_probe.store import SelectorProbeStore


def _finish_with_effect(
    store: SelectorProbeStore,
    *,
    scheduled_for: str,
    token: str,
    status: str,
    policy_outcome: str,
    occurred_at: str,
    effect_type: str,
    effect_payload: dict[str, object],
    site: str = "tiktok",
    environment: str = "production",
) -> int:
    run_id = store.start_run(
        scheduled_for=scheduled_for,
        active_version_before="sel-old",
        attempt_token=token,
    )
    store.finish_run(
        run_id,
        status=status,
        details={"status": status},
        failed_aliases=effect_payload.get("aliases", ()),
        attempt_token=token,
        policy={
            "site": site,
            "environment": environment,
            "outcome": policy_outcome,
            "occurred_at": occurred_at,
        },
        effect={
            "key": f"probe-run:{run_id}:{effect_type}",
            "type": effect_type,
            "payload": effect_payload,
        },
    )
    return run_id


def test_infrastructure_failure_state_survives_slots_and_resets_on_validation(
    tmp_path,
):
    path = tmp_path / "probe.db"
    with SelectorProbeStore(path) as store:
        _finish_with_effect(
            store,
            scheduled_for="2026-07-29T03:00:00+08:00",
            token="owner-1",
            status="infrastructure_unavailable",
            policy_outcome="infrastructure",
            occurred_at="2026-07-28T19:00:00Z",
            effect_type="probe_stale",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "active_version": "sel-old",
                "failure_started_at": "2026-07-28T19:00:00Z",
            },
        )
        first = store.probe_health_state(
            site="tiktok",
            environment="production",
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-30T03:00:00+08:00",
            token="owner-2",
            status="infrastructure_unavailable",
            policy_outcome="infrastructure",
            occurred_at="2026-07-29T19:00:00Z",
            effect_type="probe_stale",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "active_version": "sel-old",
                "failure_started_at": "2026-07-28T19:00:00Z",
            },
        )
        second = store.probe_health_state(
            site="tiktok",
            environment="production",
        )

        run_id = store.start_run(
            scheduled_for="2026-07-31T03:00:00+08:00",
            active_version_before="sel-old",
            attempt_token="owner-3",
        )
        store.finish_run(
            run_id,
            status="completed",
            details={"status": "healthy"},
            attempt_token="owner-3",
            policy={
                "site": "tiktok",
                "environment": "production",
                "outcome": "validated",
                "occurred_at": "2026-07-30T19:00:00Z",
            },
        )
        recovered = store.probe_health_state(
            site="tiktok",
            environment="production",
        )

    assert first["failure_started_at"] == "2026-07-28T19:00:00+00:00"
    assert first["retry_count"] == 1
    assert datetime.fromisoformat(first["next_retry_at"]) == datetime.fromisoformat(
        "2026-07-28T19:15:00+00:00"
    )
    assert second["failure_started_at"] == first["failure_started_at"]
    assert second["retry_count"] == 2
    assert datetime.fromisoformat(second["next_retry_at"]) == datetime.fromisoformat(
        "2026-07-29T19:30:00+00:00"
    )
    assert recovered == {
        "site": "tiktok",
        "environment": "production",
        "failure_started_at": "",
        "retry_count": 0,
        "next_retry_at": "",
        "last_validated_at": "2026-07-30T19:00:00+00:00",
    }


def test_stale_baseline_prefers_last_validation_over_later_first_failure():
    now = datetime.fromisoformat("2026-07-30T11:00:00+00:00")
    baseline = worker._failure_started_at(
        {
            "last_validated_at": "2026-07-28T19:00:00+00:00",
            "failure_started_at": "2026-07-29T19:00:00+00:00",
        },
        now,
    )

    assert baseline == datetime.fromisoformat("2026-07-28T19:00:00+00:00")
    assert (now - baseline).total_seconds() >= worker.PROBE_STALE_SECONDS


def test_probe_effects_and_recovery_are_isolated_by_site_environment(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.replace_strategy_dependencies(
            (("comment-entry", "comment-flow", "open", "click"),)
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-29T03:00:00+08:00",
            token="prod-owner",
            status="selector_validation_failed",
            policy_outcome="selector_failure",
            occurred_at="2026-07-28T19:00:00Z",
            effect_type="selector_failure",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "aliases": ["comment-entry"],
                "active_version": "sel-prod",
                "failure_code": "zero_match",
                "match_count": 0,
                "required_state": "feed_ready",
                "screenshot_path": "",
            },
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-29T04:00:00+08:00",
            token="staging-owner",
            status="selector_validation_failed",
            policy_outcome="selector_failure",
            occurred_at="2026-07-28T20:00:00Z",
            effect_type="selector_failure",
            effect_payload={
                "site": "tiktok",
                "environment": "staging",
                "aliases": ["comment-entry"],
                "active_version": "sel-staging",
                "failure_code": "zero_match",
                "match_count": 0,
                "required_state": "feed_ready",
                "screenshot_path": "",
            },
            environment="staging",
        )

        prod = store.pending_probe_effects(
            site="tiktok",
            environment="production",
        )
        staging = store.pending_probe_effects(
            site="tiktok",
            environment="staging",
        )
        assert len(prod) == len(staging) == 1
        assert prod[0]["id"] != staging[0]["id"]

        store.apply_probe_effect(
            prod[0]["id"],
            site="tiktok",
            environment="production",
        )
        store.complete_probe_effect(
            prod[0]["id"],
            site="tiktok",
            environment="production",
        )
        store.apply_probe_effect(
            staging[0]["id"],
            site="tiktok",
            environment="staging",
        )
        store.complete_probe_effect(
            staging[0]["id"],
            site="tiktok",
            environment="staging",
        )
        reasons = store.connection.execute(
            """
            SELECT site, environment, selector_version_id
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            ORDER BY environment
            """
        ).fetchall()

        _finish_with_effect(
            store,
            scheduled_for="2026-07-30T03:00:00+08:00",
            token="prod-recovery",
            status="completed",
            policy_outcome="validated",
            occurred_at="2026-07-29T19:00:00Z",
            effect_type="recovery",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "selector_version_id": "sel-prod-new",
                "bundle_hash": "sha256:" + "a" * 64,
                "covered_aliases": ["comment-entry"],
            },
        )
        recovery = store.pending_probe_effects(
            site="tiktok",
            environment="production",
        )[0]
        store.apply_probe_effect(
            recovery["id"],
            site="tiktok",
            environment="production",
        )
        remaining = store.connection.execute(
            """
            SELECT environment
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            ORDER BY environment
            """
        ).fetchall()

        assert [tuple(row) for row in reasons] == [
            ("tiktok", "production", "sel-prod"),
            ("tiktok", "staging", "sel-staging"),
        ]
        assert [row["environment"] for row in remaining] == ["staging"]
        assert store.recovery_pending(
            site="tiktok",
            environment="production",
        ) is False
        assert store.recovery_pending(
            site="tiktok",
            environment="staging",
        ) is True


def test_selector_failure_effect_is_atomic_idempotent_and_alias_scoped(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.replace_strategy_dependencies(
            (
                ("comment-entry", "comment-flow", "open", "click"),
                ("reader-entry", "reader-flow", "open", "click"),
            )
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-29T03:00:00+08:00",
            token="owner",
            status="selector_validation_failed",
            policy_outcome="selector_failure",
            occurred_at="2026-07-28T19:00:00Z",
            effect_type="selector_failure",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "aliases": ["comment-entry"],
                "active_version": "sel-old",
                "failure_code": "multiple_match",
                "match_count": 2,
                "required_state": "feed_ready",
                "screenshot_path": "",
            },
        )
        effect = store.pending_probe_effects()[0]

        first = store.apply_probe_effect(effect["id"])
        replay = store.apply_probe_effect(effect["id"])

        reasons = store.connection.execute(
            """
            SELECT strategy_id, source, reason_code, aliases_json
            FROM strategy_gate_reasons
            WHERE cleared_at IS NULL
            ORDER BY strategy_id
            """
        ).fetchall()
        alerts = store.connection.execute(
            "SELECT id, occurrence_count, status FROM probe_alerts"
        ).fetchall()
        webhook_events = store.connection.execute(
            "SELECT event_type FROM webhook_outbox ORDER BY id"
        ).fetchall()

    assert first == replay
    assert first["strategy_ids"] == ["comment-flow"]
    assert [tuple(row) for row in reasons] == [
        (
            "comment-flow",
            "probe",
            "selector_validation_failed",
            '["comment-entry"]',
        )
    ]
    assert [tuple(row) for row in alerts] == [(first["alert_id"], 1, "open")]
    assert [row["event_type"] for row in webhook_events] == ["alert_opened"]


def test_recovery_effect_keeps_manual_pause_and_queues_one_recovery_webhook(
    tmp_path,
):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.replace_strategy_dependencies(
            (("comment-entry", "comment-flow", "open", "click"),)
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-29T03:00:00+08:00",
            token="owner-1",
            status="selector_validation_failed",
            policy_outcome="selector_failure",
            occurred_at="2026-07-28T19:00:00Z",
            effect_type="selector_failure",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "aliases": ["comment-entry"],
                "active_version": "sel-old",
                "failure_code": "zero_match",
                "match_count": 0,
                "required_state": "feed_ready",
                "screenshot_path": "",
            },
        )
        failed = store.pending_probe_effects()[0]
        store.apply_probe_effect(failed["id"])
        store.complete_probe_effect(failed["id"])
        store.upsert_gate_reasons(
            (
                {
                    "strategy_id": "comment-flow",
                    "source": "manual",
                    "reason_code": "operator_pause",
                    "aliases": [],
                    "selector_version_id": "",
                    "created_by": "admin",
                },
            )
        )
        infrastructure_alert = store.open_or_update_alert(
            fingerprint="infra-current",
            failure_class="probe_unavailable",
            aliases=(),
            strategy_ids=("comment-flow",),
            active_version="sel-old",
            details={"failure_code": "registry_unavailable"},
            site="tiktok",
            environment="production",
            now="2026-07-28T20:00:00Z",
        )
        foreign_alert = store.open_or_update_alert(
            fingerprint="selector-foreign",
            failure_class="selector_validation_failed",
            aliases=("comment-entry",),
            strategy_ids=("comment-flow",),
            active_version="sel-old",
            details={"failure_code": "zero_match"},
            site="tiktok",
            environment="staging",
            now="2026-07-28T20:00:00Z",
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-30T03:00:00+08:00",
            token="owner-2",
            status="completed",
            policy_outcome="validated",
            occurred_at="2026-07-29T19:00:00Z",
            effect_type="recovery",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "selector_version_id": "sel-new",
                "bundle_hash": "sha256:" + "a" * 64,
                "covered_aliases": ["comment-entry"],
            },
        )
        recovery = store.pending_probe_effects()[0]

        first = store.apply_probe_effect(recovery["id"])
        replay = store.apply_probe_effect(recovery["id"])

        reasons = store.connection.execute(
            """
            SELECT source, reason_code
            FROM strategy_gate_reasons
            WHERE strategy_id = 'comment-flow' AND cleared_at IS NULL
            ORDER BY source
            """
        ).fetchall()
        alert_statuses = store.connection.execute(
            "SELECT id, status FROM probe_alerts ORDER BY id"
        ).fetchall()
        recovery_webhooks = store.connection.execute(
            """
            SELECT event_type
            FROM webhook_outbox
            WHERE event_type = 'alert_recovered'
            """
        ).fetchall()

    assert first == replay
    assert first["strategy_ids"] == ["comment-flow"]
    assert [tuple(row) for row in reasons] == [("manual", "operator_pause")]
    statuses = {row["id"]: row["status"] for row in alert_statuses}
    assert statuses[infrastructure_alert["id"]] == "resolved"
    assert statuses[foreign_alert["id"]] == "open"
    assert list(statuses.values()).count("resolved") == 2
    assert len(recovery_webhooks) == 1


def test_foreign_scope_alert_does_not_trigger_current_scope_recovery(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.open_or_update_alert(
            fingerprint="foreign-only",
            failure_class="probe_unavailable",
            aliases=(),
            strategy_ids=(),
            active_version="sel-old",
            details={"failure_code": "registry_unavailable"},
            site="tiktok",
            environment="staging",
            now="2026-07-28T20:00:00Z",
        )

        assert store.recovery_pending(
            site="tiktok",
            environment="production",
        ) is False


def test_probe_stale_effect_pauses_only_current_managed_strategies(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        store.replace_strategy_dependencies(
            (
                ("comment-entry", "comment-flow", "open", "click"),
                ("reader-entry", "reader-flow", "open", "click"),
            )
        )
        _finish_with_effect(
            store,
            scheduled_for="2026-07-29T03:00:00+08:00",
            token="owner",
            status="infrastructure_unavailable",
            policy_outcome="infrastructure",
            occurred_at="2026-07-30T07:01:00Z",
            effect_type="probe_stale",
            effect_payload={
                "site": "tiktok",
                "environment": "production",
                "active_version": "sel-old",
                "failure_started_at": "2026-07-28T19:00:00Z",
            },
        )
        effect = store.pending_probe_effects()[0]
        result = store.apply_probe_effect(effect["id"])
        reasons = store.connection.execute(
            """
            SELECT strategy_id, reason_code, aliases_json
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            ORDER BY strategy_id
            """
        ).fetchall()

    assert result["strategy_ids"] == ["comment-flow", "reader-flow"]
    assert [tuple(row) for row in reasons] == [
        ("comment-flow", "probe_validation_stale", '["comment-entry"]'),
        ("reader-flow", "probe_validation_stale", '["reader-entry"]'),
    ]


class PolicyRedis:
    def __init__(self):
        self.data = {}

    def eval(self, _script, key_count, *values):
        if key_count != 1:
            raise AssertionError("policy test Redis only supports gate projection")
        key = values[0]
        self.data[key] = str(values[2]).encode()
        return b"published"

    def get(self, key):
        return self.data.get(key)

    def close(self):
        return None


class PolicyRegistry:
    def __init__(self, active):
        self.active = active

    def get_active(self):
        return self.active


class PolicyLease:
    def acquire(self):
        return True

    def renew(self):
        return True

    def release(self):
        return True


def _settings():
    return {
        "selector_probe": {
            "enabled": True,
            "site": "tiktok",
            "environment": "production",
            "timezone": "Asia/Shanghai",
            "daily_time": "03:00",
            "target_url": "https://www.tiktok.com/",
            "test_profile_ids": ["profile-a", "profile-b"],
            "model_id": "",
            "observe_only": False,
            "webhook": {
                "enabled": False,
                "type": "generic",
                "url": "",
                "signing_secret": "",
            },
        },
        "browser": {
            "action_elements": {
                "comment-entry": "//button[@data-e2e='comment-icon']"
            }
        },
        "adspower": {
            "base_url": "http://local.adspower.net:50325",
            "api_key": "secret",
        },
    }


def _clock():
    return SimpleNamespace(
        now=lambda: datetime(
            2026,
            7,
            29,
            3,
            0,
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
    )


def _seeded_store_factory(path):
    store = SelectorProbeStore(path)
    if not store.managed_strategy_ids():
        store.replace_strategy_dependencies(
            (
                ("comment-entry", "comment-flow", "open", "click"),
                ("reader-entry", "reader-flow", "open", "click"),
            )
        )
    return store


def test_first_probe_unavailable_opens_deduplicated_alert_without_gate(
    tmp_path,
):
    database = tmp_path / "probe.db"
    owners = iter(("infra-owner-1", "infra-owner-2"))

    class Runtime:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def run_once():
        return worker.run_tick(
            settings_loader=_settings,
            store_factory=_seeded_store_factory,
            redis_factory=lambda _url: PolicyRedis(),
            registry_factory=lambda *_args, **_kwargs: PolicyRegistry(
                {"version": "sel-old"}
            ),
            reconcile_runner=lambda *_args: {},
            adspower_factory=lambda **_kwargs: object(),
            healing_runtime_factory=lambda **_kwargs: Runtime(),
            healing_runner=lambda _runtime: {
                "status": "infrastructure_unavailable",
                "failure_code": "probe_network_error",
            },
            lease_factory=lambda *_args, **_kwargs: PolicyLease(),
            owner_id_factory=lambda: next(owners),
            db_path=database,
            evidence_root=tmp_path / "evidence",
            clock=_clock(),
            force=True,
        )

    assert run_once()["status"] == "infrastructure_unavailable"
    assert run_once()["status"] == "infrastructure_unavailable"
    with SelectorProbeStore(database) as store:
        alert = store.connection.execute(
            """
            SELECT failure_class, occurrence_count, status
            FROM probe_alerts
            """
        ).fetchone()
        webhooks = store.connection.execute(
            "SELECT event_type FROM webhook_outbox ORDER BY id"
        ).fetchall()
        probe_reasons = store.connection.execute(
            """
            SELECT id
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            """
        ).fetchall()

    assert tuple(alert) == ("probe_unavailable", 2, "open")
    assert [row["event_type"] for row in webhooks] == ["alert_opened"]
    assert probe_reasons == []


def test_third_selector_failure_captures_before_cleanup_and_links_alert(
    tmp_path,
):
    events = []
    database = tmp_path / "probe.db"
    evidence_root = tmp_path / "evidence"

    class Runtime:
        def __enter__(self):
            events.append("runtime:enter")
            return self

        def capture_failure_screenshot(
            self,
            *,
            failed_aliases,
            target_path,
            evidence_root,
        ):
            assert failed_aliases == ("comment-entry",)
            selected = Path(target_path)
            assert selected.parent == Path(evidence_root)
            selected.parent.mkdir(parents=True, exist_ok=True)
            selected.write_bytes(b"redacted-jpeg")
            events.append("screenshot")
            return selected

        def __exit__(self, *_args):
            events.append("runtime:close")

    result = worker.run_tick(
        settings_loader=_settings,
        store_factory=_seeded_store_factory,
        redis_factory=lambda _url: PolicyRedis(),
        registry_factory=lambda *_args, **_kwargs: PolicyRegistry(
            {"version": "sel-old"}
        ),
        reconcile_runner=lambda *_args: {},
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: Runtime(),
        healing_runner=lambda _runtime: {
            "status": "selector_validation_failed",
            "proposed_pause_aliases": ["comment-entry"],
            "failure_code": "zero_match",
            "match_count": 0,
            "required_state": "feed_ready",
        },
        lease_factory=lambda *_args, **_kwargs: PolicyLease(),
        db_path=database,
        evidence_root=evidence_root,
        clock=_clock(),
    )
    with SelectorProbeStore(database) as store:
        alert = store.connection.execute(
            "SELECT screenshot_path, status FROM probe_alerts"
        ).fetchone()
        reasons = store.connection.execute(
            """
            SELECT strategy_id
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            """
        ).fetchall()

    assert result["status"] == "selector_validation_failed"
    assert result["paused_strategies"] == ["comment-flow"]
    assert events == ["runtime:enter", "screenshot", "runtime:close"]
    assert alert["status"] == "open"
    assert Path(alert["screenshot_path"]).parent == evidence_root
    assert [row["strategy_id"] for row in reasons] == ["comment-flow"]


def test_screenshot_failure_does_not_block_pause_alert_or_cleanup(tmp_path):
    events = []
    database = tmp_path / "probe.db"

    class Runtime:
        def __enter__(self):
            return self

        def capture_failure_screenshot(self, **_kwargs):
            events.append("screenshot:failed")
            raise RuntimeError("capture failed")

        def __exit__(self, *_args):
            events.append("runtime:close")

    result = worker.run_tick(
        settings_loader=_settings,
        store_factory=_seeded_store_factory,
        redis_factory=lambda _url: PolicyRedis(),
        registry_factory=lambda *_args, **_kwargs: PolicyRegistry(
            {"version": "sel-old"}
        ),
        reconcile_runner=lambda *_args: {},
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: Runtime(),
        healing_runner=lambda _runtime: {
            "status": "selector_validation_failed",
            "proposed_pause_aliases": ["comment-entry"],
            "failure_code": "zero_match",
        },
        lease_factory=lambda *_args, **_kwargs: PolicyLease(),
        db_path=database,
        evidence_root=tmp_path / "evidence",
        clock=_clock(),
    )
    with SelectorProbeStore(database) as store:
        alert = store.connection.execute(
            "SELECT screenshot_path FROM probe_alerts"
        ).fetchone()
        reason = store.connection.execute(
            """
            SELECT strategy_id
            FROM strategy_gate_reasons
            WHERE source = 'probe' AND cleared_at IS NULL
            """
        ).fetchone()

    assert result["paused_strategies"] == ["comment-flow"]
    assert alert["screenshot_path"] == ""
    assert reason["strategy_id"] == "comment-flow"
    assert events == ["screenshot:failed", "runtime:close"]


def _active_bundle():
    elements = normalize_element_definitions(
        {"comment-entry": "//button[@data-e2e='comment-icon']"}
    )
    bundle_hash = "sha256:" + hashlib.sha256(
        json.dumps(
            elements,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "version": "sel-old",
        "bundle_hash": bundle_hash,
        "elements": elements,
    }


def _evidence(bundle):
    candidate_id = bundle["elements"]["comment-entry"]["locators"][0]["id"]
    validations = []
    for profile in ("***p001", "***p002"):
        for round_number in (1, 2):
            suffix = f"{profile}-{round_number}".encode()
            digest = hashlib.sha256(suffix).hexdigest()
            validations.append(
                {
                    "profile_mask": profile,
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:" + digest,
                    "snapshot_hash": "sha256:" + hashlib.sha256(
                        b"snapshot-" + suffix
                    ).hexdigest(),
                    "page_generation": "sha256:" + hashlib.sha256(
                        b"generation-" + suffix
                    ).hexdigest(),
                    "aliases": {
                        "comment-entry": {
                            "status": "ok",
                            "candidate_id": candidate_id,
                        }
                    },
                }
            )
    return {
        "status": "passed",
        "bundle_hash": bundle["bundle_hash"],
        "profiles_passed": 2,
        "rounds_passed": 2,
        "validations": validations,
    }


def test_healthy_active_with_probe_gate_republishes_before_recovery(tmp_path):
    database = tmp_path / "probe.db"
    bundle = _active_bundle()
    registry = PolicyRegistry(bundle)

    class Runtime:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def store_factory(path):
        store = _seeded_store_factory(path)
        if not store.open_gate_reason_rows("comment-flow"):
            store.upsert_gate_reasons(
                (
                    {
                        "strategy_id": "comment-flow",
                        "source": "probe",
                        "site": "tiktok",
                        "environment": "production",
                        "reason_code": "selector_validation_failed",
                        "aliases": ["comment-entry"],
                        "selector_version_id": "sel-old",
                        "created_by": "selector-probe",
                    },
                    {
                        "strategy_id": "comment-flow",
                        "source": "manual",
                        "reason_code": "operator_pause",
                        "aliases": [],
                        "selector_version_id": "",
                        "created_by": "admin",
                    },
                )
            )
        return store

    def reconcile(store, selected_registry):
        event = store.claim_outbox_event(claim_token="publisher")
        if event is None:
            return {"acknowledged": 0, "version": selected_registry.active["version"]}
        selected_registry.active = event["bundle"]
        store.ack_outbox_event(
            event["outbox_id"],
            event["claim_token"],
            event["claim_generation"],
            outcome="published",
        )
        return {"acknowledged": 1, "version": event["version"]}

    result = worker.run_tick(
        settings_loader=_settings,
        store_factory=store_factory,
        redis_factory=lambda _url: PolicyRedis(),
        registry_factory=lambda *_args, **_kwargs: registry,
        reconcile_runner=reconcile,
        adspower_factory=lambda **_kwargs: object(),
        healing_runtime_factory=lambda **_kwargs: Runtime(),
        healing_runner=lambda _runtime: {
            "status": "healthy",
            "validation_evidence": _evidence(bundle),
        },
        lease_factory=lambda *_args, **_kwargs: PolicyLease(),
        db_path=database,
        evidence_root=tmp_path / "evidence",
        clock=_clock(),
    )
    with SelectorProbeStore(database) as store:
        reasons = store.open_gate_reason_rows("comment-flow")
        versions = store.connection.execute(
            """
            SELECT id, status
            FROM selector_versions
            ORDER BY created_at
            """
        ).fetchall()

    assert result["status"] == "published"
    assert result["new_version"] != "sel-old"
    assert registry.active["version"] == result["new_version"]
    assert [row["source"] for row in reasons] == ["manual"]
    assert [row["status"] for row in versions] == ["published"]
