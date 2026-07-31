from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import ipaddress
import json
import socket
import re
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPSConnectionPool


RETRY_SECONDS = (60, 300, 1800, 7200, 21600)
_WEBHOOK_TYPES = {"generic", "slack", "dingtalk"}
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+\-.!|>])")


class WebhookDeliveryError(RuntimeError):
    def __init__(self, code: str, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class _PinnedHTTPSAdapter(HTTPAdapter):
    def __init__(self, pinned_ip: str, hostname: str, port: int) -> None:
        super().__init__(max_retries=0)
        self.hostname = hostname
        self.port = port
        self.pool = HTTPSConnectionPool(
            host=pinned_ip,
            port=port,
            maxsize=1,
            block=True,
            assert_hostname=hostname,
            server_hostname=hostname,
        )

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        del request, verify, cert
        if proxies:
            raise requests.exceptions.ProxyError("proxies are disabled")
        return self.pool

    def add_headers(self, request, **kwargs):
        super().add_headers(request, **kwargs)
        host = f"[{self.hostname}]" if ":" in self.hostname else self.hostname
        request.headers["Host"] = (
            host if self.port == 443 else f"{host}:{self.port}"
        )

    def close(self) -> None:
        self.pool.close()
        super().close()


def _safe_url(value: object) -> tuple[str, str, int]:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise ValueError("webhook URL is invalid")
    try:
        parsed = urlsplit(value)
        port = parsed.port or 443
    except ValueError as error:
        raise ValueError("webhook URL is invalid") from error
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("webhook URL must be HTTPS without credentials")
    try:
        normalized = hostname.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as error:
        raise ValueError("webhook URL is invalid") from error
    try:
        literal = ipaddress.ip_address(normalized)
    except ValueError:
        literal = None
    if literal is not None and not _is_public(literal):
        raise ValueError("webhook URL must resolve to public addresses")
    return value, normalized, port


def _is_public(value: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        value.is_global
        and not value.is_private
        and not value.is_loopback
        and not value.is_link_local
        and not value.is_reserved
        and not value.is_multicast
        and not value.is_unspecified
    )


def _resolve_public(
    hostname: str,
    port: int,
    resolver: Callable,
) -> str:
    try:
        records = resolver(hostname, port, type=socket.SOCK_STREAM)
    except Exception as error:
        raise WebhookDeliveryError("webhook_network_error") from error
    if not isinstance(records, (list, tuple)) or not records:
        raise WebhookDeliveryError("webhook_network_error")
    pinned = ""
    for record in records:
        try:
            address = ipaddress.ip_address(record[4][0])
        except (IndexError, TypeError, ValueError) as error:
            raise WebhookDeliveryError("webhook_network_error") from error
        if not _is_public(address):
            raise ValueError("webhook URL must resolve to public addresses")
        pinned = pinned or str(address)
    return pinned


def _external_payload(value: object) -> object:
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            normalized = key.casefold().replace("-", "_")
            if normalized == "path" or normalized.endswith("_path"):
                continue
            result[key[:128]] = _external_payload(item)
        return result
    if isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        return [_external_payload(item) for item in value[:100]]
    if isinstance(value, str):
        return value[:1000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _vendor_payload(
    payload: Mapping[str, object],
    webhook_type: str,
) -> dict[str, object]:
    safe = _external_payload(payload)
    assert isinstance(safe, dict)
    if webhook_type == "generic":
        return safe
    alert_id = safe.get("alert_id", "")
    failure = safe.get("failure_class") or safe.get("code") or "probe_alert"
    aliases = safe.get("aliases")
    alias_text = ", ".join(str(item) for item in aliases) if isinstance(
        aliases,
        list,
    ) else ""
    summary = f"Selector probe alert #{alert_id}: {failure}"
    if alias_text:
        summary += f" ({alias_text})"
    neutral = (
        summary.replace("@", "＠")
        .replace("<", "‹")
        .replace(">", "›")
        .replace("&", "＆")
    )
    if webhook_type == "slack":
        return {
            "text": neutral,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "plain_text", "text": neutral},
                }
            ],
        }
    markdown = _MARKDOWN.sub(r"\\\1", neutral)
    return {
        "msgtype": "markdown",
        "markdown": {
            "title": "Selector probe alert",
            "text": f"### Selector probe alert\n\n{markdown}",
        },
    }


