import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest
from werkzeug.security import check_password_hash

from gateway import admin_users
from gateway import session_key
from gateway.management_db import open_management_db
from gateway.session_key import load_or_create_session_key


def test_session_key_is_created_once_and_reused(tmp_path):
    path = tmp_path / "private" / "session.key"

    first = load_or_create_session_key(path)
    second = load_or_create_session_key(path)

    assert first == second
    assert len(first) >= 80
    assert path.read_text(encoding="utf-8").strip() == first


def test_concurrent_session_key_creation_has_one_value_for_all_callers(tmp_path):
    path = tmp_path / "session.key"

    with ThreadPoolExecutor(max_workers=12) as executor:
        values = list(
            executor.map(
                lambda _index: load_or_create_session_key(path),
                range(24),
            )
        )

    assert len(set(values)) == 1
    assert path.read_text(encoding="utf-8").strip() == values[0]


def test_session_key_rejects_non_regular_and_symlink_paths(tmp_path):
    directory = tmp_path / "session.key"
    directory.mkdir()
    with pytest.raises(RuntimeError, match="session_key_not_regular"):
        load_or_create_session_key(directory)

    target = tmp_path / "target.key"
    target.write_text("x" * 80 + "\n", encoding="utf-8")
    link = tmp_path / "link.key"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
        load_or_create_session_key(link)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_session_key_rejects_windows_parent_junction(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    _create_windows_junction(junction, target)

    with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
        load_or_create_session_key(junction / "session.key")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction test")
def test_session_key_rejects_ancestor_junction_without_writing_outside(
    tmp_path,
):
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = tmp_path / "junction"
    _create_windows_junction(junction, outside)
    (outside / "nested").mkdir()
    outside_key = outside / "nested" / "session.key"

    with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
        load_or_create_session_key(
            junction / "nested" / "session.key",
        )

    assert not outside_key.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL test")
def test_session_key_acl_is_secure_before_first_write(tmp_path, monkeypatch):
    assert "(A;;FA;;;BA)" in session_key._SECURE_KEY_SDDL
    path = tmp_path / "session.key"
    original_write = session_key._write_new_key_to_descriptor
    checked = []

    def assert_secure_then_write(descriptor):
        assert session_key._windows_descriptor_permissions_are_secure(
            descriptor
        )
        checked.append(True)
        return original_write(descriptor)

    monkeypatch.setattr(
        session_key,
        "_write_new_key_to_descriptor",
        assert_secure_then_write,
    )

    load_or_create_session_key(path)

    assert checked == [True]


def test_session_key_rejects_invalid_existing_content(tmp_path):
    path = tmp_path / "session.key"
    for value, code in (
        ("", "session_key_empty"),
        ("short\n", "session_key_too_short"),
        ("x" * 80 + "\nembedded\n", "session_key_invalid_newline"),
    ):
        path.write_text(value, encoding="utf-8")
        with pytest.raises(RuntimeError, match=code):
            load_or_create_session_key(path)


def test_session_key_rejects_hardlinks(tmp_path):
    original = tmp_path / "original.key"
    original.write_text("x" * 80 + "\n", encoding="utf-8")
    linked = tmp_path / "session.key"
    os.link(original, linked)

    with pytest.raises(RuntimeError, match="session_key_hardlink"):
        load_or_create_session_key(linked)


def test_session_key_detects_or_blocks_target_swap(tmp_path, monkeypatch):
    path = tmp_path / "session.key"
    original_value = "o" * 80
    replacement_value = "r" * 80
    path.write_text(original_value + "\n", encoding="utf-8")
    replacement = tmp_path / "replacement.key"
    replacement.write_text(replacement_value + "\n", encoding="utf-8")
    original_read = session_key._read_key_from_descriptor
    swap_was_blocked = []

    def swap_then_read(descriptor):
        try:
            os.replace(replacement, path)
        except PermissionError:
            swap_was_blocked.append(True)
        return original_read(descriptor)

    monkeypatch.setattr(
        session_key,
        "_read_key_from_descriptor",
        swap_then_read,
    )

    if os.name != "nt":
        with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
            load_or_create_session_key(path)
        return
    try:
        result = load_or_create_session_key(path)
    except RuntimeError as error:
        assert str(error) == "session_key_unsafe_path"
        assert not swap_was_blocked
    else:
        assert result == original_value
        assert swap_was_blocked == [True]


def test_session_key_detects_or_blocks_parent_swap(tmp_path, monkeypatch):
    parent = tmp_path / "private"
    parent.mkdir()
    path = parent / "session.key"
    original_value = "o" * 80
    path.write_text(original_value + "\n", encoding="utf-8")
    parked_parent = tmp_path / "parked"
    original_read = session_key._read_key_from_descriptor
    swap_was_blocked = []

    def swap_then_read(descriptor):
        try:
            parent.rename(parked_parent)
            parent.mkdir()
            path.write_text("r" * 80 + "\n", encoding="utf-8")
        except PermissionError:
            swap_was_blocked.append(True)
        return original_read(descriptor)

    monkeypatch.setattr(
        session_key,
        "_read_key_from_descriptor",
        swap_then_read,
    )

    if os.name != "nt":
        with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
            load_or_create_session_key(path)
        return
    try:
        result = load_or_create_session_key(path)
    except RuntimeError as error:
        assert str(error) == "session_key_unsafe_path"
        assert not swap_was_blocked
    else:
        assert result == original_value
        assert swap_was_blocked == [True]


@pytest.mark.skipif(os.name == "nt", reason="POSIX descriptor cleanup test")
def test_session_key_created_inode_is_zeroed_if_renamed_outside(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session.key"
    outside = tmp_path / "outside.key"
    original_write = session_key._write_new_key_to_descriptor

    def write_then_rename(descriptor):
        value = original_write(descriptor)
        os.replace(path, outside)
        return value

    monkeypatch.setattr(
        session_key,
        "_write_new_key_to_descriptor",
        write_then_rename,
    )

    with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
        load_or_create_session_key(path)

    assert outside.read_bytes() == b""


@pytest.mark.skipif(os.name != "nt", reason="Windows raw handle test")
def test_session_key_cleans_raw_handle_if_crt_conversion_fails(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session.key"
    monkeypatch.setattr(
        session_key.msvcrt,
        "open_osfhandle",
        lambda *_args: (_ for _ in ()).throw(OSError("conversion failed")),
    )

    with pytest.raises(OSError, match="conversion failed"):
        load_or_create_session_key(path)

    assert not path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows parent identity test")
def test_session_key_parent_mismatch_deletes_empty_file_before_write(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session.key"
    writes = []
    monkeypatch.setattr(
        session_key,
        "_verify_windows_target_parent",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeError("session_key_unsafe_path")
        ),
    )
    monkeypatch.setattr(
        session_key,
        "_write_new_key_to_descriptor",
        lambda *_args: writes.append(True),
    )

    with pytest.raises(RuntimeError, match="session_key_unsafe_path"):
        load_or_create_session_key(path)

    assert writes == []
    assert not path.exists()


def test_session_key_permission_failure_is_fatal_and_removes_new_file(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session.key"
    monkeypatch.setattr(
        os,
        "chmod",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("denied")
        ),
    )

    with pytest.raises(RuntimeError, match="session_key_permissions"):
        load_or_create_session_key(path)

    assert not path.exists()


def test_create_admin_reads_hidden_password_twice_and_uses_scrypt(
    tmp_path,
    monkeypatch,
    capsys,
):
    prompts = []
    password = "correct horse battery staple"
    values = iter((password, password))
    monkeypatch.setattr(
        admin_users.getpass,
        "getpass",
        lambda prompt: prompts.append(prompt) or next(values),
    )
    database = tmp_path / "management.db"

    code = admin_users.main(
        [
            "create-admin",
            "--username",
            "admin",
            "--database",
            str(database),
        ]
    )

    assert code == 0
    assert prompts == ["Password: ", "Confirm password: "]
    captured = capsys.readouterr()
    assert captured.out == "Administrator created: admin\n"
    assert captured.err == ""
    assert password not in captured.out + captured.err
    connection = open_management_db(database)
    row = connection.execute(
        "SELECT password_hash, must_change_password FROM management_users"
    ).fetchone()
    assert row["password_hash"].startswith("scrypt:")
    assert check_password_hash(row["password_hash"], password)
    assert row["must_change_password"] == 0
    audit = connection.execute(
        "SELECT details_json FROM management_audit_events"
    ).fetchone()["details_json"]
    assert password not in audit
    assert row["password_hash"] not in audit
    connection.close()


def test_cli_rejects_password_arguments_without_echoing_value(capsys):
    password = "secret-on-command-line"

    with pytest.raises(SystemExit) as caught:
        admin_users.main(
            [
                "create-admin",
                "--username",
                "admin",
                "--password",
                password,
            ]
        )

    assert caught.value.code != 0
    captured = capsys.readouterr()
    assert password not in captured.out + captured.err


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (("short", "short"), "Password must contain at least 12 characters."),
        (
            ("long enough password", "different password"),
            "Passwords do not match.",
        ),
    ],
)
def test_cli_rejects_invalid_hidden_passwords_safely(
    tmp_path,
    monkeypatch,
    capsys,
    values,
    message,
):
    prompts = iter(values)
    monkeypatch.setattr(
        admin_users.getpass,
        "getpass",
        lambda _prompt: next(prompts),
    )

    code = admin_users.main(
        [
            "create-admin",
            "--username",
            "admin",
            "--database",
            str(tmp_path / "management.db"),
        ]
    )

    assert code != 0
    captured = capsys.readouterr()
    assert message in captured.err
    assert all(value not in captured.err for value in values)


def _create_windows_junction(link, target):
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("junction creation is unavailable")
