import json
import logging
import uuid
from collections.abc import AsyncIterator

import jsonschema
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.profile import Profile, ProfileMemory
from app.models.skill import SkillSetting
from app.models.user import User
from app.services.profiles import build_profile_block
from app.services.skill_settings import enabled_map
from app.services.skill_settings import require_enabled as _require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import LoadedSkill, ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


async def get_llm_gateway(session: AsyncSession, user: User) -> LLMGateway:
    return await load_gateway_default(session, user.tenant_id)


class SkillOut(BaseModel):
    id: str
    name: str
    version: str
    layer: str
    description: str
    llm_tier: str
    output_format: str
    enabled: bool


class SkillList(BaseModel):
    items: list[SkillOut]


class ToggleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class RunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inputs: dict = Field(default_factory=dict)
    profile_id: uuid.UUID | None = None


class RunOut(BaseModel):
    run_id: str
    skill_id: str
    text: str
    data: dict | None
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int


def _skill_out(skill: LoadedSkill, enabled: bool) -> SkillOut:
    return SkillOut(
        id=skill.manifest.id,
        name=skill.manifest.name,
        version=skill.manifest.version,
        layer=skill.manifest.layer,
        description=skill.manifest.description,
        llm_tier=skill.manifest.llm_tier,
        output_format=skill.manifest.output_format,
        enabled=enabled,
    )


def _registry_skill(registry: SkillRegistry, skill_id: str) -> LoadedSkill:
    try:
        return registry.get(skill_id)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc


async def _profile_block(
    session: AsyncSession, profile_id: uuid.UUID | None, user: User
) -> str:
    if profile_id is None:
        return ""
    profile = (
        await session.execute(
            select(Profile).where(
                Profile.id == profile_id,
                Profile.tenant_id == user.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise AppError("not_found", "账号画像不存在", 404)
    memories = (
        await session.execute(
            select(ProfileMemory).where(
                ProfileMemory.tenant_id == user.tenant_id,
                ProfileMemory.profile_id == profile.id,
            )
        )
    ).scalars().all()
    return build_profile_block(profile, list(memories))


def _validate_inputs(skill: LoadedSkill, inputs: dict) -> None:
    try:
        jsonschema.validate(inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError(
            "skill_inputs_invalid", "技能输入参数校验失败", 422
        ) from exc


@router.get("", response_model=SkillList)
async def list_skills(
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> SkillList:
    enabled = await enabled_map(session, user.tenant_id)
    items = [
        _skill_out(skill, enabled.get(skill.manifest.id, True))
        for skill in sorted(
            registry.skills.values(), key=lambda s: s.manifest.id
        )
    ]
    return SkillList(items=items)


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(
    skill_id: str,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> SkillOut:
    skill = _registry_skill(registry, skill_id)
    enabled = (await enabled_map(session, user.tenant_id)).get(skill_id, True)
    return _skill_out(skill, enabled)


@router.patch("/{skill_id}/toggle", response_model=SkillOut)
async def toggle_skill(
    skill_id: str,
    payload: ToggleIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> SkillOut:
    skill = _registry_skill(registry, skill_id)
    setting = (
        await session.execute(
            select(SkillSetting).where(
                SkillSetting.tenant_id == user.tenant_id,
                SkillSetting.skill_id == skill_id,
            )
        )
    ).scalar_one_or_none()
    if setting is None:
        session.add(
            SkillSetting(
                tenant_id=user.tenant_id,
                skill_id=skill_id,
                enabled=payload.enabled,
            )
        )
    else:
        setting.enabled = payload.enabled
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent first-time toggles can both INSERT; adopt the winner's row.
        logger.info("技能启停并发写入，重试 skill_id=%s", skill_id)
        await session.rollback()
        setting = (
            await session.execute(
                select(SkillSetting).where(
                    SkillSetting.tenant_id == user.tenant_id,
                    SkillSetting.skill_id == skill_id,
                )
            )
        ).scalar_one_or_none()
        if setting is None:
            raise
        setting.enabled = payload.enabled
        await session.commit()
    return _skill_out(skill, payload.enabled)


@router.post("/{skill_id}/run", response_model=RunOut)
async def run_skill(
    skill_id: str,
    payload: RunIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> RunOut:
    skill = _registry_skill(registry, skill_id)
    await _require_enabled(session, user.tenant_id, skill_id)
    _validate_inputs(skill, payload.inputs)
    profile_block = await _profile_block(session, payload.profile_id, user)
    gateway = await get_llm_gateway(session, user)
    executor = SkillExecutor(
        registry=registry,
        gateway=gateway,
        session=session,
        tenant_id=user.tenant_id,
        profile_block=profile_block,
        created_by=user.id,
    )
    try:
        result = await executor.run(skill_id, payload.inputs)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except jsonschema.ValidationError as exc:
        raise AppError(
            "skill_inputs_invalid", "技能输入参数校验失败", 422
        ) from exc
    except ScriptError as exc:
        raise AppError(
            "skill_script_failed", "后处理脚本执行失败", 500
        ) from exc
    except ValueError as exc:
        raise AppError(
            "skill_output_invalid", "模型输出格式异常", 502
        ) from exc
    return RunOut(
        run_id=result.run_id,
        skill_id=result.skill_id,
        text=result.text,
        data=result.data,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        duration_ms=result.duration_ms,
    )


@router.post("/{skill_id}/stream")
async def stream_skill(
    skill_id: str,
    payload: RunIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> EventSourceResponse:
    skill = _registry_skill(registry, skill_id)
    await _require_enabled(session, user.tenant_id, skill_id)
    _validate_inputs(skill, payload.inputs)
    profile_block = await _profile_block(session, payload.profile_id, user)
    gateway = await get_llm_gateway(session, user)
    executor = SkillExecutor(
        registry=registry,
        gateway=gateway,
        session=session,
        tenant_id=user.tenant_id,
        profile_block=profile_block,
        created_by=user.id,
    )
    messages = executor.build_messages(skill, payload.inputs)

    async def event_gen() -> AsyncIterator[dict[str, str]]:
        try:
            async for chunk in gateway.stream(
                messages,
                tier=skill.manifest.llm_tier,
                temperature=skill.manifest.temperature,
            ):
                yield {"data": json.dumps({"delta": chunk}, ensure_ascii=False)}
        except AppError as exc:
            yield {
                "data": json.dumps(
                    {"error": {"code": exc.code, "message": exc.message}},
                    ensure_ascii=False,
                )
            }
        except Exception:
            logger.exception("技能流式执行失败 skill_id=%s", skill_id)
            yield {
                "data": json.dumps(
                    {
                        "error": {
                            "code": "skill_stream_failed",
                            "message": "流式生成失败",
                        }
                    },
                    ensure_ascii=False,
                )
            }
        yield {"data": "[DONE]"}

    return EventSourceResponse(event_gen())
