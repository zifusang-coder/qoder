import uuid
from datetime import date

from sqlalchemy import (
    BigInteger,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class AnalyticsSnapshot(TenantScoped):
    __tablename__ = "analytics_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "content_id",
            "platform",
            "captured_at",
            name="uq_analytics_snapshots_tenant_content_platform_date",
        ),
        Index(
            "ix_analytics_snapshots_tenant_content_captured",
            "tenant_id",
            "content_id",
            "captured_at",
        ),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contents.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[date] = mapped_column(Date)
    views: Mapped[int] = mapped_column(BigInteger, default=0)
    likes: Mapped[int] = mapped_column(BigInteger, default=0)
    comments: Mapped[int] = mapped_column(BigInteger, default=0)
    shares: Mapped[int] = mapped_column(BigInteger, default=0)
    saves: Mapped[int] = mapped_column(BigInteger, default=0)
    followers_delta: Mapped[int] = mapped_column(BigInteger, default=0)
    raw: Mapped[dict] = mapped_column(JSONType, default=dict)
