import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TenantScoped, TimestampMixin, UUIDMixin


class Plan(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="CNY", nullable=False)
    limits: Mapped[dict] = mapped_column(JSONType, default=dict)
    prices: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)


class BillingStatement(TenantScoped):
    __tablename__ = "billing_statements"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "period", name="uq_billing_statements_tenant_period"
        ),
    )

    period: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="closed", nullable=False)
    plan_code: Mapped[str] = mapped_column(String(50), nullable=False)
    plan_name: Mapped[str] = mapped_column(String(100), nullable=False)
    plan_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="CNY")
    limits: Mapped[dict] = mapped_column(JSONType, default=dict)
    prices: Mapped[dict] = mapped_column(JSONType, default=dict)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    charges: Mapped[list] = mapped_column(JSONType, default=list)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    closed_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reopened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
