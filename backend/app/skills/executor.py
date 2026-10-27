import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jsonschema
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError
from app.llm.gateway import LLMGateway
from app.llm.types import Usage
from app.models.skill import SkillRun
from app.skills.manifest import LoadedSkill
from app.skills.registry import SkillRegistry
from app.skills.runner import run_script

logger = logging.getLogger(__name__)


@dataclass
class SkillRunResult:
    run_id: str
    skill_id: str
    text: str
    data: dict | None
    prompt_tokens: int
    completion_tokens: int
    duration_ms: int


def strip_fences(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def _repair_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("模型输出中未找到 JSON 对象")
    return json.loads(text[start : end + 1])


def declared_inputs(skill: LoadedSkill, inputs: dict) -> dict:
    properties = skill.manifest.inputs.get("properties") or {}
    return {key: value for key, value in inputs.items() if key in properties}


def parse_json_output(text: str) -> dict:
    cleaned = strip_fences(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        data = _repair_json(cleaned)
    if not isinstance(data, dict):
        # 对外契约与测试都按 ValueError 处理（解析失败），与类型错误无关
        raise ValueError("模型输出的 JSON 必须是对象")  # noqa: TRY004
    return data


class SkillExecutor:
    def __init__(
        self,
        registry: SkillRegistry,
        gateway: LLMGateway,
        session: AsyncSession,
        tenant_id: str,
        profile_block: str = "",
        created_by: uuid.UUID | None = None,
    ):
        self.registry = registry
        self.gateway = gateway
        self.session = session
        self.tenant_id = tenant_id
        self.profile_block = profile_block
        self.created_by = created_by

    def build_messages(self, skill: LoadedSkill, inputs: dict) -> list[dict[str, str]]:
        inputs = declared_inputs(skill, inputs)
        system = skill.prompt.strip()
        if skill.manifest.profile and self.profile_block:
            system += "\n\n" + self.profile_block
        system += (
            f"\n\n输出格式：{skill.manifest.output_format}。"
            "只输出最终结果，不要复述任务，也不要解释过程。"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(inputs, ensure_ascii=False)},
        ]

    async def _open_run(self, skill_id: str, inputs: dict) -> SkillRun:
        record = SkillRun(
            tenant_id=self.tenant_id,
            skill_id=skill_id,
            status="running",
            inputs=inputs,
            outputs={},
            created_by=self.created_by,
        )
        self.session.add(record)
        await self.session.commit()
        await self.session.refresh(record)
        return record

    def _usage(self) -> Usage:
        usage = getattr(self.gateway, "last_usage", None)
        return usage if isinstance(usage, Usage) else Usage()

    async def _fail_run(
        self, run_id: uuid.UUID, started: float, exc: BaseException
    ) -> None:
        """Persist a failed/cancelled audit row without masking the original error."""
        try:
            try:
                await self.session.rollback()
            except Exception:
                logger.exception("技能运行审计回滚失败 run_id=%s", run_id)
            record = (
                await self.session.execute(
                    select(SkillRun).where(
                        SkillRun.id == run_id,
                        SkillRun.tenant_id == self.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            if record is None:
                return
            usage = self._usage()
            record.status = (
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
            )
            record.outputs = {}
            message = (
                f"{exc.code}: {exc.message}"
                if isinstance(exc, AppError)
                else str(exc)
            )
            record.error = message[:2000]
            record.prompt_tokens = usage.prompt_tokens
            record.completion_tokens = usage.completion_tokens
            record.duration_ms = int((time.monotonic() - started) * 1000)
            record.finished_at = datetime.now(UTC)
            await self.session.commit()
        except Exception:
            logger.exception("技能运行审计保存失败 run_id=%s", run_id)

    async def run(self, skill_id: str, inputs: dict) -> SkillRunResult:
        skill = self.registry.get(skill_id)
        inputs = declared_inputs(skill, inputs)
        started = time.monotonic()
        record = await self._open_run(skill_id, inputs)
        run_id = record.id
        try:
            jsonschema.validate(inputs, skill.manifest.inputs)
            messages = self.build_messages(skill, inputs)
            text = await self.gateway.complete(
                messages,
                tier=skill.manifest.llm_tier,
                temperature=skill.manifest.temperature,
            )
            data = (
                parse_json_output(text)
                if skill.manifest.output_format == "json"
                else None
            )
            if skill.manifest.postprocess:
                data = await run_script(
                    skill.manifest.dir / skill.manifest.postprocess,
                    {"inputs": inputs, "output": data if data is not None else text},
                    skill.manifest.dir,
                )
                text = json.dumps(data, ensure_ascii=False)
            usage = self._usage()
            duration = int((time.monotonic() - started) * 1000)
            record.status = "succeeded"
            record.outputs = {"text": text, "data": data}
            record.prompt_tokens = usage.prompt_tokens
            record.completion_tokens = usage.completion_tokens
            record.duration_ms = duration
            record.finished_at = datetime.now(UTC)
            await self.session.commit()
            return SkillRunResult(
                run_id=str(record.id),
                skill_id=skill_id,
                text=text,
                data=data,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                duration_ms=duration,
            )
        except BaseException as exc:
            await self._fail_run(run_id, started, exc)
            raise
