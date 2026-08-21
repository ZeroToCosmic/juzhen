from __future__ import annotations

from pathlib import Path

from scripts import scan_repository_secrets


def _write(path: Path, text: str = "safe\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _relative_paths(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in scan_repository_secrets.discover_text_files(root)
    }


def test_discovery_walks_the_repository_root_and_includes_future_text_artifacts(tmp_path):
    expected = {
        "future_root_module.py",
        "root-runtime.log",
        "package.json",
        "package-lock.json",
        ".env.example",
        "scripts/scan_repository_secrets.py",
        "docs/guide.md",
        "tests/test_future.py",
        ".superpowers/sdd/report.md",
    }
    for relative in expected:
        _write(tmp_path / relative)

    assert expected <= _relative_paths(tmp_path)


def test_discovery_excludes_only_declared_dependencies_test_work_and_runtime_secrets(tmp_path):
    included = _write(tmp_path / "source.py")
    excluded = {
        ".venv/lib/site.py",
        "node_modules/pkg/index.js",
        ".git/config",
        "services/tiktok_api/vendor/upstream.py",
        "work/unreadable/generated.py",
        "data/stats/tiktok_cookie.json",
        "data/stats/tiktok_cookie.json.lock",
        "data/stats/tiktok_stats.db",
        "config.json",
        ".env",
    }
    for relative in excluded:
        _write(tmp_path / relative)

    discovered = _relative_paths(tmp_path)

    assert included.relative_to(tmp_path).as_posix() in discovered
    assert discovered.isdisjoint(excluded)


def test_scan_failure_names_file_and_rule_without_echoing_secret(tmp_path, capsys):
    secret = "Bearer " + ("Z" * 32)
    _write(tmp_path / "future_root_module.py", f'HEADER = "{secret}"\n')

    result = scan_repository_secrets.main(tmp_path)
    output = capsys.readouterr()

    assert result == 1
    assert "future_root_module.py: bearer-credential" in output.err
    assert secret not in output.out + output.err


def test_binary_and_runtime_ciphertext_are_not_scanned(tmp_path, capsys):
    secret = "Bearer " + ("Y" * 32)
    binary_path = tmp_path / "fixture.bin"
    binary_path.write_bytes(secret.encode("ascii") + b"\x00\xff")
    _write(tmp_path / "data/stats/tiktok_cookie.json", secret)
    _write(tmp_path / "safe.py")

    result = scan_repository_secrets.main(tmp_path)
    output = capsys.readouterr()

    assert result == 0
    assert "secret scan passed:" in output.out
    assert secret not in output.out + output.err


def test_nested_config_json_source_and_fixtures_are_discovered_and_scanned(tmp_path, capsys):
    secret = "Bearer " + ("Q" * 32)
    _write(tmp_path / "config.json", secret)
    _write(tmp_path / "config.json.backup.1", secret)
    _write(tmp_path / "config.json.corrupt", secret)
    _write(tmp_path / "docs/config.json", secret)
    _write(tmp_path / "tests/fixtures/config.json", secret)

    discovered = _relative_paths(tmp_path)
    result = scan_repository_secrets.main(tmp_path)
    output = capsys.readouterr()

    assert "config.json" not in discovered
    assert "config.json.backup.1" not in discovered
    assert "config.json.corrupt" not in discovered
    assert {"docs/config.json", "tests/fixtures/config.json"} <= discovered
    assert result == 1
    assert "docs/config.json: bearer-credential" in output.err
    assert "tests/fixtures/config.json: bearer-credential" in output.err
    assert secret not in output.out + output.err
