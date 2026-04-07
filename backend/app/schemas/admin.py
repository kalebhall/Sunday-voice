"""Pydantic schemas for admin endpoints."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Users & Roles
# ---------------------------------------------------------------------------


class RoleOut(BaseModel):
    id: int
    name: str
    description: str | None


class UserOut(BaseModel):
    id: int
    email: str
    display_name: str
    role: RoleOut
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime
    updated_at: datetime


class UserListOut(BaseModel):
    users: list[UserOut]
    total: int


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=120)
    role_id: int


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role_id: int | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class RoleListOut(BaseModel):
    roles: list[RoleOut]


# ---------------------------------------------------------------------------
# Usage / CostMeter
# ---------------------------------------------------------------------------


class UsageRowOut(BaseModel):
    provider: str
    operation: str
    period: str
    units: int
    cost_usd: Decimal


class UsageSummaryOut(BaseModel):
    period: str
    rows: list[UsageRowOut]
    total_cost_usd: Decimal
    monthly_budget_usd: float
    alert_threshold: float
    # True if current period spend >= budget * alert_threshold
    alert_triggered: bool


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditLogOut(BaseModel):
    id: int
    actor_user_id: int | None
    actor_email: str | None  # joined from users table when available
    action: str
    target_type: str | None
    target_id: str | None
    ip_address: str | None
    details: dict | None
    created_at: datetime


class AuditLogListOut(BaseModel):
    logs: list[AuditLogOut]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Retention status
# ---------------------------------------------------------------------------


class RetentionStatusOut(BaseModel):
    retention_hours: int
    cleanup_enabled: bool
    cleanup_interval_minutes: int
    last_cleanup: AuditLogOut | None


# ---------------------------------------------------------------------------
# Budget settings
# ---------------------------------------------------------------------------


class BudgetSettingsOut(BaseModel):
    monthly_budget_usd: float
    alert_threshold: float  # fraction, e.g. 0.8
    source: str  # "db" if overridden at runtime, "env" if using default


class BudgetSettingsUpdate(BaseModel):
    monthly_budget_usd: float = Field(gt=0)
    alert_threshold: float = Field(gt=0, le=1.0)
