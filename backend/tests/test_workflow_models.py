import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models.agent import AgentRun
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)


def test_workflow_settings_defaults():
    settings = Settings(_env_file=None)
    assert settings.workflow_max_parallel_nodes == 3
    assert settings.workflow_time_slice_seconds == 480
    assert settings.workflow_max_seconds == 3600
    assert settings.workflow_approval_ttl_hours == 24
    assert settings.workflow_max_active_runs_per_user == 2
    assert settings.workflow_run_stale_seconds == 300


def test_agent_run_has_workflow_columns():
    assert "workflow_run_id" in AgentRun.__table__.columns
    assert "workflow_node_id" in AgentRun.__table__.columns
    assert AgentRun.__table__.columns["workflow_run_id"].nullable is True
    assert AgentRun.__table__.columns["workflow_node_id"].nullable is True


async def test_node_attempt_is_unique_per_run(db_session, tenant_factory):
    tenant = await tenant_factory()
    workflow = Workflow(
        tenant_id=tenant.id, name="测试", created_by=uuid.uuid4()
    )
    db_session.add(workflow)
    await db_session.commit()
    version = WorkflowVersion(
        tenant_id=tenant.id,
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=1,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.commit()
    run = WorkflowRun(
        tenant_id=tenant.id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        inputs={},
        policy={},
    )
    db_session.add(run)
    await db_session.commit()
    db_session.add(
        WorkflowNodeRun(
            tenant_id=tenant.id, run_id=run.id, node_id="n1", attempt=1,
            kind="tool", status="ok",
        )
    )
    await db_session.commit()
    db_session.add(
        WorkflowNodeRun(
            tenant_id=tenant.id, run_id=run.id, node_id="n1", attempt=1,
            kind="tool", status="ok",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()
