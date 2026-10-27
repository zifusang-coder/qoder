"""partial unique active account names

Revision ID: 1a7c3d5e9b20
Revises: 08b70f176855
Create Date: 2026-10-27 04:31:35.682141

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1a7c3d5e9b20'
down_revision: Union[str, Sequence[str], None] = '08b70f176855'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(
        "uq_platform_accounts_tenant_platform_name",
        "platform_accounts",
        type_="unique",
    )
    op.create_index(
        "uq_platform_accounts_active_name",
        "platform_accounts",
        ["tenant_id", "platform", "name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_platform_accounts_active_name", table_name="platform_accounts"
    )
    op.create_unique_constraint(
        "uq_platform_accounts_tenant_platform_name",
        "platform_accounts",
        ["tenant_id", "platform", "name"],
    )
