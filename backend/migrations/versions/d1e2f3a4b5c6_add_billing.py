"""add billing plans and statements

Revision ID: d1e2f3a4b5c6
Revises: c9d0e1f2a3b4
Create Date: 2026-11-25
"""

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.billing.plans import FREE_PLAN_ID, PLAN_SEEDS

revision = "d1e2f3a4b5c6"
down_revision = "c9d0e1f2a3b4"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    # 套餐是平台级目录，租户通过 tenants.plan_id 引用；code 是业务主键，
    # 模型用 unique=True + index=True，落到库里就是一个唯一索引。
    op.create_table(
        "plans",
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("limits", JSON, nullable=False),
        sa.Column("prices", JSON, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_plans")),
    )
    op.create_index("ix_plans_code", "plans", ["code"], unique=True)

    # 种子值与 app.billing.plans.PLAN_SEEDS 保持一致，固定 id 让租户回填
    # 和代码里的 FREE_PLAN_ID 指向同一行。
    op.bulk_insert(
        sa.table(
            "plans",
            sa.column("id", sa.Uuid(as_uuid=True)),
            sa.column("code", sa.String()),
            sa.column("name", sa.String()),
            sa.column("description", sa.Text()),
            sa.column("price_cents", sa.Integer()),
            sa.column("currency", sa.String()),
            sa.column("limits", JSON),
            sa.column("prices", JSON),
            sa.column("status", sa.String()),
        ),
        [
            {
                "id": uuid.UUID(seed["id"]),
                "code": seed["code"],
                "name": seed["name"],
                "description": seed["description"],
                "price_cents": seed["price_cents"],
                "currency": "CNY",
                "limits": dict(seed["limits"]),
                "prices": dict(seed["prices"]),
                "status": "active",
            }
            for seed in PLAN_SEEDS
        ],
    )

    # 先加可空列、回填到免费版、再收紧为 NOT NULL，最后挂外键，
    # 避免已有租户行在加列时违反约束。
    op.add_column(
        "tenants", sa.Column("plan_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "tenants",
        sa.Column(
            "plan_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute(
        sa.text("UPDATE tenants SET plan_id = :plan_id").bindparams(
            plan_id=uuid.UUID(FREE_PLAN_ID)
        )
    )
    op.alter_column("tenants", "plan_id", nullable=False)
    op.create_foreign_key(
        "fk_tenants_plan_id_plans",
        "tenants",
        "plans",
        ["plan_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "billing_statements",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("plan_code", sa.String(length=50), nullable=False),
        sa.Column("plan_name", sa.String(length=100), nullable=False),
        sa.Column("plan_price_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("limits", JSON, nullable=False),
        sa.Column("prices", JSON, nullable=False),
        sa.Column("usage", JSON, nullable=False),
        sa.Column("charges", JSON, nullable=False),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("closed_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_billing_statements")),
        sa.UniqueConstraint(
            "tenant_id", "period", name="uq_billing_statements_tenant_period"
        ),
    )
    op.create_index(
        "ix_billing_statements_tenant_id", "billing_statements", ["tenant_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_billing_statements_tenant_id", table_name="billing_statements"
    )
    op.drop_table("billing_statements")

    op.drop_constraint("fk_tenants_plan_id_plans", "tenants", type_="foreignkey")
    op.drop_column("tenants", "plan_changed_at")
    op.drop_column("tenants", "plan_id")

    op.drop_index("ix_plans_code", table_name="plans")
    op.drop_table("plans")
