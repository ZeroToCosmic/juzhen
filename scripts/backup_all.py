"""One-shot full local backup for the Windows automation system.

Covers: SQLite consistency snapshots, Redis state, config.json, .env,
data/ (content, cookie ciphertext, evidence), and logs. Writes to an
external backup root with a timestamped directory and a manifest.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BACKUP_ROOT = Path(
    os.environ.get(
        "BACKUP_ROOT",
        r"C:\Users\burn1ng\Documents\Codex\backups",
    )
)
DEFAULT_KEEP = 10
REDIS_CONTAINER = os.environ.get("REDIS_CONTAINER", "local-redis")
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
APP_CONFIG_PATH = Path(
    os.environ.get("APP_CONFIG_PATH", PROJECT_ROOT / "config.json")
)
DOTENV_PATH = PROJECT_ROOT / ".env"
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
IGNORED_DIRS = {"__pycache__", ".pytest_cache"}
IGNORED_FILES = {".db-wal", ".db-shm"}


def timestamped_dir(root: Path) -> Path:
    stamp = _dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return root / stamp


def sqlite_snapshot(dst_dir: Path) -> list[dict]:
    results: list[dict] = []
    for src in sorted(DATA_DIR.rglob("*.db")):
        rel_parent = src.parent.relative_to(DATA_DIR)
        name = src.name if str(rel_parent) == "." else f"{rel_parent}_{src.name}"
        dst = dst_dir / "db" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        entry = {"source": str(src.relative_to(PROJECT_ROOT)), "target": str(dst.relative_to(dst_dir))}
        try:
            src_conn = sqlite3.connect(str(src))
            dst_conn = sqlite3.connect(str(dst))
            src_conn.backup(dst_conn)
            dst_conn.close()
            src_conn.close()
            status = sqlite3.connect(str(dst)).execute("PRAGMA integrity_check").fetchone()[0]
            entry["size"] = dst.stat().st_size
            entry["integrity"] = status
            entry["status"] = "ok"
        except Exception as error:
            entry["status"] = "failed"
            entry["error"] = str(error)
        results.append(entry)
    return results


def copy_tree_tolerant(src: Path, dst: Path) -> dict:
    copied = 0
    skipped = 0
    warnings: list[str] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        rel = Path(root).relative_to(src)
        target = dst / rel
        target.mkdir(parents=True, exist_ok=True)
        for name in files:
            if any(name.endswith(ext) for ext in IGNORED_FILES):
                continue
            source_file = Path(root) / name
            target_file = target / name
            try:
                shutil.copy2(source_file, target_file)
                copied += 1
            except OSError as error:
                warnings.append(f"{source_file}: {error}")
                skipped += 1
    return {"copied": copied, "skipped": skipped, "warnings": warnings}


def backup_redis(dst_dir: Path) -> dict:
    result: dict = {"method": None, "status": "failed"}
    redis_dir = dst_dir / "redis"
    redis_dir.mkdir(parents=True, exist_ok=True)
    rdb_dst = redis_dir / "dump.rdb"
    try:
        subprocess.run(
            ["docker", "exec", REDIS_CONTAINER, "redis-cli", "SAVE"],
            check=True,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["docker", "cp", f"{REDIS_CONTAINER}:/data/dump.rdb", str(rdb_dst)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        result = {
            "method": "rdb",
            "status": "ok",
            "target": str(rdb_dst.relative_to(dst_dir)),
            "size": rdb_dst.stat().st_size,
        }
        return result
    except Exception as error:
        result["rdb_error"] = str(error)
    try:
        import redis  # type: ignore

        client = redis.Redis.from_url(REDIS_URL, socket_timeout=10)
        client.ping()
        data: dict[str, dict] = {}
        for key in client.scan_iter(count=500):
            kind = client.type(key).decode("utf-8", "replace")
            if kind == "string":
                data[key] = {"type": kind, "value": client.get(key).decode("utf-8", "replace")}
            elif kind == "hash":
                raw = client.hgetall(key)
                data[key] = {
                    "type": kind,
                    "value": {
                        k.decode("utf-8", "replace"): v.decode("utf-8", "replace")
                        for k, v in raw.items()
                    },
                }
            elif kind == "list":
                data[key] = {
                    "type": kind,
                    "value": [v.decode("utf-8", "replace") for v in client.lrange(key, 0, -1)],
                }
            elif kind == "set":
                data[key] = {
                    "type": kind,
                    "value": [v.decode("utf-8", "replace") for v in client.smembers(key)],
                }
            elif kind == "zset":
                data[key] = {
                    "type": kind,
                    "value": [
                        [m.decode("utf-8", "replace"), s]
                        for m, s in client.zrange(key, 0, -1, withscores=True)
                    ],
                }
            else:
                data[key] = {"type": kind, "value": None}
        json_dst = redis_dir / "keys.json"
        json_dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        result = {
            "method": "keys-json",
            "status": "ok",
            "target": str(json_dst.relative_to(dst_dir)),
            "size": json_dst.stat().st_size,
            "keys": len(data),
        }
    except Exception as error:
        result["json_error"] = str(error)
    return result


def git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def prune_old(root: Path, keep: int) -> list[str]:
    if keep <= 0:
        return []
    candidates = sorted(
        (entry for entry in root.iterdir() if entry.is_dir()),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )
    removed: list[str] = []
    for entry in candidates[keep:]:
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry.name)
    return removed


def write_restore_guide(dst_dir: Path) -> None:
    guide = f"""# 备份恢复指南

