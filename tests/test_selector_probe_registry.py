import copy
import hashlib
import json

import pytest

from selector_probe.registry import (
    PublicationConflict,
    PublicationLeaseLost,
    PublicationRejected,
    RegistryTransient,
    RedisSelectorRegistry,
    RegistryKeys,
    reconcile_registry,
)
from selector_probe.store import SelectorProbeStore


def test_registry_failures_expose_stable_policy_codes():
    assert PublicationConflict.code == "publication_conflict"
    assert PublicationRejected.code == "publication_rejected"
    assert PublicationLeaseLost.code == "publication_lease_lost"
    assert RegistryTransient.code == "registry_unavailable"


def canonical_hash(elements):
    payload = json.dumps(
        elements,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def bundle(version="candidate"):
    elements = {
        "comment-entry": {
            "scope": "active_video",
            "locators": [
                {
                    "id": "entry",
                    "type": "attribute",
                    "name": "data-e2e",
                    "value": "comment-icon",
                    "enabled": True,
                }
            ],
        }
    }
    return {
        "version": version,
        "bundle_hash": canonical_hash(elements),
        "elements": elements,
    }


def evidence(value=None, *, profiles=2):
    current = value or bundle()
    alias_evidence = {
        alias: {
            "status": "ok",
            "candidate_id": next(
                item["id"] for item in definition["locators"] if item["enabled"]
            ),
        }
        for alias, definition in current["elements"].items()
    }
    validations = []
    for round_number in (1, 2):
        for profile_number in range(profiles):
            marker = f"{profile_number}:{round_number}"
            validations.append(
                {
                    "profile_mask": f"***p{profile_number:03d}",
                    "round_number": round_number,
                    "reset_evidence_hash": "sha256:"
                    + hashlib.sha256(f"reset:{marker}".encode()).hexdigest(),
                    "snapshot_hash": "sha256:"
                    + hashlib.sha256(f"snapshot:{marker}".encode()).hexdigest(),
                    "page_generation": "sha256:"
                    + hashlib.sha256(f"generation:{marker}".encode()).hexdigest(),
                    "aliases": copy.deepcopy(alias_evidence),
                }
            )
    return {
        "status": "passed",
        "bundle_hash": current["bundle_hash"],
        "profiles_passed": profiles,
        "rounds_passed": 2,
        "validations": validations,
    }


class FakeRedis:
    """Small Redis/Lua model that also records the exact eval call."""

    def __init__(self):
        self.data = {}
        self.eval_calls = []
        self.get_calls = []
        self.fail_before_eval = False

    def get(self, key):
        self.get_calls.append(key)
        return self.data.get(key)

    def eval(self, script, key_count, *values):
        self.eval_calls.append((script, key_count, values))
        if self.fail_before_eval:
            raise ConnectionError("redis unavailable")
        if key_count == 2:
            active_key, active_version_key = values[:key_count]
            (version_prefix,) = values[key_count:]
            active_version = self.data.get(active_version_key)
            immutable = (
                self.data.get(version_prefix + active_version.decode())
                if active_version
                else None
            )
            return [
                self.data.get(active_key),
                active_version,
                immutable,
            ]
        assert key_count == 6
        keys = values[:key_count]
        args = values[key_count:]
        (
            active_version_key,
            immutable_key,
            active_key,
            last_known_good_key,
            status_key,
            lease_key,
        ) = keys
        expected, version, bundle_json, bundle_hash, status_json, owner = args
        if owner and self.data.get(lease_key, b"").decode() != owner:
            return b"lease_lost"
        assert json.loads(bundle_json)["bundle_hash"] == bundle_hash
        current = self.data.get(active_version_key, b"").decode()
        immutable = self.data.get(immutable_key)
        encoded_bundle = bundle_json.encode()
        if current == version:
            if (
                immutable == encoded_bundle
                and self.data.get(active_key) == encoded_bundle
            ):
                return b"idempotent"
            return b"hash_mismatch"
        if current != expected:
            return b"conflict"
        if immutable is not None and immutable != encoded_bundle:
            return b"hash_mismatch"
        self.data.setdefault(immutable_key, encoded_bundle)
        self.data[active_version_key] = version.encode()
        self.data[active_key] = encoded_bundle
        self.data[last_known_good_key] = encoded_bundle
        self.data[status_key] = status_json.encode()
        return b"published"


def direct_event(version="candidate", expected=""):
    current = bundle(version)
    return {
        "version": version,
        "expected_previous_version": expected,
        "bundle": current,
    }


@pytest.mark.parametrize(
    ("environment", "site"),
    [
        ("production:other", "tiktok"),
        ("production", "../tiktok"),
        ("{production}", "tiktok"),
        ("production", "tik tok"),
        ("", "tiktok"),
    ],
)
def test_registry_keys_reject_unsafe_segments(environment, site):
    with pytest.raises(ValueError):
        RegistryKeys(environment=environment, site=site)


def test_publish_uses_one_lua_call_with_complete_values_and_no_element_keys():
    redis = FakeRedis()
    registry = RedisSelectorRegistry(
        redis,
        environment="production",
        site="tiktok",
    )

    assert registry.publish(direct_event()) == "published"

    _, key_count, values = redis.eval_calls[0]
    keys = values[:key_count]
    args = values[key_count:]
    assert keys == (
        registry.keys.active_version,
        registry.keys.version("candidate"),
        registry.keys.active,
        registry.keys.last_known_good,
        registry.keys.status,
        registry.keys.lease,
    )
    assert all("comment-entry" not in key for key in keys)
    assert args[0:2] == ("", "candidate")
    assert json.loads(args[2]) == bundle()
    assert args[3] == bundle()["bundle_hash"]
    assert json.loads(args[4]) == {
        "active_version": "candidate",
        "bundle_hash": bundle()["bundle_hash"],
        "health": "healthy",
        "status": "published",
    }
    assert args[5] == ""
    assert registry.get_active() == bundle()


def test_publish_is_idempotent_and_conflict_has_no_partial_version_write():
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    registry.publish(direct_event("newer"))
    before = copy.deepcopy(redis.data)

    assert registry.publish(direct_event("newer")) == "idempotent"
    with pytest.raises(PublicationConflict):
        registry.publish(direct_event("candidate", expected="older"))

    assert redis.data == before
    assert registry.keys.version("candidate") not in redis.data


@pytest.mark.parametrize(
    "mutate",
    [
        lambda event: event.update(extra=True),
        lambda event: event["bundle"].update(extra=True),
        lambda event: event["bundle"].update(bundle_hash="sha256:" + "0" * 64),
        lambda event: event.update(version="../candidate"),
        lambda event: event["bundle"].update(version="different"),
    ],
)
def test_publish_rejects_malformed_or_hash_mismatched_payload_before_redis(mutate):
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    event = direct_event()
    mutate(event)

    with pytest.raises((ValueError, PublicationRejected)):
        registry.publish(event)

    assert redis.eval_calls == []
    assert redis.data == {}


def test_get_active_rejects_invalid_utf8_or_tampered_hash():
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    redis.data[registry.keys.active] = b"\xff"
    redis.data[registry.keys.active_version] = b"candidate"
    redis.data[registry.keys.version("candidate")] = b"\xff"
    with pytest.raises(PublicationRejected):
        registry.get_active()

    damaged = bundle()
    damaged["bundle_hash"] = "sha256:" + "0" * 64
    redis.data[registry.keys.active] = json.dumps(damaged).encode()
    redis.data[registry.keys.version("candidate")] = json.dumps(damaged).encode()
    with pytest.raises(PublicationRejected):
        registry.get_active()


def test_get_active_rejects_internal_version_mismatch():
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    redis.data[registry.keys.active] = json.dumps(bundle()).encode()
    redis.data[registry.keys.active_version] = b"other"
    redis.data[registry.keys.version("other")] = json.dumps(bundle()).encode()

    with pytest.raises(PublicationRejected, match="active_version_mismatch"):
        registry.get_active()


def test_get_active_uses_one_lua_snapshot_and_rejects_torn_state_as_transient():
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    registry.publish(direct_event())
    redis.eval_calls.clear()
    redis.get_calls.clear()

    assert registry.get_active() == bundle()
    assert len(redis.eval_calls) == 1
    assert redis.eval_calls[0][1] == 2
    assert redis.eval_calls[0][2] == (
        registry.keys.active,
        registry.keys.active_version,
        f"{registry.keys.prefix}:version:",
    )
    assert redis.get_calls == []

    del redis.data[registry.keys.version("candidate")]
    with pytest.raises(RegistryTransient, match="registry_read_torn"):
        registry.get_active()


def test_crash_after_redis_publish_is_reconciled_and_acknowledged(tmp_path):
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        value = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="",
            model_id="gpt-main",
            prompt_version="selector-repair-v1",
        )
        event = store.claim_outbox_event(
            claim_token="worker-crashed",
            lease_seconds=0,
        )
        registry.publish(event)

        result = reconcile_registry(store, registry)

        assert result == {"acknowledged": 1, "version": version_id}
        assert store.get_version(version_id)["status"] == "published"


