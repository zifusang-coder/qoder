import uuid
from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, DateTime, MetaData, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import DEFAULT_TENANT_ID

JSONType = JSON().with_variant(JSONB, "postgresql")

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TenantMixin:
    tenant_id: Mapped[str] = mapped_column(
        String(36), default=DEFAULT_TENANT_ID, index=True, nullable=False
    )


class TenantScoped(Base, UUIDMixin, TenantMixin, TimestampMixin):
    __abstract__ = True
    __mapper_args__: ClassVar[dict[str, Any]] = {"eager_defaults": True}
