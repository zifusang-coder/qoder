"""add agent team

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("parent_run_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "agent_runs",
        sa.Column("role", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "agent_runs",
        sa.Column("team_state", JSON, nullable=False, server_default="{}"),
    )
    op.create_index("ix_agent_runs_parent_run_id", "agent_runs", ["parent_run_id"])
    op.create_foreign_key(
        "fk_agent_runs_parent_run_id", "agent_runs", "agent_runs",
        ["parent_run_id"], ["id"], ondelete="CASCADE",
    )

    op.create_table(
        "agent_team_entries",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("content", JSON, nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["agent_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_agent_team_entries_run_seq"),
    )
    op.create_index("ix_agent_team_entries_tenant_id", "agent_team_entries", ["tenant_id"])
    op.create_index("ix_agent_team_entries_run_id", "agent_team_entries", ["run_id"])

    op.create_table(
        "agent_team_roles",
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("tools", JSON, nullable=False),
        sa.Column("budget", JSON, nullable=False),
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "role", name="uq_agent_team_roles_tenant_role"),
    )
    op.create_index("ix_agent_team_roles_tenant_id", "agent_team_roles", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_team_roles_tenant_id", table_name="agent_team_roles")
    op.drop_table("agent_team_roles")
    op.drop_index("ix_agent_team_entries_run_id", table_name="agent_team_entries")
    op.drop_index("ix_agent_team_entries_tenant_id", table_name="agent_team_entries")
    op.drop_table("agent_team_entries")
    op.drop_constraint("fk_agent_runs_parent_run_id", "agent_runs", type_="foreignkey")
    op.drop_index("ix_agent_runs_parent_run_id", table_name="agent_runs")
    op.drop_column("agent_runs", "team_state")
    op.drop_column("agent_runs", "role")
    op.drop_column("agent_runs", "parent_run_id")