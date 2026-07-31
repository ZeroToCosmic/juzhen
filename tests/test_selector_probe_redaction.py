from __future__ import annotations

import asyncio
import os

import pytest

import selector_probe.redaction as redaction_module
from selector_probe.redaction import (
    capture_redacted_screenshot,
    delete_evidence_file,
    redact_evidence,
)


def test_alert_payload_removes_profiles_cdp_secrets_and_comment_text():
    result = redact_evidence(
        {
            "profile_id": "profile-complete-secret",
            "cdp_url": "ws://127.0.0.1/devtools/browser/secret",
            "authorization": "Bearer token",
            "comment_text": "private comment",
            "nested": {
                "accessibility_tree": {"name": "private"},
                "code": "selector_validation_failed",
            },
        },
        profile_ids=("profile-complete-secret",),
    )
    text = str(result)

    assert "profile-complete-secret" not in text
    assert "devtools/browser" not in text
    assert "Bearer token" not in text
    assert "private comment" not in text
    assert "selector_validation_failed" in text


def test_profile_ids_are_masked_inside_strings_and_dictionary_keys():
    result = redact_evidence(
        {
            "prefix-profile-long-secret-suffix": (
                "profile-long-secret/profile"
            ),
        },
        profile_ids=("profile", "profile-long-secret"),
    )
    text = str(result)

    assert "profile-long-secret" not in text
    assert "profile" not in text


def test_profile_id_is_masked_inside_unknown_object_repr():
    class Unknown:
        def __str__(self):
            return "failed for profile-long-secret"

    result = redact_evidence(
        {"value": Unknown()},
        profile_ids=("profile-long-secret",),
    )

    assert result["value"] == "failed for ***cret"


def test_redaction_enforces_collection_and_string_budgets():
    result = redact_evidence(
        {
            "items": list(range(60)),
            "long": "x" * 600,
            **{f"key_{index}": index for index in range(110)},
        },
        profile_ids=(),
    )

    assert len(result) == 100
    assert len(result["items"]) == 50
    assert len(result["long"]) == 500


class FakePage:
    def __init__(self):
        self.evaluate_calls = []
        self.screenshot_kwargs = None

    async def evaluate(self, script, argument=None):
        self.evaluate_calls.append((script, argument))
        return None

    async def screenshot(self, **kwargs):
        self.screenshot_kwargs = kwargs
        # Minimal marker-only JPEG without APP1 or COM metadata.
        return b"\xff\xd8\xff\xd9"


def test_screenshot_uses_opaque_overlays_and_never_writes_raw_image(
    tmp_path,
):
    page = FakePage()
    target = tmp_path / "redacted.jpg"

    result = asyncio.run(
        capture_redacted_screenshot(
            page,
            regions=({"x": 10, "y": 20, "width": 30, "height": 40},),
            target_path=target,
            evidence_root=tmp_path,
        )
    )

    assert result == target
    assert target.read_bytes() == b"\xff\xd8\xff\xd9"
    assert page.screenshot_kwargs == {"type": "jpeg", "quality": 70}
    assert page.evaluate_calls[0][1][0] == {
        "x": 6.0,
        "y": 16.0,
        "width": 38.0,
        "height": 48.0,
    }
    assert "position = 'fixed'" in page.evaluate_calls[0][0]
    assert "background = '#000'" in page.evaluate_calls[0][0]
    assert "remove()" in page.evaluate_calls[-1][0]


def test_screenshot_rejects_nested_evidence_path(tmp_path):
    page = FakePage()

    with pytest.raises(ValueError, match="direct children"):
        asyncio.run(
            capture_redacted_screenshot(
                page,
                regions=(),
                target_path=tmp_path / "swappable-parent" / "evidence.jpg",
                evidence_root=tmp_path,
            )
        )

    assert page.screenshot_kwargs is None


def test_root_swap_race_never_creates_evidence_outside_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "evidence"
    outside = tmp_path / "outside"
    parked = tmp_path / "parked"
    root.mkdir()
    outside.mkdir()
    page = FakePage()

    if os.name == "nt":
        original = redaction_module._windows_open_target

        def racing_open(root_handle, opened_root, target, *, create):
            root.rename(parked)
            outside.rename(root)
            return original(
                root_handle,
                opened_root,
                target,
                create=create,
            )

        monkeypatch.setattr(
            redaction_module,
            "_windows_open_target",
            racing_open,
        )
        with pytest.raises(ValueError, match="target changed"):
            asyncio.run(
                capture_redacted_screenshot(
                    page,
                    regions=(),
                    target_path=root / "race.jpg",
                    evidence_root=root,
                )
            )
        root.rename(outside)
        parked.rename(root)
        assert not (root / "race.jpg").exists()
    else:
        original = redaction_module._posix_open_target

        def racing_open(root_descriptor, name):
            root.rename(parked)
            outside.rename(root)
            return original(root_descriptor, name)

        monkeypatch.setattr(
            redaction_module,
            "_posix_open_target",
            racing_open,
        )
        with pytest.raises(ValueError, match="root changed"):
            asyncio.run(
                capture_redacted_screenshot(
                    page,
                    regions=(),
                    target_path=root / "race.jpg",
                    evidence_root=root,
                )
            )
        root.rename(outside)
        parked.rename(root)

    assert not (outside / "race.jpg").exists()


