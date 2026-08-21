"""Account business state machine (PRD F14 / transitions #12-#18, M4).

The transition table is the single authority for account business-status
moves, mirroring PRD section 6.2 rows 12-18. CAPTCHA -> MANUAL_VERIFIED
requires a manual action and enables PROBE gating; MANUAL_REVIEW is the
only exit from CAPTCHA/SUSPENDED; SUSPENDED stops dispatch.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from central.db import get_session
from central.models import Account
from central.security import require_tenant

router = APIRouter(prefix="/api/central/accounts", tags=["accounts"])

ACCOUNT_TRANSITIONS: dict[str, frozenset[str]] = {
    "ACTIVE": frozenset({"CAPTCHA", "SUSPENDED"}),
    "CAPTCHA": frozenset({"MANUAL_VERIFIED", "MANUAL_REVIEW", "SUSPENDED"}),
    "MANUAL_VERIFIED": frozenset({"CAPTCHA", "SUSPENDED"}),
    "SUSPENDED": frozenset({"MANUAL_REVIEW"}),
    "MANUAL_REVIEW": frozenset({"ACTIVE", "SUSPENDED", "CAPTCHA"}),
}


def apply_account_transition(
    session: Session,
    *,
    tenant_id: str,
    account_id: str,
    target: str,
) -> Account:
    account = (
        session.query(Account)
        .filter(Account.tenant_id == tenant_id, Account.account_id == account_id)
        .one_or_none()
    )
    if account is None:
        raise KeyError("account not found")
    allowed = ACCOUNT_TRANSITIONS.get(account.business_status, frozenset())
    if target not in allowed:
        raise ValueError(
            f"invalid transition {account.business_status} -> {target}"
        )
    account.business_status = target
    account.revision += 1
    if target == "MANUAL_VERIFIED":
        from datetime import datetime, timedelta, timezone

        from central import config

        account.manual_verified_at = datetime.now(timezone.utc)
        account.cooldown_until = datetime.now(timezone.utc) + timedelta(
            seconds=config.ACCOUNT_COOLDOWN_SECONDS
        )
    return account


class AccountStatusRequest(BaseModel):
    status: str = Field(min_length=1, max_length=32)


@router.post("/{account_id}/status")
def update_account_status(
    account_id: str,
    payload: AccountStatusRequest,
    tenant_id: str = Depends(require_tenant),
    session: Session = Depends(get_session),
) -> dict:
    try:
        account = apply_account_transition(
            session,
            tenant_id=tenant_id,
            account_id=account_id,
            target=payload.status,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="account not found") from None
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    try:
        from central import app as central_app

        central_app.event_store.publish(
            tenant_id,
            "account.status",
            {"account_id": account_id, "status": account.business_status},
        )
    except BaseException:
        pass
    return {
        "account_id": account_id,
        "business_status": account.business_status,
        "revision": account.revision,
    }
