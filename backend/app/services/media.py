import asyncio
import math
import uuid
from pathlib import Path, PurePosixPath

import jsonschema
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.errors import AppError
from app.llm.gateway import LLMGateway, load_gateway_default
from app.models.project import Content, ContentVersion, Project
from app.services.outputs import prepare_inputs
from app.services.profiles import inputs_query, profile_block_for
from app.services.skill_settings import require_enabled
from app.skills.executor import SkillExecutor
from app.skills.manifest import ManifestError
from app.skills.registry import SkillRegistry
from app.storage import StorageBackend

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
) -> tuple[Content, ContentVersion]:
    if skill_id not in MEDIA_SKILLS:
        raise AppError("not_found", "媒体技能不存在", 404)
    try:
        skill = registry.get(skill_id)
    except ManifestError as exc:
        raise AppError("not_found", str(exc), 404) from exc
    await require_enabled(session, tenant_id, skill_id)

    inputs = prepare_inputs(skill, build_inputs(skill_id, payload), tenant_id)
    try:
        jsonschema.validate(inputs, skill.manifest.inputs)
    except jsonschema.ValidationError as exc:
        raise AppError("skill_inputs_invalid", "技能输入参数校验失败", 422) from exc

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
    result = await executor.run(skill_id, inputs)
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
    meta = {
        "run_id": result.run_id,
        "kind": kind,
        "duration_seconds": _duration(data.get("duration_seconds")),
        "files": imported,
        "preview": by_name.get(str(data.get("preview") or "")),
        "cover": by_name.get(str(data.get("cover") or "")),
        "subtitles": by_name.get(str(data.get("subtitles") or "")),
        "audio": data.get("audio") if isinstance(data.get("audio"), dict) else {},
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
