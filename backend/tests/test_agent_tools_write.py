import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contracts import ToolContext
from app.agent.registry import build_registry, get_tool_registry
from app.config import get_settings
from app.errors import AppError
from app.llm.types import Usage
from app.models.media import MediaJob
from app.models.profile import Profile
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishEvent, PublishJob
from app.models.skill import SkillRun
from app.skills.registry import get_registry
from app.storage import get_storage

TENANT = "aaaaaaaa-1111-1111-1111-111111111111"
OTHER_TENANT = "bbbbbbbb-2222-2222-2222-222222222222"


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue_job(self, *args):
        self.calls.append(args)


class FakeGateway:
    def __init__(self, text: str = "生成的文案内容") -> None:
        self.text = text
        self.last_usage = Usage(prompt_tokens=10, completion_tokens=5)

    async def complete(self, messages, **kwargs) -> str:
        return self.text


async def _ctx(
    db_session: AsyncSession,
    *,
    step_index: int = 1,
    queue=None,
    tenant_id: str = TENANT,
    gateway=None,
) -> ToolContext:
    return ToolContext(
        session=db_session,
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        run_id="run-7",
        step_index=step_index,
        gateway=gateway,
        storage=get_storage(),
        registry=get_registry(),
        queue=queue,
        settings=get_settings(),
    )


async def _seed(db_session: AsyncSession, tenant_id: str = TENANT) -> dict:
    profile = Profile(tenant_id=tenant_id, name="主号", taboos=["最"])
    db_session.add(profile)
    await db_session.flush()
    project = Project(
        tenant_id=tenant_id, title="项目", topic="效率", profile_id=profile.id
    )
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=tenant_id,
        project_id=project.id,
        platform="wechat",
        kind="article",
        title="标题",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    db_session.add(
        ContentVersion(
            tenant_id=tenant_id,
            content_id=content.id,
            version=1,
            title="标题",
            body="正文内容足够长，可以做发布前检查。",
        )
    )
    await db_session.commit()
    return {"profile": profile, "project": project, "content": content}


async def _call(name: str, ctx: ToolContext, args: dict):
    spec = build_registry().get(name)
    assert spec is not None
    return await spec.handler(ctx, args)


async def test_build_registry_has_all_tools():
    names = set(build_registry().names())
    assert {
        "profile.list",
        "profile.get",
        "project.list",
        "project.get",
        "project.create",
        "content.list",
        "content.get",
        "content.create",
        "content.create_version",
        "calendar.list",
        "analytics.summary",
        "platform.capabilities",
        "publish.job.list",
        "publish.job.get",
        "publish.create_draft_job",
        "publish.precheck",
        "media.generate",
        "media.job.get",
        "skill.run",
    } <= names


async def test_project_create_is_idempotent_per_step(db_session):
    ctx = await _ctx(db_session)
    first = await _call("project.create", ctx, {"title": "新选题", "topic": "效率"})
    second = await _call("project.create", ctx, {"title": "新选题", "topic": "效率"})
    assert first.data["project"]["id"] == second.data["project"]["id"]
    rows = (await db_session.execute(select(Project))).scalars().all()
    assert len(rows) == 1
    # 重放命中的是同一条持久化行，不是新建的第二行
    assert first.data["project"]["id"] == str(rows[0].id)
    assert second.artifacts == first.artifacts

    other_step = await _call(
        "project.create", await _ctx(db_session, step_index=2), {"title": "新选题"}
    )
    assert other_step.data["project"]["id"] != first.data["project"]["id"]


