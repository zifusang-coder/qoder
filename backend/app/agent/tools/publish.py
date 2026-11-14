from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import int_arg, owned_content, owned_job, schema
from app.models.publish import PublishJob


def _job_row(job: PublishJob) -> dict:
    return {
        "id": str(job.id),
        "content_id": str(job.content_id),
        "content_version": job.content_version,
        "platform": job.platform,
        "account_id": str(job.account_id) if job.account_id else None,
        "status": job.status,
        "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
        "published_at": job.published_at.isoformat() if job.published_at else None,
        "external_url": job.external_url,
        "error": job.error[:500],
        "export_path": job.export_path,
    }


async def list_jobs(ctx: ToolContext, args: dict) -> ToolResult:
    limit = int_arg(args, "limit", default=20, minimum=1, maximum=50)
    statement = select(PublishJob).where(PublishJob.tenant_id == ctx.tenant_id)
    if args.get("status"):
        statement = statement.where(PublishJob.status == args["status"])
    if args.get("content_id"):
        # content_id 是 Uuid 列：先按租户解析成 Content 再比较，避免原始字符串直达驱动
        content = await owned_content(
            ctx.session, ctx.tenant_id, args["content_id"]
        )
        statement = statement.where(PublishJob.content_id == content.id)
    rows = (
        await ctx.session.execute(
            statement.order_by(PublishJob.created_at.desc()).limit(limit)
        )
    ).scalars().all()
    items = [_job_row(job) for job in rows]
    return ToolResult(summary=f"找到 {len(items)} 个发布任务", data={"items": items})


async def get_job(ctx: ToolContext, args: dict) -> ToolResult:
    job = await owned_job(ctx.session, ctx.tenant_id, args.get("job_id"))
    return ToolResult(summary=f"发布任务状态：{job.status}", data={"job": _job_row(job)})


SPECS = [
    ToolSpec(
        name="publish.job.list",
        description="列出发布任务及其状态。",
        input_schema=schema(
            {
                "status": {"type": "string", "maxLength": 32},
                "content_id": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }
        ),
        risk="read",
        handler=list_jobs,
    ),
    ToolSpec(
        name="publish.job.get",
        description="读取单个发布任务的状态与错误信息。",
        input_schema=schema(
            {"job_id": {"type": "string", "minLength": 1}}, ("job_id",)
        ),
        risk="read",
        handler=get_job,
    ),
]
