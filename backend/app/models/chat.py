import uuid
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Index, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class ChatConversation(TenantScoped):
    __tablename__ = "chat_conversations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "id", name="uq_chat_conversations_tenant_id_id"
        ),
        Index("ix_chat_conversations_tenant_user", "tenant_id", "user_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    title: Mapped[str] = mapped_column(String(100), default="")
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )


class ChatMessage(TenantScoped):
    __tablename__ = "chat_messages"
    __table_args__ = (
        # 消息与对话必须同租户，DB 层兜底防止跨租户挂载消息
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["chat_conversations.tenant_id", "chat_conversations.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "conversation_id", "seq", name="uq_chat_messages_conversation_seq"
        ),
        Index("ix_chat_messages_conversation_seq", "conversation_id", "seq"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    seq: Mapped[int] = mapped_column(default=0)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(32), nullable=True)
    skill_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    missing: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
