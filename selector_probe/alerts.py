from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path

from selector_probe.redaction import (
    DEFAULT_EVIDENCE_ROOT,
    delete_evidence_file,
    redact_evidence,
    resolve_evidence_path,
)


def _timestamp(value: object | None, name: str) -> str:
    if value is None:
        return datetime.now(UTC).isoformat()
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO-8601 datetime")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(UTC).isoformat()


def _strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(
        value,
        (str, bytes, bytearray),
    ):
        raise ValueError(f"{name} must be an array")
    result = tuple(
        dict.fromkeys(
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        )
    )
    if len(result) > 256:
        raise ValueError(f"{name} is too large")
    return result


class AlertService:
    def __init__(
        self,
        store: object,
        *,
        profile_ids: Sequence[str] = (),
        evidence_root: str | Path = DEFAULT_EVIDENCE_ROOT,
    ) -> None:
        self.store = store
        self.profile_ids = tuple(profile_ids)
        self.evidence_root = resolve_evidence_path(
            evidence_root,
            ".evidence-root-check",
            must_exist=False,
        ).parent

    def open_or_update(
        self,
        *,
        site: str,
        environment: str = "production",
        failure_class: str,
        aliases: Sequence[str],
        active_version: str,
        details: Mapping[str, object],
        strategy_ids: Sequence[str] = (),
        now: str | None = None,
    ) -> dict[str, object]:
        if not isinstance(site, str) or not site.strip():
            raise ValueError("site must be a non-empty string")
        if not isinstance(failure_class, str) or not failure_class.strip():
            raise ValueError("failure_class must be a non-empty string")
        if not isinstance(active_version, str):
            raise ValueError("active_version must be a string")
        if not isinstance(environment, str) or not environment.strip():
            raise ValueError("environment must be a non-empty string")
        if not isinstance(details, Mapping):
            raise ValueError("details must be an object")
        selected_aliases = _strings(aliases, "aliases")
        selected_strategies = _strings(strategy_ids, "strategy_ids")
        fingerprint = hashlib.sha256(
            (
                f"{site.strip()}\0{environment.strip()}\0"
                f"{failure_class.strip()}\0"
                f"{','.join(sorted(selected_aliases))}\0"
                f"{active_version.strip()}"
            ).encode()
        ).hexdigest()
        sanitized = redact_evidence(
            dict(details),
            profile_ids=self.profile_ids,
        )
        assert isinstance(sanitized, dict)
        alert = self.store.open_or_update_alert(
            fingerprint=fingerprint,
            failure_class=failure_class.strip(),
            aliases=selected_aliases,
            strategy_ids=selected_strategies,
            active_version=active_version.strip(),
            details=sanitized,
            site=site.strip(),
            environment=environment.strip(),
            now=_timestamp(now, "now"),
        )
        return alert

    def acknowledge(
        self,
        alert_id: int,
        *,
        now: str | None = None,
    ) -> dict[str, object]:
        return self.store.transition_alert(
            alert_id,
            status="acknowledged",
            now=_timestamp(now, "now"),
        )

    def resolve(
        self,
        alert_id: int,
        *,
        now: str | None = None,
    ) -> dict[str, object]:
        return self.store.transition_alert(
            alert_id,
            status="resolved",
            now=_timestamp(now, "now"),
        )

    def record_screenshot(
        self,
        *,
        alert_id: int,
        path: str | Path,
        created_at: str | None = None,
    ) -> None:
        selected = resolve_evidence_path(
            self.evidence_root,
            path,
            must_exist=True,
        )
        if selected.suffix.casefold() not in {".jpg", ".jpeg"}:
            raise ValueError("alert screenshot must be JPEG")
        if not selected.is_file():
            raise ValueError("alert screenshot does not exist")
        self.store.record_alert_screenshot(
            alert_id=alert_id,
            path=str(selected),
            created_at=_timestamp(created_at, "created_at"),
        )

    def cleanup_screenshots(
        self,
        *,
        now: str | None = None,
        retention_days: int = 7,
    ) -> int:
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days < 1
        ):
            raise ValueError("retention_days must be a positive integer")
        selected_now = datetime.fromisoformat(_timestamp(now, "now"))
        before = selected_now - timedelta(days=retention_days)
        deleted = 0
        for item in self.store.expired_alert_screenshots(
            before=before.isoformat(),
        ):
            try:
                delete_evidence_file(
                    self.evidence_root,
                    str(item["path"]),
                )
            except (OSError, ValueError):
                continue
            if self.store.forget_alert_screenshot(
                alert_id=item["alert_id"],
                path=str(item["path"]),
            ):
                deleted += 1
        return deleted


__all__ = ["AlertService"]