async def test_content_create_version_advances_current_version(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session)
    args = {"content_id": str(seeded["content"].id), "title": "新标题", "body": "新正文"}
    first = await _call("content.create_version", ctx, args)
    second = await _call("content.create_version", ctx, args)
    assert first.data["version"]["id"] == second.data["version"]["id"]
    assert first.artifacts == [
        {
            "type": "content",
            "id": str(seeded["content"].id),
            "title": "新标题",
            "version_id": first.data["version"]["id"],
            "version": 2,
        }
    ]
    assert second.artifacts == first.artifacts
    versions = (await db_session.execute(select(ContentVersion))).scalars().all()
    assert len(versions) == 2
    new_versions = [row for row in versions if row.version == 2]
    assert len(new_versions) == 1
    # 串行重放命中同一条已持久化的版本行
    assert first.data["version"]["id"] == str(new_versions[0].id)
    assert second.data["version"]["id"] == str(new_versions[0].id)

    content = await db_session.get(Content, seeded["content"].id)
    await db_session.refresh(content)
    assert content.current_version == 2
    assert content.title == "新标题"


async def test_content_create_version_artifact_points_at_content(db_session):
    seeded = await _seed(db_session)
    result = await _call(
        "content.create_version",
        await _ctx(db_session),
        {"content_id": str(seeded["content"].id), "title": "新标题", "body": "新正文"},
    )
    assert len(result.artifacts) == 1
    artifact = result.artifacts[0]
    assert isinstance(artifact, dict)
    assert artifact["type"] == "content"
    assert artifact["id"] == str(seeded["content"].id)
    assert artifact["version"] == 2
    version_id = result.data["version"]["id"]
    assert artifact["version_id"] == version_id
    version = await db_session.get(ContentVersion, uuid.UUID(version_id))
    assert version is not None
    assert version.content_id == seeded["content"].id


async def test_content_create_version_conflict_is_error_clean(db_session, monkeypatch):
    seeded = await _seed(db_session)
    content_id = seeded["content"].id
    db_session.add(
        ContentVersion(
            tenant_id=TENANT,
            content_id=content_id,
            version=2,
            title="并发版本",
            body="并发写入的正文",
            source="manual",
        )
    )
    await db_session.commit()

    # 模拟版本号竞态：max 查询读到旧值 1，实际要写入的 v2 已存在
    with monkeypatch.context() as patch:
        patch.setattr(
            "app.agent.tools.contents.func",
            SimpleNamespace(max=lambda _column: 1),
        )
        with pytest.raises(AppError) as exc:
            await _call(
                "content.create_version",
                await _ctx(db_session),
                {
                    "content_id": str(content_id),
                    "title": "新标题",
                    "body": "新正文",
                },
            )
    assert exc.value.code == "conflict"
    assert exc.value.status == 409

    # 冲突回滚后会话仍可用，原 content 未被脏写
    content = await db_session.get(Content, content_id)
    assert content.current_version == 1
    rows = (await db_session.execute(select(ContentVersion))).scalars().all()
    assert len(rows) == 2

    retried = await _call(
        "content.create_version",
        await _ctx(db_session, step_index=2),
        {"content_id": str(content_id), "title": "新标题", "body": "新正文"},
    )
    assert retried.data["version"]["version"] == 3


async def test_content_create_creates_content_with_first_version(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session)
    args = {
        "project_id": str(seeded["project"].id),
        "platform": "xiaohongshu",
        "title": "露营护肤清单",
        "body": "便携修护清单正文",
    }
    first = await _call("content.create", ctx, args)
    second = await _call("content.create", ctx, args)
    assert first.data["content"]["id"] == second.data["content"]["id"]
    contents = (await db_session.execute(select(Content))).scalars().all()
    new_rows = [row for row in contents if row.id != seeded["content"].id]
    assert len(new_rows) == 1
    content = new_rows[0]
    assert str(content.id) == first.data["content"]["id"]
    assert content.project_id == seeded["project"].id
    assert content.platform == "xiaohongshu"
    assert content.kind == "article"
    assert content.title == "露营护肤清单"
    assert content.current_version == 1
    assert content.status == "draft"
    versions = (
        await db_session.execute(
            select(ContentVersion).where(ContentVersion.content_id == content.id)
        )
    ).scalars().all()
    assert len(versions) == 1
    version = versions[0]
    assert version.version == 1
    assert version.title == "露营护肤清单"
    assert version.body == "便携修护清单正文"
    assert version.source == "agent"
    assert first.data["content"]["current_version"] == 1
    assert first.artifacts == [
        {
            "type": "content",
            "id": str(content.id),
            "title": "露营护肤清单",
            "version_id": str(version.id),
            "version": 1,
        }
    ]
    assert second.artifacts == first.artifacts

    other_step = await _call(
        "content.create", await _ctx(db_session, step_index=2), args
    )
    assert other_step.data["content"]["id"] != first.data["content"]["id"]


