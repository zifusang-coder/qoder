import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.llm.gateway import LLMGateway
from app.models.profile import Profile, ProfileMemory
from app.models.project import Content, ContentVersion, Project
from app.services.analytics import latest_snapshots
from app.services.profiles import MAX_MEMORIES, build_profile_block
from app.skills.executor import SkillExecutor
from app.skills.registry import SkillRegistry

MAX_EXCERPT = 500
MAX_MEMORY_CONTENT = 200
MAX_MEMORY_KIND = 32
MAX_MEMORIES_PER_RUN = 3
DEFAULT_MEMORY_WEIGHT = 0.5
METRIC_FIELDS = ("views", "likes", "comments", "shares", "saves", "followers_delta")


def _normalize_content(text: str) -> str:
    return " ".join(text.split())


def _valid_weight(value: object) -> float | None:
    """只接受有限数值权重，布尔与字符串一律视为非法。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        weight = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(weight):
        return None
    return min(1.0, max(0.0, weight))


def _memory_candidates(raw: object) -> list[tuple[str, str, float]]:
    """校验 LLM 返回的记忆候选，非法项直接丢弃。"""
    if not isinstance(raw, list):
        return []
    candidates: list[tuple[str, str, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        raw_content = item.get("content")
        if raw_content is None:
            continue
        content = str(raw_content).strip()
        if not content:
            continue
        content = content[:MAX_MEMORY_CONTENT]

        raw_kind = item.get("kind")
        kind = str(raw_kind).strip() if raw_kind is not None else ""
        if not 1 <= len(kind) <= MAX_MEMORY_KIND:
            kind = "insight"

        if "weight" in item:
            weight = _valid_weight(item["weight"])
            if weight is None:
                continue
        else:
            weight = DEFAULT_MEMORY_WEIGHT
        candidates.append((kind, content, weight))
    return candidates


async def generate_postmortem(
    *,
    session: AsyncSession,
    registry: SkillRegistry,
    gateway: LLMGateway,
    tenant_id: str,
    content_id: uuid.UUID,
    skill_id: str = "postmortem",
    created_by: uuid.UUID | None = None,
) -> dict:
    content = (
        await session.execute(
            select(Content).where(
                Content.id == content_id,
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)

    project = (
        await session.execute(
            select(Project).where(
                Project.id == content.project_id,
                Project.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if project is None:
        raise AppError("not_found", "项目不存在", 404)

    snapshot = (
        await latest_snapshots(session, tenant_id, [content.id])
    ).get(content.id)
    if snapshot is None:
        raise AppError("no_metrics", "请先导入该内容的发布数据", 409)

    version = (
        await session.execute(
            select(ContentVersion).where(
                ContentVersion.tenant_id == tenant_id,
                ContentVersion.content_id == content.id,
                ContentVersion.version == content.current_version,
            )
        )
    ).scalar_one_or_none()
    body = version.body if version is not None else ""

    profile: Profile | None = None
    profile_block = ""
    existing_contents: set[str] = set()
    if project.profile_id:
        profile = (
            await session.execute(
                select(Profile).where(
                    Profile.id == project.profile_id,
                    Profile.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if profile is not None:
            memories = (
                await session.execute(
                    select(ProfileMemory)
                    .where(
                        ProfileMemory.tenant_id == tenant_id,
                        ProfileMemory.profile_id == profile.id,
                    )
                    .order_by(
                        ProfileMemory.weight.desc(),
                        ProfileMemory.created_at.desc(),
                        ProfileMemory.id,
                    )
                    .limit(MAX_MEMORIES)
                )
            ).scalars().all()
            profile_block = build_profile_block(profile, list(memories))
            existing_rows = (
                await session.execute(
                    select(ProfileMemory.content).where(
                        ProfileMemory.tenant_id == tenant_id,
                        ProfileMemory.profile_id == profile.id,
                    )
                )
            ).scalars().all()
            existing_contents = {
                _normalize_content(item) for item in existing_rows
            }

    # 执行器内部会 commit，提前固化后续需要的值，避免依赖实例过期行为
    content_title = content.title
    platform = content.platform
    profile_id = profile.id if profile is not None else None
    inputs = {
        "title": content_title,
        "platform": platform,
        "metrics": {
            field: getattr(snapshot, field) for field in METRIC_FIELDS
        },
        "content_excerpt": body[:MAX_EXCERPT],
        "publish_time": snapshot.captured_at.isoformat(),
    }

    executor = SkillExecutor(
        registry=registry,
        gateway=gateway,
        session=session,
        tenant_id=tenant_id,
        profile_block=profile_block,
        created_by=created_by,
    )
    result = await executor.run(skill_id, inputs)

    report = result.data
    if not isinstance(report, dict):
        raise AppError("skill_output_invalid", "复盘结果格式异常", 502)

    # 执行器提交后重新取回内容，确保 meta 合并基于最新行且不触发异步懒加载
    content = (
        await session.execute(
            select(Content).where(
                Content.id == content_id,
                Content.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)

    memory_ids: list[str] = []
    if profile_id is not None:
        seen = set(existing_contents)
        for kind, memory_content, weight in _memory_candidates(
            report.get("memories")
        ):
            key = _normalize_content(memory_content)
            if key in seen:
                continue
            seen.add(key)
            memory = ProfileMemory(
                tenant_id=tenant_id,
                profile_id=profile_id,
                kind=kind,
                content=memory_content,
                weight=weight,
                source="postmortem",
                tags=[],
            )
            session.add(memory)
            await session.flush()
            memory_ids.append(str(memory.id))
            if len(memory_ids) >= MAX_MEMORIES_PER_RUN:
                break

    meta = dict(content.meta) if isinstance(content.meta, dict) else {}
    meta["postmortem"] = report
    meta["postmortem_at"] = datetime.now(UTC).isoformat()
    meta["postmortem_run_id"] = result.run_id
    content.meta = meta
    await session.commit()

    return {
        "report": report,
        "memory_ids": memory_ids,
        "run_id": result.run_id,
    }