class WebhookDispatcher:
    def __init__(
        self,
        *,
        url: str,
        signing_secret: str,
        store: object | None = None,
        webhook_type: str = "generic",
        request_fn: Callable | None = None,
        resolver: Callable = socket.getaddrinfo,
        enforce_resolution: bool | None = None,
    ) -> None:
        self.url, self.hostname, self.port = _safe_url(url)
        if webhook_type not in _WEBHOOK_TYPES:
            raise ValueError("unsupported webhook type")
        if not isinstance(signing_secret, str):
            raise ValueError("webhook signing secret must be a string")
        self.signing_secret = signing_secret
        self.store = store
        self.webhook_type = webhook_type
        self.request_fn = request_fn
        self.resolver = resolver
        self.enforce_resolution = (
            request_fn is None
            if enforce_resolution is None
            else enforce_resolution
        )

    def _request(self, **kwargs):
        pinned_ip = ""
        if self.enforce_resolution:
            pinned_ip = _resolve_public(
                self.hostname,
                self.port,
                self.resolver,
            )
        if self.request_fn is not None:
            return self.request_fn(self.url, **kwargs)
        session = requests.Session()
        session.trust_env = False
        adapter = _PinnedHTTPSAdapter(
            pinned_ip,
            self.hostname,
            self.port,
        )
        session.mount("https://", adapter)
        try:
            return session.post(self.url, **kwargs)
        finally:
            session.close()

    def send(
        self,
        payload: Mapping[str, object],
        *,
        timestamp: int | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("webhook payload must be an object")
        selected_timestamp = (
            int(datetime.now(UTC).timestamp())
            if timestamp is None
            else timestamp
        )
        if (
            isinstance(selected_timestamp, bool)
            or not isinstance(selected_timestamp, int)
            or selected_timestamp < 0
        ):
            raise ValueError("webhook timestamp must be a positive integer")
        body = json.dumps(
            _vendor_payload(dict(payload), self.webhook_type),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        timestamp_text = str(selected_timestamp)
        signature = hmac.new(
            self.signing_secret.encode(),
            timestamp_text.encode() + b"." + body,
            hashlib.sha256,
        ).hexdigest()
        selected_key = idempotency_key or (
            "selector-probe-"
            + hashlib.sha256(body).hexdigest()
        )
        if (
            not isinstance(selected_key, str)
            or not selected_key
            or len(selected_key) > 200
        ):
            raise ValueError("webhook idempotency key is invalid")
        try:
            response = self._request(
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Selector-Probe-Timestamp": timestamp_text,
                    "X-Selector-Probe-Signature": signature,
                    "Idempotency-Key": selected_key,
                },
                timeout=10,
                allow_redirects=False,
            )
        except (ValueError, WebhookDeliveryError):
            raise
        except Exception as error:
            raise WebhookDeliveryError("webhook_network_error") from error
        status = getattr(response, "status_code", None)
        if not isinstance(status, int) or not 200 <= status < 300:
            raise WebhookDeliveryError("webhook_http_error", status)

    def deliver_due(self, now: datetime | str) -> dict[str, int]:
        if self.store is None:
            raise ValueError("webhook delivery store is required")
        if isinstance(now, str):
            try:
                selected_now = datetime.fromisoformat(
                    now.replace("Z", "+00:00")
                )
            except ValueError as error:
                raise ValueError("now must be an ISO-8601 datetime") from error
        else:
            selected_now = now
        if (
            not isinstance(selected_now, datetime)
            or selected_now.tzinfo is None
            or selected_now.utcoffset() is None
        ):
            raise ValueError("now must be timezone-aware")
        selected_now = selected_now.astimezone(UTC)
        result = {
            "delivered": 0,
            "retried": 0,
            "failed": 0,
            "fenced": 0,
        }
        for _ in range(100):
            delivery = self.store.claim_webhook_delivery(
                now=selected_now.isoformat(),
            )
            if delivery is None:
                break
            try:
                self.send(
                    delivery["payload"],
                    timestamp=int(selected_now.timestamp()),
                    idempotency_key=(
                        f"selector-probe-outbox-{delivery['id']}"
                    ),
                )
            except Exception as error:
                attempt = int(delivery["attempt_count"])
                retry = attempt < len(RETRY_SECONDS)
                next_attempt = (
                    selected_now
                    + timedelta(seconds=RETRY_SECONDS[attempt - 1])
                    if retry
                    else None
                )
                code = getattr(error, "code", "webhook_delivery_failed")
                updated = self.store.fail_webhook_delivery(
                    outbox_id=delivery["id"],
                    claim_token=delivery["claim_token"],
                    claim_generation=delivery["claim_generation"],
                    error_code=code,
                    next_attempt_at=(
                        next_attempt.isoformat()
                        if next_attempt is not None
                        else None
                    ),
                    failed_at=selected_now.isoformat(),
                )
                if updated:
                    result["retried" if retry else "failed"] += 1
                else:
                    result["fenced"] += 1
                continue
            updated = self.store.complete_webhook_delivery(
                outbox_id=delivery["id"],
                claim_token=delivery["claim_token"],
                claim_generation=delivery["claim_generation"],
                completed_at=selected_now.isoformat(),
            )
            if updated:
                result["delivered"] += 1
            else:
                result["fenced"] += 1
        return result


__all__ = [
    "RETRY_SECONDS",
    "WebhookDeliveryError",
    "WebhookDispatcher",
]
