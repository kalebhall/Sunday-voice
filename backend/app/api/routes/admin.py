"""Admin-only endpoints.

All routes require the ``admin`` role.  Covers:
  - Users & roles CRUD
  - Usage / CostMeter dashboard
  - Audit log viewer
  - Retention status
  - Budget caps and alert thresholds (runtime-overridable via system_config)
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import DbSession, require_role
from app.core.config import get_settings
from app.core.security import hash_password
from app.models import ROLE_ADMIN, AuditLog, Role, User, UsageMeter
from app.models.config import SystemConfig
from app.schemas.admin import (
    AuditLogListOut,
    AuditLogOut,
    BudgetSettingsOut,
    BudgetSettingsUpdate,
    RetentionStatusOut,
    RoleListOut,
    RoleOut,
    UsageRowOut,
    UsageSummaryOut,
    UserCreate,
    UserListOut,
    UserOut,
    UserUpdate,
)

router = APIRouter()

_require_admin = require_role(ROLE_ADMIN)
AdminUser = Annotated[User, Depends(_require_admin)]

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

_KEY_BUDGET = "monthly_budget_usd"
_KEY_THRESHOLD = "budget_alert_threshold"


async def _get_config(db: AsyncSession, key: str) -> str | None:
    row = (
        await db.execute(select(SystemConfig).where(SystemConfig.key == key))
    ).scalar_one_or_none()
    return row.value if row else None


async def _set_config(db: AsyncSession, key: str, value: str) -> None:
    row = (
        await db.execute(select(SystemConfig).where(SystemConfig.key == key))
    ).scalar_one_or_none()
    if row is None:
        db.add(SystemConfig(key=key, value=value))
    else:
        row.value = value
        row.updated_at = datetime.now(UTC)


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=RoleOut(
            id=user.role.id,
            name=user.role.name,
            description=user.role.description,
        ),
        is_active=user.is_active,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@router.get("/users", response_model=UserListOut)
async def list_users(
    _admin: AdminUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
) -> UserListOut:
    offset = (page - 1) * page_size
    total = (await db.execute(select(func.count()).select_from(User))).scalar_one()
    rows = (
        await db.execute(
            select(User)
            .options(selectinload(User.role))
            .order_by(User.id)
            .offset(offset)
            .limit(page_size)
        )
    ).scalars().all()
    return UserListOut(users=[_user_out(u) for u in rows], total=total)


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    _admin: AdminUser,
    db: DbSession,
) -> UserOut:
    existing = (
        await db.execute(select(User).where(User.email == payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="email already registered",
        )
    role = (
        await db.execute(select(Role).where(Role.id == payload.role_id))
    ).scalar_one_or_none()
    if role is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="role not found",
        )
    user = User(
        email=payload.email,
        hashed_password=hash_password(payload.password),
        display_name=payload.display_name,
        role_id=payload.role_id,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user, ["role"])
    await db.commit()
    await db.refresh(user)
    return _user_out(user)


@router.get("/users/{user_id}", response_model=UserOut)
async def get_user(
    user_id: int,
    _admin: AdminUser,
    db: DbSession,
) -> UserOut:
    user = (
        await db.execute(
            select(User).options(selectinload(User.role)).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return _user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(
    user_id: int,
    payload: UserUpdate,
    admin: AdminUser,
    db: DbSession,
) -> UserOut:
    user = (
        await db.execute(
            select(User).options(selectinload(User.role)).where(User.id == user_id)
        )
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    # Prevent an admin from demoting themselves if they're the last admin.
    if payload.role_id is not None and payload.role_id != user.role_id:
        new_role = (
            await db.execute(select(Role).where(Role.id == payload.role_id))
        ).scalar_one_or_none()
        if new_role is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="role not found",
            )
        if user.role.name == ROLE_ADMIN and new_role.name != ROLE_ADMIN:
            admin_count = (
                await db.execute(
                    select(func.count())
                    .select_from(User)
                    .join(Role)
                    .where(Role.name == ROLE_ADMIN, User.is_active.is_(True))
                )
            ).scalar_one()
            if admin_count <= 1 and user.id == admin.id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="cannot demote the only active admin",
                )
        user.role_id = payload.role_id

    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.is_active is not None:
        user.is_active = payload.is_active
    if payload.password is not None:
        user.hashed_password = hash_password(payload.password)

    await db.commit()
    await db.refresh(user, ["role"])
    return _user_out(user)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_user(
    user_id: int,
    admin: AdminUser,
    db: DbSession,
) -> None:
    if user_id == admin.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="cannot deactivate your own account",
        )
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    user.is_active = False
    await db.commit()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


@router.get("/roles", response_model=RoleListOut)
async def list_roles(_admin: AdminUser, db: DbSession) -> RoleListOut:
    rows = (await db.execute(select(Role).order_by(Role.id))).scalars().all()
    return RoleListOut(
        roles=[RoleOut(id=r.id, name=r.name, description=r.description) for r in rows]
    )


# ---------------------------------------------------------------------------
# Usage / CostMeter
# ---------------------------------------------------------------------------


@router.get("/usage", response_model=UsageSummaryOut)
async def get_usage(
    _admin: AdminUser,
    db: DbSession,
    period: str | None = Query(
        default=None,
        description="YYYY-MM period bucket; defaults to current month",
        pattern=r"^\d{4}-\d{2}$",
    ),
) -> UsageSummaryOut:
    if period is None:
        period = datetime.now(UTC).strftime("%Y-%m")

    rows = (
        await db.execute(
            select(UsageMeter)
            .where(UsageMeter.period == period)
            .order_by(UsageMeter.provider, UsageMeter.operation)
        )
    ).scalars().all()

    usage_rows = [
        UsageRowOut(
            provider=r.provider,
            operation=r.operation,
            period=r.period,
            units=r.units,
            cost_usd=r.cost_usd,
        )
        for r in rows
    ]
    total_cost = sum((r.cost_usd for r in rows), Decimal("0"))

    settings = get_settings()
    budget_raw = await _get_config(db, _KEY_BUDGET)
    threshold_raw = await _get_config(db, _KEY_THRESHOLD)
    budget = float(budget_raw) if budget_raw is not None else settings.monthly_budget_usd
    threshold = float(threshold_raw) if threshold_raw is not None else settings.budget_alert_threshold

    alert_triggered = float(total_cost) >= budget * threshold

    return UsageSummaryOut(
        period=period,
        rows=usage_rows,
        total_cost_usd=total_cost,
        monthly_budget_usd=budget,
        alert_threshold=threshold,
        alert_triggered=alert_triggered,
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _audit_out(log: AuditLog, actor_email: str | None) -> AuditLogOut:
    return AuditLogOut(
        id=log.id,
        actor_user_id=log.actor_user_id,
        actor_email=actor_email,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        ip_address=log.ip_address,
        details=log.details,
        created_at=log.created_at,
    )


@router.get("/audit-logs", response_model=AuditLogListOut)
async def list_audit_logs(
    _admin: AdminUser,
    db: DbSession,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    action: str | None = Query(default=None),
    actor_user_id: int | None = Query(default=None),
) -> AuditLogListOut:
    base = select(AuditLog)
    count_base = select(func.count()).select_from(AuditLog)

    if action:
        base = base.where(AuditLog.action == action)
        count_base = count_base.where(AuditLog.action == action)
    if actor_user_id is not None:
        base = base.where(AuditLog.actor_user_id == actor_user_id)
        count_base = count_base.where(AuditLog.actor_user_id == actor_user_id)

    total = (await db.execute(count_base)).scalar_one()
    offset = (page - 1) * page_size
    logs = (
        await db.execute(
            base.order_by(AuditLog.created_at.desc()).offset(offset).limit(page_size)
        )
    ).scalars().all()

    # Batch-load actor emails for the current page.
    actor_ids = {log.actor_user_id for log in logs if log.actor_user_id is not None}
    email_map: dict[int, str] = {}
    if actor_ids:
        users = (
            await db.execute(select(User.id, User.email).where(User.id.in_(actor_ids)))
        ).all()
        email_map = {uid: email for uid, email in users}

    return AuditLogListOut(
        logs=[_audit_out(log, email_map.get(log.actor_user_id)) for log in logs],
        total=total,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# Retention status
# ---------------------------------------------------------------------------


@router.get("/retention", response_model=RetentionStatusOut)
async def get_retention_status(_admin: AdminUser, db: DbSession) -> RetentionStatusOut:
    settings = get_settings()

    # Find the most recent retention cleanup audit entry.
    last_log = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.action == "retention.cleanup")
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_out: AuditLogOut | None = None
    if last_log is not None:
        last_out = _audit_out(last_log, None)

    return RetentionStatusOut(
        retention_hours=settings.content_retention_hours,
        cleanup_enabled=settings.retention_cleanup_enabled,
        cleanup_interval_minutes=settings.retention_cleanup_interval_minutes,
        last_cleanup=last_out,
    )


# ---------------------------------------------------------------------------
# Budget settings
# ---------------------------------------------------------------------------


@router.get("/budget", response_model=BudgetSettingsOut)
async def get_budget(_admin: AdminUser, db: DbSession) -> BudgetSettingsOut:
    settings = get_settings()
    budget_raw = await _get_config(db, _KEY_BUDGET)
    threshold_raw = await _get_config(db, _KEY_THRESHOLD)

    source = "db" if (budget_raw is not None or threshold_raw is not None) else "env"
    return BudgetSettingsOut(
        monthly_budget_usd=float(budget_raw) if budget_raw is not None else settings.monthly_budget_usd,
        alert_threshold=float(threshold_raw) if threshold_raw is not None else settings.budget_alert_threshold,
        source=source,
    )


@router.patch("/budget", response_model=BudgetSettingsOut)
async def update_budget(
    payload: BudgetSettingsUpdate,
    _admin: AdminUser,
    db: DbSession,
) -> BudgetSettingsOut:
    await _set_config(db, _KEY_BUDGET, str(payload.monthly_budget_usd))
    await _set_config(db, _KEY_THRESHOLD, str(payload.alert_threshold))
    await db.commit()
    return BudgetSettingsOut(
        monthly_budget_usd=payload.monthly_budget_usd,
        alert_threshold=payload.alert_threshold,
        source="db",
    )
