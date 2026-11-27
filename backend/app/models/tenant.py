import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.billing.plans import DEFAULT_PLAN_ID
from app.models.base import Base, TimestampMixin


class Tenant(Base, TimestampMixin):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="active", nullable=False)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("plans.id", ondelete="RESTRICT"),
        default=lambda: uuid.UUID(DEFAULT_PLAN_ID),
        nullable=False,
    )
    plan_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