备份时间：{_dt.datetime.now().isoformat(timespec="seconds")}
Git 提交：{git_commit() or "unknown"}

## 恢复顺序（先停 Flask 与全部 Worker）

1. 数据库（首选 db/ 一致性快照）：
   db/ 下每个文件对应 data/ 下的源库。复制到原位置并删除对应的 -wal/-shm 文件。
   例如：db/comment_campaign_comment_campaign.db -> data/comment_campaign/comment_campaign.db
2. config.json：覆盖项目根 config.json
3. dotenv：覆盖项目根 .env
4. session.key（如存在）：复制到 data/session.key，否则重新登录即可
5. data/ 目录内容（content、cookie 密文、evidence）按需覆盖
6. redis/dump.rdb 或 redis/keys.json：dump.rdb 需替换容器内 /data/dump.rdb 后重启容器；
   keys.json 是文本兜底，用于人工核对
7. 重启 launcher.py，检查 /healthz

## 代码回滚

代码备份依赖 git 检查点（main 之外的 checkpoint 分支），本脚本不打包源码。
"""
    (dst_dir / "RESTORE.md").write_text(guide, encoding="utf-8")


def main() -> int:
    args = sys.argv[1:]
    keep = DEFAULT_KEEP
    target = None
    for i, arg in enumerate(args):
        if arg == "--keep" and i + 1 < len(args):
            keep = int(args[i + 1])
        elif arg == "--target" and i + 1 < len(args):
            target = Path(args[i + 1])

    root = target or DEFAULT_BACKUP_ROOT
    root.mkdir(parents=True, exist_ok=True)
    dst_dir = timestamped_dir(root)
    dst_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        "project_root": str(PROJECT_ROOT),
        "sqlite": [],
        "redis": {"status": "skipped"},
        "files": {},
        "warnings": [],
    }

    manifest["sqlite"] = sqlite_snapshot(dst_dir)

    data_copy = copy_tree_tolerant(DATA_DIR, dst_dir / "data")
    manifest["files"]["data"] = data_copy["copied"]
    manifest["warnings"].extend(data_copy["warnings"])

    logs_copy = copy_tree_tolerant(LOGS_DIR, dst_dir / "logs")
    manifest["files"]["logs"] = logs_copy["copied"]
    manifest["warnings"].extend(logs_copy["warnings"])

    try:
        shutil.copy2(APP_CONFIG_PATH, dst_dir / "config.json")
        manifest["files"]["config.json"] = APP_CONFIG_PATH.stat().st_size
    except OSError as error:
        manifest["warnings"].append(f"config.json: {error}")

    try:
        shutil.copy2(DOTENV_PATH, dst_dir / "dotenv")
        manifest["files"]["dotenv"] = DOTENV_PATH.stat().st_size
    except OSError as error:
        manifest["warnings"].append(f"dotenv: {error}")

    manifest["redis"] = backup_redis(dst_dir)
    write_restore_guide(dst_dir)
    (dst_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    removed = prune_old(root, keep)

    sqlite_failures = sum(1 for entry in manifest["sqlite"] if entry["status"] != "ok")
    redis_ok = manifest["redis"].get("status") == "ok"
    failed = sqlite_failures > 0 or not redis_ok

    print(f"backup dir: {dst_dir}")
    print(f"sqlite: {len(manifest['sqlite'])} db, {sqlite_failures} failed")
    print(f"redis: {manifest['redis'].get('method')} status={manifest['redis'].get('status')}")
    print(f"data files copied: {data_copy['copied']}, skipped: {data_copy['skipped']}")
    print(f"logs files copied: {logs_copy['copied']}")
    for warning in manifest["warnings"]:
        print(f"WARN: {warning}")
    if removed:
        print(f"pruned old backups: {removed}")
    if failed:
        print("BACKUP COMPLETED WITH FAILURES")
        return 1
    print("BACKUP OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
