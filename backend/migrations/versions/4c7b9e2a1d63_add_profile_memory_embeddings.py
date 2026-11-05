"""add profile memory embeddings

Revision ID: 4c7b9e2a1d63
Revises: 756cd4814c47
Create Date: 2026-11-04 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

from app.models.vector import EMBEDDING_DIM


# revision identifiers, used by Alembic.
revision: str = '4c7b9e2a1d63'
down_revision: Union[str, Sequence[str], None] = '756cd4814c47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "profile_memories",
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=True),
    )
    # 余弦距离检索使用 HNSW；表为空时建索引代价很低
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_profile_memories_embedding_hnsw "
        "ON profile_memories USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_profile_memories_embedding_hnsw")
    op.drop_column("profile_memories", "embedding")
    # 扩展可能被其他表复用，降级时不删除
