from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import schema
from app.errors import AppError
from app.models.project import Content, Project
from app.models.publish import PublishJob


def _parse_date(value: object, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise AppError("invalid_arguments", f"{field} 必须是 YYYY-MM-DD", 422) from None


async def list_calendar(ctx: ToolContext, args: dict) -> ToolResult:
    today = date.today()
    start = _parse_date(args.get("date_from") or today.isoformat(), "date_from")
    end = _parse_date(
        args.get("date_to") or (today + timedelta(days=7)).isoformat(), "date_to"
    )
    if end < start:
        raise AppError("invalid_arguments", "date_to 不能早于 date_from", 422)
    if (end - start).days > 90:
        raise AppError("invalid_arguments", "查询范围不能超过 90 天", 422)
    start_at = datetime.combine(start, time.min, tzinfo=UTC)
    end_at = datetime.combine(end + timedelta(days=1), time.min, tzinfo=UTC)
    rows = (
        await ctx.session.execute(
            select(PublishJob, Content.title, Project.title)
            .join(
                Content,
                (Content.id == PublishJob.content_id)
                & (Content.tenant_id == PublishJob.tenant_id),
            )
            .join(
                Project,
                (Project.id == Content.project_id)
                & (Project.tenant_id == Content.tenant_id),
            )
            .where(
                PublishJob.tenant_id == ctx.tenant_id,
                PublishJob.scheduled_at.is_not(None),
                PublishJob.scheduled_at >= start_at,
                PublishJob.scheduled_at < end_at,
            )
            .order_by(PublishJob.scheduled_at)
            .limit(100)
        )
    ).all()
    items = [
        {
            "job_id": str(job.id),
            "content_id": str(job.content_id),
            "content_title": content_title,
            "project_title": project_title,
            "platform": job.platform,
            "status": job.status,
            "scheduled_at": job.scheduled_at.isoformat() if job.scheduled_at else None,
        }
        for job, content_title, project_title in rows
    ]
    return ToolResult(summary=f"范围内有 {len(items)} 个发布任务", data={"items": items})


SPECS = [
    ToolSpec(
        name="calendar.list",
        description="列出指定日期范围内的发布任务。",
        input_schema=schema(
            {
                "date_from": {"type": "string", "format": "date"},
                "date_to": {"type": "string", "format": "date"},
            }
        ),
        risk="read",
        handler=list_calendar,
    )
]
