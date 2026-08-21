"""账号相关的 SQLAlchemy ORM 模型。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Enum as SqlEnum, JSON, String, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid

from database import Base


class AccountTier(str, Enum):
    """账号等级。"""

    S = "S"
    A = "A"
    B = "B"
    C = "C"


class AccountStatus(str, Enum):
    """账号当前运行状态。"""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    BANNED = "BANNED"
    COOLDOWN = "COOLDOWN"


def default_daily_actions() -> dict[str, int]:
    """为每个账号创建独立的每日动作计数对象。"""

    return {"likes": 0, "comments": 0}


class Account(Base):
    """可被任务调度的账号。

    调度器分配账号时，应在同一个事务中使用 ``SELECT ... FOR UPDATE``
    锁定候选行，再将 ``status`` 更新为 ``RUNNING`` 后提交。仅依赖模型
    字段本身无法阻止多个进程同时读取同一个空闲账号。
    """

    __tablename__ = "accounts"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    account_id: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    ads_power_id: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        nullable=False,
    )
    tier: Mapped[AccountTier] = mapped_column(
        SqlEnum(AccountTier, name="account_tier"),
        nullable=False,
        default=AccountTier.C,
    )
    status: Mapped[AccountStatus] = mapped_column(
        SqlEnum(AccountStatus, name="account_status"),
        nullable=False,
        default=AccountStatus.IDLE,
        index=True,
    )
    daily_actions: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=default_daily_actions,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
        server_default=func.now(),
    )

