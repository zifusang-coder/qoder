from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.agent.contracts import ToolContext, ToolResult, ToolSpec
from app.agent.tools._common import (
    int_arg,
    owned_content,
    owned_job,
    parse_uuid,
    schema,
)
from app.errors import AppError
from app.models.profile import Profile
from app.models.project import ContentVersion, Project
from app.models.publish import PlatformAccount, PublishEvent, PublishJob
from app.publish.registry import PlatformSpec, spec_for
from app.services.precheck import run_precheck


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


def _job_artifact(job: PublishJob, title: str = "") -> dict:
    return {"type": "publish_job", "id": str(job.id), "title": title}


def _platform_spec(platform: str) -> PlatformSpec:
    try:
        return spec_for(platform)
    except KeyError:
        raise AppError(
            "invalid_platform", f"不支持的发布平台：{platform}", 422
        ) from None


async def create_draft_job(ctx: ToolContext, args: dict) -> ToolResult:
    content = await owned_content(
        ctx.session, ctx.tenant_id, args.get("content_id")
    )
    platform = str(args.get("platform") or "")
    spec = _platform_spec(platform)
    if content.platform != platform:
        raise AppError("platform_mismatch", "内容平台与发布平台不一致", 409)
    if content.current_version < 1:
        raise AppError("empty_content", "内容还没有可发布的版本", 409)
    if content.kind not in spec.content_capabilities:
        raise AppError(
            "platform_content_mismatch",
            f"{spec.label}不支持该内容类型",
            409,
        )
    key = f"agent:{ctx.run_id}:{ctx.step_index}"
    title = content.title
    existing = (
        await ctx.session.execute(
            select(PublishJob).where(
                PublishJob.tenant_id == ctx.tenant_id,
                PublishJob.idempotency_key == key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return ToolResult(
            summary=f"发布草稿已存在：{existing.status}",
            data={"job": _job_row(existing)},
            artifacts=[_job_artifact(existing, title)],
        )
    version = (
        await ctx.session.execute(
            select(ContentVersion).where(
                ContentVersion.tenant_id == ctx.tenant_id,
                ContentVersion.content_id == content.id,
                ContentVersion.version == content.current_version,
            )
        )
    ).scalar_one_or_none()
    if version is None or not version.body.strip():
        raise AppError("empty_content", "当前版本正文为空，无法发布", 409)
    account_id = None
    if args.get("account_id"):
        account_id = parse_uuid(args["account_id"], "账号不存在")
        account = (
            await ctx.session.execute(
                select(PlatformAccount).where(
                    PlatformAccount.id == account_id,
                    PlatformAccount.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if account is None:
            raise AppError("not_found", "账号不存在", 404)
        if account.platform != platform:
            raise AppError("account_platform_mismatch", "账号平台不一致", 409)
        if account.status != "active":
            raise AppError("account_inactive", "账号已停用", 409)
    job = PublishJob(
        tenant_id=ctx.tenant_id,
        content_id=content.id,
        content_version=content.current_version,
        platform=platform,
        account_id=account_id,
        status="draft",
        idempotency_key=key,
        scheduled_at=None,
        checks={},
    )
    ctx.session.add(job)
    try:
        await ctx.session.flush()
        # 与人工创建路径一致：留一条创建事件，便于审计追溯
        ctx.session.add(
            PublishEvent(
                tenant_id=ctx.tenant_id,
                job_id=job.id,
                from_status="created",
                to_status="draft",
                action="create",
                actor=f"agent:{ctx.run_id}",
            )
        )
        await ctx.session.commit()
    except IntegrityError as exc:
        await ctx.session.rollback()
        job = (
            await ctx.session.execute(
                select(PublishJob).where(
                    PublishJob.tenant_id == ctx.tenant_id,
                    PublishJob.idempotency_key == key,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            raise AppError(
                "conflict", "发布草稿创建冲突，请重试", 409
            ) from exc
    await ctx.session.refresh(job)
    return ToolResult(
        summary=f"已创建发布草稿（{spec.label}），等待人工排期与审核",
        data={"job": _job_row(job)},
        artifacts=[_job_artifact(job, title)],
    )


async def precheck_job(ctx: ToolContext, args: dict) -> ToolResult:
    job = await owned_job(ctx.session, ctx.tenant_id, args.get("job_id"))
    content = await owned_content(ctx.session, ctx.tenant_id, job.content_id)
    version = (
        await ctx.session.execute(
            select(ContentVersion).where(
                ContentVersion.content_id == content.id,
                ContentVersion.version == job.content_version,
                ContentVersion.tenant_id == ctx.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if version is None:
        raise AppError("not_found", "内容版本不存在", 404)
    taboos: list[str] = []
    project = (
        await ctx.session.execute(
            select(Project).where(
                Project.id == content.project_id,
                Project.tenant_id == ctx.tenant_id,
            )
        )
    ).scalar_one_or_none()
    if project is not None and project.profile_id:
        profile = (
            await ctx.session.execute(
                select(Profile).where(
                    Profile.id == project.profile_id,
                    Profile.tenant_id == ctx.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if profile is not None:
            taboos = [str(item) for item in profile.taboos or []]
    report = run_precheck(
        version.body, platform=job.platform, taboos=taboos, title=version.title
    )
    # Read-only by contract (spec §6.2: "只返回结果"): the agent reports what the
    # precheck found, the job stays draft and a human advances it on the publish
    # page. Mutating it here would let the model move a job without a review.
    return ToolResult(
        summary=(
            f"检查通过，{report['warnings']} 个提醒"
            if report["passed"]
            else f"检查未通过，{report['errors']} 个错误"
        ),
        data={"job": _job_row(job), "report": report},
        artifacts=[_job_artifact(job, version.title)],
    )


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
    ToolSpec(
        name="publish.create_draft_job",
        description="为内容创建发布草稿；不排期、不发布，排期与真实发布必须由人工完成。",
        input_schema=schema(
            {
                "content_id": {"type": "string", "minLength": 1},
                "platform": {"type": "string", "minLength": 1, "maxLength": 32},
                "account_id": {"type": "string", "minLength": 1},
            },
            ("content_id", "platform"),
        ),
        risk="low",
        handler=create_draft_job,
    ),
    ToolSpec(
        name="publish.precheck",
        description=(
            "对发布草稿执行广告法与平台规则检查，只返回检查报告，"
            "不改变任务状态。"
        ),
        input_schema=schema(
            {"job_id": {"type": "string", "minLength": 1}}, ("job_id",)
        ),
        risk="low",
        handler=precheck_job,
    ),
]
