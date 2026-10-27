import uuid

from sqlalchemy import Boolean, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class Profile(TenantScoped):
    __tablename__ = "profiles"

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

    profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("profiles.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), default="insight")
    content: Mapped[str] = mapped_column(Text)
    weight: Mapped[float] = mapped_column(default=1.0)
    source: Mapped[str] = mapped_column(String(100), default="manual")
    tags: Mapped[list] = mapped_column(JSONType, default=list)
