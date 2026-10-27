import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class PlatformAccount(TenantScoped):
    __tablename__ = "platform_accounts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "platform",
            "name",
            name="uq_platform_accounts_tenant_platform_name",
        ),
    )

    platform: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(100))
    credential_enc: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="active")
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)


class PublishJob(TenantScoped):
    __tablename__ = "publish_jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_publish_jobs_tenant_key"
        ),
        Index(
            "ix_publish_jobs_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    content_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("contents.id", ondelete="CASCADE"), index=True
    )
    content_version: Mapped[int] = mapped_column(Integer)
    platform: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("platform_accounts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    external_id: Mapped[str] = mapped_column(String(200), default="")
    external_url: Mapped[str] = mapped_column(String(1000), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    checks: Mapped[dict] = mapped_column(JSONType, default=dict)
    export_path: Mapped[str] = mapped_column(String(1000), default="")


class PublishEvent(TenantScoped):
    __tablename__ = "publish_events"

    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("publish_jobs.id", ondelete="CASCADE"), index=True
    )
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    note: Mapped[str] = mapped_column(String(500), default="")
    actor: Mapped[str] = mapped_column(String(255), default="user")
