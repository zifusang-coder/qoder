import uuid

from sqlalchemy import (
    Boolean,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class Profile(TenantScoped):
    __tablename__ = "profiles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_profiles_tenant_id_id"),
    )

    name: Mapped[str] = mapped_column(String(100))
    account_name: Mapped[str] = mapped_column(String(100), default="")
    positioning: Mapped[str] = mapped_column(Text, default="")
    audience: Mapped[str] = mapped_column(Text, default="")
    tone: Mapped[str] = mapped_column(Text, default="")
    taboos: Mapped[list] = mapped_column(JSONType, default=list)
    goals: Mapped[list] = mapped_column(JSONType, default=list)
    platform_focus: Mapped[list] = mapped_column(JSONType, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProfileMemory(TenantScoped):
    __tablename__ = "profile_memories"
    __table_args__ = (
        # 租户与画像必须同属一个租户，DB 层兜底防止跨租户挂载记忆
        ForeignKeyConstraint(
            ["tenant_id", "profile_id"],
            ["profiles.tenant_id", "profiles.id"],
            ondelete="CASCADE",
        ),
        Index("ix_profile_memories_tenant_profile", "tenant_id", "profile_id"),
    )

    profile_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(32), default="insight")
    content: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(default=1.0)
    source: Mapped[str] = mapped_column(String(100), default="manual")
    tags: Mapped[list] = mapped_column(JSONType, default=list)
