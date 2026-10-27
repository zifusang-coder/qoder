"""harden publish constraints

Revision ID: 08b70f176855
Revises: 840acafce1b0
Create Date: 2026-10-27 03:48:55.895910

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '08b70f176855'
down_revision: Union[str, Sequence[str], None] = '840acafce1b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        "publish_events",
        "actor",
        existing_type=sa.String(length=100),
        type_=sa.String(length=255),
        existing_nullable=False,
    )
    op.drop_constraint(
        "fk_publish_jobs_account_id_platform_accounts",
        "publish_jobs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_publish_jobs_account_id_platform_accounts",
        "publish_jobs",
        "platform_accounts",
        ["account_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_publish_jobs_tenant_status_created",
        "publish_jobs",
        ["tenant_id", "status", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_publish_jobs_tenant_status_created", table_name="publish_jobs"
    )
    op.drop_constraint(
        "fk_publish_jobs_account_id_platform_accounts",
        "publish_jobs",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_publish_jobs_account_id_platform_accounts",
        "publish_jobs",
        "platform_accounts",
        ["account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.alter_column(
        "publish_events",
        "actor",
        existing_type=sa.String(length=255),
        type_=sa.String(length=100),
        existing_nullable=False,
    )
