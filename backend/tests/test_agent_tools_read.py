import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.contracts import ToolContext
from app.agent.registry import ToolRegistry
from app.agent.tools import (
    analytics,
    calendar,
    contents,
    media,
    platform,
    profiles,
    projects,
    publish,
)
from app.config import get_settings
from app.errors import AppError
from app.models.analytics import AnalyticsSnapshot
from app.models.media import MediaJob
from app.models.profile import Profile, ProfileMemory
from app.models.project import Content, ContentVersion, Project
from app.models.publish import PublishJob
from app.skills.registry import SkillRegistry
from app.storage import get_storage

TENANT_A = "aaaaaaaa-1111-1111-1111-111111111111"
TENANT_B = "bbbbbbbb-2222-2222-2222-222222222222"


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    for module in (
        profiles,
        projects,
        contents,
        calendar,
        analytics,
        platform,
        media,
        publish,
    ):
        registry.register_all(module.SPECS)
    return registry


async def _ctx(db_session: AsyncSession, tenant_id: str = TENANT_A) -> ToolContext:
    return ToolContext(
        session=db_session,
        tenant_id=tenant_id,
        user_id=uuid.uuid4(),
        run_id="run-1",
        step_index=1,
        gateway=None,  # read tools never touch the gateway
        storage=get_storage(),
        registry=SkillRegistry(get_settings().skills_dir),
        queue=None,
        settings=get_settings(),
    )


async def _seed(db_session: AsyncSession, tenant_id: str) -> dict:
    profile = Profile(tenant_id=tenant_id, name="主号", positioning="职场")
    db_session.add(profile)
    await db_session.flush()
    db_session.add(
        ProfileMemory(
            tenant_id=tenant_id,
            profile_id=profile.id,
            content="读者偏好短句",
            weight=2.0,
        )
    )
    project = Project(
        tenant_id=tenant_id, title="选题项目", topic="效率", profile_id=profile.id
    )
    db_session.add(project)
    await db_session.flush()
    content = Content(
        tenant_id=tenant_id,
        project_id=project.id,
        platform="wechat",
        kind="article",
        title="文章标题",
        current_version=1,
    )
    db_session.add(content)
    await db_session.flush()
    version = ContentVersion(
        tenant_id=tenant_id,
        content_id=content.id,
        version=1,
        title="文章标题",
        body="正文内容",
    )
    db_session.add(version)
    db_session.add(
        AnalyticsSnapshot(
            tenant_id=tenant_id,
            content_id=content.id,
            platform="wechat",
            captured_at=date.today(),
            views=1000,
            likes=50,
            comments=10,
            shares=5,
            saves=20,
        )
    )
    job = PublishJob(
        tenant_id=tenant_id,
        content_id=content.id,
        content_version=1,
        platform="wechat",
        status="draft",
        idempotency_key=f"key-{tenant_id}",
        scheduled_at=datetime.now(UTC) + timedelta(days=1),
    )
    db_session.add(job)
    await db_session.flush()
    media_job = MediaJob(
        tenant_id=tenant_id,
        project_id=project.id,
        skill_id="short-video",
        status="queued",
    )
    db_session.add(media_job)
    await db_session.commit()
    return {
        "profile": profile,
        "project": project,
        "content": content,
        "job": job,
        "media_job": media_job,
    }


async def _call(registry: ToolRegistry, db_session, tenant_id, name, args):
    spec = registry.get(name)
    assert spec is not None
    return await spec.handler(await _ctx(db_session, tenant_id), args)


async def test_profile_list_and_get_are_tenant_scoped(db_session):
    await _seed(db_session, TENANT_A)
    await _seed(db_session, TENANT_B)
    registry = _registry()

    listed = await _call(registry, db_session, TENANT_A, "profile.list", {})
    assert len(listed.data["items"]) == 1
    assert listed.data["items"][0]["name"] == "主号"

    own = await _call(
        registry,
        db_session,
        TENANT_A,
        "profile.get",
        {"profile_id": listed.data["items"][0]["id"]},
    )
    assert own.data["profile"]["positioning"] == "职场"
    assert own.data["memories"][0]["content"] == "读者偏好短句"


async def test_cross_tenant_get_returns_not_found(db_session):
    seeded = await _seed(db_session, TENANT_B)
    registry = _registry()
    with pytest.raises(AppError) as exc:
        await _call(
            registry,
            db_session,
            TENANT_A,
            "project.get",
            {"project_id": str(seeded["project"].id)},
        )
    assert exc.value.code == "not_found"


