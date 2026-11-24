import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.models.agent import AgentApproval, AgentPolicy, AgentRun, AgentStep
from app.models.media import MediaJob
from app.models.project import Project


async def _run(db_session, **overrides):
    values = {
        "tenant_id": "00000000-0000-0000-0000-000000000001",
        "user_id": uuid.uuid4(),
        "goal": "为一篇新文章做选题并创建草稿",
    }
    values.update(overrides)
    run = AgentRun(**values)
    db_session.add(run)
    await db_session.commit()
    await db_session.refresh(run)
    return run


async def test_agent_run_defaults(db_session):
    run = await _run(db_session)
    assert run.status == "queued"
    assert run.mode == "single"
    assert run.step_count == 0
    assert run.attempts == 0
    assert run.plan == {}
    assert run.messages == {}
    assert run.budget == {}
    assert run.usage == {}
    assert run.policy == {}
    assert run.error_code == ""


async def test_agent_samples_are_tenant_scoped(db_session):
    run = await _run(db_session)
    step = AgentStep(
        tenant_id=run.tenant_id,
        run_id=run.id,
        step_index=1,
        kind="tool",
        tool_name="profile.list",
        status="ok",
        input={},
        output={"summary": "ok"},
    )
    db_session.add(step)
    await db_session.commit()
    await db_session.refresh(step)
    assert step.tenant_id == run.tenant_id
    assert step.call_id == ""


async def test_agent_step_index_unique_per_run(db_session):
    run = await _run(db_session)
    db_session.add(
        AgentStep(
            tenant_id=run.tenant_id,
            run_id=run.id,
            step_index=1,
            kind="llm",
            status="ok",
        )
    )
    await db_session.commit()
    db_session.add(
        AgentStep(
            tenant_id=run.tenant_id,
            run_id=run.id,
            step_index=1,
            kind="llm",
            status="ok",
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_agent_steps_cascade_with_run(db_session):
    run = await _run(db_session)
    db_session.add(
        AgentStep(
            tenant_id=run.tenant_id,
            run_id=run.id,
            step_index=1,
            kind="llm",
            status="ok",
        )
    )
    await db_session.commit()
    await db_session.delete(run)
    await db_session.commit()
    remaining = (await db_session.execute(select(AgentStep))).scalars().all()
    assert remaining == []


async def test_agent_approval_lifecycle_fields(db_session):
    run = await _run(db_session)
    step = AgentStep(
        tenant_id=run.tenant_id,
        run_id=run.id,
        step_index=1,
        kind="approval",
        tool_name="publish.create_draft_job",
        status="running",
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=run.tenant_id,
        run_id=run.id,
        step_id=step.id,
        tool_name="publish.create_draft_job",
        risk="low",
        payload={"content_id": str(uuid.uuid4())},
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(approval)
    await db_session.commit()
    await db_session.refresh(approval)
    assert approval.status == "pending"
    assert approval.decided_by is None
    assert approval.decided_at is None
    assert approval.note == ""


async def test_agent_policy_unique_per_tenant(db_session):
    db_session.add(
        AgentPolicy(
            tenant_id="00000000-0000-0000-0000-000000000001",
            overrides={"tool:project.create": "confirm"},
        )
    )
    await db_session.commit()
    db_session.add(
        AgentPolicy(
            tenant_id="00000000-0000-0000-0000-000000000001",
            overrides={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


async def test_agent_run_idempotency_key_unique_per_tenant(db_session):
    await _run(db_session, idempotency_key="k1")
    with pytest.raises(IntegrityError):
        await _run(db_session, idempotency_key="k1")
    await db_session.rollback()


async def test_media_job_agent_key_persists(db_session):
    project = Project(
        tenant_id="00000000-0000-0000-0000-000000000001",
        title="项目",
    )
    db_session.add(project)
    await db_session.commit()
    job = MediaJob(
        tenant_id=project.tenant_id,
        project_id=project.id,
        skill_id="short-video",
        agent_key="run-1:3",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    assert job.agent_key == "run-1:3"


def test_agent_settings_have_defaults():
    settings = get_settings()
    assert settings.agent_max_steps_default == 12
    assert settings.agent_max_steps_ceiling == 30
    assert settings.agent_max_tool_calls == 20
    assert settings.agent_max_seconds == 300
    assert settings.agent_token_budget_default == 60000
    assert settings.agent_daily_token_quota == 500000
    assert settings.agent_approval_ttl_hours == 24
    assert settings.agent_run_stale_seconds == 300
