from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import owned_media_job, owned_project, schema
from app.errors import AppError
from app.models.media import MediaJob
from app.services.media import create_media_job, mark_queue_failed


def _job_artifact(job: MediaJob, title: str = "") -> dict:
    return {"type": "media_job", "id": str(job.id), "title": title}


async def get_media_job(ctx: ToolContext, args: dict) -> ToolResult:
    job = await owned_media_job(ctx.session, ctx.tenant_id, args.get("job_id"))
    return ToolResult(
        summary=f"媒体任务状态：{job.status}",
        data={
            "job": {
                "id": str(job.id),
                "project_id": str(job.project_id),
                "skill_id": job.skill_id,
                "status": job.status,
                "content_id": str(job.content_id) if job.content_id else None,
                "error_code": job.error_code,
                "error_message": job.error_message[:500],
                "attempts": job.attempts,
            }
        },
    )


async def generate_media_tool(ctx: ToolContext, args: dict) -> ToolResult:
    key = f"{ctx.run_id}:{ctx.step_index}"
    existing = (
        await ctx.session.execute(
            select(MediaJob).where(
                MediaJob.tenant_id == ctx.tenant_id,
                MediaJob.agent_key == key,
            )
        )
    ).scalars().first()
    if existing is not None:
        return ToolResult(
            summary=f"媒体任务已存在：{existing.status}",
            data={"job": {"id": str(existing.id), "status": existing.status}},
            artifacts=[_job_artifact(existing, existing.skill_id)],
        )
    project = await owned_project(ctx.session, ctx.tenant_id, args.get("project_id"))
    payload = args.get("payload") or {}
    if not isinstance(payload, dict):
        raise AppError("invalid_arguments", "payload 必须是对象", 422)
    job = await create_media_job(
        session=ctx.session,
        registry=ctx.registry,
        tenant_id=ctx.tenant_id,
        project=project,
        skill_id=str(args.get("skill_id") or ""),
        payload=dict(payload),
        created_by=ctx.user_id,
    )
    job.agent_key = key
    await ctx.session.commit()
    if ctx.queue is None:
        await mark_queue_failed(ctx.session, job)
        raise AppError("queue_unavailable", "任务队列暂不可用，请稍后重试", 503)
    try:
        await ctx.queue.enqueue_job("process_media_job", str(job.id))
    except Exception:  # noqa: BLE001 - 入队失败必须落库失败状态后再报错
        await mark_queue_failed(ctx.session, job)
        raise AppError(
            "queue_unavailable", "任务队列暂不可用，请稍后重试", 503
        ) from None
    return ToolResult(
        summary=f"已入队媒体任务「{job.skill_id}」",
        data={"job": {"id": str(job.id), "status": job.status}},
        artifacts=[_job_artifact(job, job.skill_id)],
    )


SPECS = [
    ToolSpec(
        name="media.job.get",
        description="轮询媒体生成任务的状态与产物关联。",
        input_schema=schema(
            {"job_id": {"type": "string", "minLength": 1}}, ("job_id",)
        ),
        risk="read",
        handler=get_media_job,
    ),
    ToolSpec(
        name="media.generate",
        description="为项目生成视频或音频，立即返回任务 id；结果需轮询 media.job.get。",
        input_schema=schema(
            {
                "project_id": {"type": "string", "minLength": 1},
                "skill_id": {"type": "string", "enum": ["short-video", "tts-voiceover"]},
                "payload": {"type": "object"},
            },
            ("project_id", "skill_id", "payload"),
        ),
        risk="low",
        handler=generate_media_tool,
    ),
]
