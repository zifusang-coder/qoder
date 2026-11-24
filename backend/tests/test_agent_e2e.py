import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.registry import get_tool_registry
from app.agent.runtime import run_agent
from app.config import get_settings
from app.llm.gateway import load_gateway_default
from app.models.agent import AgentRun
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishJob
from app.models.settings import LLMProvider
from app.models.user import User
from app.seed import ensure_admin
from app.skills.registry import get_registry
from app.storage import get_storage

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_AGENT_E2E") != "1",
    reason="设置 RUN_AGENT_E2E=1 且配置可用 LLM 提供商后才调用真实模型",
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
        pytest.fail("RUN_AGENT_E2E=1 需要开发库中配置默认 LLM 提供商")
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


async def test_agent_reaches_draft_with_real_llm(db_session):
    await ensure_admin(db_session)
    user = (
        await db_session.execute(
            select(User).where(User.role == "owner").limit(1)
        )
    ).scalar_one()
    settings = get_settings()
    await _copy_default_provider(db_session, user.tenant_id)
    gateway = await load_gateway_default(db_session, user.tenant_id)

    # 本用例固定走 content.create_version 路径，
    # 因此先为租户种一条既有项目与既有内容，让 content.create_version 有落点。
    seeded_project = Project(
        tenant_id=user.tenant_id, title="E2E 既有项目", topic="演示"
    )
    db_session.add(seeded_project)
    await db_session.flush()
    seeded_content = Content(
        tenant_id=user.tenant_id,
        project_id=seeded_project.id,
        platform="xiaohongshu",
        kind="article",
        title="E2E 既有内容",
    )
    db_session.add(seeded_content)
    await db_session.commit()
    seeded_content_id = seeded_content.id

    goal = (
        "先调用 project.create 建一个标题为「E2E 秋季护肤」的项目；"
        f"再为已有内容 {seeded_content_id} 调用 content.create_version "
        "写一版小红书图文；"
        "最后调用 publish.create_draft_job 用该内容的当前版本创建一条发布草稿。"
    )
    run = AgentRun(
        id=uuid.uuid4(),
        tenant_id=user.tenant_id,
        user_id=user.id,
        goal=goal,
        status="queued",
        plan={},
        messages={"items": []},
        result_text="",
        result={},
        budget={
            "max_steps": 10,
            "max_tokens": 100_000,
            "max_tool_calls": 12,
            "max_seconds": 600,
        },
        usage={},
        policy={},
        step_count=0,
        attempts=0,
    )
    db_session.add(run)
    await db_session.commit()

    await run_agent(
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
    assert run.status in {"succeeded", "waiting_approval"}, run.error_message
    if run.status == "waiting_approval":
        pytest.skip("模型请求人工确认，确认链路已由 test_agent_approvals.py 覆盖")

    assert run.step_count >= 2
    assert run.usage["steps"] == run.step_count
    artifacts = run.result["artifacts"]
    types = {item["type"] for item in artifacts}
    assert {"project", "content", "publish_job"} <= types

    project_id = next(
        item["id"] for item in artifacts if item["type"] == "project"
    )
    project = await db_session.get(Project, uuid.UUID(project_id))
    assert project is not None and project.tenant_id == user.tenant_id

    content_ids = [
        uuid.UUID(item["id"]) for item in artifacts if item["type"] == "content"
    ]
    contents = (
        (
            await db_session.execute(
                select(Content).where(
                    Content.id.in_(content_ids),
                    Content.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert contents, "应至少生成一条内容"

    versions = (
        (
            await db_session.execute(
                select(ContentVersion).where(
                    ContentVersion.content_id.in_(content_ids),
                    ContentVersion.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert versions, "应至少生成一版内容版本"

    jobs = (
        (
            await db_session.execute(
                select(PublishJob).where(
                    PublishJob.content_id.in_(content_ids),
                    PublishJob.tenant_id == user.tenant_id,
                )
            )
        )
        .scalars()
        .all()
    )
    assert jobs, "应创建发布草稿任务"
    assert all(job.status == "draft" for job in jobs)
    assert all(job.scheduled_at is None for job in jobs)
