from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import owned_media_job, schema


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


SPECS = [
    ToolSpec(
        name="media.job.get",
        description="轮询媒体生成任务的状态与产物关联。",
        input_schema=schema(
            {"job_id": {"type": "string", "minLength": 1}}, ("job_id",)
        ),
        risk="read",
        handler=get_media_job,
    )
]
