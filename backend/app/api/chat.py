import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

import jsonschema
from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.config import Settings, get_settings
from app.db import SessionLocal, get_session
from app.errors import AppError
from app.llm.gateway import load_gateway_default
from app.models.chat import ChatConversation, ChatMessage
from app.models.user import User
from app.permissions import editor_access
from app.services.outputs import (
    adhoc_output_dir,
    prepare_inputs,
    public_output_data,
)
from app.services.profiles import profile_block_for
from app.services.router import Intent, route_intent
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry, get_registry
from app.skills.runner import ScriptError

router = APIRouter(prefix="/chat", tags=["chat"])
logger = logging.getLogger(__name__)

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
    conversation_id: uuid.UUID | None = None
    inputs: dict | None = None


class ChatOut(BaseModel):
    reply: str
    intent: str
    skill_id: str | None = None
    run_id: str | None = None
    data: dict | None = None
    missing: list[str] | None = None
    conversation_id: str | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    profile_id: uuid.UUID | None = None
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationOut]


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    intent: str | None = None
    skill_id: str | None = None
    run_id: str | None = None
    data: dict | None = None
    missing: list[str] | None = None
    created_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[ChatMessageOut]


HISTORY_LIMIT = 10


async def _owned_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    user: User,
    *,
    lock: bool = False,
) -> ChatConversation:
    stmt = select(ChatConversation).where(
        ChatConversation.id == conversation_id,
        ChatConversation.tenant_id == user.tenant_id,
        ChatConversation.user_id == user.id,
    )
    if lock:
        # 写入路径锁住会话行，使同一对话的并发发送串行分配 seq；
        # 唯一约束是数据库层兜底。
        stmt = stmt.with_for_update()
    conversation = (await session.execute(stmt)).scalar_one_or_none()
    if conversation is None:
        raise AppError("not_found", "对话不存在", 404)
    return conversation


async def _history(session: AsyncSession, conversation_id: uuid.UUID) -> list[dict]:
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation_id)
            .order_by(ChatMessage.seq.desc())
            .limit(HISTORY_LIMIT)
        )
    ).scalars()
    return [{"role": row.role, "content": row.content} for row in reversed(list(rows))]