@pytest.mark.parametrize(
    ("tool", "argument", "seed_key"),
    [
        ("profile.get", "profile_id", "profile"),
        ("content.get", "content_id", "content"),
        ("publish.job.get", "job_id", "job"),
        ("media.job.get", "job_id", "media_job"),
    ],
)
async def test_cross_tenant_id_reads_are_not_found(
    db_session, tool, argument, seed_key
):
    seeded = await _seed(db_session, TENANT_B)
    registry = _registry()
    with pytest.raises(AppError) as exc:
        await _call(
            registry,
            db_session,
            TENANT_A,
            tool,
            {argument: str(seeded[seed_key].id)},
        )
    assert exc.value.code == "not_found"
    assert exc.value.status == 404


@pytest.mark.parametrize(
    ("tool", "argument"),
    [
        ("profile.get", "profile_id"),
        ("project.get", "project_id"),
        ("content.get", "content_id"),
        ("publish.job.get", "job_id"),
        ("media.job.get", "job_id"),
    ],
)
async def test_missing_and_malformed_ids_raise_not_found(db_session, tool, argument):
    registry = _registry()
    for value in (str(uuid.uuid4()), "not-a-uuid"):
        with pytest.raises(AppError) as exc:
            await _call(registry, db_session, TENANT_A, tool, {argument: value})
        assert exc.value.code == "not_found"


async def test_lists_hide_other_tenants(db_session):
    await _seed(db_session, TENANT_B)
    registry = _registry()
    for tool in (
        "profile.list",
        "project.list",
        "content.list",
        "publish.job.list",
    ):
        result = await _call(registry, db_session, TENANT_A, tool, {})
        assert result.data["items"] == [], tool
    calendar_items = await _call(
        registry,
        db_session,
        TENANT_A,
        "calendar.list",
        {
            "date_from": date.today().isoformat(),
            "date_to": (date.today() + timedelta(days=2)).isoformat(),
        },
    )
    assert calendar_items.data["items"] == []
    summary = await _call(
        registry, db_session, TENANT_A, "analytics.summary", {"days": 30}
    )
    assert summary.data["totals"]["views"] == 0
    assert summary.data["items"] == []


async def test_project_and_content_reads(db_session):
    seeded = await _seed(db_session, TENANT_A)
    registry = _registry()
    project = await _call(
        registry,
        db_session,
        TENANT_A,
        "project.get",
        {"project_id": str(seeded["project"].id)},
    )
    assert project.data["project"]["title"] == "选题项目"
    assert project.data["contents"][0]["current_version"] == 1

    content = await _call(
        registry,
        db_session,
        TENANT_A,
        "content.get",
        {"content_id": str(seeded["content"].id)},
    )
    assert content.data["version"]["body"] == "正文内容"
    assert content.data["content"]["platform"] == "wechat"

    listed = await _call(
        registry,
        db_session,
        TENANT_A,
        "content.list",
        {"project_id": str(seeded["project"].id)},
    )
    assert len(listed.data["items"]) == 1


async def test_calendar_lists_jobs_in_range(db_session):
    seeded = await _seed(db_session, TENANT_A)
    registry = _registry()
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=2)).isoformat()
    result = await _call(
        registry,
        db_session,
        TENANT_A,
        "calendar.list",
        {"date_from": today, "date_to": tomorrow},
    )
    assert len(result.data["items"]) == 1
    assert result.data["items"][0]["job_id"] == str(seeded["job"].id)


async def test_analytics_summary_aggregates(db_session):
    await _seed(db_session, TENANT_A)
    registry = _registry()
    result = await _call(
        registry, db_session, TENANT_A, "analytics.summary", {"days": 30}
    )
    assert result.data["totals"]["views"] == 1000
    assert result.data["totals"]["likes"] == 50
    assert result.data["items"][0]["title"] == "文章标题"


async def test_platform_capabilities_exposes_matrix(db_session):
    registry = _registry()
    result = await _call(
        registry, db_session, TENANT_A, "platform.capabilities", {}
    )
    by_key = {item["platform"]: item for item in result.data["items"]}
    assert by_key["wechat"]["capabilities"]["article"] == "auto_publish"
    assert by_key["xiaohongshu"]["capabilities"]["note"] == "manual_export"


async def test_publish_and_media_job_reads(db_session):
    seeded = await _seed(db_session, TENANT_A)
    registry = _registry()
    jobs = await _call(registry, db_session, TENANT_A, "publish.job.list", {})
    assert jobs.data["items"][0]["id"] == str(seeded["job"].id)
    job = await _call(
        registry,
        db_session,
        TENANT_A,
        "publish.job.get",
        {"job_id": str(seeded["job"].id)},
    )
    assert job.data["job"]["status"] == "draft"
    media_job = await _call(
        registry,
        db_session,
        TENANT_A,
        "media.job.get",
        {"job_id": str(seeded["media_job"].id)},
    )
    assert media_job.data["job"]["skill_id"] == "short-video"
