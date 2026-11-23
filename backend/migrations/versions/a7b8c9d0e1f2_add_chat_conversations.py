"""add chat conversations

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.create_table(
        "chat_conversations",
        sa.Column("user_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column(
            "title", sa.String(length=100), nullable=False, server_default=""
        ),
        sa.Column("profile_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "id", name="uq_chat_conversations_tenant_id_id"
        ),
    )
    op.create_index(
        "ix_chat_conversations_tenant_id", "chat_conversations", ["tenant_id"]
    )
    op.create_index(
        "ix_chat_conversations_tenant_user",
        "chat_conversations",
        ["tenant_id", "user_id"],
    )

    op.create_table(
        "chat_messages",
        sa.Column("conversation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.String(length=32), nullable=True),
        sa.Column("skill_id", sa.String(length=100), nullable=True),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("data", JSON, nullable=True),
        sa.Column("missing", JSON, nullable=True),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["chat_conversations.tenant_id", "chat_conversations.id"],
            name="fk_chat_messages_tenant_id_chat_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_tenant_id", "chat_messages", ["tenant_id"])
    op.create_index(
        "ix_chat_messages_conversation_seq",
        "chat_messages",
        ["conversation_id", "seq"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chat_messages_conversation_seq", table_name="chat_messages"
    )
    op.drop_index("ix_chat_messages_tenant_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index(
        "ix_chat_conversations_tenant_user", table_name="chat_conversations"
    )
    op.drop_index(
        "ix_chat_conversations_tenant_id", table_name="chat_conversations"
    )
    op.drop_table("chat_conversations")