async def test_content_create_without_body_creates_empty_content(db_session):
    seeded = await _seed(db_session)
    result = await _call(
        "content.create",
        await _ctx(db_session),
        {"project_id": str(seeded["project"].id), "platform": "wechat"},
    )
    content = await db_session.get(Content, uuid.UUID(result.data["content"]["id"]))
    assert content.current_version == 0
    assert content.title == ""
    assert (
        await db_session.execute(
            select(ContentVersion).where(ContentVersion.content_id == content.id)
        )
    ).scalars().all() == []
    assert result.artifacts == [
        {"type": "content", "id": str(content.id), "title": "未命名内容"}
    ]


async def test_content_create_requires_platform_and_owned_project(db_session):
    seeded = await _seed(db_session)
    with pytest.raises(AppError) as missing:
        await _call(
            "content.create",
            await _ctx(db_session),
            {"project_id": str(seeded["project"].id), "body": "正文"},
        )
    assert missing.value.code == "invalid_arguments"
    assert missing.value.status == 422
    with pytest.raises(AppError) as foreign:
        await _call(
            "content.create",
            await _ctx(db_session),
            {"project_id": str(uuid.uuid4()), "platform": "wechat"},
        )
    assert foreign.value.code == "not_found"
    assert foreign.value.status == 404
    contents = (await db_session.execute(select(Content))).scalars().all()
    assert len(contents) == 1


async def test_media_generate_enqueues_and_is_idempotent(db_session):
    seeded = await _seed(db_session)
    queue = FakeQueue()
    ctx = await _ctx(db_session, queue=queue)
    args = {
        "project_id": str(seeded["project"].id),
        "skill_id": "short-video",
        "payload": {"topic": "效率技巧", "platform": "douyin"},
    }
    first = await _call("media.generate", ctx, args)
    second = await _call("media.generate", ctx, args)
    assert first.data["job"]["id"] == second.data["job"]["id"]
    assert len(queue.calls) == 1
    assert queue.calls[0][0] == "process_media_job"
    job = await db_session.get(MediaJob, uuid.UUID(first.data["job"]["id"]))
    assert job.agent_key == "run-7:1"
    assert job.status == "queued"


async def test_media_generate_without_queue_marks_failed(db_session):
    seeded = await _seed(db_session)
    with pytest.raises(AppError) as exc:
        await _call(
            "media.generate",
            await _ctx(db_session, queue=None),
            {
                "project_id": str(seeded["project"].id),
                "skill_id": "short-video",
                "payload": {"topic": "效率技巧", "platform": "douyin"},
            },
        )
    assert exc.value.code == "queue_unavailable"
    job = (await db_session.execute(select(MediaJob))).scalars().one()
    assert job.status == "failed"
    assert job.error_code == "queue_unavailable"


async def test_media_generate_persists_agent_key_on_creation(db_session):
    seeded = await _seed(db_session)
    queue = FakeQueue()
    result = await _call(
        "media.generate",
        await _ctx(db_session, queue=queue),
        {
            "project_id": str(seeded["project"].id),
            "skill_id": "short-video",
            "payload": {"topic": "效率技巧", "platform": "douyin"},
        },
    )
    db_session.expire_all()
    job = (await db_session.execute(select(MediaJob))).scalars().one()
    assert str(job.id) == result.data["job"]["id"]
    # agent_key 必须和任务行在同一次提交里落库，而不是第二次 UPDATE
    assert job.agent_key == "run-7:1"
    assert len(queue.calls) == 1