def test_older_active_retries_pending_but_newer_active_conflicts(tmp_path):
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    assert registry.publish(direct_event("old")) == "published"

    with SelectorProbeStore(tmp_path / "probe.db") as store:
        candidate = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": candidate["bundle_hash"],
                "elements": candidate["elements"],
            },
            evidence=evidence(candidate),
            base_version_id="old",
            model_id="",
            prompt_version="",
        )
        assert reconcile_registry(store, registry)["version"] == version_id
        assert registry.get_active()["version"] == version_id

    newer = bundle("newer")
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    registry.publish(
        {
            "version": "newer",
            "expected_previous_version": "",
            "bundle": newer,
        }
    )
    with SelectorProbeStore(tmp_path / "other.db") as store:
        candidate = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": candidate["bundle_hash"],
                "elements": candidate["elements"],
            },
            evidence=evidence(candidate),
            base_version_id="old",
            model_id="",
            prompt_version="",
        )
        result = reconcile_registry(store, registry)
        row = store.connection.execute(
            "SELECT status FROM publication_outbox WHERE aggregate_id = ?",
            (version_id,),
        ).fetchone()
        assert result == {"conflict": 1, "version": version_id}
        assert row["status"] == "conflict"
        assert registry.get_active()["version"] == "newer"


def test_empty_redis_repopulates_last_published_version(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        value = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        event = store.claim_outbox_event(claim_token="publisher")
        store.ack_outbox_event(
            event["outbox_id"],
            event["claim_token"],
            event["claim_generation"],
            outcome="published",
        )

        registry = RedisSelectorRegistry(
            FakeRedis(),
            environment="production",
            site="tiktok",
        )
        result = reconcile_registry(store, registry)

        assert result == {"repopulated": 1, "version": version_id}
        assert registry.get_active()["version"] == version_id


def test_empty_redis_repopulation_marks_tampered_sqlite_lkg_failed(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        value = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        event = store.claim_outbox_event(claim_token="publisher")
        store.ack_outbox_event(
            event["outbox_id"],
            event["claim_token"],
            event["claim_generation"],
            outcome="published",
        )
        damaged = bundle(version_id)
        damaged["bundle_hash"] = "sha256:" + "0" * 64
        store.connection.execute(
            "UPDATE selector_versions SET bundle_json = ? WHERE id = ?",
            (json.dumps(damaged), version_id),
        )
        store.connection.commit()
        registry = RedisSelectorRegistry(
            FakeRedis(),
            environment="production",
            site="tiktok",
        )

        result = reconcile_registry(store, registry)

        assert result == {"publication_failed": 1, "version": version_id}
        assert store.get_version(version_id)["status"] == "publication_failed"


def test_transient_torn_redis_read_requeues_without_permanent_failure(tmp_path):
    redis = FakeRedis()
    registry = RedisSelectorRegistry(redis, environment="production", site="tiktok")
    redis.data[registry.keys.active] = json.dumps(bundle("old")).encode()
    redis.data[registry.keys.active_version] = b"old"
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        value = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="old",
            model_id="",
            prompt_version="",
        )

        result = reconcile_registry(store, registry)

        assert result == {"retry_scheduled": 1, "version": version_id}
        outbox = store.connection.execute(
            "SELECT status, last_error FROM publication_outbox"
        ).fetchone()
        assert dict(outbox) == {
            "status": "pending",
            "last_error": "redis_unavailable",
        }
        assert store.get_version(version_id)["status"] == "validated"


def test_fenced_outbox_cancels_lost_owner_and_new_owner_can_publish(tmp_path):
    redis = FakeRedis()
    registry = RedisSelectorRegistry(
        redis,
        environment="production",
        site="tiktok",
    )
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        old_run = store.start_run(
            scheduled_for="2026-07-29T03:00:00+08:00",
            active_version_before="",
            attempt_token="old-owner",
        )
        value = bundle()
        old_version = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="",
            model_id="",
            prompt_version="",
            probe_run_id=old_run,
            attempt_token="old-owner",
        )

        lost = reconcile_registry(store, registry)

        assert lost == {"lease_lost": 1, "version": old_version}
        assert registry.get_active() is None
        assert store.get_version(old_version)["status"] == "cancelled"
        assert store.connection.execute(
            "SELECT status FROM publication_outbox WHERE aggregate_id = ?",
            (old_version,),
        ).fetchone()["status"] == "cancelled"

        new_run = store.start_run(
            scheduled_for="2026-07-30T03:00:00+08:00",
            active_version_before="",
            attempt_token="new-owner",
        )
        new_version = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="",
            model_id="",
            prompt_version="",
            probe_run_id=new_run,
            attempt_token="new-owner",
        )
        redis.data[registry.keys.lease] = b"new-owner"

        published = reconcile_registry(store, registry)

        assert published == {"acknowledged": 1, "version": new_version}
        assert registry.get_active()["version"] == new_version
        assert store.get_version(new_version)["status"] == "published"


