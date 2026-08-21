import json

import pytest

from selector_probe.gates import StrategyGateService, build_dependency_index
from selector_probe.store import SelectorProbeStore


COMMENT_ENTRY = "comment-entry"
COMMENT_SUBMIT = "comment-submit"


def strategies():
    return [
        {
            "id": "comment-flow",
            "name": "Comment flow",
            "actions": [
                {
                    "id": "entry",
                    "type": "click",
                    "params": {"element": COMMENT_ENTRY},
                },
                {
                    "id": "wait",
                    "type": "pause",
                    "params": {"duration_seconds": [1, 1]},
                },
                {
                    "id": "submit",
                    "type": "click",
                    "params": {"element": COMMENT_SUBMIT},
                },
            ],
        },
        {
            "id": "reader-flow",
            "name": "Reader flow",
            "actions": [
                {
                    "id": "open",
                    "type": "click",
                    "params": {"element": "reader-entry"},
                }
            ],
        },
    ]


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.fail = False

    def set(self, key, value):
        if self.fail:
            raise ConnectionError("redis unavailable")
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    def get(self, key):
        if self.fail:
            raise ConnectionError("redis unavailable")
        return self.data.get(key)

    def eval(self, _script, key_count, *values):
        if self.fail:
            raise ConnectionError("redis unavailable")
        assert key_count == 1
        key = values[0]
        incoming_revision = int(values[1])
        payload = values[2]
        current = self.data.get(key)
        if current is not None:
            try:
                decoded = json.loads(current)
                current_revision = decoded.get("revision")
            except (TypeError, ValueError):
                current_revision = None
            if isinstance(current_revision, bool) or not isinstance(
                current_revision,
                (int, float),
            ):
                self.data[key] = payload.encode()
                return b"repaired"
            if current_revision > incoming_revision:
                return b"stale"
            if current_revision == incoming_revision:
                if json.loads(current) == json.loads(payload):
                    return b"idempotent"
                self.data[key] = payload.encode()
                return b"repaired"
        self.data[key] = payload.encode()
        return b"published"