async def test_media_generate_replays_transient_queue_failure(db_session):
    seeded = await _seed(db_session)
    args = {
        "project_id": str(seeded["project"].id),
        "skill_id": "short-video",
        "payload": {"topic": "效率技巧", "platform": "douyin"},
    }
    with pytest.raises(AppError) as exc:
        await _call("media.generate", await _ctx(db_session, queue=None), args)
    assert exc.value.code == "queue_unavailable"
    failed = (await db_session.execute(select(MediaJob))).scalars().one()
    assert failed.status == "failed"
    assert failed.error_code == "queue_unavailable"

    queue = FakeQueue()
    replayed = await _call(
        "media.generate", await _ctx(db_session, queue=queue), args
    )
    assert replayed.data["job"]["id"] == str(failed.id)
    assert replayed.data["job"]["status"] == "queued"
    assert queue.calls == [("process_media_job", str(failed.id))]
    await db_session.refresh(failed)
    assert failed.status == "queued"
    assert failed.error_code == ""

    # 已重新入队的任务再次重放不会重复入队
    again = await _call(
        "media.generate", await _ctx(db_session, queue=queue), args
    )
    assert again.data["job"]["id"] == str(failed.id)
    assert len(queue.calls) == 1


async def test_publish_create_draft_job_forces_draft_and_is_idempotent(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session)
    args = {"content_id": str(seeded["content"].id), "platform": "wechat"}
    first = await _call("publish.create_draft_job", ctx, args)
    second = await _call("publish.create_draft_job", ctx, args)
    assert first.data["job"]["id"] == second.data["job"]["id"]
    job = await db_session.get(PublishJob, uuid.UUID(first.data["job"]["id"]))
    assert job.status == "draft"
    assert job.scheduled_at is None
    assert job.idempotency_key == "agent:run-7:1"


async def test_publish_precheck_returns_report_without_touching_the_job(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session)
    created = await _call(
        "publish.create_draft_job",
        ctx,
        {"content_id": str(seeded["content"].id), "platform": "wechat"},
    )
    result = await _call(
        "publish.precheck", ctx, {"job_id": created.data["job"]["id"]}
    )
    assert "passed" in result.data["report"]
    # 工具只返回检查结果（spec §6.2），不推进任务状态。
    assert result.data["job"]["status"] == "draft"
    job = await db_session.get(PublishJob, uuid.UUID(created.data["job"]["id"]))
    await db_session.refresh(job)
    assert job.status == "draft"
    assert job.checks == {}


async def test_skill_run_unknown_skill_returns_not_found(db_session):
    with pytest.raises(AppError) as exc:
        await _call("skill.run", await _ctx(db_session), {"skill_id": "missing"})
    assert exc.value.code == "not_found"


async def test_get_tool_registry_lists_every_tool_once():
    registry = get_tool_registry()
    assert registry is get_tool_registry()
    names = registry.names()
    assert names == build_registry().names()
    assert len(names) == len(set(names))
    write_tools = {spec.name for spec in registry.all() if spec.risk != "read"}
    assert write_tools == {
        "project.create",
        "content.create",
        "content.create_version",
        "media.generate",
        "publish.create_draft_job",
        "publish.precheck",
        "skill.run",
    }
    assert all(spec.timeout_seconds > 0 for spec in registry.all())
    assert all(spec.input_schema["additionalProperties"] is False for spec in registry.all())


@pytest.mark.parametrize(
    ("tool", "args", "seed_key"),
    [
        ("content.create_version", "content_id", "content"),
        ("content.create", "project_id", "project"),
        ("publish.create_draft_job", "content_id", "content"),
        ("project.create", "profile_id", "profile"),
    ],
)
async def test_write_tools_cannot_touch_other_tenant_rows(
    db_session, tool, args, seed_key
):
    seeded = await _seed(db_session, OTHER_TENANT)
    payload = {args: str(seeded[seed_key].id)}
    if tool == "content.create_version":
        payload["body"] = "跨租户正文"
    elif tool in {"content.create", "publish.create_draft_job"}:
        payload["platform"] = "wechat"
    else:
        payload["title"] = "跨租户项目"
    with pytest.raises(AppError) as exc:
        await _call(tool, await _ctx(db_session), payload)
    assert exc.value.code == "not_found"
    assert exc.value.status == 404

    assert len((await db_session.execute(select(Project))).scalars().all()) == 1
    assert len((await db_session.execute(select(Content))).scalars().all()) == 1
    assert len((await db_session.execute(select(ContentVersion))).scalars().all()) == 1
    assert (await db_session.execute(select(PublishJob))).scalars().all() == []


