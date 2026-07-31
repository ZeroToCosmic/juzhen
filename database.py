"""异步 MySQL 数据库基础设施。

该模块只负责创建数据库引擎、连接池和会话，不包含具体业务模型。
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


load_dotenv()


# 本地 MySQL 的异步连接字符串格式：
# mysql+asyncmy://用户名:密码@127.0.0.1:3306/数据库名?charset=utf8mb4
# 例如：mysql+asyncmy://root:password@127.0.0.1:3306/automation?charset=utf8mb4
DEFAULT_DATABASE_URL = (
    "mysql+asyncmy://root:password@127.0.0.1:3306/automation?charset=utf8mb4"
)
DEFAULT_POOL_SIZE = 20
DEFAULT_MAX_OVERFLOW = 10


class Base(DeclarativeBase):
    """所有 SQLAlchemy ORM 模型的基类。"""


def _int_from_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数，当前值为 {value!r}") from exc
    if parsed < 0:
        raise ValueError(f"{name} 不能小于 0")
    return parsed


def create_database_engine(
    database_url: str | None = None,
    *,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    """创建异步 MySQL 引擎。

    ``pool_size`` 和 ``max_overflow`` 可按部署规模自定义；不传时分别使用
    ``DB_POOL_SIZE``/``DB_MAX_OVERFLOW`` 环境变量，默认值为 20/10。
    """

    resolved_url = database_url or os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL)
    resolved_pool_size = (
        _int_from_env("DB_POOL_SIZE", DEFAULT_POOL_SIZE)
        if pool_size is None
        else pool_size
    )
    resolved_max_overflow = (
        _int_from_env("DB_MAX_OVERFLOW", DEFAULT_MAX_OVERFLOW)
        if max_overflow is None
        else max_overflow
    )

    if resolved_pool_size < 0 or resolved_max_overflow < 0:
        raise ValueError("pool_size 和 max_overflow 不能小于 0")

    return create_async_engine(
        resolved_url,
        pool_size=resolved_pool_size,
        max_overflow=resolved_max_overflow,
        pool_pre_ping=True,
    )


engine = create_database_engine()
SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """提供一个异步数据库会话，并在请求结束后自动关闭它。

    可直接作为 FastAPI 等框架的依赖注入函数使用：
    ``db: AsyncSession = Depends(get_db)``。
    会话从连接池中获取连接，使用完毕后归还连接池。
    """

    async with SessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_database() -> None:
    """应用关闭时释放连接池中的所有连接。"""

    await engine.dispose()
