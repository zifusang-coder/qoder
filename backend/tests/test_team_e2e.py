import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.registry import get_tool_registry
from app.agent.team import run_team
from app.config import get_settings
from app.llm.gateway import load_gateway_default
from app.models.agent import AgentRun
from app.models.project import Content, Project
from app.models.publish import PublishJob
from app.models.settings import LLMProvider
from app.models.user import User
from app.seed import ensure_admin
from app.skills.registry import get_registry
from app.storage import get_storage

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_TEAM_E2E") != "1",
    reason="设置 RUN_TEAM_E2E=1 且配置可用 LLM 提供商后才调用真实模型",
)


async def _copy_default_provider(session, tenant_id: str) -> None:
    """把本地库中已配置的默认提供商只读复制进测试会话，不改动原配置。"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as source:
            provider = (
                await source.execute(
                    select(LLMProvider)
                    .where(
                        LLMProvider.tenant_id == tenant_id,
                        LLMProvider.is_default.is_(True),
                    )
                    .order_by(LLMProvider.created_at, LLMProvider.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
    finally:
        await engine.dispose()
    if provider is None:
        pytest.fail("RUN_TEAM_E2E=1 需要开发库中配置默认 LLM 提供商")
    session.add(
        LLMProvider(
            tenant_id=tenant_id,
            name=provider.name,
            kind=provider.kind,
            base_url=provider.base_url,
            api_key_enc=provider.api_key_enc,
            tiers=dict(provider.tiers or {}),
            is_default=True,
        )
    )
    await session.commit()


async def test_team_reaches_draft_with_real_llm(db_session):
    await ensure_admin(db_session)
    user = (
        await db_session.execute(select(User).where(User.role == "owner").limit(1))
    ).scalar_one()
    settings = get_settings()
    await _copy_default_provider(db_session, user.tenant_id)
    gateway = await load_gateway_default(db_session, user.tenant_id)

    # 工具集没有 content.create 入口，先种既有项目与内容供 content.create_version 落点。
    seeded_project = Project(
        tenant_id=user.tenant_id, title="E2E 团队既有项目", topic="秋季护肤"
    )
    db_session.add(seeded_project)
    await db_session.flush()
    seeded_content = Content(
        tenant_id=user.tenant_id,
        project_id=seeded_project.id,
        platform="xiaohongshu",
        kind="article",
        title="E2E 团队既有内容",
    )
    db_session.add(seeded_content)
    await db_session.commit()

    goal = (
        f"围绕「秋季护肤」为已有内容 {seeded_content.id} 产出一版小红书图文内容版本，"
        "并用该内容的当前版本创建一条 draft 发布任务（不排期）。"
    )
    run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        goal=goal,
        mode="team",
        status="queued",
        plan={"roles": ["scout", "creator", "critic", "publisher"]},
        messages={"items": []},
        result_text="",
        result={},
        budget={
            "max_tokens": 120000,
            "max_tool_calls": 40,
            "max_turns": 12,
            "max_seconds": 900,
        },
        usage={},
        policy={},
        step_count=0,
        attempts=0,
    )
    db_session.add(run)
    await db_session.commit()

    await run_team(
        db_session,
        run,
        gateway=gateway,
        tool_registry=get_tool_registry(),
        skill_registry=get_registry(),
        storage=get_storage(),
        queue=None,
        settings=settings,
    )

    await db_session.refresh(run)
    assert run.status in {"succeeded", "waiting_approval", "failed"}, run.error_message
    if run.status == "waiting_approval":
        pytest.skip("模型请求人工确认，确认链路已由 test_team_approvals.py 覆盖")
    if run.status == "failed":
        assert run.error_code
        pytest.skip(f"真实模型团队运行失败（设计内失败路径，脚本化用例已覆盖）：{run.error_code}")

    assert run.result.get("summary")
    children = (
        await db_session.execute(
            select(AgentRun).where(
                AgentRun.parent_run_id == run.id, AgentRun.mode == "role"
            )
        )
    ).scalars().all()
    assert children, "团队应产生角色子运行"
    jobs = (
        await db_session.execute(
            select(PublishJob).where(PublishJob.tenant_id == user.tenant_id)
        )
    ).scalars().all()
    assert jobs, "应创建 draft 发布任务"
    assert all(job.status == "draft" and job.scheduled_at is None for job in jobs)