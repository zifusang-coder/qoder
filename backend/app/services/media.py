import asyncio
import logging
import math
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import jsonschema
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.media import MediaJob
from app.models.project import Content, ContentVersion, Project
from app.models.tenant import Tenant
from app.services.outputs import prepare_inputs
from app.services.profiles import inputs_query, profile_block_for
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import LoadedSkill, ManifestError
from app.skills.registry import SkillRegistry
from app.storage import StorageBackend

logger = logging.getLogger(__name__)

MEDIA_SKILLS = {"short-video", "tts-voiceover"}
MEDIA_KINDS = {"video", "audio"}
FILE_KINDS = {"video", "audio", "image", "subtitles", "text"}
ALLOWED_SUFFIXES = {".mp4", ".mp3", ".png", ".jpg", ".jpeg", ".webp", ".srt", ".txt"}

_INPUT_KEYS = {
    "short-video": (
        "topic",
        "platform",
        "duration_seconds",
        "voice",
        "voice_name",
        "theme",
        "instructions",
    ),
    "tts-voiceover": ("text", "title", "voice_name", "rate", "instructions"),
}


def build_inputs(skill_id: str, payload: dict) -> dict:
    keys = _INPUT_KEYS.get(skill_id, ())
    inputs = {
        key: payload[key]
        for key in keys
        if key in payload and payload[key] is not None
    }
    required = "topic" if skill_id == "short-video" else "text"
    if not str(inputs.get(required, "")).strip():
        message = "请填写视频主题" if required == "topic" else "请填写口播稿"
        raise AppError("skill_inputs_invalid", message, 422)
    return inputs


def _resolve_skill(registry: SkillRegistry, skill_id: str) -> LoadedSkill:
    if skill_id not in MEDIA_SKILLS:
        raise AppError("not_found", "媒体技能不存在", 404)
    try:
        return registry.get(skill_id)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc


def _validated_inputs(
    skill_id: str, skill: LoadedSkill, payload: dict, tenant_id: str
) -> dict:
    inputs = prepare_inputs(skill, build_inputs(skill_id, payload), tenant_id)
    try:
        jsonschema.validate(inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError("skill_inputs_invalid", "技能输入参数校验失败", 422) from exc
    return inputs


def _media_name(raw: object) -> str:
    base = PurePosixPath(str(raw or "").replace("\\", "/")).name
    if (
        not base
        or base in {".", ".."}
        or "\x00" in base
        or any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in base)
        or len(base) > 120
        or Path(base).suffix.lower() not in ALLOWED_SUFFIXES
    ):
        raise AppError("skill_output_invalid", "技能返回的媒体文件名不合法", 502)
    return base


def _output_path(raw: object, outputs_root: Path) -> Path:
    candidate = Path(str(raw or ""))
    target = (
        candidate.resolve()
        if candidate.is_absolute()
        else (outputs_root / candidate).resolve()
    )
    if not target.is_relative_to(outputs_root) or not target.is_file():
        raise AppError("skill_output_invalid", "技能返回的媒体文件不可用", 502)
    return target


def _duration(raw: object) -> float:
    if isinstance(raw, bool):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return round(value, 3) if math.isfinite(value) and value > 0 else 0.0


async def generate_media(
    *,
    session: AsyncSession,
    registry: SkillRegistry,
    storage: StorageBackend,
    tenant_id: str,
    project: Project,
    skill_id: str,
    payload: dict,
    gateway: LLMGateway | None = None,
    created_by: uuid.UUID | None = None,
    fallback: dict | None = None,
) -> tuple[Content, ContentVersion]:
    skill = _resolve_skill(registry, skill_id)
    await require_enabled(session, tenant_id, skill_id)

    inputs = _validated_inputs(skill_id, skill, payload, tenant_id)

    project_id = project.id
    profile_id = project.profile_id
    project_title = project.title
    profile_block = ""
    if skill.manifest.profile:
        profile_block = await profile_block_for(
            session,
            profile_id,
            tenant_id,
            query=inputs_query(inputs),
        )

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
    result = await executor.run(skill_id, inputs, fallback=fallback)
    data = result.data or {}

    raw_files = data.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise AppError("skill_output_invalid", "技能未返回媒体文件", 502)

    outputs_root = get_settings().outputs_dir.resolve()
    imported: list[dict] = []
    by_name: dict[str, str] = {}
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise AppError("skill_output_invalid", "技能返回的文件信息不合法", 502)
        name = _media_name(item.get("name"))
        if name in seen:
            raise AppError("skill_output_invalid", "技能返回了重复的文件名", 502)
        seen.add(name)
        source = _output_path(item.get("path"), outputs_root)
        blob = await asyncio.to_thread(source.read_bytes)
        kind = str(item.get("kind") or "text")
        relpath = await asyncio.to_thread(
            storage.save_bytes,
            str(project_id),
            f"media/{result.run_id}/{name}",
            blob,
        )
        imported.append(
            {
                "name": name,
                "path": relpath,
                "kind": kind if kind in FILE_KINDS else "text",
                "size": len(blob),
            }
        )
        by_name[name] = relpath

    kind = str(data.get("kind") or "")
    if kind not in MEDIA_KINDS:
        kind = "video" if skill_id == "short-video" else "audio"
    title = str(data.get("title") or project_title or "").strip()[:300]
    body = str(data.get("narration") or data.get("text") or "")
    audio = data.get("audio") if isinstance(data.get("audio"), dict) else {}
    if result.llm_fallback:
        audio = {**audio, "llm_fallback": result.llm_fallback}
    meta = {
        "run_id": result.run_id,
        "kind": kind,
        "duration_seconds": _duration(data.get("duration_seconds")),
        "files": imported,
        "preview": by_name.get(str(data.get("preview") or "")),
        "cover": by_name.get(str(data.get("cover") or "")),
        "subtitles": by_name.get(str(data.get("subtitles") or "")),
        "audio": audio,
    }

    record = Content(
        tenant_id=tenant_id,
        project_id=project_id,
        platform=str(payload.get("platform") or "douyin"),
        kind=kind,
        title=title,
        current_version=1,
    )
    session.add(record)
    await session.flush()
    version = ContentVersion(
        tenant_id=tenant_id,
        content_id=record.id,
        version=1,
        title=title,
        body=body,
        source="skill",
        skill_run_id=result.run_id,
        meta=meta,
    )
    session.add(version)
    await session.commit()
    await session.refresh(record)
    await session.refresh(version)
    return record, version


