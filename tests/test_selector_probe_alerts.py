from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from selector_probe.alerts import AlertService
from selector_probe.store import SelectorProbeStore


@pytest.fixture
def alert_service(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        yield AlertService(
            store,
            profile_ids=("profile-complete-secret",),
            evidence_root=tmp_path,
        )


def _open(service: AlertService, *, failure_class="selector_validation_failed"):
    return service.open_or_update(
        site="tiktok",
        failure_class=failure_class,
        aliases=("评论入口",),
        active_version="sel-old",
        details={
            "retry_count": 3,
            "profile_id": "profile-complete-secret",
        },
    )


def test_repeated_failure_updates_one_open_alert(alert_service):
    first = _open(alert_service)
    second = _open(alert_service)

    assert second["id"] == first["id"]
    assert second["occurrence_count"] == 2
    assert second["status"] == "open"
    assert "profile-complete-secret" not in str(second)


def test_acknowledged_alert_stays_deduplicated_until_resolved(alert_service):
    first = _open(alert_service)
    acknowledged = alert_service.acknowledge(first["id"])
    repeated = _open(alert_service)
    resolved = alert_service.resolve(first["id"])
    reopened = _open(alert_service)

    assert acknowledged["status"] == "acknowledged"
    assert repeated["id"] == first["id"]
    assert repeated["status"] == "acknowledged"
    assert resolved["status"] == "resolved"
    assert reopened["id"] != first["id"]


def test_cleanup_removes_only_expired_alert_screenshots(
    tmp_path,
    alert_service,
):
    first = _open(alert_service)
    second = _open(
        alert_service,
        failure_class="selector_probe_unavailable",
    )
    old = tmp_path / "old.jpg"
    recent = tmp_path / "recent.jpg"
    old.write_bytes(b"old")
    recent.write_bytes(b"recent")
    alert_service.record_screenshot(
        alert_id=first["id"],
        path=old,
        created_at="2026-07-20T00:00:00Z",
    )
    alert_service.record_screenshot(
        alert_id=second["id"],
        path=recent,
        created_at="2026-07-27T00:00:00Z",
    )

    deleted = alert_service.cleanup_screenshots(
        now="2026-07-28T00:00:00Z",
        retention_days=7,
    )

    assert deleted == 1
    assert old.exists() is False
    assert recent.exists() is True


def test_alert_outbox_payload_never_contains_local_screenshot_path(
    tmp_path,
    alert_service,
):
    opened = _open(alert_service)
    screenshot = tmp_path / "evidence.jpg"
    screenshot.write_bytes(b"safe")
    alert_service.record_screenshot(
        alert_id=opened["id"],
        path=screenshot,
        created_at="2026-07-28T00:00:00Z",
    )

    rows = alert_service.store.connection.execute(
        "SELECT payload_json FROM webhook_outbox ORDER BY id"
    ).fetchall()

    assert rows
    assert str(screenshot) not in "".join(row["payload_json"] for row in rows)


def test_same_failure_is_isolated_by_environment_and_notifies_once(
    alert_service,
):
    production = _open(alert_service)
    repeated = _open(alert_service)
    staging = alert_service.open_or_update(
        site="tiktok",
        environment="staging",
        failure_class="selector_validation_failed",
        aliases=("评论入口",),
        active_version="sel-old",
        details={"retry_count": 3},
    )
    rows = alert_service.store.connection.execute(
        "SELECT event_type, payload_json FROM webhook_outbox ORDER BY id"
    ).fetchall()

    assert repeated["id"] == production["id"]
    assert staging["id"] != production["id"]
    assert len(rows) == 2
    assert '"environment":"production"' in rows[0]["payload_json"]
    assert '"environment":"staging"' in rows[1]["payload_json"]


def test_acknowledge_webhook_keeps_persisted_site_context(alert_service):
    opened = _open(alert_service)

    alert_service.acknowledge(opened["id"])
    payload = alert_service.store.connection.execute(
        """
        SELECT payload_json
        FROM webhook_outbox
        WHERE event_type = 'alert_acknowledged'
        """
    ).fetchone()["payload_json"]

    assert '"site":"tiktok"' in payload
    assert '"environment":"production"' in payload


def test_screenshot_outside_fixed_evidence_root_is_rejected(
    tmp_path,
    alert_service,
):
    outside = tmp_path.parent / "outside-alert.jpg"
    outside.write_bytes(b"outside")
    opened = _open(alert_service)

    with pytest.raises(ValueError, match="escapes"):
        alert_service.record_screenshot(
            alert_id=opened["id"],
            path=outside,
        )


def test_legacy_alert_lifecycle_backfills_unknown_scope(tmp_path):
    database = tmp_path / "legacy.db"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE probe_alerts (
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
        )
        """
    )
    connection.execute(
        """
        INSERT INTO probe_alerts (
            fingerprint,
            status,
            failure_class,
            aliases_json,
            strategy_ids_json,
            active_version,
            first_seen_at,
            last_seen_at,
            occurrence_count,
            details_json
        ) VALUES (
            'legacy-fingerprint',
            'open',
            'selector_validation_failed',
            '[]',
            '[]',
            'sel-old',
            '2026-07-20T00:00:00+00:00',
            '2026-07-20T00:00:00+00:00',
            1,
            '{}'
        )
        """
    )
    connection.commit()
    connection.close()

    with SelectorProbeStore(database) as store:
        service = AlertService(store, evidence_root=tmp_path)
        service.acknowledge(1, now="2026-07-28T00:00:00Z")
        service.resolve(1, now="2026-07-28T00:01:00Z")
        payloads = store.connection.execute(
            """
            SELECT payload_json
            FROM webhook_outbox
            WHERE event_type IN ('alert_acknowledged', 'alert_resolved')
            ORDER BY id
            """
        ).fetchall()

    assert len(payloads) == 2
    assert all('"site":"unknown"' in row["payload_json"] for row in payloads)
    assert all(
        '"environment":"unknown"' in row["payload_json"]
        for row in payloads
    )
