import uuid
from datetime import datetime

from sqlalchemy import (
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


class AgentRun(TenantScoped):
    __tablename__ = "agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_agent_runs_tenant_key"
        ),
        Index(
            "ix_agent_runs_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), index=True)
    goal: Mapped[str] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(16), default="single")
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    plan: Mapped[dict] = mapped_column(JSONType, default=dict)
    messages: Mapped[dict] = mapped_column(JSONType, default=dict)
    result_text: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    error_code: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    budget: Mapped[dict] = mapped_column(JSONType, default=dict)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    policy: Mapped[dict] = mapped_column(JSONType, default=dict)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentStep(TenantScoped):
    __tablename__ = "agent_steps"
    __table_args__ = (
        UniqueConstraint("run_id", "step_index", name="uq_agent_steps_run_step"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
    )
    step_index: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    tool_name: Mapped[str] = mapped_column(String(100), default="")
    call_id: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(16), default="running")
    input: Mapped[dict] = mapped_column(JSONType, default=dict)
    output: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentApproval(TenantScoped):
    __tablename__ = "agent_approvals"
    __table_args__ = (Index("ix_agent_approvals_tenant_status", "tenant_id", "status"),)

    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        index=True,
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("agent_steps.id", ondelete="CASCADE"),
        index=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100))
    risk: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
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


class AgentPolicy(TenantScoped):
    __tablename__ = "agent_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_agent_policies_tenant_id"),
    )

    overrides: Mapped[dict] = mapped_column(JSONType, default=dict)