def test_root_swap_race_never_deletes_file_outside_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "evidence"
    outside = tmp_path / "outside"
    parked = tmp_path / "parked"
    root.mkdir()
    outside.mkdir()
    (root / "expired.jpg").write_bytes(b"inside")
    (outside / "expired.jpg").write_bytes(b"outside")

    if os.name == "nt":
        original = redaction_module._windows_open_target

        def racing_open(root_handle, opened_root, target, *, create):
            root.rename(parked)
            outside.rename(root)
            return original(
                root_handle,
                opened_root,
                target,
                create=create,
            )

        monkeypatch.setattr(
            redaction_module,
            "_windows_open_target",
            racing_open,
        )
        with pytest.raises(ValueError, match="target changed"):
            delete_evidence_file(root, root / "expired.jpg")
        root.rename(outside)
        parked.rename(root)
        assert (root / "expired.jpg").exists()
    else:
        original = redaction_module._posix_delete_target

        def racing_delete(root_descriptor, name, opened_root, identity):
            root.rename(parked)
            outside.rename(root)
            return original(
                root_descriptor,
                name,
                opened_root,
                identity,
            )

        monkeypatch.setattr(
            redaction_module,
            "_posix_delete_target",
            racing_delete,
        )
        with pytest.raises(ValueError, match="root changed"):
            delete_evidence_file(root, root / "expired.jpg")
        root.rename(outside)
        parked.rename(root)
        assert (root / "expired.jpg").exists()

    assert (outside / "expired.jpg").read_bytes() == b"outside"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle sharing only")
def test_open_write_target_cannot_move_outside_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "evidence"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / "race.jpg"
    moved = outside / target.name
    move_attempts = []
    original = redaction_module._windows_open_target

    def racing_open(root_handle, opened_root, checked, *, create):
        handle = original(
            root_handle,
            opened_root,
            checked,
            create=create,
        )
        try:
            checked.rename(moved)
        except OSError:
            move_attempts.append("blocked")
        else:
            move_attempts.append("moved")
        return handle

    monkeypatch.setattr(
        redaction_module,
        "_windows_open_target",
        racing_open,
    )

    asyncio.run(
        capture_redacted_screenshot(
            FakePage(),
            regions=(),
            target_path=target,
            evidence_root=root,
        )
    )

    assert move_attempts == ["blocked"]
    assert target.read_bytes() == b"\xff\xd8\xff\xd9"
    assert not moved.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle sharing only")
def test_open_delete_target_cannot_move_outside_root(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "evidence"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = root / "expired.jpg"
    moved = outside / target.name
    target.write_bytes(b"inside")
    move_attempts = []
    original = redaction_module._windows_dispose

    def racing_dispose(handle):
        try:
            target.rename(moved)
        except OSError:
            move_attempts.append("blocked")
        else:
            move_attempts.append("moved")
        return original(handle)

    monkeypatch.setattr(
        redaction_module,
        "_windows_dispose",
        racing_dispose,
    )

    assert delete_evidence_file(root, target) is True
    assert move_attempts == ["blocked"]
    assert not target.exists()
    assert not moved.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor semantics only")
def test_posix_create_conflict_closes_root_descriptor(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "evidence"
    root.mkdir()
    target = root / "existing.jpg"
    target.write_bytes(b"existing")
    closed = []

    monkeypatch.setattr(
        redaction_module,
        "_posix_open_root",
        lambda _root: (12345, (1, 2)),
    )

    def conflict(_root_descriptor, _name):
        raise FileExistsError("already exists")

    monkeypatch.setattr(
        redaction_module,
        "_posix_open_target",
        conflict,
    )
    monkeypatch.setattr(redaction_module.os, "close", closed.append)

    with pytest.raises(FileExistsError):
        redaction_module._write_new_evidence(root, target, b"new")

    assert closed == [12345]
