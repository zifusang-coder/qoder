"""make chat message seq unique per conversation

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-22
"""

from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 并发发送时同一条对话的 seq 可能重复；唯一约束把静默乱序变成显式冲突，
    # 发送路径再配合会话行锁保证不会真的撞上。
    op.create_unique_constraint(
        "uq_chat_messages_conversation_seq",
        "chat_messages",
        ["conversation_id", "seq"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_chat_messages_conversation_seq",
        "chat_messages",
        type_="unique",
    )
