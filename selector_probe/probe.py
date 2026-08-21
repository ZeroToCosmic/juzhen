"""Orchestration for deterministic, manually managed selector probes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import re
import threading

from browser_public_identity import mask_profile_id


LEASE_TTL_SECONDS = 120
LEASE_HEARTBEAT_SECONDS = 30
HEARTBEAT_JOIN_TIMEOUT_SECONDS = 5.0
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVIDENCE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROGRESS_STATUSES = frozenset(
    {"queued", "running", "passed", "failed", "skipped"}
)
_CANDIDATE_UNSET = object()


class ProbeLeaseLost(RuntimeError):
    code = "probe_lease_lost"

    def __init__(self) -> None:
        super().__init__(self.code)


class _LeaseHeartbeat:
    """Renew a Redis lease while a synchronous probe owns it."""

    def __init__(self, lease: object) -> None:
        self._lease = lease
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._renew_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._run,
            name="selector-probe-lease-heartbeat",
            daemon=True,
        )

    @property
    def lost(self) -> bool:
        return self._lost.is_set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> bool:
        self._stop.set()
        self._thread.join(HEARTBEAT_JOIN_TIMEOUT_SECONDS)
        stopped = not self._thread.is_alive()
        if not stopped:
            self._lost.set()
        return stopped

    def require_owned(self, *, renew: bool = False) -> None:
        if self.lost:
            raise ProbeLeaseLost()
        if renew and not self._renew(
            lock_timeout=HEARTBEAT_JOIN_TIMEOUT_SECONDS
        ):
            raise ProbeLeaseLost()

    def _renew(self, *, lock_timeout: float | None = None) -> bool:
        acquired = (
            self._renew_lock.acquire()
            if lock_timeout is None
            else self._renew_lock.acquire(timeout=lock_timeout)
        )
        if not acquired:
            self._lost.set()
            return False
        try:
            if self.lost:
                return False
            try:
                owned = self._lease.renew() is True
            except Exception:
                owned = False
            if not owned:
                self._lost.set()
            return owned
        finally:
            self._renew_lock.release()

    def _run(self) -> None:
        while not self._stop.wait(LEASE_HEARTBEAT_SECONDS):
            if not self._renew():
                return


def _safe_error_code(error: BaseException) -> str:
    code = getattr(error, "code", "")
    if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
        return code
    return "probe_unavailable"


def _sanitize_progress_event(
    value: Mapping[str, object],
) -> dict[str, object]:
    name = str(value.get("name") or "").strip()
    if not _SAFE_CODE.fullmatch(name):
        name = "probe"
    status = str(value.get("status") or "").strip()
    if status not in _PROGRESS_STATUSES:
        status = "running"
    profile_mask = mask_profile_id(value.get("profile_mask"))
    attempt = value.get("attempt_count", 1)
    if isinstance(attempt, bool) or not isinstance(attempt, int):
        attempt = 1
    failure_code = str(value.get("failure_code") or "").strip()
    if failure_code and not _SAFE_CODE.fullmatch(failure_code):
        failure_code = "probe_unavailable"
    round_number = value.get("round")
    if (
        isinstance(round_number, bool)
        or not isinstance(round_number, int)
        or round_number not in {1, 2}
    ):
        round_number = None
    return {
        "name": name,
        "profile_mask": profile_mask,
        "status": status,
        "attempt_count": max(1, min(attempt, 99)),
        "round": round_number,
        "failure_code": failure_code,
        "summary": str(value.get("summary") or "").strip()[:160],
    }


def _managed_stage(
    runtime: object,
    name: str,
    status: str,
    **details: object,
) -> None:
    recorder = getattr(runtime, "record_business_stage", None)
    if not callable(recorder):
        return
    try:
        recorder(name, status, **details)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


def _managed_attempt_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return max(1, min(value, 3))


def _managed_element_results(validation: object) -> list[dict[str, object]]:
    if not isinstance(validation, Mapping):
        return []
    raw_elements = validation.get("elements")
    if not isinstance(raw_elements, Mapping):
        return []
    results: list[dict[str, object]] = []
    for element_id in sorted(
        key for key in raw_elements if isinstance(key, str) and key
    ):
        item = raw_elements.get(element_id)
        if not isinstance(item, Mapping):
            continue
        status = "passed" if item.get("status") == "passed" else "failed"
        result: dict[str, object] = {
            "element_id": element_id,
            "status": status,
            "failure_code": "",
            "attempt_count": _managed_attempt_count(
                item.get("attempt_count", 1)
            ),
            "selected_locator_index": None,
            "profile_results": [],
        }
        failure_code = item.get("failure_code")
        if (
            status == "failed"
            and isinstance(failure_code, str)
            and _SAFE_CODE.fullmatch(failure_code)
        ):
            result["failure_code"] = failure_code
        selected = item.get("selected_locator_index")
        if (
            not isinstance(selected, bool)
            and isinstance(selected, int)
            and selected >= 0
        ):
            result["selected_locator_index"] = selected
        profile_results = item.get("profile_results")
        if isinstance(profile_results, Sequence) and not isinstance(
            profile_results, (str, bytes, bytearray)
        ):
            safe_profiles: list[dict[str, object]] = []
            for profile_result in profile_results[:16]:
                if not isinstance(profile_result, Mapping):
                    continue
                safe_profile: dict[str, object] = {
                    "profile_mask": mask_profile_id(
                        profile_result.get("profile_mask")
                    ),
                    "status": (
                        "passed"
                        if profile_result.get("status") == "passed"
                        else "failed"
                    ),
                }
                round_number = profile_result.get("round_number")
                if not isinstance(round_number, bool) and round_number in (1, 2):
                    safe_profile["round_number"] = round_number
                code = profile_result.get("failure_code")
                if isinstance(code, str) and _SAFE_CODE.fullmatch(code):
                    safe_profile["failure_code"] = code
                selected_index = profile_result.get("selected_locator_index")
                if (
                    not isinstance(selected_index, bool)
                    and isinstance(selected_index, int)
                    and selected_index >= 0
                ):
                    safe_profile["selected_locator_index"] = selected_index
                safe_profiles.append(safe_profile)
            result["profile_results"] = safe_profiles
        results.append(result)
    return results


def _managed_matrix_complete(validation: object) -> bool:
    if not isinstance(validation, Mapping):
        return False
    profiles = validation.get("profiles_passed")
    rounds = validation.get("rounds_passed")
    validations = validation.get("validations")
    elements = validation.get("elements")
    if (
        validation.get("status") != "passed"
        or validation.get("consistent") is not True
        or isinstance(profiles, bool)
        or not isinstance(profiles, int)
        or profiles < 2
        or isinstance(rounds, bool)
        or rounds != 2
        or not isinstance(validations, Sequence)
        or isinstance(validations, (str, bytes, bytearray))
        or len(validations) != profiles * 2
        or not isinstance(elements, Mapping)
        or not elements
        or any(
            not isinstance(element_id, str) or not element_id
            for element_id in elements
        )
        or any(
            not isinstance(item, Mapping) or item.get("status") != "passed"
            for item in elements.values()
        )
    ):
        return False

    expected_elements = set(elements)
    profile_rounds: set[tuple[str, int]] = set()
    profiles_seen: set[str] = set()
    for record in validations:
        if not isinstance(record, Mapping) or set(record) != {
            "profile_mask",
            "round_number",
            "reset_evidence_hash",
            "snapshot_hash",
            "page_generation",
            "aliases",
        }:
            return False
        profile_mask = record.get("profile_mask")
        round_number = record.get("round_number")
        aliases = record.get("aliases")
        hashes = (
            record.get("reset_evidence_hash"),
            record.get("snapshot_hash"),
            record.get("page_generation"),
        )
        if (
            not isinstance(profile_mask, str)
            or not profile_mask
            or isinstance(round_number, bool)
            or round_number not in (1, 2)
            or not isinstance(aliases, Mapping)
            or not all(
                isinstance(value, str) and _EVIDENCE_HASH.fullmatch(value)
                for value in hashes
            )
            or set(aliases) != expected_elements
            or any(
                not isinstance(item, Mapping)
                or item.get("status") != "ok"
                or not isinstance(item.get("candidate_id"), str)
                or not item.get("candidate_id")
                for item in aliases.values()
            )
        ):
            return False
        pair = (profile_mask, round_number)
        if pair in profile_rounds:
            return False
        profile_rounds.add(pair)
        profiles_seen.add(profile_mask)
    return len(profiles_seen) == profiles and all(
        (profile, round_number) in profile_rounds
        for profile in profiles_seen
        for round_number in (1, 2)
    )


def _managed_result(
    status: str,
    *,
    failure_code: str = "",
    element_results: object = (),
    published: bool = False,
    reconciled: bool | None = None,
    new_version: str | None = None,
    validation_evidence: object = None,
) -> dict[str, object]:
    safe_results = (
        list(element_results)
        if isinstance(element_results, Sequence)
        and not isinstance(element_results, (str, bytes, bytearray))
        else []
    )
    failed_ids = [
        str(item["element_id"])
        for item in safe_results
        if isinstance(item, Mapping)
        and item.get("status") == "failed"
        and isinstance(item.get("element_id"), str)
    ]
    result: dict[str, object] = {
        "status": status,
        "published": published,
        "new_version": new_version,
        "proposed_pause_aliases": list(dict.fromkeys(failed_ids)),
        "element_results": safe_results,
        "attempt_count": max(
            (
                _managed_attempt_count(item.get("attempt_count"))
                for item in safe_results
                if isinstance(item, Mapping)
            ),
            default=0,
        ),
    }
    if failure_code:
        result["failure_code"] = (
            failure_code
            if _SAFE_CODE.fullmatch(failure_code)
            else "probe_unavailable"
        )
    if reconciled is not None:
        result["reconciled"] = reconciled
    if isinstance(validation_evidence, Mapping):
        result["validation_evidence"] = dict(validation_evidence)
    return result


def run_managed_probe(
    runtime: object,
    *,
    publish: bool = True,
    candidate: object = _CANDIDATE_UNSET,
) -> dict[str, object]:
    """Validate saved manual locators and optionally publish atomically."""

    if not isinstance(publish, bool):
        raise ValueError("publish must be a boolean")
    required_methods = ["validate_matrix"]
    if candidate is _CANDIDATE_UNSET:
        required_methods.append("load_candidate")
    if publish:
        required_methods.extend(
            [
                "promote_saved_fallbacks",
                "prepare_publication",
                "store_and_publish",
            ]
        )
    methods = {name: getattr(runtime, name, None) for name in required_methods}
    if any(not callable(method) for method in methods.values()):
        return _managed_result(
            "infrastructure_unavailable",
            failure_code="runtime_contract_invalid",
        )

    if candidate is _CANDIDATE_UNSET:
        try:
            candidate = methods["load_candidate"]()
        except asyncio.CancelledError:
            raise
        except Exception:
            return _managed_result(
                "infrastructure_unavailable",
                failure_code="candidate_unavailable",
            )
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(candidate.get("elements"), Mapping)
    ):
        return _managed_result(
            "infrastructure_unavailable",
            failure_code="candidate_unavailable",
        )
    if not candidate["elements"]:
        return _managed_result(
            "awaiting_element_selection",
            failure_code="awaiting_element_selection",
        )

    try:
        validation = methods["validate_matrix"](candidate, max_attempts=3)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        failure_code = _safe_error_code(error)
        if failure_code == "probe_unavailable":
            failure_code = "validation_unavailable"
        _managed_stage(
            runtime, "validate_elements", "failed", failure_code=failure_code
        )
        return _managed_result(
            "infrastructure_unavailable",
            failure_code=failure_code,
        )

    element_results = _managed_element_results(validation)
    if not isinstance(validation, Mapping):
        _managed_stage(
            runtime,
            "validate_elements",
            "failed",
            failure_code="validation_matrix_incomplete",
        )
        _managed_stage(runtime, "protect_or_recover", "failed")
        _managed_stage(runtime, "alert_and_cleanup", "passed")
        return _managed_result(
            "infrastructure_unavailable",
            failure_code="validation_matrix_incomplete",
        )
    if validation.get("status") != "passed":
        failed = [
            item for item in element_results if item.get("status") == "failed"
        ]
        failure_code = next(
            (
                str(item["failure_code"])
                for item in failed
                if isinstance(item.get("failure_code"), str)
                and item.get("failure_code")
            ),
            "selector_validation_failed",
        )
        _managed_stage(
            runtime,
            "validate_elements",
            "failed",
            failure_code=failure_code,
            attempt_count=max(
                (int(item["attempt_count"]) for item in failed), default=1
            ),
        )
        _managed_stage(runtime, "protect_or_recover", "passed")
        _managed_stage(runtime, "alert_and_cleanup", "passed")
        return _managed_result(
            "selector_validation_failed",
            failure_code=failure_code,
            element_results=element_results,
        )

    if not _managed_matrix_complete(validation):
        _managed_stage(
            runtime,
            "validate_elements",
            "failed",
            failure_code="validation_matrix_incomplete",
        )
        _managed_stage(runtime, "protect_or_recover", "failed")
        _managed_stage(runtime, "alert_and_cleanup", "passed")
        return _managed_result(
            "infrastructure_unavailable",
            failure_code="validation_matrix_incomplete",
            element_results=element_results,
        )
    _managed_stage(runtime, "validate_elements", "passed")

    if not publish:
        _managed_stage(runtime, "protect_or_recover", "passed")
        _managed_stage(runtime, "alert_and_cleanup", "passed")
        return _managed_result(
            "healthy",
            validation_evidence=validation,
            element_results=element_results,
        )

    try:
        promoted = methods["promote_saved_fallbacks"](candidate, validation)
        bundle = methods["prepare_publication"](promoted, validation)
        publication = methods["store_and_publish"](bundle, validation)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        failure_code = _safe_error_code(error)
        if failure_code == "probe_unavailable":
            failure_code = "publication_unavailable"
        _managed_stage(
            runtime,
            "protect_or_recover",
            "failed",
            failure_code=failure_code,
        )
        _managed_stage(runtime, "alert_and_cleanup", "passed")
        return _managed_result(
            "publication_failed",
            failure_code=failure_code,
            element_results=element_results,
        )

    if not isinstance(publication, Mapping):
        publication = {}
    version = publication.get("version")
    if (
        publication.get("published") is not True
        or publication.get("reconciled") is not True
        or not isinstance(version, str)
        or not version
    ):
        _managed_stage(
            runtime,
            "protect_or_recover",
            "failed",
            failure_code="publication_incomplete",
        )
        _managed_stage(runtime, "alert_and_cleanup", "passed")
        return _managed_result(
            "publication_failed",
            failure_code="publication_incomplete",
            element_results=element_results,
        )

    _managed_stage(runtime, "protect_or_recover", "passed")
    _managed_stage(runtime, "alert_and_cleanup", "passed")
    return _managed_result(
        "published",
        published=True,
        reconciled=True,
        new_version=version,
        validation_evidence=validation,
        element_results=element_results,
    )


__all__ = [
    "LEASE_HEARTBEAT_SECONDS",
    "LEASE_TTL_SECONDS",
    "ProbeLeaseLost",
    "run_managed_probe",
]
