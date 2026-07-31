"""Fail without echoing values when repository text contains credential literals."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterator, TextIO


ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".cjs",
    ".cmd",
    ".conf",
    ".css",
    ".csv",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".jsx",
    ".log",
    ".md",
    ".mjs",
    ".ps1",
    ".psm1",
    ".py",
    ".rst",
    ".scss",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsv",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
TEXT_NAMES = {
    ".env.example",
    ".env.sample",
    "Dockerfile",
    "LICENSE",
    "Makefile",
}
EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".npm-cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "vendor",
    "work",
}

PATTERNS = (
    (
        "tiktok-cookie-literal",
        re.compile(
            r"(?i)\b(?:sessionid|sid_tt|uid_tt|ttwid|msToken|odin_tt)\s*=\s*"
            r"[A-Za-z0-9%._~+/-]{8,}"
        ),
    ),
    (
        "bearer-credential",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{24,}"),
    ),
    (
        "private-key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "provider-key",
        re.compile(r"\b(?:sk|xai|ghp|github_pat)_[A-Za-z0-9_-]{20,}\b"),
    ),
)


def discover_text_files(root: str | os.PathLike[str] = ROOT) -> Iterator[Path]:
    """Yield every repository-owned text candidate under ``root`` in stable order."""
    repository_root = Path(root).resolve()
    for directory, directory_names, file_names in os.walk(
        repository_root, topdown=True, onerror=lambda _error: None
    ):
        current = Path(directory)
        directory_names[:] = sorted(
            name for name in directory_names if name not in EXCLUDED_DIRECTORY_NAMES
        )
        for name in sorted(file_names):
            path = current / name
            relative = path.relative_to(repository_root)
            if _excluded_runtime_file(relative):
                continue
            if name in TEXT_NAMES or path.suffix.lower() in TEXT_SUFFIXES:
                yield path


def _excluded_runtime_file(relative: Path) -> bool:
    normalized = relative.as_posix().lower()
    if normalized in {".env", "config.json"}:
        return True
    if "/" not in normalized and (
        normalized.startswith("config.json.backup.")
        or normalized.startswith("config.json.corrupt")
    ):
        return True
    if normalized.startswith("data/stats/tiktok_cookie.json"):
        return True
    if normalized.startswith("data/stats/tiktok_stats.db"):
        return True
    if normalized.startswith("data/tiktok_stats/cookie-secret.json"):
        return True
    if normalized.startswith("data/tiktok_stats/.cookie-secret."):
        return True
    return False


def scan_repository(root: str | os.PathLike[str] = ROOT) -> tuple[int, list[tuple[str, str]]]:
    repository_root = Path(root).resolve()
    findings: list[tuple[str, str]] = []
    scanned = 0
    for path in discover_text_files(repository_root):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\x00" in text:
            continue
        scanned += 1
        for name, pattern in PATTERNS:
            if pattern.search(text):
                findings.append((path.relative_to(repository_root).as_posix(), name))
    return scanned, findings


def main(
    root: str | os.PathLike[str] = ROOT,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    output = stdout or sys.stdout
    error_output = stderr or sys.stderr
    scanned, findings = scan_repository(root)
    if findings:
        for relative_path, name in findings:
            print(f"{relative_path}: {name}", file=error_output)
        print(f"secret scan failed: {len(findings)} finding(s)", file=error_output)
        return 1
    print(f"secret scan passed: {scanned} text files", file=output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
