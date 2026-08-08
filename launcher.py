"""本地项目启动器：检查依赖、安装缺失依赖并启动 Flask 服务。"""

from __future__ import annotations

import asyncio
import ctypes
import importlib.metadata
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, W, X, StringVar, Tk, messagebox, ttk
from urllib.parse import quote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent
PYTHON_REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
NODE_PACKAGE_FILE = PROJECT_ROOT / "package.json"
APP_URL = "http://127.0.0.1:5000/"
SERVICE_LOG_DIR = PROJECT_ROOT / "data" / "logs"
UAC_STARTUP_ERROR = "无法获得管理员权限，请重试。"
ENVIRONMENT_STARTUP_ERROR = "启动器环境初始化失败，请重新安装项目依赖。"
GENERIC_STARTUP_ERROR = "启动器启动失败，请重试。"
SHUTDOWN_ERROR = "部分后台服务未能停止，请在任务管理器中检查。"


class AdminElevationError(RuntimeError):
    """Raised when Windows cannot create the elevated launcher process."""


def load_project_environment() -> None:
    """Load project environment only after native startup reporting is available."""
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")


def ensure_admin(*, is_admin=None, shell_execute=None) -> bool:
    if os.name != "nt":
        return True
    check_admin = is_admin or ctypes.windll.shell32.IsUserAnAdmin
    launch_elevated = shell_execute or ctypes.windll.shell32.ShellExecuteW
    if check_admin():
        return True

    launcher_path = Path(__file__).resolve()
    parameters = subprocess.list2cmdline([str(launcher_path)])
    try:
        result = launch_elevated(
            None,
            "runas",
            sys.executable,
            parameters,
            str(PROJECT_ROOT),
            1,
        )
    except Exception:
        raise AdminElevationError("UAC 管理员启动失败") from None
    if result <= 32:
        raise AdminElevationError(f"UAC 管理员启动失败，错误码：{result}")
    return False


def show_startup_error(message: str, *, native_box=None) -> None:
    show = native_box or ctypes.windll.user32.MessageBoxW
    show(None, message, "启动器启动失败", 0x10)


def hidden_process_options(platform_name: str | None = None) -> dict[str, int]:
    current_platform = os.name if platform_name is None else platform_name
    if current_platform != "nt":
        return {}
    return {"creationflags": subprocess.CREATE_NO_WINDOW}


def service_failure_detail(
    name: str,
    state: dict[str, int | bool | None],
    reason: str,
    log_path: Path,
) -> str:
    returncode = state.get("returncode")
    suffix = f"（退出码 {returncode}）" if returncode is not None else ""
    return f"{name}{suffix}：{reason}；日志：{log_path}"


def parse_listening_pids(output: str, port: int) -> list[int]:
    found = set()
    for line in output.splitlines():
        fields = line.split()
        if (
            len(fields) < 5
            or fields[0].upper() != "TCP"
            or fields[-2].upper() != "LISTENING"
        ):
            continue
        _, separator, port_text = fields[1].rpartition(":")
        if not separator or port_text != str(port) or not fields[-1].isdigit():
            continue
        found.add(int(fields[-1]))
    return sorted(found)


def stop_port_listeners(
    port: int,
    *,
    command_runner=None,
    sleep=None,
    current_pid=None,
    poll_attempts: int = 20,
) -> list[int]:
    runner = command_runner or subprocess.run
    pause = sleep or time.sleep
    own_pid = os.getpid() if current_pid is None else current_pid

    def listening() -> list[int]:
        completed = runner(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "无法查询端口占用")
        return [
            pid
            for pid in parse_listening_pids(completed.stdout, port)
            if pid != own_pid
        ]

    targets = listening()
    for pid in targets:
        completed = runner(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
        )
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(detail or f"无法停止 PID {pid}")

    for _ in range(poll_attempts):
        if not listening():
            return targets
        pause(0.1)
    raise RuntimeError(f"端口 {port} 未释放")