async def _last_seq(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    value = await session.scalar(
        select(func.max(ChatMessage.seq)).where(
            ChatMessage.conversation_id == conversation_id
        )
    )
    return value or 0


def _add_message(
    session: AsyncSession,
    conversation: ChatConversation,
    *,
    seq: int,
    role: str,
    content: str,
    intent: str | None = None,
    skill_id: str | None = None,
    run_id: str | None = None,
    data: dict | None = None,
    missing: list[str] | None = None,
) -> ChatMessage:
    message = ChatMessage(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        seq=seq,
        role=role,
        content=content,
        intent=intent,
        skill_id=skill_id,
        run_id=run_id,
        data=data,
        missing=missing,
    )
    session.add(message)
    conversation.updated_at = datetime.now(UTC)
    return message


@router.get("/conversations", response_model=ConversationList)
async def list_conversations(
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ConversationList:
    rows = (
        await session.execute(
            select(ChatConversation)
            .where(
                ChatConversation.tenant_id == user.tenant_id,
                ChatConversation.user_id == user.id,
            )
            .order_by(ChatConversation.updated_at.desc())
            .limit(50)
        )
    ).scalars()
    return ConversationList(
        items=[
            ConversationOut(
                id=row.id,
                title=row.title,
                profile_id=row.profile_id,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> ConversationDetail:
    conversation = await _owned_conversation(session, conversation_id, user)
    rows = (
        await session.execute(
            select(ChatMessage)
            .where(ChatMessage.conversation_id == conversation.id)
            .order_by(ChatMessage.seq)
        )
    ).scalars()
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        profile_id=conversation.profile_id,
        updated_at=conversation.updated_at,
        messages=[
            ChatMessageOut(
                id=row.id,
                role=row.role,
                content=row.content,
                intent=row.intent,
                skill_id=row.skill_id,
                run_id=row.run_id,
                data=row.data,
                missing=row.missing,
                created_at=row.created_at,
            )
            for row in rows
        ],
    )


@router.delete("/conversations/{conversation_id}", status_code=204)
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    conversation = await _owned_conversation(session, conversation_id, user)
    await session.delete(conversation)
    await session.commit()
    return Response(status_code=204)


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


async def _open_conversation(
    session: AsyncSession,
    user: User,
    payload: ChatIn,
    *,
    lock: bool = False,
) -> tuple[ChatConversation, list[dict], str, int]:
    conversation: ChatConversation | None = None
    history: list[dict] = []
    if payload.conversation_id is not None:
        conversation = await _owned_conversation(
            session, payload.conversation_id, user, lock=lock
        )
        history = await _history(session, conversation.id)

    profile_id = payload.profile_id
    if profile_id is None and conversation is not None:
        profile_id = conversation.profile_id
    # 画像校验放在落库之前，未知画像不应留下空对话
    profile_block = await profile_block_for(
        session,
        profile_id,
        user.tenant_id,
        query=payload.message,
    )

    if conversation is None:
        conversation = ChatConversation(
            tenant_id=user.tenant_id,
            user_id=user.id,
            title=payload.message.splitlines()[0][:100],
            profile_id=payload.profile_id,
        )
        session.add(conversation)
        await session.flush()
        seq = 0
    else:
        if payload.profile_id is not None:
            conversation.profile_id = payload.profile_id
        seq = await _last_seq(session, conversation.id)
    return conversation, history, profile_block, seq


async def _skill_turn(
    session: AsyncSession,
    user: User,
    registry: SkillRegistry,
    settings: Settings,
    conversation: ChatConversation,
    seq: int,
    profile_block: str,
    payload: ChatIn,
    intent: Intent,
) -> ChatOut:
    conversation_id = str(conversation.id)
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
    if missing:
        labels = []
        for key in missing:
            spec = declared.get(key)
            desc = spec.get("description") if isinstance(spec, dict) else None
            labels.append(f"{key}（{desc}）" if desc else key)
        reply = (
            f"要执行「{skill.manifest.name}」，还需要：{'、'.join(labels)}。"
            "请通过 inputs 补充后重试。"
        )
        _add_message(
            session,
            conversation,
            seq=seq + 2,
            role="assistant",
            content=reply,
            intent="skill",
            skill_id=skill_id,
            missing=missing,
        )
        await session.commit()
        return ChatOut(
            reply=reply,
            intent="skill",
            skill_id=skill_id,
            missing=missing,
            conversation_id=conversation_id,
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
    _add_message(
        session,
        conversation,
        seq=seq + 2,
        role="assistant",
        content=reply,
        intent="skill",
        skill_id=skill_id,
        run_id=str(result.run_id),
        data=data,
    )
    await session.commit()
    return ChatOut(
        reply=reply,
        intent="skill",
        skill_id=skill_id,
        run_id=result.run_id,
        data=data,
        conversation_id=conversation_id,
    )


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

    conversation, history, profile_block, seq = await _open_conversation(
        session, user, payload, lock=True
    )
    conversation_id = str(conversation.id)

    _add_message(
        session,
        conversation,
        seq=seq + 1,
        role="user",
        content=payload.message,
    )

    if intent.kind == "chat":
        system_prompt = (
            f"{SYSTEM_CHAT}\n\n{profile_block}" if profile_block else SYSTEM_CHAT
        )
        gateway = await load_gateway_default(session, user.tenant_id)
        reply = await gateway.complete(
            [
                {"role": "system", "content": system_prompt},
                *history,
                {"role": "user", "content": payload.message},
            ],
            tier="standard",
            temperature=0.6,
        )
        _add_message(
            session,
            conversation,
            seq=seq + 2,
            role="assistant",
            content=reply,
            intent="chat",
        )
        await session.commit()
        return ChatOut(reply=reply, intent="chat", conversation_id=conversation_id)

    return await _skill_turn(
        session,
        user,
        registry,
        settings,
        conversation,
        seq,
        profile_block,
        payload,
        intent,
    )


@router.post("/stream")
async def stream_chat(
    payload: ChatIn,
    user: User = Depends(editor_access),  # noqa: B008
    session: AsyncSession = Depends(get_session),  # noqa: B008
    registry: SkillRegistry = Depends(get_registry),  # noqa: B008
) -> EventSourceResponse:
    settings = get_settings()
    tenant_id = user.tenant_id
    output_dir = str(adhoc_output_dir(tenant_id))
    intent = route_intent(payload.message, output_dir=output_dir)

    conversation, history, profile_block, seq = await _open_conversation(
        session, user, payload, lock=True
    )
    conversation_id = str(conversation.id)
    _add_message(
        session,
        conversation,
        seq=seq + 1,
        role="user",
        content=payload.message,
    )

    if intent.kind != "chat":
        # 技能调用不是逐字生成：执行完成后一次给出完整结果，
        # 前端仍只需处理 done 事件。
        result = await _skill_turn(
            session,
            user,
            registry,
            settings,
            conversation,
            seq,
            profile_block,
            payload,
            intent,
        )

        async def skill_events() -> AsyncIterator[dict[str, str]]:
            yield {
                "data": json.dumps(
                    {"done": result.model_dump()}, ensure_ascii=False
                )
            }
            yield {"data": "[DONE]"}

        return EventSourceResponse(skill_events())

    system_prompt = (
        f"{SYSTEM_CHAT}\n\n{profile_block}" if profile_block else SYSTEM_CHAT
    )
    gateway = await load_gateway_default(session, tenant_id)
    messages = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": payload.message},
    ]
    # 先提交用户消息：流式期间客户端刷新也能看到这条，且 seq 已确定。
    await session.commit()
    conversation_uuid = conversation.id
    assistant_seq = seq + 2

    async def event_gen() -> AsyncIterator[dict[str, str]]:
        chunks: list[str] = []
        try:
            async for delta in gateway.stream(
                messages, tier="standard", temperature=0.6
            ):
                chunks.append(delta)
                yield {
                    "data": json.dumps({"delta": delta}, ensure_ascii=False)
                }
            reply = "".join(chunks)
            # 流的生命周期长于请求依赖，落库使用独立会话。
            async with SessionLocal() as write_session:
                stored = await write_session.get(
                    ChatConversation, conversation_uuid
                )
                if stored is not None:
                    _add_message(
                        write_session,
                        stored,
                        seq=assistant_seq,
                        role="assistant",
                        content=reply,
                        intent="chat",
                    )
                    await write_session.commit()
            done = ChatOut(
                reply=reply, intent="chat", conversation_id=conversation_id
            )
            yield {
                "data": json.dumps(
                    {"done": done.model_dump()}, ensure_ascii=False
                )
            }
        except AppError as exc:
            yield {
                "data": json.dumps(
                    {"error": {"code": exc.code, "message": exc.message}},
                    ensure_ascii=False,
                )
            }
        except Exception:
            logger.exception(
                "对话流式生成失败 conversation_id=%s", conversation_id
            )
            yield {
                "data": json.dumps(
                    {
                        "error": {
                            "code": "chat_stream_failed",
                            "message": "流式生成失败",
                        }
                    },
                    ensure_ascii=False,
                )
            }
        yield {"data": "[DONE]"}

    return EventSourceResponse(event_gen())
