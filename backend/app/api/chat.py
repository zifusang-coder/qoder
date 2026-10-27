import json
import uuid
from pathlib import Path
from typing import Annotated

import jsonschema
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.deps import get_current_user
from app.errors import AppError
from app.llm.gateway import load_gateway_default
from app.models.user import User
from app.services.profiles import profile_block_for
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

VERDICT_LABELS = {"advance": "建议推进", "revise": "需要修改", "drop": "建议放弃"}
RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


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
    missing: list[str] | None = None


def _public_data(data: dict | None, outputs_root: Path) -> dict | None:
    if not data or "path" not in data:
        return data
    path = Path(str(data["path"]))
    try:
        rel = path.resolve().relative_to(outputs_root.resolve())
    except ValueError:
        return data
    return {**data, "path": str(rel)}


def _summarize(data: dict) -> str:
    for key in ("verdict", "summary", "reply"):
        value = data.get(key)
        if value:
            if key == "verdict" and value in VERDICT_LABELS:
                return VERDICT_LABELS[value]
            if key == "summary" and value in RISK_LABELS:
                return f"风险等级：{RISK_LABELS[value]}"
            return str(value)
    titles = data.get("titles")
    if isinstance(titles, list) and titles:
        lines = [
            item.get("text") if isinstance(item, dict) else str(item)
            for item in titles
        ]
        return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    scan = data.get("scan")
    if isinstance(scan, dict):
        level = RISK_LABELS.get(scan.get("risk_level"), scan.get("risk_level"))
        return f"风险等级：{level}；命中 {scan.get('total', 0)} 处"
    if data.get("score") is not None:
        fixes = data.get("fixes") or data.get("drift") or []
        suffix = f"；建议：{'；'.join(map(str, fixes[:3]))}" if fixes else ""
        return f"评分 {data['score']}{suffix}"
    if data.get("path"):
        return f"已生成文件：{data['path']}"
    return json.dumps(data, ensure_ascii=False)[:2000]


@router.post("", response_model=ChatOut)
async def chat(
    payload: ChatIn,
    user: User = Depends(get_current_user),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> ChatOut:
    settings = get_settings()
    output_dir = str(
        settings.outputs_dir / "adhoc" / user.tenant_id / uuid.uuid4().hex
    )
    intent = route_intent(payload.message, output_dir=output_dir)
    if intent.kind == "chat":
        profile_block = await profile_block_for(
            session, payload.profile_id, user.tenant_id
        )
        system_prompt = (
            f"{SYSTEM_CHAT}\n\n{profile_block}" if profile_block else SYSTEM_CHAT
        )
        gateway = await load_gateway_default(session, user.tenant_id)
        reply = await gateway.complete(
            [
                {"role": "system", "content": system_prompt},
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

    declared = skill.manifest.inputs.get("properties", {})
    inputs = {k: v for k, v in intent.inputs.items() if k in declared}
    missing = [
        key for key in skill.manifest.inputs.get("required", [])
        if key not in inputs or inputs[key] in (None, "", [], {})
    ]
    if missing:
        return ChatOut(
            reply=f"要执行「{skill.manifest.name}」，还需要：{'、'.join(missing)}。请补充这些信息后再试。",
            intent="skill",
            skill_id=skill_id,
            missing=missing,
        )
    try:
        jsonschema.validate(inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError(
            "missing_inputs",
            MISSING_INPUTS_MESSAGE,
            400,
            detail={"reason": exc.message},
        ) from exc
    profile_block = await profile_block_for(
        session, payload.profile_id, user.tenant_id
    )
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
        result = await executor.run(skill_id, inputs)
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

    data = _public_data(result.data, settings.outputs_dir)
    reply = result.text if data is None else _summarize(data)
    return ChatOut(
        reply=reply,
        intent="skill",
        skill_id=skill_id,
        run_id=result.run_id,
        data=data,
    )
