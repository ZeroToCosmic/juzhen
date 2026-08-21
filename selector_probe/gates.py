"""Durable selector dependency gates with complete Redis projections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import re


_ENVIRONMENT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_REASON_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

GATE_PROJECT_LUA = """
local incoming_revision = tonumber(ARGV[1])
if not incoming_revision then
    return 'invalid'
end
local current = redis.call('GET', KEYS[1])
if current then
    local ok, decoded = pcall(cjson.decode, current)
    if ok and type(decoded) == 'table'
       and type(decoded.revision) == 'number' then
        if decoded.revision > incoming_revision then
            return 'stale'
        end
        if decoded.revision == incoming_revision then
            if current == ARGV[2] then
                return 'idempotent'
            end
            redis.call('SET', KEYS[1], ARGV[2])
            return 'repaired'
        end
    else
        redis.call('SET', KEYS[1], ARGV[2])
        return 'repaired'
    end
end
redis.call('SET', KEYS[1], ARGV[2])
return 'published'
""".strip()


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    if (
        len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _text_array(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be an array")
    return tuple(sorted({_text(item, name) for item in value}))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class StrategyDependency:
    alias: str
    strategy_id: str
    action_id: str
    action_type: str
    strategy_name: str = ""


@dataclass(frozen=True)
class GateReason:
    source: str
    reason_code: str
    aliases: tuple[str, ...]
    selector_version_id: str
    created_at: str

    def public_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "reason_code": self.reason_code,
            "aliases": list(self.aliases),
            "selector_version_id": self.selector_version_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class GateDecision:
    strategy_id: str
    allowed: bool
    reasons: tuple[GateReason, ...]

    def public_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "allowed": self.allowed,
            "effective_status": "active" if self.allowed else "paused",
            "reasons": [item.public_dict() for item in self.reasons],
        }


def build_dependency_index(
    strategies: object,
) -> dict[str, tuple[StrategyDependency, ...]]:
    if isinstance(strategies, (str, bytes)) or not isinstance(
        strategies,
        Sequence,
    ):
        raise ValueError("strategies must be an array")
    strategy_ids: set[str] = set()
    dependencies: dict[str, list[StrategyDependency]] = {}
    for raw_strategy in strategies:
        if not isinstance(raw_strategy, Mapping):
            raise ValueError("strategy must be an object")
        strategy_id = _text(raw_strategy.get("id"), "strategy id")
        if strategy_id in strategy_ids:
            raise ValueError("strategy IDs must be unique")
        strategy_ids.add(strategy_id)
        strategy_name = _optional_text(
            raw_strategy.get("name", ""),
            "strategy name",
        )
        actions = raw_strategy.get("actions")
        if not isinstance(actions, list):
            raise ValueError("strategy actions must be an array")
        action_ids: set[str] = set()
        for raw_action in actions:
            if not isinstance(raw_action, Mapping) or set(raw_action) != {
                "id",
                "type",
                "params",
            }:
                raise ValueError("strategy action has an invalid shape")
            action_id = _text(raw_action.get("id"), "action id")
            if action_id in action_ids:
                raise ValueError("action IDs must be unique within a strategy")
            action_ids.add(action_id)
            action_type = _text(raw_action.get("type"), "action type")
            params = raw_action.get("params")
            if not isinstance(params, Mapping):
                raise ValueError("action params must be an object")
            if "element" not in params:
                continue
            alias_value = params.get("element")
            if not isinstance(alias_value, str):
                raise ValueError("action element must be a string")
            if not alias_value:
                raise ValueError("action element must not be empty")
            alias = _text(alias_value, "element alias")
            dependencies.setdefault(alias, []).append(
                StrategyDependency(
                    alias=alias,
                    strategy_id=strategy_id,
                    action_id=action_id,
                    action_type=action_type,
                    strategy_name=strategy_name,
                )
            )
    return {
        alias: tuple(
            sorted(
                items,
                key=lambda item: (
                    item.strategy_id,
                    item.action_id,
                    item.action_type,
                ),
            )
        )
        for alias, items in sorted(dependencies.items())
    }


class StrategyGateService:
    def __init__(
        self,
        store: object,
        *,
        redis_client: object | None = None,
        environment: str = "production",
        site: str = "tiktok",
    ) -> None:
        if not isinstance(environment, str) or not _ENVIRONMENT.fullmatch(
            environment
        ):
            raise ValueError("environment must be a safe key segment")
        if not isinstance(site, str) or not _ENVIRONMENT.fullmatch(site):
            raise ValueError("site must be a safe key segment")
        self.store = store
        self.redis = redis_client
        self.environment = environment
        self.site = site

    def rebuild_dependencies(
        self,
        strategies: object,
    ) -> dict[str, tuple[StrategyDependency, ...]]:
        index = build_dependency_index(strategies)
        previous = set(self.store.managed_strategy_ids())
        rows = [
            (
                dependency.alias,
                dependency.strategy_id,
                dependency.action_id,
                dependency.action_type,
                dependency.strategy_name,
            )
            for dependencies in index.values()
            for dependency in dependencies
        ]
        current = set(self.store.replace_strategy_dependencies(rows))
        self._project_many(previous | current)
        return index

    def pause_for_aliases(
        self,
        aliases: object,
        reason_code: str,
        selector_version_id: str,
    ) -> tuple[str, ...]:
        failed_aliases = _text_array(aliases, "aliases")
        reason = _reason_code(reason_code)
        version = _optional_text(selector_version_id, "selector_version_id")
        rows = self.store.dependency_rows_for_aliases(failed_aliases)
        by_strategy: dict[str, set[str]] = {}
        for row in rows:
            by_strategy.setdefault(str(row["strategy_id"]), set()).add(
                str(row["alias"])
            )
        prepared = [
            {
                "strategy_id": strategy_id,
                "source": "probe",
                "site": self.site,
                "environment": self.environment,
                "reason_code": reason,
                "aliases": sorted(strategy_aliases),
                "selector_version_id": version,
                "created_by": "selector-probe",
            }
            for strategy_id, strategy_aliases in sorted(by_strategy.items())
        ]
        paused = self.store.upsert_gate_reasons(prepared)
        self._project_many(paused)
        return paused

    def set_manual_pause(
        self,
        strategy_id: str,
        paused: bool,
        actor: str,
    ) -> dict[str, object]:
        strategy = _text(strategy_id, "strategy_id")
        if not isinstance(paused, bool):
            raise ValueError("paused must be a boolean")
        actor_value = _text(actor, "actor")
        if paused:
            self.store.upsert_gate_reasons(
                [
                    {
                        "strategy_id": strategy,
                        "source": "manual",
                        "reason_code": "operator_pause",
                        "aliases": [],
                        "selector_version_id": "",
                        "created_by": actor_value,
                    }
                ]
            )
        else:
            self.store.clear_gate_reasons(
                (strategy,),
                source="manual",
                cleared_by=actor_value,
            )
        self._project_many((strategy,))
        return self.check(strategy).public_dict()

    def clear_probe_reasons(
        self,
        strategy_ids: object,
        selector_version_id: str,
    ) -> tuple[str, ...]:
        strategies = _text_array(strategy_ids, "strategy_ids")
        version = _optional_text(selector_version_id, "selector_version_id")
        affected = self.store.clear_gate_reasons(
            strategies,
            source="probe",
            cleared_by=f"probe:{version}" if version else "probe",
            site=self.site,
            environment=self.environment,
        )
        self._project_many(strategies)
        return affected

    def project_strategy_ids(self, strategy_ids: object) -> bool:
        strategies = _text_array(strategy_ids, "strategy_ids")
        projected = True
        for strategy_id in strategies:
            if self.redis is None:
                projected = False
                continue
            decision, revision, _managed = self._durable_snapshot(
                strategy_id
            )
            projected = self._project(
                strategy_id,
                decision,
                revision,
            ) and projected
        return projected

    def check(self, strategy_id: str) -> GateDecision:
        strategy = _text(strategy_id, "strategy_id")
        durable, revision, managed = self._durable_snapshot(strategy)
        if not managed and revision == 0:
            return durable
        if self.redis is None:
            return self._registry_unavailable(durable)
        if self._projection_matches(strategy, durable, revision):
            return durable
        self._project(strategy, durable, revision)
        refreshed, refreshed_revision, refreshed_managed = (
            self._durable_snapshot(strategy)
        )
        if not refreshed_managed and refreshed_revision == 0:
            return refreshed
        if self._projection_matches(
            strategy,
            refreshed,
            refreshed_revision,
        ):
            return refreshed
        return self._registry_unavailable(refreshed)

    def _durable_snapshot(
        self,
        strategy_id: str,
    ) -> tuple[GateDecision, int, bool]:
        revision, managed, rows = self.store.gate_snapshot(
            strategy_id,
            site=self.site,
            environment=self.environment,
        )
        reasons: list[GateReason] = []
        for row in rows:
            try:
                aliases = json.loads(row["aliases_json"])
            except (TypeError, ValueError) as error:
                raise RuntimeError("gate aliases are corrupt") from error
            reasons.append(
                GateReason(
                    source=str(row["source"]),
                    reason_code=str(row["reason_code"]),
                    aliases=_text_array(aliases, "gate aliases"),
                    selector_version_id=str(row["selector_version_id"]),
                    created_at=str(row["created_at"]),
                )
            )
        return (
            GateDecision(
                strategy_id=strategy_id,
                allowed=not reasons,
                reasons=tuple(reasons),
            ),
            revision,
            managed,
        )

    def _project_many(self, strategy_ids: object) -> None:
        for strategy_id in sorted(set(strategy_ids)):
            if self.redis is None:
                continue
            decision, revision, _managed = self._durable_snapshot(strategy_id)
            self._project(strategy_id, decision, revision)

    def _project(
        self,
        strategy_id: str,
        decision: GateDecision,
        revision: int,
    ) -> bool:
        if self.redis is None:
            return False
        payload = _canonical_json(
            {**decision.public_dict(), "revision": revision}
        )
        try:
            result = self.redis.eval(
                GATE_PROJECT_LUA,
                1,
                self._key(strategy_id),
                str(revision),
                payload,
            )
            if isinstance(result, bytes):
                result = result.decode("utf-8", errors="strict")
            return result in {"published", "idempotent", "repaired", "stale"}
        except Exception:
            return False

    def _projection_matches(
        self,
        strategy_id: str,
        decision: GateDecision,
        revision: int,
    ) -> bool:
        expected = _canonical_json(
            {**decision.public_dict(), "revision": revision}
        )
        try:
            raw = self.redis.get(self._key(strategy_id))
            if isinstance(raw, bytes):
                if len(raw) > 65_536:
                    return False
                projection = raw.decode("utf-8", errors="strict")
            elif isinstance(raw, str):
                projection = raw
            else:
                return False
            return projection == expected
        except Exception:
            return False

    @staticmethod
    def _registry_unavailable(durable: GateDecision) -> GateDecision:
        return GateDecision(
            strategy_id=durable.strategy_id,
            allowed=False,
            reasons=durable.reasons
            + (
                GateReason(
                    source="probe",
                    reason_code="registry_unavailable",
                    aliases=(),
                    selector_version_id="",
                    created_at=datetime.now(UTC).isoformat(),
                ),
            ),
        )

    def _key(self, strategy_id: str) -> str:
        strategy = _text(strategy_id, "strategy_id")
        return f"strategy_gate:{self.environment}:{self.site}:{strategy}"


def _reason_code(value: object) -> str:
    if not isinstance(value, str) or not _REASON_CODE.fullmatch(value):
        raise ValueError("reason_code is invalid")
    return value


def _optional_text(value: object, name: str) -> str:
    if not isinstance(value, str) or value != value.strip() or len(value) > 128:
        raise ValueError(f"{name} must be a trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} is invalid")
    return value


__all__ = [
    "GATE_PROJECT_LUA",
    "GateDecision",
    "GateReason",
    "StrategyDependency",
    "StrategyGateService",
    "build_dependency_index",
]
