import uuid
from typing import Annotated

import jsonschema
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session
from app.errors import AppError
from app.llm.gateway import load_gateway_default
from app.models.user import User
from app.permissions import editor_access
from app.services.outputs import (
    adhoc_output_dir,
    prepare_inputs,
    public_output_data,
)
from app.services.profiles import profile_block_for
from app.services.router import route_intent
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError

router = APIRouter(prefix="/chat", tags=["chat"])

SYSTEM_CHAT = (
    "你是云AI新媒体运营系统的助理，服务对象是个人或小团队运营者。"
    "回答要具体、可执行，优先给步骤和判断标准；不确定时明确说明需要哪些信息。"
    "不要承诺发布结果，不要编造平台数据。"
)

VERDICT_LABELS = {"advance": "建议推进", "revise": "需要修改", "drop": "建议放弃"}
RISK_LABELS = {"low": "低", "medium": "中", "high": "高"}


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
    ]
    profile_id: uuid.UUID | None = None
    inputs: dict | None = None


class ChatOut(BaseModel):
    reply: str
    intent: str
    skill_id: str | None = None
    run_id: str | None = None
    data: dict | None = None
    missing: list[str] | None = None


def _score_value(data: dict) -> str | None:
    value = data.get("score")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return None


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _summarize(data: dict) -> str:
    scan = data.get("scan")
    if isinstance(scan, dict):
        level = scan.get("risk_level")
        label = RISK_LABELS.get(level, level) if isinstance(level, str) else "未知"
        total = scan.get("total")
        if isinstance(total, int) and not isinstance(total, bool):
            return f"风险等级：{label}；命中 {total} 处"
        return f"风险等级：{label}"
    verdict = data.get("verdict")
    if isinstance(verdict, str) and verdict:
        head = VERDICT_LABELS.get(verdict, verdict)
        score = _score_value(data)
        if score is not None:
            head += f"（{score} 分）"
        reasons = data.get("reasons")
        if isinstance(reasons, list) and reasons:
            head += f"；理由：{'；'.join(str(r) for r in reasons[:3])}"
        actions = data.get("next_actions")
        if isinstance(actions, list) and actions:
            head += f"；建议：{'；'.join(str(a) for a in actions[:3])}"
        return head
    summary = data.get("summary")
    if isinstance(summary, str) and summary:
        return f"风险等级：{RISK_LABELS[summary]}" if summary in RISK_LABELS else summary
    reply = data.get("reply")
    if isinstance(reply, str) and reply:
        return reply
    titles = data.get("titles")
    if isinstance(titles, list):
        lines = []
        for item in titles:
            text = item.get("text") if isinstance(item, dict) else item
            if text is not None and str(text):
                lines.append(str(text))
        if lines:
            return "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
    score = _score_value(data)
    if score is not None:
        fixes = data.get("fixes") or data.get("drift")
        suffix = ""
        if isinstance(fixes, (list, tuple)) and fixes:
            suffix = f"；建议：{'；'.join(str(item) for item in fixes[:3])}"
        return f"评分 {score}{suffix}"
    path = data.get("path")
    if isinstance(path, str) and path:
        return f"已生成文件：{path}"
    return "已完成，请查看结构化结果。"


@router.post("", response_model=ChatOut)
async def chat(
    payload: ChatIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> ChatOut:
    settings = get_settings()
    output_dir = str(adhoc_output_dir(user.tenant_id))
    intent = route_intent(payload.message, output_dir=output_dir)
    if intent.kind == "chat":
        profile_block = await profile_block_for(
            session,
            payload.profile_id,
            user.tenant_id,
            query=payload.message,
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

    declared = skill.manifest.inputs.get("properties") or {}
    merged = {**intent.inputs, **(payload.inputs or {})}
    inputs = prepare_inputs(skill, merged, user.tenant_id)
    missing = [
        key
        for key in skill.manifest.inputs.get("required", [])
        if key not in inputs or _is_blank(inputs[key])
    ]
    profile_block = await profile_block_for(
        session,
        payload.profile_id,
        user.tenant_id,
        query=payload.message,
    )
    if missing:
        labels = []
        for key in missing:
            spec = declared.get(key)
            desc = spec.get("description") if isinstance(spec, dict) else None
            labels.append(f"{key}（{desc}）" if desc else key)
        return ChatOut(
            reply=(
                f"要执行「{skill.manifest.name}」，还需要：{'、'.join(labels)}。"
                "请通过 inputs 补充后重试。"
            ),
            intent="skill",
            skill_id=skill_id,
            missing=missing,
        )
    try:
        jsonschema.validate(inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError(
            "skill_inputs_invalid", "技能输入参数校验失败", 422
        ) from exc
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
            "skill_inputs_invalid", "技能输入参数校验失败", 422
        ) from exc
    except ScriptError as exc:
        raise AppError("skill_script_failed", "后处理脚本执行失败", 500) from exc
    except ValueError as exc:
        raise AppError("skill_output_invalid", "模型输出格式异常", 502) from exc

    data = public_output_data(result.data, settings.outputs_dir)
    reply = result.text if data is None else _summarize(data)
    return ChatOut(
        reply=reply,
        intent="skill",
        skill_id=skill_id,
        run_id=result.run_id,
        data=data,
    )
