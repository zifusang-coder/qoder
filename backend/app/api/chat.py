import uuid
from typing import Annotated

import jsonschema
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.skills import _profile_block
from app.config import get_settings
from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.llm.gateway import load_gateway_default
from app.models.user import User
from app.services.router import route_intent
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError

router = APIRouter(prefix="/chat", tags=["chat"])

SYSTEM_CHAT = (
    "你是新媒体运营系统的助理，服务对象是个人或小团队运营者。"
    "回答要具体、可执行，优先给步骤和判断标准；不确定时明确说明需要哪些信息。"
    "不要承诺发布结果，不要编造平台数据。"
)

MISSING_INPUTS_MESSAGE = "还缺少必要信息，请补充标题、目标平台与数据指标后再试"


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    profile_id: uuid.UUID | None = None


class ChatOut(BaseModel):
    reply: str
    intent: str
    skill_id: str | None = None
    run_id: str | None = None
    data: dict | None = None


def _summarize(data: dict) -> str:
    for key in ("summary", "verdict", "reply"):
        value = data.get(key)
        if value:
            return str(value)
    return "已完成，请查看结构化结果。"


@router.post("", response_model=ChatOut)
async def chat(
    payload: ChatIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> ChatOut:
    intent = route_intent(
        payload.message,
        output_dir=str(get_settings().outputs_dir / "adhoc"),
    )
    if intent.kind == "chat":
        gateway = await load_gateway_default(session, user.tenant_id)
        reply = await gateway.complete(
            [
                {"role": "system", "content": SYSTEM_CHAT},
                {"role": "user", "content": payload.message},
            ],
            tier="standard",
            temperature=0.6,
        )
        return ChatOut(reply=reply, intent="chat")

    skill_id = intent.skill_id or ""
    try:
        skill = registry.get(skill_id)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    await require_enabled(session, user.tenant_id, skill_id)
    try:
        jsonschema.validate(intent.inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError(
            "missing_inputs",
            MISSING_INPUTS_MESSAGE,
            400,
            detail={"reason": exc.message},
        ) from exc
    profile_block = await _profile_block(session, payload.profile_id, user)
    gateway = await load_gateway_default(session, user.tenant_id)
    executor = SkillExecutor(
        registry=registry,
        gateway=gateway,
        session=session,
        tenant_id=user.tenant_id,
        profile_block=profile_block,
        created_by=user.id,
    )
    try:
        result = await executor.run(skill_id, intent.inputs)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    except jsonschema.ValidationError as exc:
        raise AppError(
            "missing_inputs",
            MISSING_INPUTS_MESSAGE,
            400,
            detail={"reason": exc.message},
        ) from exc
    except ScriptError as exc:
        raise AppError("skill_script_failed", "后处理脚本执行失败", 500) from exc
    except ValueError as exc:
        raise AppError("skill_output_invalid", "模型输出格式异常", 502) from exc

    reply = result.text if result.data is None else _summarize(result.data)
    return ChatOut(
        reply=reply,
        intent="skill",
        skill_id=skill_id,
        run_id=result.run_id,
        data=result.data,
    )
