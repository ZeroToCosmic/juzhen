"""Agent local WAL tests (M4 increment 4, PRD 15.3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.wal import (
    STAGE_RUNNING,
    STAGE_STARTING,
    STAGE_SUBMITTING,
    STAGE_VERIFYING,
    WindowWal,
)


def _wal(tmp_path: Path) -> WindowWal:
    return WindowWal(tmp_path / "windows.wal.json")


def test_set_stage_persists_and_reads_back(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", STAGE_RUNNING, meta={"profile": "p-1"})
    entry = wal.get("st-1")
    assert entry["stage"] == STAGE_RUNNING
    assert entry["meta"]["profile"] == "p-1"
    assert entry["generation"] == 0
    assert "updated_at" in entry


def test_generation_persisted(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", STAGE_STARTING)
    wal.set_generation("st-1", 3)
    assert wal.get("st-1")["generation"] == 3


def test_clear_removes_entry(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", STAGE_RUNNING)
    wal.clear("st-1")
    assert wal.get("st-1") is None


def test_recover_abandons_new_and_starting(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", "NEW")
    wal.set_stage("st-2", STAGE_STARTING)
    decisions = {d["subtask_id"]: d["action"] for d in wal.recover()}
    assert decisions == {"st-1": "abandon", "st-2": "abandon"}


def test_recover_aborts_running_as_retryable(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", STAGE_RUNNING)
    decisions = wal.recover()
    assert decisions[0]["action"] == "aborted"
    assert decisions[0]["retryable"] is True


def test_recover_marks_submitting_verifying_unverified(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", STAGE_SUBMITTING)
    wal.set_stage("st-2", STAGE_VERIFYING)
    decisions = {d["subtask_id"]: d for d in wal.recover()}
    assert decisions["st-1"]["action"] == "unverified"
    assert decisions["st-1"]["retryable"] is False
    assert decisions["st-2"]["action"] == "unverified"


def test_recover_done_noop(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", "DONE")
    assert wal.recover()[0]["action"] == "done"


def test_wal_survives_recreation(tmp_path):
    wal = _wal(tmp_path)
    wal.set_stage("st-1", STAGE_VERIFYING)
    reloaded = _wal(tmp_path)
    assert reloaded.get("st-1")["stage"] == STAGE_VERIFYING