async def test_media_and_skill_tools_cannot_touch_other_tenant_project(db_session):
    seeded = await _seed(db_session, OTHER_TENANT)
    queue = FakeQueue()
    ctx = await _ctx(db_session, queue=queue)
    for tool, args in (
        (
            "media.generate",
            {
                "project_id": str(seeded["project"].id),
                "skill_id": "short-video",
                "payload": {"topic": "跨租户", "platform": "douyin"},
            },
        ),
        (
            "skill.run",
            {
                "skill_id": "copywriting",
                "project_id": str(seeded["project"].id),
                "inputs": {"topic": "跨租户", "platform": "douyin"},
            },
        ),
    ):
        with pytest.raises(AppError) as exc:
            await _call(tool, ctx, args)
        assert exc.value.code == "not_found"
        assert exc.value.status == 404
    assert queue.calls == []
    assert (await db_session.execute(select(MediaJob))).scalars().all() == []
    assert (await db_session.execute(select(SkillRun))).scalars().all() == []


async def test_write_tools_stamp_tenant_on_created_rows(db_session):
    seeded = await _seed(db_session)
    queue = FakeQueue()
    ctx = await _ctx(db_session, queue=queue)
    await _call("project.create", ctx, {"title": "本租户项目"})
    await _call(
        "content.create",
        await _ctx(db_session, step_index=6, queue=queue),
        {
            "project_id": str(seeded["project"].id),
            "platform": "wechat",
            "title": "本租户内容",
            "body": "新内容正文",
        },
    )
    await _call(
        "content.create_version",
        ctx,
        {"content_id": str(seeded["content"].id), "body": "新正文"},
    )
    await _call(
        "media.generate",
        ctx,
        {
            "project_id": str(seeded["project"].id),
            "skill_id": "short-video",
            "payload": {"topic": "效率", "platform": "douyin"},
        },
    )
    await _call(
        "publish.create_draft_job",
        ctx,
        {"content_id": str(seeded["content"].id), "platform": "wechat"},
    )
    for model in (Project, Content, ContentVersion, MediaJob, PublishJob):
        rows = (await db_session.execute(select(model))).scalars().all()
        assert rows, model.__name__
        assert {row.tenant_id for row in rows} == {TENANT}


async def test_write_artifact_shape_carries_title(db_session):
    seeded = await _seed(db_session)
    queue = FakeQueue()
    ctx = await _ctx(db_session, queue=queue, gateway=FakeGateway("生成结果"))

    results = [
        (
            await _call("project.create", ctx, {"title": "选题"}),
            "选题",
        ),
        (
            await _call(
                "content.create",
                await _ctx(
                    db_session, step_index=6, queue=queue, gateway=FakeGateway("生成结果")
                ),
                {
                    "project_id": str(seeded["project"].id),
                    "platform": "wechat",
                    "title": "新建内容",
                    "body": "内容正文",
                },
            ),
            "新建内容",
        ),
        (
            await _call(
                "content.create_version",
                ctx,
                {
                    "content_id": str(seeded["content"].id),
                    "title": "新标题",
                    "body": "新正文",
                },
            ),
            "新标题",
        ),
        (
            await _call(
                "media.generate",
                ctx,
                {
                    "project_id": str(seeded["project"].id),
                    "skill_id": "short-video",
                    "payload": {"topic": "效率", "platform": "douyin"},
                },
            ),
            "竖版短视频",
        ),
        (
            await _call(
                "publish.create_draft_job",
                ctx,
                {"content_id": str(seeded["content"].id), "platform": "wechat"},
            ),
            "新标题",
        ),
        (
            await _call(
                "skill.run",
                ctx,
                {
                    "skill_id": "copywriting",
                    "inputs": {"topic": "效率", "platform": "douyin"},
                },
            ),
            "平台短文案",
        ),
    ]
    for result, expected_title in results:
        assert len(result.artifacts) == 1
        artifact = result.artifacts[0]
        assert {"type", "id", "title"} <= set(artifact)
        assert all(isinstance(artifact[key], str) and artifact[key] for key in ("type", "id", "title"))
        # title 必须是人类可读名称，不能把技能/工具 id 当标题
        assert artifact["title"] == expected_title
        assert artifact["title"] not in {"short-video", "tts-voiceover", "copywriting"}