class FlaskServiceSupervisor:
    """Own exactly one Flask child process."""

    def __init__(self, popen_factory=None, log_path: Path | None = None):
        self._popen = popen_factory or subprocess.Popen
        self._process = None
        self._last_returncode = None
        self._lock = threading.RLock()
        self._log_path = log_path or SERVICE_LOG_DIR / "flask-service.log"
        self._log_handle = None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def start(self, *, environment=None):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            env = os.environ.copy() if environment is None else dict(environment)
            self._open_log()
            try:
                self._process = self._popen(
                    [sys.executable, str(PROJECT_ROOT / "app.py")],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=self._log_handle,
                    stderr=self._log_handle,
                    **hidden_process_options(),
                )
            except Exception:
                self._close_log_handle()
                raise
            self._last_returncode = None
            return self._process

    def state(self) -> dict[str, int | bool | None]:
        with self._lock:
            if self._process is None:
                return {
                    "running": False,
                    "pid": None,
                    "returncode": self._last_returncode,
                }
            returncode = self._process.poll()
            if returncode is not None:
                self._last_returncode = returncode
                self._close_log_handle()
            return {
                "running": returncode is None,
                "pid": self._process.pid if returncode is None else None,
                "returncode": self._last_returncode,
            }

    def stop(self, timeout: float = 10) -> None:
        with self._lock:
            process = self._process
            if process is None:
                self._close_log_handle()
                return
            try:
                returncode = process.poll()
                if returncode is None:
                    process.terminate()
                    try:
                        returncode = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        returncode = process.wait(timeout=timeout)
                self._last_returncode = returncode
                self._process = None
            finally:
                self._close_log_handle()

    def _open_log(self) -> None:
        self._close_log_handle()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self._log_path.open("wb", buffering=0)

    def _close_log_handle(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            handle.close()


class CommentCampaignWorkerSupervisor(FlaskServiceSupervisor):
    """Own the RQ Campaign worker process and its log handle."""

    def __init__(self, popen_factory=None, log_path: Path | None = None):
        super().__init__(
            popen_factory=popen_factory,
            log_path=log_path or SERVICE_LOG_DIR / "comment-campaign-worker.log",
        )

    def start(self, *, environment=None):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            env = os.environ.copy() if environment is None else dict(environment)
            self._open_log()
            try:
                self._process = self._popen(
                    [sys.executable, "-m", "comment_campaign.worker", "serve"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=self._log_handle,
                    stderr=self._log_handle,
                    **hidden_process_options(),
                )
            except Exception:
                self._close_log_handle()
                raise
            self._last_returncode = None
            return self._process


class StatisticsWorkerSupervisor:
    """Own exactly one separate statistics worker process."""

    def __init__(
        self,
        popen_factory=None,
        stop_file: Path | None = None,
        log_path: Path | None = None,
    ):
        self._popen = popen_factory or subprocess.Popen
        self._process = None
        self._last_returncode = None
        self._lock = threading.RLock()
        self._stop_file = stop_file or (
            PROJECT_ROOT / "data" / "stats" / f".worker-stop-{os.getpid()}-{uuid.uuid4().hex}"
        )
        self._log_path = log_path or SERVICE_LOG_DIR / "statistics-worker.log"
        self._log_handle = None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def start(self, *, environment=None):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            self._remove_stop_file()
            env = os.environ.copy() if environment is None else dict(environment)
            env["TIKTOK_STATS_STOP_FILE"] = str(self._stop_file)
            self._open_log()
            try:
                self._process = self._popen(
                    [sys.executable, "-m", "tiktok_stats.worker", "serve"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=self._log_handle,
                    stderr=self._log_handle,
                    **hidden_process_options(),
                )
            except Exception:
                self._close_log_handle()
                raise
            self._last_returncode = None
            return self._process

    def state(self) -> dict[str, int | bool | None]:
        with self._lock:
            if self._process is None:
                return {"running": False, "pid": None, "returncode": self._last_returncode}
            returncode = self._process.poll()
            if returncode is not None:
                self._last_returncode = returncode
                self._close_log_handle()
            return {
                "running": returncode is None,
                "pid": self._process.pid if returncode is None else None,
                "returncode": returncode,
            }

    def stop(self, timeout: float = 10) -> None:
        with self._lock:
            process = self._process
            if process is None:
                self._close_log_handle()
                return
            try:
                returncode = process.poll()
                if returncode is None:
                    self._request_stop()
                    try:
                        returncode = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            returncode = process.wait(timeout=timeout)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            returncode = process.wait(timeout=timeout)
                self._last_returncode = returncode
                self._process = None
                self._remove_stop_file()
            finally:
                self._close_log_handle()

    def _open_log(self) -> None:
        self._close_log_handle()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self._log_path.open("wb", buffering=0)

    def _close_log_handle(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            handle.close()

    def _request_stop(self) -> None:
        self._stop_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop_file.write_text("stop\n", encoding="utf-8")

    def _remove_stop_file(self) -> None:
        try:
            self._stop_file.unlink()
        except FileNotFoundError:
            pass


class SelectorProbeWorkerSupervisor:
    """Own exactly one separate selector probe worker process."""

    def __init__(
        self,
        popen_factory=None,
        stop_file: Path | None = None,
        log_path: Path | None = None,
    ):
        self._popen = popen_factory or subprocess.Popen
        self._process = None
        self._last_returncode = None
        self._lock = threading.RLock()
        self._stop_file = stop_file or (
            PROJECT_ROOT
            / "data"
            / "selector-probe"
            / f".worker-stop-{os.getpid()}-{uuid.uuid4().hex}"
        )
        self._log_path = log_path or SERVICE_LOG_DIR / "selector-probe-worker.log"
        self._log_handle = None

    @property
    def log_path(self) -> Path:
        return self._log_path

    def start(self, *, environment=None):
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return self._process
            self._remove_stop_file()
            env = os.environ.copy() if environment is None else dict(environment)
            env["SELECTOR_PROBE_STOP_FILE"] = str(self._stop_file)
            self._open_log()
            try:
                self._process = self._popen(
                    [sys.executable, "-m", "selector_probe.worker", "serve"],
                    cwd=PROJECT_ROOT,
                    env=env,
                    stdout=self._log_handle,
                    stderr=self._log_handle,
                    **hidden_process_options(),
                )
            except Exception:
                self._close_log_handle()
                raise
            self._last_returncode = None
            return self._process

    def state(self) -> dict[str, int | bool | None]:
        with self._lock:
            if self._process is None:
                return {
                    "running": False,
                    "pid": None,
                    "returncode": self._last_returncode,
                }
            returncode = self._process.poll()
            if returncode is not None:
                self._last_returncode = returncode
                self._close_log_handle()
            return {
                "running": returncode is None,
                "pid": self._process.pid if returncode is None else None,
                "returncode": returncode,
            }

    def stop(self, timeout: float = 10) -> None:
        with self._lock:
            process = self._process
            if process is None:
                self._close_log_handle()
                return
            stop_request_error = None
            try:
                returncode = process.poll()
                if returncode is None:
                    force_stop = False
                    try:
                        self._request_stop()
                    except Exception as exc:
                        stop_request_error = exc
                        force_stop = True
                    if not force_stop:
                        try:
                            returncode = process.wait(timeout=timeout)
                        except subprocess.TimeoutExpired:
                            force_stop = True
                    if force_stop:
                        process.terminate()
                        try:
                            returncode = process.wait(timeout=timeout)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            returncode = process.wait(timeout=timeout)
                self._last_returncode = returncode
                self._process = None
                self._remove_stop_file()
                if stop_request_error is not None:
                    raise stop_request_error
            finally:
                self._close_log_handle()

    def _open_log(self) -> None:
        self._close_log_handle()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self._log_path.open("wb", buffering=0)

    def _close_log_handle(self) -> None:
        handle = self._log_handle
        self._log_handle = None
        if handle is not None:
            handle.close()

    def _request_stop(self) -> None:
        self._stop_file.parent.mkdir(parents=True, exist_ok=True)
        self._stop_file.write_text("stop\n", encoding="utf-8")

    def _remove_stop_file(self) -> None:
        try:
            self._stop_file.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str
    blocking: bool = True


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    username: str
    password: str
    database: str


def database_url(config: DatabaseConfig, database: str | None = None) -> str:
    """根据 GUI 配置生成 asyncmy 连接字符串。"""

    target_database = database or config.database
    return (
        f"mysql+asyncmy://{quote(config.username, safe='')}:{quote(config.password, safe='')}"
        f"@{config.host}:{config.port}/{quote(target_database, safe='')}?charset=utf8mb4"
    )


def _validate_database_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("数据库名只能包含英文字母、数字和下划线")
    return name


def _mysql_service_name() -> str | None:
    """查找 MySQL Windows 服务名称。"""

    configured = os.getenv("MYSQL_SERVICE_NAME", "MySQL80").strip()
    candidates = [configured] if configured else []
    result = subprocess.run(
        ["sc.exe", "query", "type=", "service", "state=", "all"],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    discovered = re.findall(r"SERVICE_NAME:\s*(\S*mysql\S*)", result.stdout, re.IGNORECASE)
    candidates.extend(discovered)
    for name in dict.fromkeys(candidates):
        probe = subprocess.run(
            ["sc.exe", "query", name],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            check=False,
        )
        if probe.returncode == 0:
            return name
    return None


def _mysql_service_state(service_name: str) -> str:
    result = subprocess.run(
        ["sc.exe", "query", service_name],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return "MISSING"
    match = re.search(r"STATE\s*:\s*\d+\s+(\w+)", result.stdout)
    return match.group(1).upper() if match else "UNKNOWN"


def ensure_mysql_service(auto_start: bool = True) -> CheckResult:
    """检查并尝试启动 MySQL Windows 服务。"""

    if os.name != "nt":
        return CheckResult("MySQL 服务", True, "当前系统不是 Windows，跳过 Windows 服务检查", blocking=False)
    service_name = _mysql_service_name()
    if not service_name:
        return CheckResult("MySQL 服务", False, "未找到 MySQL Windows 服务，请先安装 MySQL Server")
    state = _mysql_service_state(service_name)
    if state == "RUNNING":
        return CheckResult("MySQL 服务", True, f"{service_name} 正在运行")
    if state != "STOPPED" or not auto_start:
        return CheckResult("MySQL 服务", False, f"{service_name} 当前状态：{state}")

    start_result = subprocess.run(
        ["sc.exe", "start", service_name],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        check=False,
    )
    if start_result.returncode != 0:
        return CheckResult(
            "MySQL 服务",
            False,
            f"启动失败，请使用管理员权限运行 GUI：{start_result.stderr.strip() or start_result.stdout.strip()}",
        )
    for _ in range(15):
        if _mysql_service_state(service_name) == "RUNNING":
            return CheckResult("MySQL 服务", True, f"{service_name} 已自动启动")
        time.sleep(1)
    return CheckResult("MySQL 服务", False, f"{service_name} 启动超时")


async def _database_server_check(config: DatabaseConfig) -> CheckResult:
    from sqlalchemy import text
    from database import create_database_engine

    engine = create_database_engine(database_url(config, "mysql"), pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            version_row = (
                await connection.execute(
                    text("SELECT VERSION() AS version, @@default_storage_engine AS default_engine")
                )
            ).mappings().one()
            innodb_support = (
                await connection.execute(
                    text(
                        "SELECT SUPPORT FROM information_schema.ENGINES "
                        "WHERE ENGINE = 'InnoDB'"
                    )
                )
            ).scalar_one_or_none()
        version = str(version_row["version"])
        version_match = re.search(r"(\d+)\.(\d+)", version)
        if not version_match or int(version_match.group(1)) < 8 or "mariadb" in version.lower():
            return CheckResult("MySQL", False, f"版本不满足 MySQL 8+：{version}")
        if str(innodb_support or "").upper() not in {"YES", "DEFAULT"}:
            return CheckResult("InnoDB", False, "当前 MySQL 不支持 InnoDB")
        default_engine = str(version_row["default_engine"])
        if default_engine.upper() != "INNODB":
            return CheckResult("InnoDB", False, f"默认引擎为 {default_engine}，不是 InnoDB")
        return CheckResult("MySQL / InnoDB", True, f"MySQL {version}，默认引擎为 InnoDB")
    finally:
        await engine.dispose()


async def provision_database(config: DatabaseConfig) -> list[str]:
    """创建数据库、检查 InnoDB 并创建当前项目的 ORM 表。"""

    from sqlalchemy import text
    from database import Base, create_database_engine

    database_name = _validate_database_name(config.database)
    server_engine = create_database_engine(database_url(config, "mysql"), pool_size=1, max_overflow=0)
    try:
        async with server_engine.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
            )
    finally:
        await server_engine.dispose()

    project_engine = create_database_engine(database_url(config), pool_size=1, max_overflow=0)
    try:
        async with project_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    finally:
        await project_engine.dispose()
    return [f"数据库 {database_name} 已创建", "Account 表结构已初始化"]


async def set_default_innodb(config: DatabaseConfig) -> str:
    """设置当前 MySQL 实例的默认存储引擎（重启后是否保留取决于 my.ini）。"""

    from sqlalchemy import text
    from database import create_database_engine

    engine = create_database_engine(database_url(config, "mysql"), pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET GLOBAL default_storage_engine = 'InnoDB'"))
        return "默认存储引擎已设置为 InnoDB（重启后请重新检查）"
    finally:
        await engine.dispose()


def _requirement_names() -> list[str]:
    """读取 requirements.txt 中的发行包名称。"""

    names: list[str] = []
    if not PYTHON_REQUIREMENTS.exists():
        return names
    for line in PYTHON_REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith(("-", "git+")):
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9_.-]*)", line)
        if match:
            names.append(match.group(1))
    return names


def _distribution_installed(name: str) -> bool:
    try:
        importlib.metadata.version(name)
        return True
    except importlib.metadata.PackageNotFoundError:
        return False


def check_python_dependencies() -> list[CheckResult]:
    results = []
    for name in _requirement_names():
        results.append(
            CheckResult(
                f"Python: {name}",
                _distribution_installed(name),
                "已安装" if _distribution_installed(name) else "缺失",
            )
        )
    if not results:
        results.append(CheckResult("requirements.txt", False, "文件不存在或没有有效依赖"))
    return results


def _node_dependencies() -> list[str]:
    if not NODE_PACKAGE_FILE.exists():
        return []
    package = json.loads(NODE_PACKAGE_FILE.read_text(encoding="utf-8"))
    return sorted(package.get("dependencies", {}).keys())


def check_node_dependencies() -> list[CheckResult]:
    dependencies = _node_dependencies()
    if not dependencies:
        return [CheckResult("Node package.json", True, "没有声明运行时依赖", blocking=False)]
    if not shutil.which("node") or not shutil.which("npm"):
        return [CheckResult("Node.js / npm", False, "未找到 node 或 npm")]

    results = []
    for name in dependencies:
        dependency_path = PROJECT_ROOT / "node_modules" / Path(name)
        results.append(
            CheckResult(
                f"Node: {name}",
                (dependency_path / "package.json").exists(),
                "已安装" if (dependency_path / "package.json").exists() else "缺失",
            )
        )
    return results


async def _check_mysql_connection(database_url: str) -> CheckResult:
    from sqlalchemy import text

    from database import create_database_engine

    engine = create_database_engine(database_url, pool_size=1, max_overflow=0)
    try:
        async with engine.connect() as connection:
            version_row = (
                await connection.execute(
                    text("SELECT VERSION() AS version, @@default_storage_engine AS default_engine")
                )
            ).mappings().one()
            innodb_row = (
                await connection.execute(
                    text(
                        "SELECT SUPPORT FROM information_schema.ENGINES "
                        "WHERE ENGINE = 'InnoDB'"
                    )
                )
            ).scalar_one_or_none()

        version = str(version_row["version"])
        default_engine = str(version_row["default_engine"])
        version_match = re.search(r"(\d+)\.(\d+)", version)
        is_mysql_8_plus = bool(
            version_match
            and int(version_match.group(1)) >= 8
            and "mariadb" not in version.lower()
        )
        innodb_supported = str(innodb_row or "").upper() in {"YES", "DEFAULT"}
        if not is_mysql_8_plus:
            return CheckResult("MySQL", False, f"版本不满足 MySQL 8+：{version}")
        if not innodb_supported:
            return CheckResult("InnoDB", False, "当前 MySQL 不支持 InnoDB")
        if default_engine.upper() != "INNODB":
            return CheckResult(
                "InnoDB",
                False,
                f"InnoDB 已支持，但默认引擎为 {default_engine}，请改为 InnoDB",
            )
        return CheckResult("MySQL / InnoDB", True, f"MySQL {version}，默认引擎为 InnoDB")
    finally:
        await engine.dispose()


def check_mysql() -> CheckResult:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        return CheckResult("MySQL 8+ / InnoDB", False, "未配置 DATABASE_URL（当前 SQLite 模式）", blocking=False)
    if not database_url.startswith("mysql+"):
        return CheckResult("MySQL 8+ / InnoDB", False, "DATABASE_URL 不是异步 MySQL 连接字符串")
    try:
        return asyncio.run(_check_mysql_connection(database_url))
    except Exception as exc:  # 连接失败需要在 GUI 中展示，而不是让启动器崩溃
        return CheckResult("MySQL 8+ / InnoDB", False, f"连接失败：{exc}")


def check_redis() -> CheckResult:
    """检查 Celery Broker 的 Redis TCP 服务是否可访问。"""

    broker_url = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
    parsed = urlparse(broker_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 6379
    try:
        with socket.create_connection((host, port), timeout=1.5) as connection:
            connection.sendall(b"*1\r\n$4\r\nPING\r\n")
            response = connection.recv(64)
        if b"PONG" not in response.upper():
            return CheckResult("Redis Broker", False, f"{host}:{port} 未返回 PONG")
        return CheckResult("Redis Broker", True, f"{host}:{port} 连接正常")
    except OSError as exc:
        return CheckResult("Redis Broker", False, f"{host}:{port} 连接失败：{exc}")


def run_all_checks() -> list[CheckResult]:
    service_result = ensure_mysql_service(auto_start=True)
    if not os.getenv("DATABASE_URL", "").strip():
        service_result = CheckResult(
            service_result.name,
            service_result.ok,
            service_result.detail,
            blocking=False,
        )
    return [
        *check_python_dependencies(),
        *check_node_dependencies(),
        check_redis(),
        service_result,
        check_mysql(),
    ]


def install_missing_dependencies(log=None) -> int:
    """安装 requirements.txt 和 package.json 中声明的全部依赖。"""

    def write(message: str) -> None:
        if log:
            log(message)

    if any(not result.ok for result in check_python_dependencies()):
        write("正在安装 Python 依赖...\n")
        command = [sys.executable, "-m", "pip", "install", "-r", str(PYTHON_REQUIREMENTS)]
        completed = subprocess.run(command, cwd=PROJECT_ROOT, text=True, capture_output=True)
        write(completed.stdout + completed.stderr)
        if completed.returncode != 0:
            return completed.returncode

    node_results = check_node_dependencies()
    if any(not result.ok for result in node_results):
        if not shutil.which("npm"):
            write("未找到 npm，无法安装 Node 依赖。\n")
            return 1
        write("正在安装 Node 依赖...\n")
        completed = subprocess.run(
            ["npm", "install"], cwd=PROJECT_ROOT, text=True, capture_output=True
        )
        write(completed.stdout + completed.stderr)
        if completed.returncode != 0:
            return completed.returncode
    return 0


class LauncherApp:
    def __init__(self, root: Tk):
        self.root = root
        self.root.title("自动化系统启动器")
        self.root.geometry("860x760")
        self.root.minsize(760, 620)
        self.results: list[CheckResult] = []
        self.check_completed = False
        self.status = StringVar(value="点击“开始检查”检测项目环境")
        self.flask_service = FlaskServiceSupervisor()
        self.statistics_worker = StatisticsWorkerSupervisor()
        self.selector_probe_worker = SelectorProbeWorkerSupervisor()
        self.comment_campaign_worker = CommentCampaignWorkerSupervisor()
        self._restart_thread = None
        self._closing = False
        self._cancel_event = threading.Event()
        self._lifecycle_lock = threading.RLock()
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        ttk.Label(root, text="自动化系统启动器", font=("Segoe UI", 18, "bold")).pack(
            anchor=W, padx=22, pady=(18, 4)
        )
        ttk.Label(
            root,
            text="检查依赖、数据库环境，并在准备完成后启动本地控制台",
        ).pack(anchor=W, padx=22, pady=(0, 12))

        database_frame = ttk.LabelFrame(root, text="MySQL 数据库配置")
        database_frame.pack(fill=X, padx=22, pady=(0, 10))
        self.db_host = StringVar(value=os.getenv("MYSQL_HOST", "127.0.0.1"))
        self.db_port = StringVar(value=os.getenv("MYSQL_PORT", "3306"))
        self.db_user = StringVar(value=os.getenv("MYSQL_USER", "root"))
        self.db_password = StringVar()
        self.db_name = StringVar(value=os.getenv("MYSQL_DATABASE", "automation"))
        fields = [
            ("主机", self.db_host, None),
            ("端口", self.db_port, None),
            ("用户名", self.db_user, None),
            ("密码", self.db_password, "*"),
            ("数据库名", self.db_name, None),
        ]
        for column, (label, variable, show) in enumerate(fields):
            ttk.Label(database_frame, text=label).grid(row=0, column=column, padx=(10, 4), pady=(8, 2), sticky=W)
            entry = ttk.Entry(database_frame, textvariable=variable, width=16, show=show)
            entry.grid(row=1, column=column, padx=(10, 4), pady=(0, 8), sticky="ew")
            database_frame.columnconfigure(column, weight=1)
        database_actions = ttk.Frame(database_frame)
        database_actions.grid(row=2, column=0, columnspan=5, padx=10, pady=(0, 8), sticky="w")
        ttk.Button(database_actions, text="启动 MySQL 服务", command=self.start_mysql_service).pack(side=LEFT)
        ttk.Button(database_actions, text="测试连接 / 检查 InnoDB", command=self.test_database).pack(side=LEFT)
        ttk.Button(database_actions, text="设置默认 InnoDB", command=self.configure_innodb).pack(side=LEFT, padx=6)
        ttk.Button(database_actions, text="创建数据库并初始化表", command=self.provision_database).pack(side=LEFT)
        ttk.Button(database_actions, text="保存数据库配置", command=self.save_database_config).pack(side=LEFT, padx=6)

        self.tree = ttk.Treeview(root, columns=("status", "detail"), show="headings")
        self.tree.heading("status", text="状态")
        self.tree.heading("detail", text="说明")
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("detail", width=540)
        self.tree.pack(fill=BOTH, expand=True, padx=22, pady=8)

        self.log = ttk.Entry(root)
        self.log.pack(fill=X, padx=22, pady=(4, 8))

        actions = ttk.Frame(root)
        actions.pack(fill=X, padx=22, pady=(0, 18))
        self.check_button = ttk.Button(actions, text="开始检查", command=self.check)
        self.check_button.pack(side=LEFT)
        self.install_button = ttk.Button(
            actions, text="一键安装缺失依赖", command=self.install, state="disabled"
        )
        self.install_button.pack(side=LEFT, padx=8)
        self.start_button = ttk.Button(
            actions, text="检查通过后启动", command=self.start, state="disabled"
        )
        self.start_button.pack(side=RIGHT)
        ttk.Label(actions, textvariable=self.status).pack(side=LEFT, padx=12)
        self.root.after(100, self.restart)

    def _cancel_requested(self) -> bool:
        cancel_event = getattr(self, "_cancel_event", None)
        return getattr(self, "_closing", False) or (
            cancel_event is not None and cancel_event.is_set()
        )

    def _schedule_if_active(self, callback) -> bool:
        if self._cancel_requested():
            return False

        def guarded_callback():
            if not self._cancel_requested():
                callback()

        try:
            self.root.after(0, guarded_callback)
        except Exception:
            if not self._cancel_requested():
                raise
            return False
        return True

    def _set_status(self, message: str) -> None:
        self._schedule_if_active(lambda: self.status.set(message))

    def _report_startup_failure(self, detail: str) -> None:
        if self._cancel_requested():
            return
        self._set_status(detail)
        self._schedule_if_active(
            lambda message=detail: messagebox.showerror("服务启动失败", message),
        )

    def _automatic_start_failure_detail(self) -> str:
        detail = (
            "自动启动失败；"
            f"Flask 日志：{self.flask_service.log_path}；"
            f"统计服务日志：{self.statistics_worker.log_path}；"
            f"探针服务日志：{self.selector_probe_worker.log_path}"
        )
        detail += f"；评论 Campaign Worker 日志：{self.comment_campaign_worker.log_path}"
        return detail

    def _stop_services_best_effort(self) -> bool:
        stopped_cleanly = True
        services = [
            self.flask_service,
            self.statistics_worker,
            self.selector_probe_worker,
        ]
        services.append(self.comment_campaign_worker)
        for service in services:
            try:
                service.stop()
            except Exception:
                stopped_cleanly = False
        return stopped_cleanly

    def _start_service_if_active(self, service, *, environment):
        with self._lifecycle_lock:
            if self._cancel_requested():
                return None
            process = service.start(environment=environment)
            if self._cancel_requested():
                self._stop_services_best_effort()
                return None
            return process

    def _database_config(self) -> DatabaseConfig:
        try:
            port = int(self.db_port.get().strip())
        except ValueError as exc:
            raise ValueError("MySQL 端口必须是数字") from exc
        if not 1 <= port <= 65535:
            raise ValueError("MySQL 端口必须在 1 到 65535 之间")
        if not self.db_user.get().strip() or not self.db_password.get():
            raise ValueError("请填写 MySQL 用户名和密码")
        return DatabaseConfig(
            host=self.db_host.get().strip() or "127.0.0.1",
            port=port,
            username=self.db_user.get().strip(),
            password=self.db_password.get(),
            database=self.db_name.get().strip() or "automation",
        )

    def _database_worker(self, task, success_message: str) -> None:
        try:
            config = self._database_config()
        except ValueError as exc:
            messagebox.showerror("配置不完整", str(exc))
            return
        self._set_status("正在执行数据库操作...")

        def worker():
            try:
                result = asyncio.run(task(config))
                message = result if isinstance(result, str) else "；".join(result)
                self._set_status(f"{success_message}：{message}")
                self.root.after(0, lambda: self.log.configure(state="normal"))
                self.root.after(0, lambda: self.log.delete(0, END))
                self.root.after(0, lambda: self.log.insert(0, message))
            except Exception as exc:
                self._set_status(f"数据库操作失败：{exc}")
                self.root.after(0, lambda: messagebox.showerror("数据库操作失败", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def start_mysql_service(self) -> None:
        self._set_status("正在启动 MySQL 服务...")

        def worker():
            result = ensure_mysql_service(auto_start=True)
            self.root.after(0, self._render, [*self.results, result])
            self._set_status(result.detail)
            if not result.ok:
                self.root.after(0, lambda: messagebox.showerror("MySQL 服务启动失败", result.detail))

        threading.Thread(target=worker, daemon=True).start()

    def test_database(self) -> None:
        async def task(config):
            result = await _database_server_check(config)
            if not result.ok:
                raise RuntimeError(result.detail)
            return result.detail

        self._database_worker(task, "连接检查完成")

    def configure_innodb(self) -> None:
        self._database_worker(set_default_innodb, "InnoDB 配置完成")

    def provision_database(self) -> None:
        self._database_worker(provision_database, "数据库初始化完成")

    def save_database_config(self) -> None:
        try:
            config = self._database_config()
        except ValueError as exc:
            messagebox.showerror("配置不完整", str(exc))
            return
        url = database_url(config)
        env_path = PROJECT_ROOT / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        replacements = {
            "DATABASE_URL": url,
            "MYSQL_HOST": config.host,
            "MYSQL_PORT": str(config.port),
            "MYSQL_USER": config.username,
            "MYSQL_DATABASE": config.database,
        }
        found = set()
        output = []
        for line in lines:
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key in replacements:
                output.append(f"{key}={replacements[key]}")
                found.add(key)
            else:
                output.append(line)
        output.extend(f"{key}={value}" for key, value in replacements.items() if key not in found)
        env_path.write_text("\n".join(output) + "\n", encoding="utf-8")
        os.environ.update(replacements)
        self._set_status(f"数据库配置已保存到 {env_path.name}")

    def _render(self, results: list[CheckResult]) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for result in results:
            label = "通过" if result.ok else ("警告" if not result.blocking else "缺失")
            self.tree.insert("", END, values=(label, f"{result.name}：{result.detail}"))
        blocking_failures = [result for result in results if result.blocking and not result.ok]
        self.install_button.configure(
            state="normal" if blocking_failures else "disabled"
        )
        self.start_button.configure(state="normal" if not blocking_failures else "disabled")

    def check(self) -> None:
        self.check_completed = False
        self.check_button.configure(state="disabled")
        self._set_status("正在检查环境...")

        def worker():
            try:
                self.results = run_all_checks()
                self.check_completed = True
                self.root.after(0, self._render, self.results)
                self._set_status("检查完成，可启动系统" if all(
                    result.ok or not result.blocking for result in self.results
                ) else "发现缺失依赖，请先安装")
            finally:
                self.root.after(0, lambda: self.check_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def install(self) -> None:
        self.install_button.configure(state="disabled")
        self._set_status("正在安装依赖，请稍候...")

        def worker():
            code = install_missing_dependencies()
            if code == 0:
                self.root.after(0, self.check)
                self._set_status("安装完成，正在重新检查...")
            else:
                self._set_status(f"安装失败，退出码：{code}")
                self.root.after(0, lambda: self.install_button.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def _service_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.setdefault("PUBLISH_WORKER_ENABLED", "1")
        environment.setdefault("APP_CONFIG_PATH", str(PROJECT_ROOT / "config.json"))
        environment["LOCAL_DIRECT_MODE"] = "1"
        return environment

    def _wait_for_flask(self, process, timeout: float = 15) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(APP_URL, timeout=1) as response:
                    if response.status < 500:
                        return True
            except (OSError, urllib.error.URLError):
                pass
            time.sleep(0.2)
        return False

    def _restart_services(self, *, run_checks: bool) -> bool:
        if self._cancel_requested():
            return False
        if not self._stop_services_best_effort():
            self._report_startup_failure(self._automatic_start_failure_detail())
            return False
        if self._cancel_requested():
            return False
        stop_port_listeners(5000)
        if self._cancel_requested():
            return False

        if run_checks:
            self.results = run_all_checks()
            self.check_completed = True
            self._schedule_if_active(
                lambda results=self.results: self._render(results)
            )
        if self._cancel_requested():
            return False

        blocking_failures = [
            result for result in self.results if result.blocking and not result.ok
        ]
        if blocking_failures:
            self._set_status("发现缺失依赖，服务保持停止")
            return False
        if self._cancel_requested():
            return False

        environment = self._service_environment()
        flask_process = self._start_service_if_active(
            self.flask_service,
            environment=environment,
        )
        if flask_process is None:
            return False
        worker_process = self._start_service_if_active(
            self.statistics_worker,
            environment=environment,
        )
        if worker_process is None:
            self._stop_services_best_effort()
            return False
        probe_process = self._start_service_if_active(
            self.selector_probe_worker,
            environment=environment,
        )
        if probe_process is None:
            self._stop_services_best_effort()
            return False
        campaign_process = self._start_service_if_active(
            self.comment_campaign_worker,
            environment=environment,
        )
        if campaign_process is None:
            self._stop_services_best_effort()
            return False
        if not self._wait_for_flask(flask_process):
            if self._cancel_requested():
                self._stop_services_best_effort()
                return False
            flask_state = self.flask_service.state()
            self._stop_services_best_effort()
            self._report_startup_failure(
                service_failure_detail(
                    "Flask",
                    flask_state,
                    "服务启动失败或健康检查超时",
                    self.flask_service.log_path,
                )
            )
            return False
        if self._cancel_requested():
            self._stop_services_best_effort()
            return False
        worker_state = self.statistics_worker.state()
        if worker_state["running"] is not True:
            self._stop_services_best_effort()
            self._report_startup_failure(
                service_failure_detail(
                    "统计服务",
                    worker_state,
                    "启动后立即退出",
                    self.statistics_worker.log_path,
                )
            )
            return False
        if self._cancel_requested():
            self._stop_services_best_effort()
            return False
        probe_state = self.selector_probe_worker.state()
        if probe_state["running"] is not True:
            self._stop_services_best_effort()
            self._report_startup_failure(
                service_failure_detail(
                    "元素探针服务",
                    probe_state,
                    "启动后立即退出",
                    self.selector_probe_worker.log_path,
                )
            )
            return False
        campaign_state = self.comment_campaign_worker.state()
        if campaign_state["running"] is not True:
            self._stop_services_best_effort()
            self._report_startup_failure(
                service_failure_detail(
                    "评论 Campaign Worker",
                    campaign_state,
                    "启动后立即退出",
                    self.comment_campaign_worker.log_path,
                )
            )
            return False
        if self._cancel_requested():
            self._stop_services_best_effort()
            return False

        self._set_status("系统已启动，正在打开本地控制台...")
        self._schedule_if_active(lambda: webbrowser.open(APP_URL))
        return True

    def _finish_restart(self) -> None:
        if self._cancel_requested():
            return
        self.check_button.configure(state="normal")
        blocking_failures = [
            result for result in self.results if result.blocking and not result.ok
        ]
        self.start_button.configure(
            state="normal"
            if self.check_completed and not blocking_failures
            else "disabled"
        )

    def _begin_restart(self, *, run_checks: bool) -> None:
        if self._cancel_requested():
            return
        if self._restart_thread is not None and self._restart_thread.is_alive():
            return
        self.check_button.configure(state="disabled")
        self.start_button.configure(state="disabled")
        self._set_status("正在停止旧服务并启动当前版本...")

        def worker():
            try:
                self._restart_services(run_checks=run_checks)
            except Exception:
                self._stop_services_best_effort()
                self._report_startup_failure(self._automatic_start_failure_detail())
            finally:
                self._schedule_if_active(self._finish_restart)

        self._restart_thread = threading.Thread(target=worker, daemon=True)
        self._restart_thread.start()

    def restart(self) -> None:
        self._begin_restart(run_checks=True)

    def start(self) -> None:
        if not self.check_completed:
            messagebox.showwarning("鏃犳硶鍚姩", "璇峰厛瀹屾垚鐜妫€鏌ワ紒")
            return
        blocking_failures = [result for result in self.results if result.blocking and not result.ok]
        if blocking_failures:
            messagebox.showwarning("无法启动", "请先完成缺失依赖的安装。")
            return
        self._begin_restart(run_checks=False)

    def close(self) -> None:
        self._closing = True
        self._cancel_event.set()
        try:
            with self._lifecycle_lock:
                stopped_cleanly = self._stop_services_best_effort()
            if not stopped_cleanly:
                messagebox.showerror("启动器关闭失败", SHUTDOWN_ERROR)
        finally:
            self.root.destroy()


def main() -> None:
    try:
        load_project_environment()
    except Exception:
        show_startup_error(ENVIRONMENT_STARTUP_ERROR)
        return
    try:
        if not ensure_admin():
            return
    except AdminElevationError:
        show_startup_error(UAC_STARTUP_ERROR)
        return
    except Exception:
        show_startup_error(GENERIC_STARTUP_ERROR)
        return
    try:
        root = Tk()
        LauncherApp(root)
    except Exception:
        show_startup_error(GENERIC_STARTUP_ERROR)
        return
    root.mainloop()


if __name__ == "__main__":
    main()
