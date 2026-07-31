"""Atomic Redis publication and crash reconciliation for selector bundles."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any

from browser_element_schema import normalize_element_definitions


_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIRECT_FIELDS = {"version", "expected_previous_version", "bundle"}
_CLAIMED_FIELDS = _DIRECT_FIELDS | {
    "outbox_id",
    "claim_token",
    "claim_generation",
    "attempt_count",
}
_FENCED_CLAIMED_FIELDS = _CLAIMED_FIELDS | {"lease_owner"}


PUBLISH_LUA = """
if ARGV[6] ~= '' and redis.call('GET', KEYS[6]) ~= ARGV[6] then
    return 'lease_lost'
end
local current = redis.call('GET', KEYS[1]) or ''
local immutable = redis.call('GET', KEYS[2])
if current == ARGV[2] then
    if immutable == ARGV[3] and redis.call('GET', KEYS[3]) == ARGV[3] then
        redis.call('SET', KEYS[4], ARGV[3])
        redis.call('SET', KEYS[5], ARGV[5])
        return 'idempotent'
    end
    return 'hash_mismatch'
end
if current ~= ARGV[1] then
    return 'conflict'
end
if immutable and immutable ~= ARGV[3] then
    return 'hash_mismatch'
end
redis.call('SET', KEYS[2], ARGV[3], 'NX')
redis.call('SET', KEYS[1], ARGV[2])
redis.call('SET', KEYS[3], ARGV[3])
redis.call('SET', KEYS[4], ARGV[3])
redis.call('SET', KEYS[5], ARGV[5])
return 'published'
""".strip()

READ_ACTIVE_LUA = """
local active = redis.call('GET', KEYS[1])
local version = redis.call('GET', KEYS[2])
if not active and not version then
    return {false, false, false}
end
if not active or not version then
    return {active or false, version or false, false}
