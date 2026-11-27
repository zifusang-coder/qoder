import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.agent import AgentApproval, AgentRun, AgentStep
from app.models.base import DEFAULT_TENANT_ID
from app.models.media import MediaJob
from app.models.project import Content, Project
from app.models.publish import PublishJob
from app.models.workflow import (
    Workflow,
    WorkflowApproval,
    WorkflowRun,
    WorkflowVersion,
)
from app.services import live


async def _seed(db_session, tenant_id: str = DEFAULT_TENANT_ID) -> dict:
    project = Project(tenant_id=tenant_id, title="夏季选题")
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=tenant_id,
        project_id=project.id,
        platform="wechat",
        kind="article",
        title="夏日防晒指南",
    )
    db_session.add(content)
    await db_session.flush()

    review_job = PublishJob(
        tenant_id=tenant_id,
        content_id=content.id,
        content_version=1,
        platform="wechat",
        status="awaiting_review",
        idempotency_key=f"review-{uuid.uuid4()}",
    )
    failed_job = PublishJob(
        tenant_id=tenant_id,
        content_id=content.id,
        content_version=1,
        platform="douyin",
        status="publish_failed",
        error="access_token invalid",
        idempotency_key=f"failed-{uuid.uuid4()}",
    )

    run = AgentRun(
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        goal="写一篇夏日选题周报并提交审批",
        status="waiting_approval",
    )
    db_session.add(run)
    await db_session.flush()
    step = AgentStep(
        tenant_id=tenant_id,
        run_id=run.id,
        step_index=0,
        kind="tool",
        tool_name="publish.approve",
        status="running",
    )
    db_session.add(step)
    await db_session.flush()
    approval = AgentApproval(
        tenant_id=tenant_id,
        run_id=run.id,
        step_id=step.id,
        tool_name="publish.approve",
        risk="medium",
    )

    workflow = Workflow(tenant_id=tenant_id, name="周更流程", created_by=uuid.uuid4())
    db_session.add(workflow)
    await db_session.flush()
    version = WorkflowVersion(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        version=1,
        graph={"nodes": [], "edges": []},
        source_revision=0,
        published_by=uuid.uuid4(),
        published_at=datetime.now(UTC),
    )
    db_session.add(version)
    await db_session.flush()
    workflow_run = WorkflowRun(
        tenant_id=tenant_id,
        workflow_id=workflow.id,
        workflow_version_id=version.id,
        status="waiting_approval",
    )
    db_session.add(workflow_run)
    await db_session.flush()
    workflow_approval = WorkflowApproval(
        tenant_id=tenant_id,
        run_id=workflow_run.id,
        node_id="publish",
        kind="tool",
        tool_name="publish.approve",
        title="确认发布到公众号",
    )

    media = MediaJob(
        tenant_id=tenant_id,
        project_id=project.id,
        skill_id="short-video",
        status="failed",
        error_message="ffmpeg crashed",
        finished_at=datetime.now(UTC) - timedelta(hours=2),
    )
    db_session.add_all([review_job, failed_job, approval, workflow_approval, media])
    await db_session.commit()
    return {
        "project": project,
        "review_job": review_job,
        "failed_job": failed_job,
        "agent_run": run,
        "workflow": workflow,
        "workflow_run": workflow_run,
        "media": media,
    }


async def test_notifications_aggregate_attention_items(client, db_session):
    seeded = await _seed(db_session)

    resp = await client.get("/api/notifications")

    assert resp.status_code == 200
    payload = resp.json()
    items = payload["items"]
    assert payload["counts"] == {"approval": 2, "review": 1, "failure": 2}
    by_id = {item["id"]: item for item in items}

    agent_item = by_id[f"approval:{seeded['agent_run'].id}"]
    assert agent_item["severity"] == "warn"
    assert agent_item["href"] == "/assistant"
    assert "publish.approve" in agent_item["detail"]

    workflow_item = by_id[f"approval:{seeded['workflow_run'].id}"]
    assert workflow_item["href"] == (
        f"/workflows/{seeded['workflow'].id}/runs/{seeded['workflow_run'].id}"
    )
    assert "确认发布到公众号" in workflow_item["detail"]

    review_item = by_id[f"review:{seeded['review_job'].id}"]
    assert review_item["href"] == "/publish"
    assert "夏日防晒指南" in review_item["detail"]

    failed_item = by_id[f"failure:{seeded['failed_job'].id}"]
    assert failed_item["severity"] == "error"
    assert failed_item["href"] == "/publish"
    # 平台错误文本可能回显凭据，通知里只保留内容与平台信息。
    assert "access_token" not in json.dumps(failed_item, ensure_ascii=False)

    media_item = by_id[f"failure:{seeded['media'].id}"]
    assert media_item["href"] == f"/projects/{seeded['project'].id}"
    assert "ffmpeg" not in media_item["detail"]


async def test_notifications_ignore_other_tenants(client, db_session, tenant_factory):
    other = await tenant_factory(name="另一个租户")
    await _seed(db_session, tenant_id=other.id)

    resp = await client.get("/api/notifications")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"] == []
    assert payload["counts"] == {"approval": 0, "review": 0, "failure": 0}


async def test_notifications_ignore_stale_failures(client, db_session):
    seeded = await _seed(db_session)
    seeded["failed_job"].updated_at = datetime.now(UTC) - timedelta(days=30)
    seeded["media"].finished_at = datetime.now(UTC) - timedelta(days=30)
    await db_session.commit()

    resp = await client.get("/api/notifications")

    assert resp.status_code == 200
    ids = {item["id"] for item in resp.json()["items"]}
    assert f"failure:{seeded['failed_job'].id}" not in ids
    assert f"failure:{seeded['media'].id}" not in ids


async def test_notifications_stream_pushes_snapshot(client, monkeypatch, db_session):
    monkeypatch.setattr(
        live,
        "SessionLocal",
        async_sessionmaker(db_session.bind, expire_on_commit=False),
    )
    seeded = await _seed(db_session)

    async with client.stream(
        "GET", "/api/notifications/stream?frames=1"
    ) as resp:
        assert resp.status_code == 200
        body = "".join([chunk async for chunk in resp.aiter_text()])
    lines = [
        line[len("data: ") :].strip()
        for line in body.splitlines()
        if line.startswith("data: ")
    ]

    assert lines[-1] == "[DONE]"
    payload = json.loads(lines[0])
    assert payload["counts"]["approval"] == 2
    assert {item["id"] for item in payload["items"]} >= {
        f"approval:{seeded['agent_run'].id}",
        f"review:{seeded['review_job'].id}",
    }
