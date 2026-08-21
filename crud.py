"""Account 模型的异步 CRUD 操作。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Account, AccountStatus, AccountTier


async def get_idle_accounts(
    db: AsyncSession,
    limit: int,
) -> list[Account]:
    """原子地领取一批空闲账号，并将它们标记为 ``RUNNING``。

    ``FOR UPDATE SKIP LOCKED`` 会在数据库层锁定被选中的行；已经被其他
    worker 锁定的行会被跳过。状态更新和提交与查询处于同一个事务中，
    因此不会出现两个并发 worker 领取同一个账号的情况。
    """

    if limit <= 0:
        return []

    try:
        statement = (
            select(Account)
            .where(Account.status == AccountStatus.IDLE)
            .order_by(Account.updated_at, Account.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(statement)
        accounts = list(result.scalars().all())

        for account in accounts:
            account.status = AccountStatus.RUNNING

        # SELECT ... FOR UPDATE 持有的锁会一直保持到这里，提交后才释放。
        await db.commit()
        return accounts
    except Exception:
        await db.rollback()
        raise


async def get_account(db: AsyncSession, account_id: UUID) -> Account | None:
    """按 UUID 查询账号。"""

    return await db.get(Account, account_id)


async def get_account_by_external_id(
    db: AsyncSession,
    external_account_id: str,
) -> Account | None:
    """按业务侧账号 ID 查询账号。"""

    statement = select(Account).where(Account.account_id == external_account_id)
    result = await db.execute(statement)
    return result.scalar_one_or_none()


async def create_account(
    db: AsyncSession,
    *,
    account_id: str,
    ads_power_id: str,
    tier: AccountTier = AccountTier.C,
    status: AccountStatus = AccountStatus.IDLE,
    daily_actions: dict[str, int] | None = None,
) -> Account:
    """创建账号并提交事务。"""

    account = Account(
        account_id=account_id,
        ads_power_id=ads_power_id,
        tier=tier,
        status=status,
    )
    if daily_actions is not None:
        account.daily_actions = daily_actions

    try:
        db.add(account)
        await db.commit()
        await db.refresh(account)
        return account
    except Exception:
        await db.rollback()
        raise


async def update_account_status(
    db: AsyncSession,
    account: Account,
    status: AccountStatus,
) -> Account:
    """更新账号状态并提交事务。"""

    try:
        account.status = status
        await db.commit()
        await db.refresh(account)
        return account
    except Exception:
        await db.rollback()
        raise


async def list_accounts(db: AsyncSession) -> Sequence[Account]:
    """查询全部账号，按更新时间和 UUID 稳定排序。"""

    statement = select(Account).order_by(Account.updated_at, Account.id)
    result = await db.execute(statement)
    return result.scalars().all()

