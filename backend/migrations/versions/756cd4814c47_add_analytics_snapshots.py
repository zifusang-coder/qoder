"""add analytics snapshots

Revision ID: 756cd4814c47
Revises: 1a7c3d5e9b20
Create Date: 2026-10-27 05:12:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '756cd4814c47'
down_revision: Union[str, Sequence[str], None] = '1a7c3d5e9b20'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "analytics_snapshots",
        sa.Column("content_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.Date(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=False),
        sa.Column("likes", sa.Integer(), nullable=False),
        sa.Column("comments", sa.Integer(), nullable=False),
        sa.Column("shares", sa.Integer(), nullable=False),
        sa.Column("saves", sa.Integer(), nullable=False),
        sa.Column("followers_delta", sa.Integer(), nullable=False),
        sa.Column(
            "raw",
            sa.JSON().with_variant(
                postgresql.JSONB(astext_type=sa.Text()), "postgresql"
            ),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["contents.id"],
            name=op.f("fk_analytics_snapshots_content_id_contents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_analytics_snapshots")),
        sa.UniqueConstraint(
            "tenant_id",
            "content_id",
            "platform",
            "captured_at",
            name="uq_analytics_snapshots_tenant_content_platform_date",
        ),
    )
    op.create_index(
        op.f("ix_analytics_snapshots_content_id"),
        "analytics_snapshots",
        ["content_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_analytics_snapshots_tenant_id"),
        "analytics_snapshots",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        op.f("ix_analytics_snapshots_tenant_id"),
        table_name="analytics_snapshots",
    )
    op.drop_index(
        op.f("ix_analytics_snapshots_content_id"),
        table_name="analytics_snapshots",
    )
    op.drop_table("analytics_snapshots")