async def test_publish_create_draft_job_rejects_bad_platforms(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session)
    with pytest.raises(AppError) as unknown:
        await _call(
            "publish.create_draft_job",
            ctx,
            {"content_id": str(seeded["content"].id), "platform": "weibo"},
        )
    assert unknown.value.code == "invalid_platform"
    assert unknown.value.status == 422
    with pytest.raises(AppError) as mismatch:
        await _call(
            "publish.create_draft_job",
            ctx,
            {"content_id": str(seeded["content"].id), "platform": "douyin"},
        )
    assert mismatch.value.code == "platform_mismatch"
    assert (await db_session.execute(select(PublishJob))).scalars().all() == []


async def test_publish_create_draft_job_never_enqueues_or_schedules(db_session):
    seeded = await _seed(db_session)
    queue = FakeQueue()
    result = await _call(
        "publish.create_draft_job",
        await _ctx(db_session, queue=queue),
        {"content_id": str(seeded["content"].id), "platform": "wechat"},
    )
    assert queue.calls == []
    job = await db_session.get(PublishJob, uuid.UUID(result.data["job"]["id"]))
    assert job.status == "draft"
    assert job.scheduled_at is None
    assert job.published_at is None
    assert job.checks == {}


async def test_publish_create_draft_job_writes_one_create_event(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session)
    args = {"content_id": str(seeded["content"].id), "platform": "wechat"}
    await _call("publish.create_draft_job", ctx, args)
    await _call("publish.create_draft_job", ctx, args)
    events = (await db_session.execute(select(PublishEvent))).scalars().all()
    assert [
        (event.tenant_id, event.from_status, event.to_status, event.action)
        for event in events
    ] == [(TENANT, "created", "draft", "create")]
    assert events[0].actor == "agent:run-7"


async def test_publish_create_draft_job_requires_non_empty_current_version(db_session):
    seeded = await _seed(db_session)
    args = {"content_id": str(seeded["content"].id), "platform": "wechat"}

    version = (await db_session.execute(select(ContentVersion))).scalars().one()
    version.body = "  \n "
    await db_session.commit()
    with pytest.raises(AppError) as blank:
        await _call("publish.create_draft_job", await _ctx(db_session), args)
    assert blank.value.code == "empty_content"
    assert blank.value.status == 409

    content = await db_session.get(Content, seeded["content"].id)
    content.current_version = 2
    await db_session.commit()
    with pytest.raises(AppError) as missing:
        await _call("publish.create_draft_job", await _ctx(db_session), args)
    assert missing.value.code == "empty_content"
    assert missing.value.status == 409
    assert (await db_session.execute(select(PublishJob))).scalars().all() == []


async def test_publish_create_draft_job_conflict_without_replay_row(db_session, monkeypatch):
    seeded = await _seed(db_session)

    async def _fail_flush(_session):
        raise IntegrityError("INSERT INTO publish_jobs", {}, Exception("duplicate"))

    monkeypatch.setattr(AsyncSession, "flush", _fail_flush)
    with pytest.raises(AppError) as exc:
        await _call(
            "publish.create_draft_job",
            await _ctx(db_session),
            {"content_id": str(seeded["content"].id), "platform": "wechat"},
        )
    assert exc.value.code == "conflict"
    assert exc.value.status == 409
    # 回滚后会话仍可用，且没有留下半成品任务
    assert (await db_session.execute(select(PublishJob))).scalars().all() == []