async def create_media_job(
    *,
    session: AsyncSession,
    registry: SkillRegistry,
    tenant_id: str,
    project: Project,
    skill_id: str,
    payload: dict,
    created_by: uuid.UUID | None = None,
    agent_key: str = "",
) -> MediaJob:
    skill = _resolve_skill(registry, skill_id)
    await require_enabled(session, tenant_id, skill_id)
    _validated_inputs(skill_id, skill, payload, tenant_id)
    job = MediaJob(
        tenant_id=tenant_id,
        project_id=project.id,
        skill_id=skill_id,
        status="queued",
        payload=payload,
        created_by=created_by,
        agent_key=agent_key,
    )
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def mark_queue_failed(session: AsyncSession, job: MediaJob) -> None:
    job.status = "failed"
    job.error_code = "queue_unavailable"
    job.error_message = "任务队列暂不可用，请稍后重试"
    job.finished_at = datetime.now(UTC)
    await session.commit()


async def retry_media_job(
    *, session: AsyncSession, job_id: str, tenant_id: str
) -> MediaJob:
    job = await _load_job(session, job_id)
    if job.tenant_id != tenant_id:
        raise AppError("not_found", "媒体任务不存在", 404)
    if job.status != "failed":
        raise AppError("invalid_state", "只有失败的任务可以重试", 409)
    job.status = "queued"
    job.error_code = ""
    job.error_message = ""
    job.started_at = None
    job.finished_at = None
    await session.commit()
    await session.refresh(job)
    return job


async def _load_job(session: AsyncSession, job_id: str) -> MediaJob:
    try:
        parsed = uuid.UUID(str(job_id))
    except (ValueError, TypeError):
        raise AppError("not_found", "媒体任务不存在", 404) from None
    job = await session.get(MediaJob, parsed)
    if job is None:
        raise AppError("not_found", "媒体任务不存在", 404)
    return job


def _tts_fallback(job: MediaJob, project: Project, payload: dict) -> dict | None:
    if job.skill_id != "tts-voiceover":
        return None
    return {
        "title": str(payload.get("title") or project.title or "").strip()[:300],
        "narration": str(payload.get("text") or ""),
    }


async def run_media_job(
    *,
    session: AsyncSession,
    job_id: str,
    registry: SkillRegistry,
    storage: StorageBackend,
    gateway: LLMGateway | None = None,
) -> MediaJob:
    job = await _load_job(session, job_id)
    if job.status == "succeeded":
        return job

    tenant = await session.get(Tenant, job.tenant_id)
    if tenant is None or tenant.status != "active":
        job.status = "failed"
        job.error_code = "tenant_suspended"
        job.error_message = "租户已停用，任务未执行"
        job.finished_at = datetime.now(UTC)
        await session.commit()
        return job

    job.status = "running"
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    job.finished_at = None
    job.error_code = ""
    job.error_message = ""
    await session.commit()
    job_uuid = job.id

    payload = job.payload if isinstance(job.payload, dict) else {}
    try:
        project = await session.get(Project, job.project_id)
        if project is None or project.tenant_id != job.tenant_id:
            raise AppError("project_missing", "项目不存在", 404)
        content, version = await generate_media(
            session=session,
            registry=registry,
            storage=storage,
            tenant_id=job.tenant_id,
            project=project,
            skill_id=job.skill_id,
            payload=payload,
            gateway=gateway,
            created_by=job.created_by,
            fallback=_tts_fallback(job, project, payload),
        )
    except AppError as exc:
        await session.rollback()
        failed = await session.get(MediaJob, job_uuid)
        if failed is not None:
            failed.status = "failed"
            failed.error_code = exc.code
            failed.error_message = exc.message[:2000]
            failed.finished_at = datetime.now(UTC)
            await session.commit()
        return failed
    except Exception:
        logger.exception("媒体任务执行异常 job=%s", job_uuid)
        await session.rollback()
        failed = await session.get(MediaJob, job_uuid)
        if failed is not None:
            failed.status = "failed"
            failed.error_code = "internal_error"
            failed.error_message = "媒体生成失败，请稍后重试"
            failed.finished_at = datetime.now(UTC)
            await session.commit()
        return failed

    job.status = "succeeded"
    job.content_id = content.id
    job.skill_run_id = version.skill_run_id
    job.finished_at = datetime.now(UTC)
    await session.commit()
    await session.refresh(job)
    return job