@pytest.fixture
def gate_service(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        redis = FakeRedis()
        service = StrategyGateService(
            store,
            redis_client=redis,
            environment="production",
        )
        service.rebuild_dependencies(strategies())
        yield service


def test_dependency_index_maps_alias_to_exact_strategy_actions():
    index = build_dependency_index(strategies())

    assert [
        (item.strategy_id, item.action_id)
        for item in index[COMMENT_ENTRY]
    ] == [("comment-flow", "entry")]
    assert [
        (item.strategy_id, item.action_id)
        for item in index[COMMENT_SUBMIT]
    ] == [("comment-flow", "submit")]
    assert "wait" not in str(index)


@pytest.mark.parametrize(
    "invalid",
    [
        "not-an-array",
        [{"id": "", "actions": []}],
        [{"id": "flow", "actions": "not-an-array"}],
        [
            {
                "id": "flow",
                "actions": [
                    {"id": "same", "type": "click", "params": {"element": "a"}},
                    {"id": "same", "type": "click", "params": {"element": "b"}},
                ],
            }
        ],
        [
            {
                "id": "flow",
                "actions": [
                    {
                        "id": "empty",
                        "type": "click",
                        "params": {"element": ""},
                    }
                ],
            }
        ],
        [
            {
                "id": "flow",
                "actions": [
                    {"id": "click", "type": "click", "params": {"element": 7}}
                ],
            }
        ],
    ],
)
def test_dependency_index_rejects_noncanonical_shapes(invalid):
    with pytest.raises(ValueError):
        build_dependency_index(invalid)


def test_probe_recovery_never_clears_manual_pause(gate_service):
    gate_service.set_manual_pause("comment-flow", True, actor="admin")
    gate_service.pause_for_aliases(
        (COMMENT_ENTRY,),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )

    cleared = gate_service.clear_probe_reasons(
        ("comment-flow",),
        "sel-new",
    )
    decision = gate_service.check("comment-flow")

    assert cleared == ("comment-flow",)
    assert decision.allowed is False
    assert [item.source for item in decision.reasons] == ["manual"]


def test_manual_resume_never_clears_probe_pause(gate_service):
    gate_service.pause_for_aliases(
        (COMMENT_ENTRY,),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )
    gate_service.set_manual_pause("comment-flow", True, actor="admin")

    result = gate_service.set_manual_pause(
        "comment-flow",
        False,
        actor="admin",
    )

    assert result["allowed"] is False
    assert [item.source for item in gate_service.check("comment-flow").reasons] == [
        "probe"
    ]


def test_failed_alias_pauses_only_dependent_strategy(gate_service):
    paused = gate_service.pause_for_aliases(
        (COMMENT_SUBMIT,),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )

    assert paused == ("comment-flow",)
    assert gate_service.check("comment-flow").allowed is False
    assert gate_service.check("reader-flow").allowed is True


def test_repeated_probe_reason_unions_aliases_without_overwrite(gate_service):
    gate_service.pause_for_aliases(
        (COMMENT_ENTRY,),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )
    gate_service.pause_for_aliases(
        (COMMENT_SUBMIT,),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )

    reasons = gate_service.check("comment-flow").reasons
    assert len(reasons) == 1
    assert reasons[0].aliases == (COMMENT_ENTRY, COMMENT_SUBMIT)


def test_projection_is_complete_canonical_effective_decision(gate_service):
    gate_service.set_manual_pause("comment-flow", True, actor="admin")
    decision = gate_service.check("comment-flow")
    raw = gate_service.redis.get(
        "strategy_gate:production:tiktok:comment-flow"
    )

    projected = json.loads(raw)
    revision, _managed, _rows = gate_service.store.gate_snapshot(
        "comment-flow"
    )
    assert projected == {**decision.public_dict(), "revision": revision}
    assert projected["effective_status"] == "paused"


def test_redis_projection_failure_is_fail_closed_only_for_managed_strategy(
    gate_service,
):
    gate_service.redis.fail = True
    gate_service.set_manual_pause("comment-flow", True, actor="admin")

    managed = gate_service.check("comment-flow")
    unmanaged = gate_service.check("unmanaged-flow")

    assert managed.allowed is False
    assert managed.reasons[-1].reason_code == "registry_unavailable"
    assert unmanaged.allowed is True
    rows = gate_service.store.connection.execute(
        """
        SELECT source, reason_code
        FROM strategy_gate_reasons
        WHERE strategy_id = ? AND cleared_at IS NULL
        """,
        ("comment-flow",),
    ).fetchall()
    assert [tuple(row) for row in rows] == [("manual", "operator_pause")]


def test_missing_redis_client_fails_closed_only_for_managed_strategy(tmp_path):
    with SelectorProbeStore(tmp_path / "probe.db") as store:
        service = StrategyGateService(
            store,
            redis_client=None,
            environment="production",
        )
        service.rebuild_dependencies(strategies())

        managed = service.check("comment-flow")
        unmanaged = service.check("unmanaged-flow")

        assert managed.allowed is False
        assert [item.reason_code for item in managed.reasons] == [
            "registry_unavailable"
        ]
        assert unmanaged.allowed is True


def test_older_projection_cannot_overwrite_newer_durable_decision(gate_service):
    old_decision, old_revision, _managed = gate_service._durable_snapshot(
        "comment-flow"
    )
    gate_service.set_manual_pause("comment-flow", True, actor="admin")
    current = gate_service.check("comment-flow")

    assert gate_service._project(
        "comment-flow",
        old_decision,
        old_revision,
    )
    projected = json.loads(
        gate_service.redis.get("strategy_gate:production:tiktok:comment-flow")
    )
    assert projected["revision"] > old_revision
    assert projected["effective_status"] == "paused"
    assert gate_service.check("comment-flow") == current


def test_stale_projection_is_repaired_from_durable_revision(gate_service):
    decision, revision, _managed = gate_service._durable_snapshot(
        "comment-flow"
    )
    gate_service.redis.data[
        "strategy_gate:production:tiktok:comment-flow"
    ] = json.dumps(
        {
            **decision.public_dict(),
            "revision": max(revision - 1, 0),
            "allowed": False,
            "effective_status": "paused",
        }
    ).encode()

    checked = gate_service.check("comment-flow")

    assert checked.allowed is True
    repaired = json.loads(
        gate_service.redis.get("strategy_gate:production:tiktok:comment-flow")
    )
    assert repaired == {**checked.public_dict(), "revision": revision}


def test_removed_dependency_repairs_stale_redis_pause_from_revision_tombstone(
    gate_service,
):
    gate_service.pause_for_aliases(
        (COMMENT_ENTRY,),
        reason_code="selector_validation_failed",
        selector_version_id="sel-old",
    )
    key = "strategy_gate:production:tiktok:comment-flow"
    paused = json.loads(gate_service.redis.get(key))
    assert paused["allowed"] is False

    gate_service.redis.fail = True
    gate_service.clear_probe_reasons(("comment-flow",), "sel-new")
    gate_service.rebuild_dependencies(
        [item for item in strategies() if item["id"] != "comment-flow"]
    )
    gate_service.redis.fail = False

    checked = gate_service.check("comment-flow")

    revision, managed, _rows = gate_service.store.gate_snapshot("comment-flow")
    assert revision > paused["revision"]
    assert managed is False
    assert checked.allowed is True
    assert checked.reasons == ()
    repaired = json.loads(gate_service.redis.get(key))
    assert repaired == {**checked.public_dict(), "revision": revision}


@pytest.mark.parametrize(
    "corrupt_projection",
    [
        b'{"allowed":false,"effective_status":"paused"}',
        b"not-json",
        b'{"revision":"old","allowed":false}',
        None,
    ],
)
def test_authoritative_sqlite_repairs_legacy_or_invalid_projection(
    gate_service,
    corrupt_projection,
):
    key = "strategy_gate:production:tiktok:comment-flow"
    if corrupt_projection is None:
        decision, revision, _managed = gate_service._durable_snapshot(
            "comment-flow"
        )
        corrupt_projection = json.dumps(
            {
                **decision.public_dict(),
                "revision": revision,
                "allowed": False,
                "effective_status": "paused",
            }
        ).encode()
    gate_service.redis.data[key] = corrupt_projection

    checked = gate_service.check("comment-flow")

    assert checked.allowed is True
    decision, revision, _managed = gate_service._durable_snapshot(
        "comment-flow"
    )
    assert json.loads(gate_service.redis.get(key)) == {
        **decision.public_dict(),
        "revision": revision,
    }


def test_invalid_dependency_rebuild_keeps_complete_previous_index(gate_service):
    before = gate_service.store.connection.execute(
        """
        SELECT alias, strategy_id, action_id, action_type
        FROM strategy_dependencies
        ORDER BY alias, strategy_id, action_id
        """
    ).fetchall()

    with pytest.raises(ValueError):
        gate_service.rebuild_dependencies(
            [{"id": "broken", "actions": [{"id": "", "type": "click"}]}]
        )

    after = gate_service.store.connection.execute(
        """
        SELECT alias, strategy_id, action_id, action_type
        FROM strategy_dependencies
        ORDER BY alias, strategy_id, action_id
        """
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