def test_reconcile_hash_mismatch_fails_publication_and_stale_claim_cannot_ack(
    tmp_path,
):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        value = bundle()
        version_id = store.store_validated_version(
            bundle={
                "bundle_hash": value["bundle_hash"],
                "elements": value["elements"],
            },
            evidence=evidence(value),
            base_version_id="",
            model_id="",
            prompt_version="",
        )
        first = store.claim_outbox_event(
            claim_token="worker-old",
            lease_seconds=0,
        )
        second = store.claim_outbox_event(claim_token="worker-new")
        assert not store.ack_outbox_event(
            first["outbox_id"],
            first["claim_token"],
            first["claim_generation"],
            outcome="published",
        )

        redis = FakeRedis()
        registry = RedisSelectorRegistry(
            redis,
            environment="production",
            site="tiktok",
        )
        damaged = copy.deepcopy(second)
        damaged["bundle"]["bundle_hash"] = "sha256:" + "0" * 64
        with pytest.raises(PublicationRejected):
            registry.publish(damaged)
        assert store.fail_outbox_event(
            second["outbox_id"],
            second["claim_token"],
            second["claim_generation"],
            failure="hash_mismatch",
            retry=False,
        )
        row = store.connection.execute(
            "SELECT status FROM publication_outbox WHERE aggregate_id = ?",
            (version_id,),
        ).fetchone()
        assert row["status"] == "publication_failed"
