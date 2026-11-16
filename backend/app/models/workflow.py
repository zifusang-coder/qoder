import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import JSONType, TenantScoped


class Workflow(TenantScoped):
    __tablename__ = "workflows"
    __table_args__ = (
        Index("ix_workflows_tenant_archived", "tenant_id", "archived_at"),
    )

    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    draft_graph: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowVersion(TenantScoped):
    __tablename__ = "workflow_versions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id", "version", name="uq_workflow_versions_workflow_version"
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
    )
    version: Mapped[int] = mapped_column(Integer)
    graph: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_revision: Mapped[int] = mapped_column(Integer)
    published_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    note: Mapped[str] = mapped_column(Text, default="")


class WorkflowSchedule(TenantScoped):
    __tablename__ = "workflow_schedules"
    __table_args__ = (
        Index("ix_workflow_schedules_next_run", "tenant_id", "next_run_at"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_versions.id", ondelete="CASCADE")
    )
    cadence: Mapped[dict] = mapped_column(JSONType, default=dict)
    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))


class WorkflowRun(TenantScoped):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"
        ),
        UniqueConstraint(
            "schedule_id", "scheduled_for", name="uq_workflow_runs_schedule_slot"
        ),
        Index(
            "ix_workflow_runs_tenant_workflow_created",
            "tenant_id",
            "workflow_id",
            "created_at",
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflows.id", ondelete="CASCADE"),
        index=True,
    )
    workflow_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_versions.id")
    )
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_schedules.id", ondelete="SET NULL"),
        nullable=True,
    )
    scheduled_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    policy: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    step_counter: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowNodeRun(TenantScoped):
    __tablename__ = "workflow_node_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "node_id",
            "attempt",
            name="uq_workflow_node_runs_run_node_attempt",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(64))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")
    tool_step_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    input: Mapped[dict] = mapped_column(JSONType, default=dict)
    output: Mapped[dict] = mapped_column(JSONType, default=dict)
    raw_output: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class WorkflowApproval(TenantScoped):
    __tablename__ = "workflow_approvals"
    __table_args__ = (
        Index("ix_workflow_approvals_tenant_status", "tenant_id", "status"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        index=True,
    )
    node_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    tool_name: Mapped[str] = mapped_column(String(100), default="")
    risk: Mapped[str] = mapped_column(String(16), default="")
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    note: Mapped[str] = mapped_column(Text, default="")
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