async def test_skill_run_records_run_and_returns_its_id(db_session):
    seeded = await _seed(db_session)
    ctx = await _ctx(db_session, gateway=FakeGateway("生成结果"))
    result = await _call(
        "skill.run",
        ctx,
        {
            "skill_id": "copywriting",
            "inputs": {"topic": "效率", "platform": "douyin"},
            "project_id": str(seeded["project"].id),
        },
    )
    run_id = result.data["run_id"]
    assert result.data["text"] == "生成结果"
    assert result.artifacts == [
        {"type": "skill_run", "id": run_id, "title": "平台短文案"}
    ]
    run = await db_session.get(SkillRun, uuid.UUID(run_id))
    assert run is not None
    assert run.tenant_id == TENANT
    assert run.status == "succeeded"
    assert run.skill_id == "copywriting"
    assert run.created_by == ctx.user_id
    assert run.outputs["text"] == "生成结果"


async def test_skill_run_requires_gateway_and_valid_inputs(db_session):
    with pytest.raises(AppError) as missing_gateway:
        await _call(
            "skill.run",
            await _ctx(db_session),
            {"skill_id": "copywriting", "inputs": {"topic": "效率", "platform": "douyin"}},
        )
    assert missing_gateway.value.code == "llm_not_configured"
    with pytest.raises(AppError) as invalid:
        await _call(
            "skill.run",
            await _ctx(db_session, gateway=FakeGateway()),
            {"skill_id": "copywriting", "inputs": {"topic": "效率"}},
        )
    assert invalid.value.code == "invalid_arguments"
    assert invalid.value.status == 422
    assert (await db_session.execute(select(SkillRun))).scalars().all() == []


async def test_skill_run_rejects_media_skills(db_session):
    with pytest.raises(AppError) as exc:
        await _call(
            "skill.run",
            await _ctx(db_session, gateway=FakeGateway()),
            {"skill_id": "short-video", "inputs": {"topic": "效率"}},
        )
    assert exc.value.code == "invalid_arguments"
    assert exc.value.status == 422
    assert (await db_session.execute(select(SkillRun))).scalars().all() == []


async def test_skill_run_sanitizes_nested_output_paths(db_session, tmp_path):
    outputs_root = tmp_path / "outputs"
    run_dir = outputs_root / "adhoc" / TENANT / "run-1"
    payload = {
        "titles": [
            {
                "text": "标题",
                "style": "信息型",
                "reason": f"封面见 {run_dir / 'cover.png'}",
            }
        ],
        "outputs_dir": str(run_dir),
        "files": [{"path": str(run_dir / "cover.png")}],
        "nested": {"deep": [str(run_dir / "video.mp4"), 3]},
        "outside": "/opt/secret/config.yaml",
    }
    ctx = await _ctx(
        db_session, gateway=FakeGateway(json.dumps(payload, ensure_ascii=False))
    )
    ctx.settings = SimpleNamespace(outputs_dir=outputs_root)
    result = await _call(
        "skill.run",
        ctx,
        {"skill_id": "title-hook", "inputs": {"topic": "效率"}},
    )
    data = result.data["data"]
    assert data["outputs_dir"] == f"adhoc/{TENANT}/run-1"
    assert data["files"] == [{"path": f"adhoc/{TENANT}/run-1/cover.png"}]
    assert data["titles"][0]["reason"] == f"封面见 adhoc/{TENANT}/run-1/cover.png"
    assert data["nested"]["deep"] == [f"adhoc/{TENANT}/run-1/video.mp4", 3]
    # outputs 目录之外的绝对路径无法相对化，保持原样
    assert data["outside"] == "/opt/secret/config.yaml"
    assert str(outputs_root) not in json.dumps(data, ensure_ascii=False)
