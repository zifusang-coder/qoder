import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.agent.registry import get_tool_registry
from app.config import get_settings
from app.llm.gateway import load_gateway_default
from app.models.agent import AgentRun
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishJob
from app.models.settings import LLMProvider
from app.models.user import User
from app.models.workflow import (
    Workflow,
    WorkflowNodeRun,
    WorkflowRun,
    WorkflowVersion,
)
from app.seed import ensure_admin
from app.skills.registry import get_registry
from app.storage import get_storage
from app.workflow.engine import EngineDeps, run_workflow

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_WORKFLOW_E2E") != "1",
    reason="设置 RUN_WORKFLOW_E2E=1 且配置可用 LLM 提供商后才调用真实模型",
)

def graph_for(content_id: uuid.UUID) -> dict:
    return {
        "nodes": [
            {
                "id": "in",
                "type": "input",
                "position": {"x": 0, "y": 0},
                "data": {
                    "fields": [
                        {"key": "topic", "type": "string", "required": True}
                    ]
                },
            },
            {
                "id": "writer",
                "type": "agent",
                "position": {"x": 240, "y": 0},
                "data": {
                    "goal": (
                        "围绕 {{inputs.topic}} 为主题：先用 project.create 建一个"
                        "标题为「E2E 工作流选题」的项目；再为已有内容"
                        f" {content_id} 调用 content.create_version 写一版小红书图文；"
                        "最后为这条内容调用 publish.create_draft_job"
                        "（platform 传 xiaohongshu）创建发布草稿。不要真正发布。"
                    ),
                    "tools": [
                        "project.create",
                        "content.create_version",
                        "publish.create_draft_job",
                    ],
                    "budget": {"max_steps": 8, "max_seconds": 600},
                    "retry": 0,
                },
            },
            {
                "id": "out",
                "type": "output",
                "position": {"x": 480, "y": 0},
                "data": {},
            },
        ],
        "edges": [
            {"id": "in-writer", "source": "in", "target": "writer"},
            {"id": "writer-out", "source": "writer", "target": "out"},
        ],
    }


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
        pytest.fail("RUN_WORKFLOW_E2E=1 需要开发库中配置默认 LLM 提供商")
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


async def test_workflow_reaches_draft_with_real_llm(db_session):
    await ensure_admin(db_session)
    user = (
        await db_session.execute(
            select(User).where(User.role == "owner").limit(1)
        )
    ).scalar_one()
    settings = get_settings()
    await _copy_default_provider(db_session, user.tenant_id)
    gateway = await load_gateway_default(db_session, user.tenant_id)

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

    graph = graph_for(seeded_content.id)
    workflow = Workflow(
        tenant_id=user.tenant_id,
        name="E2E 工作流",
        description="真实模型端到端",
        created_by=user.id,
        draft_graph=graph,
        draft_revision=1,
    )
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=user.tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph=graph,
        source_revision=1,
        published_by=user.id,
        published_at=datetime.now(UTC),
        note="E2E",
    )
    db_session.add(version)
    await db_session.flush()
    run = WorkflowRun(
        tenant_id=user.tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        trigger="manual",
        triggered_by=user.id,
        status="queued",
        inputs={"topic": "秋季护肤"},
        policy={},
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    await db_session.commit()

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    deps = EngineDeps(
        gateway=gateway,
        tool_registry=get_tool_registry(),
        skill_registry=get_registry(),
        storage=get_storage(),
        queue=None,
        settings=settings,
    )
    outcome = await run_workflow(factory, run.id, deps=deps)
    assert outcome in {"succeeded", "waiting_approval"}

    await db_session.refresh(run)
    if run.status == "waiting_approval":
        pytest.skip(
            "模型请求人工确认，审批链路已由 test_workflow_agent_node.py 覆盖"
        )

    assert run.status == "succeeded", run.error_message
    node_run = (
        await db_session.execute(
            select(WorkflowNodeRun).where(
                WorkflowNodeRun.run_id == run.id,
                WorkflowNodeRun.node_id == "writer",
            )
        )
    ).scalars().one()
    assert node_run.status == "ok"
    assert node_run.agent_run_id is not None
    assert run.result["items"][0]["node_id"] == "writer"
    artifacts = node_run.output["result"]["artifacts"]
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

    child = (
        await db_session.execute(
            select(AgentRun).where(AgentRun.workflow_run_id == run.id)
        )
    ).scalars().one()
    assert child.mode == "node"
    assert child.status == "succeeded"
    assert run.usage.get("nodes", 0) >= 1