end
local immutable = redis.call('GET', ARGV[1] .. version)
return {active, version, immutable or false}
""".strip()


class PublicationConflict(RuntimeError):
    code = "publication_conflict"


class PublicationRejected(RuntimeError):
    code = "publication_rejected"


class PublicationLeaseLost(RuntimeError):
    code = "publication_lease_lost"


class RegistryTransient(RuntimeError):
    code = "registry_unavailable"


def _safe_segment(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
        raise ValueError(f"{name} must be a safe registry key segment")
    return value


def _safe_version(value: object, name: str, *, empty: bool = False) -> str:
    if empty and value == "":
        return ""
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise ValueError(f"{name} must be a safe version ID")
    return value


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (RecursionError, TypeError, ValueError) as error:
        raise PublicationRejected("payload_not_json_safe") from error


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _decoded_text(value: object, *, limit: int, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        if len(value) > limit:
            raise PublicationRejected(f"{name}_too_large")
        try:
            return value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise PublicationRejected(f"{name}_invalid_utf8") from error
    if isinstance(value, str):
        if len(value.encode("utf-8")) > limit:
            raise PublicationRejected(f"{name}_too_large")
        return value
    raise PublicationRejected(f"{name}_invalid_type")


def _normalize_bundle(value: object, version: str) -> tuple[dict[str, object], str]:
    if not isinstance(value, Mapping) or set(value) != {
        "version",
        "bundle_hash",
        "elements",
    }:
        raise PublicationRejected("bundle_invalid")
    if value.get("version") != version:
        raise PublicationRejected("bundle_version_mismatch")
    raw_elements = value.get("elements")
    if not isinstance(raw_elements, dict) or not raw_elements:
        raise PublicationRejected("bundle_invalid")
    try:
        elements = normalize_element_definitions(raw_elements)
    except (TypeError, ValueError) as error:
        raise PublicationRejected("bundle_invalid") from error
    if elements != raw_elements:
        raise PublicationRejected("bundle_not_canonical")
    if len(elements) > 256 or any(
        len(definition["locators"]) > 5 for definition in elements.values()
    ):
        raise PublicationRejected("bundle_resource_limit")
    expected_hash = _sha256(elements)
    supplied_hash = value.get("bundle_hash")
    if (
        not isinstance(supplied_hash, str)
        or not _HASH.fullmatch(supplied_hash)
        or supplied_hash != expected_hash
    ):
        raise PublicationRejected("bundle_hash_mismatch")
    bundle = {
        "version": version,
        "bundle_hash": expected_hash,
        "elements": elements,
    }
    if len(_canonical_json(bundle).encode("utf-8")) > 262_144:
        raise PublicationRejected("bundle_resource_limit")
    return bundle, expected_hash


def _normalize_event(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) not in (
        _DIRECT_FIELDS,
        _CLAIMED_FIELDS,
        _FENCED_CLAIMED_FIELDS,
    ):
        raise PublicationRejected("publication_event_invalid")
    version = _safe_version(value.get("version"), "version")
    expected = _safe_version(
        value.get("expected_previous_version"),
        "expected_previous_version",
        empty=True,
    )
    bundle, bundle_hash = _normalize_bundle(value.get("bundle"), version)
    result = {
        "version": version,
        "expected_previous_version": expected,
        "bundle": bundle,
        "bundle_hash": bundle_hash,
    }
    if set(value) in (_CLAIMED_FIELDS, _FENCED_CLAIMED_FIELDS):
        outbox_id = value.get("outbox_id")
        attempt_count = value.get("attempt_count")
        claim_generation = value.get("claim_generation")
        claim_token = value.get("claim_token")
        if (
            isinstance(outbox_id, bool)
            or not isinstance(outbox_id, int)
            or outbox_id < 1
            or isinstance(attempt_count, bool)
            or not isinstance(attempt_count, int)
            or attempt_count < 1
            or isinstance(claim_generation, bool)
            or not isinstance(claim_generation, int)
            or claim_generation < 1
            or not isinstance(claim_token, str)
            or not claim_token
            or claim_token != claim_token.strip()
            or len(claim_token) > 256
        ):
            raise PublicationRejected("publication_claim_invalid")
        result.update(
            outbox_id=outbox_id,
            attempt_count=attempt_count,
            claim_generation=claim_generation,
            claim_token=claim_token,
        )
        if set(value) == _FENCED_CLAIMED_FIELDS:
            lease_owner = value.get("lease_owner")
            if (
                not isinstance(lease_owner, str)
                or not lease_owner
                or lease_owner != lease_owner.strip()
                or len(lease_owner) > 256
            ):
                raise PublicationRejected("publication_lease_invalid")
            result["lease_owner"] = lease_owner
    return result


@dataclass(frozen=True)
class RegistryKeys:
    environment: str
    site: str
    namespace: str = "selector_registry"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "environment",
            _safe_segment(self.environment, "environment"),
        )
        object.__setattr__(self, "site", _safe_segment(self.site, "site"))
        object.__setattr__(
            self,
            "namespace",
            _safe_segment(self.namespace, "namespace"),
        )

    @property
    def prefix(self) -> str:
        return f"{self.namespace}:{self.environment}:{self.site}"

    @property
    def active(self) -> str:
        return f"{self.prefix}:active"

    @property
    def active_version(self) -> str:
        return f"{self.prefix}:active_version"

    @property
    def last_known_good(self) -> str:
        return f"{self.prefix}:last_known_good"

    @property
    def status(self) -> str:
        return f"{self.prefix}:probe_status"

    @property
    def lease(self) -> str:
        return f"{self.prefix}:lease"

    def version(self, version_id: object) -> str:
        return f"{self.prefix}:version:{_safe_version(version_id, 'version_id')}"


class RedisSelectorRegistry:
    def __init__(
        self,
        redis_client: object,
        *,
        environment: str,
        site: str,
        namespace: str = "selector_registry",
    ) -> None:
        self.redis = redis_client
        self.keys = RegistryKeys(
            environment=environment,
            site=site,
            namespace=namespace,
        )

    def publish(self, event: object) -> str:
        publication = _normalize_event(event)
        bundle = publication["bundle"]
        assert isinstance(bundle, dict)
        bundle_json = _canonical_json(bundle)
        status_json = _canonical_json(
            {
                "active_version": publication["version"],
                "bundle_hash": publication["bundle_hash"],
                "health": "healthy",
                "status": "published",
            }
        )
        result = self.redis.eval(
            PUBLISH_LUA,
            6,
            self.keys.active_version,
            self.keys.version(publication["version"]),
            self.keys.active,
            self.keys.last_known_good,
            self.keys.status,
            self.keys.lease,
            publication["expected_previous_version"],
            publication["version"],
            bundle_json,
            publication["bundle_hash"],
            status_json,
            publication.get("lease_owner", ""),
        )
        decoded = _decoded_text(
            result,
            limit=64,
            name="publication_result",
        )
        if decoded in {"published", "idempotent"}:
            return decoded
        if decoded == "conflict":
            raise PublicationConflict("active selector version changed")
        if decoded == "hash_mismatch":
            raise PublicationRejected("redis_hash_mismatch")
        if decoded == "lease_lost":
            raise PublicationLeaseLost("publication lease lost")
        raise PublicationRejected("redis_publication_result_invalid")

    def get_active(self) -> dict[str, object] | None:
        raw = self.redis.eval(
            READ_ACTIVE_LUA,
            2,
            self.keys.active,
            self.keys.active_version,
            f"{self.keys.prefix}:version:",
        )
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise RegistryTransient("registry_read_invalid")
        raw_active, raw_version, raw_immutable = raw
        if raw_active is None and raw_version is None and raw_immutable is None:
            return None
        if (
            raw_active is None
            or raw_version is None
            or raw_immutable is None
        ):
            raise RegistryTransient("registry_read_torn")
        text = _decoded_text(
            raw_active,
            limit=262_144,
            name="active_bundle",
        )
        active_version = _decoded_text(
            raw_version,
            limit=128,
            name="active_version",
        )
        immutable_text = _decoded_text(
            raw_immutable,
            limit=262_144,
            name="immutable_bundle",
        )
        if text is None or active_version is None or immutable_text is None:
            raise RegistryTransient("registry_read_torn")
        if text != immutable_text:
            raise PublicationRejected("immutable_bundle_mismatch")
        try:
            value = json.loads(text)
        except (RecursionError, TypeError, ValueError) as error:
            raise PublicationRejected("active_bundle_invalid_json") from error
        if not isinstance(value, Mapping):
            raise PublicationRejected("active_bundle_invalid")
        version = value.get("version")
        try:
            safe_version = _safe_version(version, "active version")
        except ValueError as error:
            raise PublicationRejected("active_bundle_invalid") from error
        if active_version != safe_version:
            raise PublicationRejected("active_version_mismatch")
        bundle, _bundle_hash = _normalize_bundle(value, safe_version)
        return bundle


def _direct_from_version(version: Mapping[str, Any], expected: str) -> dict[str, object]:
    bundle = version.get("bundle")
    if not isinstance(bundle, Mapping):
        raise PublicationRejected("stored_bundle_invalid")
    return {
        "version": version["id"],
        "expected_previous_version": expected,
        "bundle": dict(bundle),
    }


def reconcile_registry(
    store: object,
    registry: RedisSelectorRegistry,
) -> dict[str, object]:
    event = store.claim_outbox_event()
    if event is None:
        active = registry.get_active()
        if active is not None:
            return {"acknowledged": 0, "version": active["version"]}
        last_published = store.last_published_version(
            site=registry.keys.site,
            environment=registry.keys.environment,
        )
        if last_published is None:
            return {"acknowledged": 0, "version": ""}
        try:
            result = registry.publish(_direct_from_version(last_published, ""))
        except PublicationRejected:
            failed = store.mark_version_publication_failed(last_published["id"])
            return {
                "publication_failed": int(failed),
                "version": last_published["id"],
            }
        if result not in {"published", "idempotent"}:
            raise PublicationRejected("repopulation_failed")
        return {"repopulated": 1, "version": last_published["id"]}

    outbox_id = event["outbox_id"]
    event_token = event["claim_token"]
    version = event["version"]
    try:
        active = registry.get_active()
        if active is None and event["expected_previous_version"]:
            previous = store.last_published_version(
                site=registry.keys.site,
                environment=registry.keys.environment,
            )
            if (
                previous is not None
                and previous["id"] == event["expected_previous_version"]
            ):
                registry.publish(_direct_from_version(previous, ""))
                active = registry.get_active()
        if active is not None and active["version"] == version:
            if active["bundle_hash"] != event["bundle"]["bundle_hash"]:
                store.fail_outbox_event(
                    outbox_id,
                    event_token,
                    event["claim_generation"],
                    failure="hash_mismatch",
                    retry=False,
                )
                return {"publication_failed": 1, "version": version}
            acknowledged = store.ack_outbox_event(
                outbox_id,
                event_token,
                event["claim_generation"],
                outcome="idempotent",
            )
            return {
                "acknowledged": int(acknowledged),
                "version": version,
            }
        outcome = registry.publish(event)
        acknowledged = store.ack_outbox_event(
            outbox_id,
            event_token,
            event["claim_generation"],
            outcome=outcome,
        )
        return {"acknowledged": int(acknowledged), "version": version}
    except PublicationLeaseLost:
        cancelled = store.cancel_outbox_event(
            outbox_id,
            event_token,
            event["claim_generation"],
        )
        return {"lease_lost": int(cancelled), "version": version}
    except PublicationConflict:
        acknowledged = store.ack_outbox_event(
            outbox_id,
            event_token,
            event["claim_generation"],
            outcome="conflict",
        )
        return {"conflict": int(acknowledged), "version": version}
    except PublicationRejected:
        store.fail_outbox_event(
            outbox_id,
            event_token,
            event["claim_generation"],
            failure="hash_mismatch",
            retry=False,
        )
        return {"publication_failed": 1, "version": version}
    except Exception:
        store.fail_outbox_event(
            outbox_id,
            event_token,
            event["claim_generation"],
            failure="redis_unavailable",
            retry=True,
        )
        return {"retry_scheduled": 1, "version": version}


__all__ = [
    "PUBLISH_LUA",
    "READ_ACTIVE_LUA",
    "PublicationConflict",
    "PublicationLeaseLost",
    "PublicationRejected",
    "RedisSelectorRegistry",
    "RegistryTransient",
    "RegistryKeys",
    "reconcile_registry",
]
