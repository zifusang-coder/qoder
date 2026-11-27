import uuid
from datetime import UTC, datetime

from app.billing.dimensions import DIMENSION_KEYS
from app.billing.usage import compute_usage
from app.models.agent import AgentRun
from app.models.media import MediaJob
from app.models.project import Project
from app.models.skill import SkillRun
from app.models.workflow import Workflow, WorkflowRun, WorkflowVersion


async def _skill_run(session, tenant_id, created_at, prompt=0, completion=0):
    session.add(
        SkillRun(
            tenant_id=tenant_id,
            skill_id="title-hook",
            prompt_tokens=prompt,
            completion_tokens=completion,
            created_at=created_at,
        )
    )


async def _agent_run(session, tenant_id, created_at, mode="single", usage=None):
    session.add(
        AgentRun(
            tenant_id=tenant_id,
            user_id=uuid.uuid4(),
            goal="g",
            mode=mode,
            usage=usage or {},
            created_at=created_at,
        )
    )


async def _workflow_run(session, tenant_id, created_at, usage=None):
    workflow = Workflow(
        tenant_id=tenant_id, name="流", created_by=uuid.uuid4()
    )
    session.add(workflow)
    await session.flush()
    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph={},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=created_at,
    )
    session.add(version)
    await session.flush()
    session.add(
        WorkflowRun(
            tenant_id=tenant_id,
            workflow_id=workflow.id,
            workflow_version_id=version.id,
            usage=usage or {},
            created_at=created_at,
        )
    )


async def _media_job(
    session, tenant_id, created_at, started, finished, status="succeeded"
):
    project = Project(tenant_id=tenant_id, title="项目")
    session.add(project)
    await session.flush()
    session.add(
        MediaJob(
            tenant_id=tenant_id,
            project_id=project.id,
            skill_id="short-video",
            status=status,
            started_at=started,
            finished_at=finished,
            created_at=created_at,
        )
    )


async def test_usage_aggregates_period_and_tenant(db_session, tenant_factory):
    tenant = await tenant_factory()
    other = await tenant_factory()
    inside = datetime(2026, 10, 15, tzinfo=UTC)
    outside = datetime(2026, 9, 15, tzinfo=UTC)
    await _skill_run(db_session, tenant.id, inside, 1200, 800)
    await _skill_run(db_session, tenant.id, outside, 9000, 9000)
    await _skill_run(db_session, other.id, inside, 5000, 5000)
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        usage={"prompt_tokens": 300, "completion_tokens": 200},
    )
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        mode="role",
        usage={"prompt_tokens": 100, "completion_tokens": 50},
    )
    await _workflow_run(db_session, tenant.id, inside)
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside,
        inside.replace(minute=20),
    )
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside,
        inside.replace(minute=30),
        status="failed",
    )
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-10")
    assert usage["llm.prompt_tokens"] == 1600
    assert usage["llm.completion_tokens"] == 1050
    assert usage["run.skill"] == 1
    assert usage["run.agent"] == 1
    assert usage["run.workflow"] == 1
    assert usage["media.jobs"] == 2
    assert usage["media.seconds"] == 1200


async def test_usage_respects_beijing_month_boundary(db_session, tenant_factory):
    tenant = await tenant_factory()
    before = datetime(2026, 10, 31, 15, 59, 59, tzinfo=UTC)
    at_start = datetime(2026, 10, 31, 16, 0, 0, tzinfo=UTC)
    at_end = datetime(2026, 11, 30, 16, 0, 0, tzinfo=UTC)
    await _skill_run(db_session, tenant.id, before, 100, 100)
    await _skill_run(db_session, tenant.id, at_start, 200, 200)
    await _skill_run(db_session, tenant.id, at_end, 400, 400)
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-11")
    assert usage["llm.prompt_tokens"] == 200
    assert usage["run.skill"] == 1

    # 2026-10-31T15:59:59Z 是北京时间 10-31 23:59:59，仍属 2026-10；
    # 2026-10-31T16:00:00Z 是北京时间 11-01 00:00:00，已属 2026-11（不含在上一条里）。
    october = await compute_usage(db_session, tenant.id, "2026-10")
    assert october["llm.prompt_tokens"] == 100
    assert october["run.skill"] == 1

    # 2026-11-30T16:00:00Z 是北京时间 12-01 00:00:00，是 2026-12 的起点，不算进 2026-11。
    december = await compute_usage(db_session, tenant.id, "2026-12")
    assert december["llm.prompt_tokens"] == 400
    assert december["run.skill"] == 1


async def test_usage_returns_every_dimension_key_with_zeros(db_session, tenant_factory):
    tenant = await tenant_factory()
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-10")
    assert set(usage) == set(DIMENSION_KEYS)
    assert len(usage) == 7
    for key in DIMENSION_KEYS:
        assert usage[key] == 0
        assert isinstance(usage[key], int)


async def test_workflow_usage_tokens_are_not_counted(db_session, tenant_factory):
    tenant = await tenant_factory()
    inside = datetime(2026, 10, 15, tzinfo=UTC)
    await _workflow_run(
        db_session,
        tenant.id,
        inside,
        usage={"prompt_tokens": 777, "completion_tokens": 888},
    )
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-10")
    assert usage["run.workflow"] == 1
    assert usage["llm.prompt_tokens"] == 0
    assert usage["llm.completion_tokens"] == 0


async def test_agent_role_mode_tokens_counted_without_run_count(
    db_session, tenant_factory
):
    tenant = await tenant_factory()
    inside = datetime(2026, 10, 15, tzinfo=UTC)
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        mode="role",
        usage={"prompt_tokens": 11, "completion_tokens": 7},
    )
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        mode="team",
        usage={"prompt_tokens": 13, "completion_tokens": 5},
    )
    await _agent_run(
        db_session,
        tenant.id,
        inside,
        mode="role",
        usage={"prompt_tokens": "12", "completion_tokens": None},
    )
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-10")
    # role 子运行不计入 run.agent，但 token 仍计费
    assert usage["run.agent"] == 1
    assert usage["llm.prompt_tokens"] == 36
    assert usage["llm.completion_tokens"] == 12


async def test_media_seconds_only_for_succeeded_jobs_with_timestamps(
    db_session, tenant_factory
):
    tenant = await tenant_factory()
    inside = datetime(2026, 10, 15, tzinfo=UTC)
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside,
        inside.replace(minute=1),
    )
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside,
        inside.replace(minute=2),
    )
    await _media_job(
        db_session, tenant.id, inside, inside.replace(minute=4), None
    )
    await _media_job(
        db_session, tenant.id, inside, None, None, status="queued"
    )
    await _media_job(
        db_session,
        tenant.id,
        inside,
        inside.replace(minute=5),
        inside.replace(minute=9),
        status="failed",
    )
    await db_session.commit()

    usage = await compute_usage(db_session, tenant.id, "2026-10")
    # 所有状态都计入 media.jobs
    assert usage["media.jobs"] == 5
    # 只有 succeeded 且 started_at/finished_at 都存在的任务贡献时长：60 + 120
    assert usage["media.seconds"] == 180
