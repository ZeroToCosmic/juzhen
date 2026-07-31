from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import socket

import pytest

from selector_probe.alerts import AlertService
from selector_probe.store import SelectorProbeStore
from selector_probe.webhook import RETRY_SECONDS, WebhookDispatcher


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


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
    dispatcher.send(
        {"alert_id": 1, "code": "selector_validation_failed"},
        timestamp=1000,
    )

    assert captured["headers"]["X-Selector-Probe-Timestamp"] == "1000"
    expected = hmac.new(
        b"secret",
        b"1000." + captured["data"],
        hashlib.sha256,
    ).hexdigest()
    assert captured["headers"]["X-Selector-Probe-Signature"] == expected
    assert captured["timeout"] == 10
    assert captured["allow_redirects"] is False
    assert captured["headers"]["Idempotency-Key"].startswith(
        "selector-probe-"
    )


@pytest.mark.parametrize(
    ("webhook_type", "expected_key"),
    [("slack", "blocks"), ("dingtalk", "markdown")],
)
def test_vendor_webhooks_map_sanitized_alert(webhook_type, expected_key):
    captured = {}

    def request_fn(url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(200)

    dispatcher = WebhookDispatcher(
        request_fn=request_fn,
        url="https://hooks.example.test/probe",
        signing_secret="secret",
        webhook_type=webhook_type,
    )
    dispatcher.send(
        {
            "alert_id": 1,
            "failure_class": "selector_validation_failed",
            "aliases": ["评论入口"],
            "screenshot_path": "C:/private/raw.jpg",
        },
        timestamp=1000,
    )
    payload = json.loads(captured["data"])

    assert expected_key in payload
    assert "C:/private/raw.jpg" not in str(payload)
    if webhook_type == "slack":
        assert payload["blocks"][0]["text"]["type"] == "plain_text"


def test_vendor_payload_neutralizes_mentions_and_markdown():
    captured = {}

    dispatcher = WebhookDispatcher(
        request_fn=lambda _url, **kwargs: (
            captured.update(kwargs) or FakeResponse(200)
        ),
        url="https://hooks.example.test/probe",
        signing_secret="secret",
        webhook_type="slack",
    )
    dispatcher.send(
        {
            "alert_id": 1,
            "failure_class": "@channel *urgent* <@U123>",
            "aliases": ["@everyone [click](https://evil.test)"],
        },
        timestamp=1000,
    )
    body = captured["data"].decode()

    assert "@channel" not in body
    assert "<@U123>" not in body
    assert '"type":"plain_text"' in body


def test_default_transport_rejects_private_dns_before_request():
    requested = False

    def resolver(_host, port, **_kwargs):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("10.0.0.8", port),
            )
        ]

    def request_fn(_url, **_kwargs):
        nonlocal requested
        requested = True
        return FakeResponse(200)

    dispatcher = WebhookDispatcher(
        request_fn=request_fn,
        resolver=resolver,
        enforce_resolution=True,
        url="https://hooks.example.test/probe",
        signing_secret="secret",
    )

    with pytest.raises(ValueError, match="public"):
        dispatcher.send({"alert_id": 1}, timestamp=1000)
    assert requested is False


def test_delivery_retries_then_marks_failed_without_removing_alert(tmp_path):
    now = datetime(2026, 7, 28, tzinfo=UTC)
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        alert = AlertService(store).open_or_update(
            site="tiktok",
            failure_class="selector_validation_failed",
            aliases=("评论入口",),
            active_version="sel-old",
            details={"retry_count": 3},
            now=now.isoformat(),
        )
        attempts = []

        def request_fn(_url, **_kwargs):
            attempts.append(_kwargs)
            return FakeResponse(503)

        dispatcher = WebhookDispatcher(
            store=store,
            request_fn=request_fn,
            url="https://hooks.example.test/probe",
            signing_secret="secret",
        )
        current = now
        for delay in RETRY_SECONDS:
            result = dispatcher.deliver_due(current)
            current += timedelta(seconds=delay)

        row = store.connection.execute(
            "SELECT status, attempt_count FROM webhook_outbox"
        ).fetchone()
        retained = store.connection.execute(
            "SELECT status FROM probe_alerts WHERE id = ?",
            (alert["id"],),
        ).fetchone()

    assert result["failed"] == 1
    assert row["status"] == "failed"
    assert row["attempt_count"] == 5
    assert len(attempts) == 5
    assert len(
        {
            attempt["headers"]["Idempotency-Key"]
            for attempt in attempts
        }
    ) == 1
    assert retained["status"] == "open"


def test_attempt_limit_is_hard_even_for_stale_pending_row(tmp_path):
    now = datetime(2026, 7, 28, tzinfo=UTC)
    requested = []
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        AlertService(store, evidence_root=tmp_path).open_or_update(
            site="tiktok",
            failure_class="selector_validation_failed",
            aliases=("评论入口",),
            active_version="sel-old",
            details={},
            now=now.isoformat(),
        )
        store.connection.execute(
            """
            UPDATE webhook_outbox
            SET attempt_count = 5, status = 'pending'
            """
        )
        store.connection.commit()
        dispatcher = WebhookDispatcher(
            store=store,
            request_fn=lambda *_args, **_kwargs: requested.append(True),
            url="https://hooks.example.test/probe",
            signing_secret="secret",
        )

        result = dispatcher.deliver_due(now)
        status = store.connection.execute(
            "SELECT status FROM webhook_outbox"
        ).fetchone()["status"]

    assert requested == []
    assert result["delivered"] == 0
    assert status == "failed"


def test_fenced_completion_is_not_reported_as_delivered():
    class Store:
        def __init__(self):
            self.claimed = False

        def claim_webhook_delivery(self, **_kwargs):
            if self.claimed:
                return None
            self.claimed = True
            return {
                "id": 1,
                "payload": {"alert_id": 1},
                "attempt_count": 1,
                "claim_token": "owner",
                "claim_generation": 1,
            }

        def complete_webhook_delivery(self, **_kwargs):
            return False

    dispatcher = WebhookDispatcher(
        store=Store(),
        request_fn=lambda *_args, **_kwargs: FakeResponse(200),
        url="https://hooks.example.test/probe",
        signing_secret="secret",
    )

    result = dispatcher.deliver_due(
        datetime(2026, 7, 28, tzinfo=UTC)
    )

    assert result["delivered"] == 0
    assert result["fenced"] == 1
