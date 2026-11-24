import math
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.profile import Profile, ProfileMemory
from app.models.project import Content, ContentVersion, Project
from app.services.analytics import latest_snapshots
from app.services.embeddings import embed_memories
from app.services.profiles import profile_block_from_profile
from app.services.skill_settings import require_enabled
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


def _memory_key(text: str) -> str:
    """比对人选与已有记忆时使用同一顺序：先去首尾空白、再截断、最后压缩空白。"""
    return _normalize_content(text.strip()[:MAX_MEMORY_CONTENT])


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
        if not isinstance(raw_content, str):
            continue
        content = raw_content.strip()
        if not content:
            continue
        content = content[:MAX_MEMORY_CONTENT]

        raw_kind = item.get("kind")
        kind = raw_kind.strip() if isinstance(raw_kind, str) else ""
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
    gateway: LLMGateway | None = None,
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

    await require_enabled(session, tenant_id, skill_id)
    registry.get(skill_id)

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
            profile_block = await profile_block_from_profile(
                session,
                profile,
                tenant_id,
                query=f"{content.title}\n{body[:MAX_EXCERPT]}",
            )

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

    # 延迟到 404/409 校验之后再解析网关，避免缺少 LLM 配置掩盖真实错误
    if gateway is None:
        gateway = await load_gateway_default(session, tenant_id)

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

    # 执行器提交后锁定并刷新内容行，确保 meta 合并基于最新数据（避免并发写覆盖）
    content = (
        await session.execute(
            select(Content)
            .where(
                Content.id == content_id,
                Content.tenant_id == tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if content is None:
        raise AppError("not_found", "内容不存在", 404)

    memory_ids: list[str] = []
    new_memories: list[ProfileMemory] = []
    if profile_id is not None:
        # 锁住画像行并在锁内重新读取，避免并发复盘写入重复记忆
        locked = (
            await session.execute(
                select(Profile.id)
                .where(Profile.id == profile_id, Profile.tenant_id == tenant_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        # 画像可能在 LLM 调用期间被删除，此时跳过记忆回写但仍保存复盘结果
        if locked is not None:
            existing_rows = (
                await session.execute(
                    select(ProfileMemory.content).where(
                        ProfileMemory.tenant_id == tenant_id,
                        ProfileMemory.profile_id == profile_id,
                    )
                )
            ).scalars().all()
            seen = {_memory_key(item) for item in existing_rows}
            for kind, memory_content, weight in _memory_candidates(
                report.get("memories")
            ):
                key = _memory_key(memory_content)
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
                new_memories.append(memory)
                memory_ids.append(str(memory.id))
                if len(memory_ids) >= MAX_MEMORIES_PER_RUN:
                    break

    meta = dict(content.meta) if isinstance(content.meta, dict) else {}
    meta["postmortem"] = report
    meta["postmortem_at"] = datetime.now(UTC).isoformat()
    meta["postmortem_run_id"] = result.run_id
    content.meta = meta
    await session.commit()

    # 先释放画像/内容行锁，再调用嵌入服务；失败时记忆仍保留，可后续回填
    if await embed_memories(session, tenant_id, new_memories):
        await session.commit()

    return {
        "report": report,
        "memory_ids": memory_ids,
        "run_id": result.run_id,
    }
