"""add workflow engine

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-11-15
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None

JSON = sa.JSON().with_variant(
    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
)


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("workflow_run_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.add_column(
        "agent_runs",
        sa.Column("workflow_node_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_agent_runs_workflow_run_id", "agent_runs", ["workflow_run_id"]
    )

    op.create_table(
        "workflows",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("draft_graph", JSON, nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
    )
    op.create_index("ix_workflows_tenant_id", "workflows", ["tenant_id"])
    op.create_index(
        "ix_workflows_tenant_archived", "workflows", ["tenant_id", "archived_at"]
    )

    op.create_table(
        "workflow_versions",
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("graph", JSON, nullable=False),
        sa.Column("source_revision", sa.Integer(), nullable=False),
        sa.Column("published_by", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
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
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "version", name="uq_workflow_versions_workflow_version"
        ),
    )
    op.create_index(
        "ix_workflow_versions_tenant_id", "workflow_versions", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_versions_workflow_id", "workflow_versions", ["workflow_id"]
    )

    op.create_table(
        "workflow_schedules",
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("cadence", JSON, nullable=False),
        sa.Column("inputs", JSON, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.Uuid(as_uuid=True), nullable=False),
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
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_schedules_tenant_id", "workflow_schedules", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_schedules_workflow_id", "workflow_schedules", ["workflow_id"]
    )
    op.create_index(
        "ix_workflow_schedules_next_run_at", "workflow_schedules", ["next_run_at"]
    )
    op.create_index(
        "ix_workflow_schedules_next_run",
        "workflow_schedules",
        ["tenant_id", "next_run_at"],
    )

    op.create_table(
        "workflow_runs",
        sa.Column("workflow_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workflow_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("triggered_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("schedule_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("inputs", JSON, nullable=False),
        sa.Column("policy", JSON, nullable=False),
        sa.Column("result", JSON, nullable=False),
        sa.Column("error_code", sa.String(length=100), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("usage", JSON, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("resume_count", sa.Integer(), nullable=False),
        sa.Column("step_counter", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            ["workflow_id"], ["workflows.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_version_id"], ["workflow_versions.id"]
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["workflow_schedules.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"
        ),
        sa.UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_workflow_runs_schedule_slot"
        ),
    )
    op.create_index("ix_workflow_runs_tenant_id", "workflow_runs", ["tenant_id"])
    op.create_index("ix_workflow_runs_workflow_id", "workflow_runs", ["workflow_id"])
    op.create_index("ix_workflow_runs_status", "workflow_runs", ["status"])
    op.create_index(
        "ix_workflow_runs_tenant_workflow_created",
        "workflow_runs",
        ["tenant_id", "workflow_id", "created_at"],
    )

    op.create_table(
        "workflow_node_runs",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("tool_step_index", sa.Integer(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("input", JSON, nullable=False),
        sa.Column("output", JSON, nullable=False),
        sa.Column("raw_output", JSON, nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
            ["run_id"], ["workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "node_id",
            "attempt",
            name="uq_workflow_node_runs_run_node_attempt",
        ),
    )
    op.create_index(
        "ix_workflow_node_runs_tenant_id", "workflow_node_runs", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_node_runs_run_id", "workflow_node_runs", ["run_id"]
    )

    op.create_table(
        "workflow_approvals",
        sa.Column("run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("node_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("risk", sa.String(length=16), nullable=False),
        sa.Column("payload", JSON, nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_by", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
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
            ["run_id"], ["workflow_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_approvals_tenant_id", "workflow_approvals", ["tenant_id"]
    )
    op.create_index(
        "ix_workflow_approvals_run_id", "workflow_approvals", ["run_id"]
    )
    op.create_index(
        "ix_workflow_approvals_tenant_status",
        "workflow_approvals",
        ["tenant_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("workflow_approvals")
    op.drop_table("workflow_node_runs")
    op.drop_table("workflow_runs")
    op.drop_table("workflow_schedules")
    op.drop_table("workflow_versions")
    op.drop_table("workflows")
    op.drop_index("ix_agent_runs_workflow_run_id", table_name="agent_runs")
    op.drop_column("agent_runs", "workflow_node_id")
    op.drop_column("agent_runs", "workflow_run_id")
