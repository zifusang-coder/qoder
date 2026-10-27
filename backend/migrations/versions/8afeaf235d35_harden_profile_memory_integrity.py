"""harden profile memory integrity

Revision ID: 8afeaf235d35
Revises: 7261962b1b5f
Create Date: 2026-10-27 00:59:38.259062

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '8afeaf235d35'
down_revision: Union[str, Sequence[str], None] = '7261962b1b5f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # profiles(tenant_id, id) 是复合外键的父键，必须先建立唯一约束
    op.create_unique_constraint(
        "uq_profiles_tenant_id_id", "profiles", ["tenant_id", "id"]
    )
    # 用 (tenant_id, profile_id) 复合外键替代单列 profile_id 外键，
    # 在 DB 层阻止记忆挂载到其他租户的画像；级联删除语义保持不变
    op.drop_constraint(
        "fk_profile_memories_profile_id_profiles",
        "profile_memories",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_profile_memories_tenant_id_profiles",
        "profile_memories",
        "profiles",
        ["tenant_id", "profile_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )
    # 单列索引由复合索引取代；现有查询均同时按 tenant_id 过滤，
    # 可以直接命中 (tenant_id, profile_id) 复合索引
    op.drop_index(
        "ix_profile_memories_profile_id", table_name="profile_memories"
    )
    op.create_index(
        "ix_profile_memories_tenant_profile",
        "profile_memories",
        ["tenant_id", "profile_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_profile_memories_tenant_profile", table_name="profile_memories"
    )
    op.create_index(
        "ix_profile_memories_profile_id",
        "profile_memories",
        ["profile_id"],
    )
    op.drop_constraint(
        "fk_profile_memories_tenant_id_profiles",
        "profile_memories",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_profile_memories_profile_id_profiles",
        "profile_memories",
        "profiles",
        ["profile_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint("uq_profiles_tenant_id_id", "profiles", type_="unique")
